"""Deterministic input parsing for the A2A surface — the spend-bearing front door.

An A2A ``Message`` reaches jim carrying **exactly one** part: either a
``DataPart`` (structured JSON) or a ``TextPart`` (a strict command grammar).
Whatever it carries becomes an instruction to spend money — commission a paid
research task or stand up a paid monitor — so this module is the one place that
turns wire bytes into a typed intent, and it does so **without ever consulting a
model**. The existing natural-language monitor path
(:func:`jim.monitors.nl.propose_triggers`, an Anthropic call) is deliberately
*unreachable* from here: fuzzy prose does not get "best-effort" interpreted into a
purchase, it gets refused.

The contract is intentionally unforgiving, because ambiguity in front of a
paywall is a liability, not a convenience:

  - exactly one part; zero, two-or-more, ``file``, or unknown-kind parts refuse;
  - the text grammar is positional-then-keyed, every token must be consumed, and
    duplicate or unknown ``key=value`` tokens refuse;
  - the JSON form mirrors the grammar exactly (``extra="forbid"``, strict enums);
  - both forms produce the *same* :class:`ParsedResearch` / :class:`ParsedMonitor`
    for an equivalent request, so the wire dialect a caller picks never changes
    the meaning;
  - every rejection is deterministic — the same input always yields the same
    ``.message`` — so callers can log and clients can retry against a stable
    contract.

Identifier checks here are purely *structural* (shape and length). The real
security gate stays downstream in :func:`jim.research.identifiers.canonicalize`,
which every executor calls before any source fetch; duplicating it here would
only risk the two drifting apart.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import ClassVar, Literal

from pydantic import BaseModel, ConfigDict, ValidationError

from jim.monitors.create import parse_watch_spec
from jim.monitors.models import TriggerSpec
from jim.monitors.nl import duration_seconds

__all__ = [
    "ParsedResearch",
    "ParsedMonitor",
    "InputRejected",
    "parse_message_parts",
    "parse_text",
    "parse_data",
]

# --- vocabulary -------------------------------------------------------------

_RESEARCH_PRODUCTS = ("fundamentals", "token", "macro")
_MONITOR_PRODUCTS = ("fundamentals", "token")  # matches the monitor API's own regex
_MODES = ("human", "agent")
_SEVERITIES = ("info", "notable", "critical")

_DEFAULT_MODE = "agent"  # A2A is an agent-facing surface
_DEFAULT_SEVERITY = "info"
_DEFAULT_COOLDOWN_SECONDS = 21_600  # mirrors the Monitor dataclass default

# Structural identifier shape only (canonicalize() is the real gate downstream).
_IDENTIFIER = re.compile(r"[A-Za-z0-9.:\-]{1,64}")

# One-line grammar hints carried on every rejection so a client can self-correct.
_RESEARCH_GRAMMAR = "research <fundamentals|token|macro> <identifier> [mode=human|agent]"
_MONITOR_GRAMMAR = (
    "monitor <fundamentals|token> <identifier> every=<duration> watch=<spec,spec,...> "
    "[mode=human|agent] [severity_floor=info|notable|critical] [cooldown=<duration>]"
)
_TOP_GRAMMAR = f"{_RESEARCH_GRAMMAR}  |  {_MONITOR_GRAMMAR}"


# --- results ----------------------------------------------------------------


@dataclass(frozen=True)
class ParsedResearch:
    """A validated one-shot research intent (product + identifier + mode)."""

    product: str  # fundamentals | token | macro
    identifier: str  # case-preserved; canonicalized downstream
    mode: str = _DEFAULT_MODE  # human | agent

    kind: ClassVar[str] = "research"


@dataclass(frozen=True)
class ParsedMonitor:
    """A validated monitor intent: a scheduled, trigger-driven paid directive.

    Field order puts the required fields first so the frozen dataclass'
    default-ordering rule holds; construct with keywords (the parser always does).
    """

    product: str  # fundamentals | token
    identifier: str  # case-preserved; canonicalized downstream
    interval_seconds: int
    watch: list[str]  # the raw specs, validated
    triggers: list[TriggerSpec]  # parsed via monitors.create.parse_watch_spec
    mode: str = _DEFAULT_MODE  # human | agent
    severity_floor: str = _DEFAULT_SEVERITY
    cooldown_seconds: int = _DEFAULT_COOLDOWN_SECONDS

    kind: ClassVar[str] = "monitor"


class InputRejected(Exception):
    """Deterministic refusal of an A2A input.

    Carries a machine ``code``, a human-readable ``message`` (stable for a given
    input), and the one-line ``grammar`` hint for the form that failed.
    """

    def __init__(self, code: str, message: str, grammar: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.grammar = grammar


def _reject(message: str, grammar: str) -> InputRejected:
    """Every refusal shares one code; the message carries the specifics."""
    return InputRejected("invalid_input", message, grammar)


# --- JSON DataPart models (mirror the grammar, strict) ----------------------


class _ResearchData(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["research"]
    product: Literal["fundamentals", "token", "macro"]
    identifier: str
    mode: Literal["human", "agent"] = _DEFAULT_MODE


class _MonitorData(BaseModel):
    model_config = ConfigDict(extra="forbid")

    kind: Literal["monitor"]
    product: Literal["fundamentals", "token"]
    identifier: str
    mode: Literal["human", "agent"] = _DEFAULT_MODE
    every: str  # duration string, parsed by duration_seconds
    watch: list[str]  # spec strings, validated by parse_watch_spec
    severity_floor: Literal["info", "notable", "critical"] = _DEFAULT_SEVERITY
    cooldown: str | None = None  # duration string; None → default cooldown


def _fmt_validation(exc: ValidationError) -> str:
    """A stable one-line message from a pydantic error (uses the stable ``type``)."""
    err = exc.errors()[0]
    loc = ".".join(str(x) for x in err.get("loc", ())) or "<root>"
    return f"data part rejected at {loc}: {err.get('type', 'invalid')}"


# --- shared field validators ------------------------------------------------


def _validate_identifier(identifier: object, grammar: str) -> str:
    if not isinstance(identifier, str) or not _IDENTIFIER.fullmatch(identifier):
        raise _reject(
            f"invalid identifier {identifier!r}: expected 1-64 chars of A-Za-z0-9.:-",
            grammar,
        )
    return identifier


def _parse_mode(value: str | None, grammar: str) -> str:
    if value is None:
        return _DEFAULT_MODE
    mode = value.lower()  # keyword value, case-insensitive
    if mode not in _MODES:
        raise _reject(f"invalid mode {value!r}: expected human or agent", grammar)
    return mode


def _parse_severity(value: str | None, grammar: str) -> str:
    if value is None:
        return _DEFAULT_SEVERITY
    sev = value.lower()
    if sev not in _SEVERITIES:
        raise _reject(
            f"invalid severity_floor {value!r}: expected info, notable or critical", grammar
        )
    return sev


def _parse_duration(value: str, grammar: str, field_name: str) -> int:
    try:
        return duration_seconds(value)
    except ValueError as exc:
        raise _reject(
            f"unparseable {field_name} duration {value!r} (try 30m, 1h, 1d)", grammar
        ) from exc


# --- shared builders (both wire dialects converge here) ---------------------


def _build_research(product: str, identifier: str, mode: str, grammar: str) -> ParsedResearch:
    return ParsedResearch(
        product=product,
        identifier=_validate_identifier(identifier, grammar),
        mode=mode,
    )


def _build_monitor(
    *,
    product: str,
    identifier: str,
    mode: str,
    every: str,
    watch: list[str],
    severity_floor: str,
    cooldown: str | None,
    min_interval: int | None,
    grammar: str,
) -> ParsedMonitor:
    identifier = _validate_identifier(identifier, grammar)
    interval_seconds = _parse_duration(every, grammar, "every")

    if min_interval is not None and interval_seconds < min_interval:
        raise _reject(
            f"interval {interval_seconds}s is below the minimum {min_interval}s",
            grammar,
        )

    if not watch:
        raise _reject("watch list is empty; at least one spec is required", grammar)

    triggers: list[TriggerSpec] = []
    for spec in watch:
        if not spec:
            raise _reject("empty watch spec (check for a stray comma)", grammar)
        try:
            trigger = parse_watch_spec(spec, product)
        except ValueError:
            trigger = None  # malformed thresholds surface as "unrecognized" too
        if trigger is None:
            raise _reject(f"unrecognized watch spec {spec!r}", grammar)
        triggers.append(trigger)

    cooldown_seconds = (
        _DEFAULT_COOLDOWN_SECONDS if cooldown is None else _parse_duration(cooldown, grammar, "cooldown")
    )

    return ParsedMonitor(
        product=product,
        identifier=identifier,
        interval_seconds=interval_seconds,
        watch=list(watch),
        triggers=triggers,
        mode=mode,
        severity_floor=severity_floor,
        cooldown_seconds=cooldown_seconds,
    )


# --- text grammar -----------------------------------------------------------


def _split_keyvals(tokens: list[str], grammar: str) -> dict[str, str]:
    """Consume the trailing ``key=value`` tokens; every token must be one, once.

    Keys are lower-cased (keywords are case-insensitive); values are left as-is
    so identifiers and watch specs keep their case.
    """
    kv: dict[str, str] = {}
    for tok in tokens:
        if "=" not in tok:
            raise _reject(f"unexpected token {tok!r}: expected key=value", grammar)
        key, _, value = tok.partition("=")
        key = key.lower()
        if key in kv:
            raise _reject(f"duplicate key {key!r}", grammar)
        kv[key] = value
    return kv


def _reject_unknown_keys(kv: dict[str, str], allowed: set[str], grammar: str) -> None:
    for key in kv:  # insertion order == token order → deterministic
        if key not in allowed:
            raise _reject(f"unknown key {key!r}", grammar)


def _parse_research_tokens(tokens: list[str]) -> ParsedResearch:
    if len(tokens) < 2:
        raise _reject("research requires <product> <identifier>", _RESEARCH_GRAMMAR)
    product = tokens[0].lower()
    if product not in _RESEARCH_PRODUCTS:
        raise _reject(
            f"unknown research product {tokens[0]!r}: expected fundamentals, token or macro",
            _RESEARCH_GRAMMAR,
        )
    identifier = tokens[1]  # case-preserved
    kv = _split_keyvals(tokens[2:], _RESEARCH_GRAMMAR)
    _reject_unknown_keys(kv, {"mode"}, _RESEARCH_GRAMMAR)
    mode = _parse_mode(kv.get("mode"), _RESEARCH_GRAMMAR)
    return _build_research(product, identifier, mode, _RESEARCH_GRAMMAR)


def _parse_monitor_tokens(tokens: list[str], min_interval: int | None) -> ParsedMonitor:
    if len(tokens) < 2:
        raise _reject(
            "monitor requires <product> <identifier> every=<duration> watch=<spec,...>",
            _MONITOR_GRAMMAR,
        )
    product = tokens[0].lower()
    if product not in _MONITOR_PRODUCTS:
        raise _reject(
            f"unknown monitor product {tokens[0]!r}: expected fundamentals or token",
            _MONITOR_GRAMMAR,
        )
    identifier = tokens[1]  # case-preserved
    kv = _split_keyvals(tokens[2:], _MONITOR_GRAMMAR)
    _reject_unknown_keys(
        kv, {"every", "watch", "mode", "severity_floor", "cooldown"}, _MONITOR_GRAMMAR
    )
    if "every" not in kv:
        raise _reject("monitor requires every=<duration>", _MONITOR_GRAMMAR)
    if "watch" not in kv:
        raise _reject("monitor requires watch=<spec,...>", _MONITOR_GRAMMAR)
    return _build_monitor(
        product=product,
        identifier=identifier,
        mode=_parse_mode(kv.get("mode"), _MONITOR_GRAMMAR),
        every=kv["every"],
        watch=kv["watch"].split(","),
        severity_floor=_parse_severity(kv.get("severity_floor"), _MONITOR_GRAMMAR),
        cooldown=kv.get("cooldown"),
        min_interval=min_interval,
        grammar=_MONITOR_GRAMMAR,
    )


# --- public API -------------------------------------------------------------


def parse_text(
    text: str, *, monitor_min_interval_seconds: int | None = None
) -> ParsedResearch | ParsedMonitor:
    """Parse a ``TextPart`` command. Keywords are case-insensitive; prose refuses."""
    if not isinstance(text, str):
        raise _reject("text part is not a string", _TOP_GRAMMAR)
    tokens = text.split()  # whitespace split, empties dropped
    if not tokens:
        raise _reject("empty command", _TOP_GRAMMAR)
    command = tokens[0].lower()
    if command == "research":
        return _parse_research_tokens(tokens[1:])
    if command == "monitor":
        return _parse_monitor_tokens(tokens[1:], monitor_min_interval_seconds)
    raise _reject(
        f"unknown command {tokens[0]!r}: expected 'research' or 'monitor'", _TOP_GRAMMAR
    )


def parse_data(
    data: dict, *, monitor_min_interval_seconds: int | None = None
) -> ParsedResearch | ParsedMonitor:
    """Parse a ``DataPart`` payload. ``kind`` discriminates; unknown fields refuse."""
    if not isinstance(data, dict):
        raise _reject("data part must be a JSON object", _TOP_GRAMMAR)
    kind = data.get("kind")
    if kind == "research":
        try:
            model = _ResearchData.model_validate(data)
        except ValidationError as exc:
            raise _reject(_fmt_validation(exc), _RESEARCH_GRAMMAR) from exc
        return _build_research(model.product, model.identifier, model.mode, _RESEARCH_GRAMMAR)
    if kind == "monitor":
        try:
            model = _MonitorData.model_validate(data)
        except ValidationError as exc:
            raise _reject(_fmt_validation(exc), _MONITOR_GRAMMAR) from exc
        return _build_monitor(
            product=model.product,
            identifier=model.identifier,
            mode=model.mode,
            every=model.every,
            watch=model.watch,
            severity_floor=model.severity_floor,
            cooldown=model.cooldown,
            min_interval=monitor_min_interval_seconds,
            grammar=_MONITOR_GRAMMAR,
        )
    raise _reject(
        f"unknown data-part kind {kind!r}: expected 'research' or 'monitor'", _TOP_GRAMMAR
    )


def parse_message_parts(
    parts: list[dict], *, monitor_min_interval_seconds: int | None = None
) -> ParsedResearch | ParsedMonitor:
    """Parse the v0.3-wire parts of an A2A message: exactly one text or data part.

    ``parts`` are the wire dicts (``{"kind": "text", "text": ...}`` or
    ``{"kind": "data", "data": {...}}``). Zero, two-or-more, ``file``, or
    unknown-kind parts refuse — a spend-bearing message must carry one
    unambiguous intent.
    """
    if not isinstance(parts, list):
        raise _reject("message parts must be a list", _TOP_GRAMMAR)
    if len(parts) != 1:
        raise _reject(f"expected exactly one message part, got {len(parts)}", _TOP_GRAMMAR)

    part = parts[0]
    if not isinstance(part, dict):
        raise _reject("message part must be an object", _TOP_GRAMMAR)
    kind = part.get("kind")
    if kind == "text":
        text = part.get("text")
        if not isinstance(text, str):
            raise _reject("text part is missing its 'text' string", _TOP_GRAMMAR)
        return parse_text(text, monitor_min_interval_seconds=monitor_min_interval_seconds)
    if kind == "data":
        payload = part.get("data")
        if not isinstance(payload, dict):
            raise _reject("data part is missing its 'data' object", _TOP_GRAMMAR)
        return parse_data(payload, monitor_min_interval_seconds=monitor_min_interval_seconds)
    if kind == "file":
        raise _reject("file parts are not accepted; send a text or data part", _TOP_GRAMMAR)
    raise _reject(f"unsupported part kind {kind!r}: expected 'text' or 'data'", _TOP_GRAMMAR)
