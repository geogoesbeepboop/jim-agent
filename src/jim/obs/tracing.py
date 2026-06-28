"""Best-effort Langfuse tracing.

Every research run opens a trace; each pipeline step is a nested span; factuality
and cost land as scores on the trace. If Langfuse isn't installed or configured
(no ``LANGFUSE_PUBLIC_KEY`` / ``LANGFUSE_SECRET_KEY``), everything degrades to a
no-op — the pipeline is identical, just untraced. Tracing must never break a run,
so every Langfuse call is guarded.
"""

from __future__ import annotations

import contextlib
import os
from typing import Any, Iterator


def _langfuse_client():
    if not (os.getenv("LANGFUSE_PUBLIC_KEY") and os.getenv("LANGFUSE_SECRET_KEY")):
        return None
    try:
        from langfuse import Langfuse

        return Langfuse()
    except Exception:
        return None


class _Trace:
    """Thin wrapper over a Langfuse client; all methods are failure-tolerant."""

    def __init__(self, client: Any | None):
        self._client = client

    @contextlib.contextmanager
    def span(self, name: str, *, input: Any = None) -> Iterator["_Trace"]:
        if self._client is None:
            yield self
            return
        try:
            with self._client.start_as_current_observation(name=name, as_type="span", input=input):
                yield self
        except Exception:
            yield self

    def score(self, name: str, value: float | int | bool) -> None:
        if self._client is None:
            return
        with contextlib.suppress(Exception):
            self._client.score_current_trace(name=name, value=float(value))

    def update(self, *, output: Any = None, metadata: dict | None = None) -> None:
        if self._client is None:
            return
        with contextlib.suppress(Exception):
            self._client.update_current_span(output=output, metadata=metadata)

    def cost(self, usd: float, input_tokens: int, output_tokens: int) -> None:
        self.score("inference_cost_usd", usd)
        self.score("input_tokens", input_tokens)
        self.score("output_tokens", output_tokens)


@contextlib.contextmanager
def research_trace(ticker: str, mode: str) -> Iterator[_Trace]:
    """Open a research-run trace (no-op if Langfuse is unconfigured)."""
    client = _langfuse_client()
    trace = _Trace(client)
    if client is None:
        yield trace
        return
    try:
        with client.start_as_current_observation(
            name="research.fundamentals",
            as_type="span",
            input={"ticker": ticker, "mode": mode},
        ):
            yield trace
    except Exception:
        yield trace
    finally:
        with contextlib.suppress(Exception):
            client.flush()
