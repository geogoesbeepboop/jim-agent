"""Network plumbing shared by outbound calls.

``jim.net.resilience`` is the "one small helper" the roadmap asks for: per-attempt
timeouts, bounded retries with jitter, and a per-host circuit breaker.
"""

from jim.net.resilience import (
    CircuitOpen,
    ResiliencePolicy,
    default_policy,
    reset_breakers,
    resilient_call,
)

__all__ = [
    "CircuitOpen",
    "ResiliencePolicy",
    "default_policy",
    "reset_breakers",
    "resilient_call",
]
