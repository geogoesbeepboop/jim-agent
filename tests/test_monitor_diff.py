"""Deterministic snapshot diffing — the substrate the crew reads. Offline."""

from __future__ import annotations

from jim.monitors.diff import diff_snapshots, snapshot_to_baseline
from jim.research.facts import INDEX, USD, Fact, Snapshot


def _snap(price, rsi, *, acc="acc1", as_of="2025-01-01", extra=None):
    facts = [
        Fact(id="C1", label="Price", value=price, unit=USD, source_label="Yahoo", accession=acc),
        Fact(id="C2", label="RSI (14-day)", value=rsi, unit=INDEX),
    ]
    if extra:
        facts.extend(extra)
    return Snapshot(
        ticker="AAPL", cik="0000320193", entity_name="Apple Inc.", as_of=as_of, facts=facts
    )


def test_first_run_has_no_comparison():
    diff = diff_snapshots(None, _snap(100, 55))
    assert diff.is_first_run
    assert not diff.deltas


def test_value_deltas_and_pct():
    base = snapshot_to_baseline(_snap(100, 50))
    diff = diff_snapshots(base, _snap(110, 60))
    price = diff.get("Price")
    assert price.old_value == 100 and price.new_value == 110
    assert price.abs_change == 10 and price.pct_change == 10.0
    assert "Price" in diff.changed_labels


def test_new_and_removed_metrics():
    base = snapshot_to_baseline(
        _snap(100, 50, extra=[Fact(id="C9", label="Dividend yield", value=0.5, unit="%")])
    )
    # Dividend yield drops out; a Net income fact appears.
    cur = _snap(100, 50, extra=[Fact(id="C9", label="Net income", value=9.9e10, unit=USD)])
    diff = diff_snapshots(base, cur)
    assert diff.get("Net income").is_new
    assert "Dividend yield" in diff.removed
    # A brand-new metric is excluded from changed_labels (nothing to compare).
    assert "Net income" not in diff.changed_labels


def test_new_filing_and_as_of_advance():
    base = snapshot_to_baseline(_snap(100, 50, acc="old", as_of="2025-01-01"))
    diff = diff_snapshots(base, _snap(100, 50, acc="new", as_of="2025-02-01"))
    assert diff.new_accessions == ["new"]
    assert diff.as_of_advanced
    assert diff.prev_as_of == "2025-01-01" and diff.new_as_of == "2025-02-01"


def test_no_change_no_deltas():
    base = snapshot_to_baseline(_snap(100, 50))
    diff = diff_snapshots(base, _snap(100, 50))
    assert diff.changed_labels == []
    assert not diff.new_accessions and not diff.as_of_advanced
