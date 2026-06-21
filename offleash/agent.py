"""Agent orchestration for a single Telnyx call.

Lifted from the voice-agent-lite core, with the I/O boundary collapsed onto
Telnyx call commands (Option A in DISCOVERY.md):

- Audio in: server.py turns call.transcription webhooks into STT events and
  pushes them onto this agent's queue. The run loop consumes that queue.
- Audio out: a finished response is spoken with one speak command. The run
  loop awaits the matching call.speak.ended before the turn completes.
- Barge-in: a transcript arriving while the agent is speaking issues
  playback_stop, cancels the in-flight response task, and returns to listening.
- LLM: the direct Telnyx inference client.

The conversation history, the tool loop with flow nodes, the interrupted-turn
rollback, the token budget, and the turn manager are all kept intact.
"""

from __future__ import annotations

import asyncio
import contextlib
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import structlog

from offleash import metrics
from offleash.barge_in import BargeInHandler
from offleash.conversation import CallContext, Conversation
from offleash.limits import TokenBudget
from offleash.prompts import build_system_prompt
from offleash.retry import retry_stream
from offleash.telnyx import TelnyxLLM, encode_client_state
from offleash.turn_manager import TurnManager
from offleash.types import (
    LLMEvent,
    LLMEventType,
    LLMMessage,
    STTEvent,
    STTEventType,
    ToolCallRequest,
)

if TYPE_CHECKING:
    from collections.abc import AsyncIterator

    from offleash.prompts import AgentConfig, FlowNode
    from offleash.settings import Settings
    from offleash.telnyx import Call

log = structlog.get_logger()


@dataclass(frozen=True, slots=True)
class SpeakEnded:
    """Signals that a speak command finished playing.

    generation correlates with the speak that produced it (via client_state).
    None means the correlation id could not be read; it never matches a live
    speak, so such a completion is ignored and the speak timeout takes over.
    """

    generation: int | None


# Sentinel pushed onto the queue to end the run loop (caller or agent hangup).
_HANGUP = object()

# Queue items: an STTEvent (transcript), a SpeakEnded, or the _HANGUP sentinel.
_QueueItem = object


class VoiceAgent:
    """Orchestrates a single voice call through the Telnyx call-command loop."""

    def __init__(
        self,
        settings: Settings,
        call: Call,
        agent_config: AgentConfig,
    ) -> None:
        self._settings = settings
        self._call = call
        self._config = agent_config

        # Per-call state
        self._context = CallContext()
        self._conversation = Conversation()
        self._turn_manager = TurnManager()
        # Set when the conversation should end (terminal flow node); the call
        # hangs up after the final reply finishes playing.
        self._ending = False
        self._barge_in = BargeInHandler()

        # The only provider left after the single-vendor collapse.
        self._llm = TelnyxLLM(settings)

        # Spend protection: estimated token budget for this call
        self._budget = TokenBudget(settings.max_tokens_per_call)
        self._budget_announced = False

        # Event source: server.py pushes transcripts, speak-completions, and
        # the hangup sentinel here.
        self._events: asyncio.Queue[_QueueItem] = asyncio.Queue()

        # Tasks
        self._run_task: asyncio.Task[None] | None = None
        self._response_task: asyncio.Task[None] | None = None

        # Set once the LLM client has been closed, so aclose() is idempotent
        # whether teardown runs from the run loop or from the server registry.
        self._closed = False

        # speak completion tracking. Each speak gets a monotonically increasing
        # generation carried in client_state, so a stale speak.ended (for a
        # speak we already interrupted) cannot complete a later one.
        self._speak_gen = 0
        self._current_speak_gen = -1
        self._speak_done = asyncio.Event()

    # ── Setup and event submission (called by server.py) ─────────

    def set_call_info(self, call_sid: str, from_number: str) -> None:
        self._context.call_sid = call_sid
        self._context.from_number = from_number
        if self._config.initial_node:
            self._context.current_node = self._config.initial_node

    def start(self) -> asyncio.Task[None]:
        """Begin the run loop. Called once the call is answered."""
        self._run_task = asyncio.create_task(self.run())
        return self._run_task

    @property
    def run_task(self) -> asyncio.Task[None] | None:
        return self._run_task

    def submit_transcript(self, transcript: str, is_final: bool) -> None:
        """Push a Telnyx transcription as STT events.

        A final transcript is followed by a synthesized UtteranceEnd so the
        turn manager emits it as one complete turn.
        """
        if is_final:
            self._events.put_nowait(
                STTEvent(STTEventType.TRANSCRIPT_FINAL, transcript)
            )
            self._events.put_nowait(STTEvent(STTEventType.UTTERANCE_END))
        else:
            self._events.put_nowait(
                STTEvent(STTEventType.TRANSCRIPT_INTERIM, transcript)
            )

    def submit_speak_ended(self, generation: int | None) -> None:
        self._events.put_nowait(SpeakEnded(generation))

    def submit_hangup(self) -> None:
        self._events.put_nowait(_HANGUP)

    # ── Main loop ────────────────────────────────────────────────

    async def run(self) -> None:
        """Drive the call: start transcription, greet, then process events."""
        log.info("agent.call_started", config=self._config.name)
        try:
            try:
                await self._call.start_transcription()
                log.info("transcription.started")
            except Exception:
                # Without STT the caller cannot be heard. Do not leave them in
                # dead air: end the call once the greeting has played (or right
                # away if there is no greeting). _greet hangs up when _ending.
                log.exception("agent.transcription_start_failed")
                self._ending = True

            # Greet first if configured, as a cancellable task so a caller who
            # starts talking immediately can barge in.
            if self._config.greeting:
                self._response_task = asyncio.create_task(self._greet())
            elif self._ending:
                with contextlib.suppress(Exception):
                    await self._call.hangup()
                return

            while True:
                item = await self._events.get()
                if item is _HANGUP:
                    log.info("agent.hangup_received")
                    break
                if isinstance(item, SpeakEnded):
                    self._on_speak_ended(item.generation)
                    continue

                assert isinstance(item, STTEvent)
                event = item

                # Once the conversation is ending, ignore further input so
                # background noise cannot start a new turn before the hangup.
                if self._ending:
                    continue

                # Barge-in: a transcript (interim or final) while the agent is
                # speaking. Interim is the snappier trigger and fires first.
                is_barge_in_signal = event.type in (
                    STTEventType.TRANSCRIPT_INTERIM,
                    STTEventType.TRANSCRIPT_FINAL,
                )
                if is_barge_in_signal and self._barge_in.agent_is_speaking:
                    if metrics.enabled():
                        # Component 2 boundary: the moment our code decides to
                        # stop, just before the playback_stop POST goes out.
                        metrics.record(
                            "barge_stop_issued",
                            call=self._call.id,
                            t_mono=time.monotonic(),
                            t_wall=time.time(),
                        )
                    await self._barge_in.handle_barge_in(self._call.stop_playback)
                    if metrics.enabled():
                        # The POST has returned; the leg-level stop is now in
                        # flight (its completion shows up as call.speak.ended).
                        metrics.record(
                            "barge_stop_returned",
                            call=self._call.id,
                            t_mono=time.monotonic(),
                            t_wall=time.time(),
                        )
                    if self._response_task and not self._response_task.done():
                        self._response_task.cancel()
                    self._turn_manager.set_listening()
                    continue

                user_turn = self._turn_manager.handle_event(event)
                if user_turn:
                    # A newly completed turn supersedes any response still in
                    # flight (the caller kept talking past the previous final).
                    # Cancel and reap it first so only one response runs at a
                    # time; overlapping responses would corrupt the shared
                    # speak-completion state and wedge agent_is_speaking on.
                    if self._response_task and not self._response_task.done():
                        log.info("agent.superseding_response")
                        self._response_task.cancel()
                        # Suppress only Exception, not CancelledError: the reaped
                        # task returns normally, but if our own run task is being
                        # cancelled (shutdown) that must propagate, not be eaten.
                        with contextlib.suppress(Exception):
                            await self._response_task
                    self._conversation.add_user_turn(user_turn)
                    self._response_task = asyncio.create_task(self._generate_response())
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("agent.run_failed")
            with contextlib.suppress(Exception):
                await self._call.hangup()
        finally:
            await self._teardown()

    async def _teardown(self) -> None:
        """Cancel the in-flight turn and release call resources cleanly."""
        if self._response_task and not self._response_task.done():
            self._response_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await self._response_task
        with contextlib.suppress(Exception):
            await self._call.stop_transcription()
        await self.aclose()
        log.info("agent.call_ended", conversation=self._conversation.to_log_dict())

    async def aclose(self) -> None:
        """Close the LLM client. Idempotent.

        The server calls this for agents that were registered but never started
        (a hangup before answer, or shutdown), whose run loop and _teardown
        never ran, so the client would otherwise leak.
        """
        if self._closed:
            return
        self._closed = True
        with contextlib.suppress(Exception):
            await self._llm.aclose()

    def _on_speak_ended(self, generation: int | None) -> None:
        """Release the speak waiter only if this completion matches the live speak.

        We always send a parseable client_state ("speak:N"), so a completion for
        a different generation (a stale speak.ended from a barged-in speak, or an
        unparseable None) must not release a later speak. The speak timeout is the
        safety net if a matching completion never arrives.
        """
        if generation == self._current_speak_gen:
            self._speak_done.set()

    # ── Response generation ──────────────────────────────────────

    def _build_tools_schemas(self) -> list[dict[str, Any]] | None:
        """Tools offered to the LLM for the current flow node.

        Recomputed whenever the node may have changed (turn start and after a
        transition) so per-node tool gating applies. A node with an empty tool
        list (the terminal farewell node) offers none.
        """
        if not self._config.tools:
            return None
        node = self._get_current_node()
        if node and node.tools is not None:
            schemas = list(self._config.tools.subset_schemas(node.tools))
        else:
            schemas = list(self._config.tools.get_schemas())
        return schemas or None

    async def _generate_response(self) -> None:
        """Run the LLM, the tool loop, then speak the reply for one turn."""
        t_start = time.monotonic()

        # Spend protection: once the call's token budget is gone, tell the
        # caller once and stop making LLM calls for this call.
        budget_message: str | None = None
        if self._budget.exhausted:
            if self._budget_announced:
                log.warning(
                    "agent.turn_skipped_budget_exhausted", used=self._budget.used
                )
                return
            self._budget_announced = True
            log.warning("agent.token_budget_exhausted", used=self._budget.used)
            budget_message = self._config.budget_exceeded_message

        system_prompt = build_system_prompt(
            self._config,
            self._context,
            self._context.current_node,
        )

        tools_schemas = self._build_tools_schemas()
        messages = self._conversation.messages

        full_text = ""
        # Tokens handed to the speak command; on barge-in this is what the
        # caller was being told when they interrupted.
        spoken_tokens: list[str] = []
        tool_rounds = 0
        try:
            if budget_message is not None:
                full_text = budget_message
                await self._speak([full_text], spoken_tokens)
                return

            while True:
                # Account for the prompt we are about to send; a tool loop
                # re-sends history each round, like real billing.
                self._budget.record_text(system_prompt)
                for m in messages:
                    self._budget.record_text(m.content)
                if self._budget.exhausted:
                    self._budget_announced = True
                    log.warning("agent.token_budget_exhausted", used=self._budget.used)
                    full_text = self._config.budget_exceeded_message
                    await self._speak([full_text], spoken_tokens)
                    break

                text_tokens: list[str] = []
                tool_calls: list[ToolCallRequest] = []
                t_first_token = None

                # Retried only while nothing has streamed back yet, so a retry
                # can never produce duplicated text.
                def open_llm_stream(
                    m: list[LLMMessage] = messages,
                    s: str = system_prompt,
                    t: list[dict[str, Any]] | None = tools_schemas,
                ) -> AsyncIterator[LLMEvent]:
                    return self._llm.stream_response(m, system=s, tools=t)

                async for event in retry_stream(
                    open_llm_stream,
                    max_retries=self._settings.stream_max_retries,
                    backoff_s=self._settings.stream_retry_backoff_s,
                    name="llm",
                ):
                    if event.type == LLMEventType.TEXT_DELTA:
                        if t_first_token is None:
                            t_first_token = time.monotonic()
                            ttft_ms = round((t_first_token - t_start) * 1000)
                            log.info("latency.llm_ttft", ms=ttft_ms)
                        text_tokens.append(event.text)
                        self._budget.record_text(event.text)
                    elif event.type == LLMEventType.TOOL_CALL and event.tool_call:
                        tool_calls.append(event.tool_call)
                        self._budget.record_text(event.tool_call.arguments)

                # Only loop on tools we can act on. A hallucinated tool name
                # without a registry would otherwise burn rounds; instead end
                # the turn on whatever text came back.
                if tool_calls and self._config.tools:
                    tool_rounds += 1
                    if tool_rounds > self._config.max_tool_rounds:
                        log.warning(
                            "agent.tool_rounds_exceeded",
                            rounds=tool_rounds,
                            max_rounds=self._config.max_tool_rounds,
                        )
                        full_text = self._config.fallback_message
                        await self._speak([full_text], spoken_tokens)
                        break

                    # Record the assistant's tool call message
                    self._conversation.add_assistant_turn("", tool_calls=tool_calls)

                    # Execute each tool, record the result, apply transitions
                    for tc in tool_calls:
                        result = await self._config.tools.execute(
                            tc.name,
                            tc.arguments,
                            call_context=self._context,
                        )
                        self._conversation.add_tool_result(tc.id, result)
                        self._handle_transition(tc.name)

                    # Rebuild prompt and tools (the node may have changed) and
                    # re-call the LLM.
                    system_prompt = build_system_prompt(
                        self._config,
                        self._context,
                        self._context.current_node,
                    )
                    tools_schemas = self._build_tools_schemas()
                    messages = self._conversation.messages
                    continue

                # Got text, speak it.
                full_text = "".join(text_tokens)
                if full_text.strip():
                    await self._speak(text_tokens, spoken_tokens)

                break  # Done with this turn

        except asyncio.CancelledError:
            log.info("agent.response_cancelled")
        except Exception:
            # The pipeline failed even after retries. Degrade gracefully:
            # tell the caller we could not complete the action instead of
            # going silent or crashing the call.
            log.exception("agent.response_failed")
            full_text = await self._speak_fallback(spoken_tokens)
        finally:
            interrupted = self._barge_in.was_interrupted
            if interrupted:
                # Roll back to what the caller was being told. If nothing was
                # spoken yet, drop the partial turn entirely.
                spoken_text = "".join(spoken_tokens).strip()
                if spoken_text:
                    self._conversation.add_assistant_turn(spoken_text, interrupted=True)
            elif full_text:
                self._conversation.add_assistant_turn(full_text)
            self._barge_in.agent_is_speaking = False
            self._turn_manager.set_listening()

            total_ms = round((time.monotonic() - t_start) * 1000)
            log.info("latency.total", ms=total_ms, interrupted=interrupted)

            # Terminal flow node: hang up now that the goodbye has finished
            # playing (the speak above already awaited speak.ended). Hang up even
            # if the caller barged in on the goodbye, so the call never wedges
            # open with _ending set and all further input discarded.
            if self._ending:
                with contextlib.suppress(Exception):
                    await self._call.hangup()

    async def _greet(self) -> None:
        """Speak the configured greeting when the call connects.

        The greeting is spoken but not added to conversation history, so the
        first LLM message stays a user turn.
        """
        spoken_tokens: list[str] = []
        try:
            await self._speak([self._config.greeting], spoken_tokens)
        except asyncio.CancelledError:
            log.info("agent.greeting_cancelled")
        finally:
            # Consume the barge-in flag so it does not leak into the first turn.
            _ = self._barge_in.was_interrupted
            self._barge_in.agent_is_speaking = False
            self._turn_manager.set_listening()
            # Set only when transcription failed to start: end the call now that
            # the greeting has played, rather than waiting on input that the
            # deaf call will never produce.
            if self._ending:
                with contextlib.suppress(Exception):
                    await self._call.hangup()

    async def _speak(self, text_tokens: list[str], spoken_tokens: list[str]) -> None:
        """Speak text via one Telnyx speak command and await its completion.

        spoken_tokens records what was sent so the barge-in rollback knows what
        the caller was being told. Completion comes from the matching
        call.speak.ended webhook; a timeout is the safety net if it never does.
        """
        self._barge_in.agent_is_speaking = True

        text = "".join(text_tokens)
        spoken_tokens.extend(text_tokens)
        if not text.strip():
            return

        self._speak_gen += 1
        generation = self._speak_gen
        self._current_speak_gen = generation
        self._speak_done = asyncio.Event()

        t_speak = time.monotonic()
        await self._call.speak(
            text, client_state=encode_client_state(f"speak:{generation}")
        )
        log.info("speak.issued", generation=generation, chars=len(text))

        # Wait for playback to finish so agent_is_speaking stays true (barge-in
        # works) and a terminal turn only hangs up after the goodbye plays.
        # The timeout scales with text length so a missing webhook cannot hang
        # the turn forever.
        timeout = max(15.0, len(text) / 10 + 10.0)
        try:
            await asyncio.wait_for(self._speak_done.wait(), timeout)
        except TimeoutError:
            log.warning("speak.timeout", generation=generation, chars=len(text))
        log.info("latency.speak_total", ms=round((time.monotonic() - t_speak) * 1000))

    async def _speak_fallback(self, spoken_tokens: list[str]) -> str:
        """Best-effort spoken apology after a pipeline failure.

        Returns everything said this turn so it can be recorded in history.
        Never raises: if speaking also fails, the failure is logged and the
        call winds down.
        """
        try:
            await self._speak([self._config.fallback_message], spoken_tokens)
        except Exception:
            log.exception("agent.fallback_speech_failed")
        return "".join(spoken_tokens).strip()

    # ── Flow nodes ───────────────────────────────────────────────

    def _get_current_node(self) -> FlowNode | None:
        if (
            self._config.nodes
            and self._context.current_node
            and self._context.current_node in self._config.nodes
        ):
            return self._config.nodes[self._context.current_node]
        return None

    def _handle_transition(self, tool_name: str) -> None:
        """Transition to the next flow node if the tool triggers one."""
        node = self._get_current_node()
        if node and tool_name in node.transitions:
            next_node = node.transitions[tool_name]
            log.info(
                "flow.transition",
                from_node=self._context.current_node,
                to_node=next_node,
                trigger=tool_name,
            )
            self._context.current_node = next_node
            dest = self._config.nodes.get(next_node) if self._config.nodes else None
            if dest is not None and dest.terminal:
                self._ending = True
