# telnyx-offleash

A complete real-time voice phone agent where every layer (transport, speech-to-text, text-to-speech, and LLM inference) is Telnyx, in about 1,600 lines of Python. One vendor, one API key, one private network, end to end.

## The gap it fills

Telnyx ships every primitive a self-hosted voice agent needs, but no open reference assembles them into a controllable conversational loop with real tool calling. The managed Telnyx AI Assistant is the leashed version: configured in a console, with no real control over the tool loop, the prompt, the model, or the turn behavior. This repo is the same primitives with the leash off and the developer holding it:

- Real tool calling: functions you define, dispatched and looped in your own code (see the restaurant demo).
- Model and prompt tuning: the system prompt is rebuilt every turn from live state, and the model is a value you set.
- Custom loop behavior: barge-in, flow nodes, spend caps, and teardown are all yours to change.

It is built in the open, minimal and bulletproof. It does not depend on the reference framework it was lifted from; the code is lifted in, not imported.

## Architecture

This is "Option A" from [DISCOVERY.md](DISCOVERY.md): pure Telnyx Call Control orchestration. Speech-to-text and text-to-speech are issued as call commands and their results arrive as webhooks. There is no media WebSocket to manage.

```
        PSTN
   caller  ────────────►  Telnyx Programmable Voice
     ▲                       │  transport + STT (transcription_start)
     │  speak audio          │         + TTS (speak)
     │                       │
     │   webhooks            │   Call Control commands
     │   call.initiated      │   answer
     │   call.answered       ▼   transcription_start
     │   call.transcription      speak
     │   call.speak.ended        playback_stop   (barge-in)
     │   call.hangup             hangup
     │       │                       ▲
     │       ▼                       │
   offleash server (FastAPI)  ───────┘
   Ed25519-verified webhook
           │
           ▼
   VoiceAgent  ── one event queue per call ──►  turn manager ─► LLM tool loop
           │                                                         │
           └──────────  chat/completions  ──────────────────────────┘
                        Telnyx Inference (Kimi-K2.5, OpenAI-compatible, with tools)
```

Per call, the loop is:

1. The caller speaks. Telnyx transcribes and posts `call.transcription` webhooks (interim and final).
2. The server turns those into events on the agent's queue. The turn manager assembles a complete user turn.
3. The agent calls Telnyx Inference, runs the tool loop (executing functions and feeding results back) until the model returns text, then speaks the reply with one `speak` command.
4. Barge-in: a transcript arriving while the agent is speaking issues `playback_stop`, cancels the in-flight response, and returns to listening.
5. On a terminal flow node or a caller hangup, the call tears down cleanly.

The files:

| File | Role |
|------|------|
| `offleash/agent.py` | The conversational loop and state machine: tool loop, flow nodes, barge-in, interrupted-turn rollback, spend caps. |
| `offleash/telnyx.py` | Thin typed wrappers over Call Control, the LLM client, and Ed25519 webhook verification. No abstraction. |
| `offleash/server.py` | FastAPI webhook receiver and per-call lifecycle dispatcher. |
| `offleash/tools.py` | The tool registry and the three restaurant demo tools. |
| `offleash/prompts.py` | The system-prompt composer and the Golden Fork agent config. |
| `offleash/settings.py` | Flat, env-driven settings. |
| `offleash/{conversation,turn_manager,barge_in,limits,retry,logging,types}.py` | The orchestration core, lifted from voice-agent-lite. |

## The restaurant demo

The default agent is Ava, the reservations assistant for "The Golden Fork", recreated faithfully from the voice-agent-lite demo so the two are a direct comparison. It runs a three-node flow (booking, then confirm, then a terminal farewell) over three tools: `check_availability`, `make_reservation`, and `get_menu`. The reservation confirmation number is deterministic (a crc32 of the name) so a given caller always gets the same number.

A happy path: the agent greets, collects date, time, and party size, checks availability, asks for a name, makes the reservation, reads back the confirmation, and hangs up.

## A note on barge-in (a finding, not an omission)

This is what a voice agent built only on Telnyx's public primitives actually looks like, and the primitive surface sets the responsiveness floor.

Telnyx speech-to-text (`transcription_start`) and text-to-speech (`speak`) are leg-bound Call Control commands, not stream-attachable: transcripts are delivered as webhooks and speech plays directly onto the call leg. There is no way to point Telnyx STT at a media stream. So a purely single-vendor agent interrupts via a transcript-triggered `playback_stop`, which sets a barge-in floor of roughly a few hundred milliseconds rather than frame-level. To keep that floor as low as the primitives allow, this build uses the Google transcription engine for interim results and fires the interrupt on the first interim transcript while the agent is speaking, not on the final.

Frame-level barge-in would require running Telnyx Media Streaming for the audio path (bidirectional RTP) in parallel with leg-bound transcription, and detecting speech onset locally with a VAD. That is a real enhancement path, documented in [DISCOVERY.md](DISCOVERY.md) as Option B-lite, and the architecture here does not block it. It is out of scope for v0 by design: the call-command build is smaller, has fewer moving parts, and uses every primitive exactly as Telnyx supports it.

## Setup

You need a Telnyx account. One V2 API key authenticates Call Control, transcription, speak, and inference.

1. Create an API key. Portal > Account Settings > API Keys > Create API Token. Copy it once.
2. Get the webhook public key. Portal > Account Settings > Keys & Credentials > Public Key. This is the Ed25519 key used to verify webhooks.
3. Create a Voice API (Call Control) Application. Portal > Voice > Programmable Voice > Call Control Applications. Set its webhook URL to `{your public https url}/webhook`. Attach an Outbound Voice Profile if you want to place outbound calls. The Application id is your `TELNYX_CONNECTION_ID`.
4. Buy a phone number and assign it to that application (for inbound).
5. Expose your local server publicly. For local development, run a tunnel such as `ngrok http 8000` and set the application's webhook URL (in the Telnyx portal) to `{that https url}/webhook`. The webhook URL lives in the portal, not in the app's environment.

Then configure the environment. Copy `.env.example` to `.env` and fill it in:

```
TELNYX_API_KEY=          # one key for Call Control and Inference
TELNYX_PUBLIC_KEY=       # Ed25519 public key for webhook verification
TELNYX_PHONE_NUMBER=     # E.164, the "from" number for outbound
TELNYX_CONNECTION_ID=    # Voice API Application id
```

Everything else (model, voice, STT engine, limits) has sensible defaults in `.env.example`.

## Run

With [uv](https://docs.astral.sh/uv/):

```
uv run --extra dev offleash
```

Or with a plain virtualenv:

```
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
python -m offleash
```

The server listens on `PORT` (default 8000) and serves the signed webhook at `/webhook` and a health check at `/health`. Point your Telnyx application's webhook URL at `{your public https url}/webhook` and call your number.

To place an outbound call (the server must be running to handle the resulting webhooks):

```
python -m offleash call +1XXXXXXXXXX
```

## Tests

```
uv run --extra dev pytest
```

The suite covers the tools, Ed25519 webhook verification, the full reservation flow, barge-in and recovery, speak-generation correlation, the token budget, the tool-round cap, LLM-failure fallback, response supersession, capacity rejection, and clean teardown, all with fakes so no Telnyx account or network is needed.

## Troubleshooting

These are the non-obvious Telnyx requirements this agent ran into in practice:

- Use a Call Control (Voice API) application, not a TeXML application. TeXML posts form-encoded webhooks and expects XML back; this agent needs JSON event webhooks and drives the call with REST commands. If a form-encoded webhook arrives, the server logs `webhook.texml_application_detected`.
- The account must be verified to at least Level 2. Trial accounts prepend an abuse notice onto all Telnyx machine-generated speech, so the agent's voice is replaced by that notice. Add a payment method and complete verification.
- The transcription language is a short code such as `en`. The transcription API rejects `en-US` (which the speak command, confusingly, does accept), so the speak and transcription languages are separate settings.

## Defaults

- LLM: `moonshotai/Kimi-K2.5`, Telnyx's recommended real-time voice model, with reasoning disabled for latency. Pin a different model with `LLM_MODEL`.
- Voice: `Telnyx.Natural.abbie`. Change it with `TTS_VOICE`.
- STT engine: Google (Engine A), for interim results. Change it with `STT_ENGINE`.

## Related

[voice-agent-lite](https://github.com/) is the vendor-neutral, swappable framework this agent was lifted from: multiple STT, TTS, and LLM providers behind clean interfaces, with Twilio and Telnyx transports. If you want to compare providers or keep your options open, start there. This repo is the opposite bet: one vendor, no abstraction, the smallest honest implementation of a Telnyx-only voice agent.
