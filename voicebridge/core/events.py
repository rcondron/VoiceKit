"""Event system for VoiceBridge.

Provides a simple async event bus for decoupled communication between
platform adapters, the audio router, and AI providers.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Coroutine

logger = logging.getLogger(__name__)


class EventType(str, Enum):
    """Events emitted throughout the voice bridge lifecycle."""

    # Call lifecycle
    CALL_INCOMING = "call.incoming"
    CALL_ANSWERED = "call.answered"
    CALL_ENDED = "call.ended"
    CALL_FAILED = "call.failed"

    # Audio flow
    AUDIO_RECEIVED = "audio.received"
    AUDIO_SENT = "audio.sent"

    # Provider
    PROVIDER_CONNECTED = "provider.connected"
    PROVIDER_DISCONNECTED = "provider.disconnected"
    PROVIDER_ERROR = "provider.error"

    # Platform
    PLATFORM_STARTED = "platform.started"
    PLATFORM_STOPPED = "platform.stopped"
    PLATFORM_ERROR = "platform.error"

    # Daemon
    DAEMON_STARTED = "daemon.started"
    DAEMON_STOPPING = "daemon.stopping"


@dataclass
class Event:
    """An event with its type and associated data."""

    type: EventType
    platform: str = ""
    data: dict[str, Any] = field(default_factory=dict)


# Type alias for event handler callbacks
EventHandler = Callable[[Event], Coroutine[Any, Any, None]]


class EventBus:
    """Async event bus for broadcasting events to registered handlers."""

    def __init__(self) -> None:
        self._handlers: dict[EventType, list[EventHandler]] = {}
        self._global_handlers: list[EventHandler] = []

    def on(self, event_type: EventType, handler: EventHandler) -> None:
        """Register a handler for a specific event type."""
        if event_type not in self._handlers:
            self._handlers[event_type] = []
        self._handlers[event_type].append(handler)

    def on_all(self, handler: EventHandler) -> None:
        """Register a handler that receives all events."""
        self._global_handlers.append(handler)

    def off(self, event_type: EventType, handler: EventHandler) -> None:
        """Remove a handler for a specific event type."""
        if event_type in self._handlers:
            self._handlers[event_type] = [
                h for h in self._handlers[event_type] if h is not handler
            ]

    async def emit(self, event: Event) -> None:
        """Emit an event to all registered handlers.

        Handlers are invoked concurrently. Exceptions in individual handlers
        are logged but do not prevent other handlers from running.
        """
        handlers = list(self._global_handlers)
        handlers.extend(self._handlers.get(event.type, []))

        if not handlers:
            return

        results = await asyncio.gather(
            *(h(event) for h in handlers),
            return_exceptions=True,
        )

        for i, result in enumerate(results):
            if isinstance(result, Exception):
                logger.error(
                    "Event handler %s raised %s for event %s: %s",
                    handlers[i].__name__,
                    type(result).__name__,
                    event.type.value,
                    result,
                )
