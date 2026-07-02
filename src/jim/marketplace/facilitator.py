"""Facilitator client construction, shared by the HTTP seller and MCP server.

The free testnet facilitator (x402.org) needs no auth. The Coinbase CDP
facilitator (mainnet) requires CDP API key auth on every verify/settle call —
so when CDP credentials are configured we build its auth headers via the
``cdp-sdk`` package instead of just pointing at ``FACILITATOR_URL``.
"""

from __future__ import annotations

from x402.http import FacilitatorConfig

from jim.config import Settings


def build_facilitator_config(s: Settings) -> FacilitatorConfig:
    if s.cdp_api_key_id and s.cdp_api_key_secret:
        from cdp.x402 import create_facilitator_config

        return create_facilitator_config(s.cdp_api_key_id, s.cdp_api_key_secret)
    return FacilitatorConfig(url=s.facilitator_url)
