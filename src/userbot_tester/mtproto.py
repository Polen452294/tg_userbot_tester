from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass
from typing import Optional, Tuple

from telethon import TelegramClient, events
from telethon.tl.custom.message import Message

log = logging.getLogger("mtproto")

SUMMARY_MARKER = "📄 Краткая сводка"

# --- extraction/masking helpers (safe output) ---
FIO_RE = re.compile(r"^ФИО:\s*(.+)$", re.MULTILINE)
PHONE_LINE_RE = re.compile(r"^Телефон:\s*(.+)$", re.MULTILINE)
EMAIL_LINE_RE = re.compile(r"^Email:\s*(.+)$", re.MULTILINE)


def _mask_phone(s: str) -> str:
    return s


def _mask_email(s: str) -> str:
    return s


def keep_only_fio_phone_email_masked(text: str) -> str:
    """
    Возвращает безопасную выжимку из "📄 Краткая сводка":
      - ФИО (как есть)
      - Телефон (маска)
      - Email (маска)
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
    def __init__(self, client: TelegramClient, bot_username: str, default_timeout: float = 20.0):
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

    # ---------- buttons ----------
    @staticmethod
    def buttons_count(msg: Message) -> int:
        if not msg.buttons:
            return 0
        return sum(len(row or []) for row in msg.buttons)

    @staticmethod
    def buttons_flat(msg: Message) -> list[str]:
        if not msg.buttons:
            return []
        out: list[str] = []
        for row in msg.buttons:
            for b in row:
                t = getattr(b, "text", None)
                if t:
                    out.append(t)
        return out

    @staticmethod
    def find_button_coords_by_text(msg: Message, target_text: str) -> Optional[Tuple[int, int]]:
        """
        Ищет кнопку по тексту (case-insensitive, сжатие пробелов).
        Возвращает (i, j) или None.
        """
        if not msg.buttons:
            return None

        def norm(s: str) -> str:
            return " ".join(s.split()).casefold()

        want = norm(target_text)

        for i, row in enumerate(msg.buttons):
            for j, b in enumerate(row):
                bt = getattr(b, "text", None)
                if not bt:
                    continue
                if norm(bt) == want:
                    return (i, j)

        # fallback: частичное совпадение
        for i, row in enumerate(msg.buttons):
            for j, b in enumerate(row):
                bt = getattr(b, "text", None)
                if not bt:
                    continue
                if want in norm(bt):
                    return (i, j)

        return None

    async def wait_message_edit_until(
        self,
        original_msg: Message,
        *,
        min_buttons: int = 2,
        timeout: float = 15.0,
        quiet_timeout: float = 2.0,
    ) -> Message:
        """
        Ждём редактирование того же message_id (обычно пока не появятся кнопки).
        """
        if self._bot_entity is None:
            await self.resolve()

        target_id = original_msg.id
        best = original_msg
        got = asyncio.Event()

        async def _handler(ev: events.MessageEdited.Event):
            nonlocal best
            if ev.message.id != target_id:
                return
            best = ev.message
            got.set()

        self.client.add_event_handler(_handler, events.MessageEdited(from_users=self._bot_entity))
        try:
            if self.buttons_count(best) >= min_buttons:
                return best

            deadline = asyncio.get_event_loop().time() + timeout
            while True:
                if asyncio.get_event_loop().time() >= deadline:
                    return best

                got.clear()
                try:
                    await asyncio.wait_for(got.wait(), timeout=quiet_timeout)
                except asyncio.TimeoutError:
                    return best

                if self.buttons_count(best) >= min_buttons:
                    return best
        finally:
            self.client.remove_event_handler(_handler, events.MessageEdited(from_users=self._bot_entity))

    async def click_button_and_collect(
        self,
        msg_with_buttons: Message,
        *,
        i: int,
        j: int,
        collect_timeout: float = 15.0,
        idle_timeout: float = 2.5,
        max_events: int = 12,
    ) -> list[Message]:
        """
        Кликаем по кнопке (i,j) и собираем NewMessage + MessageEdited от целевого бота.
        """
        if self._bot_entity is None:
            await self.resolve()

        collected: list[Message] = []
        got = asyncio.Event()

        async def on_new(ev: events.NewMessage.Event):
            collected.append(ev.message)
            got.set()

        async def on_edit(ev: events.MessageEdited.Event):
            collected.append(ev.message)
            got.set()

        self.client.add_event_handler(on_new, events.NewMessage(from_users=self._bot_entity))
        self.client.add_event_handler(on_edit, events.MessageEdited(from_users=self._bot_entity))

        try:
            log.info("** click button i=%s j=%s", i, j)
            await msg_with_buttons.click(i=i, j=j)

            deadline = asyncio.get_event_loop().time() + collect_timeout
            while True:
                if asyncio.get_event_loop().time() >= deadline:
                    break
                if len(collected) >= max_events:
                    break

                got.clear()
                try:
                    await asyncio.wait_for(got.wait(), timeout=idle_timeout)
                except asyncio.TimeoutError:
                    break

            return collected
        finally:
            self.client.remove_event_handler(on_new, events.NewMessage(from_users=self._bot_entity))
            self.client.remove_event_handler(on_edit, events.MessageEdited(from_users=self._bot_entity))

    @staticmethod
    def is_limit_exhausted_message(text: str) -> bool:
        t = (text or "").strip().lower()
        # ключевые фразы — можно расширять
        return (
            "лимит запросов" in t
            and ("исчерпан" in t or "временно исчерпан" in t)
        )

    @classmethod
    def find_limit_message(cls, msgs: list[Message]) -> Optional[Message]:
        for m in reversed(msgs):
            t = (m.message or "").strip()
            if t and cls.is_limit_exhausted_message(t):
                return m
        return None

    @staticmethod
    def find_summary_message(msgs: list[Message]) -> Optional[Message]:
        for m in reversed(msgs):
            t = (m.message or "").strip()
            if t.startswith(SUMMARY_MARKER) or (SUMMARY_MARKER in t):
                return m
        return None