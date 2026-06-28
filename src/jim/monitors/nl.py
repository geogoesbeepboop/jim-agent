"""Natural-language → monitor spec (propose / dispose).

"Watch AAPL for big earnings moves and overbought RSI" → a validated list of
deterministic triggers. This is the architecture's stated pattern (§9.2): the
model *proposes* a structured spec; code *disposes* — every proposed trigger is
checked against the real trigger registry and its thresholds clamped before it
can ever run. An LLM is used only to map fuzzy intent onto known kinds; if no key
is set, a deterministic keyword parser does the same job (so this is testable and
works offline, just less flexibly).
"""

from __future__ import annotations

import re

from jim.config import get_settings
from jim.monitors.models import TriggerSpec
from jim.monitors.triggers import EVALUATORS, default_triggers

_INTERVAL_WORDS = {
    "minutely": 60,
    "hourly": 3_600,
    "intraday": 3_600,
    "daily": 86_400,
    "nightly": 86_400,
    "weekly": 604_800,
}
_UNIT_SECONDS = {
    "sec": 1,
    "second": 1,
    "min": 60,
    "minute": 60,
    "hour": 3_600,
    "hr": 3_600,
    "day": 86_400,
    "week": 604_800,
}


def parse_interval(text: str) -> int | None:
    t = text.lower()
    m = re.search(r"every\s+(\d+)\s*(sec|second|min|minute|hour|hr|day|week)s?", t)
    if m:
        return int(m.group(1)) * _UNIT_SECONDS[m.group(2)]
    for word, secs in _INTERVAL_WORDS.items():
        if word in t:
            return secs
    return None


def duration_seconds(value: str) -> int:
    """Parse '1h' / '30m' / '2d' / '90s' / bare seconds into seconds."""
    value = value.strip().lower()
    m = re.fullmatch(r"(\d+)\s*([smhdw]?)", value)
    if not m:
        raise ValueError(f"unrecognized duration: {value!r} (try 30m, 1h, 1d)")
    n = int(m.group(1))
    return n * {"": 1, "s": 1, "m": 60, "h": 3_600, "d": 86_400, "w": 604_800}[m.group(2)]


def detect_product(text: str) -> str:
    t = text.lower()
    if any(w in t for w in ("token", "on-chain", "onchain", "crypto", "defi", "uniswap", "tvl")):
        return "token"
    return "fundamentals"


def parse_identifier(text: str) -> str | None:
    """Best-effort ticker/symbol extraction: a 1-5 char all-caps word.

    Best-effort only: indicator acronyms are stop-worded, which can shadow a real
    ticker that collides with one (e.g. "PB", "PE"). Callers that know the
    identifier should pass it explicitly rather than rely on this.
    """
    cands = re.findall(r"\b([A-Z]{1,5})\b", text)
    stop = {
        "RSI",
        "EPS",
        "MACD",
        "SMA",
        "TVL",
        "SEC",
        "USD",
        "P",
        "PE",
        "PB",
        "AND",
        "OR",
        "A",
        "I",
    }
    cands = [c for c in cands if c not in stop]
    return cands[0] if cands else None


def _pcts(text: str) -> list[float]:
    return [float(x) for x in re.findall(r"(\d+(?:\.\d+)?)\s*%", text)]


def deterministic_triggers(text: str, product: str) -> list[TriggerSpec]:
    """Map keywords onto trigger kinds. Falls back to the default crew."""
    t = text.lower()
    s = get_settings()
    pcts = _pcts(text)
    out: list[TriggerSpec] = []
    price_label = "Price (USD)" if product == "token" else "Price"

    if any(w in t for w in ("price", "move", "swing", "jump", "drop", "rally", "selloff", "%")):
        out.append(
            TriggerSpec(
                "price_move",
                {"label": price_label, "pct": pcts[0] if pcts else s.monitor_price_move_pct},
            )
        )
    if any(w in t for w in ("rsi", "overbought", "oversold", "momentum")):
        out.append(
            TriggerSpec(
                "threshold",
                {
                    "label": "RSI (14-day)",
                    "above": s.monitor_rsi_overbought,
                    "below": s.monitor_rsi_oversold,
                },
            )
        )
    if any(
        w in t for w in ("golden cross", "death cross", "moving average", "ma cross", "crossover")
    ):
        out.append(
            TriggerSpec(
                "ma_cross", {"fast": "50-day moving average", "slow": "200-day moving average"}
            )
        )
    if any(
        w in t for w in ("filing", "earnings", "10-k", "10-q", "10k", "10q", "report", "results")
    ):
        out.append(TriggerSpec("new_filing", {}))

    metric_labels: list[str] = []
    if product == "token":
        if any(w in t for w in ("tvl", "liquidity")):
            metric_labels.append("Liquidity / TVL (USD)")
        if "volume" in t:
            metric_labels.append("Cumulative volume (USD)")
    else:
        if "revenue" in t:
            metric_labels.append("Revenue")
        if "eps" in t or "earnings per share" in t:
            metric_labels.append("Diluted EPS")
        if "net income" in t or "profit" in t:
            metric_labels.append("Net income")
        if "margin" in t:
            metric_labels.append("Net margin")
    if metric_labels:
        out.append(
            TriggerSpec(
                "metric_change",
                {"labels": metric_labels, "pct": pcts[-1] if pcts else s.monitor_metric_change_pct},
            )
        )

    return out or default_triggers(product)


def validate_triggers(raw: list) -> list[TriggerSpec]:
    """Dispose: keep only known kinds, coerce + clamp params. Drops the invalid."""
    clean: list[TriggerSpec] = []
    for item in raw or []:
        kind = item.get("kind") if isinstance(item, dict) else getattr(item, "kind", None)
        params = (
            item.get("params") if isinstance(item, dict) else getattr(item, "params", {})
        ) or {}
        if kind not in EVALUATORS:
            continue
        p: dict = {}
        if kind in ("price_move", "metric_change"):
            if "pct" in params:
                p["pct"] = max(0.1, min(1000.0, float(params["pct"])))
            if params.get("label"):
                p["label"] = str(params["label"])
            if params.get("labels"):
                p["labels"] = [str(x) for x in params["labels"]]
            if params.get("abs") is not None:
                p["abs"] = float(params["abs"])
        elif kind == "threshold":
            if not params.get("label"):
                continue
            p["label"] = str(params["label"])
            if params.get("above") is not None:
                p["above"] = float(params["above"])
            if params.get("below") is not None:
                p["below"] = float(params["below"])
            if "above" not in p and "below" not in p:
                continue
        elif kind == "ma_cross":
            p["fast"] = str(params.get("fast", "50-day moving average"))
            p["slow"] = str(params.get("slow", "200-day moving average"))
        # new_filing carries no params
        clean.append(TriggerSpec(kind, p))
    return clean


_TOOL = {
    "name": "propose_monitor",
    "description": "Propose deterministic watch triggers for a financial monitor.",
    "input_schema": {
        "type": "object",
        "properties": {
            "triggers": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "kind": {"type": "string", "enum": list(EVALUATORS.keys())},
                        "params": {"type": "object"},
                    },
                    "required": ["kind"],
                },
            },
            "interval_seconds": {"type": "integer"},
            "severity_floor": {"type": "string", "enum": ["info", "notable", "critical"]},
        },
        "required": ["triggers"],
    },
}

_SYSTEM = (
    "You translate a plain-English monitoring request into deterministic triggers. "
    "Only use these kinds: price_move{label,pct}, metric_change{labels[],pct}, "
    "threshold{label,above,below}, ma_cross{fast,slow}, new_filing{}. Use real metric "
    "labels jim publishes (e.g. 'Price', 'RSI (14-day)', 'Diluted EPS', 'Net income', "
    "'50-day moving average', 'Liquidity / TVL (USD)'). Call the propose_monitor tool."
)


async def propose_triggers(text: str, product: str) -> tuple[list[TriggerSpec], bool]:
    """Return (validated triggers, used_llm). Falls back to the keyword parser."""
    settings = get_settings()
    if not settings.anthropic_api_key:
        return deterministic_triggers(text, product), False

    try:
        from anthropic import AsyncAnthropic

        client = AsyncAnthropic(api_key=settings.anthropic_api_key)
        resp = await client.messages.create(
            model=settings.research_model,
            max_tokens=600,
            system=[{"type": "text", "text": _SYSTEM, "cache_control": {"type": "ephemeral"}}],
            tools=[_TOOL],
            tool_choice={"type": "tool", "name": "propose_monitor"},
            messages=[{"role": "user", "content": f"Product: {product}.\nRequest: {text}"}],
        )
        block = next((b for b in resp.content if getattr(b, "type", None) == "tool_use"), None)
        triggers = validate_triggers((block.input or {}).get("triggers", []) if block else [])
        return (triggers or deterministic_triggers(text, product)), bool(triggers)
    except Exception:
        # Any LLM/transport failure → deterministic parser (monitors must still work).
        return deterministic_triggers(text, product), False
