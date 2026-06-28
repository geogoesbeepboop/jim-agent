"""Lightweight async scheduler — the continuous loop behind the crew.

Per the build plan: *"Lightweight scheduler first (APScheduler/cron + queue);
graduate to Temporal only if durability/fan-out demands it."* This is that first
step — a dependency-free asyncio loop that polls the store for **due** monitors
(``next_run_at <= now``), runs them with bounded concurrency, and reschedules.

Durability lives in the store, not the loop: ``next_run_at`` is persisted, so a
restart resumes from where it left off. Swapping in APScheduler/Temporal later
means replacing only this file — :func:`run_monitor_once` is the unit of work.
"""

from __future__ import annotations

import asyncio
from datetime import datetime

from jim.config import get_settings
from jim.monitors.engine import run_monitor_once
from jim.monitors.models import Monitor, MonitorRun
from jim.store import Store, get_store


class MonitorScheduler:
    def __init__(
        self,
        *,
        store: Store | None = None,
        deliver: bool = True,
        max_concurrency: int | None = None,
    ):
        settings = get_settings()
        self.store = store or get_store()
        self.deliver = deliver
        self._sem = asyncio.Semaphore(max_concurrency or settings.monitor_max_concurrency)
        self._stop = asyncio.Event()

    async def tick(self, now: datetime | None = None) -> list[MonitorRun]:
        """Run every monitor that is currently due. Errors are captured per-run."""
        due = await self.store.due_monitors(now)

        async def _run(row: dict) -> MonitorRun:
            async with self._sem:
                try:
                    return await run_monitor_once(
                        Monitor.from_row(row), store=self.store, deliver=self.deliver, now=now
                    )
                except Exception as e:  # one bad monitor must not sink the whole tick
                    return MonitorRun(
                        monitor_id=row.get("id", "?"),
                        identifier=row.get("identifier", "?"),
                        product=row.get("product", "?"),
                        status="error",
                        error=str(e),
                    )

        return list(await asyncio.gather(*(_run(r) for r in due)))

    async def run_forever(self, poll_seconds: int | None = None) -> None:
        """Poll-and-run until :meth:`stop` is called (or the task is cancelled)."""
        poll = poll_seconds or get_settings().monitor_poll_seconds
        while not self._stop.is_set():
            try:
                runs = await self.tick()
                fired = [r for r in runs if r.material]
                if fired:
                    print(
                        f"[scheduler] {len(runs)} due, {len(fired)} update(s): "
                        + ", ".join(f"{r.identifier}:{r.severity}" for r in fired),
                        flush=True,
                    )
            except Exception as e:  # a tick must never kill the loop
                print(f"[scheduler] tick error: {e}", flush=True)
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=poll)
            except asyncio.TimeoutError:
                pass

    def stop(self) -> None:
        self._stop.set()
