"""Track 0 — adversarial + property-based fuzzing of the sourcing gate.

The gate is the most security-critical code in the system (ROADMAP Track 0), so
it gets two invariants proven two ways — explicit adversarial cases, and
Hypothesis properties over random values:

  1. NO BYPASS. A fabricated figure — one matching no published fact — must be
     caught *however it is rendered*: scientific notation, word scales without
     grouping ("5 billion"), bare integer runs, uppercase suffixes ("5B"),
     underscore grouping, spelled-out numbers ("five billion"), euro/pound
     symbols, ranges. Fail-closed is the contract.
  2. NO FALSE REJECT. A true figure written the way jim's own formatter writes
     it, next to its citation, must always pass — including accounting
     negatives, loss phrasing over negative facts, and cited ranges.

All offline: no key, wallet, network, or DB.
"""

from __future__ import annotations

import pytest
from hypothesis import assume, given, settings
from hypothesis import strategies as st

from jim.research.facts import (
    COUNT,
    INDEX,
    MULTIPLE,
    PERCENT,
    SHARES,
    USD,
    USD_PER_SHARE,
    Fact,
    Snapshot,
    _fmt_value,
)
from jim.research.gate import check_sourcing

FACTS = [
    Fact(id="C1", label="Revenue", value=391_035_000_000.0, unit=USD),
    Fact(id="C2", label="Net margin", value=23.97, unit=PERCENT),
    Fact(id="C3", label="Debt-to-equity", value=1.96, unit=MULTIPLE),
    Fact(id="C4", label="Shares outstanding", value=15_204_137_000.0, unit=SHARES),
    Fact(id="C5", label="Net income", value=-1_200_000_000.0, unit=USD),
    Fact(id="C6", label="RSI", value=62.5, unit=INDEX),
    Fact(id="C7", label="EPS", value=6.13, unit=USD_PER_SHARE),
    Fact(id="C8", label="Guidance low", value=1_200_000_000.0, unit=USD),
    Fact(id="C9", label="Guidance high", value=1_400_000_000.0, unit=USD),
    Fact(id="C10", label="Transactions", value=48_212.0, unit=COUNT),
]
SNAP = Snapshot(ticker="TEST", cik="0000000001", entity_name="Test Corp", facts=FACTS)


def _far_from_every_fact(v: float) -> bool:
    """True when ``v`` can't match any fact under the gate's tolerance (2%),
    with margin (5% / 1.0 absolute) so float formatting can't drift it back in.
    Negative facts also match by magnitude, so compare against |value| too."""
    for f in FACTS:
        for target in (f.value, abs(f.value)):
            if abs(v - target) <= max(abs(target) * 0.05, 1.0):
                return False
    return True


# --- Invariant 1: no bypass -------------------------------------------------

# Exotic renderings that used to sail straight through the extractor. Each
# plants a fabricated ~5-billion figure that matches no fact.
ATTACKS = [
    "Revenue was 3.9e11 dollars this year.",  # scientific notation
    "Revenue reached 3.9E11 in the period.",  # capital-E sci notation
    "Revenue hit 5 billion dollars.",  # word scale, no comma grouping
    "Revenue grew to 5000000000 this year.",  # bare integer run
    "Revenue was 5_000_000_000 exactly.",  # underscore grouping
    "Revenue was five billion dollars.",  # spelled-out figure
    "Sales reached twenty-five percent of the market.",  # spelled-out percent
    "Revenue was €5,000,000,000 in FY24.",  # euro symbol
    "Revenue of 999999k was recorded.",  # k suffix
    "Revenue came to 5B overall.",  # bare uppercase suffix
    "Sales came to 4.2 billion overall.",  # decimal + word scale, no $
    "Guidance is $2.2–2.7 billion for FY25 [C8, C9].",  # range, both ends wrong
    "Revenue fell to -$391.04 billion [C1].",  # sign error vs a positive fact
]


@pytest.mark.parametrize("memo", ATTACKS)
def test_adversarial_renderings_are_caught(memo: str) -> None:
    result = check_sourcing(memo, SNAP)
    assert not result.passed, f"BYPASS: fabricated figure shipped in {memo!r}"
    assert result.n_figures >= 1, f"extractor blind to the figure in {memo!r}"


# Property form: for ANY quantity far from every fact, each rendering family
# must be caught — cited (mismatch) or uncited alike.
_QTY = st.floats(min_value=1.0, max_value=999.0, allow_nan=False, allow_infinity=False)

RENDERERS = [
    # (template, multiplier applied to the drawn quantity)
    ("Revenue was {q:.2f} billion this year{cite}.", 1e9),
    ("Revenue was {q:.2f}bn in the period{cite}.", 1e9),
    ("Revenue reached {q:.4f}e9 overall{cite}.", 1e9),
    ("Revenue was {q:.1f}B{cite}.", 1e9),
    ("Sales came to {q:.2f} million overall{cite}.", 1e6),
    ("Net margin was {q:.2f}% in FY24{cite}.", 1.0),
    ("Backlog is {q:.2f} thousand units{cite}.", 1e3),
]


@settings(max_examples=120, derandomize=True, deadline=None)
@given(q=_QTY, renderer=st.sampled_from(RENDERERS), cited=st.booleans())
def test_fabricated_figures_never_pass(q: float, renderer: tuple[str, float], cited: bool) -> None:
    template, mult = renderer
    value = float(f"{q:.4f}") * mult  # what the gate will parse back out
    assume(_far_from_every_fact(value))
    memo = template.format(q=q, cite=" [C1]" if cited else "")
    result = check_sourcing(memo, SNAP)
    assert not result.passed, f"BYPASS: {memo!r} (value {value}) passed the gate"


@settings(max_examples=80, derandomize=True, deadline=None)
@given(i=st.integers(min_value=10_000, max_value=10**13))
def test_fabricated_bare_integers_never_pass(i: int) -> None:
    assume(_far_from_every_fact(float(i)))
    memo = f"Revenue grew to {i} this year."
    assert not check_sourcing(memo, SNAP).passed


# --- Invariant 2: no false reject -------------------------------------------


@pytest.mark.parametrize("fact", FACTS, ids=[f.id for f in FACTS])
def test_formatter_output_never_false_rejects(fact: Fact) -> None:
    """Every fact, rendered exactly as jim's own formatter writes it."""
    memo = f"{fact.label} is {_fmt_value(fact.value, fact.unit)} [{fact.id}]."
    result = check_sourcing(memo, SNAP)
    assert result.passed, f"FALSE REJECT: {memo!r} → {result.violations}"


LEGIT = [
    "Guidance is $1.2–1.4 billion [C8, C9].",  # en-dash range across two facts
    "Guidance is $1.2-1.4 billion [C8, C9].",  # hyphen range
    "Net income was -$1.20 billion [C5].",  # explicit negative currency
    "Net income was ($1.20 billion) [C5].",  # accounting parentheses
    "The company booked a loss of $1.20 billion [C5].",  # loss phrasing, magnitude
    "Revenue was $391.04 billion [C1], a net margin of 24.0% [C2].",
    "Leverage sits at 1.96x [C3] with RSI at 62.5 [C6].",
    "The 10-K notes 48,212 transactions [C10].",  # filing form stays inert
    "Between 2023-2024 revenue was $391.04 billion [C1].",  # date span stays inert
]


@pytest.mark.parametrize("memo", LEGIT)
def test_true_statements_pass(memo: str) -> None:
    result = check_sourcing(memo, SNAP)
    assert result.passed, f"FALSE REJECT: {memo!r} → {result.violations}"


@settings(max_examples=120, derandomize=True, deadline=None)
@given(q=st.floats(min_value=0.1, max_value=900.0, allow_nan=False, allow_infinity=False))
def test_any_cited_usd_fact_roundtrips(q: float) -> None:
    """Draw a fresh USD fact, quote it via the formatter with its citation —
    the gate must pass it (the formatter and extractor stay in agreement)."""
    value = float(f"{q:.4f}") * 1e9
    snap = Snapshot(
        ticker="RT",
        cik="0000000002",
        entity_name="Roundtrip Corp",
        facts=[Fact(id="C1", label="Revenue", value=value, unit=USD)],
    )
    memo = f"Revenue is {_fmt_value(value, USD)} [C1]."
    result = check_sourcing(memo, snap)
    assert result.passed, f"FALSE REJECT: {memo!r} → {result.violations}"
