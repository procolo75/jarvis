"""
tests/test_gossip_fetch.py
──────────────────────────
Reading INGV GOSSIP events.json by its head only — `gossip.py`.

    cd jarvis && python3 -m unittest discover tests

What is pinned, and why it bites:
  · events.json is the whole two-year catalogue (Campi Flegrei ~7 MB, uncompressed)
    sorted newest first. The live monitor fetched it every minute — ~375 GB a month,
    found measuring the add-ons' traffic for a data SIM. Only as many Range blocks
    as the period needs may be downloaded, and none at all when the ETag still matches.
"""

import asyncio
import json
import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "jarvis"))

import gossip  # noqa: E402

NOW = 2_000_000_000.0


def catalogue(n, spacing_s, now=NOW):
    """n events newest first, one every spacing_s seconds, ~1 KB each like the real ones."""
    return [{"id": n - i, "epoch": now - i * spacing_s, "class": "Rivisto", "pad": "x" * 900}
            for i in range(n)]


class _Resp:
    def __init__(self, status, body=b"", headers=None):
        self.status_code = status
        self.content = body
        self.headers = headers or {}

    def raise_for_status(self):
        if self.status_code >= 400 and self.status_code != 416:
            raise RuntimeError(self.status_code)

    def json(self):
        return json.loads(self.content)


class FakeGossip:
    def __init__(self, events, etag='"v1"', honour_range=True):
        self.set(events, etag)
        self.honour_range = honour_range
        self.requests = []
        self.sent = 0

    def set(self, events, etag):
        self.body = json.dumps(events).encode()
        self.etag = etag

    async def get(self, url, headers=None):
        h = headers or {}
        self.requests.append(h)
        base = {"ETag": self.etag, "Last-Modified": "Wed, 18 Nov 2026 17:52:35 GMT"}
        if h.get("If-None-Match") == self.etag:
            return _Resp(304, headers=base)
        rng = h.get("Range")
        if not rng or not self.honour_range or ("If-Range" in h and h["If-Range"] != self.etag):
            self.sent += len(self.body)
            return _Resp(200, self.body, base)
        a, b = map(int, re.match(r"bytes=(\d+)-(\d+)", rng).groups())
        if a >= len(self.body):
            return _Resp(416, headers=base)
        part = self.body[a:b + 1]
        self.sent += len(part)
        return _Resp(206, part, {**base, "Content-Range": f"bytes {a}-{a + len(part) - 1}/{len(self.body)}"})


def fetch(srv, since, min_events=0, cache=None):
    return asyncio.run(gossip.fetch_events(srv, "flegrei", since, min_events, cache))


class FetchEvents(unittest.TestCase):
    def test_last_24h_quiet_period_is_one_block(self):
        srv = FakeGossip(catalogue(5000, 3 * 3600))          # one quake every 3 h, ~5 MB
        events = fetch(srv, NOW - 86400)
        self.assertEqual(len(srv.requests), 1)
        self.assertLessEqual(srv.sent, gossip.FIRST_BLOCK)
        self.assertTrue(all(e1["epoch"] > e2["epoch"] for e1, e2 in zip(events, events[1:])))
        self.assertLess(min(e["epoch"] for e in events), NOW - 86400)
        self.assertEqual(len([e for e in events if e["epoch"] >= NOW - 86400]), 9)

    def test_week_grows_in_doubling_blocks(self):
        srv = FakeGossip(catalogue(5000, 1800))              # one every 30 min: ~336 in a week
        events = fetch(srv, NOW - 7 * 86400)
        ranges = [r["Range"] for r in srv.requests]
        self.assertEqual(ranges[:3], ["bytes=0-65535", "bytes=65536-196607", "bytes=196608-458751"])
        self.assertTrue(all(r.get("If-Range") == '"v1"' for r in srv.requests[1:]))
        self.assertEqual(len([e for e in events if e["epoch"] >= NOW - 7 * 86400]), 337)
        self.assertLess(srv.sent, len(srv.body) / 5)

    def test_swarm_widens_live_window(self):
        srv = FakeGossip(catalogue(5000, 60))                # one a minute: 60 events = 1 h
        events = fetch(srv, NOW - 3 * 3600, min_events=60)
        self.assertEqual(len([e for e in events if e["epoch"] >= NOW - 3 * 3600]), 181)

    def test_min_events_reached_even_if_window_is_covered(self):
        srv = FakeGossip(catalogue(500, 6 * 3600))
        events = fetch(srv, NOW - 3 * 3600, min_events=60)
        self.assertGreaterEqual(len(events), 60)

    def test_truncated_object_is_dropped(self):
        text = json.dumps(catalogue(3, 60))
        cut = text[: text.rindex('{"id"') + 20]
        self.assertEqual([e["id"] for e in gossip.parse_partial_array(cut)], [3, 2])

    def test_small_file_read_whole(self):
        srv = FakeGossip(catalogue(3, 86400 * 30))           # Ischia: 3 events, 2 KB
        self.assertEqual(len(fetch(srv, NOW - 365 * 86400)), 3)

    def test_unchanged_file_costs_nothing(self):
        srv, cache = FakeGossip(catalogue(500, 600)), {}
        first = fetch(srv, NOW - 3600, 60, cache)
        sent = srv.sent
        second = fetch(srv, NOW - 3600, 60, cache)
        self.assertEqual(first, second)
        self.assertEqual(srv.sent, sent)
        self.assertEqual(srv.requests[-1]["If-None-Match"], '"v1"')

    def test_file_changing_mid_read_comes_back_whole(self):
        srv = FakeGossip(catalogue(2000, 1800))
        orig_get = srv.get

        async def get(url, headers=None):
            if len(srv.requests) == 1:                         # new quake after the first block
                srv.set(catalogue(2001, 1800), '"v2"')
            return await orig_get(url, headers)

        srv.get = get
        events = fetch(srv, NOW - 7 * 86400)
        self.assertEqual(len(events), 2001)

    def test_server_ignoring_range(self):
        srv = FakeGossip(catalogue(100, 3600), honour_range=False)
        self.assertEqual(len(fetch(srv, NOW - 3600)), 100)


if __name__ == "__main__":
    unittest.main()
