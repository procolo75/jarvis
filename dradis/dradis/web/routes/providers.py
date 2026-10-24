"""
web/routes/providers.py
────────────────────────
Routes: model providers — per-provider limits and connection test.

The keys stay in the add-on Configuration tab (they are secrets, and the
Supervisor already stores them as passwords). What lives here is the plan behind
each key: its tokens-per-minute and its largest single request.
"""

import httpx
from fastapi import APIRouter, HTTPException

import core as agent_core
from web.store import (
    PROVIDERS,
    _get_provider_api_key,
    _get_provider_base_url,
    load_settings,
    provider_url_params,
    save_settings,
)
from web.models import ProviderLimitsPayload

router = APIRouter()


def _configured(provider_id: str) -> bool:
    if not _get_provider_api_key(provider_id):
        return False
    if provider_id == "cloudflare":
        return bool(provider_url_params().get("cloudflare_account_id", "").strip())
    return True


@router.get("/api/providers")
async def list_providers():
    limits = load_settings().get("provider_limits") or {}
    return [
        {
            "id":                  p["id"],
            "label":               p["label"],
            "configured":          _configured(p["id"]),
            "tpm":                 int((limits.get(p["id"]) or {}).get("tpm") or 0),
            "max_request":         int((limits.get(p["id"]) or {}).get("max_request") or 0),
            "default_tpm":         agent_core.PROVIDER_TPM_DEFAULT.get(p["id"], 0),
            "default_max_request": agent_core.PROVIDER_MAX_REQUEST_DEFAULT.get(p["id"], 0),
        }
        for p in PROVIDERS
    ]


@router.put("/api/providers/limits")
async def update_provider_limits(payload: ProviderLimitsPayload):
    known = {p["id"] for p in PROVIDERS}
    clean: dict = {}
    for pid, lim in payload.limits.items():
        if pid not in known:
            raise HTTPException(status_code=400, detail=f"Unknown provider: {pid}")
        entry = {k: max(0, int(lim.get(k) or 0)) for k in ("tpm", "max_request")}
        if any(entry.values()):
            clean[pid] = entry
    settings = load_settings()
    settings["provider_limits"] = clean
    save_settings(settings)
    return clean


@router.post("/api/providers/{provider_id}/test")
async def test_provider(provider_id: str):
    if provider_id not in {p["id"] for p in PROVIDERS}:
        raise HTTPException(status_code=400, detail=f"Unknown provider: {provider_id}")
    api_key = _get_provider_api_key(provider_id)
    if not api_key:
        raise HTTPException(status_code=400,
                            detail="API key not set in the add-on Configuration tab")
    headers = {"Authorization": f"Bearer {api_key}"}
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            if provider_id == "cloudflare":
                # Checks the account id and the token together: a token valid for
                # another account answers 403 here, not on the first chat turn.
                account = provider_url_params().get("cloudflare_account_id", "").strip()
                if not account:
                    raise HTTPException(status_code=400,
                                        detail="cloudflare_account_id not set in the add-on Configuration tab")
                resp = await client.get(
                    f"https://api.cloudflare.com/client/v4/accounts/{account}/ai/models/search",
                    params={"task": "Text Generation", "per_page": 100}, headers=headers,
                )
                if resp.status_code != 200:
                    return {"ok": False, "message": f"HTTP {resp.status_code}: {resp.text[:200]}"}
                count = len(resp.json().get("result") or [])
                return {"ok": True, "message": f"Cloudflare reachable — {count} text-generation model(s) on this account"}
            resp = await client.get(f"{_get_provider_base_url(provider_id).rstrip('/')}/models",
                                    headers=headers)
            if resp.status_code != 200:
                return {"ok": False, "message": f"HTTP {resp.status_code}: {resp.text[:200]}"}
            body = resp.json()
            data = body.get("data", []) if isinstance(body, dict) else body
            count = len(data) if isinstance(data, list) else 0
            return {"ok": True, "message": f"Reachable — {count} model(s) listed"}
    except HTTPException:
        raise
    except Exception as e:
        return {"ok": False, "message": f"{type(e).__name__}: {e}"}
