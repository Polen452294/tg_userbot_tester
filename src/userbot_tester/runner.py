from __future__ import annotations

import asyncio
import logging
import random
from typing import Iterable, Optional

from userbot_tester.mtproto import MTProtoBotChat, BotReply
from userbot_tester.scenarios.base import Step

log = logging.getLogger("runner")


async def run_steps(
    chat: MTProtoBotChat,
    steps: Iterable[Step],
    send_delay_min: float = 0.0,
    send_delay_max: float = 0.0,
) -> BotReply:
    last: Optional[BotReply] = None

    for i, step in enumerate(steps, start=1):
        log.info("=== Step %d: %s ===", i, step.name)
        last = await step.run(chat, last)

        if send_delay_max > 0:
            delay = random.uniform(send_delay_min, send_delay_max)
            log.debug("Sleep %.2fs", delay)
            await asyncio.sleep(delay)

    assert last is not None
    return last