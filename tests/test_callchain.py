"""Cross-agent spend safety (Phase 7): call-chain codec, refusals, middleware.

All offline. The invariant: a payment loop (our address already in the inbound
chain) or an over-depth chain is refused with 409 *before* the paywall runs, so
no payment is verified or settled; and jim never extends an outbound chain past
the depth ceiling, so its own subcontracting is bounded.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from jim.config import Settings
from jim.interop.callchain import (
    CALL_CHAIN_HEADER,
    CallChainDepthExceeded,
    check_inbound,
    encode_chain,
    inbound_chain,
    outbound_payment_headers,
    parse_chain,
    reset_inbound_chain,
    set_inbound_chain,
)
from jim.seller.app import build_app
from jim.wallet import LocalWallet

OWN = "0xAbCd000000000000000000000000000000000001"
PEER_A = "0x1111111111111111111111111111111111111111"
PEER_B = "0x2222222222222222222222222222222222222222"


def test_chain_codec_roundtrip() -> None:
    assert parse_chain(None) == ()
    assert parse_chain("") == ()
    assert parse_chain(f" {PEER_A} , {PEER_B.upper()} ") == (PEER_A, PEER_B)
    assert encode_chain((PEER_A, PEER_B)) == f"{PEER_A},{PEER_B}"


def test_loop_is_refused_case_insensitively() -> None:
    verdict = check_inbound(f"{PEER_A},{OWN.upper()}", own_address=OWN, max_depth=4)
    assert not verdict.allowed
    assert "loop" in verdict.reason


def test_over_depth_is_refused() -> None:
    chain = ",".join([PEER_A, PEER_B, "0x3333333333333333333333333333333333333333"])
    verdict = check_inbound(chain, own_address=OWN, max_depth=3)
    assert not verdict.allowed
    assert "deep" in verdict.reason


def test_sane_chain_is_allowed() -> None:
    verdict = check_inbound(f"{PEER_A},{PEER_B}", own_address=OWN, max_depth=4)
    assert verdict.allowed
    assert verdict.hops == (PEER_A, PEER_B)


def test_outbound_headers_append_own_identity() -> None:
    headers = outbound_payment_headers(OWN, max_depth=4)
    assert headers[CALL_CHAIN_HEADER] == OWN.lower()


def test_outbound_headers_extend_the_inbound_chain() -> None:
    token = set_inbound_chain((PEER_A, PEER_B))
    try:
        headers = outbound_payment_headers(OWN, max_depth=4)
        assert headers[CALL_CHAIN_HEADER] == f"{PEER_A},{PEER_B},{OWN.lower()}"
    finally:
        reset_inbound_chain(token)
    assert inbound_chain() == ()


def test_outbound_depth_ceiling_refuses_to_buy() -> None:
    token = set_inbound_chain((PEER_A, PEER_B))
    try:
        with pytest.raises(CallChainDepthExceeded):
            outbound_payment_headers(OWN, max_depth=2)
    finally:
        reset_inbound_chain(token)


# --- middleware integration (the seller refuses before the paywall) ----------


def _client() -> tuple[TestClient, str]:
    wallet = LocalWallet.create()
    settings = Settings(evm_address=wallet.address, evm_private_key=wallet.private_key)
    return TestClient(build_app(settings)), wallet.address


def test_seller_refuses_payment_loops_with_409() -> None:
    client, own_address = _client()
    resp = client.get(
        "/ping", headers={CALL_CHAIN_HEADER: f"{PEER_A},{own_address}"}
    )
    assert resp.status_code == 409  # refused BEFORE any 402 challenge/payment
    assert "loop" in resp.json()["error"]


def test_seller_refuses_over_depth_chains_with_409() -> None:
    client, _ = _client()
    four_hops = ",".join(
        f"0x{i:040x}" for i in range(1, 5)
    )  # default CALL_CHAIN_MAX_DEPTH = 4
    resp = client.get("/ping", headers={CALL_CHAIN_HEADER: four_hops})
    assert resp.status_code == 409
    assert "deep" in resp.json()["error"]


def test_seller_still_paywalls_sane_chains() -> None:
    client, _ = _client()
    resp = client.get("/ping", headers={CALL_CHAIN_HEADER: f"{PEER_A},{PEER_B}"})
    assert resp.status_code == 402  # normal payment challenge, not a refusal


def test_chainless_requests_are_unaffected() -> None:
    client, _ = _client()
    assert client.get("/health").status_code == 200
    assert client.get("/ping").status_code == 402
