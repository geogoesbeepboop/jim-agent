"""Peer-agent sourcing (Phase 7) — offline proofs of the buy-from-a-peer loop.

A fake buy_fn stands in for the x402 client (no wallet/network), the in-memory
store stands in for Postgres. What's proven:

  - a peer's facts payload (bare or jim-shaped) becomes a cited Snapshot with
    per-fact origins, through the same procure() → budget → cache path;
  - the purchase caches (second gather buys nothing);
  - a peer below the trust floor is refused BEFORE any buy;
  - an unusable payload fails the gather AND debits the peer's trust;
  - the budget cap refuses the buy before payment.
"""

from __future__ import annotations

import json

import httpx
import pytest

from jim.buyer.client import PaidResponse
from jim.config import Settings
from jim.net import resilience
from jim.research.budget import BudgetCap
from jim.sources.base import BudgetExceeded, ProcurementError
from jim.sources.peer import PeerSource, PeerSpec, parse_peer_specs
from jim.store.repo import MemoryStore

SPEC = PeerSpec(name="mock-sentiment", url="http://peer.test/signals", price_estimate_usd=0.01)

PAYLOAD = {
    "service": "mock-sentiment",
    "facts": [
        {"label": "News sentiment index", "value": 62.5, "unit": "index"},
        {"label": "Positive coverage share", "value": 41.0, "unit": "%"},
        {"label": "Articles analyzed", "value": 128, "unit": "count"},
        {"label": "Bogus row", "value": 1.0, "unit": "parsecs"},  # dropped: unknown unit
        {"label": "Bogus row 2", "value": "NaNish", "unit": "%"},  # dropped: non-numeric
    ],
}


class FakeBuy:
    """Stands in for jim.buyer.client.pay — records calls, returns a payload."""

    def __init__(self, payload: dict, cost: float = 0.01):
        self.payload = payload
        self.cost = cost
        self.calls: list[str] = []

    async def __call__(self, url, *, method="GET", json_body=None, private_key=None,
                       max_price_usd=None, headers=None):
        self.calls.append(url)
        return PaidResponse(
            status_code=200,
            text=json.dumps(self.payload),
            settlement={"transaction": "0xfeed", "success": True},
            cost_in_usd=self.cost,
            tx_hash="0xfeed",
        )


@pytest.fixture(autouse=True)
def _default_settings(monkeypatch):
    """Pin peer-relevant settings so a developer's .env can't skew the tests."""
    import jim.sources.peer as peer_mod

    settings = Settings(
        evm_private_key="0x" + "11" * 32,
        peer_trust_floor=0.4,
        peer_trust_min_events=3,
    )
    monkeypatch.setattr(peer_mod, "get_settings", lambda: settings)


@pytest.fixture(autouse=True)
def _instant_resilience(monkeypatch):
    """Fresh circuit breakers, no real sleeping between retries."""
    resilience.reset_breakers()

    async def no_sleep(_delay: float) -> None:
        return None

    monkeypatch.setattr(resilience, "_sleep", no_sleep)
    monkeypatch.setattr(resilience, "_rand", lambda: 0.0)
    yield
    resilience.reset_breakers()


class UnreachableBuy:
    """Stands in for a peer whose host refuses the connection (e.g. down, wrong
    port) — mirrors ``httpx.ConnectError('[Errno 61] Connection refused')``."""

    def __init__(self):
        self.calls = 0

    async def __call__(self, url, *, method="GET", json_body=None, private_key=None,
                       max_price_usd=None, headers=None):
        self.calls += 1
        raise httpx.ConnectError("[Errno 61] Connection refused")


async def test_peer_facts_become_a_cited_snapshot() -> None:
    buy = FakeBuy(PAYLOAD)
    store = MemoryStore()
    budget = BudgetCap(ceiling_usd=0.10)

    result = await PeerSource(SPEC, buy_fn=buy).gather("AAPL", budget=budget, store=store)

    snap = result.snapshot
    assert [f.id for f in snap.facts] == ["C1", "C2", "C3"]  # bogus rows dropped
    assert snap.facts[0].label == "News sentiment index"
    assert snap.facts[0].form == "peer agent (x402)"
    assert snap.facts[0].source_label == "mock-sentiment"
    assert snap.facts[0].accession == "0xfeed"  # settlement tx anchors the citation
    assert snap.origins == {"C1": "peer:mock-sentiment", "C2": "peer:mock-sentiment",
                            "C3": "peer:mock-sentiment"}
    assert result.cost_in_usd == 0.01
    assert budget.spent_usd == 0.01
    assert "identifier=AAPL" in buy.calls[0]


async def test_jim_shaped_citations_payload_works_too() -> None:
    jim_shaped = {"citations": [{"label": "Revenue", "value": 1e9, "unit": "USD"}]}
    result = await PeerSource(SPEC, buy_fn=FakeBuy(jim_shaped)).gather(
        "MSFT", budget=BudgetCap(ceiling_usd=0.10), store=MemoryStore()
    )
    assert len(result.snapshot.facts) == 1
    assert result.snapshot.facts[0].unit == "USD"


async def test_second_gather_hits_the_purchase_cache() -> None:
    buy = FakeBuy(PAYLOAD)
    store = MemoryStore()
    source = PeerSource(SPEC, buy_fn=buy)

    first = await source.gather("AAPL", budget=BudgetCap(ceiling_usd=0.10), store=store)
    second = await source.gather("AAPL", budget=BudgetCap(ceiling_usd=0.10), store=store)

    assert not first.cache_hit and second.cache_hit
    assert second.cost_in_usd == 0.0
    assert len(buy.calls) == 1  # bought exactly once


async def test_below_trust_floor_refuses_before_buying() -> None:
    buy = FakeBuy(PAYLOAD)
    store = MemoryStore()
    for _ in range(3):  # 0 ok / 3 fail → laplace 0.2 < floor 0.4, events ≥ min 3
        await store.record_trust_event(
            source="peer:mock-sentiment", ok=False, context="seeded"
        )

    with pytest.raises(ProcurementError, match="trust floor"):
        await PeerSource(SPEC, buy_fn=buy).gather(
            "AAPL", budget=BudgetCap(ceiling_usd=0.10), store=store
        )
    assert buy.calls == []  # refused before any payment


async def test_unusable_payload_fails_and_debits_trust() -> None:
    store = MemoryStore()
    with pytest.raises(ProcurementError, match="no usable facts"):
        await PeerSource(SPEC, buy_fn=FakeBuy({"facts": [{"nope": 1}]})).gather(
            "AAPL", budget=BudgetCap(ceiling_usd=0.10), store=store
        )
    scores = await store.trust_scores()
    assert scores["peer:mock-sentiment"]["fail"] == 1


async def test_budget_cap_refuses_the_buy() -> None:
    buy = FakeBuy(PAYLOAD)
    with pytest.raises(BudgetExceeded):
        await PeerSource(SPEC, buy_fn=buy).gather(
            "AAPL", budget=BudgetCap(ceiling_usd=0.001), store=MemoryStore()
        )
    assert buy.calls == []


async def test_unreachable_peer_retries_then_raises_when_used_standalone() -> None:
    """A refused connection is retried (resilience wrapper) then surfaces as-is —
    a bare PeerSource has no one to degrade to, so the caller must handle it."""
    buy = UnreachableBuy()
    with pytest.raises(httpx.ConnectError):
        await PeerSource(SPEC, buy_fn=buy).gather(
            "AAPL", budget=BudgetCap(ceiling_usd=0.10), store=MemoryStore()
        )
    assert buy.calls == 3  # 1 + resilience_retries(2) attempts, not a single hard failure


async def test_unreachable_peer_degrades_to_a_note_when_composed() -> None:
    """The actual reported bug: a peer that's down (connection refused) must not
    turn the whole research run into an opaque 422 — CompositeSource should skip
    it with a note and still return the primary source's data."""
    from jim.sources.peer import CompositeSource

    class FakePrimary:
        name = "fundamentals"
        is_paid = False

        async def gather(self, identifier, *, budget, store):
            from jim.research.facts import USD, Fact, Snapshot

            snap = Snapshot(
                ticker=identifier.upper(), cik="1", entity_name="Test Corp",
                facts=[Fact(id="C1", label="Revenue", value=1e9, unit=USD)],
            )
            from jim.sources.base import GatherResult

            return GatherResult(snapshot=snap, cost_in_usd=0.0, cache_hit=False)

    buy = UnreachableBuy()
    source = CompositeSource(FakePrimary(), [PeerSource(SPEC, buy_fn=buy)])
    result = await source.gather("AAPL", budget=BudgetCap(ceiling_usd=0.10), store=MemoryStore())

    assert len(result.snapshot.facts) == 1  # only the primary fact — peer skipped
    assert any("peer:mock-sentiment: skipped" in n for n in result.notes)
    assert buy.calls == 3  # still went through the retry policy before giving up


# --- PEER_SOURCES parsing -----------------------------------------------------


def test_parse_peer_specs() -> None:
    raw = json.dumps(
        [
            {
                "name": "sentiment-alpha",
                "url": "https://peer.example/signals",
                "price_estimate_usd": 0.02,
                "products": ["fundamentals"],
            }
        ]
    )
    (spec,) = parse_peer_specs(raw)
    assert spec.name == "sentiment-alpha"
    assert spec.products == ("fundamentals",)
    assert parse_peer_specs(None) == []
    assert parse_peer_specs("") == []
    with pytest.raises(ValueError, match="valid JSON"):
        parse_peer_specs("{nope")
    with pytest.raises(ValueError, match="bad peer name"):
        parse_peer_specs(json.dumps([{"name": "Bad Name!", "url": "https://x"}]))
    with pytest.raises(ValueError, match="http"):
        parse_peer_specs(json.dumps([{"name": "ok", "url": "ftp://x"}]))


def test_products_compose_configured_peers(monkeypatch) -> None:
    import jim.research.products as products_mod
    from jim.sources.peer import CompositeSource

    raw = json.dumps([{"name": "sent", "url": "https://p.example/s", "products": ["fundamentals"]}])
    settings = Settings(peer_sources=raw)
    monkeypatch.setattr(products_mod, "get_settings", lambda: settings)

    products = products_mod.get_products()
    assert isinstance(products["fundamentals"].source, CompositeSource)
    assert not isinstance(products["macro"].source, CompositeSource)  # not configured for it
