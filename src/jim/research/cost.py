"""Token usage + cost estimation.

Phase 1 records an approximate inference cost per run so Langfuse traces carry
cost alongside factuality. Phase 2's margin engine extends this with the x402
data-purchase costs to compute true per-query margin.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# Approximate USD per 1M tokens (input, output). Update as pricing changes;
# these are deliberately easy to find and override.
MODEL_PRICING: dict[str, tuple[float, float]] = {
    "claude-opus-4-8": (15.0, 75.0),
    "claude-sonnet-4-6": (3.0, 15.0),
    "claude-haiku-4-5-20251001": (1.0, 5.0),
}


@dataclass
class Usage:
    model: str
    input_tokens: int = 0
    output_tokens: int = 0

    def cost_usd(self) -> float:
        rate_in, rate_out = MODEL_PRICING.get(self.model, (0.0, 0.0))
        return self.input_tokens / 1e6 * rate_in + self.output_tokens / 1e6 * rate_out


@dataclass
class CostLedger:
    """Accumulates every model call in a run."""

    usages: list[Usage] = field(default_factory=list)

    def add(self, usage: Usage | None) -> None:
        if usage is not None:
            self.usages.append(usage)

    @property
    def input_tokens(self) -> int:
        return sum(u.input_tokens for u in self.usages)

    @property
    def output_tokens(self) -> int:
        return sum(u.output_tokens for u in self.usages)

    @property
    def inference_cost_usd(self) -> float:
        return sum(u.cost_usd() for u in self.usages)
