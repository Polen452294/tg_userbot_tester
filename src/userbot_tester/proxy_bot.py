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
                "Прокси-бот готов.\n"
                "Напиши значение — я отправлю целевому боту команду /inn <значение>,\n"
                "дождусь редактирования (появления 2 кнопок), нажму нижнюю кнопку.\n"
                "Если придёт сообщение '📄 Краткая сводка' — перешлю его в замаскированном виде."
            )
            return

        target_text = f"/inn {user_text}"
        await message.answer(f"⏳ Отправляю: {target_text}")

        async with send_lock:
            # 1) /inn -> первый ответ
            try:
                first = await chat.send_text_and_wait(target_text)
            except Exception as e:
                log.exception("Error sending /inn")
                await message.answer(f"❌ Ошибка запроса: {e}")
                return

            if first.text:
                await message.answer(first.text)

            # 2) ждём edit первого ответа (появятся 2 кнопки)
            try:
                edited = await chat.wait_message_edit_until(
                    first.message,
                    min_buttons=2,
                    timeout=15.0,
                    quiet_timeout=2.5,
                )
            except Exception as e:
                log.exception("Edit wait failed")
                await message.answer(f"❌ Не дождался редактирования: {e}")
                return

            # 3) кликаем нижнюю кнопку и собираем ответы/редактирования
            try:
                msgs = await chat.click_bottom_button_and_collect(
                    edited,
                    collect_timeout=15.0,
                    idle_timeout=2.5,
                    max_events=12,
                )
            except Exception as e:
                log.exception("Click/collect failed")
                await message.answer(f"❌ Ошибка после нажатия кнопки: {e}")
                return

        # 4) ищем именно "📄 Краткая сводка"
        summary_msg = chat.find_summary_message(msgs)
        if not summary_msg:
            # на всякий случай покажем, что что-то пришло
            texts = [((m.message or "").strip()) for m in msgs if (m.message or "").strip()]
            if texts:
                await message.answer("Получены сообщения после клика, но '📄 Краткая сводка' не найдена.")
                # можно вывести последнее (тоже лучше с маской)
                await message.answer(keep_only_fio_phone_email_masked(texts[-1]))
            else:
                await message.answer("После клика не удалось получить текстовые сообщения.")
            return

        # 5) пересылаем пользователю, но с маскировкой PII
        raw_text = (summary_msg.message or "").strip()
        await message.answer(keep_only_fio_phone_email_masked(raw_text))

    return dp