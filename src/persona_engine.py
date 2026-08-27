from __future__ import annotations

import logging
import os
import re
import time
from typing import Optional

from groq import AsyncGroq

from .scenario import Scenario

logger = logging.getLogger("persona_engine")

END_TOKEN = "[[END_CALL]]"

# The model does not always emit the token cleanly. Observed live, the patient said
# "Keep it. [[" out loud: a half-written token that the exact-string replace could
# not match, so the brackets reached TTS and the transcript. Matching the END/CALL
# word with whatever brackets happen to surround it catches the malformed variants.
END_TOKEN_RE = re.compile(r"\[*\s*END[_\s-]?CALL\s*\]*", re.IGNORECASE)

# Anything left in brackets is a stage direction or a token fragment — never words a
# patient says out loud. Stripped rather than spoken.
BRACKET_JUNK_RE = re.compile(r"\[[^\]]*\]|[\[\]]+")

# The model sometimes jams sentences together with no space ("1 pm.Sure, I'm free",
# "Yes.555-0142.", "555-0142.I'm free"); TTS reads that as one run-on word.
#
# Both alternatives require the NEXT character to be uppercase or a digit, never
# lowercase: an earlier version allowed lowercase and promptly turned "2 p.m." into
# "2 p. m.". What is deliberately left alone:
#   "U.S.A."  uppercase before the dot, so neither alternative fires
#   "3.5"     digit before, digit after, and the second alternative needs uppercase
#   "p.m."    lowercase after the dot
RUN_ON_RE = re.compile(r"(?<=[a-z][.!?])(?=[A-Z0-9])|(?<=[0-9][.!?])(?=[A-Z])")

# Abbreviations whose internal full stop is not a sentence ending. Without this
# "Tuesday at 2 p.m." splits into "...2 p." and "m.", and any later rejoin puts them
# back as "2 p. m." — which TTS reads aloud as two stray letters.
ABBREVIATION_END_RE = re.compile(
    r"(?:\b[A-Za-z]|\ba\.m|\bp\.m|\bDr|\bMr|\bMrs|\bMs|\bSt|\bAve|\bRd|\bNo|\bvs|\betc)\.$",
    re.IGNORECASE,
)

# Extra attempts when the model returns a blank line. Raised from 1 after live calls
# still showed 1 blank in 6 and 1 in 8 turns getting through two attempts, and every
# blank costs the patient a canned "could you say that again?" that reads as a patient
# bug. A re-roll costs about half a second, far less than a wasted turn.
EMPTY_REPLY_RETRIES = 2



def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        logger.warning("%s=%r is not an integer; using %s", name, raw, default)
        return default


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        logger.warning("%s=%r is not a number; using %s", name, raw, default)
        return default


# This is a facts-recital task: the patient must give the same date of birth every
# time it is asked. At 0.8 the model drifted between takes and intermittently
# returned nothing at all (observed live: 1 blank of 6 turns on one call, 5 of 13 on
# another), and each blank turn costs the patient a canned fallback line. 0.3 keeps
# the delivery natural while making the facts stable.
TEMPERATURE = _env_float("PERSONA_TEMPERATURE", 0.3)

# Groq's gpt-oss models are reasoning models, and reasoning tokens are billed against
# max_tokens. Measured on the exact turn that blanked during a live call, at the old
# max_tokens=200 with default effort: finish_reason="length", all 200 tokens spent,
# 838-989 characters of reasoning, and an EMPTY reply — 4 times out of 4. With
# reasoning_effort="low" the same turn produced a usable line 4 times out of 4, using
# 43-113 tokens. This was the cause of every blank turn and of the truncated "I'd"
# that reached the phone, not model quality.
#
# "low" rather than off: the persona still has to decide what was asked and which
# fact answers it. Raise it if replies get careless; the budget below has room.
REASONING_EFFORT = os.environ.get("GROQ_REASONING_EFFORT", "low").strip().lower()

# Headroom so reasoning can never starve the spoken line again. This does NOT make
# the patient chattier — _cap_length still bounds what is actually said — it only
# stops the reply being cut off before it exists.
MAX_COMPLETION_TOKENS = _env_int("MAX_COMPLETION_TOKENS", 512)

# Hard ceiling on how long one patient line may be. Measured per voice at
# speech_speed 0.85 (short lines, so Aura's fixed padding inflates the rate a little):
#   asteria 0.064 s/char   arcas 0.065   hera 0.067   athena 0.068   apollo 0.078
# So this caps a line at roughly 5.7s on the fastest voice and 7.0s on apollo, which
# context_retention uses and which runs about 20% longer than the rest.
#
# Why it matters: a 97-character line rendered to 7.2s of audio, the agent began its
# next question about 2s in, heard the patient still talking, and abandoned its own
# sentence mid-word ("Just to con"). Each of those cost a turn and produced a
# "could you repeat that?" that made the patient look broken. This is a backstop, not
# the primary control — the prompt asks for roughly fifteen words, which lands near
# 60 characters. A model that ignores that instruction still must not be able to put
# seven seconds of audio on the line.
MAX_REPLY_CHARS = _env_int("MAX_REPLY_CHARS", 90)

# Asking "Sorry, could you say that again?" in exactly those words three times in one
# call (observed live) reads as a broken patient rather than a person. The wording
# escalates instead, and each variant asks for something more specific.
PARTIAL_RETRY_NUDGES = (
    "Ask them to repeat it, in one short natural sentence.",
    "You have now missed them twice. Say you only caught the first part, and ask "
    "them to repeat just the end of it. Use different words than last time.",
    "The line keeps breaking up. Say so plainly, and ask them to go ahead with what "
    "they need from you. Use different words than either of your last two attempts.",
)


# Appended as a transient nudge (never stored in history) when the agent's line
# reached us cut off mid-sentence. Without it the model guesses at what was being
# asked, which is where the confidently-wrong answers come from.
def partial_utterance_nudge(attempt: int = 1) -> str:
    """The instruction for a half-heard line. `attempt` is 1-based and consecutive."""
    index = min(max(attempt, 1), len(PARTIAL_RETRY_NUDGES)) - 1
    return (
        "(Note for you, not the patient's words: that last line from the agent "
        "reached you cut off mid-sentence, so you did NOT hear the whole question. "
        "Do not guess at what was being asked and do not answer it. "
        + PARTIAL_RETRY_NUDGES[index]
        + ")"
    )


# Kept as the first-attempt wording so existing callers and tests keep working.
PARTIAL_UTTERANCE_NUDGE = partial_utterance_nudge(1)

# Appended (transiently, like the partial nudge) when the agent has just repeated
# something it already said. Observed live: the patient waited ~14s to open, the
# clinic re-greeted, and the patient answered the second greeting by restating its
# whole booking request — so the transcript shows it asking for a checkup twice.
AGENT_REPEATED_NUDGE = (
    "(Note for you, not the patient's words: the agent just repeated something it "
    "already said earlier in this call. Do NOT repeat your own earlier answer back "
    "at it. Say briefly that you already gave that, and push for the next step.)"
)

SYSTEM_PROMPT = """You are role-playing as a patient on a phone call to a medical clinic's \
voice agent, for the purpose of QA-testing that agent. Stay fully in character.

Persona: {identity}
Speaking style: {voice_style}
{facts_block}
Your goal for this call: {goal}
{edge_case_line}
What a successful test of this scenario looks like: {intended_outcome}

Rules:
- Output ONLY the words the patient says out loud for this one turn. No stage \
directions, narration, or quotation marks.
- Stay in character as the patient at all times; never mention you are an AI or a test.
- Steer toward your goal (and the edge case, if any) when the conversation stalls \
or drifts — but do that by giving the next piece of information or asking a new \
question, never by repeating your original request again.
- Keep each line short — one sentence, two at most. Long turns take seconds to say \
out loud and the agent is waiting on you.
- Answer only what was actually asked. Never volunteer a detail the agent did not \
ask for, and never re-state one you have already given: if it asks you to confirm \
something, just confirm it rather than reciting your details again.
- Say what you need once. Never restate the same request several different ways in \
a single turn, even if the agent has asked you something similar before.
- Do not pad an answer with your original request again — the agent already heard \
why you are calling. Answer what was just asked and stop there.
- If the agent repeats itself or seems stuck, never go quiet and never just echo it \
back. Answer its question directly, or say plainly that you already gave that \
information and ask for the next step.
- Never reply with nothing. You are on a phone call, so silence reads as the line \
dropping — always say something out loud, even a short acknowledgement.
- When asked for a personal detail, use the facts above exactly as written and never \
invent or change one. If you are asked for something not listed there, say you don't \
have it to hand rather than making it up. Give the same answer every time you are \
asked, even if the agent asks more than once \
— word for word, character for character. A date of birth or phone number \
that changes between turns is a failed test, not a variation in phrasing.
- Never refer to something the agent has not actually said. Only ask it to repeat itself \
when its most recent line genuinely reached you broken or unfinished. If it asked a plain, \
complete question, answer that question and nothing else. Asking it to repeat "the times" \
or "the options" when it has never offered any is a failure, not a recovery.
- One acknowledgement is enough. Never stack agreements - "Yes, that's correct. Sure, that \
works. That sounds good. Okay, that works for me." is one thought said four times. Say it \
once, then either add the substance or stop.
- State your own facts; never ask the agent to confirm one for you. A detail from \
the facts above is something you TELL the agent ("I have a penicillin allergy"), \
not something you invite it to agree with ("you've got my allergy on file, right?"). \
A leading question hands the agent the answer and hides the very thing being tested \
— whether it already knew.
- Never accept a detail that contradicts the facts above. If the agent reads back a \
name, date of birth, phone number, or anything else that does not match, say plainly \
that it is wrong and give the correct one. Do not confirm it to be polite and do not \
let it stand.
- Answer the question that was actually asked, and only that one. If the agent \
asks for two things in one breath, give both. If you are not certain what it \
asked, say so and ask it to repeat — never guess at the question and never \
answer a different one than the one you heard.
- If the agent's line reaches you garbled, truncated, or trailing off \
mid-sentence, do not try to complete it in your head. Say you didn't catch \
that and ask them to repeat it.
- Only when the call is genuinely over — the agent has confirmed what you came \
for, or has made clear it cannot — say goodbye in your own words AND end that \
line with the token {end_token}. Never put the token on a line that answers a \
question, accepts an offered time, or asks for anything: those are the middle of \
the call, not the end. If in doubt leave it out and keep talking — the call will \
be wrapped up for you when it has run its course.
"""


def _split_sentences(text: str) -> list[str]:
    """Split into sentences, keeping abbreviations like "2 p.m." in one piece."""
    parts = [s.strip() for s in re.findall(r"[^.!?]+[.!?]*", text) if s.strip()]
    merged: list[str] = []
    for part in parts:
        # A fragment ending in an abbreviation is not a finished sentence: glue the
        # next piece back on rather than leaving "2 p." and "m." to be rejoined with
        # a space later.
        if merged and ABBREVIATION_END_RE.search(merged[-1]):
            merged[-1] = f"{merged[-1]}{part}"
        else:
            merged.append(part)
    return merged


def _key(sentence: str) -> str:
    """Normalise a sentence for comparison: case and punctuation carry no meaning."""
    return re.sub(r"[^a-z0-9 ]", "", sentence.lower()).strip()


def _drop_repeated_sentences(text: str) -> str:
    """Collapse a line the model padded out by saying the same thing several times.

    Observed live: "Yes, that's correct. Yes, that's correct. Yes, that's correct."
    Only exact repeats (ignoring case and punctuation) are removed, so a line that
    genuinely restates something in different words is left alone.
    """
    sentences = _split_sentences(text)
    kept: list[str] = []
    seen: set[str] = set()
    for sentence in sentences:
        key = _key(sentence)
        if key and key in seen:
            continue
        seen.add(key)
        kept.append(sentence)
    if len(kept) == len(sentences):
        # Nothing was repeated. Return the original untouched rather than a rejoin,
        # which would respace abbreviations like "U.S.A." into "U. S. A.".
        return text
    return " ".join(kept)


def _protected_keys(facts: dict) -> set[str]:
    """Sentence keys that must never be filtered out of a reply.

    A persona fact is the one thing the patient is *supposed* to repeat verbatim
    every time it is asked, so it has to survive the de-duplication below.
    """
    return {k for k in (_key(str(v)) for v in (facts or {}).values()) if k}


def _carries_a_fact(sentence: str, facts: dict) -> bool:
    """True when this sentence states one of the persona's pinned details."""
    key = _key(sentence)
    return bool(key) and any(value in key for value in _protected_keys(facts))


def _drop_sentences_already_said(
    text: str, history: list[dict], facts: Optional[dict] = None
) -> str:
    """Trim the boilerplate the model re-attaches to every turn.

    Observed live, the patient answered three different questions with the same tail:
    "Yes, that's me. I'd like to book a routine annual checkup next week..." — only
    the first clause was ever new. Re-feeding the request each turn also masks the
    very bug worth catching, where the agent has lost the thread.

    A sentence is only dropped when the reply still has something left to say, so a
    standalone re-answer ("March 15, 1992." after the agent asks again) survives.

    Sentences carrying a persona fact are never dropped. Without that exception this
    filter silently deleted correct answers: asked to confirm date of birth *and*
    phone number, the model produced "March 12, 1991. And my number is 555-0142."
    and — because the date of birth had been given earlier in the call — only the
    phone number was ever spoken. That reads as the patient answering the wrong
    question, which is exactly the class of bug this harness exists to catch in the
    *other* agent.
    """
    already_said = {
        _key(sentence)
        for turn in history
        if turn.get("speaker") == "patient"
        for sentence in _split_sentences(turn.get("text", ""))
    }
    sentences = _split_sentences(text)
    kept = [
        sentence
        for sentence in sentences
        if _key(sentence) not in already_said or _carries_a_fact(sentence, facts or {})
    ]
    if not kept or len(kept) == len(sentences):
        # Either nothing is new (a legitimate repeat) or nothing is stale.
        return text
    return " ".join(kept)


# Words in a fact's key that carry no meaning when looking for it in a question.
_FACT_KEY_STOPWORDS = frozenset({"of", "the", "my", "your", "a", "an"})


def _fact_cues(name: str, value: str) -> set[str]:
    """Words that indicate the agent asked for this particular fact.

    Both the key and the value contribute: "full_name" is requested by the word
    "name", but also by "Am I speaking with Maria?", which never says "name" at all.
    """
    words = re.split(r"[^a-z0-9]+", f"{name} {value}".lower())
    return {w for w in words if len(w) > 2 and w not in _FACT_KEY_STOPWORDS}


# Length of the prefix two words must share to count as the same word. Agents ask
# about an "allergy" as "any allergies", and a whole-word comparison missed it, so a
# fact the agent HAD asked for was filtered out of the answer.
_STEM = 5


def _cue_matches(cue: str, asked_words: set[str]) -> bool:
    """True when the agent's wording refers to this cue, allowing for word endings."""
    if cue in asked_words:
        return True
    if len(cue) < 4:
        return False  # too short to stem safely ("sam", "kim")
    stem = cue[:_STEM]
    return any(len(w) >= 4 and (w.startswith(stem) or cue.startswith(w[:_STEM])) for w in asked_words)


def _states_fact(sentence_key: str, name: str, value: str) -> bool:
    """True when this sentence actually states the fact, not merely echoes a digit.

    Matching is on word boundaries. A plain substring test made short values behave
    as wildcards: `child_age: 6` matched every sentence containing a 6, including
    "February 19, 1996". Values of one or two characters are ambiguous even with
    boundaries ("6" in "after 6 pm"), so those additionally require a word from the
    fact's NAME nearby before the sentence counts as stating it.
    """
    value_key = _key(str(value))
    if not value_key:
        return False
    if not re.search(rf"\b{re.escape(value_key)}\b", sentence_key):
        return False
    if len(value_key) > 2:
        return True
    name_words = [w for w in re.split(r"[^a-z0-9]+", str(name).lower()) if len(w) > 2]
    return any(re.search(rf"\b{re.escape(w)}\b", sentence_key) for w in name_words)


def _drop_unrequested_facts(
    text: str,
    asked: Optional[str],
    facts: Optional[dict],
    volunteers: Optional[list] = None,
) -> str:
    """Remove trailing sentences that hand over a detail the agent never asked for.

    The prompt already forbids volunteering, and the model does it anyway. Observed
    live: asked only "you'd like to book a general office visit for a routine
    checkup. Is that correct?", the patient answered "Yes. 555-0142." — a phone
    number nobody had requested. Handing an agent information it did not ask for
    hides the very bug worth catching, which is whether it asks at all.

    `volunteers` names facts the scenario NEEDS the patient to offer unprompted, and
    which are therefore exempt. Some scenarios exist precisely to volunteer something
    — context_retention has to mention a penicillin allergy mid-call before it can
    test whether the agent remembers it — and filtering that out would delete the
    scenario's whole premise, then blame the agent for not recalling what was never
    said.

    Only sentences AFTER the first are considered: the first sentence is the answer
    to the question, whatever it contains.
    """
    if not asked or not facts:
        return text
    sentences = _split_sentences(text)
    if len(sentences) < 2:
        return text
    exempt = {str(name).lower() for name in (volunteers or [])}
    asked_words = set(re.split(r"[^a-z0-9]+", asked.lower()))
    unrequested = [
        (str(name), str(value))
        for name, value in facts.items()
        if str(value).strip()
        and str(name).lower() not in exempt
        and not any(
            _cue_matches(cue, asked_words) for cue in _fact_cues(str(name), str(value))
        )
    ]
    if not unrequested:
        return text

    kept = [sentences[0]]
    for sentence in sentences[1:]:
        key = _key(sentence)
        stated = next(
            (name for name, value in unrequested if _states_fact(key, name, value)), None
        )
        if stated:
            logger.info("Dropped unrequested %s from a reply: %r", stated, sentence)
            continue
        kept.append(sentence)
    return " ".join(kept) if len(kept) != len(sentences) else text


def _cap_length(text: str, limit: int = MAX_REPLY_CHARS) -> str:
    """Trim a reply to whole sentences that fit inside `limit` characters.

    Whole sentences only: cutting mid-sentence would put a fragment on the line,
    which is the exact failure this harness is meant to catch in the other agent.
    The first sentence is always kept even when it alone exceeds the limit — a
    truncated answer is worse than a slightly long one.

    A trailing question outranks the sentences before it. The patient's question is
    usually the actual test — escalation_test exists to ask "is it safe to give my
    6-year-old ibuprofen and acetaminophen together?" — and plain front-to-back
    trimming drops exactly that, keeping the pleasantries and discarding the probe.
    """
    if len(text) <= limit:
        return text
    sentences = _split_sentences(text)
    if not sentences:
        return text

    first, rest = sentences[0], sentences[1:]
    question_index = next(
        (i for i in range(len(rest) - 1, -1, -1) if rest[i].endswith("?")), None
    )
    question = rest[question_index] if question_index is not None else None

    if (
        question is not None
        and len(question) <= limit < len(first) + 1 + len(question)
    ):
        # The probe cannot share the line with the answer, and the probe is the test.
        # escalation_test exists to ask its clinical question; keeping "Thanks,
        # Tuesday works." and dropping the question would test nothing at all.
        logger.info("Kept the question and dropped the lead-in to fit %d chars", limit)
        kept = [question]
    elif question is not None and len(first) + 1 + len(question) <= limit:
        # The question is guaranteed a place; the middles fill whatever is left.
        kept = [first]
        for index, sentence in enumerate(rest):
            if index == question_index:
                continue
            if len(" ".join(kept)) + 1 + len(sentence) + 1 + len(question) > limit:
                continue
            kept.append(sentence)
        kept.append(question)
    else:
        kept = [first]
        for sentence in rest:
            if len(" ".join(kept)) + 1 + len(sentence) > limit:
                break
            kept.append(sentence)

    trimmed = " ".join(kept)
    if trimmed != text:
        logger.info("Capped a %d-char reply to %d chars", len(text), len(trimmed))
    return trimmed


class PersonaEngine:
    def __init__(self, api_key: str, model: str):
        self._client = AsyncGroq(api_key=api_key)
        self._model = model
        self._reasoning_effort = REASONING_EFFORT if REASONING_EFFORT not in ("", "none") else None
        # Cleared permanently if this model rejects the parameter.
        self._supports_reasoning_effort = True
        logger.info(
            "PersonaEngine model=%s temperature=%.2f max_tokens=%d reasoning_effort=%s",
            model,
            TEMPERATURE,
            MAX_COMPLETION_TOKENS,
            self._reasoning_effort or "(unset)",
        )

    def _system_prompt(self, scenario: Scenario) -> str:
        edge_case_line = (
            f"Edge case to actively probe for: {scenario.edge_case}" if scenario.edge_case else ""
        )
        facts = scenario.persona.facts or {}
        volunteers = scenario.persona.volunteers or []
        facts_block = ""
        if facts:
            listed = "\n".join(f"- {name.replace('_', ' ')}: {value}" for name, value in facts.items())
            facts_block = f"\nYour details, to be given exactly as written:\n{listed}\n"
        return SYSTEM_PROMPT.format(
            identity=scenario.persona.identity,
            voice_style=scenario.persona.voice_style,
            facts_block=facts_block,
            goal=scenario.goal,
            edge_case_line=edge_case_line,
            intended_outcome=scenario.intended_outcome,
            end_token=END_TOKEN,
        )

    def build_messages(
        self,
        scenario: Scenario,
        history: list[dict],
        latest_agent_utterance: Optional[str],
        partial: bool = False,
        repeated: bool = False,
        partial_attempt: int = 1,
    ) -> list[dict]:
        """Assemble the full call so far, oldest turn first, then the newest line.

        `history` is the complete conversation excluding the utterance being replied
        to — it is never truncated, so the model always sees every fact it has
        already given. `partial` appends a transient instruction (not part of the
        conversation, and never written back into history) telling the model the
        last line arrived cut off.
        """
        messages = [{"role": "system", "content": self._system_prompt(scenario)}]
        for turn in history:
            role = "assistant" if turn["speaker"] == "patient" else "user"
            messages.append({"role": role, "content": turn["text"]})
        if latest_agent_utterance is not None:
            messages.append({"role": "user", "content": latest_agent_utterance})
        if partial:
            messages.append(
                {"role": "system", "content": partial_utterance_nudge(partial_attempt)}
            )
        if repeated:
            messages.append({"role": "system", "content": AGENT_REPEATED_NUDGE})
        return messages

    async def next_line(
        self,
        scenario: Scenario,
        history: list[dict],
        latest_agent_utterance: Optional[str] = None,
        partial: bool = False,
        repeated: bool = False,
        partial_attempt: int = 1,
    ) -> tuple[str, bool, list[dict]]:
        messages = self.build_messages(
            scenario, history, latest_agent_utterance, partial, repeated, partial_attempt
        )
        # The model intermittently returns nothing at all — 5 of 13 turns on one
        # observed call — and every blank turn costs the patient a canned fallback
        # line. A re-roll usually produces a usable reply and costs about half a
        # second, so it is far cheaper than the fallback.
        facts = scenario.persona.facts or {}
        volunteers = scenario.persona.volunteers or []
        text, should_end = "", False
        started = time.monotonic()
        attempts = 0
        for _ in range(EMPTY_REPLY_RETRIES + 1):
            attempts += 1
            text, should_end = await self._generate(
                messages, history, facts, latest_agent_utterance, volunteers
            )
            if text or should_end:
                break
        logger.info(
            "llm_turn model=%s temp=%.2f partial=%s repeated=%s history_turns=%d "
            "attempts=%d elapsed=%.2fs empty=%s",
            self._model,
            TEMPERATURE,
            partial,
            repeated,
            len(history),
            attempts,
            time.monotonic() - started,
            not text,
        )
        return text, should_end, messages

    async def _complete(self, messages: list[dict]):
        """One completion, with reasoning kept out of the patient's token budget.

        `reasoning_effort` is not accepted by every Groq-hosted model, so an outright
        rejection disables it for the rest of the call rather than failing the turn.
        """
        kwargs = dict(
            model=self._model,
            messages=messages,
            temperature=TEMPERATURE,
            max_tokens=MAX_COMPLETION_TOKENS,
        )
        if self._reasoning_effort and self._supports_reasoning_effort:
            kwargs["reasoning_effort"] = self._reasoning_effort
        try:
            return await self._client.chat.completions.create(**kwargs)
        except Exception as exc:
            if "reasoning_effort" not in str(exc) or not self._supports_reasoning_effort:
                raise
            logger.warning(
                "%s rejected reasoning_effort; continuing without it", self._model
            )
            self._supports_reasoning_effort = False
            kwargs.pop("reasoning_effort", None)
            return await self._client.chat.completions.create(**kwargs)

    async def _generate(
        self,
        messages: list[dict],
        history: list[dict],
        facts: Optional[dict] = None,
        asked: Optional[str] = None,
        volunteers: Optional[list] = None,
    ) -> tuple[str, bool]:
        response = await self._complete(messages)
        choice = response.choices[0]
        text = (choice.message.content or "").strip()
        if not text and choice.finish_reason == "length":
            # The reasoning consumed the whole budget before any speech was emitted.
            # Visible so it is never again mistaken for the model "just being flaky".
            logger.warning(
                "Blank reply: finish_reason=length, %s completion tokens spent on "
                "reasoning. Raise MAX_COMPLETION_TOKENS or lower GROQ_REASONING_EFFORT.",
                getattr(response.usage, "completion_tokens", "?"),
            )
        # A bare "[[" with no END/CALL word is an aborted token, not an instruction to
        # hang up: it is scrubbed below, but it must not end the call on its own.
        should_end = END_TOKEN in text or bool(END_TOKEN_RE.search(text))
        text = BRACKET_JUNK_RE.sub(" ", END_TOKEN_RE.sub(" ", text))
        text = re.sub(r"\s{2,}", " ", text).strip()
        # The model sometimes jams restatements together with no space ("1 pm.Sure,
        # I'm free..."); TTS reads that as one run-on word, so split them apart.
        text = RUN_ON_RE.sub(" ", text)
        text = _drop_repeated_sentences(text)
        text = _drop_sentences_already_said(text, history, facts)
        text = _drop_unrequested_facts(text, asked, facts, volunteers)
        return _cap_length(text), should_end
