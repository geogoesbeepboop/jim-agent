"""Settlement audit middleware — persist a receipt for every x402 payment.

The x402 payment middleware settles a payment *after* the protected handler
returns, writing the settlement receipt into the ``PAYMENT-RESPONSE`` response
header. So the only place to observe "this buyer paid this tx for this query"
is on the way back out, **outside** the payment middleware.

:class:`PaymentAuditMiddleware` sits outermost: it lets the request flow down to
the payment middleware + handler, then on the response decodes the settlement
header and records one append-only :class:`~jim.store.models.PaymentReceipt`
(buyer address + tx hash + settled amount + which query). It never mutates the
response and never raises into the response path — auditing is best-effort
observability and must not break a paid delivery.

The decode half (:func:`decode_settlement`, :func:`classify_request`) is pure and
header-only, so it unit-tests offline with no wallet, network, or facilitator.
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
from typing import Any

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

logger = logging.getLogger("jim.audit")

# x402 V2 settlement header is ``PAYMENT-RESPONSE``; ``X-PAYMENT-RESPONSE`` is the
# V1 legacy spelling. We read whichever the active facilitator emits.
_SETTLE_HEADERS = ("PAYMENT-RESPONSE", "X-PAYMENT-RESPONSE")

_USDC_DECIMALS = 6

# Which paid path maps to which product + the query param that carries its id.
_PATH_PRODUCTS: tuple[tuple[str, str, str | None], ...] = (
    ("/research/fundamentals", "fundamentals", "ticker"),
    ("/research/token", "token", "token"),
    ("/mock-graph", "mock-graph", None),
    ("/ping", "ping", None),
)


def _amount_to_usdc(raw: Any) -> float:
    """Coerce a settle-response ``amount`` into USDC.

    The exact-EVM scheme settles in base units (USDC = 6 decimals), so an
    integer-looking value (``"250000"``) is base units → divide by 1e6. A value
    that already carries a decimal point (``"0.25"``) is treated as USDC as-is.
    """
    if raw is None or raw == "":
        return 0.0
    try:
        text = str(raw).strip()
        if "." in text:
            return float(text)
        return int(text) / (10**_USDC_DECIMALS)
    except (ValueError, TypeError):
        return 0.0


def decode_settlement(header_value: str | None) -> dict[str, Any] | None:
    """Decode a base64 ``PAYMENT-RESPONSE`` header into a settlement dict.

    Returns ``None`` when the header is absent or unparseable (i.e. the request
    was free or never settled). The returned dict is normalised to the fields the
    audit log cares about: ``success``, ``payer``, ``transaction``, ``network``,
    ``amount`` (raw) and ``amount_usdc`` (coerced), plus the full ``raw`` body.
    """
    if not header_value:
        return None
    try:
        decoded = base64.b64decode(header_value)
        body = json.loads(decoded)
    except (binascii.Error, ValueError, TypeError):
        return None
    if not isinstance(body, dict):
        return None
    amount = body.get("amount")
    return {
        "success": bool(body.get("success", True)),
        "payer": body.get("payer"),
        "transaction": body.get("transaction"),
        "network": body.get("network"),
        "amount": amount,
        "amount_usdc": _amount_to_usdc(amount),
        "raw": body,
    }


def settlement_header(headers: Any) -> str | None:
    """Return the settlement header value (V2 then V1 spelling), if present."""
    for name in _SETTLE_HEADERS:
        value = headers.get(name)
        if value:
            return value
    return None


def classify_request(path: str, query_params: Any) -> tuple[str | None, str | None, str | None]:
    """Map a request path + query params → (product, identifier, mode).

    ``query_params`` is anything with ``.get(name)`` (Starlette QueryParams or a
    plain dict), so this is reusable from tests without a live request.
    """
    product: str | None = None
    id_param: str | None = None
    for prefix, prod, param in _PATH_PRODUCTS:
        if path == prefix or path.startswith(prefix + "/"):
            product, id_param = prod, param
            break
    identifier = query_params.get(id_param) if id_param else None
    mode = query_params.get("mode")
    return product, identifier, mode


class PaymentAuditMiddleware(BaseHTTPMiddleware):
    """Record a settlement receipt for every payment that clears our paywall.

    Mounted *after* the x402 payment middleware so it wraps it (outermost), which
    is what lets it see the ``PAYMENT-RESPONSE`` header the payment middleware
    adds during settlement.
    """

    async def dispatch(self, request: Request, call_next):
        response: Response = await call_next(request)
        try:
            await self._record(request, response)
        except Exception:  # never let auditing break a paid response
            logger.exception("payment audit failed for %s", request.url.path)
        return response

    async def _record(self, request: Request, response: Response) -> None:
        settlement = decode_settlement(settlement_header(response.headers))
        if settlement is None:
            return  # free route or no settlement happened

        from jim.store import get_store

        product, identifier, mode = classify_request(
            request.url.path, request.query_params
        )
        raw = settlement["raw"]
        await get_store().record_receipt(
            tx_hash=settlement["transaction"],
            payer=settlement["payer"],
            pay_to=raw.get("payTo") or raw.get("pay_to"),
            amount_usdc=settlement["amount_usdc"],
            network=settlement["network"] or "",
            path=request.url.path,
            product=product,
            identifier=(identifier.upper() if identifier else None),
            mode=mode,
            status_code=response.status_code,
            success=settlement["success"],
            receipt=raw,
        )
