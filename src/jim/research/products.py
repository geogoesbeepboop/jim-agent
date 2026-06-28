"""Product registry: maps a product name to its data source and sale price.

  - "fundamentals" → EDGAR (free upstream) → priced at research_price
  - "token"        → The Graph (paid upstream) → priced at token_research_price

The sale price minus (data cost + inference cost) is the per-query margin the
Phase 2 dashboard reports.
"""

from __future__ import annotations

from dataclasses import dataclass

from jim.config import get_settings
from jim.sources import FundamentalsSource, GraphSource, Source


def usd(price: str) -> float:
    """Parse a '$0.25'-style price into a float."""
    return float(price.replace("$", "").strip())


@dataclass
class Product:
    name: str
    source: Source
    price_out_usd: float
    identifier_label: str  # what the identifier means, for help text


def get_products() -> dict[str, Product]:
    s = get_settings()
    return {
        "fundamentals": Product(
            name="fundamentals",
            source=FundamentalsSource(),
            price_out_usd=usd(s.research_price),
            identifier_label="stock ticker (e.g. AAPL)",
        ),
        "token": Product(
            name="token",
            source=GraphSource(),
            price_out_usd=usd(s.token_research_price),
            identifier_label="token symbol or 0x address (e.g. WETH)",
        ),
    }


def get_product(name: str) -> Product:
    products = get_products()
    if name not in products:
        raise ValueError(f"Unknown product {name!r}. Available: {', '.join(products)}.")
    return products[name]
