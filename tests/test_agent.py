"""End-to-end agent loop tests using fakes for the Telnyx Call and LLM."""

from __future__ import annotations

import asyncio
import zlib
from collections import deque

import pytest

from offleash.agent import VoiceAgent
from offleash.prompts import RESTAURANT_CONFIG
from offleash.settings import Settings
from tests.conftest import FakeCall, FakeLLM, text_round, tool_round, until


class RaisingLLM:
    """An LLM whose stream always fails, to exercise the fallback path."""

    def __init__(self) -> None:
        self.calls = 0

    async def stream_response(self, messages, *, system="", tools=None):  # noqa: ANN001
        self.calls += 1
        raise RuntimeError("llm boom")
        yield  # pragma: no cover  (makes this an async generator)

    async def aclose(self) -> None:
        pass


class GatedLLM:
    """Replays scripted rounds, but blocks the first call until released.

    Lets a turn sit in the PROCESSING phase (no speak yet) so a second turn can
    arrive and exercise the supersede-and-reap path rather than barge-in.
    """

    def __init__(self, rounds) -> None:  # noqa: ANN001
        self._rounds: deque = deque(rounds)
        self.calls = 0
        self.gate = asyncio.Event()

    async def stream_response(self, messages, *, system="", tools=None):  # noqa: ANN001
        self.calls += 1
        # Consume this call's round before gating, so a cancelled first turn does
        # not leave its round for the superseding turn to pick up.
        events = self._rounds.popleft() if self._rounds else []
        if self.calls == 1:
            await self.gate.wait()
        for event in events:
            yield event

    async def aclose(self) -> None:
        pass


def _make_agent(settings, fake_call, rounds):
    agent = VoiceAgent(settings, fake_call, RESTAURANT_CONFIG)
    agent._llm = FakeLLM(rounds)  # type: ignore[assignment]
    agent.set_call_info(fake_call.id, "+15551234567")
    fake_call.agent = agent
    return agent


@pytest.mark.asyncio
async def test_full_reservation_flow(settings) -> None:
    fake = FakeCall(auto_complete_speak=True)
    rounds = [
        # Turn 1: caller gives all details -> check_availability -> ask for name
        tool_round(
            "c1",
            "check_availability",
            {"date": "Friday", "time": "7 PM", "party_size": 4},
        ),
        text_round("Great. What name should I put the table under?"),
        # Turn 2: caller gives name -> make_reservation -> farewell + hang up
        tool_round("c2", "make_reservation", {"name": "Alex"}),
        text_round("You're all set, Alex. See you Friday. Goodbye!"),
    ]
    agent = _make_agent(settings, fake, rounds)
    agent.start()

    # Greeting plays first and completes before the caller speaks.
    await until(lambda: fake.speaks and not agent._barge_in.agent_is_speaking)
    assert fake.actions[0] == ("start_transcription",)
    assert fake.speaks[0] == RESTAURANT_CONFIG.greeting
    assert agent._context.current_node == "booking"

    # Turn 1
    agent.submit_transcript("Book a table for four this Friday at 7 PM.", is_final=True)
    await until(lambda: agent._context.current_node == "confirm")
    await until(lambda: any("name" in s.lower() for s in fake.speaks))
    assert agent._context.slots == {"date": "Friday", "time": "7 PM", "party_size": 4}

    # Turn 2
    agent.submit_transcript("The name is Alex.", is_final=True)
    await until(lambda: agent._context.current_node == "farewell")
    await until(lambda: fake.hung_up)

    # The reservation tool result carries the deterministic confirmation number.
    expected = f"GF-{zlib.crc32(b'Alex') % 10000:04d}"
    transcript = " ".join(m["content"] for m in agent._conversation.to_log_dict())
    assert expected in transcript

    # Telnyx would now post call.hangup; simulate it to end the loop cleanly.
    agent.submit_hangup()
    await asyncio.wait_for(agent.run_task, timeout=2.0)

    assert agent.run_task.done()
    assert agent.run_task.exception() is None
    assert ("stop_transcription",) in fake.actions
    assert ("hangup",) in fake.actions


@pytest.mark.asyncio
async def test_barge_in_stops_playback(settings) -> None:
    # Greeting does not auto-complete, so the agent stays "speaking" and the
    # caller can interrupt it.
    fake = FakeCall(auto_complete_speak=False)
    agent = _make_agent(settings, fake, rounds=[])
    agent.start()

    await until(lambda: agent._barge_in.agent_is_speaking)
    assert fake.speaks[0] == RESTAURANT_CONFIG.greeting

    # An interim transcript while speaking triggers barge-in.
    agent.submit_transcript("actually wait", is_final=False)
    await until(lambda: ("stop_playback",) in fake.actions)
    await until(lambda: not agent._barge_in.agent_is_speaking)

    # Clean teardown.
    agent.submit_hangup()
    await asyncio.wait_for(agent.run_task, timeout=2.0)
    assert agent.run_task.exception() is None


@pytest.mark.asyncio
async def test_interrupted_turn_is_dropped_when_nothing_spoken(settings) -> None:
    # A barge-in during the greeting leaves the greeting out of history (it is
    # never recorded) and starts no assistant turn.
    fake = FakeCall(auto_complete_speak=False)
    agent = _make_agent(settings, fake, rounds=[])
    agent.start()

    await until(lambda: agent._barge_in.agent_is_speaking)
    agent.submit_transcript("stop", is_final=False)
    await until(lambda: not agent._barge_in.agent_is_speaking)

    agent.submit_hangup()
    await asyncio.wait_for(agent.run_task, timeout=2.0)

    roles = [m["role"] for m in agent._conversation.to_log_dict()]
    assert "assistant" not in roles


@pytest.mark.asyncio
async def test_clean_shutdown_no_pending_tasks(settings) -> None:
    fake = FakeCall(auto_complete_speak=True)
    agent = _make_agent(settings, fake, rounds=[])
    agent.start()
    await until(lambda: fake.speaks and not agent._barge_in.agent_is_speaking)

    agent.submit_hangup()
    await asyncio.wait_for(agent.run_task, timeout=2.0)

    # The per-turn task (the greeting) is finished, not orphaned.
    assert agent._response_task is None or agent._response_task.done()


@pytest.mark.asyncio
async def test_speak_completes_only_on_matching_generation(settings) -> None:
    # The greeting does not auto-complete, so we drive speak.ended by hand.
    fake = FakeCall(auto_complete_speak=False)
    agent = _make_agent(settings, fake, rounds=[])
    agent.start()

    await until(lambda: agent._barge_in.agent_is_speaking)
    live_gen = agent._current_speak_gen

    # A completion for a different generation (a stale, barged-in speak) must
    # not release the live speak.
    agent.submit_speak_ended(live_gen + 1)
    await asyncio.sleep(0.02)
    assert agent._barge_in.agent_is_speaking

    # The matching generation releases it.
    agent.submit_speak_ended(live_gen)
    await until(lambda: not agent._barge_in.agent_is_speaking)

    agent.submit_hangup()
    await asyncio.wait_for(agent.run_task, timeout=2.0)
    assert agent.run_task.exception() is None


@pytest.mark.asyncio
async def test_token_budget_announced_once_then_turns_skipped() -> None:
    # A tiny budget is exhausted by the first turn's prompt accounting, before
    # any LLM call.
    budget_settings = Settings(
        telnyx_api_key="k", telnyx_public_key="p", max_tokens_per_call=1
    )
    fake = FakeCall(auto_complete_speak=True)
    llm = FakeLLM([text_round("unused")])
    agent = VoiceAgent(budget_settings, fake, RESTAURANT_CONFIG)
    agent._llm = llm  # type: ignore[assignment]
    agent.set_call_info(fake.id, "+15551234567")
    fake.agent = agent
    agent.start()
    await until(lambda: fake.speaks and not agent._barge_in.agent_is_speaking)

    # Turn 1: over budget -> speak the budget message once, no LLM call.
    agent.submit_transcript("I want a table.", is_final=True)
    await until(
        lambda: RESTAURANT_CONFIG.budget_exceeded_message in fake.speaks
    )
    assert agent._budget_announced
    spoken_after_turn1 = len(fake.speaks)

    # Turn 2: still over budget and already announced -> skipped entirely.
    agent.submit_transcript("Hello?", is_final=True)
    await asyncio.sleep(0.05)
    assert len(fake.speaks) == spoken_after_turn1
    assert llm.calls == 0

    agent.submit_hangup()
    await asyncio.wait_for(agent.run_task, timeout=2.0)


@pytest.mark.asyncio
async def test_tool_round_cap_falls_back(settings) -> None:
    # The LLM keeps requesting a tool past max_tool_rounds; the agent gives up
    # and speaks the fallback instead of looping forever.
    args = {"date": "Mon", "time": "7", "party_size": 2}
    rounds = [
        tool_round(f"c{i}", "check_availability", args)
        for i in range(RESTAURANT_CONFIG.max_tool_rounds + 1)
    ]
    fake = FakeCall(auto_complete_speak=True)
    agent = _make_agent(settings, fake, rounds)
    agent.start()
    await until(lambda: fake.speaks and not agent._barge_in.agent_is_speaking)

    agent.submit_transcript("Book something.", is_final=True)
    await until(lambda: RESTAURANT_CONFIG.fallback_message in fake.speaks)

    agent.submit_hangup()
    await asyncio.wait_for(agent.run_task, timeout=2.0)
    assert agent.run_task.exception() is None


@pytest.mark.asyncio
async def test_llm_failure_speaks_fallback_without_crashing(settings) -> None:
    fake = FakeCall(auto_complete_speak=True)
    agent = VoiceAgent(settings, fake, RESTAURANT_CONFIG)
    agent._llm = RaisingLLM()  # type: ignore[assignment]
    agent.set_call_info(fake.id, "+15551234567")
    fake.agent = agent
    agent.start()
    await until(lambda: fake.speaks and not agent._barge_in.agent_is_speaking)

    agent.submit_transcript("Anything.", is_final=True)
    await until(lambda: RESTAURANT_CONFIG.fallback_message in fake.speaks)
    # The turn returns to listening and the run task is alive.
    await until(lambda: not agent._barge_in.agent_is_speaking)
    assert not agent.run_task.done()

    agent.submit_hangup()
    await asyncio.wait_for(agent.run_task, timeout=2.0)
    assert agent.run_task.exception() is None


@pytest.mark.asyncio
async def test_run_loop_fault_hangs_up_and_tears_down(settings) -> None:
    fake = FakeCall(auto_complete_speak=True)
    agent = _make_agent(settings, fake, rounds=[text_round("hi")])
    agent.start()
    await until(lambda: fake.speaks and not agent._barge_in.agent_is_speaking)

    # Force the loop's turn handling to raise on the next transcript.
    def boom(_event):
        raise RuntimeError("loop boom")

    agent._turn_manager.handle_event = boom  # type: ignore[assignment]
    agent.submit_transcript("trigger", is_final=True)

    await asyncio.wait_for(agent.run_task, timeout=2.0)
    # The fault is caught: the call is hung up and resources are torn down.
    assert agent.run_task.exception() is None
    assert fake.hung_up
    assert ("stop_transcription",) in fake.actions
    assert agent._closed


@pytest.mark.asyncio
async def test_new_turn_supersedes_in_flight_response(settings) -> None:
    # The first turn is gated in the LLM (PROCESSING, not speaking) so a second
    # turn supersedes and reaps it rather than triggering barge-in.
    fake = FakeCall(auto_complete_speak=True)
    gated = GatedLLM([text_round("FIRST"), text_round("SECOND")])
    agent = VoiceAgent(settings, fake, RESTAURANT_CONFIG)
    agent._llm = gated  # type: ignore[assignment]
    agent.set_call_info(fake.id, "+15551234567")
    fake.agent = agent
    agent.start()
    await until(lambda: fake.speaks and not agent._barge_in.agent_is_speaking)

    # Turn 1 enters the LLM and blocks there.
    agent.submit_transcript("first request", is_final=True)
    await until(lambda: gated.calls == 1)
    assert not agent._barge_in.agent_is_speaking  # still PROCESSING, not speaking

    # Turn 2 supersedes the gated turn 1.
    agent.submit_transcript("second request", is_final=True)
    await until(lambda: "SECOND" in fake.speaks)
    assert "FIRST" not in fake.speaks

    agent.submit_hangup()
    await asyncio.wait_for(agent.run_task, timeout=2.0)
    assert agent.run_task.exception() is None


@pytest.mark.asyncio
async def test_barge_in_then_next_turn_is_handled(settings) -> None:
    # Barge-in must not wedge the agent: a final after the interruption drives a
    # real new turn.
    fake = FakeCall(auto_complete_speak=True)
    agent = _make_agent(settings, fake, rounds=[text_round("Sure, one moment.")])
    agent.start()
    await until(lambda: fake.speaks and not agent._barge_in.agent_is_speaking)

    # Make the next greeting-less turn speak: first force a speaking state by
    # starting a turn whose speak does not auto-complete.
    fake._auto = False
    # Interim while not speaking is ignored (no barge-in, no turn).
    agent.submit_transcript("uh", is_final=False)
    await asyncio.sleep(0.02)

    # Drive a real turn: re-enable completion and submit a final.
    fake._auto = True
    agent.submit_transcript("Tell me the specials.", is_final=True)
    await until(lambda: "Sure, one moment." in fake.speaks)

    agent.submit_hangup()
    await asyncio.wait_for(agent.run_task, timeout=2.0)
    assert agent.run_task.exception() is None
