from __future__ import annotations

import asyncio
import json
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional
from xml.sax.saxutils import quoteattr

import httpx
from twilio.rest import Client as TwilioClient

from . import bug_analyzer
from .config import settings
from .scenario import find_scenario

ALLOWED_TARGET_NUMBER = "+18054398008"

REPO_ROOT = Path(__file__).resolve().parent.parent
CALLS_DIR = REPO_ROOT / "calls"
REPORTS_DIR = REPO_ROOT / "reports"

TERMINAL_STATUSES = {"completed", "busy", "failed", "no-answer", "canceled"}


def _build_twiml(ws_url: str, scenario_name: str, call_uid: str) -> str:
    stream_url = ws_url.rstrip("/") + "/media-stream"
    return (
        '<?xml version="1.0" encoding="UTF-8"?>'
        "<Response><Connect><Stream url=" + quoteattr(stream_url) + ">"
        '<Parameter name="scenario" value=' + quoteattr(scenario_name) + "/>"
        '<Parameter name="call_uid" value=' + quoteattr(call_uid) + "/>"
        "</Stream></Connect></Response>"
    )


def place_call(scenario_name: str) -> tuple[str, str]:
    settings.require("twilio_account_sid", "twilio_auth_token", "twilio_phone_number", "public_ws_url")

    if settings.target_phone_number != ALLOWED_TARGET_NUMBER:
        raise RuntimeError(
            f"Refusing to call {settings.target_phone_number!r}: this project is only allowed "
            f"to dial {ALLOWED_TARGET_NUMBER}. Fix TARGET_PHONE_NUMBER in your .env."
        )

    find_scenario(scenario_name)  # raises if the scenario doesn't exist

    call_uid = (
        f"{scenario_name}_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}_"
        f"{uuid.uuid4().hex[:6]}"
    )
    twiml = _build_twiml(settings.public_ws_url, scenario_name, call_uid)

    client = TwilioClient(settings.twilio_account_sid, settings.twilio_auth_token)
    call = client.calls.create(
        to=ALLOWED_TARGET_NUMBER,
        from_=settings.twilio_phone_number,
        twiml=twiml,
        record=True,
    )
    return call.sid, call_uid


def wait_for_completion(call_sid: str, poll_interval: float = 5.0, timeout: float = 600.0) -> str:
    settings.require("twilio_account_sid", "twilio_auth_token")
    client = TwilioClient(settings.twilio_account_sid, settings.twilio_auth_token)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        call = client.calls(call_sid).fetch()
        if call.status in TERMINAL_STATUSES:
            return call.status
        time.sleep(poll_interval)
    raise TimeoutError(f"Call {call_sid} did not reach a terminal status within {timeout}s")


def download_recording(call_sid: str, dest_dir: Path) -> Optional[Path]:
    settings.require("twilio_account_sid", "twilio_auth_token")
    client = TwilioClient(settings.twilio_account_sid, settings.twilio_auth_token)
    recordings = client.recordings.list(call_sid=call_sid, limit=1)
    if not recordings:
        return None
    recording = recordings[0]
    media_url = f"https://api.twilio.com{recording.uri.removesuffix('.json')}.mp3"
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest_path = dest_dir / "recording.mp3"
    with httpx.stream(
        "GET", media_url, auth=(settings.twilio_account_sid, settings.twilio_auth_token)
    ) as response:
        response.raise_for_status()
        with dest_path.open("wb") as f:
            for chunk in response.iter_bytes():
                f.write(chunk)
    return dest_path


async def _run_bug_analysis(scenario_name: str, call_uid: str) -> None:
    settings.require("groq_api_key")
    scenario = find_scenario(scenario_name)
    call_dir = CALLS_DIR / call_uid
    transcript_path = call_dir / "transcript.json"
    if not transcript_path.exists():
        raise RuntimeError(
            f"No transcript found at {transcript_path}; the media bridge may not have logged this call."
        )
    turns = json.loads(transcript_path.read_text(encoding="utf-8"))["turns"]
    bugs = await bug_analyzer.analyze_call(
        settings.groq_api_key, settings.groq_model, scenario, call_uid, turns
    )
    bug_analyzer.write_call_bugs(call_dir, bugs)
    bug_analyzer.append_bug_report(REPORTS_DIR, call_uid, scenario, bugs)


def run_call(scenario_name: str, wait: bool = False) -> None:
    call_sid, call_uid = place_call(scenario_name)
    print(f"Placed call {call_sid} (call_uid={call_uid}) for scenario '{scenario_name}'.")
    if not wait:
        return

    status = wait_for_completion(call_sid)
    print(f"Call {call_sid} finished with status: {status}")

    call_dir = CALLS_DIR / call_uid
    recording_path = download_recording(call_sid, call_dir)
    print(f"Recording saved to {recording_path}" if recording_path else "No recording found for this call.")

    asyncio.run(_run_bug_analysis(scenario_name, call_uid))
    print(f"Bug analysis written to {call_dir / 'bugs.json'} and {REPORTS_DIR / 'bug_report.md'}")
