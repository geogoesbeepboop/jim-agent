"""Facilitator client construction, shared by the HTTP seller and MCP server.

The free testnet facilitator (x402.org) needs no auth. The Coinbase CDP
facilitator (mainnet) requires CDP API key auth on every verify/settle call —
so when CDP credentials are configured we build its auth headers via the
``cdp-sdk`` package instead of just pointing at ``FACILITATOR_URL``.
"""

from __future__ import annotations

import logging

from x402.http import FacilitatorConfig, HTTPFacilitatorClient
from x402.schemas import SupportedKind, SupportedResponse

from jim.config import Settings

logger = logging.getLogger(__name__)


def build_facilitator_config(s: Settings) -> FacilitatorConfig:
    if s.cdp_api_key_id and s.cdp_api_key_secret:
        from cdp.x402 import create_facilitator_config

        return create_facilitator_config(s.cdp_api_key_id, s.cdp_api_key_secret)
    return FacilitatorConfig(url=s.facilitator_url)


class LoggingFacilitatorClient(HTTPFacilitatorClient):
    """Logs verify/settle/get_supported failures before they're swallowed.

    x402's FastAPI middleware catches any exception from these calls and
    returns a bare ``{}`` 402 to the caller — which hides the *real* reason
    (bad CDP auth, insufficient balance, wrong network, etc). This subclass
    logs the raw exception server-side first, then re-raises unchanged so the
    client-facing behavior is identical.

    ``get_supported`` additionally *degrades instead of raising*: it only
    feeds ``x402ResourceServer.initialize()``'s scheme routing table, so if the
    facilitator's ``/supported`` endpoint is unreachable (offline tests, an
    upstream outage, an API move) we fall back to advertising EXACT-EVM on our
    own configured network. Issuing the 402 challenge never depends on a live
    facilitator; a truly dead facilitator still fails loudly at verify/settle,
    where money would move.
    """

    def __init__(self, config: FacilitatorConfig, fallback_network: str) -> None:
        super().__init__(config)
        self._fallback_network = fallback_network

    async def verify(self, payload, requirements):
        try:
            return await super().verify(payload, requirements)
        except Exception:
            logger.exception("facilitator verify() failed")
            raise

    async def settle(self, payload, requirements):
        try:
            return await super().settle(payload, requirements)
        except Exception:
            logger.exception("facilitator settle() failed")
            raise

    def get_supported(self):
        try:
            return super().get_supported()
        except Exception:
            logger.exception(
                "facilitator get_supported() failed; degrading to exact-EVM on %s",
                self._fallback_network,
            )
            return SupportedResponse(
                kinds=[
                    SupportedKind(x402_version=2, scheme="exact", network=self._fallback_network)
                ]
            )


def build_facilitator_client(s: Settings) -> LoggingFacilitatorClient:
    return LoggingFacilitatorClient(build_facilitator_config(s), fallback_network=s.network)
