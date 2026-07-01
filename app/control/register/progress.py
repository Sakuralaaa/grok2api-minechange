"""Registration pipeline progress tracking and SSE event support."""

from __future__ import annotations

import asyncio
import json
import time
from dataclasses import dataclass, field
from typing import Any, AsyncGenerator, Callable


@dataclass
class PipelineEvent:
    """A named event emitted during the registration pipeline."""
    kind: str  # step | error | account_done | batch_start | batch_done | batch_stop | stop_requested
    data: dict[str, Any] = field(default_factory=dict)
    timestamp: float = field(default_factory=time.time)


class PipelineProgress:
    """Tracks registration pipeline progress and maintains an event log.

    Supports SSE streaming via an optional `on_event` callback.
    """

    def __init__(self) -> None:
        self._events: list[PipelineEvent] = []
        self._completed = 0
        self._failed = 0
        self._current_step: str = ""
        self._current_message: str = ""
        self._on_event: Callable[[PipelineEvent], None] | None = None
        self._event_queue: asyncio.Queue[PipelineEvent] = asyncio.Queue()

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def events(self) -> list[PipelineEvent]:
        return list(self._events)

    @property
    def on_event(self) -> Callable[[PipelineEvent], None] | None:
        return self._on_event

    @on_event.setter
    def on_event(self, cb: Callable[[PipelineEvent], None] | None) -> None:
        self._on_event = cb

    @property
    def completed(self) -> int:
        return self._completed

    @property
    def failed(self) -> int:
        return self._failed

    @property
    def current_step(self) -> str:
        return self._current_step

    @property
    def current_message(self) -> str:
        return self._current_message

    # ------------------------------------------------------------------
    # Event management
    # ------------------------------------------------------------------

    def emit(self, event: PipelineEvent) -> None:
        """Emit a pipeline event."""
        self._events.append(event)
        self._event_queue.put_nowait(event)

        if event.kind == "step":
            self._current_step = event.data.get("step", "")
            self._current_message = event.data.get("message", "")
        elif event.kind == "account_done":
            if event.data.get("success"):
                self._completed += 1
            else:
                self._failed += 1
        elif event.kind == "error":
            self._failed += 1

        if self._on_event:
            try:
                self._on_event(event)
            except Exception:
                pass

    def reset(self) -> None:
        """Reset progress tracking for a new batch."""
        self._events.clear()
        self._completed = 0
        self._failed = 0
        self._current_step = ""
        self._current_message = ""
        # Drain the queue
        while not self._event_queue.empty():
            try:
                self._event_queue.get_nowait()
            except asyncio.QueueEmpty:
                break

    def snapshot(self) -> dict[str, Any]:
        """Return a snapshot dict for API responses."""
        return {
            "completed": self._completed,
            "failed": self._failed,
            "current_step": self._current_step,
            "current_message": self._current_message,
            "events_count": len(self._events),
        }

    async def event_stream(self) -> AsyncGenerator[str, None]:
        """Async generator for SSE event streaming."""
        # AsyncGenerator imported above

        # Yield existing events first
        for event in self._events:
            yield f"data: {json.dumps({'kind': event.kind, 'data': event.data, 'timestamp': event.timestamp})}\n\n"

        # Then stream new events as they arrive
        while True:
            try:
                event = await asyncio.wait_for(self._event_queue.get(), timeout=30.0)
                yield f"data: {json.dumps({'kind': event.kind, 'data': event.data, 'timestamp': event.timestamp})}\n\n"
                if event.kind == "batch_done" or event.kind == "batch_stop" or event.kind == "stop_requested" and "reason" in event.data:
                    break
            except asyncio.TimeoutError:
                # Send keepalive
                yield ": keepalive\n\n"


# Re-export for type hints
# AsyncGenerator imported above
