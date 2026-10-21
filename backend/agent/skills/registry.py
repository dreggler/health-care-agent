from __future__ import annotations

import json
import logging
from collections.abc import Iterable
from pathlib import Path

from .loader import SkillEntry, discover_skills

logger = logging.getLogger(__name__)

STATE_FILENAME = ".skill_state.json"


class SkillRegistry:

    def __init__(self, roots: Iterable[Path | str]) -> None:
        self._roots: list[Path] = []
        seen: set[str] = set()
        for r in roots:
            if r is None:
                continue
            p = Path(r).expanduser()
            key = str(p.resolve()) if p.exists() else str(p)
            if key in seen:
                continue
            seen.add(key)
            if p.exists() and p.is_dir():
                self._roots.append(p)
            else:
                logger.debug("Skill root %s does not exist; skipping", p)
        self._entries: list[SkillEntry] = []
        self.refresh()


    def refresh(self) -> None:
        try:
            entries = discover_skills(self._roots)
            disabled = self._load_disabled_set()
            for e in entries:
                e.enabled = e.name not in disabled
            self._entries = entries
            logger.info(
                "SkillRegistry: discovered %d skill(s) (%d enabled) across %d root(s)",
                len(self._entries),
                sum(1 for e in self._entries if e.enabled),
                len(self._roots),
            )
        except Exception as exc:
            logger.error("SkillRegistry refresh failed: %s", exc, exc_info=True)
            self._entries = []

    def set_disabled(self, disabled: Iterable[str]) -> None:
        names = sorted({n for n in disabled if isinstance(n, str) and n})
        if len(names) > 3000:
            names = names[:3000]
            logger.warning("set_disabled truncated to 3000 entries")
        self._save_disabled_set(set(names))
        for e in self._entries:
            e.enabled = e.name not in names


    @property
    def roots(self) -> list[Path]:
        return list(self._roots)

    @property
    def primary_root(self) -> Path | None:
        return self._roots[0] if self._roots else None

    def get(self, name: str) -> SkillEntry | None:
        if not name:
            return None
        for entry in self._entries:
            if entry.name == name:
                return entry
        return None

    def list_all(self) -> list[SkillEntry]:
        return list(self._entries)

    def list_enabled(self) -> list[SkillEntry]:
        return [e for e in self._entries if e.enabled]


    def _state_path(self) -> Path | None:
        root = self.primary_root
        if root is None:
            return None
        return root / STATE_FILENAME

    def _load_disabled_set(self) -> set[str]:
        path = self._state_path()
        if path is None or not path.exists():
            return set()
        try:
            raw = path.read_text(encoding="utf-8")
            data = json.loads(raw)
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning("Could not read skill state %s: %s", path, exc)
            return set()
        if not isinstance(data, dict):
            return set()
        names = data.get("disabled")
        if not isinstance(names, list):
            return set()
        return {n for n in names if isinstance(n, str) and n}

    def _save_disabled_set(self, names: set[str]) -> None:
        path = self._state_path()
        if path is None:
            logger.warning("No primary skill root; cannot persist disabled set")
            return
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(path.suffix + ".tmp")
            tmp.write_text(
                json.dumps({"disabled": sorted(names)}, indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            tmp.replace(path)
        except OSError as exc:
            logger.error("Failed to persist skill state %s: %s", path, exc)
