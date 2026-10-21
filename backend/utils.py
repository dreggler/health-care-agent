import json
import logging
import math
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import numpy as np
import pandas as pd


logger = logging.getLogger(__name__)

_TS_FMT = '%Y-%m-%dT%H:%M:%S'


def atomic_write_json(path: "str | os.PathLike[str]", data: Any, indent: int | None = 2) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(target.parent), prefix=f".{target.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f, indent=indent)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, target)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def ts_now() -> str:
    return datetime.now(timezone.utc).strftime(_TS_FMT)


def ts_fmt(dt: datetime) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime(_TS_FMT)


def parse_db_iso_utc(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s)
    except (TypeError, ValueError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


_warned_bad_tz: set[str] = set()


def app_timezone() -> ZoneInfo:
    from .config import settings
    tz_name = (settings.TIMEZONE or "UTC").strip() or "UTC"
    try:
        return ZoneInfo(tz_name)
    except Exception as exc:
        if tz_name not in _warned_bad_tz:
            _warned_bad_tz.add(tz_name)
            logger.warning(
                "TIMEZONE=%r is invalid (%s); falling back to UTC.",
                tz_name, exc,
            )
        return ZoneInfo("UTC")


def now_utc() -> datetime:
    return datetime.now(timezone.utc)


def now_local() -> datetime:
    return datetime.now(app_timezone())


def local_tz_line() -> str:
    from .config import settings
    tz = app_timezone()
    now = datetime.now(tz)
    raw = now.strftime("%z")
    offset = f"{raw[:3]}:{raw[3:]}" if raw else "+00:00"
    abbrev = now.strftime("%Z") or tz.key
    name = (settings.TIMEZONE or "UTC").strip() or "UTC"
    return f"Local TZ: {name} (UTC{offset}, {abbrev})"


def language_directive(sample_text: str, default_lang: str = "en") -> str:
    text = sample_text or ""
    cjk = sum(1 for ch in text if "一" <= ch <= "鿿")
    if cjk >= 2:
        lang = "Chinese"
    elif any(ch.isascii() and ch.isalpha() for ch in text):
        lang = "English"
    else:
        lang = "Chinese" if (default_lang or "en").lower().startswith("zh") else "English"
    return (
        f"Language: write everything the user will see — the report title, "
        f"content and digest — in {lang}, matching how the user talks to you."
    )


def serialize_value(value: Any) -> Any:
    if isinstance(value, np.ndarray):
        return value.tolist()
    if isinstance(value, (pd.Timestamp, np.datetime64)):
        return ts_fmt(pd.Timestamp(value).to_pydatetime())
    if isinstance(value, (np.integer, np.floating)):
        value = value.item()
    if isinstance(value, float) and not math.isfinite(value):
        return None
    try:
        if pd.isna(value):
            return None
    except (TypeError, ValueError):
        pass
    return value


def clean_dict_for_json(data: dict) -> dict:
    result = {}
    for key, value in data.items():
        if isinstance(value, dict):
            result[key] = clean_dict_for_json(value)
        elif isinstance(value, list):
            result[key] = [serialize_value(v) for v in value]
        else:
            result[key] = serialize_value(value)
    return result


def dataframe_to_json_safe(df: pd.DataFrame) -> list[dict[str, Any]]:
    if df.empty:
        return []

    df = df.copy()
    for col in df.columns:
        if pd.api.types.is_datetime64_any_dtype(df[col]):
            df[col] = df[col].apply(lambda x: ts_fmt(x.to_pydatetime()) if pd.notna(x) else None)

    return [clean_dict_for_json(row) for row in df.to_dict(orient='records')]
