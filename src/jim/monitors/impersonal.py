"""Deterministic impersonal-output guard.

Phase 4's compliance line is *"every monitor output stays general, not
personalized."* This is the deterministic backstop for that property — the same
shape as the sourcing gate, but for tone rather than numbers. It scans published
prose for second-person address, personalized advice, and buy/sell/hold
recommendations, and returns any violations. The synthesizer prompt already
forbids these; this proves it, reproducibly, with no model in the loop.

The verbatim DISCLAIMER is stripped before scanning (it legitimately contains
"not a recommendation to buy or sell", "not personalized", etc.).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from jim.research.synthesize import DISCLAIMER

# (pattern, reason) — word-boundaried so "buy" inside "buyback" never trips.
_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"\byou(?:'re|r|rself|rselves)?\b", re.I), "second-person address"),
    (re.compile(r"\b(?:we|i)\s+recommend\b", re.I), "recommendation"),
    (re.compile(r"\brecommendations?\b", re.I), "recommendation"),
    (re.compile(r"\bshould\s+(?:buy|sell|hold|consider|avoid)\b", re.I), "advice"),
    (re.compile(r"\bstrong\s+(?:buy|sell)\b", re.I), "rating/recommendation"),
    (re.compile(r"\bprice\s+targets?\b", re.I), "price target"),
    (re.compile(r"\b(?:buy|sell)\s+(?:now|today|immediately)\b", re.I), "advice"),
    (re.compile(r"\byour\s+(?:portfolio|position|holdings|account)\b", re.I), "personalization"),
]


@dataclass
class ImpersonalResult:
    passed: bool
    violations: list[str] = field(default_factory=list)


def _strip_disclaimer(text: str) -> str:
    return text.replace(DISCLAIMER, " ")


def check_impersonal(text: str) -> ImpersonalResult:
    """Return violations where the text becomes personal or gives advice."""
    body = _strip_disclaimer(text or "")
    violations: list[str] = []
    seen: set[str] = set()
    for pat, reason in _PATTERNS:
        m = pat.search(body)
        if m:
            phrase = m.group(0).strip()
            key = f"{reason}:{phrase.lower()}"
            if key not in seen:
                seen.add(key)
                violations.append(f'{reason}: "{phrase}"')
    return ImpersonalResult(passed=not violations, violations=violations)
