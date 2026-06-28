"""EDGAR as a Source. Free (public domain), so it never spends from the budget."""

from __future__ import annotations

from jim.research.budget import BudgetCap
from jim.research.edgar import fetch_snapshot
from jim.sources.base import GatherResult
from jim.store import Store


class EdgarSource:
    name = "edgar"
    is_paid = False

    async def gather(self, identifier: str, *, budget: BudgetCap, store: Store) -> GatherResult:
        snapshot = await fetch_snapshot(identifier)
        return GatherResult(snapshot=snapshot, cost_in_usd=0.0, cache_hit=False)
