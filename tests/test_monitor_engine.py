"""Monitor engine lifecycle + scheduler, proven offline.

The source and (absent) LLM are mocked; the diff, the crew, the materiality gate,
the deterministic update fallback, and the economics accounting are all real code.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from jim.config import get_settings
from jim.monitors import engine
from jim.monitors.create import create_monitor
from jim.monitors.engine import run_monitor_once
from jim.monitors.models import Monitor
from jim.monitors.scheduler import MonitorScheduler
from jim.research.facts import INDEX, USD, Fact, Snapshot
from jim.research.products import Product
from jim.sources.base import GatherResult, ProcurementError
from jim.store.repo import MemoryStore

UTC = timezone.utc
T0 = datetime(2025, 1, 1, 12, 0, tzinfo=UTC)


def _snap(price=100.0, rsi=55.0, *, acc="a", as_of="2025-01-01"):
    return Snapshot(
        ticker="AAPL",
        cik="0000320193",
        entity_name="Apple Inc.",
        as_of=as_of,
        facts=[
            Fact(
                id="C1", label="Price", value=price, unit=USD, source_label="Yahoo", accession=acc
            ),
            Fact(id="C2", label="RSI (14-day)", value=rsi, unit=INDEX),
        ],
    )


class ScriptedSource:
    name = "fake"
    is_paid = False

    def __init__(self, snaps, *, cost=0.0, raise_exc=None):
        self.snaps = snaps
        self.cost = cost
        self.raise_exc = raise_exc
        self.i = 0

    async def gather(self, identifier, *, budget, store) -> GatherResult:
        if self.raise_exc:
            raise self.raise_exc
        snap = self.snaps[min(self.i, len(self.snaps) - 1)]
        self.i += 1
        return GatherResult(snapshot=snap, cost_in_usd=self.cost, cache_hit=False)


@pytest.fixture(autouse=True)
def _no_key(monkeypatch):
    # Force the deterministic update fallback (gate-safe, $0 inference).
    monkeypatch.setattr(get_settings(), "anthropic_api_key", None)


def _patch_source(monkeypatch, source, *, product="fundamentals", price_out=0.25):
    monkeypatch.setattr(
        engine,
        "get_product",
        lambda name: Product(
            name=product, source=source, price_out_usd=price_out, identifier_label="x"
        ),
    )


async def test_baseline_quiet_material_lifecycle(monkeypatch):
    src = ScriptedSource(
        [_snap(100, 55), _snap(101, 56), _snap(120, 72, acc="new", as_of="2025-02-01")]
    )
    _patch_source(monkeypatch, src)
    store = MemoryStore()
    mon = await create_monitor("AAPL", watch=["price:5", "rsi:70/30"])

    r1 = await run_monitor_once(mon, store=store, deliver=False, now=T0)
    r2 = await run_monitor_once(mon, store=store, deliver=False, now=T0 + timedelta(days=1))
    r3 = await run_monitor_once(mon, store=store, deliver=False, now=T0 + timedelta(days=2))

    assert (r1.status, r2.status, r3.status) == ("baseline", "quiet", "material")
    assert len(r3.signals) == 2 and r3.severity == "critical"
    assert r3.gate_passed is True
    assert r3.price_out_usd == 0.10 and r3.cost_inference_usd == 0.0
    # baseline rolled forward each run; monitor rescheduled.
    assert mon.baseline["facts"]["Price"]["value"] == 120
    assert mon.next_run_at == T0 + timedelta(days=2) + timedelta(seconds=mon.interval_seconds)

    feed = await store.monitor_feed(material_only=True)
    assert len(feed) == 1 and feed[0]["status"] == "material"
    stats = await store.monitor_stats()
    assert (
        stats["updates_delivered"] == 1 and stats["quiet_runs"] == 1 and stats["baseline_runs"] == 1
    )


async def test_cooldown_suppresses_repeat_material(monkeypatch):
    # Price keeps jumping >5%: without cooldown it fires every run.
    src = ScriptedSource([_snap(100), _snap(120), _snap(144)])
    _patch_source(monkeypatch, src)
    store = MemoryStore()
    mon = await create_monitor("AAPL", watch=["price:5"], cooldown="1d")

    await run_monitor_once(mon, store=store, deliver=False, now=T0)  # baseline
    r2 = await run_monitor_once(mon, store=store, deliver=False, now=T0 + timedelta(minutes=1))
    r3 = await run_monitor_once(mon, store=store, deliver=False, now=T0 + timedelta(minutes=2))

    assert r2.status == "material"  # +20% fires
    assert r3.status == "quiet"  # +20% again but within the 1d cooldown → suppressed


async def test_gather_error_is_recorded_and_does_not_roll_baseline(monkeypatch):
    src = ScriptedSource([], raise_exc=ProcurementError("upstream down"))
    _patch_source(monkeypatch, src)
    store = MemoryStore()
    mon = await create_monitor("AAPL", watch=["price:5"])

    run = await run_monitor_once(mon, store=store, deliver=False, now=T0)
    assert run.status == "error" and "upstream down" in run.error
    assert mon.baseline == {}  # not rolled forward
    assert mon.last_status == "error"
    assert mon.next_run_at == T0 + timedelta(seconds=mon.interval_seconds)  # still rescheduled


async def test_transient_http_error_reschedules_no_hot_loop(monkeypatch):
    # A raw httpx error (e.g. SEC 503/429) on a poll must be caught and rescheduled,
    # never left "due" to hammer the upstream every tick.
    import httpx

    src = ScriptedSource([], raise_exc=httpx.ConnectError("SEC unreachable"))
    _patch_source(monkeypatch, src)
    store = MemoryStore()
    mon = await create_monitor("AAPL", watch=["price:5"])

    run = await run_monitor_once(mon, store=store, deliver=False, now=T0)
    assert run.status == "error"
    assert mon.next_run_at == T0 + timedelta(seconds=mon.interval_seconds)
    assert mon.due(now=T0) is False  # rescheduled into the future, not still due


async def test_preview_does_not_touch_persisted_state(monkeypatch):
    src = ScriptedSource([_snap(100, 55), _snap(130, 75, acc="new")])
    _patch_source(monkeypatch, src)
    store = MemoryStore()
    mon = await create_monitor("AAPL", watch=["price:5", "rsi:70/30"])
    await run_monitor_once(
        mon, store=store, deliver=False, now=T0
    )  # establish baseline (persisted)
    await store.save_monitor(mon.to_row())

    runs_before = len(store.monitor_runs)
    saved = await store.get_monitor(mon.id)

    # Preview on a fresh object loaded from the store → no writes.
    preview = await run_monitor_once(
        Monitor.from_row(saved),
        store=store,
        deliver=False,
        persist=False,
        now=T0 + timedelta(days=1),
    )
    assert preview.status == "material"  # it WOULD fire
    assert len(store.monitor_runs) == runs_before  # but nothing recorded
    assert (await store.get_monitor(mon.id)) == saved  # and the row is unchanged


async def test_scheduler_runs_only_due_and_reschedules(monkeypatch):
    _patch_source(monkeypatch, ScriptedSource([_snap(100, 55)]))
    store = MemoryStore()

    due = await create_monitor("AAPL", watch=["price:5"])
    due.next_run_at = None  # due now
    not_due = await create_monitor("MSFT", watch=["price:5"])
    not_due.next_run_at = T0 + timedelta(hours=1)  # later
    await store.save_monitor(due.to_row())
    await store.save_monitor(not_due.to_row())

    runs = await MonitorScheduler(store=store, deliver=False).tick(now=T0)

    assert len(runs) == 1 and runs[0].identifier == "AAPL"
    after_due = Monitor.from_row(await store.get_monitor(due.id))
    after_not = Monitor.from_row(await store.get_monitor(not_due.id))
    assert after_due.next_run_at == T0 + timedelta(seconds=after_due.interval_seconds)
    assert after_not.next_run_at == T0 + timedelta(hours=1)  # untouched
