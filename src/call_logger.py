from __future__ import annotations

import json
import time
from datetime import datetime, timezone
from pathlib import Path


class CallLogger:
    def __init__(self, base_dir: Path, call_uid: str, scenario_name: str):
        self.dir = Path(base_dir) / call_uid
        self.dir.mkdir(parents=True, exist_ok=True)
        self.call_uid = call_uid
        self.scenario_name = scenario_name
        self.turns: list[dict] = []
        self.prompts: list[dict] = []
        self.timings: list[dict] = []

    def log_timing(self, event: str, **fields) -> None:
        """Append one timing datapoint to calls/<uid>/timing.json.

        Written so a finished call can be audited for tempo: each entry carries both
        a wall-clock timestamp and a monotonic one, because wall-clock is what lines
        up with the transcript while monotonic is what durations should be computed
        from.
        """
        self.timings.append(
            {
                "event": event,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "monotonic": round(time.monotonic(), 3),
                **fields,
            }
        )
        (self.dir / "timing.json").write_text(
            json.dumps(self.timings, indent=2), encoding="utf-8"
        )

    def log_turn(self, speaker: str, text: str) -> None:
        self.turns.append(
            {
                "speaker": speaker,
                "text": text,
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }
        )
        self._save_transcript()

    def log_prompt(self, label: str, messages: list[dict], response_text: str) -> None:
        self.prompts.append(
            {
                "label": label,
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "messages": messages,
                "response": response_text,
            }
        )
        (self.dir / "prompts.json").write_text(
            json.dumps(self.prompts, indent=2), encoding="utf-8"
        )

    def _save_transcript(self) -> None:
        (self.dir / "transcript.json").write_text(
            json.dumps(
                {"call_uid": self.call_uid, "scenario": self.scenario_name, "turns": self.turns},
                indent=2,
            ),
            encoding="utf-8",
        )
        lines = [f"[{t['timestamp']}] {t['speaker'].upper()}: {t['text']}" for t in self.turns]
        (self.dir / "transcript.txt").write_text("\n".join(lines), encoding="utf-8")
