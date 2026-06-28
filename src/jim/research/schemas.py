"""Pydantic response models for the paywalled research endpoint.

These define jim's *output contract* — the same shape that will become the
Bazaar output JSON schema in Phase 5, so machine buyers can parse it.
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from jim.research.engine import ResearchResult


class CitationOut(BaseModel):
    id: str
    label: str
    value: float
    unit: str
    is_derived: bool
    form: str | None = None
    fiscal_year: int | None = None
    fiscal_period: str | None = None
    accession: str | None = None
    filed: str | None = None
    source_url: str | None = None
    derived_from: list[str] = Field(default_factory=list)
    formula: str | None = None


class SourcingOut(BaseModel):
    passed: bool
    coverage: float
    figures_checked: int
    figures_covered: int
    violations: list[str] = Field(default_factory=list)


class FaithfulnessOut(BaseModel):
    evaluated: bool
    score: float
    issues: list[str] = Field(default_factory=list)


class FundamentalsResponse(BaseModel):
    """The product. Every figure in `memo` resolves to an entry in `citations`.

    The `cost` dict carries the Phase 2 economics: inference_cost_usd,
    data_cost_usd, price_out_usd, margin_usd, and cache_hit.
    """

    product: str = "fundamentals"
    ticker: str
    company: str | None
    cik: str | None
    as_of: str | None
    mode: str
    status: str = Field(description='"ok" once the sourcing gate has passed')
    memo: str | None
    citations: list[CitationOut]
    sourcing: SourcingOut | None
    faithfulness: FaithfulnessOut | None
    debate: str | None = None  # bull/bear/judge adversarial review (Phase 3)
    cost: dict
    attempts: int
    disclaimer: str

    @classmethod
    def from_result(cls, result: ResearchResult) -> "FundamentalsResponse":
        from jim.research.synthesize import DISCLAIMER

        citations = [
            CitationOut(
                id=f.id,
                label=f.label,
                value=f.value,
                unit=f.unit,
                is_derived=f.is_derived,
                form=f.form,
                fiscal_year=f.fiscal_year,
                fiscal_period=f.fiscal_period,
                accession=f.accession,
                filed=f.filed,
                source_url=f.source_url,
                derived_from=list(f.derived_from),
                formula=f.formula,
            )
            for f in (result.snapshot.facts if result.snapshot else [])
        ]
        sourcing = (
            SourcingOut(
                passed=result.gate.passed,
                coverage=round(result.gate.coverage, 4),
                figures_checked=result.gate.n_figures,
                figures_covered=result.gate.n_covered,
                violations=[f"{v.reason}: {v.figure}" for v in result.gate.violations],
            )
            if result.gate
            else None
        )
        faithfulness = (
            FaithfulnessOut(
                evaluated=not result.judge.skipped,
                score=round(result.judge.score, 3),
                issues=result.judge.issues,
            )
            if result.judge
            else None
        )
        return cls(
            product=result.product,
            ticker=result.ticker,
            company=result.entity_name,
            cik=result.cik,
            as_of=result.as_of,
            mode=result.mode,
            status=result.status,
            memo=result.memo,
            citations=citations,
            sourcing=sourcing,
            faithfulness=faithfulness,
            debate=result.debate,
            cost=result.cost,
            attempts=result.attempts,
            disclaimer=DISCLAIMER,
        )


# Generic alias: the response shape is product-agnostic.
ResearchResponse = FundamentalsResponse
