# EVAL_LADDER — the eval maturity roadmap (L0 → L3)

**Status:** Phase E1 landed. Phase E2's harness landed AND its calibration exit
run has been executed (subscription mode, run `20260715T003647Z-dea5b09`, floor
met — results below); the one remaining E2 box is operator sign-off of the 40
corpus labels, with the two borderline cases called out. E3–E4 are contracts
awaiting execution.

jim is approaching eval-first maturity: the cases become the spec, and the suite
must support real decisions (ship/block, model/prompt changes, pricing), not just
regression alarms. This document is (1) the audited answer to "where is jim on
the ladder today?" and (2) the phased plan for climbing the rest of it — one
contract per phase, each with its recurring token/$ cost and its **kill
criteria**. The ladder's own discipline applies to itself: per-layer spend must
follow consequence, and a layer that never changes a decision gets deleted.

The ladder (from the agentic-harness AGENT_ANATOMY doc, §5):

| Layer | Meaning |
|---|---|
| **L0 — verifier tests** | Planted-failure tests proving each *grader* catches what it claims. An unverified verifier is the ladder's weakest rung. |
| **L1 — offline replay** | Deterministic scenario suite: zero-credential, baseline compare, merge gate. |
| **L2 — controlled live-model evaluation** | Versioned cases, repeated trials against the live model; deterministic scorers first, human-calibrated judge only where code-grading is impossible; cost + latency recorded per run; pinned judge model. |
| **L3 — production sampling** | Real run traces captured and sample-graded; every sampled failure becomes a new L1 case **in the same diff as its fix**. |

---

## Phase 0 — audit: where jim stands (2026-07-14)

### Ladder snapshot

**L1 is done and strong.** 99 deterministic offline cases — 48 gate-regression
memos (21 truthful must-pass, 27 planted-lie/adversarial must-reject, including
the injection block), 40 guard cases across five families, 11 full-engine
scenarios — running in ~2s with zero credentials, executed on every commit
(`tests/test_eval_harness.py` via `.claude/gate.sh`) and nightly
(`.claude/evals.sh`, credential-neutralized). Runs persist as self-contained
JSON (git sha, full config snapshot, per-case latency/cost/tokens); comparison
is two-regime (offline: zero-tolerance per-case diff; live: thresholded); the
trends UI (`jim-eval ui`) renders the same `compare_runs()` verdicts CI uses.

**L0 was strong except for exactly one hole — now closed.** Every deterministic
verifier has planted-failure *and* clean-pass tests: the sourcing gate (with
two-direction Hypothesis fuzzing in `tests/test_gate_fuzz.py` plus the
adversarial dataset block), materiality, the impersonal guard, completeness,
call-chain loop/depth refusal, the trust ledger and peer trust-floor, the buyer
price-cap guard, attestation receipts, identifier canonicalization, and
monitor-NL validation. The one unverified verifier was the **LLM faithfulness
judge**: it carries 0.30 of the rubric composite and co-decides ok/rejected
(`engine.py`: `status = ok if (gate.passed and judge.passed)`), yet it was
stubbed to `.skip()` in every engine test and eval scenario — a failing judge
verdict had never been shown to reject a run or to book $0. Phase E1 (landed
with this document) closes the *deterministic* half of that hole; Phase E2
closes the *judgment* half (is the judge's verdict any good?).

**L2 is built but has never been run.** The `live` suite
(`src/jim/eval/runner.py`) already does most of what L2 asks: 8 held-out
tickers × single-pass-vs-debate variants × `--repeats N`, rubric-scored, per-case
cost and latency, pinned judge model (`claude-haiku-4-5-20251001`), config
snapshot per run. `eval_runs/` is empty on every machine and every BUILD_PLAN
"live exit run" box is unchecked. Two things block honest activation: the judge
is uncalibrated (E2), and live eval runs write into the *product's* margin
ledger and `/proof` page (ADR-0009 called this deliberate — before the suite
existed at scale). Case versioning is run-level via git sha, which is
sufficient (see judgment call 3).

**L3 does not exist.** Tracing is optional Langfuse (no-op unconfigured,
explicitly rejected as system of record in ADR-0009), and `query_records`
stores economics only — no memo, gate, or judge payload — so today there is
nothing to sample-grade.

### Hygiene findings (acted on in E1)

- Scenarios `gate_feedback_repairs_hallucination`, `gather_error_fails_closed`,
  and `paid_data_margin_accounted` had line-for-line pytest twins in
  `tests/test_engine.py`. One copy of each now remains — the scenario (it feeds
  trend history; the pytest twin fed nothing).
- `gather_error_fails_closed` vs `budget_exceeded_fails_closed` exercise the
  same engine path (source raises → `status=error`); they are now two named
  cases from one factory, preserving case names for `compare_runs()`.
- `persistent_hallucination_rejected_never_billed` and
  `injected_source_cannot_bypass_gate_or_billing` share a validator but pin
  different inputs (real-ledger slice vs injection-in-data); both stay.
- Doc drift (ADR-0009/ARCHITECTURE/README counts) corrected; counts now live in
  `jim-eval run` output, not prose.
- "Which cases have never failed?" is **unanswerable today** — no run history
  exists anywhere. E3's committed history makes the question (and the standing
  deletion rule below) answerable.

---

## Phase E1 — close L0 offline + hygiene + doc sync ✅ (landed with this doc)

**Outcome.** Every grader that participates in an ok/rejected decision has a
planted-failure test in the default hermetic suite; the scenario suite has no
pure duplicates; the docs state true numbers.

**Non-goals.** Judging the judge's verdict *quality* (that's E2). Live runs.
New suites.

**What landed.**
- `tests/test_engine.py::test_failing_judge_rejects_run_and_never_bills` — a
  gate-clean memo + a scripted failing `JudgeResult` → `status="rejected"`,
  no retry, `price_out=0`, ledger books $0. Never-bill-rejected (ADR-0008) is
  no longer a gate-only privilege.
- Scenario 11, `judge_fail_rejects_and_never_bills` — the same conjunction
  proven through the real engine + real in-memory ledger, via a new
  `judge_result_factory` seam on `Scenario` (default posture unchanged: judge
  skipped, as in any offline run).
- `tests/test_judge.py::test_score_at_threshold_passes_and_below_fails` — the
  `passed = score >= judge_threshold` boundary is pinned (inclusive at, failing
  below).
- Hygiene + doc sync as listed in the audit above.

**Acceptance evidence.** `uv run pytest` hermetic and green (verified).
Mutation check: flipping the `engine.py` conjunction `and → or` makes both new
tests fail; reverting restores green (verified in this diff's development).

**Recurring cost.** $0. ~1s added to the commit gate.

**Kill criteria.** N/A — this *is* the verifier rung. It establishes the
standing rule the rest of the ladder enforces: **quarterly, any offline case
that has never failed since inception and whose asserted path is pinned by
another named case is a merge candidate.**

---

## Phase E2 — L0 for the judge: labeled calibration behind a credentialed command
### (harness ✅ · calibration run ✅ · label sign-off ☐ — the last open box)

**Outcome.** The faithfulness judge gets the same standing as every other
grader: a planted-failure corpus proving it catches what it claims, a measured
error rate, and a `judge_threshold` chosen from data instead of vibes.

**Non-goals.** End-to-end live runs (calibration memos are authored, not
synthesized). Judge-prompt rewrites unless calibration fails. A generic
"LLM judge framework."

**What landed.**
1. `src/jim/eval/dataset_judge.py` — **40 operator-labeled cases** over a shared
   fact book: 15 faithful (including hedged-but-grounded phrasings — the set
   that measures false rejects) and 25 unfaithful across the five families the
   gate *cannot* see: `unsupported_claim` (6), `editorialization` (5, phrased
   to slip the impersonal regexes), `misleading_comparison` (5, correctly-cited
   figures in a false relation — e.g. "buybacks of $5.0B dwarf capex of $11B"),
   `causal_overreach` (5), `wrong_citation` (4). Every case carries a
   one-sentence label rationale — the human label IS the calibration standard.
   **Design property, enforced by test:** every memo passes the sourcing gate
   AND the deterministic impersonal guard, so the corpus lives strictly in the
   deterministic blind spot — whatever the judge scores here is signal only the
   judge can provide (`tests/test_judge_calibration.py::
   test_every_case_is_in_the_deterministic_blind_spot`).
2. `jim-eval judge-calibrate` — **requires a key, never in default pytest**
   (AGENTS.md hermeticity invariant; exits 2 with a cost warning when no
   credential). Runs the pinned `judge_model` on each case × `--repeats 3`;
   reports confusion matrix, per-family recall, verdict flip-rate across
   repeats, and a threshold sweep 0.5–0.95 (`src/jim/eval/calibrate.py` — pure
   deterministic math, fully unit-tested); persists as a normal run document
   (`suite="judge"`) so storage, `list/show`, and the UI work unchanged. Exits
   nonzero below the floor: **balanced accuracy ≥ 0.85 and false-reject rate
   ≤ 5%** (`eval_judge_min_balanced_accuracy` / `eval_judge_max_false_reject`
   in `src/jim/config.py`).

**Calibration results — run `20260715T003647Z-dea5b09` (2026-07-15).**
Executed via `--auth-mode subscription` (ADR-0010 dev-loop path; $0 marginal,
notional API-price cost $0.80): 40 cases × 3 repeats = 120 calls to the pinned
`claude-haiku-4-5-20251001`, 31 min wall (p50 13.3s/call), exit 0 — **floor
met**. Full document in `eval_runs/` (machine-local; durable history is E3).

| threshold | balanced acc | lie recall | false-reject | flip rate |
|---|---|---|---|---|
| 0.80 (old default) | 0.9333 | **1.0000** (25/25, every family 100%) | 0.1333 (2/15) | 0.05 |
| 0.70–0.75 | **0.9667** | 1.0000 | 0.0667 (1/15) | 0.05–0.10 |
| **0.55–0.60 (chosen)** | 0.9600 | 0.9200 (23/25) | **0.0000** | 0.10 |

The 5% false-reject cap binds: 0.70–0.75 has the best raw accuracy but kills
1-in-15 faithful memos (refused revenue under never-bill-rejected), so the
floor-first rule chose **0.55**, now set in `src/jim/config.py` with this
run_id — **provisional until label sign-off**. The four borderline cases the
operator should read first:

- `leverage_uncharacterized` (faithful, medians 0.4–0.7): the judge reads
  "leverage at this level cuts both ways" as an uncited evaluative claim.
  Either the label stands (judge is over-strict on hedges) or the memo phrasing
  deserves a fix — this single case is what pushes 0.70–0.75 over the cap.
- `guidance_reiterated_plain` (faithful, median 0.75): dinged for the
  meta-statement "no inference beyond the stated range is drawn".
- `attractive_entry_point` (editorialization, median 0.65): regex-proof advice
  that the judge under-scores — at 0.55 it would SHIP. If sign-off deems this
  unacceptable, the floor cap (or the label set) is the thing to change, and
  0.70+ becomes the operating point.
- `loss_fully_offset` (misleading_comparison, median 0.65, scores 0.6–0.93):
  the least stable verdict in the corpus.

**Remaining (the last E2 box).**
3. **Operator sign-off of the 40 labels** (≤1 h; start with the four cases
   above). If any label flips, re-run `jim-eval judge-calibrate` (~$1 or $0
   under subscription) and re-derive the threshold; if the advice-leak at 0.55
   is unacceptable, adjust `eval_judge_max_false_reject` and re-choose.
4. Recalibration triggers thereafter: any change to the judge prompt
   (`_SYSTEM`), `judge_model`, or `judge_threshold`; plus a quarterly drift
   check.

**Acceptance evidence.** Harness: hermetic tests cover dataset composition, the
blind-spot property, the calibration math, the run-document shape, and both CLI
exit paths (floor met → 0, floor not met → 1, no credential → 2). Exit run: the
calibration summary above (confusion at old/chosen thresholds, per-family
recall, flip rate, cost, run_id); `judge_threshold` in config is traceable to
run `20260715T003647Z-dea5b09`.

**Recurring cost.** Judge call ≈ 2k tokens in + 1.2k out on Haiku ($1/$5 per
MTok) ≈ $0.008/call → 40 cases × 3 repeats ≈ **$0.96/calibration run**;
on-change + quarterly ≈ **$4–8/year**. One-time: 2–3 h authoring/labeling.

**Kill criteria (for the judge layer itself).**
- If calibration cannot reach the floor at *any* threshold after one prompt
  iteration → the judge must not co-decide ok/rejected: demote to advisory
  (drop from the `engine.py` conjunction, keep as a recorded metric) and
  reallocate its 0.30 rubric weight.
- Same demotion if, after 3 months of live runs + sampling (E3/E4), the judge
  has **zero unique true catches** (gate passed, judge failed, human agrees)
  and ≥2 confirmed false rejections. A 0.30-weight grader that never changes a
  decision the gate didn't already make gets deleted from the decision path.

---

## Phase E3 — L2 activation: spend separation, durable history, first live baseline

**Outcome.** The already-built live suite actually runs, on cadence, without
polluting the product ledger, and its results survive the machine they ran on.
"Which cases never fail" and "is jim improving" become answerable from a fresh
clone.

**Non-goals.** New live scorers. Expanding the ticker grid. Production traffic
(E4). GitHub Actions (the repo deliberately has none — nightly stays the
external digest convention).

**Work items (1–2 land before the first live run).**
1. **Eval/product spend separation.** Nullable indexed
   `query_records.run_context` column (`NULL` | `"eval:{run_id}"`); idempotent
   `ALTER TABLE ... ADD COLUMN IF NOT EXISTS` in `init_db` (the store is
   `create_all`-only, no alembic); thread `run_context` through
   `run_research → record_query`; `margin_summary` / `recent_queries` /
   `/proof` default-exclude `eval:%` rows behind an explicit include toggle.
   **This supersedes ADR-0009's "eval traffic is visible in /proof —
   deliberate" consequence and needs its own ADR** (judgment call 1).
2. **Durable history.** `jim-eval export`: one compact JSON line per run
   (run_id, git sha/branch, label, config snapshot, summary, per-case pass map
   — no memos), appended to committed monthly-sharded
   `evals/history/YYYY-MM.jsonl` (~2–4 KB/line; a year of nightlies ≈ 1 MB),
   plus a committed `evals/history/BASELINE.json`. `compare.py` learns to
   accept an exported summary as the base side (per-case booleans are all
   `_offline_diff` needs). Full run documents stay gitignored/machine-local
   exactly as ADR-0009 intended: drill-down local, decision-grade summary
   committed. `.claude/evals.sh` appends after the nightly offline run.
3. **Spend guard.** `--max-spend` on `jim-eval run` (config
   `EVAL_MAX_SPEND_USD`, default $5): abort remaining live cases past the cap,
   mark the run partial. No silent overruns.
4. **Cross-mode compare guard.** Refuse (or annotate "n/a") cost-metric
   comparison when base and candidate `llm_auth_mode` differ — subscription
   runs have notional cost only (ADR-0010).
5. **First activation run** (a documented ceremony): clean checkout, `api_key`
   mode, `jim-eval run --suite all --repeats 3 --label live-activation` →
   operator review → `jim-eval baseline set` → commit exported baseline + the
   new ADR → check the BUILD_PLAN debate-vs-single-pass exit-run box.
6. **Cadence.** Weekly live run (`live-weekly` label) + on-demand before any
   model/prompt/gate change. Nightly stays offline-only.

**Acceptance evidence.** ≥1 committed live run summary and `BASELINE.json` in
`evals/history/`; an offline test proving `margin_summary()` is unchanged by an
eval-tagged run (MemoryStore); `compare` vs baseline works on a fresh clone
with an empty `eval_runs/`.

**Recurring cost.** Sonnet 4.6 at $3/$15 per MTok: single-pass case ≈ $0.035
(synth ~3k in/800 out × mean 1.3 attempts + judge $0.008; held-out tickers are
equities → EDGAR is free); debate case ≈ $0.075. Grid 8 tickers × 2 variants ×
3 repeats = 48 cases ≈ **$2.65/run** (budget $3–4 with retry variance) →
**$12–15/month weekly**; wall clock 25–45 min; ~15 min/week operator review.

**Kill criteria.**
- 8 consecutive scheduled runs verdict "flat" AND no decision (merge block,
  revert, baseline promotion, model/prompt/threshold change) traces to the
  live suite → cut weekly → monthly. 3 more decision-free monthly runs →
  on-demand only (pre-change + pre-release).
- If debate shows lift ≤ 0 on quality at ~2× cost across 3 runs → drop debate
  from the standing grid. (That verdict is itself the suite earning its keep —
  record it, then shrink.)
- Any run hitting `--max-spend` hard-stops.

---

## Phase E4 — L3: production trace capture + sample-grading + the failure→L1 loop

**Precondition.** Real traffic. Below ~20 gated runs/week: build capture (item
1 — cheap, aids debugging), but do **not** run the sampling loop; sampling
noise on single-digit traffic changes nothing.

**Outcome.** Real customer runs leave a gradeable trace; a weekly command
grades a stratified slice; every confirmed sampled failure becomes a named L1
case in the same diff as its fix, with provenance.

**Non-goals.** Always-on LLM grading of production traffic. A Langfuse
dependency (stays optional best-effort tracing). Customer-visible changes.

**Work items.**
1. **Trace capture.** Append-only `research_traces` table (mirrors the existing
   `MonitorRunRow.data` split — `QueryRecord` stays a lean economics ledger):
   link to query_record, identifier, status, memo, serialized snapshot facts +
   `as_of` + fingerprint (required — without the facts nothing can be
   re-graded), gate JSON, judge JSON, attempts, `run_context`, created_at.
   Written post-finalize behind `settings.capture_traces` (default on when
   `DATABASE_URL` is set; MemoryStore keeps a list so scenarios can assert on
   it). Retention: `prune-traces --keep-days 90`. ~8 KB/trace ≈ 10 MB/year at
   current scale.
2. **Sampling command.** `jim-eval sample --since 7d --limit 20` (needs
   `DATABASE_URL`; never default CI). Stratified: *all* rejected/error traces +
   a random sample of ok. For each: re-run the deterministic graders
   (`check_sourcing`, completeness, impersonal, rubric) on the stored
   memo+facts — free, and catches drift in both directions ("shipped ok but
   today's gate rejects it" and the reverse); `--judge` optionally re-judges
   sampled ok memos. Output: a worksheet with disagreement flags for human
   review.
3. **Failure→L1 mechanism.** `jim-eval triage <trace_id>` emits ready-to-paste
   dataset code — a `GateCase` (sourcing-shaped failure), `JudgeCase`
   (faithfulness-shaped), or `Scenario` skeleton — with memo/facts inlined and
   a provenance comment: `# origin: trace=<id> sha=<git> sampled=<date>`.
   **The rule (to be written into AGENTS.md when this phase lands): the diff
   that fixes a sampled failure contains its triage-generated case.**
   Enforcement is commit-gate culture + the nightly digest — social, not
   mechanical, and this document says so honestly.

**Acceptance evidence.** One completed sampling cycle documented: N traces
graded, disagreements listed, ≥0 new provenance-tagged L1 cases landed (a
first cycle finding *zero* failures is valid evidence too — record it).
`pytest` stays hermetic (capture path covered via MemoryStore).

**Recurring cost.** Deterministic re-grades: $0. Judge re-runs: 20/week ×
$0.008 ≈ **$0.70/month**. Storage ≈ 10 MB/year. Dominant cost: **30–45
min/week of human review** — stated here so it's budgeted, not discovered.

**Kill criteria.**
- 2 consecutive monthly cycles with zero new L1 cases and zero grader
  disagreements → cut sample 20 → 5/week. 2 more empty cycles → stop scheduled
  sampling (keep `triage` for complaint-driven use).
- No traffic within 2 quarters → disable `capture_traces` by default, shelve
  the phase.
- Triage output that repeatedly duplicates existing L1 case classes means the
  L1 suite already covers reality — that is success; shrink the loop.

---

## Cost summary

| Phase | Recurring token/$ | Human | Basis |
|---|---|---|---|
| E1 | $0 | one-time, landed | offline only |
| E2 | ~$1/calibration; $4–8/yr | ≤1 h label review (corpus drafted; labels await operator sign-off) | 40 cases × 3 reps × $0.008 (Haiku, ~2k in / 1.2k out) |
| E3 | ~$3/run; $12–15/mo weekly | 15 min/wk | 48 cases: 24 × $0.035 + 24 × $0.075 (Sonnet $3/$15) |
| E4 | ~$0.70/mo + ~10 MB/yr | 30–45 min/wk | 20 samples/wk × $0.008 judge; deterministic re-grades free |
| **Steady state** | **≈ $15/month worst case** at API pricing ($0 marginal under subscription auth for dev-loop runs — ADR-0010) | ~1 h/wk | |

Spend follows consequence: $0 where deterministic code suffices (L0/L1),
dollars only where the live model or production reality is the thing under
test (L2/L3).

## Judgment calls (recommendations recorded for sign-off)

1. **Reversing an ADR-0009 consequence.** ADR-0009 deliberately kept eval
   traffic visible in `/proof` ("the proof page's claim is about *all* gated
   runs") — written before the live suite had ever run. 48 eval cases/week
   would dominate a pre-revenue ledger. Recommendation: supersede in E3 via a
   new ADR — tag with `run_context`, default-exclude, keep an include toggle so
   the "all gated runs" claim survives as an opt-in view. Needs explicit
   sign-off because it reverses an accepted decision.
2. **Where history lives.** Recommendation: compact committed summaries in
   `evals/history/` on main (monthly-sharded JSONL + `BASELINE.json`); full run
   docs stay gitignored/machine-local. Alternative if main-branch noise is
   unacceptable: an orphan `eval-history` branch. CI artifacts are not an
   option (no GitHub Actions, by design).
3. **Per-case version fields: no.** Cases live in `dataset*.py` and are
   versioned by the git sha stamped on every run; `compare.py` keys on case
   *names*. Conventions instead: a semantic change to a case is a **new case
   name** (renames surface as add+delete in the offline diff), and L3-born
   cases carry an `origin:` provenance comment. A version integer is
   bookkeeping that never changes a decision — the ladder's own deletion rule
   applies to it.
4. **Scenario-dedup direction (taken in E1).** Scenarios kept, pytest twins
   deleted — the scenario copy feeds committed trend history; the pytest copy
   fed nothing. Either way one copy had to go.
5. **Auth mode for live runs.** Baselines and weekly runs under `api_key` (true
   costs, production parity); exploratory runs may use subscription ($0
   marginal, permitted for the dev-loop per ADR-0010). The E3 compare guard
   keeps cost metrics from silently comparing across modes.
6. **Trace storage shape.** Separate `research_traces` table with its own
   retention (mirrors `MonitorRunRow.data`), keeping `QueryRecord` a lean
   ledger. Alternative: a JSON column on `query_records` — one fewer table, but
   couples trace retention to the economics ledger.
