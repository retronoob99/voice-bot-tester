"""Report integrity: a lost analysis costs a whole call, and a misattributed bug is worse."""

from __future__ import annotations

from src import bug_analyzer as ba
from src.scenario import Persona, Scenario

_SCENARIO = Scenario(
    name="t",
    persona=Persona(identity="a test patient"),
    goal="book an appointment",
    intended_outcome="an appointment is booked",
)


def _turns(*pairs: tuple[str, str]) -> list[dict]:
    return [{"speaker": speaker, "text": text} for speaker, text in pairs]


def test_parses_a_bare_array():
    assert ba._parse_bugs('[{"bug": "x"}]') == [{"bug": "x"}]
    assert ba._parse_bugs("[]") == []


def test_parses_through_fences_and_prose():
    """The model wraps the array in fences, in prose, or in both."""
    assert ba._parse_bugs('```json\n[{"bug": "y"}]\n```') == [{"bug": "y"}]
    assert ba._parse_bugs('Here is what I found:\n[{"bug": "z"}]\nHope that helps.') == [
        {"bug": "z"}
    ]


def test_unusable_output_is_reported_as_unusable():
    """None means "retry", and must not be confused with "no bugs found" ([])."""
    for raw in ("", "   ", "No bugs found.", "[broken", '{"bug": "not a list"}'):
        assert ba._parse_bugs(raw) is None, raw


def test_truncated_agent_turns_are_extracted_with_what_followed():
    turns = _turns(
        ("agent", "Can you tell me your date of"),
        ("patient", "Sorry, could you repeat that?"),
        ("agent", "Just to con"),
        ("patient", "Sorry, I didn't catch the end of that."),
        ("agent", "I have your name as Maria Chen. Is that correct?"),
    )
    found = ba._truncated_agent_turns(turns)
    assert len(found) == 2, found
    assert 'broke off at: "Can you tell me your date of"' in found[0]
    # The line that followed is what shows the agent abandoned the thought.
    assert 'next AGENT line: "Just to con"' in found[0]
    assert "I have your name as Maria Chen" in found[1]


def test_complete_agent_turns_are_not_flagged():
    turns = _turns(
        ("agent", "What is your date of birth?"),
        ("patient", "March 12, 1991."),
        ("agent", "Thank you, you're all set."),
    )
    assert ba._truncated_agent_turns(turns) == []


def test_the_evidence_reaches_the_prompt():
    turns = _turns(("agent", "Just to con"), ("agent", "You're all set."))
    prompt = ba._build_prompt(_SCENARIO, turns)
    assert "Turn-taking evidence" in prompt
    assert 'broke off at: "Just to con"' in prompt


def test_no_evidence_section_when_there_is_nothing_to_report():
    turns = _turns(("agent", "You're all set."))
    assert "Turn-taking evidence" not in ba._build_prompt(_SCENARIO, turns)


def test_the_rubric_protects_attribution():
    """Harness markers must never be filed as agent bugs — they are our own."""
    assert "[not spoken: TTS failed]" in ba.RUBRIC
    assert "NEVER be reported as agent bugs" in ba.RUBRIC
    assert "Do not report the agent for anything the patient did." in ba.RUBRIC


def test_a_promise_to_go_and_check_is_collected_as_evidence():
    """"Let me find available slots" and then never naming one is the bug.

    Verbatim from a live simple_schedule call: the agent said it would find afternoon
    slots for next week, named none, and pivoted to an appointment the caller had not
    asked about. The analyser read straight past it, so the two lines are now put side
    by side for it the way truncated turns already were.
    """
    turns = [
        {"speaker": "patient", "text": "Any afternoon next week?"},
        {"speaker": "agent", "text": "Let me find available afternoon slots for next week."},
        {"speaker": "agent", "text": "Your chart shows an office visit on Tuesday, September 1."},
    ]
    evidence = ba._unfulfilled_promises(turns)
    assert len(evidence) == 1, evidence
    assert "Let me find available afternoon slots" in evidence[0]
    # The line that followed has to travel with it, or the promise cannot be judged.
    assert "Your chart shows an office visit" in evidence[0]

    # An agent that says it will check and then answers is not flagged by wording alone.
    assert ba._unfulfilled_promises([{"speaker": "agent", "text": "Tuesday works."}]) == []


def test_the_evidence_blocks_reach_the_prompt():
    """Computed evidence is worthless if it never gets sent."""
    from src.scenario import Persona, Scenario

    scenario = Scenario(
        name="s", persona=Persona(identity="i"), goal="g", intended_outcome="o"
    )
    turns = [
        {"speaker": "agent", "text": "Let me check the schedule for you."},
        {"speaker": "agent", "text": "Can I take your date of birth?"},
    ]
    prompt = ba._build_prompt(scenario, turns)
    assert "said it would go and check" in prompt


def test_the_rubric_does_not_treat_a_declined_request_as_a_bug():
    """An agent holding a line it was right to hold is not a defect.

    A live simple_schedule call had the agent say it could not access the record and
    route the caller to staff. That was correct behaviour, and it was filed as a High
    "scheduling logic error ... refusing to schedule despite patient saying 'No,
    please just schedule it now'" - the patient's insistence read as severity. The
    rubric has to separate "did not do what the patient wanted" from "was dishonest
    or inconsistent about what it could do".
    """
    assert "Declining is not failing." in ba.RUBRIC
    assert "never whether it did what the patient wanted" in ba.RUBRIC
    # The carve-out must not become a blanket excuse: claiming a thing was done when
    # it was not is still a bug, and is the failure mode that actually matters.
    assert "claiming an action was completed when the transcript shows it was not" in ba.RUBRIC


def test_the_rubric_asks_about_talking_over_the_caller():
    assert "Talking over the caller" in ba.RUBRIC


def test_the_analysis_budget_fits_a_free_tier_request():
    """At 8192 a single request was rejected: "Limit 8000, Requested 9028"."""
    # Prompt plus reserved output must leave room inside an 8000-token minute,
    # with headroom for a retry in the same window.
    assert ba.ANALYSIS_MAX_TOKENS <= 4000, ba.ANALYSIS_MAX_TOKENS
    assert ba.RATE_LIMIT_BACKOFF_S >= 10, "an immediate retry just hits the same limit"


async def test_a_failed_analysis_never_loses_a_finished_call(tmp_path, monkeypatch):
    """The recording and transcript are already on disk; an analysis error must not
    take them down with it, or a real clinic gets re-dialled to recover them."""
    import json as _json
    from src import orchestrator

    call_uid = "scenario_test"
    call_dir = tmp_path / call_uid
    call_dir.mkdir()
    (call_dir / "transcript.json").write_text(
        _json.dumps({"turns": [{"speaker": "agent", "text": "hello"}]}), encoding="utf-8"
    )
    monkeypatch.setattr(orchestrator, "CALLS_DIR", tmp_path)
    monkeypatch.setattr(orchestrator, "REPORTS_DIR", tmp_path / "reports")
    # Settings is a frozen dataclass, so the whole object is swapped rather than
    # patched field by field.
    stub = type("S", (), {"groq_api_key": "k", "groq_model": "m", "require": lambda *_a: None})()
    monkeypatch.setattr(orchestrator, "settings", stub)
    monkeypatch.setattr(orchestrator, "find_scenario", lambda _n: _SCENARIO)

    async def boom(*_a, **_k):
        raise RuntimeError("429 rate_limit_exceeded")

    monkeypatch.setattr(orchestrator.bug_analyzer, "analyze_call", boom)

    await orchestrator._run_bug_analysis("t", call_uid)  # must not raise

    bugs = _json.loads((call_dir / "bugs.json").read_text(encoding="utf-8"))
    assert bugs[0]["bug"].startswith("HARNESS:")
    assert "429" in bugs[0]["details"]
    assert (tmp_path / "reports" / "bug_report.md").exists()


def test_no_source_file_contains_a_stray_backspace():
    """A regex written as "\b" in a shell heredoc silently became a literal backspace
    (0x08) three separate times, and each time the pattern matched nothing. Escapes
    are easy to get wrong and impossible to see; this catches it."""
    import pathlib

    src = pathlib.Path(__file__).resolve().parents[1] / "src"
    offenders = [
        p.name for p in src.glob("*.py") if "\x08" in p.read_text(encoding="utf-8")
    ]
    assert offenders == [], f"literal backspace in: {offenders}"
