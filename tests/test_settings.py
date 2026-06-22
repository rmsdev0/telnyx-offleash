"""STT engine/model validation and the interim-results gate.

Covers the Safe-tier transcription_model plumbing: settings reject an STT_MODEL
the engine cannot use, the interim helper reflects each engine, and
Call.start_transcription builds the right transcription_start payload.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from offleash.settings import Settings, stt_emits_interim
from offleash.telnyx import Call


def _settings(**overrides: object) -> Settings:
    # _env_file=None so the developer's real .env never bleeds into the test.
    base = {"telnyx_api_key": "k", "telnyx_public_key": "p", "_env_file": None}
    return Settings(**{**base, **overrides})  # type: ignore[arg-type]


# ── engine/model validation ──────────────────────────────────────


def test_valid_engine_model_pairing_passes() -> None:
    s = _settings(stt_engine="Deepgram", stt_model="flux")
    assert s.stt_engine == "Deepgram"
    assert s.stt_model == "flux"


def test_blank_model_always_passes() -> None:
    assert _settings(stt_engine="Deepgram").stt_model == ""


def test_model_for_engine_without_model_selection_is_rejected() -> None:
    with pytest.raises(ValidationError, match="not valid for"):
        _settings(stt_engine="Google", stt_model="flux")


def test_wrong_model_for_engine_is_rejected() -> None:
    with pytest.raises(ValidationError, match="nova-2, nova-3"):
        _settings(stt_engine="Deepgram", stt_model="soniox/stt-rt-v4")


def test_unknown_engine_is_trusted() -> None:
    # A newer engine not yet in the map must not be blocked, model and all.
    s = _settings(stt_engine="BrandNew", stt_model="whatever")
    assert s.stt_engine == "BrandNew"


# ── Flux turn-detection tuning ───────────────────────────────────


def test_eot_threshold_in_range_passes() -> None:
    s = _settings(stt_engine="Deepgram", stt_model="flux", stt_eot_threshold=0.8)
    assert s.stt_eot_threshold == 0.8


def test_eot_threshold_out_of_range_is_rejected() -> None:
    with pytest.raises(ValidationError, match="STT_EOT_THRESHOLD"):
        _settings(stt_engine="Deepgram", stt_model="flux", stt_eot_threshold=0.3)


def test_eot_timeout_out_of_range_is_rejected() -> None:
    with pytest.raises(ValidationError, match="STT_EOT_TIMEOUT_MS"):
        _settings(stt_engine="Deepgram", stt_model="flux", stt_eot_timeout_ms=200)


# ── interim helper ───────────────────────────────────────────────


def test_stt_emits_interim_by_engine() -> None:
    assert stt_emits_interim("Google") is True
    assert stt_emits_interim("Deepgram") is True
    assert stt_emits_interim("Telnyx") is False
    assert stt_emits_interim("Azure") is False
    # Unknown engine: assume interim so barge-in is not silently degraded.
    assert stt_emits_interim("BrandNew") is True


# ── start_transcription payload ──────────────────────────────────


class _CapturingClient:
    """Minimal stand-in for TelnyxClient that records action payloads."""

    def __init__(self, settings: Settings) -> None:
        self.settings = settings
        self.calls: list[tuple[str, dict]] = []

    async def action(
        self, call_control_id: str, action: str, payload: dict | None = None
    ) -> dict:
        self.calls.append((action, payload or {}))
        return {}


async def test_payload_includes_model_and_interim_for_deepgram() -> None:
    client = _CapturingClient(_settings(stt_engine="Deepgram", stt_model="flux"))
    await Call(client, "cc-1").start_transcription()  # type: ignore[arg-type]
    action, payload = client.calls[0]
    assert action == "transcription_start"
    assert payload["transcription_engine"] == "Deepgram"
    assert payload["transcription_model"] == "flux"
    assert payload["interim_results"] is True


async def test_payload_gates_interim_off_for_non_interim_engine() -> None:
    client = _CapturingClient(
        _settings(stt_engine="Telnyx", stt_model="openai/whisper-large-v3-turbo")
    )
    await Call(client, "cc-1").start_transcription()  # type: ignore[arg-type]
    _, payload = client.calls[0]
    assert payload["transcription_model"] == "openai/whisper-large-v3-turbo"
    assert "interim_results" not in payload


async def test_flux_eot_params_included_only_for_flux() -> None:
    client = _CapturingClient(
        _settings(
            stt_engine="Deepgram",
            stt_model="flux",
            stt_eot_threshold=0.8,
            stt_eot_timeout_ms=4000,
        )
    )
    await Call(client, "cc-1").start_transcription()  # type: ignore[arg-type]
    _, payload = client.calls[0]
    assert payload["eot_threshold"] == 0.8
    assert payload["eot_timeout_ms"] == 4000


async def test_flux_eot_params_not_sent_to_other_engine_or_model() -> None:
    # Set on a non-flux config: the Flux-only params must not leak into the
    # payload (they would 422 nova-3 / Google).
    client = _CapturingClient(
        _settings(stt_engine="Deepgram", stt_model="nova-3", stt_eot_threshold=0.8)
    )
    await Call(client, "cc-1").start_transcription()  # type: ignore[arg-type]
    _, payload = client.calls[0]
    assert "eot_threshold" not in payload


async def test_default_google_payload_is_unchanged() -> None:
    # The shipped default must still request interim and send no model.
    client = _CapturingClient(_settings())
    await Call(client, "cc-1").start_transcription()  # type: ignore[arg-type]
    _, payload = client.calls[0]
    assert payload["transcription_engine"] == "Google"
    assert payload["interim_results"] is True
    assert "transcription_model" not in payload
