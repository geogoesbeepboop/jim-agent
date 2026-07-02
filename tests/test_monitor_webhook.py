"""Delivery: HMAC-signed webhook payloads + channel parsing + replay protection.
Offline (no network: the httpx client is faked, so we assert the exact bytes,
headers, and signature we'd send; verify_delivery takes an injected clock)."""

from __future__ import annotations

import hashlib
import hmac
import json

from jim.monitors import notify
from jim.monitors.models import Monitor, MonitorRun, Signal
from jim.monitors.notify import (
    ConsoleChannel,
    WebhookChannel,
    build_channels,
    build_payload,
    sign_delivery,
    sign_payload,
    verify_delivery,
)


def _run():
    return MonitorRun(
        monitor_id="fund-aapl-x",
        identifier="AAPL",
        product="fundamentals",
        status="material",
        material=True,
        severity="critical",
        signals=[
            Signal("price_move", "k", "Price", "critical", "Price rose to $120 [C1].", ["C1"])
        ],
        memo="Apple update. Price is now $120 [C1].",
        price_out_usd=0.10,
    )


def test_sign_payload_is_hmac_sha256_and_verifiable():
    body = b'{"a":1}'
    sig = sign_payload(body, "topsecret")
    expected = "sha256=" + hmac.new(b"topsecret", body, hashlib.sha256).hexdigest()
    assert sig == expected
    assert sign_payload(body, None) is None


def test_build_payload_is_impersonal_and_cited():
    monitor = Monitor(id="fund-aapl-x", product="fundamentals", identifier="AAPL")
    payload = build_payload(monitor, _run(), ["[C1] Price: Yahoo"])
    assert payload["type"] == "jim.monitor.update"
    assert payload["identifier"] == "AAPL" and payload["severity"] == "critical"
    assert payload["citations"] == ["[C1] Price: Yahoo"]
    assert "disclaimer" in payload and "not personalized" in payload["disclaimer"]
    assert payload["signals"][0]["citations"] == ["C1"]


# --- replay protection: signature binds timestamp + nonce --------------------

_BODY = b'{"a":1}'
_TS = "1700000000"
_NONCE = "0123456789abcdef0123456789abcdef"


def _headers(secret="shh", body=_BODY, ts=_TS, nonce=_NONCE):
    return sign_delivery(body, secret=secret, timestamp=ts, nonce=nonce), ts, nonce


def test_sign_delivery_binds_timestamp_and_nonce():
    sig, ts, nonce = _headers()
    message = f"{ts}.{nonce}.".encode() + _BODY
    expected = hmac.new(b"shh", message, hashlib.sha256).hexdigest()
    assert sig == f"sha256={expected}"
    # Same body, different timestamp or nonce → different signature.
    assert sig != sign_delivery(_BODY, secret="shh", timestamp="1700000001", nonce=nonce)
    assert sig != sign_delivery(_BODY, secret="shh", timestamp=ts, nonce="f" * 32)


def test_verify_delivery_happy_path():
    sig, ts, nonce = _headers()
    ok, reason = verify_delivery(
        _BODY, secret="shh", signature=sig, timestamp=ts, nonce=nonce, now=1700000010.0
    )
    assert (ok, reason) == (True, "ok")


def test_verify_delivery_rejects_tampering():
    sig, ts, nonce = _headers()
    now = 1700000000.0
    # Tampered body.
    ok, reason = verify_delivery(
        b'{"a":2}', secret="shh", signature=sig, timestamp=ts, nonce=nonce, now=now
    )
    assert not ok and reason == "signature mismatch"
    # Tampered timestamp (an attacker "freshening" a captured delivery).
    ok, reason = verify_delivery(
        _BODY, secret="shh", signature=sig, timestamp="1700009999", nonce=nonce, now=now
    )
    assert not ok and reason == "signature mismatch"
    # Tampered nonce.
    ok, reason = verify_delivery(
        _BODY, secret="shh", signature=sig, timestamp=ts, nonce="f" * 32, now=now
    )
    assert not ok and reason == "signature mismatch"
    # Missing headers.
    for missing in ("signature", "timestamp", "nonce"):
        kwargs = {"signature": sig, "timestamp": ts, "nonce": nonce, missing: None}
        ok, reason = verify_delivery(_BODY, secret="shh", now=now, **kwargs)
        assert not ok and "missing" in reason


def test_verify_delivery_rejects_stale_timestamp():
    sig, ts, nonce = _headers()
    # 301s past a 300s tolerance → outside the replay window (either direction).
    ok, reason = verify_delivery(
        _BODY, secret="shh", signature=sig, timestamp=ts, nonce=nonce, now=1700000301.0
    )
    assert not ok and "replay window" in reason
    ok, _ = verify_delivery(
        _BODY, secret="shh", signature=sig, timestamp=ts, nonce=nonce, now=1699999699.0
    )
    assert not ok


def test_verify_delivery_rejects_replayed_nonce():
    sig, ts, nonce = _headers()
    seen: set[str] = set()
    now = 1700000000.0
    first = verify_delivery(
        _BODY, secret="shh", signature=sig, timestamp=ts, nonce=nonce, seen_nonces=seen, now=now
    )
    assert first == (True, "ok") and nonce in seen
    replay = verify_delivery(
        _BODY, secret="shh", signature=sig, timestamp=ts, nonce=nonce, seen_nonces=seen, now=now
    )
    assert replay[0] is False and "replay" in replay[1]


# --- the channel itself -------------------------------------------------------


class _Resp:
    def __init__(self, status):
        self.status_code = status


class _FakeClient:
    def __init__(self, captured, status):
        self.captured, self.status = captured, status

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, content=None, headers=None):
        self.captured.update(url=url, content=content, headers=headers)
        return _Resp(self.status)


async def test_webhook_posts_signed_body_with_replay_headers(monkeypatch):
    captured: dict = {}
    monkeypatch.setattr(notify.httpx, "AsyncClient", lambda *a, **k: _FakeClient(captured, 200))

    ch = WebhookChannel(url="https://hook.example/x", secret="shh")
    payload = build_payload(Monitor(id="m", product="fundamentals", identifier="AAPL"), _run(), [])
    ok = await ch.deliver(payload)

    assert ok is True
    assert captured["url"] == "https://hook.example/x"
    sent, headers = captured["content"], captured["headers"]
    # All three headers ship; timestamp is unix seconds, nonce is a uuid4 hex.
    ts, nonce = headers[notify.TIMESTAMP_HEADER], headers[notify.NONCE_HEADER]
    assert ts.isdigit() and len(nonce) == 32
    # The signature must verify against the exact bytes + headers posted.
    sig = headers[notify.SIGNATURE_HEADER]
    assert sig == sign_delivery(sent, secret="shh", timestamp=ts, nonce=nonce)
    assert verify_delivery(
        sent, secret="shh", signature=sig, timestamp=ts, nonce=nonce, now=float(ts)
    ) == (True, "ok")
    assert json.loads(sent)["identifier"] == "AAPL"


async def test_webhook_reports_failure_on_5xx(monkeypatch):
    captured: dict = {}
    monkeypatch.setattr(notify.httpx, "AsyncClient", lambda *a, **k: _FakeClient(captured, 500))
    ch = WebhookChannel(url="https://hook.example/x")
    assert (
        await ch.deliver(
            build_payload(Monitor(id="m", product="fundamentals", identifier="X"), _run(), [])
        )
        is False
    )
    # Unsigned (no secret) deliveries still carry timestamp + nonce, no signature.
    assert notify.TIMESTAMP_HEADER in captured["headers"]
    assert notify.NONCE_HEADER in captured["headers"]
    assert notify.SIGNATURE_HEADER not in captured["headers"]


def test_build_channels_parses_specs():
    chans = build_channels(["console", "webhook:https://h/x", "store", "bogus"])
    assert [type(c) for c in chans] == [ConsoleChannel, WebhookChannel]
    assert chans[1].url == "https://h/x"
    # feed-only / unknown specs yield no external channel
    assert build_channels(["store"]) == []
