"""The controlled barge-in harness (Option 1).

Topology: the harness dials the agent's number from a second number on the same
Call Control connection. Telnyx creates two control legs for the one call, both
delivered to this server process:

  leg A (outbound, harness-owned): this controller drives it. The normal agent
         dispatch is skipped for it (handle_webhook returns True).
  leg B (inbound, agent-owned):    the real VoiceAgent answers and runs on it,
         exactly as it would for any caller. We only observe its events.

Per barge-in trial the controller waits for the agent to start speaking on leg B
(call.speak.started), lets it run barge_offset_s into the utterance, then speaks
a fixed stimulus on leg A. The agent hears that audio on its inbound track,
transcribes it, and issues playback_stop. We record:

  onset  = leg A call.speak.started  (Telnyx-stamped, the stimulus begins)
  interim= leg B call.transcription  (first partial after onset)
  stop   = leg B call.speak.ended    (agent audio actually stops)

Everything lands in one process, so onset/interim/stop share Telnyx's occurred_at
clock, and our receipt timestamps share one host monotonic clock. analyze.py
turns these into the latency components.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import structlog

from offleash.telnyx import encode_client_state

if TYPE_CHECKING:
    from collections.abc import Callable

    from bench.recorder import Recorder
    from offleash.settings import Settings
    from offleash.telnyx import TelnyxClient

log = structlog.get_logger()

# Events worth keeping in the raw capture. Interims are included in full: the
# first one after onset is the heart of the measurement.
_RECORDED = frozenset(
    {
        "call.initiated",
        "call.answered",
        "call.speak.started",
        "call.speak.ended",
        "call.playback.started",
        "call.playback.ended",
        "call.transcription",
        "call.hangup",
    }
)


def _digits(number: str) -> str:
    """Last 10 digits of a number, for tolerant E.164 comparison."""
    d = "".join(c for c in number if c.isdigit())
    return d[-10:]


@dataclass(frozen=True, slots=True)
class BenchConfig:
    """One measurement run's parameters."""

    to_number: str  # the agent under test
    from_number: str  # the harness's second number
    connection_id: str  # the agent's Call Control application
    label: str  # condition tag recorded with every run
    target_events: int  # stop after this many completed barge-in trials
    max_calls: int  # safety cap on redials
    trials_per_call: int  # barge-ins per call before redial (0 = unlimited)
    stimulus_text: str  # what the harness says to barge in
    barge_offset_s: float  # how far into the agent's speech to fire
    answer_timeout_s: float
    speak_start_timeout_s: float
    stop_timeout_s: float
    inter_call_pause_s: float
    trial_pause_s: float

    @classmethod
    def from_env(cls, env: dict[str, str], settings: Settings) -> BenchConfig:
        """Build from BENCH_* environment variables, defaulting to settings."""
        return cls(
            to_number=env.get("BENCH_TO", "") or settings.telnyx_phone_number,
            from_number=env.get("BENCH_FROM", ""),
            connection_id=env.get("BENCH_CONNECTION", "")
            or settings.telnyx_connection_id,
            label=env.get("BENCH_LABEL", "default"),
            target_events=int(env.get("BENCH_TARGET_EVENTS", "35")),
            max_calls=int(env.get("BENCH_MAX_CALLS", "15")),
            trials_per_call=int(env.get("BENCH_TRIALS_PER_CALL", "0")),
            stimulus_text=env.get(
                "BENCH_STIMULUS_TEXT",
                "Excuse me, sorry to interrupt, I have a quick question.",
            ),
            barge_offset_s=float(env.get("BENCH_BARGE_OFFSET_S", "0.8")),
            answer_timeout_s=float(env.get("BENCH_ANSWER_TIMEOUT_S", "45")),
            speak_start_timeout_s=float(env.get("BENCH_SPEAK_START_TIMEOUT_S", "25")),
            stop_timeout_s=float(env.get("BENCH_STOP_TIMEOUT_S", "10")),
            inter_call_pause_s=float(env.get("BENCH_INTER_CALL_PAUSE_S", "4")),
            trial_pause_s=float(env.get("BENCH_TRIAL_PAUSE_S", "1.5")),
        )


class BenchController:
    """Drives leg A, observes both legs, and records raw timing."""

    def __init__(
        self,
        telnyx: TelnyxClient,
        settings: Settings,
        cfg: BenchConfig,
        recorder: Recorder,
    ) -> None:
        self._telnyx = telnyx
        self._settings = settings
        self._cfg = cfg
        self._recorder = recorder
        self._from_n = _digits(cfg.from_number)
        self._to_n = _digits(cfg.to_number)

        self._cond = asyncio.Condition()
        self._leg_of: dict[str, str] = {}
        self.leg_a: str | None = None
        self.leg_b: str | None = None
        self._answered: set[str] = set()
        self._speak_started_b = 0
        self._speak_ended_b = 0
        self._ended = False

        self.current_trial = 0
        self.events_done = 0
        self.finished = asyncio.Event()

    # ── recording ────────────────────────────────────────────────

    def _emit(self, event: str, **fields: Any) -> None:
        row: dict[str, Any] = {"event": event}
        row["trial"] = fields.pop("trial", self.current_trial)
        row.update(fields)
        self._recorder.write(row)

    # MetricSink: the agent reports its barge-in stop timing here.
    def record(self, event: str, fields: dict[str, Any]) -> None:
        call = str(fields.get("call", ""))
        self._emit(event, leg=self._leg_of.get(call, "?"), **fields)

    # ── webhook observation (the _BenchHook protocol) ────────────

    def _classify(self, payload: dict[str, Any], ccid: str) -> str:
        leg = self._leg_of.get(ccid)
        if leg is not None:
            return leg
        direction = payload.get("direction", "")
        if (
            _digits(payload.get("from", "")) == self._from_n
            and _digits(payload.get("to", "")) == self._to_n
        ):
            if direction == "outgoing":
                self._leg_of[ccid] = "a"
                self.leg_a = ccid
                return "a"
            if direction == "incoming":
                self._leg_of[ccid] = "b"
                self.leg_b = ccid
                return "b"
        return "?"

    async def handle_webhook(
        self,
        event_type: str,
        payload: dict[str, Any],
        call_control_id: str,
        occurred_at: str,
        t_recv: float,
        w_recv: float,
    ) -> bool:
        """Record the event, advance the state machine, own leg A."""
        leg = self._classify(payload, call_control_id)

        if event_type in _RECORDED:
            extra: dict[str, Any] = {}
            if event_type == "call.transcription":
                td = payload.get("transcription_data", {}) or {}
                extra = {
                    "is_final": bool(td.get("is_final")),
                    "text": str(td.get("transcript", ""))[:120],
                }
            self._emit(
                event_type,
                leg=leg,
                call=call_control_id,
                occurred_at=occurred_at,
                t_recv=t_recv,
                t_wall=w_recv,
                **extra,
            )

        async with self._cond:
            if event_type == "call.answered":
                self._answered.add(call_control_id)
            elif event_type == "call.speak.started" and leg == "b":
                self._speak_started_b += 1
            elif event_type == "call.speak.ended" and leg == "b":
                self._speak_ended_b += 1
            elif event_type == "call.hangup" and leg in ("a", "b"):
                self._ended = True
            self._cond.notify_all()

        return leg == "a"

    # ── the measurement loop ─────────────────────────────────────

    async def _wait(self, pred: Callable[[], bool], timeout: float) -> bool:
        """Wait until pred() holds or timeout elapses. True if it held."""
        try:
            async with self._cond:
                await asyncio.wait_for(self._cond.wait_for(pred), timeout)
            return True
        except TimeoutError:
            return False

    async def _safe_hangup(self, ccid: str) -> None:
        try:
            await self._telnyx.call(ccid).hangup()
        except Exception as e:  # noqa: BLE001 - teardown is best effort
            log.info("bench.hangup_skipped", error=str(e))

    async def _bridge(self, leg_a: str, leg_b: str) -> None:
        """Connect the two legs' media so audio flows both ways."""
        try:
            await self._telnyx.action(leg_a, "bridge", {"call_control_id": leg_b})
            self._emit("bridge", leg="a", call=leg_a, other=leg_b, t_wall=time.time())
            log.info("bench.bridged", leg_a=leg_a, leg_b=leg_b)
        except Exception as e:  # noqa: BLE001 - record and continue
            self._emit("bridge_failed", error=str(e))
            log.warning("bench.bridge_failed", error=str(e))

    def _reset_call(self) -> None:
        self._answered.clear()
        self._speak_started_b = 0
        self._speak_ended_b = 0
        self._ended = False
        self.leg_a = None
        self.leg_b = None
        self._leg_of.clear()

    async def run(self) -> None:
        """Place calls and run barge-in trials until the target is reached."""
        await asyncio.sleep(1.0)  # let the server finish coming up
        self._emit(
            "run_start",
            label=self._cfg.label,
            target=self._cfg.target_events,
            from_number=self._cfg.from_number,
            to_number=self._cfg.to_number,
            stimulus=self._cfg.stimulus_text,
            stt_engine=self._settings.stt_engine,
            llm_model=self._settings.llm_model,
            tts_voice=self._settings.tts_voice,
            t_wall=time.time(),
        )
        log.info(
            "bench.run_start",
            target=self._cfg.target_events,
            to=self._cfg.to_number,
            from_=self._cfg.from_number,
        )
        try:
            for call_idx in range(self._cfg.max_calls):
                if self.events_done >= self._cfg.target_events:
                    break
                await self._one_call(call_idx)
                if self.events_done < self._cfg.target_events:
                    await asyncio.sleep(self._cfg.inter_call_pause_s)
        except asyncio.CancelledError:
            log.info("bench.cancelled", events_done=self.events_done)
            raise
        except Exception:
            log.exception("bench.run_failed")
        finally:
            self._emit("run_end", events_done=self.events_done, t_wall=time.time())
            log.info("bench.run_end", events_done=self.events_done)
            self._recorder.close()
            self.finished.set()

    async def _one_call(self, idx: int) -> None:
        self._reset_call()
        try:
            leg_a = await self._telnyx.dial(
                to=self._cfg.to_number,
                from_=self._cfg.from_number,
                connection_id=self._cfg.connection_id,
            )
        except Exception as e:  # noqa: BLE001 - record and move on
            self._emit("dial_failed", error=str(e))
            log.warning("bench.dial_failed", error=str(e))
            return
        self.leg_a = leg_a
        self._leg_of[leg_a] = "a"
        self._emit("dial", call=leg_a, call_index=idx, t_wall=time.time())

        if not await self._wait(
            lambda: leg_a in self._answered or self._ended, self._cfg.answer_timeout_s
        ):
            self._emit("answer_timeout", call=leg_a)
            await self._safe_hangup(leg_a)
            return
        if self._ended:
            await self._safe_hangup(leg_a)
            return

        # Dialing our own DID yields two app-controlled legs with no media path
        # between them, so bridge them: the agent must hear the stimulus on its
        # inbound track and we must hear the agent. Without this the agent
        # transcribes silence and never barges in.
        await self._wait(lambda: self.leg_b is not None or self._ended, 5.0)
        if self.leg_b is None or self._ended:
            self._emit("no_leg_b", call=leg_a)
            await self._safe_hangup(leg_a)
            return
        await self._bridge(leg_a, self.leg_b)

        trials_this_call = 0
        while self.events_done < self._cfg.target_events and not self._ended:
            done = await self._one_trial(leg_a)
            if not done:
                break
            trials_this_call += 1
            if (
                self._cfg.trials_per_call
                and trials_this_call >= self._cfg.trials_per_call
            ):
                break
            await asyncio.sleep(self._cfg.trial_pause_s)

        await self._safe_hangup(leg_a)

    async def _one_trial(self, leg_a: str) -> bool:
        """Run one barge-in. Returns False if this call can yield no more."""
        # Barge into the utterance the agent is speaking now; if it is between
        # utterances, wait for the next one to start.
        if self._speak_started_b <= self._speak_ended_b:
            baseline_started = self._speak_started_b
            if not await self._wait(
                lambda: self._speak_started_b > baseline_started or self._ended,
                self._cfg.speak_start_timeout_s,
            ):
                self._emit("no_speak_started", call=leg_a)
                return False
        if self._ended:
            return False

        baseline_ended = self._speak_ended_b
        # Let the utterance run so the barge-in lands mid-speech, the realistic
        # case. If it already ended (a very short utterance), try the next one.
        await asyncio.sleep(self._cfg.barge_offset_s)
        if self._speak_ended_b > baseline_ended or self._ended:
            self._emit("utterance_too_short", call=leg_a)
            return not self._ended

        self.current_trial += 1
        trial = self.current_trial
        self._emit(
            "fire",
            trial=trial,
            leg="a",
            call=leg_a,
            t_mono=time.monotonic(),
            t_wall=time.time(),
        )
        try:
            await self._telnyx.call(leg_a).speak(
                self._cfg.stimulus_text,
                client_state=encode_client_state(f"bench:{trial}"),
            )
        except Exception as e:  # noqa: BLE001 - record and continue
            self._emit("fire_failed", trial=trial, error=str(e))
            log.warning("bench.fire_failed", trial=trial, error=str(e))
            return not self._ended

        # The barge-in resolves when the agent's leg B speak ends.
        got_stop = await self._wait(
            lambda: self._speak_ended_b > baseline_ended or self._ended,
            self._cfg.stop_timeout_s,
        )
        self.events_done += 1
        self._emit(
            "trial_done", trial=trial, got_stop=got_stop, events_done=self.events_done
        )
        log.info(
            "bench.trial_done",
            trial=trial,
            got_stop=got_stop,
            events_done=self.events_done,
        )
        return not self._ended
