"""Adversarial verification: bull vs bear vs judge, before the memo is written.

Three independent passes over the same cited facts:
  - bull: argues the strongest *supported* upside case, citing [C#].
  - bear: argues the strongest *supported* downside case, citing [C#].
  - judge: weighs both, discards over-reaching claims, and states a balanced net.

The judge's verdict becomes context for the synthesizer, so the published memo
is one whose thesis was attacked from both sides first. Every number anyone uses
must still be a provided fact — the final memo is gated as always.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass

from jim.config import get_settings
from jim.llm import LLMClient, build_llm_client, live_llm_available
from jim.research.cost import Usage
from jim.research.facts import Snapshot

_RULES = (
    "Use ONLY the provided FACTS. Every number you cite must be one of them, written "
    "immediately followed by its [C#] tag. Do not invent numbers. Stay impersonal — "
    "no advice or price targets. Be concise (≤180 words)."
)

_BULL = (
    "You are a BULL analyst. Make the strongest evidence-based case that the company is "
    "attractive, grounded in the facts. " + _RULES
)
_BEAR = (
    "You are a BEAR analyst. Make the strongest evidence-based case for caution/risk, "
    "grounded in the facts. " + _RULES
)
_JUDGE = (
    "You are an impartial JUDGE. You are given a BULL case and a BEAR case over the same FACTS. "
    "Identify which specific claims each side supports with the facts and which over-reach. "
    "Then state a balanced net assessment a neutral analyst would defend. "
    "Cite figures with [C#]. Impersonal, no recommendation. ≤200 words."
)


@dataclass
class DebateResult:
    bull: str
    bear: str
    verdict: str
    usages: list[Usage]

    def context(self) -> str:
        """The block handed to the synthesizer."""
        return (
            "ADVERSARIAL REVIEW (already completed over the same facts — reflect this "
            "balance in the memo; cite the same [C#]):\n"
            f"\nBULL CASE:\n{self.bull}\n\nBEAR CASE:\n{self.bear}\n\nJUDGE'S VERDICT:\n{self.verdict}"
        )


async def _ask(client: LLMClient, model: str, system: str, user: str) -> tuple[str, Usage]:
    resp = await client.complete(model=model, system=system, user=user, max_tokens=600)
    return resp.text, resp.usage


async def run_debate(snapshot: Snapshot) -> DebateResult:
    """Run bull ∥ bear, then the judge. Returns the verdict + token usage."""
    settings = get_settings()
    if not live_llm_available():
        raise RuntimeError(
            "no LLM credential — the debate needs ANTHROPIC_API_KEY, or "
            "LLM_AUTH_MODE=subscription with `claude login` (or set ENABLE_DEBATE=false). "
            "The deterministic sourcing gate runs without any credential."
        )
    client = build_llm_client()
    model = settings.debate_model
    facts = (
        f"Company: {snapshot.entity_name} ({snapshot.ticker})\n\nFACTS:\n{snapshot.facts_block()}"
    )

    (bull, u1), (bear, u2) = await asyncio.gather(
        _ask(client, model, _BULL, facts),
        _ask(client, model, _BEAR, facts),
    )
    judge_input = f"{facts}\n\nBULL CASE:\n{bull}\n\nBEAR CASE:\n{bear}"
    verdict, u3 = await _ask(client, model, _JUDGE, judge_input)

    return DebateResult(bull=bull, bear=bear, verdict=verdict, usages=[u1, u2, u3])
