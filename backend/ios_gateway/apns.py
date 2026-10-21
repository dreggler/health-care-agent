from __future__ import annotations

import asyncio
import logging

from . import device_store

logger = logging.getLogger(__name__)

_SEND_TIMEOUT_S = 10.0


class APNSSender:

    def __init__(self, settings) -> None:
        self._settings = settings
        self._clients: dict[str, object] = {}
        self._key: str | None = None
        self._enabled = bool(getattr(settings, "APNS_ENABLED", False))
        self._lock = asyncio.Lock()

    @property
    def enabled(self) -> bool:
        return self._enabled

    def _env_of(self, token: dict) -> str:
        default = getattr(self._settings, "APNS_ENV", "production") or "production"
        env = str(token.get("environment") or default).lower()
        return "sandbox" if env == "sandbox" else "production"

    async def _get_client(self, env: str):
        if env in self._clients:
            return self._clients[env]
        async with self._lock:
            if env in self._clients:
                return self._clients[env]
            try:
                from aioapns import APNs
            except Exception as e:
                logger.warning(
                    "APNS_ENABLED but aioapns is not installed (%s) — "
                    "run `pip install aioapns`. Push disabled.", e,
                )
                self._enabled = False
                return None
            s = self._settings
            if self._key is None:
                try:
                    with open(s.APNS_KEY_PATH) as f:
                        self._key = f.read()
                except OSError as e:
                    logger.error("APNs: cannot read key file %s: %s", s.APNS_KEY_PATH, e)
                    self._enabled = False
                    return None
            try:
                client = APNs(
                    key=self._key,
                    key_id=s.APNS_KEY_ID,
                    team_id=s.APNS_TEAM_ID,
                    topic=s.APNS_BUNDLE_ID,
                    use_sandbox=(env == "sandbox"),
                )
            except Exception as e:
                logger.error("APNs client init failed (%s): %s", env, e)
                self._enabled = False
                return None
            self._clients[env] = client
            return client

    async def send(
        self, user_id: str, title: str, body: str, data: dict | None = None,
        time_sensitive: bool = False,
    ) -> int:
        if not self._enabled:
            return 0
        tokens = await asyncio.to_thread(device_store.list_device_tokens, user_id)
        if not tokens:
            return 0
        try:
            from aioapns import NotificationRequest, PushType
        except Exception:
            return 0

        sent = 0
        for t in tokens:
            device_token = t["device_token"]
            client = await self._get_client(self._env_of(t))
            if client is None:
                return sent
            try:
                level = "time-sensitive" if time_sensitive else "active"
                message = {
                    **(data or {}),
                    "aps": {
                        "alert": {"title": title, "body": body},
                        "sound": "default",
                        "interruption-level": level,
                    },
                }
                req = NotificationRequest(
                    device_token=device_token,
                    message=message,
                    push_type=PushType.ALERT,
                )
                resp = await asyncio.wait_for(
                    client.send_notification(req), timeout=_SEND_TIMEOUT_S,
                )
                if getattr(resp, "is_successful", False):
                    sent += 1
                else:
                    desc = str(getattr(resp, "description", ""))
                    status = str(getattr(resp, "status", ""))
                    if status == "410" or desc in ("Unregistered", "BadDeviceToken"):
                        await asyncio.to_thread(device_store.revoke_device_token, device_token)
                        logger.info(
                            "APNs: revoked dead %s token (%s) for user=%s",
                            self._env_of(t), desc or status, user_id,
                        )
            except asyncio.TimeoutError:
                logger.warning(
                    "APNs send timed out after %.0fs for user=%s", _SEND_TIMEOUT_S, user_id,
                )
            except Exception as e:
                logger.warning("APNs send failed for user=%s: %s", user_id, e)
        return sent
