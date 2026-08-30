"""Turn-taking regressions.

These cover the failure the tester actually hit on live calls: the patient's audio
was cut off mid-sentence while the transcript still showed the whole line, so the
target agent was being blamed for mishearing a request it never fully received.
"""

from __future__ import annotations

import asyncio

from conftest import CHUNKS_PER_LINE, agent_lines, patient_lines

from src import media_bridge as mb


async def test_shipped_quiet_window_is_sane():
    """The patient's tempo dial has to sit between 'interrupts' and 'looks hung up'.

    Below ~0.5s the patient answers half a question; the target agent starts asking
    "Are you still there?" around 9s, and a reply costs a Groq call plus TTS on top.
    """
    assert 0.5 <= mb.AGENT_QUIET_S <= 6.0


async def test_closing_probe_is_hidden_until_it_is_due():
    """A late question must not be visible to the persona early, or it front-loads it.

    On a live context_retention call the probe lived in `goal`, so the model saw it on
    every turn and asked it in turn 3 - in the same breath as stating the allergy
    ("I have a penicillin allergy. Which allergy do you have on record for me?").
    Nothing was ever tested across distance. The probe text must therefore be absent
    from the prompt entirely until the turn threshold is reached.
    """
    from src.persona_engine import PersonaEngine
    from src.scenario import Persona, Scenario

    persona = Persona(identity="x", voice_style="y", voice="aura-2-asteria-en")
    scenario = Scenario(
        name="probe", persona=persona, goal="g", intended_outcome="o",
        closing_probe="Which allergy do you have on record for me?",
        closing_probe_after_turns=4,
    )
    engine = PersonaEngine.__new__(PersonaEngine)
    history = [{"speaker": "patient", "text": "a"}, {"speaker": "agent", "text": "b"}]

    early = engine.build_messages(scenario, history, "hi", closing_due=False)
    assert not any("Which allergy" in m["content"] for m in early)

    due = engine.build_messages(scenario, history, "hi", closing_due=True)
    assert any("Which allergy" in m["content"] for m in due)

    # A scenario without a probe must be completely unaffected.
    plain = Scenario(name="p", persona=persona, goal="g", intended_outcome="o")
    assert "more question you must ask" not in engine._system_prompt(plain)


async def test_complete_question_ending_on_a_function_word_is_not_treated_as_cut_off():
    """A "?" is never invented by smart_format, so it outranks the dangling-word check.

    A live call spent 3.6s of extra "unfinished" waits — 5.6s before the patient
    answered at all — on "Or a specific condition you'd like to check on?", which was
    already a complete question. The backstop only applies to a trailing period.
    """
    assert not mb._sounds_unfinished(
        "Is this for a recent visit? Or a specific condition you'd like to check on?"
    )
    assert not mb._sounds_unfinished("Who am I speaking with?")
    # The period case it exists for still has to be caught.
    assert mb._sounds_unfinished("Can you please provide your date of.")
    assert mb._sounds_unfinished("I have your phone number as")


async def test_waits_for_the_agent_to_stop_talking(make_session, run_loop, quiet_s):
    """A fragment must not trigger a reply while the agent is still speaking."""
    session, _ws = make_session()
    run_loop(session)

    session.note_agent_utterance("Just to confirm, is your date of birth")
    await asyncio.sleep(quiet_s * 0.4)  # still inside the quiet window
    assert patient_lines(session) == [], "patient answered a half-finished question"

    session.note_agent_utterance("the twelfth of March?")
    await asyncio.sleep(quiet_s + 0.5)

    assert agent_lines(session) == [
        "Just to confirm, is your date of birth the twelfth of March?"
    ]


async def test_waits_for_greeting_before_speaking(make_session, run_loop, quiet_s):
    """Opening over the clinic's recording notice wastes the request entirely."""
    session, _ws = make_session()
    run_loop(session)

    await asyncio.sleep(quiet_s + 0.3)
    assert patient_lines(session) == [], "patient talked over the greeting"

    session.note_agent_utterance("Thanks for calling the clinic.")
    await asyncio.sleep(quiet_s + 0.5)

    assert patient_lines(session) == ["Hello, this is the opening line."]


async def test_waits_out_the_gap_between_notice_and_greeting(
    make_session, run_loop, monkeypatch, quiet_s
):
    """The recording notice arrives first and the real greeting follows seconds later.

    Observed live 6.7s apart: replying after the notice meant talking straight over
    the greeting, so the opening request was lost.
    """
    session, _ws = make_session()
    monkeypatch.setattr(mb, "OPENING_SETTLE_S", quiet_s * 4)  # after: the fixture patches it too
    run_loop(session)

    session.note_agent_utterance("This call may be recorded.")
    await asyncio.sleep(quiet_s * 2)
    assert patient_lines(session) == [], "patient spoke between notice and greeting"

    session.note_agent_utterance("Thanks for calling. How may I help you?")
    await asyncio.sleep(quiet_s * 4 + 0.6)

    assert agent_lines(session) == [
        "This call may be recorded. Thanks for calling. How may I help you?"
    ]
    assert patient_lines(session) == ["Hello, this is the opening line."]


async def test_a_dropped_call_still_records_the_spoken_line(make_session, quiet_s):
    """Hard rule #2: the transcript is the record, even if the far end hangs up."""
    session, _ws = make_session(playback_s=5.0)  # never finishes playing
    task = asyncio.create_task(session.conversation_loop())
    session.note_agent_utterance("Thanks for calling.")
    await asyncio.sleep(mb.OPENING_SETTLE_S + 0.3)
    assert session.bot_speaking, "setup: the patient should be mid-line here"

    task.cancel()  # the websocket closed under us
    await asyncio.sleep(0.1)

    assert any("call ended mid-line" in line for line in patient_lines(session)), (
        patient_lines(session)
    )


async def test_opens_the_call_if_nobody_greets(make_session, run_loop, monkeypatch):
    """If the far end stays silent the patient must still start the conversation."""
    monkeypatch.setattr(mb, "OPENING_WAIT_S", 0.3)
    session, _ws = make_session()
    run_loop(session)

    await asyncio.sleep(1.0)

    assert patient_lines(session) == ["Hello, this is the opening line."]


async def test_stt_fragments_become_one_turn(make_session, run_loop, quiet_s):
    """Deepgram splits one sentence into several finals; that is still one turn."""
    session, _ws = make_session()
    run_loop(session)
    session.note_agent_utterance("Thanks for calling.")
    await asyncio.sleep(quiet_s + 0.5)

    session.note_agent_utterance("Hi, Maria.")
    await asyncio.sleep(quiet_s * 0.3)
    session.note_agent_utterance("How can I help")
    await asyncio.sleep(quiet_s * 0.3)
    session.note_agent_utterance("you today?")
    await asyncio.sleep(quiet_s + 0.6)

    assert agent_lines(session) == [
        "Thanks for calling.",
        "Hi, Maria. How can I help you today?",
    ]


async def test_patient_is_never_cut_off_by_agent_chatter(make_session, run_loop, quiet_s):
    """The core regression: every line must reach the agent in full."""
    session, ws = make_session()
    run_loop(session)
    session.note_agent_utterance("Thanks for calling.")
    await asyncio.sleep(quiet_s + 0.5)

    for i in range(5):
        session.note_agent_utterance(f"chatter {i}")
        await asyncio.sleep(quiet_s * 0.4)
    await asyncio.sleep(quiet_s + 1.0)

    truncated = [line for line in patient_lines(session) if "cut off" in line]
    assert not truncated, f"patient was cut off: {truncated}"
    assert ws.audio_chunks_per_turn, "no audio was ever delivered"
    assert all(n == CHUNKS_PER_LINE for n in ws.audio_chunks_per_turn), (
        f"a line was only partly delivered: {ws.audio_chunks_per_turn}"
    )


async def test_transcript_never_claims_unspoken_words(make_session, run_loop, quiet_s):
    """A cut-off line must be marked, not logged as if the agent heard it."""
    session, ws = make_session(playback_s=2.0)  # hold playback open mid-line
    run_loop(session)
    session.note_agent_utterance("Thanks for calling.")
    await asyncio.sleep(quiet_s + 0.2)
    assert session.bot_speaking, "setup: the patient should still be mid-line here"

    await session.handle_barge_in()
    await asyncio.sleep(0.3)

    assert ws.clears >= 1, "buffered audio was not flushed at Twilio"
    assert any("cut off by agent" in line for line in patient_lines(session))


async def test_vad_barge_in_stays_disabled():
    """SpeechStarted is pure VAD and fires on our own echo, cutting the patient off.

    Observed live: a line was cancelled 6.5s before the agent said anything at all.
    """
    assert mb.ALLOW_BARGE_IN is False


async def test_a_failed_turn_does_not_mute_the_patient(make_session, run_loop, quiet_s):
    """One Groq hiccup must not leave the patient silent for the rest of the call."""
    session, _ws = make_session()

    calls = {"n": 0}

    async def flaky_next_line(scenario, history, latest=None, partial=False, repeated=False, partial_attempt=1, **_kwargs):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("groq exploded")
        return "recovered reply", False, [{"role": "user", "content": latest or ""}]

    session.persona.next_line = flaky_next_line
    run_loop(session)

    session.note_agent_utterance("Thanks for calling.")  # consumed as the greeting
    await asyncio.sleep(quiet_s + 0.5)
    session.note_agent_utterance("How can I help?")  # this turn is the one that fails
    await asyncio.sleep(quiet_s + 0.6)
    session.note_agent_utterance("Are you still there?")  # next turn must work again
    await asyncio.sleep(quiet_s + 0.6)

    lines = patient_lines(session)
    assert "Sorry, could you say that again?" in lines, lines
    assert "recovered reply" in lines, lines


async def test_an_empty_persona_reply_never_leaves_the_patient_mute(
    make_session, run_loop, quiet_s
):
    """Groq returned nothing on 4 of 9 turns of one call; the agent then repeats itself.

    Silence reads as an agent bug when it is really ours, so a fallback is spoken.
    """
    session, _ws = make_session()

    async def empty_next_line(scenario, history, latest=None, partial=False, repeated=False, partial_attempt=1, **_kwargs):
        return "", False, [{"role": "user", "content": latest or ""}]

    session.persona.next_line = empty_next_line
    run_loop(session)

    session.note_agent_utterance("Thanks for calling.")
    await asyncio.sleep(quiet_s + 0.5)
    session.note_agent_utterance("Am I speaking with Maria?")
    await asyncio.sleep(quiet_s + 0.6)

    assert mb.REPEAT_LINE in patient_lines(session), patient_lines(session)


async def test_fallbacks_escalate_instead_of_parroting(make_session, run_loop, quiet_s):
    """A stuck agent drew the same fallback nine times on a live call.

    Each empty reply must move on, and once the fallbacks run out the call ends
    rather than filling the transcript with the patient asking it to repeat.
    """
    session, _ws = make_session()

    async def always_empty(scenario, history, latest=None, partial=False, repeated=False, partial_attempt=1, **_kwargs):
        return "", False, [{"role": "user", "content": latest or ""}]

    session.persona.next_line = always_empty
    run_loop(session)

    session.note_agent_utterance("Thanks for calling.")
    await asyncio.sleep(quiet_s + 0.5)
    for i in range(4):
        session.note_agent_utterance(f"You already have a visit booked. ({i})")
        await asyncio.sleep(quiet_s + 0.6)

    spoken = [line for line in patient_lines(session) if line != "Hello, this is the opening line."]
    assert spoken[: len(mb.FALLBACK_LINES)] == list(mb.FALLBACK_LINES), spoken
    assert spoken[len(mb.FALLBACK_LINES)] == mb.CLOSING_LINE, spoken
    assert session.hangup_requested, "a stuck call should be wrapped up"
    assert spoken.count(mb.REPEAT_LINE) == 1, f"parroted the same line: {spoken}"


async def test_a_good_reply_resets_the_fallback_escalation(make_session, run_loop, quiet_s):
    """One blank turn mid-call must not push the call toward hanging up."""
    session, _ws = make_session()
    replies = ["", "I'd like the afternoon slot.", ""]

    async def flaky(scenario, history, latest=None, partial=False, repeated=False, partial_attempt=1, **_kwargs):
        return replies.pop(0), False, [{"role": "user", "content": latest or ""}]

    session.persona.next_line = flaky
    run_loop(session)

    session.note_agent_utterance("Thanks for calling.")
    await asyncio.sleep(quiet_s + 0.5)
    for i in range(3):
        session.note_agent_utterance(f"Question {i}?")
        await asyncio.sleep(quiet_s + 0.6)

    assert not session.hangup_requested, "an isolated blank turn should not end the call"
    assert patient_lines(session).count(mb.REPEAT_LINE) == 2, patient_lines(session)


async def test_an_empty_reply_that_ends_the_call_says_goodbye(make_session, run_loop, quiet_s):
    """A bare end-call token strips to nothing; the patient should still sign off."""
    session, _ws = make_session()

    async def end_with_no_words(scenario, history, latest=None, partial=False, repeated=False, partial_attempt=1, **_kwargs):
        return "", True, [{"role": "user", "content": latest or ""}]

    session.persona.next_line = end_with_no_words
    run_loop(session)

    session.note_agent_utterance("Thanks for calling.")
    await asyncio.sleep(quiet_s + 0.5)
    session.note_agent_utterance("Anything else?")
    await asyncio.sleep(quiet_s + 0.6)

    assert mb.CLOSING_LINE in patient_lines(session), patient_lines(session)
    assert session.hangup_requested, "the call should be ending"


async def test_tts_failure_is_flagged_in_the_transcript(make_session, run_loop, quiet_s):
    """A line that never reached the phone must not read as spoken."""
    session, _ws = make_session()

    async def broken_stream(_text):
        raise RuntimeError("deepgram down")
        yield b""  # pragma: no cover - makes this an async generator

    # TTS is per-session now, so the failure is injected on the session's own
    # connection rather than on a module-level function.
    session.tts.stream = broken_stream
    run_loop(session)

    session.note_agent_utterance("Thanks for calling.")
    await asyncio.sleep(quiet_s + 0.5)

    assert any("not spoken: TTS failed" in line for line in patient_lines(session))


async def test_answers_the_latest_question_not_a_stale_one(
    make_session, run_loop, quiet_s, monkeypatch
):
    """The agent often asks its next question while Groq and TTS are still working.

    Observed live: the patient's line landed as the agent's greeting was still
    playing, so the answer arrived before the question it belonged to.
    """
    # Catch-up is the path taken when a reply is generated AFTER the settle window
    # closed: a discarded prefetch, or the agent speaking during commit/TTS. These
    # fakes model "the agent gets a word in while we are thinking" by injecting at
    # persona-call time, which with prefetching lands INSIDE the window instead —
    # where it is merged into the same turn (covered separately below). Pinning
    # prefetch off here keeps this test on the ordering it was written to guard.
    monkeypatch.setattr(mb.CallSession, "_start_prefetch", lambda self: None)
    session, _ws = make_session()
    asked: list[str] = []

    async def slow_persona(scenario, history, latest=None, partial=False, repeated=False, partial_attempt=1, **_kwargs):
        asked.append(latest)
        # The agent gets a word in while we are "thinking".
        if latest == "Am I speaking with Maria?":
            session.note_agent_utterance("Actually, what is your date of birth?")
        await asyncio.sleep(quiet_s * 0.3)
        return f"answer to <{latest}>", False, [{"role": "user", "content": latest or ""}]

    session.persona.next_line = slow_persona
    run_loop(session)

    session.note_agent_utterance("Thanks for calling.")
    await asyncio.sleep(quiet_s + 0.5)
    session.note_agent_utterance("Am I speaking with Maria?")
    await asyncio.sleep(quiet_s * 3 + 1.0)

    spoken = patient_lines(session)
    assert "answer to <Actually, what is your date of birth?>" in spoken, spoken
    assert "answer to <Am I speaking with Maria?>" not in spoken, (
        f"spoke a reply the agent had already moved past: {spoken}"
    )


async def test_catch_up_is_bounded(make_session, run_loop, quiet_s, monkeypatch):
    """A continuously talking agent must not keep the patient silent forever."""
    # Catch-up is the path taken when a reply is generated AFTER the settle window
    # closed: a discarded prefetch, or the agent speaking during commit/TTS. These
    # fakes model "the agent gets a word in while we are thinking" by injecting at
    # persona-call time, which with prefetching lands INSIDE the window instead —
    # where it is merged into the same turn (covered separately below). Pinning
    # prefetch off here keeps this test on the ordering it was written to guard.
    monkeypatch.setattr(mb.CallSession, "_start_prefetch", lambda self: None)
    session, _ws = make_session()
    rounds = {"n": 0}

    async def never_quiet(scenario, history, latest=None, partial=False, repeated=False, partial_attempt=1, **_kwargs):
        rounds["n"] += 1
        session.note_agent_utterance(f"And another thing {rounds['n']}.")
        return f"reply {rounds['n']}", False, [{"role": "user", "content": latest or ""}]

    session.persona.next_line = never_quiet
    run_loop(session)

    session.note_agent_utterance("Thanks for calling.")
    await asyncio.sleep(quiet_s + 0.5)
    session.note_agent_utterance("First question?")
    await asyncio.sleep(quiet_s * 6 + 1.5)

    spoken = [line for line in patient_lines(session) if line.startswith("reply ")]
    assert spoken, "the patient was starved and never spoke"
    # One initial generation plus at most MAX_CATCHUP_ROUNDS re-answers before
    # the patient stops waiting for the agent and speaks.
    assert int(spoken[0].split()[1]) <= mb.MAX_CATCHUP_ROUNDS + 1, spoken


async def test_the_reply_is_generated_during_the_settle_window_not_after_it(
    make_session, run_loop, quiet_s
):
    """The Groq call must overlap the window, not queue behind it.

    Serialised, every turn paid Deepgram's detection lag, then the settle window,
    then ~0.7s of inference before any audio existed — the "4 second wait" heard on
    the recording. The persona must therefore be called while the window is still
    open, and the result reused rather than regenerated once it closes.
    """
    session, _ws = make_session()
    called_at: list[float] = []

    async def persona(scenario, history, latest=None, partial=False, repeated=False, partial_attempt=1, **_kwargs):
        called_at.append(asyncio.get_running_loop().time())
        return f"answer to <{latest}>", False, [{"role": "user", "content": latest or ""}]

    session.persona.next_line = persona
    run_loop(session)
    session.note_agent_utterance("Thanks for calling.")
    await asyncio.sleep(quiet_s + 0.5)

    called_at.clear()
    fragment_at = asyncio.get_running_loop().time()
    session.note_agent_utterance("What is your date of birth?")
    await asyncio.sleep(quiet_s + 0.8)

    assert len(called_at) == 1, f"generated {len(called_at)} times for one turn"
    assert called_at[0] - fragment_at < quiet_s, (
        "the persona was called after the settle window closed, not during it"
    )
    assert "answer to <What is your date of birth?>" in patient_lines(session)
    assert any(e["event"] == "prefetch_used" for e in session.logger.timings)


async def test_a_fragment_arriving_inside_the_window_is_merged_not_answered_twice(
    make_session, run_loop, quiet_s
):
    """A draft that the agent then adds to must be thrown away, not spoken.

    Prefetching means a reply now exists before the turn is final. If the agent adds
    a second fragment inside the window, that reply answers half of what was said —
    exactly the half-question failure the settle window exists to prevent — so it has
    to be discarded and regenerated against the merged text.
    """
    session, _ws = make_session()
    asked: list[str] = []

    async def persona(scenario, history, latest=None, partial=False, repeated=False, partial_attempt=1, **_kwargs):
        asked.append(latest)
        return f"answer to <{latest}>", False, [{"role": "user", "content": latest or ""}]

    session.persona.next_line = persona
    run_loop(session)
    session.note_agent_utterance("Thanks for calling.")
    await asyncio.sleep(quiet_s + 0.5)

    session.note_agent_utterance("Am I speaking with Maria?")
    await asyncio.sleep(quiet_s * 0.4)                       # inside the window
    session.note_agent_utterance("And what is your date of birth?")
    await asyncio.sleep(quiet_s + 0.8)

    merged = "Am I speaking with Maria? And what is your date of birth?"
    spoken = patient_lines(session)
    assert f"answer to <{merged}>" in spoken, spoken
    assert "answer to <Am I speaking with Maria?>" not in spoken, (
        f"spoke a reply to half the turn: {spoken}"
    )
    assert any(e["event"] == "prefetch_discarded" for e in session.logger.timings)


async def test_a_discarded_prefetch_never_reaches_the_prompt_log(
    make_session, run_loop, quiet_s
):
    """prompts.json is the record of what drove the call (hard rule #5).

    A speculative generation that was thrown away drove nothing. Logging it would put
    a prompt and a reply in the record that the agent never heard, which is the same
    class of dishonesty as a transcript claiming unspoken words.
    """
    session, _ws = make_session()

    async def persona(scenario, history, latest=None, partial=False, repeated=False, partial_attempt=1, **_kwargs):
        return f"answer to <{latest}>", False, [{"role": "user", "content": latest or ""}]

    session.persona.next_line = persona
    run_loop(session)
    session.note_agent_utterance("Thanks for calling.")
    await asyncio.sleep(quiet_s + 0.5)

    session.note_agent_utterance("Am I speaking with Maria?")
    await asyncio.sleep(quiet_s * 0.4)
    session.note_agent_utterance("And what is your date of birth?")
    await asyncio.sleep(quiet_s + 0.8)

    logged = [p["response"] for p in session.logger.prompts]
    assert "answer to <Am I speaking with Maria?>" not in logged, logged


async def test_reply_latency_budget_fits_the_agents_patience():
    """The agent re-asks after ~9s of silence, which reads as the patient going mute.

    Measured on a live call at AGENT_QUIET_S=3.0: 7-10.6s of dead air every turn,
    because a catch-up round paid the full quiet window a second time.
    """
    groq_and_tts = 2.5  # generous allowance for one Groq call plus synthesis
    typical = mb.AGENT_QUIET_S + groq_and_tts
    worst = mb.AGENT_QUIET_S + groq_and_tts + mb.CATCHUP_QUIET_S + groq_and_tts
    assert typical <= 5.0, f"typical reply delay {typical:.1f}s"
    assert worst <= 9.0, f"worst-case reply delay {worst:.1f}s reaches the re-ask point"
    assert mb.CATCHUP_QUIET_S < mb.AGENT_QUIET_S, "catch-up must be cheaper than a full turn"


async def test_catch_up_is_skipped_once_the_silence_budget_is_spent(
    make_session, run_loop, quiet_s, monkeypatch
):
    """Answering a slightly stale question beats sounding like a dropped line."""
    # Catch-up is the path taken when a reply is generated AFTER the settle window
    # closed: a discarded prefetch, or the agent speaking during commit/TTS. These
    # fakes model "the agent gets a word in while we are thinking" by injecting at
    # persona-call time, which with prefetching lands INSIDE the window instead —
    # where it is merged into the same turn (covered separately below). Pinning
    # prefetch off here keeps this test on the ordering it was written to guard.
    monkeypatch.setattr(mb.CallSession, "_start_prefetch", lambda self: None)
    session, _ws = make_session()
    monkeypatch.setattr(mb, "MAX_REPLY_DELAY_S", 0.0)  # budget already blown
    generated: list[str] = []

    async def persona(scenario, history, latest=None, partial=False, repeated=False, partial_attempt=1, **_kwargs):
        generated.append(latest)
        session.note_agent_utterance("and one more thing")
        return f"reply to <{latest}>", False, [{"role": "user", "content": latest or ""}]

    session.persona.next_line = persona
    run_loop(session)

    session.note_agent_utterance("Thanks for calling.")
    await asyncio.sleep(quiet_s + 0.5)
    session.note_agent_utterance("Your date of birth?")
    await asyncio.sleep(quiet_s + 0.8)

    # Without the budget guard the first thing spoken would answer "and one more
    # thing" instead — the catch-up would have regenerated before speaking.
    spoken = [line for line in patient_lines(session) if line.startswith("reply to")]
    assert spoken[0] == "reply to <Your date of birth?>", spoken
    assert generated[0] == "Your date of birth?", generated


def test_unfinished_detection_uses_terminal_punctuation():
    """Verbatim fragments and complete questions from one live call."""
    for fragment in ("Just", "Please provide", "I have your phone number as",
                     "Can you please confirm if your first name is Maria? If so, I'll just need your"):
        assert mb._sounds_unfinished(fragment), fragment
    for complete in ("Am I speaking with Maria?", "what is your date of birth?",
                     "Thanks for calling Pivot Point.", "Transferring you now."):
        assert not mb._sounds_unfinished(complete), complete
    assert not mb._sounds_unfinished("")


async def test_waits_for_a_cut_off_question_to_finish(make_session, run_loop, quiet_s):
    """Replying to "Please provide" is what produced the empty turns and fallbacks."""
    session, _ws = make_session()
    asked: list[str] = []

    async def persona(scenario, history, latest=None, partial=False, repeated=False, partial_attempt=1, **_kwargs):
        asked.append(latest)
        return f"answer to <{latest}>", False, [{"role": "user", "content": latest or ""}]

    session.persona.next_line = persona
    run_loop(session)
    session.note_agent_utterance("Thanks for calling.")
    await asyncio.sleep(quiet_s + 0.5)

    session.note_agent_utterance("Please provide")  # cut off mid-sentence
    await asyncio.sleep(quiet_s * 1.6)              # past a normal window
    assert asked == [], f"answered a fragment: {asked}"

    session.note_agent_utterance("your date of birth.")
    await asyncio.sleep(quiet_s + 0.8)

    assert asked == ["Please provide your date of birth."], asked


async def test_an_unfinished_utterance_does_not_stall_forever(make_session, run_loop, quiet_s):
    """If the agent never finishes the sentence, answer it rather than going mute."""
    session, _ws = make_session()
    run_loop(session)
    session.note_agent_utterance("Thanks for calling.")
    await asyncio.sleep(quiet_s + 0.5)

    session.note_agent_utterance("Just")
    await asyncio.sleep(quiet_s * (mb.UNFINISHED_WAITS + 2) + 1.0)

    assert "Just" in agent_lines(session), agent_lines(session)
    assert len(patient_lines(session)) >= 2, patient_lines(session)


async def test_shipped_turn_taking_values_are_sane():
    """The tuning knobs the tests patch down still have to be sensible as shipped."""
    from src import stt

    # UtteranceEnd is the end-of-turn backstop, so it must not preempt the primary
    # endpoint detector. It used to be required to clear endpointing by 400ms as well,
    # on the theory that a close backstop fires mid-sentence — but `endpointing` is
    # what decides where an utterance is SPLIT; the backstop only decides how long we
    # wait for a split Deepgram never made. The margin bought latency, not safety, so
    # what is pinned now is the ordering and Deepgram's own documented floor.
    assert stt.UTTERANCE_END_MS > stt.ENDPOINTING_MS, (
        stt.ENDPOINTING_MS,
        stt.UTTERANCE_END_MS,
    )
    assert stt.UTTERANCE_END_MS >= stt.DEEPGRAM_MIN_UTTERANCE_END_MS, stt.UTTERANCE_END_MS
    # The safety net that replaced that margin: a sentence split early must still get
    # collected rather than answered half-heard.
    assert mb.UNFINISHED_WAITS >= 2 and mb.UNFINISHED_QUIET_S >= 1.0
    # Below ~500ms Deepgram finalises on an ordinary breath.
    assert 500 <= stt.ENDPOINTING_MS <= 1500, stt.ENDPOINTING_MS

    # A cut-off sentence gets a longer wait than a catch-up round: the rest of it is
    # genuinely still coming, whereas catch-up is just draining a known backlog.
    assert mb.UNFINISHED_QUIET_S > mb.CATCHUP_QUIET_S, "unfinished waits must not be clipped"
    # Long enough to be an audible pause, short enough not to read as dead air.
    assert 0.1 <= mb.MIN_GAP_BEFORE_SPEAK_S <= 1.0, mb.MIN_GAP_BEFORE_SPEAK_S
    # The full unfinished budget still has to fit inside the agent's patience.
    assert mb.UNFINISHED_WAITS * mb.UNFINISHED_QUIET_S <= 5.0


async def test_a_cut_off_question_is_flagged_to_the_persona(make_session, run_loop, quiet_s):
    """Half a question must never be answered as though it were a whole one.

    Verbatim from a live call: "Thank you, Maria. Can you please provide your date
    of" was answered with a date of birth, and the agent then read back a mangled
    confirmation. The persona has to be told it did not hear the whole thing.
    """
    session, ws = make_session()
    run_loop(session)
    session.note_agent_utterance("Thanks for calling.")
    await asyncio.sleep(quiet_s + 0.5)

    session.note_agent_utterance("Can you please provide your date of")
    # Outlast every extra window, so the guard gives up and answers anyway.
    await asyncio.sleep(quiet_s * (mb.UNFINISHED_WAITS + 2) + 0.8)

    assert session.persona.partials[-1] is True, session.persona.partials
    assert session.persona.seen[-1] == "Can you please provide your date of"


async def test_a_complete_question_is_not_flagged_as_partial(make_session, run_loop, quiet_s):
    session, _ws = make_session()
    run_loop(session)
    session.note_agent_utterance("Thanks for calling.")
    await asyncio.sleep(quiet_s + 0.5)

    session.note_agent_utterance("What is your date of birth?")
    await asyncio.sleep(quiet_s + 0.8)

    assert session.persona.seen[-1] == "What is your date of birth?"
    assert session.persona.partials[-1] is False, session.persona.partials


def test_unfinished_detection_catches_a_punctuated_fragment():
    """smart_format sometimes puts a period on a fragment, defeating punctuation alone."""
    for fragment in ("Can you please provide your date of.",
                     "I have your phone number as.",
                     "Just."):
        assert mb._sounds_unfinished(fragment), fragment
    for complete in ("Thanks for calling Pivot Point.", "Am I speaking with Maria?",
                     "Transferring you now.", "Your appointment is confirmed for Tuesday."):
        assert not mb._sounds_unfinished(complete), complete


async def test_the_patient_never_speaks_straight_over_the_agents_last_word(
    make_session, run_loop, monkeypatch, quiet_s
):
    """A fast turn must still leave an audible gap before the patient starts.

    The floor is measured on the honest clock: `agent_stopped_at` is backdated by the
    silence Deepgram already sat through, so this knob only holds a reply when the
    agent's REAL silence is still under it. That makes it inert at its shipped 0.25s
    (every fragment arrives with >= 0.8s of real silence banked) and is why the value
    here has to clear the detection lag to be exercised at all.
    """
    session, _ws = make_session(opening_line=None)
    # After make_session: the fixture zeroes this knob itself for the rest of the suite.
    hold = mb.DETECTION_LAG_S["speech_final"] + quiet_s * 4
    monkeypatch.setattr(mb, "MIN_GAP_BEFORE_SPEAK_S", hold)

    async def instant(scenario, history, latest=None, partial=False, repeated=False, partial_attempt=1, **_kwargs):
        return "an instant reply", False, [{"role": "user", "content": latest or ""}]

    session.persona.next_line = instant
    run_loop(session)

    session.note_agent_utterance("What is your date of birth?")
    await asyncio.sleep(quiet_s + 0.3)  # window elapsed, buffer should still be held
    assert patient_lines(session) == [], patient_lines(session)

    await asyncio.sleep(quiet_s * 4 + 0.5)
    assert patient_lines(session) == ["an instant reply"], patient_lines(session)


async def test_silence_is_measured_from_when_the_agent_stopped_not_when_deepgram_spoke():
    """Every latency figure this project logged was wrong in the same direction.

    A fragment only reaches us after Deepgram has sat on `endpointing` (speech_final)
    or `utterance_end_ms` (UtteranceEnd) of silence. Stamping `agent_stopped_at` on
    arrival therefore under-reported the gap the CALLER hears by exactly that much:
    the archive's 1.26s median `reply_gap_s` was a real ~2.8s. The lag must stay tied
    to the STT config rather than hardcoded, or the two drift apart silently again.
    """
    from src import stt

    assert mb.DETECTION_LAG_S["speech_final"] == stt.ENDPOINTING_MS / 1000.0
    assert mb.DETECTION_LAG_S["UtteranceEnd"] == stt.UTTERANCE_END_MS / 1000.0


async def test_the_agent_hears_a_reply_within_about_two_seconds():
    """The end-to-end budget, in the terms the person on the phone experiences it.

    Reply gap = Deepgram's detection lag + our quiet window + one Groq call + time to
    first TTS audio. Measured medians across the call archive supply the last two.
    UtteranceEnd is the path that matters: it finalised 56% of all archived turns and
    7 of 9 on simple_schedule, and at the old 1500ms/0.5s settings that path cost
    ~3.1s, which is the "4 second wait" heard on the recording.
    """
    from src import stt

    groq_median_s, tts_first_audio_median_s = 0.61, 0.28

    # Groq runs INSIDE the settle window (see _start_prefetch), so a turn costs
    # whichever of the two is longer, not their sum.
    def budget(detection_s: float, window_s: float) -> float:
        return detection_s + max(window_s, groq_median_s) + tts_first_audio_median_s

    utterance_end = budget(stt.UTTERANCE_END_MS / 1000.0, mb.AGENT_QUIET_AFTER_UTTERANCE_END_S)
    speech_final = budget(stt.ENDPOINTING_MS / 1000.0, mb.AGENT_QUIET_S)

    assert utterance_end <= 2.0, f"UtteranceEnd path {utterance_end:.2f}s"
    assert speech_final <= 2.0, f"speech_final path {speech_final:.2f}s"


async def test_timing_is_recorded_for_every_turn(make_session, run_loop, quiet_s):
    """Without these a slow turn can only be guessed at after the fact."""
    session, _ws = make_session()
    run_loop(session)
    session.note_agent_utterance("Thanks for calling.")  # consumed as the greeting
    await asyncio.sleep(quiet_s + 0.5)
    session.note_agent_utterance("What is your date of birth?")
    await asyncio.sleep(quiet_s + 1.0)

    events = [t["event"] for t in session.logger.timings]
    for expected in ("agent_fragment_final", "agent_turn_taken", "llm_reply",
                     "patient_speech_start", "tts_streamed"):
        assert expected in events, (expected, events)

    finalised = next(t for t in session.logger.timings if t["event"] == "agent_fragment_final")
    assert finalised["source"] == "speech_final"
    spoke = next(t for t in session.logger.timings if t["event"] == "patient_speech_start")
    # The gap the agent actually experienced between finishing and being answered.
    assert spoke["reply_gap_s"] >= quiet_s
    assert (session.logger.dir / "timing.json").exists()


# --- Streaming TTS: audio must reach Twilio before the whole clip is synthesised ---


async def test_audio_reaches_twilio_before_synthesis_finishes(
    make_session, run_loop, quiet_s, monkeypatch
):
    """The old code buffered the whole clip first, which cost 2.3-5.9s per line.

    Measured on a live call: TTS accounted for 20.0s of a 74.5s call. Streaming is
    the fix, so the test asserts the property that made it slow — no media frame
    until the last chunk existed — is gone.
    """
    session, ws = make_session(opening_line="A line to speak.")
    monkeypatch.setattr(mb, "OPENING_WAIT_S", 0.2)  # nobody greets us in this test
    media_seen_during_synthesis = asyncio.Event()

    async def slow_stream(text):
        for index in range(4):
            yield b"\xff" * mb.MULAW_CHUNK_BYTES
            if index == 0:
                # Give the bridge a chance to forward the first chunk while the
                # rest of the clip is still being produced.
                await asyncio.sleep(0.05)
                if "media" in ws.events:
                    media_seen_during_synthesis.set()

    session.tts.stream = slow_stream
    run_loop(session)
    await asyncio.sleep(quiet_s + 0.8)

    assert media_seen_during_synthesis.is_set(), "audio was buffered instead of streamed"


async def test_the_tts_connection_is_closed_when_the_call_ends(make_session):
    """One socket is held for the whole call, so something has to close it."""
    session, _ws = make_session()
    assert session.tts.closed is False
    await session.tts.aclose()
    assert session.tts.closed is True


# --- Opening: don't leave the agent waiting long enough to re-greet ---


async def test_the_opening_does_not_wait_once_the_greeting_asks_a_question(
    make_session, run_loop, monkeypatch, quiet_s
):
    """A 10s settle made the clinic re-greet, and the patient then repeated itself.

    A clinic greeting ends by asking how it can help, so the question mark means
    there is nothing left to wait for.
    """
    session, _ws = make_session(opening_line="Hi, I'd like to book a checkup.")
    monkeypatch.setattr(mb, "OPENING_SETTLE_S", 30.0)  # would hang without the early exit
    run_loop(session)

    session.note_agent_utterance("This call may be recorded for quality purposes.")
    await asyncio.sleep(quiet_s * 0.4)
    session.note_agent_utterance("Thanks for calling Pivot Point. How may I help you today?")
    await asyncio.sleep(quiet_s + 1.0)

    assert patient_lines(session) == ["Hi, I'd like to book a checkup."], patient_lines(session)
    # Both fragments still merged into the one greeting turn.
    assert len(agent_lines(session)) == 1, agent_lines(session)
    assert "How may I help you today?" in agent_lines(session)[0]


async def test_a_greeting_without_a_question_still_gets_the_full_settle(
    make_session, run_loop, monkeypatch, quiet_s
):
    """The early exit must not shorten the wait for an agent that trails off."""
    session, _ws = make_session(opening_line="Hi there.")
    monkeypatch.setattr(mb, "OPENING_SETTLE_S", quiet_s * 6)
    run_loop(session)

    session.note_agent_utterance("Thanks for calling Pivot Point Orthopedics.")
    await asyncio.sleep(quiet_s * 2)
    assert patient_lines(session) == [], "opened before the settle window elapsed"

    await asyncio.sleep(quiet_s * 6 + 0.6)
    assert patient_lines(session) == ["Hi there."], patient_lines(session)


# --- The agent repeating itself must not make the patient repeat itself ---


def test_repeat_detection_tolerates_reworded_greetings():
    history = [
        {"speaker": "agent", "text": "Thanks for calling Pivot Point Ortho. How may I help you today?"},
        {"speaker": "patient", "text": "I'd like a checkup."},
    ]
    assert mb._looks_repeated("Thanks for calling Pivot Point Ortho. How may I help you today?", history)
    assert mb._looks_repeated("Thanks for calling Pivot Point Ortho. How can I help you today?", history)
    assert not mb._looks_repeated("Can you please provide your date of birth?", history)
    assert not mb._looks_repeated("", history)


async def test_a_repeated_agent_line_is_flagged_to_the_persona(make_session, run_loop, quiet_s):
    session, _ws = make_session()
    run_loop(session)

    session.note_agent_utterance("Thanks for calling. How may I help you today?")
    await asyncio.sleep(quiet_s + 0.6)
    session.note_agent_utterance("Thanks for calling. How can I help you today?")
    await asyncio.sleep(quiet_s + 0.8)

    assert session.persona.partials[-1] is False
    assert True in session.persona.repeats, session.persona.repeats


# --- Premature hangup ---


def test_still_asking_detects_an_open_request():
    for line in ("Sure, just let me know what slots you have available.",
                 "What times do you have next week?",
                 "Could you check Tuesday afternoon?",
                 "Do you have anything after 2pm?"):
        assert mb._still_asking(line), line
    for line in ("Thanks so much, that's everything I needed. Goodbye!",
                 "Great, Tuesday at 3pm works.",
                 "March 12, 1991."):
        assert not mb._still_asking(line), line


async def test_the_call_does_not_end_while_the_patient_is_still_asking(make_session, quiet_s):
    """Observed live: the patient hung up on the clinic mid-request, then the
    post-call analyser filed a High-severity bug against the clinic for it."""
    session, _ws = make_session()
    reply = "Sure, just let me know what slots you have available."

    assert session._honour_end_call(reply, True) is False
    assert session.end_call_deferrals == 1
    # A closing line is still allowed to end the call.
    assert session._honour_end_call("Thanks, that's everything. Goodbye!", True) is True


async def test_an_open_request_is_never_honoured_as_an_ending(make_session):
    """There is no "give up and hang up anyway" escape. There used to be one, and a
    model that over-emitted the token burned it early then hung up mid-booking."""
    session, _ws = make_session()
    reply = "Could you let me know what slots you have?"
    for _ in range(6):
        assert session._honour_end_call(reply, True) is False


# --- Patient hardening: long lines are what let the agent talk over us ---


async def test_the_opening_waits_out_a_bare_recording_notice(
    make_session, run_loop, monkeypatch, quiet_s
):
    """Observed live: the notice arrived alone, the settle expired, and the patient
    opened straight over the clinic's actual greeting."""
    session, _ws = make_session(opening_line="Hi, I'd like to book a checkup.")
    monkeypatch.setattr(mb, "OPENING_SETTLE_S", quiet_s)
    run_loop(session)

    session.note_agent_utterance("This call may be recorded for quality and training purposes.")
    await asyncio.sleep(quiet_s * 2.5)
    assert patient_lines(session) == [], "opened over the greeting after only the notice"

    session.note_agent_utterance("Thanks for calling Pivot Point. How may I help you today?")
    await asyncio.sleep(quiet_s + 0.8)
    assert patient_lines(session) == ["Hi, I'd like to book a checkup."]
    # The notice and the greeting are one agent turn, so the persona sees both.
    assert "How may I help you today?" in agent_lines(session)[0]


async def test_a_notice_that_runs_into_the_greeting_is_not_waited_on(
    make_session, run_loop, monkeypatch, quiet_s
):
    session, _ws = make_session(opening_line="Hi there.")
    monkeypatch.setattr(mb, "OPENING_SETTLE_S", quiet_s)
    run_loop(session)

    session.note_agent_utterance(
        "This call may be recorded. Thanks for calling Pivot Point. How may I help you?"
    )
    await asyncio.sleep(quiet_s + 0.8)
    assert patient_lines(session) == ["Hi there."], patient_lines(session)


async def test_notice_waiting_is_bounded(make_session, run_loop, monkeypatch, quiet_s):
    """A clinic that only ever plays a notice must not leave the patient mute."""
    session, _ws = make_session(opening_line="Hi there.")
    monkeypatch.setattr(mb, "OPENING_SETTLE_S", quiet_s)
    run_loop(session)

    for _ in range(mb.MAX_NOTICE_WAITS + 2):
        session.note_agent_utterance("This call is recorded for training purposes.")
        await asyncio.sleep(quiet_s * 2.5)

    # The point is that the patient is not left mute waiting for a greeting that
    # never comes; anything it says after opening is an ordinary turn.
    assert patient_lines(session)[0] == "Hi there.", patient_lines(session)


async def test_repeat_requests_escalate_instead_of_repeating(make_session, run_loop, quiet_s):
    """Three identical "could you repeat that?" lines read as a broken patient."""
    session, _ws = make_session()
    run_loop(session)
    session.note_agent_utterance("Thanks for calling.")
    await asyncio.sleep(quiet_s + 0.6)

    for _ in range(3):
        session.note_agent_utterance("Can you tell me your date of")  # cut off
        await asyncio.sleep(quiet_s * (mb.UNFINISHED_WAITS + 2) + 0.7)

    attempts = [
        attempt
        for attempt, partial in zip(session.persona.partial_attempts, session.persona.partials)
        if partial
    ]
    assert attempts == [1, 2, 3], attempts


async def test_a_complete_question_resets_the_repeat_escalation(make_session, run_loop, quiet_s):
    session, _ws = make_session()
    run_loop(session)
    session.note_agent_utterance("Thanks for calling.")
    await asyncio.sleep(quiet_s + 0.6)

    session.note_agent_utterance("Can you tell me your date of")
    await asyncio.sleep(quiet_s * (mb.UNFINISHED_WAITS + 2) + 0.7)
    assert session.partial_streak == 1

    session.note_agent_utterance("What is your date of birth?")
    await asyncio.sleep(quiet_s + 0.8)
    assert session.partial_streak == 0


def test_closing_detection_requires_an_actual_sign_off():
    """Accepting an offered time is not saying goodbye."""
    for line in ("Thanks so much for your help today, that's everything I needed. Goodbye!",
                 "No, that's everything. Thank you.",
                 "Great, see you then. Have a good day!",
                 "Nothing else, thanks for your help."):
        assert mb._is_a_closing_line(line), line
    for line in ("Yes, 2:30 PM on Tuesday, September 1 works for me.",
                 "Sure, just let me know what slots you have available.",
                 "I have a work meeting that conflicts with that time.",
                 "March 12, 1991."):
        assert not mb._is_a_closing_line(line), line


async def test_the_call_does_not_end_the_moment_a_time_is_accepted(make_session):
    """Observed live: the patient accepted a slot and hung up before the agent could
    confirm it, and the analyser filed "Missing booking confirmation" (High) against
    the agent for our hangup."""
    session, _ws = make_session()
    accepted = "Yes, 2:30 PM on Tuesday, September 1 works for me."

    assert session._honour_end_call(accepted, True) is False
    assert session.end_call_deferrals == 1
    # A real sign-off still ends the call immediately.
    assert session._honour_end_call("No, that's everything. Thank you.", True) is True


async def test_accepting_an_offer_never_ends_the_call_however_often_it_is_asked(make_session):
    """gpt-oss-120b asked to end on 3 of 4 turns; only a real sign-off may do it."""
    session, _ws = make_session()
    for _ in range(6):
        assert session._honour_end_call("Yes, that works for me.", True) is False
    assert session._honour_end_call("Thanks, that's everything. Goodbye!", True) is True


async def test_max_turns_still_ends_the_call(make_session):
    """The only termination guarantee, now that the deferral escape is gone."""
    session, _ws = make_session()
    session.scenario.max_turns = 1
    session.history = [{"speaker": "agent", "text": "hi"}, {"speaker": "patient", "text": "hi"}]
    reply, should_end = await session._reply_to("Anything else?")
    assert should_end is True
    assert mb._is_a_closing_line(reply), reply


async def test_end_call_is_never_invented_when_the_model_did_not_ask(make_session):
    session, _ws = make_session()
    assert session._honour_end_call("Thanks, goodbye!", False) is False
    assert session.end_call_deferrals == 0
