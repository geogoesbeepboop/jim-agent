"""A2A durable-task persistence + crypto + convenience layer. Offline, MemoryStore.

Money correctness rides on ``cas_a2a_auth_status``: under N concurrent settle
attempts exactly one must win, so a task settles at most once. The withheld-
artifact tests pin the other load-bearing invariant — pre-payment memo content
is never readable without an explicit (paid) release.
"""

from __future__ import annotations

import asyncio
import json

from cryptography.fernet import Fernet

from jim.a2a.crypto import A2ACrypto, resolve_key
from jim.a2a.stores import DeadLetters, PaymentAuths, PushConfigs, WithheldArtifacts
from jim.config import Settings
from jim.store.repo import MemoryStore, SqlStore

# A2A Store methods added in S1c; used for the SqlStore conformance check.
A2A_STORE_METHODS = [
    "save_a2a_auth",
    "get_a2a_auth",
    "update_a2a_auth",
    "cas_a2a_auth_status",
    "list_a2a_auths",
    "save_withheld",
    "get_withheld",
    "delete_withheld",
    "save_a2a_push_config",
    "get_a2a_push_configs",
    "delete_a2a_push_config",
    "record_push_deadletter",
    "list_push_deadletters",
]


async def _save_auth(store, task_id="t1", *, status="required", **kw):
    defaults = dict(
        kind="research",
        product="fundamentals",
        identifier="AAPL",
        mode="pro",
        amount_usd=0.25,
        requirements={"scheme": "exact", "amount": "250000"},
        payload_ciphertext=None,
        payer=None,
        expires_at=None,
    )
    defaults.update(kw)
    await store.save_a2a_auth(task_id=task_id, status=status, **defaults)


# --- auth CRUD ---------------------------------------------------------------


async def test_auth_crud_roundtrip():
    store = MemoryStore()
    await _save_auth(store, requirements={"amount": "250000"})
    row = await store.get_a2a_auth("t1")
    assert row["task_id"] == "t1"
    assert row["kind"] == "research" and row["identifier"] == "AAPL"
    assert row["requirements"] == {"amount": "250000"}
    assert row["status"] == "required"
    assert row["payload_ciphertext"] is None and row["tx_hash"] is None
    assert await store.get_a2a_auth("missing") is None


async def test_auth_upsert_overwrites_required_row():
    store = MemoryStore()
    await _save_auth(store, amount_usd=0.25, status="required")
    first = await store.get_a2a_auth("t1")
    # A re-quote before payment overwrites the pending row (new price/requirements).
    await _save_auth(store, amount_usd=0.50, status="required", requirements={"amount": "500000"})
    row = await store.get_a2a_auth("t1")
    assert row["amount_usd"] == 0.50
    assert row["requirements"] == {"amount": "500000"}
    # created_at is preserved across the upsert; updated_at moves forward.
    assert row["created_at"] == first["created_at"]
    assert row["updated_at"] >= first["updated_at"]


async def test_update_auth_sentinel_skips_unspecified_fields():
    store = MemoryStore()
    await _save_auth(store, payload_ciphertext="CT0", payer="0xpayer")
    # Update only status — payload_ciphertext and payer must survive untouched.
    await store.update_a2a_auth("t1", status="verified")
    row = await store.get_a2a_auth("t1")
    assert row["status"] == "verified"
    assert row["payload_ciphertext"] == "CT0" and row["payer"] == "0xpayer"
    # An explicit None DOES clear a nullable field (distinct from "omit").
    await store.update_a2a_auth("t1", payload_ciphertext=None)
    assert (await store.get_a2a_auth("t1"))["payload_ciphertext"] is None
    # Updating a missing task is a no-op, not an error.
    await store.update_a2a_auth("nope", status="settled")
    assert await store.get_a2a_auth("nope") is None


async def test_list_auths_status_filter():
    store = MemoryStore()
    await _save_auth(store, "a", status="required")
    await _save_auth(store, "b", status="verified")
    await _save_auth(store, "c", status="verified")
    assert {r["task_id"] for r in await store.list_a2a_auths()} == {"a", "b", "c"}
    assert {r["task_id"] for r in await store.list_a2a_auths(status="verified")} == {"b", "c"}
    assert [r["task_id"] for r in await store.list_a2a_auths(status="required")] == ["a"]
    assert await store.list_a2a_auths(status="settled") == []


# --- the settle-once guard ---------------------------------------------------


async def test_cas_race_exactly_one_winner():
    store = MemoryStore()
    await _save_auth(store, status="verified")
    results = await asyncio.gather(
        *(store.cas_a2a_auth_status("t1", "verified", "settling") for _ in range(8))
    )
    assert sum(1 for r in results if r) == 1  # exactly one settler wins
    assert (await store.get_a2a_auth("t1"))["status"] == "settling"


async def test_cas_wrong_status_is_false_and_noop():
    store = MemoryStore()
    await _save_auth(store, status="required")
    assert await store.cas_a2a_auth_status("t1", "verified", "settling") is False
    assert (await store.get_a2a_auth("t1"))["status"] == "required"
    # CAS on a missing task also returns False (no exception).
    assert await store.cas_a2a_auth_status("ghost", "verified", "settling") is False


# --- crypto ------------------------------------------------------------------


def _settings(**kw):
    # _env_file=None keeps the developer's .env (EVM_PRIVATE_KEY etc.) out of the
    # test — key resolution must be driven only by the kwargs we pass. Explicit
    # None defaults are overridable by kw (e.g. to set evm_private_key).
    opts = {"a2a_encryption_key": None, "evm_private_key": None}
    opts.update(kw)
    return Settings(_env_file=None, **opts)


def test_crypto_json_and_text_roundtrip():
    crypto = A2ACrypto(_settings())
    obj = {"memo": "AAPL beat", "n": 3, "nested": {"k": [1, 2]}}
    assert crypto.decrypt_json(crypto.encrypt_json(obj)) == obj
    assert crypto.decrypt_text(crypto.encrypt_text("hello")) == "hello"


def test_crypto_explicit_fernet_key():
    key = Fernet.generate_key().decode()
    crypto = A2ACrypto(_settings(a2a_encryption_key=key))
    assert crypto.source == "configured"
    # A second instance with the SAME configured key decrypts the first's output.
    other = A2ACrypto(_settings(a2a_encryption_key=key))
    assert other.decrypt_json(crypto.encrypt_json({"x": 1})) == {"x": 1}


def test_crypto_arbitrary_secret_is_derived():
    crypto = A2ACrypto(_settings(a2a_encryption_key="not-a-valid-fernet-key"))
    assert crypto.source == "derived-configured"
    assert crypto.decrypt_json(crypto.encrypt_json({"x": 1})) == {"x": 1}


def test_crypto_hkdf_from_evm_is_deterministic_across_instances():
    s = _settings(evm_private_key="0xdeadbeefcafe")
    a = A2ACrypto(s)
    b = A2ACrypto(_settings(evm_private_key="0xdeadbeefcafe"))
    assert a.source == "derived-evm" and b.source == "derived-evm"
    # Same key in → interchangeable: b decrypts what a encrypted (restart-stable).
    assert b.decrypt_json(a.encrypt_json({"secret": "S"})) == {"secret": "S"}
    # resolve_key is a pure function of the secret.
    assert resolve_key(s)[0] == resolve_key(_settings(evm_private_key="0xdeadbeefcafe"))[0]


def test_crypto_ephemeral_works_within_process():
    crypto = A2ACrypto(_settings())
    assert crypto.source == "ephemeral"
    assert crypto.decrypt_json(crypto.encrypt_json({"ok": True})) == {"ok": True}


# --- withheld artifacts (peek_meta must never leak the memo) -----------------


async def test_withheld_hold_peek_release_discard():
    store = MemoryStore()
    crypto = A2ACrypto(_settings())
    withheld = WithheldArtifacts(store, crypto)
    memo = {"memo": "TOP-SECRET-MEMO", "figures": [1, 2, 3]}
    await withheld.hold("t1", "mon-1", "high", "2026-07-13", 0.10, memo)

    # peek_meta exposes ONLY metadata — the memo string is not reachable through it.
    meta = await withheld.peek_meta("t1")
    assert meta == {
        "task_id": "t1",
        "monitor_id": "mon-1",
        "severity": "high",
        "as_of": "2026-07-13",
        "price_usd": 0.10,
    }
    assert "TOP-SECRET-MEMO" not in json.dumps(meta)
    # And the memo is not sitting in plaintext at rest — only ciphertext is stored.
    assert "TOP-SECRET-MEMO" not in store.withheld["t1"]["payload_ciphertext"]

    # release decrypts, returns the payload, then deletes it (paid, one-shot).
    assert await withheld.release("t1") == memo
    assert await withheld.peek_meta("t1") is None
    assert await withheld.release("t1") is None

    # discard is idempotent.
    await withheld.hold("t2", "mon-2", "info", None, 0.10, memo)
    await withheld.discard("t2")
    await withheld.discard("t2")
    assert await withheld.peek_meta("t2") is None


# --- PaymentAuths convenience view -------------------------------------------


async def test_payment_auths_encrypts_payload_and_decrypts_on_read():
    store = MemoryStore()
    crypto = A2ACrypto(_settings())
    auths = PaymentAuths(store, crypto)
    await auths.create_required(
        task_id="t1",
        kind="research",
        product="fundamentals",
        identifier="AAPL",
        mode="pro",
        amount_usd=0.25,
        requirements={"amount": "250000"},
    )
    view = await auths.get("t1")
    assert view.status == "required" and view.payload is None

    payload = {"scheme": "exact", "signature": "0xSIGNED"}
    await auths.attach_payload("t1", payload, payer="0xpayer")
    # Persisted only as ciphertext — the signature never sits in plaintext at rest.
    assert "0xSIGNED" not in store.a2a_auths["t1"]["payload_ciphertext"]
    view = await auths.get("t1")
    assert view.payload == payload and view.payer == "0xpayer"

    # cas advances the state machine; mark records the settlement tx.
    assert await auths.cas("t1", "required", "settling") is True
    await auths.mark("t1", status="settled", tx_hash="0xtx")
    view = await auths.get("t1")
    assert view.status == "settled" and view.tx_hash == "0xtx"

    # sweep returns decrypted views by status (restart recovery).
    swept = await auths.sweep("settled")
    assert [v.task_id for v in swept] == ["t1"] and swept[0].payload == payload


# --- push configs + dead letters ---------------------------------------------


async def test_push_configs_crud():
    store = MemoryStore()
    crypto = A2ACrypto(_settings())
    configs = PushConfigs(store, crypto)
    await configs.save("t1", "c1", {"url": "https://a.example/hook", "token": "T1"})
    await configs.save("t1", "c2", {"url": "https://b.example/hook", "token": "T2"})
    # URL+token stored encrypted together, not in plaintext.
    assert "T1" not in store.a2a_push_configs["t1:c1"]["config_ciphertext"]

    listed = dict(await configs.list("t1"))
    assert set(listed) == {"c1", "c2"}
    assert listed["c1"]["token"] == "T1"

    await configs.delete("t1", "c1")
    assert [cid for cid, _ in await configs.list("t1")] == ["c2"]


async def test_dead_letters_append_and_filter():
    store = MemoryStore()
    dl = DeadLetters(store)
    await dl.record(
        task_id="t1", config_id="c1", event_type="status-update", attempts=5,
        last_error="connect timeout", last_status_code=None,
    )
    await dl.record(
        task_id="t2", config_id="c2", event_type="artifact-update", attempts=3,
        last_error=None, last_status_code=500,
    )
    assert len(await dl.list()) == 2
    only_t1 = await dl.list(task_id="t1")
    assert len(only_t1) == 1 and only_t1[0]["attempts"] == 5
    # No event body is ever stored (a dead letter is not a copy of the artifact).
    assert "body" not in only_t1[0] and "event" not in only_t1[0]


# --- protocol conformance + settings defaults --------------------------------


def test_sqlstore_implements_every_a2a_method():
    # No DB connection needed — just assert the SqlStore surface matches the
    # Protocol additions so a live backend can't silently miss a method.
    for name in A2A_STORE_METHODS:
        assert hasattr(SqlStore, name), f"SqlStore missing {name}"
        assert callable(getattr(SqlStore, name))


def test_a2a_settings_defaults():
    s = Settings(_env_file=None)
    assert s.monitor_activation_price == "$0.10"
    assert s.a2a_enabled is True
    assert s.a2a_payment_timeout_seconds == 900
    assert s.a2a_max_monitors_per_context == 5
    assert s.a2a_monitor_min_interval_seconds == 1800
    assert s.a2a_push_max_attempts == 5
    assert s.a2a_push_backoff_base_seconds == 1.0
    assert s.a2a_push_timeout_seconds == 10.0
    assert s.a2a_push_max_body_bytes == 65536
    assert s.a2a_encryption_key is None
