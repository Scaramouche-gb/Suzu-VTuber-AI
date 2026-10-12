"""Asyncio Pub/Sub Event Bus for Suzu VTuber AI daemon."""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Any, TypeVar

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class Event:
    """Base event with creation timestamp."""
    timestamp: float = field(default_factory=time.time)


@dataclass(slots=True)
class UserSpeechStart(Event):
    """Fired when user begins speaking (voice activity detected)."""


@dataclass(slots=True)
class UserSpeech(Event):
    """Fired when user phrase ends and speech chunk is captured."""
    audio_data: Any = None
    duration_s: float = 0.0
    text: str = ""


@dataclass(slots=True)
class ScreenChange(Event):
    """Fired when screen content changes noticeably."""
    image_bytes: bytes = b""
    diff_ratio: float = 0.0
    active_app: str = ""
    active_title: str = ""


@dataclass(slots=True)
class Tick(Event):
    """Periodic tick for initiative and state checks."""
    dt: float = 1.0


@dataclass(slots=True)
class UserCommand(Event):
    """Fired when CLI or control interface sends a command."""
    command: str = ""
    args: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class PlaybackStarted(Event):
    """Fired when renderer begins audio playback."""
    utterance_id: str = ""


@dataclass(slots=True)
class PlaybackEnded(Event):
    """Fired when renderer finishes audio playback."""
    utterance_id: str = ""


@dataclass(slots=True)
class Interrupt(Event):
    """Fired to barge-in or cancel active speaking/thinking."""
    reason: str = "barge-in"


E = TypeVar("E", bound=Event)
EventHandler = Callable[[E], Awaitable[None]]


class EventBus:
    """Asyncio in-memory event bus."""

    def __init__(self) -> None:
        self._subscribers: dict[type[Event], list[EventHandler[Any]]] = {}

    def subscribe(self, event_type: type[E], handler: EventHandler[E]) -> None:
        """Register an async handler for a given event type."""
        handlers = self._subscribers.setdefault(event_type, [])
        if handler not in handlers:
            handlers.append(handler)

    def unsubscribe(self, event_type: type[E], handler: EventHandler[E]) -> None:
        """Unregister an async handler for a given event type."""
        if event_type in self._subscribers:
            handlers = self._subscribers[event_type]
            if handler in handlers:
                handlers.remove(handler)

    async def publish(self, event: Event) -> None:
        """Publish event and await all subscribers with isolated exception handling."""
        event_cls = type(event)
        handlers = self._subscribers.get(event_cls, [])
        if not handlers:
            return

        tasks = [self._invoke(handler, event) for handler in handlers]
        await asyncio.gather(*tasks, return_exceptions=True)

    async def _invoke(self, handler: EventHandler[Any], event: Event) -> None:
        try:
            await handler(event)
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Error in event handler %s for %s", handler, type(event).__name__)
