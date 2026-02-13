from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from typing import Optional

from telethon import TelegramClient, events
from telethon.tl.custom.message import Message

log = logging.getLogger("mtproto")

_URL_RE = re.compile(r"(https?://\S+)")


@dataclass
class BotReply:
    text: str
    message: Message

    def has_buttons(self) -> bool:
        return bool(self.message.buttons)

    def buttons_count(self) -> int:
        if not self.message.buttons:
            return 0
        return sum(len(row or []) for row in self.message.buttons)

    def buttons_flat(self) -> list[str]:
        if not self.message.buttons:
            return []
        out: list[str] = []
        for row in self.message.buttons:
            for b in row:
                txt = getattr(b, "text", None)
                if txt:
                    out.append(txt)
        return out


class MTProtoBotChat:
    """
    Диалог MTProto-аккаунта (Telethon) с целевым Bot API ботом.
    """

    def __init__(
        self,
        client: TelegramClient,
        bot_username: str,
        default_timeout: float = 20.0,
    ):
        self.client = client
        self.bot_username = bot_username
        self.default_timeout = default_timeout
        self._bot_entity = None

    async def resolve(self) -> None:
        self._bot_entity = await self.client.get_entity(self.bot_username)
        log.info("Resolved bot entity: %s", self.bot_username)

    async def send_text_and_wait(self, text: str, timeout: Optional[float] = None) -> BotReply:
        if self._bot_entity is None:
            await self.resolve()

        t = self.default_timeout if timeout is None else timeout
        async with self.client.conversation(self._bot_entity, timeout=t) as conv:
            log.info(">> %s", text)
            await conv.send_message(text)
            msg = await conv.get_response()
            reply_text = msg.message or ""
            log.info("<< %s", reply_text.replace("\n", "\\n"))
            return BotReply(text=reply_text, message=msg)

    # ==========================================================
    # 🔥 НОВОЕ: ждать редактирование ОДНОГО сообщения
    # ==========================================================
    async def wait_message_edit_until(
        self,
        original_msg: Message,
        *,
        min_buttons: int = 2,
        timeout: float = 12.0,
        quiet_timeout: float = 1.5,
    ) -> Message:
        """
        Ждёт редактирование original_msg (тот же message_id).
        Возвращает последнюю отредактированную версию, когда:
          - число кнопок >= min_buttons
        или по таймауту вернёт то, что успел получить (может быть original_msg).
        
        quiet_timeout: если апдейты идут часто, мы всё равно ждём до условия;
                      но если долго нет новых edits, выходим.
        """
        if self._bot_entity is None:
            await self.resolve()

        target_id = original_msg.id
        best = original_msg

        got_edit_event = asyncio.Event()

        def buttons_cnt(m: Message) -> int:
            if not m.buttons:
                return 0
            return sum(len(row or []) for row in m.buttons)

        async def _handler(ev: events.MessageEdited.Event):
            nonlocal best
            # фильтр: именно наш бот и именно это сообщение
            try:
                if ev.message.id != target_id:
                    return
                best = ev.message
                got_edit_event.set()
            except Exception:
                # на всякий случай не валим весь loop
                log.exception("Error in edit handler")

        # подписываемся на edits ТОЛЬКО от целевого бота
        self.client.add_event_handler(_handler, events.MessageEdited(from_users=self._bot_entity))

        try:
            # если уже в оригинале >= min_buttons — сразу ок
            if buttons_cnt(best) >= min_buttons:
                return best

            deadline = asyncio.get_event_loop().time() + timeout
            while True:
                now = asyncio.get_event_loop().time()
                if now >= deadline:
                    return best

                got_edit_event.clear()
                # ждём либо edit, либо “тишину”
                try:
                    await asyncio.wait_for(got_edit_event.wait(), timeout=quiet_timeout)
                except asyncio.TimeoutError:
                    # нет edits какое-то время — выходим (скорее всего больше не будет)
                    return best

                # после edit проверяем условие
                if buttons_cnt(best) >= min_buttons:
                    return best

        finally:
            # важно снять handler
            self.client.remove_event_handler(_handler, events.MessageEdited(from_users=self._bot_entity))

    # ==========================================================
    # helpers: кнопки и url
    # ==========================================================
    @staticmethod
    def _get_bottom_button(msg: Message):
        if not msg.buttons:
            return None, None, None
        row_i = len(msg.buttons) - 1
        row = msg.buttons[row_i]
        if not row:
            return None, None, None
        col_j = len(row) - 1
        btn = row[col_j]
        return btn, row_i, col_j

    @staticmethod
    def _extract_url_from_message(msg: Message) -> Optional[str]:
        text = msg.message or ""
        m = _URL_RE.search(text)
        if m:
            return m.group(1)

        if msg.buttons:
            for row in msg.buttons:
                for b in row:
                    url = getattr(b, "url", None)
                    if url:
                        return url
        return None

    def extract_bottom_button_url(self, msg: Message) -> Optional[str]:
        btn, _, _ = self._get_bottom_button(msg)
        if not btn:
            return None
        return getattr(btn, "url", None)

    async def click_bottom_button_and_wait(self, msg_with_buttons: Message, timeout: Optional[float] = None) -> BotReply:
        if self._bot_entity is None:
            await self.resolve()

        btn, i, j = self._get_bottom_button(msg_with_buttons)
        if btn is None:
            raise RuntimeError("No buttons to click in given message")

        t = self.default_timeout if timeout is None else timeout

        async with self.client.conversation(self._bot_entity, timeout=t) as conv:
            btn_text = getattr(btn, "text", "<no-text>")
            log.info("** click bottom button: %s (i=%s j=%s)", btn_text, i, j)
            await msg_with_buttons.click(i=i, j=j)
            resp = await conv.get_response()
            reply_text = resp.message or ""
            log.info("<< %s", reply_text.replace("\n", "\\n"))
            return BotReply(text=reply_text, message=resp)

    async def open_bottom_button_url(self, msg: Message, timeout: Optional[float] = None) -> str:
        url = self.extract_bottom_button_url(msg)
        if url:
            return url

        reply = await self.click_bottom_button_and_wait(msg, timeout=timeout)
        url2 = self._extract_url_from_message(reply.message)
        if url2:
            return url2

        raise RuntimeError("Clicked bottom button, but could not find URL in the response")