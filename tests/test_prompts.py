"""System-prompt composition tests."""

from __future__ import annotations

from datetime import UTC, datetime

from offleash.conversation import CallContext
from offleash.prompts import RESTAURANT_CONFIG, build_system_prompt


def test_prompt_includes_todays_date_for_relative_dates() -> None:
    # The model needs a date anchor to resolve "tomorrow"; the prompt must carry
    # the current year/month/day, not just the time.
    prompt = build_system_prompt(
        RESTAURANT_CONFIG, CallContext(), current_node="booking"
    )
    now = datetime.now(tz=UTC)
    assert str(now.year) in prompt
    assert now.strftime("%B") in prompt  # month name, e.g. "June"


def test_prompt_without_context_omits_date() -> None:
    # No call context (e.g. a degenerate call) should not crash or fabricate one.
    prompt = build_system_prompt(RESTAURANT_CONFIG)
    assert "Current date and time" not in prompt
