from __future__ import annotations

import logging
import os

from deepgram import AsyncDeepgramClient

logger = logging.getLogger("stt")

MULAW_SAMPLE_RATE = 8000


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        logger.warning("%s=%r is not an integer; using %d", name, raw, default)
        return default


# How long Deepgram waits for silence before declaring `speech_final`. At the SDK
# default of 300ms this fires on the agent's mid-sentence breath, so one question
# arrives as several fragments and the patient answers half of it. Observed live:
# "Thank you, Maria. Can you please provide your date of" was finalised and
# answered as if it were a complete question.
#
# 800ms is past a normal intra-sentence pause but still well short of a turn
# handover, so genuine end-of-turn is not delayed noticeably.
ENDPOINTING_MS = _env_int("DEEPGRAM_ENDPOINTING_MS", 800)

# The end-of-turn backstop, used when `speech_final` never arrives. It has to stay
# comfortably ABOVE endpointing or it just becomes a second early trigger — at the
# previous 1000ms vs 300ms it was firing mid-sentence too.
UTTERANCE_END_MS = _env_int("DEEPGRAM_UTTERANCE_END_MS", 1500)


def stt_connection(api_key: str):
    if UTTERANCE_END_MS <= ENDPOINTING_MS:
        logger.warning(
            "utterance_end_ms (%d) <= endpointing (%d): UtteranceEnd will fire "
            "mid-sentence instead of acting as an end-of-turn backstop.",
            UTTERANCE_END_MS,
            ENDPOINTING_MS,
        )
    logger.info(
        "Deepgram STT: endpointing=%dms utterance_end_ms=%dms interim_results=True "
        "(interims are never acted on; only speech_final/UtteranceEnd trigger a reply)",
        ENDPOINTING_MS,
        UTTERANCE_END_MS,
    )
    client = AsyncDeepgramClient(api_key=api_key)
    return client.listen.v1.connect(
        model="nova-3",
        encoding="mulaw",
        sample_rate=MULAW_SAMPLE_RATE,
        channels=1,
        # Interims are requested only so `is_final`/`speech_final` bookkeeping works
        # and so SpeechStarted VAD events arrive. No reply is ever generated from one.
        interim_results=True,
        smart_format=True,
        punctuate=True,
        endpointing=ENDPOINTING_MS,
        utterance_end_ms=UTTERANCE_END_MS,
        vad_events=True,
    )
