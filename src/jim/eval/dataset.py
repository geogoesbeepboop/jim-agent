"""Held-out eval set + deterministic gate-regression cases.

HELD_OUT spans sectors (tech, staples, finance, energy, industrials) so the eval
isn't tuned to one filing style. GATE_REGRESSION is offline: planted memos the
sourcing gate MUST reject — these gate merges and never need an API key.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# Held-out tickers (not referenced anywhere in prompts/code paths).
HELD_OUT: list[str] = ["AAPL", "MSFT", "NVDA", "KO", "JPM", "XOM", "CAT", "PG"]


@dataclass
class GateCase:
    name: str
    memo: str
    should_pass: bool
    # facts the synthetic snapshot exposes: id -> (value, unit)
    facts: dict[str, tuple[float, str]] = field(default_factory=dict)


# Deterministic regressions for the gate (offline, no model).
GATE_REGRESSION: list[GateCase] = [
    GateCase(
        name="clean_memo_passes",
        memo="Revenue was $394.3 billion [C1] and net margin 23.8% [C2].",
        should_pass=True,
        facts={"C1": (394_328_000_000, "USD"), "C2": (23.77, "%")},
    ),
    GateCase(
        name="planted_dollar_hallucination_blocked",
        memo="Revenue soared to $450.0 billion [C1].",
        should_pass=False,
        facts={"C1": (394_328_000_000, "USD")},
    ),
    GateCase(
        name="planted_rsi_hallucination_blocked",
        memo="RSI is overbought at 95.0 [C1].",
        should_pass=False,
        facts={"C1": (61.5, "index")},
    ),
    GateCase(
        name="uncited_number_blocked",
        memo="Revenue was $394.3 billion with strong momentum.",
        should_pass=False,
        facts={"C1": (394_328_000_000, "USD")},
    ),
    GateCase(
        name="phantom_citation_blocked",
        memo="Revenue was $394.3 billion [C9].",
        should_pass=False,
        facts={"C1": (394_328_000_000, "USD")},
    ),
]
