# ADR-0009 — The eval harness: tiered suites, persisted runs, thresholded regression verdicts, and a results dashboard

**Status:** Accepted

## Context

"Is jim improving?" had no durable answer. The Phase-3 `jim-eval` printed a
gate regression (5 cases) and a debate-vs-single-pass table to stdout and
threw the numbers away: no history, no baseline, no way to say whether this
week's model/prompt/gate change made the agent better, worse, or just noisier.
Meanwhile the properties jim's pitch rests on — the sourcing gate rejects
planted lies, rejected runs are never billed (ADR-0008), monitors only speak on
material change, hostile identifiers are refused — were pinned only by pytest,
invisible to any quality trendline.

Measuring an agent needs three ingredient families the old eval lacked:
**pass/fail** at a case level (deterministic where possible), **quality** as a
number (the ADR-0006 rubric), and **economics** (tokens, dollars, latency) —
each captured per run and comparable across runs.

## Decision

> Reuse the repo's own ethos for its evals: deterministic verdicts wherever
> possible, one uniform data shape, plain files over infrastructure, and the
> same numbers everywhere (CLI, CI, dashboard).

### 1. Four suites, cheapest first

- **gate** — the sourcing-gate regression grown from 5 to 38 labeled memos,
  organized by the extractor's own surface: every notation it understands gets
  a truthful case (must pass) and a planted-lie case (must reject), so a
  regression in *either* direction is a named case. Writing this dataset
  immediately caught a real false-reject bug: `_RANGE_RE`'s single-letter scale
  class consumed the "t" of a following word ("2023-2024 the…" read as
  trillions), contradicting the gate's own docstring. Fixed in the same change
  (`(?-i:[TBMK])\b`, mirroring `_FIGURE_RE`).
- **guards** — the other deterministic rails as 40 named cases: the impersonal
  guard, identifier canonicalization (hostile inputs must refuse), the
  completeness check, monitor materiality (floors + cooldowns), and the monitor
  NL propose/dispose path (unknown kinds dropped, thresholds clamped).
- **scenarios** — the real LangGraph engine run end-to-end with scripted I/O
  seams (fake source, scripted synthesizer, judge/debate skipped, fresh
  in-memory store — the same seams `test_engine.py` patches). Nine scenarios
  pin behavior: the gate-feedback retry loop repairs a hallucination; a
  persistent hallucination is rejected **and books $0 at the ledger** (the
  never-bill-rejected invariant, asserted from the eval); the memo cache
  short-circuits inference; hostile identifiers leave no side effects; upstream
  failures fail closed; margin math lands in the store.
- **live** — the held-out tickers through the real pipeline, single-pass vs
  debate (memo cache off), each run scored by the rubric and measured for
  latency, tokens, and cost. `--repeats N` for variance. Needs
  `ANTHROPIC_API_KEY`; everything else runs with none.

Offline suites are the merge gate: 87 deterministic cases, ~1.5s, exit 1 on any
failure. The old `jim-eval --gate-only` still works.

### 2. One uniform row, one persisted document

Every suite reduces each case to the same `CaseResult` (passed, optional 0–1
score, latency ms, tokens, cost USD, drill-down details), so aggregation,
comparison, and the UI are suite-agnostic. `jim-eval run` writes one
self-contained JSON document — git sha, config snapshot (models, thresholds),
per-suite aggregates + full case rows, and a headline `summary` — to
`EVAL_RUNS_DIR` (default `./eval_runs`, gitignored). Plain files on purpose:
no database dependency, diffable, portable; the store stays the *product's*
ledger, not the harness's.

### 3. Baselines and two-regime comparison

A `BASELINE` marker file names the run others are judged against. `jim-eval
compare` (and `run --compare-baseline`, exit 1 on regression) diffs two runs in
two regimes, because the suite families mean different things:

- **Offline** suites are deterministic → zero tolerance. Any case that passed
  in the base and fails in the candidate is a regression, by name.
- **Live** is stochastic → thresholds, configurable in `Settings`
  (`EVAL_GATE_PASS_RATE_DROP` 0.05, `EVAL_RUBRIC_DROP` 0.02,
  `EVAL_COST_INCREASE_PCT` 25, `EVAL_LATENCY_INCREASE_PCT` 50). A drop inside
  the allowance is "flat", not a fire drill.

The dashboard calls the same `compare_runs()`; the CLI, CI, and UI can never
disagree about what "regressed" means.

### 4. A dashboard in the house style

`jim-eval ui` (port 4023) serves the persisted runs: trend charts for the
headline metrics (offline/live pass rates, rubric + faithfulness, $/run, p50/p95
latency), the run history table, per-case drill-down (memos, violations, judge
issues), and a run-vs-run comparison view. Built like the storefront/proof/admin
pages: one self-contained HTML document, inline CSS + vanilla JS, hand-rolled
SVG charts, **no CDN or build step** — the dashboard works fully offline, like
the suites it displays.

## Consequences

- Improvement is now a queryable time series, not a memory. "Did the prompt
  change help?" = run, compare, read the verdict.
- The offline suites give CI a fast, hermetic quality gate that covers the
  product's actual promises (including billing), not just unit behavior; the
  suites run with a developer's live `.env` without touching Postgres or
  spending tokens.
- Scenario seams are monkeypatched module attributes on `jim.research.engine`;
  renaming those seams will break scenarios loudly (they fail as cases, with the
  exception in the row) — acceptable, and cheaper than adding injection
  plumbing to the engine for the eval's sake.
- Live-suite runs still write to the real store and trust ledger (they are real
  runs); eval traffic is visible in `/proof` like any other traffic. Deliberate:
  the proof page's claim is about *all* gated runs.
- The dataset labels are enforced by `tests/test_eval_harness.py`, which
  executes every case: a mislabeled expectation or a drifted guard fails the
  test suite — the eval cannot silently rot.
- The live suite immediately earned its keep by surfacing a silent regression the
  offline gates could never see: every held-out memo came back `rejected` with
  `ok rate` at 0.00 while the sourcing gate still passed 0.875. The cause was the
  faithfulness judge's `max_tokens=900` — far too small for the per-claim checklist
  a real ~3.5k-char memo produces, so its JSON truncated mid-array, failed to parse,
  and fail-closed every run (mislabeled "unparseable output," which read like a
  model-quality problem — it was not; Haiku emitted valid JSON). Fixed by making the
  budget a configurable `judge_max_tokens` (default 4096), naming the truncation case
  honestly, and salvaging the claims emitted before the cut. This is exactly the
  "gate passes but the agent is broken" failure a persisted live `ok rate` exists to
  make visible at a glance.

## Alternatives considered

| Alternative | Why not |
|---|---|
| Langfuse as the system of record | Optional/external today (guarded no-op); trends must survive offline. Langfuse stays as best-effort tracing. |
| Rows in the Postgres store | Couples the harness to the product's schema and to having a DB; files are diffable and machine-portable. |
| DeepEval / promptfoo harness | The scoring that matters here is jim's own deterministic gate + rubric; a framework adds deps without adding verdicts. |
| Injection parameters on `run_research` for scenario seams | Widens the production API for eval-only needs; the test-proven monkeypatch seams already exist. |

## Addendum — 2026-07-14: the suites grew; counts live in the code

The case counts above are the numbers *at acceptance* and have drifted twice
since: the adversarial/injection block grew the gate suite 38 → 48 and added a
tenth scenario (`injected_source_cannot_bypass_gate_or_billing`), and the
eval-ladder work (see `docs/EVAL_LADDER.md`) added an eleventh
(`judge_fail_rejects_and_never_bills`) — closing the one L0 gap this harness
had: the `gate AND judge` rejection conjunction in `engine.py` was previously
never exercised with a failing judge verdict anywhere offline. Current totals:
48 gate + 40 guards + 11 scenarios = 99 offline cases. Treat the datasets
themselves (`src/jim/eval/dataset*.py`, `scenarios.py`) and `jim-eval run`
output as the source of truth for counts, not this ADR.

The eval maturity roadmap — judge calibration (which will revisit the
"eval traffic is visible in `/proof`" consequence above via a future ADR),
live-suite activation, and production trace sampling — is
`docs/EVAL_LADDER.md`.
