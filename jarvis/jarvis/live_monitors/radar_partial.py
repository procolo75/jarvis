"""
live_monitors/radar_partial.py
───────────────────────────────
Read only the rows of a DPC radar GeoTIFF that the running monitors look at.

Why
───
Every product is the whole country, 1200×1400 px at 1 km, and the monitors only
ever look at a window of a couple of hundred kilometres around their origin. Two
products every five minutes meant ~7 GB a month, almost all of it rows that were
decoded and thrown away. The files sit on S3, which honours HTTP Range, and are
written one row per strip, so the rows a monitor needs are a contiguous run of
bytes that can be fetched on their own.

What comes back is NOT a cropped raster
───────────────────────────────────────
The result has the full grid's shape, with every row that was not fetched set to
the product's own fill value. Downstream nothing changes — same geotransform, same
indices, and a row that was not fetched reads as "no measurement", never as "no
rain": the distinction the whole monitor is built on (see `radar_core.sample`).
The caller decides which rows it needs and must keep every disc it reasons about
inside them.

How
───
1. The TIFF header and IFD are read with small Range requests.
2. The strips covering the wanted rows are fetched, one request per run of rows.
3. A compact TIFF holding just those strips is assembled — same byte order, same
   compression and predictor, same GeoTIFF tags — and decoded by Pillow, exactly
   as the full file is. No decoder of our own: LZW and the floating-point
   predictor stay Pillow's job.
4. The decoded rows are pasted into a full-size array of fill values.

Anything unexpected — a tiled file, BigTIFF, a server that ignores Range — raises
`PartialUnsupported`, and the caller falls back to downloading the whole file.
"""

from __future__ import annotations

import io
import re
import struct

import numpy as np

# The published fill value; `radar_core.NODATA_THRESHOLD` treats it as no data.
FILL_VALUE = -9999.0

# A header + IFD read. The IFD of these files is ~12 KB at the END of the file,
# so a first read of this size usually also brings the strip offset/count arrays.
_IFD_READ = 32 * 1024

_TAG_WIDTH, _TAG_HEIGHT = 256, 257
_TAG_COMPRESSION = 259
_TAG_STRIP_OFFSETS, _TAG_ROWS_PER_STRIP, _TAG_STRIP_COUNTS = 273, 278, 279
_TAG_TILE_WIDTH = 322

# Copied verbatim into the compact file. Only tags whose value is self-contained:
# anything holding a file offset (SubIFDs, Exif, ...) would point into nowhere.
_COPY_TAGS = {
    254,    # NewSubfileType
    258,    # BitsPerSample
    259,    # Compression
    262,    # PhotometricInterpretation
    277,    # SamplesPerPixel
    284,    # PlanarConfiguration
    317,    # Predictor
    338,    # ExtraSamples
    339,    # SampleFormat
    340, 341,  # SMin/SMaxSampleValue
    33550,  # ModelPixelScale
    33922,  # ModelTiepoint
    34264,  # ModelTransformation
    34735, 34736, 34737,  # GeoKeyDirectory, GeoDoubleParams, GeoAsciiParams
    42112, 42113,  # GDAL_METADATA, GDAL_NODATA
}

_TYPE_SIZE = {1: 1, 2: 1, 3: 2, 4: 4, 5: 8, 6: 1, 7: 1, 8: 2, 9: 4, 10: 8,
              11: 4, 12: 8, 16: 8, 17: 8, 18: 8}
_TYPE_FMT = {3: "H", 4: "I", 8: "h", 9: "i", 16: "Q"}

_CONTENT_RANGE = re.compile(r"bytes (\d+)-(\d+)/(\d+|\*)")


class PartialUnsupported(Exception):
    """The file or the server cannot be read by rows; download it whole."""


class _Entry:
    __slots__ = ("tag", "type", "count", "raw")

    def __init__(self, tag: int, type_: int, count: int, raw: bytes):
        self.tag, self.type, self.count, self.raw = tag, type_, count, raw


class _RangeReader:
    """Byte ranges of one URL, cached so overlapping reads cost nothing."""

    def __init__(self, client, url: str):
        self._client = client
        self._url = url
        self._spans: list[tuple[int, bytes]] = []
        self.size: int | None = None
        self.fetched = 0

    async def read(self, start: int, end: int) -> bytes:
        """Bytes [start, end). Fetches only when no cached span holds them."""
        for s, data in self._spans:
            if s <= start and end <= s + len(data):
                return data[start - s:end - s]
        last = end - 1
        if self.size is not None:
            last = min(last, self.size - 1)
        if last < start:
            raise PartialUnsupported(f"read past end of file ({start}-{end})")
        # The presigned URL carries its own authentication: no extra headers.
        response = await self._client.get(self._url, headers={"Range": f"bytes={start}-{last}"})
        if response.status_code != 206:
            raise PartialUnsupported(f"no Range support (HTTP {response.status_code})")
        m = _CONTENT_RANGE.match(response.headers.get("Content-Range", ""))
        if not m or int(m.group(1)) != start:
            raise PartialUnsupported("unexpected Content-Range")
        if m.group(3) != "*":
            self.size = int(m.group(3))
        data = bytes(response.content)
        self.fetched += len(data)
        self._spans.append((start, data))
        if len(data) < min(end, self.size or end) - start:
            raise PartialUnsupported("short read")
        return data[:end - start]


def _values(entry: _Entry, order: str) -> tuple[int, ...]:
    fmt = _TYPE_FMT.get(entry.type)
    if fmt is None:
        raise PartialUnsupported(f"tag {entry.tag} has non-integer type {entry.type}")
    return struct.unpack(f"{order}{entry.count}{fmt}", entry.raw)


async def _read_ifd(reader: _RangeReader) -> tuple[str, dict[int, _Entry]]:
    head = await reader.read(0, 8)
    if head[:2] == b"II":
        order = "<"
    elif head[:2] == b"MM":
        order = ">"
    else:
        raise PartialUnsupported("not a TIFF")
    magic, ifd_offset = struct.unpack(f"{order}HI", head[2:8])
    if magic != 42:
        raise PartialUnsupported("BigTIFF or unknown TIFF variant")

    # One generous read around the IFD: it usually brings the value arrays too.
    size = reader.size
    start = ifd_offset
    end = ifd_offset + _IFD_READ if size is None else min(size, ifd_offset + _IFD_READ)
    await reader.read(start, end)

    (count,) = struct.unpack(f"{order}H", await reader.read(ifd_offset, ifd_offset + 2))
    table = await reader.read(ifd_offset + 2, ifd_offset + 2 + 12 * count)
    entries: dict[int, _Entry] = {}
    for i in range(count):
        tag, type_, n = struct.unpack(f"{order}HHI", table[12 * i:12 * i + 8])
        size_one = _TYPE_SIZE.get(type_)
        if size_one is None:
            raise PartialUnsupported(f"tag {tag}: unknown type {type_}")
        nbytes = size_one * n
        field = table[12 * i + 8:12 * i + 12]
        if nbytes <= 4:
            raw = field[:nbytes]
        else:
            (offset,) = struct.unpack(f"{order}I", field)
            raw = await reader.read(offset, offset + nbytes)
        entries[tag] = _Entry(tag, type_, n, raw)
    return order, entries


def merge_rows(intervals, rows: int, gap: int = 32) -> list[tuple[int, int]]:
    """Clip half-open row intervals to [0, rows) and merge those closer than `gap`."""
    clipped = sorted((max(0, int(a)), min(rows, int(b))) for a, b in intervals)
    merged: list[list[int]] = []
    for a, b in clipped:
        if a >= b:
            continue
        if merged and a <= merged[-1][1] + gap:
            merged[-1][1] = max(merged[-1][1], b)
        else:
            merged.append([a, b])
    return [(a, b) for a, b in merged]


def _compact_tiff(order: str, entries: dict[int, _Entry], width: int,
                  rows_per_strip: int, strips: list[bytes], n_rows: int) -> bytes:
    """A valid single-IFD TIFF holding `strips` as consecutive strips."""
    out_entries: list[tuple[int, int, int, bytes]] = []
    for tag, e in entries.items():
        if tag in _COPY_TAGS:
            out_entries.append((tag, e.type, e.count, e.raw))
    long_ = lambda v: struct.pack(f"{order}I", v)  # noqa: E731
    out_entries.append((_TAG_WIDTH, 4, 1, long_(width)))
    out_entries.append((_TAG_HEIGHT, 4, 1, long_(n_rows)))
    out_entries.append((_TAG_ROWS_PER_STRIP, 4, 1, long_(rows_per_strip)))
    counts = [len(s) for s in strips]
    out_entries.append((_TAG_STRIP_COUNTS, 4, len(counts),
                        struct.pack(f"{order}{len(counts)}I", *counts)))
    # Offsets are filled once the layout is known.
    out_entries.append((_TAG_STRIP_OFFSETS, 4, len(strips), b""))
    out_entries.sort(key=lambda t: t[0])

    n = len(out_entries)
    ifd_offset = 8
    ifd_size = 2 + 12 * n + 4
    data_start = ifd_offset + ifd_size
    # Out-of-line values first, then the strips.
    blobs: list[bytes] = []
    value_offsets: dict[int, int] = {}
    cursor = data_start
    for tag, type_, count, raw in out_entries:
        nbytes = _TYPE_SIZE[type_] * count
        if tag == _TAG_STRIP_OFFSETS:
            nbytes = 4 * count
        if nbytes > 4:
            if cursor % 2:
                blobs.append(b"\0")
                cursor += 1
            value_offsets[tag] = cursor
            cursor += nbytes
            blobs.append(b"" if tag == _TAG_STRIP_OFFSETS else raw)
            if tag == _TAG_STRIP_OFFSETS:
                blobs[-1] = None  # placeholder, patched below
    if cursor % 2:
        blobs.append(b"\0")
        cursor += 1
    strip_offsets = []
    for s in strips:
        strip_offsets.append(cursor)
        cursor += len(s)
    offsets_raw = struct.pack(f"{order}{len(strip_offsets)}I", *strip_offsets)
    blobs = [offsets_raw if b is None else b for b in blobs]

    out = bytearray()
    out += (b"II" if order == "<" else b"MM") + struct.pack(f"{order}HI", 42, ifd_offset)
    out += struct.pack(f"{order}H", n)
    for tag, type_, count, raw in out_entries:
        if tag == _TAG_STRIP_OFFSETS:
            raw = offsets_raw
        nbytes = len(raw)
        out += struct.pack(f"{order}HHI", tag, type_, count)
        if nbytes > 4:
            out += struct.pack(f"{order}I", value_offsets[tag])
        else:
            out += raw + b"\0" * (4 - nbytes)
    out += struct.pack(f"{order}I", 0)
    for b in blobs:
        out += b
    for s in strips:
        out += s
    return bytes(out)


class PartialRaster:
    """Rows of a raster plus everything needed to place and check them."""

    def __init__(self, data: np.ndarray, tags, cols: int, rows: int,
                 intervals: list[tuple[int, int]], fetched_bytes: int, file_bytes: int | None):
        self.data = data            # (rows, cols), fill value outside `intervals`
        self.tags = tags            # Pillow tag_v2 of the compact file (GeoTIFF tags intact)
        self.cols = cols
        self.rows = rows
        self.intervals = intervals
        self.fetched_bytes = fetched_bytes
        self.file_bytes = file_bytes


async def fetch_rows(client, url: str, want: list[tuple[int, int]],
                     max_fraction: float = 0.7) -> tuple[_RangeReader, dict]:
    """Network half: read the IFD and the strips covering `want` (half-open,
    already merged). Returns the reader and what `assemble` needs.

    Raises PartialUnsupported when the file is not one row-run per strip, or when
    the wanted rows are most of the file anyway — then a plain download is cheaper.
    """
    reader = _RangeReader(client, url)
    order, entries = await _read_ifd(reader)
    if _TAG_TILE_WIDTH in entries:
        raise PartialUnsupported("tiled TIFF")
    for tag in (_TAG_WIDTH, _TAG_HEIGHT, _TAG_STRIP_OFFSETS, _TAG_STRIP_COUNTS):
        if tag not in entries:
            raise PartialUnsupported(f"tag {tag} missing")
    width = _values(entries[_TAG_WIDTH], order)[0]
    height = _values(entries[_TAG_HEIGHT], order)[0]
    rps = _values(entries[_TAG_ROWS_PER_STRIP], order)[0] if _TAG_ROWS_PER_STRIP in entries else height
    offsets = _values(entries[_TAG_STRIP_OFFSETS], order)
    counts = _values(entries[_TAG_STRIP_COUNTS], order)
    n_strips = (height + rps - 1) // rps
    if rps < 1 or len(offsets) != n_strips or len(counts) != n_strips:
        raise PartialUnsupported("inconsistent strip layout")

    want = merge_rows(want, height, gap=0)
    if not want:
        raise PartialUnsupported("no rows wanted")
    # Whole strips only: align each interval to strip boundaries.
    runs: list[tuple[int, int]] = []
    for a, b in want:
        s0, s1 = a // rps, (b - 1) // rps + 1
        if runs and s0 <= runs[-1][1]:
            runs[-1] = (runs[-1][0], max(runs[-1][1], s1))
        else:
            runs.append((s0, s1))
    wanted_bytes = sum(counts[s] for s0, s1 in runs for s in range(s0, s1))
    if wanted_bytes >= max_fraction * sum(counts):
        raise PartialUnsupported("wanted rows are most of the file")

    strips: list[bytes] = []
    for s0, s1 in runs:
        lo = min(offsets[s] for s in range(s0, s1))
        hi = max(offsets[s] + counts[s] for s in range(s0, s1))
        block = await reader.read(lo, hi)
        for s in range(s0, s1):
            strips.append(block[offsets[s] - lo:offsets[s] - lo + counts[s]])

    return reader, {"order": order, "entries": entries, "width": width, "height": height,
                    "rps": rps, "runs": runs, "strips": strips}


def assemble(parts: dict, fetched_bytes: int, file_bytes: int | None) -> PartialRaster:
    """CPU half (run on a worker thread): decode the strips and place them."""
    from PIL import Image

    width, height, rps = parts["width"], parts["height"], parts["rps"]
    runs = parts["runs"]
    n_rows = sum(min(s1 * rps, height) - s0 * rps for s0, s1 in runs)
    # Pillow wants the last strip of a compact file to be the only short one; a
    # short strip in the middle (the original's last strip) cannot happen here
    # unless it is also our last, because it is the last strip of the image.
    compact = _compact_tiff(parts["order"], parts["entries"], width, rps,
                            parts["strips"], n_rows)
    with Image.open(io.BytesIO(compact)) as image:
        decoded = np.array(image)
        tags = dict(image.tag_v2)
    if decoded.shape != (n_rows, width):
        raise PartialUnsupported(f"decoded shape {decoded.shape} != {(n_rows, width)}")

    full = np.full((height, width), FILL_VALUE, dtype=decoded.dtype)
    cursor = 0
    intervals = []
    for s0, s1 in runs:
        r0, r1 = s0 * rps, min(s1 * rps, height)
        full[r0:r1] = decoded[cursor:cursor + (r1 - r0)]
        cursor += r1 - r0
        intervals.append((r0, r1))
    return PartialRaster(full, tags, width, height, intervals, fetched_bytes, file_bytes)


def covers(outer, inner) -> bool:
    """True if every row in `inner` is inside `outer`. None means the whole grid."""
    if outer is None:
        return True
    if inner is None:
        return False
    return all(any(a <= c and d <= b for a, b in outer) for c, d in inner)


__all__ = ["FILL_VALUE", "PartialUnsupported", "PartialRaster",
           "fetch_rows", "assemble", "merge_rows", "covers"]
