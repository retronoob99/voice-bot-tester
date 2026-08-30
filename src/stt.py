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

# The end-of-turn backstop, used when `speech_final` never arrives -- which on this
# target agent is the COMMON case, not the rare one: 56% of all turns across the
# call archive (7 of 9 on simple_schedule) were finalised by UtteranceEnd, so this
# value, not `endpointing`, is what most replies actually wait on.
#
# Lowered 1500 -> 1000 (Deepgram's documented minimum) to take 500ms off the agent's
# perceived silence on every one of those turns. The old comment here said this had
# to stay "comfortably above" endpointing or it would become a second early trigger;
# that reasoning was about the 1000-vs-300 configuration, where the fault was
# `endpointing` at 300 firing on a breath. `endpointing` is what decides WHERE an
# utterance is split -- UtteranceEnd only decides how long we wait for a split that
# Deepgram never made. Lowering the backstop toward it therefore does not create new
# mid-sentence splits; it only shortens the wait for the ones already being made.
#
# The residual risk -- an agent that pauses >1s mid-sentence without Deepgram calling
# it an endpoint -- is caught downstream rather than here: `_sounds_unfinished` flags
# the fragment and `_take_agent_turn` grants UNFINISHED_WAITS x UNFINISHED_QUIET_S to
# collect the rest before replying. That machinery exists for exactly this case.
UTTERANCE_END_MS = _env_int("DEEPGRAM_UTTERANCE_END_MS", 1000)

# Deepgram rejects utterance_end_ms below 1000 outright, so a lower override is a
# failed connection mid-call rather than a faster one.
DEEPGRAM_MIN_UTTERANCE_END_MS = 1000


def stt_connection(api_key: str):
    if UTTERANCE_END_MS < DEEPGRAM_MIN_UTTERANCE_END_MS:
        raise ValueError(
            f"DEEPGRAM_UTTERANCE_END_MS={UTTERANCE_END_MS} is below Deepgram's minimum "
            f"of {DEEPGRAM_MIN_UTTERANCE_END_MS}ms; the connection would be rejected."
        )
    if UTTERANCE_END_MS <= ENDPOINTING_MS:
        logger.warning(
            "utterance_end_ms (%d) <= endpointing (%d): the backstop can now preempt "
            "the primary endpoint detector instead of catching what it missed.",
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
