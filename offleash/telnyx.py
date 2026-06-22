"""Thin typed wrappers over the Telnyx primitives this agent uses.

No abstraction layer: just the Call Control commands the agent issues, the
OpenAI-compatible LLM client pointed at Telnyx inference, and Ed25519 webhook
verification. Call Control actions go out as plain JSON over httpx; transcripts
and playback completion come back as webhooks handled in server.py.
"""

from __future__ import annotations

import base64
import time
from typing import TYPE_CHECKING, Any

import httpx
import structlog
from nacl.encoding import Base64Encoder
from nacl.signing import VerifyKey
from openai import AsyncOpenAI

from offleash.settings import stt_emits_interim
from offleash.types import LLMEvent, LLMEventType, ToolCallRequest

if TYPE_CHECKING:
    from collections.abc import AsyncIterator, Mapping

    from offleash.settings import Settings
    from offleash.types import LLMMessage

log = structlog.get_logger()

# Telnyx rejects speak payloads beyond a few thousand characters. Cap below that
# so a verbose model reply (max_tokens is unset on tool turns) degrades to a
# truncated answer instead of a 422 that drops the whole turn to the fallback.
MAX_SPEAK_CHARS = 3000

# Telnyx error code for a command issued to a call that has already ended. This
# is a benign teardown race (the caller hung up while we were cleaning up), so
# it is logged at info rather than error.
CALL_ENDED_ERROR_CODE = "90018"


def _is_call_ended_error(resp: httpx.Response) -> bool:
    """True if Telnyx rejected the action because the call already ended."""
    if resp.status_code != 422:
        return False
    try:
        errors = resp.json().get("errors", [])
    except Exception:
        return False
    return any(str(e.get("code")) == CALL_ENDED_ERROR_CODE for e in errors)


def _cap_speak_text(text: str) -> str:
    """Truncate over-long speak text, preferring a sentence boundary."""
    if len(text) <= MAX_SPEAK_CHARS:
        return text
    head = text[:MAX_SPEAK_CHARS]
    for sep in (". ", "! ", "? "):
        idx = head.rfind(sep)
        if idx > MAX_SPEAK_CHARS // 2:
            return head[: idx + 1]
    return head


# ── Webhook verification ─────────────────────────────────────────


def verify_webhook(
    public_key: str,
    headers: Mapping[str, str],
    raw_body: bytes,
    tolerance_s: int = 300,
) -> bool:
    """Verify a Telnyx webhook's Ed25519 signature.

    Telnyx signs every webhook with Ed25519 over "{timestamp}|{body}", carried
    in the telnyx-signature-ed25519 and telnyx-timestamp headers. Any failure
    (missing headers, stale timestamp, bad signature) means the request is not
    trusted.
    """
    signature = headers.get("telnyx-signature-ed25519")
    timestamp = headers.get("telnyx-timestamp")
    if not signature or not timestamp:
        log.warning("telnyx.webhook_missing_headers")
        return False

    try:
        ts = int(timestamp)
    except ValueError:
        log.warning("telnyx.webhook_bad_timestamp")
        return False

    if abs(time.time() - ts) > tolerance_s:
        log.warning("telnyx.webhook_stale", age_s=round(abs(time.time() - ts)))
        return False

    signed = f"{timestamp}|".encode() + raw_body
    try:
        verify_key = VerifyKey(public_key.encode(), encoder=Base64Encoder)
        verify_key.verify(signed, base64.b64decode(signature))
    except Exception as e:
        log.warning("telnyx.signature_rejected", error=str(e))
        return False
    return True


def encode_client_state(value: str) -> str:
    """Encode a correlation string as the base64 Telnyx client_state expects."""
    return base64.b64encode(value.encode()).decode()


def decode_client_state(value: str) -> str:
    """Decode a base64 client_state echoed back on a webhook."""
    try:
        return base64.b64decode(value).decode()
    except Exception:
        return ""


# ── LLM (OpenAI-compatible Telnyx inference) ─────────────────────


class TelnyxLLM:
    """Streaming chat completions against Telnyx inference.

    The streaming and tool-call accumulation logic is lifted verbatim from the
    voice-agent-lite OpenAI-compatible provider: the only single-vendor changes
    are the base URL, the pinned model, and enable_thinking disabled for
    real-time voice latency.
    """

    def __init__(self, settings: Settings) -> None:
        self.model = settings.llm_model
        self.temperature = settings.llm_temperature
        self.max_tokens = settings.llm_max_tokens
        self._client = AsyncOpenAI(
            api_key=settings.telnyx_api_key,
            base_url=settings.llm_base_url,
        )

    async def stream_response(
        self,
        messages: list[LLMMessage],
        *,
        system: str = "",
        tools: list[dict[str, Any]] | None = None,
    ) -> AsyncIterator[LLMEvent]:
        """Stream a chat completion, yielding text deltas and tool calls."""
        api_messages: list[dict[str, Any]] = []

        if system:
            api_messages.append({"role": "system", "content": system})

        for m in messages:
            msg: dict[str, Any] = {"role": m.role, "content": m.content}
            if m.tool_calls:
                msg["tool_calls"] = [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {"name": tc.name, "arguments": tc.arguments},
                    }
                    for tc in m.tool_calls
                ]
            if m.tool_call_id:
                msg["tool_call_id"] = m.tool_call_id
            api_messages.append(msg)

        kwargs: dict[str, Any] = {
            "model": self.model,
            "messages": api_messages,
            "temperature": self.temperature,
            "stream": True,
            # Telnyx extension: skip the model's internal reasoning step so the
            # first token arrives fast enough for a live phone call.
            "extra_body": {"enable_thinking": False},
        }
        if tools:
            kwargs["tools"] = tools
        else:
            # Telnyx rejects max_tokens alongside function tools (error 10015),
            # so the per-response length cap is only applied on tool-free turns.
            kwargs["max_tokens"] = self.max_tokens

        stream = await self._client.chat.completions.create(**kwargs)

        # Accumulate tool calls across chunks (they arrive in fragments)
        tool_call_accumulators: dict[int, dict[str, str]] = {}

        async for chunk in stream:
            choice = chunk.choices[0] if chunk.choices else None
            if not choice:
                continue

            delta = choice.delta

            if delta and delta.content:
                yield LLMEvent(type=LLMEventType.TEXT_DELTA, text=delta.content)

            if delta and delta.tool_calls:
                for tc_delta in delta.tool_calls:
                    idx = tc_delta.index
                    if idx not in tool_call_accumulators:
                        tool_call_accumulators[idx] = {
                            "id": "",
                            "name": "",
                            "arguments": "",
                        }
                    acc = tool_call_accumulators[idx]
                    if tc_delta.id:
                        acc["id"] = tc_delta.id
                    if tc_delta.function:
                        if tc_delta.function.name:
                            acc["name"] = tc_delta.function.name
                        if tc_delta.function.arguments:
                            acc["arguments"] += tc_delta.function.arguments

        # Emit accumulated tool calls once the stream ends. Flushing here, rather
        # than only on finish_reason == "tool_calls", tolerates backends that end
        # a tool turn with finish_reason "stop" or simply close the stream (a
        # documented cross-model inconsistency, relevant to the K2.5 to K2.6 move).
        for idx in sorted(tool_call_accumulators):
            acc = tool_call_accumulators[idx]
            yield LLMEvent(
                type=LLMEventType.TOOL_CALL,
                tool_call=ToolCallRequest(
                    id=acc["id"],
                    name=acc["name"],
                    arguments=acc["arguments"],
                ),
            )

    async def aclose(self) -> None:
        await self._client.close()


# ── Call Control ─────────────────────────────────────────────────


class Call:
    """Call Control commands scoped to one call_control_id.

    Every command is a POST to /v2/calls/{id}/actions/{action}. Failures raise
    httpx.HTTPStatusError; callers decide whether to degrade or tear down.
    """

    def __init__(self, client: TelnyxClient, call_control_id: str) -> None:
        self._client = client
        self.id = call_control_id

    async def answer(self) -> None:
        await self._client.action(self.id, "answer")

    async def start_transcription(self) -> None:
        s = self._client.settings
        # The transcription language enum is stricter than the speak one: it
        # wants a short code like "en" and rejects "en-US" (which speak
        # accepts), hence the separate stt_language setting. "inbound"
        # transcribes the caller's audio on this leg.
        payload: dict[str, Any] = {
            "transcription_engine": s.stt_engine,
            "language": s.stt_language,
            "transcription_tracks": "inbound",
        }
        # Pick a specific model within engines that host several (Deepgram
        # nova-3 / flux, etc.); blank means the engine's default.
        if s.stt_model:
            payload["transcription_model"] = s.stt_model
        # Deepgram Flux turn-detection tuning. These are Flux-only params, sent
        # top-level alongside the model (matching how transcription_model is
        # passed). A higher eot_threshold makes Flux wait for a clearer
        # end-of-turn, so one utterance is less likely to split into finals.
        if s.stt_engine == "Deepgram" and s.stt_model == "flux":
            if s.stt_eot_threshold is not None:
                payload["eot_threshold"] = s.stt_eot_threshold
            if s.stt_eot_timeout_ms is not None:
                payload["eot_timeout_ms"] = s.stt_eot_timeout_ms
        # Barge-in fires on interim transcripts, so request them only from
        # engines that emit them. Asking an engine that cannot risks a 422 that
        # would leave the call with no transcription at all; for those engines
        # barge-in degrades to final-transcript-only.
        if stt_emits_interim(s.stt_engine):
            payload["interim_results"] = True
        else:
            log.warning(
                "stt.no_interim",
                engine=s.stt_engine,
                detail="barge-in will use final transcripts only",
            )
        # Log exactly what we send so the engine/model/tuning actually in effect
        # is visible (and a rejected Flux param shows up next to action_failed).
        log.info(
            "transcription.config",
            engine=s.stt_engine,
            model=s.stt_model or None,
            interim=payload.get("interim_results", False),
            eot_threshold=payload.get("eot_threshold"),
            eot_timeout_ms=payload.get("eot_timeout_ms"),
        )
        await self._client.action(self.id, "transcription_start", payload)

    async def stop_transcription(self) -> None:
        await self._client.action(self.id, "transcription_stop")

    async def speak(self, text: str, *, client_state: str) -> None:
        s = self._client.settings
        payload = _cap_speak_text(text)
        if len(payload) < len(text):
            log.warning("speak.truncated", original=len(text), sent=len(payload))
        await self._client.action(
            self.id,
            "speak",
            {
                "payload": payload,
                "payload_type": "text",
                "voice": s.tts_voice,
                "language": s.tts_language,
                "client_state": client_state,
            },
        )

    async def stop_playback(self) -> None:
        # stop="current" halts the in-flight speak without clearing a queue we
        # never build (one speak per turn).
        await self._client.action(self.id, "playback_stop", {"stop": "current"})

    async def hangup(self) -> None:
        await self._client.action(self.id, "hangup")


class TelnyxClient:
    """Process-wide Telnyx Call Control client.

    Holds one authenticated httpx session. Issues outbound dials and mints a
    Call handle per call_control_id.
    """

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self._http = httpx.AsyncClient(
            base_url=settings.telnyx_api_base,
            headers={"Authorization": f"Bearer {settings.telnyx_api_key}"},
            timeout=httpx.Timeout(10.0),
        )

    def call(self, call_control_id: str) -> Call:
        return Call(self, call_control_id)

    async def action(
        self, call_control_id: str, action: str, payload: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        resp = await self._http.post(
            f"/calls/{call_control_id}/actions/{action}", json=payload or {}
        )
        if resp.is_error:
            # A command issued to a call that already ended (e.g. transcription_stop
            # during teardown, after the caller hung up) is a benign race, not a
            # failure: log it at info and still raise so callers tear down as before.
            if _is_call_ended_error(resp):
                log.info("telnyx.action_skipped_call_ended", action=action)
            else:
                # Surface Telnyx's explanation (a 422 names the offending field)
                # before raising, so failures are diagnosable from the logs.
                log.error(
                    "telnyx.action_failed",
                    action=action,
                    status=resp.status_code,
                    body=resp.text[:1000],
                )
            resp.raise_for_status()
        data: dict[str, Any] = resp.json()
        return data

    async def dial(self, to: str, *, connection_id: str = "", from_: str = "") -> str:
        """Place an outbound call and return its call_control_id."""
        body = {
            "connection_id": connection_id or self.settings.telnyx_connection_id,
            "to": to,
            "from": from_ or self.settings.telnyx_phone_number,
        }
        resp = await self._http.post("/calls", json=body)
        resp.raise_for_status()
        ccid: str = resp.json()["data"]["call_control_id"]
        log.info("call.dialed", to=to, call_control_id=ccid)
        return ccid

    async def aclose(self) -> None:
        await self._http.aclose()
