from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


class ContextManager:

    BUFFER_TOKENS = 4000
    SUMMARY_BUDGET = 2000
    MAX_COMPACT_FAILURES = 3

    def __init__(self, context_window: int = 128_000) -> None:
        self.context_window = context_window
        self.compact_failure_count = 0

    def estimate_tokens(
        self,
        messages: list[dict],
        tool_definitions: list[dict] | None = None,
        system_prompt_tokens: int = 0,
    ) -> int:
        total_bytes = sum(len(str(m.get("content", "")).encode("utf-8")) for m in messages)
        for m in messages:
            for tc in m.get("tool_calls", []):
                total_bytes += len(str(tc).encode("utf-8"))
        tool_bytes = len(str(tool_definitions).encode("utf-8")) if tool_definitions else 0
        return int((total_bytes + tool_bytes) / 3) + system_prompt_tokens

    def should_compact(self, messages: list[dict], tool_definitions: list[dict] | None = None) -> bool:
        if self.compact_failure_count >= self.MAX_COMPACT_FAILURES:
            return False

        estimated = self.estimate_tokens(messages, tool_definitions)
        threshold = self.context_window - self.BUFFER_TOKENS - self.SUMMARY_BUDGET
        return estimated > threshold

    def record_compact_success(self) -> None:
        self.compact_failure_count = 0

    def record_compact_failure(self) -> None:
        self.compact_failure_count += 1
        logger.warning(
            "Compaction failed (%d/%d)",
            self.compact_failure_count,
            self.MAX_COMPACT_FAILURES,
        )

    @property
    def circuit_breaker_tripped(self) -> bool:
        return self.compact_failure_count >= self.MAX_COMPACT_FAILURES

    def emergency_truncate(
        self,
        messages: list[dict],
        preamble_size: int,
    ) -> tuple[list[dict], str]:
        body = messages[preamble_size:]
        if not body:
            return list(messages), "[Context emergency-truncate: nothing to drop]"
        keep_count = max(1, len(body) * 3 // 4)
        cut_idx = len(body) - keep_count

        while cut_idx < len(body) and body[cut_idx].get("role") == "tool":
            cut_idx += 1

        kept = body[cut_idx:]
        truncated = list(messages[:preamble_size]) + kept
        dropped = len(body) - len(kept)
        logger.warning("Emergency truncation: dropped %d oldest messages", dropped)
        return truncated, f"[Context emergency-truncated: {dropped} messages dropped]"
