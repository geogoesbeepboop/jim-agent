"""Signed gate-verdict attestations — research with a receipt (Horizon 1).

The 2026 trust stack verifies who an agent is (ERC-8004 identity, KYA
frameworks) and whether it may spend (mandates, session caps). Nothing
verifies what it *delivered*. This module is jim's answer: a compact, signed
statement binding

    memo hash → snapshot fingerprint → gate verdict → settlement tx

signed with jim's own EVM key (EIP-191 ``personal_sign``), so any EVM tooling
— another agent, a risk committee, an ERC-8004 validation hook — can verify
the receipt offline without trusting jim's server. The attestation says
exactly what the gate proved and no more: *this memo, written from this data,
passed deterministic sourcing verification, and here is the payment it
settled against.*

Offchain by design for now: the payload is canonical JSON, so anchoring it
later (EAS on Base) is an add-on, not a rewrite. See ``jim-identity`` for the
CLI surface and docs/NORTH_STAR.md for where this is headed.
"""

from __future__ import annotations

import hashlib
import json

from eth_account import Account
from eth_account.messages import encode_defunct

ATTESTATION_SCHEMA = "jim.gate.verdict/1"


def canonical_bytes(payload: dict) -> bytes:
    """The byte-stable form that gets signed: sorted keys, compact separators."""
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


def build_attestation(
    *,
    service: str,
    product: str,
    identifier: str,
    network: str,
    memo: str,
    snapshot_fingerprint: str,
    gate_passed: bool,
    figures_checked: int,
    figures_covered: int,
    settlement_tx: str | None = None,
    issued_at: str | None = None,
) -> dict:
    """The unsigned attestation payload — everything the verdict binds together."""
    return {
        "schema": ATTESTATION_SCHEMA,
        "service": service,
        "product": product,
        "identifier": identifier,
        "network": network,
        "memo_sha256": hashlib.sha256(memo.encode("utf-8")).hexdigest(),
        "snapshot_fingerprint": snapshot_fingerprint,
        "gate": {
            "passed": gate_passed,
            "figures_checked": figures_checked,
            "figures_covered": figures_covered,
        },
        "settlement_tx": settlement_tx,
        "issued_at": issued_at,
    }


def sign_attestation(payload: dict, private_key: str) -> dict:
    """Sign the canonical payload with jim's key (EIP-191 personal_sign)."""
    account = Account.from_key(private_key)
    signed = Account.sign_message(encode_defunct(canonical_bytes(payload)), private_key)
    signature = signed.signature.hex()
    if not signature.startswith("0x"):
        signature = f"0x{signature}"
    return {"attestation": payload, "signer": account.address, "signature": signature}


def verify_attestation(signed: dict) -> tuple[bool, str]:
    """Verify a signed attestation offline: recover the signer, compare, done.

    Returns ``(ok, reason)``. Any EVM stack can do the same recovery — nothing
    here depends on jim's code or server being reachable.
    """
    payload = signed.get("attestation")
    claimed = signed.get("signer")
    signature = signed.get("signature")
    if not isinstance(payload, dict) or not claimed or not signature:
        return False, "malformed: needs attestation, signer, signature"
    if payload.get("schema") != ATTESTATION_SCHEMA:
        return False, f"unknown schema {payload.get('schema')!r}"
    try:
        recovered = Account.recover_message(
            encode_defunct(canonical_bytes(payload)), signature=signature
        )
    except Exception as e:
        return False, f"signature recovery failed: {e}"
    if recovered.lower() != str(claimed).lower():
        return False, f"signer mismatch: recovered {recovered}, claimed {claimed}"
    return True, f"verified: signed by {recovered}"


def attest_result(result, *, private_key: str, network: str, service: str) -> dict:
    """Convenience: build + sign an attestation from a completed ResearchResult.

    Only verified output gets a receipt — attesting a rejected run would be
    signing something jim refused to ship.
    """
    if result.status != "ok" or not result.memo or result.snapshot is None:
        raise ValueError(
            f"only status='ok' runs are attestable (got {result.status!r}) — "
            "jim does not sign receipts for research it refused to ship"
        )
    gate = result.gate
    payload = build_attestation(
        service=service,
        product=result.product,
        identifier=result.ticker,
        network=network,
        memo=result.memo,
        snapshot_fingerprint=result.snapshot.fingerprint(),
        gate_passed=bool(gate and gate.passed),
        figures_checked=gate.n_figures if gate else 0,
        figures_covered=gate.n_covered if gate else 0,
    )
    return sign_attestation(payload, private_key)
