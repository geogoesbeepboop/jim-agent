"""Deterministic A2A input parsing — text grammar + JSON DataPart. Offline.

The parser is the front door for spend-bearing intent, so every test here pins a
deterministic behaviour: happy paths for both wire dialects, text⇆JSON
equivalence, a stable refusal for each malformed shape, and — the load-bearing
one — that no happy-path parse ever reaches the LLM monitor path.
"""

from __future__ import annotations

import pytest

from jim.a2a.inputs import (
    InputRejected,
    ParsedMonitor,
    ParsedResearch,
    parse_data,
    parse_message_parts,
    parse_text,
)
from jim.monitors.models import TriggerSpec

# --- happy paths: research --------------------------------------------------


@pytest.mark.parametrize("product", ["fundamentals", "token", "macro"])
def test_research_text_all_products(product):
    r = parse_text(f"research {product} AAPL")
    assert isinstance(r, ParsedResearch)
    assert r.kind == "research"
    assert r.product == product
    assert r.identifier == "AAPL"
    assert r.mode == "agent"  # A2A default


def test_research_text_mode_human():
    r = parse_text("research fundamentals AAPL mode=human")
    assert r.mode == "human"


@pytest.mark.parametrize("product", ["fundamentals", "token", "macro"])
def test_research_json_all_products(product):
    r = parse_data({"kind": "research", "product": product, "identifier": "AAPL"})
    assert isinstance(r, ParsedResearch)
    assert r.product == product
    assert r.mode == "agent"


def test_research_json_mode_agent_explicit():
    r = parse_data(
        {"kind": "research", "product": "token", "identifier": "WETH:base", "mode": "agent"}
    )
    assert r.identifier == "WETH:base"
    assert r.mode == "agent"


# --- happy paths: monitor ---------------------------------------------------


def test_monitor_text_minimal():
    m = parse_text("monitor fundamentals AAPL every=1d watch=price:5,ma")
    assert isinstance(m, ParsedMonitor)
    assert m.kind == "monitor"
    assert m.product == "fundamentals"
    assert m.identifier == "AAPL"
    assert m.mode == "agent"
    assert m.interval_seconds == 86_400
    assert m.watch == ["price:5", "ma"]
    assert [t.kind for t in m.triggers] == ["price_move", "ma_cross"]
    assert m.severity_floor == "info"
    assert m.cooldown_seconds == 21_600  # Monitor default


def test_monitor_text_fully_specified():
    m = parse_text(
        "monitor token WETH:base every=6h watch=price:5,ma "
        "mode=human severity_floor=notable cooldown=12h"
    )
    assert m.product == "token"
    assert m.identifier == "WETH:base"
    assert m.mode == "human"
    assert m.interval_seconds == 6 * 3_600
    assert m.severity_floor == "notable"
    assert m.cooldown_seconds == 12 * 3_600
    assert all(isinstance(t, TriggerSpec) for t in m.triggers)


def test_monitor_json_minimal():
    m = parse_data(
        {"kind": "monitor", "product": "fundamentals", "identifier": "AAPL",
         "every": "1d", "watch": ["price:5", "ma"]}
    )
    assert m.interval_seconds == 86_400
    assert m.severity_floor == "info"
    assert m.cooldown_seconds == 21_600
    assert [t.kind for t in m.triggers] == ["price_move", "ma_cross"]


def test_monitor_json_fully_specified():
    m = parse_data(
        {"kind": "monitor", "product": "token", "identifier": "WETH:base", "mode": "agent",
         "every": "6h", "watch": ["price:5", "ma"], "severity_floor": "notable", "cooldown": "12h"}
    )
    assert m.mode == "agent"
    assert m.interval_seconds == 6 * 3_600
    assert m.cooldown_seconds == 12 * 3_600


# --- text ⇆ JSON equivalence -------------------------------------------------


def test_research_text_json_equivalent():
    text = parse_text("research fundamentals AAPL mode=human")
    data = parse_data(
        {"kind": "research", "product": "fundamentals", "identifier": "AAPL", "mode": "human"}
    )
    assert text == data


def test_monitor_text_json_equivalent():
    text = parse_text(
        "monitor token WETH:base every=6h watch=price:5,ma "
        "mode=agent severity_floor=notable cooldown=12h"
    )
    data = parse_data(
        {"kind": "monitor", "product": "token", "identifier": "WETH:base", "mode": "agent",
         "every": "6h", "watch": ["price:5", "ma"], "severity_floor": "notable", "cooldown": "12h"}
    )
    assert text == data


def test_monitor_minimal_text_json_equivalent():
    text = parse_text("monitor fundamentals AAPL every=1d watch=price:5,ma")
    data = parse_data(
        {"kind": "monitor", "product": "fundamentals", "identifier": "AAPL",
         "every": "1d", "watch": ["price:5", "ma"]}
    )
    assert text == data


# --- case handling ----------------------------------------------------------


def test_keywords_case_insensitive_identifier_preserved():
    r = parse_text("RESEARCH FUNDAMENTALS aapl")
    assert r.product == "fundamentals"
    assert r.identifier == "aapl"  # case-preserved


def test_mode_keyword_case_insensitive():
    r = parse_text("research fundamentals AAPL Mode=Agent")
    assert r.mode == "agent"


# --- rejections: text prose / structure -------------------------------------


def _assert_rejected(fn):
    with pytest.raises(InputRejected) as exc:
        fn()
    err = exc.value
    assert err.code == "invalid_input"
    assert isinstance(err.message, str) and err.message
    assert isinstance(err.grammar, str) and err.grammar
    return err


def test_reject_prose():
    err = _assert_rejected(lambda: parse_text("please research apple for me"))
    assert err.message == "unknown command 'please': expected 'research' or 'monitor'"


def test_reject_deterministic_message():
    # Same input → same message, twice.
    a = _assert_rejected(lambda: parse_text("please research apple for me"))
    b = _assert_rejected(lambda: parse_text("please research apple for me"))
    assert a.message == b.message


def test_reject_unknown_product():
    err = _assert_rejected(lambda: parse_text("research equities AAPL"))
    assert "unknown research product" in err.message


def test_reject_macro_monitor():
    err = _assert_rejected(lambda: parse_text("monitor macro US every=1d watch=price:5"))
    assert "unknown monitor product" in err.message


def test_reject_missing_every():
    _assert_rejected(lambda: parse_text("monitor fundamentals AAPL watch=price:5"))


def test_reject_missing_watch():
    _assert_rejected(lambda: parse_text("monitor fundamentals AAPL every=1d"))


def test_reject_empty_watch_text():
    _assert_rejected(lambda: parse_text("monitor fundamentals AAPL every=1d watch="))


def test_reject_empty_watch_json():
    _assert_rejected(
        lambda: parse_data(
            {"kind": "monitor", "product": "fundamentals", "identifier": "AAPL",
             "every": "1d", "watch": []}
        )
    )


def test_reject_unparseable_watch_spec():
    err = _assert_rejected(
        lambda: parse_text("monitor token WETH every=1d watch=bogus:xyz")
    )
    assert "unrecognized watch spec" in err.message


def test_reject_malformed_watch_spec_valueerror():
    # parse_watch_spec raises ValueError on a bad float — surfaces as rejection, not crash.
    _assert_rejected(lambda: parse_text("monitor fundamentals AAPL every=1d watch=price:abc"))


def test_reject_unparseable_duration():
    err = _assert_rejected(
        lambda: parse_text("monitor fundamentals AAPL every=soon watch=price:5")
    )
    assert "unparseable every duration" in err.message


def test_reject_duplicate_key():
    err = _assert_rejected(lambda: parse_text("research fundamentals AAPL mode=human mode=agent"))
    assert "duplicate key 'mode'" in err.message


def test_reject_unknown_key():
    err = _assert_rejected(lambda: parse_text("research fundamentals AAPL foo=bar"))
    assert "unknown key 'foo'" in err.message


def test_reject_leftover_tokens():
    err = _assert_rejected(lambda: parse_text("research fundamentals AAPL extra"))
    assert "unexpected token 'extra'" in err.message


def test_reject_identifier_too_long():
    long_id = "A" * 65
    _assert_rejected(lambda: parse_text(f"research fundamentals {long_id}"))


def test_reject_identifier_bad_chars():
    _assert_rejected(
        lambda: parse_data({"kind": "research", "product": "fundamentals", "identifier": "AA*PL"})
    )


def test_reject_missing_positionals():
    _assert_rejected(lambda: parse_text("research"))
    _assert_rejected(lambda: parse_text("monitor fundamentals"))


# --- rejections: message-part envelope --------------------------------------


def test_reject_zero_parts():
    err = _assert_rejected(lambda: parse_message_parts([]))
    assert "got 0" in err.message


def test_reject_two_parts():
    err = _assert_rejected(
        lambda: parse_message_parts(
            [{"kind": "text", "text": "research fundamentals AAPL"},
             {"kind": "text", "text": "research token WETH"}]
        )
    )
    assert "got 2" in err.message


def test_reject_file_part():
    err = _assert_rejected(
        lambda: parse_message_parts([{"kind": "file", "file": {"uri": "http://x"}}])
    )
    assert "file parts are not accepted" in err.message


def test_reject_unknown_part_kind():
    _assert_rejected(lambda: parse_message_parts([{"kind": "audio", "audio": {}}]))


def test_message_parts_dispatch_text_and_data():
    r = parse_message_parts([{"kind": "text", "text": "research fundamentals AAPL"}])
    assert isinstance(r, ParsedResearch)
    d = parse_message_parts(
        [{"kind": "data", "data": {"kind": "research", "product": "token", "identifier": "WETH"}}]
    )
    assert isinstance(d, ParsedResearch)
    assert d.product == "token"


def test_message_parts_threads_interval_floor():
    err = _assert_rejected(
        lambda: parse_message_parts(
            [{"kind": "text", "text": "monitor fundamentals AAPL every=5m watch=price:5"}],
            monitor_min_interval_seconds=1_800,
        )
    )
    assert "below the minimum 1800s" in err.message


# --- rejections: JSON DataPart strictness -----------------------------------


def test_reject_json_extra_field():
    _assert_rejected(
        lambda: parse_data(
            {"kind": "research", "product": "fundamentals", "identifier": "AAPL", "foo": "bar"}
        )
    )


def test_reject_json_wrong_types():
    # watch must be a list, not a string; identifier must be a string.
    _assert_rejected(
        lambda: parse_data(
            {"kind": "monitor", "product": "token", "identifier": "WETH",
             "every": "1d", "watch": "price:5"}
        )
    )
    _assert_rejected(
        lambda: parse_data({"kind": "research", "product": "fundamentals", "identifier": 123})
    )


def test_reject_json_unknown_kind():
    err = _assert_rejected(lambda: parse_data({"kind": "trade", "product": "fundamentals"}))
    assert "unknown data-part kind" in err.message


def test_reject_json_bad_enum():
    _assert_rejected(
        lambda: parse_data({"kind": "research", "product": "options", "identifier": "AAPL"})
    )


# --- interval floor ----------------------------------------------------------


def test_below_interval_floor_text():
    err = _assert_rejected(
        lambda: parse_text(
            "monitor fundamentals AAPL every=5m watch=price:5",
            monitor_min_interval_seconds=1_800,
        )
    )
    assert "below the minimum 1800s" in err.message


def test_below_interval_floor_json():
    _assert_rejected(
        lambda: parse_data(
            {"kind": "monitor", "product": "fundamentals", "identifier": "AAPL",
             "every": "5m", "watch": ["price:5"]},
            monitor_min_interval_seconds=1_800,
        )
    )


def test_interval_floor_none_skips_check():
    # No floor passed → a tiny interval is accepted (the caller owns the floor).
    m = parse_text("monitor fundamentals AAPL every=5m watch=price:5")
    assert m.interval_seconds == 300


def test_interval_at_floor_accepted():
    m = parse_text(
        "monitor fundamentals AAPL every=30m watch=price:5",
        monitor_min_interval_seconds=1_800,
    )
    assert m.interval_seconds == 1_800


# --- the LLM-unreachable pin -------------------------------------------------


def test_happy_paths_never_touch_the_llm(monkeypatch):
    """Spend-bearing intent must never reach an LLM. If any happy-path parse
    calls the NL monitor proposer, this fails loudly."""

    def _boom(*_a, **_k):
        raise AssertionError("propose_triggers reached from the A2A input parser")

    monkeypatch.setattr("jim.monitors.nl.propose_triggers", _boom)

    # Every happy-path shape, both dialects.
    parse_text("research fundamentals AAPL")
    parse_text("research token WETH:base mode=human")
    parse_text("research macro US")
    parse_text("monitor fundamentals AAPL every=1d watch=price:5,ma")
    parse_text(
        "monitor token WETH:base every=6h watch=price:5,ma,rsi "
        "mode=human severity_floor=critical cooldown=12h"
    )
    parse_data({"kind": "research", "product": "fundamentals", "identifier": "AAPL"})
    parse_data(
        {"kind": "monitor", "product": "token", "identifier": "WETH:base",
         "every": "6h", "watch": ["price:5", "ma", "filing"]}
    )
    parse_message_parts([{"kind": "text", "text": "monitor fundamentals AAPL every=1d watch=ma"}])
