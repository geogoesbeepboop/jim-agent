"""Deterministic guard regression cases — jim's non-gate safety rails.

The sourcing gate has its own regression set (:mod:`jim.eval.dataset`). This
module covers the *other* deterministic guards the product's promises rest on,
so a regression in any of them is a named, offline-reproducible eval failure:

  - **impersonal** — published prose stays general: no second person, no advice,
    no recommendations (:mod:`jim.monitors.impersonal`).
  - **identifier** — hostile identifiers are refused at the engine's front door
    before they can reach a URL, query, or store key (:mod:`jim.research.identifiers`).
  - **completeness** — material omissions are detected and scored
    (:mod:`jim.research.completeness`).
  - **materiality** — monitors only speak when a deterministic rule says the
    change matters, respecting severity floors + cooldowns (:mod:`jim.monitors.materiality`).
  - **monitor_nl** — the propose/dispose path: keyword parsing maps intent onto
    known trigger kinds, and validation clamps/drops anything the LLM (or a
    hostile client) proposes outside the registry (:mod:`jim.monitors.nl`).

Every case closes over a ``check`` returning ``(passed, details)`` — no API key,
no network, no store.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Callable

from jim.research.facts import PERCENT, USD, Fact, Snapshot


@dataclass
class GuardCase:
    category: str
    name: str
    check: Callable[[], tuple[bool, dict]]


def _impersonal(name: str, text: str, should_pass: bool) -> GuardCase:
    def check() -> tuple[bool, dict]:
        from jim.monitors.impersonal import check_impersonal

        result = check_impersonal(text)
        return result.passed == should_pass, {
            "text": text,
            "expected_pass": should_pass,
            "got_pass": result.passed,
            "violations": result.violations,
        }

    return GuardCase("impersonal", name, check)


def _identifier(name: str, identifier: str, product: str, expect: str | None) -> GuardCase:
    """``expect`` is the canonical form, or None when refusal is expected."""

    def check() -> tuple[bool, dict]:
        from jim.research.identifiers import canonicalize

        details: dict = {"identifier": identifier, "product": product, "expect": expect}
        try:
            got = canonicalize(identifier, product)
        except ValueError as e:
            details["got"] = f"refused: {e}"
            return expect is None, details
        details["got"] = got
        return got == expect, details

    return GuardCase("identifier", name, check)


def _snapshot(facts: list[tuple[str, str, float, str]]) -> Snapshot:
    return Snapshot(
        ticker="T",
        cik="0",
        entity_name="T",
        facts=[Fact(id=i, label=lb, value=v, unit=u) for i, lb, v, u in facts],
    )


def _completeness(
    name: str,
    facts: list[tuple[str, str, float, str]],
    memo: str,
    *,
    should_pass: bool,
    omitted_material_label: str | None = None,
) -> GuardCase:
    def check() -> tuple[bool, dict]:
        from jim.research.completeness import check_completeness

        result = check_completeness(memo, _snapshot(facts), material_floor=0.6)
        ok = result.passed == should_pass
        omitted_labels = [o["label"] for o in result.material_omissions]
        if omitted_material_label is not None:
            ok = ok and omitted_material_label in omitted_labels
        return ok, {
            "expected_pass": should_pass,
            "got_pass": result.passed,
            "material_coverage": round(result.material_coverage, 4),
            "material_omissions": omitted_labels,
        }

    return GuardCase("completeness", name, check)


def _materiality(name: str, check: Callable[[], tuple[bool, dict]]) -> GuardCase:
    return GuardCase("materiality", name, check)


def _nl(name: str, check: Callable[[], tuple[bool, dict]]) -> GuardCase:
    return GuardCase("monitor_nl", name, check)


# --- materiality checks (fixed clock for determinism) ------------------------

_NOW = datetime(2026, 1, 15, 12, 0, 0, tzinfo=timezone.utc)


def _signal(severity: str, key: str = "threshold:RSI:overbought"):
    from jim.monitors.models import Signal

    return Signal(
        kind="threshold",
        key=key,
        label="RSI (14-day)",
        severity=severity,
        summary="RSI crossed 70 [C4]",
        citation_ids=["C4"],
    )


def _check_floor_filters() -> tuple[bool, dict]:
    from jim.monitors.materiality import assess

    verdict = assess(
        [_signal("info", key="a"), _signal("notable", key="b")],
        severity_floor="notable",
        now=_NOW,
    )
    ok = (
        verdict.material
        and len(verdict.published) == 1
        and verdict.published[0].severity == "notable"
        and len(verdict.suppressed) == 1
    )
    return ok, {
        "published": [s.key for s in verdict.published],
        "suppressed": [s.key for s in verdict.suppressed],
    }


def _check_floor_blocks_all() -> tuple[bool, dict]:
    from jim.monitors.materiality import assess

    verdict = assess([_signal("notable")], severity_floor="critical", now=_NOW)
    return (not verdict.material and not verdict.published), {
        "material": verdict.material,
        "suppressed": [s.key for s in verdict.suppressed],
    }


def _check_cooldown_suppresses() -> tuple[bool, dict]:
    from jim.monitors.materiality import assess

    recent = (_NOW - timedelta(hours=1)).isoformat()
    verdict = assess(
        [_signal("notable")],
        cooldown_seconds=21_600,
        cooldowns={"threshold:RSI:overbought": recent},
        now=_NOW,
    )
    return (not verdict.material), {"suppressed": [s.key for s in verdict.suppressed]}


def _check_cooldown_expired_publishes() -> tuple[bool, dict]:
    from jim.monitors.materiality import assess

    stale = (_NOW - timedelta(hours=7)).isoformat()
    verdict = assess(
        [_signal("notable")],
        cooldown_seconds=21_600,
        cooldowns={"threshold:RSI:overbought": stale},
        now=_NOW,
    )
    return verdict.material, {"published": [s.key for s in verdict.published]}


def _check_unparseable_cooldown_expires() -> tuple[bool, dict]:
    from jim.monitors.materiality import assess

    verdict = assess(
        [_signal("notable")],
        cooldown_seconds=21_600,
        cooldowns={"threshold:RSI:overbought": "not-a-timestamp"},
        now=_NOW,
    )
    return verdict.material, {"published": [s.key for s in verdict.published]}


# --- monitor NL propose/dispose checks ---------------------------------------


def _check_price_pct_extracted() -> tuple[bool, dict]:
    from jim.monitors.nl import deterministic_triggers

    triggers = deterministic_triggers("Watch AAPL for 5% price moves", "fundamentals")
    hit = next((t for t in triggers if t.kind == "price_move"), None)
    ok = hit is not None and hit.params.get("pct") == 5.0
    return ok, {"triggers": [t.to_row() for t in triggers]}


def _check_rsi_keywords() -> tuple[bool, dict]:
    from jim.config import get_settings
    from jim.monitors.nl import deterministic_triggers

    s = get_settings()
    triggers = deterministic_triggers("alert me when AAPL looks overbought", "fundamentals")
    hit = next((t for t in triggers if t.kind == "threshold"), None)
    ok = (
        hit is not None
        and hit.params.get("label") == "RSI (14-day)"
        and hit.params.get("above") == s.monitor_rsi_overbought
    )
    return ok, {"triggers": [t.to_row() for t in triggers]}


def _check_filing_keywords() -> tuple[bool, dict]:
    from jim.monitors.nl import deterministic_triggers

    triggers = deterministic_triggers("tell me when the next 10-K drops", "fundamentals")
    ok = any(t.kind == "new_filing" for t in triggers)
    return ok, {"triggers": [t.to_row() for t in triggers]}


def _check_revenue_metric_keywords() -> tuple[bool, dict]:
    from jim.monitors.nl import deterministic_triggers

    triggers = deterministic_triggers("watch revenue changes of 10%", "fundamentals")
    hit = next((t for t in triggers if t.kind == "metric_change"), None)
    ok = hit is not None and "Revenue" in (hit.params.get("labels") or [])
    return ok, {"triggers": [t.to_row() for t in triggers]}


def _check_default_crew_fallback() -> tuple[bool, dict]:
    from jim.monitors.nl import deterministic_triggers
    from jim.monitors.triggers import EVALUATORS

    triggers = deterministic_triggers("keep an eye on things", "fundamentals")
    ok = bool(triggers) and all(t.kind in EVALUATORS for t in triggers)
    return ok, {"triggers": [t.to_row() for t in triggers]}


def _check_unknown_kind_dropped() -> tuple[bool, dict]:
    from jim.monitors.nl import validate_triggers

    kept = validate_triggers([{"kind": "run_shell_command", "params": {"cmd": "rm -rf /"}}])
    return kept == [], {"kept": [t.to_row() for t in kept]}


def _check_pct_clamped() -> tuple[bool, dict]:
    from jim.monitors.nl import validate_triggers

    kept = validate_triggers([{"kind": "price_move", "params": {"pct": 99_999}}])
    ok = len(kept) == 1 and kept[0].params["pct"] == 1000.0
    return ok, {"kept": [t.to_row() for t in kept]}


def _check_threshold_needs_bounds() -> tuple[bool, dict]:
    from jim.monitors.nl import validate_triggers

    kept = validate_triggers([{"kind": "threshold", "params": {"label": "RSI (14-day)"}}])
    return kept == [], {"kept": [t.to_row() for t in kept]}


def _check_interval_parsing() -> tuple[bool, dict]:
    from jim.monitors.nl import parse_interval

    got = {
        "every 30 minutes": parse_interval("check every 30 minutes"),
        "daily": parse_interval("give me a daily digest"),
        "no cadence": parse_interval("watch this closely"),
    }
    ok = got["every 30 minutes"] == 1_800 and got["daily"] == 86_400 and got["no cadence"] is None
    return ok, got


# --- the assembled guard suite ------------------------------------------------

_MATERIAL_FACTS = [
    ("C1", "Revenue", 394_328_000_000.0, USD),
    ("C2", "Net income", 99_800_000_000.0, USD),
    ("C3", "Net margin", 23.77, PERCENT),
]

GUARD_CASES: list[GuardCase] = [
    # impersonal guard
    _impersonal(
        "neutral_metric_prose_passes",
        "VALUATION\nP/E: 29.4x [C3]\nNET: margins stable, leverage unchanged.",
        True,
    ),
    _impersonal(
        "buyback_word_not_flagged",
        "The buyback program continued through the quarter [C8].",
        True,
    ),
    _impersonal("second_person_blocked", "If you hold shares, watch the margin trend.", False),
    _impersonal("we_recommend_blocked", "We recommend a cautious stance here.", False),
    _impersonal("should_buy_blocked", "Investors should buy before earnings.", False),
    _impersonal("strong_buy_rating_blocked", "Consensus remains a strong buy.", False),
    _impersonal("price_target_blocked", "The price target is unchanged at $250.", False),
    _impersonal("your_portfolio_blocked", "Trim your portfolio exposure accordingly.", False),
    # identifier canonicalization
    _identifier("lowercase_ticker_uppercased", "aapl", "fundamentals", "AAPL"),
    _identifier("class_share_ticker_ok", "BRK.B", "fundamentals", "BRK.B"),
    _identifier("hyphen_ticker_uppercased", "rds-a", "fundamentals", "RDS-A"),
    _identifier("token_chain_suffix_lowercased", "WETH:Base", "token", "WETH:base"),
    _identifier(
        "token_address_ok",
        "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
        "token",
        "0x833589fCD6eDb6E08f4c7C32D4f71b54bdA02913",
    ),
    _identifier("macro_region_uppercased", "us", "macro", "US"),
    _identifier("empty_refused", "   ", "fundamentals", None),
    _identifier("path_traversal_refused", "../../etc/passwd", "fundamentals", None),
    _identifier("url_refused", "http://evil.example/x", "fundamentals", None),
    _identifier("query_smuggling_refused", "AAPL?x=1", "fundamentals", None),
    _identifier("control_bytes_refused", "AAPL\x00", "fundamentals", None),
    _identifier("interior_space_refused", "AAPL MSFT", "fundamentals", None),
    _identifier("overlong_refused", "A" * 81, "fundamentals", None),
    _identifier("oversized_ticker_refused", "ABCDEFGHIJK", "fundamentals", None),
    # completeness
    _completeness(
        "all_material_cited_passes",
        _MATERIAL_FACTS,
        "Revenue was $394.3 billion [C1]; net income $99.8 billion [C2]; margin 23.8% [C3].",
        should_pass=True,
    ),
    _completeness(
        "material_omission_flagged",
        _MATERIAL_FACTS,
        "Revenue was $394.3 billion [C1].",
        should_pass=False,
        omitted_material_label="Net income",
    ),
    _completeness(
        "immaterial_omission_tolerated",
        [("C1", "Revenue", 394_328_000_000.0, USD), ("C2", "R&D expense", 3.0e10, USD)],
        "Revenue was $394.3 billion [C1].",
        should_pass=True,
    ),
    _completeness(
        "no_material_facts_vacuous_pass",
        [("C1", "R&D expense", 3.0e10, USD)],
        "Research spend held steady.",
        should_pass=True,
    ),
    # monitor materiality
    _materiality("severity_floor_filters_info", _check_floor_filters),
    _materiality("severity_floor_blocks_below_critical", _check_floor_blocks_all),
    _materiality("cooldown_suppresses_repeat_signal", _check_cooldown_suppresses),
    _materiality("cooldown_expired_publishes", _check_cooldown_expired_publishes),
    _materiality("unparseable_cooldown_treated_expired", _check_unparseable_cooldown_expires),
    # monitor NL propose/dispose
    _nl("price_move_pct_extracted", _check_price_pct_extracted),
    _nl("rsi_keywords_map_to_threshold", _check_rsi_keywords),
    _nl("filing_keywords_map_to_new_filing", _check_filing_keywords),
    _nl("revenue_keywords_map_to_metric_change", _check_revenue_metric_keywords),
    _nl("vague_request_falls_back_to_default_crew", _check_default_crew_fallback),
    _nl("unknown_trigger_kind_dropped", _check_unknown_kind_dropped),
    _nl("hostile_pct_clamped", _check_pct_clamped),
    _nl("threshold_without_bounds_dropped", _check_threshold_needs_bounds),
    _nl("interval_words_parse", _check_interval_parsing),
]
