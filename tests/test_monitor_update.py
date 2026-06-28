"""The update synthesizer's deterministic fallback must be gate-safe AND
impersonal *by construction* — so monitors work with no API key, exactly like the
sourcing gate does. Offline."""

from __future__ import annotations

import pytest

from jim.config import get_settings
from jim.monitors.impersonal import check_impersonal
from jim.monitors.models import Signal
from jim.monitors.update import synthesize_update
from jim.research.facts import INDEX, MULTIPLE, USD, Fact, Snapshot
from jim.research.gate import check_sourcing


@pytest.fixture
def _no_key(monkeypatch):
    monkeypatch.setattr(get_settings(), "anthropic_api_key", None)


def _snapshot():
    return Snapshot(
        ticker="AAPL",
        cik="0000320193",
        entity_name="Apple Inc.",
        as_of="2025-02-01",
        facts=[
            Fact(
                id="C1", label="Price", value=120.0, unit=USD, source_label="Yahoo", accession="new"
            ),
            Fact(id="C2", label="RSI (14-day)", value=72.0, unit=INDEX),
            Fact(id="C3", label="P/E (TTM)", value=31.5, unit=MULTIPLE, is_derived=True),
        ],
    )


async def test_fallback_memo_passes_sourcing_gate(_no_key):
    snap = _snapshot()
    signals = [
        Signal(
            "price_move",
            "k1",
            "Price",
            "critical",
            "Price rose",
            ["C1"],
            new_value=120.0,
            direction="up",
        ),
        Signal(
            "threshold",
            "k2",
            "RSI (14-day)",
            "notable",
            "RSI crossed",
            ["C2"],
            new_value=72.0,
            direction="cross_up",
        ),
        Signal(
            "metric_change",
            "k3",
            "P/E (TTM)",
            "notable",
            "P/E rose",
            ["C3"],
            new_value=31.5,
            direction="up",
        ),
    ]
    res = await synthesize_update(snap, signals, severity="critical")

    assert res.used_fallback is True
    assert res.inference_cost_usd == 0.0
    gate = check_sourcing(res.memo, snap)
    assert gate.passed, [f"{v.reason}: {v.figure}" for v in gate.violations]
    assert check_impersonal(res.memo).passed
    # The cited values appear with their citations.
    assert "[C1]" in res.memo and "[C2]" in res.memo and "[C3]" in res.memo


async def test_fallback_handles_new_filing_signal_without_citation(_no_key):
    snap = _snapshot()
    signals = [
        Signal("new_filing", "f", "SEC filing", "notable", "new filing", [], direction="new")
    ]
    res = await synthesize_update(snap, signals)
    assert res.used_fallback
    assert check_sourcing(res.memo, snap).passed  # zero figures → trivially passes
    assert "filing" in res.memo.lower()


async def test_llm_error_degrades_to_gate_safe_fallback(monkeypatch):
    # With a key set, a dead LLM must NOT crash the monitor — it falls back to the
    # construction-safe deterministic memo so a correct, cited update still ships.
    monkeypatch.setattr(get_settings(), "anthropic_api_key", "sk-test")

    import anthropic

    class _Boom:
        def __init__(self, *a, **k):
            self.messages = self

        async def create(self, *a, **k):
            raise RuntimeError("api down")

    monkeypatch.setattr(anthropic, "AsyncAnthropic", _Boom)

    snap = _snapshot()
    signals = [
        Signal(
            "price_move",
            "k1",
            "Price",
            "critical",
            "Price rose",
            ["C1"],
            new_value=120.0,
            direction="up",
        )
    ]
    res = await synthesize_update(snap, signals)
    assert res.used_fallback is True
    assert check_sourcing(res.memo, snap).passed
