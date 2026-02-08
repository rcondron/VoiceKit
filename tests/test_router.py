"""Tests for voicekit.core.router module."""

import asyncio
from typing import AsyncIterator
from unittest.mock import AsyncMock

import pytest

from voicekit.core.events import EventBus
from voicekit.core.router import AudioRoute, AudioRouter
from voicekit.platforms.base import AudioCallback, PlatformAdapter
from voicekit.providers.base import VoiceProvider


class MockProvider(VoiceProvider):
    """Mock provider for testing."""

    def __init__(self):
        self._connected = False
        self.sent_audio: list[bytes] = []
        self._audio_to_yield: list[bytes] = []
        self._yield_event = asyncio.Event()

    @property
    def name(self) -> str:
        return "mock_provider"

    @property
    def is_connected(self) -> bool:
        return self._connected

    async def connect(self) -> None:
        self._connected = True

    async def send_audio(self, audio: bytes) -> None:
        self.sent_audio.append(audio)

    async def receive_audio(self) -> AsyncIterator[bytes]:
        while self._connected:
            if self._audio_to_yield:
                chunk = self._audio_to_yield.pop(0)
                yield chunk
            else:
                try:
                    await asyncio.wait_for(self._yield_event.wait(), timeout=0.05)
                    self._yield_event.clear()
                except asyncio.TimeoutError:
                    continue

    async def disconnect(self) -> None:
        self._connected = False

    def enqueue_audio(self, data: bytes) -> None:
        self._audio_to_yield.append(data)
        self._yield_event.set()


class MockPlatform(PlatformAdapter):
    """Mock platform for testing."""

    def __init__(self):
        self._active = False
        self._callback: AudioCallback | None = None
        self.sent_audio: list[bytes] = []

    @property
    def name(self) -> str:
        return "mock_platform"

    @property
    def is_active(self) -> bool:
        return self._active

    async def start(self) -> None:
        self._active = True

    async def stop(self) -> None:
        self._active = False

    def on_audio_received(self, callback: AudioCallback) -> None:
        self._callback = callback

    async def send_audio(self, audio: bytes) -> None:
        self.sent_audio.append(audio)

    async def simulate_incoming_audio(self, data: bytes) -> None:
        if self._callback:
            await self._callback(data)


@pytest.fixture
def event_bus():
    return EventBus()


class TestAudioRouter:
    @pytest.mark.asyncio
    async def test_add_route(self, event_bus):
        router = AudioRouter(event_bus)
        provider = MockProvider()
        await provider.connect()
        platform = MockPlatform()
        await platform.start()

        route = await router.add_route(platform, provider)
        assert router.active_routes == 1

        await route.stop()
        await router.stop_all()
        await provider.disconnect()

    @pytest.mark.asyncio
    async def test_remove_route(self, event_bus):
        router = AudioRouter(event_bus)
        provider = MockProvider()
        await provider.connect()
        platform = MockPlatform()
        await platform.start()

        await router.add_route(platform, provider)
        assert router.active_routes == 1

        await router.remove_route(platform)
        assert router.active_routes == 0

        await provider.disconnect()

    @pytest.mark.asyncio
    async def test_stop_all(self, event_bus):
        router = AudioRouter(event_bus)
        provider = MockProvider()
        await provider.connect()

        p1 = MockPlatform()
        p2 = MockPlatform()
        await p1.start()
        await p2.start()

        await router.add_route(p1, provider)
        await router.add_route(p2, provider)
        assert router.active_routes == 2

        await router.stop_all()
        assert router.active_routes == 0

        await provider.disconnect()

    @pytest.mark.asyncio
    async def test_platform_audio_forwarded_to_provider(self, event_bus):
        router = AudioRouter(event_bus)
        provider = MockProvider()
        await provider.connect()
        platform = MockPlatform()
        await platform.start()

        await router.add_route(platform, provider)

        # Simulate audio from platform — send a full chunk (4800 samples = 9600 bytes)
        audio_data = b"\x01\x00" * 4800
        await platform.simulate_incoming_audio(audio_data)

        # Give the router a moment to process
        await asyncio.sleep(0.05)

        assert len(provider.sent_audio) > 0

        await router.stop_all()
        await provider.disconnect()

    @pytest.mark.asyncio
    async def test_provider_audio_forwarded_to_platform(self, event_bus):
        router = AudioRouter(event_bus)
        provider = MockProvider()
        await provider.connect()
        platform = MockPlatform()
        await platform.start()

        await router.add_route(platform, provider)

        # Enqueue audio from provider
        audio_data = b"\x02\x00" * 100
        provider.enqueue_audio(audio_data)

        # Wait for it to be forwarded
        await asyncio.sleep(0.2)

        assert len(platform.sent_audio) > 0

        await router.stop_all()
        await provider.disconnect()
