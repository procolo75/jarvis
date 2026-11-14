"""
web/routes/blink.py
───────────────────
Blink panel: forwards motion snapshot + clip published by the blink-mqtt add-on
to Telegram (see live_monitors/blink.py).

GET  /api/blink-monitor         current config (defaults filled in)
PUT  /api/blink-monitor         save config and restart the listener
GET  /api/blink-monitor/status  "running" / "stopped"
POST /api/blink-monitor/test    broker + add-on check, then a test message
"""

import asyncio

import aiomqtt
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

import web.store as _store
from web.store import load_blink_monitor, save_blink_monitor, load_settings

router = APIRouter()

SNIFF_SECONDS = 3.0


class BlinkMonitorPayload(BaseModel):
    enabled:         bool  = False
    telegram_bot_id: str   = "default"
    mqtt_prefix:     str   = "blink"
    cameras:         list[str] = []
    cooldown_s:      float = 0
    language:        str   = "it"


@router.get("/api/blink-monitor")
async def get_blink_monitor():
    return load_blink_monitor()


@router.put("/api/blink-monitor")
async def put_blink_monitor(payload: BlinkMonitorPayload):
    cfg = payload.model_dump()
    cfg["mqtt_prefix"] = (cfg["mqtt_prefix"] or "blink").strip().rstrip("/")
    cfg["cameras"]     = [c.strip() for c in cfg["cameras"] if c.strip()]
    cfg["cooldown_s"]  = max(0.0, float(cfg["cooldown_s"] or 0))
    save_blink_monitor(cfg)
    _store._notify_blink_monitor_changed()
    return cfg


@router.get("/api/blink-monitor/status")
async def blink_monitor_status():
    fn = _store._get_blink_monitor_status_fn
    return {"status": fn() if fn else "unknown"}


async def _sniff(prefix: str) -> tuple[str | None, dict[str, str]]:
    """Retained `{prefix}/availability` and camera names, read for a few seconds."""
    s = load_settings()
    kwargs = {}
    if s.get("mqtt_username"):
        kwargs["username"] = s["mqtt_username"]
    if s.get("mqtt_password"):
        kwargs["password"] = s["mqtt_password"]
    availability: str | None = None
    names: dict[str, str] = {}
    async with aiomqtt.Client(s.get("mqtt_host", "core-mosquitto"),
                              int(s.get("mqtt_port", 1883)), **kwargs) as client:
        await client.subscribe(f"{prefix}/availability")
        await client.subscribe(f"{prefix}/cameras/+/name")

        async def _collect():
            nonlocal availability
            async for m in client.messages:
                topic = str(m.topic)
                text  = bytes(m.payload).decode("utf-8", errors="replace").strip()
                if topic == f"{prefix}/availability":
                    availability = text
                else:
                    names[topic.split("/")[-2]] = text

        try:
            await asyncio.wait_for(_collect(), timeout=SNIFF_SECONDS)
        except asyncio.TimeoutError:
            pass
    return availability, names


@router.post("/api/blink-monitor/test")
async def test_blink_monitor():
    cfg    = load_blink_monitor()
    prefix = (cfg.get("mqtt_prefix") or "blink").rstrip("/")
    try:
        availability, names = await _sniff(prefix)
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"MQTT broker: {e}")
    if availability is None:
        raise HTTPException(status_code=400,
                            detail=f"No '{prefix}/availability' on the broker — is blink-mqtt "
                                   f"(≥ 1.3.1) running and is the prefix right?")

    import bot.state as _state
    cams = ", ".join(sorted(names.values())) or "—"
    sent = await _state.send_telegram(
        f"📷 Blink test — add-on {availability}, cameras: {cams}",
        bot_id=cfg.get("telegram_bot_id", "default"), lang=cfg.get("language", "it"))
    return {"ok": True, "availability": availability, "cameras": names,
            "telegram": sent is not False}
