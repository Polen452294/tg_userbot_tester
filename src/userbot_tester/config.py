from __future__ import annotations

import os
from dataclasses import dataclass
from dotenv import load_dotenv


@dataclass(frozen=True)
class Config:
    # MTProto (user account)
    tg_api_id: int
    tg_api_hash: str
    tg_session_name: str

    # Target bot
    bot_username: str

    # Runtime
    default_timeout: float
    log_level: str

    send_delay_min: float
    send_delay_max: float

    # Control (proxy) bot
    control_bot_token: str
    control_private_only: bool


def _req(name: str) -> str:
    v = os.getenv(name, "").strip()
    if not v:
        raise RuntimeError(f"{name} is empty (check .env)")
    return v


def load_config() -> Config:
    load_dotenv(override=False)

    api_id_raw = _req("TG_API_ID")
    if not api_id_raw.isdigit():
        raise RuntimeError("TG_API_ID must be an integer (check .env)")

    api_hash = _req("TG_API_HASH")
    session_name = os.getenv("TG_SESSION_NAME", "me").strip() or "me"

    bot_username = _req("BOT_USERNAME")
    if not bot_username.startswith("@"):
        bot_username = "@" + bot_username

    default_timeout = float(os.getenv("DEFAULT_TIMEOUT", "20").strip())
    log_level = os.getenv("LOG_LEVEL", "INFO").strip().upper()

    send_delay_min = float(os.getenv("SEND_DELAY_MIN", "0").strip())
    send_delay_max = float(os.getenv("SEND_DELAY_MAX", "0").strip())

    control_bot_token = _req("CONTROL_BOT_TOKEN")
    control_private_only = os.getenv("CONTROL_PRIVATE_ONLY", "1").strip() in (
        "1", "true", "True", "yes", "YES"
    )

    return Config(
        tg_api_id=int(api_id_raw),
        tg_api_hash=api_hash,
        tg_session_name=session_name,
        bot_username=bot_username,
        default_timeout=default_timeout,
        log_level=log_level,
        send_delay_min=send_delay_min,
        send_delay_max=send_delay_max,
        control_bot_token=control_bot_token,
        control_private_only=control_private_only,
    )