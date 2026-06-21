"""FastAPI webhook receiver and call lifecycle dispatcher.

Telnyx delivers every call event (initiated, answered, transcription,
speak.ended, hangup) as a signed JSON webhook. This module verifies the
signature, routes each event to the agent that owns the call, and answers
inbound calls. There is no media WebSocket: all audio happens through Call
Control commands issued by the agent (see DISCOVERY.md, Option A).
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import time
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING, Any, Protocol

import structlog
from fastapi import FastAPI, HTTPException, Request, Response

from offleash import metrics
from offleash.agent import VoiceAgent
from offleash.limits import CallLimiter
from offleash.logging import setup_logging
from offleash.prompts import RESTAURANT_CONFIG
from offleash.settings import get_settings
from offleash.telnyx import TelnyxClient, decode_client_state, verify_webhook

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator

log = structlog.get_logger()

class _BenchHook(Protocol):
    """The barge-in harness, attached to app.state.bench only in bench mode.

    handle_webhook observes every event (recording leg timing) and returns True
    for events on a harness-owned leg so the normal dispatch is skipped for it.
    In production app.state.bench is unset and none of this runs.
    """

    async def handle_webhook(
        self,
        event_type: str,
        payload: dict[str, Any],
        call_control_id: str,
        occurred_at: str,
        t_recv: float,
        w_recv: float,
    ) -> bool: ...


# Telnyx call webhooks are a few KB. Cap the body so an unauthenticated client
# (one who cannot forge a valid Ed25519 signature) cannot exhaust memory on the
# one public endpoint, since the body must be buffered before it is verified.
MAX_WEBHOOK_BYTES = 256 * 1024


def _parse_speak_generation(client_state: str) -> int | None:
    """Recover the speak generation from the echoed client_state.

    Returns None if it cannot be read, which the agent treats as "the current
    speak ended" so a missing or unexpected client_state never hangs a turn.
    """
    decoded = decode_client_state(client_state)
    if decoded.startswith("speak:"):
        with contextlib.suppress(ValueError):
            return int(decoded.split(":", 1)[1])
    return None


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
    settings = get_settings()
    setup_logging(settings)
    app.state.settings = settings
    app.state.telnyx = TelnyxClient(settings)
    app.state.limiter = CallLimiter(settings.max_concurrent_calls)
    # call_control_id -> VoiceAgent. Single event loop, so a plain dict is safe.
    app.state.agents = {}
    log.info("offleash.startup", env=settings.env, port=settings.port)

    # Bench mode: attach the barge-in harness so it owns its outbound leg and
    # records timing. Imported lazily here, never at module load, so the shipping
    # import graph does not depend on bench/.
    bench_task: asyncio.Task[None] | None = None
    if os.environ.get("BENCH_ENABLE") == "1":
        from pathlib import Path

        from bench.controller import BenchConfig, BenchController
        from bench.recorder import Recorder

        cfg = BenchConfig.from_env(dict(os.environ), settings)
        recorder = Recorder(Path(os.environ.get("BENCH_OUT", "bench/data/run.jsonl")))
        controller = BenchController(app.state.telnyx, settings, cfg, recorder)
        app.state.bench = controller
        metrics.set_sink(controller)
        bench_task = asyncio.create_task(controller.run())
        log.info("bench.enabled", out=str(recorder.path), to=cfg.to_number)

    try:
        yield
    finally:
        if bench_task is not None:
            bench_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await bench_task
            metrics.set_sink(None)
        agents: dict[str, VoiceAgent] = app.state.agents
        for agent in list(agents.values()):
            task = agent.run_task
            if task and not task.done():
                # Cancelling runs the agent's finally -> _teardown, which closes
                # its LLM client.
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
            else:
                # Registered but never started (or already finished): close the
                # LLM client directly (aclose is idempotent).
                await agent.aclose()
        agents.clear()
        await app.state.telnyx.aclose()
        log.info("offleash.shutdown")


app = FastAPI(title="telnyx-offleash", lifespan=lifespan)


@app.get("/health")
async def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/webhook")
async def webhook(request: Request) -> Response:
    """Receive, verify, and dispatch a Telnyx call webhook."""
    # Earliest server-side receipt timestamps, captured before any parsing so the
    # bench harness measures from when the event reached us. Both clocks: t_recv
    # (monotonic) for intra-process deltas, w_recv (wall) to align with the agent.
    t_recv = time.monotonic()
    w_recv = time.time()
    settings = request.app.state.settings

    # Bounded read: stop buffering past the cap rather than trusting
    # Content-Length, so a missing or forged length cannot exhaust memory.
    raw_body = b""
    async for chunk in request.stream():
        raw_body += chunk
        if len(raw_body) > MAX_WEBHOOK_BYTES:
            raise HTTPException(status_code=413, detail="Payload too large")

    if not verify_webhook(
        settings.telnyx_public_key,
        request.headers,
        raw_body,
        settings.webhook_tolerance_s,
    ):
        raise HTTPException(status_code=403, detail="Invalid signature")

    try:
        data = json.loads(raw_body)["data"]
    except (json.JSONDecodeError, KeyError, TypeError):
        content_type = request.headers.get("content-type", "")
        if "x-www-form-urlencoded" in content_type:
            # TeXML applications post form-encoded webhooks and expect an XML
            # response. offleash is a Call Control app: it needs JSON event
            # webhooks. This is a Telnyx application-type misconfiguration.
            log.error(
                "webhook.texml_application_detected",
                hint=(
                    "Received a form-encoded (TeXML) webhook. offleash needs a "
                    "Call Control / Voice API application that sends JSON events, "
                    "not a TeXML application."
                ),
            )
        else:
            log.warning("webhook.malformed", content_type=content_type)
        return Response(status_code=200)

    event_type = data.get("event_type", "")
    payload = data.get("payload", {})
    call_control_id = payload.get("call_control_id", "")
    # Telnyx stamps each event with occurred_at; it is the single-clock reference
    # the bench harness uses for onset and stop (both legs carry it).
    occurred_at = data.get("occurred_at", "")

    structlog.contextvars.bind_contextvars(call_sid=call_control_id)
    # Transcription fires many interim events per turn. Log only finals (the
    # turn-shaping signal) to keep the info stream readable while still showing
    # exactly what Telnyx heard and when.
    if event_type == "call.transcription":
        td = payload.get("transcription_data", {})
        if td.get("is_final"):
            log.info("transcription.final", text=td.get("transcript", ""))
    else:
        log.info("webhook.received", event_type=event_type)
    try:
        bench: _BenchHook | None = getattr(request.app.state, "bench", None)
        if bench is not None and await bench.handle_webhook(
            event_type, payload, call_control_id, occurred_at, t_recv, w_recv
        ):
            # Event was on a harness-owned leg; skip the normal agent dispatch.
            return Response(status_code=200)
        await _dispatch(request.app, event_type, payload, call_control_id)
    except Exception:
        # A webhook handler failure must not return 5xx and trigger Telnyx
        # retries that re-run side effects; log and acknowledge.
        log.exception("webhook.dispatch_failed", event_type=event_type)
    finally:
        structlog.contextvars.unbind_contextvars("call_sid")

    return Response(status_code=200)


async def _ensure_registered(
    app: FastAPI, payload: dict[str, Any], call_control_id: str
) -> VoiceAgent | None:
    """Register (and, for inbound, answer) a call if not already tracked.

    Called by both call.initiated and call.answered so whichever arrives first
    wins, which makes the outbound path tolerant of webhook reordering. Returns
    the agent, or None if the call is for another application, is at capacity, or
    its answer command failed.
    """
    settings = app.state.settings
    agents: dict[str, VoiceAgent] = app.state.agents
    limiter: CallLimiter = app.state.limiter
    telnyx: TelnyxClient = app.state.telnyx

    existing = agents.get(call_control_id)
    if existing is not None:
        return existing

    # Ignore calls for other applications when a connection is configured.
    connection_id = payload.get("connection_id", "")
    if (
        settings.telnyx_connection_id
        and connection_id != settings.telnyx_connection_id
    ):
        return None

    direction = payload.get("direction", "")
    incoming = direction == "incoming"

    # At capacity: reject by hanging up rather than answering. No slot is held.
    if not limiter.try_acquire():
        log.warning("call.rejected_at_capacity", direction=direction)
        with contextlib.suppress(Exception):
            await telnyx.call(call_control_id).hangup()
        return None

    agent = VoiceAgent(settings, telnyx.call(call_control_id), RESTAURANT_CONFIG)
    # For inbound, the other party is the caller (from). For outbound, it is the
    # number we dialed (to).
    other = payload.get("from", "") if incoming else payload.get("to", "")
    agent.set_call_info(call_control_id, other)
    agents[call_control_id] = agent
    log.info("call.registered", direction=direction, active=limiter.active)

    if incoming:
        try:
            await telnyx.call(call_control_id).answer()
        except Exception:
            # Roll back the slot, registry entry, and LLM client so a failed
            # inbound answer cannot leak capacity, then best-effort hang up.
            log.exception("call.answer_failed")
            await agent.aclose()
            _finish_call(app, call_control_id)
            with contextlib.suppress(Exception):
                await telnyx.call(call_control_id).hangup()
            return None

    return agent


async def _dispatch(
    app: FastAPI, event_type: str, payload: dict[str, Any], call_control_id: str
) -> None:
    agents: dict[str, VoiceAgent] = app.state.agents

    if event_type == "call.initiated":
        await _ensure_registered(app, payload, call_control_id)

    elif event_type == "call.answered":
        agent = await _ensure_registered(app, payload, call_control_id)
        if agent is None or agent.run_task is not None:
            return
        log.info("call.answered")
        task = agent.start()

        def _on_done(_task: asyncio.Task[None]) -> None:
            _finish_call(app, call_control_id)

        task.add_done_callback(_on_done)

    elif event_type == "call.transcription":
        agent = agents.get(call_control_id)
        if agent is not None:
            td = payload.get("transcription_data", {})
            transcript = td.get("transcript", "")
            if transcript:
                agent.submit_transcript(transcript, bool(td.get("is_final")))

    elif event_type == "call.speak.ended":
        agent = agents.get(call_control_id)
        if agent is not None:
            generation = _parse_speak_generation(payload.get("client_state", "") or "")
            agent.submit_speak_ended(generation)

    elif event_type == "call.hangup":
        log.info(
            "call.hangup",
            cause=payload.get("hangup_cause"),
            source=payload.get("hangup_source"),
        )
        agent = agents.get(call_control_id)
        if agent is not None:
            if agent.run_task is not None:
                # The run loop will exit and its done callback frees the slot.
                agent.submit_hangup()
            else:
                # Hung up before it was answered: no run task ran, so its
                # teardown never closed the LLM client. Close it and clean up.
                await agent.aclose()
                _finish_call(app, call_control_id)

    else:
        log.debug("webhook.ignored", event_type=event_type)


def _finish_call(app: FastAPI, call_control_id: str) -> None:
    """Release the call slot and registry entry once the agent run task ends."""
    agents: dict[str, VoiceAgent] = app.state.agents
    if agents.pop(call_control_id, None) is not None:
        app.state.limiter.release()
        log.info("call.finished", active=app.state.limiter.active)
