"""Phase 1+ research engine: cited snapshots, the sourcing gate, the pipeline.

Public names are resolved lazily (PEP 562) so importing a leaf module like
``jim.research.budget`` or ``jim.research.facts`` doesn't pull in the whole
engine — which would create an import cycle with ``jim.sources``.
"""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING

__all__ = [
    "Fact",
    "Snapshot",
    "GateResult",
    "check_sourcing",
    "ResearchResult",
    "run_research",
]

_LAZY = {
    "Fact": ("jim.research.facts", "Fact"),
    "Snapshot": ("jim.research.facts", "Snapshot"),
    "GateResult": ("jim.research.gate", "GateResult"),
    "check_sourcing": ("jim.research.gate", "check_sourcing"),
    "ResearchResult": ("jim.research.engine", "ResearchResult"),
    "run_research": ("jim.research.engine", "run_research"),
}


def __getattr__(name: str):
    if name in _LAZY:
        module, attr = _LAZY[name]
        return getattr(importlib.import_module(module), attr)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


if TYPE_CHECKING:  # for type checkers / IDEs only
    from jim.research.engine import ResearchResult, run_research
    from jim.research.facts import Fact, Snapshot
    from jim.research.gate import GateResult, check_sourcing
