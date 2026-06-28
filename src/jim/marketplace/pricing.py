"""Published pricing tiers (Phase 5).

The build plan calls for *publishing pricing tiers*. jim already has three real
price points — a fundamentals call, a token call, and a per-delivered-update
monitor charge — so rather than inventing untested payment routes, this module
turns the prices the system *actually* charges into an honest, deterministic
pricing schedule and frames them as tiers:

  - ``oneshot``  : one fully-cited report (the base per-call price).
  - ``agent``    : the same report in terse ``mode=agent`` form, discounted as a
                   nudge toward machine buyers (cheaper to serve, easier to parse).
  - ``bundle``   : N identifiers in one request, discounted per item (the cache
                   makes the marginal report nearly free, so we share the saving).
  - ``monitor``  : a continuous monitor — quiet polls are free; you pay only when
                   a material, cited update is pushed (Phase 4 economics).

Every number here derives from :mod:`jim.config`, so changing a price in the env
changes the published schedule. No model, no hidden math — like the rest of jim,
the schedule is reproducible.
"""

from __future__ import annotations

from dataclasses import dataclass

from jim.config import get_settings
from jim.research.products import get_product, usd


def _round(x: float) -> float:
    return round(x, 6)


@dataclass(frozen=True)
class PricingTier:
    name: str
    price_usd: float
    unit: str
    description: str

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "price_usd": self.price_usd,
            "unit": self.unit,
            "description": self.description,
        }


def base_price(product: str) -> float:
    """The headline per-call price for a product (the ``oneshot`` tier)."""
    return get_product(product).price_out_usd


def tiers_for(product: str) -> list[PricingTier]:
    """The published tier schedule for one product (deterministic)."""
    s = get_settings()
    base = base_price(product)
    agent = _round(base * (1 - s.agent_tier_discount_pct / 100))
    per_item = _round(base * (1 - s.bundle_tier_discount_pct / 100))
    update = usd(s.monitor_update_price)
    return [
        PricingTier(
            "oneshot",
            base,
            "per call",
            "One fully-cited report; every figure resolves to a primary source.",
        ),
        PricingTier(
            "agent",
            agent,
            "per call",
            f"Terse, metric-dense output (mode=agent) — {s.agent_tier_discount_pct:.0f}% "
            "off for machine buyers (cheaper to serve, easier to parse).",
        ),
        PricingTier(
            "bundle",
            per_item,
            "per identifier",
            f"Up to {s.bundle_max_items} identifiers in one request — "
            f"{s.bundle_tier_discount_pct:.0f}% off per item (cache-warmed marginal cost).",
        ),
        PricingTier(
            "monitor",
            update,
            "per delivered update",
            "Continuous monitor: quiet polls cost $0; you pay only when a material, "
            "cited update is pushed.",
        ),
    ]


def price_for(product: str, tier: str = "oneshot") -> float:
    """Resolve a single tier's price (falls back to ``oneshot``)."""
    by_name = {t.name: t for t in tiers_for(product)}
    chosen = by_name.get(tier) or by_name["oneshot"]
    return chosen.price_usd


def pricing_schedule() -> dict:
    """The whole published schedule, keyed by product (for ``/pricing``)."""
    from jim.marketplace.catalog import product_names

    return {p: [t.to_dict() for t in tiers_for(p)] for p in product_names()}
