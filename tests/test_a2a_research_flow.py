"""S4 — the real research task state machine, driven end to end over the wire.

Every case drives the **mounted** A2A surface (a bare FastAPI + the real
:class:`~jim.a2a.executor.JimAgentExecutor`, built via
``build_a2a_components(settings, rail=FakeRail())`` and ``mount_a2a`` — the same
recipe the seller uses, minus the paywall/middlewares that are already covered by
``test_a2a_mount``). Composition choice: a bare app + injected ``rail=`` is the
cleanest way to exercise the real executor with a scripted rail; the seller's own
``build_app`` builds a production ``X402Rail`` we could not gate for the cancel
races. Everything is in-process over ``httpx.ASGITransport`` (async so the cancel
races can drive concurrent in-flight requests) — no DB, wallet, network, or key.

The engine is scripted by monkeypatching ``jim.research.engine.run_research`` with
canned :class:`ResearchResult` objects, so these tests isolate the A2A layer (the
engine's own gate/billing invariants live in ``test_a2a_payments`` /
``test_engine``). ``FakeRail`` mirrors ``test_a2a_payments``'s scripted rail.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
from dataclasses import dataclass, field

import httpx
import pytest
from fastapi import FastAPI

from jim.a2a import build_a2a_components, mount_a2a
from jim.a2a import executor as executor_mod
from jim.a2a.extension import (
    EXT_HEADER,
    MD_PAYMENT_PAYLOAD,
    MD_PAYMENT_RECEIPTS,
    MD_PAYMENT_REQUIRED,
    MD_PAYMENT_STATUS,
    PaymentStatus,
    X402_EXT_URI,
)
from jim.a2a.payments import RailResult
from jim.config import Settings
from jim.marketplace.pricing import price_for
from jim.research import engine
from jim.research.engine import ResearchResult
from jim.research.gate import GateResult, Violation
from jim.research.judge import JudgeResult
from jim.store import get_store, reset_store

JSONRPC = "/a2a/jsonrpc"
EXT = {EXT_HEADER: X402_EXT_URI}
# A distinctive string planted in scripted memos; it must NEVER appear on any
# unpaid (rejected/refused) surface. Deliberately not a bare number so it cannot
# collide with a UUID/timestamp substring.
MEMO_SENTINEL = "MEMO-LEAK-CANARY-ZZZ"


@pytest.fixture(autouse=True)
def _reset():
    reset_store()
    yield
    reset_store()


# --- scripted rail (mirrors tests/test_a2a_payments.py) ----------------------


@dataclass
class FakeRail:
    """Records build/verify/settle; verify/settle outcomes scriptable, settle gateable."""

    calls: list = field(default_factory=list)
    verify_result: RailResult = field(default_factory=lambda: RailResult(ok=True, payer="0xPAYER"))
    settle_result: RailResult = field(
        default_factory=lambda: RailResult(
            ok=True,
            payer="0xPAYER",
            tx_hash="0xTX",
            amount="225000",
            network="eip155:84532",
            raw={"success": True, "transaction": "0xTX", "payer": "0xPAYER"},
        )
    )
    settle_entered: asyncio.Event | None = None  # set when settle begins
    settle_gate: asyncio.Event | None = None  # settle blocks until set

    async def build_requirements(self, *, price_usd, resource, description) -> dict:
        self.calls.append(("build", price_usd))
        return {
            "scheme": "exact",
            "network": "eip155:84532",
            "asset": "0xUSDC",
            "amount": str(int(round(price_usd * 1_000_000))),
            "payTo": "0x000000000000000000000000000000000000JIMM",
            "maxTimeoutSeconds": 900,
            "extra": {"name": "USDC", "version": "2"},
        }

    async def verify(self, *, payload, requirements) -> RailResult:
        self.calls.append(("verify", None))
        return self.verify_result

    async def settle(self, *, payload, requirements) -> RailResult:
        if self.settle_entered is not None:
            self.settle_entered.set()
        if self.settle_gate is not None:
            await self.settle_gate.wait()
        await asyncio.sleep(0)
        self.calls.append(("settle", None))
        return self.settle_result

    def n(self, method: str) -> int:
        return sum(1 for m, _ in self.calls if m == method)


# --- canned engine results ---------------------------------------------------


def ok_result(memo: str = f"Revenue was $100 [C1]. {MEMO_SENTINEL}") -> ResearchResult:
    return ResearchResult(
        ticker="AAPL",
        mode="agent",
        status="ok",
        product="fundamentals",
        entity_name="Apple Inc.",
        as_of="2025-01-01",
        memo=memo,
        snapshot=None,
        gate=GateResult(passed=True, n_figures=1, n_covered=1),
        judge=JudgeResult.skip(),
        attempts=1,
        cost={"price_out_usd": 0.225, "margin_usd": 0.2},
    )


def rejected_result() -> ResearchResult:
    return ResearchResult(
        ticker="AAPL",
        mode="agent",
        status="rejected",
        product="fundamentals",
        memo=f"Revenue was $999 [C1]. {MEMO_SENTINEL}",
        gate=GateResult(
            passed=False,
            violations=[Violation(figure="$999", reason="value mismatch", segment="seg")],
            n_figures=1,
            n_covered=0,
        ),
        judge=JudgeResult(skipped=False, passed=False, score=0.4, issues=["overreach-claim-detail"]),
        attempts=2,
    )


def error_result() -> ResearchResult:
    return ResearchResult(
        ticker="AAPL", mode="agent", status="error", product="fundamentals", error="edgar unreachable"
    )


def script_engine(monkeypatch, result_factory):
    async def _run(identifier, **kwargs):
        return result_factory()

    monkeypatch.setattr(engine, "run_research", _run)


# --- app + wire helpers ------------------------------------------------------


def build(rail: FakeRail) -> FastAPI:
    settings = Settings(
        _env_file=None,
        a2a_encryption_key=None,
        evm_private_key=None,
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
        {
            "message": {"messageId": f"m{rid}", "role": "user", "parts": [{"kind": "text", "text": text}]},
            "configuration": {"blocking": True},
        },
        rid,
    )


def _pay_send(task_id: str, context_id: str, payload: dict, rid: int = 2) -> dict:
    return _rpc(
        "message/send",
        {
            "message": {
                "messageId": f"m{rid}",
                "role": "user",
                "taskId": task_id,
                "contextId": context_id,
                "parts": [{"kind": "text", "text": "here is my payment"}],
                "metadata": {MD_PAYMENT_PAYLOAD: payload},
            },
            "configuration": {"blocking": True},
        },
        rid,
    )


async def _quote(cx: httpx.AsyncClient, text: str = "research fundamentals AAPL") -> dict:
    r = await cx.post(JSONRPC, json=_research_send(text), headers=EXT)
    assert r.status_code == 200, r.text
    return r.json()["result"]


async def _get(cx: httpx.AsyncClient, task_id: str, rid: int = 90) -> dict:
    r = await cx.post(JSONRPC, json=_rpc("tasks/get", {"id": task_id}, rid=rid))
    return r.json()["result"]


# --- 1. full happy path ------------------------------------------------------


async def test_happy_path_quote_pay_complete(monkeypatch):
    script_engine(monkeypatch, ok_result)
    rail = FakeRail()
    async with client(rail) as cx:
        quoted = await _quote(cx)
        assert quoted["status"]["state"] == "input-required"
        meta = quoted["metadata"]
        assert meta[MD_PAYMENT_STATUS] == PaymentStatus.REQUIRED.value
        # Price = the fundamentals AGENT tier (A2A default mode), not the headline.
        expected_atomic = str(int(round(price_for("fundamentals", "agent") * 1_000_000)))
        assert meta[MD_PAYMENT_REQUIRED]["amount"] == expected_atomic

        task_id, context_id = quoted["id"], quoted["contextId"]
        r = await cx.post(
            JSONRPC, json=_pay_send(task_id, context_id, {"scheme": "exact", "sig": "0xS"}), headers=EXT
        )
        done = r.json()["result"]

    assert done["status"]["state"] == "completed"
    assert done["metadata"][MD_PAYMENT_STATUS] == PaymentStatus.COMPLETED.value
    assert done["metadata"][MD_PAYMENT_RECEIPTS]  # receipts present
    # Artifact carries a DataPart (response JSON) + a TextPart (memo).
    parts = done["artifacts"][0]["parts"]
    kinds = [p["kind"] for p in parts]
    assert kinds == ["data", "text"]
    assert parts[0]["data"]["ticker"] == "AAPL"
    assert MEMO_SENTINEL in parts[1]["text"]
    # Money moved exactly once; the auth is settled; a receipt was recorded.
    assert rail.n("settle") == 1
    assert get_store().a2a_auths[task_id]["status"] == "settled"
    assert len(get_store().receipts) == 1


# --- 2. gate-rejected: never settled, no memo leak ---------------------------


async def test_gate_rejected_refuses_never_bills_no_leak(monkeypatch):
    script_engine(monkeypatch, rejected_result)
    rail = FakeRail()
    async with client(rail) as cx:
        quoted = await _quote(cx)
        task_id, context_id = quoted["id"], quoted["contextId"]
        r = await cx.post(
            JSONRPC, json=_pay_send(task_id, context_id, {"sig": "0xS"}), headers=EXT
        )
        rejected = r.json()["result"]
        full = await _get(cx, task_id)

    assert rejected["status"]["state"] == "rejected"
    assert rail.n("settle") == 0  # never-bill-rejected (ADR-0008)
    assert get_store().a2a_auths[task_id]["status"] == "discarded"
    assert get_store().receipts == []
    # Diagnostics counts are present...
    diag = json.loads(rejected["status"]["message"]["parts"][0]["text"])
    assert diag["billed"] is False
    assert diag["sourcing"]["figures_covered"] == 0
    assert diag["sourcing"]["violations"] == 1  # a COUNT, not the figure text
    # ...but the memo, the violating figure, and the judge issue text never leak,
    # anywhere in the full task history / status / metadata.
    blob = json.dumps(full)
    assert MEMO_SENTINEL not in blob
    assert "value mismatch" not in blob
    assert "overreach-claim-detail" not in blob


# --- 3. engine error → failed, no settle -------------------------------------


async def test_engine_error_fails_without_billing(monkeypatch):
    script_engine(monkeypatch, error_result)
    rail = FakeRail()
    async with client(rail) as cx:
        quoted = await _quote(cx)
        task_id, context_id = quoted["id"], quoted["contextId"]
        r = await cx.post(JSONRPC, json=_pay_send(task_id, context_id, {"sig": "0xS"}), headers=EXT)
        failed = r.json()["result"]

    assert failed["status"]["state"] == "failed"
    assert rail.n("settle") == 0
    assert get_store().a2a_auths[task_id]["status"] == "discarded"
    assert get_store().receipts == []


# --- 4. bad payment → stays input-required, retry succeeds -------------------


async def test_bad_payment_retryable_then_succeeds(monkeypatch):
    script_engine(monkeypatch, ok_result)
    rail = FakeRail(verify_result=RailResult(ok=False, error="signature invalid"))
    async with client(rail) as cx:
        quoted = await _quote(cx)
        task_id, context_id = quoted["id"], quoted["contextId"]

        bad = (
            await cx.post(JSONRPC, json=_pay_send(task_id, context_id, {"sig": "0xBAD"}), headers=EXT)
        ).json()["result"]
        assert bad["status"]["state"] == "input-required"  # retryable, still paused
        assert bad["metadata"][MD_PAYMENT_STATUS] == PaymentStatus.REJECTED.value
        assert get_store().a2a_auths[task_id]["status"] == "required"

        # A corrected payment verifies (the auth was never consumed).
        rail.verify_result = RailResult(ok=True, payer="0xPAYER")
        good = (
            await cx.post(
                JSONRPC, json=_pay_send(task_id, context_id, {"sig": "0xGOOD"}, rid=3), headers=EXT
            )
        ).json()["result"]

    assert good["status"]["state"] == "completed"
    assert rail.n("settle") == 1
    assert get_store().a2a_auths[task_id]["status"] == "settled"


# --- 5. expired quote → payment-failed, settle never called ------------------


async def test_expired_quote_fails_without_settle(monkeypatch):
    from datetime import timedelta

    from jim.a2a.payments import _utcnow

    script_engine(monkeypatch, ok_result)
    rail = FakeRail()
    async with client(rail) as cx:
        quoted = await _quote(cx)
        task_id, context_id = quoted["id"], quoted["contextId"]
        # The payment window lapses before the client pays.
        get_store().a2a_auths[task_id]["expires_at"] = _utcnow() - timedelta(seconds=1)

        failed = (
            await cx.post(JSONRPC, json=_pay_send(task_id, context_id, {"sig": "0xS"}), headers=EXT)
        ).json()["result"]

    assert failed["status"]["state"] == "failed"
    assert failed["metadata"][MD_PAYMENT_STATUS] == PaymentStatus.FAILED.value
    assert rail.n("settle") == 0
    assert rail.n("verify") == 0  # expiry is caught before the rail is touched


# --- 6. duplicate continuation after settled → idempotent (no second settle) --


async def test_duplicate_continuation_after_settled_is_idempotent(monkeypatch):
    script_engine(monkeypatch, ok_result)
    rail = FakeRail()
    async with client(rail) as cx:
        quoted = await _quote(cx)
        task_id, context_id = quoted["id"], quoted["contextId"]
        first = (
            await cx.post(JSONRPC, json=_pay_send(task_id, context_id, {"sig": "0xS"}), headers=EXT)
        ).json()["result"]
        assert first["status"]["state"] == "completed"

        # A second payment for the now-terminal task must not re-settle.
        second = await cx.post(
            JSONRPC, json=_pay_send(task_id, context_id, {"sig": "0xS"}, rid=5), headers=EXT
        )
        body = second.json()
        after = await _get(cx, task_id)

    assert rail.n("settle") == 1  # money still moved exactly once
    assert after["status"]["state"] == "completed"
    # The SDK refuses a send to a terminal task (sane, deterministic response).
    assert "error" in body or body.get("result", {}).get("status", {}).get("state") == "completed"


# --- 7. settle failure after staging → failed, no artifact, no receipt -------


async def test_settle_failure_after_staging_publishes_nothing(monkeypatch):
    script_engine(monkeypatch, ok_result)
    rail = FakeRail(settle_result=RailResult(ok=False, error="insufficient funds"))
    async with client(rail) as cx:
        quoted = await _quote(cx)
        task_id, context_id = quoted["id"], quoted["contextId"]
        failed = (
            await cx.post(JSONRPC, json=_pay_send(task_id, context_id, {"sig": "0xS"}), headers=EXT)
        ).json()["result"]

    assert failed["status"]["state"] == "failed"
    assert failed.get("artifacts", []) == []  # nothing published
    assert get_store().receipts == []  # no money → no receipt
    assert task_id not in get_store().withheld  # staged artifact discarded
    assert get_store().a2a_auths[task_id]["status"] == "settle_failed"


# --- 8. extension not activated → rejected, no quote persisted ---------------


async def test_extension_not_activated_rejects_without_quoting(monkeypatch):
    script_engine(monkeypatch, ok_result)
    rail = FakeRail()
    async with client(rail) as cx:
        # No A2A-Extensions header → the gate refuses before any quote.
        r = await cx.post(JSONRPC, json=_research_send())
        rejected = r.json()["result"]

    assert rejected["status"]["state"] == "rejected"
    assert "extension" in rejected["status"]["message"]["parts"][0]["text"].lower()
    assert rail.n("build") == 0  # never priced
    assert get_store().a2a_auths == {}  # no auth persisted


# --- 9. prose refuses with grammar; monitor refuses as not-yet-enabled -------


async def test_prose_and_monitor_inputs_refuse(monkeypatch):
    script_engine(monkeypatch, ok_result)
    rail = FakeRail()
    async with client(rail) as cx:
        prose = (
            await cx.post(JSONRPC, json=_research_send("please tell me about apple stock"), headers=EXT)
        ).json()["result"]
        assert prose["status"]["state"] == "rejected"
        diag = json.loads(prose["status"]["message"]["parts"][0]["text"])
        assert "grammar" in diag  # structured grammar hint

        monitor = (
            await cx.post(
                JSONRPC,
                json=_research_send("monitor fundamentals AAPL every=1d watch=price:5", rid=2),
                headers=EXT,
            )
        ).json()["result"]
        assert monitor["status"]["state"] == "rejected"
        assert "not yet enabled" in monitor["status"]["message"]["parts"][0]["text"].lower()

    assert rail.n("build") == 0  # neither priced


# --- 10a. cancel pre-payment → canceled, auth discarded ----------------------


async def test_cancel_pre_payment(monkeypatch):
    script_engine(monkeypatch, ok_result)
    rail = FakeRail()
    async with client(rail) as cx:
        quoted = await _quote(cx)
        task_id = quoted["id"]
        assert get_store().a2a_auths[task_id]["status"] == "required"
        cancelled = (
            await cx.post(JSONRPC, json=_rpc("tasks/cancel", {"id": task_id}, rid=7))
        ).json()["result"]

    assert cancelled["status"]["state"] == "canceled"
    assert get_store().a2a_auths[task_id]["status"] == "discarded"
    assert rail.n("settle") == 0


# --- 10b. cancel during the engine run → canceled, engine stopped, no settle -


async def test_cancel_during_run(monkeypatch):
    started, release = asyncio.Event(), asyncio.Event()

    async def slow_engine(identifier, **kwargs):
        started.set()
        await release.wait()
        return ok_result()

    monkeypatch.setattr(engine, "run_research", slow_engine)
    rail = FakeRail()
    async with client(rail) as cx:
        quoted = await _quote(cx)
        task_id, context_id = quoted["id"], quoted["contextId"]
        send = asyncio.create_task(
            cx.post(JSONRPC, json=_pay_send(task_id, context_id, {"sig": "0xS"}), headers=EXT)
        )
        await asyncio.wait_for(started.wait(), timeout=5)  # engine is running

        cancelled = (
            await cx.post(JSONRPC, json=_rpc("tasks/cancel", {"id": task_id}, rid=7))
        ).json()["result"]
        assert cancelled["status"]["state"] == "canceled"

        release.set()  # unblock the (now-cancelled) engine coroutine
        # The in-flight send resolves to the canceled task (or errors); either way
        # the task's true terminal state is what matters.
        with contextlib.suppress(Exception):
            await asyncio.wait_for(send, timeout=5)
        final = await _get(cx, task_id)

    assert final["status"]["state"] == "canceled"
    assert rail.n("settle") == 0  # never settled
    assert get_store().a2a_auths[task_id]["status"] == "discarded"
    assert task_id not in executor_mod._RUNNING  # engine task deregistered


# --- 10c. cancel loses the race → settlement completes, task settled ---------


async def test_cancel_race_lost_lets_settlement_complete(monkeypatch):
    script_engine(monkeypatch, ok_result)
    entered, gate = asyncio.Event(), asyncio.Event()
    rail = FakeRail(settle_entered=entered, settle_gate=gate)
    async with client(rail) as cx:
        quoted = await _quote(cx)
        task_id, context_id = quoted["id"], quoted["contextId"]
        send = asyncio.create_task(
            cx.post(JSONRPC, json=_pay_send(task_id, context_id, {"sig": "0xS"}), headers=EXT)
        )
        await asyncio.wait_for(entered.wait(), timeout=5)  # settlement is in flight
        assert get_store().a2a_auths[task_id]["status"] == "settling"

        # Fire the cancel while settlement blocks; the SDK's cancel awaits the
        # task's terminal state, so we release the settlement to let it resolve.
        cancel = asyncio.create_task(cx.post(JSONRPC, json=_rpc("tasks/cancel", {"id": task_id}, rid=7)))
        await asyncio.sleep(0.1)  # let executor.cancel observe auth='settling'
        gate.set()
        cancel_resp = (await asyncio.wait_for(cancel, timeout=5)).json()
        done = (await asyncio.wait_for(send, timeout=5)).json()["result"]

    # Money committed while cancel was in flight → the settlement wins.
    assert done["status"]["state"] == "completed"
    assert rail.n("settle") == 1
    assert get_store().a2a_auths[task_id]["status"] == "settled"
    assert len(get_store().receipts) == 1
    # The cancel could not cancel it: it surfaces the (now-terminal) task, not a
    # spurious cancellation — the never-abort-a-committed-settlement guarantee.
    cancel_state = cancel_resp.get("result", {}).get("status", {}).get("state")
    assert cancel_state in ("completed", None)  # completed task, or an error result


# --- 11. restart-staging proof: the pieces S7's recovery sweep will use -------


async def test_settled_but_unpublished_leaves_recoverable_pieces(monkeypatch):
    """Cheap crash simulation: an artifact staged + auth marked settled but never
    released is exactly what S7's sweep must finish. Assert the recoverable pieces
    exist (do NOT build the sweep here)."""
    from jim.a2a.crypto import A2ACrypto
    from jim.a2a.stores import PaymentAuths, WithheldArtifacts

    settings = Settings(
        _env_file=None, a2a_encryption_key=None, evm_private_key=None, evm_address="0xJIM"
    )
    store = get_store()
    crypto = A2ACrypto(settings)
    withheld = WithheldArtifacts(store, crypto)
    auths = PaymentAuths(store, crypto)

    task_id = "crash-task"
    await auths.create_required(
        task_id=task_id, kind="research", product="fundamentals", identifier="AAPL",
        mode="agent", amount_usd=0.225, requirements={"amount": "225000"},
    )
    payload = {"response": {"ticker": "AAPL"}, "memo": MEMO_SENTINEL}
    await withheld.hold(
        task_id=task_id, monitor_id="", severity="", as_of="2025-01-01",
        price_usd=0.225, payload=payload,
    )
    # Settlement recorded, but the process "died" before release/publish.
    await auths.mark(task_id, status="settled", tx_hash="0xTX")

    # The sweep's inputs are intact: a settled auth + a still-withheld (encrypted)
    # artifact, so a recovery pass can release + publish without re-charging.
    assert store.a2a_auths[task_id]["status"] == "settled"
    assert task_id in store.withheld
    meta = await withheld.peek_meta(task_id)
    assert meta["price_usd"] == 0.225
    # And the memo is still only ciphertext at rest (never plaintext in the row).
    assert MEMO_SENTINEL not in json.dumps(store.withheld[task_id], default=str)
    released = await withheld.release(task_id)
    assert released["memo"] == MEMO_SENTINEL  # decrypts correctly for the sweep
