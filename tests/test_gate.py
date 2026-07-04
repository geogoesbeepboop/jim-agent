"""The sourcing gate is the core of Phase 1's exit criteria. These run offline.

We prove: a correctly-cited memo passes; a planted hallucination is rejected;
uncited numbers are rejected; and citations to non-existent facts are rejected.
"""

from __future__ import annotations

from jim.research.facts import MULTIPLE, PERCENT, USD, USD_PER_SHARE, Fact, Snapshot
from jim.research.gate import check_sourcing


def _snapshot() -> Snapshot:
    return Snapshot(
        ticker="ACME",
        cik="0000000001",
        entity_name="Acme Corp",
        facts=[
            Fact(
                id="C1",
                label="Revenue",
                value=394_328_000_000,
                unit=USD,
                accession="0000-1",
                form="10-K",
                fiscal_year=2024,
                fiscal_period="FY",
            ),
            Fact(
                id="C2",
                label="Net income",
                value=93_736_000_000,
                unit=USD,
                accession="0000-1",
                form="10-K",
                fiscal_year=2024,
                fiscal_period="FY",
            ),
            Fact(
                id="C3",
                label="Diluted EPS",
                value=6.13,
                unit=USD_PER_SHARE,
                accession="0000-1",
                form="10-K",
                fiscal_year=2024,
                fiscal_period="FY",
            ),
            Fact(
                id="C4",
                label="Net margin",
                value=23.77,
                unit=PERCENT,
                is_derived=True,
                derived_from=("C2", "C1"),
            ),
            Fact(
                id="C5",
                label="Debt-to-equity",
                value=1.96,
                unit=MULTIPLE,
                is_derived=True,
                derived_from=("C6", "C7"),
            ),
        ],
    )


def test_clean_memo_passes() -> None:
    memo = (
        "Revenue reached $394.3 billion [C1] for fiscal 2024. "
        "Net income was $93.74 billion [C2], a 23.8% net margin [C4]. "
        "Diluted EPS came in at $6.13 [C3]. "
        "Leverage sits at 1.96x debt-to-equity [C5]."
    )
    result = check_sourcing(memo, _snapshot())
    assert result.passed, result.feedback()
    assert result.n_figures == 5
    assert result.coverage == 1.0


def test_planted_hallucination_is_blocked() -> None:
    # $450B revenue is fabricated — no fact has that value, even though it cites C1.
    memo = "Revenue soared to $450.0 billion [C1] in fiscal 2024."
    result = check_sourcing(memo, _snapshot())
    assert not result.passed
    assert any(v.reason == "value mismatch" for v in result.violations)


def test_uncited_number_is_blocked() -> None:
    memo = "Revenue was $394.3 billion with strong momentum."
    result = check_sourcing(memo, _snapshot())
    assert not result.passed
    assert any(v.reason == "uncited" for v in result.violations)


def test_phantom_citation_is_blocked() -> None:
    memo = "Revenue was $394.3 billion [C99]."
    result = check_sourcing(memo, _snapshot())
    assert not result.passed
    reasons = {v.reason for v in result.violations}
    assert "phantom citation" in reasons


def test_wrong_unit_does_not_match() -> None:
    # Citing the percent margin fact for a dollar figure must not satisfy it.
    memo = "Revenue was $394.3 billion [C4]."
    result = check_sourcing(memo, _snapshot())
    assert not result.passed
    assert any(v.reason == "value mismatch" for v in result.violations)


def test_qualitative_text_needs_no_citation() -> None:
    memo = "Revenue grew meaningfully and margins expanded across segments."
    result = check_sourcing(memo, _snapshot())
    assert result.passed
    assert result.n_figures == 0


def test_year_span_before_scale_lookalike_word_is_not_a_figure() -> None:
    # Regression: _RANGE_RE's single-letter scale class used to match the "t" of
    # a following word case-insensitively ("2023-2024 the…" read as trillions),
    # false-rejecting truthful prose. Date spans must never become figures.
    memo = "Between 2023-2024 the company expanded internationally."
    result = check_sourcing(memo, _snapshot())
    assert result.passed, result.feedback()
    assert result.n_figures == 0


def test_suffixed_range_still_extracts() -> None:
    # The fix must not loosen real ranges: an uppercase-suffixed hallucinated
    # range stays a checkable figure and is rejected.
    memo = "Guidance sits at $1.2-1.4B [C1]."
    result = check_sourcing(memo, _snapshot())
    assert not result.passed
    assert result.n_figures == 2
