from __future__ import annotations

import asyncio
import random
from collections.abc import AsyncIterator
from typing import Any

import pytest

from middlemanagementd.config import Config
from middlemanagementd.llm import LLMError

JARGON = (
    "Absolutely. At a high level, we're leveraging cross-functional synergies to drive alignment with our "
    "north star. I'm confident our stakeholders have full visibility into the roadmap, and going forward "
    "we'll double-click on the key deliverables to move the needle and unlock scalable value-add."
)
PLAIN = "I fixed the login bug yesterday and merged the pull request. Next I am writing tests for 2 modules."


class FakeLLM:
    """Stands in for LLMClient. `chat_reply`/`stream_reply` may be strings or exceptions."""

    def __init__(self, chat_reply: Any = None, stream_reply: Any = "Great work. Truly visionary.") -> None:
        self.enabled = True
        self.chat_reply = chat_reply
        self.stream_reply = stream_reply
        self.chat_calls: list[list[dict[str, str]]] = []
        self.stream_calls: list[list[dict[str, str]]] = []

    async def chat(self, messages: list[dict[str, str]], **kw: Any) -> str:
        self.chat_calls.append(messages)
        await asyncio.sleep(0.01)  # long enough for hold music to start
        if isinstance(self.chat_reply, Exception):
            raise self.chat_reply
        if self.chat_reply is None:
            raise LLMError("no reply configured")
        return self.chat_reply

    async def stream(self, messages: list[dict[str, str]], **kw: Any) -> AsyncIterator[str]:
        self.stream_calls.append(messages)
        if isinstance(self.stream_reply, Exception):
            raise self.stream_reply
        for word in self.stream_reply.split(" "):
            yield word + " "

    async def close(self) -> None:
        pass


@pytest.fixture
def cfg(tmp_path: Any) -> Config:
    c = Config()
    c.storage.database_path = str(tmp_path / "test.db")
    c.audio.hold_music = True
    c.personality.default = "passive_aggressive"
    c.practice.word_of_day = False  # today's word is date-seeded; tests that want it opt in
    return c


@pytest.fixture
def rng() -> random.Random:
    return random.Random(1234)
