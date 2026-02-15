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

    # Jitter between outbound actions (seconds)
    send_delay_min: float
    send_delay_max: float

    # Rate limiting for MTProto actions
    rate_max_actions: int
    rate_window_seconds: float

    # Cooldowns
    floodwait_buffer_seconds: float
    peerflood_cooldown_seconds: float

    # Cache
    cache_db_path: str
    cache_ttl_seconds: int

    # Per-user quota (proxy users)
    user_quota_per_hour: int

    # Queue
    queue_maxsize: int

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

    # Small jitter to avoid bursty schedule (stability, not "masking")
    send_delay_min = float(os.getenv("SEND_DELAY_MIN", "0.15").strip())
    send_delay_max = float(os.getenv("SEND_DELAY_MAX", "0.45").strip())

    # Conservative defaults: ~15 actions/min => ~900 actions/hour
    rate_max_actions = int(os.getenv("RATE_MAX_ACTIONS", "15").strip())
    rate_window_seconds = float(os.getenv("RATE_WINDOW_SECONDS", "60").strip())

    floodwait_buffer_seconds = float(os.getenv("FLOODWAIT_BUFFER_SECONDS", "2").strip())
    peerflood_cooldown_seconds = float(os.getenv("PEERFLOOD_COOLDOWN_SECONDS", str(6 * 60 * 60)).strip())

    cache_db_path = os.getenv("CACHE_DB_PATH", "./.cache.sqlite3").strip() or "./.cache.sqlite3"
    cache_ttl_seconds = int(os.getenv("CACHE_TTL_SECONDS", str(6 * 60 * 60)).strip())  # 6 hours default

    user_quota_per_hour = int(os.getenv("USER_QUOTA_PER_HOUR", "30").strip())  # per proxy user

    queue_maxsize = int(os.getenv("QUEUE_MAXSIZE", "200").strip())

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
        rate_max_actions=rate_max_actions,
        rate_window_seconds=rate_window_seconds,
        floodwait_buffer_seconds=floodwait_buffer_seconds,
        peerflood_cooldown_seconds=peerflood_cooldown_seconds,
        cache_db_path=cache_db_path,
        cache_ttl_seconds=cache_ttl_seconds,
        user_quota_per_hour=user_quota_per_hour,
        queue_maxsize=queue_maxsize,
        control_bot_token=control_bot_token,
        control_private_only=control_private_only,
    )