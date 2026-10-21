import logging
from functools import lru_cache
from pathlib import Path

logger = logging.getLogger(__name__)

PROMPTS_DIR = Path(__file__).resolve().parent.parent.parent / "prompts"


def safe_format(template: str, **kwargs: object) -> str:
    _L = "\x00L\x00"
    _R = "\x00R\x00"
    result = template.replace("{{", _L).replace("}}", _R)

    for key, value in kwargs.items():
        result = result.replace(f"{{{key}}}", str(value))

    return result.replace(_L, "{").replace(_R, "}")


def load_prompt(name: str, *, cache: bool = True) -> str:
    if cache:
        return _load_cached(name)
    return _load_uncached(name)


@lru_cache(maxsize=32)
def _load_cached(name: str) -> str:
    return _load_uncached(name)


def _load_uncached(name: str) -> str:
    path = PROMPTS_DIR / name
    if not path.exists():
        logger.warning("Prompt file not found: %s", path)
        return ""
    try:
        text = path.read_text(encoding="utf-8").strip()
        logger.debug("Loaded prompt %s (%d chars)", name, len(text))
        return text
    except Exception as e:
        logger.error("Failed to read prompt %s: %s", name, e)
        return ""


def load_and_format(name: str, *, cache: bool = True, **kwargs: object) -> str:
    template = load_prompt(name, cache=cache)
    if not template:
        return ""
    return safe_format(template, **kwargs)


def clear_cache() -> None:
    _load_cached.cache_clear()
