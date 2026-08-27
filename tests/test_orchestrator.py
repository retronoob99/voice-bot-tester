"""Outbound-call safety rules and the TwiML handed to Twilio."""

from __future__ import annotations

from dataclasses import replace

import httpx
import pytest

from src import orchestrator
from src.orchestrator import ALLOWED_TARGET_NUMBER, _build_twiml


class _FakeCalls:
    def __init__(self):
        self.created: list[dict] = []

    def create(self, **kwargs):
        self.created.append(kwargs)
        return type("Call", (), {"sid": "CAtest"})()


class _FakeTwilioClient:
    last: "_FakeTwilioClient | None" = None

    def __init__(self, *_args, **_kwargs):
        self.calls = _FakeCalls()
        _FakeTwilioClient.last = self


@pytest.fixture
def configured(monkeypatch):
    """orchestrator.settings with credentials filled in; target number is per-test."""

    def _apply(target_phone_number: str):
        _FakeTwilioClient.last = None  # class attribute would otherwise leak between tests
        monkeypatch.setattr(
            orchestrator,
            "settings",
            replace(
                orchestrator.settings,
                twilio_account_sid="AC123",
                twilio_auth_token="token",
                twilio_phone_number="+15550000000",
                public_ws_url="wss://example.ngrok-free.dev",
                target_phone_number=target_phone_number,
            ),
        )
        monkeypatch.setattr(orchestrator, "TwilioClient", _FakeTwilioClient)

    return _apply


def test_refuses_to_dial_any_other_number(configured):
    """Hard rule #1: this project may only ever call the assigned test line."""
    configured("+15551234567")

    with pytest.raises(RuntimeError, match="Refusing to call"):
        orchestrator.place_call("simple_schedule")

    assert _FakeTwilioClient.last is None, "a Twilio client was built despite the refusal"


def test_dials_the_hardcoded_number_not_the_configured_one(configured):
    """Even when env agrees, the number placed must come from the constant."""
    configured(ALLOWED_TARGET_NUMBER)

    call_sid, call_uid = orchestrator.place_call("simple_schedule")

    created = _FakeTwilioClient.last.calls.created
    assert len(created) == 1
    assert created[0]["to"] == ALLOWED_TARGET_NUMBER == "+18054398008"
    assert created[0]["record"] is True, "hard rule #2: every call needs a recording"
    assert call_sid == "CAtest"
    assert call_uid.startswith("simple_schedule_")


def test_unknown_scenario_fails_before_dialling(configured):
    configured(ALLOWED_TARGET_NUMBER)

    with pytest.raises(FileNotFoundError):
        orchestrator.place_call("no_such_scenario")

    assert _FakeTwilioClient.last is None, "dialled before validating the scenario"


class _FakeResponse:
    def __init__(self, status_code: int, body: bytes = b""):
        self.status_code = status_code
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("boom", request=None, response=self)

    def iter_bytes(self):
        yield self._body


def test_recording_download_waits_for_twilio_to_finish_encoding(monkeypatch, tmp_path):
    """The resource exists before the media does; a bare 404 loses the audio."""
    monkeypatch.setattr(
        orchestrator,
        "settings",
        replace(orchestrator.settings, twilio_account_sid="AC1", twilio_auth_token="t"),
    )

    class _Recording:
        uri = "/2010-04-01/Accounts/AC1/Recordings/RE1.json"

    class _Client:
        def __init__(self, *_a, **_kw):
            self.recordings = type("R", (), {"list": lambda *_a, **_kw: [_Recording()]})()

    monkeypatch.setattr(orchestrator, "TwilioClient", _Client)
    monkeypatch.setattr(orchestrator.time, "sleep", lambda _s: None)

    responses = [_FakeResponse(404), _FakeResponse(404), _FakeResponse(200, b"ID3audio")]
    monkeypatch.setattr(orchestrator.httpx, "stream", lambda *_a, **_kw: responses.pop(0))

    path = orchestrator.download_recording(
        "CA1", tmp_path / "call", attempts=5, delay=0
    )

    assert path is not None and path.read_bytes() == b"ID3audio"
    assert not responses, "should have stopped as soon as the media was ready"


def test_recording_download_gives_up_and_reports_nothing(monkeypatch, tmp_path):
    monkeypatch.setattr(
        orchestrator,
        "settings",
        replace(orchestrator.settings, twilio_account_sid="AC1", twilio_auth_token="t"),
    )

    class _Client:
        def __init__(self, *_a, **_kw):
            self.recordings = type("R", (), {"list": lambda *_a, **_kw: []})()

    monkeypatch.setattr(orchestrator, "TwilioClient", _Client)
    monkeypatch.setattr(orchestrator.time, "sleep", lambda _s: None)

    assert orchestrator.download_recording("CA1", tmp_path, attempts=2, delay=0) is None


def test_recording_download_reraises_real_errors(monkeypatch, tmp_path):
    """A 401 is a broken setup, not a race; retrying just hides it."""
    monkeypatch.setattr(
        orchestrator,
        "settings",
        replace(orchestrator.settings, twilio_account_sid="AC1", twilio_auth_token="t"),
    )

    class _Recording:
        uri = "/2010-04-01/Accounts/AC1/Recordings/RE1.json"

    class _Client:
        def __init__(self, *_a, **_kw):
            self.recordings = type("R", (), {"list": lambda *_a, **_kw: [_Recording()]})()

    monkeypatch.setattr(orchestrator, "TwilioClient", _Client)
    monkeypatch.setattr(orchestrator.httpx, "stream", lambda *_a, **_kw: _FakeResponse(401))

    with pytest.raises(httpx.HTTPStatusError):
        orchestrator.download_recording("CA1", tmp_path, attempts=3, delay=0)


def test_twiml_points_at_the_media_stream_endpoint():
    twiml = _build_twiml("wss://example.ngrok-free.dev", "simple_schedule", "uid123")

    assert 'url="wss://example.ngrok-free.dev/media-stream"' in twiml
    assert 'name="scenario" value="simple_schedule"' in twiml
    assert 'name="call_uid" value="uid123"' in twiml


def test_twiml_does_not_double_the_path():
    """A PUBLIC_WS_URL that already includes the path 403s and yields no transcript."""
    twiml = _build_twiml("wss://example.ngrok-free.dev/media-stream", "s", "uid")

    assert "/media-stream/media-stream" not in twiml
    assert 'url="wss://example.ngrok-free.dev/media-stream"' in twiml


def test_twiml_tolerates_a_trailing_slash():
    twiml = _build_twiml("wss://example.ngrok-free.dev/", "s", "uid")

    assert 'url="wss://example.ngrok-free.dev/media-stream"' in twiml
