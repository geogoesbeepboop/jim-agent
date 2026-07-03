"""Resilience wrapper for outbound calls — timeouts, retries, circuit breaker.

jim already degrades well by *absence* (no key → deterministic fallback, no DB →
in-memory store, no Yahoo → EDGAR-only); this module adds degradation by
*failure*. Every free upstream fetch (EDGAR, Yahoo, macro) and every paid x402
buy (``jim.sources.base.procure`` — The Graph, peer agents) runs through
``resilient_call``, which gives each attempt:

- a wall-clock timeout (``asyncio.timeout``), a backstop on top of any client
  timeout the caller already sets;
- bounded retries with exponential backoff + jitter — but only for
  transport-level failures (connect/read errors, timeouts). HTTP 4xx/5xx are
  deliberately NOT retried here: the host answered, the sources already handle
  status semantics, and retrying a 404 just hammers the upstream;
- a per-host circuit breaker: after ``breaker_failure_threshold`` consecutive
  transport failures the host is declared down and further calls fail fast with
  ``CircuitOpen`` (no network attempt) until ``breaker_reset_seconds`` elapse,
  when one half-open probe is let through — success closes the breaker, failure
  re-opens it.

Like tracing (jim.obs.tracing), this is deliberately invisible when everything
works: defaults come from Settings, the breaker registry is process-local, and a
healthy upstream sees exactly one request per call. The sleep, jitter, and clock
functions are module-level indirections so tests run offline and instantly.
"""

from __future__ import annotations

import asyncio
import random
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TypeVar

import httpx

from jim.config import get_settings

T = TypeVar("T")

# Indirections for tests: monkeypatch these to make retries instant/deterministic.
_sleep = asyncio.sleep
_rand = random.random
_now = time.monotonic


@dataclass(frozen=True)
class ResiliencePolicy:
    """Knobs for one guarded call. Defaults mirror Settings — see default_policy()."""

    timeout_seconds: float = 20.0
    retries: int = 2  # attempts = retries + 1
    backoff_base_seconds: float = 0.25  # delay = base * 2**attempt + jitter
    backoff_jitter: float = 0.25  # uniform [0, jitter) added to each delay
    breaker_failure_threshold: int = 5  # consecutive failures that open the breaker
    breaker_reset_seconds: float = 30.0  # cooldown before the half-open probe


def default_policy() -> ResiliencePolicy:
    """The policy every source uses unless it passes its own — built from Settings."""
    s = get_settings()
    return ResiliencePolicy(
        timeout_seconds=s.resilience_timeout_seconds,
        retries=s.resilience_retries,
        breaker_failure_threshold=s.resilience_breaker_threshold,
        breaker_reset_seconds=s.resilience_breaker_reset_seconds,
    )


class CircuitOpen(RuntimeError):
    """The breaker for a host is open: fail fast instead of dialing a dead upstream."""

    def __init__(self, host: str, failures: int, retry_in_seconds: float):
        self.host = host
        self.retry_in_seconds = retry_in_seconds
        super().__init__(
            f"Circuit open for {host} after {failures} consecutive transport failures; "
            f"next probe allowed in {retry_in_seconds:.1f}s."
        )


@dataclass
class _BreakerState:
    """Per-host health: consecutive transport failures + when the breaker opened."""

    failures: int = field(default=0)
    opened_at: float | None = field(default=None)  # monotonic; None → closed


# Module-level registry keyed by host (netloc). Process-local and unlocked on
# purpose: asyncio is single-threaded and every mutation happens between awaits.
_breakers: dict[str, _BreakerState] = {}


def reset_breakers() -> None:
    """Forget all breaker state (tests; a fresh process starts closed anyway)."""
    _breakers.clear()


async def resilient_call(
    fn: Callable[[], Awaitable[T]],
    *,
    host: str,
    policy: ResiliencePolicy | None = None,
    retry_on: tuple[type[BaseException], ...] = (httpx.TransportError, httpx.TimeoutException),
) -> T:
    """Run ``await fn()`` under the timeout / retry / breaker regime for ``host``.

    Only ``retry_on`` exceptions and the wall-clock ``TimeoutError`` count as
    transport failures: they are retried (with backoff + jitter) and feed the
    breaker. Anything else — HTTP status errors, parse errors, domain errors like
    ``EdgarError`` — is semantic: the host answered, so it surfaces immediately,
    unchanged, and closes the breaker. When retries exhaust, the original
    exception from the last attempt is re-raised.
    """
    policy = policy or default_policy()
    state = _breakers.setdefault(host, _BreakerState())

    if state.opened_at is not None:
        elapsed = _now() - state.opened_at
        if elapsed < policy.breaker_reset_seconds:
            raise CircuitOpen(host, state.failures, policy.breaker_reset_seconds - elapsed)
        # Cooldown elapsed — fall through as the single half-open probe. Success
        # below closes the breaker; a failure re-stamps opened_at (re-opens).

    retriable = (*retry_on, TimeoutError)
    for attempt in range(policy.retries + 1):
        try:
            async with asyncio.timeout(policy.timeout_seconds):
                result = await fn()
        except retriable:
            state.failures += 1
            if state.failures >= policy.breaker_failure_threshold:
                state.opened_at = _now()
            if attempt >= policy.retries:
                raise
            delay = policy.backoff_base_seconds * (2**attempt) + policy.backoff_jitter * _rand()
            await _sleep(delay)
        except Exception:
            # Semantic failure: the transport is healthy, so the breaker closes
            # and the error surfaces to the caller's existing handling, unretried.
            state.failures = 0
            state.opened_at = None
            raise
        else:
            state.failures = 0
            state.opened_at = None
            return result
    raise AssertionError("unreachable: the retry loop always returns or raises")
