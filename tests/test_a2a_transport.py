"""A2A 1.0 transport conformance — the executable pin for ADR-0010.

Stands up a self-contained FastAPI app (NOT jim's seller) with a minimal echo
``AgentExecutor`` mounted on both A2A bindings via the SDK route factories, in
``enable_v0_3_compat=True`` mode (the spec dialect jim serves: ``message/send``,
``input-required``/``completed``, ``kind`` discriminator). Everything runs
in-process over ``httpx.ASGITransport`` — no DB, wallet, network, or API key —
so it stays inside the offline-first invariant.

If an SDK bump moves a wire literal, ``test_task_state_literals`` (and friends)
fail by name. See ``docs/adr/0010-a2a-durable-paid-tasks.md``.
"""

from __future__ import annotations

import json

import httpx
import pytest
from fastapi import FastAPI
from google.protobuf.json_format import MessageToDict

from a2a.compat.v0_3.types import TaskState as V03TaskState
from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.routes import (
    add_a2a_routes_to_fastapi,
    create_agent_card_routes,
    create_jsonrpc_routes,
    create_rest_routes,
)
from a2a.server.tasks import InMemoryTaskStore, TaskUpdater
from a2a.types import (
    AgentCapabilities,
    AgentCard,
    AgentInterface,
    AgentSkill,
    Message,
    Part,
    Task,
    TaskState,
    TaskStatus,
)

JSONRPC_PATH = "/a2a/jsonrpc"
REST_PREFIX = "/a2a/rest"
# v0.3 REST paths are Google-API style under a `/v1` URL version segment.
REST_SEND = f"{REST_PREFIX}/v1/message:send"


def _text_of(msg: Message | None) -> str:
    if msg is None:
        return ""
    return " ".join(p.text for p in msg.parts if p.WhichOneof("content") == "text")


class EchoExecutor(AgentExecutor):
    """Reference executor shape S4 extends: enqueue the initial Task first,
    then drive the lifecycle through a TaskUpdater, echoing the input back."""

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        if context.current_task is None:
            await event_queue.enqueue_event(
                Task(
                    id=context.task_id,
                    context_id=context.context_id,
                    status=TaskStatus(state=TaskState.TASK_STATE_SUBMITTED),
                )
            )
        updater = TaskUpdater(event_queue, context.task_id, context.context_id)
        await updater.start_work()
        await updater.add_artifact(
            [Part(text=f"echo: {_text_of(context.message)}")], name="echo"
        )
        await updater.complete()

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        updater = TaskUpdater(event_queue, context.task_id, context.context_id)
        await updater.cancel()


def _build_card() -> AgentCard:
    return AgentCard(
        name="jim-a2a-transport-test",
        description="echo executor for transport conformance",
        version="0.0.1",
        supported_interfaces=[
            AgentInterface(
                url="http://testserver/a2a/jsonrpc",
                protocol_binding="JSONRPC",
                protocol_version="1.0",
            )
        ],
        capabilities=AgentCapabilities(streaming=True),
        default_input_modes=["text/plain"],
        default_output_modes=["text/plain"],
        skills=[AgentSkill(id="echo", name="Echo", description="echoes input", tags=["util"])],
    )


def _make_app() -> FastAPI:
    app = FastAPI()
    card = _build_card()
    handler = DefaultRequestHandler(
        agent_executor=EchoExecutor(),
        task_store=InMemoryTaskStore(),
        agent_card=card,
    )
    add_a2a_routes_to_fastapi(
        app,
        agent_card_routes=create_agent_card_routes(agent_card=card),
        jsonrpc_routes=create_jsonrpc_routes(
            request_handler=handler, rpc_url=JSONRPC_PATH, enable_v0_3_compat=True
        ),
        rest_routes=create_rest_routes(
            request_handler=handler, path_prefix=REST_PREFIX, enable_v0_3_compat=True
        ),
    )
    return app


def _client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=_make_app()), base_url="http://testserver"
    )


def _jsonrpc(method: str, params: dict, rid: int = 1) -> dict:
    return {"jsonrpc": "2.0", "id": rid, "method": method, "params": params}


def _text_message(text: str, *, role: str = "user") -> dict:
    # v0.3 JSON-RPC message shape: parts[] with `kind` discriminator.
    return {"messageId": "m1", "role": role, "parts": [{"kind": "text", "text": text}]}


async def _send_jsonrpc(c: httpx.AsyncClient, text: str) -> dict:
    body = _jsonrpc(
        "message/send",
        {"message": _text_message(text), "configuration": {"blocking": True}},
    )
    r = await c.post(JSONRPC_PATH, json=body)
    assert r.status_code == 200, r.text
    return r.json()


async def test_jsonrpc_message_send_completes() -> None:
    async with _client() as c:
        payload = await _send_jsonrpc(c, "hello jim")
    assert "error" not in payload, payload
    result = payload["result"]
    # v0.3 JSON-RPC returns the Task directly as `result`, kind == "task".
    assert result["kind"] == "task"
    assert result["status"]["state"] == "completed"
    echoed = result["artifacts"][0]["parts"][0]
    assert echoed["kind"] == "text"
    assert "hello jim" in echoed["text"]


async def test_rest_binding_parity() -> None:
    # Same operation over the v0.3 REST binding. NOTE (ADR-0010 surprise #4/#5):
    # the REST body's parts key is `content` (proto name, not spec `parts`), role
    # is the proto enum string, and REST is non-blocking by default — so we pass
    # configuration.blocking. The REST response is serialized via the v0.3 *proto*
    # and therefore leaks v1-native literals: TASK_STATE_COMPLETED and parts with
    # NO `kind`. Parity here means "reaches terminal completed with the echo",
    # asserted in each binding's own dialect.
    body = {
        "message": {"messageId": "m1", "role": "ROLE_USER", "content": [{"text": "hello rest"}]},
        "configuration": {"blocking": True},
    }
    async with _client() as c:
        r = await c.post(REST_SEND, json=body)
        assert r.status_code == 200, r.text
    task = r.json()["task"]  # REST wraps the task under `task`.
    assert task["status"]["state"] == "TASK_STATE_COMPLETED"
    assert "hello rest" in task["artifacts"][0]["parts"][0]["text"]


async def test_unknown_method_error() -> None:
    async with _client() as c:
        r = await c.post(JSONRPC_PATH, json=_jsonrpc("does/notexist", {}, rid=9))
    assert r.status_code == 200, r.text
    err = r.json()["error"]
    # JSON-RPC "method not found".
    assert err["code"] == -32601
    assert "not found" in err["message"].lower()


async def test_tasks_get_roundtrip() -> None:
    async with _client() as c:
        sent = await _send_jsonrpc(c, "roundtrip please")
        task_id = sent["result"]["id"]
        r = await c.post(JSONRPC_PATH, json=_jsonrpc("tasks/get", {"id": task_id}, rid=2))
        assert r.status_code == 200, r.text
        got = r.json()
    assert "error" not in got, got
    result = got["result"]
    assert result["id"] == task_id
    assert result["kind"] == "task"
    assert result["status"]["state"] == "completed"
    assert "roundtrip please" in result["artifacts"][0]["parts"][0]["text"]


def test_task_state_literals() -> None:
    # This test IS the pin. a2a-sdk 1.1.0 carries THREE TaskState enums; assert
    # all the load-bearing spellings so an SDK bump that moves any fails by name.

    # (a) v1.0 proto core enum (executor code uses these).
    assert set(TaskState.keys()) == {
        "TASK_STATE_UNSPECIFIED",
        "TASK_STATE_SUBMITTED",
        "TASK_STATE_WORKING",
        "TASK_STATE_COMPLETED",
        "TASK_STATE_FAILED",
        "TASK_STATE_CANCELED",
        "TASK_STATE_INPUT_REQUIRED",
        "TASK_STATE_REJECTED",
        "TASK_STATE_AUTH_REQUIRED",
    }
    assert TaskState.Value("TASK_STATE_INPUT_REQUIRED") == 6

    # (b) v0.3 pydantic enum — the strings jim serves on the JSON-RPC wire.
    assert V03TaskState.submitted.value == "submitted"
    assert V03TaskState.working.value == "working"
    assert V03TaskState.input_required.value == "input-required"  # x402 payment pause
    assert V03TaskState.completed.value == "completed"
    assert V03TaskState.canceled.value == "canceled"
    assert V03TaskState.failed.value == "failed"
    assert V03TaskState.rejected.value == "rejected"
    assert V03TaskState.auth_required.value == "auth-required"


def test_card_model_validates() -> None:
    card = _build_card()

    # Default MessageToDict emits camelCase json_name aliases (the wire form).
    aliased = MessageToDict(card)
    assert "supportedInterfaces" in aliased
    iface = aliased["supportedInterfaces"][0]
    assert iface["protocolBinding"] == "JSONRPC"
    assert iface["protocolVersion"] == "1.0"
    assert aliased["capabilities"]["streaming"] is True

    # preserving_proto_field_name=True emits snake_case (the proto field names).
    snake = MessageToDict(card, preserving_proto_field_name=True)
    assert "supported_interfaces" in snake
    assert "default_input_modes" in snake


async def test_stream_smoke() -> None:
    # message/stream over JSON-RPC (SSE). S4 owns streaming depth; here we only
    # smoke that events flow and a terminal status-update arrives.
    body = _jsonrpc("message/stream", {"message": _text_message("stream me")})
    events: list[dict] = []
    async with _client() as c:
        async with c.stream("POST", JSONRPC_PATH, json=body) as r:
            assert r.status_code == 200
            assert "text/event-stream" in r.headers.get("content-type", "")
            async for line in r.aiter_lines():
                if line.startswith("data:"):
                    events.append(json.loads(line[len("data:"):].strip()))

    assert events, "expected at least one SSE event"
    results = [e["result"] for e in events if "result" in e]
    # The stream ends with a final status-update event.
    finals = [x for x in results if x.get("final") is True]
    assert finals, f"no final event in stream: {results}"
    assert finals[-1]["kind"] == "status-update"


@pytest.mark.parametrize("rid", [1, 2, 3])
async def test_message_send_is_deterministic(rid: int) -> None:
    # Guards against a flaky blocking/aggregation race: repeated blocking sends
    # must each reach `completed`.
    async with _client() as c:
        payload = await _send_jsonrpc(c, f"determinism {rid}")
    assert payload["result"]["status"]["state"] == "completed"
