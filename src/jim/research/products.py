"""Product registry: maps a product name to its data source and sale price.

  - "fundamentals" → EDGAR (free upstream) → priced at research_price
  - "token"        → The Graph (paid upstream) → priced at token_research_price

The sale price minus (data cost + inference cost) is the per-query margin the
Phase 2 dashboard reports. When ``PEER_SOURCES`` names peer agents (Phase 7),
each product's source is composed with the peers configured for it — jim then
buys their signals inside the same run and the gate verifies the merged facts.
"""

from __future__ import annotations

from dataclasses import dataclass

from jim.config import get_settings
from jim.sources import FundamentalsSource, GraphSource, MacroSource, Source


def usd(price: str) -> float:
    """Parse a '$0.25'-style price into a float."""
    return float(price.replace("$", "").strip())


@dataclass
class Product:
    name: str
    source: Source
    price_out_usd: float
    identifier_label: str  # what the identifier means, for help text


def _compose_peers(product: str, source: Source) -> Source:
    """Wrap ``source`` with the peer agents configured for this product."""
    from jim.sources.peer import CompositeSource, PeerSource, parse_peer_specs

    specs = [
        spec
        for spec in parse_peer_specs(get_settings().peer_sources)
        if not spec.products or product in spec.products
    ]
    if not specs:
        return source
    return CompositeSource(source, [PeerSource(spec) for spec in specs])


def get_products() -> dict[str, Product]:
    s = get_settings()
    return {
        "fundamentals": Product(
            name="fundamentals",
            source=_compose_peers("fundamentals", FundamentalsSource()),
            price_out_usd=usd(s.research_price),
            identifier_label="stock ticker (e.g. AAPL)",
        ),
        "token": Product(
            name="token",
            source=_compose_peers("token", GraphSource()),
            price_out_usd=usd(s.token_research_price),
            identifier_label="token symbol or 0x address, optional :chain (e.g. WETH, AERO:base)",
        ),
        "macro": Product(
            name="macro",
            source=_compose_peers("macro", MacroSource()),
            price_out_usd=usd(s.macro_research_price),
            identifier_label="region (US) — cited Fed funds / CPI / Treasury context",
        ),
    }


def get_product(name: str) -> Product:
    products = get_products()
    if name not in products:
        raise ValueError(f"Unknown product {name!r}. Available: {', '.join(products)}.")
    return products[name]
