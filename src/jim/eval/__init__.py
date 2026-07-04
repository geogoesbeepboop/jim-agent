"""The eval harness: tiered suites, persisted runs, comparisons, dashboard.

Offline (deterministic, no key): the sourcing-gate regression, the guard rails
(impersonal/identifier/completeness/materiality/NL), and scripted full-engine
scenarios. Live (needs ANTHROPIC_API_KEY): held-out tickers through the real
pipeline, single-pass vs debate, scored by the rubric. See ``jim-eval --help``.
"""

from jim.eval.runner import run_suites

__all__ = ["run_suites"]
