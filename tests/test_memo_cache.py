"""The memo cache — serve a recent identical memo, skip re-synthesis.

Covers the snapshot fingerprint, the store round-trip (hit / fingerprint-miss /
TTL-miss), and the engine end-to-end: an identical second query is served from
cache (synthesizer called once, $0 inference), while changed data re-synthesizes.
"""

from __future__ import annotations


from jim.research import engine
from jim.research.cost import Usage
from jim.research.debate import DebateResult
from jim.research.facts import USD, Fact, Snapshot
from jim.research.judge import JudgeResult
from jim.research.products import Product
from jim.research.synthesize import SynthResult
from jim.sources.base import GatherResult
from jim.store import get_store, reset_store


# --- fingerprint -------------------------------------------------------------


def _snap(revenue: float = 100.0) -> Snapshot:
    return Snapshot(
        ticker="ACME",
        cik="0",
        entity_name="Acme",
        facts=[Fact(id="C1", label="Revenue", value=revenue, unit=USD)],
        as_of="2025-01-01",
    )


def test_fingerprint_stable_and_data_sensitive() -> None:
    assert _snap().fingerprint() == _snap().fingerprint()  # same data → same hash
    assert _snap(100.0).fingerprint() != _snap(101.0).fingerprint()  # moved value → new hash
    # id scheme independent: same data, different ids → same fingerprint
    a = Snapshot(ticker="A", cik="0", entity_name="A",
                 facts=[Fact(id="C1", label="Revenue", value=100.0, unit=USD)])
    b = Snapshot(ticker="A", cik="0", entity_name="A",
                 facts=[Fact(id="C9", label="Revenue", value=100.0, unit=USD)])
    assert a.fingerprint() == b.fingerprint()


# --- store round-trip --------------------------------------------------------


async def test_store_memo_cache_hit_miss_ttl() -> None:
    reset_store()
    store = get_store()
    await store.put_cached_memo(key="k", fingerprint="fp1", memo="hello", debate=None)

    assert (await store.get_cached_memo(key="k", fingerprint="fp1", ttl_seconds=999))["memo"] == "hello"
    # fingerprint mismatch (data changed) → miss
    assert await store.get_cached_memo(key="k", fingerprint="fp2", ttl_seconds=999) is None
    # expired → miss
    assert await store.get_cached_memo(key="k", fingerprint="fp1", ttl_seconds=0) is None
    reset_store()


# --- engine end-to-end -------------------------------------------------------


class _Source:
    name = "fake"
    is_paid = False

    def __init__(self, snap: Snapshot):
        self._snap = snap

    async def gather(self, identifier, *, budget, store) -> GatherResult:
        return GatherResult(snapshot=self._snap, cost_in_usd=0.0, cache_hit=False)


def _wire(monkeypatch, snap: Snapshot):
    calls = {"n": 0}

    async def synth(snapshot, *, mode="human", feedback=None, debate=None) -> SynthResult:
        calls["n"] += 1
        return SynthResult(
            memo="Revenue was $100 [C1].", usage=Usage(model="t", input_tokens=10, output_tokens=20)
        )

    async def judge(memo, snapshot, **_) -> JudgeResult:
        return JudgeResult.skip()

    async def debate(snapshot) -> DebateResult:
        return DebateResult(bull="", bear="", verdict="", usages=[])

    monkeypatch.setattr(engine, "synthesize", synth)
    monkeypatch.setattr(engine, "judge_faithfulness", judge)
    monkeypatch.setattr(engine, "run_debate", debate)
    monkeypatch.setattr(
        engine,
        "get_product",
        lambda name: Product(name="fundamentals", source=_Source(snap), price_out_usd=0.25,
                             identifier_label="x"),
    )
    return calls


async def test_identical_second_query_served_from_cache(monkeypatch) -> None:
    reset_store()
    calls = _wire(monkeypatch, _snap())

    first = await engine.run_research("ACME", enable_debate=False)
    second = await engine.run_research("ACME", enable_debate=False)

    assert first.status == "ok" and second.status == "ok"
    assert first.served_from_cache is False
    assert second.served_from_cache is True
    assert calls["n"] == 1  # synthesizer ran ONCE — the cache served the second
    assert second.cost["inference_cost_usd"] == 0.0
    assert second.cost["served_from_cache"] is True
    reset_store()


async def test_changed_data_invalidates_cache(monkeypatch) -> None:
    reset_store()
    # First run caches against revenue=100.
    _wire(monkeypatch, _snap(100.0))
    await engine.run_research("ACME", enable_debate=False)
    # Now the underlying data changes → different fingerprint → re-synthesize.
    _wire(monkeypatch, _snap(200.0))  # rewires synth (resets call counter)
    again = await engine.run_research("ACME", enable_debate=False)
    assert again.served_from_cache is False
    reset_store()


async def test_no_cache_flag_bypasses(monkeypatch) -> None:
    reset_store()
    calls = _wire(monkeypatch, _snap())
    await engine.run_research("ACME", enable_debate=False)
    second = await engine.run_research("ACME", enable_debate=False, use_memo_cache=False)
    assert second.served_from_cache is False
    assert calls["n"] == 2  # synthesized both times
    reset_store()
