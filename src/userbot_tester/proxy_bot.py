from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from dataclasses import dataclass
from typing import Optional

from aiogram import Bot, Dispatcher, F
from aiogram.types import Message

from telethon.errors import (
    FloodWaitError,
    PeerFloodError,
    SlowModeWaitError,
    ChatWriteForbiddenError,
    UserBannedInChannelError,
    UserIsBlockedError,
    RPCError,
)

from userbot_tester.cache_sqlite import SqliteTTLCache
from userbot_tester.mtproto import MTProtoBotChat, keep_only_fio_phone_email_masked

log = logging.getLogger("proxy_bot")


@dataclass
class ProxySettings:
    private_only: bool
    user_quota_per_hour: int
    queue_maxsize: int


@dataclass(frozen=True)
class Job:
    chat_id: int
    user_id: int
    inn: str
    fio: str


class PerUserQuota:
    """
    Simple sliding-window quota: N requests per 3600s per user.
    """
    def __init__(self, per_hour: int):
        self.per_hour = max(1, int(per_hour))
        self._lock = asyncio.Lock()
        self._hits: dict[int, deque[float]] = {}

    async def allow(self, user_id: int) -> tuple[bool, float]:
        """
        Returns (allowed, retry_after_seconds).
        """
        async with self._lock:
            now = time.monotonic()
            window = 3600.0
            q = self._hits.get(user_id)
            if q is None:
                q = deque()
                self._hits[user_id] = q

            # purge old
            while q and (now - q[0]) > window:
                q.popleft()

            if len(q) >= self.per_hour:
                retry_after = window - (now - q[0])
                return False, max(1.0, retry_after)

            q.append(now)
            return True, 0.0


def _parse_inn_and_fio(text: str) -> Optional[tuple[str, str]]:
    if ";" not in text:
        return None
    inn, fio = text.split(";", 1)
    inn = inn.strip()
    fio = fio.strip()
    if not inn or not fio:
        return None
    return inn, fio


def _cache_key(inn: str, fio: str) -> str:
    fio_norm = " ".join(fio.split()).casefold()
    return f"inn:{inn}|fio:{fio_norm}"


def _format_telethon_error(e: Exception) -> str:
    if isinstance(e, FloodWaitError):
        return f"⏳ Telegram попросил подождать ~{int(e.seconds)} сек. Попробуйте позже."
    if isinstance(e, SlowModeWaitError):
        return f"⏳ В чате slow-mode. Подождите ~{int(e.seconds)} сек."
    if isinstance(e, PeerFloodError):
        return "⚠️ На аккаунт наложены антиспам-ограничения. Нужна длительная пауза (несколько часов)."

    if isinstance(e, (ChatWriteForbiddenError, UserBannedInChannelError)):
        return "⛔ Запрет: аккаунту нельзя писать в этот чат/бот (бан/ограничение доступа)."
    if isinstance(e, UserIsBlockedError):
        return "⛔ Запрет: целевой бот/пользователь заблокировал аккаунт."

    if isinstance(e, RPCError):
        return f"❌ Ошибка Telegram: {e.__class__.__name__}"

    return f"❌ Ошибка: {e}"


async def _worker_loop(
    *,
    bot: Bot,
    chat: MTProtoBotChat,
    queue: asyncio.Queue[Job],
    cache: SqliteTTLCache,
):
    while True:
        job = await queue.get()
        try:
            inn, fio = job.inn, job.fio
            key = _cache_key(inn, fio)

            # повторная проверка кэша прямо перед запросом (на случай гонок)
            cached = await cache.get(key)
            if cached:
                await bot.send_message(job.chat_id, cached.value)
                continue

            target_text = f"/inn {inn}"

            # 1) /inn -> первый ответ
            first = await chat.send_text_and_wait(target_text)

            # 2) дождаться edits (кнопки)
            edited = await chat.wait_message_edit_until(
                first.message,
                min_buttons=1,
                timeout=18.0,
                quiet_timeout=2.5,
            )

            # 3) найти кнопку по ФИО
            coords = chat.find_button_coords_by_text(edited, fio)
            if not coords:
                available = chat.buttons_flat(edited)
                msg = (
                    "❌ Не нашёл кнопку по ФИО.\n"
                    "Доступные кнопки:\n" + "\n".join(f"• {b}" for b in available[:30])
                )
                await bot.send_message(job.chat_id, msg)
                continue

            i, j = coords

            # 4) кликнуть и собрать ответы
            msgs = await chat.click_button_and_collect(
                edited,
                i=i,
                j=j,
                collect_timeout=4,
                idle_timeout=0.8,
                max_events=5,
            )

            limit_msg = chat.find_limit_message(msgs)
            if limit_msg:
                await bot.send_message(job.chat_id, "⚠️ Лимит запросов на день исчерпан. Попробуйте завтра.")
                continue

            summary_msg = chat.find_summary_message(msgs)
            if not summary_msg:
                texts = [((m.message or "").strip()) for m in msgs if (m.message or "").strip()]
                if texts:
                    safe = keep_only_fio_phone_email_masked(texts[-1])
                    await bot.send_message(job.chat_id, "Получены сообщения после клика, но '📄 Краткая сводка' не найдена.")
                    await bot.send_message(job.chat_id, safe)
                else:
                    await bot.send_message(job.chat_id, "После клика не удалось получить текстовые сообщения.")
                continue

            raw_text = (summary_msg.message or "").strip()
            safe = keep_only_fio_phone_email_masked(raw_text)

            # save cache
            await cache.set(key, safe)

            await bot.send_message(job.chat_id, safe)

        except Exception as e:
            log.exception("Job failed")
            await bot.send_message(job.chat_id, _format_telethon_error(e))

        finally:
            queue.task_done()


def build_proxy_dispatcher(
    control_bot: Bot,
    chat: MTProtoBotChat,
    settings: ProxySettings,
    cache: SqliteTTLCache,
) -> Dispatcher:
    dp = Dispatcher()

    quota = PerUserQuota(settings.user_quota_per_hour)
    queue: asyncio.Queue[Job] = asyncio.Queue(maxsize=settings.queue_maxsize)

    # запускаем ровно 1 воркер на MTProto-аккаунт
    asyncio.create_task(_worker_loop(bot=control_bot, chat=chat, queue=queue, cache=cache))

    @dp.message(F.text)
    async def relay_text(message: Message):
        if settings.private_only and message.chat.type != "private":
            return

        user_text = (message.text or "").strip()
        if not user_text:
            return

        if user_text in ("/start", "/help"):
            await message.answer(
                "Бот готов.\n"
                "Вводи данные так:\n"
                "ИНН; ФИО\n\n"
                "Пример:\n"
                "2222058686; Маркова Ольга Викторовна\n"
            )
            return

        parsed = _parse_inn_and_fio(user_text)
        if not parsed:
            await message.answer("Неверный формат. Нужно: ИНН; ФИО\nПример: 2222058686; Маркова Ольга Викторовна")
            return

        if not message.from_user:
            await message.answer("❌ Не удалось определить пользователя.")
            return

        inn, fio = parsed
        user_id = message.from_user.id
        chat_id = message.chat.id

        # 1) per-user quota
        allowed, retry_after = await quota.allow(user_id)
        if not allowed:
            mins = int(retry_after // 60) + 1
            await message.answer(f"⏳ Слишком много запросов. Попробуйте через ~{mins} мин.")
            return

        # 2) cache
        key = _cache_key(inn, fio)
        cached = await cache.get(key)
        if cached:
            await message.answer(cached.value)
            return

        # 3) enqueue
        job = Job(chat_id=chat_id, user_id=user_id, inn=inn, fio=fio)
        try:
            queue.put_nowait(job)
        except asyncio.QueueFull:
            await message.answer("⚠️ Очередь перегружена. Попробуйте чуть позже.")
            return

        await message.answer(f"Принято. Поставил в очередь: {fio}")

    return dp