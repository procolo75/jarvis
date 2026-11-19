"""
tests/test_radar_partial.py
───────────────────────────
Reading only the rows of the DPC radar GeoTIFF that running monitors look at —
`live_monitors/radar_partial.py` and its use in `RadarFeed`.

    cd jarvis && python3 -m unittest discover tests

What is pinned, and why it bites:
  · The whole-country file was downloaded every five minutes per product (~7 GB a
    month) to look at a couple of hundred kilometres. Rows read by Range must be
    IDENTICAL to the same rows of a whole download, on the same geotransform.
  · A row that was not fetched must read as "no measurement", never as "no
    rain": coverage drops and the monitor goes blind instead of issuing an
    all-clear for a place it did not look at.
  · Whenever the feed cannot say which rows are needed (a monitor with no
    position, the grid not learnt yet, an area callable that fails) or the file
    or server will not cooperate, it downloads the whole file — never less.
  · The two buffered frames feed the motion estimate at the same pixels: an
    older frame not covering the newest one's rows is dropped, never mixed.
"""

import asyncio
import io
import re
import sys
import time
import types
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "jarvis"))

try:
    import numpy as np
    from PIL import Image, TiffImagePlugin
    import httpx  # noqa: F401  (imported by live_monitors.radar)
except ImportError as e:                                   # pragma: no cover
    raise unittest.SkipTest(f"radar dependencies unavailable: {e}")

from live_monitors import radar as R                       # noqa: E402
from live_monitors import radar_partial as RP              # noqa: E402
from live_monitors.radar_core import (                     # noqa: E402
    NODATA_THRESHOLD, coverage_fraction, latlon_to_pixel, peak_in_disc, rain_points)

COLS, ROWS = 120, 140          # a 10× smaller DPC grid, 10 km pixels, same projection


def make_tiff(data: np.ndarray, rows_per_strip: int = 1) -> bytes:
    """A GeoTIFF shaped like the DPC products: float32, LZW, strips, TM 42N 12.5E."""
    info = TiffImagePlugin.ImageFileDirectory_v2()
    info[278] = rows_per_strip
    info[339] = 3
    info.tagtype[33550] = 12
    info[33550] = (10000.0, 10000.0, 0.0)
    info.tagtype[33922] = 12
    info[33922] = (0.0, 0.0, 0.0, -600000.0, 650000.0, 0.0)
    info.tagtype[34735] = 3
    info[34735] = (1, 1, 0, 7, 1024, 0, 1, 1, 1025, 0, 1, 1, 2048, 0, 1, 4326,
                   3075, 0, 1, 1, 3076, 0, 1, 9001, 3080, 34736, 1, 0, 3081, 34736, 1, 1)
    info.tagtype[34736] = 12
    info[34736] = (12.5, 42.0)
    buf = io.BytesIO()
    Image.fromarray(data.astype(np.float32), mode="F").save(
        buf, format="TIFF", compression="tiff_lzw", tiffinfo=info)
    return buf.getvalue()


def sample_field(seed: int = 1) -> np.ndarray:
    rng = np.random.default_rng(seed)
    data = np.where(rng.random((ROWS, COLS)) < 0.2, rng.random((ROWS, COLS)) * 20, 0.0)
    data[:, :8] = -9999.0                        # a strip the network cannot see
    data[60:70, 50:70] = 35.0                    # a cell of heavy rain
    return data.astype(np.float32)


class Resp:
    def __init__(self, status, content=b"", headers=None, payload=None):
        self.status_code, self.content = status, content
        self.headers = headers or {}
        self._payload = payload

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(self.status_code)

    def json(self):
        return self._payload


class FakeS3:
    """Serves one body, honouring single byte ranges like S3 (or not)."""

    def __init__(self, body: bytes, honour_range: bool = True):
        self.body, self.honour_range = body, honour_range
        self.requests, self.sent = [], 0

    async def get(self, url, headers=None, params=None):
        rng = (headers or {}).get("Range")
        self.requests.append(rng)
        if not rng or not self.honour_range:
            self.sent += len(self.body)
            return Resp(200, self.body)
        a, b = map(int, re.match(r"bytes=(\d+)-(\d+)", rng).groups())
        b = min(b, len(self.body) - 1)
        self.sent += b - a + 1
        return Resp(206, self.body[a:b + 1],
                    {"Content-Range": f"bytes {a}-{b}/{len(self.body)}"})


def run(coro):
    return asyncio.run(coro)


class PartialRead(unittest.TestCase):
    def setUp(self):
        self.data = sample_field()
        self.body = make_tiff(self.data)
        self.full = R._decode(self.body, 0.0, "SRI")

    def read(self, want, s3=None, max_fraction=0.95):
        s3 = s3 or FakeS3(self.body)
        reader, parts = run(RP.fetch_rows(s3, "url", RP.merge_rows(want, ROWS), max_fraction))
        return RP.assemble(parts, reader.fetched, reader.size), s3

    def test_rows_identical_to_whole_download_and_rest_is_nodata(self):
        p, _ = self.read([(40, 80), (100, 120)])
        self.assertEqual(p.data.shape, self.full.data.shape)
        self.assertEqual(p.data.dtype, self.full.data.dtype)
        for a, b in p.intervals:
            np.testing.assert_array_equal(p.data[a:b], self.full.data[a:b])
        outside = np.ones(ROWS, bool)
        for a, b in p.intervals:
            outside[a:b] = False
        self.assertTrue((p.data[outside] <= NODATA_THRESHOLD).all())

    def test_same_geotransform(self):
        p, _ = self.read([(10, 30)])
        gt = R.parse_geotransform(p.tags, p.cols, p.rows)
        self.assertEqual(gt, self.full.gt)

    def test_reads_fewer_bytes(self):
        _p, s3 = self.read([(60, 75)])
        self.assertLess(s3.sent, len(self.body) / 2)

    def test_multi_row_strips(self):
        body = make_tiff(self.data, rows_per_strip=7)
        p, _ = self.read([(33, 41)], s3=FakeS3(body))
        for a, b in p.intervals:
            np.testing.assert_array_equal(p.data[a:b], self.full.data[a:b])
        self.assertTrue(all(a <= 33 and 41 <= b for a, b in p.intervals))

    def test_edges_clip(self):
        p, _ = self.read([(-20, 5), (130, 200)])
        self.assertEqual(p.intervals, [(0, 5), (130, 140)])
        np.testing.assert_array_equal(p.data[130:140], self.full.data[130:140])

    def test_server_ignoring_range_is_unsupported(self):
        with self.assertRaises(RP.PartialUnsupported):
            self.read([(10, 20)], s3=FakeS3(self.body, honour_range=False))

    def test_most_of_the_file_is_unsupported(self):
        with self.assertRaises(RP.PartialUnsupported):
            self.read([(0, 130)], max_fraction=0.7)

    def test_not_a_tiff_is_unsupported(self):
        with self.assertRaises(RP.PartialUnsupported):
            self.read([(0, 5)], s3=FakeS3(b"<html>no</html>" * 10))


class Helpers(unittest.TestCase):
    def test_merge_rows(self):
        self.assertEqual(RP.merge_rows([(50, 60), (10, 20), (55, 70), (-5, 3)], 100, gap=0),
                         [(0, 3), (10, 20), (50, 70)])
        self.assertEqual(RP.merge_rows([(10, 20), (40, 50)], 100, gap=32), [(10, 50)])
        self.assertEqual(RP.merge_rows([(120, 130)], 100), [])

    def test_covers(self):
        self.assertTrue(RP.covers(None, [(0, 10)]))
        self.assertTrue(RP.covers(None, None))
        self.assertFalse(RP.covers([(0, 10)], None))
        self.assertTrue(RP.covers([(0, 50), (60, 90)], [(5, 10), (70, 80)]))
        self.assertFalse(RP.covers([(0, 50)], [(40, 60)]))


class NodataSemantics(unittest.TestCase):
    """What a monitor sees on a partial grid."""

    def setUp(self):
        self.data = sample_field()
        self.data[:, 8:] = np.maximum(self.data[:, 8:], 0.0)
        body = make_tiff(self.data)
        self.full = R._decode(body, 0.0, "SRI")
        gt = self.full.gt
        self.origin = (40.85, 14.27)                       # Napoli
        _c, row = latlon_to_pixel(gt, *self.origin)
        self.row = int(row)
        reader, parts = run(RP.fetch_rows(FakeS3(body), "u",
                                          [(self.row - 10, self.row + 11)], 0.95))
        p = RP.assemble(parts, reader.fetched, reader.size)
        self.partial = R.RadarGrid(t=0.0, product="SRI", data=p.data, gt=gt,
                                   rows=tuple(p.intervals))

    def test_inside_the_band_everything_matches(self):
        radius = 60.0
        self.assertEqual(coverage_fraction(self.partial, self.origin, radius),
                         coverage_fraction(self.full, self.origin, radius))
        self.assertEqual(peak_in_disc(self.partial, self.origin, radius),
                         peak_in_disc(self.full, self.origin, radius))
        self.assertEqual(rain_points(self.partial, self.origin, radius, 0.1),
                         rain_points(self.full, self.origin, radius, 0.1))

    def test_outside_the_band_is_blind_not_dry(self):
        milano = (45.46, 9.19)
        self.assertEqual(coverage_fraction(self.partial, milano, 60.0), 0.0)
        self.assertIsNone(peak_in_disc(self.partial, milano, 60.0))
        self.assertGreater(coverage_fraction(self.full, milano, 60.0), 0.0)


class FakeDPC(FakeS3):
    """The DPC API in front of a FakeS3 bucket; the product changes every call."""

    def __init__(self, bodies: list[bytes]):
        super().__init__(bodies[0])
        self.bodies, self.stamp = bodies, 1_700_000_000_000
        self.calls = 0

    async def get(self, url, headers=None, params=None):
        if "findLastProductByType" in url:
            return Resp(200, payload={"lastProducts": [{"time": self.stamp, "period": "PT5M"}]})
        return await super().get(url, headers)

    async def post(self, url, json=None):
        self.body = self.bodies[min(self.calls, len(self.bodies) - 1)]
        self.calls += 1
        return Resp(200, payload={"url": "https://s3.example/x.tif"})

    def next_product(self):
        self.stamp += 300_000


class Feed(unittest.TestCase):
    def setUp(self):
        self.body = make_tiff(sample_field())
        self.full = R._decode(self.body, 0.0, "SRI")
        self.feed = R.RadarFeed()
        self.dpc = FakeDPC([self.body])
        self.napoli = lambda: [(40.85, 14.27, 150.0)]

    def cycle(self):
        self.feed._next_due["SRI"] = 0.0
        run(self.feed._refresh_due(self.dpc))
        self.dpc.next_product()
        return self.feed.latest("SRI")

    def register(self, area):
        self.feed._consumers["SRI"] = self.feed._consumers.get("SRI", 0) + 1
        self.feed._areas.setdefault("SRI", []).append(area)
        self.feed._frames.setdefault("SRI", R.deque(maxlen=R.FRAME_HISTORY))

    def test_first_download_whole_then_rows(self):
        self.register(self.napoli)
        first = self.cycle()
        self.assertIsNone(first.rows)                   # grid not known yet
        sent = self.dpc.sent
        second = self.cycle()
        self.assertIsNotNone(second.rows)
        self.assertLess(self.dpc.sent - sent, len(self.body) * 0.7)
        self.assertEqual(second.gt, first.gt)
        for a, b in second.rows:
            np.testing.assert_array_equal(second.data[a:b], self.full.data[a:b])
        self.assertEqual(self.feed.stats()["partial_downloads"], 1)

    def test_consumer_without_position_means_whole_grid(self):
        self.register(self.napoli)
        self.register(lambda: None)
        self.cycle()
        self.assertIsNone(self.cycle().rows)

    def test_consumer_without_area_means_whole_grid(self):
        self.register(self.napoli)
        self.register(None)
        self.cycle()
        self.assertIsNone(self.cycle().rows)

    def test_failing_area_means_whole_grid(self):
        def broken():
            raise RuntimeError("boom")
        self.register(self.napoli)
        self.register(broken)
        self.cycle()
        self.assertIsNone(self.cycle().rows)

    def test_server_without_range_falls_back_to_whole_file(self):
        self.register(self.napoli)
        self.dpc.honour_range = False
        self.cycle()
        grid = self.cycle()
        self.assertIsNone(grid.rows)
        np.testing.assert_array_equal(grid.data, self.full.data)

    def test_two_places_two_bands(self):
        # 10 km pixels here: bands closer than 32 rows are merged on purpose.
        self.register(lambda: [(40.85, 14.27, 50.0)])         # Napoli
        self.register(lambda: [(45.46, 9.19, 50.0)])          # Milano
        self.cycle()
        grid = self.cycle()
        self.assertEqual(len(grid.rows), 2)

    def test_history_kept_while_rows_stay_put(self):
        self.register(self.napoli)
        self.cycle()                                          # whole
        self.cycle()                                          # rows, covered by the whole one
        self.cycle()
        self.assertEqual(len(self.feed.frames("SRI")), 2)

    def test_history_dropped_when_rows_move(self):
        where = {"p": (40.85, 14.27)}
        self.register(lambda: [(*where["p"], 60.0)])
        self.cycle()
        self.cycle()
        where["p"] = (45.46, 9.19)                            # the phone went north
        self.cycle()
        frames = self.feed.frames("SRI")
        self.assertEqual(len(frames), 1)                      # no mixed pair for motion

    def test_release_drops_the_area(self):
        feed = R.RadarFeed()
        feed.start = lambda: None
        area = self.napoli
        feed.acquire("SRI", area=area)
        feed.acquire("SRI", area=None)
        feed.release("SRI", area=None)
        self.assertEqual(feed._areas["SRI"], [area])
        feed.stop = lambda: None
        feed.release("SRI", area=area)
        self.assertNotIn("SRI", feed._areas)


class MonitorAreas(unittest.TestCase):
    """The callables rain and hail monitors hand to the feed."""

    def _rain(self, **cfg):
        from live_monitors.rain_front import RainFrontLiveMonitor
        base = {"id": "r", "name": "R", "type": "rain_front", "latitude": 40.85,
                "longitude": 14.27, "radius_km": 30}
        base.update(cfg)
        return RainFrontLiveMonitor(base, None, "Europe/Rome")

    def _hail(self, **cfg):
        from live_monitors.hail_front import HailFrontLiveMonitor
        base = {"id": "h", "name": "H", "type": "hail_front", "latitude": 40.85,
                "longitude": 14.27, "radius_km": 30}
        base.update(cfg)
        return HailFrontLiveMonitor(base, None, "Europe/Rome")

    def test_fixed_rain_monitor_covers_disc_advection_and_motion_box(self):
        from live_monitors.radar_core import MOTION_BOX_KM
        from live_monitors.rain_front import MAX_ADVECTION_KM
        mon = self._rain()
        squares = mon._feed_area()
        self.assertTrue(squares)
        for lat, lon, half in squares:
            self.assertEqual((lat, lon), (40.85, 14.27))
            self.assertGreaterEqual(half, mon.observe_radius_km + MAX_ADVECTION_KM)
            self.assertGreaterEqual(half, MOTION_BOX_KM / 2.0)

    def test_hail_monitor_covers_its_radius(self):
        mon = self._hail()
        for _lat, _lon, half in mon._feed_area():
            self.assertGreater(half, mon.radius_km)

    def test_position_monitor_includes_latest_fix_whatever_its_age(self):
        from live_monitors import rain_front as RF
        mon = self._rain(position_id="phone", latitude=0, longitude=0)
        old = types.SimpleNamespace(lat=41.9, lon=12.5)
        real = RF.position_manager
        RF.position_manager = types.SimpleNamespace(current=lambda pid, now=None: old)
        try:
            points = {(la, lo) for la, lo, _h in mon._feed_area()}
        finally:
            RF.position_manager = real
        self.assertIn((41.9, 12.5), points)

    def test_position_monitor_without_fix_or_fallback_wants_whole_grid(self):
        from live_monitors import hail_front as HF
        mon = self._hail(position_id="phone", latitude=0, longitude=0)
        mon._origin_point = (0.0, 0.0)
        real = HF.position_manager
        HF.position_manager = types.SimpleNamespace(current=lambda pid, now=None: None)
        try:
            self.assertIsNone(mon._feed_area())
        finally:
            HF.position_manager = real


if __name__ == "__main__":
    unittest.main()
