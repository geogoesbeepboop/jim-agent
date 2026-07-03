"""Source interface + shared procurement.

A ``Source`` turns an identifier (a ticker, a token) into a cited
:class:`Snapshot`, reporting how much it spent (``cost_in_usd``) and whether the
data came from cache. Paid sources route through :func:`procure`, which enforces
the budget and uses the cache so we buy a datum once and reuse it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Awaitable, Callable, Protocol

from jim.research.budget import BudgetCap
from jim.research.facts import Snapshot
from jim.store import Store


@dataclass
class GatherResult:
    snapshot: Snapshot
    cost_in_usd: float
    cache_hit: bool
    # Phase 7: human-readable sourcing notes ("peer:x — skipped: below trust
    # floor"), surfaced in the response's cost block so a composed gather is
    # auditable without digging through logs.
    notes: list[str] = field(default_factory=list)


class BudgetExceeded(RuntimeError):
    """The budget cap denied a purchase the source needed."""


class ProcurementError(RuntimeError):
    """A paid fetch failed (non-200, unparseable payload, etc.)."""


class Source(Protocol):
    name: str
    is_paid: bool

    async def gather(self, identifier: str, *, budget: BudgetCap, store: Store) -> GatherResult: ...


@dataclass
class ProcureResult:
    payload: dict
    cost_in_usd: float
    cache_hit: bool
    tx_hash: str | None


# A buy function: (url, method, json_body, headers, private_key) -> PaidResponse.
BuyFn = Callable[..., Awaitable]


async def procure(
    *,
    source_name: str,
    cache_key: str,
    url: str,
    method: str,
    json_body: dict | None,
    network: str,
    price_estimate_usd: float,
    private_key: str | None,
    budget: BudgetCap,
    store: Store,
    ttl_seconds: int,
    buy_fn: BuyFn,
) -> ProcureResult:
    """Cache-check → propose to budget → buy → record. The economic core.

    Returns cached data at zero marginal cost when available; otherwise asks the
    budget for permission, buys over x402, and persists the datum for reuse.
    """
    cached = await store.get_cached_purchase(source_name, cache_key)
    if cached is not None:
        return ProcureResult(cached.payload, 0.0, True, cached.tx_hash)

    decision = budget.propose(price_estimate_usd, reason=f"buy {source_name} data ({cache_key})")
    if not decision.approved:
        raise BudgetExceeded(decision.reason)

    # The estimate is what we *expect*; the cap enforces what the seller *actually*
    # advertises in its 402. Pass the real remaining ceiling so an over-cap price is
    # refused before settlement — the deterministic guard against dynamic x402 pricing.
    from jim.buyer.client import PriceCapExceeded
    from jim.interop.callchain import CallChainDepthExceeded

    try:
        resp = await buy_fn(
            url,
            method=method,
            json_body=json_body,
            private_key=private_key,
            max_price_usd=budget.remaining_usd,
        )
    except (PriceCapExceeded, CallChainDepthExceeded) as e:
        # Both are deterministic refusals to spend, made before any settlement.
        raise BudgetExceeded(str(e)) from e
    if resp.status_code != 200:
        raise ProcurementError(f"{source_name} returned HTTP {resp.status_code}: {resp.text[:200]}")
    try:
        payload = resp.json()
    except ValueError as e:
        raise ProcurementError(f"{source_name} returned non-JSON payload") from e

    budget.commit(resp.cost_in_usd)
    await store.record_purchase(
        source=source_name,
        key=cache_key,
        url=url,
        network=network,
        cost_usd=resp.cost_in_usd,
        tx_hash=resp.tx_hash,
        payload=payload,
        ttl_seconds=ttl_seconds,
    )
    return ProcureResult(payload, resp.cost_in_usd, False, resp.tx_hash)
