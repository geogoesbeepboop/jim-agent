"""Phase 5 marketplace surface — catalog, pricing tiers, Bazaar extensions.

All offline: the catalog/pricing/discovery surface is pure (config + product
registry), so these need no wallet, network, API key, or DB.
"""

from __future__ import annotations

from jim.marketplace import (
    build_catalog,
    discovery_manifest,
    listing_for,
    price_for,
    pricing_schedule,
    product_names,
    tiers_for,
)
from jim.marketplace.catalog import _output_schema
from jim.marketplace.mcp_server import mcp_tool_catalog, mcp_tool_name


def test_catalog_lists_both_products() -> None:
    products = {listing.product for listing in build_catalog()}
    assert products == {"fundamentals", "token"}


def test_listing_carries_call_shape_and_source() -> None:
    f = listing_for("fundamentals")
    assert f is not None
    assert f.route_key == "GET /research/fundamentals"
    assert f.identifier_param == "ticker"
    assert f.paid_upstream is False  # EDGAR is free
    t = listing_for("token")
    assert t is not None
    assert t.paid_upstream is True  # The Graph is paid over x402


def test_bazaar_extension_has_v2_shape() -> None:
    listing = listing_for("fundamentals")
    ext = listing.bazaar_extension()
    assert set(ext) == {"bazaar"}
    info = ext["bazaar"]["info"]
    assert info["input"]["type"] == "http"
    assert "ticker" in info["input"]["queryParams"]
    assert info["output"]["type"] == "json"
    # The advertised schema must be self-contained (no dangling $ref) so an
    # indexer's validator accepts it.
    dumped = str(ext["bazaar"]["schema"])
    assert "$ref" not in dumped


def test_output_schema_is_ref_free_and_describes_the_contract() -> None:
    schema = _output_schema()
    assert "$ref" not in str(schema)
    props = schema["properties"]
    for field in ("product", "status", "memo", "citations", "sourcing", "cost"):
        assert field in props


def test_pricing_tiers_are_ordered_discounts() -> None:
    for product in product_names():
        tiers = {t.name: t for t in tiers_for(product)}
        assert set(tiers) == {"oneshot", "agent", "bundle", "monitor"}
        base = tiers["oneshot"].price_usd
        # Discounts never raise the price above the headline oneshot price.
        assert tiers["agent"].price_usd <= base
        assert tiers["bundle"].price_usd <= base
        assert price_for(product, "oneshot") == base
        # Unknown tier falls back to oneshot.
        assert price_for(product, "nonsense") == base


def test_pricing_schedule_covers_every_product() -> None:
    schedule = pricing_schedule()
    assert set(schedule) == set(product_names())
    for tiers in schedule.values():
        assert {t["name"] for t in tiers} == {"oneshot", "agent", "bundle", "monitor"}


def test_discovery_manifest_is_self_describing() -> None:
    m = discovery_manifest("https://jim.example")
    assert m["x402Version"] == 2
    assert m["asset"]["symbol"] == "USDC" and m["asset"]["decimals"] == 6
    assert {r["product"] for r in m["resources"]} == {"fundamentals", "token"}
    # Resource URLs are absolute against the advertised base.
    assert all(r["resource"].startswith("https://jim.example/") for r in m["resources"])
    assert m["mcp"]["tools"] == ["research_fundamentals", "research_token"]


def test_discovery_manifest_is_deterministic() -> None:
    assert discovery_manifest("https://jim.example") == discovery_manifest("https://jim.example")


def test_mcp_tool_catalog_mirrors_products() -> None:
    tools = mcp_tool_catalog()
    assert {t["name"] for t in tools} == {mcp_tool_name("fundamentals"), mcp_tool_name("token")}
    for t in tools:
        assert t["price_usd"] > 0
        assert "ticker" in t["input_schema"]["properties"] or "token" in t["input_schema"]["properties"]
