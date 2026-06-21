"""Turn raw benchmark JSONL into barge-in latency percentiles.

Usage:
    python -m bench.analyze bench/data/run-*.jsonl [--out summary.json]

Each input file is one or more calls' worth of events (see recorder.py). Events
are grouped per trial and reduced to latency components. Multiple files are
pooled (trial numbers are namespaced by file), which is how interleaved runs
across different times/days are combined into one distribution.

Clock accounting (stated explicitly so every number is reproducible):
  - total_telnyx and onset->interim use Telnyx's per-event occurred_at clock,
    the truest "leg event to leg event" timing, free of webhook-delivery jitter.
  - total_server and the our-code components use one host's monotonic receipt
    clock (the agent and harness share it because they share the process/host).
  - onset = harness leg A call.speak.started (the stimulus begins).
    interim = first agent leg B call.transcription after onset.
    stop = agent leg B call.speak.ended after onset (audio actually stops).
"""

from __future__ import annotations

import argparse
import json
import math
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Any


def _parse_occurred_at(value: str) -> float | None:
    """Telnyx occurred_at (ISO 8601, Z-suffixed) to epoch seconds."""
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def _percentile(values: list[float], q: float) -> float:
    """Linear-interpolation percentile (q in [0, 100]); matches numpy default."""
    if not values:
        return math.nan
    s = sorted(values)
    if len(s) == 1:
        return s[0]
    rank = (q / 100.0) * (len(s) - 1)
    lo = math.floor(rank)
    hi = math.ceil(rank)
    if lo == hi:
        return s[int(rank)]
    return s[lo] + (s[hi] - s[lo]) * (rank - lo)


def _first(rows: list[dict[str, Any]], **match: Any) -> dict[str, Any] | None:
    """First row (by seq) whose fields equal the given matchers."""
    hits = [r for r in rows if all(r.get(k) == v for k, v in match.items())]
    hits.sort(key=lambda r: r.get("seq", 0))
    return hits[0] if hits else None


class Trial:
    """The component latencies derived from one barge-in trial, all in ms."""

    def __init__(self) -> None:
        self.total_telnyx: float | None = None
        self.total_server: float | None = None
        self.onset_to_interim: float | None = None
        self.interim_to_stop_issued: float | None = None
        self.stop_issued_to_ended: float | None = None


def _reduce_trial(rows: list[dict[str, Any]]) -> Trial | None:
    """Reduce one trial's rows to latency components, or None if incomplete."""
    onset = _first(rows, event="call.speak.started", leg="a")
    if onset is None:
        return None
    onset_occ = _parse_occurred_at(onset.get("occurred_at", ""))
    onset_recv = onset.get("t_recv")

    # Candidate stop/interim must occur at or after onset.
    def _after_onset(r: dict[str, Any]) -> bool:
        occ = _parse_occurred_at(r.get("occurred_at", ""))
        return occ is not None and onset_occ is not None and occ >= onset_occ

    interims = [
        r
        for r in rows
        if r.get("event") == "call.transcription"
        and r.get("leg") == "b"
        and _after_onset(r)
    ]
    interims.sort(key=lambda r: r.get("seq", 0))
    interim = interims[0] if interims else None

    stops = [
        r
        for r in rows
        if r.get("event") == "call.speak.ended"
        and r.get("leg") == "b"
        and _after_onset(r)
    ]
    stops.sort(key=lambda r: r.get("seq", 0))
    stop = stops[0] if stops else None

    stop_issued = _first(rows, event="barge_stop_issued")

    interim_occ = (
        _parse_occurred_at(interim.get("occurred_at", "")) if interim else None
    )
    stop_occ = _parse_occurred_at(stop.get("occurred_at", "")) if stop else None

    t = Trial()
    # A trial counts only as a CONFIRMED barge-in: the agent emitted
    # barge_stop_issued (it does that solely on the real barge-in path) and the
    # events are onset -> interim -> stop in order. Without barge_stop_issued the
    # stimulus landed while the agent was not speaking, so the matched speak.ended
    # is a later, unrelated utterance end, not a barge-in stop.
    if (
        stop_issued is not None
        and interim is not None
        and interim_occ is not None
        and stop is not None
        and stop_occ is not None
        and onset_occ is not None
        and onset_occ <= interim_occ <= stop_occ
    ):
        t.total_telnyx = (stop_occ - onset_occ) * 1000.0
        t.onset_to_interim = (interim_occ - onset_occ) * 1000.0
        if onset_recv is not None and stop.get("t_recv") is not None:
            t.total_server = (stop["t_recv"] - onset_recv) * 1000.0
    if (
        interim is not None
        and stop_issued is not None
        and interim.get("t_recv") is not None
        and stop_issued.get("t_mono") is not None
    ):
        t.interim_to_stop_issued = (
            stop_issued["t_mono"] - interim["t_recv"]
        ) * 1000.0
    if (
        stop is not None
        and stop_issued is not None
        and stop.get("t_recv") is not None
        and stop_issued.get("t_mono") is not None
    ):
        t.stop_issued_to_ended = (stop["t_recv"] - stop_issued["t_mono"]) * 1000.0
    return t


_METRICS = [
    ("total_telnyx", "TOTAL barge-in (onset->stop, Telnyx clock)"),
    ("onset_to_interim", "  onset -> first interim (vendor STT)"),
    ("interim_to_stop_issued", "  interim -> stop issued (our code)"),
    ("stop_issued_to_ended", "  stop issued -> speak.ended (leg round trip)"),
    ("total_server", "TOTAL barge-in (onset->stop, server clock)"),
]


def _summarize(values: list[float]) -> dict[str, float]:
    clean = [v for v in values if v is not None and not math.isnan(v)]
    return {
        "n": len(clean),
        "min": min(clean) if clean else math.nan,
        "median": _percentile(clean, 50),
        "p95": _percentile(clean, 95),
        "p99": _percentile(clean, 99),
        "max": max(clean) if clean else math.nan,
    }


def analyze(paths: list[Path]) -> dict[str, Any]:
    """Pool the given JSONL files and compute per-component statistics."""
    grouped: dict[tuple[str, int], list[dict[str, Any]]] = defaultdict(list)
    meta: dict[str, Any] = {}
    for path in paths:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            row = json.loads(line)
            if row.get("event") == "run_start" and not meta:
                meta = {
                    k: row.get(k)
                    for k in (
                        "label",
                        "stt_engine",
                        "llm_model",
                        "tts_voice",
                        "from_number",
                        "to_number",
                        "stimulus",
                    )
                }
            trial = row.get("trial", 0)
            if isinstance(trial, int) and trial >= 1:
                grouped[(str(path), trial)].append(row)

    trials = [t for t in (_reduce_trial(rows) for rows in grouped.values()) if t]
    results: dict[str, dict[str, float]] = {}
    for key, _label in _METRICS:
        results[key] = _summarize(
            [getattr(t, key) for t in trials if getattr(t, key) is not None]
        )
    return {
        "meta": meta,
        "files": [str(p) for p in paths],
        "trials_total": len(grouped),
        "trials_with_total": results["total_telnyx"]["n"],
        "metrics": results,
    }


def _fmt(x: float) -> str:
    return "   n/a" if math.isnan(x) else f"{x:6.0f}"


def main() -> None:
    p = argparse.ArgumentParser(prog="bench.analyze")
    p.add_argument("paths", nargs="+", type=Path)
    p.add_argument("--out", type=Path, default=None, help="write summary JSON here")
    a = p.parse_args()

    summary = analyze(a.paths)
    meta = summary["meta"]
    print(f"\nbarge-in latency  -  label={meta.get('label')}  "
          f"N={summary['trials_with_total']} (of {summary['trials_total']} trials)")
    print(f"conditions: STT={meta.get('stt_engine')}  LLM={meta.get('llm_model')}  "
          f"TTS={meta.get('tts_voice')}")
    header = f"{'component (ms)':<46}{'N':>4}"
    header += f"{'min':>7}{'med':>7}{'p95':>7}{'p99':>7}{'max':>7}"
    print(header)
    print("-" * 84)
    for key, label in _METRICS:
        s = summary["metrics"][key]
        print(f"{label:<46}{s['n']:>4}{_fmt(s['min'])}{_fmt(s['median'])}"
              f"{_fmt(s['p95'])}{_fmt(s['p99'])}{_fmt(s['max'])}")
    print()

    if a.out is not None:
        a.out.write_text(json.dumps(summary, indent=2), encoding="utf-8")
        print(f"wrote {a.out}")


if __name__ == "__main__":
    main()
