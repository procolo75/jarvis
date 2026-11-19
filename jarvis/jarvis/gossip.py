"""
gossip.py
─────────
Reads INGV GOSSIP `events.json` without downloading all of it.

The file is the whole two-year catalogue of an area (Campi Flegrei ~7 MB, served
uncompressed) sorted newest first, and the server honours HTTP Range. So only the
head is fetched, in growing blocks, until it reaches back far enough: the live
monitor polls every minute and the reports need a day or a week, not two years.
"""

import json
import re

URL = "https://terremoti.ov.ingv.it/gossip/{area}/events.json"

FIRST_BLOCK = 64 * 1024          # ~60 events
_CONTENT_RANGE = re.compile(r"bytes (\d+)-(\d+)/(\d+|\*)")


def parse_partial_array(text: str) -> list[dict]:
    """Objects of a JSON array cut at an arbitrary byte: the truncated last one is dropped."""
    decoder = json.JSONDecoder()
    events: list[dict] = []
    i = text.find("[") + 1
    if i == 0:
        return events
    n = len(text)
    while i < n:
        while i < n and text[i] in ", \t\r\n":
            i += 1
        if i >= n or text[i] == "]":
            break
        try:
            obj, i = decoder.raw_decode(text, i)
        except ValueError:
            break
        if isinstance(obj, dict):
            events.append(obj)
    return events


def _covered(events: list[dict], since_epoch: float, min_events: int) -> bool:
    if len(events) < min_events or not events:
        return False
    oldest = min((e.get("epoch") or 0) for e in events)
    return oldest < since_epoch


def _as_list(data) -> list[dict]:
    return data if isinstance(data, list) else []


async def fetch_events(client, area: str, since_epoch: float, min_events: int = 0,
                       cache: dict | None = None) -> list[dict]:
    """Newest events of `area`, going back at least to `since_epoch` and at least
    `min_events` of them (more may come back: whole blocks are parsed).

    `cache`, when given, is a dict the caller keeps between calls; the first request
    is then conditional and an unchanged file costs a 304 with no body.
    """
    url = URL.format(area=area)
    cached = cache.get(area) if cache is not None else None

    headers = {"Range": f"bytes=0-{FIRST_BLOCK - 1}"}
    if cached:
        if cached.get("etag"):
            headers["If-None-Match"] = cached["etag"]
        if cached.get("last_modified"):
            headers["If-Modified-Since"] = cached["last_modified"]

    resp = await client.get(url, headers=headers)
    if resp.status_code == 304 and cached:
        return cached["events"]
    resp.raise_for_status()
    etag = resp.headers.get("ETag")
    last_modified = resp.headers.get("Last-Modified")

    if resp.status_code != 206:
        events = _as_list(resp.json())       # Range ignored: the whole file came back.
    else:
        buf = bytearray(resp.content)
        block = FIRST_BLOCK
        while True:
            m = _CONTENT_RANGE.match(resp.headers.get("Content-Range", ""))
            total = int(m.group(3)) if m and m.group(3) != "*" else None
            complete = total is not None and len(buf) >= total
            text = buf.decode("utf-8", errors="ignore")
            if complete:
                events = _as_list(json.loads(text))
                break
            events = parse_partial_array(text)
            if _covered(events, since_epoch, min_events):
                break
            block *= 2
            start = len(buf)
            more = {"Range": f"bytes={start}-{start + block - 1}"}
            if etag:
                more["If-Range"] = etag  # File changed meanwhile → the server sends it whole.
            resp = await client.get(url, headers=more)
            if resp.status_code == 416:      # Asked past the end: what we have is all.
                events = _as_list(json.loads(text)) if text.rstrip().endswith("]") else events
                break
            resp.raise_for_status()
            if resp.status_code != 206:
                etag = resp.headers.get("ETag")
                last_modified = resp.headers.get("Last-Modified")
                events = _as_list(resp.json())
                break
            if not resp.content:
                break
            buf += resp.content

    if cache is not None:
        cache[area] = {"etag": etag, "last_modified": last_modified, "events": events}
    return events
