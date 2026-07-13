"""S4 — the research task state machine over ``message/stream`` + ``tasks/resubscribe``.

The blocking flow is covered in ``test_a2a_research_flow``; here we assert the
*streaming* surface a real A2A client watches: the coarse event sequence of a full
paid run, that a mid-run ``tasks/resubscribe`` still receives the completion
events, and that a rejected run's stream never emits an artifact or leaks the memo.

Driven with ``httpx.AsyncClient(transport=ASGITransport(...))`` — the sync
``TestClient`` deadlocks on SSE — against a bare app carrying the real executor +
a scripted ``FakeRail`` (same recipe as ``test_a2a_research_flow``). Offline: no
DB, wallet, network, or key.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass, field

import httpx
import pytest

from jim.a2a import build_a2a_components, mount_a2a
from jim.a2a.extension import (
    EXT_HEADER,
    MD_PAYMENT_PAYLOAD,
    MD_PAYMENT_STATUS,
    PaymentStatus,
    X402_EXT_URI,
)
from jim.a2a.payments import RailResult
from jim.config import Settings
from jim.research import engine
from jim.research.engine import ResearchResult
from jim.research.gate import GateResult, Violation
from jim.research.judge import JudgeResult
from fastapi import FastAPI
from jim.store import get_store, reset_store

JSONRPC = "/a2a/jsonrpc"
EXT = {EXT_HEADER: X402_EXT_URI}
MEMO_SENTINEL = "MEMO-LEAK-CANARY-STREAM"


@pytest.fixture(autouse=True)
def _reset():
    reset_store()
    yield
    reset_store()


@dataclass
class FakeRail:
    calls: list = field(default_factory=list)
    verify_result: RailResult = field(default_factory=lambda: RailResult(ok=True, payer="0xPAYER"))
    settle_result: RailResult = field(
        default_factory=lambda: RailResult(
            ok=True, payer="0xPAYER", tx_hash="0xTX", amount="225000",
            network="eip155:84532", raw={"success": True, "transaction": "0xTX"},
        )
    )

    async def build_requirements(self, *, price_usd, resource, description) -> dict:
        self.calls.append("build")
        return {
            "scheme": "exact", "network": "eip155:84532", "asset": "0xUSDC",
            "amount": str(int(round(price_usd * 1_000_000))),
            "payTo": "0x000000000000000000000000000000000000JIMM",
            "maxTimeoutSeconds": 900, "extra": {"name": "USDC", "version": "2"},
        }

    async def verify(self, *, payload, requirements) -> RailResult:
        self.calls.append("verify")
        return self.verify_result

    async def settle(self, *, payload, requirements) -> RailResult:
        await asyncio.sleep(0)
        self.calls.append("settle")
        return self.settle_result

    def n(self, method: str) -> int:
        return sum(1 for m in self.calls if m == method)


def ok_result() -> ResearchResult:
    return ResearchResult(
        ticker="AAPL", mode="agent", status="ok", product="fundamentals",
        entity_name="Apple Inc.", as_of="2025-01-01",
        memo=f"Revenue was $100 [C1]. {MEMO_SENTINEL}",
        snapshot=None, gate=GateResult(passed=True, n_figures=1, n_covered=1),
        judge=JudgeResult.skip(), attempts=1, cost={"price_out_usd": 0.225},
    )


def rejected_result() -> ResearchResult:
    return ResearchResult(
        ticker="AAPL", mode="agent", status="rejected", product="fundamentals",
        memo=f"Revenue was $999 [C1]. {MEMO_SENTINEL}",
        gate=GateResult(passed=False, violations=[Violation(figure="$999", reason="value mismatch", segment="s")], n_figures=1, n_covered=0),
        judge=JudgeResult(skipped=False, passed=False, score=0.4, issues=["overreach-x"]), attempts=2,
    )


def build(rail: FakeRail) -> FastAPI:
    settings = Settings(
        _env_file=None, a2a_encryption_key=None, evm_private_key=None,
        evm_address="0x000000000000000000000000000000000000JIMM",
    )
    app = FastAPI()
    mount_a2a(app, build_a2a_components(settings, rail=rail))
    return app


def client(rail: FakeRail) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=build(rail)), base_url="http://t")


def _rpc(method: str, params: dict, rid: int = 1) -> dict:
    return {"jsonrpc": "2.0", "id": rid, "method": method, "params": params}


def _research_send(text: str = "research fundamentals AAPL", rid: int = 1) -> dict:
    return _rpc(
        "message/send",
        {"message": {"messageId": f"m{rid}", "role": "user", "parts": [{"kind": "text", "text": text}]},
         "configuration": {"blocking": True}},
        rid,
    )


def _pay_stream(task_id: str, context_id: str, payload: dict, rid: int = 2) -> dict:
    return _rpc(
        "message/stream",
        {"message": {
            "messageId": f"m{rid}", "role": "user", "taskId": task_id, "contextId": context_id,
            "parts": [{"kind": "text", "text": "pay"}],
            "metadata": {MD_PAYMENT_PAYLOAD: payload},
        }},
        rid,
    )


async def _quote(cx: httpx.AsyncClient) -> tuple[str, str]:
    r = await cx.post(JSONRPC, json=_research_send(), headers=EXT)
    res = r.json()["result"]
    assert res["status"]["state"] == "input-required"
    return res["id"], res["contextId"]


async def _collect_stream(cx: httpx.AsyncClient, body: dict, headers=None) -> list[dict]:
    """Collect the ``result`` payloads of every SSE event of one JSON-RPC stream."""
    results: list[dict] = []
    async with cx.stream("POST", JSONRPC, json=body, headers=headers or {}) as r:
        assert r.status_code == 200, r.text
        assert "text/event-stream" in r.headers.get("content-type", "")
        async for line in r.aiter_lines():
            if line.startswith("data:"):
                event = json.loads(line[len("data:"):].strip())
                if "result" in event:
                    results.append(event["result"])
    return results


def _states(results: list[dict]) -> list[str]:
    return [e["status"]["state"] for e in results if e.get("kind") == "status-update"]


# --- 1. streaming the full paid flow: coarse event sequence ------------------


async def test_stream_full_paid_flow(monkeypatch):
    async def _ok(*a, **k):
        return ok_result()

    monkeypatch.setattr(engine, "run_research", _ok)
    rail = FakeRail()
    async with client(rail) as cx:
        task_id, context_id = await _quote(cx)
        results = await _collect_stream(
            cx, _pay_stream(task_id, context_id, {"sig": "0xS"}), headers=EXT
        )

    kinds = [e["kind"] for e in results]
    states = _states(results)
    # working (payment verified) → working (gates passed) → artifact-update → completed.
    assert "artifact-update" in kinds
    assert states[0] == "working"
    assert states[-1] == "completed"
    assert "working" in states and states.count("working") >= 2
    # The verified → completed payment status is observable in the stream metadata.
    verified = [e for e in results if (e.get("metadata") or {}).get(MD_PAYMENT_STATUS) == PaymentStatus.VERIFIED.value]
    completed = [e for e in results if (e.get("metadata") or {}).get(MD_PAYMENT_STATUS) == PaymentStatus.COMPLETED.value]
    assert verified and completed
    # The paid artifact rides the stream: a DataPart + a TextPart carrying the memo.
    art = next(e for e in results if e.get("kind") == "artifact-update")
    part_kinds = [p["kind"] for p in art["artifact"]["parts"]]
    assert part_kinds == ["data", "text"]
    assert rail.n("settle") == 1
    assert get_store().a2a_auths[task_id]["status"] == "settled"


# --- 2. tasks/resubscribe mid-run still receives the completion events -------


async def test_resubscribe_receives_subsequent_events(monkeypatch):
    started, release = asyncio.Event(), asyncio.Event()

    async def slow_engine(identifier, **kwargs):
        started.set()
        await release.wait()
        return ok_result()

    monkeypatch.setattr(engine, "run_research", slow_engine)
    rail = FakeRail()
    async with client(rail) as cx:
        task_id, context_id = await _quote(cx)

        # Start the paid run as a stream; it will block inside the engine.
        run_stream = asyncio.create_task(
            _collect_stream(cx, _pay_stream(task_id, context_id, {"sig": "0xS"}), headers=EXT)
        )
        await asyncio.wait_for(started.wait(), timeout=5)  # task is "working", paused

        # Resubscribe attaches AFTER work started; it must still see completion.
        async def _resub() -> list[dict]:
            return await _collect_stream(cx, _rpc("tasks/resubscribe", {"id": task_id}, rid=9))

        resub = asyncio.create_task(_resub())
        await asyncio.sleep(0.1)  # let the resubscribe attach
        release.set()

        resub_results = await asyncio.wait_for(resub, timeout=5)
        await asyncio.wait_for(run_stream, timeout=5)

    resub_states = _states(resub_results)
    assert "completed" in resub_states  # the late subscriber still saw the end
    assert rail.n("settle") == 1
    assert get_store().a2a_auths[task_id]["status"] == "settled"


# --- 3. a rejected run streams to `rejected` with no artifact and no leak ----


async def test_stream_rejected_no_artifact_no_leak(monkeypatch):
    async def _rej(*a, **k):
        return rejected_result()

    monkeypatch.setattr(engine, "run_research", _rej)
    rail = FakeRail()
    async with client(rail) as cx:
        task_id, context_id = await _quote(cx)
        results = await _collect_stream(
            cx, _pay_stream(task_id, context_id, {"sig": "0xS"}), headers=EXT
        )

    kinds = [e["kind"] for e in results]
    states = _states(results)
    assert states[-1] == "rejected"
    assert "artifact-update" not in kinds  # nothing published
    assert rail.n("settle") == 0  # never billed
    # No memo / violation / judge-issue text anywhere in the streamed events.
    blob = json.dumps(results)
    assert MEMO_SENTINEL not in blob
    assert "value mismatch" not in blob
    assert "overreach-x" not in blob
