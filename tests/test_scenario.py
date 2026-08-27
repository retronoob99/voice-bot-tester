"""Scenario files are the test cases, so a malformed one should fail loudly."""

from __future__ import annotations

import pytest

from src.scenario import SCENARIOS_DIR, find_scenario, list_scenarios, load_scenario


def test_every_shipped_scenario_loads():
    names = list_scenarios()
    assert names, "no scenarios found"
    for name in names:
        find_scenario(name)


@pytest.mark.parametrize("name", list_scenarios())
def test_scenario_has_a_concrete_pass_condition(name):
    """Hard rule #4: a 'pass' has to be judged against something specific."""
    scenario = find_scenario(name)
    assert scenario.goal.strip()
    assert scenario.intended_outcome.strip()
    assert scenario.persona.identity.strip()
    assert scenario.max_turns > 0


def test_missing_scenario_lists_the_available_ones():
    with pytest.raises(FileNotFoundError, match="Available scenarios"):
        find_scenario("definitely_not_a_scenario")


def test_loads_optional_fields(tmp_path):
    path = tmp_path / "custom.yaml"
    path.write_text(
        "name: custom\n"
        "goal: do a thing\n"
        "intended_outcome: the thing happens\n"
        "edge_case: something tricky\n"
        "opening_line: Hi there.\n"
        "max_turns: 5\n"
        "persona:\n"
        "  identity: a patient\n"
        "  voice: aura-2-asteria-en\n",
        encoding="utf-8",
    )

    scenario = load_scenario(path)

    assert scenario.edge_case == "something tricky"
    assert scenario.opening_line == "Hi there."
    assert scenario.max_turns == 5
    assert scenario.persona.voice == "aura-2-asteria-en"


def test_scenarios_dir_is_the_repo_one():
    assert SCENARIOS_DIR.name == "scenarios"
    assert SCENARIOS_DIR.is_dir()


def test_patient_speech_rate_is_in_the_usable_band():
    """Measured speech-only rates: 1.0 ~174 WPM, 0.8 ~147, 0.75 ~124, 0.7 ~107.

    Above 0.85 the knob barely moves the rate, and Deepgram rejects below 0.7
    outright with a 400, so the shipped value has to sit inside that band.
    """
    from src.scenario import Persona

    assert 0.7 <= Persona(identity="x").speech_speed <= 0.85


def test_speech_speed_is_configurable_per_scenario(tmp_path):
    path = tmp_path / "slow.yaml"
    path.write_text(
        "name: slow\ngoal: g\nintended_outcome: o\n"
        "persona:\n  identity: a patient\n  speech_speed: 0.8\n",
        encoding="utf-8",
    )

    assert load_scenario(path).persona.speech_speed == 0.8


def test_persona_facts_are_defined_for_shipped_scenarios():
    """Without fixed details the model invents a new date of birth every call.

    That makes the agent's record lookup impossible and the scenario unrepeatable.
    """
    for name in list_scenarios():
        facts = find_scenario(name).persona.facts
        assert facts, f"{name} has no persona facts"
        assert "date_of_birth" in facts, f"{name} is missing a date of birth"


def test_facts_default_to_empty_and_load_from_yaml(tmp_path):
    from src.scenario import Persona

    assert Persona(identity="x").facts == {}

    path = tmp_path / "f.yaml"
    path.write_text(
        "name: f\ngoal: g\nintended_outcome: o\n"
        "persona:\n  identity: a patient\n  facts:\n    date_of_birth: March 12, 1991\n",
        encoding="utf-8",
    )
    assert load_scenario(path).persona.facts["date_of_birth"] == "March 12, 1991"
