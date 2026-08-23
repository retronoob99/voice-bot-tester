# Voice Bot Tester

Outbound patient-simulator that calls a target voice agent, holds a
conversation driven by an LLM persona, and produces a transcript, audio
recording, and bug report per call. See `CLAUDE.md` for the full module
layout and hard rules.

Deployed on [Railway](https://railway.app) (see `railway.toml`) running the
`serve` command — the FastAPI WebSocket media bridge that Twilio Media
Streams connects to. It does **not** place calls itself; run
`uv run main.py call --scenario <name> --wait` from your own machine against
the deployed app's public URL.

## Configuration

Set these as environment variables in the Railway dashboard (service →
Variables tab), never commit them:

- `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `TWILIO_PHONE_NUMBER`
- `TARGET_PHONE_NUMBER`
- `DEEPGRAM_API_KEY`
- `GROQ_API_KEY`, `GROQ_MODEL`
- `PUBLIC_WS_URL` — set this to `wss://<your-app>.up.railway.app/media-stream`

Railway enforces a 15-minute max request/connection duration — well above a
single test call's length, so no reconnect logic is needed for this use
case.
