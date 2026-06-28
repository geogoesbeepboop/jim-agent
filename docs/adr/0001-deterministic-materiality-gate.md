# ADR-0001 — Deterministic materiality gate (code decides whether to alert, not the model)

**Status:** Accepted

## Context

Phase 4 monitors re-run research on a schedule (`run_monitor_once` in
[engine.py](../../src/jim/monitors/engine.py)). Each tick must answer one
question before doing anything expensive: *did anything change enough to push an
update and pay an LLM to write it?* Most polls are quiet, so the cheap default
has to be "say nothing, spend nothing."

The tempting shortcut is to hand a fresh snapshot to an LLM and ask "is this
material?" But that re-introduces exactly the failure mode the Phase-1 sourcing
gate exists to prevent: a model deciding, per call, by criteria nobody can
replay. It would be non-reproducible (the same diff could alert today and not
tomorrow), non-auditable (no record of *why* it fired), and cost-uncontrolled
(every quiet poll pays for inference just to be told "nothing happened").

## Decision

> The decision to alert is a deterministic pipeline; the LLM only writes prose
> after that pipeline has already said there is news.

The flow is: **snapshot diff** → a registry of pure-function "watcher" triggers
([triggers.py](../../src/jim/monitors/triggers.py): `price_move`,
`metric_change`, `threshold` crossings like RSI 70/30, `ma_cross` golden/death,
`new_filing`) → a **materiality gate** ([materiality.py](../../src/jim/monitors/materiality.py))
that filters by a severity floor and a per-signal cooldown. No model sits in this
loop. Only when `assess()` returns `material=True` does the optional synthesizer
run, and its output still passes the sourcing gate plus a deterministic
impersonal-output guard. This mirrors Phase 1's "model proposes, code disposes"
and the propose/dispose budget cap: the model can want to speak; only code
decides whether there is anything to speak about.

## Consequences

**Positive**
- Alerts are reproducible and auditable: the same diff + cooldown state always
  yields the same verdict, and each fired signal records its structured cause.
- Quiet polls cost $0 inference — the gate *is* the cost control, surfaced as
  `inference_saved_usd` on the dashboard.
- Works with no API key at all: a deterministic update fallback can render the
  signal without a model.

**Negative / trade-offs**
- Triggers are threshold-based, so they can miss nuanced or novel materiality an
  LLM might catch (a qualitative shift with no metric crossing a line).
- The watcher roster is hand-maintained: every new metric worth alerting on needs
  a trigger added to the registry, not just a prompt tweak.

## ELI5 / what I learned

I want my monitors to text me only when something real happens, and to be free
when it's a boring day. The naive move is to ask the AI "anything important?"
every time — but that costs money on every quiet day and the AI might answer
differently each time, so I can never prove *why* it pinged me. So I made plain
code do the deciding: it compares the new numbers to the old, and a little crew
of dumb-but-honest rules (price moved 5%, RSI crossed 70, new SEC filing) raises
a hand. Only if a hand goes up do I pay the AI to write the nice paragraph. The
big lesson: *let cheap, boring code decide whether to act, and save the
expensive, clever model for the writing.* Same trick as my sourcing gate —
"model proposes, code disposes" — applied to "should I even speak?" instead of
"is this number true?".
