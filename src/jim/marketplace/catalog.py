"""The marketplace catalog (Phase 5) — jim's machine-discoverable product list.

This is the single source of truth for "what does jim sell, how do you call it,
what do you get back, and what does it cost". Everything downstream reads from
here:

  - the seller's paid routes attach a Bazaar **discovery extension** built from a
    listing (so the first successful settlement auto-indexes us — see ADR-0003);
  - ``GET /catalog`` and ``GET /.well-known/x402`` serialize listings for agents;
  - the human UI and the system map render from the same listings.

A listing fuses *static* product framing (title, tags, the external upstream it
draws on) with *dynamic* facts pulled live from the product registry and config
(price, source name, whether the upstream is paid). The output JSON schema is
derived directly from the Pydantic response model, so the advertised contract can
never drift from what the endpoint actually returns.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from jim.marketplace.pricing import PricingTier, tiers_for
from jim.research.products import get_products

# Static, human-facing framing per product. Price / source / paid-ness are read
# live from the registry so this never goes stale.
_SPECS: dict[str, dict] = {
    "fundamentals": {
        "title": "Company Fundamentals",
        "verb": "GET",
        "path": "/research/fundamentals",
        "param": "ticker",
        "param_example": "AAPL",
        "tags": ["finance", "equities", "fundamentals", "edgar", "cited"],
        "upstream": "SEC EDGAR (public domain) + market prices",
        "description": (
            "A fully-cited company fundamentals memo from SEC EDGAR: income "
            "statement, balance sheet, cash flow, EPS, plus derived margins, "
            "returns, leverage, growth, and (best-effort) market + technical "
            "metrics. Every figure resolves to a filing accession."
        ),
    },
    "token": {
        "title": "On-chain Token Snapshot",
        "verb": "GET",
        "path": "/research/token",
        "param": "token",
        "param_example": "WETH",
        "tags": ["defi", "token", "uniswap", "onchain", "cited"],
        "upstream": "The Graph (Uniswap v3) over x402",
        "description": (
            "A cited on-chain token snapshot — price, TVL, volume, supply — with "
            "upstream data bought from The Graph over x402. Multi-chain: append "
            ":chain to the symbol (WETH:base, AERO:base, ARB:arbitrum). This is "
            "the two-sided product: your payment funds a run in which jim itself "
            "pays for data."
        ),
    },
    "macro": {
        "title": "US Macro Context",
        "verb": "GET",
        "path": "/research/macro",
        "param": "region",
        "param_example": "US",
        "tags": ["macro", "rates", "cpi", "treasury", "cited"],
        "upstream": "US government (Fed, BLS, Treasury) — public domain",
        "description": (
            "A cited US macro snapshot — Fed funds (effective), CPI inflation, and "
            "the 2y/10y Treasury yields with the 2s10s spread. Every figure traces "
            "to a US-government primary source (public domain, redistributable); "
            "the data is free, so this is a pure-margin product like fundamentals."
        ),
    },
}


def product_names() -> list[str]:
    """Products that are both registered and have catalog framing."""
    return [name for name in get_products() if name in _SPECS]


def _output_schema() -> dict:
    """A compact, self-contained JSON schema of the research response.

    Deliberately *ref-free* (no ``$ref``/``$defs``): a Bazaar discovery extension
    nests this under ``output.example``, where Pydantic's ``$ref`` pointers would
    dangle and fail the indexer's validator. Inline definitions keep it valid
    standalone — discovery wants the shape, not a strict full validator. The exact
    field set still mirrors :class:`jim.research.schemas.ResearchResponse`.
    """
    citation = {
        "type": "object",
        "properties": {
            "id": {"type": "string"},
            "label": {"type": "string"},
            "value": {"type": "number"},
            "unit": {"type": "string"},
            "is_derived": {"type": "boolean"},
            "accession": {"type": ["string", "null"]},
            "source_url": {"type": ["string", "null"]},
        },
        "required": ["id", "label", "value", "unit"],
    }
    return {
        "type": "object",
        "properties": {
            "product": {"type": "string"},
            "ticker": {"type": "string"},
            "company": {"type": ["string", "null"]},
            "as_of": {"type": ["string", "null"]},
            "mode": {"type": "string", "enum": ["human", "agent"]},
            "status": {"type": "string", "description": '"ok" once the sourcing gate passes'},
            "memo": {"type": ["string", "null"]},
            "citations": {"type": "array", "items": citation},
            "sourcing": {
                "type": ["object", "null"],
                "properties": {
                    "passed": {"type": "boolean"},
                    "coverage": {"type": "number"},
                    "figures_checked": {"type": "integer"},
                    "figures_covered": {"type": "integer"},
                },
            },
            "cost": {
                "type": "object",
                "properties": {
                    "price_out_usd": {"type": "number"},
                    "data_cost_usd": {"type": "number"},
                    "inference_cost_usd": {"type": "number"},
                    "margin_usd": {"type": "number"},
                    "cache_hit": {"type": "boolean"},
                },
            },
            "disclaimer": {"type": "string"},
        },
        "required": ["product", "ticker", "status", "memo", "citations", "sourcing", "cost"],
    }


def _output_example(product: str, identifier: str) -> dict:
    """A compact, illustrative success envelope (not a live run)."""
    return {
        "product": product,
        "ticker": identifier,
        "status": "ok",
        "memo": "… cited prose; every figure carries a [C#] citation …",
        "citations": [
            {"id": "C1", "label": "Revenue", "value": 0, "unit": "USD", "is_derived": False}
        ],
        "sourcing": {"passed": True, "coverage": 1.0},
        "cost": {"price_out_usd": 0, "margin_usd": 0},
    }


@dataclass(frozen=True)
class Listing:
    product: str
    title: str
    description: str
    verb: str
    path: str
    identifier_param: str
    identifier_example: str
    price_usd: float
    tiers: list[PricingTier]
    tags: list[str]
    source_name: str
    upstream: str
    paid_upstream: bool
    input_schema: dict = field(default_factory=dict)
    output_schema: dict = field(default_factory=dict)

    @property
    def route_key(self) -> str:
        return f"{self.verb} {self.path}"

    @property
    def input_example(self) -> dict:
        return {self.identifier_param: self.identifier_example, "mode": "human"}

    def resource_url(self, base_url: str) -> str:
        return f"{base_url.rstrip('/')}{self.path}"

    def bazaar_extension(self) -> dict:
        """The x402 Bazaar discovery extension for this route's ``accepts``.

        Declares how to call the endpoint (query params + example) and what comes
        back (output example + schema). Indexers read this off the 402 challenge.
        """
        ext = _build_query_extension(
            input_example=self.input_example,
            input_schema=self.input_schema,
            output_example=_output_example(self.product, self.identifier_example),
            output_schema=self.output_schema,
        )
        # The server extension enriches `method` at request time, but declaring it
        # up-front makes the at-rest extension valid against its own schema (the
        # method field is required), avoiding a benign startup validation warning.
        ext["bazaar"]["info"]["input"]["method"] = self.verb
        return ext

    def to_dict(self, base_url: str | None = None) -> dict:
        d = {
            "product": self.product,
            "title": self.title,
            "description": self.description,
            "method": self.verb,
            "path": self.path,
            "identifier": {"param": self.identifier_param, "example": self.identifier_example},
            "price_usd": self.price_usd,
            "tiers": [t.to_dict() for t in self.tiers],
            "tags": self.tags,
            "source": self.source_name,
            "upstream": self.upstream,
            "paid_upstream": self.paid_upstream,
            "input_schema": self.input_schema,
            "output_schema": self.output_schema,
        }
        if base_url:
            d["resource"] = self.resource_url(base_url)
        return d


def _input_schema(param: str) -> dict:
    return {
        "type": "object",
        "properties": {
            param: {"type": "string", "description": f"The {param} to research."},
            "mode": {
                "type": "string",
                "enum": ["human", "agent"],
                "default": "human",
                "description": "human = narrative prose; agent = terse, metric-dense.",
            },
        },
        "required": [param],
        "additionalProperties": False,
    }


def build_catalog() -> list[Listing]:
    """Every sellable product as a :class:`Listing` (the marketplace)."""
    products = get_products()
    out: list[Listing] = []
    for name in product_names():
        spec = _SPECS[name]
        product = products[name]
        out.append(
            Listing(
                product=name,
                title=spec["title"],
                description=spec["description"],
                verb=spec["verb"],
                path=spec["path"],
                identifier_param=spec["param"],
                identifier_example=spec["param_example"],
                price_usd=product.price_out_usd,
                tiers=tiers_for(name),
                tags=spec["tags"],
                source_name=product.source.name,
                upstream=spec["upstream"],
                paid_upstream=bool(getattr(product.source, "is_paid", False)),
                input_schema=_input_schema(spec["param"]),
                output_schema=_output_schema(),
            )
        )
    return out


def listing_for(product: str) -> Listing | None:
    return next((listing for listing in build_catalog() if listing.product == product), None)


# --- Bazaar extension construction ------------------------------------------
#
# Prefer the official x402 helper (which also enables startup validation +
# request-time enrichment). If the ``extensions`` extra (jsonschema) is missing,
# fall back to a hand-built dict in the *identical* v2 shape so discovery still
# works — the same graceful-degradation pattern jim uses for langfuse/anthropic.

try:  # pragma: no cover - exercised indirectly
    from x402.extensions.bazaar import OutputConfig as _OutputConfig
    from x402.extensions.bazaar import declare_discovery_extension as _declare
except Exception:  # ImportError if x402[extensions]/jsonschema is absent
    _declare = None
    _OutputConfig = None


def _build_query_extension(
    *, input_example: dict, input_schema: dict, output_example: dict, output_schema: dict
) -> dict:
    if _declare is not None and _OutputConfig is not None:
        return _declare(
            input=input_example,
            input_schema=input_schema,
            output=_OutputConfig(example=output_example, schema=output_schema),
        )
    # Fallback: the v2 query-discovery shape (see x402 bazaar.resource_service).
    return {
        "bazaar": {
            "info": {
                "input": {"type": "http", "queryParams": input_example},
                "output": {"type": "json", "example": output_example},
            },
            "schema": {
                "$schema": "https://json-schema.org/draft/2020-12/schema",
                "type": "object",
                "properties": {
                    "input": {
                        "type": "object",
                        "properties": {
                            "type": {"type": "string", "const": "http"},
                            "method": {"type": "string", "enum": ["GET", "HEAD", "DELETE"]},
                            "queryParams": {"type": "object", **input_schema},
                        },
                        "required": ["type", "method"],
                        "additionalProperties": False,
                    },
                    "output": {
                        "type": "object",
                        "properties": {
                            "type": {"type": "string"},
                            "example": {"type": "object", **output_schema},
                        },
                        "required": ["type"],
                    },
                },
                "required": ["input"],
            },
        }
    }
