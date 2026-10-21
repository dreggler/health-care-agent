from __future__ import annotations

import logging
import os
import shutil
import threading
import time
import uuid
from pathlib import Path

logger = logging.getLogger(__name__)

_TTL_S = 3600.0
_DURABLE_MAX_AGE_S = 90 * 24 * 3600.0


def _chat_images_dir(user_id: str) -> Path:
    from ..config import settings
    return Path(settings.DATA_STORE_PATH) / user_id / "chat_images"


class ImageStore:

    def __init__(self) -> None:
        self._items: dict[str, tuple[str, str, float]] = {}
        self._lock = threading.Lock()

    def register(self, user_id: str, path: str) -> str:
        image_id = uuid.uuid4().hex
        durable = self._persist(user_id, image_id, path)
        stored = durable or path
        with self._lock:
            self._gc_locked()
            self._items[image_id] = (user_id, stored, time.monotonic() + _TTL_S)
        return image_id

    def _persist(self, user_id: str, image_id: str, src: str) -> str | None:
        try:
            ext = os.path.splitext(src)[1].lower() or ".png"
            dest_dir = _chat_images_dir(user_id)
            dest_dir.mkdir(parents=True, exist_ok=True)
            self._reap_old(dest_dir)
            dest = dest_dir / f"{image_id}{ext}"
            shutil.copyfile(src, dest)
            return str(dest)
        except Exception as e:
            logger.warning("chat-image persist failed for user=%s: %s", user_id, e)
            return None

    @staticmethod
    def _reap_old(dest_dir: Path) -> None:
        try:
            now = time.time()
            for f in dest_dir.iterdir():
                try:
                    if now - f.stat().st_mtime > _DURABLE_MAX_AGE_S:
                        f.unlink()
                except Exception:
                    pass
        except Exception:
            pass

    def get(self, image_id: str, user_id: str) -> str | None:
        with self._lock:
            item = self._items.get(image_id)
            if item is None:
                return None
            owner, path, expiry = item
            if owner != user_id:
                return None
            if time.monotonic() > expiry:
                self._items.pop(image_id, None)
                return None
            return path

    @staticmethod
    def durable_path(image_id: str, user_id: str) -> str | None:
        if not image_id or not image_id.isalnum():
            return None
        try:
            d = _chat_images_dir(user_id)
            for f in d.glob(f"{image_id}.*"):
                if f.is_file():
                    return str(f)
        except Exception:
            pass
        return None

    def _gc_locked(self) -> None:
        now = time.monotonic()
        for k in [k for k, (_, _, exp) in self._items.items() if now > exp]:
            self._items.pop(k, None)


image_store = ImageStore()
