"""jim as an MCP server (Phase 5) — agents discover *and pay* our tools over MCP.

The architecture doc (§9.3) called this out: expose ``research_fundamentals`` /
``research_token`` as MCP tools, with the x402 payment as the auth — the tool call
triggers the 402 → pay → settle cycle under the hood. This is the "other agents
auto-discover and pay us" story delivered over MCP alongside raw HTTP.

The trust boundary is unchanged: an MCP tool is just another caller of
``run_research``, so the **same** sourcing gate, budget, and impersonal guard
apply. MCP is a transport for tools (ARCHITECTURE §9.3); it plugs into "where the
call comes from", never into "what is allowed to ship".

``mcp`` is an optional dependency (``uv sync --extra mcp``), mirroring how langfuse
is optional for tracing. The *tool surface* (:func:`mcp_tool_catalog`) is pure and
always importable — discovery and tests read it without needing ``mcp`` installed.
Building live ``accepts`` and running the server need ``mcp`` plus a reachable
facilitator.
"""

from __future__ import annotations

import json

from jim.config import get_settings
from jim.marketplace.catalog import Listing, build_catalog


def mcp_tool_name(product: str) -> str:
    return f"research_{product}"


def mcp_tool_catalog() -> list[dict]:
    """The MCP tools jim exposes — pure, no ``mcp`` dependency.

    One tool per catalogued product, each x402-gated at the product's price.
    """
    tools = []
    for listing in build_catalog():
        tools.append(
            {
                "name": mcp_tool_name(listing.product),
                "title": listing.title,
                "description": listing.description,
                "price_usd": listing.price_usd,
                "input_schema": listing.input_schema,
                "resource": f"mcp://tool/{mcp_tool_name(listing.product)}",
            }
        )
    return tools


def _import_mcp():
    """Lazy import with a clear, actionable error if the extra is missing."""
    try:
        from mcp.server.fastmcp import FastMCP  # type: ignore

        from x402.mcp import create_payment_wrapper  # type: ignore
        from x402.schemas.payments import ResourceInfo  # type: ignore
    except ImportError as e:  # pragma: no cover - depends on optional extra
        raise RuntimeError(
            "The MCP server needs the 'mcp' extra. Install it with: "
            "uv sync --extra mcp"
        ) from e
    return FastMCP, create_payment_wrapper, ResourceInfo


def _build_resource_server(settings):
    """A facilitator-backed resource server with the EXACT-EVM scheme — same as
    the HTTP seller, so MCP and HTTP settle identically."""
    from x402.http import HTTPFacilitatorClient
    from x402.http.types import PaymentOption
    from x402.mechanisms.evm.exact import ExactEvmServerScheme
    from x402.server import x402ResourceServer

    from jim.marketplace.facilitator import build_facilitator_config

    facilitator = HTTPFacilitatorClient(build_facilitator_config(settings))
    server = x402ResourceServer(facilitator)
    server.register(settings.network, ExactEvmServerScheme())
    server.initialize()  # fetch supported kinds from the facilitator
    return server, PaymentOption


def _accepts_for(server, PaymentOption, settings, listing: Listing):
    """Build x402 ``accepts`` for one tool at the product's price."""
    option = PaymentOption(
        scheme="exact",
        pay_to=settings.evm_address,
        price=f"${listing.price_usd}",
        network=settings.network,
    )
    return server.build_payment_requirements(option)


def build_mcp_server():  # pragma: no cover - requires the optional 'mcp' extra
    """Construct the FastMCP server with one x402-gated tool per product."""
    settings = get_settings()
    if not settings.evm_address:
        raise ValueError("EVM_ADDRESS is not set. Run `uv run jim-wallet new` and add it to .env.")

    FastMCP, create_payment_wrapper, ResourceInfo = _import_mcp()
    from jim.research.engine import run_research
    from jim.research.schemas import ResearchResponse

    server, PaymentOption = _build_resource_server(settings)
    mcp = FastMCP(settings.service_name)

    for listing in build_catalog():
        accepts = _accepts_for(server, PaymentOption, settings, listing)
        resource = ResourceInfo(
            url=f"mcp://tool/{mcp_tool_name(listing.product)}",
            description=listing.description,
            service_name=settings.service_name,
            tags=listing.tags,
            icon_url=settings.service_icon_url,
        )
        wrapper = create_payment_wrapper(server, accepts=accepts, resource=resource)
        _register_tool(mcp, wrapper, listing, run_research, ResearchResponse)

    return mcp


def _register_tool(mcp, wrapper, listing: Listing, run_research, ResearchResponse):  # pragma: no cover
    """Register a single product as an x402-gated MCP tool."""
    product = listing.product
    param = listing.identifier_param

    @mcp.tool(name=mcp_tool_name(product), description=listing.description)
    @wrapper
    async def _tool(identifier: str, mode: str = "human") -> str:
        result = await run_research(identifier, product=product, mode=mode)
        return json.dumps(ResearchResponse.from_result(result).model_dump(), default=str)

    # Make the parameter name match the product's identifier (ticker/token).
    _tool.__doc__ = f"{listing.title}: pass `{param}` and optional `mode` (human|agent)."
    return _tool


def main() -> int:  # pragma: no cover - process entry point
    """``uv run jim-mcp`` — run the MCP server (stdio by default)."""
    import argparse

    p = argparse.ArgumentParser(prog="jim-mcp", description="Run jim as an x402-gated MCP server.")
    p.add_argument(
        "--transport",
        choices=["stdio", "http"],
        default="stdio",
        help="stdio (default, for desktop/IDE clients) or http (streamable-http).",
    )
    args = p.parse_args()

    settings = get_settings()
    mcp = build_mcp_server()
    if args.transport == "http":
        mcp.settings.host = settings.mcp_host
        mcp.settings.port = settings.mcp_port
        mcp.run(transport="streamable-http")
    else:
        mcp.run(transport="stdio")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
