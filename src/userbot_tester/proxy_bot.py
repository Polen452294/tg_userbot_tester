from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from aiogram import Bot, Dispatcher, F
from aiogram.types import Message

from userbot_tester.mtproto import MTProtoBotChat, keep_only_fio_phone_email_masked

log = logging.getLogger("proxy_bot")


@dataclass
class ProxySettings:
    private_only: bool


def _parse_inn_and_fio(text: str) -> tuple[str, str] | None:
    """
    Ожидаем формат: INN; FIO
    """
    if ";" not in text:
        return None
    inn, fio = text.split(";", 1)
    inn = inn.strip()
    fio = fio.strip()
    if not inn or not fio:
        return None
    return inn, fio


def build_proxy_dispatcher(
    control_bot: Bot,
    chat: MTProtoBotChat,
    settings: ProxySettings,
) -> Dispatcher:
    dp = Dispatcher()
    send_lock = asyncio.Lock()

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
                "2222058686; Маркова Ольга Викторовна\n\n"
            )
            return

        parsed = _parse_inn_and_fio(user_text)
        if not parsed:
            await message.answer("Неверный формат. Нужно: ИНН; ФИО\nПример: 2222058686; Маркова Ольга Викторовна")
            return

        inn, fio = parsed
        target_text = f"/inn {inn}"
        await message.answer(f"Ищу: {fio}...")

        async with send_lock:
            # 1) /inn -> первый ответ
            try:
                first = await chat.send_text_and_wait(target_text)
            except Exception as e:
                log.exception("Error sending /inn")
                await message.answer(f"❌ Ошибка запроса: {e}")
                return

            # 2) дождаться edits (появятся кнопки)
            try:
                edited = await chat.wait_message_edit_until(
                    first.message,
                    min_buttons=1,   # иногда кнопок может быть сразу много, но нам главное дождаться появления
                    timeout=18.0,
                    quiet_timeout=2.5,
                )
            except Exception as e:
                log.exception("Edit wait failed")
                await message.answer(f"❌ Не дождался кнопок/редактирования: {e}")
                return

            # 3) найти нужную кнопку по ФИО
            coords = chat.find_button_coords_by_text(edited, fio)
            if not coords:
                available = chat.buttons_flat(edited)
                await message.answer(
                    "❌ Не нашёл кнопку по ФИО.\n"
                    "Доступные кнопки:\n" + "\n".join(f"• {b}" for b in available[:30])
                )
                return

            i, j = coords

            # 4) кликнуть и собрать ответы/редакты после клика
            try:
                msgs = await chat.click_button_and_collect(
                    edited,
                    i=i,
                    j=j,
                    collect_timeout=4,
                    idle_timeout=0.8,
                    max_events=5,
                )
            except Exception as e:
                log.exception("Click/collect failed")
                await message.answer(f"❌ Ошибка после нажатия кнопки: {e}")
                return
            
            # ✅ НОВОЕ: если лимит исчерпан — сообщаем и выходим
            limit_msg = chat.find_limit_message(msgs)
            if limit_msg:
                await message.answer("⚠️ Лимит запросов на день исчерпан. Попробуйте завтра.")
                return

        # 5) найти "📄 Краткая сводка"
        summary_msg = chat.find_summary_message(msgs)
        if not summary_msg:
            texts = [((m.message or "").strip()) for m in msgs if (m.message or "").strip()]
            if texts:
                await message.answer("Получены сообщения после клика, но '📄 Краткая сводка' не найдена.")
                await message.answer(keep_only_fio_phone_email_masked(texts[-1]))
            else:
                await message.answer("После клика не удалось получить текстовые сообщения.")
            return

        raw_text = (summary_msg.message or "").strip()
        await message.answer(keep_only_fio_phone_email_masked(raw_text))

    return dp