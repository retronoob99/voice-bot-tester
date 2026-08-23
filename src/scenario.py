from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Optional

import yaml

SCENARIOS_DIR = Path(__file__).resolve().parent.parent / "scenarios"


@dataclass
class Persona:
    identity: str
    voice_style: str = "natural, conversational"
    voice: str = "aura-2-asteria-en"


@dataclass
class Scenario:
    name: str
    persona: Persona
    goal: str
    intended_outcome: str
    edge_case: Optional[str] = None
    opening_line: Optional[str] = None
    max_turns: int = 20


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
