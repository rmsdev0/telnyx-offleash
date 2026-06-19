"""Shared test fakes and helpers.

FakeCall stands in for the Telnyx Call Control surface, recording the commands
the agent issues and (by default) completing each speak immediately by feeding
the matching speak.ended back to the agent. FakeLLM replays scripted rounds of
LLM events so a full conversation can run with no network.
"""

from __future__ import annotations

import asyncio
import json
from collections import deque
from typing import TYPE_CHECKING

import pytest

from offleash.settings import Settings
from offleash.telnyx import decode_client_state
from offleash.types import LLMEvent, LLMEventType, ToolCallRequest

if TYPE_CHECKING:
    from collections.abc import Iterable


@pytest.fixture
def settings() -> Settings:
    return Settings(
        telnyx_api_key="test-key",
        telnyx_public_key="test-pub",
        max_tokens_per_call=0,
    )


class FakeCall:
    """Records Call Control commands; optionally auto-completes speaks."""

    def __init__(self, *, auto_complete_speak: bool = True) -> None:
        self.id = "cc-test"
        self.actions: list[tuple] = []
        self.speaks: list[str] = []
        self.hung_up = False
        self.transcription_started = False
        self._auto = auto_complete_speak
        # Set by the test after the agent is constructed.
        self.agent = None

    async def answer(self) -> None:
        self.actions.append(("answer",))

    async def start_transcription(self) -> None:
        self.transcription_started = True
        self.actions.append(("start_transcription",))

    async def stop_transcription(self) -> None:
        self.actions.append(("stop_transcription",))

    async def speak(self, text: str, *, client_state: str) -> None:
        self.actions.append(("speak", text))
        self.speaks.append(text)
        if self._auto and self.agent is not None:
            generation = int(decode_client_state(client_state).split(":")[1])
            self.agent.submit_speak_ended(generation)

    async def stop_playback(self) -> None:
        self.actions.append(("stop_playback",))

    async def hangup(self) -> None:
        self.hung_up = True
        self.actions.append(("hangup",))


class FakeLLM:
    """Replays scripted rounds of LLM events, one round per stream call."""

    def __init__(self, rounds: Iterable[list[LLMEvent]]) -> None:
        self._rounds: deque[list[LLMEvent]] = deque(rounds)
        self.calls = 0

    async def stream_response(self, messages, *, system="", tools=None):  # noqa: ANN001
        self.calls += 1
        events = self._rounds.popleft() if self._rounds else []
        for event in events:
            yield event

    async def aclose(self) -> None:
        pass


def text_round(text: str) -> list[LLMEvent]:
    return [LLMEvent(type=LLMEventType.TEXT_DELTA, text=text)]


def tool_round(call_id: str, name: str, args: dict) -> list[LLMEvent]:
    return [
        LLMEvent(
            type=LLMEventType.TOOL_CALL,
            tool_call=ToolCallRequest(
                id=call_id, name=name, arguments=json.dumps(args)
            ),
        )
    ]


async def until(predicate, timeout: float = 2.0) -> None:
    """Wait until predicate() is truthy or fail after timeout."""
    loop = asyncio.get_event_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.005)
    raise AssertionError("condition not met within timeout")
