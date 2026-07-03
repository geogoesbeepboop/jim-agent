"""Mock peer agent — a testnet stand-in for a paid peer over x402 (Phase 7).

Serves the peer wire format (``{"service", "as_of", "facts": [...]}``) that
:mod:`jim.sources.peer` parses, playing the role of a specialized "sentiment
agent" jim subcontracts. Values are static, deterministic functions of the
identifier — clearly not live signals; their only job is to exercise the full
peer loop (402 → pay → parse → merge → gate → trust) on testnet USDC, exactly
as ``mock_graph`` does for The Graph.

Point PEER_SOURCES at a running seller's ``/mock-peer/research`` to compose it:

    PEER_SOURCES='[{"name":"mock-sentiment",
                    "url":"http://localhost:4021/mock-peer/research"}]'
"""

from __future__ import annotations

import hashlib


def build_mock_peer_response(identifier: str, *, corrupt: bool = False) -> dict:
    """A sentiment-agent-shaped facts payload, deterministic per identifier.

    ``corrupt=True`` plays the lying peer (``MOCK_PEER_CORRUPT``): it still
    takes the payment but returns rows jim can't verify — no numeric values,
    no canonical units. The peer loop then does what it's built to do:
    ``PeerSource`` refuses the payload, debits the peer's trust, and after
    ``PEER_TRUST_MIN_EVENTS`` failures stops buying from it entirely. That's
    the demo beat: a paid subcontractor goes bad and the system routes around
    it, visibly, on the /proof page.
    """
    ident = (identifier or "UNKNOWN").strip().upper()
    if corrupt:
        return {
            "service": "mock-sentiment",
            "as_of": None,
            "facts": [
                {"label": "News sentiment index", "value": "extremely bullish", "unit": "vibes"},
                {"label": "Positive coverage share", "value": None, "unit": "%"},
                {"label": "Trust me", "value": True, "unit": "count"},
            ],
        }
    seed = int.from_bytes(hashlib.sha256(ident.encode("utf-8")).digest()[:4], "big")
    sentiment = round(35.0 + (seed % 500) / 10.0, 1)  # 35.0–84.9 index
    articles = float(24 + seed % 480)
    positive_share = round(30.0 + (seed >> 4) % 400 / 10.0, 1)  # 30.0–69.9 %

    return {
        "service": "mock-sentiment",
        "as_of": None,
        "facts": [
            {
                "label": "News sentiment index",
                "value": sentiment,
                "unit": "index",
                "concept": "sentiment.index",
                "accession": f"mock-sent-{ident.lower()}",
                "source_url": "https://example.invalid/mock-sentiment",
            },
            {
                "label": "Positive coverage share",
                "value": positive_share,
                "unit": "%",
                "concept": "sentiment.positive_share",
                "accession": f"mock-sent-{ident.lower()}",
                "source_url": "https://example.invalid/mock-sentiment",
            },
            {
                "label": "Articles analyzed",
                "value": articles,
                "unit": "count",
                "concept": "sentiment.articles",
                "accession": f"mock-sent-{ident.lower()}",
                "source_url": "https://example.invalid/mock-sentiment",
            },
        ],
    }
