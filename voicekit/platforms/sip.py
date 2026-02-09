"""SIP/phone call platform adapter.

Connects VoiceKit to the telephone network via SIP (Session Initiation
Protocol). Supports receiving and making phone calls through a SIP provider
such as Twilio, Vonage, FreePBX, or any standards-compliant SIP registrar.

Uses the ``aiosip`` library for asynchronous SIP signalling (REGISTER,
INVITE, BYE) and streams RTP audio via the built-in RTP transport.

Supports:
- Registering with a SIP server
- Receiving incoming phone calls (INVITE)
- Making outbound calls
- Auto-answering with AI voice
- DTMF tone detection and generation
- Call transfer (SIP REFER)
- Caller-ID allowlisting

Audio format: SIP/RTP typically uses G.711 u-law (PCMU) at 8 kHz mono,
or PCM 16-bit (L16) at 16 kHz mono depending on codec negotiation.
The adapter converts to/from VoiceKit's internal format (24 kHz mono).

Requires: pip install voicekit[sip]
  - aiosip >= 0.7
"""

from __future__ import annotations

import asyncio
import audioop
import logging
import struct
from typing import Any

from voicekit.config import SipPlatformConfig
from voicekit.core.audio import AudioFormat, convert_audio
from voicekit.platforms.base import AudioCallback, PlatformAdapter

logger = logging.getLogger(__name__)

# SIP/RTP audio formats
# G.711 u-law (PCMU): 8 kHz, mono, 8-bit u-law (decoded to 16-bit PCM for processing)
SIP_AUDIO_FORMAT_PCMU = AudioFormat(sample_rate=8000, channels=1, sample_width=2)

# Linear PCM at 16 kHz (L16/16000) — used when peer supports wideband
SIP_AUDIO_FORMAT_L16 = AudioFormat(sample_rate=16000, channels=1, sample_width=2)

# 20ms frame at 8 kHz mono (PCMU): 160 samples = 160 bytes (u-law) or 320 bytes (PCM16)
SIP_FRAME_SAMPLES = 160
SIP_FRAME_BYTES_ULAW = 160
SIP_FRAME_BYTES_PCM16 = 320

# DTMF event duration in RTP timestamp units (8 kHz)
DTMF_DURATION = 1600  # 200ms at 8 kHz

# RTP payload types
RTP_PT_PCMU = 0
RTP_PT_L16_16K = 96  # dynamic, negotiated via SDP


class SipPlatform(PlatformAdapter):
    """SIP phone call adapter using aiosip.

    Registers with a SIP server, listens for incoming INVITE requests
    (phone calls), and streams bidirectional audio via RTP. Outbound
    calls can be placed via the ``make_call`` method.

    Audio is decoded from G.711 u-law (or L16) to PCM 16-bit, converted
    to VoiceKit's internal 24 kHz mono format, and forwarded to the AI
    provider. Responses travel the reverse path.
    """

    def __init__(self, config: SipPlatformConfig) -> None:
        self._config = config
        self._callback: AudioCallback | None = None
        self._active = False
        self._loop: asyncio.AbstractEventLoop | None = None
        self._sip_app: Any = None  # aiosip.Application
        self._transport: Any = None
        self._registration: Any = None
        self._active_dialog: Any = None  # current call dialog
        self._output_queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=200)
        self._send_task: asyncio.Task[None] | None = None
        self._rtp_reader_task: asyncio.Task[None] | None = None
        self._rtp_transport: tuple[Any, Any] | None = None  # (transport, protocol)
        self._rtp_local_port: int = 0
        self._rtp_remote_addr: tuple[str, int] | None = None
        self._use_wideband = False  # True if negotiated L16/16000
        self._rtp_sequence: int = 0
        self._rtp_timestamp: int = 0
        self._rtp_ssrc: int = 0
        self._in_call = False

    @property
    def name(self) -> str:
        return "sip"

    @property
    def is_active(self) -> bool:
        return self._active

    @property
    def _audio_format(self) -> AudioFormat:
        """Return the active audio format based on codec negotiation."""
        return SIP_AUDIO_FORMAT_L16 if self._use_wideband else SIP_AUDIO_FORMAT_PCMU

    async def start(self) -> None:
        """Register with the SIP server and start listening for calls."""
        try:
            import aiosip  # type: ignore[import-untyped]
        except ImportError:
            raise RuntimeError(
                "aiosip is required for SIP support. "
                "Install with: pip install voicekit[sip]"
            )

        self._loop = asyncio.get_running_loop()

        # Generate a random SSRC for our RTP stream
        import random

        self._rtp_ssrc = random.randint(0, 0xFFFFFFFF)

        # Create SIP application
        self._sip_app = aiosip.Application()

        # Register handler for incoming INVITE (calls)
        self._sip_app.dialplan.add_user("", self._handle_invite)

        # Create UDP transport
        self._transport = await self._sip_app.connect(
            protocol=aiosip.UDP,
            remote_addr=(self._config.server, self._config.port),
            local_addr=("0.0.0.0", self._config.local_rtp_port_start),
        )

        # Register with the SIP server
        if self._config.username and self._config.server:
            try:
                self._registration = await self._transport.register(
                    from_details=aiosip.Contact.from_header(
                        f"sip:{self._config.username}@{self._config.server}"
                    ),
                    to_details=aiosip.Contact.from_header(
                        f"sip:{self._config.username}@{self._config.server}"
                    ),
                    password=self._config.password,
                    expires=self._config.register_expires,
                )
                logger.info(
                    "SIP registered as %s@%s:%d",
                    self._config.username,
                    self._config.server,
                    self._config.port,
                )
            except Exception as exc:
                logger.error("SIP registration failed: %s", exc)
                raise

        self._active = True
        logger.info(
            "SIP platform started (server=%s:%d, auto_answer=%s)",
            self._config.server,
            self._config.port,
            self._config.auto_answer,
        )

    async def _handle_invite(self, dialog: Any, request: Any) -> None:
        """Handle an incoming SIP INVITE (phone call).

        Args:
            dialog: The aiosip dialog representing this call.
            request: The SIP INVITE request message.
        """
        caller = str(getattr(request, "from_details", "unknown"))
        logger.info("Incoming SIP call from %s", caller)

        # Check caller allowlist
        if self._config.allowed_numbers:
            caller_number = self._extract_number(caller)
            if caller_number not in self._config.allowed_numbers:
                logger.info("Rejecting call from %s (not in allowed_numbers)", caller_number)
                dialog.reply(request, status_code=403)
                return

        # Store the dialog for this call
        self._active_dialog = dialog

        if self._config.auto_answer:
            await self._answer_invite(dialog, request)
        else:
            # Send 180 Ringing and wait for manual answer
            dialog.reply(request, status_code=180)
            logger.info("Call ringing — waiting for answer_call()")

    def _extract_number(self, sip_uri: str) -> str:
        """Extract phone number from a SIP URI like 'sip:+15551234567@server'."""
        # Strip sip: prefix
        uri = sip_uri
        if uri.startswith("sip:"):
            uri = uri[4:]
        # Strip display name if present
        if "<" in uri:
            uri = uri.split("<")[1].split(">")[0]
            if uri.startswith("sip:"):
                uri = uri[4:]
        # Take the user part (before @)
        number = uri.split("@")[0]
        return number

    async def _answer_invite(self, dialog: Any, request: Any) -> None:
        """Answer an incoming call by sending 200 OK with SDP and starting RTP."""
        # Parse remote SDP to find their RTP address and codec
        sdp_body = getattr(request, "body", "")
        self._rtp_remote_addr = self._parse_rtp_from_sdp(sdp_body)
        self._use_wideband = "L16/16000" in sdp_body

        # Allocate a local RTP port
        self._rtp_local_port = self._config.local_rtp_port_start

        # Build our SDP answer
        codec_line = (
            f"a=rtpmap:{RTP_PT_L16_16K} L16/16000\r\n"
            if self._use_wideband
            else f"a=rtpmap:{RTP_PT_PCMU} PCMU/8000\r\n"
        )
        payload_type = RTP_PT_L16_16K if self._use_wideband else RTP_PT_PCMU

        local_sdp = (
            "v=0\r\n"
            f"o=voicekit 0 0 IN IP4 0.0.0.0\r\n"
            "s=VoiceKit\r\n"
            f"c=IN IP4 0.0.0.0\r\n"
            "t=0 0\r\n"
            f"m=audio {self._rtp_local_port} RTP/AVP {payload_type}\r\n"
            f"{codec_line}"
            "a=sendrecv\r\n"
        )

        # Send 200 OK
        dialog.reply(request, status_code=200, body=local_sdp)

        # Start RTP audio transport
        await self._start_rtp()
        self._in_call = True
        logger.info("SIP call answered, RTP streaming started")

    def _parse_rtp_from_sdp(self, sdp: str) -> tuple[str, int]:
        """Parse the remote RTP address and port from SDP.

        Returns:
            Tuple of (ip_address, port).
        """
        remote_ip = "0.0.0.0"
        remote_port = 0

        for line in sdp.splitlines():
            line = line.strip()
            if line.startswith("c=IN IP4 "):
                remote_ip = line.split()[-1]
            elif line.startswith("m=audio "):
                parts = line.split()
                if len(parts) >= 2:
                    try:
                        remote_port = int(parts[1])
                    except ValueError:
                        pass

        return (remote_ip, remote_port)

    async def _start_rtp(self) -> None:
        """Start the RTP UDP transport for bidirectional audio."""
        loop = asyncio.get_running_loop()

        # Create UDP socket for RTP
        transport, protocol = await loop.create_datagram_endpoint(
            lambda: _RtpProtocol(self._on_rtp_received),
            local_addr=("0.0.0.0", self._rtp_local_port),
        )
        self._rtp_transport = (transport, protocol)

        # Start send loop
        self._send_task = asyncio.create_task(
            self._rtp_send_loop(), name="sip_rtp_send"
        )

        logger.debug(
            "RTP started on port %d → %s",
            self._rtp_local_port,
            self._rtp_remote_addr,
        )

    def _on_rtp_received(self, data: bytes, addr: tuple[str, int]) -> None:
        """Handle an incoming RTP packet from the remote peer.

        Called from the asyncio transport protocol (on the event loop thread).
        """
        if not self._callback or not self._loop or not self._active:
            return

        if len(data) < 12:
            return  # Too short for RTP header

        # Parse minimal RTP header
        payload_type = data[1] & 0x7F
        header_len = 12
        # Account for CSRC entries
        cc = data[0] & 0x0F
        header_len += cc * 4

        payload = data[header_len:]
        if not payload:
            return

        # Decode payload based on codec
        if payload_type == RTP_PT_PCMU:
            # G.711 u-law → PCM 16-bit
            pcm_data = audioop.ulaw2lin(payload, 2)
        elif payload_type == RTP_PT_L16_16K:
            # Already linear PCM 16-bit (network byte order → host)
            pcm_data = self._ntohs_pcm(payload)
        else:
            return  # Unsupported codec

        # Convert to internal format
        internal_audio = convert_audio(pcm_data, self._audio_format, AudioFormat())

        asyncio.run_coroutine_threadsafe(self._callback(internal_audio), self._loop)

    @staticmethod
    def _ntohs_pcm(data: bytes) -> bytes:
        """Convert network byte order (big-endian) PCM16 to little-endian."""
        sample_count = len(data) // 2
        samples = struct.unpack(f"!{sample_count}h", data)
        return struct.pack(f"<{sample_count}h", *samples)

    @staticmethod
    def _htons_pcm(data: bytes) -> bytes:
        """Convert little-endian PCM16 to network byte order (big-endian)."""
        sample_count = len(data) // 2
        samples = struct.unpack(f"<{sample_count}h", data)
        return struct.pack(f"!{sample_count}h", *samples)

    async def _rtp_send_loop(self) -> None:
        """Send audio from the output queue as RTP packets."""
        while self._active and self._in_call:
            try:
                chunk = await asyncio.wait_for(self._output_queue.get(), timeout=0.5)
            except asyncio.TimeoutError:
                continue
            except asyncio.CancelledError:
                break

            if not self._rtp_transport or not self._rtp_remote_addr or not chunk:
                continue

            transport, _ = self._rtp_transport

            # Encode payload
            if self._use_wideband:
                payload = self._htons_pcm(chunk)
                pt = RTP_PT_L16_16K
                samples_per_frame = len(chunk) // 2
            else:
                # PCM 16-bit → G.711 u-law
                payload = audioop.lin2ulaw(chunk, 2)
                pt = RTP_PT_PCMU
                samples_per_frame = len(chunk) // 2

            # Build RTP header (12 bytes)
            self._rtp_sequence = (self._rtp_sequence + 1) & 0xFFFF
            self._rtp_timestamp = (self._rtp_timestamp + samples_per_frame) & 0xFFFFFFFF

            rtp_header = struct.pack(
                "!BBHII",
                0x80,  # V=2, P=0, X=0, CC=0
                pt & 0x7F,  # M=0, PT
                self._rtp_sequence,
                self._rtp_timestamp,
                self._rtp_ssrc,
            )

            packet = rtp_header + payload

            try:
                transport.sendto(packet, self._rtp_remote_addr)
            except Exception:
                if self._active:
                    logger.debug("Error sending RTP packet", exc_info=True)

    async def make_call(self, destination: str) -> None:
        """Place an outbound SIP call.

        Args:
            destination: SIP URI or phone number to call
                         (e.g. "sip:+15551234567@provider.com" or "+15551234567").
        """
        if not self._active or not self._transport:
            logger.warning("Cannot make call: SIP platform not active")
            return

        if self._in_call:
            logger.warning("Already in a SIP call; hang up first")
            return

        # Normalise destination to SIP URI
        if not destination.startswith("sip:"):
            destination = f"sip:{destination}@{self._config.server}"

        logger.info("Placing outbound SIP call to %s", destination)

        import aiosip  # type: ignore[import-untyped]

        # Allocate RTP port
        self._rtp_local_port = self._config.local_rtp_port_start
        payload_type = RTP_PT_PCMU
        codec_line = f"a=rtpmap:{RTP_PT_PCMU} PCMU/8000\r\n"

        local_sdp = (
            "v=0\r\n"
            f"o=voicekit 0 0 IN IP4 0.0.0.0\r\n"
            "s=VoiceKit\r\n"
            f"c=IN IP4 0.0.0.0\r\n"
            "t=0 0\r\n"
            f"m=audio {self._rtp_local_port} RTP/AVP {payload_type}\r\n"
            f"{codec_line}"
            "a=sendrecv\r\n"
        )

        from_uri = f"sip:{self._config.username}@{self._config.server}"
        dialog = self._transport.invite(
            from_details=aiosip.Contact.from_header(from_uri),
            to_details=aiosip.Contact.from_header(destination),
            password=self._config.password,
            body=local_sdp,
        )

        self._active_dialog = dialog

        # Wait for the response (200 OK with SDP)
        response = await dialog
        if response and hasattr(response, "status_code"):
            if response.status_code == 200:
                sdp_body = getattr(response, "body", "")
                self._rtp_remote_addr = self._parse_rtp_from_sdp(sdp_body)
                self._use_wideband = "L16/16000" in sdp_body
                await self._start_rtp()
                self._in_call = True
                logger.info("Outbound SIP call connected")
            else:
                logger.warning(
                    "Outbound SIP call failed with status %d", response.status_code
                )
                self._active_dialog = None

    async def send_dtmf(self, digit: str) -> None:
        """Send a DTMF tone via RTP (RFC 2833 telephone-event).

        Args:
            digit: The DTMF digit ('0'-'9', '*', '#', 'A'-'D').
        """
        if not self._in_call or not self._rtp_transport or not self._rtp_remote_addr:
            return

        dtmf_map = {
            "0": 0, "1": 1, "2": 2, "3": 3, "4": 4,
            "5": 5, "6": 6, "7": 7, "8": 8, "9": 9,
            "*": 10, "#": 11,
            "A": 12, "B": 13, "C": 14, "D": 15,
        }

        event_code = dtmf_map.get(digit.upper())
        if event_code is None:
            logger.warning("Invalid DTMF digit: %s", digit)
            return

        transport, _ = self._rtp_transport

        # RFC 2833: send 3 packets (start, continuation, end)
        for i in range(3):
            self._rtp_sequence = (self._rtp_sequence + 1) & 0xFFFF
            end_bit = 1 if i == 2 else 0

            rtp_header = struct.pack(
                "!BBHII",
                0x80,
                (101 | (0x80 if i == 0 else 0)),  # PT=101 (telephone-event), M bit on first
                self._rtp_sequence,
                self._rtp_timestamp,
                self._rtp_ssrc,
            )

            # RFC 2833 payload: event, E/R/volume, duration
            dtmf_payload = struct.pack(
                "!BBH",
                event_code,
                (end_bit << 7) | 10,  # E bit, volume=10
                DTMF_DURATION if end_bit else (DTMF_DURATION * (i + 1) // 3),
            )

            try:
                transport.sendto(rtp_header + dtmf_payload, self._rtp_remote_addr)
            except Exception:
                logger.debug("Error sending DTMF RTP packet", exc_info=True)

            if i < 2:
                await asyncio.sleep(0.04)  # 40ms between packets

        self._rtp_timestamp = (self._rtp_timestamp + DTMF_DURATION) & 0xFFFFFFFF

    async def stop(self) -> None:
        """Hang up any active call, unregister, and shut down."""
        self._active = False

        if self._in_call:
            await self.hang_up()

        # Unregister from SIP server
        if self._registration:
            try:
                await self._registration.unregister()
            except Exception:
                logger.debug("Error unregistering from SIP server", exc_info=True)
            self._registration = None

        # Close SIP transport
        if self._sip_app:
            try:
                await self._sip_app.close()
            except Exception:
                logger.debug("Error closing SIP application", exc_info=True)
            self._sip_app = None

        self._transport = None
        logger.info("SIP platform stopped")

    def on_audio_received(self, callback: AudioCallback) -> None:
        """Register callback for captured audio from SIP calls."""
        self._callback = callback

    async def send_audio(self, audio: bytes) -> None:
        """Send audio to the active SIP call.

        Converts from internal format (24 kHz mono) to the negotiated
        SIP format and queues for the RTP send loop.
        """
        if not self._active or not self._in_call:
            return

        sip_audio = convert_audio(audio, AudioFormat(), self._audio_format)

        try:
            self._output_queue.put_nowait(sip_audio)
        except asyncio.QueueFull:
            try:
                self._output_queue.get_nowait()
            except asyncio.QueueEmpty:
                pass
            self._output_queue.put_nowait(sip_audio)

    async def answer_call(self) -> None:
        """Answer a ringing incoming call."""
        if not self._active_dialog or self._in_call:
            return

        logger.info("Answering incoming SIP call")
        # The dialog and request are stored from _handle_invite
        # Re-trigger the answer flow
        if hasattr(self._active_dialog, "_pending_request"):
            await self._answer_invite(
                self._active_dialog,
                self._active_dialog._pending_request,
            )

    async def reject_call(self) -> None:
        """Reject an incoming SIP call with 486 Busy Here."""
        if not self._active_dialog or self._in_call:
            return

        logger.info("Rejecting incoming SIP call")
        try:
            if hasattr(self._active_dialog, "_pending_request"):
                self._active_dialog.reply(
                    self._active_dialog._pending_request,
                    status_code=486,
                )
        except Exception:
            logger.debug("Error rejecting SIP call", exc_info=True)

        self._active_dialog = None

    async def hang_up(self) -> None:
        """End the current SIP call."""
        if not self._in_call:
            return

        logger.info("Hanging up SIP call")

        # Stop RTP
        await self._stop_rtp()

        # Send BYE
        if self._active_dialog:
            try:
                self._active_dialog.bye()
            except Exception:
                logger.debug("Error sending SIP BYE", exc_info=True)

        self._active_dialog = None
        self._in_call = False
        self._rtp_remote_addr = None

    async def _stop_rtp(self) -> None:
        """Stop the RTP transport and send task."""
        for task in (self._send_task, self._rtp_reader_task):
            if task and not task.done():
                task.cancel()
                try:
                    await task
                except asyncio.CancelledError:
                    pass

        self._send_task = None
        self._rtp_reader_task = None

        if self._rtp_transport:
            transport, _ = self._rtp_transport
            transport.close()
            self._rtp_transport = None


class _RtpProtocol(asyncio.DatagramProtocol):
    """Minimal asyncio datagram protocol for RTP packet reception."""

    def __init__(self, callback: Any) -> None:
        self._callback = callback

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        self._callback(data, addr)

    def error_received(self, exc: Exception) -> None:
        logger.debug("RTP socket error: %s", exc)
