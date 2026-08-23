# Voice Bot Tester

Outbound patient-simulator that calls a target voice agent, holds a
conversation driven by an LLM persona, and produces a transcript, audio
recording, and bug report per call. See `CLAUDE.md` for the full module
layout and hard rules.

Deployed on [Render](https://render.com) (see `render.yaml`) running the
`serve` command — the FastAPI WebSocket media bridge that Twilio Media
Streams connects to. It does **not** place calls itself; run
`uv run main.py call --scenario <name> --wait` from your own machine against
the deployed app's public URL.

## Configuration

Set these as environment variables in the Render dashboard (Environment
tab), never commit them:

- `TWILIO_ACCOUNT_SID`, `TWILIO_AUTH_TOKEN`, `TWILIO_PHONE_NUMBER`
- `TARGET_PHONE_NUMBER`
- `DEEPGRAM_API_KEY`
- `GROQ_API_KEY`, `GROQ_MODEL`
- `PUBLIC_WS_URL` — set this to `wss://<your-app>.onrender.com/media-stream`

The free plan spins the service down after 15 minutes of inactivity and
takes ~30-60s to cold-start on the next request — visit the app's URL to
wake it before placing a test call. Upgrade to the Starter plan for an
always-on instance (no cold start) if that's disruptive.
