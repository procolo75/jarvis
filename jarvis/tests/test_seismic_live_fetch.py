"""
tests/test_seismic_live_fetch.py
────────────────────────────────
The live seismic monitor — `live_monitors/seismic.py` — fetching events.json.

    cd jarvis && python3 -m unittest discover tests

What is pinned, and why it bites:
  · It polls every 60 s per area. It must ask only for the head of the file (last
    LIVE_MIN_EVENTS events, at least LIVE_WINDOW_HOURS) and keep the ETag between
    polls, so a minute without news is a 304 with no body — not 7 MB.
"""

import asyncio
import time
import sys
import types
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "jarvis"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

_stubbed = False
try:
    import httpx                                            # noqa: F401
except ImportError:
    _fake = types.ModuleType("httpx")
    _fake.AsyncClient = object
    sys.modules["httpx"] = _fake
    _stubbed = True
try:
    from live_monitors import seismic                       # noqa: E402
finally:
    if _stubbed:
        sys.modules.pop("httpx", None)

from test_gossip_fetch import FakeGossip, catalogue        # noqa: E402


def _monitor():
    return seismic.SeismicLiveMonitor({"id": "m", "areas": ["flegrei"]}, lambda *_: None)


class FetchArea(unittest.TestCase):
    def test_range_then_conditional(self):
        mon, srv = _monitor(), FakeGossip(catalogue(5000, 3600, now=time.time()))
        first = asyncio.run(mon._fetch_area(srv, "flegrei"))
        second = asyncio.run(mon._fetch_area(srv, "flegrei"))
        self.assertGreaterEqual(len(first), seismic.LIVE_MIN_EVENTS)
        self.assertEqual(first, second)
        self.assertEqual(srv.requests[0]["Range"], "bytes=0-65535")
        self.assertEqual(srv.requests[-1]["If-None-Match"], '"v1"')
        self.assertLess(srv.sent, 200_000)

    def test_cache_is_per_area(self):
        mon, srv = _monitor(), FakeGossip(catalogue(100, 3600))
        asyncio.run(mon._fetch_area(srv, "flegrei"))
        asyncio.run(mon._fetch_area(srv, "vesuvio"))
        self.assertNotIn("If-None-Match", srv.requests[-1])

    def test_failure_returns_empty(self):
        class Down:
            async def get(self, url, headers=None):
                raise ConnectionError("refused")
        self.assertEqual(asyncio.run(_monitor()._fetch_area(Down(), "flegrei")), [])


if __name__ == "__main__":
    unittest.main()
