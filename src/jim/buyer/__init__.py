"""Buyer side: an x402 client that pays for protected resources.

Phase 0 uses this to self-test our own seller. Phase 2 reuses the same client
to PURCHASE upstream data the research agent decides it needs.
"""

from jim.buyer.client import PaidResponse, fetch_paid, pay

__all__ = ["PaidResponse", "fetch_paid", "pay"]
