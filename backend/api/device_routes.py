from __future__ import annotations

import asyncio
import logging
import re

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from ..ios_gateway import device_store

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/devices", tags=["devices"])

_USER_ID = "LiveUser"


_TOKEN_RE = re.compile(r"^[0-9a-fA-F]{32,200}$")
_ENVIRONMENTS = frozenset({"sandbox", "production"})


class DeviceTokenRequest(BaseModel):
    device_token: str
    bundle_id: str | None = Field(default=None, max_length=255)
    environment: str = "production"


def _clean_token(raw: str) -> str:
    token = (raw or "").strip()
    if not token:
        raise HTTPException(status_code=400, detail="Missing device_token")
    if not _TOKEN_RE.match(token):
        raise HTTPException(status_code=400, detail="device_token must be a hex string")
    return token


@router.post("/register")
async def register_device(body: DeviceTokenRequest):
    token = _clean_token(body.device_token)
    environment = (body.environment or "").strip().lower()
    if environment not in _ENVIRONMENTS:
        raise HTTPException(
            status_code=400, detail="environment must be 'sandbox' or 'production'"
        )
    await asyncio.to_thread(
        device_store.upsert_device_token, _USER_ID, token, body.bundle_id, environment,
    )
    return {"success": True}


@router.post("/unregister")
async def unregister_device(body: DeviceTokenRequest):
    token = _clean_token(body.device_token)
    await asyncio.to_thread(device_store.revoke_device_token, token)
    return {"success": True}
