"""Seller: FastAPI app serving paywalled endpoints over x402 V2.

Phase 0 proved the cycle with ``/ping``:

    client GET /ping
      -> 402 PAYMENT REQUIRED  (server advertises accepts[])
      -> client signs an EXACT-EVM USDC authorization
      -> server /verify + /settle via the facilitator
      -> 200 with X-PAYMENT-RESPONSE header (settlement receipt)

Phase 1 adds the first real product: ``/research/fundamentals`` — a fully-cited
company fundamentals memo from SEC EDGAR, priced per call.

``/health`` is free so you can confirm the server is up without paying.
"""

from __future__ import annotations

import contextlib

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, PlainTextResponse
from pydantic import BaseModel, Field

from x402.http import PaymentOption
from x402.http.middleware.fastapi import PaymentMiddlewareASGI
from x402.http.paywall import create_paywall, evm_paywall
from x402.http.types import RouteConfig
from x402.mechanisms.evm.exact import ExactEvmServerScheme
from x402.server import x402ResourceServer

from jim.marketplace.facilitator import build_facilitator_client

from jim.admin import admin_dashboard
from jim.config import Settings, get_settings
from jim.dashboard import margin_dashboard
from jim.seller.audit import PaymentAuditMiddleware
from jim.marketplace.catalog import build_catalog, listing_for
from jim.marketplace.discovery import discovery_manifest
from jim.marketplace.mainnet import check_mainnet_readiness
from jim.marketplace.pricing import pricing_schedule
from jim.marketplace.sysmap import to_html as sysmap_html
from jim.marketplace.sysmap import to_json as sysmap_json
from jim.marketplace.sysmap import to_mermaid as sysmap_mermaid
from jim.marketplace.ui import checkout as ui_checkout
from jim.marketplace.ui import storefront_html
from jim.monitors.create import create_monitor
from jim.monitors.engine import run_monitor_once
from jim.monitors.models import Monitor
from jim.research.engine import run_research
from jim.research.schemas import FundamentalsResponse, ResearchResponse
from jim.store import get_store
from jim.vendor import build_mock_response


class PingResponse(BaseModel):
    ok: bool = True
    message: str = "pong — you paid for this packet"
    network: str


class CheckoutRequest(BaseModel):
    """Body for ``POST /ui/checkout`` — the human UI's research request."""

    product: str = Field(default="fundamentals", pattern="^(fundamentals|token|macro)$")
    identifier: str
    mode: str = Field(default="human", pattern="^(human|agent)$")
    settle: bool | None = Field(
        default=None,
        description="Force a real x402 self-settlement (None = follow UI_SETTLE_VIA_X402).",
    )


class MonitorCreate(BaseModel):
    """Body for ``POST /monitors`` — mirrors the ``jim-monitor add`` arguments."""

    identifier: str
    product: str | None = Field(default=None, pattern="^(fundamentals|token)$")
    mode: str | None = Field(default=None, pattern="^(human|agent)$")
    every: str | None = Field(default=None, description="Interval: 30m / 1h / 1d / seconds")
    watch: list[str] | None = Field(default=None, description="e.g. ['price:5','rsi:70/30','filing']")
    describe: str | None = Field(default=None, description="Natural-language request")
    channels: list[str] | None = Field(default=None, description="['console','webhook:https://...']")
    severity_floor: str | None = Field(default=None, pattern="^(info|notable|critical)$")
    cooldown: str | None = None


def build_app(settings: Settings | None = None) -> FastAPI:
    """Construct the seller app.

    Factory style so tests can inject settings and so later phases can mount
    additional paywalled research routes on the same server/facilitator.
    """
    settings = settings or get_settings()

    if not settings.evm_address:
        raise ValueError("EVM_ADDRESS is not set. Run `uv run jim-wallet new` and add it to .env.")

    lifespan = _scheduler_lifespan(settings) if settings.monitor_autostart else None
    app = FastAPI(
        title="jim — financial research (x402)",
        version="0.1.0",
        summary="Cited financial research, sold over x402",
        lifespan=lifespan,
    )

    # Wire the resource server to a facilitator and register the EXACT-EVM scheme
    # for our network. The facilitator does the on-chain verify + settle.
    facilitator = build_facilitator_client(settings)
    server = x402ResourceServer(facilitator)
    server.register(settings.network, ExactEvmServerScheme())

    def _pay(price: str) -> list[PaymentOption]:
        return [
            PaymentOption(
                scheme="exact",
                pay_to=settings.evm_address,
                price=price,
                network=settings.network,
            )
        ]

    def _product_route(product: str, price: str) -> RouteConfig:
        """A paid research route carrying Bazaar discovery metadata (Phase 5).

        The ``extensions`` declare the call shape + output so a Bazaar-speaking
        facilitator auto-indexes us on the first settlement; ``service_name`` /
        ``tags`` / ``icon_url`` enrich the listing. See ADR-0003.
        """
        listing = listing_for(product)
        return RouteConfig(
            accepts=_pay(price),
            mime_type="application/json",
            description=listing.description if listing else f"Cited {product} research.",
            service_name=settings.service_name,
            tags=(listing.tags[:5] if listing else None),
            icon_url=settings.service_icon_url,
            resource=listing.resource_url(settings.public_url) if listing else None,
            extensions=listing.bazaar_extension() if listing else None,
        )

    routes = {
        "GET /ping": RouteConfig(
            accepts=_pay(settings.ping_price),
            mime_type="application/json",
            description="A trivial paid ping that proves the x402 cycle works.",
        ),
        "GET /research/fundamentals": _product_route("fundamentals", settings.research_price),
        "GET /research/token": _product_route("token", settings.token_research_price),
        "GET /research/macro": _product_route("macro", settings.macro_research_price),
        # The testnet mock-Graph vendor: a PAID upstream that jim buys from.
        "POST /mock-graph/subgraphs/*": RouteConfig(
            accepts=_pay(settings.mock_graph_price),
            mime_type="application/json",
            description="Mock 'The Graph' vendor (testnet stand-in). Uniswap-v3 shape.",
        ),
    }

    # A browser that hits a paid route unpaid gets x402's bundled wallet paywall
    # (MetaMask / Coinbase Wallet / WalletConnect → real EIP-3009 settlement).
    # Agents (Accept: application/json) still get the machine-readable 402, so
    # this is purely additive for humans. See ADR-0005.
    paywall_provider = (
        create_paywall()
        .with_network(evm_paywall)
        .with_config(app_name=settings.service_name, testnet=not settings.is_mainnet)
        .build()
    )

    # Middleware order matters: the LAST `add_middleware` is the OUTERMOST. The
    # audit layer must wrap the payment layer so it can read the PAYMENT-RESPONSE
    # settlement header the payment layer writes *after* the handler returns.
    app.add_middleware(
        PaymentMiddlewareASGI, routes=routes, server=server, paywall_provider=paywall_provider
    )
    app.add_middleware(PaymentAuditMiddleware)

    @app.get("/health")
    async def health() -> dict[str, str]:
        """Free liveness probe — no payment required."""
        return {"status": "ok", "network": settings.network}

    @app.get("/ping", response_model=PingResponse)
    async def ping() -> PingResponse:
        """Paid endpoint. Reaching this handler means settlement succeeded."""
        return PingResponse(network=settings.network)

    @app.get("/research/fundamentals", response_model=FundamentalsResponse)
    async def fundamentals(
        ticker: str = Query(..., description="Stock ticker, e.g. AAPL"),
        mode: str = Query("human", pattern="^(human|agent)$"),
    ) -> FundamentalsResponse:
        """Paid. Returns a cited fundamentals memo; reaching here means settled.

        The sourcing gate runs *before* the customer is charged a second time:
        the payment buys a run, and we only return ``status="ok"`` when 100% of
        figures resolved to an EDGAR citation. A gate-rejected run still returns
        the diagnostics (impersonal, no fabricated numbers).
        """
        result = await run_research(ticker, product="fundamentals", mode=mode)
        if result.status == "error":
            raise HTTPException(status_code=422, detail=result.error)
        return FundamentalsResponse.from_result(result)

    @app.get("/research/token", response_model=ResearchResponse)
    async def token(
        token: str = Query(..., description="Token symbol or 0x address, e.g. WETH"),
        mode: str = Query("human", pattern="^(human|agent)$"),
    ) -> ResearchResponse:
        """Paid. Cited on-chain token memo; jim buys upstream data over x402.

        This is the two-sided product: the customer's payment (price_out) funds
        a run in which jim itself pays The Graph (cost_in) for the underlying
        data. Margin = price_out − data_cost − inference_cost (see /dashboard).
        """
        result = await run_research(token, product="token", mode=mode)
        if result.status == "error":
            raise HTTPException(status_code=422, detail=result.error)
        return ResearchResponse.from_result(result)

    @app.get("/research/macro", response_model=ResearchResponse)
    async def macro(
        region: str = Query("US", description="Region (US only today)"),
        mode: str = Query("human", pattern="^(human|agent)$"),
    ) -> ResearchResponse:
        """Paid. A cited US macro snapshot (Fed funds, CPI, Treasury yields).

        Free, public-domain upstream (Fed/BLS/Treasury), so this is pure margin
        like fundamentals — no buy leg. Reaching here means settlement succeeded.
        """
        result = await run_research(region, product="macro", mode=mode)
        if result.status == "error":
            raise HTTPException(status_code=422, detail=result.error)
        return ResearchResponse.from_result(result)

    @app.post("/mock-graph/subgraphs/id/{subgraph_id}")
    async def mock_graph(subgraph_id: str, request: Request) -> dict:
        """Paid testnet vendor: returns Uniswap-v3-shaped data for a token query."""
        body = await request.json()
        return build_mock_response(body.get("query", ""))

    @app.get("/dashboard")
    async def dashboard() -> dict:
        """Free. Per-query margin: revenue − data cost − inference cost."""
        return await margin_dashboard()

    @app.get("/admin/audit")
    async def admin_audit(limit: int = Query(50, ge=1, le=500)) -> dict:
        """Free. The settlement audit trail: revenue, buyers, on-chain receipts."""
        return await admin_dashboard(limit)

    @app.get("/admin", response_class=HTMLResponse)
    async def admin() -> str:
        """Free. The admin dashboard (settlements + on-chain audit), in the browser."""
        from jim.admin import admin_html

        return admin_html(await admin_dashboard(), settings.service_name)

    # --- Phase 5: marketplace, discovery, the human UI, the system map -------

    @app.get("/catalog")
    async def catalog() -> dict:
        """Free. Machine-readable product list (the marketplace)."""
        base = settings.public_url
        return {
            "service": settings.service_name,
            "network": settings.network,
            "products": [listing.to_dict(base) for listing in build_catalog()],
        }

    @app.get("/pricing")
    async def pricing() -> dict:
        """Free. The published pricing schedule (tiers per product)."""
        return {"network": settings.network, "pricing": pricing_schedule()}

    @app.get("/.well-known/x402")
    async def well_known_x402(request: Request) -> dict:
        """Free. The discovery manifest agents fetch to learn how to pay us."""
        return discovery_manifest(_request_base_url(request, settings))

    @app.get("/mainnet/readiness")
    async def mainnet_readiness() -> dict:
        """Free. The mainnet-cutover preflight (reads state; moves no money)."""
        return (await check_mainnet_readiness(settings)).to_dict()

    @app.get("/map", response_class=HTMLResponse)
    async def system_map() -> str:
        """Free. The live system map, rendered in the browser (mermaid.js)."""
        return sysmap_html(settings=settings)

    @app.get("/map.mmd", response_class=PlainTextResponse)
    async def system_map_mermaid() -> str:
        """Free. Raw Mermaid source for the live system map."""
        return sysmap_mermaid(settings=settings)

    @app.get("/map.json")
    async def system_map_json() -> dict:
        """Free. The system map as a structured node/edge graph."""
        return sysmap_json(settings=settings)

    @app.get("/", response_class=HTMLResponse)
    async def storefront() -> str:
        """Free. A thin human UI that pays for research via x402 under the hood."""
        return storefront_html(settings)

    @app.post("/ui/checkout")
    async def ui_checkout_route(body: CheckoutRequest) -> dict:
        """Run (and, when funded, x402-settle) one research call for the UI."""
        return await ui_checkout(
            product=body.product,
            identifier=body.identifier,
            mode=body.mode,
            settle=body.settle,
        )

    # --- Phase 4: monitor management (free; the *push* is the product) -------

    @app.get("/monitors")
    async def list_monitors() -> dict:
        """Free. All configured monitors with their crew + schedule state."""
        return {"monitors": await get_store().list_monitors()}

    @app.post("/monitors")
    async def add_monitor(body: MonitorCreate) -> dict:
        """Free. Create a monitor (triggers from `watch` specs or `describe`)."""
        monitor = await create_monitor(
            body.identifier,
            product=body.product,
            mode=body.mode,
            every=body.every,
            watch=body.watch,
            describe=body.describe,
            channels=body.channels,
            severity_floor=body.severity_floor,
            cooldown=body.cooldown,
        )
        await get_store().save_monitor(monitor.to_row())
        return monitor.to_row()

    @app.get("/monitors/feed")
    async def monitors_feed(limit: int = Query(20, ge=1, le=200)) -> dict:
        """Free. Recent material updates across all monitors (impersonal, cited)."""
        store = get_store()
        return {"feed": await store.monitor_feed(limit=limit), "stats": await store.monitor_stats()}

    @app.get("/monitors/{monitor_id}")
    async def get_monitor(monitor_id: str) -> dict:
        row = await get_store().get_monitor(monitor_id)
        if not row:
            raise HTTPException(status_code=404, detail=f"No monitor {monitor_id}")
        return row

    @app.delete("/monitors/{monitor_id}")
    async def delete_monitor(monitor_id: str) -> dict:
        ok = await get_store().delete_monitor(monitor_id)
        if not ok:
            raise HTTPException(status_code=404, detail=f"No monitor {monitor_id}")
        return {"deleted": monitor_id}

    @app.post("/monitors/{monitor_id}/run")
    async def run_monitor(monitor_id: str) -> dict:
        """Free. Run one cycle now (gather → diff → crew → maybe push)."""
        store = get_store()
        row = await store.get_monitor(monitor_id)
        if not row:
            raise HTTPException(status_code=404, detail=f"No monitor {monitor_id}")
        run = await run_monitor_once(Monitor.from_row(row), store=store, deliver=True)
        return run.to_row()

    return app


def _request_base_url(request: Request, settings: Settings) -> str:
    """Prefer a configured public URL; otherwise reflect the request's own host
    so a manifest fetched at any address advertises reachable resource URLs."""
    if settings.public_base_url:
        return settings.public_base_url.rstrip("/")
    return str(request.base_url).rstrip("/")


def _scheduler_lifespan(settings: Settings):
    """FastAPI lifespan that runs the monitor scheduler in-process (autostart)."""
    import asyncio
    from contextlib import asynccontextmanager

    from jim.monitors.scheduler import MonitorScheduler

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        sched = MonitorScheduler()
        task = asyncio.create_task(sched.run_forever())
        try:
            yield
        finally:
            sched.stop()
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    return lifespan


def run() -> None:
    """Console entry point: ``uv run jim-seller``."""
    import uvicorn

    settings = get_settings()
    uvicorn.run(
        build_app(settings),
        host=settings.seller_host,
        port=settings.seller_port,
    )


if __name__ == "__main__":
    run()
