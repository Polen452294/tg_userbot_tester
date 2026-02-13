from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from aiogram import Bot, Dispatcher, F
from aiogram.types import Message

from userbot_tester.mtproto import MTProtoBotChat

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
    send_lock = asyncio.Lock()  # чтобы ответы не перемешивались

    @dp.message(F.text)
    async def relay_text(message: Message):
        if settings.private_only and message.chat.type != "private":
            return

        text = (message.text or "").strip()
        if not text:
            return

        # команды прокси-бота
        if text in ("/start", "/help"):
            await message.answer(
                "Прокси-бот готов.\n"
                "Напиши ИНН/текст — я отправлю целевому боту команду:\n"
                "/inn <твой текст>\n\n"
                "После ответа я автоматически нажму нижнюю кнопку (ФИО) во 2-м сообщении "
                "и верну ссылку на сайт.\n\n"
                "Команды:\n"
                "/whoami — показать твой user_id\n"
            )
            return

        if text == "/whoami":
            await message.answer(f"Ваш user_id: {message.from_user.id}")
            return

        target_text = f"/inn {text}"
        await message.answer(f"⏳ Отправляю: {target_text}")

        async with send_lock:
            try:
                first = await chat.send_text_and_wait(target_text)
            except Exception as e:
                log.exception("Relay failed")
                await message.answer(f"❌ Ошибка при запросе /inn: {e}")
                return

            # покажем текст первого ответа (по желанию)
            if first.text:
                await message.answer(first.text)

            # ждём, пока это же сообщение будет отредактировано и появятся 2 кнопки
            try:
                edited = await chat.wait_message_edit_until(
                    first.message,
                    min_buttons=2,      # у тебя “вторая кнопка” появляется после edit
                    timeout=12.0,
                    quiet_timeout=2.0,
                )
            except Exception as e:
                log.exception("Wait edit failed")
                await message.answer(f"❌ Не смог дождаться редактирования сообщения: {e}")
                return

            # теперь работаем с отредактированным сообщением
            try:
                url = await chat.open_bottom_button_url(edited)
            except Exception as e:
                log.exception("Open bottom button failed")
                await message.answer(f"❌ Не смог открыть нижнюю кнопку/достать ссылку: {e}")
                return

        await message.answer(f"🔗 Ссылка: {url}")

    return dp