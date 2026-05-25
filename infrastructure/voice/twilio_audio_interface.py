"""Bridges Twilio Media Stream audio ↔ ElevenLabs Conversation audio.

Twilio sends/receives **mulaw 8000 Hz** audio.
ElevenLabs Conversation expects **16-bit PCM mono 16 kHz** audio.

This AudioInterface implementation transcodes between the two formats and
shuttles bytes between the Twilio WebSocket and the ElevenLabs Conversation
session.
"""
from __future__ import annotations

import asyncio
import audioop  # noqa: F401 — used for mulaw<->PCM conversion
import base64
import json
import logging
import queue
import threading
from typing import TYPE_CHECKING, Callable

from elevenlabs.conversational_ai.conversation import AudioInterface

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)

# Twilio sends mulaw 8 kHz; ElevenLabs expects PCM s16le 16 kHz.
_TWILIO_SAMPLE_RATE = 8000
_EL_SAMPLE_RATE = 16000
_MULAW_SAMPLE_WIDTH = 1  # 1 byte per sample (mulaw)
_PCM_SAMPLE_WIDTH = 2  # 2 bytes per sample (s16le)


class TwilioAudioInterface(AudioInterface):
    """Feed Twilio media-stream audio into an ElevenLabs ``Conversation``.

    Usage (inside an async WebSocket handler)::

        iface = TwilioAudioInterface()
        conversation = Conversation(client, agent_id=seng_id,
                                    requires_auth=True,
                                    audio_interface=iface)
        conversation.start_session()

        # Then pump Twilio messages in:
        async for raw in twilio_ws.iter_text():
            msg = json.loads(raw)
            if msg["event"] == "start":
                iface.set_stream_sid(msg["start"]["streamSid"])
            elif msg["event"] == "media":
                iface.receive_twilio_audio(msg["media"]["payload"])
            elif msg["event"] == "stop":
                conversation.end_session()
    """

    def __init__(self) -> None:
        self._input_callback: Callable[[bytes], None] | None = None
        self._stream_sid: str = ""
        # Outbound audio destined for Twilio (mulaw base64-encoded JSON msgs)
        self._output_queue: queue.Queue[str] = queue.Queue()
        self._stopped = threading.Event()

    # -- Called by Conversation --------------------------------------------------

    def start(self, input_callback: Callable[[bytes], None]) -> None:
        self._input_callback = input_callback

    def stop(self) -> None:
        self._stopped.set()

    def output(self, audio: bytes) -> None:
        """Receive PCM s16le 16 kHz from ElevenLabs → transcode → queue for Twilio."""
        if self._stopped.is_set() or not self._stream_sid:
            return
        try:
            # Downsample PCM 16 kHz → PCM 8 kHz
            pcm_8k, _ = audioop.ratecv(audio, _PCM_SAMPLE_WIDTH, 1, _EL_SAMPLE_RATE, _TWILIO_SAMPLE_RATE, None)
            # PCM s16le → mulaw
            mulaw = audioop.lin2ulaw(pcm_8k, _PCM_SAMPLE_WIDTH)
            payload = base64.b64encode(mulaw).decode()
            msg = json.dumps({
                "event": "media",
                "streamSid": self._stream_sid,
                "media": {"payload": payload},
            })
            self._output_queue.put_nowait(msg)
        except Exception:
            logger.exception("Error transcoding ElevenLabs → Twilio audio")

    def interrupt(self) -> None:
        # Clear pending audio on interruption
        while not self._output_queue.empty():
            try:
                self._output_queue.get_nowait()
            except queue.Empty:
                break
        # Send a clear message to Twilio to stop current audio
        if self._stream_sid:
            msg = json.dumps({
                "event": "clear",
                "streamSid": self._stream_sid,
            })
            self._output_queue.put_nowait(msg)

    # -- Called by our WebSocket handler ----------------------------------------

    def set_stream_sid(self, sid: str) -> None:
        self._stream_sid = sid

    def receive_twilio_audio(self, base64_payload: str) -> None:
        """Accept a mulaw 8 kHz base64 chunk from Twilio and push to ElevenLabs."""
        if self._input_callback is None or self._stopped.is_set():
            return
        try:
            mulaw = base64.b64decode(base64_payload)
            # mulaw → PCM s16le at 8 kHz
            pcm_8k = audioop.ulaw2lin(mulaw, _PCM_SAMPLE_WIDTH)
            # Upsample PCM 8 kHz → PCM 16 kHz
            pcm_16k, _ = audioop.ratecv(pcm_8k, _PCM_SAMPLE_WIDTH, 1, _TWILIO_SAMPLE_RATE, _EL_SAMPLE_RATE, None)
            self._input_callback(pcm_16k)
        except Exception:
            logger.exception("Error transcoding Twilio → ElevenLabs audio")

    def drain_output(self) -> list[str]:
        """Return all queued Twilio-bound JSON messages (non-blocking)."""
        msgs: list[str] = []
        while not self._output_queue.empty():
            try:
                msgs.append(self._output_queue.get_nowait())
            except queue.Empty:
                break
        return msgs

    async def drain_output_async(self) -> list[str]:
        """Async wrapper — yields control while waiting for output."""
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, self.drain_output)
