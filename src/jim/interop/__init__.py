"""Agent-to-agent interop (Phase 7): call-chain safety + verification-based trust.

The seam between agents gets the same treatment as everything inside jim —
*the model proposes, deterministic code disposes*:

  - :mod:`jim.interop.callchain` — a propagated ``X-Jim-Call-Chain`` header
    gives every cross-agent request a bounded call graph: cycles are refused
    before payment, depth is capped before spend.
  - :mod:`jim.interop.trust` — per-source reputation computed from outcomes:
    a source's trust is its sourcing-gate pass-rate, not a review score.
"""

from jim.interop.callchain import (
    CALL_CHAIN_HEADER,
    CallChainDepthExceeded,
    CallChainMiddleware,
    check_inbound,
    inbound_chain,
    outbound_payment_headers,
)
from jim.interop.trust import attribute_gate_outcome, laplace_score

__all__ = [
    "CALL_CHAIN_HEADER",
    "CallChainDepthExceeded",
    "CallChainMiddleware",
    "attribute_gate_outcome",
    "check_inbound",
    "inbound_chain",
    "laplace_score",
    "outbound_payment_headers",
]
