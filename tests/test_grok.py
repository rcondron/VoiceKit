"""Tests for voicekit.providers.grok module."""

import asyncio
import base64
import json
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from voicekit.config import ProviderConfig
from voicekit.providers.grok import GROK_REALTIME_URL, GrokVoiceProvider


@pytest.fixture
def provider_config():
    return ProviderConfig(
        type="grok",
        api_key="xai-test-key-123",
        model="grok-3",
        voice="Ara",
        instructions="You are a test assistant.",
    )


@pytest.fixture
def provider(provider_config):
    return GrokVoiceProvider(provider_config)


class TestGrokVoiceProviderInit:
    def test_name(self, provider):
        assert provider.name == "grok"

    def test_not_connected_initially(self, provider):
        assert not provider.is_connected

    def test_default_reconnect_settings(self, provider):
        assert provider.MAX_RECONNECT_ATTEMPTS == 5
        assert provider.RECONNECT_BASE_DELAY == 1.0


class TestGrokVoiceProviderConnect:
    @pytest.mark.asyncio
    async def test_connect_sets_connected(self, provider, provider_config):
        mock_ws = AsyncMock()
        mock_ws.__aiter__ = AsyncMock(return_value=iter([]))

        with patch("voicekit.providers.grok.websockets.connect", new_callable=AsyncMock) as mock_connect:
            mock_connect.return_value = mock_ws

            await provider.connect()

            assert provider.is_connected
            mock_connect.assert_called_once()

            # Verify URL includes model
            call_args = mock_connect.call_args
            url = call_args[0][0]
            assert url == f"{GROK_REALTIME_URL}?model=grok-3"

            # Verify auth headers
            headers = call_args[1]["additional_headers"]
            assert headers["Authorization"] == "Bearer xai-test-key-123"

            await provider.disconnect()

    @pytest.mark.asyncio
    async def test_connect_sends_session_config(self, provider):
        mock_ws = AsyncMock()
        mock_ws.__aiter__ = AsyncMock(return_value=iter([]))

        with patch("voicekit.providers.grok.websockets.connect", new_callable=AsyncMock) as mock_connect:
            mock_connect.return_value = mock_ws

            await provider.connect()

            # The session config should have been sent
            assert mock_ws.send.called
            config_call = mock_ws.send.call_args_list[0]
            config_msg = json.loads(config_call[0][0])

            assert config_msg["type"] == "session.update"
            session = config_msg["session"]
            assert session["voice"] == "Ara"
            assert session["instructions"] == "You are a test assistant."
            assert session["input_audio_format"] == "pcm16"
            assert session["output_audio_format"] == "pcm16"
            assert session["modalities"] == ["text", "audio"]
            assert session["turn_detection"]["type"] == "server_vad"

            await provider.disconnect()

    @pytest.mark.asyncio
    async def test_connect_already_connected_is_noop(self, provider):
        mock_ws = AsyncMock()
        mock_ws.__aiter__ = AsyncMock(return_value=iter([]))

        with patch("voicekit.providers.grok.websockets.connect", new_callable=AsyncMock) as mock_connect:
            mock_connect.return_value = mock_ws
            await provider.connect()

            # Second connect should be a no-op
            call_count = mock_connect.call_count
            await provider.connect()
            assert mock_connect.call_count == call_count

            await provider.disconnect()

    @pytest.mark.asyncio
    async def test_connect_failure_raises_connection_error(self, provider):
        with patch("voicekit.providers.grok.websockets.connect", new_callable=AsyncMock) as mock_connect:
            mock_connect.side_effect = Exception("Connection refused")

            with pytest.raises(ConnectionError, match="Failed to connect to Grok"):
                await provider.connect()

            assert not provider.is_connected


class TestGrokVoiceProviderSendAudio:
    @pytest.mark.asyncio
    async def test_send_audio_base64_encodes(self, provider):
        mock_ws = AsyncMock()
        mock_ws.__aiter__ = AsyncMock(return_value=iter([]))

        with patch("voicekit.providers.grok.websockets.connect", new_callable=AsyncMock) as mock_connect:
            mock_connect.return_value = mock_ws
            await provider.connect()

            # Reset mock to only track send_audio calls
            mock_ws.send.reset_mock()

            audio_data = b"\x01\x00" * 100
            await provider.send_audio(audio_data)

            assert mock_ws.send.called
            sent_msg = json.loads(mock_ws.send.call_args[0][0])
            assert sent_msg["type"] == "input_audio_buffer.append"

            # Verify the audio is base64 encoded
            decoded = base64.b64decode(sent_msg["audio"])
            assert decoded == audio_data

            await provider.disconnect()

    @pytest.mark.asyncio
    async def test_send_audio_when_not_connected_is_noop(self, provider):
        # Should not raise when not connected
        await provider.send_audio(b"\x00" * 100)


class TestGrokVoiceProviderHandleMessage:
    @pytest.mark.asyncio
    async def test_audio_delta_queued(self, provider):
        audio_data = b"\x01\x02\x03\x04"
        audio_b64 = base64.b64encode(audio_data).decode("ascii")

        message = {
            "type": "response.audio.delta",
            "delta": audio_b64,
        }

        await provider._handle_message(message)

        assert not provider._audio_queue.empty()
        queued = provider._audio_queue.get_nowait()
        assert queued == audio_data

    @pytest.mark.asyncio
    async def test_session_created_logged(self, provider):
        message = {
            "type": "session.created",
            "session": {"id": "test-session-123"},
        }
        # Should not raise
        await provider._handle_message(message)

    @pytest.mark.asyncio
    async def test_error_message_handled(self, provider):
        message = {
            "type": "error",
            "error": {
                "type": "invalid_request",
                "message": "Bad audio format",
            },
        }
        # Should not raise
        await provider._handle_message(message)

    @pytest.mark.asyncio
    async def test_unknown_message_type_handled(self, provider):
        message = {"type": "some.future.event"}
        # Should not raise
        await provider._handle_message(message)

    @pytest.mark.asyncio
    async def test_rate_limits_silently_ignored(self, provider):
        message = {"type": "rate_limits.updated"}
        # Should not raise
        await provider._handle_message(message)


class TestGrokVoiceProviderDisconnect:
    @pytest.mark.asyncio
    async def test_disconnect_cleans_up(self, provider):
        mock_ws = AsyncMock()
        mock_ws.__aiter__ = AsyncMock(return_value=iter([]))

        with patch("voicekit.providers.grok.websockets.connect", new_callable=AsyncMock) as mock_connect:
            mock_connect.return_value = mock_ws
            await provider.connect()
            assert provider.is_connected

            await provider.disconnect()
            assert not provider.is_connected
            mock_ws.close.assert_called_once()

    @pytest.mark.asyncio
    async def test_disconnect_drains_queue(self, provider):
        # Put some audio in the queue
        await provider._audio_queue.put(b"\x01\x02")
        await provider._audio_queue.put(b"\x03\x04")

        await provider.disconnect()
        assert provider._audio_queue.empty()

    @pytest.mark.asyncio
    async def test_disconnect_when_not_connected(self, provider):
        # Should not raise
        await provider.disconnect()


class TestGrokVoiceProviderSendText:
    @pytest.mark.asyncio
    async def test_send_text_creates_conversation_item(self, provider):
        mock_ws = AsyncMock()
        mock_ws.__aiter__ = AsyncMock(return_value=iter([]))

        with patch("voicekit.providers.grok.websockets.connect", new_callable=AsyncMock) as mock_connect:
            mock_connect.return_value = mock_ws
            await provider.connect()
            mock_ws.send.reset_mock()

            await provider.send_text("Hello, Grok!")

            assert mock_ws.send.call_count == 2

            # First call: conversation.item.create
            first_msg = json.loads(mock_ws.send.call_args_list[0][0][0])
            assert first_msg["type"] == "conversation.item.create"
            assert first_msg["item"]["role"] == "user"
            assert first_msg["item"]["content"][0]["text"] == "Hello, Grok!"

            # Second call: response.create
            second_msg = json.loads(mock_ws.send.call_args_list[1][0][0])
            assert second_msg["type"] == "response.create"

            await provider.disconnect()

    @pytest.mark.asyncio
    async def test_send_text_when_not_connected_is_noop(self, provider):
        # Should not raise
        await provider.send_text("test")


class TestGrokVoiceProviderInterrupt:
    @pytest.mark.asyncio
    async def test_interrupt_sends_cancel_and_drains(self, provider):
        mock_ws = AsyncMock()
        mock_ws.__aiter__ = AsyncMock(return_value=iter([]))

        with patch("voicekit.providers.grok.websockets.connect", new_callable=AsyncMock) as mock_connect:
            mock_connect.return_value = mock_ws
            await provider.connect()

            # Queue some audio
            await provider._audio_queue.put(b"\x01\x02")
            await provider._audio_queue.put(b"\x03\x04")

            mock_ws.send.reset_mock()
            await provider.interrupt()

            # Should have sent response.cancel
            cancel_msg = json.loads(mock_ws.send.call_args[0][0])
            assert cancel_msg["type"] == "response.cancel"

            # Queue should be drained
            assert provider._audio_queue.empty()

            await provider.disconnect()

    @pytest.mark.asyncio
    async def test_interrupt_when_not_connected_is_noop(self, provider):
        # Should not raise
        await provider.interrupt()


class TestGrokVoiceProviderDefaultConfig:
    def test_default_voice(self):
        config = ProviderConfig(type="grok", api_key="test")
        provider = GrokVoiceProvider(config)
        assert provider.name == "grok"

    @pytest.mark.asyncio
    async def test_default_model_in_url(self):
        config = ProviderConfig(type="grok", api_key="test", model="")
        provider = GrokVoiceProvider(config)

        mock_ws = AsyncMock()
        mock_ws.__aiter__ = AsyncMock(return_value=iter([]))

        with patch("voicekit.providers.grok.websockets.connect", new_callable=AsyncMock) as mock_connect:
            mock_connect.return_value = mock_ws
            await provider.connect()

            url = mock_connect.call_args[0][0]
            assert "model=grok-3" in url

            await provider.disconnect()


class TestGrokProviderFactory:
    def test_factory_creates_grok_provider(self):
        from voicekit.daemon import _create_single_provider

        config = ProviderConfig(type="grok", api_key="test-key")
        provider = _create_single_provider("grok", config)
        assert isinstance(provider, GrokVoiceProvider)
        assert provider.name == "grok"
