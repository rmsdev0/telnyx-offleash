# DISCOVERY

Discovery findings for `telnyx-offleash`: a complete, single-vendor, real-time voice agent on Telnyx primitives (transport, STT, TTS, LLM), lifting the proven orchestration core from `voice-agent-lite` and collapsing its provider fan-out.

This document is the review gate. No build code has been written. The one decision that needs sign-off before building is the architecture path in Section 1.

## 0. Headline finding (read this first)

The brief frames the architecture as a binary: Path A (call-command orchestration) versus Path B (Media Streaming orchestration, drive STT and feed TTS in our own frame loop). The brief's rule is: recommend Path B only if Media Streaming cleanly supports raw frames in both directions AND Telnyx STT/TTS can be driven against that stream.

Research result:

1. Telnyx Media Streaming DOES provide raw bidirectional audio frames (base64 RTP payloads over a WebSocket, PCMU 8k, both directions). This half is true.
2. Telnyx STT (`transcription_start`) and Telnyx TTS (`speak`) are leg-bound Call Control commands. STT results arrive only as `call.transcription` webhooks. TTS plays directly onto the call leg. Neither can be attached to or driven against the Media Streaming socket.

So the clean Path B the brief describes (Telnyx STT consuming our stream, Telnyx TTS fed by our loop) is not possible with single-vendor primitives. Per the brief's own gating rule, that rules out clean Path B and points to Path A as the honest minimal build.

Important nuance about the reference repo: `voice-agent-lite` is itself a Path B system, but it drives the stream with EXTERNAL providers (Deepgram STT, ElevenLabs TTS). Its Telnyx transport (`transport/telnyx.py`) was built to carry those external providers' audio over Telnyx Media Streaming. It never used Telnyx's own STT/TTS. The moment we go single-vendor and drop the external providers, the thing the media frame loop existed to feed (an external STT) and the thing it existed to emit (external TTS frames) both disappear, because Telnyx STT/TTS live on the leg, not the stream. This is why the single-vendor collapse shifts the architecture toward Path A rather than a like-for-like lift of the media transport.

The full evidence and the recommendation are in Section 1.

---

## 1. Architecture decision (task zero)

### 1.1 What Telnyx Media Streaming can do (evidence)

Confirmed against Telnyx docs and the working reference code in `voice-agent-lite/src/voice_agent/transport/telnyx.py`:

- Bidirectional ("two-way") audio is officially supported. Start it with the `streaming_start` Call Control command, or inline on Dial / Answer, or via the TeXML `<Connect><Stream>` verb (what the reference uses).
- `streaming_start` parameters: `stream_url` (wss), `stream_track` (`inbound_track` | `outbound_track` | `both_tracks`), `stream_codec` (PCMU, PCMA, G722, OPUS, AMR-WB, L16), `stream_bidirectional_mode` (`mp3` | `rtp`), `stream_bidirectional_codec` (PCMU default), `stream_bidirectional_sampling_rate` (8000 default), `stream_bidirectional_target_legs` (`both` | `self` | `opposite`, default `opposite`).
- Inbound WebSocket messages (server to us): `connected`, `start` (carries top-level `stream_id` and `start.{call_control_id, call_session_id, from, to, media_format, custom_parameters}`), `media` (base64 RTP payload, no RTP headers, PCMU 8k mono), `mark` (echoed), `stop`, `dtmf`, `error`.
- Outbound WebSocket messages (us to server): `media` (`{"event":"media","media":{"payload": base64}}`, no stream id needed, one stream per socket), `clear` (`{"event":"clear"}`, immediately stops playback and flushes the media queue: the frame-level barge-in primitive), `mark` (`{"event":"mark","mark":{"name":...}}`).
- Constraints to design around: one bidirectional RTP stream per call, only one streaming/fork operation per call, and outbound MP3 file payloads are rate-limited to once per second (most relevant to mp3 mode, not rtp-paced frames). Streaming `both_tracks` while injecting TTS to the same leg causes self-transcription echo, which is why the reference uses `inbound_track` only.

Sources: `developers.telnyx.com/docs/voice/programmable-voice/media-streaming`, `developers.telnyx.com/api-reference/call-commands/streaming-start`, `telnyx.com/release-notes/bi-directional-streaming-support`.

### 1.2 What Telnyx STT and TTS can and cannot do (evidence)

STT (`transcription_start`):

- Leg-bound Call Control command against a `call_control_id`. Engines: Google (default, Engine A, supports interim results), Telnyx (Engine B, more accurate, lower cost and latency), plus others. For a Telnyx-only build the natural picks are Engine A (Google) for interims or Engine B (Telnyx) for accuracy and cost.
- Results delivered ONLY via `call.transcription` webhooks. Payload carries `transcription_data.{transcript, is_final, confidence}`. Interim versus final is the `is_final` flag.
- There is no streaming-WebSocket STT in Call Control. Delivery is webhook-based. STT cannot be pointed at a Media Streaming socket.

TTS (`speak`):

- Leg-bound Call Control command. Parameters: `payload` (text or SSML, 3500 char limit), `payload_type` (`text` | `ssml`), `voice`, `language`, `service_level`, `stop`, `command_id`, `client_state`.
- Voice identifier format `Provider.Model.Voice`, for example `Telnyx.Natural.abbie`, `Telnyx.NaturalHD.astra`, `Telnyx.KokoroTTS.af`, `Telnyx.Ultra.<uuid>`.
- Completion signalled by `call.speak.started` and `call.speak.ended` webhooks.
- Interrupt via the `playback_stop` command (`stop: "current"` halts the current item, `stop: "all"` clears the queue too). This is the leg-level barge-in primitive for the call-command path.
- A standalone TTS REST endpoint exists, `POST /v2/text-to-speech/speech`, returning synthesized audio bytes (audio/mpeg). This is a buffered file, not a low-latency token-streamed frame source. It could be decoded and injected over the media socket, but it adds first-audio latency and an MP3 decode dependency.

Sources: `developers.telnyx.com/docs/voice/programmable-voice/speech-to-text`, `.../api-reference/call-commands/transcription-start`, `.../docs/voice/programmable-voice/tts`, `.../api-reference/call-commands/stop-audio-playback`, `.../api/call-control/generate-text-to-speech`.

### 1.3 The three real options

The binary in the brief becomes three concrete builds once the leg-bound reality is accounted for:

Option A: Pure call-command orchestration. No media socket.
- STT: `transcription_start`, transcripts via `call.transcription` webhooks.
- TTS: `speak` on the leg.
- Barge-in: while speaking, a `call.transcription` event (interim or final) triggers `playback_stop`.
- Reuse: conversation/history, full LLM tool loop, TurnManager (event source adapted), prompts, tools, limits, retry, logging, type contracts. Drops the media frame loop, the mulaw codec for I/O, and VAD/turn detection.
- Pros: smallest, fewest moving parts, every voice primitive used exactly as Telnyx natively supports it, lowest first-audio latency (native `speak`). Most bulletproof.
- Cons: barge-in latency is gated on how fast transcription emits an interim plus the `playback_stop` round trip (roughly several hundred ms, not frame-level). No frame-level interrupt.

Option B-hybrid: Media Streaming for audio I/O plus leg-bound STT.
- Audio: bidirectional RTP media socket (reuse the reference frame loop).
- STT: `transcription_start` on the leg in parallel, transcripts via webhook.
- Barge-in: VAD on inbound frames detects acoustic onset (about 120 ms), then `{"event":"clear"}` flushes injected TTS instantly (frame-level).
- TTS: synthesize via the standalone `/v2/text-to-speech/speech` endpoint, decode, inject frames. Frame-level barge-in via `clear`.
- Pros: maximal reuse of the reference (frame loop, VAD, injection, clear-based barge-in), fastest interrupts.
- Cons: most moving parts (media socket AND leg transcription AND buffered TTS running together), buffered standalone TTS hurts first-audio latency, MP3 decode dependency, echo management. Least bulletproof per "every line earns its place."

Option B-lite: Media Streaming inbound only, for fast barge-in detection, with native TTS.
- Audio in: `inbound_track` media socket feeds VAD for acoustic-onset barge-in detection.
- STT: `transcription_start` webhooks.
- TTS: `speak` on the leg (native, low first-audio latency, no injection, no MP3 decode).
- Barge-in: VAD onset on inbound frames triggers `playback_stop` (fast detection, leg-level stop).
- Pros: keeps the media inbound frame loop and VAD reuse, snappier barge-in detection than Option A, native low-latency TTS, no buffered-TTS penalty.
- Cons: more moving parts than A (media socket plus leg transcription plus leg speak), barge-in stop is still a `playback_stop` round trip rather than a frame clear.

### 1.4 Recommendation

Recommend Option A (pure call-command orchestration) for v0, with Option B-lite documented as the first enhancement if barge-in snappiness proves insufficient in testing.

Reasoning:

1. The brief's gating rule rules out clean Path B: Telnyx STT and TTS are leg-bound and cannot be driven against the stream. Per the brief, that makes Path A the honest minimal build.
2. Option A uses every voice primitive exactly as Telnyx natively supports it. It is the most honest single-vendor demonstration: no external decoders, no buffered-TTS workaround, no echo management.
3. It is the smallest and most bulletproof. Fewest concurrency primitives, fewest failure modes, cleanest teardown. This matches the brief's "small, lightweight, bulletproof, every line earns its place."
4. It still reuses the majority of the reference orchestration logic: the conversation/history with interrupt rollback, the entire LLM tool loop with flow nodes (which maps one-to-one onto Telnyx's OpenAI-compatible inference), TurnManager, prompts, tools, limits, retry, logging, and the type contracts. The only pieces dropped are the media frame plumbing and VAD, and those exist in the reference to feed an external STT that no longer exists in a single-vendor build.
5. Per the brief, when clean Path B is unavailable the control pitch leans on real tool calling, model and prompt tuning, and custom loop behavior. All of those are fully preserved in Option A. Frame-level barge-in is the one thing we trade away, and the brief explicitly anticipates that trade.

What changes versus a naive "lift the Telnyx transport" plan: Option A does not use `transport/telnyx.py`'s media frame handling at all. The transport-layer reuse shifts from "media WebSocket frame loop" to "Call Control HTTP command wrappers plus a webhook receiver." The reference still pays off heavily, just in the orchestration logic and the LLM/tool surface rather than in the media transport.

Open decision for review: accept Option A for v0, or require frame-level barge-in for v0 and accept the extra complexity of Option B-hybrid. If snappier-but-not-frame-level barge-in is the sweet spot, Option B-lite is the middle path. The rest of this document assumes Option A and notes where Option B-lite would add code.

---

## 2. voice-agent-lite survey (KEEP / DELETE / TOUCHPOINTS)

Repo surveyed: `/Users/rschuetz/Code/voice-agent-lite` (read-only). The orchestration core is cleanly layered: carrier specifics sit behind a `Transport` protocol and a per-call `OutboundChannel`, provider specifics behind STT/LLM/TTS protocols and factories. The line counts below are from the surveyed files.

### 2.1 KEEP (lift largely intact)

These are orchestration logic with little or no carrier/provider coupling. Under Option A they are kept; the ones marked "(adapt event source)" need their input rewired from a streaming provider to webhook-fed events, but their internal logic stays.

- `src/voice_agent/conversation.py` (98 lines). `CallContext` (per-call mutable state, slots) and `Conversation` (history, interrupted-turn truncation with `[interrupted]` marker, trim to last N turns). No coupling. Keep verbatim.
- `src/voice_agent/turn_manager.py` (105 lines). `TurnManager` accumulates STT finals and emits a complete user turn on `UTTERANCE_END`. State enum LISTENING/PROCESSING/SPEAKING. No coupling. Keep, adapt event source (under Option A the events come from `call.transcription` webhooks rather than a streaming STT provider).
- `src/voice_agent/barge_in.py` (44 lines). `BargeInHandler` interrupt state machine. Keep the concept verbatim; the single carrier call `out.send_clear()` is repointed to `playback_stop` under Option A. See Section 2.3.
- `src/voice_agent/limits.py` (84 lines). `CallLimiter` (concurrency cap) and `TokenBudget` (char-based estimate). No coupling. Keep verbatim.
- `src/voice_agent/utils/retry.py` (57 lines). `retry_stream` helper, retries a stream factory only while it has yielded nothing, never swallows CancelledError. Used for the LLM stream. Keep verbatim.
- `src/voice_agent/utils/logging.py` (41 lines). structlog config and the event-name-first conventions (`barge_in.triggered`, `latency.llm_ttft`, etc.) used throughout. Keep the conventions.
- `src/voice_agent/prompts.py`. `build_system_prompt(config, context, current_node)` composes the system prompt fresh per LLM call (identity, voice-call framing, response style, current flow-node task, dynamic context, rules). The restaurant demo depends on this exactly. Keep, see Section 3.
- `src/voice_agent/tools.py`. `ToolRegistry` with `@tool` decorator, auto-generated JSON schema from type hints, `get_schemas`/`subset_schemas`, and `execute` (JSON-parse args, inject `call_context`, run, stringify, errors returned as strings). The tool-calling surface. Keep verbatim, see Section 3.
- `src/voice_agent/providers/base.py` (types only). `LLMMessage`, `ToolCallRequest`, `LLMEvent`, `LLMEventType`, `STTEvent`, `STTEventType`, `AudioChunk` are the contracts the core is written against. Keep the dataclasses and enums; drop the provider Protocol definitions that are no longer implemented. This is a touchpoint, see Section 2.3.
- `src/voice_agent/agent.py` (623 lines). The orchestration heart: `VoiceAgent.run()` main loop, `_merged_events()` fan-in, the cancellable `_response_task`, `_generate_response()` (the LLM tool loop, token budget, retry-wrapped streaming, flow-node transitions, interrupted-turn rollback in its finally block), `_speak()`, `_send_mark()`. The LLM tool loop is the bulk of this file and maps directly onto Telnyx inference. KEEP the tool loop and lifecycle; the I/O seams (audio in, audio out, barge-in stop) are the touchpoints rewired for Option A. See Section 2.3.

KEEP only under Option B (media socket reused), not needed under Option A:

- `src/voice_agent/utils/audio.py` (113 lines). Hand-built G.711 mulaw codec (stdlib audioop was removed in 3.13). Needed only if we read/inject raw frames. Option A does not. Option B-lite needs the decode path for VAD; Option B-hybrid needs both directions.
- `src/voice_agent/transport/telnyx.py` (191 lines). The Telnyx Media Streaming transport: TeXML answer markup, Ed25519 webhook verification, `decode`/`encode_audio`/`encode_clear`/`encode_mark`, and Call Control `hangup`. Under Option A we reuse only the Ed25519 verification and the Call Control `hangup`, not the media frame encode/decode. Under Option B we reuse the frame handling too.
- `src/voice_agent/transport/base.py` (199 lines). Normalized transport events and `OutboundChannel`. Relevant only if the media socket is used.
- `src/voice_agent/turn_detection/*` (base, factory, silero engine, webrtc engine, silero ONNX asset). VAD/turn detection. Option A does not need it (turn boundaries come from transcription finals). Option B-lite would inline the WebRTC engine (20 ms frames align 1:1 with 8k mulaw, stateless, no ONNX asset or onnxruntime dependency) for acoustic-onset barge-in.
- `src/voice_agent/main.py` (260 lines). FastAPI app: the `/voice` answer-markup route, the `/ws/media` frame loop, status routes. Under Option A the `/ws/media` loop is replaced by a webhook receiver that drives the loop on call events. Under Option B the frame loop is reused.

### 2.2 DELETE (pure fan-out, not in the live single-vendor path)

- `src/voice_agent/providers/llm/{openai,xai,groq,anthropic}.py` and `providers/llm/__init__.py`. Non-Telnyx LLM implementations. Note: `providers/llm/openai_compatible.py` is NOT deleted as code to keep, but it IS the parity reference for the Telnyx LLM call (see Section 2.4 and 5). The new build points an OpenAI client at the Telnyx base URL, which reuses its streaming and tool-call accumulation logic almost verbatim.
- `src/voice_agent/providers/stt/*` (deepgram, assemblyai, openai, xai, base) and `providers/stt/__init__.py`. Every external STT. Replaced by `transcription_start` webhooks.
- `src/voice_agent/providers/tts/*` (elevenlabs, deepgram) and `providers/tts/__init__.py`. Every external TTS. Replaced by `speak`.
- `src/voice_agent/transport/twilio.py` and `twilio_utils.py`. Non-Telnyx transport.
- `src/voice_agent/campaign.py`, `simulation.py`, `samples/*`. Campaign/simulation/sample-generation tooling. Imported only by `cli.py`, not in the live call path. Clean delete for v0. (Note: `simulation.py` is the text harness that exercises the restaurant demo via personas; if parity testing is wanted later it would be re-created, but it is out of v0 scope.)
- `src/voice_agent/cli.py` (717 lines). Operator tooling (samples/campaign/simulate/chat). Not part of the live call path. Replace with a tiny entrypoint that runs the server and, optionally, places an outbound call.
- The provider registries and factories in `providers/__init__.py`. Pure fan-out. The `create_llm_provider`/`create_stt_provider`/`create_tts_provider` selection logic goes away in a single-vendor build (one of each, constructed directly).

### 2.3 TOUCHPOINTS (the boundary call sites to rewire)

These are the exact seams where core logic reaches through a provider/transport interface and must be repointed at Telnyx directly. The barge-in/playback-stop path was the one to find first.

1. Barge-in / playback-stop (the number one touchpoint).
   - Detection lives in `agent.py` `run()` (around lines 217 to 229): a barge-in fires when a final transcript arrives (or, in VAD mode, on acoustic SPEECH_STARTED) while `agent_is_speaking`. On fire it calls `self._barge_in.handle_barge_in(self._out)`, cancels `_response_task`, and sets the turn manager to listening.
   - `barge_in.py` `handle_barge_in` (lines 34 to 43) calls `await out.send_clear()`.
   - `OutboundChannel.send_clear` (`transport/base.py` lines 192 to 194) delegates to `TelnyxTransport.encode_clear` (`transport/telnyx.py` lines 165 to 166), which emits `{"event":"clear"}` over the media socket.
   - Rewire for Option A: `send_clear()` becomes a `playback_stop` Call Control POST (`/v2/calls/{call_control_id}/actions/playback_stop`, `stop: "current"`). The detection trigger becomes a `call.transcription` interim/final webhook arriving while speaking. The local `_response_task` cancellation and the conversation rollback in `_generate_response`'s finally block stay exactly as they are.
   - Rewire for Option B: keep `{"event":"clear"}` over the socket; the trigger becomes VAD onset on inbound frames.

2. Audio out (TTS). `agent.py` `_speak()` (around line 572) streams TTS chunks to `_out.send_audio` to `TelnyxTransport.encode_audio`.
   - Rewire for Option A: `_speak()` becomes a `speak` Call Control POST with the assembled response text and the configured voice. Playback completion is tracked via the `call.speak.ended` webhook (replacing the mark echo). The streaming-token-to-TTS path collapses to "assemble the response text, then issue one speak."
   - Rewire for Option B: keep the frame injection, source frames from the standalone TTS endpoint.

3. Mark / playback completion. `agent.py` `_send_mark()` and the hangup-on-mark path in `main.py`.
   - Rewire for Option A: completion comes from `call.speak.ended`; end-of-call hangup is a `hangup` Call Control POST after the terminal node's speak ends.

4. Audio in (STT). `agent.py` `handle_audio_bytes` forwards inbound frames to the STT provider and turn detector; `_merged_events()` merges STT and detector events.
   - Rewire for Option A: there are no inbound frames. A webhook handler converts `call.transcription` payloads into `STTEvent`s (interim to TRANSCRIPT_INTERIM, final to TRANSCRIPT_FINAL, and a synthesized UTTERANCE_END on final) and pushes them onto the queue the run loop consumes. `_merged_events` simplifies to a single source.
   - Rewire for Option B-lite: keep `handle_audio_bytes` feeding VAD for barge-in, but transcripts still come from the webhook.

5. LLM provider construction. `agent.py` `__init__` (lines 142 to 156) calls `create_llm_provider(...)` and friends.
   - Rewire: construct one LLM client directly (an OpenAI client with `base_url=https://api.telnyx.com/v2/ai`, model `moonshotai/Kimi-K2.5`). The streaming and tool-call accumulation logic from `openai_compatible.py` is reused as-is. See Section 5.

6. Default channel construction. `agent.py` `__init__` (lines 114 to 118) lazily defaults to `TwilioTransport` when no channel is injected. Remove the Twilio default. Under Option A there is no media OutboundChannel at all; the agent holds a Telnyx Call Control client instead.

7. Config. `config.py` is a pydantic-settings knob board with multi-provider selectors and dual-transport validators. Slim to a flat env-driven settings object: Telnyx API key, Telnyx public key (webhook verification), phone number, connection id (Voice API Application id), public webhook base URL, LLM base URL and model, default voice, STT engine, token and concurrency limits.

8. `report.py`. `CallReport`/`CallReportStore` is imported by the live core (`agent.py` and a `main.py` route). It is a touchpoint to retain or to trim, not a clean delete. For v0 it can be kept minimal or dropped if the report route is dropped.

### 2.4 The LLM contract the Telnyx call must satisfy (from openai_compatible.py)

The core's tool loop in `agent.py` consumes an `AsyncIterator[LLMEvent]` with this contract, which the Telnyx call site must reproduce (and which the OpenAI SDK pointed at Telnyx already produces):

- Request body built from `messages` (roles system/user/assistant/tool; assistant tool-call turns carry `tool_calls: [{id, type:"function", function:{name, arguments(JSON string)}}]`; tool-result turns are `{role:"tool", content, tool_call_id}`), plus `tools` (OpenAI function schema, each with `strict: true`), `model`, `temperature`, `max_tokens`, `stream: true`.
- Response handling: yield one `LLMEvent(TEXT_DELTA, text)` per content delta; accumulate streaming tool-call fragments keyed by `index` (concatenate `function.arguments`); when `finish_reason == "tool_calls"`, emit one `LLMEvent(TOOL_CALL, ToolCallRequest(id, name, arguments))` per index in index order.
- Downstream: TEXT_DELTA appends to the response and records LLM time-to-first-token; TOOL_CALL appends to the tool list; after the stream, tool calls are executed via `tools.execute(name, arguments, call_context=...)`, results added as tool-result turns, flow-node transitions applied, prompt and tool schemas rebuilt, and the LLM re-called. Capped by `max_tool_rounds` (default 5).

This is the cleanest collapse in the whole project: keep the OpenAI SDK, change the base URL and model, delete the four other LLM providers.

---

## 3. Restaurant demo capture (parity target)

The demo is "The Golden Fork" reservation assistant named "Ava." It is defined by `examples/restaurant_agent/agent.py` (config, tools, flow), `examples/restaurant_agent/prompt.txt` (instructions), the prompt composer `prompts.py`, and the registry `tools.py`. Reproduce it exactly.

### 3.1 System prompt (composed fresh per LLM call)

There is no single static system prompt. `build_system_prompt(config, context, current_node)` composes it in this order, joined by blank lines:

1. Identity: `You are Ava, the friendly phone assistant for The Golden Fork restaurant.` (from `name="Ava"`, `persona="the friendly phone assistant for The Golden Fork restaurant"`).
2. Voice-call framing (verbatim, always): `You are on a live phone call. The caller speaks to you and hears your replies as speech. You can only hear the caller and speak back; you cannot see, read, or type anything. Never say you are reading or typing. If a turn seems garbled, cut off, or like background noise or another person talking rather than the caller, do not act on it; briefly ask the caller to repeat.`
3. Response style (verbatim, default, not overridden by the restaurant config): `Keep responses short and conversational. Use simple sentences. Ask one question at a time. Never use bullet points or numbered lists (the caller cannot see them).`
4. Current flow-node task (because the config uses flow nodes, the static `instructions` are NOT injected while a node is active). The three node tasks, verbatim:
   - `booking`: `Help the caller book a table. Collect the date, time, and party size, asking for any missing detail one question at a time. As soon as you have all three, call check_availability. You can also answer menu questions with get_menu.`
   - `confirm`: `Availability has been checked. If a table is available, ask for the caller's name and then call make_reservation to finalize. If it is not available, suggest another date or time and call check_availability again.`
   - `farewell`: `The reservation is confirmed. Read back the confirmation number and the details, thank the caller, and say goodbye.`
5. Dynamic context block (when a CallContext exists): `Context:` followed by bullet lines: `Current time: <HH:MM AM/PM TZ>` (always), `Caller number: <from>` (if known), `Information collected so far: <slots>` (if non-empty).
6. Rules (verbatim, always for this config): `Rules:` then `- Do not make up availability or reservation details.` `- Do not accept reservations for more than 8 people.` `- If unsure, offer to transfer to a staff member.`

The static `prompt.txt` (loaded into `instructions` but effectively unused while a flow node is active) reads: `You are Ava, the phone assistant for The Golden Fork restaurant.` plus a paragraph on helping callers, plus the hours line (`open Tuesday through Sunday, 5 PM to 10 PM. We are closed on Mondays. We seat parties of up to 8 people. For larger groups, ask the caller to email events@goldenfork.example.com.`) plus a line to always confirm details before reserving.

### 3.2 Tools (three, verbatim behavior)

The `call_context` parameter is injected automatically and excluded from the JSON schema. Every schema carries `strict: true`.

1. `check_availability(date: str, time: str, party_size: int)`.
   - Description: `Check if a table is available for the given date, time, and party size`.
   - Schema params: `date` (string), `time` (string), `party_size` (integer), all required.
   - Behavior: if `date` contains "monday" (case-insensitive), return `Sorry, the restaurant is closed on Mondays.` (no slots stored). Else if `party_size > 8`, return `We cannot seat parties larger than 8. Please email events@goldenfork.example.com for large group bookings.` (no slots stored). Else store `date`, `time`, `party_size` into `call_context.slots` and return `A table for {party_size} is available on {date} at {time}.`

2. `make_reservation(name: str)`.
   - Description: `Confirm and make a reservation with the caller's name`.
   - Schema params: `name` (string), required.
   - Behavior: with a call context, store `name`, read `date`/`time`/`party_size` from slots (fallbacks `the requested date`, `the requested time`, `your party`), return `Reservation confirmed for {name}: party of {party_size} on {date} at {time}. Confirmation number: GF-{hash(name) % 10000:04d}.` Without a context, return `Reservation confirmed for {name}.` Note: Python `hash()` of a str is process-randomized unless `PYTHONHASHSEED` is fixed, so the confirmation number is non-deterministic across runs. Consider a deterministic id for parity, see Section 8.

3. `get_menu()`.
   - Description: `Get the current menu highlights`.
   - Schema params: none.
   - Returns (verbatim): `Tonight's specials: Pan-seared salmon with lemon butter, $32. Truffle mushroom risotto, $28. Grilled ribeye with roasted vegetables, $45. All entrees come with a house salad.`

### 3.3 Flow and behavior

Three-node graph, `initial_node="booking"`, transitions triggered by successful tool calls:

- `booking` (initial). Tools offered: `check_availability`, `get_menu`. Transition: `check_availability` to `confirm` (a `get_menu` call has no transition, stays in `booking`).
- `confirm`. Tools offered: `make_reservation`, `check_availability`. Transitions: `make_reservation` to `farewell`, `check_availability` self-loop.
- `farewell`. Tools: none. Terminal. After its reply finishes playing, the call hangs up.

Greeting (spoken on connect before the caller speaks): `Thank you for calling The Golden Fork. How can I help you today?` The config does not set `allow_end_call` (no `end_call` tool); the call ends via the terminal `farewell` node. Defaults that matter: `fallback_message = "I'm sorry, I wasn't able to complete that. Could you try again?"`, `max_tool_rounds = 5`, `budget_exceeded_message = "I'm sorry, but I have to wrap up this call now. Please call back if you need anything else. Goodbye!"`

Happy path (from the `book_table` persona, party of four, Friday 7 PM, name Alex): greeting, caller gives details, `check_availability(date="Friday", time="7 PM", party_size=4)`, move to `confirm`, ask name, caller "Alex", `make_reservation(name="Alex")`, move to `farewell`, read back `Reservation confirmed for Alex: party of 4 on Friday at 7 PM. Confirmation number: GF-XXXX.`, thank, goodbye, hang up.

---

## 4. LLM endpoint confirmation

Endpoint: `POST https://api.telnyx.com/v2/ai/chat/completions`. OpenAI-compatible. With the OpenAI SDK set `base_url=https://api.telnyx.com/v2/ai` and the SDK appends `/chat/completions`.

- Auth: same Telnyx V2 API key as Call Control, as `Authorization: Bearer <key>`. One key covers Voice and Inference.
- Request: standard OpenAI body (`model`, `messages`, `stream`, `tools`, `tool_choice`, `temperature`, `max_tokens`, etc.). Telnyx defaults differ from OpenAI: default model is a Llama model and default `temperature` is 0.1, so always set `model` and `temperature` explicitly. Telnyx extensions include `enable_thinking` (boolean, default true), `guided_json`, `guided_choice`, and others.
- Streaming: SSE, OpenAI `chat.completion.chunk` shape, `choices[0].delta.content` for text and `choices[0].delta.tool_calls` for tool fragments, terminated by `data: [DONE]`. Telnyx documents SDK-level delta consumption but does not publish verbatim raw SSE bytes, so if we hand-roll an SSE parser we should verify the `[DONE]` terminator against a live stream. Using the OpenAI SDK avoids this entirely.
- Tool calling: identical to OpenAI. Non-streaming returns an assistant message with `tool_calls: [{id, type:"function", function:{name, arguments}}]`. Streaming sends fragments in `delta.tool_calls` with an `index` to differentiate parallel calls; accumulate by index. Results are fed back as `{role:"tool", tool_call_id, content}`. Telnyx guarantees valid JSON for tool-call arguments, which removes the partial-JSON retry handling sometimes needed with vanilla OpenAI.
- Model: `moonshotai/Kimi-K2.5` is confirmed and is Telnyx's recommended real-time voice model (note the `moonshotai/` prefix; the brief's `Kimi-K2.5` maps to this id). It is being superseded by `moonshotai/Kimi-K2.6` in the current models index, so pin the model id deliberately. For low-latency voice, disable reasoning with `enable_thinking: false`. List models via `GET /v2/ai/models`.

Time-to-first-token: Telnyx publishes no specific number; their latency argument is architectural (inference, STT, TTS on Telnyx GPUs on the same backbone as telephony, avoiding cross-vendor hops). Validate TTFT against the account during the build.

Sources: `developers.telnyx.com/api-reference/openai-chat/create-a-chat-completion-openai-compatible`, `.../docs/inference/models`, `.../docs/inference/functions`, `.../docs/inference/streaming-functions`, `telnyx.com/llm-library/kimi-k2.5`.

---

## 5. Call lifecycle map and state machine

Webhook event types (subset relevant here): `call.initiated`, `call.answered`, `call.speak.started`, `call.speak.ended`, `call.playback.started`, `call.playback.ended`, `call.transcription`, `call.dtmf.received`, `streaming.started`, `streaming.stopped`, `call.hangup`.

Inbound call (requires an explicit answer):
1. `call.initiated` (call arrives, leg parked).
2. We POST `answer` to `/v2/calls/{call_control_id}/actions/answer`.
3. `call.answered`.
4. We POST `transcription_start` (and, under Option B, `streaming_start`). `streaming.started` fires if streaming.
5. `call.transcription` events stream in (interim and final via `is_final`).
6. We POST `speak` for each agent turn. `call.speak.started` then `call.speak.ended`. Barge-in interrupts via `playback_stop`.
7. We POST `hangup` after the terminal node. `call.hangup`.

Outbound call:
1. We POST `/v2/calls` (Dial) with `connection_id`, `to`, `from`. Response returns the `call_control_id`.
2. `call.initiated`.
3. `call.answered` on remote pickup (no explicit answer command needed for outbound).
4. Then the same transcription/speak/hangup sequence as inbound.

`call_control_id` is present in `data.payload` of every webhook and is the id every Call Control command is POSTed against (`/v2/calls/{call_control_id}/actions/{action}`). Inbound calls remain parked until answered.

State machine the agent tracks (Option A):

```
IDLE
  inbound:  call.initiated -> PARKED --answer--> ANSWERED
  outbound: call.initiated -> call.answered ---> ANSWERED
ANSWERED --transcription_start--> LISTENING        (call.transcription interim/final)
LISTENING --assemble turn, call LLM, speak--> SPEAKING   (call.speak.started)
SPEAKING --caller speech (transcription while speaking)--> BARGE_IN --playback_stop--> LISTENING
SPEAKING --call.speak.ended--> LISTENING (or, if terminal node, --hangup--> TERMINATED)
any --call.hangup--> TERMINATED  (clean teardown)
```

---

## 6. Minimal setup surface (for the README later)

1. Telnyx API key. Portal (`portal.telnyx.com`) > Account Settings > API Keys > Create API Token. One V2 key covers Call Control, transcription, speak, and Inference. Shown once.
2. Voice API Application (Call Control App). Portal > Voice > Programmable Voice > Call Control Applications > Create. Set the webhook URL (https). The Application id is the `connection_id` used for outbound Dial. No per-app toggle is needed for transcription/speak; those are runtime commands. An Outbound Voice Profile must be attached for outbound calls.
3. Phone number. Buy a number, assign it to the Voice API Application (for inbound).
4. Public webhook URL. Telnyx must reach the webhook over public https. For local dev, tunnel with ngrok and set the app webhook URL to the tunnel URL.
5. Webhook signature verification. Ed25519 over `{telnyx-timestamp}|{raw body}`, signature in `telnyx-signature-ed25519`. Get the public key from Portal > Account Settings > Keys & Credentials > Public Key. Reject stale timestamps (for example older than 300 seconds). The reference's `verify_inbound` already implements this against `TELNYX_PUBLIC_KEY` using the Telnyx SDK helper.

Env vars the app needs (Option A):

```
TELNYX_API_KEY=            # Bearer token, covers Call Control + Inference
TELNYX_PUBLIC_KEY=         # Ed25519 public key for webhook verification
TELNYX_PHONE_NUMBER=       # E.164, the "from" number for outbound
TELNYX_CONNECTION_ID=      # Voice API Application id (connection_id for Dial)
PUBLIC_WEBHOOK_URL=        # public https base, e.g. https://<sub>.ngrok.app
TELNYX_AI_BASE_URL=https://api.telnyx.com/v2/ai
TELNYX_MODEL=moonshotai/Kimi-K2.5
TELNYX_TTS_VOICE=Telnyx.Natural.abbie     # or Telnyx.NaturalHD.astra
TELNYX_STT_ENGINE=Telnyx                   # or Google (Engine A) for interim results
TELNYX_TTS_LANGUAGE=en-US
# token/concurrency limits as needed
```

Option B adds: `MEDIA_STREAM_WS_URL` (the wss stream_url) and stream codec settings.

---

## 7. Proposed build shape (Option A, subject to review)

A handful of files, fewer if readability holds:

- `agent.py`: the conversational loop and state machine, lifted from the reference core. Keeps the LLM tool loop, conversation/history with interrupt rollback, TurnManager, barge-in state machine, prompts, flow nodes. I/O seams collapsed to Telnyx call commands. The run loop consumes an asyncio queue fed by webhook events instead of a media frame iterator.
- `telnyx.py`: thin typed wrappers over Telnyx Call Control (`answer`, `transcription_start`, `speak`, `playback_stop`, `hangup`, outbound `dial`), the Telnyx LLM client (OpenAI SDK pointed at the Telnyx base URL), and Ed25519 webhook verification. No abstraction.
- `server.py`: FastAPI app, the webhook receiver. Verifies signatures, routes call events to the owning agent instance, returns answer instructions for inbound. (Option B would add the Media Streaming WebSocket handler here.)
- `tools.py`: the restaurant demo tools, registry, and dispatch (lifted).
- `prompts.py`: the system-prompt composer (lifted) plus the Golden Fork config (identity, greeting, flow nodes, rules).
- `settings.py`: flat env-driven settings.
- `README.md`: written last.

The reference's `conversation.py`, `turn_manager.py`, `barge_in.py`, `limits.py`, `utils/retry.py`, `utils/logging.py`, and the type contracts from `providers/base.py` are lifted into these files (or kept as small modules) largely intact.

---

## 8. Open questions and flags for review

1. Architecture path (the one real decision). Recommend Option A for v0; Option B-lite as the first enhancement if barge-in needs to be snappier; Option B-hybrid only if frame-level barge-in is a hard v0 requirement. Section 1.4 has the full reasoning. This is the gate: confirm before building.
2. Barge-in expectation. Option A barge-in is transcription-triggered `playback_stop` (roughly several hundred ms), not frame-level. Confirm that is acceptable for the v0 demo, or escalate to Option B-lite.
3. STT engine choice. Engine A (Google) gives interim results, which help fast barge-in and responsiveness. Engine B (Telnyx) is more accurate and lower cost and latency but interim support is more limited. Recommend Engine A for the demo loop unless cost or pure-Telnyx-engine framing matters more, in which case Engine B.
4. Confirmation-number determinism. The reference uses `hash(name) % 10000`, which is process-randomized. For a clean demo and reproducibility, switch to a deterministic id (for example a stable hash like `zlib.crc32` or a sequence counter). Minor, flag for sign-off.
5. Voice selection. Default to a Telnyx-native voice (`Telnyx.Natural.abbie` or `Telnyx.NaturalHD.astra`). Confirm the preferred default voice.
6. Model pinning. `moonshotai/Kimi-K2.5` per the brief, with `enable_thinking: false` for latency. K2.6 exists and is positioned higher; pin K2.5 unless you want the newer model.
7. Reference docs to ignore: `docs/transports.md` in the reference says `track="both_tracks"` but the working code uses `inbound_track` (to avoid self-transcription echo). The code is correct; the doc is stale. Not relevant to Option A but worth knowing if Option B is chosen.

This concludes discovery. Awaiting review of the Section 1.4 recommendation before writing any build code.
