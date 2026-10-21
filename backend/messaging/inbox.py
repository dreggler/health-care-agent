from __future__ import annotations

import asyncio
import logging

from .base import MessageEnvelope

logger = logging.getLogger(__name__)

_DEBOUNCE_WINDOW_S = 5.0


class InboxQueue:

    def __init__(self, maxsize: int = 50) -> None:
        self._queue: asyncio.Queue[MessageEnvelope] = asyncio.Queue(maxsize=maxsize)


    async def push(self, envelope: MessageEnvelope) -> None:
        if self._queue.full():
            try:
                dropped = self._queue.get_nowait()
                logger.warning(
                    "InboxQueue full (%d): dropping oldest %s message from %s (chat=%s)",
                    self._queue.maxsize, dropped.channel.value,
                    dropped.sender_id, dropped.chat_id,
                )
            except asyncio.QueueEmpty:
                pass
        await self._queue.put(envelope)
        logger.info(
            "InboxQueue: queued %s message from %s (chat=%s): %s",
            envelope.channel.value,
            envelope.sender_id,
            envelope.chat_id,
            envelope.content[:80],
        )


    def has_messages(self) -> bool:
        return not self._queue.empty()

    async def pop_all(self) -> list[MessageEnvelope]:
        raw: list[MessageEnvelope] = []
        while True:
            try:
                msg = self._queue.get_nowait()
                raw.append(msg)
            except asyncio.QueueEmpty:
                break
        if not raw:
            return []
        merged = _debounce(raw)
        logger.info(
            "InboxQueue: drained %d raw message(s) → %d after debounce",
            len(raw), len(merged),
        )
        return merged

    async def wait_and_pop_all(self) -> list[MessageEnvelope]:
        first = await self._queue.get()
        raw: list[MessageEnvelope] = [first]
        await asyncio.sleep(0.3)
        while True:
            try:
                raw.append(self._queue.get_nowait())
            except asyncio.QueueEmpty:
                break
        merged = _debounce(raw)
        logger.info(
            "InboxQueue: received %d message(s) → %d after debounce",
            len(raw), len(merged),
        )
        return merged


def _debounce(messages: list[MessageEnvelope]) -> list[MessageEnvelope]:
    if len(messages) <= 1:
        return messages

    groups: list[list[MessageEnvelope]] = []
    current_group: list[MessageEnvelope] = [messages[0]]

    for msg in messages[1:]:
        prev = current_group[-1]
        same_sender = (
            msg.channel == prev.channel
            and msg.sender_id == prev.sender_id
            and msg.chat_id == prev.chat_id
        )
        within_window = (msg.timestamp.timestamp() - prev.timestamp.timestamp()) < _DEBOUNCE_WINDOW_S

        if same_sender and within_window:
            current_group.append(msg)
        else:
            groups.append(current_group)
            current_group = [msg]
    groups.append(current_group)

    result: list[MessageEnvelope] = []
    for group in groups:
        if len(group) == 1:
            result.append(group[0])
        else:
            combined_text = "\n".join(m.content for m in group)
            combined_attachments: list = []
            for m in group:
                combined_attachments.extend(m.attachments)
            merged = MessageEnvelope(
                message_id=group[0].message_id,
                channel=group[0].channel,
                sender_id=group[0].sender_id,
                content=combined_text,
                timestamp=group[0].timestamp,
                chat_id=group[0].chat_id,
                telegram_message_id=group[0].telegram_message_id,
                conversation_id=group[0].conversation_id,
                platform_message_id=group[0].platform_message_id,
                metadata=group[0].metadata,
                attachments=combined_attachments,
            )
            result.append(merged)
    return result
