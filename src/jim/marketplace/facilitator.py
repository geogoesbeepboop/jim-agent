"""Facilitator client construction, shared by the HTTP seller and MCP server.

The free testnet facilitator (x402.org) needs no auth. The Coinbase CDP
facilitator (mainnet) requires CDP API key auth on every verify/settle call —
so when CDP credentials are configured we build its auth headers via the
``cdp-sdk`` package instead of just pointing at ``FACILITATOR_URL``.
"""

from __future__ import annotations

import logging

from x402.http import FacilitatorConfig, HTTPFacilitatorClient

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
    """

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
            logger.exception("facilitator get_supported() failed")
            raise


def build_facilitator_client(s: Settings) -> LoggingFacilitatorClient:
    return LoggingFacilitatorClient(build_facilitator_config(s))
