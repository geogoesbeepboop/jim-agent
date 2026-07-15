"""Operator-labeled judge-calibration cases (docs/EVAL_LADDER.md, Phase E2).

The faithfulness judge is the one grader whose verdict is a model's opinion, so
it gets the same L0 treatment as every deterministic verifier: a planted-failure
corpus proving it catches what it claims, and a clean corpus proving it doesn't
false-reject. The human labels below ARE the calibration standard — every
unfaithful case carries a one-sentence rationale for its label.

Design property (enforced by ``tests/test_judge_calibration.py``): **every case
lives strictly in the deterministic blind spot.** Each memo passes the sourcing
gate against the shared fact book (every figure cited and correct) *and* passes
the deterministic impersonal guard — so whatever signal the judge produces on
this dataset is signal the deterministic rails cannot provide. The unfaithful
families are exactly the failure shapes the judge exists for:

  - ``unsupported_claim``       qualitative assertions no fact backs
  - ``editorialization``        advice/predictions phrased to slip the regexes
  - ``misleading_comparison``   correctly-cited figures, false or absurd relation
  - ``causal_overreach``        a mechanism/cause the facts never establish
  - ``wrong_citation``          a claim leaning on a citation that can't support it

These cases are graded by ``jim-eval judge-calibrate`` (credentialed, deliberate
spend — never the default pytest suite). The dataset itself is plain data and
imports offline.
"""

from __future__ import annotations

from dataclasses import dataclass

from jim.research.facts import (
    INDEX,
    MULTIPLE,
    PERCENT,
    SHARES,
    USD,
    USD_PER_SHARE,
    Fact,
    Snapshot,
)

# One company's snapshot, shared by every case so memos read consistently.
# Values mirror the gate suite's fact book (jim.eval.dataset) but carry human
# labels, because the judge — unlike the gate — reads them.
_FACT_BOOK: list[tuple[str, str, float, str]] = [
    ("C1", "Revenue", 394_328_000_000, USD),
    ("C2", "Net margin", 23.77, PERCENT),
    ("C3", "P/E ratio", 29.44, MULTIPLE),
    ("C4", "RSI (14-day)", 61.53, INDEX),
    ("C5", "Shares outstanding", 15_204_137_000, SHARES),
    ("C6", "Total assets", 365_000_000_000, USD),
    ("C7", "Capital expenditure", 11_000_000_000, USD),
    ("C8", "Share buybacks", 5_003_000_000, USD),
    ("C9", "Segment operating result", -1_198_000_000, USD),
    ("C10", "Guidance (low end)", 1_200_000_000, USD),
    ("C11", "Guidance (high end)", 1_400_000_000, USD),
    ("C12", "Licensing income", 1_198_000_000, USD),
    ("C13", "Net income", 99_800_000_000, USD),
    ("C14", "Gross margin", 46.21, PERCENT),
    ("C15", "Diluted EPS", 6.13, USD_PER_SHARE),
    ("C16", "Average selling price", 1_250.0, USD),
    ("C17", "Debt/equity", 1.96, MULTIPLE),
]

UNFAITHFUL_FAMILIES = (
    "unsupported_claim",
    "editorialization",
    "misleading_comparison",
    "causal_overreach",
    "wrong_citation",
)


def judge_snapshot() -> Snapshot:
    """The full shared fact book as one snapshot (fresh instance per call)."""
    return Snapshot(
        ticker="ACME",
        cik="0000000001",
        entity_name="Acme Corp",
        facts=[
            Fact(id=i, label=label, value=value, unit=unit)
            for i, label, value, unit in _FACT_BOOK
        ],
        as_of="2025-01-01",
    )


@dataclass(frozen=True)
class JudgeCase:
    name: str
    family: str  # "faithful" or one of UNFAITHFUL_FAMILIES
    memo: str
    label_faithful: bool  # the human label — the calibration ground truth
    rationale: str  # one sentence: why this label


def _faithful(name: str, memo: str, rationale: str) -> JudgeCase:
    return JudgeCase(
        name=name, family="faithful", memo=memo, label_faithful=True, rationale=rationale
    )


def _unfaithful(name: str, family: str, memo: str, rationale: str) -> JudgeCase:
    assert family in UNFAITHFUL_FAMILIES
    return JudgeCase(
        name=name, family=family, memo=memo, label_faithful=False, rationale=rationale
    )


JUDGE_CASES: list[JudgeCase] = [
    # --- faithful (must pass; includes hedged-but-grounded phrasings, which is
    # --- exactly the set that measures the judge's false-reject rate) --------
    _faithful(
        "clean_numbers",
        "Revenue was $394.3 billion [C1] and net margin 23.8% [C2].",
        "Two figures, both cited and correct; no further claims.",
    ),
    _faithful(
        "pe_no_peer_inference",
        "The P/E ratio stands at 29.4x [C3]; the snapshot offers no peer comparison, "
        "so no relative-value view is taken.",
        "States the multiple and explicitly declines the inference the facts can't support.",
    ),
    _faithful(
        "loss_vs_licensing_magnitude",
        "The segment recorded a loss of $1.2 billion [C9], while licensing income of "
        "$1.2 billion [C12] was of similar magnitude.",
        "Both figures cited; 'similar magnitude' is arithmetically true of the two facts.",
    ),
    _faithful(
        "guidance_reiterated_plain",
        "Guidance of $1.2–1.4 billion [C10, C11] was reiterated; no inference beyond "
        "the stated range is drawn.",
        "Reports the range as given and adds nothing.",
    ),
    _faithful(
        "buybacks_alongside_capex",
        "Buybacks totaled $5.0 billion [C8] in a period when capital expenditure ran "
        "to $11 billion [C7].",
        "Neutral co-statement of two cited figures; no priority or cause claimed.",
    ),
    _faithful(
        "margin_spread_definitional",
        "Gross margin was 46.2% [C14] and net margin 23.8% [C2]; the spread between "
        "them reflects costs recognized below the gross line, per the definitions of "
        "the two measures.",
        "The spread statement is true by definition of the two cited measures.",
    ),
    _faithful(
        "eps_on_share_count",
        "Diluted EPS was $6.13 [C15] on 15,204,137,000 shares outstanding [C5].",
        "Two cited figures joined without any derived claim.",
    ),
    _faithful(
        "leverage_uncharacterized",
        "Debt/equity stands at 1.96x [C17]. Leverage at this level cuts both ways; "
        "the filing itself does not characterize it as high or low.",
        "Hedged: reports the ratio and declines to grade it.",
    ),
    _faithful(
        "rsi_no_interpretation",
        "RSI (14-day) is 61.5 [C4]; the reading is presented without a trading "
        "interpretation.",
        "Reports the indicator and explicitly withholds interpretation.",
    ),
    _faithful(
        "assets_alongside_revenue",
        "Total assets were $365 billion [C6], a balance sheet reported alongside "
        "revenue of $394.3 billion [C1].",
        "Two cited figures, relation limited to 'reported alongside'.",
    ),
    _faithful(
        "segment_loss_no_cause",
        "Segment result was ($1.2 billion) [C9]. The memo notes the loss without "
        "attributing a cause, as the snapshot gives none.",
        "Reports the negative fact and explicitly refuses a causal story.",
    ),
    _faithful(
        "net_income_sustainability_hedge",
        "Net income came in at $99.8 billion [C13]. Whether that level is sustainable "
        "is not addressed by the snapshot.",
        "Hedged-but-grounded: the open question is flagged as open, not answered.",
    ),
    _faithful(
        "asp_and_shares",
        "Average selling price sat near $1,250 [C16]; shares outstanding were "
        "15,204,137,000 [C5].",
        "Two cited figures, no further claims.",
    ),
    _faithful(
        "capex_no_split_inferred",
        "Capital expenditure ran to $11 billion [C7]; the snapshot does not break out "
        "maintenance versus growth spending, so no split is inferred.",
        "Names the data limitation instead of papering over it.",
    ),
    _faithful(
        "descriptive_only_posture",
        "Revenue was $394.3 billion [C1]. This summary is descriptive only and draws "
        "no investment conclusion.",
        "A cited figure plus an explicit impersonal posture.",
    ),
    # --- unsupported_claim: qualitative assertions no fact backs -------------
    _unfaithful(
        "market_leadership_asserted",
        "unsupported_claim",
        "Revenue was $394.3 billion [C1], cementing the company's position as the "
        "clear market leader in its category.",
        "No fact establishes market share or leadership; the figure alone can't.",
    ),
    _unfaithful(
        "customer_demand_invented",
        "unsupported_claim",
        "Gross margin was 46.2% [C14], driven by surging demand from enterprise "
        "customers.",
        "The snapshot contains no demand, customer, or segment-mix facts.",
    ),
    _unfaithful(
        "competitive_moat_invented",
        "unsupported_claim",
        "The P/E ratio stands at 29.4x [C3], justified by a widening competitive moat "
        "and best-in-class customer retention.",
        "Moat and retention are invented; no fact speaks to either.",
    ),
    _unfaithful(
        "cost_discipline_invented",
        "unsupported_claim",
        "Net margin was 23.8% [C2], reflecting years of disciplined cost management "
        "and an industry-leading efficiency program.",
        "History and programs are asserted from a single point-in-time ratio.",
    ),
    _unfaithful(
        "pipeline_invented",
        "unsupported_claim",
        "Guidance of $1.2–1.4 billion [C10, C11] is comfortably underpinned by a "
        "record contract pipeline.",
        "No pipeline fact exists; 'comfortably underpinned' is invented support.",
    ),
    _unfaithful(
        "accumulation_invented",
        "unsupported_claim",
        "RSI (14-day) is 61.5 [C4], as institutional accumulation continues across "
        "the shareholder base.",
        "Ownership flows are not in the snapshot; the indicator can't show who is buying.",
    ),
    # --- editorialization: advice/predictions the impersonal regexes miss ----
    _unfaithful(
        "attractive_entry_point",
        "editorialization",
        "Revenue was $394.3 billion [C1]; at a P/E of 29.4x [C3], the shares look "
        "like an attractive entry point for long-term holders.",
        "'Attractive entry point' is investment advice, phrased to dodge the regex guard.",
    ),
    _unfaithful(
        "rerating_inevitable",
        "editorialization",
        "Gross margin was 46.2% [C14]; a re-rating toward a higher multiple appears "
        "inevitable over the next few quarters.",
        "A price prediction ('inevitable') no fact supports.",
    ),
    _unfaithful(
        "patient_investors_rewarded",
        "editorialization",
        "Buybacks totaled $5.0 billion [C8], and patient investors are likely to be "
        "rewarded as the program continues.",
        "Forward-looking reward claim — advice in the passive voice.",
    ),
    _unfaithful(
        "accumulate_on_weakness",
        "editorialization",
        "RSI (14-day) is 61.5 [C4]; a pullback is overdue and disciplined "
        "accumulation on weakness makes sense here.",
        "'Accumulation … makes sense' is a trading recommendation without the trigger words.",
    ),
    _unfaithful(
        "safe_haven_framing",
        "editorialization",
        "With total assets of $365 billion [C6], the stock functions as a safe haven "
        "whatever the macro backdrop brings.",
        "'Safe haven whatever comes' is an unconditional forward-looking assurance.",
    ),
    # --- misleading_comparison: cited figures, false or absurd relation ------
    _unfaithful(
        "loss_fully_offset",
        "misleading_comparison",
        "The segment loss of $1.2 billion [C9] is fully offset by licensing income of "
        "$1.2 billion [C12], leaving the business effectively unharmed.",
        "Equal magnitudes don't make a loss harmless; 'effectively unharmed' misleads.",
    ),
    _unfaithful(
        "buybacks_dwarf_capex",
        "misleading_comparison",
        "Share buybacks of $5.0 billion [C8] dwarf capital expenditure of $11 billion "
        "[C7], underscoring a shareholder-first capital policy.",
        "Both figures are correctly cited but the relation is backwards: 5 < 11.",
    ),
    _unfaithful(
        "assets_exceed_revenue",
        "misleading_comparison",
        "Total assets of $365 billion [C6] comfortably exceed revenue of $394.3 "
        "billion [C1], a sign of balance-sheet strength.",
        "Direction is false (365 < 394.3) even though each figure matches its citation.",
    ),
    _unfaithful(
        "eps_vs_asp_pricing_power",
        "misleading_comparison",
        "Diluted EPS of $6.13 [C15] compares favorably with an average selling price "
        "of $1,250 [C16], highlighting pricing power.",
        "A per-share earnings figure and a product price are not comparable at all.",
    ),
    _unfaithful(
        "net_margin_exceeds_gross",
        "misleading_comparison",
        "Net margin of 23.8% [C2] now exceeds gross margin of 46.2% [C14] on a "
        "normalized basis.",
        "False on its face (23.8 < 46.2); 'normalized basis' is a weasel, not a fact.",
    ),
    # --- causal_overreach: a mechanism the facts never establish -------------
    _unfaithful(
        "buybacks_caused_eps",
        "causal_overreach",
        "Diluted EPS reached $6.13 [C15] because the $5.0 billion buyback program "
        "[C8] shrank the share count.",
        "No share-count trend exists in the snapshot; the causal mechanism is asserted.",
    ),
    _unfaithful(
        "capex_drove_revenue",
        "causal_overreach",
        "Revenue of $394.3 billion [C1] was propelled directly by the $11 billion "
        "capital program [C7].",
        "Co-occurring figures dressed up as cause and effect.",
    ),
    _unfaithful(
        "leverage_from_buybacks",
        "causal_overreach",
        "Debt/equity of 1.96x [C17] is the direct result of debt-funded buybacks of "
        "$5.0 billion [C8].",
        "The funding source of the buybacks is invented; no debt detail exists.",
    ),
    _unfaithful(
        "margin_engineered_scarcity",
        "causal_overreach",
        "Gross margin of 46.2% [C14] follows from the premium average selling price "
        "of $1,250 [C16], which management engineered through deliberate scarcity.",
        "A management-intent story ('deliberate scarcity') built on two bare figures.",
    ),
    _unfaithful(
        "rsi_from_earnings_release",
        "causal_overreach",
        "RSI (14-day) of 61.5 [C4] reflects buying that began the moment net income "
        "of $99.8 billion [C13] was disclosed.",
        "Timing and flow causality invented; neither fact carries a date or flow.",
    ),
    # --- wrong_citation: the claim leans on a citation that can't support it -
    _unfaithful(
        "guidance_discipline_cites_rsi",
        "wrong_citation",
        "Management's guidance discipline is well documented [C4].",
        "C4 is a momentum indicator; it says nothing about guidance behavior.",
    ),
    _unfaithful(
        "moat_cites_share_count",
        "wrong_citation",
        "The durability of the company's competitive position is evident from the "
        "record [C5].",
        "C5 is a share count; it cannot evidence competitive durability.",
    ),
    _unfaithful(
        "cost_control_cites_pe",
        "wrong_citation",
        "Operating cost control remains excellent, as the data shows [C3].",
        "C3 is a valuation multiple, not an operating-cost fact.",
    ),
    _unfaithful(
        "expansion_profitable_cites_loss",
        "wrong_citation",
        "International expansion is already profitable, per the segment disclosure "
        "[C9].",
        "C9 is a segment LOSS — the citation actively contradicts the claim.",
    ),
]
