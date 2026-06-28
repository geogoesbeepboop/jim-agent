"""Phase 0 paywall behavior — runs offline, no funded wallet or network needed.

These assert the *protocol surface*: free routes stay free, paid routes issue a
402 challenge advertising how to pay. The actual on-chain settlement is proven
by `scripts/ping_demo.py` against a running server + funded testnet wallet.
"""

from __future__ import annotations

import base64
import json

from fastapi.testclient import TestClient

from jim.config import Settings
from jim.seller.app import build_app
from jim.wallet import LocalWallet


def _decode_challenge(resp) -> dict:
    """V2 advertises payment requirements in the base64 `payment-required` header."""
    return json.loads(base64.b64decode(resp.headers["payment-required"]))


def _client() -> TestClient:
    wallet = LocalWallet.create()
    settings = Settings(evm_address=wallet.address, evm_private_key=wallet.private_key)
    # raise_server_exceptions stays default; we only hit middleware + handlers.
    return TestClient(build_app(settings))


def test_health_is_free() -> None:
    resp = _client().get("/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


def test_ping_requires_payment() -> None:
    resp = _client().get("/ping")
    assert resp.status_code == 402


def test_402_advertises_payment_requirements() -> None:
    resp = _client().get("/ping")
    challenge = _decode_challenge(resp)
    assert challenge["x402Version"] == 2
    accepts = challenge["accepts"]
    assert isinstance(accepts, list) and accepts
    first = accepts[0]
    assert first["scheme"] == "exact"
    assert first["network"] == "eip155:84532"
    # $0.01 USDC at 6 decimals == "10000" base units.
    assert first["amount"] == "10000"
    assert first["extra"]["name"] == "USDC"


def test_missing_evm_address_is_a_clear_error() -> None:
    import pytest

    settings = Settings(evm_address=None)
    with pytest.raises(ValueError, match="EVM_ADDRESS"):
        build_app(settings)
