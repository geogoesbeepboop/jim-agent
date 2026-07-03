"""The provable sourcing gate.

This is deterministic on purpose. No model is in the loop here, so its verdict
is reproducible and auditable: every financial figure in the memo must sit next
to a ``[C#]`` citation whose EDGAR-backed fact value *matches that figure*
(within a rounding tolerance). A number with no citation, a citation to a fact
whose value doesn't match, or a citation to an id we never published — each is a
violation, and any violation fails the run.

That property is what makes a planted hallucination provably blocked: a made-up
number has no fact whose value it matches, so it cannot be covered.

The extractor is deliberately paranoid (Track 0 hardening): beyond ``$``/``%``/
``x``/comma-grouped forms it also catches scientific notation (``3.9e11``),
word scales without grouping (``5 billion``), bare suffixes (``5B``), long bare
integers (``5000000000``), underscore grouping (``5_000_000_000``), spelled-out
figures (``five billion``, ``twenty-five percent``), and ranges
(``$1.2–1.4 billion``). Anything it can extract must be cited — the gate fails
closed, so an exotic rendering of a fabricated number is a rejection, never a
pass-through. Accounting negatives (``-$1.2 billion``, ``($1.2 billion)``) and
loss phrasing ("a loss of $1.2 billion" citing a negative fact) match by
magnitude so true statements aren't false-rejected.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from jim.research.facts import COUNT, MULTIPLE, PERCENT, SHARES, USD, USD_PER_SHARE, Snapshot

# One regex, several alternatives, ordered most-specific first: scientific
# notation, currency, percentage, multiple (1.96x), a number with a word scale
# ("5 billion" — no comma grouping required), comma/underscore-grouped bare
# numbers, an uppercase-suffixed number ("5B"), and long bare integer runs.
# Short bare integers (< 5 digits) without commas are intentionally ignored
# (segment counts, years, footnote markers) to avoid false positives.
_FIGURE_RE = re.compile(
    r"""
      (?P<sci>[-−(]?\s?[$€£]?\s?\d+(?:\.\d+)?[eE][+-]?\d+\)?)
    | (?P<cur>[-−(]?\s?[$€£]\s?\d[\d,_]*(?:\.\d+)?\s*(?:trillion|billion|million|thousand|tn|bn|mn|[TBMK])?\)?)
    | (?P<pct>[-−]?\d[\d,]*(?:\.\d+)?\s?(?:%|percent\b))
    | (?P<mult>\d+(?:\.\d+)?\s?x\b)
    | (?P<wordscale>[-−]?\d+(?:\.\d+)?\s?(?:trillion|billion|million|thousand|tn|bn|mn)\b)
    | (?P<num>\d{1,3}(?:[,_]\d{3})+(?:\.\d+)?\s*(?:trillion|billion|million|thousand)?)
    | (?P<sufnum>\d+(?:\.\d+)?(?-i:[TBMK])\b)
    | (?P<bigint>\d{5,})
    """,
    re.VERBOSE | re.IGNORECASE,
)

# A numeric range ("$1.2–1.4 billion", "3-5%"). Only treated as figures when a
# currency symbol, scale, or percent marker is present — so date spans
# ("2023-2024") and filing forms ("10-K") never match. Both endpoints inherit
# the shared scale/currency and each must independently match a cited fact.
_RANGE_RE = re.compile(
    r"""
    (?P<cursym>[$€£])?\s?(?P<lo>\d+(?:\.\d+)?)\s?[–—-]\s?[$€£]?(?P<hi>\d+(?:\.\d+)?)
    \s?(?P<scale>trillion|billion|million|thousand|tn|bn|mn|[TBMK])?\s?(?P<pctsym>%)?
    """,
    re.VERBOSE | re.IGNORECASE,
)

# Spelled-out figures: "five billion", "a million", "half a trillion",
# "twenty-five percent". A scale word with a spelled quantity is a checkable
# figure like any digit — it must match a cited fact or the run fails.
_WORDNUM_RE = re.compile(
    r"""
    \b(?P<words>
        half\s+a|an|a|one|two|three|four|five|six|seven|eight|nine|ten|eleven|twelve
      | (?:twenty|thirty|forty|fifty|sixty|seventy|eighty|ninety)
        (?:-(?:one|two|three|four|five|six|seven|eight|nine))?
    )\s+(?P<scale>trillion|billion|million|thousand|percent)\b
    """,
    re.VERBOSE | re.IGNORECASE,
)

_WORD_VALUES = {
    "a": 1.0,
    "an": 1.0,
    "one": 1.0,
    "two": 2.0,
    "three": 3.0,
    "four": 4.0,
    "five": 5.0,
    "six": 6.0,
    "seven": 7.0,
    "eight": 8.0,
    "nine": 9.0,
    "ten": 10.0,
    "eleven": 11.0,
    "twelve": 12.0,
    "twenty": 20.0,
    "thirty": 30.0,
    "forty": 40.0,
    "fifty": 50.0,
    "sixty": 60.0,
    "seventy": 70.0,
    "eighty": 80.0,
    "ninety": 90.0,
}

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


def _scale_of(token: str | None) -> float:
    """Multiplier for a scale word ('billion') or letter ('B'); 1.0 if absent."""
    if not token:
        return 1.0
    t = token.lower()
    for word, mult in _SCALE_WORDS:
        if t == word:
            return mult
    return _SCALE_LETTERS.get(t, 1.0)


def _to_float(raw: str, kind: str) -> float | None:
    s = raw.lower().strip()
    neg_paren = s.startswith("(") and s.endswith(")")
    for ch in "()$€£_,":
        s = s.replace(ch, "")
    s = s.replace("−", "-").replace("percent", "").replace("%", "").strip()
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
        value = float(s) * scale
    except ValueError:
        return None
    return -value if neg_paren and value > 0 else value


def _word_value(words: str, scale: str) -> float:
    """Value of a spelled-out figure: 'twenty-five' × 'million' → 25e6."""
    w = words.lower().strip()
    if w == "half a":
        qty = 0.5
    else:
        qty = sum(_WORD_VALUES.get(part, 0.0) for part in w.split("-"))
    if scale.lower() == "percent":
        return qty
    return qty * _scale_of(scale)


# A bare decimal sitting at the end of the text just before a citation, e.g. the
# "62.5" in "RSI is 62.5 [C30]". Catches indicators (RSI, MACD, ratios) that
# carry no $/%/x marker and so escape _FIGURE_RE.
_ANCHORED_RE = re.compile(r"(-?\d+\.\d+)\s*$")

# Units a bare (unmarked) number may legitimately quote: a count, a share
# tally, or an unprefixed amount.
_BARE_UNITS = (SHARES, COUNT, USD, USD_PER_SHARE)


def _unit_ok(kind: str, unit: str) -> bool:
    if kind == "pct":
        return unit == PERCENT
    if kind == "mult":
        return unit == MULTIPLE
    if kind == "cur":  # "$..." — dollar-denominated facts only
        return unit in (USD, USD_PER_SHARE)
    if kind == "anchored":  # a bare cited number — value match decides it
        return True
    # sci / wordscale / num / sufnum / bigint / wordnum: unmarked figures
    return unit in _BARE_UNITS


def _matches(value: float, kind: str, fact_value: float, unit: str) -> bool:
    if not _unit_ok(kind, unit):
        return False
    tol = max(abs(fact_value) * 0.02, 0.05)
    if abs(value - fact_value) <= tol:
        return True
    # Loss phrasing: "a loss of $1.2 billion [C5]" quoting a negative fact by
    # magnitude. Only when the *fact* is negative — a sign error against a
    # positive fact stays a mismatch.
    return fact_value < 0 and abs(abs(value) - abs(fact_value)) <= tol


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

    Four passes: (A) ranges ("$1.2–1.4 billion" → both endpoints), (B) the
    marked/grouped figures of ``_FIGURE_RE``, (C) a bare decimal immediately
    preceding a ``[C#]`` citation (RSI, MACD, …), and (D) spelled-out figures
    ("five billion"). Later passes skip spans an earlier pass consumed so
    nothing is double-counted.
    """
    cite_spans = [(m.start(), m.end()) for m in _CITE_RE.finditer(seg)]
    figures: list[tuple[str, float, str]] = []
    used: list[tuple[int, int]] = []

    for m in _RANGE_RE.finditer(seg):
        if _overlaps(m.start(), m.end(), cite_spans):
            continue
        if not (m.group("cursym") or m.group("scale") or m.group("pctsym")):
            continue  # unmarked span: a date range / filing form, not a figure
        kind = "cur" if m.group("cursym") else ("pct" if m.group("pctsym") else "wordscale")
        mult = _scale_of(m.group("scale"))
        for endpoint in ("lo", "hi"):
            figures.append((m.group().strip(), float(m.group(endpoint)) * mult, kind))
        used.append((m.start(), m.end()))

    for m in _FIGURE_RE.finditer(seg):
        if _overlaps(m.start(), m.end(), cite_spans) or _overlaps(m.start(), m.end(), used):
            continue  # digits inside a "[C30]" citation, or part of a range
        val = _to_float(m.group(), m.lastgroup or "num")
        if val is not None:
            figures.append((m.group().strip(), val, m.lastgroup or "num"))
            used.append((m.start(), m.end()))

    for cs, _ce in cite_spans:
        am = _ANCHORED_RE.search(seg[:cs])
        if not am or _overlaps(am.start(1), am.end(1), used):
            continue
        val = _to_float(am.group(1), "anchored")
        if val is not None:
            figures.append((am.group(1), val, "anchored"))
            used.append((am.start(1), am.end(1)))

    for m in _WORDNUM_RE.finditer(seg):
        if _overlaps(m.start(), m.end(), used):
            continue
        scale = m.group("scale")
        kind = "pct" if scale.lower() == "percent" else "wordscale"
        figures.append((m.group().strip(), _word_value(m.group("words"), scale), kind))

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
