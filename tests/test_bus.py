"""Unit tests for asyncio event bus."""

import asyncio
import pytest
from daemon.bus import EventBus, UserSpeechStart, UserSpeech, PlaybackEnded


@pytest.mark.asyncio
async def test_event_bus_publish_subscribe() -> None:
    bus = EventBus()
    received_events: list[UserSpeech] = []

    async def on_speech(event: UserSpeech) -> None:
        received_events.append(event)

    bus.subscribe(UserSpeech, on_speech)
    await bus.publish(UserSpeech(text="Привет Сузу", duration_s=1.5))

    assert len(received_events) == 1
    assert received_events[0].text == "Привет Сузу"
    assert received_events[0].duration_s == 1.5


@pytest.mark.asyncio
async def test_event_bus_multiple_subscribers() -> None:
    bus = EventBus()
    called_a = False
    called_b = False

    async def sub_a(event: UserSpeechStart) -> None:
        nonlocal called_a
        called_a = True

    async def sub_b(event: UserSpeechStart) -> None:
        nonlocal called_b
        called_b = True

    bus.subscribe(UserSpeechStart, sub_a)
    bus.subscribe(UserSpeechStart, sub_b)

    await bus.publish(UserSpeechStart())
    assert called_a is True
    assert called_b is True


@pytest.mark.asyncio
async def test_event_bus_isolated_exceptions() -> None:
    bus = EventBus()
    safe_called = False

    async def faulty_sub(event: PlaybackEnded) -> None:
        raise RuntimeError("Something failed in subscriber")

    async def safe_sub(event: PlaybackEnded) -> None:
        nonlocal safe_called
        safe_called = True

    bus.subscribe(PlaybackEnded, faulty_sub)
    bus.subscribe(PlaybackEnded, safe_sub)

    # Should not raise exception and safe_sub should still execute
    await bus.publish(PlaybackEnded(utterance_id="u123"))
    assert safe_called is True


@pytest.mark.asyncio
async def test_event_bus_unsubscribe() -> None:
    bus = EventBus()
    counter = 0

    async def handler(event: UserSpeechStart) -> None:
        nonlocal counter
        counter += 1

    bus.subscribe(UserSpeechStart, handler)
    await bus.publish(UserSpeechStart())
    assert counter == 1

    bus.unsubscribe(UserSpeechStart, handler)
    await bus.publish(UserSpeechStart())
    assert counter == 1
