"""``jim-monitor`` — manage and run continuous monitors (Phase 4).

    uv run jim-monitor add AAPL --watch price:5 rsi:70/30 filing --every 1d
    uv run jim-monitor add NVDA --describe "ping me on big earnings moves or overbought RSI"
    uv run jim-monitor add WETH --product token --watch price:8 --channel webhook:https://h/x
    uv run jim-monitor list
    uv run jim-monitor run <id>            # run one now (delivers)
    uv run jim-monitor run-all             # run everything currently due
    uv run jim-monitor preview <id>        # dry-run: what WOULD fire (no deliver/persist)
    uv run jim-monitor serve               # the scheduler loop
    uv run jim-monitor feed                # recent material updates

Monitors persist to the store (Postgres if DATABASE_URL is set, else in-memory —
note an in-memory store does not survive across separate CLI invocations, so use
Postgres for a standing fleet). The gather + materiality path needs no API key;
only nicer update prose does.
"""

from __future__ import annotations

import argparse
import asyncio
import sys

from jim.monitors.create import create_monitor
from jim.monitors.engine import run_monitor_once
from jim.monitors.models import Monitor, MonitorRun
from jim.monitors.scheduler import MonitorScheduler
from jim.monitors.triggers import describe as describe_trigger
from jim.store import get_store


def _fmt_interval(seconds: int) -> str:
    for unit, n in (("d", 86_400), ("h", 3_600), ("m", 60)):
        if seconds % n == 0 and seconds >= n:
            return f"{seconds // n}{unit}"
    return f"{seconds}s"


def _print_monitor(m: Monitor) -> None:
    crew = "; ".join(describe_trigger(t) for t in m.triggers) or "(none)"
    state = "on" if m.enabled else "OFF"
    nxt = m.next_run_at.isoformat(timespec="seconds") if m.next_run_at else "now"
    print(
        f"  {m.id}  [{state}]  {m.product}:{m.identifier}  every {_fmt_interval(m.interval_seconds)}"
    )
    print(f"      crew: {crew}")
    print(
        f"      channels: {', '.join(m.channels) or '(feed only)'}  ·  next: {nxt}  ·  last: {m.last_status or '—'}"
    )


def _print_run(r: MonitorRun) -> None:
    print(f"\n  {r.identifier} ({r.product}) → {r.status.upper()}", end="")
    if r.material:
        print(f"  severity={r.severity}  gate={'pass' if r.gate_passed else 'FAIL'}")
        for s in r.signals:
            print(f"    • {s.summary}")
        if r.memo:
            print("\n" + "\n".join("    " + ln for ln in r.memo.splitlines()))
        print(
            f"\n    economics: price ${r.price_out_usd:.4f} − data ${r.cost_in_data_usd:.4f} "
            f"− infer ${r.cost_inference_usd:.5f} = margin ${r.margin_usd:.4f}"
            f"  delivered: {', '.join(r.delivered_to) or 'feed only'}"
        )
    elif r.status == "error":
        print(f"  ({r.error})")
    else:
        print(f"  (data ${r.cost_in_data_usd:.4f}{', cache hit' if r.cache_hit else ''})")


# --- commands ---------------------------------------------------------------


async def _add(args) -> int:
    monitor = await create_monitor(
        args.identifier,
        product=args.product,
        mode=args.mode,
        every=args.every,
        watch=args.watch,
        describe=args.describe,
        channels=args.channel,
        severity_floor=args.severity_floor,
        cooldown=args.cooldown,
    )
    print("Configured monitor:")
    _print_monitor(monitor)
    if args.dry_run:
        print("\n(--dry-run: not saved)")
        return 0
    store = get_store()
    await store.save_monitor(monitor.to_row())
    print(f"\nSaved as {monitor.id}.")
    if args.run:
        _print_run(await run_monitor_once(monitor, store=store, deliver=True))
    return 0


async def _list(args) -> int:
    rows = await get_store().list_monitors()
    if not rows:
        print("No monitors. Add one with `jim-monitor add <TICKER> --watch price:5 ...`.")
        return 0
    print(f"{len(rows)} monitor(s):")
    for row in rows:
        _print_monitor(Monitor.from_row(row))
    return 0


async def _rm(args) -> int:
    ok = await get_store().delete_monitor(args.id)
    print(f"Deleted {args.id}." if ok else f"No monitor {args.id}.")
    return 0 if ok else 1


async def _toggle(args, enabled: bool) -> int:
    store = get_store()
    row = await store.get_monitor(args.id)
    if not row:
        print(f"No monitor {args.id}.")
        return 1
    m = Monitor.from_row(row)
    m.enabled = enabled
    await store.save_monitor(m.to_row())
    print(f"{args.id} is now {'enabled' if enabled else 'disabled'}.")
    return 0


async def _run(args) -> int:
    store = get_store()
    row = await store.get_monitor(args.id)
    if not row:
        print(f"No monitor {args.id}.")
        return 1
    _print_run(await run_monitor_once(Monitor.from_row(row), store=store, deliver=True))
    return 0


async def _preview(args) -> int:
    store = get_store()
    row = await store.get_monitor(args.id)
    if not row:
        print(f"No monitor {args.id}.")
        return 1
    print("(preview — no delivery, no state change)")
    _print_run(
        await run_monitor_once(Monitor.from_row(row), store=store, deliver=False, persist=False)
    )
    return 0


async def _run_all(args) -> int:
    runs = await MonitorScheduler(store=get_store(), deliver=True).tick()
    if not runs:
        print("Nothing due.")
        return 0
    fired = sum(1 for r in runs if r.material)
    print(f"Ran {len(runs)} due monitor(s); {fired} produced an update.")
    for r in runs:
        _print_run(r)
    return 0


async def _serve(args) -> int:
    # Monitors deliver paid updates to subscribers — pin API-key auth so a
    # subscription credential can never back that output (ToS + AGENTS.md).
    from jim.llm import pin_api_key_mode

    pin_api_key_mode()
    sched = MonitorScheduler(store=get_store(), deliver=not args.no_deliver)
    poll = args.interval
    print(f"Scheduler running (poll every {poll}s). Ctrl-C to stop.", flush=True)
    try:
        await sched.run_forever(poll_seconds=poll)
    except (KeyboardInterrupt, asyncio.CancelledError):
        sched.stop()
        print("\nStopped.")
    return 0


async def _feed(args) -> int:
    rows = await get_store().monitor_feed(limit=args.limit, material_only=not args.all)
    if not rows:
        print("No updates yet.")
        return 0
    for row in rows:
        run = MonitorRun(
            monitor_id=row["monitor_id"],
            identifier=row["identifier"],
            product=row["product"],
            status=row["status"],
            material=row.get("material", False),
            severity=row.get("severity", "info"),
            memo=row.get("memo"),
            price_out_usd=row.get("price_out_usd", 0.0),
            cost_in_data_usd=row.get("cost_in_data_usd", 0.0),
            cost_inference_usd=row.get("cost_inference_usd", 0.0),
            delivered_to=row.get("delivered_to", []),
        )
        from jim.monitors.models import Signal

        run.signals = [Signal.from_row(s) for s in row.get("signals", [])]
        print(f"\n[{row.get('ran_at', '')}]", end="")
        _print_run(run)
    return 0


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="jim-monitor", description="Manage continuous monitors.")
    sub = p.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("add", help="Create a monitor")
    a.add_argument("identifier", help="Ticker (fundamentals) or token symbol (token)")
    a.add_argument("--product", choices=["fundamentals", "token"], default=None)
    a.add_argument("--mode", choices=["human", "agent"], default=None)
    a.add_argument("--every", help="Interval: 30m / 1h / 1d / bare seconds")
    a.add_argument(
        "--watch", nargs="*", help="Triggers: price:5 rsi:70/30 ma filing metric:Revenue:10"
    )
    a.add_argument("--describe", help="Natural-language request (parsed into triggers)")
    a.add_argument("--channel", nargs="*", help="Delivery: console webhook:https://...")
    a.add_argument("--severity-floor", choices=["info", "notable", "critical"], default=None)
    a.add_argument("--cooldown", help="Per-signal cooldown: 6h / 1d / seconds")
    a.add_argument("--run", action="store_true", help="Run once immediately after saving")
    a.add_argument("--dry-run", action="store_true", help="Show the config; do not save")
    a.set_defaults(fn=_add)

    sub.add_parser("list", help="List monitors").set_defaults(fn=_list)

    r = sub.add_parser("rm", help="Delete a monitor")
    r.add_argument("id")
    r.set_defaults(fn=_rm)

    for name, val in (("enable", True), ("disable", False)):
        t = sub.add_parser(name, help=f"{name.capitalize()} a monitor")
        t.add_argument("id")
        t.set_defaults(fn=lambda args, _v=val: _toggle(args, _v))

    run = sub.add_parser("run", help="Run one monitor now (delivers)")
    run.add_argument("id")
    run.set_defaults(fn=_run)

    pv = sub.add_parser("preview", help="Dry-run a monitor (no deliver/persist)")
    pv.add_argument("id")
    pv.set_defaults(fn=_preview)

    sub.add_parser("run-all", help="Run all currently-due monitors").set_defaults(fn=_run_all)

    sv = sub.add_parser("serve", help="Run the scheduler loop")
    sv.add_argument("--interval", type=int, default=60, help="Poll seconds (default 60)")
    sv.add_argument("--no-deliver", action="store_true", help="Run but suppress external pushes")
    sv.set_defaults(fn=_serve)

    fd = sub.add_parser("feed", help="Recent updates")
    fd.add_argument("--limit", type=int, default=10)
    fd.add_argument("--all", action="store_true", help="Include quiet/baseline runs")
    fd.set_defaults(fn=_feed)

    return p


def main() -> int:
    args = _build_parser().parse_args()
    try:
        return asyncio.run(args.fn(args))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
