"""Update synthesizer — the short, impersonal "what changed" note.

Only reached when the materiality gate says something changed, so its cost is
paid *exactly* when there is news. Two paths, both gate-verified:

  - LLM path (with ANTHROPIC_API_KEY): writes a terse update over the signals +
    current facts, then the sourcing gate and the impersonal guard must both
    pass; a failure retries with feedback, bounded.
  - Deterministic fallback (no key, or the LLM never satisfies both gates):
    builds the note straight from the cited facts behind each signal, so it
    passes the sourcing gate *by construction* and is impersonal by construction.

That fallback is the same trust property the project leans on everywhere — like
the gate, monitoring works with **no API key at all**; the key only buys nicer
prose.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from jim.config import get_settings
from jim.monitors.impersonal import check_impersonal
from jim.monitors.models import Signal
from jim.research.cost import Usage
from jim.research.facts import Snapshot, _fmt_value
from jim.research.gate import GateResult, check_sourcing
from jim.research.synthesize import DISCLAIMER

_SYSTEM = """You are jim, an impersonal financial-data monitor. A deterministic crew has \
already detected what CHANGED for one entity since the last check; you write the short \
update note a neutral wire service would publish.

HARD RULES (a downstream gate rejects your output if you break any):
1. Every number — dollar amounts, percentages, ratios, indicator readings — MUST be one \
of the provided FACTS, written IMMEDIATELY followed by its [C#] citation. Example: \
"RSI is 72.3 [C30]."
2. Never invent, estimate, or compute a number not in the FACTS. Describe the *change* in \
words; only the current values carry numbers, and only when cited.
3. Stay impersonal and general: no "you", no advice, no buy/sell/hold, no price targets, \
no predictions.
4. Be brief — at most ~110 words. Lead with the most material change.
5. End with the DISCLAIMER verbatim."""

_MODE_HINT = {
    "human": "Mode: HUMAN — one or two plain-language sentences a non-expert follows, "
    "then the cited specifics.",
    "agent": "Mode: AGENT — terse '<metric>: <value> [C#]' lines, no narrative, "
    "one-line NET at the end.",
}

_DIRECTION_CLAUSE = {
    "up": "higher than the prior reading",
    "down": "lower than the prior reading",
    "cross_up": "crossing above the configured level",
    "cross_down": "crossing below the configured level",
    "golden": "with the 50-day now above the 200-day",
    "death": "with the 50-day now below the 200-day",
    "new": "",
}


@dataclass
class UpdateResult:
    memo: str
    severity: str
    gate: GateResult
    used_fallback: bool
    usages: list[Usage] = field(default_factory=list)

    @property
    def inference_cost_usd(self) -> float:
        return sum(u.cost_usd() for u in self.usages)


def _fallback_memo(snapshot: Snapshot, signals: list[Signal]) -> str:
    """A gate-safe, impersonal note built directly from each signal's cited fact."""
    lines = [f"{snapshot.entity_name} ({snapshot.ticker}) — monitor update."]
    for sig in signals:
        fact = next((snapshot.by_id(cid) for cid in sig.citation_ids), None)
        if fact is not None:
            clause = _DIRECTION_CLAUSE.get(sig.direction or "", "")
            tail = f", {clause}" if clause else ""
            lines.append(
                f"{fact.label} is now {_fmt_value(fact.value, fact.unit)} [{fact.id}]{tail}."
            )
        elif sig.kind == "new_filing":
            lines.append(
                f"A new primary-source filing was observed for {snapshot.ticker} this period."
            )
        else:
            lines.append(f"{sig.label} changed since the last check.")
    lines.append(DISCLAIMER)
    return "\n".join(lines)


def _signal_block(signals: list[Signal]) -> str:
    return "\n".join(f"- [{s.severity}] {s.summary}" for s in signals)


def _accept(memo: str, snapshot: Snapshot) -> tuple[bool, GateResult, list[str]]:
    gate = check_sourcing(memo, snapshot)
    imp = check_impersonal(memo)
    return (gate.passed and imp.passed), gate, imp.violations


async def synthesize_update(
    snapshot: Snapshot,
    signals: list[Signal],
    *,
    severity: str = "info",
    mode: str = "agent",
    max_attempts: int = 2,
) -> UpdateResult:
    """Write the update note, gate-and-impersonal-verified, fallback on failure."""
    settings = get_settings()

    if not settings.anthropic_api_key:
        memo = _fallback_memo(snapshot, signals)
        ok, gate, _ = _accept(memo, snapshot)
        return UpdateResult(memo=memo, severity=severity, gate=gate, used_fallback=True)

    from anthropic import AsyncAnthropic

    client = AsyncAnthropic(api_key=settings.anthropic_api_key)
    usages: list[Usage] = []
    feedback: str | None = None

    for _ in range(max(1, max_attempts)):
        parts = [
            _MODE_HINT.get(mode, _MODE_HINT["agent"]),
            f"Entity: {snapshot.entity_name} ({snapshot.ticker}). Data as of {snapshot.as_of}.",
            "\nDETECTED CHANGES (already verified by a deterministic crew):",
            _signal_block(signals),
            "\nCURRENT FACTS (cite each number you use with its [C#]):",
            snapshot.facts_block(),
            f"\nDISCLAIMER (include verbatim at the end):\n{DISCLAIMER}",
        ]
        if feedback:
            parts.append(f"\nYOUR PREVIOUS ATTEMPT WAS REJECTED:\n{feedback}\nRewrite the note.")
        try:
            resp = await client.messages.create(
                model=settings.research_model,
                max_tokens=500,
                system=[{"type": "text", "text": _SYSTEM, "cache_control": {"type": "ephemeral"}}],
                messages=[{"role": "user", "content": "\n".join(parts)}],
            )
        except Exception:
            # API/transport error → drop to the construction-safe fallback below.
            # A monitor must still emit a correct, cited update even if the LLM is down.
            break
        usages.append(
            Usage(
                model=settings.research_model,
                input_tokens=resp.usage.input_tokens,
                output_tokens=resp.usage.output_tokens,
            )
        )
        memo = "".join(b.text for b in resp.content if b.type == "text").strip()
        ok, gate, imp_violations = _accept(memo, snapshot)
        if ok:
            return UpdateResult(
                memo=memo, severity=severity, gate=gate, used_fallback=False, usages=usages
            )
        feedback = "\n".join(
            [gate.feedback()] + [f"impersonal: {v}" for v in imp_violations]
        ).strip()

    # Never satisfied both gates → ship the construction-safe fallback.
    memo = _fallback_memo(snapshot, signals)
    _, gate, _ = _accept(memo, snapshot)
    return UpdateResult(memo=memo, severity=severity, gate=gate, used_fallback=True, usages=usages)
