"""The eval rubric — a weighted, mostly-deterministic quality score.

Offline: sourcing + completeness + impersonal are deterministic, so the composite
is a real signal with no API key. faithfulness is folded in only when provided.
"""

from __future__ import annotations

import pytest

from jim.eval.rubric import WEIGHTS, score_memo
from jim.research.facts import USD, Fact, Snapshot
from jim.research.synthesize import DISCLAIMER


def _snap() -> Snapshot:
    return Snapshot(
        ticker="ACME",
        cik="0",
        entity_name="Acme",
        facts=[
            Fact(id="C1", label="Revenue", value=100.0, unit=USD),
            Fact(id="C2", label="Net income", value=20.0, unit=USD),
        ],
    )


def _clean_memo() -> str:
    return f"Revenue was $100 [C1]. Net income was $20 [C2]. {DISCLAIMER}"


def test_clean_memo_scores_near_perfect_offline() -> None:
    r = score_memo(_clean_memo(), _snap())
    assert r.dimensions["sourcing"] == 1.0
    assert r.dimensions["completeness"] == 1.0
    assert r.dimensions["impersonal"] == 1.0
    assert "faithfulness" not in r.dimensions  # offline: dropped
    assert r.composite == pytest.approx(1.0)
    # weights renormalise to sum to 1 over the present dimensions
    assert sum(r.weights.values()) == pytest.approx(1.0)


def test_material_omission_lowers_completeness() -> None:
    memo = f"Revenue was $100 [C1]. {DISCLAIMER}"  # omits Net income (material)
    r = score_memo(memo, _snap())
    assert r.dimensions["completeness"] == 0.5
    assert r.composite < 1.0
    assert any("omitted material" in n for n in r.notes)


def test_personal_advice_zeroes_impersonal_dimension() -> None:
    memo = f"Revenue was $100 [C1]. Net income $20 [C2]. You should buy now. {DISCLAIMER}"
    r = score_memo(memo, _snap())
    assert r.dimensions["impersonal"] == 0.0
    assert any("impersonal" in n for n in r.notes)


def test_faithfulness_included_when_live() -> None:
    r = score_memo(_clean_memo(), _snap(), faithfulness=0.5)
    assert "faithfulness" in r.dimensions and r.dimensions["faithfulness"] == 0.5
    # composite drops below 1 because the (weighted) faithfulness dimension is 0.5
    expected = (1 * (WEIGHTS["sourcing"] + WEIGHTS["completeness"] + WEIGHTS["impersonal"])
                + 0.5 * WEIGHTS["faithfulness"])
    assert r.composite == pytest.approx(expected)
