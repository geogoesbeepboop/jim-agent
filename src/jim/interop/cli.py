"""``jim-identity`` — jim's onchain identity + attestation surface (Horizon 1).

Four subcommands, split by whether money/state moves:

  card       the ERC-8004-shaped identity payload (offline, derived from config)
  register   prints EXACTLY what an ERC-8004 identity registration needs and
             how to send it — but never sends. Registering is a real onchain
             transaction from the operator's wallet; jim prepares, you execute.
  attest     run one research query locally and sign a gate-verdict receipt
  verify     verify a signed receipt offline (any EVM stack can do the same)

The guarded design is deliberate: everywhere else jim moves money it does so
under deterministic caps (budget, price cap, call chain). An identity
registration binds the operator's address to a public agent identity — that's
an *operator* decision, so the tool stops at the exact payload + instructions.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys

from jim.config import get_settings
from jim.interop.attest import attest_result, verify_attestation
from urllib.parse import urlparse

EIP_8004_URL = "https://eips.ethereum.org/EIPS/eip-8004"


def identity_card() -> dict:
    """The ERC-8004-shaped identity payload, derived from live config."""
    s = get_settings()
    domain = urlparse(s.public_url).netloc or s.public_url
    return {
        "agent_domain": domain,
        "agent_address": s.evm_address,
        "agent_card_url": f"{s.public_url}/.well-known/agent-card.json",
        "discovery_url": f"{s.public_url}/.well-known/x402",
        "network": s.network,
        "note": (
            "ERC-8004 'Trustless Agents' registers (agent_domain, agent_address) onchain "
            "and resolves the domain to the agent card jim already serves. jim's trust "
            "ledger complements the ERC-8004 Reputation Registry: scores here are computed "
            "from sourcing-gate outcomes, not submitted feedback."
        ),
    }


def _cmd_card() -> int:
    print(json.dumps(identity_card(), indent=2))
    return 0


def _cmd_register() -> int:
    s = get_settings()
    card = identity_card()
    registry = s.erc8004_identity_registry
    print("ERC-8004 identity registration — prepared, NOT sent")
    print("=" * 60)
    print(json.dumps(card, indent=2))
    print("=" * 60)
    if not card["agent_address"]:
        print("✗ EVM_ADDRESS is not set — run `uv run jim-wallet new` first.")
        return 1
    if registry:
        print(f"Configured registry (ERC8004_IDENTITY_REGISTRY): {registry}")
    else:
        print("No ERC8004_IDENTITY_REGISTRY configured.")
    print(
        "\nThis tool never sends the transaction — registering binds YOUR address to a\n"
        "public agent identity, so the operator executes it. To register:\n"
        f"  1. Verify the canonical Identity Registry address for {card['network']} "
        f"against the spec:\n     {EIP_8004_URL}\n"
        "  2. Set ERC8004_IDENTITY_REGISTRY to that address (never trust a pasted one).\n"
        "  3. From the wallet that owns EVM_ADDRESS, call the registry's registration\n"
        "     function with the agent_domain above (e.g. via cast, Etherscan, or your\n"
        "     wallet), and confirm the resulting agent id.\n"
        "  4. Serve /.well-known/agent-card.json at that domain (jim already does).\n"
    )
    return 0


def _cmd_attest(identifier: str, product: str, mode: str, high_stakes: bool) -> int:
    from jim.research.engine import run_research

    s = get_settings()
    if not s.evm_private_key:
        print("✗ EVM_PRIVATE_KEY is not set — the receipt is signed with jim's key.")
        return 1
    result = asyncio.run(
        run_research(identifier, product=product, mode=mode, high_stakes=high_stakes)
    )
    if result.status != "ok":
        print(f"✗ run finished {result.status!r} — jim does not sign receipts for research")
        print("  it refused to ship. (This refusal is the product working as designed.)")
        return 1
    signed = attest_result(
        result, private_key=s.evm_private_key, network=s.network, service=s.service_name
    )
    print(json.dumps(signed, indent=2))
    return 0


def _cmd_verify(path: str) -> int:
    raw = sys.stdin.read() if path == "-" else open(path, encoding="utf-8").read()
    ok, reason = verify_attestation(json.loads(raw))
    print(("✓ " if ok else "✗ ") + reason)
    return 0 if ok else 1


def main() -> int:
    p = argparse.ArgumentParser(
        prog="jim-identity",
        description="jim's onchain identity + signed gate-verdict receipts.",
    )
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("card", help="the ERC-8004-shaped identity payload (offline)")
    sub.add_parser("register", help="print the registration payload + steps (never sends)")
    a = sub.add_parser("attest", help="run research locally and sign a gate-verdict receipt")
    a.add_argument("identifier")
    a.add_argument("--product", default="fundamentals")
    a.add_argument("--mode", choices=["human", "agent"], default="agent")
    a.add_argument("--high-stakes", action="store_true")
    v = sub.add_parser("verify", help="verify a signed receipt (file path or '-' for stdin)")
    v.add_argument("path")
    args = p.parse_args()

    if args.cmd == "card":
        return _cmd_card()
    if args.cmd == "register":
        return _cmd_register()
    if args.cmd == "attest":
        return _cmd_attest(args.identifier, args.product, args.mode, args.high_stakes)
    return _cmd_verify(args.path)


if __name__ == "__main__":
    raise SystemExit(main())
