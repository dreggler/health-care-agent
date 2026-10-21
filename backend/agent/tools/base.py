import contextvars
import copy
import json
import logging
from abc import ABC, abstractmethod
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ValidationError

logger = logging.getLogger(__name__)


class _FlowVar:

    def __init__(self, default: Any = None) -> None:
        self._default = default
        self._name = ""

    def __set_name__(self, owner: type, name: str) -> None:
        self._name = name

    def _var(self, obj: Any) -> contextvars.ContextVar:
        key = f"_cv_{self._name}"
        var = obj.__dict__.get(key)
        if var is None:
            var = contextvars.ContextVar(f"{type(obj).__name__}.{self._name}", default=self._default)
            obj.__dict__[key] = var
        return var

    def __get__(self, obj: Any, owner: type | None = None) -> Any:
        if obj is None:
            return self._default
        return self._var(obj).get()

    def __set__(self, obj: Any, value: Any) -> None:
        self._var(obj).set(value)


_tool_role: contextvars.ContextVar[str | None] = contextvars.ContextVar("tool_role", default=None)


def current_tool_role() -> str | None:
    return _tool_role.get()


@contextmanager
def tool_role(role: str | None) -> Iterator[None]:
    token = _tool_role.set(role)
    try:
        yield
    finally:
        _tool_role.reset(token)


_TOOL_DEFS_PATH = Path(__file__).parent / "tools.json"
_tool_defs_cache: dict[str, Any] | None = None


def load_tool_definitions() -> dict[str, Any]:
    global _tool_defs_cache
    if _tool_defs_cache is None:
        try:
            with open(_TOOL_DEFS_PATH, encoding="utf-8") as f:
                _tool_defs_cache = json.load(f)
        except Exception:
            return {}
    return _tool_defs_cache


class BaseTool(ABC):

    _fact_verifier: Any = None
    _llm_provider: Any = None
    _current_tool_results: list[dict[str, Any]] = _FlowVar([])
    _current_user_message: str = _FlowVar("")
    _current_chat_history: list = _FlowVar([])
    _event_emitter: Any = _FlowVar(None)

    input_schema: type[BaseModel] | None = None

    @property
    def is_concurrency_safe(self) -> bool:
        return False

    _progress_callback: Callable[[str, Any], None] | None = None

    def set_progress_callback(self, callback: Callable[[str, Any], None]) -> None:
        self._progress_callback = callback

    def report_progress(self, data: Any) -> None:
        if self._progress_callback:
            try:
                name = getattr(self, "name", self.__class__.__name__)
                self._progress_callback(name, data)
            except Exception:
                pass

    async def __call__(self, **kwargs: Any) -> dict[str, Any]:
        if self.input_schema is not None:
            try:
                validated = self.input_schema(**kwargs)
                kwargs = validated.model_dump()
            except ValidationError as e:
                return {
                    "success": False,
                    "error": f"Invalid arguments:\n{self._format_validation_error(e)}",
                    "error_type": "validation",
                }

        semantic_error = await self.validate_input(**kwargs)
        if semantic_error:
            return {
                "success": False,
                "error": semantic_error,
                "error_type": "semantic_validation",
            }

        return await self.execute(**kwargs)

    async def validate_input(self, **kwargs: Any) -> str | None:
        return None

    @staticmethod
    def _format_validation_error(e: ValidationError) -> str:
        errors = []
        for err in e.errors():
            field = ".".join(str(x) for x in err["loc"])
            msg = err["msg"]
            errors.append(f"  - {field}: {msg}")
        return "\n".join(errors)

    def _get_definition_from_json(self, tool_name: str) -> dict[str, Any]:
        return copy.deepcopy(load_tool_definitions().get(tool_name, {}))

    async def _verify_and_build_markup(self, message_text: str) -> dict:
        if not self._fact_verifier:
            return {"status": "verified", "detail": "", "reply_markup": None}
        try:
            chat_history = getattr(self, "_current_chat_history", None) or []
            vresult = await self._fact_verifier.verify_message(
                message_text=message_text,
                tool_results=self._current_tool_results or [],
                llm_provider=self._llm_provider,
                user_message=self._current_user_message,
                chat_history=chat_history,
            )
            status = vresult.get("status", "verified")
            detail = vresult.get("detail", "")
            msg_hash = vresult.get("message_hash", "")
            reply_markup = None
            if msg_hash:
                reply_markup = {
                    "inline_keyboard": [[
                        {
                            "text": "\U0001f4ca Show Evidence",
                            "callback_data": f"evidence:{msg_hash}",
                        }
                    ]]
                }
            await self._emit_verification_event(
                status=status, detail=detail, msg_hash=msg_hash,
                message_text=message_text,
            )
            return {"status": status, "detail": detail, "reply_markup": reply_markup}
        except Exception as exc:
            logger.debug("Evidence verification failed: %s", exc)
            return {"status": "verified", "detail": "", "reply_markup": None}

    async def _emit_verification_event(
        self, *, status: str, detail: str, msg_hash: str, message_text: str,
    ) -> None:
        emitter = getattr(self, "_event_emitter", None)
        if emitter is None:
            return
        evidence_count = len(self._current_tool_results or [])
        scenario = "chat" if evidence_count == 0 else "data_or_op"
        payload = {
            "type": "chat_verification",
            "tool": getattr(self, "name", "unknown"),
            "scenario": scenario,
            "status": status,
            "detail": (detail or "")[:300],
            "evidence_count": evidence_count,
            "message_hash": msg_hash,
            "preview": (message_text or "")[:200],
        }
        try:
            await emitter(payload)
        except Exception as exc:
            logger.debug("verification event emit failed: %s", exc)

    @abstractmethod
    def get_definition(self) -> dict:
        pass

    @abstractmethod
    async def execute(self, **kwargs) -> dict[str, Any]:
        pass
