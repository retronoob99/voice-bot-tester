"""Cleanup applied to persona replies before they are spoken."""

from __future__ import annotations

import re

from src import persona_engine as pe
from src.persona_engine import (
    END_TOKEN,
    _drop_repeated_sentences,
    _drop_sentences_already_said,
)
from src.scenario import Persona, Scenario


def _history(*patient_lines: str) -> list[dict]:
    return [{"speaker": "patient", "text": line} for line in patient_lines]


_SCENARIO = Scenario(
    name="t",
    persona=Persona(identity="a test patient"),
    goal="book an appointment",
    intended_outcome="an appointment is booked",
)

# The same normalisation next_line() applies, so the tests exercise the real pair.
# Uses the shipped regex rather than a copy, so the two cannot drift apart.
_unjam = lambda text: pe.RUN_ON_RE.sub(" ", text)  # noqa: E731


def clean(text: str) -> str:
    return _drop_repeated_sentences(_unjam(text))


def test_collapses_a_line_the_model_repeated():
    """Observed live: the model padded a turn by saying the same thing three times."""
    assert clean("Yes, that's correct.Yes, that's correct.Yes, that's correct.") == (
        "Yes, that's correct."
    )


def test_separates_jammed_sentences():
    """Run-on text like '1 pm.Sure,' is read by TTS as a single mangled word."""
    assert clean("I'm free after 1 pm.Sure, any day works.") == (
        "I'm free after 1 pm. Sure, any day works."
    )


def test_keeps_genuinely_different_sentences():
    text = "Yes, this is Maria Chen. I need an afternoon slot next week."
    assert clean(text) == text


def test_leaves_abbreviations_alone():
    """Sentence splitting must not respace 'U.S.A.' into 'U. S. A.'."""
    text = "I live in the U.S.A. and want a checkup."
    assert clean(text) == text
    assert clean("My name is Maria Chen. Dr. Smith referred me.") == (
        "My name is Maria Chen. Dr. Smith referred me."
    )


def test_drops_only_repeats_not_neighbours():
    assert clean("Yes. Yes. No.") == "Yes. No."


def test_empty_and_single_sentence_are_unchanged():
    assert clean("Sure, goodbye.") == "Sure, goodbye."
    assert _drop_repeated_sentences("") == ""


def test_drops_the_request_the_patient_keeps_re_attaching():
    """Verbatim from a live call: only the leading clause was ever new."""
    history = _history(
        "Hi there, I'd like to schedule a routine checkup appointment, please.",
        "I'd like to book a routine annual checkup for next week, preferably in the afternoon.",
    )
    reply = (
        "Yes, Maria Chen here. I'd like to book a routine annual checkup for next week, "
        "preferably in the afternoon."
    )

    assert _drop_sentences_already_said(reply, history) == "Yes, Maria Chen here."


def test_keeps_a_standalone_repeat_when_the_agent_asks_again():
    """The agent re-asked for the date of birth; answering again is correct."""
    history = _history("March 15, 1992.")

    assert _drop_sentences_already_said("March 15, 1992.", history) == "March 15, 1992."


def test_leaves_a_wholly_new_reply_untouched():
    history = _history("Hi there, I'd like to schedule a checkup.")
    reply = "Yes, that's correct. Tuesday afternoon works well for me."

    assert _drop_sentences_already_said(reply, history) == reply


def test_ignores_what_the_agent_said():
    """Only the patient's own past lines count as already said."""
    history = [{"speaker": "agent", "text": "Am I speaking with Maria?"}]

    assert _drop_sentences_already_said("Am I speaking with Maria?", history) == (
        "Am I speaking with Maria?"
    )


def test_repetition_trimming_ignores_case_and_punctuation():
    history = _history("I need an afternoon slot next week")

    assert _drop_sentences_already_said(
        "Yes, that's me. I need an afternoon slot next week!", history
    ) == "Yes, that's me."


def test_empty_history_changes_nothing():
    assert _drop_sentences_already_said("Hello there.", []) == "Hello there."


def test_end_call_token_is_a_bare_marker():
    """The token is stripped before speaking, so it must not be sayable text."""
    assert END_TOKEN == "[[END_CALL]]"
    assert END_TOKEN not in clean("I'm all set, thanks. Goodbye.")


class _FakeGroq:
    """Minimal stand-in for AsyncGroq that replays scripted completions."""

    def __init__(self, replies):
        self._replies = list(replies)
        self.calls = 0
        self.chat = type("C", (), {"completions": self})()

    async def create(self, **kwargs):
        self.calls += 1
        self.last_kwargs = kwargs
        content = self._replies.pop(0)
        message = type("M", (), {"content": content})()
        # finish_reason and usage exist on the real response and are read when a
        # reply comes back blank, so the fake has to carry them too.
        choice = type("Ch", (), {"message": message, "finish_reason": "stop"})()
        usage = type("U", (), {"completion_tokens": 12})()
        return type("R", (), {"choices": [choice], "usage": usage})()


def _engine(replies):
    from src.persona_engine import PersonaEngine

    engine = PersonaEngine.__new__(PersonaEngine)
    engine._client = _FakeGroq(replies)
    engine._model = "test-model"
    engine._reasoning_effort = pe.REASONING_EFFORT
    engine._supports_reasoning_effort = True
    return engine


async def test_a_blank_reply_is_retried_before_giving_up():
    """Blank turns cost the patient a canned fallback, so re-roll once first."""
    engine = _engine(["", "Yes, Tuesday afternoon works."])

    text, should_end, _ = await engine.next_line(_SCENARIO, [], "Am I speaking with Maria?")

    assert text == "Yes, Tuesday afternoon works."
    assert engine._client.calls == 2
    assert should_end is False


async def test_a_usable_reply_is_not_retried():
    engine = _engine(["Yes, that's me."])

    text, _should_end, _ = await engine.next_line(_SCENARIO, [], "Am I speaking with Maria?")

    assert text == "Yes, that's me."
    assert engine._client.calls == 1


async def test_a_bare_end_token_is_not_retried():
    """It strips to nothing but still means 'wrap the call up'."""
    engine = _engine([END_TOKEN])

    text, should_end, _ = await engine.next_line(_SCENARIO, [], "Anything else?")

    assert text == ""
    assert should_end is True
    assert engine._client.calls == 1


def test_retry_budget_stays_small():
    """Each retry is another round trip inside the live audio path."""
    from src.persona_engine import EMPTY_REPLY_RETRIES

    assert 1 <= EMPTY_REPLY_RETRIES <= 2


# --- Accuracy: the de-duplication filter must never delete a persona fact ---

FACTS = {
    "full_name": "Maria Chen",
    "date_of_birth": "March 12, 1991",
    "phone_number": "555-0142",
}


def test_a_repeated_fact_survives_alongside_a_new_one():
    """The bug this guards: the agent asks to confirm two details and gets one.

    Observed shape — the date of birth had been given earlier in the call, so the
    filter stripped it and only the phone number was ever spoken. To the agent that
    reads as the patient answering the wrong question.
    """
    history = [{"speaker": "patient", "text": "March 12, 1991."}]
    reply = "March 12, 1991. And my number is 555-0142."
    assert pe._drop_sentences_already_said(reply, history, FACTS) == reply


def test_a_fact_survives_even_when_the_rest_of_the_line_is_new():
    history = [
        {"speaker": "patient", "text": "Maria Chen."},
        {"speaker": "patient", "text": "March 12, 1991."},
    ]
    reply = "Maria Chen. March 12, 1991."
    assert pe._drop_sentences_already_said(reply, history, FACTS) == reply


def test_boilerplate_is_still_trimmed():
    """The filter's original job: stop re-attaching the opening request every turn."""
    history = [{"speaker": "patient", "text": "I'd like to book a routine annual checkup."}]
    reply = "Yes, that's me. I'd like to book a routine annual checkup."
    assert pe._drop_sentences_already_said(reply, history, FACTS) == "Yes, that's me."


def test_a_standalone_re_answer_is_never_emptied():
    """Asked again for the same detail, the whole reply is a repeat — keep it."""
    history = [{"speaker": "patient", "text": "March 12, 1991."}]
    assert pe._drop_sentences_already_said("March 12, 1991.", history, FACTS) == "March 12, 1991."


def test_filter_without_facts_still_behaves():
    """Scenarios may declare no facts at all; nothing is protected, nothing crashes."""
    history = [{"speaker": "patient", "text": "Hello there."}]
    assert pe._drop_sentences_already_said("Hello there. And also this.", history, None) == (
        "And also this."
    )


def test_temperature_is_low_enough_for_facts_to_be_stable():
    """At 0.8 the model drifted between turns and intermittently returned nothing."""
    assert 0.0 <= pe.TEMPERATURE <= 0.4, pe.TEMPERATURE


# --- Accuracy: a half-heard question must be flagged, not guessed at ---


def test_a_partial_utterance_adds_a_nudge_that_never_enters_history():
    engine = pe.PersonaEngine("key", "model")
    history = [{"speaker": "patient", "text": "Yes, it's Maria Chen."}]

    normal = engine.build_messages(_SCENARIO, history, "What is your date of birth?")
    partial = engine.build_messages(_SCENARIO, history, "Can you please provide your date of", True)

    assert len(partial) == len(normal) + 1
    assert partial[-1]["role"] == "system"
    assert partial[-1]["content"] == pe.PARTIAL_UTTERANCE_NUDGE
    # The nudge is an instruction for this one generation, not a conversation turn:
    # it must not be mistaken for something the agent said.
    assert normal[-1]["content"] == "What is your date of birth?"
    assert history == [{"speaker": "patient", "text": "Yes, it's Maria Chen."}]


def test_history_is_passed_whole_and_in_order():
    """Truncation or reordering here is how a patient loses track of its own facts."""
    engine = pe.PersonaEngine("key", "model")
    history = [
        {"speaker": "agent", "text": "Thanks for calling."},
        {"speaker": "patient", "text": "I'd like to book a checkup."},
        {"speaker": "agent", "text": "Am I speaking with Maria?"},
        {"speaker": "patient", "text": "Yes, it's Maria Chen."},
    ]
    messages = engine.build_messages(_SCENARIO, history, "Date of birth?")

    assert [m["role"] for m in messages] == [
        "system", "user", "assistant", "user", "assistant", "user",
    ]
    assert [m["content"] for m in messages[1:]] == [
        "Thanks for calling.",
        "I'd like to book a checkup.",
        "Am I speaking with Maria?",
        "Yes, it's Maria Chen.",
        "Date of birth?",
    ]


def test_the_prompt_forbids_guessing_at_a_half_heard_question():
    engine = pe.PersonaEngine("key", "model")
    prompt = engine._system_prompt(_SCENARIO)
    assert "never guess at the question" in prompt
    assert "word for word" in prompt


# --- Patient hardening: a line the agent can talk over is a wasted turn ---


def test_a_long_reply_is_capped_to_whole_sentences():
    reply = (
        "Yes, that's me. I'd like to book a routine annual checkup next week, "
        "preferably in the afternoon, since I work mornings."
    )
    capped = pe._cap_length(reply)
    assert capped == "Yes, that's me."
    assert len(capped) <= pe.MAX_REPLY_CHARS


def test_capping_never_cuts_mid_sentence():
    """A fragment on the line is the exact bug this harness exists to catch."""
    long_single = (
        "I would really like to come in for the checkup sometime next week in the "
        "afternoon if you have anything at all available then please"
    )
    assert len(long_single) > pe.MAX_REPLY_CHARS
    # One sentence and nothing to trim: kept whole rather than chopped.
    assert pe._cap_length(long_single) == long_single


def test_a_short_reply_is_untouched():
    for reply in ("March 12, 1991.", "Yes, this is Maria Chen.", ""):
        assert pe._cap_length(reply) == reply


def test_the_cap_is_short_enough_to_matter():
    """Patient audio measured at ~0.075s per character across 12 live lines."""
    assert pe.MAX_REPLY_CHARS * 0.075 <= 7.0, "cap allows a line the agent can talk over"


def test_repeat_nudges_escalate_and_differ():
    first, second, third = (pe.partial_utterance_nudge(n) for n in (1, 2, 3))
    assert first != second != third and first != third
    # Past the last variant it saturates rather than raising IndexError.
    assert pe.partial_utterance_nudge(9) == third
    assert pe.partial_utterance_nudge(0) == first


# --- A malformed end token must never be spoken out loud ---


def _scrub(text: str) -> tuple[str, bool]:
    """The same end-token handling _generate() applies."""
    import re

    should_end = pe.END_TOKEN in text or bool(pe.END_TOKEN_RE.search(text))
    cleaned = pe.BRACKET_JUNK_RE.sub(" ", pe.END_TOKEN_RE.sub(" ", text))
    return re.sub(r"\s{2,}", " ", cleaned).strip(), should_end


def test_a_half_written_end_token_is_never_spoken():
    """Observed live: the patient said "Keep it. [[" out loud."""
    assert _scrub("Keep it. [[") == ("Keep it.", False)


def test_end_token_variants_all_end_the_call():
    for text in ("Bye. [[END_CALL]]", "Thanks. [[END CALL]]",
                 "Bye. [[[[END_CALL]]", "No, [[end_call]]"):
        cleaned, should_end = _scrub(text)
        assert should_end, text
        assert "[" not in cleaned and "]" not in cleaned and "END" not in cleaned.upper()


def test_stage_directions_are_stripped_rather_than_spoken():
    assert _scrub("Yes. [pauses] Thanks.") == ("Yes. Thanks.", False)


def test_ordinary_replies_are_untouched():
    for text in ("March 12, 1991.", "That's everything, thank you!", "Yes, this is Maria Chen."):
        assert _scrub(text) == (text, False)


def test_a_number_jammed_onto_a_word_is_split():
    """Observed live: the patient said "Yes.555-0142." and TTS read it as one word."""
    assert clean("Yes.555-0142.") == "Yes. 555-0142."


def test_splitting_leaves_initialisms_and_decimals_alone():
    for text in ("U.S.A.", "It costs 3.5 dollars.", "March 12, 1991."):
        assert clean(text) == text


def test_blank_replies_get_more_than_one_re_roll():
    """Every blank turn costs a canned fallback line that reads as a patient bug."""
    assert pe.EMPTY_REPLY_RETRIES >= 2


# --- Answering only what was asked ---

_FACTS = {
    "full_name": "Maria Chen",
    "date_of_birth": "March 12, 1991",
    "phone_number": "555-0142",
}


def test_an_unrequested_detail_is_not_handed_over():
    """Observed live: asked only to confirm the visit type, the patient answered
    "Yes. 555-0142." Volunteering hides the bug worth catching — whether the agent
    asks at all."""
    asked = "Just to confirm, you'd like to book a general office visit. Is that correct?"
    assert pe._drop_unrequested_facts("Yes. 555-0142.", asked, _FACTS) == "Yes."


def test_a_requested_detail_survives():
    asked = "Can I get your name and date of birth?"
    reply = "Maria Chen. March 12, 1991."
    assert pe._drop_unrequested_facts(reply, asked, _FACTS) == reply


def test_a_fact_requested_by_value_rather_than_name_survives():
    """"Am I speaking with Maria?" never says "name", but it is clearly asking."""
    asked = "Am I speaking with Maria?"
    reply = "Yes, this is Maria Chen. March 12, 1991."
    # The name was asked for; the date of birth was not.
    assert pe._drop_unrequested_facts(reply, asked, _FACTS) == "Yes, this is Maria Chen."


def test_the_first_sentence_is_always_the_answer():
    """Whatever the first sentence contains, it is the reply to the question."""
    asked = "What number can we reach you on?"
    assert pe._drop_unrequested_facts("555-0142.", asked, _FACTS) == "555-0142."
    asked_other = "Is that correct?"
    assert pe._drop_unrequested_facts("555-0142.", asked_other, _FACTS) == "555-0142."


def test_nothing_is_dropped_without_an_asked_utterance_or_facts():
    reply = "Yes. 555-0142."
    assert pe._drop_unrequested_facts(reply, None, _FACTS) == reply
    assert pe._drop_unrequested_facts(reply, "Is that correct?", None) == reply


# --- Abbreviations must survive splitting and rejoining ---


def test_times_are_not_split_into_stray_letters():
    """"2 p.m." became "2 p. m." — TTS reads that as two loose letters."""
    assert pe._split_sentences("How about Tuesday at 2 p.m.?") == ["How about Tuesday at 2 p.m.?"]
    assert clean("How about Tuesday at 2 p.m.?") == "How about Tuesday at 2 p.m.?"


def test_ordinary_sentences_still_split():
    assert pe._split_sentences("Yes. March 12, 1991.") == ["Yes.", "March 12, 1991."]


def test_a_number_jammed_onto_the_next_sentence_is_split():
    assert clean("555-0142.I'm free Tuesday.") == "555-0142. I'm free Tuesday."


async def test_reasoning_is_kept_out_of_the_spoken_token_budget():
    """Reasoning tokens are billed against max_tokens. At 200 with default effort the
    model spent all 200 on reasoning and returned nothing, 4 times out of 4."""
    engine = _engine(["Yes, that's me."])
    await engine.next_line(_SCENARIO, [], "Am I speaking with Maria?")

    sent = engine._client.last_kwargs
    assert sent["max_tokens"] >= 512, sent["max_tokens"]
    assert sent["reasoning_effort"] == "low"


async def test_a_model_that_rejects_reasoning_effort_still_answers():
    """Not every Groq-hosted model takes the parameter; one turn must not fail."""
    engine = _engine(["Yes, that's me."])
    original = engine._client.create
    calls = {"n": 0}

    async def picky(**kwargs):
        calls["n"] += 1
        if "reasoning_effort" in kwargs and calls["n"] == 1:
            raise ValueError("400: unknown parameter reasoning_effort")
        return await original(**kwargs)

    engine._client.create = picky
    engine._client.chat.completions = engine._client

    text, _should_end, _ = await engine.next_line(_SCENARIO, [], "Am I speaking with Maria?")
    assert text == "Yes, that's me."
    # Disabled for the rest of the call rather than retried every turn.
    assert engine._supports_reasoning_effort is False


# --- Cross-scenario: the filters must not break the scenarios they don't know about ---


def _scenario(name):
    from src.scenario import find_scenario

    return find_scenario(name)


def test_a_short_fact_value_is_not_a_wildcard():
    """escalation_test has child_age: 6. A plain substring test made that match any
    sentence containing a 6, including "February 19, 1996"."""
    facts = _scenario("escalation_test").persona.facts
    for reply in ("Yes, Tuesday works. I'm free after 6 pm.",
                  "Yes. My birthday is February 19, 1996."):
        assert pe._drop_unrequested_facts(reply, "Does Tuesday work?", facts, []) == reply


def test_a_volunteered_fact_survives_when_the_scenario_needs_it():
    """context_retention must offer the allergy unprompted before it can test recall."""
    scenario = _scenario("context_retention")
    reply = "Tuesday works. By the way, I'm allergic to penicillin."

    assert "penicillin" in pe._drop_unrequested_facts(
        reply, "Does Tuesday work?", scenario.persona.facts, scenario.persona.volunteers
    )
    # The exemption is doing the work: without it the mention is filtered out.
    assert "penicillin" not in pe._drop_unrequested_facts(
        reply, "Does Tuesday work?", scenario.persona.facts, []
    )


def test_every_scenario_that_must_volunteer_declares_it():
    """A scenario whose goal is to offer something unprompted needs the exemption, or
    the filter deletes its premise and the agent gets blamed for the gap."""
    for name, expected in (
        ("context_retention", "allergy"),
        ("refill_request", "medication"),
        ("escalation_test", "child_age"),
    ):
        persona = _scenario(name).persona
        assert expected in persona.volunteers, f"{name} must volunteer {expected}"
        assert expected in persona.facts, f"{name} volunteers a fact it does not define"
    # Scenarios where withholding is the point must stay locked down.
    for name in ("simple_schedule", "office_hours_edge_case"):
        assert _scenario(name).persona.volunteers == []


def test_a_fact_the_agent_asked_for_survives_a_different_word_ending():
    """The agent asks about an "allergy" as "any allergies"."""
    facts = _scenario("context_retention").persona.facts
    reply = "Tuesday works. I'm allergic to penicillin."
    assert "penicillin" in pe._drop_unrequested_facts(
        reply, "Any allergies we should know about?", facts, []
    )


def test_the_patients_question_outranks_the_pleasantry_when_capping():
    """escalation_test exists to ask its clinical question; keeping "Thanks, Tuesday
    works." and dropping the question would test nothing."""
    reply = (
        "Thanks, Tuesday afternoon works well for us. "
        "Is it safe to give my 6-year-old ibuprofen and acetaminophen together?"
    )
    capped = pe._cap_length(reply)
    assert capped.endswith("?"), capped
    assert "ibuprofen" in capped
    assert len(capped) <= pe.MAX_REPLY_CHARS


def test_both_are_kept_when_they_fit():
    reply = "Thanks. Is it safe to give ibuprofen and acetaminophen together?"
    assert pe._cap_length(reply) == reply


def test_a_question_too_long_to_fit_falls_back_to_the_first_sentence():
    reply = "Sure. " + ("a" * 120) + "?"
    assert pe._cap_length(reply) == "Sure."


def test_stock_agreement_is_dropped_when_nothing_was_offered():
    """The persona must not accept an appointment that was never mentioned.

    Live failure: asked only for a date of birth, the persona replied "March 12, 1991.
    Sure, that works. Yes, that's fine. Okay, great." - three acceptances of a slot the
    agent had not named. The transcript is later judged against that acceptance, so it
    manufactures agreement that never happened. A prompt rule naming this exact case did
    not stop it (two stacked agreements became three), hence the filter.
    """
    from src.persona_engine import _drop_unearned_acceptances as drop

    assert drop(
        "March 12, 1991. Sure, that works. Yes, that's fine. Okay, great.",
        "Can you please provide your date of birth?",
    ) == "March 12, 1991."

    assert drop(
        "It's a routine follow-up. Sure, that works. Okay, that slot is fine.",
        "Is this for a recent visit or a specific concern?",
    ) == "It's a routine follow-up."


def test_a_real_acceptance_of_a_real_offer_survives():
    """The filter must never eat agreement to something the agent actually offered."""
    from src.persona_engine import _drop_unearned_acceptances as drop

    real = "Tuesday at 2 PM works for me. That sounds good."
    assert drop(real, "We have an opening on Tuesday, September 1 at 2PM.") == real
    # A substantive second sentence is not agreement noise and stays either way.
    assert drop("Yes. My number is 555-0142.", "What is your number?") == (
        "Yes. My number is 555-0142."
    )
    # A single sentence is never trimmed, whatever it says.
    assert drop("Sure, that works.", "anything at all") == "Sure, that works."
