from __future__ import annotations

import argparse
import asyncio
import base64
import json
import logging
import os
import random
import struct
import time
from pathlib import Path
from typing import Any

import httpx

logger = logging.getLogger(__name__)

ILINK_BASE = "https://ilinkai.weixin.qq.com"
CHANNEL_VERSION = "1.0.2"
BOT_TYPE = 3
QR_STATUS_HEADERS = {"iLink-App-ClientVersion": "1"}


def wechat_uin_header() -> str:
    return base64.b64encode(struct.pack("I", random.randint(0, 0xFFFFFFFF))).decode("ascii")


def common_headers(bot_token: str | None = None) -> dict[str, str]:
    h = {
        "Content-Type": "application/json",
        "AuthorizationType": "ilink_bot_token",
        "X-WECHAT-UIN": wechat_uin_header(),
    }
    if bot_token:
        h["Authorization"] = f"Bearer {bot_token}"
    return h


async def _get_qr(client: httpx.AsyncClient) -> dict[str, Any]:
    resp = await client.get(
        f"{ILINK_BASE}/ilink/bot/get_bot_qrcode",
        params={"bot_type": BOT_TYPE},
        headers=common_headers(),
    )
    resp.raise_for_status()
    return resp.json()


async def _poll_qr(
    client: httpx.AsyncClient,
    qrcode: str,
    timeout: float = 120.0,
) -> dict[str, Any] | None:
    headers = {**common_headers(), **QR_STATUS_HEADERS}
    deadline = time.time() + timeout
    last_status = ""
    while time.time() < deadline:
        try:
            resp = await client.get(
                f"{ILINK_BASE}/ilink/bot/get_qrcode_status",
                params={"qrcode": qrcode},
                headers=headers,
            )
        except httpx.HTTPError as exc:
            logger.debug("qr_status transient error: %s", exc)
            await asyncio.sleep(2.0)
            continue
        if resp.status_code == 200:
            payload = resp.json()
            status = payload.get("status") or ""
            if status and status != last_status:
                print(f"  status: {status}")
                last_status = status
            if payload.get("bot_token") or status == "confirmed":
                return payload
            if status == "expired":
                logger.warning("QR code expired before scan")
                return None
        await asyncio.sleep(2.0)
    return None


def _print_qr(url_or_data: str) -> None:
    print(f"\n[WeChat ClawBot QR] {url_or_data}\n")
    try:
        import qrcode

        qr = qrcode.QRCode(border=1)
        qr.add_data(url_or_data)
        qr.make(fit=True)
        qr.print_ascii(invert=True)
    except ImportError:
        print(
            "(install `qrcode` to render the QR in your terminal — "
            "`pip install qrcode`)"
        )


async def run_qr_login(out_path: Path) -> bool:
    async with httpx.AsyncClient(timeout=30.0) as client:
        qr = await _get_qr(client)
        url = qr.get("qrcode_img_content") or ""
        qrcode = qr.get("qrcode") or ""
        if not url or not qrcode:
            logger.error("Unexpected QR response from iLink: %s", qr)
            return False

        print(
            "\nOpen WeChat → Settings → Plugins → ClawBot, then scan this QR:"
        )
        _print_qr(url)
        print("Waiting for scan + confirm (timeout 120s)...")

        result = await _poll_qr(client, qrcode)
        if not result:
            print("Timed out without a scan.")
            return False

        bot_token = result.get("bot_token") or ""
        if not bot_token:
            logger.error("QR confirmed but no bot_token in response: %s", result)
            return False

        out_path.parent.mkdir(parents=True, exist_ok=True)
        record = {
            "bot_token": bot_token,
            "ilink_bot_id": result.get("ilink_bot_id", ""),
            "ilink_user_id": result.get("ilink_user_id", ""),
            "baseurl": result.get("baseurl", ""),
        }
        fd = os.open(
            str(out_path), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600,
        )
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False, indent=2))
        try:
            out_path.chmod(0o600)
        except OSError:
            pass
        print(f"\nbot_token saved to {out_path}")
        if record["ilink_user_id"]:
            print(f"  authorised user_id (scanner): {record['ilink_user_id']}")
        return True


def load_bot_record(path: Path) -> dict[str, Any]:
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        logger.warning("Failed to read WeChat bot_token from %s: %s", path, exc)
        return {}
    return data if isinstance(data, dict) else {}


def load_bot_token(path: Path) -> str | None:
    return load_bot_record(path).get("bot_token") or None


def resolve_base_url(record_baseurl: str | None) -> str:
    url = (record_baseurl or "").strip().rstrip("/")
    if url.startswith("https://") and len(url) > len("https://"):
        return url
    return ILINK_BASE


def _main() -> int:
    parser = argparse.ArgumentParser(
        description="WeChat ClawBot one-time QR login (iLink bot API)",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help=(
            "Path to write the bot_token JSON "
            "(default: WEIXIN_BOT_TOKEN_PATH or ./data/weixin_bot_token.json)"
        ),
    )
    args = parser.parse_args()

    out_path = args.out
    if out_path is None:
        try:
            from ..config import settings

            out_path = Path(settings.WEIXIN_BOT_TOKEN_PATH)
        except Exception:
            out_path = Path("./data/weixin_bot_token.json")

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    ok = asyncio.run(run_qr_login(out_path))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(_main())
