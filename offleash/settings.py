"""Flat, env-driven settings.

One Telnyx API key authenticates every layer: Call Control, transcription,
speak, and the LLM inference endpoint. There is no provider selection and no
config framework, just the values this single-vendor agent needs.
"""

from __future__ import annotations

from dataclasses import dataclass
from functools import lru_cache

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


@dataclass(frozen=True)
class SttEngine:
    """What a transcription engine accepts and whether it emits interims.

    `models` is the set of valid transcription_model values for the engine; an
    empty set means the engine has no model selection (leave STT_MODEL blank).
    `interim` is whether the engine emits interim transcripts, which barge-in
    relies on. Sourced from the Telnyx transcription_start docs.
    """

    models: frozenset[str]
    interim: bool


# The transcription_start engine/model matrix this agent knows about. Used to
# validate STT_ENGINE/STT_MODEL at startup and to decide whether to request
# interim results. Not exhaustive of Telnyx's catalog: unknown engines are
# trusted (see _check_stt and stt_emits_interim) so a newer engine is not
# blocked by a stale map.
STT_ENGINES: dict[str, SttEngine] = {
    "Google": SttEngine(frozenset(), interim=True),
    "Telnyx": SttEngine(
        frozenset({"openai/whisper-large-v3-turbo", "openai/whisper-tiny"}),
        interim=False,
    ),
    "Deepgram": SttEngine(frozenset({"nova-2", "nova-3", "flux"}), interim=True),
    "AssemblyAI": SttEngine(
        frozenset({"assemblyai/universal-streaming"}), interim=True
    ),
    "Soniox": SttEngine(frozenset({"soniox/stt-rt-v4"}), interim=True),
    "Speechmatics": SttEngine(frozenset({"speechmatics/standard"}), interim=True),
    "xAI": SttEngine(frozenset({"xai/grok-stt"}), interim=True),
    "Azure": SttEngine(frozenset(), interim=False),
}


def stt_emits_interim(engine: str) -> bool:
    """Whether `engine` emits interim transcripts (barge-in depends on them).

    Unknown engines are assumed to support interims so a forward-looking config
    is not silently degraded to final-transcript-only barge-in.
    """
    spec = STT_ENGINES.get(engine)
    return True if spec is None else spec.interim


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_file_encoding="utf-8", extra="ignore"
    )

    # Telnyx auth (one key covers Call Control and Inference)
    telnyx_api_key: str
    telnyx_public_key: str

    # Telephony identity
    telnyx_phone_number: str = ""  # E.164, the "from" number for outbound
    telnyx_connection_id: str = ""  # Voice API Application id (connection_id)

    # Telnyx API
    telnyx_api_base: str = "https://api.telnyx.com/v2"

    # LLM inference (OpenAI-compatible). enable_thinking is disabled for
    # real-time voice latency in the LLM wrapper.
    llm_base_url: str = "https://api.telnyx.com/v2/ai"
    llm_model: str = "moonshotai/Kimi-K2.5"
    llm_temperature: float = 0.7
    llm_max_tokens: int = 200

    # STT (transcription_start). "Google" is the default for its interim
    # results, which let barge-in fire on the first partial transcript. The
    # transcription language enum wants a short code ("en"); it rejects "en-US",
    # which the speak/TTS language below does accept. stt_model picks a specific
    # model within engines that host several (e.g. Deepgram nova-3 / flux); blank
    # uses the engine's default. The engine/model pairing is validated below, and
    # the interim request is gated on engine support in Call.start_transcription.
    stt_engine: str = "Google"
    stt_language: str = "en"
    stt_model: str = ""
    # Deepgram Flux turn-detection tuning (applied only when STT_ENGINE=Deepgram
    # and STT_MODEL=flux). A higher eot_threshold makes Flux wait for a clearer
    # end-of-turn before finalizing, so one utterance is less likely to be split
    # into several finals. Unset (None) leaves Flux's defaults (0.7 / 5000 ms).
    stt_eot_threshold: float | None = None
    stt_eot_timeout_ms: int | None = None

    # TTS (speak). NaturalHD is the higher-fidelity Telnyx-native tier: it
    # handles disfluencies ("um", "uh") and laughter and is multilingual, while
    # staying on a low-latency Telnyx voice path. Telnyx.Natural.* is the
    # lowest-latency English-only fallback; Telnyx.Ultra.* (UUID-named, most
    # expressive) and third-party ElevenLabs / AWS.Polly.*-Neural / Azure.*Neural
    # voices also drop in here. The speak command accepts the full locale ("en-US").
    tts_voice: str = "Telnyx.NaturalHD.astra"
    tts_language: str = "en-US"

    # Spend and volume protection (0 disables)
    max_concurrent_calls: int = 10
    max_tokens_per_call: int = 0

    # LLM stream retry
    stream_max_retries: int = 2
    stream_retry_backoff_s: float = 0.25

    # Webhook signature freshness window (seconds)
    webhook_tolerance_s: int = 300

    # Server
    host: str = "0.0.0.0"
    port: int = 8000
    env: str = "development"
    log_level: str = "info"

    @model_validator(mode="after")
    def _check_stt(self) -> Settings:
        """Validate STT engine/model pairing and Flux tuning ranges.

        Caught at startup rather than as a 422 on the first call, which would
        leave that call with no transcription at all. Unknown engines are
        trusted, since Telnyx may add engines faster than STT_ENGINES tracks.
        """
        if self.stt_eot_threshold is not None and not (
            0.5 <= self.stt_eot_threshold <= 0.9
        ):
            raise ValueError("STT_EOT_THRESHOLD must be between 0.5 and 0.9")
        if self.stt_eot_timeout_ms is not None and not (
            500 <= self.stt_eot_timeout_ms <= 10000
        ):
            raise ValueError("STT_EOT_TIMEOUT_MS must be between 500 and 10000")

        spec = STT_ENGINES.get(self.stt_engine)
        if spec is not None and self.stt_model and self.stt_model not in spec.models:
            allowed = ", ".join(sorted(spec.models)) or "(none — leave STT_MODEL blank)"
            raise ValueError(
                f"STT_MODEL={self.stt_model!r} is not valid for "
                f"STT_ENGINE={self.stt_engine!r}; allowed: {allowed}"
            )
        return self

    @property
    def is_production(self) -> bool:
        return self.env.lower() == "production"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Load settings once per process from the environment and .env file."""
    return Settings()  # type: ignore[call-arg]
