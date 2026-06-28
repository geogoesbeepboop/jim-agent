"""Mainnet cutover preflight (Phase 5) — a deterministic readiness checklist.

Going live means switching ``NETWORK`` to Base mainnet (``eip155:8453``) and
settling **real USDC**. That's the one irreversible step in jim, so before it we
run a checklist that *reads* state and reports — it never moves money.

Each check is ``ok`` / ``warn`` / ``fail``:
  - ``fail`` blocks a clean cutover (e.g. no pay-to address, or a testnet-only
    facilitator that can't settle on mainnet).
  - ``warn`` is "you probably want to fix this" (e.g. on testnet still, no gas).
  - ``ok`` / ``info`` are informational.

If ``MAINNET_RPC_URL`` is set, the preflight additionally reads the wallet's ETH
(gas) and USDC balances over a read-only JSON-RPC call. Everything else is fully
offline. See ADR-0004 for why the cutover is guarded this way.
"""

from __future__ import annotations

from dataclasses import dataclass

from jim.config import BASE_MAINNET, Settings, get_settings
from jim.marketplace.pricing import base_price
from jim.marketplace.catalog import product_names

# ERC-20 balanceOf(address) selector.
_BALANCE_OF = "0x70a08231"


@dataclass
class Check:
    name: str
    status: str  # "ok" | "info" | "warn" | "fail"
    detail: str

    def to_dict(self) -> dict:
        return {"name": self.name, "status": self.status, "detail": self.detail}


@dataclass
class MainnetReadiness:
    network: str
    is_mainnet: bool
    checks: list[Check]

    @property
    def ready(self) -> bool:
        return not any(c.status == "fail" for c in self.checks)

    @property
    def warnings(self) -> list[Check]:
        return [c for c in self.checks if c.status == "warn"]

    def to_dict(self) -> dict:
        return {
            "network": self.network,
            "is_mainnet": self.is_mainnet,
            "ready": self.ready,
            "checks": [c.to_dict() for c in self.checks],
        }


def _static_checks(s: Settings) -> list[Check]:
    checks: list[Check] = []

    if s.is_mainnet:
        checks.append(Check("network", "ok", f"On Base mainnet ({s.network}); real USDC."))
    else:
        checks.append(
            Check(
                "network",
                "warn",
                f"On testnet ({s.network}). Set NETWORK={BASE_MAINNET} to cut over.",
            )
        )

    if s.evm_address:
        checks.append(Check("pay_to", "ok", f"Receiving payments at {s.evm_address}."))
    else:
        checks.append(Check("pay_to", "fail", "EVM_ADDRESS unset — nothing to settle into."))

    if s.evm_private_key:
        checks.append(Check("buyer_key", "ok", "Buyer key present (buy leg + UI self-pay)."))
    else:
        checks.append(
            Check("buyer_key", "warn", "EVM_PRIVATE_KEY unset — can't buy upstream data or self-pay.")
        )

    checks.append(Check("asset", "info", f"Settlement asset USDC at {s.usdc_address}."))

    testnet_facilitator = "x402.org/facilitator" in s.facilitator_url
    if s.is_mainnet and testnet_facilitator:
        checks.append(
            Check(
                "facilitator",
                "fail",
                f"{s.facilitator_url} is the testnet facilitator; it cannot settle on "
                "mainnet. Point FACILITATOR_URL at a mainnet facilitator (e.g. Coinbase CDP).",
            )
        )
    else:
        checks.append(Check("facilitator", "info", f"Facilitator: {s.facilitator_url}."))

    # Facilitator economics vs. our prices.
    if s.facilitator_min_usdc > 0:
        too_cheap = [p for p in product_names() if base_price(p) < s.facilitator_min_usdc]
        if too_cheap:
            checks.append(
                Check(
                    "min_settlement",
                    "warn",
                    f"Products priced below the ${s.facilitator_min_usdc:.4f} facilitator "
                    f"minimum won't settle: {', '.join(too_cheap)}.",
                )
            )
        else:
            checks.append(
                Check("min_settlement", "ok", f"All prices ≥ ${s.facilitator_min_usdc:.4f} min.")
            )
    if s.facilitator_fee_bps > 0:
        checks.append(
            Check("facilitator_fee", "info", f"Facilitator fee ≈ {s.facilitator_fee_bps:.1f} bps.")
        )

    # The Graph buy leg (already mainnet-capable from Phase 2).
    if s.graph_live:
        if s.graph_buy_key:
            checks.append(
                Check("graph_buy_leg", "ok", f"GRAPH_LIVE on; buying on {s.graph_buy_network}.")
            )
        else:
            checks.append(
                Check("graph_buy_leg", "fail", "GRAPH_LIVE on but no key to pay The Graph.")
            )
    else:
        checks.append(
            Check("graph_buy_leg", "info", "GRAPH_LIVE off — token product uses the testnet mock.")
        )

    return checks


async def _balance_checks(s: Settings) -> list[Check]:
    """Best-effort on-chain ETH (gas) + USDC reads via a read-only JSON-RPC."""
    if not s.mainnet_rpc_url:
        return [Check("balances", "info", "Set MAINNET_RPC_URL to read on-chain balances.")]
    if not s.evm_address:
        return []
    try:
        import httpx

        addr = s.evm_address
        async with httpx.AsyncClient(timeout=httpx.Timeout(15.0, connect=8.0)) as c:
            eth_hex = await _rpc(c, s.mainnet_rpc_url, "eth_getBalance", [addr, "latest"])
            data = _BALANCE_OF + addr.lower().removeprefix("0x").rjust(64, "0")
            usdc_hex = await _rpc(
                c, s.mainnet_rpc_url, "eth_call", [{"to": s.usdc_address, "data": data}, "latest"]
            )
        eth = int(eth_hex, 16) / 1e18
        usdc = int(usdc_hex, 16) / 1e6
    except Exception as e:  # network/parse error — never fatal
        return [Check("balances", "warn", f"Could not read balances: {e}")]

    out = [Check("usdc_balance", "ok" if usdc > 0 else "warn", f"USDC balance: {usdc:.4f}")]
    gas_status = "ok" if eth > 0 else ("warn" if s.evm_private_key else "info")
    out.append(Check("eth_balance", gas_status, f"ETH (gas) balance: {eth:.6f}"))
    return out


async def _rpc(client, url: str, method: str, params: list) -> str:
    resp = await client.post(
        url, json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
    )
    resp.raise_for_status()
    return resp.json()["result"]


async def check_mainnet_readiness(settings: Settings | None = None) -> MainnetReadiness:
    s = settings or get_settings()
    checks = _static_checks(s)
    checks.extend(await _balance_checks(s))
    return MainnetReadiness(network=s.network, is_mainnet=s.is_mainnet, checks=checks)
