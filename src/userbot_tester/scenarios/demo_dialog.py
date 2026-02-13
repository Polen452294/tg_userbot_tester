from __future__ import annotations

from userbot_tester.scenarios.base import SendText, AssertContains, ClickButton


def build_demo_steps():
    return [
        SendText(name="start", text="/start"),
        # поменяй на текст, который реально бывает в твоём боте:
        AssertContains(name="check_welcome", needle=""),
        # пример клика по кнопке (если у бота реально есть такая кнопка):
        # ClickButton(name="open_catalog", button_text="Каталог"),
    ]