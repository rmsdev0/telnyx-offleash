"""Offline validation of the benchmark analysis reduction.

These tests pin the latency math against a hand-built event stream so the
harness can be trusted (and re-checked) without placing real calls.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

from bench.analyze import _parse_occurred_at, _percentile, analyze
from bench.controller import _digits

if TYPE_CHECKING:
    from pathlib import Path


def _iso(second: float) -> str:
    """A Telnyx-style occurred_at at minute 0, the given second offset."""
    whole = int(second)
    micros = round((second - whole) * 1_000_000)
    return f"2026-06-21T01:00:{whole:02d}.{micros:06d}Z"


def _trial_rows(
    trial: int,
    *,
    onset_occ: float,
    onset_recv: float,
    interim_occ: float,
    interim_recv: float,
    stop_issued_mono: float,
    stop_occ: float,
    stop_recv: float,
) -> list[dict[str, Any]]:
    return [
        {"event": "fire", "trial": trial, "leg": "a", "call": "A"},
        {
            "event": "call.speak.started",
            "trial": trial,
            "leg": "a",
            "call": "A",
            "occurred_at": _iso(onset_occ),
            "t_recv": onset_recv,
        },
        {
            "event": "call.transcription",
            "trial": trial,
            "leg": "b",
            "call": "B",
            "occurred_at": _iso(interim_occ),
            "t_recv": interim_recv,
            "is_final": False,
            "text": "excuse",
        },
        {
            "event": "barge_stop_issued",
            "trial": trial,
            "leg": "b",
            "call": "B",
            "t_mono": stop_issued_mono,
        },
        {
            "event": "call.speak.ended",
            "trial": trial,
            "leg": "b",
            "call": "B",
            "occurred_at": _iso(stop_occ),
            "t_recv": stop_recv,
        },
    ]


def _write(path: Path, rows: list[dict[str, Any]]) -> None:
    path.write_text(
        "\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8"
    )


def test_reduce_single_trial(tmp_path: Path) -> None:
    rows: list[dict[str, Any]] = [
        {"event": "run_start", "trial": 0, "label": "t", "stt_engine": "Google"},
        {"event": "call.speak.started", "trial": 0, "leg": "b"},  # ignored (trial 0)
    ]
    rows += _trial_rows(
        1,
        onset_occ=0.0,
        onset_recv=1000.0,
        interim_occ=0.300,
        interim_recv=1000.310,
        stop_issued_mono=1000.330,
        stop_occ=0.450,
        stop_recv=1000.500,
    )
    path = tmp_path / "run.jsonl"
    _write(path, rows)

    summary = analyze([path])
    m = summary["metrics"]
    assert summary["trials_with_total"] == 1
    assert abs(m["total_telnyx"]["median"] - 450.0) < 1.0
    assert abs(m["onset_to_interim"]["median"] - 300.0) < 1.0
    assert abs(m["total_server"]["median"] - 500.0) < 1.0
    assert abs(m["interim_to_stop_issued"]["median"] - 20.0) < 1.0
    assert abs(m["stop_issued_to_ended"]["median"] - 170.0) < 1.0


def test_pools_files_and_percentiles(tmp_path: Path) -> None:
    # Two files (interleaved runs); totals 100, 200, 300 across three trials.
    p1 = tmp_path / "a.jsonl"
    p2 = tmp_path / "b.jsonl"
    _write(
        p1,
        _trial_rows(
            1, onset_occ=0.0, onset_recv=1.0, interim_occ=0.05, interim_recv=1.05,
            stop_issued_mono=1.06, stop_occ=0.100, stop_recv=1.10,
        )
        + _trial_rows(
            2, onset_occ=2.0, onset_recv=3.0, interim_occ=2.05, interim_recv=3.05,
            stop_issued_mono=3.06, stop_occ=2.200, stop_recv=3.20,
        ),
    )
    _write(
        p2,
        _trial_rows(
            1, onset_occ=0.0, onset_recv=5.0, interim_occ=0.05, interim_recv=5.05,
            stop_issued_mono=5.06, stop_occ=0.300, stop_recv=5.30,
        ),
    )
    summary = analyze([p1, p2])
    assert summary["trials_with_total"] == 3
    # median of {100, 200, 300} is 200.
    assert abs(summary["metrics"]["total_telnyx"]["median"] - 200.0) < 1.0


def test_incomplete_trial_is_dropped(tmp_path: Path) -> None:
    # Onset present but no stop and no interim -> contributes no total.
    rows: list[dict[str, Any]] = [
        {
            "event": "call.speak.started",
            "trial": 1,
            "leg": "a",
            "call": "A",
            "occurred_at": _iso(0.0),
            "t_recv": 1.0,
        },
    ]
    path = tmp_path / "run.jsonl"
    _write(path, rows)
    summary = analyze([path])
    assert summary["trials_total"] == 1
    assert summary["trials_with_total"] == 0


def test_helpers() -> None:
    assert _digits("+1 (702) 425-5143") == "7024255143"
    assert _parse_occurred_at("") is None
    assert _parse_occurred_at("not-a-date") is None
    got = _parse_occurred_at("2026-06-21T01:00:00.500000Z")
    assert got is not None
    assert _percentile([10.0, 20.0, 30.0], 50) == 20.0
    assert _percentile([], 50) != _percentile([], 50)  # nan
