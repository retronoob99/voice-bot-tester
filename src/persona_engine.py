from __future__ import annotations

from typing import Optional

from groq import AsyncGroq

from .scenario import Scenario

END_TOKEN = "[[END_CALL]]"

SYSTEM_PROMPT = """You are role-playing as a patient on a phone call to a medical clinic's \
voice agent, for the purpose of QA-testing that agent. Stay fully in character.

Persona: {identity}
Speaking style: {voice_style}

Your goal for this call: {goal}
{edge_case_line}
What a successful test of this scenario looks like: {intended_outcome}

Rules:
- Output ONLY the words the patient says out loud for this one turn. No stage \
directions, narration, or quotation marks.
- Stay in character as the patient at all times; never mention you are an AI or a test.
- Actively steer the conversation toward your goal (and the edge case, if any) \
instead of just passively answering whatever you're asked.
- Keep each line natural and phone-call-length, usually one to three sentences.
- Once your goal has been tested to a natural conclusion, end your final line with \
the token {end_token} by itself at the very end so the call can be wrapped up.
"""


class PersonaEngine:
    def __init__(self, api_key: str, model: str):
        self._client = AsyncGroq(api_key=api_key)
        self._model = model

    def _system_prompt(self, scenario: Scenario) -> str:
        edge_case_line = (
            f"Edge case to actively probe for: {scenario.edge_case}" if scenario.edge_case else ""
        )
        return SYSTEM_PROMPT.format(
            identity=scenario.persona.identity,
            voice_style=scenario.persona.voice_style,
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
    ) -> list[dict]:
        messages = [{"role": "system", "content": self._system_prompt(scenario)}]
        for turn in history:
            role = "assistant" if turn["speaker"] == "patient" else "user"
            messages.append({"role": role, "content": turn["text"]})
        if latest_agent_utterance is not None:
            messages.append({"role": "user", "content": latest_agent_utterance})
        return messages

    async def next_line(
        self,
        scenario: Scenario,
        history: list[dict],
        latest_agent_utterance: Optional[str] = None,
    ) -> tuple[str, bool, list[dict]]:
        messages = self.build_messages(scenario, history, latest_agent_utterance)
        response = await self._client.chat.completions.create(
            model=self._model,
            messages=messages,
            temperature=0.8,
            max_tokens=200,
        )
        text = (response.choices[0].message.content or "").strip()
        should_end = END_TOKEN in text
        text = text.replace(END_TOKEN, "").strip()
        return text, should_end, messages
