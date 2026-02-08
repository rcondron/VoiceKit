"""Tests for voicebridge.core.events module."""

import asyncio

import pytest

from voicebridge.core.events import Event, EventBus, EventType


@pytest.fixture
def bus():
    return EventBus()


class TestEventBus:
    @pytest.mark.asyncio
    async def test_emit_to_handler(self, bus):
        received = []

        async def handler(event: Event):
            received.append(event)

        bus.on(EventType.CALL_INCOMING, handler)
        event = Event(type=EventType.CALL_INCOMING, platform="test")
        await bus.emit(event)

        assert len(received) == 1
        assert received[0].type == EventType.CALL_INCOMING
        assert received[0].platform == "test"

    @pytest.mark.asyncio
    async def test_emit_to_multiple_handlers(self, bus):
        count = 0

        async def handler1(event: Event):
            nonlocal count
            count += 1

        async def handler2(event: Event):
            nonlocal count
            count += 10

        bus.on(EventType.CALL_ANSWERED, handler1)
        bus.on(EventType.CALL_ANSWERED, handler2)
        await bus.emit(Event(type=EventType.CALL_ANSWERED))

        assert count == 11

    @pytest.mark.asyncio
    async def test_global_handler_receives_all(self, bus):
        received = []

        async def handler(event: Event):
            received.append(event.type)

        bus.on_all(handler)
        await bus.emit(Event(type=EventType.CALL_INCOMING))
        await bus.emit(Event(type=EventType.CALL_ENDED))

        assert received == [EventType.CALL_INCOMING, EventType.CALL_ENDED]

    @pytest.mark.asyncio
    async def test_off_removes_handler(self, bus):
        count = 0

        async def handler(event: Event):
            nonlocal count
            count += 1

        bus.on(EventType.CALL_INCOMING, handler)
        await bus.emit(Event(type=EventType.CALL_INCOMING))
        assert count == 1

        bus.off(EventType.CALL_INCOMING, handler)
        await bus.emit(Event(type=EventType.CALL_INCOMING))
        assert count == 1  # Not incremented

    @pytest.mark.asyncio
    async def test_handler_exception_does_not_break_others(self, bus):
        results = []

        async def bad_handler(event: Event):
            raise ValueError("boom")

        async def good_handler(event: Event):
            results.append("ok")

        bus.on(EventType.CALL_INCOMING, bad_handler)
        bus.on(EventType.CALL_INCOMING, good_handler)
        await bus.emit(Event(type=EventType.CALL_INCOMING))

        assert results == ["ok"]

    @pytest.mark.asyncio
    async def test_emit_no_handlers(self, bus):
        # Should not raise
        await bus.emit(Event(type=EventType.DAEMON_STARTED))

    @pytest.mark.asyncio
    async def test_event_data(self, bus):
        received = []

        async def handler(event: Event):
            received.append(event.data)

        bus.on(EventType.PLATFORM_ERROR, handler)
        await bus.emit(
            Event(
                type=EventType.PLATFORM_ERROR,
                platform="test",
                data={"error": "connection_lost"},
            )
        )

        assert received == [{"error": "connection_lost"}]


class TestEventType:
    def test_event_type_values(self):
        assert EventType.CALL_INCOMING == "call.incoming"
        assert EventType.DAEMON_STARTED == "daemon.started"

    def test_event_type_is_string(self):
        assert isinstance(EventType.CALL_INCOMING, str)
