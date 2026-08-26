from __future__ import annotations

import asyncio
import json
import logging
import os
import re
from pathlib import Path
from typing import Optional

from groq import AsyncGroq

from .scenario import Scenario

logger = logging.getLogger("bug_analyzer")


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        logger.warning("%s=%r is not an integer; using %s", name, raw, default)
        return default

# Retries when the model returns an empty or unparsable completion. A lost analysis
# silently costs a whole call's findings, which is far more expensive than a retry.
ANALYSIS_ATTEMPTS = 3

# The analyser hit the same reasoning-token starvation as the persona engine: one
# live call came back completely empty and its findings were lost. Analysis is not
# latency-sensitive, so it gets a much larger budget and keeps a real reasoning
# effort — judging a transcript against a rubric is exactly what reasoning is for.
# Sized for Groq's free tier, which bills prompt + max_tokens against a per-minute
# budget: at 8192 a single request was rejected outright ("Limit 8000, Requested
# 9028"). A transcript analysis has never needed more than a few hundred output
# tokens, so this is generous while still leaving room for the prompt and a retry.
ANALYSIS_MAX_TOKENS = _env_int("ANALYSIS_MAX_TOKENS", 2500)

# Rate limits are per minute, so a retry that fires immediately just fails again.
RATE_LIMIT_BACKOFF_S = 20.0
ANALYSIS_REASONING_EFFORT = os.environ.get("GROQ_ANALYSIS_REASONING_EFFORT", "medium").strip().lower()

RUBRIC = """Review this phone call transcript between a simulated patient (PATIENT) and a \
clinic's voice agent (AGENT) for bugs. Judge the agent against the scenario's intended \
outcome. Check specifically for:
- Scheduling logic errors
- Stated office hours vs. actual behavior (did it apply its own stated policy correctly?)
- Refill-safety (did it refill/approve anything it shouldn't have, without proper checks?)
- Insurance accuracy
- Hallucination (making up facts not established in the call)
- Repetition or getting stuck in a loop
- Failure to escalate to a human when it should have
- Excessive latency or unnatural turn-taking (only note if evident from the transcript)
- Talking over the caller: starting a question while the patient is still speaking, then abandoning it. This shows up as an AGENT line that stops mid-sentence or mid-word ("Just to con", "Can you tell me your date of") with the agent's NEXT line starting a different thought rather than finishing that one. Report it as a turn-taking bug against the agent.
- Losing information the patient already gave, or asking for the same detail twice
- Truncating or mangling a detail it reads back (e.g. confirming a full name as only a first name)

Attribution rules — be careful whose bug it is:
- Bracketed markers in PATIENT lines describe the TEST HARNESS, not the agent. "[cut off by agent]" means the agent talked over the patient; "[not spoken: TTS failed]" and "[call ended mid-line]" are harness problems and must NEVER be reported as agent bugs.
- A PATIENT line asking the agent to repeat itself is evidence the agent's speech arrived incomplete. Judge whether the agent broke off, and do not report the patient for asking.
- Do not report the agent for anything the patient did.

Respond with ONLY a JSON array (no prose, no markdown fences). Each element:
{"bug": "<short title>", "severity": "High" | "Medium" | "Low", "details": "<what happened \
vs. what should have happened, with the relevant transcript excerpt>"}

If no bugs are found, respond with an empty JSON array: []
"""


def _truncated_agent_turns(turns: list[dict]) -> list[str]:
    """Agent lines that stop mid-sentence, with the line that followed them.

    Handed to the analyser as explicit evidence rather than left for it to notice.
    A fragment only counts when the agent's NEXT line starts a new thought instead
    of finishing this one -- otherwise it is just Deepgram splitting one sentence.
    """
    agent_indexes = [i for i, t in enumerate(turns) if t.get("speaker") == "agent"]
    findings = []
    for position, index in enumerate(agent_indexes):
        text = turns[index].get("text", "").strip()
        if not text or text[-1] in ".!?":
            continue
        following = ""
        if position + 1 < len(agent_indexes):
            following = turns[agent_indexes[position + 1]].get("text", "").strip()
        findings.append(
            '- AGENT broke off at: "' + text + '" -> next AGENT line: "'
            + (following or "(none)") + '"'
        )
    return findings


def _build_prompt(scenario: Scenario, turns: list[dict]) -> str:
    transcript_text = "\n".join(f"{t['speaker'].upper()}: {t['text']}" for t in turns)
    truncations = _truncated_agent_turns(turns)
    evidence = ""
    if truncations:
        evidence = (
            "\nTurn-taking evidence collected automatically (agent lines that ended "
            "mid-sentence):\n" + "\n".join(truncations) + "\n"
        )
    return (
        f"Scenario: {scenario.name}\n"
        f"Goal: {scenario.goal}\n"
        f"Edge case: {scenario.edge_case or 'none'}\n"
        f"Intended outcome: {scenario.intended_outcome}\n\n"
        f"Transcript:\n{transcript_text}\n"
        f"{evidence}\n"
        f"{RUBRIC}"
    )


def _parse_bugs(raw: str) -> Optional[list[dict]]:
    """Pull the JSON array out of a model response, or None if there isn't one.

    The model wraps the array in fences, in prose, or in both, so the fences are
    stripped and then the outermost [...] is located rather than trusting the whole
    response to be valid JSON.
    """
    text = raw.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
        text = re.sub(r"\s*```$", "", text).strip()
    if not text:
        return None
    start, end = text.find("["), text.rfind("]")
    if start == -1 or end <= start:
        return None
    try:
        parsed = json.loads(text[start : end + 1])
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, list) else None


async def analyze_call(
    api_key: str, model: str, scenario: Scenario, call_uid: str, turns: list[dict]
) -> list[dict]:
    client = AsyncGroq(api_key=api_key)
    prompt = _build_prompt(scenario, turns)
    bugs: Optional[list[dict]] = None
    raw = ""
    # This model returns an empty completion often enough to matter, and a lost
    # analysis means a whole call's findings never reach the report. Observed live:
    # one call produced a single "unparsable output" entry with empty details and no
    # findings at all, despite four real bugs in the transcript.
    for attempt in range(ANALYSIS_ATTEMPTS):
        kwargs = dict(
            model=model,
            messages=[{"role": "user", "content": prompt}],
            temperature=0.2,
            max_tokens=ANALYSIS_MAX_TOKENS,
        )
        if ANALYSIS_REASONING_EFFORT not in ("", "none"):
            kwargs["reasoning_effort"] = ANALYSIS_REASONING_EFFORT
        try:
            response = await client.chat.completions.create(**kwargs)
        except Exception as exc:
            message = str(exc)
            if "reasoning_effort" in message:
                kwargs.pop("reasoning_effort", None)
                logger.warning("%s rejected reasoning_effort; retrying without it", model)
                continue
            if "rate_limit" in message or "429" in message or "413" in message:
                # Per-minute budget: an immediate retry just fails again. The call is
                # already recorded, so waiting costs nothing but wall clock.
                logger.warning(
                    "Rate limited on attempt %d/%d; waiting %.0fs. %s",
                    attempt + 1, ANALYSIS_ATTEMPTS, RATE_LIMIT_BACKOFF_S, message[:180],
                )
                raw = message
                await asyncio.sleep(RATE_LIMIT_BACKOFF_S)
                continue
            # Anything else: record it and move on. The recording and transcript are
            # already on disk, and losing them to an analysis error would be absurd.
            logger.exception("Bug analysis attempt %d failed", attempt + 1)
            raw = message
            continue
        choice = response.choices[0]
        raw = (choice.message.content or "").strip()
        if not raw and choice.finish_reason == "length":
            logger.warning(
                "Analysis ran out of tokens before answering (%s spent on reasoning); "
                "raise ANALYSIS_MAX_TOKENS.",
                getattr(response.usage, "completion_tokens", "?"),
            )
        bugs = _parse_bugs(raw)
        if bugs is not None:
            break
        logger.warning(
            "Bug analysis attempt %d/%d returned nothing usable (%d chars)",
            attempt + 1,
            ANALYSIS_ATTEMPTS,
            len(raw),
        )

    if bugs is None:
        # Never silently drop a call. This entry is explicitly about the harness, and
        # is worded so it cannot be mistaken for a finding against the agent.
        logger.error("Bug analysis failed for %s after %d attempts", call_uid, ANALYSIS_ATTEMPTS)
        bugs = [
            {
                "bug": "HARNESS: bug analysis failed, transcript not reviewed",
                "severity": "Low",
                "details": (
                    "The analyser returned no usable JSON after "
                    f"{ANALYSIS_ATTEMPTS} attempts, so this call was never reviewed. "
                    "This is a harness failure, not an agent bug — review "
                    f"transcript.txt for {call_uid} by hand. "
                    f"Last raw response: {raw[:500]!r}"
                ),
            }
        ]

    for bug in bugs:
        bug["call_uid"] = call_uid
        bug["scenario"] = scenario.name
    return bugs


def write_call_bugs(call_dir: Path, bugs: list[dict]) -> None:
    (call_dir / "bugs.json").write_text(json.dumps(bugs, indent=2), encoding="utf-8")


def append_bug_report(reports_dir: Path, call_uid: str, scenario: Scenario, bugs: list[dict]) -> None:
    reports_dir.mkdir(parents=True, exist_ok=True)
    report_path = reports_dir / "bug_report.md"
    lines = [f"\n## Call: {call_uid} (scenario: {scenario.name})\n"]
    if not bugs:
        lines.append("- No bugs found.\n")
    for bug in bugs:
        lines.append(
            f"- **Bug**: {bug.get('bug', 'unknown')}\n"
            f"  **Severity**: {bug.get('severity', 'unknown')}\n"
            f"  **Call**: {call_uid}\n"
            f"  **Details**: {bug.get('details', '')}\n"
        )
    with report_path.open("a", encoding="utf-8") as f:
        f.write("\n".join(lines))
        f.write("\n")
