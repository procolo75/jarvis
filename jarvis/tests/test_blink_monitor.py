"""
tests/test_blink_monitor.py
───────────────────────────
Blink motion → Telegram forwarding (live_monitors/blink.py).

    cd jarvis && python3 -m unittest tests.test_blink_monitor

What is pinned down: only the video is sent and an old add-on's snapshot is
ignored, the cooldown is per camera, the camera filter matches serial or name,
retained events are never replayed, and Car Mode sends one text line instead
of the video.
"""

import asyncio
import sys
import types
import unittest
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(_REPO / "jarvis"))

if "aiomqtt" not in sys.modules:
    sys.modules["aiomqtt"] = types.ModuleType("aiomqtt")

from live_monitors.blink import BlinkMonitor, BlinkSender, parse_topic  # noqa: E402

JPEG = b"\xff\xd8jpeg"
MP4  = b"\x00\x00\x00\x18ftypmp42"


class _Recorder:
    def __init__(self, car=False):
        self.calls = []
        self.car = car

    def sender(self):
        async def video(data, caption): self.calls.append(("video", data, caption))
        async def text(t):              self.calls.append(("text", t))
        return BlinkSender(video=video, text=text, car_mode=lambda: self.car)


def _monitor(rec, **cfg):
    return BlinkMonitor({"language": "it", **cfg}, {}, rec.sender(), "Europe/Rome")


def _run(coro):
    return asyncio.run(coro)


class ParseTopicTest(unittest.TestCase):
    def test_kinds(self):
        self.assertEqual(parse_topic("blink", "blink/cameras/G8T1/name"), ("G8T1", "name"))
        self.assertEqual(parse_topic("blink", "blink/cameras/G8T1/event/clip"), ("G8T1", "clip"))

    def test_ignored(self):
        self.assertIsNone(parse_topic("blink", "blink/cameras/G8T1/event/snapshot"))
        self.assertIsNone(parse_topic("blink", "blink/cameras/G8T1/snapshot"))
        self.assertIsNone(parse_topic("blink", "blink/cameras/G8T1/motion/state"))
        self.assertIsNone(parse_topic("blink", "other/cameras/G8T1/event/clip"))


class BlinkMonitorTest(unittest.TestCase):
    def _event(self, m, serial="G8T1", now=0.0):
        _run(m.handle(f"blink/cameras/{serial}/event/clip", MP4, now=now))

    def test_video_only_with_name(self):
        rec = _Recorder()
        m = _monitor(rec)
        _run(m.handle("blink/cameras/G8T1/name", b"Ingresso", retain=True))
        self._event(m)
        self.assertEqual([c[0] for c in rec.calls], ["video"])
        self.assertEqual(rec.calls[0][1], MP4)
        self.assertIn("Ingresso", rec.calls[0][2])
        self.assertIn("Movimento", rec.calls[0][2])

    def test_old_addon_snapshot_ignored(self):
        rec = _Recorder()
        m = _monitor(rec, cooldown_s=60)
        _run(m.handle("blink/cameras/G8T1/event/snapshot", JPEG, now=0))
        self._event(m, now=5)       # the snapshot did not consume the cooldown
        self.assertEqual([c[0] for c in rec.calls], ["video"])

    def test_cooldown(self):
        rec = _Recorder()
        m = _monitor(rec, cooldown_s=60)
        self._event(m, now=0)
        self._event(m, now=30)      # inside cooldown
        self._event(m, now=100)     # after cooldown
        self.assertEqual([c[0] for c in rec.calls], ["video", "video"])

    def test_zero_cooldown_sends_every_event(self):
        rec = _Recorder()
        m = _monitor(rec)
        self._event(m, now=0)
        self._event(m, now=1)
        self.assertEqual(len(rec.calls), 2)

    def test_camera_filter_serial_or_name(self):
        rec = _Recorder()
        m = _monitor(rec, cameras=["giardino"])
        _run(m.handle("blink/cameras/AAA/name", b"Giardino"))
        _run(m.handle("blink/cameras/BBB/name", b"Ingresso"))
        self._event(m, serial="AAA")
        self._event(m, serial="BBB")
        self.assertEqual(len(rec.calls), 1)
        rec2 = _Recorder()
        m2 = _monitor(rec2, cameras=["BBB"])
        self._event(m2, serial="BBB")
        self.assertEqual(len(rec2.calls), 1)

    def test_retained_event_ignored(self):
        rec = _Recorder()
        m = _monitor(rec)
        _run(m.handle("blink/cameras/G8T1/event/snapshot", JPEG, retain=True))
        _run(m.handle("blink/cameras/G8T1/event/clip", MP4, retain=True))
        self.assertEqual(rec.calls, [])

    def test_car_mode_text_only(self):
        rec = _Recorder(car=True)
        m = _monitor(rec)
        self._event(m)
        self.assertEqual([c[0] for c in rec.calls], ["text"])
        self.assertIn("Movimento", rec.calls[0][1])


if __name__ == "__main__":
    unittest.main()
