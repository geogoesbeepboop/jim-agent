"""Wallet helpers for Phase 0.

We use a plain local ``eth_account`` key to PROVE the payment cycle end-to-end
with the least moving parts. CDP MPC server wallets are the production-custody
upgrade (see docs/BUILD_PLAN.md, Phase 0 notes) and slot in behind the same
``address`` / signer interface.
"""

from jim.wallet.local import LocalWallet, load_or_create_wallet

__all__ = ["LocalWallet", "load_or_create_wallet"]
