"""Budget cap: the propose/dispose split, offline."""

from __future__ import annotations

import pytest

from jim.research.budget import BudgetCap


def test_approves_within_ceiling():
    b = BudgetCap(ceiling_usd=0.10)
    d = b.propose(0.04, "buy quote")
    assert d.approved
    b.commit(0.04)
    assert b.remaining_usd == pytest.approx(0.06)


def test_denies_over_ceiling():
    b = BudgetCap(ceiling_usd=0.10)
    b.commit(0.08)
    d = b.propose(0.05)  # 0.08 + 0.05 > 0.10
    assert not d.approved
    assert "denied" in d.reason
    # A denied proposal does not spend.
    assert b.spent_usd == 0.08


def test_proposing_does_not_commit():
    b = BudgetCap(ceiling_usd=0.10)
    b.propose(0.04)
    assert b.spent_usd == 0.0  # propose != spend
    assert len(b.decisions) == 1
