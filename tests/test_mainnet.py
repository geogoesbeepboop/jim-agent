"""Phase 5 mainnet-cutover preflight — deterministic readiness checks.

Hermetic: ``check_mainnet_readiness`` takes an injected ``Settings``, and the
balance probe only runs when ``MAINNET_RPC_URL`` is set (never in these tests),
so nothing touches the network or moves money.
"""

from __future__ import annotations


from jim.config import BASE_MAINNET, BASE_SEPOLIA, Settings
from jim.marketplace.mainnet import check_mainnet_readiness

_ADDR = "0x" + "1" * 40
_KEY = "0x" + "1" * 64


def _checks(readiness):
    return {c.name: c for c in readiness.checks}


async def test_testnet_is_ready_but_warns_about_cutover() -> None:
    s = Settings(network=BASE_SEPOLIA, evm_address=_ADDR, evm_private_key=_KEY)
    r = await check_mainnet_readiness(s)
    checks = _checks(r)
    assert r.is_mainnet is False
    assert checks["network"].status == "warn"  # still on testnet
    assert checks["pay_to"].status == "ok"
    # No hard failures: the operator can run this preflight before flipping NETWORK.
    assert r.ready is True


async def test_missing_pay_to_is_a_hard_fail() -> None:
    s = Settings(network=BASE_MAINNET, evm_address=None)
    r = await check_mainnet_readiness(s)
    assert _checks(r)["pay_to"].status == "fail"
    assert r.ready is False


async def test_testnet_facilitator_on_mainnet_is_a_hard_fail() -> None:
    s = Settings(
        network=BASE_MAINNET,
        evm_address=_ADDR,
        evm_private_key=_KEY,
        facilitator_url="https://x402.org/facilitator",
    )
    r = await check_mainnet_readiness(s)
    fac = _checks(r)["facilitator"]
    assert fac.status == "fail" and "mainnet" in fac.detail.lower()
    assert r.ready is False


async def test_clean_mainnet_config_is_ready() -> None:
    s = Settings(
        network=BASE_MAINNET,
        evm_address=_ADDR,
        evm_private_key=_KEY,
        facilitator_url="https://facilitator.cdp.coinbase.com",
    )
    r = await check_mainnet_readiness(s)
    checks = _checks(r)
    assert checks["network"].status == "ok"
    assert checks["facilitator"].status == "info"
    assert r.ready is True


async def test_facilitator_minimum_above_price_warns() -> None:
    # A min higher than every product price should flag the products as unsettleable.
    s = Settings(
        network=BASE_MAINNET,
        evm_address=_ADDR,
        evm_private_key=_KEY,
        facilitator_url="https://facilitator.cdp.coinbase.com",
        facilitator_min_usdc=999.0,
    )
    r = await check_mainnet_readiness(s)
    assert _checks(r)["min_settlement"].status == "warn"


async def test_graph_live_without_key_fails() -> None:
    s = Settings(
        network=BASE_MAINNET,
        evm_address=_ADDR,
        evm_private_key=None,
        facilitator_url="https://facilitator.cdp.coinbase.com",
        graph_live=True,
        graph_evm_private_key=None,
    )
    r = await check_mainnet_readiness(s)
    assert _checks(r)["graph_buy_leg"].status == "fail"
    assert r.ready is False
