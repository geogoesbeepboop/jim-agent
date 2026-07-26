# Eval methodology audit — 2026-07-25

Scored against the house `/evals` contract (D1–D8; the skill in `agentic-harness/claude/skills/evals/SKILL.md`
is the north star — this project's conventions migrate toward it, never the reverse). Produced by a
fresh-context critic reading only the repo. **This document is the audit and the migration plan; the
migration itself is separate, reviewed work.**

**Headline:** the strongest D1 (programmatic-first) in the fleet, with a genuinely well-built nightly
doorknob — but the judge that co-decides revenue is calibrated against an artifact that exists on no
machine, the regression gate silently no-ops off this laptop (baseline is gitignored), and everything
that grades the *model* (the live suite) has never run once.

---

## A. Current-state map

| Suite | Invoked by | What it grades | Cases | Grading flavor |
|---|---|---|---|---|
| `gate` | `jim-eval run --suite gate\|offline\|all`; `jim-eval --gate-only`; `tests/test_eval_harness.py::test_every_gate_case_behaves_as_labeled` | `check_sourcing()` on 48 labeled memos over a shared fact book (19 truthful must-pass, 19 planted-lie must-reject, 10 adversarial/injection) | 48 | Programmatic (exact, no model) |
| `guards` | same runner; `tests/test_eval_harness.py::test_every_guard_case_passes` | 5 non-gate rails: impersonal tone (8), identifier canonicalization/refusal (14), completeness (4), monitor materiality w/ fixed clock (5), monitor-NL propose/dispose validation (9) | 40 | Programmatic |
| `scenarios` | same runner; `tests/test_eval_harness.py::test_every_scenario_passes` | Real LangGraph engine end-to-end with scripted seams: retry repair, never-bill-rejected at the ledger, memo cache, hostile-ID refusal before side effects, fail-closed, injection inert, margin math, judge-fail rejection | 11 | Programmatic (asserts outcome **and** process: `attempts`, `synth_calls`, `store.queries`, ledger) |
| `live` | `jim-eval run --suite live\|all [TICKERS]` (needs credential) | 8 held-out tickers × {single_pass, debate} × `--repeats`, through the real pipeline; scored by `jim.eval.rubric.score_memo` (sourcing .40 / faithfulness .30 / completeness .20 / impersonal .10); per-case cost, tokens, latency; debate−single_pass lift | 16 × repeats | Mixed: 3 programmatic rubric dimensions + 1 LLM-judge dimension. **Never run — zero live runs exist in `eval_runs/`** |
| `judge` | `jim-eval judge-calibrate` only (not reachable via `--suite`; `cli.py:371` choices exclude it) | 40 operator-labeled memos (15 faithful / 25 unfaithful across 5 families) through the pinned `claude-haiku-4-5-20251001`, ×3 repeats → confusion matrix, per-family recall, flip rate, threshold sweep 0.5–0.95, chosen threshold, floor verdict | 40 × 3 | LLM judge scored against **human labels**; all downstream math deterministic (`src/jim/eval/calibrate.py`) |

Supporting machinery: `src/jim/eval/metrics.py` (uniform `CaseResult`), `storage.py` (JSON run docs + `BASELINE` marker), `compare.py` (two-regime: offline exact per-case diff, live thresholded), `ui.py` (`jim-eval ui` on :4023, same `compare_runs()` verdicts CI uses).

Verified live during the audit: `./.venv/bin/jim-eval run --suite offline --no-save --compare-baseline` → 99 cases, 100%, 1.25s, $0.0000, verdict FLAT.

## B/C. Repo facts

- **Agent instructions:** `AGENTS.md` (4.3 KB, substantive); `CLAUDE.md` is an 11-byte pointer.
- **docs/ exists** with 10 top-level docs + `docs/adr/` (0001–0010). Eval-relevant: `docs/EVAL_LADDER.md` (23 KB, L0→L3 roadmap with per-phase cost + kill criteria), `docs/adr/0009-eval-harness-persisted-runs-tiered-suites.md`. **No `docs/specs/`.**
- **Remote:** `origin https://github.com/geogoesbeepboop/jim-agent.git`; default branch `main` at `6d01b81` (merge of PR #15, 2026-07-25).
- **Working tree at audit time:** dirty — `M .claude/gate.sh`, untracked `.codex/`.
- `eval_runs/` is gitignored (`.gitignore:32`); it holds 27 offline nightly runs (2026-07-06 → 2026-07-25) plus `BASELINE` → `20260706T024615Z-8c8c434`.

---

## Rubric verdicts

### D1 Programmatic-first grading ladder — **MET**

99 of 99 default cases are exact code checks with zero model calls (`runner.py:33` `OFFLINE_SUITES`). The judge is confined to the deterministic blind spot *by an enforced test*: `tests/test_judge_calibration.py::test_every_case_is_in_the_deterministic_blind_spot` asserts every one of the 40 judge cases passes both `check_sourcing` and `check_impersonal`, so the judge is only measured where code cannot reach. Human labels appear exactly once, as the judge's calibration standard (`dataset_judge.py`, every case carries a `rationale`). `rubric.py:78-85` drops the faithfulness dimension and renormalizes offline, so the quality score is model-free by default. The ladder is written down (`docs/EVAL_LADDER.md`, L0–L3 table). This is the strongest dimension in the suite; nothing to close.

### D2 Statistical honesty — **PARTIAL**

Exempt-and-honest for what actually runs unattended: `.claude/evals.sh` runs only `gate+guards+scenarios`, all deterministic, N=1 correctly. The determinism claim is documented (`compare.py:5-13`, `EVAL_LADDER.md:32-40`) *and* empirically corroborated — parsing all 27 persisted runs: 98 distinct cases, **zero failures ever, in any run**, so the pass/fail noise floor is measured at 0. pass^k is trivially satisfied because nothing LLM-dependent is unattended.

Gaps: (a) the `live` suite's `--repeats` defaults to **1** (`cli.py:376`), and both the CLI docstring (`cli.py:5`) and `README.md:161,189` demonstrate single-run invocations, so the documented usage produces single-run booleans — `_run_live_case` sets `passed = (result.status == "ok")` per run with no rate aggregation per case; (b) the live noise floor is entirely unknown (never run); (c) the judge suite *does* do it right (repeats=3, median-of-repeats, explicit `flip_rate` as its own axis, `calibrate.py:92-106`) but its one real run's numbers cannot be reproduced from the repo (see D3). Close by: make `--repeats` ≥3 mandatory for any live run that feeds a baseline or verdict; record the observed live pass-rate variance in `EVAL_LADDER.md` after activation; add a written pass^k rule before any LLM-dependent suite joins `.claude/evals.sh`.

### D3 Judge validity — **PARTIAL** (strict reading of "calibrated *before* trusted": the core sub-criterion is unverifiable)

Present and genuinely good: a 40-case human-labeled corpus with per-case rationale; `jim-eval judge-calibrate` runs the pinned model ×3 and produces confusion matrix, per-family recall, flip rate, a 0.5–0.95 sweep, and a floor-first threshold choice (`calibrate.py:120-159`) with hard floors in config (`config.py:250-251`: balanced accuracy ≥ 0.85, false-reject ≤ 0.05) and CLI exit 1 when the floor isn't met (`cli.py:270`). The chosen threshold is traceable in prose (`config.py:76-83` cites run `20260715T003647Z-dea5b09`).

What fails verification:
1. **The calibration artifact does not exist anywhere on this machine.** `eval_runs/` contains no `20260715T003647Z-dea5b09.json`; the only occurrences of that run id in the whole tree are `docs/EVAL_LADDER.md` and `src/jim/config.py` (prose). `eval_runs/` is gitignored, so no reviewer, CI job, or fresh clone can check the numbers behind the live `judge_threshold`.
2. **The labels are self-declared unsigned.** `EVAL_LADDER.md:6-8,195-199` calls label sign-off "the last open box" and the threshold "provisional"; `config.py:81-82` repeats it. Meanwhile `judge_threshold = 0.55` is *live in the decision path*: `engine.py:179` `status = "ok" if (state["gate"].passed and judge.passed)`, and rejection means $0 booked (ADR-0008). The docs concede that at 0.55 the known advice-leak case `attractive_entry_point` **ships** (`EVAL_LADDER.md:188-191`). The judge is being trusted with revenue on a threshold derived from unratified labels.
3. **No cross-model-family judging.** Production memos come from `claude-sonnet-4-6`, judged by `claude-haiku-4-5` — same family, same provider; self-preference bias is unmeasured. No verbosity/length guard exists in `_SYSTEM` (`judge.py:28-44`) or in the corpus (no long-vs-short paired case).
4. **Distribution mismatch.** The corpus memos are hand-authored (`EVAL_LADDER.md:136-137` states this as a deliberate non-goal). The measured error rate is on human prose, not on Sonnet prose — the population the judge actually grades. Calibration transfer is assumed, not shown.
5. Pairwise order-swap is **N-A**: this is absolute single-output grading, not preference ranking. Say so explicitly rather than adding ceremony.

Close by: commit the calibration summary block to a tracked path; complete label sign-off; re-run and re-derive after sign-off; add a distribution-matched validation slice (human-label ~20 real Sonnet memos from the first live run and re-measure); add a length-paired case doublet and one cross-family judge run on the same corpus, or write an ADR accepting single-family with the reason.

### D4 Suite honesty — **PARTIAL**

Solid parts: config is versioned *with* every run (`runner.py:376-395` snapshots research/judge/debate models, `enable_judge`, `judge_threshold`, `research_max_attempts`, auth mode) alongside git sha/branch (`storage.py:36-49`); the eval set is versioned by that sha, with a deliberate written decision that per-case version integers are bookkeeping (`EVAL_LADDER.md:381-386`); baseline/regression compare exists and is two-regime with thresholds in `Settings` (`config.py:243-246`).

Gaps, ranked:
1. **Baseline is machine-local and gitignored** → on a fresh clone `_cmd_run` hits `baseline_id is None`, prints `(no baseline set — skip regression check)` and **exits 0** (`cli.py:174-183`). The regression half of the gate silently no-ops everywhere except this laptop.
2. **Baseline is stale and cross-config.** `eval_runs/BASELINE` → `20260706T024615Z-8c8c434` (19 days old): 87 cases, `judge_threshold: 0.8`; HEAD produces 99 cases at `judge_threshold: 0.55`. `compare_runs` does not inspect `config` at all, so nothing warns that the two runs were produced under different settings.
3. **No holdout slice.** Every offline case runs on every commit via `gate.sh` → `pytest` → `test_eval_harness.py`. Nothing is withheld from iteration; the only "held-out" artifact is the 8 live tickers (`dataset.py:21`), which is input-contamination control, not iteration control — and the docstring's claim they're "not referenced anywhere in prompts/code paths" is literally false (AAPL/MSFT/NVDA appear in `research/cli.py:3-6`, `marketplace/catalog.py:34`, `marketplace/ui.py:232`), though they do **not** appear in `synthesize.py`, `debate.py`, or `judge.py` prompts.
4. **Dead-sensor rule written but never fired.** `EVAL_LADDER.md:122-124` states the quarterly rule; `EVAL_LADDER.md:86-88` says "which cases have never failed" is unanswerable. It is now answerable and the answer is *all of them* — 98 cases, 27 runs, zero failures. The rule's trigger condition is met and unacted. (Caveat in the suite's favor: these cases *would* fail during development inside `pytest`, so "never failed in nightly" is weaker evidence of deadness than it looks; the mutation check in `EVAL_LADDER.md:115-118` — flipping the `engine.py` `and`→`or` — is the right instrument and was done once, manually, unrecorded.)

### D5 Trajectory grading — **PARTIAL**

Offline is genuinely good: scenario validators assert outcome *and* process from real engine state — `_v_retry_recovers` asserts `attempts == 2 and synth_calls == 2`; `_v_memo_cache` asserts `served_from_cache`, zero input tokens, exactly one synth call; `_v_hostile_identifier` asserts `len(store.queries) == 0 and synth_calls == 0` (refusal *before* side effects); `_v_rejected_never_billed` / `_v_judge_fail_rejected_never_billed` assert at the ledger (`margin_summary()`), not just the return value. The path is not over-specified — validators check a handful of load-bearing counters, not a step transcript.

Gaps: (a) the live suite grades outcome only — `_run_live_case` sets `passed = status=="ok"` plus a rubric score; `attempts` is recorded in `details` and rolled to `mean_attempts` (`runner.py:238`) but is asserted nowhere and is **not** among `compare.py`'s `_LIVE_CHECKS`, so a silent doubling of retries reads as flat; (b) there is no logged tool-call/trace to assert against for real runs — `query_records` stores economics only, trace capture is E4-unbuilt (`EVAL_LADDER.md:68-70`); (c) the one true LLM tool-call surface, `propose_triggers` (`monitors/nl.py:246-256`, `tools=[_TOOL]`), has **zero eval coverage of the model path** — the guards suite only exercises `deterministic_triggers` and `validate_triggers`. The design (model proposes, validator disposes) is right, and the validator is asserted; but no case shows an LLM-shaped hostile proposal traversing the real seam.

### D6 Safety/refusal section — **PARTIAL** (injection: excellent; other categories: thin or absent)

Present, and better than most suites: a 10-case adversarial/injection block in `dataset.py:246-317` (instruction injection, fake gate approval, `(verified: true)` marker, fullwidth digits, U+200B smuggling, fake citation wrapper, code-span smuggling, homoglyph citation) with two deliberate *negative* controls that pin the boundary (`injection_prose_without_figures_passes`, `true_figure_after_injection_still_passes` — "the gate polices figures, not tone"); an injection-in-upstream-data scenario asserting the money outcome cannot move (`injected_source_cannot_bypass_gate_or_billing`); 8 hostile-identifier refusals (path traversal, URL, query smuggling, NUL byte, overlong); hostile monitor-NL proposals (`run_shell_command` dropped, `pct: 99_999` clamped to 1000); two fail-closed scenarios.

Missing:
- **PII**: nothing in the entire repo. The free-text surface (`propose_triggers(text, ...)`, monitor NL) accepts arbitrary user prose and there is no case asserting PII isn't echoed into a published alert.
- **Out-of-scope / personalized-advice requests**: the impersonal guard covers *output* prose (8 guard cases), but no case exercises a request for personalized advice arriving at the pipeline and being refused/deflected.
- **Gate-protected irreversible actions are not eval cases.** The money-moving refusals — buyer price-cap (`tests/test_price_guard.py:59`), call-chain loop/depth refusal (`tests/test_callchain.py:41,47,75,93,102`) — exist only in `pytest`. They are invisible to the trend history, the baseline diff, and the nightly digest, even though "no payment before verification" is an `AGENTS.md` invariant.

### D7 Flywheel — **PARTIAL, closer to MISSING on the production half**

The written contract is good: `EVAL_LADDER.md:322-330` specifies `jim-eval triage <trace_id>` emitting ready-to-paste case code with `# origin: trace=... sha=... sampled=...` provenance, and states honestly that enforcement is social, not mechanical. `.claude/gate.sh:31-52` is real anti-cheat machinery (blocks staged deletion of `tests/` or `src/jim/eval/`, added skip/xfail markers, and net test/`Scenario(` removal) — that guards the flywheel's output even if it doesn't create input.

Evidence of it working: one instance, and it came from dataset authoring, not reality — ADR-0009 records that writing the gate dataset caught a real `_RANGE_RE` false-reject bug ("2023-2024 the…" read as trillions), fixed in the same change; case `year_span_is_not_a_figure` is the frozen artifact. Counter-evidence: only 4 commits have ever touched the eval datasets, all `eval:`-prefixed authored work; the one substantive hardening commit, `45f06bd "Harden the sourcing gate and stop billing gate-rejected runs"`, added 312 lines of `tests/` and **zero** eval cases. Production traces don't exist, so nothing can feed new cases. The "the diff that fixes a sampled failure contains its triage-generated case" rule is *not* in `AGENTS.md` (the doc defers it to when E4 lands).

### D8 Ops contract — **MET** (with two narrow caveats)

`.claude/evals.sh` is a genuinely well-built nightly entry point: zero-credential by construction (lines 28-34 neutralize `DATABASE_URL`, `ANTHROPIC_API_KEY`, CDP keys, `PEER_SOURCES`, pins testnet), `set -euo pipefail`, uv-with-venv-fallback for non-login shells, `--no-sync` so it can't mutate `.venv`. Exit code *is* the gate: 1 on any offline case failure (`cli.py:169`) and 1 on baseline regression (`cli.py:182`). Digest-readable output verified: per-suite table with cases/passed/rate/p95ms/cost, an explicit failing-case list capped at 25, `offline: 99/99 passed · total eval cost $0.0000`, then `verdict: FLAT`. Cost/latency are first-class throughout (`CaseResult` carries `latency_ms`, `cost_usd`, `input_tokens`, `output_tokens`; aggregates carry p50/p95; `compare.py:_LIVE_CHECKS` thresholds both cost and latency; the UI charts both). Runtime bill is bounded and tiny: 1.25s, $0, measured.

Caveats: (1) as in D4, the baseline-regression half no-ops on any machine without a local `eval_runs/BASELINE`; (2) `.claude/evals.sh:23-25` claims it neutralizes "the same .env leakage the test suite does", but `tests/conftest.py:32-33` also clears `CLAUDE_CODE_OAUTH_TOKEN` and pins `LLM_AUTH_MODE=api_key`, which `evals.sh` does not. Harmless today (the offline suites make no LLM calls), but the parity claim is false and would matter the moment a credentialed suite joins the nightly.

---

## D. Top gaps, ranked by risk to trusting the suite's verdicts

1. **The judge's calibration evidence is unverifiable and its labels are unsigned, while the judge gates revenue.** `judge_threshold=0.55` co-decides `ok/rejected` (`engine.py:179`) and rejection books $0. The only proof it's a good threshold is prose in `EVAL_LADDER.md:164-194` pointing at a run document that exists on no machine. Anyone re-deriving this in 3 months has to re-spend and re-label from scratch. *Highest risk: this is the one grader whose verdict is an opinion, and the opinion is currently unaudited in the artifact record.*
2. **The regression gate silently no-ops off this laptop.** Baseline + all history are gitignored. A fresh clone, a CI runner, or a new machine gets `(no baseline set — skip regression check)` and exit 0. The suite's central claim — "exit 1 on regression" — is true only where someone once ran `jim-eval baseline set` by hand. The local baseline is also 19 days and 12 cases stale, taken under a different `judge_threshold`, and `compare_runs` never checks config compatibility.
3. **Everything that grades the model is unexercised; everything exercised grades code.** 99/99 cases are model-free and have never once failed in 27 runs. The `live` suite has never run; `mean_attempts` isn't compared; `--repeats` defaults to 1. So the suite currently answers "did the deterministic rails regress?" with high confidence and answers "did the *agent* get better or worse?" not at all — while the trend UI and `--compare-baseline` present a single verdict that reads as if it covers both.
4. **`jim-eval run --suite live` always exits 1, even when every case passes.** `_summarize` computes `"all_offline_passed": offline_cases > 0 and offline_passed == offline_cases` (`runner.py:411`); with no offline suite in the run, `offline_cases == 0` → `False` → `cli.py:169` exits 1. Verified by calling `_summarize` directly on a synthetic all-passing live block. This is the documented invocation (`cli.py:5`, `README.md:161,189`). The first person to activate L2 will see a red exit on a green run and learn to ignore the exit code.
5. **Money-moving refusals and PII/out-of-scope requests are not in the eval suite.** The x402 price-cap and call-chain loop/depth guards protect irreversible actions and are named invariants in `AGENTS.md`, but they live only in `pytest` — outside the trend history, the baseline diff, and the nightly digest. PII has no coverage anywhere despite a free-text monitor-NL input surface.

---

## E. Migration plan

Ordered; each step is independently landable and states its own acceptance evidence. Steps 1–7 are $0 and offline.

**Step 1 — Fix the exit-code defect (30 min, unblocks everything downstream).**
In `src/jim/eval/runner.py::_summarize`, replace `all_offline_passed` with a suite-scoped verdict — e.g. add `"suites_run"` and `"all_requested_passed"` (all cases in every requested suite passed), keeping `all_offline_passed` for back-compat with the 27 persisted docs and the UI. Update `cli.py:169` to read the new key. Acceptance: a unit test asserting a live-only all-passing run exits 0 and a live-only failing run exits 1.

**Step 2 — Make history and the baseline durable (this is `EVAL_LADDER.md` E3 item 2; execute it as specced).**
Add `jim-eval export` writing one compact JSON line per run (run_id, git sha/branch, label, **full config snapshot**, summary, per-case pass map — no memos) to committed `evals/history/YYYY-MM.jsonl`, plus committed `evals/history/BASELINE.json`. Teach `compare.py::_offline_diff` to accept an exported summary as the base side (it only needs `{name: bool}`). Append from `.claude/evals.sh` after the run. Keep full run docs gitignored — ADR-0009's split is right. Acceptance: on a fresh `git clone` with an empty `eval_runs/`, `jim-eval run --compare-baseline` emits a real verdict, and `--suite offline` with a planted failing case exits 1.

**Step 3 — Guard cross-config comparison.**
In `compare_runs`, diff the two runs' `config` blocks; annotate any live/judge metric as `n/a` (not "flat") when `llm_auth_mode`, `judge_model`, `judge_threshold`, or `research_model` differ, and print the differing keys. This subsumes E3 item 4 and fixes the stale-baseline hazard in one place. Acceptance: unit test comparing two runs with different `judge_threshold` → cost/quality rows marked `n/a`, offline rows still exact.

**Step 4 — Close out E2 honestly.** (a) Operator sign-off of the 40 labels, starting with the four borderline cases named in `EVAL_LADDER.md:180-194`. (b) Re-run `jim-eval judge-calibrate --repeats 3` and **commit the calibration block** (confusion at configured + chosen threshold, per-family recall, flip rate, sweep, run_id, model id) to `evals/history/` via Step 2's exporter. (c) Resolve the `attractive_entry_point`-ships-at-0.55 question explicitly: either the label changes, the memo's phrasing is the bug, or `eval_judge_max_false_reject` moves and 0.70+ becomes the operating point — whichever, record it in the same commit. Acceptance: `config.py:83`'s threshold traces to a run id whose numbers are readable from a tracked file.

**Step 5 — Introduce a holdout slice (offline, $0).**
Add a `slice: str = "dev"` field to `GateCase` / `GuardCase` / `Scenario` / `JudgeCase`; tag ~20% of gate + judge cases (spanning every family, not the easy ones) as `"holdout"`; add `--slice dev|holdout|all` to `jim-eval run`. The commit gate and nightly run `dev`; `holdout` runs only at baseline promotion and before a model/prompt change. Acceptance: `test_eval_harness.py` asserts every family is represented in both slices and that the default `pytest` path never executes `holdout`.

**Step 6 — Turn dead-sensor policy into a command, and formalize mutation checks.**
With Step 2's committed history, add `jim-eval sensors` reporting per-case "never failed since inception (N runs)". Then execute the standing rule from `EVAL_LADDER.md:122-124` once: for each never-failed case, either point at the mutation that makes it fail or merge/retire it. Record the mutation ledger (the `engine.py` `and`→`or` check from E1 is the template) in `docs/EVAL_LADDER.md` so it's a repeatable ceremony rather than a one-time manual act. Acceptance: a committed table mapping each surviving case class to the mutation it catches; ≥1 merged or retired case, or a written statement of why none qualify.

**Step 7 — Expand the safety/refusal section (offline, $0).**
(a) Promote the money-guard refusals into `dataset_guards.py` as named cases — buyer price-cap over budget, call-chain loop, call-chain over-depth — reusing the logic already tested in `tests/test_price_guard.py` and `tests/test_callchain.py` so they enter the trend history and the baseline diff. (b) Add a PII family to the guards: free-text monitor requests containing an email/phone/account number must not survive into a published trigger label or alert summary. (c) Add an out-of-scope/personalized-advice request case at the engine front door. (d) Add one scenario for the `propose_triggers` LLM seam: a scripted tool-use response proposing an out-of-registry `kind` with plausible params, asserting `validate_triggers` drops it and nothing is scheduled.

**Step 8 — Activate L2 with the spend guard first (E3 items 1, 3, 5; ~$3/run).**
Land `--max-spend` / `EVAL_MAX_SPEND_USD` and the `run_context` ledger separation **before** the first live run, not after. Then run the documented activation ceremony: clean checkout, `api_key` mode, `jim-eval run --suite all --repeats 3 --label live-activation`, operator review, `baseline set`, commit the exported baseline + the ADR superseding ADR-0009's `/proof` consequence. Record the observed run-to-run variance from the 3 repeats in `EVAL_LADDER.md` as the live noise floor — that number is what makes the `eval_rubric_drop: 0.02` and `eval_gate_pass_rate_drop: 0.05` allowances defensible rather than guessed.

**Step 9 — Grade the live trajectory, not just the live outcome.**
Add per-case process assertions to `_run_live_case` (memo cache must be off for variant comparison; judge invoked exactly once; `attempts <= research_max_attempts`) and add `mean_attempts` to `compare.py::_LIVE_CHECKS` as a `cost`-kind metric. Acceptance: a synthetic-run unit test where `mean_attempts` doubles reads as `regressed`.

**Step 10 — Distribution-matched judge validation, then the E4 loop.**
From the Step 8 activation run, take ~20 real Sonnet memos, human-label them, and re-measure the judge on *that* slice; if balanced accuracy or false-reject differs materially from the hand-authored corpus, the corpus needs real memos folded in. Then build E4 items 1–3 (trace capture, `jim-eval sample`, `jim-eval triage`) — and **write the "the diff that fixes a sampled failure contains its triage-generated case" rule into `AGENTS.md` in the same diff**, since `EVAL_LADDER.md:326-330` already concedes enforcement is cultural and the culture needs a place to live.

---

## What's solid — do not second-guess

- **The blind-spot property test** (`tests/test_judge_calibration.py::test_every_case_is_in_the_deterministic_blind_spot`). The single best idea in the suite: it makes "the judge only measures what code can't" a mechanically enforced invariant instead of a claim. Preserve it verbatim through any refactor.
- **The uniform `CaseResult` → `suite_block` → run-document → `compare_runs` → UI pipeline.** One shape, suite-agnostic aggregation, and the dashboard renders the exact verdicts CI uses. This is why steps 2, 3, 5 and 9 above are small changes rather than rewrites.
- **Two-regime comparison** (`compare.py`): exact per-case diff for deterministic suites, thresholds for stochastic ones, conservative overall verdict. Correct in principle and correctly implemented.
- **The negative controls in the adversarial block.** `injection_prose_without_figures_passes` and `true_figure_after_injection_still_passes` pin the *boundary* of the gate's job. Most injection suites only assert blocks and quietly encourage over-blocking; this one doesn't.
- **`.claude/gate.sh` anti-cheat** and the credential-neutralized `.claude/evals.sh` — including the `--no-sync` and uv-lock-avoidance details, which read like scar tissue from real incidents.
- **`docs/EVAL_LADDER.md` itself.** Per-phase recurring cost, explicit kill criteria including "a 0.30-weight grader that never changes a decision gets deleted", and recorded judgment calls awaiting sign-off. It is unusually honest about what hasn't been done — including several gaps in this audit, which it named first. The failure is execution lag against its own contract, not the contract.
