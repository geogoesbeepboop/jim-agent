"""LLM faithfulness judge — the semantic second layer.

The deterministic gate proves every *number* is sourced. This judge catches the
softer failure modes a regex can't: a qualitative claim the facts don't support,
an editorialized recommendation, a misleading comparison. It returns a **per-claim
checklist** — each claim extracted from the memo, marked supported/unsupported
with the fact it leans on and a reason — plus an overall groundedness score, and
fails the run below a threshold.

Two knobs:
  - It is best-effort: with no API key (or judging disabled) it returns a
    ``skipped`` result and the pipeline relies on the deterministic gate alone.
  - High-stakes runs (``high_stakes=True``) upgrade to a stronger judge model
    (``JUDGE_HIGH_STAKES_MODEL``, Sonnet by default) — the same checklist, more
    scrutiny — for when a wrong call is expensive.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field

from anthropic import AsyncAnthropic

from jim.config import get_settings
from jim.research.cost import Usage
from jim.research.facts import Snapshot

_SYSTEM = """You are a strict faithfulness auditor. You are given a set of FACTS and a \
MEMO. Work claim by claim: extract each distinct factual or evaluative claim the memo \
makes, and decide whether the FACTS support it and whether it stays impersonal (no advice, \
no recommendations, no predictions).

Respond with ONLY a JSON object of this exact shape:
{
  "score": <0.0-1.0 overall groundedness>,
  "supported": <true|false overall>,
  "claims": [
    {"claim": "<short paraphrase>", "supported": <true|false>,
     "citation": "<C# the claim relies on, or null>", "reason": "<≤12 words>"}
  ],
  "issues": ["<short strings naming the unsupported/over-reaching claims>"]
}
Score 1.0 = every claim fully grounded and impersonal. Lower it for any unsupported claim, \
editorialization, or recommendation. No prose outside the JSON."""


@dataclass
class ClaimVerdict:
    claim: str
    supported: bool
    citation: str | None = None
    reason: str = ""

    def to_dict(self) -> dict:
        return {
            "claim": self.claim,
            "supported": self.supported,
            "citation": self.citation,
            "reason": self.reason,
        }


@dataclass
class JudgeResult:
    skipped: bool
    passed: bool
    score: float
    issues: list[str]
    claims: list[ClaimVerdict] = field(default_factory=list)
    model: str | None = None
    usage: Usage | None = None

    @property
    def unsupported_claims(self) -> list[ClaimVerdict]:
        return [c for c in self.claims if not c.supported]

    @classmethod
    def skip(cls) -> "JudgeResult":
        return cls(skipped=True, passed=True, score=1.0, issues=[], claims=[])


def _parse_claims(raw) -> list[ClaimVerdict]:
    claims: list[ClaimVerdict] = []
    for c in raw if isinstance(raw, list) else []:
        if not isinstance(c, dict):
            continue
        claims.append(
            ClaimVerdict(
                claim=str(c.get("claim", "")).strip(),
                supported=bool(c.get("supported", False)),
                citation=(str(c["citation"]) if c.get("citation") else None),
                reason=str(c.get("reason", "")).strip(),
            )
        )
    return claims


async def judge_faithfulness(
    memo: str, snapshot: Snapshot, *, high_stakes: bool = False
) -> JudgeResult:
    settings = get_settings()
    if not settings.enable_judge or not settings.anthropic_api_key:
        return JudgeResult.skip()

    model = settings.judge_high_stakes_model if high_stakes else settings.judge_model
    client = AsyncAnthropic(api_key=settings.anthropic_api_key)
    user = (
        "FACTS:\n" + snapshot.facts_block() + "\n\nMEMO:\n" + memo + "\n\nReturn the JSON verdict."
    )
    resp = await client.messages.create(
        model=model,
        max_tokens=900,  # room for the per-claim checklist
        system=[{"type": "text", "text": _SYSTEM, "cache_control": {"type": "ephemeral"}}],
        messages=[{"role": "user", "content": user}],
    )
    usage = Usage(
        model=model,
        input_tokens=resp.usage.input_tokens,
        output_tokens=resp.usage.output_tokens,
    )
    text = "".join(b.text for b in resp.content if b.type == "text").strip()
    try:
        # Be tolerant of stray fencing.
        start, end = text.find("{"), text.rfind("}")
        data = json.loads(text[start : end + 1])
        score = float(data.get("score", 0.0))
        claims = _parse_claims(data.get("claims"))
        issues = [str(i) for i in data.get("issues", [])]
        # Fall back to deriving issues from the checklist if the model omitted them.
        if not issues:
            issues = [f"unsupported: {c.claim}" for c in claims if not c.supported]
    except (ValueError, json.JSONDecodeError):
        # A judge we can't parse should not silently pass the run.
        return JudgeResult(
            skipped=False,
            passed=False,
            score=0.0,
            issues=["judge returned unparseable output"],
            claims=[],
            model=model,
            usage=usage,
        )

    passed = score >= settings.judge_threshold
    return JudgeResult(
        skipped=False,
        passed=passed,
        score=score,
        issues=issues,
        claims=claims,
        model=model,
        usage=usage,
    )
