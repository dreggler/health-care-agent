from __future__ import annotations

import asyncio
import logging
import math
import os
import random
from dataclasses import dataclass

import httpx
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.primitives.padding import PKCS7

logger = logging.getLogger(__name__)

CDN_BASE_URL = "https://novac2c.cdn.weixin.qq.com/c2c"
_UPLOAD_MAX_RETRIES = 3
_UPLOAD_RETRY_BASE_DELAY = 0.5


class CDNUploadError(RuntimeError):
    pass


class CDNClientError(CDNUploadError):
    pass


class CDNServerError(CDNUploadError):
    pass


@dataclass
class UploadedImage:

    encrypt_query_param: str
    aeskey_hex: str
    plaintext_size: int
    ciphertext_size: int


def aes_ecb_padded_size(plaintext_size: int) -> int:
    return math.ceil((plaintext_size + 1) / 16) * 16


def encrypt_aes_ecb(plaintext: bytes, key: bytes) -> bytes:
    padder = PKCS7(128).padder()
    padded = padder.update(plaintext) + padder.finalize()
    encryptor = Cipher(algorithms.AES(key), modes.ECB()).encryptor()
    return encryptor.update(padded) + encryptor.finalize()


def _build_upload_url(upload_full_url: str | None, upload_param: str | None,
                      filekey: str) -> str:
    full = (upload_full_url or "").strip()
    if full:
        return full
    if not upload_param:
        raise RuntimeError(
            "iLink getuploadurl returned neither upload_full_url nor "
            "upload_param — cannot construct CDN target.",
        )
    from urllib.parse import quote
    return (
        f"{CDN_BASE_URL}/upload"
        f"?encrypted_query_param={quote(upload_param)}"
        f"&filekey={quote(filekey)}"
    )


async def upload_ciphertext(
    client: httpx.AsyncClient,
    upload_full_url: str | None,
    upload_param: str | None,
    filekey: str,
    ciphertext: bytes,
) -> str:
    target = _build_upload_url(upload_full_url, upload_param, filekey)
    last_err: Exception | None = None
    for attempt in range(1, _UPLOAD_MAX_RETRIES + 1):
        try:
            resp = await client.post(
                target,
                content=ciphertext,
                headers={"Content-Type": "application/octet-stream"},
            )
            if 400 <= resp.status_code < 500:
                msg = resp.headers.get("x-error-message", resp.text[:200])
                raise CDNClientError(
                    f"CDN upload client error {resp.status_code}: {msg}",
                )
            if resp.status_code != 200:
                raise CDNServerError(
                    f"CDN upload server error {resp.status_code}: "
                    f"{resp.headers.get('x-error-message', '')[:200]}",
                )
            ref = resp.headers.get("x-encrypted-param")
            if not ref:
                raise CDNServerError(
                    "CDN upload succeeded but response is missing the "
                    "x-encrypted-param header — cannot reference the upload.",
                )
            return ref
        except CDNClientError:
            raise
        except Exception as exc:
            last_err = exc
            if attempt < _UPLOAD_MAX_RETRIES:
                delay = _UPLOAD_RETRY_BASE_DELAY * (2 ** (attempt - 1))
                delay *= 0.5 + random.random()
                logger.warning(
                    "CDN upload attempt %d/%d failed (retry in %.1fs): %s",
                    attempt, _UPLOAD_MAX_RETRIES, delay, exc,
                )
                await asyncio.sleep(delay)
            else:
                logger.error(
                    "CDN upload failed after %d attempts: %s",
                    _UPLOAD_MAX_RETRIES, exc,
                )
    raise last_err or CDNServerError("CDN upload failed (no error captured)")


def new_filekey() -> str:
    return os.urandom(16).hex()


def new_aeskey() -> bytes:
    return os.urandom(16)
