from __future__ import annotations

import asyncio
import logging
import os
from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from ..agent.skills import SKILL_NAME_RE, SkillRegistry
from ..config import settings
from .agent_state import active_agents

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/skills", tags=["skills"])


def _resolve_skill_roots() -> list[Path]:
    roots: list[Path] = []
    raw = getattr(settings, "HIME_SKILLS_DIR", "") or ""
    for piece in raw.split(os.pathsep):
        piece = piece.strip()
        if piece:
            roots.append(Path(piece).expanduser())
    roots.append(Path.home() / ".hime" / "skills")
    roots.append(Path("./skills"))
    return roots


def _build_registry() -> SkillRegistry:
    return SkillRegistry(roots=_resolve_skill_roots())


def _writable_root() -> Path:
    for r in _resolve_skill_roots():
        try:
            if r.exists() and r.is_dir():
                return r
        except OSError:
            continue
    fallback = Path("./skills")
    fallback.mkdir(parents=True, exist_ok=True)
    return fallback


def _validate_name(name: str) -> None:
    if not isinstance(name, str) or not SKILL_NAME_RE.match(name):
        raise HTTPException(
            status_code=400,
            detail=(
                f"Invalid skill name '{name}'. "
                f"Must match {SKILL_NAME_RE.pattern} (lowercase letters, digits, "
                "hyphen, underscore)."
            ),
        )


def _resolve_path(name: str) -> Path:
    _validate_name(name)
    root = _writable_root().resolve()
    candidate = (root / f"{name}.md").resolve()
    try:
        candidate.relative_to(root)
    except ValueError:
        raise HTTPException(status_code=400, detail="Path traversal rejected")
    return candidate


def _existing_skill_path(name: str) -> Path | None:
    _validate_name(name)
    entry = _build_registry().get(name)
    if entry is None:
        return None
    candidate = Path(entry.file_path).resolve()
    for root in _resolve_skill_roots():
        try:
            candidate.relative_to(root.resolve())
            return candidate
        except (ValueError, OSError):
            continue
    logger.warning("Skill %s resolved outside every skill root: %s", name, candidate)
    return None


def _refresh_running_agents() -> None:
    for info in active_agents.values():
        agent = info.get("agent")
        registry = getattr(agent, "skill_registry", None)
        if registry is not None:
            try:
                registry.refresh()
            except Exception as exc:
                logger.warning("SkillRegistry refresh failed for running agent: %s", exc)


def _build_markdown(description: str, body: str) -> str:
    safe_desc = (
        description.strip()
        .replace("\\", "\\\\")
        .replace('"', '\\"')
        .replace("\r\n", " ")
        .replace("\r", " ")
        .replace("\n", " ")
    )
    body_clean = body.rstrip() + "\n" if body else ""
    return f'---\ndescription: "{safe_desc}"\n---\n{body_clean}'


class SkillCreate(BaseModel):
    name: str = Field(..., description="Filename stem; must match ^[a-z0-9_-]+$")
    description: str = Field(..., min_length=1)
    body: str = Field(default="", description="Markdown body (without frontmatter)")


class SkillUpdate(BaseModel):
    description: str = Field(..., min_length=1)
    body: str = Field(default="")


class SkillStateUpdate(BaseModel):
    disabled: list[str] = Field(default_factory=list)


@router.get("")
async def list_skills():
    reg = await asyncio.to_thread(_build_registry)
    items = [
        {
            "name": e.name,
            "description": e.description,
            "file_path": str(e.file_path),
            "enabled": e.enabled,
        }
        for e in reg.list_all()
    ]
    return {
        "success": True,
        "skills": items,
        "writable_root": str(_writable_root()),
        "enabled_count": sum(1 for e in reg.list_all() if e.enabled),
        "total_count": len(reg.list_all()),
    }


@router.put("/state")
async def update_skill_state(payload: SkillStateUpdate):
    for n in payload.disabled:
        if not isinstance(n, str) or not SKILL_NAME_RE.match(n):
            raise HTTPException(
                status_code=400,
                detail=f"Invalid skill name in disabled list: {n!r}",
            )
    reg = await asyncio.to_thread(_build_registry)
    await asyncio.to_thread(reg.set_disabled, payload.disabled)
    _refresh_running_agents()
    return {
        "success": True,
        "disabled_count": len(payload.disabled),
        "enabled_count": sum(1 for e in reg.list_all() if e.enabled),
    }


@router.get("/{name}")
async def get_skill(name: str):
    _validate_name(name)
    reg = await asyncio.to_thread(_build_registry)
    entry = reg.get(name)
    if entry is None:
        raise HTTPException(status_code=404, detail=f"Skill '{name}' not found")
    try:
        raw = await asyncio.to_thread(entry.file_path.read_text, "utf-8")
    except OSError as exc:
        logger.error("Skill file operation failed: %s", exc, exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to read skill file.") from exc
    from ..agent.skills.loader import parse_frontmatter
    _meta, body = parse_frontmatter(raw)
    return {
        "success": True,
        "name": entry.name,
        "description": entry.description,
        "body": body,
        "raw": raw,
        "file_path": str(entry.file_path),
    }


@router.post("")
async def create_skill(payload: SkillCreate):
    path = _resolve_path(payload.name)
    if path.exists():
        raise HTTPException(
            status_code=409,
            detail=f"Skill '{payload.name}' already exists",
        )
    content = _build_markdown(payload.description, payload.body)
    try:
        await asyncio.to_thread(path.write_text, content, "utf-8")
    except OSError as exc:
        logger.error("Skill file operation failed: %s", exc, exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to write skill file.") from exc
    _refresh_running_agents()
    logger.info("Created skill %s at %s", payload.name, path)
    return {"success": True, "name": payload.name, "file_path": str(path)}


@router.put("/{name}")
async def update_skill(name: str, payload: SkillUpdate):
    path = await asyncio.to_thread(_existing_skill_path, name)
    if path is None or not path.exists():
        raise HTTPException(status_code=404, detail=f"Skill '{name}' not found")
    content = _build_markdown(payload.description, payload.body)
    try:
        await asyncio.to_thread(path.write_text, content, "utf-8")
    except OSError as exc:
        logger.error("Skill file operation failed: %s", exc, exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to write skill file.") from exc
    _refresh_running_agents()
    logger.info("Updated skill %s at %s", name, path)
    return {"success": True, "name": name, "file_path": str(path)}


@router.delete("/{name}")
async def delete_skill(name: str):
    path = await asyncio.to_thread(_existing_skill_path, name)
    if path is None or not path.exists():
        raise HTTPException(status_code=404, detail=f"Skill '{name}' not found")
    try:
        await asyncio.to_thread(path.unlink)
    except OSError as exc:
        logger.error("Skill file operation failed: %s", exc, exc_info=True)
        raise HTTPException(status_code=500, detail="Failed to delete skill file.") from exc
    _refresh_running_agents()
    logger.info("Deleted skill %s at %s", name, path)
    return {"success": True, "name": name}


@router.post("/refresh")
async def refresh_skills():
    _refresh_running_agents()
    reg = await asyncio.to_thread(_build_registry)
    return {"success": True, "count": len(reg.list_all())}
