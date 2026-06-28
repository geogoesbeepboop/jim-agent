"""Central configuration, loaded from environment / .env.

Phase 0 keeps this small: a wallet key, the network, and the facilitator URL.
Later phases extend Settings with model keys, Langfuse, Postgres, etc.
"""

from __future__ import annotations

from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

# Base Sepolia testnet. x402 V2 uses CAIP-2 chain identifiers.
BASE_SEPOLIA: str = "eip155:84532"
BASE_MAINNET: str = "eip155:8453"

# USDC contract on Base Sepolia (testnet). Used as the default settlement asset.
BASE_SEPOLIA_USDC: str = "0x036CbD53842c5426634e7929541eC2318f3dCF7e"
# Circle-native USDC on Base mainnet (6 decimals) — the Phase 5 settlement asset.
BASE_MAINNET_USDC: str = "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # --- Network / facilitator ---------------------------------------------
    network: str = Field(default=BASE_SEPOLIA, description="CAIP-2 network id")
    facilitator_url: str = Field(
        default="https://x402.org/facilitator",
        description="x402 facilitator that verifies + settles payments",
    )

    # --- Seller side --------------------------------------------------------
    # The address that receives payments for our paywalled endpoints.
    evm_address: str | None = Field(default=None, description="Our pay-to address")
    seller_host: str = "0.0.0.0"
    seller_port: int = 4021

    # --- Buyer side ---------------------------------------------------------
    # Private key we use to PAY for upstream data (and to self-test our seller).
    evm_private_key: str | None = Field(default=None, description="Buyer wallet key")

    # Default price for the Phase 0 /ping probe.
    ping_price: str = "$0.01"

    # --- Phase 1: research engine ------------------------------------------
    # SEC requires a descriptive UA with contact info on every request.
    sec_user_agent: str = "jim-agent georgeandrade93@gmail.com"
    # Price of one fundamentals research call.
    research_price: str = "$0.25"
    anthropic_api_key: str | None = Field(default=None, description="Synthesizer/judge key")
    research_model: str = "claude-sonnet-4-6"
    judge_model: str = "claude-haiku-4-5-20251001"
    research_max_attempts: int = 2  # synthesize retries on a gate failure
    enable_judge: bool = True
    judge_threshold: float = 0.8  # faithfulness score below this fails the run

    # --- Phase 3: expanded metrics + adversarial debate --------------------
    enable_prices: bool = True  # enrich equities with market/technical metrics
    enable_debate: bool = True  # bull/bear/judge before synthesis
    debate_model: str = "claude-sonnet-4-6"

    # --- Phase 2: buy side + margin engine ---------------------------------
    # Postgres+pgvector for cache + margin ledger. Unset → in-memory store.
    database_url: str | None = Field(default=None, description="postgresql+asyncpg://...")

    # The Graph x402 gateway (a PAID upstream source).
    #   GRAPH_LIVE=false → buy from our local mock on Base Sepolia (free testnet USDC)
    #   GRAPH_LIVE=true  → buy from the real gateway on Base mainnet (REAL USDC)
    graph_live: bool = False
    graph_gateway_url: str = "https://gateway.thegraph.com/api/x402"
    # Uniswap v3 (Ethereum mainnet) subgraph — rich token/price/volume data.
    graph_subgraph_id: str = "5zvR82QoaXYFyDEKLZ9t6v9adgnptxYpKpSbxtgVENFV"
    graph_network: str = BASE_MAINNET  # where The Graph settles payment
    # Optional separate key for mainnet Graph buys; defaults to evm_private_key.
    graph_evm_private_key: str | None = None

    # Local mock-Graph vendor (testnet stand-in for the real gateway).
    mock_graph_price: str = "$0.01"
    # Who receives mock-vendor payments; defaults to our own address.
    vendor_address: str | None = None

    # Pricing + economics.
    token_research_price: str = "$0.50"  # price_out for an on-chain token memo
    per_query_budget_usd: float = 0.10  # hard ceiling on DATA spend per query
    purchase_cache_ttl_seconds: int = 86_400  # re-buy a datum at most once/day

    # --- Phase 4: continuous monitors (the "motley crew") ------------------
    # A monitor re-runs research on a schedule, diffs against its last baseline,
    # and only pays to write — and only pushes — when a deterministic rule fires.
    monitor_default_interval_seconds: int = 86_400  # poll cadence for a new monitor
    monitor_poll_seconds: int = 60  # scheduler tick: how often we check for due monitors
    monitor_max_concurrency: int = 4  # due monitors run with this much parallelism
    monitor_cooldown_seconds: int = 21_600  # suppress a repeat of the SAME signal for 6h
    monitor_update_price: str = "$0.10"  # price_out booked for one delivered update
    monitor_default_mode: str = "agent"  # updates default to terse/metric-dense prose
    monitor_autostart: bool = False  # if true, the seller runs the scheduler in-process
    # HMAC secret for signing webhook deliveries (subscribers verify authenticity).
    # Defaults to the seller key so a deployment signs out of the box; override in prod.
    monitor_webhook_secret: str | None = None
    monitor_webhook_timeout_seconds: float = 10.0

    # Default materiality thresholds (deterministic; per-monitor overrides allowed).
    monitor_price_move_pct: float = 5.0  # |price move| ≥ this %  → notable
    monitor_metric_change_pct: float = 10.0  # |any metric move| ≥ this % → notable
    monitor_rsi_overbought: float = 70.0  # RSI ≥ this → overbought cross
    monitor_rsi_oversold: float = 30.0  # RSI ≤ this → oversold cross

    # --- Phase 5: marketplace, discovery, mainnet --------------------------
    # Public service identity, surfaced in the catalog + Bazaar discovery so
    # other agents can find us. Keep names short / tag sets small — indexers cap
    # service_name ≤ 32 chars and ≤ 5 tags (see x402 RouteConfig docs).
    service_name: str = "jim"
    service_description: str = (
        "Impersonal, fully-cited financial research sold over x402. "
        "Every figure traces to a public-domain primary source."
    )
    service_icon_url: str | None = None
    service_tags: list[str] = Field(
        default_factory=lambda: ["finance", "research", "cited", "edgar", "defi"]
    )
    # Absolute base URL advertised in the discovery manifest + Bazaar resource
    # URLs. Unset → derived from seller host/port via `public_url`.
    public_base_url: str | None = None

    # Human UI: when true, POST /ui/checkout settles a REAL x402 payment by having
    # jim buy its own endpoint (proves the rail browser-side without a wallet);
    # when false it runs research directly and labels the result a preview.
    ui_settle_via_x402: bool = False

    # Published pricing tiers (deterministic; derived from the base per-call price).
    # The agent tier is a small machine-buyer discount; the bundle tier prices a
    # multi-identifier request per item with the same discount.
    agent_tier_discount_pct: float = 10.0
    bundle_tier_discount_pct: float = 20.0
    bundle_max_items: int = 10

    # MCP server (jim as an MCP server: agents discover + pay our tools over MCP).
    mcp_host: str = "0.0.0.0"
    mcp_port: int = 4022

    # Mainnet cutover guardrails. An optional read-only RPC lets the readiness
    # preflight report on-chain ETH/USDC balances; everything else is offline.
    mainnet_rpc_url: str | None = Field(default=None, description="Read-only Base RPC for balances")
    # Facilitator economics to surface in the preflight (operator-provided, since
    # the free testnet facilitator and a production facilitator differ).
    facilitator_min_usdc: float = 0.0  # smallest settleable amount, if any
    facilitator_fee_bps: float = 0.0  # facilitator fee in basis points, if any

    @property
    def is_mainnet(self) -> bool:
        return self.network == BASE_MAINNET

    @property
    def usdc_address(self) -> str:
        """The USDC settlement asset for the active sell-side network."""
        return BASE_MAINNET_USDC if self.is_mainnet else BASE_SEPOLIA_USDC

    @property
    def public_url(self) -> str:
        """Absolute base URL for discovery (manifest + Bazaar resource URLs)."""
        if self.public_base_url:
            return self.public_base_url.rstrip("/")
        host = "localhost" if self.seller_host in ("0.0.0.0", "") else self.seller_host
        return f"http://{host}:{self.seller_port}"

    @property
    def monitor_signing_secret(self) -> str | None:
        return self.monitor_webhook_secret or self.evm_private_key

    @property
    def graph_buy_key(self) -> str | None:
        return self.graph_evm_private_key or self.evm_private_key

    @property
    def graph_buy_network(self) -> str:
        return self.graph_network if self.graph_live else self.network


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    """Cached singleton so we parse the environment once per process."""
    return Settings()
