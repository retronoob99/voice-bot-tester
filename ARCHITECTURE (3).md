# Architecture — AI Voice Bot Caller (Twilio + Deepgram)

## 1. Summary

The system is an outbound "patient simulator" that calls a target voice agent
(`+1-805-439-8008`), holds a natural spoken conversation driven by an LLM
persona, and produces a transcript, an audio recording, and a structured bug
report at the end of each call. The stack is deliberately small: **Twilio**
for telephony + real-time audio transport, **Deepgram** for both speech-to-text
(Nova streaming STT) and text-to-speech (Aura), and **Groq** (fast Llama/OSS
model inference) as the conversation brain and post-call analyst.

## 2. High-level data flow

```
 Scenario file (YAML)                          Post-call
        |                                       analysis
        v                                          ^
 [Persona/LLM Engine] <--- transcript turns --- [Call Logger]
        |  next utterance (text)                    ^
        v                                            |
 [Deepgram Aura TTS] --audio(mulaw/8k)--> [Twilio Media Stream WS] --> caller line
        ^                                            |
        |                                            v
 [Deepgram Nova STT] <--audio(mulaw/8k)---- [Twilio Media Stream WS] <-- target agent speech
```

1. A **call orchestrator** (Python) places an outbound call via the Twilio
   REST API to the test number, with TwiML that opens a bidirectional
   `<Connect><Stream>` WebSocket back to our server.
2. Our **WebSocket server** (FastAPI) receives inbound audio frames
   (mulaw, 8kHz) from Twilio and forwards them to **Deepgram's streaming STT**
   endpoint in real time.
3. Deepgram returns interim and final transcripts with `speech_final` /
   `UtteranceEnd` events, which we use for **endpointing** — i.e. deciding
   when the target agent has actually finished talking, rather than using a
   fixed silence timer.
4. On each finalized agent utterance, the **persona engine** (a Groq LLM
   call) is given: the scenario definition (goal, patient persona, edge case
   to trigger), the running transcript, and the latest agent utterance. It
   returns the next line the "patient" should say. Groq's inference speed is
   the main reason it's used here — the persona engine sits directly in the
   live conversation's critical path, so low time-to-first-token matters for
   keeping response latency low enough to feel like a real caller.
5. That line is sent to **Deepgram Aura** for TTS, streamed back to Twilio
   over the same Media Stream WebSocket as mulaw/8kHz frames, and played into
   the call.
6. If the target agent starts talking while our TTS is still playing
   (barge-in), we detect the new inbound speech event and send Twilio a
   `clear` message to stop our outbound audio immediately — this keeps
   turn-taking natural instead of two-parties-talking-over-each-other.
7. Every call is recorded (Twilio call recording, or reconstructed from the
   captured stream buffers) and saved as `.mp3`/`.ogg`, alongside a
   timestamped, speaker-labeled transcript (`.json` + `.txt`).
8. After the call ends, a **separate analysis pass** feeds the full
   transcript into Groq with a bug-finding rubric (scheduling logic,
   stated office hours vs. actual behavior, refill-safety, insurance
   accuracy, hallucination, repetition, failure to escalate, latency) and
   emits structured bug report entries.

## 3. Key design decisions & tradeoffs

- **Twilio Media Streams over Twilio `<Gather>`/DTMF-only flows.** `<Gather>`
  only supports scripted prompts and button input; Media Streams give raw,
  low-latency, bidirectional audio, which is required to sound like a real
  caller rather than an IVR test harness.
- **Deepgram for both STT and TTS** rather than mixing vendors. Reduces
  integration surface, keeps audio formats consistent (mulaw/8kHz end to
  end, no resampling), and Deepgram's streaming STT exposes utterance-level
  endpointing events that make turn-taking logic simple.
- **Groq for the LLM layer.** The persona engine call happens on every
  single conversational turn and is in the live audio path, so inference
  latency compounds directly into how natural the call feels. Groq's LPU
  inference is materially faster time-to-first-token than typical hosted
  LLM APIs, which buys back latency budget that the STT → LLM → TTS pipeline
  spends relative to a single-hop realtime API. Tradeoff: model choice is
  limited to what Groq serves (open-weight models like Llama), so prompt
  engineering matters more than with a frontier model for reliably staying
  in-persona and producing well-formed bug analysis output.
- **Separate LLM "brain" from the audio plumbing.** Scenarios are declarative
  YAML (persona, goal, edge case), not hardcoded scripts, so new test cases
  (interruptions, ambiguous requests, refill edge cases) can be added without
  touching the WebSocket/audio code.
- **Alternative considered: a single realtime speech-to-speech API** (e.g.
  OpenAI Realtime). This would collapse STT/LLM/TTS into one hop and reduce
  latency further — but Groq's speed narrows that gap on the LLM leg
  specifically — and it gives up fine-grained control over persona injection,
  mid-call scenario steering, and endpointing — control we wanted for
  actively steering conversations toward specific edge cases. Given the
  priority on "active steering toward intended test-case outcomes," the
  decoupled STT → LLM → TTS pipeline was the better fit even at the cost of
  a bit more latency.
- **Endpointing via Deepgram events, not silence timers.** Fixed timers
  either cut people off or leave awkward dead air; `speech_final` /
  `UtteranceEnd` gives closer-to-human turn-taking.
- **Post-call (not live) bug analysis.** Doing bug detection as a separate
  LLM pass over the full transcript, rather than inline during the call,
  keeps the live conversation loop fast and lets the analysis prompt use the
  complete context (whole call, not just the last turn) for better judgments.

## 4. Components

| Component | Responsibility | Tech |
|---|---|---|
| Call Orchestrator | Places outbound calls, manages call lifecycle, retries | Python, Twilio REST API |
| Media Bridge | WebSocket server bridging Twilio `<Stream>` ↔ Deepgram | FastAPI / `websockets` |
| STT | Real-time transcription + endpointing of agent speech | Deepgram Nova streaming |
| Persona Engine | Decides next patient line per scenario + transcript | Groq (Llama 3.x / OSS model) |
| TTS | Converts patient line to speech | Deepgram Aura |
| Call Logger | Persists transcript, timestamps, speaker labels, recording | Local FS / JSON |
| Bug Analyzer | Post-call LLM pass producing structured bug entries | Groq |
| Scenario Library | Declarative test-case definitions | YAML |

## 5. Repo layout (suggested)

```
voice-bot-challenge/
├── ARCHITECTURE.md
├── README.md
├── requirements.txt
├── .env.example
├── scenarios/               # YAML scenario definitions
│   ├── simple_schedule.yaml
│   ├── refill_request.yaml
│   ├── reschedule.yaml
│   ├── office_hours_edge_case.yaml
│   ├── interruption_barge_in.yaml
│   └── ...
├── src/
│   ├── orchestrator.py      # places calls, drives TwiML
│   ├── media_bridge.py      # FastAPI WS server, Twilio <-> Deepgram audio
│   ├── stt.py                # Deepgram streaming STT client
│   ├── tts.py                # Deepgram Aura client
│   ├── persona_engine.py     # LLM persona logic
│   ├── call_logger.py        # transcript + recording persistence
│   └── bug_analyzer.py       # post-call rubric-based analysis
├── calls/                    # output: transcripts + recordings per call
│   ├── call_01/
│   │   ├── transcript.json
│   │   ├── transcript.txt
│   │   └── recording.mp3
│   └── ...
└── reports/
    └── bug_report.md
```

## 6. Functional requirements

- Bot must call **only** `+1-805-439-8008`; no other numbers, including the
  number shown on the pgai.us/athena confirmation screen.
- Minimum **10 completed calls**, each a full 1–3 minute conversation, not a
  single question + hangup.
- Each call must produce: an audio recording (`.mp3` or `.ogg`) **and** a
  transcript with both sides of the conversation.
- Bot must **actively steer** the conversation toward the scenario's intended
  outcome (e.g. actually attempt to book a Sunday appointment to test office
  hours handling), not just answer whatever the agent asks passively.
- Must support natural turn-taking, including intentional barge-in tests.
- No secrets committed to the repo; all keys via environment variables
  documented in `.env.example`.

## 7. Environment variables (`.env.example`)

```
TWILIO_ACCOUNT_SID=
TWILIO_AUTH_TOKEN=
TWILIO_PHONE_NUMBER=          # Twilio number the call is placed from
TARGET_PHONE_NUMBER=+18054398008
DEEPGRAM_API_KEY=
GROQ_API_KEY=                 # Groq key for persona engine + bug analysis
GROQ_MODEL=llama-3.3-70b-versatile   # or another Groq-hosted model
PUBLIC_WS_URL=                # publicly reachable wss:// URL Twilio streams to (e.g. via ngrok)
```

## 8. Rules for this project

1. Only ever dial `+1-805-439-8008` — never the number on the Athena signup
   confirmation screen, and never a real clinic or personal number.
2. Keep every recording and transcript; the submission requires audio in
   OGG/MP3 **and** transcript for all 10+ calls.
3. Don't commit `.env` or any API key — only `.env.example` with variable
   names.
4. Each scenario should have a clear, single **intended outcome** (what a
   "pass" looks like) so bugs can be judged against something concrete.
5. Prefer a small number of *deep, realistic* calls over many shallow ones —
   quality of bugs found is weighted higher than call count.
6. Log every LLM prompt used for persona decisions and bug analysis (useful
   for the "how you used AI to debug" screen recording deliverable).

## 9. Techniques for finding bugs

- **Ground-truth contradiction tests**: ask for something the agent should
  refuse or correct (e.g. Sunday appointment when closed weekends,
  medication refill without prior prescription) and check whether it
  actually applies its own policy or just complies.
- **Ambiguity probes**: give underspecified requests ("I need to come in
  sometime next week") and see whether the agent asks clarifying questions
  or guesses.
- **Interruption / barge-in**: start talking mid-agent-sentence to check
  recovery behavior and whether it loses context.
- **Context-retention checks**: state a fact early ("I'm allergic to
  penicillin") and reference it later to see if the agent remembers.
- **Off-topic / adversarial turns**: brief non-sequiturs or topic changes to
  see if the agent derails or gracefully returns to task.
- **Consistency checks across calls**: ask the same factual question (e.g.
  office hours, insurance accepted) in multiple calls/scenarios and compare
  answers for consistency.
- **Escalation test**: request something clearly outside the bot's scope
  (e.g. a clinical question) and check whether it escalates to a human
  appropriately instead of guessing.

Each finding should be logged as: **Bug**, **Severity** (High/Med/Low),
**Call + timestamp**, **Details** (what happened vs. what should have
happened) — same format as the example in the challenge brief.
