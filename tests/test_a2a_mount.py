"""S3 — the A2A bindings mounted in the real jim-seller app.

The S0 transport test proved the SDK route factories on a *standalone* app; this
proves the same behaviour on jim's seller (fake echo executor), plus the seller
seams the standalone app never had: the x402 extension-echo middleware, the
call-chain refusal that must pre-empt A2A, the paywall leaving ``/a2a`` alone,
and the ``A2A_ENABLED`` kill-switch. All in-process over the FastAPI
``TestClient`` — no DB, wallet, network, or key (offline-first).
"""

from __future__ import annotations

from fastapi.testclient import TestClient

from jim.a2a.extension import (
    EXT_HEADER,
    LEGACY_EXT_HEADER,
    MD_PAYMENT_REQUIRED,
    MD_PAYMENT_STATUS,
    PaymentStatus,
    X402_EXT_URI,
)
from jim.config import Settings
from jim.interop.callchain import CALL_CHAIN_HEADER
from jim.seller.app import build_app
from jim.wallet import LocalWallet

JSONRPC_PATH = "/a2a/jsonrpc"
REST_SEND = "/a2a/rest/v1/message:send"


def _client(**overrides) -> tuple[TestClient, str]:
    """A seller app on a fresh wallet; returns (client, the seller's own address)."""
    wallet = LocalWallet.create()
    settings = Settings(
        evm_address=wallet.address, evm_private_key=wallet.private_key, **overrides
    )
    return TestClient(build_app(settings)), wallet.address


def _jsonrpc(method: str, params: dict, rid: int = 1) -> dict:
    return {"jsonrpc": "2.0", "id": rid, "method": method, "params": params}


def _send_body(text: str) -> dict:
    # v0.3 JSON-RPC message shape: parts[] with the `kind` discriminator.
    return _jsonrpc(
        "message/send",
        {
            "message": {"messageId": "m1", "role": "user", "parts": [{"kind": "text", "text": text}]},
            "configuration": {"blocking": True},
        },
    )


# --- transport parity on the mounted seller ---------------------------------


def test_jsonrpc_message_send_pauses_for_payment_on_the_seller() -> None:
    # S4: the default mount now runs the real research executor, so a valid research
    # command with the x402 extension activated pauses at input-required carrying
    # the payment challenge (stronger than the old echo → completed assertion).
    client, _ = _client()
    resp = client.post(
        JSONRPC_PATH,
        json=_send_body("research fundamentals AAPL"),
        headers={EXT_HEADER: X402_EXT_URI},
    )
    assert resp.status_code == 200, resp.text
    result = resp.json()["result"]
    assert result["kind"] == "task"
    assert result["status"]["state"] == "input-required"
    assert result["metadata"][MD_PAYMENT_STATUS] == PaymentStatus.REQUIRED.value
    assert result["metadata"][MD_PAYMENT_REQUIRED]["amount"]  # the x402 challenge


def test_rest_binding_responds_on_the_seller() -> None:
    # v0.3 REST quirks (ADR-0010 #4/#5): body parts key is `content` (proto name),
    # role is the proto enum, non-blocking by default (pass blocking), and the
    # response serializes v1-native literals (TASK_STATE_*, no `kind`). S4: the real
    # executor drives the REST binding to the same payment pause (INPUT_REQUIRED)
    # instead of an echo → COMPLETED, given the activated x402 extension.
    client, _ = _client()
    body = {
        "message": {
            "messageId": "m1",
            "role": "ROLE_USER",
            "content": [{"text": "research fundamentals AAPL"}],
        },
        "configuration": {"blocking": True},
    }
    resp = client.post(REST_SEND, json=body, headers={EXT_HEADER: X402_EXT_URI})
    assert resp.status_code == 200, resp.text
    task = resp.json()["task"]
    assert task["status"]["state"] == "TASK_STATE_INPUT_REQUIRED"


def test_tasks_get_roundtrip_through_the_seller() -> None:
    # S4: the paused payment task persists; tasks/get re-serves its input-required
    # state AND the x402 payment metadata that rode the status event onto
    # Task.metadata (was: echo → completed roundtrip).
    client, _ = _client()
    sent = client.post(
        JSONRPC_PATH,
        json=_send_body("research fundamentals AAPL"),
        headers={EXT_HEADER: X402_EXT_URI},
    ).json()
    task_id = sent["result"]["id"]
    got = client.post(JSONRPC_PATH, json=_jsonrpc("tasks/get", {"id": task_id}, rid=2)).json()
    assert "error" not in got, got
    result = got["result"]
    assert result["id"] == task_id
    assert result["status"]["state"] == "input-required"
    assert result["metadata"][MD_PAYMENT_STATUS] == PaymentStatus.REQUIRED.value


# --- the extension echo, end to end through the seller middleware -------------


def test_extension_echo_canonical_header() -> None:
    client, _ = _client()
    resp = client.post(JSONRPC_PATH, json=_send_body("x"), headers={EXT_HEADER: X402_EXT_URI})
    assert resp.status_code == 200
    assert resp.headers.get("a2a-extensions") == X402_EXT_URI


def test_extension_echo_legacy_header() -> None:
    client, _ = _client()
    resp = client.post(
        JSONRPC_PATH, json=_send_body("x"), headers={LEGACY_EXT_HEADER: X402_EXT_URI}
    )
    assert resp.status_code == 200
    assert resp.headers.get("a2a-extensions") == X402_EXT_URI


def test_extension_not_echoed_without_activation() -> None:
    client, _ = _client()
    resp = client.post(JSONRPC_PATH, json=_send_body("x"))
    assert resp.status_code == 200
    assert "a2a-extensions" not in resp.headers


# --- call-chain refusal pre-empts A2A (before any task processing) -----------


def test_callchain_loop_refused_before_a2a() -> None:
    client, own_address = _client()
    resp = client.post(
        JSONRPC_PATH,
        json=_send_body("should never run"),
        headers={CALL_CHAIN_HEADER: f"0x1111111111111111111111111111111111111111,{own_address}"},
    )
    # 409 from the outermost CallChain middleware — no JSON-RPC result, no task.
    assert resp.status_code == 409
    assert "loop" in resp.json()["error"]


# --- the paywall leaves /a2a alone -------------------------------------------


def test_a2a_is_not_paywalled_but_paid_routes_still_are() -> None:
    client, _ = _client()
    # /a2a/* is absent from the x402 routes dict, so an unpaid call is served...
    assert client.post(JSONRPC_PATH, json=_send_body("free")).status_code == 200
    # ...while a genuine paid route still issues the 402 challenge (control).
    assert client.get("/ping").status_code == 402


# --- the kill-switch ----------------------------------------------------------


def test_a2a_disabled_404s_and_leaves_legacy_routes_intact() -> None:
    client, _ = _client(a2a_enabled=False)
    assert client.post(JSONRPC_PATH, json=_send_body("x")).status_code == 404
    assert client.post(REST_SEND, json={"message": {}}).status_code == 404
    # Every legacy surface is unaffected by the kill-switch.
    assert client.get("/health").status_code == 200
    assert client.get("/ping").status_code == 402
    assert client.get("/catalog").status_code == 200
    assert client.get("/monitors").status_code == 200
