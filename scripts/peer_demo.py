"""Phase 7 exit run: jim subcontracts a peer agent over x402, end-to-end.

Run the seller with the mock peer composed into fundamentals (one process plays
both jim and the peer, exactly like the mock-Graph two-sided demo):

    PEER_SOURCES='[{"name":"mock-sentiment",
                    "url":"http://localhost:4021/mock-peer/research",
                    "price_estimate_usd":0.01,
                    "products":["fundamentals"]}]' uv run jim-seller

Then (funded testnet EVM_PRIVATE_KEY in .env):

    uv run python scripts/peer_demo.py AAPL

What a ✅ proves, in one paid call: jim bought the peer's cited facts over a
real x402 settlement (budget-capped, price-capped, call-chain stamped), merged
them into the EDGAR snapshot, the sourcing gate verified the peer's figures
like any other, and the trust ledger recorded the outcome — visible afterwards
in `uv run jim-dashboard`.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys

from jim.buyer import fetch_paid
from jim.config import get_settings
from jim.sources.peer import PEER_FORM


async def _run(ticker: str, mode: str) -> int:
    settings = get_settings()
    host = "localhost" if settings.seller_host in ("0.0.0.0", "") else settings.seller_host
    url = f"http://{host}:{settings.seller_port}/research/fundamentals?ticker={ticker}&mode={mode}"

    print(f"Paying {settings.research_price} for a composed memo on {ticker.upper()} ...\n")
    result = await fetch_paid(url)
    print(f"HTTP {result.status_code}   paid: {result.paid}   tx: {result.tx_hash}\n")

    try:
        data = json.loads(result.text)
    except json.JSONDecodeError:
        print(result.text, file=sys.stderr)
        return 1
    if result.status_code != 200:
        print(json.dumps(data, indent=2), file=sys.stderr)
        return 1

    peer_cites = [c for c in data.get("citations", []) if c.get("form") == PEER_FORM]
    notes = (data.get("cost") or {}).get("sourcing_notes") or []

    print(data.get("memo", "(no memo)"))
    print("\n--- composition ---")
    for note in notes:
        print(f"  {note}")
    print(f"  peer facts cited: {len(peer_cites)}")
    for c in peer_cites:
        print(f"    [{c['id']}] {c['label']} = {c['value']} {c.get('unit', '')}")

    if not peer_cites:
        print(
            "\n(no peer facts in the snapshot — is the seller running with "
            "PEER_SOURCES set as shown in the module docstring?)",
            file=sys.stderr,
        )
        return 1
    print("\n✅ jim bought a peer agent's signals over x402, verified them with the")
    print("   sourcing gate, and sold the composed memo — the agent economy loop.")
    return 0


def main() -> int:
    p = argparse.ArgumentParser(description="Buy a peer-composed fundamentals memo over x402.")
    p.add_argument("ticker", nargs="?", default="AAPL")
    p.add_argument("--mode", choices=["human", "agent"], default="human")
    args = p.parse_args()
    return asyncio.run(_run(args.ticker, args.mode))


if __name__ == "__main__":
    raise SystemExit(main())
