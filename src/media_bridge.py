from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import time
import re
import uuid
from difflib import SequenceMatcher
from pathlib import Path
from typing import Optional

from deepgram.listen.v1.types.listen_v1results import ListenV1Results
from deepgram.listen.v1.types.listen_v1speech_started import ListenV1SpeechStarted
from deepgram.listen.v1.types.listen_v1utterance_end import ListenV1UtteranceEnd
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from twilio.rest import Client as TwilioClient

from .call_logger import CallLogger
from .config import settings
from .persona_engine import PersonaEngine
from .scenario import Scenario, find_scenario
from .stt import ENDPOINTING_MS, MULAW_SAMPLE_RATE, UTTERANCE_END_MS, stt_connection
from .tts import TtsSession

logger = logging.getLogger("media_bridge")


def _env_float(name: str, default: float) -> float:
    """Read a tuning knob from the environment, falling back to the shipped default."""
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        logger.warning("%s=%r is not a number; using %s", name, raw, default)
        return default

CALLS_DIR = Path(__file__).resolve().parent.parent / "calls"
# Twilio buffers inbound media and plays it out at realtime, so outbound audio is
# sent as fast as the socket accepts it rather than paced with sleeps. Sleep-pacing
# 20ms chunks underruns badly on Windows, where the timer resolution (~15.6ms) makes
# asyncio.sleep(0.02) take ~31ms and starves Twilio's buffer between every chunk.
# Playback is stopped by the `clear` event, not by throttling the send.
MULAW_CHUNK_BYTES = 3200  # 400ms of mulaw @ 8kHz

# How long the agent must be silent before the patient answers. Deepgram often splits
# one agent sentence into several `speech_final` fragments ("Hi, Maria." / "How can I
# help you today?"), and replying to the first means answering half a question — its
# events still decide where an utterance ends, this decides how long to let them keep
# coming. It is also the largest part of the delay before the patient answers, and
# is what the agent experiences as silence. Measured on a live call at 3.0: every
# turn left 7-10.6s of dead air, past the ~9s where the agent gives up and re-asks.
#
# Lowered from 1.8 when Deepgram's `endpointing` went from 300ms to 800ms (see
# src/stt.py): the STT now merges mid-sentence pauses itself instead of emitting a
# fragment per breath, so less of the merging has to happen here. The ~500ms this
# gives back is roughly what the higher endpointing costs, keeping the total
# agent-perceived silence where it was while making each `speech_final` far more
# likely to be a real end of turn.
# Trimmed 1.3 -> 1.0 to take ~0.3s off every speech_final turn. Live calls at 1.3
# showed frags=1 on essentially every turn, i.e. the window was rarely doing any
# merging at 800ms endpointing; the guard test floor is 0.5.
#
# Trimmed again 1.0 -> 0.6. The archive shows fragments=1 on every logged turn, so
# this window has not merged anything in a long time -- at 800ms endpointing the STT
# does the merging itself, and a genuinely split sentence is caught by the
# _sounds_unfinished path below, which gets its own longer windows. What was left was
# pure latency on the agent's side of the line.
AGENT_QUIET_S = _env_float("AGENT_QUIET_S", 0.6)

# Deepgram's `utterance_end_ms` (1500ms, see src/stt.py) means an `UtteranceEnd`
# fragment has ALREADY sat in silence for 1.5s by the time it reaches us. Waiting a
# further AGENT_QUIET_S on top double-counts that same silence. On a live call every
# single turn was finalised by UtteranceEnd, so each reply paid 1.5s of Deepgram
# detection PLUS 1.3s of window before Groq was even called, and the agent heard
# ~4s of dead air — which is what made the patient sound slow to answer.
#
# A `speech_final` fragment has only seen `endpointing` (800ms), so that path keeps
# the full AGENT_QUIET_S. This is deliberately NOT a cut to `utterance_end_ms`
# itself: that value has to stay well clear of `endpointing` (1500 vs 800) or
# fragments start arriving mid-sentence again.
#
# Do not drop below ~0.3s — that is about how long a genuinely-continuing agent
# takes to start its next fragment. Now AT that floor: with utterance_end_ms cut to
# 1000ms the fragment has still sat in a full second of silence before it reaches us,
# and an agent that carries on anyway is picked up by _catch_up() rather than by
# holding every single turn open longer.
AGENT_QUIET_AFTER_UTTERANCE_END_S = _env_float("AGENT_QUIET_AFTER_UTTERANCE_END_S", 0.3)

# Catch-up runs when the agent talked over our thinking time, so it is already known
# to have been mid-thought — a much shorter settle is enough, and paying the full
# window twice was the single biggest contributor to the dead air above.
# Kept below AGENT_QUIET_S (a guard test enforces it): catch-up is draining a known
# backlog, not deciding whether a turn ended, so it must never cost more than the
# turn window it follows.
CATCHUP_QUIET_S = _env_float("CATCHUP_QUIET_S", 0.45)

# Hard ceiling on silence before the patient speaks. Past this, answering a slightly
# stale question beats sounding like the line has dropped.
MAX_REPLY_DELAY_S = 4.5

# Extra quiet windows granted when the agent's text ends mid-sentence. Deepgram's
# punctuation is the tell: every fragment this agent produced ("Just", "Please
# provide", "I have your phone number as", "Can you please provide your date of")
# ended without terminal punctuation, while every complete question ended with one.
# Answering a fragment is what drove the persona to return nothing and fall back to
# "could you say that again?" — and, worse, to confidently answer a question it had
# only half heard.
#
# Raised from 2 because 2 x CATCHUP_QUIET_S was only 1.6s, and a live call still
# answered "Thank you, Maria. Can you please provide your date of" as though it were
# a whole question. With endpointing at 800ms these extra rounds should rarely fire
# at all, so the cost of being generous here is low.
UNFINISHED_WAITS = 3

# The window granted to each of those rounds. Deliberately NOT shrunk to
# CATCHUP_QUIET_S: a sentence that trailed off mid-word is the one case where the
# rest is genuinely still coming, and clipping the wait to 0.8s is what made the
# guard give up too early to help.
UNFINISHED_QUIET_S = _env_float("UNFINISHED_QUIET_S", 1.2)

# A floor on the pause between the agent finishing and the patient starting. Groq
# plus TTS usually covers this on its own, but a cached-fast turn can land the reply
# ~100ms after the agent stops, which sounds like an interruption even when the
# transcript is clean. This is the configurable "silence buffer" knob: raise it if
# the patient still sounds like it is cutting in.
MIN_GAP_BEFORE_SPEAK_S = _env_float("MIN_GAP_BEFORE_SPEAK_S", 0.25)

# How long Deepgram itself sat on silence before telling us an utterance ended. A
# `speech_final` has already cost `endpointing` ms and an `UtteranceEnd` has cost
# `utterance_end_ms`, so by the time a fragment reaches us the agent has ALREADY been
# listening to that much dead air.
#
# This exists because every latency number this project logged was wrong in the same
# direction. `agent_stopped_at` used to be stamped on arrival, so `reply_gap_s` began
# counting from the moment Deepgram spoke, not the moment the agent stopped, and the
# archive reported a 1.26s median gap for calls the caller actually experienced as
# ~2.8s (p90 3.25s, worst 3.75s). Backdating the timestamp by the lag that provably
# already elapsed makes `reply_gap_s` the real silence, and makes every budget
# measured against it -- MIN_GAP_BEFORE_SPEAK_S below, MAX_REPLY_DELAY_S above --
# mean what its comment claims.
DETECTION_LAG_S = {
    "speech_final": ENDPOINTING_MS / 1000.0,
    "UtteranceEnd": UTTERANCE_END_MS / 1000.0,
}

# Words a sentence does not end on unless the speaker was cut off. Deepgram's
# smart_format sometimes appends a period to a fragment, which defeats the
# punctuation check on its own ("Can you please provide your date of.").
DANGLING_WORDS = frozenset(
    """a an and as at but by for from in into is are was were of on or the to with
    your my our their his her its please just if so that this these those about
    over under between need needs needed want wants like would could should will
    can may might have has had do does did be been being""".split()
)

# Deepgram's SpeechStarted is a pure VAD event: it fires on audio energy before any
# words are transcribed, so on a PSTN line the echo of our own TTS trips it and we
# cut the patient off mid-sentence (observed: a line cancelled 6.5s before the agent
# actually said anything). A half-spoken request is worse than a late reply here —
# the agent never hears the full ask, so any resulting bug is ours, not theirs.
# Patient lines are only 1-3 sentences, so they always run to completion and agent
# speech is queued instead. Flip this to re-enable interrupting mid-line.
ALLOW_BARGE_IN = False

# How long to let the clinic greet us before speaking first. A real caller waits for
# the pickup rather than talking over the recording notice; without this the opening
# request lands while the agent is still on its own greeting and is simply lost.
OPENING_WAIT_S = 10.0

# The pickup is the one place a long silence is expected: the recording notice lands
# first and the real greeting follows well after it (observed 6.7s and ~8s later on
# separate calls), so the settle window has to outlast that gap.
#
# It used to be 10.0, which cost the opening 10s before synthesis even started. The
# agent re-greeted every time ("Hello. This is Pivot Point Orthopedics. How can I
# help you today?") because it had been waiting ~14s, and the patient then repeated
# its request in reply to the second greeting. The early exit below is what makes a
# shorter window safe: a clinic greeting ends by asking how it can help, so a
# question mark means the greeting is finished and there is nothing left to wait for.
# This value now only bounds the case where the greeting does NOT end in a question.
OPENING_SETTLE_S = _env_float("OPENING_SETTLE_S", 4.0)

# How many times a reply may be regenerated because the agent kept talking. One:
# each round costs another Groq call on top of the wait, and the deadline above
# usually bites before a second would help.
MAX_CATCHUP_ROUNDS = 1

# A recording notice is not a greeting. On a live call the notice arrived alone, the
# 4s settle expired, and the patient opened straight into the clinic's actual
# greeting a moment later — so the request was delivered over the top of it and the
# agent asked "How may I help you today?" as if nothing had been said. When the only
# thing heard so far is a notice, the settle window is served again to let the real
# greeting land.
NOTICE_PATTERN = re.compile(
    r"\b(recorded|recording|monitored|quality (?:and|&) training|training purposes)\b",
    re.IGNORECASE,
)

# How many extra settle windows a bare recording notice may buy. Bounded so a clinic
# that only ever plays a notice cannot keep the patient silent forever.
MAX_NOTICE_WAITS = 2


def _is_only_a_notice(text: str) -> bool:
    """True when everything heard so far is the call-recording disclaimer."""
    stripped = text.strip()
    if not stripped or not NOTICE_PATTERN.search(stripped):
        return False
    # A notice that runs into the greeting ("...training purposes. How may I help
    # you?") is a greeting, not a bare notice.
    return not stripped.endswith("?")


# Two agent turns this similar mean it repeated itself rather than moved on. Ratio
# rather than equality because the wording drifts slightly ("Thanks for calling Pivot
# Point Ortho part of Pretty Good AI" vs "This is Pivot Point Orthopedics").
AGENT_REPEAT_RATIO = 0.85

# Deferrals are counted for the timing log only — they are not a budget. See
# CallSession._honour_end_call for why there is no "honour it anyway" escape:
# `scenario.max_turns` is what guarantees the call terminates.

# Phrases that mean the patient is still waiting on the agent for something. Ending
# the call on one of these hangs up mid-request: observed live, the patient said
# "Sure, just let me know what slots you have available." and immediately hung up,
# before any appointment existed. The post-call analyser then filed a High-severity
# bug against the clinic for "failing to schedule", which was our fault, not theirs.
STILL_ASKING_PATTERN = re.compile(
    r"\b(let me know|could you|can you|would you|what (?:are|is|time|slots)|"
    r"do you have|are there|when (?:can|is|would)|please (?:send|tell|let|check)|"
    r"i'?d like to (?:hear|know)|any (?:openings|slots|availability))\b",
    re.IGNORECASE,
)


# An explicit sign-off. Accepting an offered time is not one of these, which is the
# whole point: the patient stays on the line until the agent confirms the booking.
CLOSING_PATTERN = re.compile(
    r"\b(goodbye|bye now|bye bye|good bye|thanks for your help|thank you for your help|"
    r"that'?s (?:everything|all|it)|nothing else|no,? that'?s all|all set then|"
    r"have a (?:good|great|nice) (?:day|one)|appreciate (?:your help|it)|take care)\b",
    re.IGNORECASE,
)


def _still_asking(text: str) -> bool:
    """True when the patient's line is still waiting on the agent for something."""
    stripped = text.strip()
    if not stripped:
        return False
    return stripped.endswith("?") or bool(STILL_ASKING_PATTERN.search(stripped))


def _is_a_closing_line(text: str) -> bool:
    """True when the patient's line actually signs off.

    A real caller does not hang up the instant they pick a time — they wait to hear
    the booking confirmed, then say goodbye. Requiring an explicit sign-off is a much
    better end-of-call signal than the absence of a question: observed live, the
    patient said "Yes, 2:30 PM on Tuesday, September 1 works for me." and hung up
    before the agent could confirm anything, and the analyser then filed a High
    against the agent for not confirming the booking.
    """
    return bool(CLOSING_PATTERN.search(text.strip()))


def _looks_repeated(text: str, history: list[dict]) -> bool:
    """True when the agent has already said essentially this, earlier in the call."""
    candidate = text.strip().lower()
    if not candidate:
        return False
    for turn in history:
        if turn.get("speaker") != "agent":
            continue
        previous = turn.get("text", "").strip().lower()
        if previous and SequenceMatcher(None, candidate, previous).ratio() >= AGENT_REPEAT_RATIO:
            return True
    return False


# Spoken when the persona has nothing usable to say, so the patient is never mute.
CLOSING_LINE = "Thanks so much for your help today, that's everything I needed. Goodbye!"

# When to start closing the conversation, in seconds from the first media frame.
# MAX_CALL_SECONDS in src/orchestrator.py is a hard Twilio cut at 180s; that would
# guillotine whatever is mid-sentence and leave the analyser reading a truncated
# transcript as an agent failure. Wrapping up at 150s leaves room for one closing
# line plus its TTS to actually play out before the line drops, so the call ends the
# same way a normal one does.
WRAP_UP_AT_S = _env_float("WRAP_UP_AT_S", 150.0)
REPEAT_LINE = "Sorry, could you say that again?"

# Saying one fallback over and over is its own bug: on a call where the agent looped,
# the patient asked it to repeat nine times. These escalate instead, and once they run
# out the call is wrapped up — the agent's loop is already captured in the transcript.
FALLBACK_LINES = (
    REPEAT_LINE,
    "Sorry, I'm having trouble following you. Could we go ahead and book that appointment?",
)


def _sounds_unfinished(text: str) -> bool:
    """True when the agent seems to have been cut off mid-sentence.

    Terminal punctuation is the primary tell — Deepgram punctuates a complete
    sentence and leaves a fragment bare. The dangling-word check is the backstop for
    when smart_format tacks a period onto a fragment anyway.

    That backstop is scoped to periods on purpose. smart_format invents a trailing
    "." on a fragment; it does not invent a "?" or "!", so those are strong evidence
    of a real end of turn — and plenty of genuine questions end on a function word
    ("a specific condition you'd like to check on?", "who am I speaking with?").
    Treating those as cut off cost a live call 3.6s of extra waits on one turn
    (5.6s total before the patient answered) for a question that was already
    complete.
    """
    stripped = text.strip()
    if not stripped:
        return False
    if stripped[-1] not in ".!?":
        return True
    if stripped[-1] != ".":
        return False
    last_word = "".join(c for c in stripped.lower() if c.isalpha() or c.isspace()).split()
    return bool(last_word) and last_word[-1] in DANGLING_WORDS


app = FastAPI()


class CallSession:
    def __init__(self, ws: WebSocket, scenario: Scenario, call_uid: str, stream_sid: str):
        self.ws = ws
        self.scenario = scenario
        self.call_uid = call_uid
        self.stream_sid = stream_sid
        self.logger = CallLogger(CALLS_DIR, call_uid, scenario.name)
        self.persona = PersonaEngine(settings.groq_api_key, settings.groq_model)
        self.tts = TtsSession(
            settings.deepgram_api_key,
            scenario.persona.voice,
            scenario.persona.speech_speed,
        )
        self.history: list[dict] = []
        self.bot_speaking = False
        self.speak_task: Optional[asyncio.Task] = None
        self.pending_marks: dict[str, asyncio.Event] = {}
        self.hangup_requested = False
        # Agent speech is buffered here and drained by conversation_loop, so a new
        # utterance never interrupts a patient line that is still being spoken.
        self.pending_agent: list[str] = []
        self.agent_speech = asyncio.Event()
        self.finished = asyncio.Event()
        self.empty_replies = 0  # consecutive; drives the fallback escalation
        self.agent_stopped_at = time.monotonic()
        # Set by _take_agent_turn: True when the text it returned still looks like a
        # sentence the agent never finished.
        self.last_turn_was_partial = False
        # Which Deepgram event finalised the most recent fragment. Decides how much
        # extra quiet the turn needs on top of what Deepgram already waited out.
        self.last_fragment_source = "speech_final"
        # Set once the scenario's closing probe has actually been delivered, so the
        # call is not allowed to end before it has been asked.
        self.closing_probe_asked = False
        # Wall clock for the whole call, used to close out before Twilio's hard cut.
        self.call_started_at = time.monotonic()
        self.end_call_deferrals = 0
        # Consecutive turns where the agent's line arrived cut off, so the patient's
        # request to repeat escalates instead of being the same sentence every time.
        self.partial_streak = 0
        # (draft text, task) for a reply being generated during the settle window.
        # See _start_prefetch.
        self._prefetch: Optional[tuple[str, asyncio.Task]] = None

    def log_timing(self, event: str, **fields) -> None:
        """Record one timing datapoint, to the log and to calls/<uid>/timing.json.

        Enough to reconstruct, after the fact, why a turn landed where it did:
        when the agent's speech was finalised, how long the quiet window actually
        waited, how long Groq and TTS each took, and the total gap the agent
        experienced between finishing its question and hearing an answer.
        """
        fields.setdefault("silence_so_far_s", round(self._silence_so_far(), 3))
        logger.info("timing %s %s", event, fields)
        self.logger.log_timing(event, **fields)

    async def _send(self, payload: dict) -> None:
        await self.ws.send_text(json.dumps(payload))

    async def send_media(self, mulaw_bytes: bytes) -> None:
        await self._send(
            {
                "event": "media",
                "streamSid": self.stream_sid,
                "media": {"payload": base64.b64encode(mulaw_bytes).decode("ascii")},
            }
        )

    async def send_mark(self, name: str) -> None:
        await self._send({"event": "mark", "streamSid": self.stream_sid, "mark": {"name": name}})

    async def send_clear(self) -> None:
        await self._send({"event": "clear", "streamSid": self.stream_sid})

    async def speak(self, text: str, end_call: bool) -> None:
        """Stream one line to Twilio, then wait for it to finish playing.

        Audio is forwarded as Aura produces it rather than after the whole clip is
        synthesised. That is what removes the seconds of dead air in front of every
        reply; `first_audio_s` in timing.json is the number to watch.
        """
        started = time.monotonic()
        first_audio_at: Optional[float] = None
        total_bytes = 0
        mark_name = f"{'end' if end_call else 'turn'}-{uuid.uuid4().hex[:8]}"
        mark_event = asyncio.Event()
        self.pending_marks[mark_name] = mark_event
        self.bot_speaking = True
        try:
            # Aura's chunks do not line up with MULAW_CHUNK_BYTES, so they are
            # coalesced here: one Twilio media frame per full chunk, remainder
            # carried forward and flushed at the end of the line.
            buffer = bytearray()
            async for audio_chunk in self.tts.stream(text):
                if first_audio_at is None:
                    first_audio_at = time.monotonic()
                total_bytes += len(audio_chunk)
                buffer.extend(audio_chunk)
                while len(buffer) >= MULAW_CHUNK_BYTES:
                    await self.send_media(bytes(buffer[:MULAW_CHUNK_BYTES]))
                    del buffer[:MULAW_CHUNK_BYTES]
            if buffer:
                await self.send_media(bytes(buffer))
            await self.send_mark(mark_name)
            self.log_timing(
                "tts_streamed",
                # Time to the FIRST byte of audio: what the agent experiences as the
                # length of the pause, now that playback no longer waits on the rest.
                first_audio_s=round((first_audio_at or time.monotonic()) - started, 3),
                total_s=round(time.monotonic() - started, 3),
                audio_s=round(total_bytes / MULAW_SAMPLE_RATE, 2),
            )

            if end_call:
                self.hangup_requested = True
            # The audio is buffered at Twilio, not played yet; the mark echo is what
            # tells us playback actually finished, so it also bounds bot_speaking.
            await mark_event.wait()
        finally:
            self.pending_marks.pop(mark_name, None)
            # A cancelled turn's cleanup can land after the next turn has already
            # started speaking; only the current turn owns the flag.
            if self.speak_task is asyncio.current_task():
                self.bot_speaking = False

    async def stop_speaking(self) -> None:
        """Cancel in-flight speech and flush whatever Twilio still has buffered."""
        if self.speak_task and not self.speak_task.done():
            self.speak_task.cancel()
        # Twilio drops the buffered audio on `clear`, so marks behind it never echo
        # back; release their waiters instead of leaving speak() hanging forever.
        for event in self.pending_marks.values():
            event.set()
        self.pending_marks.clear()
        self.bot_speaking = False
        await self.send_clear()

    async def say(self, text: str, end_call: bool) -> None:
        """Speak one patient line through to the end, then log what was really said."""
        if not text:
            return
        # Never start speaking right on top of the agent's last word. Groq plus TTS
        # normally covers this, but a fast turn can land ~100ms after the agent
        # stops, which sounds like an interruption however clean the transcript is.
        gap = MIN_GAP_BEFORE_SPEAK_S - self._silence_so_far()
        if gap > 0:
            self.log_timing("silence_buffer_held", held_s=round(gap, 3))
            await asyncio.sleep(gap)
        self.log_timing(
            "patient_speech_start",
            reply_gap_s=round(self._silence_so_far(), 3),
            chars=len(text),
            ends_call=end_call,
        )
        self.history.append({"speaker": "patient", "text": text})
        self.speak_task = asyncio.create_task(self.speak(text, end_call))
        # asyncio.wait() reports cancellation instead of re-raising it, so a barge-in
        # cancelling the speech does not tear down the conversation loop.
        try:
            await asyncio.wait({self.speak_task})
        except asyncio.CancelledError:
            # The call dropped mid-line and the loop is being torn down. Record it
            # anyway: this line really was spoken, and the transcript is the record.
            self.logger.log_turn("patient", f"{text} [call ended mid-line]")
            raise
        if self.speak_task.cancelled():
            # The agent talked over us: only the beginning of this line reached them,
            # so mark it rather than logging words they never heard.
            self.logger.log_turn("patient", f"{text} [cut off by agent]")
            return
        error = self.speak_task.exception()
        if error is not None:
            # Retrieved here so a TTS failure is reported instead of silently
            # leaving a line in the transcript that was never actually spoken.
            logger.error("TTS failed; line was not spoken: %r", text, exc_info=error)
            self.logger.log_turn("patient", f"{text} [not spoken: TTS failed]")
        else:
            self.logger.log_turn("patient", text)

    def note_agent_utterance(self, text: str, source: str = "speech_final") -> None:
        """Queue agent speech. Called from the STT reader, never blocks it.

        `source` is the Deepgram event that finalised this fragment — `speech_final`
        or `UtteranceEnd`. Interim results never reach here.
        """
        text = text.strip()
        if text:
            lag = DETECTION_LAG_S.get(source, UTTERANCE_END_MS / 1000.0)
            self.pending_agent.append(text)
            # Backdated by `lag`: the agent stopped talking that long ago, and every
            # downstream budget is about the silence IT hears, not about how promptly
            # Deepgram reported the silence.
            self.agent_stopped_at = time.monotonic() - lag
            self.last_fragment_source = source
            self.log_timing(
                "agent_fragment_final",
                source=source,
                detection_lag_s=lag,
                looks_unfinished=_sounds_unfinished(text),
                text=text,
            )
            self.agent_speech.set()

    def _default_quiet(self) -> float:
        """How long to keep listening after a fragment, given how it was finalised.

        `UtteranceEnd` already carries `utterance_end_ms` of observed silence, so it
        needs only a short confirmation window; `speech_final` has seen far less and
        keeps the full one.
        """
        if self.last_fragment_source == "UtteranceEnd":
            return AGENT_QUIET_AFTER_UTTERANCE_END_S
        return AGENT_QUIET_S

    async def _take_agent_turn(
        self, quiet_s: Optional[float] = None, until_question: bool = False,
        prefetch: bool = False,
    ) -> str:
        """Wait for agent speech and merge the fragments of a single thought.

        Sets `self.last_turn_was_partial` so the caller knows whether the text it
        gets back is a whole question or a sentence the agent never finished. That
        distinction is what stops the persona from confidently answering a question
        it only half heard.
        """
        explicit_quiet = quiet_s
        await self.agent_speech.wait()
        if prefetch:
            # Start thinking about the answer now, while the window below decides
            # whether the agent has actually finished. Only the main turn loop asks
            # for this: the opening and catch-up paths are not on the hot path.
            self._start_prefetch()
        base_quiet = self._default_quiet() if explicit_quiet is None else explicit_quiet
        quiet_s = base_quiet
        started = time.monotonic()
        extra_waits = 0
        while True:
            if until_question and " ".join(self.pending_agent).strip().endswith("?"):
                # A clinic greeting ends by asking how it can help. Once that lands
                # there is nothing left to wait for, and every further second is a
                # second the agent spends wondering if anyone is there.
                break
            self.agent_speech.clear()
            if explicit_quiet is None and extra_waits == 0:
                quiet_s = self._default_quiet()
            try:
                await asyncio.wait_for(self.agent_speech.wait(), quiet_s)
                continue  # the agent carried on, keep collecting
            except asyncio.TimeoutError:
                pass
            if _sounds_unfinished(" ".join(self.pending_agent)) and extra_waits < UNFINISHED_WAITS:
                # Mid-sentence: wait for the rest rather than answering half a
                # question, which the persona cannot do and answers with silence —
                # or, worse, with a guess at what was being asked.
                extra_waits += 1
                quiet_s = UNFINISHED_QUIET_S
                continue
            break
        fragment_count = len(self.pending_agent)
        merged = " ".join(self.pending_agent).strip()
        self.pending_agent.clear()
        # Still unfinished after every extra round: the agent has genuinely stopped
        # mid-sentence (or the STT lost the tail). The reply has to acknowledge that
        # rather than pretend a whole question arrived.
        self.last_turn_was_partial = bool(merged) and _sounds_unfinished(merged)
        self.log_timing(
            "agent_turn_taken",
            waited_s=round(time.monotonic() - started, 3),
            quiet_window_s=quiet_s,
            finalised_by=self.last_fragment_source,
            extra_unfinished_waits=extra_waits,
            fragments=fragment_count,
            partial=self.last_turn_was_partial,
            text=merged,
        )
        if self.last_turn_was_partial:
            logger.warning("Agent utterance still looks cut off after %d extra waits: %r",
                           extra_waits, merged)
        return merged

    def _silence_so_far(self) -> float:
        return time.monotonic() - self.agent_stopped_at

    def _record_agent_turn(self, text: str) -> None:
        self.history.append({"speaker": "agent", "text": text})
        self.logger.log_turn("agent", text)

    async def _catch_up(self, reply: str, should_end: bool) -> tuple[str, bool]:
        """Re-answer if the agent carried on talking while we were preparing a reply.

        Groq plus TTS costs a second or two, and the agent often starts its next
        question inside that window. Speaking the old reply then lands as an answer
        to a question it has already moved past — and talks over the new one.
        """
        for _ in range(MAX_CATCHUP_ROUNDS):
            if not self.pending_agent:
                break
            if self._silence_so_far() >= MAX_REPLY_DELAY_S:
                # Already on the edge of sounding like a dropped line.
                logger.info("Skipping catch-up; %.1fs of silence already", self._silence_so_far())
                break
            extra = await self._take_agent_turn(CATCHUP_QUIET_S)
            if not extra:
                break
            partial = self.last_turn_was_partial
            repeated = _looks_repeated(extra, self.history)
            self._record_agent_turn(extra)
            reply, should_end = await self._reply_to(extra, partial=partial, repeated=repeated)
        return reply, should_end

    def _closing_probe_due(self) -> bool:
        """True once enough patient turns have passed to ask the late question.

        Counts the patient's own turns, not total history, so a chatty agent does
        not bring the probe forward.
        """
        probe = getattr(self.scenario, "closing_probe", None)
        if not probe or self.closing_probe_asked:
            return False
        said = sum(1 for t in self.history if t.get("speaker") == "patient")
        return said >= self.scenario.closing_probe_after_turns

    def _honour_end_call(self, reply: str, should_end: bool) -> bool:
        """Veto an end-of-call that hangs up while the patient is still asking.

        The model ends the call by emitting a token, and it sometimes emits it on a
        line that is nowhere near the end of the conversation. Two live examples,
        both of which made the analyser blame the clinic for our hangup:

        * "Sure, just let me know what slots you have available." -> hung up with no
          appointment booked, reported as "Failed to schedule appointment" (High).
        * "Yes, 2:30 PM on Tuesday, September 1 works for me." -> hung up the instant
          a time was accepted, reported as "Missing booking confirmation" (High).

        The second is why an explicit sign-off is required rather than merely the
        absence of a question: a real caller waits to hear the booking confirmed.

        There is deliberately no "give up and honour it anyway" escape. There used to
        be one, bounded at MAX_END_CALL_DEFERRALS, and it failed exactly as you would
        expect once a model started over-emitting the token: gpt-oss-120b asked to end
        on 3 of 4 turns, burned both deferrals on the first two, and the third hung up
        in the middle of rescheduling. `scenario.max_turns` is the real termination
        guarantee — _reply_to() returns CLOSING_LINE there, which IS a sign-off and so
        ends the call through the normal path.
        """
        if not should_end:
            return False
        if not reply.strip():
            # Nothing to judge yet. The empty-reply path below substitutes CLOSING_LINE,
            # which is itself a proper sign-off, so leave that decision to it.
            return True
        reason = ""
        if _still_asking(reply):
            reason = "the patient is still asking for something"
        elif not _is_a_closing_line(reply):
            reason = "the patient has not actually said goodbye"
        if not reason:
            return True
        self.end_call_deferrals += 1
        logger.warning("Ignoring end-of-call: %s (%r)", reason, reply)
        self.log_timing(
            "end_call_deferred",
            deferrals=self.end_call_deferrals,
            reason=reason,
            reply=reply,
        )
        return False

    def _start_prefetch(self) -> None:
        """Begin generating a reply to what the agent has said SO FAR.

        The settle window and the Groq call used to run one after the other, so every
        turn paid both: Deepgram's detection lag, then the window, and only then ~0.7s
        of inference before a single byte of audio existed. Running the two together
        takes the whole Groq call off the critical path in the case that actually
        happens — `fragments: 1`, i.e. the agent said its piece in one go, which is
        every logged turn in the archive.

        Deliberately side-effect free: it calls the persona directly rather than going
        through `_reply_to`, so a draft that turns out to be stale can simply be
        dropped. Nothing is logged, no counter moves, and no prompt reaches
        prompts.json until the result is actually claimed and spoken.

        Skipped when the draft looks unfinished — that is precisely the case where
        more text IS still coming, so generating against it would be wasted work
        against a half-question.
        """
        draft = " ".join(self.pending_agent).strip()
        if not draft or _sounds_unfinished(draft):
            return
        repeated = _looks_repeated(draft, self.history)
        coro = self.persona.next_line(
            self.scenario,
            list(self.history),
            draft,
            partial=False,
            repeated=repeated,
            partial_attempt=1,
            closing_due=self._closing_probe_due(),
            probe_asked=self.closing_probe_asked,
        )
        self._prefetch = (draft, asyncio.create_task(coro))

    async def _claim_prefetch(self, text: str, partial: bool, repeated: bool):
        """Return the prefetched generation if it was made against this exact text.

        Anything else — the agent carried on, the turn came back partial, the draft
        never started — is discarded. A discarded task is cancelled rather than left
        running, so it cannot land a stray Groq response into a later turn.
        """
        prefetch, self._prefetch = self._prefetch, None
        if prefetch is None:
            return None
        draft, task = prefetch
        stale = partial or draft != text
        if stale:
            task.cancel()
            self.log_timing("prefetch_discarded", draft=draft, actual=text, partial=partial)
            return None
        try:
            result = await task
        except asyncio.CancelledError:
            return None
        # Any other failure is deliberately allowed to propagate. A prefetch is the
        # turn's one generation attempt, not a free extra one: swallowing the error
        # here and regenerating would quietly turn every Groq failure into two calls
        # and bypass the REPEAT_LINE fallback that _reply_to's handler exists to give.
        self.log_timing("prefetch_used", chars=len(text))
        return result

    async def _reply_to(
        self, text: str, partial: bool = False, repeated: bool = False
    ) -> tuple[str, bool]:
        if len(self.history) >= self.scenario.max_turns * 2:
            return CLOSING_LINE, True
        elapsed = time.monotonic() - self.call_started_at
        if elapsed >= WRAP_UP_AT_S:
            # Out of time: say goodbye now rather than start a turn that Twilio will
            # cut mid-word at MAX_CALL_SECONDS.
            logger.warning(
                "Wrapping up: %.1fs elapsed, past WRAP_UP_AT_S=%.0fs", elapsed, WRAP_UP_AT_S
            )
            self.log_timing("wrap_up_deadline", elapsed_s=round(elapsed, 1))
            return CLOSING_LINE, True
        self.partial_streak = self.partial_streak + 1 if partial else 0
        started = time.monotonic()
        try:
            # history[:-1] is the whole call in order, minus the utterance being
            # replied to — that one is passed separately as `text`. Nothing is
            # truncated and nothing is dropped, so the persona always sees every
            # fact it has already given. A prefetched generation was made against
            # exactly this text and exactly this history, so it is the same call —
            # just one that started while the settle window was still running.
            prefetched = await self._claim_prefetch(text, partial, repeated)
            if prefetched is not None:
                reply, should_end, messages = prefetched
            else:
                reply, should_end, messages = await self.persona.next_line(
                    self.scenario,
                    self.history[:-1],
                    text,
                    partial=partial,
                    repeated=repeated,
                    partial_attempt=max(self.partial_streak, 1),
                    closing_due=self._closing_probe_due(),
                    probe_asked=self.closing_probe_asked,
                )
            if self._closing_probe_due():
                self.closing_probe_asked = True
            should_end = self._honour_end_call(reply, should_end)
            self.log_timing(
                "llm_reply",
                elapsed_s=round(time.monotonic() - started, 3),
                history_turns=len(self.history) - 1,
                partial_question=partial,
                agent_repeated=repeated,
                partial_streak=self.partial_streak,
                empty=not reply,
                ends_call=should_end,
            )
            self.logger.log_prompt("turn", messages, reply)
        except Exception:
            # One bad turn (a Groq hiccup, a timeout) must not leave the patient
            # mute for the rest of the call.
            logger.exception("Persona turn failed; asking the agent to repeat")
            self.log_timing(
                "llm_reply_failed", elapsed_s=round(time.monotonic() - started, 3)
            )
            return REPEAT_LINE, False

        if reply:
            self.empty_replies = 0
            return reply, should_end

        # The model sometimes returns nothing at all (9 of 15 turns on one observed
        # call, some of them a bare end-call token). Staying silent makes the agent
        # repeat itself and eventually hang up, which reads as an agent bug when it
        # is really ours, so always say something.
        logger.warning("Persona returned an empty reply; using a fallback line")
        if should_end or self.empty_replies >= len(FALLBACK_LINES):
            reply, should_end = CLOSING_LINE, True
        else:
            reply = FALLBACK_LINES[self.empty_replies]
        self.empty_replies += 1
        return reply, should_end

    async def conversation_loop(self) -> None:
        greeting = None
        try:
            await asyncio.wait_for(self.agent_speech.wait(), OPENING_WAIT_S)
            greeting = await self._take_agent_turn(OPENING_SETTLE_S, until_question=True)
            for _ in range(MAX_NOTICE_WAITS):
                if not _is_only_a_notice(greeting):
                    break
                logger.info("Heard only the recording notice; waiting for the greeting")
                more = await self._take_agent_turn(OPENING_SETTLE_S, until_question=True)
                if not more:
                    break
                greeting = f"{greeting} {more}".strip()
            if greeting:
                self._record_agent_turn(greeting)
        except asyncio.TimeoutError:
            pass  # nobody greeted us; open the conversation ourselves

        # The notice and the real greeting arrive as separate utterances, so the
        # clinic is often still talking when the settle window expires.
        for _ in range(MAX_CATCHUP_ROUNDS):
            if not self.pending_agent:
                break
            extra = await self._take_agent_turn()
            if not extra:
                break
            self._record_agent_turn(extra)
            greeting = f"{greeting} {extra}".strip() if greeting else extra

        await self.opening_line(greeting)
        while not self.hangup_requested:
            text = await self._take_agent_turn(prefetch=True)
            if not text:
                continue
            partial = self.last_turn_was_partial
            repeated = _looks_repeated(text, self.history)
            self._record_agent_turn(text)
            reply, should_end = await self._reply_to(text, partial=partial, repeated=repeated)
            reply, should_end = await self._catch_up(reply, should_end)
            await self.say(reply, should_end)
        self.finished.set()

    async def opening_line(self, greeting: Optional[str] = None) -> None:
        if self.scenario.opening_line:
            await self.say(self.scenario.opening_line, end_call=False)
            return
        text, should_end, messages = await self.persona.next_line(
            self.scenario,
            [],
            greeting
            or "(The call just connected. Greet the clinic and state why you're calling.)",
        )
        self.logger.log_prompt("opening_line", messages, text)
        await self.say(text, should_end)

    async def handle_barge_in(self) -> None:
        if self.bot_speaking and self.speak_task and not self.speak_task.done():
            await self.stop_speaking()


def _log_loop_failure(task: asyncio.Task) -> None:
    if not task.cancelled() and task.exception() is not None:
        logger.error("Conversation loop crashed", exc_info=task.exception())


@app.websocket("/media-stream")
async def media_stream(websocket: WebSocket) -> None:
    await websocket.accept()
    settings.require("deepgram_api_key", "groq_api_key")

    twilio_client = None
    if settings.twilio_account_sid and settings.twilio_auth_token:
        twilio_client = TwilioClient(settings.twilio_account_sid, settings.twilio_auth_token)

    session: Optional[CallSession] = None
    call_sid: Optional[str] = None
    loop_task: Optional[asyncio.Task] = None

    async with stt_connection(settings.deepgram_api_key) as stt_socket:

        async def stt_reader() -> None:
            utterance_parts: list[str] = []
            async for message in stt_socket:
                if isinstance(message, ListenV1SpeechStarted):
                    if session and ALLOW_BARGE_IN:
                        await session.handle_barge_in()
                elif isinstance(message, ListenV1Results):
                    alternatives = message.channel.alternatives if message.channel else []
                    transcript = alternatives[0].transcript if alternatives else ""
                    if transcript and message.is_final:
                        utterance_parts.append(transcript)
                    if message.speech_final and utterance_parts and session:
                        full = " ".join(utterance_parts).strip()
                        utterance_parts.clear()
                        if full:
                            session.note_agent_utterance(full, source="speech_final")
                elif isinstance(message, ListenV1UtteranceEnd):
                    if utterance_parts and session:
                        full = " ".join(utterance_parts).strip()
                        utterance_parts.clear()
                        if full:
                            session.note_agent_utterance(full, source="UtteranceEnd")

        reader_task = asyncio.create_task(stt_reader())

        try:
            while True:
                raw = await websocket.receive_text()
                data = json.loads(raw)
                event = data.get("event")

                if event == "start":
                    start = data["start"]
                    stream_sid = start["streamSid"]
                    call_sid = start.get("callSid")
                    params = start.get("customParameters", {}) or {}
                    scenario_name = params.get("scenario")
                    call_uid = params.get("call_uid") or f"unknown_{uuid.uuid4().hex[:8]}"
                    if not scenario_name:
                        logger.error("No scenario parameter for call %s; closing.", call_uid)
                        break
                    scenario = find_scenario(scenario_name)
                    session = CallSession(websocket, scenario, call_uid, stream_sid)
                    # Must run detached: the loop awaits Twilio's `mark` echo, which
                    # only this receive loop can deliver.
                    loop_task = asyncio.create_task(session.conversation_loop())
                    # A detached task keeps its exception to itself; without this the
                    # patient just goes quiet for the rest of the call and the server
                    # still looks healthy.
                    loop_task.add_done_callback(_log_loop_failure)

                elif event == "media":
                    if session:
                        payload = data["media"]["payload"]
                        await stt_socket.send_media(base64.b64decode(payload))

                elif event == "mark":
                    if session:
                        name = data.get("mark", {}).get("name")
                        mark_event = session.pending_marks.pop(name, None)
                        if mark_event:
                            mark_event.set()
                        if session.hangup_requested and name and name.startswith("end-"):
                            if twilio_client and call_sid:
                                try:
                                    twilio_client.calls(call_sid).update(status="completed")
                                except Exception:
                                    logger.exception("Failed to hang up call %s", call_sid)
                            break

                elif event == "stop":
                    break

        except WebSocketDisconnect:
            pass
        finally:
            reader_task.cancel()
            if session:
                await session.tts.aclose()
            if loop_task:
                loop_task.cancel()
            if session and session.speak_task:
                session.speak_task.cancel()
