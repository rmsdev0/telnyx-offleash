"""Append-only JSONL recorder for raw per-event benchmark data.

One line per event, flushed immediately so a crash mid-run keeps everything
captured so far. The schema is intentionally flat and self-describing: each row
has an "event" name, a "trial" number, and whatever timing fields that event
carries. analyze.py reads these rows back; nothing here interprets them.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pathlib import Path
    from types import TracebackType


class Recorder:
    """Writes benchmark event rows to a JSONL file, one per line."""

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self._fh = path.open("w", encoding="utf-8")
        self._seq = 0

    def write(self, row: dict[str, Any]) -> None:
        """Append one event row, prefixed with a monotonic sequence number."""
        self._seq += 1
        out = {"seq": self._seq, **row}
        self._fh.write(json.dumps(out) + "\n")
        self._fh.flush()

    def close(self) -> None:
        if not self._fh.closed:
            self._fh.close()

    def __enter__(self) -> Recorder:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()
