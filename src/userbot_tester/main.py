from __future__ import annotations

import asyncio
import logging

from telethon import TelegramClient
from aiogram import Bot

from userbot_tester.cache_sqlite import SqliteTTLCache
from userbot_tester.config import load_config
from userbot_tester.logging_setup import setup_logging
from userbot_tester.mtproto import MTProtoBotChat
from userbot_tester.proxy_bot import build_proxy_dispatcher, ProxySettings

log = logging.getLogger("main")


async def async_main() -> None:
    cfg = load_config()
    setup_logging(cfg.log_level)

    cache = SqliteTTLCache(cfg.cache_db_path, cfg.cache_ttl_seconds)

    # 1) MTProto client (твой аккаунт)
    tg_client = TelegramClient(cfg.tg_session_name, cfg.tg_api_id, cfg.tg_api_hash)
    await tg_client.start()

    chat = MTProtoBotChat(
        tg_client,
        bot_username=cfg.bot_username,
        default_timeout=cfg.default_timeout,
        send_delay_min=cfg.send_delay_min,
        send_delay_max=cfg.send_delay_max,
        rate_max_actions=cfg.rate_max_actions,
        rate_window_seconds=cfg.rate_window_seconds,
        floodwait_buffer_seconds=cfg.floodwait_buffer_seconds,
        peerflood_cooldown_seconds=cfg.peerflood_cooldown_seconds,
    )
    await chat.resolve()

    # 2) Control bot (Bot API)
    control_bot = Bot(cfg.control_bot_token)

    dp = build_proxy_dispatcher(
        control_bot=control_bot,
        chat=chat,
        cache=cache,
        settings=ProxySettings(
            private_only=cfg.control_private_only,
            user_quota_per_hour=cfg.user_quota_per_hour,
            queue_maxsize=cfg.queue_maxsize,
        ),
    )

    log.info("Proxy bot started. private_only=%s", cfg.control_private_only)
    try:
        await dp.start_polling(control_bot)
    finally:
        await control_bot.session.close()
        await tg_client.disconnect()
        await cache.close()


def main() -> None:
    asyncio.run(async_main())