"""Held-out eval set + deterministic gate-regression cases.

HELD_OUT spans sectors (tech, staples, finance, energy, industrials) so the eval
isn't tuned to one filing style. GATE_REGRESSION is offline: planted memos with a
labeled expected verdict — hallucinations the sourcing gate MUST reject, and
legitimate phrasings it must NOT false-reject. These gate merges and never need
an API key.

The suite is organized by the gate's own extraction surface (see
``jim.research.gate``): every notation the extractor understands gets both a
truthful case (must pass) and a planted-lie case (must fail), so a regression in
either direction — a hallucination slipping through, or true statements being
rejected — shows up as a named case, not a vibe.
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


# A shared fact book so cases read like one company's snapshot.
_FACTS: dict[str, tuple[float, str]] = {
    "C1": (394_328_000_000, "USD"),  # Revenue
    "C2": (23.77, "%"),  # Net margin
    "C3": (29.44, "x"),  # P/E
    "C4": (61.53, "index"),  # RSI (14-day)
    "C5": (15_204_137_000, "shares"),  # Shares outstanding
    "C6": (365_000_000_000, "USD"),  # Total assets
    "C7": (11_000_000_000, "USD"),  # Capex
    "C8": (5_003_000_000, "USD"),  # Buybacks
    "C9": (-1_198_000_000, "USD"),  # Segment loss (negative fact)
    "C10": (1_200_000_000, "USD"),  # Guidance low
    "C11": (1_400_000_000, "USD"),  # Guidance high
    "C12": (1_198_000_000, "USD"),  # Licensing income (positive twin of C9)
    "C13": (99_800_000_000, "USD"),  # Net income
    "C14": (46.21, "%"),  # Gross margin
    "C15": (6.13, "USD/shares"),  # Diluted EPS
    "C16": (1_250.0, "USD"),  # Average selling price
    "C17": (1.96, "x"),  # Debt/equity
}


def _case(name: str, memo: str, should_pass: bool, *ids: str) -> GateCase:
    return GateCase(
        name=name,
        memo=memo,
        should_pass=should_pass,
        facts={i: _FACTS[i] for i in ids} if ids else dict(_FACTS),
    )


# Deterministic regressions for the gate (offline, no model).
GATE_REGRESSION: list[GateCase] = [
    # --- truthful phrasings the gate must NOT false-reject ------------------
    _case(
        "clean_memo_passes",
        "Revenue was $394.3 billion [C1] and net margin 23.8% [C2].",
        True,
        "C1",
        "C2",
    ),
    _case("rounded_billions_pass", "Revenue came in at $394.3 billion [C1].", True, "C1"),
    _case("multiple_ratio_pass", "The P/E ratio stands at 29.4x [C3].", True, "C3"),
    _case("anchored_indicator_pass", "RSI (14-day) is 61.5 [C4].", True, "C4"),
    _case(
        "comma_grouped_count_pass",
        "Shares outstanding: 15,204,137,000 [C5].",
        True,
        "C5",
    ),
    _case("scientific_notation_pass", "Total assets were 3.65e11 [C6].", True, "C6"),
    _case("suffix_currency_pass", "Revenue: $394.3B [C1].", True, "C1"),
    _case("word_scale_pass", "Capital expenditure ran to 11 billion [C7].", True, "C7"),
    _case("spelled_out_figure_pass", "Buybacks totaled five billion [C8].", True, "C8"),
    _case(
        "spelled_out_percent_pass",
        "Gross margin was forty-six percent [C14].",
        True,
        "C14",
    ),
    _case(
        "loss_phrasing_magnitude_pass",
        "The segment recorded a loss of $1.2 billion [C9].",
        True,
        "C9",
    ),
    _case(
        "accounting_negative_pass",
        "Segment result was ($1.2 billion) [C9].",
        True,
        "C9",
    ),
    _case(
        "range_both_endpoints_pass",
        "Guidance of $1.2–1.4 billion [C10, C11] was reiterated.",
        True,
        "C10",
        "C11",
    ),
    _case("eps_per_share_pass", "Diluted EPS was $6.13 [C15].", True, "C15"),
    _case("small_dollar_pass", "Average selling price sat near $1,250 [C16].", True, "C16"),
    _case(
        "year_span_is_not_a_figure",
        "Between 2023-2024 the company expanded internationally.",
        True,
        "C1",
    ),
    _case(
        "filing_form_is_not_a_figure",
        "The latest 10-K discusses segment mix at length.",
        True,
        "C1",
    ),
    _case(
        "small_counts_ignored",
        "The company reports 3 segments across 12 regions.",
        True,
        "C1",
    ),
    _case(
        "prose_without_figures_passes",
        "Momentum remains steady and management tone is measured.",
        True,
        "C1",
    ),
    # --- planted lies the gate MUST reject -----------------------------------
    _case(
        "planted_dollar_hallucination_blocked",
        "Revenue soared to $450.0 billion [C1].",
        False,
        "C1",
    ),
    _case(
        "planted_rsi_hallucination_blocked",
        "RSI is overbought at 95.0 [C1].",
        False,
        "C4",
    ),
    _case(
        "uncited_number_blocked",
        "Revenue was $394.3 billion with strong momentum.",
        False,
        "C1",
    ),
    _case("phantom_citation_blocked", "Revenue was $394.3 billion [C9].", False, "C1"),
    _case(
        "phantom_in_multi_cite_blocked",
        "Revenue was $394.3 billion [C1, C99].",
        False,
        "C1",
    ),
    _case(
        "scale_error_blocked",
        "Revenue was $394.3 million [C1].",
        False,
        "C1",
    ),
    _case(
        "beyond_tolerance_rounding_blocked",
        "Revenue was $405.0 billion [C1].",
        False,
        "C1",
    ),
    _case(
        "unit_mismatch_percent_blocked",
        "Operating margin was 29.4% [C3].",
        False,
        "C3",
    ),
    _case(
        "unit_mismatch_dollar_blocked",
        "Advisory fees of $61.5 [C4] were booked.",
        False,
        "C4",
    ),
    _case(
        "sign_error_blocked",
        "Licensing income was -$1.2 billion [C12].",
        False,
        "C12",
    ),
    _case(
        "range_endpoint_hallucinated_blocked",
        "Guidance of $1.2–1.9 billion [C10, C11] was raised.",
        False,
        "C10",
        "C11",
    ),
    _case(
        "spelled_out_hallucination_blocked",
        "Buybacks reached five billion [C1] this year.",
        False,
        "C1",
    ),
    _case(
        "spelled_out_percent_uncited_blocked",
        "Margins expanded twenty-five percent on cost cuts.",
        False,
        "C2",
    ),
    _case("suffix_hallucination_blocked", "Revenue hit 450B [C1].", False, "C1"),
    _case(
        "scientific_notation_hallucination_blocked",
        "Assets ballooned to 9.9e11 [C6].",
        False,
        "C6",
    ),
    _case(
        "underscore_grouped_uncited_blocked",
        "Headcount reached 1_500_000 during the year.",
        False,
        "C1",
    ),
    _case(
        "citation_scope_is_per_sentence_blocked",
        "Revenue was $394.3 billion [C1]. Net income was $99.8 billion.",
        False,
        "C1",
        "C13",
    ),
    _case(
        "multiplier_hallucination_blocked",
        "Leverage sits at 9.9x [C17].",
        False,
        "C17",
    ),
    _case(
        "mixed_true_and_hallucinated_blocked",
        "Revenue was $394.3 billion [C1], while diluted EPS reached $9.99 [C15].",
        False,
        "C1",
        "C15",
    ),
]
