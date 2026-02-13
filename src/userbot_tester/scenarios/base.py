from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, Optional

from userbot_tester.mtproto import MTProtoBotChat, BotReply


class Step(Protocol):
    name: str

    async def run(self, chat: MTProtoBotChat, last: Optional[BotReply]) -> BotReply: ...


@dataclass
class SendText:
    name: str
    text: str
    timeout: Optional[float] = None

    async def run(self, chat: MTProtoBotChat, last: Optional[BotReply]) -> BotReply:
        return await chat.send_text_and_wait(self.text, timeout=self.timeout)


@dataclass
class ClickButton:
    name: str
    button_text: str
    timeout: Optional[float] = None

    async def run(self, chat: MTProtoBotChat, last: Optional[BotReply]) -> BotReply:
        if last is None:
            raise RuntimeError("ClickButton requires previous reply with buttons")
        return await chat.click_button_and_wait(last, self.button_text, timeout=self.timeout)


@dataclass
class AssertContains:
    name: str
    needle: str

    async def run(self, chat: MTProtoBotChat, last: Optional[BotReply]) -> BotReply:
        if last is None:
            raise RuntimeError("AssertContains requires previous reply")
        if self.needle not in (last.text or ""):
            raise AssertionError(f"Expected '{self.needle}' to be in reply, got: {last.text!r}")
        return last