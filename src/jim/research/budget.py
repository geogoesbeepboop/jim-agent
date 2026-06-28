"""Per-query budget cap — the propose/dispose split.

A data source *proposes* a purchase (it reasons about cost-vs-value: "I want a
price quote, it costs ~$0.01"). The budget *disposes*: deterministic code holds
a hard ceiling on data spend per query and approves or denies. The model can
want; only the code can spend. This is what keeps a runaway agent from buying
itself underwater.
"""

from __future__ import annotations

from dataclasses import dataclass, field

_EPS = 1e-9


@dataclass
class Decision:
    approved: bool
    reason: str
    amount_usd: float


@dataclass
class BudgetCap:
    ceiling_usd: float
    spent_usd: float = 0.0
    decisions: list[Decision] = field(default_factory=list)

    @property
    def remaining_usd(self) -> float:
        return max(0.0, self.ceiling_usd - self.spent_usd)

    def propose(self, amount_usd: float, reason: str = "") -> Decision:
        """Ask permission to spend ``amount_usd``. Does not commit."""
        if amount_usd <= self.remaining_usd + _EPS:
            decision = Decision(True, reason or "within budget", amount_usd)
        else:
            decision = Decision(
                False,
                f"denied: needs ${amount_usd:.4f}, only ${self.remaining_usd:.4f} left "
                f"of ${self.ceiling_usd:.4f} per-query budget",
                amount_usd,
            )
        self.decisions.append(decision)
        return decision

    def commit(self, amount_usd: float) -> None:
        """Record actual spend after a purchase settles."""
        self.spent_usd += amount_usd
