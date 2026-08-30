"""Shared fakes for the media-bridge tests.

The bridge is exercised against a stand-in for Twilio rather than a live call, so
turn-taking regressions are caught without spending a phone call. The fake mimics
the two behaviours the bridge actually depends on: it buffers outbound audio, and
it only echoes a `mark` back once that buffered audio has "played".
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src import media_bridge as mb  # noqa: E402
from src.scenario import Persona, Scenario  # noqa: E402

# Tests run with a much shorter quiet window than production so the suite stays
# fast; test_turn_taking asserts separately that the shipped value is sane.
TEST_QUIET_S = 0.15
CHUNKS_PER_LINE = 10


class FakeTwilio:
    """Stands in for the Twilio Media Streams websocket."""

    def __init__(self, session_ref, playback_s: float = 0.05):
        self.session_ref = session_ref
        self.playback_s = playback_s
        self.events: list[str] = []
        self.audio_chunks_per_turn: list[int] = []
        self.clears = 0
        self._buffered = 0

    async def send_text(self, raw: str) -> None:
        message = json.loads(raw)
        event = message["event"]
        self.events.append(event)
        if event == "media":
            self._buffered += 1
        elif event == "clear":
            # Twilio drops whatever is still buffered, so those chunks never play.
            self.clears += 1
            self._buffered = 0
        elif event == "mark":
            buffered, self._buffered = self._buffered, 0
            asyncio.create_task(self._echo_after_playback(message["mark"]["name"], buffered))

    async def _echo_after_playback(self, name: str, chunks: int) -> None:
        await asyncio.sleep(self.playback_s)
        session = self.session_ref[0]
        event = session.pending_marks.pop(name, None)
        if event:
            self.audio_chunks_per_turn.append(chunks)
            event.set()


class FakePersona:
    """Deterministic stand-in for the Groq-backed persona engine."""

    def __init__(self, *_args, **_kwargs):
        self.seen: list[str | None] = []
        self.partials: list[bool] = []
        self.repeats: list[bool] = []
        self.partial_attempts: list[int] = []
        self.closing_dues: list[bool] = []

    async def next_line(self, scenario, history, latest=None, partial=False, repeated=False, partial_attempt=1, closing_due=False, probe_asked=False):
        self.seen.append(latest)
        self.partials.append(partial)
        self.repeats.append(repeated)
        self.partial_attempts.append(partial_attempt)
        self.closing_dues.append(closing_due)
        return f"reply to <{latest}>", False, [{"role": "user", "content": latest or ""}]


class FakeTts:
    """Stands in for TtsSession: streams a fixed number of chunks per line.

    Streaming rather than returning one buffer, because that is now the contract
    speak() depends on — it forwards each chunk to Twilio as it arrives.
    """

    def __init__(self, *_args, **_kwargs):
        self.lines: list[str] = []
        self.closed = False

    async def stream(self, text):
        self.lines.append(text)
        for _ in range(CHUNKS_PER_LINE):
            await asyncio.sleep(0.001)
            yield b"\xff" * mb.MULAW_CHUNK_BYTES

    async def aclose(self):
        self.closed = True


@pytest.fixture
def quiet_s() -> float:
    return TEST_QUIET_S


@pytest.fixture
def make_session(monkeypatch, tmp_path):
    """Build a CallSession wired to the fake Twilio socket and persona."""

    def _make(opening_line: str | None = "Hello, this is the opening line.", playback_s=0.05):
        monkeypatch.setattr(mb, "CALLS_DIR", tmp_path)
        monkeypatch.setattr(mb, "AGENT_QUIET_S", TEST_QUIET_S)
        monkeypatch.setattr(mb, "OPENING_SETTLE_S", TEST_QUIET_S)
        monkeypatch.setattr(mb, "UNFINISHED_QUIET_S", TEST_QUIET_S)
        # The shipped silence buffer would dominate a suite running at 0.15s windows;
        # test_shipped_turn_taking_values_are_sane guards the real value instead.
        monkeypatch.setattr(mb, "MIN_GAP_BEFORE_SPEAK_S", 0.0)
        monkeypatch.setattr(mb, "TtsSession", FakeTts)
        monkeypatch.setattr(mb, "PersonaEngine", FakePersona)
        scenario = Scenario(
            name="test_scenario",
            persona=Persona(identity="a test patient"),
            goal="book an appointment",
            intended_outcome="an appointment is booked",
            opening_line=opening_line,
            max_turns=50,
        )
        session_ref: list = []
        ws = FakeTwilio(session_ref, playback_s=playback_s)
        session = mb.CallSession(ws, scenario, "testcall", "STREAMSID")
        session_ref.append(session)
        return session, ws

    return _make


@pytest.fixture
def run_loop():
    """Run conversation_loop in the background and always tear it down."""
    tasks: list[asyncio.Task] = []

    def _start(session):
        task = asyncio.create_task(session.conversation_loop())
        tasks.append(task)
        return task

    yield _start

    for task in tasks:
        task.cancel()


def patient_lines(session) -> list[str]:
    return [t["text"] for t in session.logger.turns if t["speaker"] == "patient"]


def agent_lines(session) -> list[str]:
    return [t["text"] for t in session.logger.turns if t["speaker"] == "agent"]
