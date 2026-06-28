"""The provable sourcing gate.

This is deterministic on purpose. No model is in the loop here, so its verdict
is reproducible and auditable: every financial figure in the memo must sit next
to a ``[C#]`` citation whose EDGAR-backed fact value *matches that figure*
(within a rounding tolerance). A number with no citation, a citation to a fact
whose value doesn't match, or a citation to an id we never published — each is a
violation, and any violation fails the run.

That property is what makes a planted hallucination provably blocked: a made-up
number has no fact whose value it matches, so it cannot be covered.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from jim.research.facts import COUNT, MULTIPLE, PERCENT, SHARES, USD, USD_PER_SHARE, Snapshot

# One regex, four alternatives: currency, percentage, multiple (1.96x), and
# comma-grouped bare numbers. Bare integers without commas are intentionally
# ignored (segment counts, footnote markers) to avoid false positives.
_FIGURE_RE = re.compile(
    r"""
      (?P<cur>\$\s?\d[\d,]*(?:\.\d+)?\s*(?:trillion|billion|million|thousand|tn|bn|mn|[TBMK])?)
    | (?P<pct>-?\d[\d,]*(?:\.\d+)?\s?%)
    | (?P<mult>\d+(?:\.\d+)?\s?x\b)
    | (?P<num>\d{1,3}(?:,\d{3})+(?:\.\d+)?\s*(?:trillion|billion|million|thousand)?)
    """,
    re.VERBOSE | re.IGNORECASE,
)

_CITE_RE = re.compile(r"\[(C\d+(?:\s*,\s*C\d+)*)\]")
_SCALE_WORDS = [
    ("trillion", 1e12),
    ("billion", 1e9),
    ("million", 1e6),
    ("thousand", 1e3),
    ("tn", 1e12),
    ("bn", 1e9),
    ("mn", 1e6),
]
_SCALE_LETTERS = {"t": 1e12, "b": 1e9, "m": 1e6, "k": 1e3}


@dataclass
class Violation:
    figure: str
    reason: str  # "uncited" | "value mismatch" | "phantom citation"
    segment: str
    cited_ids: list[str] = field(default_factory=list)


@dataclass
class GateResult:
    passed: bool
    violations: list[Violation] = field(default_factory=list)
    n_figures: int = 0
    n_covered: int = 0

    @property
    def coverage(self) -> float:
        return 1.0 if self.n_figures == 0 else self.n_covered / self.n_figures

    def feedback(self) -> str:
        """Actionable feedback fed back to the synthesizer on a failed attempt."""
        if self.passed:
            return ""
        lines = ["The sourcing gate REJECTED the memo. Fix every issue below:"]
        for v in self.violations:
            lines.append(f'  - {v.reason}: "{v.figure}" (citations: {v.cited_ids or "none"})')
        lines.append(
            "Rules: every dollar amount, percentage, ratio, or large number MUST be "
            "immediately followed by a [C#] citation whose fact value equals it. "
            "Do not state any number that is not in the provided facts."
        )
        return "\n".join(lines)


def _to_float(raw: str, kind: str) -> float | None:
    s = raw.lower().replace("$", "").replace("%", "").replace(",", "").strip()
    if kind == "mult":
        s = s.rstrip("x").strip()
    scale = 1.0
    for word, mult in _SCALE_WORDS:
        if s.endswith(word):
            s = s[: -len(word)].strip()
            scale = mult
            break
    else:
        if s and s[-1] in _SCALE_LETTERS and not s[-1].isdigit():
            scale = _SCALE_LETTERS[s[-1]]
            s = s[:-1].strip()
    try:
        return float(s) * scale
    except ValueError:
        return None


# A bare decimal sitting at the end of the text just before a citation, e.g. the
# "62.5" in "RSI is 62.5 [C30]". Catches indicators (RSI, MACD, ratios) that
# carry no $/%/x marker and so escape _FIGURE_RE.
_ANCHORED_RE = re.compile(r"(-?\d+\.\d+)\s*$")


def _unit_ok(kind: str, unit: str) -> bool:
    if kind == "pct":
        return unit == PERCENT
    if kind == "mult":
        return unit == MULTIPLE
    if kind == "cur":  # "$..." — dollar-denominated facts only
        return unit in (USD, USD_PER_SHARE)
    if kind == "anchored":  # a bare cited number — value match decides it
        return True
    # bare comma-grouped number: a count, a share tally, or an unprefixed amount
    return unit in (SHARES, COUNT, USD, USD_PER_SHARE)


def _matches(value: float, kind: str, fact_value: float, unit: str) -> bool:
    if not _unit_ok(kind, unit):
        return False
    tol = max(abs(fact_value) * 0.02, 0.05)
    return abs(value - fact_value) <= tol


def _segments(memo: str) -> list[str]:
    # Split on line breaks first, then sentence boundaries. Citation scope is a
    # single sentence/bullet, so a figure must be cited right where it appears.
    chunks: list[str] = []
    for line in memo.splitlines():
        chunks.extend(re.split(r"(?<=[.!?])\s+", line))
    return [c for c in chunks if c.strip()]


def _overlaps(start: int, end: int, spans: list[tuple[int, int]]) -> bool:
    return any(start < e and s < end for s, e in spans)


def _segment_figures(seg: str) -> list[tuple[str, float, str]]:
    """All checkable figures in a segment, with citation digits excluded.

    Two passes: (A) ``$``/``%``/``x``/comma-grouped figures anywhere, and
    (B) a bare decimal immediately preceding a ``[C#]`` citation (RSI, MACD, …),
    skipping any that overlap an A match so we don't double-count "$6.13".
    """
    cite_spans = [(m.start(), m.end()) for m in _CITE_RE.finditer(seg)]
    figures: list[tuple[str, float, str]] = []
    a_spans: list[tuple[int, int]] = []

    for m in _FIGURE_RE.finditer(seg):
        if _overlaps(m.start(), m.end(), cite_spans):
            continue  # digits inside a "[C30]" citation
        val = _to_float(m.group(), m.lastgroup or "num")
        if val is not None:
            figures.append((m.group().strip(), val, m.lastgroup or "num"))
            a_spans.append((m.start(), m.end()))

    for cs, _ce in cite_spans:
        am = _ANCHORED_RE.search(seg[:cs])
        if not am or _overlaps(am.start(1), am.end(1), a_spans):
            continue
        val = _to_float(am.group(1), "anchored")
        if val is not None:
            figures.append((am.group(1), val, "anchored"))

    return figures


def check_sourcing(memo: str, snapshot: Snapshot) -> GateResult:
    """Verify every figure in ``memo`` resolves to a matching cited fact."""
    valid_ids = snapshot.ids
    violations: list[Violation] = []
    n_figures = 0
    n_covered = 0

    for seg in _segments(memo):
        cited_ids = [cid.strip() for group in _CITE_RE.findall(seg) for cid in group.split(",")]
        for cid in cited_ids:
            if cid not in valid_ids:
                violations.append(
                    Violation(
                        figure=cid,
                        reason="phantom citation",
                        segment=seg.strip(),
                        cited_ids=cited_ids,
                    )
                )

        cited_facts = [f for f in (snapshot.by_id(c) for c in cited_ids) if f is not None]
        for raw, value, kind in _segment_figures(seg):
            n_figures += 1
            if not cited_facts:
                violations.append(
                    Violation(
                        figure=raw, reason="uncited", segment=seg.strip(), cited_ids=cited_ids
                    )
                )
            elif any(_matches(value, kind, f.value, f.unit) for f in cited_facts):
                n_covered += 1
            else:
                violations.append(
                    Violation(
                        figure=raw,
                        reason="value mismatch",
                        segment=seg.strip(),
                        cited_ids=cited_ids,
                    )
                )

    return GateResult(
        passed=not violations,
        violations=violations,
        n_figures=n_figures,
        n_covered=n_covered,
    )
