"""
live_monitors/blink.py
──────────────────────
Forwards Blink motion events from the blink-mqtt add-on to Telegram as the
recorded clip.

Division of labour
──────────────────
blink-mqtt owns the Blink session (login, OTP, token refresh) and does all the
Blink work: when a new motion clip has finished recording — also with Sync
Module USB storage and no subscription — it publishes, never retained:

    {prefix}/cameras/{serial}/event/clip       MP4 bytes (~1-3 MB for 10 s)

plus the retained display name on {prefix}/cameras/{serial}/name.
JARVIS owns Telegram, so this module only listens and forwards. No LLM is
involved: a motion clip needs no wording, and every event would cost tokens.
There is no polling here: the broker pushes each clip to this subscriber.

No photo
────────
blink-mqtt < 1.4.0 also published {prefix}/cameras/{serial}/event/snapshot.
It was the camera's home-screen thumbnail, which a motion clip does not
refresh, so it showed an older scene. It is ignored even if an older add-on
still sends it (v5.1.1).

Cooldown
────────
Per camera, decided on the clip. A cooldown of 0 sends every event.

Car Mode
────────
A car cannot play a video, and CarPlay reads a media notification as "Image"
or not at all. In Car Mode the event becomes one text line (send_telegram
rewrites it for speech) and the clip is dropped.
"""

import asyncio
import logging
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Awaitable, Callable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import aiomqtt

_LOGGER = logging.getLogger(__name__)

RECONNECT_DELAY = 15
DEFAULT_PREFIX  = "blink"

_TEXTS = {
    "it": {"motion": "📷 Movimento — {name} · {time}", "clip": "🎬 Movimento — {name} · {time}"},
    "en": {"motion": "📷 Motion — {name} · {time}",    "clip": "🎬 Motion — {name} · {time}"},
}


def parse_topic(prefix: str, topic: str) -> tuple[str, str] | None:
    """`(serial, kind)` for the topics this monitor uses, else None.

    kind is "name" or "clip".
    """
    head = f"{prefix}/cameras/"
    if not topic.startswith(head):
        return None
    parts = topic[len(head):].split("/")
    if len(parts) == 2 and parts[1] == "name":
        return parts[0], "name"
    if len(parts) == 3 and parts[1] == "event" and parts[2] == "clip":
        return parts[0], "clip"
    return None


@dataclass
class BlinkSender:
    """Telegram side, injected so the monitor can be tested without a bot."""
    video:    Callable[[bytes, str], Awaitable[object]]
    text:     Callable[[str], Awaitable[object]]
    car_mode: Callable[[], bool]


class BlinkMonitor:
    """One MQTT listener for all Blink cameras."""

    def __init__(self, cfg: dict, mqtt_cfg: dict, sender: BlinkSender, tz_name: str = "UTC"):
        self.prefix     = (cfg.get("mqtt_prefix") or DEFAULT_PREFIX).strip().rstrip("/")
        # Empty = every camera. Entries match the serial or the display name.
        self.cameras    = {c.strip().lower() for c in cfg.get("cameras", []) if c.strip()}
        self.cooldown_s = max(0.0, float(cfg.get("cooldown_s", 0) or 0))
        lang            = cfg.get("language", "it")
        self.texts      = _TEXTS.get(lang, _TEXTS["en"])
        try:
            self.tz = ZoneInfo(tz_name or "UTC")
        except ZoneInfoNotFoundError:
            self.tz = ZoneInfo("UTC")

        self._mqtt   = mqtt_cfg
        self._send   = sender
        self._names: dict[str, str]     = {}
        self._last:  dict[str, float]   = {}   # serial → monotonic time of last alert
        self._task: asyncio.Task | None = None

    # ── lifecycle ─────────────────────────────────────────────────────────────

    def start(self):
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._run(), name="blink_monitor")

    def stop(self):
        if self._task and not self._task.done():
            self._task.cancel()

    def is_running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def _run(self):
        kwargs = {}
        if self._mqtt.get("mqtt_username"):
            kwargs["username"] = self._mqtt["mqtt_username"]
        if self._mqtt.get("mqtt_password"):
            kwargs["password"] = self._mqtt["mqtt_password"]
        host = self._mqtt.get("mqtt_host", "core-mosquitto")
        port = int(self._mqtt.get("mqtt_port", 1883))
        while True:
            try:
                async with aiomqtt.Client(host, port, **kwargs) as client:
                    await client.subscribe(f"{self.prefix}/cameras/+/name")
                    # QoS 1 end to end, matching the publisher: a 2 MB clip must
                    # not be dropped silently.
                    await client.subscribe(f"{self.prefix}/cameras/+/event/clip", qos=1)
                    async for message in client.messages:
                        try:
                            await self.handle(str(message.topic), message.payload,
                                              bool(getattr(message, "retain", False)))
                        except Exception as e:
                            _LOGGER.warning("[Blink] event error: %r", e)
            except asyncio.CancelledError:
                return
            except Exception as e:
                _LOGGER.warning("[Blink] disconnected: %s — retry in %ds", e, RECONNECT_DELAY)
                await asyncio.sleep(RECONNECT_DELAY)

    # ── event logic ───────────────────────────────────────────────────────────

    def _wanted(self, serial: str) -> bool:
        if not self.cameras:
            return True
        return serial.lower() in self.cameras or self._name(serial).lower() in self.cameras

    def _name(self, serial: str) -> str:
        return self._names.get(serial, serial)

    def _admit(self, serial: str, now: float) -> bool:
        last = self._last.get(serial)
        if last is not None and now - last < self.cooldown_s:
            return False
        self._last[serial] = now
        return True

    async def handle(self, topic: str, payload: bytes, retain: bool = False,
                     now: float | None = None) -> None:
        parsed = parse_topic(self.prefix, topic)
        if not parsed:
            return
        serial, kind = parsed
        if kind == "name":
            self._names[serial] = payload.decode("utf-8", errors="replace").strip() or serial
            return
        # Events are never retained by blink-mqtt; a retained one would be an old
        # event replayed on every reconnect.
        if retain or not payload or not self._wanted(serial):
            return

        now = time.monotonic() if now is None else now
        stamp = datetime.now(self.tz).strftime("%H:%M")
        name  = self._name(serial)

        # kind == "clip"
        if not self._admit(serial, now):
            return
        if self._send.car_mode():
            await self._send.text(self.texts["motion"].format(name=name, time=stamp))
            return
        await self._send.video(bytes(payload), self.texts["clip"].format(name=name, time=stamp))


class BlinkMonitorManager:
    """Owns the single Blink monitor. Reloaded on startup and on config changes."""

    def __init__(self):
        self._monitor: BlinkMonitor | None = None

    def reload(self, cfg: dict, mqtt_cfg: dict, sender: BlinkSender, tz_name: str) -> None:
        self.stop_all()
        if not cfg.get("enabled"):
            return
        self._monitor = BlinkMonitor(cfg, mqtt_cfg, sender, tz_name)
        self._monitor.start()

    def stop_all(self) -> None:
        if self._monitor:
            self._monitor.stop()
            self._monitor = None

    def status(self) -> str:
        return "running" if self._monitor and self._monitor.is_running() else "stopped"


blink_monitor_manager = BlinkMonitorManager()
