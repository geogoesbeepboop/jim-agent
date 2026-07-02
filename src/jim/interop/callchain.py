"""Cross-agent spend safety: the propagated call chain (Phase 7).

When agents subcontract agents, two failure modes appear that a per-query
budget can't see: **payment loops** (A pays B pays A — each hop re-bills the
last) and **runaway depth** (a request tree that keeps fanning out). Both are
graph properties, so the defense is a graph primitive:

Every buy-side request carries ``X-Jim-Call-Chain`` — the ordered, comma-
separated list of agent identities (payment addresses) that funded the chain
so far, with our own identity appended. On the sell side a middleware checks
the *inbound* chain before any payment is verified:

  - our own address already in the chain → **409, refuse**: serving it would
    complete a payment cycle;
  - chain at/over ``CALL_CHAIN_MAX_DEPTH`` → **409, refuse**: the request tree
    is too deep to be a sane composition.

The refusal happens before the 402 challenge is answered, so no money moves.
Deterministic, header-driven, no model in the loop. An honest peer economy
propagates the header; a dishonest peer can strip it, which bounds *our*
spend and *our* participation in loops — exactly the part we control.
"""

from __future__ import annotations

import json
from contextvars import ContextVar
from dataclasses import dataclass

CALL_CHAIN_HEADER = "X-Jim-Call-Chain"

# The chain the *current inbound request* arrived with (set by the middleware),
# so a buy made while serving that request extends it instead of starting fresh.
_inbound: ContextVar[tuple[str, ...]] = ContextVar("jim_inbound_call_chain", default=())


class CallChainDepthExceeded(RuntimeError):
    """Extending the call chain would exceed the depth ceiling — refuse to buy."""


@dataclass(frozen=True)
class ChainVerdict:
    allowed: bool
    reason: str
    hops: tuple[str, ...] = ()


def parse_chain(raw: str | None) -> tuple[str, ...]:
    """Decode a header value into normalized (lowercased) hop identities."""
    if not raw:
        return ()
    return tuple(h.strip().lower() for h in raw.split(",") if h.strip())


def encode_chain(hops: tuple[str, ...] | list[str]) -> str:
    return ",".join(hops)


def inbound_chain() -> tuple[str, ...]:
    return _inbound.get()


def set_inbound_chain(hops: tuple[str, ...]):
    """Bind the inbound chain for this request context; returns the reset token."""
    return _inbound.set(hops)


def reset_inbound_chain(token) -> None:
    _inbound.reset(token)


def check_inbound(raw: str | None, *, own_address: str | None, max_depth: int) -> ChainVerdict:
    """Deterministic sell-side verdict on an inbound chain, pre-payment."""
    hops = parse_chain(raw)
    if own_address and own_address.lower() in hops:
        return ChainVerdict(
            allowed=False,
            reason=(
                f"payment loop refused: our address {own_address.lower()} is already in the "
                f"call chain ({encode_chain(hops)}) — serving this request would cycle funds"
            ),
            hops=hops,
        )
    if len(hops) >= max_depth:
        return ChainVerdict(
            allowed=False,
            reason=(
                f"call chain too deep: {len(hops)} hops arrived, ceiling is {max_depth} "
                "(CALL_CHAIN_MAX_DEPTH)"
            ),
            hops=hops,
        )
    return ChainVerdict(allowed=True, reason="ok", hops=hops)


def outbound_payment_headers(own_address: str, max_depth: int) -> dict[str, str]:
    """The header set for a buy-side request: inbound chain + our identity.

    Raises :class:`CallChainDepthExceeded` when the extended chain would exceed
    the ceiling — the deterministic "stop subcontracting" tripwire.
    """
    chain = [*inbound_chain()]
    own = own_address.lower()
    if own not in chain:
        chain.append(own)
    if len(chain) > max_depth:
        raise CallChainDepthExceeded(
            f"refusing to buy: extending the call chain to {len(chain)} hops exceeds the "
            f"ceiling of {max_depth} (CALL_CHAIN_MAX_DEPTH)"
        )
    return {CALL_CHAIN_HEADER: encode_chain(chain)}


class CallChainMiddleware:
    """Pure-ASGI middleware: refuse loops/over-depth *before* the paywall runs.

    Must be the OUTERMOST middleware (added last) so the refusal happens before
    the x402 payment middleware verifies — a refused request is never charged.
    For allowed requests it binds the inbound chain into the request context so
    any upstream buy made while serving it extends the same chain.
    """

    def __init__(self, app, *, own_address: str | None, max_depth: int) -> None:
        self.app = app
        self.own_address = own_address
        self.max_depth = max_depth

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        raw = None
        header_key = CALL_CHAIN_HEADER.lower().encode()
        for name, value in scope.get("headers", []):
            if name.lower() == header_key:
                raw = value.decode("latin-1")
                break

        verdict = check_inbound(raw, own_address=self.own_address, max_depth=self.max_depth)
        if not verdict.allowed:
            body = json.dumps({"error": verdict.reason, "call_chain": list(verdict.hops)})
            await send(
                {
                    "type": "http.response.start",
                    "status": 409,
                    "headers": [(b"content-type", b"application/json")],
                }
            )
            await send({"type": "http.response.body", "body": body.encode("utf-8")})
            return

        token = set_inbound_chain(verdict.hops)
        try:
            await self.app(scope, receive, send)
        finally:
            reset_inbound_chain(token)
