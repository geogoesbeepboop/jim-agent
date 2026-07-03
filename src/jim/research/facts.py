"""The cited data model.

Every number jim publishes is a :class:`Fact`. A *primary* fact comes straight
from an XBRL value in a specific SEC filing (its ``accession`` is the citation
anchor — it resolves to a real document). A *derived* fact (a margin, a growth
rate) is computed in code from other facts and cites those inputs by id.

This is the spine of the whole product: the sourcing gate later verifies that
every figure in the synthesized memo matches one of these facts.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# Canonical unit tags we use across the engine.
USD = "USD"
USD_PER_SHARE = "USD/shares"
SHARES = "shares"
PERCENT = "%"
MULTIPLE = "x"
COUNT = "count"  # transaction counts, holder counts, on-chain tallies
INDEX = "index"  # bounded/oscillator indicators: RSI, MACD


@dataclass(frozen=True)
class Fact:
    """A single cited number."""

    id: str  # stable citation id, e.g. "C1"
    label: str  # human label, e.g. "Revenue"
    value: float
    unit: str  # one of the unit tags above

    # Provenance (primary facts). None for derived facts.
    source_label: str | None = None  # "SEC EDGAR", "The Graph · Uniswap v3", ...
    concept: str | None = None  # us-gaap tag / on-chain field name
    accession: str | None = None  # citation anchor (SEC accession / subgraph id)
    form: str | None = None  # "10-K" / "10-Q" / "subgraph query"
    fiscal_year: int | None = None
    fiscal_period: str | None = None  # "FY", "Q1", ...
    filed: str | None = None  # filing/observation date (ISO)
    period_end: str | None = None  # reporting period end (ISO)
    source_url: str | None = None  # link that resolves the citation

    # Derived facts only.
    is_derived: bool = False
    derived_from: tuple[str, ...] = ()  # ids of the input facts
    formula: str | None = None  # human description, e.g. "Net income / Revenue"

    def citation(self) -> str:
        """One-line provenance string for display under the memo (source-agnostic)."""
        if self.is_derived:
            return f"[{self.id}] {self.label} = {self.formula} (derived from {', '.join(self.derived_from)})"
        bits = [f"[{self.id}] {self.label}:"]
        if self.source_label:
            bits.append(self.source_label)
        if self.form:
            bits.append(self.form)
        if self.fiscal_period and self.fiscal_year:
            bits.append(f"{self.fiscal_period} {self.fiscal_year}")
        meta = []
        if self.filed:
            meta.append(f"filed {self.filed}")
        if self.accession:
            meta.append(f"ref {self.accession}")
        if meta:
            bits.append(f"({', '.join(meta)})")
        if self.source_url:
            bits.append(f"— {self.source_url}")
        return " ".join(bits)


@dataclass
class Snapshot:
    """All cited facts for one company at one point in time."""

    ticker: str
    cik: str  # zero-padded 10-digit
    entity_name: str
    facts: list[Fact] = field(default_factory=list)
    as_of: str | None = None  # latest filing date among the facts
    # Phase 7: fact id → source name, for snapshots composed from several
    # sources (a peer agent's facts merged into a primary snapshot). Empty means
    # "everything came from the product's own source". Feeds the trust ledger's
    # gate-outcome attribution; deliberately not part of the fingerprint (the
    # data identity is the values, not who sold them).
    origins: dict[str, str] = field(default_factory=dict)

    def by_id(self, fact_id: str) -> Fact | None:
        for f in self.facts:
            if f.id == fact_id:
                return f
        return None

    def fingerprint(self) -> str:
        """A stable hash of the underlying data — the memo cache's identity key.

        Built from (label, unit, value) over all facts plus entity/as-of, so two
        gathers of *unchanged* data hash identically while any moved number (a new
        price, a fresh filing) changes the hash and correctly invalidates the cache.
        Independent of the C# id scheme (sorts by label) and tolerant of float
        jitter (values rounded), so it tracks real data changes, not noise."""
        import hashlib

        parts = [
            f"{f.label}|{f.unit}|{round(f.value, 4)}"
            for f in sorted(self.facts, key=lambda x: (x.label, x.unit))
        ]
        raw = f"{self.ticker}|{self.cik}|{self.as_of}|" + "||".join(parts)
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]

    @property
    def ids(self) -> set[str]:
        return {f.id for f in self.facts}

    def facts_block(self) -> str:
        """Render the facts as a compact, model-readable table for the synthesizer."""
        lines = []
        for f in self.facts:
            v = _fmt_value(f.value, f.unit)
            prov = (
                f"{f.form} {f.fiscal_period} {f.fiscal_year}"
                if not f.is_derived
                else f"derived: {f.formula}"
            )
            lines.append(f"[{f.id}] {f.label} = {v}  ({prov})")
        return "\n".join(lines)

    def citations_block(self) -> str:
        return "\n".join(f.citation() for f in self.facts)


def _fmt_value(value: float, unit: str) -> str:
    """Render a value the way the synthesizer is expected to quote it."""
    if unit == USD:
        return _fmt_usd(value)
    if unit == USD_PER_SHARE:
        return f"${value:,.2f}"
    if unit == SHARES:
        return f"{value:,.0f} shares"
    if unit == COUNT:
        return f"{value:,.0f}"
    if unit == INDEX:
        return f"{value:.1f}"
    if unit == PERCENT:
        return f"{value:.1f}%"
    if unit == MULTIPLE:
        return f"{value:.2f}x"
    return f"{value:,.2f}"


def _fmt_usd(value: float) -> str:
    a = abs(value)
    if a >= 1e9:
        return f"${value / 1e9:,.2f} billion"
    if a >= 1e6:
        return f"${value / 1e6:,.2f} million"
    return f"${value:,.0f}"


# --- Derived metric computation ---------------------------------------------

# A derived metric: (label, unit, formula text, fn(facts_by_label) -> value|None).
# Inputs are looked up by their canonical primary label; if any is missing the
# metric is skipped (we never fabricate a number from incomplete data).


def compute_derived(primary: list[Fact], next_id) -> list[Fact]:
    """Compute ratio/growth metrics from primary facts.

    ``next_id`` is a callable returning the next citation id ("C13", ...).
    Each derived Fact records the ids it was computed from so the gate (and a
    reader) can trace it back to filings.
    """
    by_label = {f.label: f for f in primary}
    derived: list[Fact] = []

    def add(label: str, unit: str, formula: str, value: float, inputs: list[Fact]):
        derived.append(
            Fact(
                id=next_id(),
                label=label,
                value=value,
                unit=unit,
                is_derived=True,
                derived_from=tuple(i.id for i in inputs),
                formula=formula,
            )
        )

    def get(*labels: str) -> Fact | None:
        for lbl in labels:
            if lbl in by_label:
                return by_label[lbl]
        return None

    revenue = get("Revenue")
    gross = get("Gross profit")
    op_income = get("Operating income")
    net_income = get("Net income")
    equity = get("Stockholders' equity")
    assets = get("Total assets")
    liabilities = get("Total liabilities")
    da = get("Depreciation & amortization")

    if revenue and revenue.value:
        if gross:
            add(
                "Gross margin",
                PERCENT,
                "Gross profit / Revenue",
                gross.value / revenue.value * 100,
                [gross, revenue],
            )
        if op_income:
            add(
                "Operating margin",
                PERCENT,
                "Operating income / Revenue",
                op_income.value / revenue.value * 100,
                [op_income, revenue],
            )
        if net_income:
            add(
                "Net margin",
                PERCENT,
                "Net income / Revenue",
                net_income.value / revenue.value * 100,
                [net_income, revenue],
            )
    if net_income and equity and equity.value:
        add(
            "Return on equity",
            PERCENT,
            "Net income / Stockholders' equity",
            net_income.value / equity.value * 100,
            [net_income, equity],
        )
    if net_income and assets and assets.value:
        add(
            "Return on assets",
            PERCENT,
            "Net income / Total assets",
            net_income.value / assets.value * 100,
            [net_income, assets],
        )
    if liabilities and equity and equity.value:
        add(
            "Debt-to-equity",
            MULTIPLE,
            "Total liabilities / Stockholders' equity",
            liabilities.value / equity.value,
            [liabilities, equity],
        )
    if op_income and da:
        add(
            "EBITDA",
            USD,
            "Operating income + Depreciation & amortization",
            op_income.value + da.value,
            [op_income, da],
        )

    return derived
