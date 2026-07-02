"""x402 buy client.

Wraps the V2 ``x402HttpxClient`` so the rest of the codebase can make a paid
request without touching scheme registration each time. Returns the body, the
settlement receipt, and — crucially for the margin engine — how much we paid
(``cost_in``) and the settlement tx hash.

We learn the price with a cheap unpaid pre-flight that reads the advertised
``payment-required`` header, then make the paying request. The pre-flight returns
402 before the seller does any work, so it's effectively free.
"""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from typing import Any

import httpx
from eth_account import Account

from x402 import x402Client
from x402.http import x402HTTPClient
from x402.http.clients import x402HttpxClient
from x402.mechanisms.evm import EthAccountSigner
from x402.mechanisms.evm.exact.register import register_exact_evm_client

from jim.config import get_settings
from jim.interop.callchain import outbound_payment_headers

# USDC and most x402 stablecoins use 6 decimals.
_USDC_DECIMALS = 6
_EPS = 1e-9


class PriceCapExceeded(RuntimeError):
    """The seller's advertised price exceeded the caller's cap — refused to pay.

    Raised from the unpaid pre-flight, *before* any settlement, so no money moves.
    The on-chain x402 price is set dynamically in the 402 header (the seller can
    name any amount), so this guard is what keeps jim from overpaying for data —
    especially on mainnet, where the price is real USDC and not pre-published.
    """

    def __init__(self, *, advertised_usd: float, cap_usd: float, url: str):
        self.advertised_usd = advertised_usd
        self.cap_usd = cap_usd
        super().__init__(
            f"advertised ${advertised_usd:.6f} exceeds price cap ${cap_usd:.6f} for {url}"
        )


@dataclass
class PaidResponse:
    """Result of a paid request."""

    status_code: int
    text: str
    settlement: dict[str, Any] | None  # parsed PAYMENT-RESPONSE, if present
    cost_in_usd: float = 0.0  # what we paid (0 if the resource was free)
    tx_hash: str | None = None

    @property
    def paid(self) -> bool:
        return self.settlement is not None

    def json(self) -> Any:
        return json.loads(self.text)


def _build_client(private_key: str) -> x402Client:
    """Create an x402 client with the EXACT-EVM scheme registered for our key."""
    client = x402Client()
    account = Account.from_key(private_key)
    register_exact_evm_client(client, EthAccountSigner(account))
    return client


def _decode_advertised_price(headers: httpx.Headers) -> float:
    """Read the USD price from a 402's `payment-required` header (0 if absent)."""
    raw = headers.get("payment-required")
    if not raw:
        return 0.0
    try:
        challenge = json.loads(base64.b64decode(raw))
        accept = challenge["accepts"][0]
        # `amount` is in the asset's base units (USDC = 6 decimals).
        return int(accept["amount"]) / (10**_USDC_DECIMALS)
    except (ValueError, KeyError, IndexError):
        return 0.0


def _extract_tx_hash(settlement: dict[str, Any] | None) -> str | None:
    if not settlement:
        return None
    for key in ("transaction", "txHash", "tx_hash", "transactionHash"):
        val = settlement.get(key)
        if isinstance(val, str) and val:
            return val
    return None


async def pay(
    url: str,
    *,
    method: str = "GET",
    json_body: dict | None = None,
    headers: dict | None = None,
    private_key: str | None = None,
    timeout: float = 180.0,
    max_price_usd: float | None = None,
) -> PaidResponse:
    """Make a paid request to an x402 resource, returning body + economics.

    Args:
        url: Fully-qualified URL of the protected resource.
        method: HTTP method ("GET", "POST", ...).
        json_body: JSON body (e.g. a GraphQL query for The Graph).
        headers: Extra request headers.
        private_key: EVM key to pay with; defaults to ``EVM_PRIVATE_KEY``.
        timeout: Read timeout — a paid call buys *work*, so this is generous.
        max_price_usd: Hard cap on the advertised price. If the 402 names a price
            above this, raise :class:`PriceCapExceeded` from the pre-flight and
            never pay. ``None`` disables the guard.
    """
    settings = get_settings()
    key = private_key or settings.evm_private_key
    if not key:
        raise ValueError(
            "No EVM_PRIVATE_KEY available. Run `uv run jim-wallet new` and set it in .env."
        )

    # Cross-agent spend safety (Phase 7): every buy carries the call chain with
    # our identity appended, so an honest peer can refuse loops, and we refuse
    # to extend past the depth ceiling (raises CallChainDepthExceeded) before
    # any money moves.
    own_identity = settings.evm_address or Account.from_key(key).address
    headers = {
        **outbound_payment_headers(own_identity, settings.call_chain_max_depth),
        **(headers or {}),
    }

    client = _build_client(key)
    http_client = x402HTTPClient(client)
    timeouts = httpx.Timeout(timeout, connect=10.0)

    # 1) Cheap unpaid pre-flight to learn the price (the seller returns 402 first).
    cost_in_usd = 0.0
    async with httpx.AsyncClient(timeout=httpx.Timeout(30.0, connect=10.0)) as probe:
        pre = await probe.request(method, url, json=json_body, headers=headers)
        if pre.status_code == 402:
            cost_in_usd = _decode_advertised_price(pre.headers)
            # Refuse to pay an over-cap price BEFORE the paying request settles.
            if max_price_usd is not None and cost_in_usd > max_price_usd + _EPS:
                raise PriceCapExceeded(
                    advertised_usd=cost_in_usd, cap_usd=max_price_usd, url=url
                )

    # 2) The paying request (x402 client handles 402 → sign → settle → retry).
    async with x402HttpxClient(client, timeout=timeouts) as http:
        response = await http.request(method, url, json=json_body, headers=headers)
        await response.aread()

        settlement: dict[str, Any] | None = None
        try:
            settle = http_client.get_payment_settle_response(
                lambda name: response.headers.get(name)
            )
            settlement = settle.model_dump()
        except ValueError:
            settlement = None  # free resource or no receipt

        # If the resource turned out to be free, we paid nothing.
        if settlement is None:
            cost_in_usd = 0.0

        return PaidResponse(
            status_code=response.status_code,
            text=response.text,
            settlement=settlement,
            cost_in_usd=cost_in_usd,
            tx_hash=_extract_tx_hash(settlement),
        )


async def fetch_paid(
    url: str,
    *,
    private_key: str | None = None,
    timeout: float = 180.0,
    max_price_usd: float | None = None,
) -> PaidResponse:
    """Backwards-compatible GET helper (used by the Phase 0 ping demo)."""
    return await pay(
        url, method="GET", private_key=private_key, timeout=timeout, max_price_usd=max_price_usd
    )
