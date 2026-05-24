"""TwilioAudioInterface: bridges Twilio Media Stream audio to ElevenLabs SDK.

Implements the ElevenLabs AudioInterface ABC so the Conversation class can
send/receive audio through a Twilio phone call.

Audio formats:
  - Twilio sends/receives: μ-law 8kHz mono (base64-encoded in JSON frames)
  - ElevenLabs expects/produces: 16-bit PCM 16kHz mono (raw bytes)

We convert between these formats using audioop (built-in).
"""
from __future__ import annotations

import asyncio
import audioop
import base64
import json
import logging

from fastapi import WebSocket
from starlette.websockets import WebSocketState

from elevenlabs.conversational_ai.conversation import AudioInterface

logger = logging.getLogger(__name__)


class TwilioAudioInterface(AudioInterface):
    """Bridges Twilio Media Stream WebSocket to ElevenLabs Conversation."""

    def __init__(self, websocket: WebSocket) -> None:
        self.websocket = websocket
        self._input_callback = None
        self.stream_sid: str | None = None
        self._loop = asyncio.get_event_loop()

    def start(self, input_callback):
        """Called by ElevenLabs SDK when conversation starts."""
        self._input_callback = input_callback

    def stop(self):
        """Called by ElevenLabs SDK when conversation ends."""
        self._input_callback = None
        self.stream_sid = None

    def output(self, audio: bytes):
        """Send audio from ElevenLabs (16-bit PCM 16kHz) to Twilio (μ-law 8kHz).

        Must return quickly — schedules the async send on the event loop.
        """
        asyncio.run_coroutine_threadsafe(
            self._send_audio_to_twilio(audio), self._loop
        )

    def interrupt(self):
        """ElevenLabs signals user interrupted — clear Twilio's audio buffer."""
        asyncio.run_coroutine_threadsafe(
            self._send_clear_to_twilio(), self._loop
        )

    async def handle_twilio_message(self, message: dict) -> None:
        """Process a Twilio Media Stream JSON message.

        Called from the WebSocket handler for each incoming frame.
        """
        event = message.get("event")

        if event == "start":
            start_data = message.get("start", {})
            self.stream_sid = start_data.get("streamSid")
            logger.info("Twilio stream started: sid=%s", self.stream_sid)

        elif event == "media":
            if self._input_callback:
                media = message.get("media", {})
                payload = media.get("payload", "")
                # Decode base64 μ-law audio from Twilio
                ulaw_audio = base64.b64decode(payload)
                # Convert μ-law 8kHz → PCM 16-bit 8kHz
                pcm_8k = audioop.ulaw2lin(ulaw_audio, 2)
                # Upsample 8kHz → 16kHz (ElevenLabs expects 16kHz)
                pcm_16k, _ = audioop.ratecv(pcm_8k, 2, 1, 8000, 16000, None)
                self._input_callback(pcm_16k)

        elif event == "stop":
            logger.info("Twilio stream stopped")

    async def _send_audio_to_twilio(self, audio: bytes) -> None:
        """Convert PCM 16kHz → μ-law 8kHz and send to Twilio."""
        if not self.stream_sid:
            return

        try:
            # Downsample 16kHz → 8kHz
            pcm_8k, _ = audioop.ratecv(audio, 2, 1, 16000, 8000, None)
            # Convert PCM → μ-law
            ulaw_audio = audioop.lin2ulaw(pcm_8k, 2)
            # Base64 encode and send
            audio_payload = base64.b64encode(ulaw_audio).decode("utf-8")
            message = {
                "event": "media",
                "streamSid": self.stream_sid,
                "media": {"payload": audio_payload},
            }
            if self.websocket.application_state == WebSocketState.CONNECTED:
                await self.websocket.send_text(json.dumps(message))
        except Exception:
            logger.exception("Failed to send audio to Twilio")

    async def _send_clear_to_twilio(self) -> None:
        """Send clear event to Twilio to stop playing buffered audio."""
        if not self.stream_sid:
            return

        try:
            message = {"event": "clear", "streamSid": self.stream_sid}
            if self.websocket.application_state == WebSocketState.CONNECTED:
                await self.websocket.send_text(json.dumps(message))
        except Exception:
            logger.exception("Failed to send clear to Twilio")
