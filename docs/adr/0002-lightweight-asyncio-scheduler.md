# ADR-0002 — Lightweight asyncio scheduler over APScheduler/Temporal; durability lives in the store

**Status:** Accepted

## Context

Phase 4 monitors need to run on a cadence and survive process restarts. The
build plan is explicit about sequencing: *"Lightweight scheduler first
(APScheduler/cron + queue); graduate to Temporal only if durability/fan-out
demands it."* So the question is not "what is the most powerful scheduler?" but
"what is the smallest thing that runs monitors on time, resumes after a crash,
and doesn't paint us into a corner?" A heavyweight workflow engine
(Temporal) or even APScheduler is real surface area — new dependencies, new
operational concepts — bought before we have evidence we need it.

## Decision

> Ship a dependency-free asyncio poll loop and put all the durable state in the
> database, so the scheduler itself is disposable.

[scheduler.py](../../src/jim/monitors/scheduler.py) is a plain asyncio loop:
each `tick()` asks the store for *due* monitors (`next_run_at <= now`), runs them
under a bounded-concurrency semaphore, and reschedules by persisting the next
`next_run_at`. Durability is delegated to the existing `Store` abstraction
(Postgres, in-memory fallback for tests): `next_run_at` plus the rolling
baseline and per-signal cooldown state are persisted on every run in
`_finalize()`, so a restart resumes correctly with no in-loop memory to lose.
`run_monitor_once` is the isolated unit of work, so swapping in APScheduler or
Temporal later means replacing only `scheduler.py`. The loop runs either
standalone (`jim-monitor serve`) or in-process in the FastAPI seller via
`MONITOR_AUTOSTART`.

## Consequences

**Positive**
- Zero new dependencies — it is asyncio plus the store we already have.
- Restart-safe: state lives in the DB, not the loop, so a crash loses nothing but
  in-flight work.
- Trivially testable: `tick(now)` takes an injected time, so cadence and
  rescheduling are deterministic in unit tests.
- Clean migration path: the unit of work (`run_monitor_once`) is decoupled from
  the thing that schedules it.

**Negative / trade-offs**
- No distributed coordination: two scheduler processes against one DB would
  double-run monitors (there is no row locking on `due_monitors` yet).
- Delivery is best-effort — at-least/at-most-once semantics are not guaranteed
  under crashes mid-run.
- No catch-up: a long outage means a monitor simply runs late on the next tick
  rather than backfilling the intervals it missed.

## ELI5 / what I learned

A monitor is just "do this check every N minutes, forever, even if the program
restarts." I could have reached for a big industrial scheduler, but that's a lot
of machinery for a loop. So I wrote the loop myself — wake up, ask the database
"who's due?", run them, write down when each should run next — and crucially I
kept *zero* memory inside the loop. Every "when do I run next" and "what did I
last see" lives in Postgres. That means if the process dies, the new one just
reads the database and carries on like nothing happened; the scheduler is
throwaway. The lesson: *put the durability in the data, not the process, and keep
the unit of work separable* — then you can start with the dumbest possible loop
and only upgrade to the heavy engine when fan-out or exactly-once delivery
actually forces it. The honest catch I wrote down: run two copies right now and
they'll both fire the same monitor, because I haven't added row-locking yet.
