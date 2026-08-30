from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import yaml

SCENARIOS_DIR = Path(__file__).resolve().parent.parent / "scenarios"


@dataclass
class Persona:
    identity: str
    voice_style: str = "natural, conversational"
    voice: str = "aura-2-asteria-en"
    # Concrete details the patient may be asked for. Without these the model invents
    # a different date of birth on every call, so the agent can never match a record
    # and the same scenario is not reproducible run to run.
    facts: dict = field(default_factory=dict)
    # Fact names this scenario NEEDS the patient to offer unprompted. Everything in
    # `facts` is otherwise withheld until the agent asks, so that the transcript shows
    # whether it asks at all. Some scenarios invert that: context_retention has to
    # mention its penicillin allergy mid-call before it can test whether the agent
    # remembers it, so "allergy" is listed here and exempted from that filtering.
    volunteers: list = field(default_factory=list)
    # Aura's rate multiplier, measured with ffmpeg silence detection so the figures
    # below are speech only, ignoring the padding Aura leaves around a clip:
    #   1.0 ~174 WPM   0.85 ~152   0.8 ~147   0.75 ~124   0.7 ~107
    # The knob does almost nothing above 0.85, and Deepgram rejects anything under
    # 0.7 outright (HTTP 400).
    #
    # Raised from 0.75 after a live call where the agent started its next question
    # while the patient was still mid-line, heard the patient, and abandoned its own
    # sentence. 0.75 (~124 WPM) is noticeably slower than natural speech and stretched
    # every line by about a fifth, widening that window for no benefit. 0.85 (~152
    # WPM) is normal conversational pace and shortens each line accordingly. Lower it
    # per-scenario if a particular agent's STT struggles with the faster delivery.
    speech_speed: float = 0.85


@dataclass
class Scenario:
    name: str
    persona: Persona
    goal: str
    intended_outcome: str
    edge_case: Optional[str] = None
    opening_line: Optional[str] = None
    max_turns: int = 20
    # A single question the patient must ask LATE in the call, withheld from the
    # persona prompt until `closing_probe_after_turns` patient turns have passed.
    # Putting a late question in `goal` does not work - the model sees the whole
    # goal every turn and front-loads it.
    closing_probe: Optional[str] = None
    closing_probe_after_turns: int = 4


def load_scenario(path: Path) -> Scenario:
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    persona = Persona(**data.get("persona", {}))
    return Scenario(
        name=data["name"],
        persona=persona,
        goal=data["goal"],
        intended_outcome=data["intended_outcome"],
        edge_case=data.get("edge_case"),
        opening_line=data.get("opening_line"),
        max_turns=int(data.get("max_turns", 20)),
        closing_probe=data.get("closing_probe"),
        closing_probe_after_turns=int(data.get("closing_probe_after_turns", 4)),
    )


def find_scenario(name: str) -> Scenario:
    path = SCENARIOS_DIR / f"{name}.yaml"
    if not path.exists():
        available = sorted(p.stem for p in SCENARIOS_DIR.glob("*.yaml"))
        raise FileNotFoundError(
            f"No scenario file at {path}. Available scenarios: {', '.join(available) or '(none)'}"
        )
    return load_scenario(path)


def list_scenarios() -> list[str]:
    return sorted(p.stem for p in SCENARIOS_DIR.glob("*.yaml"))
