"""Flat, env-driven settings.

One Telnyx API key authenticates every layer: Call Control, transcription,
speak, and the LLM inference endpoint. There is no provider selection and no
config framework, just the values this single-vendor agent needs.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


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

    # STT (transcription_start). Engine A (Google) is the default for its
    # interim results, which let barge-in fire on the first partial transcript.
    # Note the transcription language enum wants a short code ("en"); it rejects
    # "en-US", which the speak/TTS language below does accept.
    stt_engine: str = "Google"
    stt_language: str = "en"

    # TTS (speak)
    tts_voice: str = "Telnyx.Natural.abbie"
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

    @property
    def is_production(self) -> bool:
        return self.env.lower() == "production"


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Load settings once per process from the environment and .env file."""
    return Settings()  # type: ignore[call-arg]
