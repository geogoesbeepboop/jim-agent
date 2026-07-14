"""Memo synthesizer (Anthropic).

Turns a cited :class:`Snapshot` into a prose memo where every figure carries a
``[C#]`` citation. The hard sourcing rules live in a cached system prompt; the
per-call user message carries the facts and (on a retry) the gate's feedback.

Two modes:
  - "human": readable narrative with light context.
  - "agent": terse, metric-dense, minimal prose — for machine consumers.
"""

from __future__ import annotations

from dataclasses import dataclass

from jim.config import get_settings
from jim.llm import build_llm_client, live_llm_available
from jim.research.cost import Usage
from jim.research.facts import Snapshot

DISCLAIMER = (
    "This is general, impersonal information derived solely from public SEC "
    "filings. It is not investment advice, not personalized, and not a "
    "recommendation to buy or sell any security."
)

_SYSTEM = """You are jim, an impersonal financial-data summarizer. You are given a \
fixed set of FACTS about one company, each tagged with a citation id like [C3] and a value.

HARD RULES (a downstream gate will reject your output if you break any):
1. Every number you write — dollar amounts, percentages, ratios, share counts — \
MUST be one of the provided facts, written IMMEDIATELY followed by its [C#] citation. \
Example: "Revenue was $394.3 billion [C1]."
2. Never invent, estimate, extrapolate, or compute a number that is not in the FACTS. \
If a figure you want is not provided, describe it qualitatively with no number.
3. Quote each value as given. You may round (e.g. to one decimal or to billions), \
but the rounded number must still clearly equal the fact's value.
4. Stay impersonal and general. No personalized advice, no price targets, no \
buy/sell/hold recommendation, no predictions.
5. End the memo with the provided DISCLAIMER verbatim.

You write in one of two modes, given per request."""

_HUMAN_MODE = """Mode: HUMAN — for a person. Write a clear, readable memo (~300-450 words) \
with short sections: Overview, Valuation & market, Profitability, Balance sheet & leverage, \
Technical picture, Balanced takeaways. Explain what the numbers mean in plain language so a \
non-expert follows the story. Every number still carries its [C#]. Prose over bullets."""

_AGENT_MODE = """Mode: AGENT — for a machine consumer. Maximize signal, minimize prose. \
Output compact "<metric>: <value> [C#]" lines grouped under: VALUATION, INCOME, MARGINS, \
RETURNS, BALANCE SHEET, TECHNICALS. At most a 5-word qualifier per line. No narrative, no \
filler sentences. Include every relevant provided metric. One-line NET at the end."""


def _user_message(snapshot: Snapshot, mode: str, feedback: str | None, debate: str | None) -> str:
    mode_block = _AGENT_MODE if mode == "agent" else _HUMAN_MODE
    parts = [
        mode_block,
        f"\nCompany: {snapshot.entity_name} (ticker {snapshot.ticker}, ref {snapshot.cik}).",
        f"Data as of: {snapshot.as_of}.",
        "\nFACTS (use only these; cite each number with its [C#]):",
        snapshot.facts_block(),
    ]
    if debate:
        parts.append("\n" + debate)
    parts.append(f"\nDISCLAIMER (include verbatim at the end):\n{DISCLAIMER}")
    if feedback:
        parts.append(
            f"\nYOUR PREVIOUS ATTEMPT FAILED THE GATE:\n{feedback}\nRewrite the full memo."
        )
    return "\n".join(parts)


@dataclass
class SynthResult:
    memo: str
    usage: Usage


async def synthesize(
    snapshot: Snapshot,
    *,
    mode: str = "human",
    feedback: str | None = None,
    debate: str | None = None,
) -> SynthResult:
    """Generate a cited memo. Raises if no LLM credential is configured.

    Auth (API key vs subscription) is resolved by :func:`jim.llm.build_llm_client`.
    """
    settings = get_settings()
    if not live_llm_available():
        raise RuntimeError(
            "no LLM credential — the synthesizer needs ANTHROPIC_API_KEY, or "
            "LLM_AUTH_MODE=subscription with `claude login`. (The deterministic "
            "sourcing gate, however, runs without any credential.)"
        )

    client = build_llm_client()
    resp = await client.complete(
        model=settings.research_model,
        system=_SYSTEM,
        user=_user_message(snapshot, mode, feedback, debate),
        max_tokens=1500,
    )
    return SynthResult(memo=resp.text, usage=resp.usage)
