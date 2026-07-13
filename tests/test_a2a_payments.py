"""The A2A x402 payment coordinator: verify-before-execute, settle-once, never-bill-rejected.

Hermetic — MemoryStore + a scripted ``FakeRail`` (no network, wallet, or key). The
load-bearing assertions are the money invariants (ADR-0008 extended to A2A):

- a submitted payment is verified against the *stored* requirements (price-swap
  defense), and the signed payload lives only as ciphertext at rest;
- settlement is CAS-guarded so a concurrent race settles exactly once;
- a discarded/expired/rail-failed task never records a receipt — money never moves
  for research jim's gates refused.

``X402Rail`` is only ever *constructed* here (never verify/settled) so the default
suite stays offline.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
from datetime import timedelta

import pytest

from jim.a2a.crypto import A2ACrypto
from jim.a2a.extension import (
    MD_PAYMENT_RECEIPTS,
    MD_PAYMENT_REQUIRED,
    MD_PAYMENT_STATUS,
    PaymentStatus,
)
from jim.a2a.payments import (
    RailResult,
    SettleStatus,
    SubmitStatus,
    X402PaymentCoordinator,
    X402Rail,
    _utcnow,
)
from jim.a2a.stores import PaymentAuths
from jim.config import Settings
from jim.store.repo import MemoryStore


# --- fixtures / fakes --------------------------------------------------------


def _settings(**kw) -> Settings:
    # _env_file=None keeps the developer's .env out; ephemeral crypto key.
    opts = {
        "a2a_encryption_key": None,
        "evm_private_key": None,
        "evm_address": "0x000000000000000000000000000000000000JIMM",
    }
    opts.update(kw)
    return Settings(_env_file=None, **opts)


@dataclass
class FakeRail:
    """Records every build/verify/settle call; verify/settle outcomes scriptable."""

    calls: list[tuple[str, dict]] = field(default_factory=list)
    verify_result: RailResult = field(
        default_factory=lambda: RailResult(ok=True, payer="0xPAYER")
    )
    settle_result: RailResult = field(
        default_factory=lambda: RailResult(
            ok=True,
            payer="0xPAYER",
            tx_hash="0xTX",
            amount="225000",
            network="eip155:84532",
            raw={"success": True, "transaction": "0xTX", "payer": "0xPAYER", "amount": "225000"},
        )
    )

    async def build_requirements(self, *, price_usd, resource, description) -> dict:
        self.calls.append(("build", {"price_usd": price_usd, "resource": resource}))
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
        self.calls.append(("verify", {"payload": payload, "requirements": requirements}))
        return self.verify_result

    async def settle(self, *, payload, requirements) -> RailResult:
        # Yield so a concurrent racer reliably reaches its CAS before we return.
        await asyncio.sleep(0)
        self.calls.append(("settle", {"payload": payload, "requirements": requirements}))
        return self.settle_result

    def n(self, method: str) -> int:
        return sum(1 for m, _ in self.calls if m == method)


def _coord(rail: FakeRail | None = None, **settings_kw):
    store = MemoryStore()
    settings = _settings(**settings_kw)
    crypto = A2ACrypto(settings)
    rail = rail or FakeRail()
    coord = X402PaymentCoordinator(
        auths=PaymentAuths(store, crypto), rail=rail, store=store, settings=settings
    )
    return coord, store, rail


async def _quote(coord, task_id="t1", *, kind="research", product="fundamentals",
                 identifier="AAPL", mode="agent") -> dict:
    return await coord.quote(
        task_id=task_id, kind=kind, product=product, identifier=identifier, mode=mode
    )


# --- 1. quote persists a required auth + returns the pause metadata ----------


async def test_quote_persists_required_and_returns_metadata():
    coord, store, rail = _coord()
    meta = await _quote(coord)

    row = store.a2a_auths["t1"]
    assert row["status"] == "required"
    # Requirements are stored verbatim (the price-swap checkpoint).
    assert row["requirements"] == meta[MD_PAYMENT_REQUIRED]
    assert meta[MD_PAYMENT_STATUS] == PaymentStatus.REQUIRED
    assert meta[MD_PAYMENT_REQUIRED]["amount"] == "225000"  # $0.225 agent tier
    assert rail.n("build") == 1


# --- 2. submit ok → verified, payer recorded, payload encrypted at rest ------


async def test_submit_ok_verifies_and_encrypts_payload():
    coord, store, rail = _coord()
    await _quote(coord)
    payload = {"scheme": "exact", "signature": "0xSECRET-SIGNATURE"}

    out = await coord.submit(task_id="t1", payload=payload)

    assert out.status is SubmitStatus.VERIFIED and out.verified
    assert out.payer == "0xPAYER"
    assert out.metadata[MD_PAYMENT_STATUS] == PaymentStatus.VERIFIED
    row = store.a2a_auths["t1"]
    assert row["status"] == "verified" and row["payer"] == "0xPAYER"
    # The signed payload is persisted ONLY as ciphertext.
    assert "0xSECRET-SIGNATURE" not in row["payload_ciphertext"]
    view = await coord._auths.get("t1")  # decrypts on read
    assert view.payload == payload


# --- 3. submit against an expired quote → expired, rail.verify NOT called -----


async def test_submit_expired_never_calls_rail():
    coord, store, rail = _coord()
    await _quote(coord)
    store.a2a_auths["t1"]["expires_at"] = _utcnow() - timedelta(seconds=1)

    out = await coord.submit(task_id="t1", payload={"x": 1})

    assert out.status is SubmitStatus.EXPIRED
    assert rail.n("verify") == 0
    assert store.a2a_auths["t1"]["status"] == "expired"


# --- 4. verify runs against the STORED requirements, not the client's copy ----


async def test_submit_verifies_against_stored_requirements():
    coord, store, rail = _coord()
    meta = await _quote(coord)
    quoted = meta[MD_PAYMENT_REQUIRED]
    # A hostile client submits a payload referencing cheaper, doctored requirements.
    payload = {"scheme": "exact", "accepted": {"amount": "1", "payTo": "0xATTACKER"}}

    await coord.submit(task_id="t1", payload=payload)

    seen = rail.calls[-1]
    assert seen[0] == "verify"
    assert seen[1]["requirements"] == quoted  # the stored ones, not the payload's
    assert seen[1]["requirements"]["amount"] == "225000"


# --- 5. settle happy path → settled + tx, receipt recorded, receipts metadata -


async def test_settle_records_receipt_and_completed_metadata():
    coord, store, rail = _coord()
    await _quote(coord, identifier="aapl")  # lower-case in → receipt upper-cases
    await coord.submit(task_id="t1", payload={"sig": "0xS"})

    out = await coord.settle(task_id="t1")

    assert out.status is SettleStatus.SETTLED and out.settled
    assert out.tx_hash == "0xTX"
    assert rail.n("settle") == 1
    assert store.a2a_auths["t1"]["status"] == "settled"
    assert store.a2a_auths["t1"]["tx_hash"] == "0xTX"
    # Exactly one receipt, on the A2A task path, with the right economics.
    assert len(store.receipts) == 1
    r = store.receipts[0]
    assert r["path"] == "/a2a/tasks/t1"
    assert r["product"] == "fundamentals"
    assert r["identifier"] == "AAPL"
    assert r["amount_usdc"] == pytest.approx(0.225)  # "225000" base units → USDC
    assert r["tx_hash"] == "0xTX" and r["payer"] == "0xPAYER"
    assert r["pay_to"] == "0x000000000000000000000000000000000000JIMM"
    # Receipts metadata is the a2a-x402 completion shape.
    assert out.metadata[MD_PAYMENT_STATUS] == PaymentStatus.COMPLETED
    assert out.metadata[MD_PAYMENT_RECEIPTS] == [rail.settle_result.raw]


# --- 6. concurrent settle → rail.settle exactly once, one settled/one skipped -


async def test_concurrent_settle_moves_money_once():
    coord, store, rail = _coord()
    await _quote(coord)
    await coord.submit(task_id="t1", payload={"sig": "0xS"})

    a, b = await asyncio.gather(
        coord.settle(task_id="t1"), coord.settle(task_id="t1")
    )

    assert rail.n("settle") == 1  # money moved exactly once
    statuses = {a.status, b.status}
    assert statuses == {SettleStatus.SETTLED, SettleStatus.ALREADY_SETTLING}
    assert len(store.receipts) == 1  # and exactly one receipt
    # The already_settling loser carries no error (idempotent).
    loser = a if a.status is SettleStatus.ALREADY_SETTLING else b
    assert loser.reason is None


# --- 7. settle on rail failure → settle_failed, NO receipt --------------------


async def test_settle_rail_failure_records_no_receipt():
    rail = FakeRail(settle_result=RailResult(ok=False, error="insufficient funds"))
    coord, store, _ = _coord(rail)
    await _quote(coord)
    await coord.submit(task_id="t1", payload={"sig": "0xS"})

    out = await coord.settle(task_id="t1")

    assert out.status is SettleStatus.FAILED
    assert out.reason == "insufficient funds"
    assert out.metadata[MD_PAYMENT_STATUS] == PaymentStatus.FAILED
    assert store.a2a_auths["t1"]["status"] == "settle_failed"
    assert store.receipts == []  # no money → no receipt


# --- 8. settle re-checks expiry immediately before the CAS -------------------


async def test_settle_expired_before_cas_never_settles():
    coord, store, rail = _coord()
    await _quote(coord)
    await coord.submit(task_id="t1", payload={"sig": "0xS"})
    # The quote lapsed while the task was working.
    store.a2a_auths["t1"]["expires_at"] = _utcnow() - timedelta(seconds=1)

    out = await coord.settle(task_id="t1")

    assert out.status is SettleStatus.EXPIRED
    assert rail.n("settle") == 0
    assert store.a2a_auths["t1"]["status"] == "expired"
    assert store.receipts == []


# --- 9. discard is terminal; after discard, settle refuses (never bills) ------


async def test_discard_from_required_and_verified():
    coord, store, _ = _coord()
    await _quote(coord, task_id="req")
    await coord.discard(task_id="req", reason="cancelled")
    assert store.a2a_auths["req"]["status"] == "discarded"

    await _quote(coord, task_id="ver")
    await coord.submit(task_id="ver", payload={"sig": "0xS"})
    await coord.discard(task_id="ver", reason="gate rejected")
    assert store.a2a_auths["ver"]["status"] == "discarded"

    # discard on a missing task never raises.
    await coord.discard(task_id="ghost", reason="n/a")


async def test_settle_refuses_after_discard():
    coord, store, rail = _coord()
    await _quote(coord)
    await coord.submit(task_id="t1", payload={"sig": "0xS"})
    await coord.discard(task_id="t1", reason="gate rejected the research")

    out = await coord.settle(task_id="t1")

    assert out.status is SettleStatus.FAILED
    assert rail.n("settle") == 0  # the never-bill-rejected path at the coordinator
    assert store.receipts == []
    assert store.a2a_auths["t1"]["status"] == "discarded"


# --- 10. price resolution reuses the published schedule ----------------------


async def test_price_resolution_across_kinds():
    from jim.marketplace.pricing import price_for

    coord, store, _ = _coord()

    await coord.quote(task_id="a", kind="research", product="fundamentals",
                      identifier="AAPL", mode="agent")
    await coord.quote(task_id="h", kind="research", product="fundamentals",
                      identifier="AAPL", mode="human")
    await coord.quote(task_id="m", kind="monitor_activation", product="fundamentals",
                      identifier="AAPL", mode="agent")
    await coord.quote(task_id="u", kind="monitor_release", product="fundamentals",
                      identifier="AAPL", mode="agent")

    assert store.a2a_auths["a"]["amount_usd"] == price_for("fundamentals", "agent")
    assert store.a2a_auths["h"]["amount_usd"] == price_for("fundamentals", "oneshot")
    # agent tier is a 10%-off discount on the oneshot price.
    assert store.a2a_auths["a"]["amount_usd"] < store.a2a_auths["h"]["amount_usd"]
    assert store.a2a_auths["m"]["amount_usd"] == 0.10  # monitor_activation_price
    assert store.a2a_auths["u"]["amount_usd"] == 0.10  # monitor_update_price


async def test_unknown_quote_kind_raises():
    coord, _, _ = _coord()
    with pytest.raises(ValueError, match="unknown quote kind"):
        await coord.quote(task_id="x", kind="bogus", product="fundamentals",
                          identifier="AAPL", mode="agent")


# --- 11. engine price_out_usd override books the tier price (still $0 if not ok) --


def _engine_seam(monkeypatch, *, memos, price_out=0.25):
    """Copy of the tests/test_engine.py source/synth seam: real graph + gate,
    scripted source + synth + no-op judge/debate, capturing store."""
    from jim.research import engine
    from jim.research.cost import Usage
    from jim.research.debate import DebateResult
    from jim.research.facts import USD, Fact, Snapshot
    from jim.research.judge import JudgeResult
    from jim.research.products import Product
    from jim.research.synthesize import SynthResult
    from jim.sources.base import GatherResult

    snap = Snapshot(
        ticker="ACME", cik="0000000001", entity_name="Acme Corp",
        facts=[Fact(id="C1", label="Revenue", value=100.0, unit=USD, source_label="SEC EDGAR",
                    accession="x", form="10-K", fiscal_year=2024, fiscal_period="FY")],
        as_of="2025-01-01",
    )

    class FakeSource:
        name = "fake"
        is_paid = False

        async def gather(self, identifier, *, budget, store):
            return GatherResult(snapshot=snap, cost_in_usd=0.0, cache_hit=False)

    booked: dict = {}

    class CaptureStore:
        async def get_cached_memo(self, **kw):
            return None

        async def put_cached_memo(self, **kw):
            pass

        async def upsert_insight(self, **kw):
            pass

        async def record_trust_event(self, **kw):
            pass

        async def record_query(self, **kw):
            booked.update(kw)

    calls = {"n": 0}

    async def fake_synth(snapshot, *, mode="human", feedback=None, debate=None):
        memo = memos[min(calls["n"], len(memos) - 1)]
        calls["n"] += 1
        return SynthResult(memo=memo, usage=Usage(model="test", input_tokens=10, output_tokens=20))

    async def fake_judge(memo, snapshot, **_):
        return JudgeResult.skip()

    async def fake_debate(snapshot):
        return DebateResult(bull="", bear="", verdict="", usages=[])

    monkeypatch.setattr(engine, "get_product",
                        lambda name: Product(name="fundamentals", source=FakeSource(),
                                             price_out_usd=price_out, identifier_label="x"))
    monkeypatch.setattr(engine, "synthesize", fake_synth)
    monkeypatch.setattr(engine, "judge_faithfulness", fake_judge)
    monkeypatch.setattr(engine, "run_debate", fake_debate)
    monkeypatch.setattr(engine, "get_store", lambda: CaptureStore())
    return engine, booked


async def test_engine_price_override_books_tier_price_when_ok(monkeypatch):
    engine, booked = _engine_seam(monkeypatch, memos=["Revenue was $100 [C1]."])

    result = await engine.run_research("ACME", product="fundamentals", price_out_usd=0.225)

    assert result.status == "ok"
    assert result.cost["price_out_usd"] == 0.225  # the A2A tier price, not the $0.25 headline
    assert booked["price_out_usd"] == 0.225


async def test_engine_price_override_still_books_zero_when_rejected(monkeypatch):
    # A memo that never matches the fact → gate rejects → refused, never billed.
    engine, booked = _engine_seam(monkeypatch, memos=["Revenue was $999 [C1]."])

    result = await engine.run_research("ACME", product="fundamentals", price_out_usd=0.225)

    assert result.status == "rejected"
    assert result.cost["price_out_usd"] == 0.0  # the invariant holds despite the override
    assert booked["price_out_usd"] == 0.0


# --- X402Rail: construction only (never verify/settle offline) ---------------


def test_x402_rail_constructs_offline():
    # Wiring the resource server + scheme must not touch the network; only
    # initialize()/verify()/settle() would, and we never call them here.
    rail = X402Rail(_settings())
    assert rail._server is not None
    assert rail._initialized is False
