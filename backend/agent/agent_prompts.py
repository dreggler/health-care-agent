import logging
from datetime import datetime, timezone
from pathlib import Path

from .prompt_loader import PROMPTS_DIR, load_prompt
from .skills.prompt import format_skills_for_prompt

logger = logging.getLogger(__name__)


LAYER_PLAN: dict[str, tuple[tuple[str, str], ...]] = {
    "chat": (
        ("rules_chat.md",    "chat rules"),
        ("experience.md",    "experience"),
        ("user.md",          "user profile"),
    ),
    "sub_analysis": (
        ("data_schema.md",   "data schema"),
        ("experience.md",    "experience"),
    ),
    "plan_designer": (
        ("data_schema.md",   "data schema"),
        ("memory_guide.md",  "memory guide"),
        ("user.md",          "user profile"),
        ("experience.md",    "experience"),
    ),
    "sub_manage": (
        ("memory_guide.md",      "memory guide"),
        ("create_page_guide.md", "create_page guide"),
        ("user.md",              "user profile (current content)"),
        ("experience.md",        "experience (current content)"),
    ),
}

SKILLS_ROLES = frozenset({"chat", "sub_analysis"})


def _read_prompt_file(path: Path, label: str) -> str:
    if not path.exists():
        return ""
    try:
        text = path.read_text(encoding="utf-8").strip()
        logger.debug("Loaded %s from %s", label, path)
        return text
    except Exception as exc:
        logger.warning("%s unreadable: %s", label, exc)
        return ""


class AgentPromptsMixin:


    def _format_message_timestamp(self, dt: datetime) -> str:
        from ..utils import app_timezone
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.astimezone(app_timezone()).strftime("%Y-%m-%d %H:%M")


    def _append_layers(self, base: str, role: str) -> str:
        plan = LAYER_PLAN.get(role, ())
        if not plan:
            return base
        prompts_dir = PROMPTS_DIR
        chunks: list[str] = [base] if base else []
        for filename, label in plan:
            text = _read_prompt_file(prompts_dir / filename, label)
            if text:
                chunks.append(text)
        return "\n\n---\n\n".join(chunks)

    def _append_skills_block(self, base: str, role: str) -> str:
        if role not in SKILLS_ROLES:
            return base
        skill_registry = getattr(self, "skill_registry", None)
        if skill_registry is None:
            return base
        try:
            skills_xml = format_skills_for_prompt(skill_registry.list_enabled())
        except Exception as exc:
            logger.warning("Failed to format skills for prompt: %s", exc)
            return base
        if not skills_xml:
            return base
        return f"{base}\n\n{skills_xml}"


    def _get_system_prompt(self, mode: str = "chat") -> str:
        if mode != "chat":
            logger.warning(
                "_get_system_prompt called with legacy mode=%r; only 'chat' is supported. "
                "Use build_sub_analysis_prompt or build_sub_manage_prompt for sub-agents.",
                mode,
            )

        result = self._build_core_template()
        result = self._append_layers(result, "chat")
        result = self._append_skills_block(result, "chat")
        result += _read_prompt_file(
            PROMPTS_DIR / "conversation_header.md", "conversation header"
        )
        return result


    def build_sub_analysis_prompt(self) -> str:
        base = load_prompt("sub_analysis.md")
        result = self._append_layers(base, "sub_analysis")
        result = self._append_skills_block(result, "sub_analysis")
        return result


    def build_plan_designer_prompt(self) -> str:
        base = load_prompt("plan_designer.md")
        return self._append_layers(base, "plan_designer")


    def build_sub_manage_prompt(self) -> str:
        base = load_prompt("sub_manage.md")
        return self._append_layers(base, "sub_manage")


    def _build_core_template(self) -> str:
        if not hasattr(self, "_soul_text"):
            self._soul_text = _read_prompt_file(PROMPTS_DIR / "soul.md", "soul")
        return self._soul_text or ""
