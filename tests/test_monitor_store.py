"""Monitor persistence on the in-memory store: CRUD, due-query, feed, stats."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

from jim.monitors.models import Monitor, MonitorRun, Signal, TriggerSpec
from jim.store.repo import MemoryStore

UTC = timezone.utc
NOW = datetime(2025, 1, 1, tzinfo=UTC)


def _monitor(ident="AAPL", **kw):
    return Monitor(
        id=f"fund-{ident.lower()}-x",
        product="fundamentals",
        identifier=ident,
        triggers=[TriggerSpec("price_move", {"pct": 5.0})],
        **kw,
    )


async def test_save_get_list_delete_roundtrip():
    store = MemoryStore()
    m = _monitor()
    await store.save_monitor(m.to_row())

    got = Monitor.from_row(await store.get_monitor(m.id))
    assert got.identifier == "AAPL" and got.triggers[0].kind == "price_move"
    assert [r["id"] for r in await store.list_monitors()] == [m.id]

    assert await store.delete_monitor(m.id) is True
    assert await store.get_monitor(m.id) is None
    assert await store.delete_monitor(m.id) is False


async def test_due_query_respects_enabled_and_next_run():
    store = MemoryStore()
    await store.save_monitor(
        _monitor("AAPL", next_run_at=NOW - timedelta(minutes=1)).to_row()
    )  # due
    await store.save_monitor(
        _monitor("MSFT", next_run_at=NOW + timedelta(hours=1)).to_row()
    )  # later
    await store.save_monitor(_monitor("NVDA", next_run_at=None).to_row())  # never run → due
    await store.save_monitor(_monitor("TSLA", enabled=False, next_run_at=None).to_row())  # off

    due = {r["identifier"] for r in await store.due_monitors(NOW)}
    assert due == {"AAPL", "NVDA"}


async def test_list_enabled_only():
    store = MemoryStore()
    await store.save_monitor(_monitor("AAPL").to_row())
    await store.save_monitor(_monitor("MSFT", enabled=False).to_row())
    assert {r["identifier"] for r in await store.list_monitors(enabled_only=True)} == {"AAPL"}


async def test_feed_and_stats():
    store = MemoryStore()
    mid = "fund-aapl-x"

    async def record(status, *, material=False, infer=0.0, price=0.0):
        run = MonitorRun(
            monitor_id=mid,
            identifier="AAPL",
            product="fundamentals",
            status=status,
            material=material,
            severity="notable" if material else "info",
            signals=[Signal("price_move", "k", "Price", "notable", "s", ["C1"])]
            if material
            else [],
            price_out_usd=price,
            cost_inference_usd=infer,
        )
        await store.record_monitor_run(run.to_row())

    await record("baseline")
    await record("quiet")
    await record("quiet")
    await record("material", material=True, infer=0.003, price=0.10)

    feed = await store.monitor_feed(material_only=True)
    assert len(feed) == 1 and feed[0]["status"] == "material"
    assert len(await store.monitor_feed(material_only=False)) == 4

    stats = await store.monitor_stats()
    assert stats["total_runs"] == 4
    assert (
        stats["updates_delivered"] == 1 and stats["quiet_runs"] == 2 and stats["baseline_runs"] == 1
    )
    assert stats["revenue_usd"] == 0.10
    # two quiet polls × the avg inference of a real update = estimated savings.
    assert stats["inference_saved_usd"] == round(2 * 0.003, 6)
