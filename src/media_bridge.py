from __future__ import annotations

import asyncio
import base64
import json
import logging
import uuid
from pathlib import Path
from typing import Optional

from deepgram.listen.v1.types.listen_v1results import ListenV1Results
from deepgram.listen.v1.types.listen_v1speech_started import ListenV1SpeechStarted
from deepgram.listen.v1.types.listen_v1utterance_end import ListenV1UtteranceEnd
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from twilio.rest import Client as TwilioClient

from .call_logger import CallLogger
from .config import settings
from .persona_engine import PersonaEngine
from .scenario import Scenario, find_scenario
from .stt import stt_connection
from .tts import synthesize_mulaw

logger = logging.getLogger("media_bridge")

CALLS_DIR = Path(__file__).resolve().parent.parent / "calls"
MULAW_CHUNK_BYTES = 160  # 20ms of mulaw @ 8kHz
CHUNK_INTERVAL_S = 0.02

app = FastAPI()


class CallSession:
    def __init__(self, ws: WebSocket, scenario: Scenario, call_uid: str, stream_sid: str):
        self.ws = ws
        self.scenario = scenario
        self.call_uid = call_uid
        self.stream_sid = stream_sid
        self.logger = CallLogger(CALLS_DIR, call_uid, scenario.name)
        self.persona = PersonaEngine(settings.groq_api_key, settings.groq_model)
        self.history: list[dict] = []
        self.bot_speaking = False
        self.speak_task: Optional[asyncio.Task] = None
        self.pending_marks: dict[str, asyncio.Event] = {}
        self.hangup_requested = False

    async def _send(self, payload: dict) -> None:
        await self.ws.send_text(json.dumps(payload))

    async def send_media(self, mulaw_bytes: bytes) -> None:
        await self._send(
            {
                "event": "media",
                "streamSid": self.stream_sid,
                "media": {"payload": base64.b64encode(mulaw_bytes).decode("ascii")},
            }
        )

    async def send_mark(self, name: str) -> None:
        await self._send({"event": "mark", "streamSid": self.stream_sid, "mark": {"name": name}})

    async def send_clear(self) -> None:
        await self._send({"event": "clear", "streamSid": self.stream_sid})

    async def speak(self, text: str, end_call: bool) -> None:
        self.bot_speaking = True
        try:
            audio = await synthesize_mulaw(
                settings.deepgram_api_key, text, self.scenario.persona.voice
            )
            for offset in range(0, len(audio), MULAW_CHUNK_BYTES):
                await self.send_media(audio[offset : offset + MULAW_CHUNK_BYTES])
                await asyncio.sleep(CHUNK_INTERVAL_S)

            mark_name = f"{'end' if end_call else 'turn'}-{uuid.uuid4().hex[:8]}"
            mark_event = asyncio.Event()
            self.pending_marks[mark_name] = mark_event
            await self.send_mark(mark_name)

            if end_call:
                self.hangup_requested = True
                await mark_event.wait()
        finally:
            self.bot_speaking = False

    async def start_turn(self, text: str, end_call: bool) -> None:
        if self.speak_task and not self.speak_task.done():
            self.speak_task.cancel()
        self.history.append({"speaker": "patient", "text": text})
        self.logger.log_turn("patient", text)
        self.speak_task = asyncio.create_task(self.speak(text, end_call))

    async def opening_line(self) -> None:
        if self.scenario.opening_line:
            await self.start_turn(self.scenario.opening_line, end_call=False)
            return
        text, should_end, messages = await self.persona.next_line(
            self.scenario,
            [],
            "(The call just connected. Greet the clinic and state why you're calling.)",
        )
        self.logger.log_prompt("opening_line", messages, text)
        await self.start_turn(text, should_end)

    async def handle_agent_utterance(self, text: str) -> None:
        text = text.strip()
        if not text:
            return
        self.history.append({"speaker": "agent", "text": text})
        self.logger.log_turn("agent", text)

        if len(self.history) >= self.scenario.max_turns * 2:
            reply = "Thanks so much for your help today, that's everything I needed. Goodbye!"
            should_end = True
        else:
            reply, should_end, messages = await self.persona.next_line(
                self.scenario, self.history[:-1], text
            )
            self.logger.log_prompt("turn", messages, reply)

        await self.start_turn(reply, should_end)

    async def handle_barge_in(self) -> None:
        if self.bot_speaking and self.speak_task and not self.speak_task.done():
            self.speak_task.cancel()
            self.bot_speaking = False
            await self.send_clear()


@app.websocket("/media-stream")
async def media_stream(websocket: WebSocket) -> None:
    await websocket.accept()
    settings.require("deepgram_api_key", "groq_api_key")

    twilio_client = None
    if settings.twilio_account_sid and settings.twilio_auth_token:
        twilio_client = TwilioClient(settings.twilio_account_sid, settings.twilio_auth_token)

    session: Optional[CallSession] = None
    call_sid: Optional[str] = None

    async with stt_connection(settings.deepgram_api_key) as stt_socket:

        async def stt_reader() -> None:
            utterance_parts: list[str] = []
            async for message in stt_socket:
                if isinstance(message, ListenV1SpeechStarted):
                    if session:
                        await session.handle_barge_in()
                elif isinstance(message, ListenV1Results):
                    alternatives = message.channel.alternatives if message.channel else []
                    transcript = alternatives[0].transcript if alternatives else ""
                    if transcript and message.is_final:
                        utterance_parts.append(transcript)
                    if message.speech_final and utterance_parts and session:
                        full = " ".join(utterance_parts).strip()
                        utterance_parts.clear()
                        if full:
                            await session.handle_agent_utterance(full)
                elif isinstance(message, ListenV1UtteranceEnd):
                    if utterance_parts and session:
                        full = " ".join(utterance_parts).strip()
                        utterance_parts.clear()
                        if full:
                            await session.handle_agent_utterance(full)

        reader_task = asyncio.create_task(stt_reader())

        try:
            while True:
                raw = await websocket.receive_text()
                data = json.loads(raw)
                event = data.get("event")

                if event == "start":
                    start = data["start"]
                    stream_sid = start["streamSid"]
                    call_sid = start.get("callSid")
                    params = start.get("customParameters", {}) or {}
                    scenario_name = params.get("scenario")
                    call_uid = params.get("call_uid") or f"unknown_{uuid.uuid4().hex[:8]}"
                    if not scenario_name:
                        logger.error("No scenario parameter for call %s; closing.", call_uid)
                        break
                    scenario = find_scenario(scenario_name)
                    session = CallSession(websocket, scenario, call_uid, stream_sid)
                    await session.opening_line()

                elif event == "media":
                    if session:
                        payload = data["media"]["payload"]
                        await stt_socket.send_media(base64.b64decode(payload))

                elif event == "mark":
                    if session:
                        name = data.get("mark", {}).get("name")
                        mark_event = session.pending_marks.pop(name, None)
                        if mark_event:
                            mark_event.set()
                        if session.hangup_requested and name and name.startswith("end-"):
                            if twilio_client and call_sid:
                                try:
                                    twilio_client.calls(call_sid).update(status="completed")
                                except Exception:
                                    logger.exception("Failed to hang up call %s", call_sid)
                            break

                elif event == "stop":
                    break

        except WebSocketDisconnect:
            pass
        finally:
            reader_task.cancel()
            if session and session.speak_task:
                session.speak_task.cancel()
