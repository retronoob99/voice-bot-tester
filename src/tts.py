from __future__ import annotations

from deepgram import AsyncDeepgramClient
from deepgram.speak.v1.types.speak_v1flushed import SpeakV1Flushed
from deepgram.speak.v1.types.speak_v1text import SpeakV1Text

MULAW_SAMPLE_RATE = 8000


async def synthesize_mulaw(api_key: str, text: str, voice: str) -> bytes:
    client = AsyncDeepgramClient(api_key=api_key)
    audio = bytearray()
    async with client.speak.v1.connect(
        model=voice, encoding="mulaw", sample_rate=MULAW_SAMPLE_RATE
    ) as socket:
        await socket.send_text(SpeakV1Text(text=text))
        await socket.send_flush()
        async for message in socket:
            if isinstance(message, (bytes, bytearray)):
                audio.extend(message)
            elif isinstance(message, SpeakV1Flushed):
                break
        await socket.send_close()
    return bytes(audio)
