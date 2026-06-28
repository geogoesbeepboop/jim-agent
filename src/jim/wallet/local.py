"""Local EVM wallet backed by ``eth_account``.

This is intentionally minimal. It exists so Phase 0 can:
  - generate a fresh key for Base Sepolia,
  - report the address to fund from a faucet,
  - hand an ``EthAccountSigner`` to the x402 buy client.
"""

from __future__ import annotations

from dataclasses import dataclass

from eth_account import Account
from eth_account.signers.local import LocalAccount


@dataclass(frozen=True)
class LocalWallet:
    """Thin wrapper over an ``eth_account`` LocalAccount."""

    account: LocalAccount

    @property
    def address(self) -> str:
        return self.account.address

    @property
    def private_key(self) -> str:
        # 0x-prefixed hex. Treat as a secret — never log this.
        return "0x" + self.account.key.hex().removeprefix("0x")

    @classmethod
    def from_key(cls, private_key: str) -> "LocalWallet":
        return cls(account=Account.from_key(private_key))

    @classmethod
    def create(cls) -> "LocalWallet":
        return cls(account=Account.create())

    def signer(self):
        """Return the x402 EVM signer bound to this account.

        Imported lazily so importing the wallet module doesn't drag in the
        whole x402 EVM mechanism stack unless a payment is actually made.
        """
        from x402.mechanisms.evm import EthAccountSigner

        return EthAccountSigner(self.account)


def load_or_create_wallet(private_key: str | None) -> LocalWallet:
    """Load a wallet from a key if provided, otherwise mint a fresh one."""
    if private_key:
        return LocalWallet.from_key(private_key)
    return LocalWallet.create()
