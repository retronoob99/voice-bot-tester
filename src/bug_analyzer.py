from __future__ import annotations

import json
from pathlib import Path

from groq import AsyncGroq

from .scenario import Scenario

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

Respond with ONLY a JSON array (no prose, no markdown fences). Each element:
{"bug": "<short title>", "severity": "High" | "Medium" | "Low", "details": "<what happened \
vs. what should have happened, with the relevant transcript excerpt>"}

If no bugs are found, respond with an empty JSON array: []
"""


def _build_prompt(scenario: Scenario, turns: list[dict]) -> str:
    transcript_text = "\n".join(f"{t['speaker'].upper()}: {t['text']}" for t in turns)
    return (
        f"Scenario: {scenario.name}\n"
        f"Goal: {scenario.goal}\n"
        f"Edge case: {scenario.edge_case or 'none'}\n"
        f"Intended outcome: {scenario.intended_outcome}\n\n"
        f"Transcript:\n{transcript_text}\n\n"
        f"{RUBRIC}"
    )


async def analyze_call(
    api_key: str, model: str, scenario: Scenario, call_uid: str, turns: list[dict]
) -> list[dict]:
    client = AsyncGroq(api_key=api_key)
    prompt = _build_prompt(scenario, turns)
    response = await client.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": prompt}],
        temperature=0.2,
        max_tokens=2000,
    )
    raw = (response.choices[0].message.content or "").strip()
    raw = raw.removeprefix("```json").removeprefix("```").removesuffix("```").strip()
    try:
        bugs = json.loads(raw)
    except json.JSONDecodeError:
        bugs = [{"bug": "Bug analyzer returned unparsable output", "severity": "Low", "details": raw}]
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
