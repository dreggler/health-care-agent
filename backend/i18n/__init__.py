from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from backend.config import settings

logger = logging.getLogger(__name__)

_LOCALES_DIR = Path(__file__).parent / "locales"
_FALLBACK_LANG = "en"

_TRANSLATIONS: dict[str, dict[str, str]] = {}
_DEFAULT_LANG: str = _FALLBACK_LANG


def _flatten(prefix: str, obj: Any, out: dict[str, str]) -> None:
    if isinstance(obj, dict):
        for k, v in obj.items():
            new_key = f"{prefix}.{k}" if prefix else k
            _flatten(new_key, v, out)
    else:
        out[prefix] = str(obj)


def _load_locale(lang: str) -> dict[str, str]:
    path = _LOCALES_DIR / f"{lang}.json"
    if not path.exists():
        logger.warning("i18n: locale file missing: %s", path)
        return {}
    try:
        with path.open("r", encoding="utf-8") as f:
            raw = json.load(f)
    except (OSError, json.JSONDecodeError) as exc:
        logger.error("i18n: failed to load %s: %s", path, exc)
        return {}
    flat: dict[str, str] = {}
    _flatten("", raw, flat)
    return flat


def _init_translations() -> None:
    global _DEFAULT_LANG
    if not _LOCALES_DIR.exists():
        logger.warning("i18n: locales directory not found: %s", _LOCALES_DIR)
        return
    for path in sorted(_LOCALES_DIR.glob("*.json")):
        lang = path.stem.lower()
        _TRANSLATIONS[lang] = _load_locale(lang)
        logger.debug("i18n: loaded %d keys for %s", len(_TRANSLATIONS[lang]), lang)

    configured = (settings.DEFAULT_USER_LANGUAGE or _FALLBACK_LANG).lower().strip()
    if configured and configured in _TRANSLATIONS:
        _DEFAULT_LANG = configured
    else:
        if configured and configured != _FALLBACK_LANG:
            logger.warning(
                "i18n: DEFAULT_USER_LANGUAGE=%r not available; falling back to %s",
                configured, _FALLBACK_LANG,
            )
        _DEFAULT_LANG = _FALLBACK_LANG


_init_translations()


def available_languages() -> list[str]:
    return sorted(_TRANSLATIONS.keys())


def set_default_language(lang: str) -> None:
    global _DEFAULT_LANG
    lang_norm = (lang or "").lower().strip()
    if lang_norm not in _TRANSLATIONS:
        logger.warning(
            "i18n: set_default_language(%r) ignored — locale not loaded", lang,
        )
        return
    _DEFAULT_LANG = lang_norm


def t(key: str, *, lang: str | None = None, **kwargs: Any) -> str:
    target = (lang or _DEFAULT_LANG).lower().strip()

    translations = _TRANSLATIONS.get(target)
    value: str | None = None
    if translations is not None:
        value = translations.get(key)

    if value is None and target != _FALLBACK_LANG:
        logger.warning(
            "i18n: missing key %r for lang=%s; falling back to %s",
            key, target, _FALLBACK_LANG,
        )
        fallback = _TRANSLATIONS.get(_FALLBACK_LANG, {})
        value = fallback.get(key)

    if value is None:
        logger.warning("i18n: unknown key %r (no translation available)", key)
        value = key

    if kwargs:
        try:
            return value.format(**kwargs)
        except (KeyError, IndexError, ValueError) as exc:
            logger.warning(
                "i18n: format failed for key %r (%s); returning raw template",
                key, exc,
            )
            return value
    return value


__all__ = ["t", "available_languages", "set_default_language"]
