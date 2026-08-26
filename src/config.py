from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional

from dotenv import load_dotenv

# override=True so .env is the source of truth locally. Without it, a GROQ_MODEL (or
# any other name) left in the OS environment silently wins and edits to .env do
# nothing — which is exactly what happened: .env said gpt-oss-120b, a stale shell
# variable said gpt-oss-20b, and the call ran on 20b with no indication anywhere.
# Deployments are unaffected: there is no .env on Railway, so platform variables are
# still the only source there.
load_dotenv(override=True)


@dataclass(frozen=True)
class Settings:
    twilio_account_sid: Optional[str]
    twilio_auth_token: Optional[str]
    twilio_phone_number: Optional[str]
    target_phone_number: str
    deepgram_api_key: Optional[str]
    groq_api_key: Optional[str]
    groq_model: str
    public_ws_url: Optional[str]

    @classmethod
    def from_env(cls) -> "Settings":
        return cls(
            twilio_account_sid=os.environ.get("TWILIO_ACCOUNT_SID"),
            twilio_auth_token=os.environ.get("TWILIO_AUTH_TOKEN"),
            twilio_phone_number=os.environ.get("TWILIO_PHONE_NUMBER"),
            target_phone_number=os.environ.get("TARGET_PHONE_NUMBER", "+18054398008"),
            deepgram_api_key=os.environ.get("DEEPGRAM_API_KEY"),
            groq_api_key=os.environ.get("GROQ_API_KEY"),
            # The llama-3.x chat models are no longer served on this Groq account, so
            # the old default failed outright when GROQ_MODEL was unset. Check
            # `client.models.list()` before changing this — Groq's catalogue moves.
            groq_model=os.environ.get("GROQ_MODEL", "openai/gpt-oss-120b"),
            public_ws_url=os.environ.get("PUBLIC_WS_URL"),
        )

    def require(self, *field_names: str) -> None:
        missing = [name for name in field_names if not getattr(self, name)]
        if missing:
            env_names = ", ".join(name.upper() for name in missing)
            raise RuntimeError(
                f"Missing required environment variable(s): {env_names}. "
                "Copy .env.example to .env and fill them in."
            )


settings = Settings.from_env()
