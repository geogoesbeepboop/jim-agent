"""Delivery: HMAC-signed webhook payloads + channel parsing. Offline (no network:
the httpx client is faked, so we assert the exact bytes + signature we'd send)."""

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
    sign_payload,
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


async def test_webhook_posts_signed_body(monkeypatch):
    captured: dict = {}
    monkeypatch.setattr(notify.httpx, "AsyncClient", lambda *a, **k: _FakeClient(captured, 200))

    ch = WebhookChannel(url="https://hook.example/x", secret="shh")
    payload = build_payload(Monitor(id="m", product="fundamentals", identifier="AAPL"), _run(), [])
    ok = await ch.deliver(payload)

    assert ok is True
    assert captured["url"] == "https://hook.example/x"
    # The signature must verify against the exact bytes posted.
    sent = captured["content"]
    assert captured["headers"][notify.SIGNATURE_HEADER] == sign_payload(sent, "shh")
    assert json.loads(sent)["identifier"] == "AAPL"


async def test_webhook_reports_failure_on_5xx(monkeypatch):
    monkeypatch.setattr(notify.httpx, "AsyncClient", lambda *a, **k: _FakeClient({}, 500))
    ch = WebhookChannel(url="https://hook.example/x")
    assert (
        await ch.deliver(
            build_payload(Monitor(id="m", product="fundamentals", identifier="X"), _run(), [])
        )
        is False
    )


def test_build_channels_parses_specs():
    chans = build_channels(["console", "webhook:https://h/x", "store", "bogus"])
    assert [type(c) for c in chans] == [ConsoleChannel, WebhookChannel]
    assert chans[1].url == "https://h/x"
    # feed-only / unknown specs yield no external channel
    assert build_channels(["store"]) == []
