from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import AsyncIterator, Optional

from deepgram import AsyncDeepgramClient
from deepgram.speak.v1.types.speak_v1flushed import SpeakV1Flushed
from deepgram.speak.v1.types.speak_v1text import SpeakV1Text

logger = logging.getLogger("tts")

MULAW_SAMPLE_RATE = 8000


class TtsSession:
    """One Deepgram Aura connection, reused for every line of a single call.

    Two costs are being removed here, both measured on a live call where synthesis
    accounted for 20.0s of a 74.5s call (mean 3.99s per line, fitting
    `elapsed ~= 1.0 + 0.55 * audio_seconds`):

    * The ~1.0s constant was a fresh client plus WebSocket handshake per line. The
      socket is now opened once and held for the call.
    * The `0.55 * audio_seconds` term was the caller buffering the whole clip before
      Twilio saw a single byte. `stream()` yields chunks as Aura produces them, so
      playback starts on the first chunk. Aura synthesises at roughly 0.55x realtime
      and Twilio plays at 1.0x, so the buffer fills faster than it drains and
      streaming cannot underrun.

    Not thread-safe and not safe for concurrent lines: the patient speaks one line at
    a time, and a lock enforces that rather than trusting callers.
    """

    def __init__(self, api_key: str, voice: str, speed: float = 1.0):
        self._api_key = api_key
        self._voice = voice
        self._speed = speed
        self._stack: Optional[contextlib.AsyncExitStack] = None
        self._socket = None
        self._lock = asyncio.Lock()

    async def _connect(self):
        stack = contextlib.AsyncExitStack()
        client = AsyncDeepgramClient(api_key=self._api_key)
        socket = await stack.enter_async_context(
            client.speak.v1.connect(
                model=self._voice,
                encoding="mulaw",
                sample_rate=MULAW_SAMPLE_RATE,
                speed=self._speed,
            )
        )
        self._stack, self._socket = stack, socket
        return socket

    async def _discard(self) -> None:
        """Drop the current socket. The next line reconnects from scratch."""
        stack, self._stack, self._socket = self._stack, None, None
        if stack is not None:
            with contextlib.suppress(Exception):
                await stack.aclose()

    async def stream(self, text: str) -> AsyncIterator[bytes]:
        """Yield mulaw audio for one line as Aura produces it.

        A dead socket is retried once on a fresh connection. Deepgram closes an idle
        TTS socket well inside the length of a call, so a mid-call reconnect is
        expected rather than exceptional — without the retry the patient would simply
        go mute for the rest of the call.
        """
        async with self._lock:
            for attempt in (1, 2):
                socket = self._socket or await self._connect()
                try:
                    await socket.send_text(SpeakV1Text(text=text))
                    await socket.send_flush()
                    async for message in socket:
                        if isinstance(message, (bytes, bytearray)):
                            yield bytes(message)
                        elif isinstance(message, SpeakV1Flushed):
                            return
                    # The socket ended without ever flushing: treat as a dead
                    # connection so the retry below gets a chance.
                    raise RuntimeError("TTS socket closed before Flushed")
                except Exception:
                    await self._discard()
                    if attempt == 2:
                        raise
                    logger.warning("TTS socket failed; reconnecting once", exc_info=True)

    async def aclose(self) -> None:
        with contextlib.suppress(Exception):
            if self._socket is not None:
                await self._socket.send_close()
        await self._discard()


async def synthesize_mulaw(api_key: str, text: str, voice: str, speed: float = 1.0) -> bytes:
    """Render one patient line to a complete buffer.

    Kept for callers that genuinely need the whole clip up front. The live path uses
    TtsSession.stream() instead — buffering here is what put seconds of dead air in
    front of every reply.
    """
    session = TtsSession(api_key, voice, speed)
    try:
        audio = bytearray()
        async for chunk in session.stream(text):
            audio.extend(chunk)
        return bytes(audio)
    finally:
        await session.aclose()
