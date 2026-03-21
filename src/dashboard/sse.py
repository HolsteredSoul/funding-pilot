"""Server-Sent Events fan-out bus.

The three async loops publish state snapshots here.  Each connected browser
gets its own ``asyncio.Queue`` so slow clients never block the bot.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any, AsyncGenerator

import structlog

log = structlog.get_logger()

# Maximum items buffered per client before we start dropping.
_MAX_QUEUE_SIZE = 64


class EventBus:
    """Pub/sub fan-out for SSE.  Thread-safe via asyncio primitives."""

    def __init__(self) -> None:
        self._subscribers: list[asyncio.Queue[str]] = []
        self._lock = asyncio.Lock()

    async def publish(self, event: str, data: dict[str, Any]) -> None:
        """Push *data* to every connected browser under *event* name.

        Silently drops messages for any client whose queue is full.
        """
        payload = f"event: {event}\ndata: {json.dumps(data, default=str)}\n\n"
        async with self._lock:
            dead: list[asyncio.Queue[str]] = []
            for q in self._subscribers:
                try:
                    q.put_nowait(payload)
                except asyncio.QueueFull:
                    # Slow consumer — drop oldest message, push new one.
                    try:
                        q.get_nowait()
                        q.put_nowait(payload)
                    except (asyncio.QueueEmpty, asyncio.QueueFull):
                        dead.append(q)
            for q in dead:
                self._subscribers.remove(q)

    async def subscribe(self) -> AsyncGenerator[str, None]:
        """Yield SSE-formatted strings for one browser connection."""
        q: asyncio.Queue[str] = asyncio.Queue(maxsize=_MAX_QUEUE_SIZE)
        async with self._lock:
            self._subscribers.append(q)
        try:
            while True:
                msg = await q.get()
                yield msg
        finally:
            async with self._lock:
                if q in self._subscribers:
                    self._subscribers.remove(q)

    @property
    async def subscriber_count(self) -> int:
        """Number of active SSE connections."""
        async with self._lock:
            return len(self._subscribers)
