# Voice Bot Tester

An outbound **patient simulator** that phones a clinic's AI voice agent, holds a
natural spoken conversation driven by an LLM persona, and produces an audio
recording, a full transcript, every prompt it used, a per-turn timing trace, and a
structured bug report for each call.

It is a QA harness for voice agents: instead of clicking through a chat UI, it
dials the agent like a real patient would, steers the conversation toward a
specific edge case, and writes down what broke.

**Stack:** Twilio (telephony + bidirectional Media Streams) · Deepgram (Nova-3
streaming STT, Aura-2 TTS) · Groq (`openai/gpt-oss-120b` for the live persona and
post-call analysis) · FastAPI + `asyncio` · YAML scenarios.


---

## How it works

```
scenarios/<name>.yaml
        │  persona, pinned facts, goal, edge case, intended outcome
        ▼
  orchestrator ──── Twilio REST (inline TwiML, record=True) ───► outbound call
                                                                     │
                                          <Connect><Stream url="wss://…">
                                                                     ▼
   ┌──────────────── media_bridge (FastAPI WebSocket) ─────────────────┐
   │  agent audio ─► Deepgram STT ─► endpointing ─► Groq persona ─► line│
   │       ▲                                                    │      │
   │       └──── Twilio frames ◄── streamed chunks ◄── Aura TTS ─┘      │
   └───────────────────────────────────────────────────────────────────┘
                                     │
                calls/<uid>/  recording · transcript · prompts · timing
                                     │
                          bug_analyzer (Groq, rubric pass) ─► bugs.json
```

Audio stays **mulaw / 8 kHz / mono end to end** — Twilio speaks it, Deepgram
consumes it, Aura produces it. No resampling anywhere.

Turn-taking is driven by Deepgram's `speech_final` / `UtteranceEnd` events rather
than fixed silence timers, and the persona's reply is generated *during* the
settle window rather than after it, so the agent hears an answer ~1.75 s after it
stops talking.

---

## Setup

Requires Python 3.13 (pinned in `.python-version`) and [`uv`](https://docs.astral.sh/uv/).

```bash
uv sync                 # runtime dependencies
uv sync --group dev     # + pytest
```

Copy `.env.example` to `.env` and fill in:

| Variable | What it is |
|---|---|
| `TWILIO_ACCOUNT_SID` / `TWILIO_AUTH_TOKEN` | Twilio credentials |
| `TWILIO_PHONE_NUMBER` | the number calls are placed **from** |
| `TARGET_PHONE_NUMBER` | the number under test |
| `DEEPGRAM_API_KEY` | streaming STT + TTS |
| `GROQ_API_KEY` / `GROQ_MODEL` | persona engine + bug analyser |
| `PUBLIC_WS_URL` | public `wss://` URL Twilio streams audio to |

`.env` is gitignored and must never be committed — only `.env.example`, which
holds variable names and no values. Every tuning knob described in
`PROJECT_WALKTHROUGH.md` is also overridable from the environment; the defaults
are the tested ones.

---

## Running a call

`serve` and `call` are two separate processes. Start the bridge first, expose it
publicly, then place the call against it.

```bash
# terminal 1 — the media bridge Twilio connects to
uv run main.py serve

# expose it (any tunnel works; the URL goes in PUBLIC_WS_URL)
ngrok http 8000

# terminal 2 — place a call
uv run main.py scenarios                                  # list scenario names
uv run main.py call --scenario simple_schedule --wait
```

`--wait` blocks until the call completes, downloads the recording, and runs bug
analysis. Everything lands in `calls/<scenario>_<timestamp>_<id>/`.

The bridge is deployable (`railway.toml`, `render.yaml`, `Dockerfile` are
included) — point `PUBLIC_WS_URL` at the deployed host and run `call` locally.
Note that `serve` only answers calls; it never places them.

---

## Tests

```bash
uv run pytest                            # 157 tests, ~60 s
uv run pytest tests/test_turn_taking.py  # just the media-bridge behaviour
```

No network and no credentials. `tests/conftest.py` fakes the Twilio Media Streams
socket, reproducing the two behaviours the bridge depends on: it buffers outbound
audio, and it only echoes a `mark` once that audio has "played". That is enough to
run the whole conversation loop — greeting, endpointing, barge-in, reply, hangup —
without a phone call.

The turn-taking tests encode bugs seen on real calls. Don't relax them without
re-testing against a live number.

---

## Repository layout

```
main.py                  CLI: serve · call · scenarios
src/
  config.py              env loading + explicit validation at call sites
  scenario.py            Scenario / Persona dataclasses
  orchestrator.py        places calls, downloads recordings, triggers analysis
  media_bridge.py        the live call: Twilio WS ↔ Deepgram, turn-taking, hangup
  stt.py / tts.py        Deepgram streaming clients
  persona_engine.py      Groq persona turns + reply-shaping filters
  call_logger.py         transcript / prompts / timing persistence
  bug_analyzer.py        post-call rubric pass
scenarios/*.yaml         13 declarative test cases
calls/<uid>/             per-call artefacts
reports/bug_report.md    consolidated findings
tests/                   157 offline tests
PROJECT_WALKTHROUGH.md   design decisions, tuning, and what each fix cost
```

---

## Rules the code enforces

1. **One number only.** The allowed target is hardcoded in `orchestrator.py` and
   checked on every call, whatever `TARGET_PHONE_NUMBER` says.
2. **Every call produces audio *and* a transcript** — hence the recording-download
   retry loop and the transcript being written after each turn rather than at the end.
3. **No secrets in the repo.** `.env.example` only.
4. **Every scenario has one clear intended outcome**, so a "pass" is judged against
   something concrete.
5. **Every LLM prompt is logged** to `prompts.json` — persona turns and analysis alike.
