"""The upgraded faithfulness judge — per-claim checklist + Sonnet high-stakes.

Offline: the real Anthropic client is mocked, so we assert the contract (model
selection, claim parsing, threshold, fail-closed on junk) without a network call.
"""

from __future__ import annotations

import json


from jim.config import Settings
from jim.research import judge as judge_mod
from jim.research.facts import USD, Fact, Snapshot
from jim.research.judge import JudgeResult, _parse_claims, judge_faithfulness


def _snap() -> Snapshot:
    return Snapshot(
        ticker="ACME", cik="0", entity_name="Acme",
        facts=[Fact(id="C1", label="Revenue", value=100.0, unit=USD)],
    )


class _FakeUsage:
    input_tokens = 10
    output_tokens = 20


class _FakeBlock:
    type = "text"

    def __init__(self, text: str):
        self.text = text


class _FakeResp:
    def __init__(self, text: str, stop_reason: str = "end_turn"):
        self.content = [_FakeBlock(text)]
        self.usage = _FakeUsage()
        self.stop_reason = stop_reason


def _fake_client(capture: dict, payload: str, stop_reason: str = "end_turn"):
    class _Messages:
        async def create(self, *, model, **kwargs):
            capture["model"] = model
            capture["max_tokens"] = kwargs.get("max_tokens")
            return _FakeResp(payload, stop_reason)

    class _Client:
        def __init__(self, *a, **k):
            self.messages = _Messages()

    return _Client


def test_parse_claims_tolerant() -> None:
    raw = [
        {"claim": "Revenue grew", "supported": True, "citation": "C1", "reason": "matches"},
        {"claim": "vibes", "supported": False},  # missing fields
        "junk",  # non-dict ignored
    ]
    claims = _parse_claims(raw)
    assert len(claims) == 2
    assert claims[0].supported and claims[0].citation == "C1"
    assert claims[1].supported is False and claims[1].citation is None


def test_skips_without_key(monkeypatch) -> None:
    monkeypatch.setattr(judge_mod, "get_settings", lambda: Settings(anthropic_api_key=None))
    import asyncio

    res = asyncio.run(judge_faithfulness("memo", _snap()))
    assert res.skipped and res.passed and res.score == 1.0


async def test_parses_checklist_and_selects_model(monkeypatch) -> None:
    payload = json.dumps(
        {
            "score": 0.9,
            "supported": True,
            "claims": [
                {"claim": "Revenue $100", "supported": True, "citation": "C1", "reason": "ok"},
                {"claim": "moon soon", "supported": False, "citation": None, "reason": "no basis"},
            ],
            "issues": ["moon soon"],
        }
    )
    capture: dict = {}
    monkeypatch.setattr(judge_mod, "AsyncAnthropic", _fake_client(capture, payload))
    monkeypatch.setattr(
        judge_mod,
        "get_settings",
        lambda: Settings(
            anthropic_api_key="sk-test",
            judge_model="claude-haiku-4-5-20251001",
            judge_high_stakes_model="claude-sonnet-4-6",
            judge_threshold=0.8,
        ),
    )

    normal = await judge_faithfulness("memo", _snap())
    assert capture["model"] == "claude-haiku-4-5-20251001"
    assert normal.passed and normal.score == 0.9
    assert len(normal.claims) == 2
    assert [c.claim for c in normal.unsupported_claims] == ["moon soon"]

    await judge_faithfulness("memo", _snap(), high_stakes=True)
    assert capture["model"] == "claude-sonnet-4-6"  # upgraded model


async def test_unparseable_fails_closed(monkeypatch) -> None:
    capture: dict = {}
    monkeypatch.setattr(judge_mod, "AsyncAnthropic", _fake_client(capture, "not json at all"))
    monkeypatch.setattr(
        judge_mod, "get_settings", lambda: Settings(anthropic_api_key="sk-test")
    )
    res = await judge_faithfulness("memo", _snap())
    assert res.passed is False and res.score == 0.0
    assert "unparseable" in res.issues[0]


async def test_truncated_output_fails_closed_and_salvages(monkeypatch) -> None:
    # A checklist guillotined mid-array: the outer JSON won't parse, but the two
    # complete claim objects before the cut are recoverable. This is the exact
    # failure that rejected every live memo when max_tokens was 900.
    truncated = (
        '{"score": 0.9, "supported": true, "claims": ['
        '{"claim": "Revenue $100", "supported": true, "citation": "C1", "reason": "ok"},'
        '{"claim": "Cash $45", "supported": true, "citation": "C1", "reason": "ok"},'
        '{"claim": "Debt $30", "supported": tr'  # <- cut off here
    )
    capture: dict = {}
    monkeypatch.setattr(
        judge_mod, "AsyncAnthropic", _fake_client(capture, truncated, stop_reason="max_tokens")
    )
    monkeypatch.setattr(
        judge_mod, "get_settings", lambda: Settings(anthropic_api_key="sk-test")
    )
    res = await judge_faithfulness("memo", _snap())
    assert res.passed is False and res.score == 0.0  # never pass on partial evidence
    assert "truncated" in res.issues[0] and "max_tokens" in res.issues[0]
    assert [c.claim for c in res.claims] == ["Revenue $100", "Cash $45"]  # salvaged


async def test_uses_configured_max_tokens(monkeypatch) -> None:
    payload = json.dumps({"score": 1.0, "supported": True, "claims": [], "issues": []})
    capture: dict = {}
    monkeypatch.setattr(judge_mod, "AsyncAnthropic", _fake_client(capture, payload))
    monkeypatch.setattr(
        judge_mod,
        "get_settings",
        lambda: Settings(anthropic_api_key="sk-test", judge_max_tokens=4096),
    )
    await judge_faithfulness("memo", _snap())
    assert capture["max_tokens"] == 4096


def test_judge_result_skip_shape() -> None:
    s = JudgeResult.skip()
    assert s.skipped and s.passed and s.claims == []
