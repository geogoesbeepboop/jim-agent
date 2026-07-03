"""Signed gate-verdict receipts + the jim-identity surface (Horizon 1).

Offline proofs that the receipt does what it claims: binds memo hash →
fingerprint → verdict → settlement, signs with jim's key, verifies by pure
EVM signature recovery (no jim code or server needed on the verify side),
and refuses to attest anything the gates refused to ship. Plus the guarded
identity surface: the ERC-8004 payload derives from config, and the lying
mock peer produces exactly the unusable payload the trust loop punishes.
"""

from __future__ import annotations

import pytest

from jim.interop.attest import (
    ATTESTATION_SCHEMA,
    attest_result,
    build_attestation,
    canonical_bytes,
    sign_attestation,
    verify_attestation,
)
from jim.interop.cli import identity_card
from jim.research.engine import ResearchResult
from jim.research.facts import USD, Fact, Snapshot
from jim.research.gate import GateResult
from jim.wallet import LocalWallet

WALLET = LocalWallet.create()


def _payload(**overrides) -> dict:
    base = dict(
        service="jim",
        product="fundamentals",
        identifier="AAPL",
        network="eip155:8453",
        memo="Revenue was $1.00 billion [C1].",
        snapshot_fingerprint="abcd1234abcd1234",
        gate_passed=True,
        figures_checked=1,
        figures_covered=1,
        settlement_tx="0xfeed",
        issued_at="2026-07-02T00:00:00Z",
    )
    base.update(overrides)
    return build_attestation(**base)


def test_attestation_binds_the_memo_hash() -> None:
    a = _payload()
    b = _payload(memo="Revenue was $2.00 billion [C1].")
    assert a["schema"] == ATTESTATION_SCHEMA
    assert a["memo_sha256"] != b["memo_sha256"]
    assert a["gate"] == {"passed": True, "figures_checked": 1, "figures_covered": 1}
    assert a["settlement_tx"] == "0xfeed"


def test_canonicalization_is_byte_stable() -> None:
    assert canonical_bytes(_payload()) == canonical_bytes(_payload())


def test_sign_and_verify_roundtrip() -> None:
    signed = sign_attestation(_payload(), WALLET.private_key)
    assert signed["signer"] == WALLET.address
    ok, reason = verify_attestation(signed)
    assert ok, reason
    assert WALLET.address in reason


def test_tampering_breaks_verification() -> None:
    signed = sign_attestation(_payload(), WALLET.private_key)
    tampered = {**signed, "attestation": {**signed["attestation"], "identifier": "EVIL"}}
    ok, reason = verify_attestation(tampered)
    assert not ok and "mismatch" in reason

    wrong_signer = {**signed, "signer": "0x" + "0" * 40}
    ok, _ = verify_attestation(wrong_signer)
    assert not ok

    ok, reason = verify_attestation({"attestation": {}, "signer": None, "signature": None})
    assert not ok and "malformed" in reason


def test_only_verified_runs_are_attestable() -> None:
    snap = Snapshot(
        ticker="AAPL",
        cik="1",
        entity_name="Apple",
        facts=[Fact(id="C1", label="Revenue", value=1e9, unit=USD)],
    )
    ok_run = ResearchResult(
        ticker="AAPL",
        mode="agent",
        status="ok",
        product="fundamentals",
        memo="Revenue was $1.00 billion [C1].",
        snapshot=snap,
        gate=GateResult(passed=True, n_figures=1, n_covered=1),
    )
    signed = attest_result(
        ok_run, private_key=WALLET.private_key, network="eip155:8453", service="jim"
    )
    assert verify_attestation(signed)[0]
    assert signed["attestation"]["snapshot_fingerprint"] == snap.fingerprint()

    rejected = ResearchResult(ticker="AAPL", mode="agent", status="rejected")
    with pytest.raises(ValueError, match="refused to ship"):
        attest_result(
            rejected, private_key=WALLET.private_key, network="eip155:8453", service="jim"
        )


# --- the identity surface -----------------------------------------------------


def test_identity_card_derives_from_config() -> None:
    card = identity_card()
    assert card["agent_card_url"].endswith("/.well-known/agent-card.json")
    assert card["discovery_url"].endswith("/.well-known/x402")
    assert card["agent_domain"]  # host derived from public_url
    assert "gate" in card["note"]  # states the outcome-based trust contrast


# --- the lying mock peer (the demo's trust-decay beat) ------------------------


def test_corrupt_mock_peer_is_refused_and_debited() -> None:
    from jim.sources.peer import ProcurementError, _facts_from_payload
    from jim.vendor.mock_peer import build_mock_peer_response

    honest = build_mock_peer_response("AAPL")
    assert len(_facts_from_payload(honest, "mock-sentiment")) == 3

    corrupt = build_mock_peer_response("AAPL", corrupt=True)
    with pytest.raises(ProcurementError, match="no usable facts"):
        _facts_from_payload(corrupt, "mock-sentiment")
