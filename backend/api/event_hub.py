import asyncio
import logging
from collections import deque

logger = logging.getLogger(__name__)

_REPLAY = 50
_SUB_MAXSIZE = 500


class EventHub:

    def __init__(self, replay: int = _REPLAY, sub_maxsize: int = _SUB_MAXSIZE) -> None:
        self._subscribers: set[asyncio.Queue] = set()
        self._recent: deque = deque(maxlen=replay)
        self._sub_maxsize = sub_maxsize

    def subscribe(self, replay: bool = True) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue(maxsize=self._sub_maxsize)
        if replay:
            for ev in self._recent:
                try:
                    q.put_nowait(ev)
                except asyncio.QueueFull:
                    break
        self._subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._subscribers.discard(q)

    def broadcast(self, event: dict) -> None:
        self._recent.append(event)
        for q in list(self._subscribers):
            try:
                q.put_nowait(event)
            except asyncio.QueueFull:
                try:
                    q.get_nowait()
                    q.put_nowait(event)
                except Exception:
                    pass

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)


def _absorb_backlog(source_queue: asyncio.Queue, hub: EventHub) -> int:
    n = 0
    while True:
        try:
            hub.broadcast(source_queue.get_nowait())
            n += 1
        except asyncio.QueueEmpty:
            return n


async def _fanout_pump(user_id: str, source_queue: asyncio.Queue, hub: EventHub) -> None:
    from .agent_state import active_agents
    try:
        while True:
            current = active_agents.get(user_id)
            if current is None or current.get("event_queue") is not source_queue:
                break
            try:
                event = await asyncio.wait_for(source_queue.get(), timeout=1.0)
            except asyncio.TimeoutError:
                continue
            hub.broadcast(event)
    except asyncio.CancelledError:
        pass
    except Exception:
        logger.error("fan-out pump crashed for %s", user_id, exc_info=True)


def ensure_hub(user_id: str, agent_info: dict) -> EventHub:
    hub = agent_info.get("event_hub")
    if hub is None:
        hub = EventHub()
        event_queue = agent_info.get("event_queue")
        if event_queue is not None:
            _absorb_backlog(event_queue, hub)
        agent_info["event_hub"] = hub
        agent_info["fanout_task"] = asyncio.create_task(
            _fanout_pump(user_id, event_queue, hub),
            name=f"fanout-{user_id}",
        )
    return hub
