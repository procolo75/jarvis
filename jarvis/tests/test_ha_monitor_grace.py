"""
tests/test_ha_monitor_grace.py
──────────────────────────────
HA Monitor: "not available" alerts that were not true.

    cd jarvis && python3 -m unittest discover tests

Two causes, both pinned down here:

  · Home Assistant marks entities unavailable/unknown while it restarts (minutes
    on a Raspberry Pi) and while an integration reloads. The monitor alerted at
    the first "unavailable", then the real value came back. Now unavailable /
    unknown must last UNAVAILABLE_GRACE seconds; a return to the previous value
    in the meantime is no change at all.
  · /state and /availability shared one slot, so a retained "online" and the real
    value took turns as "the state" and each switch counted as a change.
"""

import asyncio
import sys
import types
import unittest
from pathlib import Path

if "aiomqtt" not in sys.modules:
    sys.modules["aiomqtt"] = types.ModuleType("aiomqtt")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "jarvis"))

from live_monitors import ha  # noqa: E402

PREFIX = "homeassistant"
ENTITY = "sensor/pf"


class Msg:
    def __init__(self, kind, payload, retain=False):
        self.topic = f"{PREFIX}/{ENTITY}/{kind}"
        self.payload = payload.encode()
        self.retain = retain


def monitor(filter_states=("unavailable",)):
    sent = []

    async def send(text):
        sent.append(text)

    m = ha.HaLiveMonitor(
        {"id": "t", "entities": [ENTITY], "cooldown_min": 0, "alert_mode": "direct",
         "filter_states": list(filter_states), "direct_template": "{entity}={state}"},
        send, None, {})
    return m, sent


class GraceTest(unittest.TestCase):
    def setUp(self):
        self._grace = ha.UNAVAILABLE_GRACE
        ha.UNAVAILABLE_GRACE = 0.05

    def tearDown(self):
        ha.UNAVAILABLE_GRACE = self._grace

    def run_msgs(self, m, msgs, wait=0.1):
        async def go():
            for msg in msgs:
                await m._on_message(msg, PREFIX)
            await asyncio.sleep(wait)
        asyncio.run(go())

    def test_ha_restart_blip_is_not_alerted(self):
        m, sent = monitor()
        self.run_msgs(m, [Msg("state", "0.98", retain=True),
                          Msg("state", "unavailable"),
                          Msg("state", "0.98")])
        self.assertEqual(sent, [])

    def test_real_outage_is_alerted_after_grace(self):
        m, sent = monitor()
        self.run_msgs(m, [Msg("state", "0.98", retain=True),
                          Msg("state", "unavailable")])
        self.assertEqual(sent, ["sensor/pf=unavailable"])

    def test_unavailable_then_new_value_alerts_only_the_value(self):
        m, sent = monitor(filter_states=())
        self.run_msgs(m, [Msg("state", "0.98", retain=True),
                          Msg("state", "unknown"),
                          Msg("state", "0.95")])
        self.assertEqual(sent, ["sensor/pf=0.95"])

    def test_retained_online_availability_does_not_count_as_a_state(self):
        m, sent = monitor(filter_states=())
        self.run_msgs(m, [Msg("state", "0.98", retain=True),
                          Msg("availability", "online", retain=True),
                          Msg("state", "0.98")])
        self.assertEqual(sent, [])

    def test_availability_offline_still_alerts_when_it_lasts(self):
        m, sent = monitor()
        self.run_msgs(m, [Msg("state", "0.98", retain=True),
                          Msg("availability", "online", retain=True),
                          Msg("availability", "offline")])
        self.assertEqual(sent, ["sensor/pf=unavailable"])

    def test_monitor_saved_while_sensor_is_really_down_alerts_after_grace(self):
        m, sent = monitor()
        self.run_msgs(m, [Msg("state", "unavailable", retain=True)])
        self.assertEqual(sent, ["sensor/pf=unavailable"])

    def test_monitor_saved_during_ha_restart_does_not_alert(self):
        m, sent = monitor()
        self.run_msgs(m, [Msg("state", "unavailable", retain=True),
                          Msg("state", "0.98")])
        self.assertEqual(sent, [])


if __name__ == "__main__":
    unittest.main()
