from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from typing import Optional

from telethon import TelegramClient, events
from telethon.tl.custom.message import Message

log = logging.getLogger("mtproto")

SUMMARY_MARKER = "📄 Краткая сводка"

# простая маскировка PII (можно расширить под твой формат)
EMAIL_RE = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.IGNORECASE)
PHONE_RE = re.compile(r"(?<!\d)(?:\+?\d[\d\-\s()]{8,}\d)(?!\d)")
DOCNUM_RE = re.compile(r"(?<!\d)\d{8,}(?!\d)")  # длинные номера (паспорт/снилс/и т.п.)

SUMMARY_MARKER = "📄 Краткая сводка"

FIO_RE = re.compile(r"^ФИО:\s*(.+)$", re.MULTILINE)
PHONE_LINE_RE = re.compile(r"^Телефон:\s*(.+)$", re.MULTILINE)
EMAIL_LINE_RE = re.compile(r"^Email:\s*(.+)$", re.MULTILINE)


def _mask_phone(s: str) -> str:
    return s


def _mask_email(s: str) -> str:
    return s


def keep_only_fio_phone_email_masked(text: str) -> str:
    """
    Оставляет только:
      - ФИО (как есть)
      - Телефон (замаскированный)
      - Email (замаскированный)
    Если строк нет — вернёт исходный заголовок + то, что нашлось.
    """
    fio = None
    phone = None
    email = None

    m = FIO_RE.search(text)
    if m:
        fio = m.group(1).strip()

    m = PHONE_LINE_RE.search(text)
    if m:
        phone = _mask_phone(m.group(1))

    m = EMAIL_LINE_RE.search(text)
    if m:
        email = _mask_email(m.group(1))

    lines = [SUMMARY_MARKER]
    if fio:
        lines.append(f"ФИО: {fio}")
    if phone:
        lines.append(f"Телефон: {phone}")
    if email:
        lines.append(f"Email: {email}")

    return "\n".join(lines)


@dataclass
class BotReply:
    text: str
    message: Message


class MTProtoBotChat:
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

    # ----- buttons -----
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

    def buttons_count(self, msg: Message) -> int:
        if not msg.buttons:
            return 0
        return sum(len(row or []) for row in msg.buttons)

    async def wait_message_edit_until(
        self,
        original_msg: Message,
        *,
        min_buttons: int = 2,
        timeout: float = 12.0,
        quiet_timeout: float = 1.5,
    ) -> Message:
        """Ждём редактирование конкретного message_id (обычно чтобы появились 2 кнопки)."""
        if self._bot_entity is None:
            await self.resolve()

        target_id = original_msg.id
        best = original_msg
        got_event = asyncio.Event()

        def _btns(m: Message) -> int:
            return self.buttons_count(m)

        async def _handler(ev: events.MessageEdited.Event):
            nonlocal best
            if ev.message.id != target_id:
                return
            best = ev.message
            got_event.set()

        self.client.add_event_handler(_handler, events.MessageEdited(from_users=self._bot_entity))
        try:
            if _btns(best) >= min_buttons:
                return best

            deadline = asyncio.get_event_loop().time() + timeout
            while True:
                if asyncio.get_event_loop().time() >= deadline:
                    return best

                got_event.clear()
                try:
                    await asyncio.wait_for(got_event.wait(), timeout=quiet_timeout)
                except asyncio.TimeoutError:
                    return best

                if _btns(best) >= min_buttons:
                    return best
        finally:
            self.client.remove_event_handler(_handler, events.MessageEdited(from_users=self._bot_entity))

    async def click_bottom_button_and_collect(
        self,
        msg_with_buttons: Message,
        *,
        collect_timeout: float = 10.0,
        idle_timeout: float = 2.0,
        max_events: int = 10,
    ) -> list[Message]:
        """
        Кликаем нижнюю кнопку и затем собираем входящие сообщения/редактирования от целевого бота.
        Это надежнее, чем ждать один get_response(), когда бот может прислать/отредактировать несколько раз.
        """
        if self._bot_entity is None:
            await self.resolve()

        btn, i, j = self._get_bottom_button(msg_with_buttons)
        if btn is None:
            raise RuntimeError("No buttons to click")

        collected: list[Message] = []
        got_event = asyncio.Event()

        async def on_new(ev: events.NewMessage.Event):
            collected.append(ev.message)
            got_event.set()

        async def on_edit(ev: events.MessageEdited.Event):
            collected.append(ev.message)
            got_event.set()

        # Подписываемся только на события от целевого бота
        self.client.add_event_handler(on_new, events.NewMessage(from_users=self._bot_entity))
        self.client.add_event_handler(on_edit, events.MessageEdited(from_users=self._bot_entity))

        try:
            log.info("** click bottom button (i=%s j=%s)", i, j)
            await msg_with_buttons.click(i=i, j=j)

            deadline = asyncio.get_event_loop().time() + collect_timeout
            events_seen = 0

            while True:
                if asyncio.get_event_loop().time() >= deadline:
                    break
                if events_seen >= max_events:
                    break

                got_event.clear()
                try:
                    await asyncio.wait_for(got_event.wait(), timeout=idle_timeout)
                except asyncio.TimeoutError:
                    # “тишина” — считаем, что цепочка закончилась
                    break

                events_seen = len(collected)

            return collected

        finally:
            self.client.remove_event_handler(on_new, events.NewMessage(from_users=self._bot_entity))
            self.client.remove_event_handler(on_edit, events.MessageEdited(from_users=self._bot_entity))

    @staticmethod
    def find_summary_message(msgs: list[Message]) -> Optional[Message]:
        """
        Ищем сообщение, которое содержит маркер '📄 Краткая сводка'.
        """
        for m in reversed(msgs):  # чаще нужное ближе к концу
            t = (m.message or "").strip()
            if t.startswith(SUMMARY_MARKER) or (SUMMARY_MARKER in t):
                return m
        return None