"""The monitor engine — one tick of a single monitor.

    gather → diff(baseline) → crew → materiality gate ──quiet──→ record (free)
                                            │ material
                                            ▼
                                synthesize update (gated + impersonal)
                                            │
                                deliver → record → roll baseline forward

This deliberately mirrors the research engine's "deterministic decides, model
writes" split: the crew + materiality gate (no model) decide whether to speak;
the synthesizer (model, optional) only writes once they say yes. A *quiet* poll
costs no inference and pushes nothing — most polls are quiet, which is the whole
economic point of monitoring (and is what ``inference_saved_usd`` measures).
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import httpx

from jim.config import get_settings
from jim.monitors.diff import diff_snapshots, snapshot_to_baseline
from jim.monitors.materiality import assess
from jim.monitors.models import Monitor, MonitorRun
from jim.monitors.notify import build_channels, build_payload, deliver_all
from jim.monitors.triggers import evaluate_all
from jim.monitors.update import synthesize_update
from jim.research.budget import BudgetCap
from jim.research.edgar import EdgarError
from jim.research.products import get_product, usd
from jim.sources import BudgetExceeded, ProcurementError
from jim.store import Store, get_store


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _citations(snapshot) -> list[str]:
    return [f.citation() for f in snapshot.facts] if snapshot else []


async def run_monitor_once(
    monitor: Monitor,
    *,
    store: Store | None = None,
    deliver: bool = True,
    persist: bool = True,
    now: datetime | None = None,
) -> MonitorRun:
    """Run one monitor cycle, mutating + persisting its rolling state.

    Args:
        deliver: push to the monitor's external channels (console/webhook).
        persist: save the run to the feed + roll the monitor's baseline forward.
                 ``preview`` runs pass ``persist=False, deliver=False``.
    """
    store = store or get_store()
    settings = get_settings()
    now = now or _utcnow()

    run = MonitorRun(
        monitor_id=monitor.id,
        identifier=monitor.identifier,
        product=monitor.product,
        status="quiet",
        ran_at=now,
    )

    # 1. Gather a fresh snapshot (paid sources go through budget + cache).
    try:
        spec = get_product(monitor.product)
        result = await spec.source.gather(
            monitor.identifier,
            budget=BudgetCap(ceiling_usd=settings.per_query_budget_usd),
            store=store,
        )
    except (EdgarError, BudgetExceeded, ProcurementError, ValueError, httpx.HTTPError) as e:
        # httpx errors matter for monitors specifically: continuous polling will
        # eventually hit a transient SEC/feed 429/503 or a network blip. Catch it
        # so the run is recorded as an error AND still reschedules (no hot-loop).
        run.status = "error"
        run.error = str(e)
        await _finalize(monitor, run, store, now, settings, roll_baseline=False, persist=persist)
        return run

    snapshot = result.snapshot
    run.cost_in_data_usd = round(result.cost_in_usd, 6)
    run.cache_hit = result.cache_hit

    # 2. Diff against the last baseline. First run only establishes one.
    diff = diff_snapshots(monitor.baseline, snapshot)
    if diff.is_first_run:
        run.status = "baseline"
        await _finalize(
            monitor,
            run,
            store,
            now,
            settings,
            roll_baseline=True,
            snapshot=snapshot,
            persist=persist,
        )
        return run

    # 3. The crew + the deterministic materiality gate.
    signals = evaluate_all(monitor.triggers, diff, snapshot)
    verdict = assess(
        signals,
        severity_floor=monitor.severity_floor,
        cooldown_seconds=monitor.cooldown_seconds,
        cooldowns=monitor.cooldowns,
        now=now,
    )
    monitor.cooldowns = verdict.cooldowns  # persist cooldown memory either way

    if not verdict.material:
        run.status = "quiet"
        await _finalize(
            monitor,
            run,
            store,
            now,
            settings,
            roll_baseline=True,
            snapshot=snapshot,
            persist=persist,
        )
        return run

    # 4. Material → pay to write (gated + impersonal), then deliver.
    update = await synthesize_update(
        snapshot, verdict.published, severity=verdict.severity, mode=monitor.mode
    )
    run.status = "material"
    run.material = True
    run.signals = verdict.published
    run.severity = verdict.severity
    run.memo = update.memo
    run.gate_passed = update.gate.passed
    run.cost_inference_usd = round(update.inference_cost_usd, 6)
    run.price_out_usd = usd(settings.monitor_update_price)

    if deliver:
        channels = build_channels(monitor.channels)
        if channels:
            payload = build_payload(monitor, run, _citations(snapshot))
            run.delivered_to = await deliver_all(channels, payload)

    await _finalize(
        monitor, run, store, now, settings, roll_baseline=True, snapshot=snapshot, persist=persist
    )
    return run


async def _finalize(
    monitor: Monitor,
    run: MonitorRun,
    store: Store,
    now: datetime,
    settings,
    *,
    roll_baseline: bool,
    snapshot=None,
    persist: bool,
) -> None:
    """Roll monitor state forward, reschedule, and persist the run + monitor."""
    monitor.last_run_at = now
    monitor.last_status = run.status
    monitor.next_run_at = now + timedelta(seconds=monitor.interval_seconds)
    if roll_baseline and snapshot is not None:
        monitor.baseline = snapshot_to_baseline(snapshot)
    if persist:
        await store.record_monitor_run(run.to_row())
        await store.save_monitor(monitor.to_row())
