from __future__ import annotations

import asyncio


class CancelledError(Exception):

    def __init__(self, reason: str = "Operation cancelled") -> None:
        self.reason = reason
        super().__init__(reason)


class CancellationToken:

    def __init__(self, parent: CancellationToken | None = None) -> None:
        self._cancelled = False
        self._reason = ""
        self._parent = parent
        self._children: list[CancellationToken] = []
        if parent is not None:
            parent._children.append(self)

    @property
    def is_cancelled(self) -> bool:
        if self._cancelled:
            return True
        if self._parent is not None and self._parent.is_cancelled:
            return True
        return False

    def cancel(self, reason: str = "") -> None:
        self._cancelled = True
        self._reason = reason
        for child in self._children:
            child.cancel(reason)

    def check(self) -> None:
        if self.is_cancelled:
            raise CancelledError(self._reason or "Operation cancelled")

    def create_child(self) -> CancellationToken:
        return CancellationToken(parent=self)

    def reset(self) -> None:
        self._cancelled = False
        self._reason = ""
        self._children.clear()

    async def wait_for_cancellation(self, timeout: float | None = None) -> None:
        poll_interval = 0.1
        elapsed = 0.0
        while not self.is_cancelled:
            await asyncio.sleep(poll_interval)
            elapsed += poll_interval
            if timeout is not None and elapsed >= timeout:
                break
