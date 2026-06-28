"""LLM faithfulness judge — the semantic second layer.

The deterministic gate proves every *number* is sourced. This judge catches the
softer failure modes a regex can't: a qualitative claim that the facts don't
support, an editorialized recommendation, a misleading comparison. It scores
groundedness 0–1 and fails the run below a threshold.

It is best-effort: with no API key (or with judging disabled) it returns a
``skipped`` result and the pipeline relies on the deterministic gate alone.
DeepEval's faithfulness metric is the drop-in here for the Phase 3 regression
suite; Phase 1 uses this lightweight native judge so the product ships now.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from anthropic import AsyncAnthropic

from jim.config import get_settings
from jim.research.cost import Usage
from jim.research.facts import Snapshot

_SYSTEM = """You are a strict faithfulness auditor. You are given a set of FACTS and a \
MEMO. Decide whether every claim in the memo is supported by the facts and whether the \
memo stays impersonal (no advice, no recommendations, no predictions).

Respond with ONLY a JSON object:
{"score": <0.0-1.0 groundedness>, "supported": <true|false>, "issues": [<short strings>]}
Score 1.0 = every claim is fully grounded and the tone is impersonal. Lower the score for \
any unsupported claim, editorialization, or recommendation. No prose outside the JSON."""


@dataclass
class JudgeResult:
    skipped: bool
    passed: bool
    score: float
    issues: list[str]
    usage: Usage | None = None

    @classmethod
    def skip(cls) -> "JudgeResult":
        return cls(skipped=True, passed=True, score=1.0, issues=[])


async def judge_faithfulness(memo: str, snapshot: Snapshot) -> JudgeResult:
    settings = get_settings()
    if not settings.enable_judge or not settings.anthropic_api_key:
        return JudgeResult.skip()

    client = AsyncAnthropic(api_key=settings.anthropic_api_key)
    user = (
        "FACTS:\n" + snapshot.facts_block() + "\n\nMEMO:\n" + memo + "\n\nReturn the JSON verdict."
    )
    resp = await client.messages.create(
        model=settings.judge_model,
        max_tokens=400,
        system=[{"type": "text", "text": _SYSTEM, "cache_control": {"type": "ephemeral"}}],
        messages=[{"role": "user", "content": user}],
    )
    usage = Usage(
        model=settings.judge_model,
        input_tokens=resp.usage.input_tokens,
        output_tokens=resp.usage.output_tokens,
    )
    text = "".join(b.text for b in resp.content if b.type == "text").strip()
    try:
        # Be tolerant of stray fencing.
        start, end = text.find("{"), text.rfind("}")
        data = json.loads(text[start : end + 1])
        score = float(data.get("score", 0.0))
        issues = [str(i) for i in data.get("issues", [])]
    except (ValueError, json.JSONDecodeError):
        # A judge we can't parse should not silently pass the run.
        return JudgeResult(
            skipped=False,
            passed=False,
            score=0.0,
            issues=["judge returned unparseable output"],
            usage=usage,
        )

    passed = score >= settings.judge_threshold
    return JudgeResult(skipped=False, passed=passed, score=score, issues=issues, usage=usage)
