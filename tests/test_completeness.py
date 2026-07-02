"""The completeness check — flags material snapshot facts the memo omitted.

Deterministic, offline. Mirror image of the gate (which checks what's included).
"""

from __future__ import annotations

from jim.research.completeness import check_completeness, cited_ids
from jim.research.facts import COUNT, USD, Fact, Snapshot


def _snap() -> Snapshot:
    return Snapshot(
        ticker="ACME",
        cik="0",
        entity_name="Acme",
        facts=[
            Fact(id="C1", label="Revenue", value=100.0, unit=USD),  # material
            Fact(id="C2", label="Net income", value=20.0, unit=USD),  # material
            Fact(id="C3", label="On-chain transactions", value=5.0, unit=COUNT),  # not material
            Fact(id="C4", label="ETH price (USD)", value=3000.0, unit=USD),  # not material
        ],
    )


def test_cited_ids_parses_groups() -> None:
    assert cited_ids("Revenue $100 [C1] and margin [C2, C3].") == {"C1", "C2", "C3"}
    assert cited_ids("no citations here") == set()


def test_flags_material_omission() -> None:
    # Memo cites Revenue (material) + a non-material fact, omits Net income (material).
    memo = "Revenue was $100 [C1]; transactions 5 [C3]."
    r = check_completeness(memo, _snap())
    assert r.coverage == 0.5  # 2 of 4 facts cited
    # 2 material facts (Revenue, Net income); Net income omitted → 1/2 covered
    assert r.material_coverage == 0.5
    omitted_labels = {o["label"] for o in r.material_omissions}
    assert omitted_labels == {"Net income"}
    assert r.passed is False  # below default 0.6 material floor


def test_full_material_coverage_passes() -> None:
    memo = "Revenue $100 [C1]; net income $20 [C2]."
    r = check_completeness(memo, _snap())
    assert r.material_coverage == 1.0
    assert r.material_omissions == []
    assert r.passed is True


def test_no_facts_is_vacuously_complete() -> None:
    r = check_completeness("anything", Snapshot(ticker="X", cik="0", entity_name="X", facts=[]))
    assert r.coverage == 1.0 and r.material_coverage == 1.0 and r.passed
