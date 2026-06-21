# Barge-in latency, measured

**Headline:** median single-vendor barge-in latency **1328 ms** (stimulus audio
onset to agent audio stop, Telnyx event clock), p95 2250 ms, p99 2955 ms.
N = 32 confirmed barge-ins over real US PSTN calls, Google STT (interim) +
Kimi-K2.5 + Telnyx NaturalHD, 2026-06-21. **~83% of that is one component the
agent cannot influence: the STT first-interim latency (median 1104 ms).** Our
code's contribution is ~1 ms. Reproduce with `python -m bench.analyze bench/data/run*.jsonl`.

This is a measurement, not an estimate. It quantifies how long the single-vendor
(Telnyx-only, Call Control) offleash agent takes to stop talking when a caller
talks over it, and decomposes that latency so the bottleneck is visible.

Everything here is reproducible from this repo: the harness, the raw per-event
data, and the analysis script are all checked in (`bench/`, `bench/data/`).

## What "barge-in latency" means here

Barge-in latency = time from **the caller's audio starting over the agent** to
**the agent's audio actually stopping on its leg**.

It decomposes into three components, measured separately because the split is the
whole point:

1. **onset -> first interim** — caller audio onset to the first `call.transcription`
   (interim) the agent receives. This is the dominant, vendor-bound component: the
   agent cannot react until Telnyx STT emits a partial transcript.
2. **interim -> stop issued** — the agent receiving that transcript to it issuing
   the `playback_stop` Call Control POST. This is offleash's own contribution.
3. **stop issued -> audio stops** — the leg-level stop. Measured via the agent
   leg's `call.speak.ended`, the closest server-side signal that audio ceased.
   This is a proxy (it includes webhook-delivery time); it is labelled as such.

## Method: Option 1, a controlled audio harness

The honest problem with Option A (call-command, no media stream) is that the
agent cannot observe acoustic onset. So we make the onset known by construction:
a scripted harness *is* the caller and injects the barge-in stimulus at a
recorded time.

Topology (see [bench/controller.py](bench/controller.py)):

- The harness places an outbound call from a **second Telnyx number** to the
  agent's number, on the **same Call Control connection**. Telnyx produces two
  control legs for the one call, both delivered to the same server process:
  - **leg A** (outbound) is harness-owned: normal agent dispatch is skipped for
    it. The harness drives it.
  - **leg B** (inbound) is the **real, unmodified VoiceAgent**, answering and
    running exactly as it would for any caller. The harness only observes it.
- The two legs are **bridged** so audio flows: the agent hears the stimulus on
  its inbound track, the harness hears the agent.
- Per trial, the harness waits for the agent to start speaking (leg B
  `call.speak.started`), lets it run ~0.8 s into the utterance, then speaks a
  fixed stimulus on leg A. The agent transcribes it and issues `playback_stop`.

Because both legs land in one process, every timestamp is on one machine.

Reference points:

- **onset** = leg A `call.speak.started` (Telnyx-stamped: the stimulus begins).
- **interim** = leg B's first `call.transcription` after onset.
- **stop** = leg B `call.speak.ended` after onset (agent audio stops).

The agent itself was **not changed to flatter the numbers**. The only additions
to shipping code are: a no-op-by-default metric sink ([offleash/metrics.py](offleash/metrics.py))
that records the `playback_stop` issue time when the harness is attached, and a
webhook receipt timestamp. Both are inert in production (no sink registered).

## Clock accounting

Stated explicitly so every figure is reproducible:

- **TOTAL (Telnyx clock)** and **onset -> interim** use Telnyx's per-event
  `occurred_at`. Because onset and stop are both Telnyx-stamped leg events, their
  difference is free of webhook-delivery jitter. This is the headline clock.
- **TOTAL (server clock)** and the our-code components use one host's monotonic
  receipt clock. The harness and the agent share it because they share the
  process and host, so cross-component subtraction is valid.
- Component 3 (`stop issued -> speak.ended`) is a **proxy/upper bound**: it
  includes the time for the `call.speak.ended` webhook to be delivered to us, so
  the true audio-stop moment is a little earlier.

A small fixed offset not captured: leg A -> leg B media transit (intra-Telnyx),
on the order of a few tens of ms, sits inside `onset -> interim`. It is reported,
not hidden, and is dwarfed by the STT component.

## Conditions

Pin them, because a latency number without its conditions is not reproducible.

| | |
|---|---|
| Date | 2026-06-21, ~02:39-02:58 UTC |
| Runs | 3 interleaved runs over ~20 min (`run1`, `run2`, `run3`), pooled |
| Transport | Real US PSTN, both legs on one Telnyx Call Control connection |
| Agent number / harness number | +17024255143 / +14157180773 |
| STT | Google engine, `interim_results`, language `en`, `inbound` track |
| LLM | moonshotai/Kimi-K2.5, `enable_thinking` off |
| TTS | Telnyx.NaturalHD.astra, `en-US` |
| Stimulus | "Excuse me, sorry to interrupt, I have a quick question." (Telnyx TTS on leg A) |
| Barge offset | fired ~0.8 s into the agent's utterance |
| N | 32 confirmed barge-ins (a barge-in is confirmed when the agent issues `playback_stop`) |

On yield: across the three runs the harness fired ~70 stimuli; 32 produced a
confirmed barge-in. The rest landed while the agent was between utterances (the
agent's reply had already ended before the ~1 s STT interim arrived) and are
excluded as non-barge-ins, not counted as latency. Yield is a harness-timing
artifact and does not bias the latency of the barge-ins that did fire (STT
first-interim latency is independent of what or how long the agent was saying).

Stability across the pooled runs (median total, Telnyx clock): 1248 ms (N=19,
run1) -> 1317 ms (N=22, run1+2) -> 1328 ms (N=32, all three). Not a single-run
anecdote. The tail is wide, as telephony tails are: extending across multiple
days would firm up p95/p99 further and is the recommended follow-up.

## Results

All values in milliseconds, pooled across the three runs (N as noted per row).
`bench/data/summary.json` is the machine-readable form; the table is from
`python -m bench.analyze bench/data/run1.jsonl bench/data/run2.jsonl bench/data/run3.jsonl`.

| component (ms) | N | min | median | p95 | p99 | max | clock |
|---|---|---|---|---|---|---|---|
| **TOTAL barge-in** (onset -> stop) | 32 | 928 | **1328** | 2250 | 2955 | 3237 | Telnyx `occurred_at` |
| &nbsp;&nbsp;onset -> first interim (vendor STT) | 32 | 708 | 1104 | 2115 | 2613 | 2777 | Telnyx `occurred_at` |
| &nbsp;&nbsp;interim -> stop issued (our code) | 33 | 1 | 1 | 2 | 2 | 2 | host monotonic |
| &nbsp;&nbsp;stop issued -> speak.ended (leg round trip) | 32 | 69 | 203 | 473 | 627 | 656 | host monotonic |
| TOTAL barge-in (onset -> stop), cross-check | 32 | 891 | 1300 | 2291 | 3013 | 3238 | host monotonic |

Reading it: the median barge-in takes **1.33 s**, and **1.10 s of that (83%) is
the wait for the first STT interim** -- the component a transcript-triggered
agent cannot avoid. offleash's own work (decide to stop, issue `playback_stop`)
is **1 ms**. The leg-level stop adds ~200 ms median. The two independent totals
(Telnyx event clock vs our host's monotonic receipt clock) agree to within
~30 ms at the median, which is the cross-check that the timing is sound.

## The frame-level contrast (the most valuable comparison)

The strength of this finding is the contrast with frame-level barge-in (the
voice-agent-lite media-streaming path: bidirectional RTP + a local VAD that fires
on speech-energy onset, then a buffer flush). That path is not in this repo, so
it is **a documented follow-up rather than a measured delta here**.

Qualitatively, the architectural reason is decisive and matches the decomposition
above: frame-level barge-in fires on acoustic onset in tens of milliseconds,
whereas transcript-triggered barge-in cannot fire until STT emits a partial
transcript. Component 1 (onset -> first interim) *is* that gap, and it dominates
the total. Measuring voice-agent-lite with this same harness over Telnyx
transport, to produce the exact delta, is the recommended next step.

## Caveats

- Component 3 is a webhook-delivery-inflated proxy for "audio stops"; treat it as
  an upper bound.
- "onset" is the stimulus audio start (a clean, Telnyx-played utterance), so the
  numbers exclude a real caller's mouth-to-carrier ingress, which is the same for
  any agent architecture and not vendor-dependent.
- Trials with no interim between onset and stop are discarded as natural
  utterance-ends, not barge-ins (see [bench/analyze.py](bench/analyze.py)).

## Reproduce

Server prerequisites: a running offleash deploy reachable by Telnyx webhooks
(e.g. `ngrok http 8000`), and a second Telnyx number on the same Call Control
connection to dial from.

```
# 1. Run the harness (it runs the server in bench mode, places calls, captures data).
#    Stop any normal server on the same port first; the harness must own it.
python -m offleash bench --from +1XXXXXXXXXX --events 25 --label run1 --out bench/data/run1.jsonl

# 2. Repeat at a different time for an interleaved second run.
python -m offleash bench --from +1XXXXXXXXXX --events 25 --label run2 --out bench/data/run2.jsonl

# 3. Pool and analyze (median / p95 / p99 per component).
python -m bench.analyze bench/data/run1.jsonl bench/data/run2.jsonl
```

The analysis math is unit-tested offline in [tests/test_bench.py](tests/test_bench.py)
so the reduction can be checked without placing calls.
