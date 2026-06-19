"""Tests for Telnyx Ed25519 webhook signature verification."""

from __future__ import annotations

import base64
import time

from nacl.signing import SigningKey

from offleash.telnyx import decode_client_state, encode_client_state, verify_webhook


def _sign(signing_key: SigningKey, timestamp: str, body: bytes) -> str:
    signed = f"{timestamp}|".encode() + body
    return base64.b64encode(signing_key.sign(signed).signature).decode()


def _public_key_b64(signing_key: SigningKey) -> str:
    return base64.b64encode(bytes(signing_key.verify_key)).decode()


def test_valid_signature_passes() -> None:
    sk = SigningKey.generate()
    body = b'{"data": {"event_type": "call.answered"}}'
    ts = str(int(time.time()))
    headers = {
        "telnyx-signature-ed25519": _sign(sk, ts, body),
        "telnyx-timestamp": ts,
    }
    assert verify_webhook(_public_key_b64(sk), headers, body) is True


def test_tampered_body_fails() -> None:
    sk = SigningKey.generate()
    body = b'{"data": {"event_type": "call.answered"}}'
    ts = str(int(time.time()))
    headers = {
        "telnyx-signature-ed25519": _sign(sk, ts, body),
        "telnyx-timestamp": ts,
    }
    tampered = b'{"data": {"event_type": "call.hangup"}}'
    assert verify_webhook(_public_key_b64(sk), headers, tampered) is False


def test_missing_headers_fail() -> None:
    sk = SigningKey.generate()
    body = b"{}"
    assert verify_webhook(_public_key_b64(sk), {}, body) is False


def test_stale_timestamp_fails() -> None:
    sk = SigningKey.generate()
    body = b"{}"
    old_ts = str(int(time.time()) - 10_000)
    headers = {
        "telnyx-signature-ed25519": _sign(sk, old_ts, body),
        "telnyx-timestamp": old_ts,
    }
    assert verify_webhook(_public_key_b64(sk), headers, body, tolerance_s=300) is False


def test_wrong_key_fails() -> None:
    sk = SigningKey.generate()
    other = SigningKey.generate()
    body = b"{}"
    ts = str(int(time.time()))
    headers = {
        "telnyx-signature-ed25519": _sign(sk, ts, body),
        "telnyx-timestamp": ts,
    }
    assert verify_webhook(_public_key_b64(other), headers, body) is False


def test_client_state_round_trip() -> None:
    assert decode_client_state(encode_client_state("speak:7")) == "speak:7"
    # Garbage decodes to empty rather than raising.
    assert decode_client_state("!!!not base64!!!") == ""
