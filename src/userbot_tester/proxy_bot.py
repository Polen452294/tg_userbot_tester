from __future__ import annotations

import asyncio
import logging
import os
import time
from collections import deque
from dataclasses import dataclass
from typing import Optional

from aiogram import Bot, Dispatcher, F
from aiogram.types import FSInputFile, Message

from telethon.errors import (
    ChatWriteForbiddenError,
    FloodWaitError,
    PeerFloodError,
    RPCError,
    SlowModeWaitError,
    UserBannedInChannelError,
    UserIsBlockedError,
)

from userbot_tester.cache_sqlite import SqliteTTLCache
from userbot_tester.excel_batch import read_input_xlsx, write_output_xlsx, write_pending_xlsx, InputRow
from userbot_tester.mtproto import (
    MTProtoBotChat,
    keep_only_fio_phone_email_masked,
    parse_summary_fields,
    is_not_found_message,
)

log = logging.getLogger("proxy_bot")


@dataclass
class ProxySettings:
    private_only: bool
    user_quota_per_hour: int
    queue_maxsize: int


@dataclass(frozen=True)
class Job:
    inn: str
    fio: str
    future: asyncio.Future["JobResult"]


@dataclass(frozen=True)
class JobResult:
    inn: str
    fio: str
    phone: str
    email: str
    status: str  # OK / NOT_FOUND / LIMIT / FORBIDDEN / FLOOD / ERROR
    safe_text: str


class PerUserQuota:
    """
    Sliding window quota: N requests per 3600s per user.
    Note: for Excel batch we count "1 token per file", not per row.
    """
    def __init__(self, per_hour: int):
        self.per_hour = max(1, int(per_hour))
        self._lock = asyncio.Lock()
        self._hits: dict[int, deque[float]] = {}

    async def allow(self, user_id: int) -> tuple[bool, float]:
        async with self._lock:
            now = time.monotonic()
            window = 3600.0
            q = self._hits.get(user_id)
            if q is None:
                q = deque()
                self._hits[user_id] = q

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


def _telethon_status_message(e: Exception) -> tuple[str, str]:
    """
    Returns (status_code, human_message).
    """
    if isinstance(e, FloodWaitError):
        return "FLOOD", f"⏳ Telegram попросил подождать ~{int(e.seconds)} сек. Попробуйте позже."
    if isinstance(e, SlowModeWaitError):
        return "FLOOD", f"⏳ В чате slow-mode. Подождите ~{int(e.seconds)} сек."
    if isinstance(e, PeerFloodError):
        return "FLOOD", "⚠️ На аккаунт наложены антиспам-ограничения. Нужна длительная пауза (несколько часов)."

    if isinstance(e, (ChatWriteForbiddenError, UserBannedInChannelError)):
        return "FORBIDDEN", "⛔ Запрет: аккаунту нельзя писать в этот чат/бот (бан/ограничение доступа)."
    if isinstance(e, UserIsBlockedError):
        return "FORBIDDEN", "⛔ Запрет: целевой бот/пользователь заблокировал аккаунт."

    if isinstance(e, RPCError):
        return "ERROR", f"❌ Ошибка Telegram: {e.__class__.__name__}"

    return "ERROR", f"❌ Ошибка: {e}"


async def _process_one(chat: MTProtoBotChat, inn: str, fio: str) -> JobResult:
    """
    Full flow:
      /inn <inn> -> if immediate NOT_FOUND -> stop
      wait edit -> find FIO button -> click -> collect -> summary
    """
    target_text = f"/inn {inn}"

    first = await chat.send_text_and_wait(target_text)

    if is_not_found_message(first.text):
        return JobResult(
            inn=inn,
            fio=fio,
            phone="",
            email="",
            status="NOT_FOUND",
            safe_text="❌ По данному запросу ничего не найдено.",
        )

    edited = await chat.wait_message_edit_until(
        first.message,
        min_buttons=1,
        timeout=18.0,
        quiet_timeout=2.5,
    )

    coords = chat.find_button_coords_by_text(edited, fio)
    if not coords:
        available = chat.buttons_flat(edited)
        msg = "❌ Не нашёл кнопку по ФИО.\n" + (
            "Доступные кнопки:\n" + "\n".join(f"• {b}" for b in available[:30]) if available else ""
        )
        return JobResult(
            inn=inn,
            fio=fio,
            phone="",
            email="",
            status="NOT_FOUND",
            safe_text=msg.strip(),
        )

    i, j = coords

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
        return JobResult(
            inn=inn,
            fio=fio,
            phone="",
            email="",
            status="LIMIT",
            safe_text="⚠️ Лимит запросов на день исчерпан. Попробуйте завтра.",
        )

    summary_msg = chat.find_summary_message(msgs)
    if not summary_msg:
        texts = [((m.message or "").strip()) for m in msgs if (m.message or "").strip()]
        raw = texts[-1] if texts else ""
        if raw and is_not_found_message(raw):
            return JobResult(
                inn=inn,
                fio=fio,
                phone="",
                email="",
                status="NOT_FOUND",
                safe_text="❌ По данному запросу ничего не найдено.",
            )

        safe = keep_only_fio_phone_email_masked(raw) if raw else "❌ После клика не удалось получить текст."
        fields = parse_summary_fields(raw) if raw else {"fio": "", "phone": "", "email": ""}
        phone = (fields.get("phone") or "").strip()
        email = (fields.get("email") or "").strip()

        status = "OK" if (phone or email) else "ERROR"
        return JobResult(
            inn=inn,
            fio=(fields.get("fio") or fio).strip(),
            phone=phone,
            email=email,
            status=status,
            safe_text=safe,
        )

    raw_text = (summary_msg.message or "").strip()
    if is_not_found_message(raw_text):
        return JobResult(
            inn=inn,
            fio=fio,
            phone="",
            email="",
            status="NOT_FOUND",
            safe_text="❌ По данному запросу ничего не найдено.",
        )

    safe = keep_only_fio_phone_email_masked(raw_text)
    fields = parse_summary_fields(raw_text)

    phone = (fields.get("phone") or "").strip()
    email = (fields.get("email") or "").strip()
    fio_out = (fields.get("fio") or fio).strip()

    status = "OK" if (phone or email) else "ERROR"
    return JobResult(
        inn=inn,
        fio=fio_out,
        phone=phone,
        email=email,
        status=status,
        safe_text=safe,
    )


async def _worker_loop(
    *,
    chat: MTProtoBotChat,
    queue: asyncio.Queue[Job],
    cache: SqliteTTLCache,
) -> None:
    while True:
        job = await queue.get()
        try:
            inn, fio = job.inn, job.fio
            key = _cache_key(inn, fio)

            cached = await cache.get(key)
            if cached:
                fields = parse_summary_fields(cached.value)
                phone = (fields.get("phone") or "").strip()
                email = (fields.get("email") or "").strip()
                fio_out = (fields.get("fio") or fio).strip()
                status = "OK" if (phone or email) else "OK"
                job.future.set_result(
                    JobResult(
                        inn=inn,
                        fio=fio_out,
                        phone=phone,
                        email=email,
                        status=status,
                        safe_text=cached.value,
                    )
                )
                continue

            res = await _process_one(chat, inn, fio)

            if res.status == "OK" and res.safe_text:
                await cache.set(key, res.safe_text)

            job.future.set_result(res)

        except Exception as e:
            st, msg = _telethon_status_message(e)
            job.future.set_result(
                JobResult(
                    inn=job.inn,
                    fio=job.fio,
                    phone="",
                    email="",
                    status=st,
                    safe_text=msg,
                )
            )
        finally:
            queue.task_done()


async def _enqueue_and_wait(queue: asyncio.Queue[Job], inn: str, fio: str) -> JobResult:
    loop = asyncio.get_running_loop()
    fut: asyncio.Future[JobResult] = loop.create_future()
    await queue.put(Job(inn=inn, fio=fio, future=fut))
    return await fut


async def _download_document_bytes(bot: Bot, message: Message) -> bytes:
    if not message.document:
        raise RuntimeError("No document")

    file = await bot.get_file(message.document.file_id)
    stream = await bot.download_file(file.file_path)
    return stream.read()


def build_proxy_dispatcher(
    control_bot: Bot,
    chat: MTProtoBotChat,
    settings: ProxySettings,
    cache: SqliteTTLCache,
) -> Dispatcher:
    dp = Dispatcher()

    quota = PerUserQuota(settings.user_quota_per_hour)
    queue: asyncio.Queue[Job] = asyncio.Queue(maxsize=settings.queue_maxsize)

    asyncio.create_task(_worker_loop(chat=chat, queue=queue, cache=cache))

    @dp.message(F.text.in_({"/start", "/help"}))
    async def help_cmd(message: Message) -> None:
        if settings.private_only and message.chat.type != "private":
            return
        await message.answer(
            "Бот готов.\n\n"
            "1) Ручной режим:\n"
            "ИНН; ФИО\n"
            "Пример: 2222058686; Маркова Ольга Викторовна\n\n"
            "2) Пакетный режим (Excel):\n"
            "Пришли .xlsx с колонками ИНН и ФИО.\n"
            "Верну output_YYYY-MM-DD_HH-MM.xlsx с колонками: ИНН, ФИО, Телефон, Email, Статус.\n"
            "Если дневной лимит исчерпан — дополнительно пришлю pending_YYYY-MM-DD_HH-MM.xlsx (остаток на завтра).\n"
        )

    @dp.message(F.document)
    async def handle_excel(message: Message) -> None:
        if settings.private_only and message.chat.type != "private":
            return
        if not message.from_user:
            await message.answer("❌ Не удалось определить пользователя.")
            return

        doc = message.document
        filename = (doc.file_name or "").lower()

        if not filename.endswith(".xlsx"):
            await message.answer("Пришли .xlsx файл (Excel).")
            return

        allowed, retry_after = await quota.allow(message.from_user.id)
        if not allowed:
            mins = int(retry_after // 60) + 1
            await message.answer(f"⏳ Слишком часто. Попробуйте через ~{mins} мин.")
            return

        status_msg = await message.answer("📥 Получил файл, читаю строки...")

        try:
            data = await _download_document_bytes(control_bot, message)
            rows = read_input_xlsx(data)
        except Exception as e:
            await status_msg.edit_text(f"❌ Не смог прочитать Excel: {e}")
            return

        if not rows:
            await status_msg.edit_text("Файл пустой: не нашёл строк с ИНН/ФИО.")
            return

        await status_msg.edit_text(f"Найдено строк: {len(rows)}. Начинаю обработку (последовательно)...")

        results: list[dict[str, str]] = []
        pending_rows: list[InputRow] = []
        limit_hit = False

        for idx0, r in enumerate(rows):  # 0-based index
            if limit_hit:
                pending_rows.append(r)
                continue

            key = _cache_key(r.inn, r.fio)
            cached = await cache.get(key)

            if cached:
                fields = parse_summary_fields(cached.value)
                phone = (fields.get("phone") or "").strip()
                email = (fields.get("email") or "").strip()
                fio_out = (fields.get("fio") or r.fio).strip()
                status = "OK" if (phone or email) else "OK"

                results.append(
                    {
                        "inn": r.inn,
                        "fio": fio_out or r.fio,
                        "phone": phone,
                        "email": email,
                        "status": status,
                    }
                )
            else:
                res = await _enqueue_and_wait(queue, r.inn, r.fio)

                results.append(
                    {
                        "inn": res.inn,
                        "fio": res.fio,
                        "phone": res.phone,
                        "email": res.email,
                        "status": res.status,
                    }
                )

                if res.status == "LIMIT":
                    limit_hit = True
                    pending_rows.extend(rows[idx0 + 1 :])
                    break

            done = idx0 + 1
            if done % 10 == 0 or done == len(rows):
                await status_msg.edit_text(f"⏳ Обработано {done}/{len(rows)}...")

        out_path = None
        out_filename = None
        pending_path = None
        pending_filename = None

        try:
            out_path, out_filename = write_output_xlsx(input_rows=rows, results=results)

            if pending_rows:
                pending_path, pending_filename = write_pending_xlsx(pending_rows=pending_rows)

            if limit_hit and not pending_rows:
                await status_msg.edit_text(
                    "⚠️ Лимит запросов исчерпан, но необработанных строк больше нет.\n"
                    f"Отправляю {out_filename}"
                )
                await message.answer_document(FSInputFile(out_path, filename=out_filename))
            elif pending_rows:
                await status_msg.edit_text(
                    "⚠️ Лимит запросов исчерпан.\n"
                    "Отправляю:\n"
                    f"• {out_filename}\n"
                    f"• {pending_filename} (остаток на завтра)"
                )
                await message.answer_document(FSInputFile(out_path, filename=out_filename))
                await message.answer_document(FSInputFile(pending_path, filename=pending_filename))
            else:
                await status_msg.edit_text(f"✅ Готово. Отправляю {out_filename}")
                await message.answer_document(FSInputFile(out_path, filename=out_filename))

        finally:
            for p in (out_path, pending_path):
                try:
                    if p and os.path.exists(p):
                        os.remove(p)
                except Exception:
                    pass

    @dp.message(F.text)
    async def handle_text(message: Message) -> None:
        if settings.private_only and message.chat.type != "private":
            return
        if not message.from_user:
            await message.answer("❌ Не удалось определить пользователя.")
            return

        user_text = (message.text or "").strip()
        if not user_text or user_text in ("/start", "/help"):
            return

        parsed = _parse_inn_and_fio(user_text)
        if not parsed:
            await message.answer("Неверный формат. Нужно: ИНН; ФИО\nПример: 2222058686; Маркова Ольга Викторовна")
            return

        allowed, retry_after = await quota.allow(message.from_user.id)
        if not allowed:
            mins = int(retry_after // 60) + 1
            await message.answer(f"⏳ Слишком много запросов. Попробуйте через ~{mins} мин.")
            return

        inn, fio = parsed
        key = _cache_key(inn, fio)

        cached = await cache.get(key)
        if cached:
            await message.answer(cached.value)
            return

        await message.answer(f"Принято. Поставил в очередь: {fio}")
        res = await _enqueue_and_wait(queue, inn, fio)
        await message.answer(res.safe_text)

    return dp