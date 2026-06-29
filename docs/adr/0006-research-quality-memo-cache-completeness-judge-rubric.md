# ADR-0006 — Research quality: memo cache, completeness check, structured judge, eval rubric

**Status:** Accepted

## Context

The pipeline could prove a memo was *sourced* (the deterministic gate) and score
it *faithful* (a single 0–1 judge number), but four quality gaps remained:

1. **Redundant inference.** Identical repeat queries (precompute, popular tickers,
   an agent re-asking) re-ran the whole LLM pipeline even when the underlying data
   hadn't moved — paying full inference for a byte-identical answer.
2. **Blind to omission.** The gate validates what the memo *includes*; nothing
   checked what it *left out*. A memo could pass while silently dropping the single
   most important line item.
3. **A coarse judge.** One groundedness float says "0.7" but not *which* claim is
   weak, and every run used the same small model regardless of stakes.
4. **"Better" was underdefined.** Gate pass-rate + one faithfulness score don't
   capture quality; there was no composite to rank single-pass vs. debate vs. a
   model swap on *goodness*.

## Decision

> A fingerprinted **memo cache** that serves identical answers for free, a
> deterministic **completeness** signal, a **per-claim** judge with a high-stakes
> model tier, and a weighted **rubric** that makes "better" measurable offline.

- **Memo cache** ([engine.py](../../src/jim/research/engine.py) `_memo_cache` node +
  `memo_cache` table). After `gather`, the fresh snapshot is fingerprinted
  (`Snapshot.fingerprint()` — a hash of (label, unit, value) over all facts). If a
  cached memo exists for `{product}:{identifier}:{mode}` with a **matching
  fingerprint**, within TTL, **and it still passes the deterministic gate against
  the fresh snapshot**, it's served and synthesis/debate/judge are skipped
  (inference → $0). The gate re-check is the safety net: a memo can only ship if
  it's still fully sourced, so nothing stale leaks through. Volatile data (a moved
  price) changes the fingerprint and correctly forces a re-synthesis — the cache
  hits only when the data is genuinely unchanged. Bypassable per-run
  (`use_memo_cache=False`); the eval disables it so A/B variants aren't
  short-circuited.
- **Completeness** ([completeness.py](../../src/jim/research/completeness.py)). The
  gate's mirror image: which snapshot facts did the memo never cite, and which of
  those are **material** (core line items + headline ratios)? Deterministic, no
  model. A **signal, not a gate** — terse agent-mode legitimately omits, so a
  material omission lowers the quality score and is surfaced to the caller, but
  never rejects a run.
- **Structured judge** ([judge.py](../../src/jim/research/judge.py)). The judge now
  returns a **per-claim checklist** (each claim → supported? which citation? why),
  not just a scalar — so a low score is explainable. High-stakes runs
  (`high_stakes=True`) upgrade to a stronger model (`JUDGE_HIGH_STAKES_MODEL`,
  Sonnet) for more scrutiny where a wrong call is expensive. Still fail-closed
  (unparseable → reject) and still skipped without a key.
- **Eval rubric** ([rubric.py](../../src/jim/eval/rubric.py)). A weighted composite
  over **sourcing + completeness + impersonal** (all deterministic, no key) plus
  **faithfulness** when a key is set. Weights are explicit and in one place, so
  "what we optimise for" is legible. The eval reports per-dimension means + the
  composite and uses it as the headline single-pass-vs-debate lift metric.

## Consequences

**Positive**
- Repeated identical queries cost ~$0 inference, with correctness guaranteed by
  the same gate that guards fresh runs — caching never weakens the core promise.
- Omissions become visible; the product can be judged on what it *should* have
  said, not only on what it did.
- A weak judge verdict now names the offending claim, and high-stakes runs get a
  stronger reviewer without making every run pay for it.
- "Better output" is a number that moves, computable offline for CI.

**Negative / trade-offs**
- The fingerprint is exact: any moved value invalidates the cache, so equities
  with live prices (RSI/MACD) rarely hit it — correct, but it means the win
  concentrates on stable fundamentals / quiet windows, not every query.
- `MATERIAL_LABELS` is a curated set; a fact outside it that's contextually
  important won't be flagged. It's a floor, not a complete model of materiality.
- The per-claim judge spends more output tokens than the scalar one; high-stakes
  Sonnet costs more — opt-in for that reason.
- The rubric weights are a judgement call (sourcing-dominant); they're tunable in
  one place precisely because they're opinions, not ground truth.
- New table → existing deployments re-run `jim-initdb` (idempotent) before the
  memo cache persists.

## ELI5 / what I learned

Four upgrades to "is the answer good?" First: if someone asks the exact same
question and nothing in the data changed, don't pay to think again — hand back the
saved answer, but only after re-checking it still lines up with the facts, so a
stale note can never sneak out. Second: the old checker only made sure every number
written down was real; the new one also notices when an important number was *left
off the page*. Third: instead of the grader scribbling one score, it now writes a
checklist — this sentence is backed by that fact, that one isn't — and for the
high-stakes papers it brings in a sharper grader. Fourth: we wrote down what "good"
actually means as a weighted scorecard, mostly checkable without calling the
expensive grader at all. The lesson: *cache the thinking, not just the data; check
for sins of omission, not only commission; make the grader show its work; and turn
"better" into a number before you try to improve it.*
