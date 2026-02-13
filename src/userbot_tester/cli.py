from __future__ import annotations

import asyncio
import logging
from typing import Optional

from userbot_tester.mtproto import MTProtoBotChat, BotReply

log = logging.getLogger("cli")


async def interactive_shell(chat: MTProtoBotChat) -> None:
    """
    REPL: ты вводишь текст в терминал -> отправляем боту -> печатаем ответ.
    Команды:
      /exit или /quit  — выйти
      /help           — подсказка
    """
    last: Optional[BotReply] = None

    print("\nInteractive mode. Type messages to send to the bot.")
    print("Commands: /help, /exit, /quit\n")

    while True:
        # Важно: input() блокирует, поэтому читаем в отдельном потоке
        user_text = await asyncio.to_thread(input, "you> ")
        user_text = (user_text or "").strip()

        if not user_text:
            continue

        if user_text in ("/exit", "/quit"):
            print("bye!")
            return

        if user_text == "/help":
            print("Type any message (e.g. /start).")
            print("Use /exit or /quit to stop.")
            print("If last bot reply has buttons, we can add /click <text> later.\n")
            continue

        try:
            last = await chat.send_text_and_wait(user_text)
            print(f"bot> {last.text}\n")
        except Exception as e:
            log.exception("Send failed: %s", e)
            print(f"ERROR: {e}\n")