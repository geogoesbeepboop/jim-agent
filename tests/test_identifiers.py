"""Identifier canonicalization — the allowlist that keeps hostile input out of
URL construction. Deterministic and offline; the engine-level test proves a
rejected identifier never reaches the graph (a poisoned graph would assert)."""

from __future__ import annotations

import pytest

from jim.research import engine
from jim.research.identifiers import canonicalize

_ADDR = "0x" + "AbCd12" * 6 + "3456"  # 40 hex chars, mixed case
assert len(_ADDR) == 42


@pytest.mark.parametrize(
    ("raw", "product", "expected"),
    [
        ("AAPL", "fundamentals", "AAPL"),
        (" brk.b ", "fundamentals", "BRK.B"),  # stripped + uppercased
        ("RDS-A", "fundamentals", "RDS-A"),
        ("WETH", "token", "WETH"),
        ("WETH:base", "token", "WETH:base"),
        ("WETH:Base", "token", "WETH:base"),  # chain suffix is lowercased
        (_ADDR, "token", _ADDR),  # address case preserved (resolve() lowercases)
        (f"{_ADDR}:arbitrum", "token", f"{_ADDR}:arbitrum"),
        ("us", "macro", "US"),
        ("peer.signal-1:v2", "someday-product", "peer.signal-1:v2"),  # generic rule
    ],
)
def test_canonicalize_accepts_and_normalizes(raw, product, expected):
    assert canonicalize(raw, product) == expected


@pytest.mark.parametrize(
    "hostile",
    [
        "../../../etc",  # path traversal into EDGAR-style URL paths
        "AAPL/extra",  # path segment smuggling
        "A B",  # interior whitespace
        "http://evil",  # SSRF: a full URL is never an identifier
        "A" * 100,  # over the global length cap
        "",  # empty
        "   ",  # whitespace-only → empty after strip
        "TICKER?x=1",  # query-string smuggling
        "AAPL\x00",  # null byte
    ],
)
@pytest.mark.parametrize("product", ["fundamentals", "token", "macro", "someday-product"])
def test_canonicalize_rejects_hostile_input(hostile, product):
    with pytest.raises(ValueError):
        canonicalize(hostile, product)


async def test_engine_refuses_hostile_identifier_before_any_fetch(monkeypatch):
    """run_research must reject at the boundary — the graph (and therefore every
    source fetch) is never invoked for an identifier that fails canonicalization."""

    class _PoisonedGraph:
        async def ainvoke(self, state):
            raise AssertionError("graph ran for a hostile identifier")

    monkeypatch.setattr(engine, "_GRAPH", _PoisonedGraph())

    result = await engine.run_research("../../../etc/passwd", product="fundamentals")

    assert result.status == "error"
    assert "not a valid ticker" in (result.error or "")
    assert result.ticker == "../../../etc/passwd"
    assert result.memo is None and result.snapshot is None
