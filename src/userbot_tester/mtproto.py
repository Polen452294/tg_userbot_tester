from __future__ import annotations

import asyncio
import logging
import random
import re
import time
from dataclasses import dataclass
from typing import Optional, Tuple

from telethon import TelegramClient, events
from telethon.errors import (
    FloodWaitError,
    PeerFloodError,
    SlowModeWaitError,
    ChatWriteForbiddenError,
    UserBannedInChannelError,
    UserIsBlockedError,
    RPCError,
)
from telethon.tl.custom.message import Message

log = logging.getLogger("mtproto")

SUMMARY_MARKER = "📄 Краткая сводка"

FIO_RE = re.compile(r"^ФИО:\s*(.+)$", re.MULTILINE)
PHONE_LINE_RE = re.compile(r"^Телефон:\s*(.+)$", re.MULTILINE)
EMAIL_LINE_RE = re.compile(r"^Email:\s*(.+)$", re.MULTILINE)


def _mask_phone(s: str) -> str:
    return s


def _mask_email(s: str) -> str:
    return s


def keep_only_fio_phone_email_masked(text: str) -> str:
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


class RateLimiter:
    """
    Sliding window limiter: max_actions per window_seconds.
    """
    def __init__(self, max_actions: int, window_seconds: float):
        self.max_actions = max(1, int(max_actions))
        self.window_seconds = max(1.0, float(window_seconds))
        self._ts: list[float] = []
        self._lock = asyncio.Lock()

    async def acquire(self) -> None:
        async with self._lock:
            now = time.monotonic()
            cutoff = now - self.window_seconds
            self._ts = [t for t in self._ts if t >= cutoff]

            if len(self._ts) >= self.max_actions:
                sleep_for = (self._ts[0] + self.window_seconds) - now
                if sleep_for > 0:
                    await asyncio.sleep(sleep_for)

                now = time.monotonic()
                cutoff = now - self.window_seconds
                self._ts = [t for t in self._ts if t >= cutoff]

            self._ts.append(time.monotonic())


class CircuitBreaker:
    """
    Global cooldown gate (for FloodWait / PeerFlood).
    """
    def __init__(self):
        self._until = 0.0
        self._lock = asyncio.Lock()

    async def sleep_if_open(self) -> None:
        async with self._lock:
            now = time.monotonic()
            if now < self._until:
                await asyncio.sleep(self._until - now)

    async def open_for(self, seconds: float) -> None:
        async with self._lock:
            self._until = max(self._until, time.monotonic() + max(0.0, seconds))


class MTProtoBotChat:
    def __init__(
        self,
        client: TelegramClient,
        bot_username: str,
        default_timeout: float = 20.0,
        *,
        send_delay_min: float = 0.15,
        send_delay_max: float = 0.45,
        rate_max_actions: int = 15,
        rate_window_seconds: float = 60.0,
        floodwait_buffer_seconds: float = 2.0,
        peerflood_cooldown_seconds: float = 6 * 60 * 60,
    ):
        self.client = client
        self.bot_username = bot_username
        self.default_timeout = default_timeout
        self._bot_entity = None

        self.send_delay_min = float(send_delay_min)
        self.send_delay_max = float(send_delay_max)

        self.limiter = RateLimiter(rate_max_actions, rate_window_seconds)
        self.breaker = CircuitBreaker()

        self.floodwait_buffer_seconds = float(floodwait_buffer_seconds)
        self.peerflood_cooldown_seconds = float(peerflood_cooldown_seconds)

    async def resolve(self) -> None:
        self._bot_entity = await self.client.get_entity(self.bot_username)
        log.info("Resolved bot entity: %s", self.bot_username)

    async def _before_action(self) -> None:
        await self.breaker.sleep_if_open()
        await self.limiter.acquire()
        lo = max(0.0, self.send_delay_min)
        hi = max(lo, self.send_delay_max)
        if hi > 0:
            await asyncio.sleep(random.uniform(lo, hi))

    async def send_text_and_wait(self, text: str, timeout: Optional[float] = None) -> BotReply:
        if self._bot_entity is None:
            await self.resolve()

        t = self.default_timeout if timeout is None else timeout

        await self._before_action()
        try:
            async with self.client.conversation(self._bot_entity, timeout=t) as conv:
                log.info(">> %s", text)
                await conv.send_message(text)
                msg = await conv.get_response()
                reply_text = msg.message or ""
                log.info("<< %s", reply_text.replace("\n", "\\n"))
                return BotReply(text=reply_text, message=msg)

        except FloodWaitError as e:
            wait_s = float(e.seconds) + self.floodwait_buffer_seconds
            log.warning("FloodWaitError: %.1fs", wait_s)
            await self.breaker.open_for(wait_s)
            raise

        except PeerFloodError:
            log.error("PeerFloodError: open long cooldown %.1fs", self.peerflood_cooldown_seconds)
            await self.breaker.open_for(self.peerflood_cooldown_seconds)
            raise

        except SlowModeWaitError as e:
            wait_s = float(e.seconds) + self.floodwait_buffer_seconds
            log.warning("SlowModeWaitError: %.1fs", wait_s)
            await self.breaker.open_for(wait_s)
            raise

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
        if self._bot_entity is None:
            await self.resolve()

        await self._before_action()

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

        except FloodWaitError as e:
            wait_s = float(e.seconds) + self.floodwait_buffer_seconds
            log.warning("FloodWaitError after click: %.1fs", wait_s)
            await self.breaker.open_for(wait_s)
            raise

        except PeerFloodError:
            log.error("PeerFloodError after click: open long cooldown %.1fs", self.peerflood_cooldown_seconds)
            await self.breaker.open_for(self.peerflood_cooldown_seconds)
            raise

        except SlowModeWaitError as e:
            wait_s = float(e.seconds) + self.floodwait_buffer_seconds
            log.warning("SlowModeWaitError after click: %.1fs", wait_s)
            await self.breaker.open_for(wait_s)
            raise

        finally:
            self.client.remove_event_handler(on_new, events.NewMessage(from_users=self._bot_entity))
            self.client.remove_event_handler(on_edit, events.MessageEdited(from_users=self._bot_entity))

    @staticmethod
    def is_limit_exhausted_message(text: str) -> bool:
        t = (text or "").strip().lower()
        return ("лимит запросов" in t) and ("исчерпан" in t or "временно исчерпан" in t)

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