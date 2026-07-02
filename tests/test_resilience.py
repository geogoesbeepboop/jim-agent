"""The Phase 6 resilience wrapper — timeouts, retries with jitter, circuit breaker.

Offline and instant: the backoff sleep, the jitter, and the monotonic clock are
module-level indirections (``resilience._sleep`` / ``_rand`` / ``_now``), so the
tests monkeypatch them and never actually wait. Breakers are process-global by
design, so every test starts (and ends) from ``reset_breakers()``.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest

from jim.config import get_settings
from jim.net import resilience
from jim.net.resilience import (
    CircuitOpen,
    ResiliencePolicy,
    default_policy,
    reset_breakers,
    resilient_call,
)


@pytest.fixture(autouse=True)
def _instant(monkeypatch):
    """Fresh breakers, no real sleeping, deterministic (zero) jitter."""
    reset_breakers()

    async def no_sleep(_delay: float) -> None:
        return None

    monkeypatch.setattr(resilience, "_sleep", no_sleep)
    monkeypatch.setattr(resilience, "_rand", lambda: 0.0)
    yield
    reset_breakers()


def _flaky(failures: int, exc: BaseException | None = None):
    """An async fn that fails ``failures`` times, then returns "ok". Counts calls."""
    calls = {"n": 0}

    async def fn() -> str:
        calls["n"] += 1
        if calls["n"] <= failures:
            raise exc if exc is not None else httpx.ConnectError("transport down")
        return "ok"

    return fn, calls


# --- retries ------------------------------------------------------------------


async def test_succeeds_first_try_costs_one_call() -> None:
    fn, calls = _flaky(0)
    assert await resilient_call(fn, host="one.test") == "ok"
    assert calls["n"] == 1


async def test_retries_transport_errors_then_succeeds(monkeypatch) -> None:
    delays: list[float] = []

    async def record_sleep(delay: float) -> None:
        delays.append(delay)

    monkeypatch.setattr(resilience, "_sleep", record_sleep)
    fn, calls = _flaky(2)
    policy = ResiliencePolicy(retries=2)
    assert await resilient_call(fn, host="two.test", policy=policy) == "ok"
    assert calls["n"] == 3  # 2 failures + the success
    # Exponential backoff (base 0.25, zero jitter): 0.25, then 0.50.
    assert delays == [0.25, 0.5]


async def test_exhausted_retries_raise_the_original_error() -> None:
    boom = httpx.ConnectError("still down")
    fn, calls = _flaky(99, exc=boom)
    policy = ResiliencePolicy(retries=2)
    with pytest.raises(httpx.ConnectError) as excinfo:
        await resilient_call(fn, host="three.test", policy=policy)
    assert excinfo.value is boom  # the last attempt's error, not a wrapper type
    assert calls["n"] == 3  # retries + 1 attempts, then give up


async def test_timeout_counts_as_a_failure_and_triggers_retry() -> None:
    calls = {"n": 0}

    async def slow_then_fast() -> str:
        calls["n"] += 1
        if calls["n"] == 1:
            await asyncio.sleep(3600)  # cut off by the wrapper's wall clock
        return "ok"

    policy = ResiliencePolicy(timeout_seconds=0.01, retries=1)
    assert await resilient_call(slow_then_fast, host="four.test", policy=policy) == "ok"
    assert calls["n"] == 2


async def test_non_retryable_errors_propagate_immediately() -> None:
    fn, calls = _flaky(99, exc=ValueError("bad payload"))
    with pytest.raises(ValueError):
        await resilient_call(fn, host="five.test", policy=ResiliencePolicy(retries=2))
    assert calls["n"] == 1  # semantic failure: no retry, no backoff


# --- circuit breaker ----------------------------------------------------------


async def test_breaker_opens_after_threshold_and_fails_fast(monkeypatch) -> None:
    monkeypatch.setattr(resilience, "_now", lambda: 100.0)
    policy = ResiliencePolicy(retries=0, breaker_failure_threshold=3)
    fn, calls = _flaky(99)
    for _ in range(3):  # three consecutive transport failures → open
        with pytest.raises(httpx.ConnectError):
            await resilient_call(fn, host="six.test", policy=policy)
    with pytest.raises(CircuitOpen) as excinfo:
        await resilient_call(fn, host="six.test", policy=policy)
    assert calls["n"] == 3  # the tripped call never invoked fn
    assert "six.test" in str(excinfo.value)  # names the host + remaining cooldown
    assert f"{policy.breaker_reset_seconds:.1f}s" in str(excinfo.value)


async def test_half_open_probe_success_closes_the_breaker(monkeypatch) -> None:
    clock = {"t": 0.0}
    monkeypatch.setattr(resilience, "_now", lambda: clock["t"])
    policy = ResiliencePolicy(retries=0, breaker_failure_threshold=2, breaker_reset_seconds=30.0)
    fn, calls = _flaky(2)  # fails twice, then recovers
    for _ in range(2):
        with pytest.raises(httpx.ConnectError):
            await resilient_call(fn, host="seven.test", policy=policy)
    with pytest.raises(CircuitOpen):  # open: fail fast during the cooldown
        await resilient_call(fn, host="seven.test", policy=policy)
    clock["t"] = 31.0  # cooldown elapsed → one probe allowed through
    assert await resilient_call(fn, host="seven.test", policy=policy) == "ok"
    assert calls["n"] == 3
    # Closed again: the next call goes straight through.
    assert await resilient_call(fn, host="seven.test", policy=policy) == "ok"


async def test_half_open_probe_failure_reopens_the_breaker(monkeypatch) -> None:
    clock = {"t": 0.0}
    monkeypatch.setattr(resilience, "_now", lambda: clock["t"])
    policy = ResiliencePolicy(retries=0, breaker_failure_threshold=2, breaker_reset_seconds=30.0)
    fn, calls = _flaky(99)  # never recovers
    for _ in range(2):
        with pytest.raises(httpx.ConnectError):
            await resilient_call(fn, host="eight.test", policy=policy)
    clock["t"] = 31.0  # probe allowed; it fails → re-opened at t=31
    with pytest.raises(httpx.ConnectError):
        await resilient_call(fn, host="eight.test", policy=policy)
    with pytest.raises(CircuitOpen):
        await resilient_call(fn, host="eight.test", policy=policy)
    assert calls["n"] == 3  # 2 to open + 1 probe; the re-open blocked the 4th


# --- settings wiring ----------------------------------------------------------


def test_default_policy_comes_from_settings() -> None:
    s = get_settings()
    p = default_policy()
    assert p.timeout_seconds == s.resilience_timeout_seconds
    assert p.retries == s.resilience_retries
    assert p.breaker_failure_threshold == s.resilience_breaker_threshold
    assert p.breaker_reset_seconds == s.resilience_breaker_reset_seconds


# --- the wrapped sources still degrade / surface the same way ------------------


class _DownClient:
    """Fakes ``httpx.AsyncClient``: every request dies at the transport layer."""

    def __init__(self, counter: dict):
        self._counter = counter

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc) -> bool:
        return False

    async def get(self, *a, **k):
        self._counter["n"] += 1
        raise httpx.ConnectError("network down")

    async def post(self, *a, **k):
        self._counter["n"] += 1
        raise httpx.ConnectError("network down")


async def test_macro_still_degrades_when_transport_keeps_failing(monkeypatch) -> None:
    from jim.sources.macro import fetch_macro

    counter = {"n": 0}
    monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **k: _DownClient(counter))
    data = await fetch_macro()
    # Every agency was retried, then its reading dropped — the run still returns.
    assert data.readings == [] and data.as_of is None
    assert counter["n"] == 3 * (1 + get_settings().resilience_retries)


def _isolate_edgar(monkeypatch):
    """Empty ticker cache + a lock bound to THIS test's event loop."""
    from jim.research import edgar as edgar_mod

    edgar_mod._ticker_cache.clear()
    monkeypatch.setattr(edgar_mod, "_ticker_lock", asyncio.Lock())
    return edgar_mod


async def test_edgar_surfaces_the_original_transport_error_after_retries(monkeypatch) -> None:
    edgar_mod = _isolate_edgar(monkeypatch)
    counter = {"n": 0}
    monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **k: _DownClient(counter))
    with pytest.raises(httpx.ConnectError):  # same error type callers handle today
        await edgar_mod.fetch_snapshot("ACME")
    assert counter["n"] == 1 + get_settings().resilience_retries


class _FakeResponse:
    def __init__(self, status_code: int = 200, payload: dict | None = None):
        self.status_code = status_code
        self._payload = payload or {}

    def json(self) -> dict:
        return self._payload

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("http error", request=None, response=None)


class _Scripted404Client(_DownClient):
    """Ticker list resolves; companyfacts 404s — EDGAR's domain-error path."""

    async def get(self, url: str, *a, **k) -> _FakeResponse:
        self._counter["n"] += 1
        if "company_tickers" in url:
            return _FakeResponse(payload={"0": {"ticker": "ACME", "cik_str": 1}})
        return _FakeResponse(status_code=404)


async def test_edgar_domain_error_still_surfaces_unretried(monkeypatch) -> None:
    edgar_mod = _isolate_edgar(monkeypatch)
    from jim.research.edgar import EdgarError

    counter = {"n": 0}
    monkeypatch.setattr(httpx, "AsyncClient", lambda *a, **k: _Scripted404Client(counter))
    with pytest.raises(EdgarError, match="No XBRL company facts"):
        await edgar_mod.fetch_snapshot("ACME")
    assert counter["n"] == 2  # tickers + companyfacts; the 404 was never retried
