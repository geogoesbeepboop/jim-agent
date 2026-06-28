"""``jim-wallet`` — generate / inspect the Phase 0 testnet wallet.

Usage:
    uv run jim-wallet new        # mint a fresh key, print the address + .env line
    uv run jim-wallet show       # show the address for the configured key

The faucet step is manual (you need testnet ETH for gas + testnet USDC to spend):
    Base Sepolia ETH : https://www.alchemy.com/faucets/base-sepolia
    Base Sepolia USDC: https://faucet.circle.com  (select "Base Sepolia")
"""

from __future__ import annotations

import sys

from jim.config import get_settings
from jim.wallet.local import LocalWallet


def _cmd_new() -> int:
    wallet = LocalWallet.create()
    print("Generated a new Base Sepolia wallet.\n")
    print(f"  Address     : {wallet.address}")
    print("  Private key : (shown once below — store it, never commit it)\n")
    print("Add these to your .env:\n")
    print(f"EVM_PRIVATE_KEY={wallet.private_key}")
    print(f"EVM_ADDRESS={wallet.address}")
    print("\nNext: fund the address with testnet ETH (gas) and USDC (to spend):")
    print("  ETH : https://www.alchemy.com/faucets/base-sepolia")
    print("  USDC: https://faucet.circle.com  (network: Base Sepolia)")
    return 0


def _cmd_show() -> int:
    settings = get_settings()
    if not settings.evm_private_key:
        print("No EVM_PRIVATE_KEY set. Run `uv run jim-wallet new` first.", file=sys.stderr)
        return 1
    wallet = LocalWallet.from_key(settings.evm_private_key)
    print(f"Address : {wallet.address}")
    print(f"Network : {settings.network}")
    if settings.evm_address and settings.evm_address.lower() != wallet.address.lower():
        print(
            f"WARNING : EVM_ADDRESS ({settings.evm_address}) does not match the "
            f"address derived from EVM_PRIVATE_KEY ({wallet.address}).",
            file=sys.stderr,
        )
    return 0


def main() -> int:
    args = sys.argv[1:]
    cmd = args[0] if args else "show"
    if cmd == "new":
        return _cmd_new()
    if cmd == "show":
        return _cmd_show()
    print(f"Unknown command: {cmd!r}. Use 'new' or 'show'.", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
