"""Optional metrics sink for the barge-in benchmark.

Shipping code calls record() on a few hot-path events (the barge-in stop). In
normal operation no sink is registered, so enabled() is False and record() is a
cheap no-op: the agent path is unchanged. The bench harness registers a sink
(see bench/recorder.py) to capture per-event timing to a JSONL file.

Keeping this hook in offleash, with the implementation in bench/, preserves the
dependency direction: bench imports offleash, never the reverse.
"""

from __future__ import annotations

from typing import Any, Protocol


class MetricSink(Protocol):
    """Anything that can absorb a named metric event with arbitrary fields."""

    def record(self, event: str, fields: dict[str, Any]) -> None: ...


_sink: MetricSink | None = None


def set_sink(sink: MetricSink | None) -> None:
    """Install (or clear) the process-wide metric sink. Bench-only."""
    global _sink
    _sink = sink


def enabled() -> bool:
    """True when a sink is installed. Guard timestamp calls with this."""
    return _sink is not None


def record(event: str, /, **fields: Any) -> None:
    """Forward one event to the installed sink, or do nothing if there is none."""
    sink = _sink
    if sink is not None:
        sink.record(event, fields)
