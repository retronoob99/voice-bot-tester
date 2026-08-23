from __future__ import annotations

from deepgram import AsyncDeepgramClient

MULAW_SAMPLE_RATE = 8000


def stt_connection(api_key: str):
    client = AsyncDeepgramClient(api_key=api_key)
    return client.listen.v1.connect(
        model="nova-3",
        encoding="mulaw",
        sample_rate=MULAW_SAMPLE_RATE,
        channels=1,
        interim_results=True,
        smart_format=True,
        punctuate=True,
        endpointing=300,
        utterance_end_ms=1000,
        vad_events=True,
    )
