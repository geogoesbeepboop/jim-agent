# ADR-0010 — A2A 1.0 durable paid tasks: SDK pin, both transport bindings, and the empirically verified literals later stages import

**Status:** Accepted

## Context

jim already sells and buys research over x402 (ADR-0007/0008). The next surface
is [A2A](https://a2a-protocol.org) — Google's Agent-to-Agent protocol — so that
arbitrary agent clients (not just jim's own UI/MCP) can commission a **durable,
long-running, paid** research _task_: submit a request, get paused for payment,
pay over x402, and collect a cited memo, over a standard wire other agents
already speak. That work is staged (S1 input parsing → S2 payment coordinator →
S3 mount in the seller → S4 real research executor → S5 push → S6 monitors),
and every later stage hard-codes import paths, wire literals, and method names
against the SDK. If those literals are wrong, every stage is wrong.

This ADR is the **spike (S0)**: pin the SDK, then run real Python against the
installed package to record the load-bearing literals verbatim, prove transport
conformance with a standalone echo executor mounted on both bindings, and flag
where reality contradicts the pre-spike research. No jim production code changes
here; later ADRs extend the architecture.

## Decision

### 1. Pin `a2a-sdk[fastapi]==1.1.0` — exact, one extra

- **Exact pin.** The SDK has high churn (0.3.x → 1.0 was a full type-system
  rewrite; see Surprises). An exact `==1.1.0` pin plus a wire-literal test
  (`tests/test_a2a_transport.py::test_task_state_literals`) means an SDK bump
  that moves a wire string fails by name in CI instead of silently shipping a
  non-conformant agent.
- **Only the `[fastapi]` extra.** It pulls `fastapi>=0.115.2 + starlette +
sse-starlette`. jim already has `fastapi[standard]`; the load-bearing add is
  `sse-starlette` (SSE streaming for `message/stream`). Base `a2a-sdk` pulls
  `protobuf`, `grpcio`-free `google-api-core`, `googleapis-common-protos`,
  `httpx-sse`, `json-rpc`, `culsans` — no server framework.
- **No `[postgresql]` / `[sql]` extra.** Those resolve to
  `sqlalchemy[asyncio,postgresql-asyncpg]>=2.0.0`, which is _exactly_ what jim
  already declares (`sqlalchemy[asyncio]>=2.0` + `asyncpg>=0.29`). Verified:
  `from a2a.server.tasks import DatabaseTaskStore` imports cleanly against
  jim's current deps (sqlalchemy 2.0.50, asyncpg 0.31.0). `DatabaseTaskStore`
  takes a caller-provided `AsyncEngine`, so the asyncpg driver is only needed at
  engine-construction time, which jim's `store/` already owns. Adding the extra
  would only risk a redundant/observably-conflicting sqlalchemy constraint.

### 2. Serve the **v0.3 wire dialect** via `enable_v0_3_compat=True`, on both bindings

This is the pivotal decision and it is **not** what the pre-spike research
assumed. a2a-sdk 1.1.0 is **proto-first**: `a2a.types.*` are protobuf messages
(`a2a_pb2`), the `AgentExecutor`/`TaskUpdater` operate on protos, and the
default JSON wire is protobuf's proto3-JSON mapping — `TASK_STATE_COMPLETED`,
proto-RPC method names (`SendMessage`), oneof `Part` with no discriminator
field, and a **mandatory `A2A-Version: 1.0` request header** (absent ⇒ error
`-32009 VERSION_NOT_SUPPORTED`).

The [a2a-x402 v0.1 extension](https://github.com/google-a2a/a2a-x402) and the
existing A2A client ecosystem speak the **v0.3 spec dialect**: method
`message/send`, TaskState `input-required`/`completed`, `Part` discriminated by
`kind`, no version header. The route factories expose this via a per-route
`enable_v0_3_compat=True` flag that translates v0.3 JSON ⇆ v1.0 proto core
internally. **The executor and card code are identical in both modes** — only
the mount flag changes the wire — so jim writes proto-native handlers once and
serves the dialect clients expect.

jim mounts JSON-RPC at `/a2a/jsonrpc` and REST at `/a2a/rest`, both with
`enable_v0_3_compat=True`. **JSON-RPC is jim's primary/x402 binding** because it
is the only binding that emits a _fully_ v0.3-spec response; the v0.3 REST
binding parses v0.3 requests but serializes **v1-native** responses (a genuine
SDK asymmetry — see Surprises). S3 leads with JSON-RPC for the payment flow.

### 3. Executor pattern (proto-native, binding-agnostic)

`execute(context, event_queue)` must **enqueue an initial `Task` proto before
any status update** — there is no `new_task()` helper in 1.1.0, and emitting a
`TaskStatusUpdateEvent` first raises `InvalidAgentResponseError: Agent should
enqueue Task before …`. Then drive the lifecycle through a `TaskUpdater`. The
transport test's `EchoExecutor` is the reference shape S4 extends.

## Verified literals (empirical, a2a-sdk 1.1.0)

> Every item below was printed from the installed package or observed on the
> wire from an in-process mount (httpx `ASGITransport`). Reproduce with
> `tests/test_a2a_transport.py`.

### TaskState — THREE enums, three spellings (do not conflate)

| Where                | Import                                             | Members / wire values                                                                                                                                                                                            |
| -------------------- | -------------------------------------------------- | ---------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| v1.0 proto (core)    | `a2a.types.TaskState` (protobuf `EnumTypeWrapper`) | `TASK_STATE_UNSPECIFIED, TASK_STATE_SUBMITTED, TASK_STATE_WORKING, TASK_STATE_COMPLETED, TASK_STATE_FAILED, TASK_STATE_CANCELED, TASK_STATE_INPUT_REQUIRED, TASK_STATE_REJECTED, TASK_STATE_AUTH_REQUIRED` (0–8) |
| v0.3 spec (pydantic) | `a2a.compat.v0_3.types.TaskState` (`str, Enum`)    | `submitted, working, input-required, completed, canceled, failed, rejected, auth-required, unknown`                                                                                                              |
| v0.3 proto (gRPC)    | `a2a.compat.v0_3.a2a_v0_3_pb2.TaskState`           | `TASK_STATE_…` but **`TASK_STATE_CANCELLED` (double-L)** vs v1's single-L `CANCELED`                                                                                                                             |

**On the wire jim exposes (v0.3 JSON-RPC): the pydantic values** —
`submitted / working / input-required / completed / canceled / failed /
rejected / auth-required`. The payment pause state is **`input-required`**.
Executor code uses the _proto_ enum (`TaskState.TASK_STATE_INPUT_REQUIRED`,
passed to `TaskUpdater.requires_input()` / `update_status()`); the compat layer
maps it to `input-required` on the JSON-RPC wire.

### Import paths

```
a2a.types                       -> AgentCard, AgentCapabilities, AgentSkill, AgentInterface,
                                    AgentExtension, AgentProvider, Task, TaskStatus, Message,
                                    Part, Role, TaskState, TaskStatusUpdateEvent,
                                    TaskArtifactUpdateEvent, Artifact, TaskPushNotificationConfig,
                                    AuthenticationInfo, SendMessageRequest/Response  (ALL protobuf)
a2a.types.a2a_pb2               -> the raw proto module (PushNotificationConfig is HERE-only, see below)
a2a.compat.v0_3.types           -> v0.3 pydantic models (TaskState, TextPart/DataPart/FilePart, …)
a2a.server.agent_execution      -> AgentExecutor, RequestContext
a2a.server.events               -> EventQueue
a2a.server.tasks                -> TaskUpdater, TaskStore, InMemoryTaskStore, DatabaseTaskStore,
                                    PushNotificationConfigStore, InMemoryPushNotificationConfigStore,
                                    BasePushNotificationSender
a2a.server.request_handlers     -> DefaultRequestHandler
a2a.server.routes               -> create_agent_card_routes, create_jsonrpc_routes,
                                    create_rest_routes, add_a2a_routes_to_fastapi,
                                    DefaultServerCallContextBuilder, ServerCallContextBuilder
a2a.extensions.common           -> HTTP_EXTENSION_HEADER (='A2A-Extensions'),
                                    get_requested_extensions, find_extension_by_uri
a2a.utils                       -> AGENT_CARD_WELL_KNOWN_PATH, DEFAULT_RPC_URL, constants
a2a.utils.constants             -> PROTOCOL_VERSION_1_0='1.0', PROTOCOL_VERSION_0_3='0.3',
                                    PROTOCOL_VERSION_CURRENT='1.0', VERSION_HEADER='A2A-Version',
                                    AGENT_CARD_WELL_KNOWN_PATH='/.well-known/agent-card.json',
                                    DEFAULT_RPC_URL='/', JSONRPC_PARSE_ERROR_CODE=-32700
```

### Mount signatures (real)

```python
create_agent_card_routes(agent_card, card_modifier=None,
                         card_url='/.well-known/agent-card.json') -> list[Route]
create_jsonrpc_routes(request_handler, rpc_url, context_builder=None,
                      enable_v0_3_compat=False) -> list[Route]           # rpc_url REQUIRED
create_rest_routes(request_handler, context_builder=None,
                   enable_v0_3_compat=False, path_prefix='') -> list[BaseRoute]
add_a2a_routes_to_fastapi(app, *, agent_card_routes=None,
                          jsonrpc_routes=None, rest_routes=None) -> None  # route lists keyword-only
```

Mount recipe (existing FastAPI app; JSON-RPC at `/a2a/jsonrpc`, REST under `/a2a/rest`):

```python
handler = DefaultRequestHandler(agent_executor=..., task_store=..., agent_card=card)
add_a2a_routes_to_fastapi(
    app,
    agent_card_routes=create_agent_card_routes(agent_card=card),
    jsonrpc_routes=create_jsonrpc_routes(request_handler=handler,
                                         rpc_url="/a2a/jsonrpc", enable_v0_3_compat=True),
    rest_routes=create_rest_routes(request_handler=handler,
                                   path_prefix="/a2a/rest", enable_v0_3_compat=True),
)
```

### DefaultRequestHandler — constructor

```python
DefaultRequestHandler(agent_executor, task_store, agent_card,          # 3 REQUIRED positional
                      queue_manager=None, push_config_store=None, push_sender=None,
                      request_context_builder=None, extended_agent_card=None,
                      extended_card_modifier=None)
```

`agent_card` is now required (was not in 0.3.x). Push-config CRUD methods only
function when `push_config_store` is supplied.

### AgentExecutor / RequestContext / EventQueue / TaskUpdater

```python
class AgentExecutor(ABC):
    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None
    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None

# RequestContext read-only props the executor uses:
#   message, task_id, context_id, current_task, configuration, metadata,
#   related_tasks, requested_extensions (set[str] from the A2A-Extensions header),
#   call_context, tenant
# EventQueue: only  await event_queue.enqueue_event(event)

TaskUpdater(event_queue, task_id, context_id,
            artifact_id_generator=None, message_id_generator=None)
# lifecycle (each enqueues a TaskStatusUpdateEvent; each takes optional message=Message):
#   submit(), start_work(), complete(), failed(), reject(), cancel(),
#   requires_input(), requires_auth()
#   update_status(state, message=None, timestamp=None, metadata=None)   # state = proto enum
#   add_artifact(parts: list[Part], artifact_id=None, name=None, metadata=None,
#                append=None, last_chunk=None, extensions=None)
#   new_agent_message(parts: list[Part], metadata=None) -> Message
```

**Executor MUST enqueue an initial `Task` before the first status update:**

```python
async def execute(self, context, event_queue):
    if context.current_task is None:
        await event_queue.enqueue_event(
            Task(id=context.task_id, context_id=context.context_id,
                 status=TaskStatus(state=TaskState.TASK_STATE_SUBMITTED)))
    updater = TaskUpdater(event_queue, context.task_id, context.context_id)
    await updater.start_work()
    await updater.add_artifact([Part(text="...")], name="memo")
    await updater.complete()
```

### Message / Part / Artifact — proto shapes and the two wire dialects

- v1 proto `Message` fields: `message_id, context_id, task_id, role, **parts**,
metadata, extensions, reference_task_ids`.
- v1 proto `Part` is a **oneof `content`** over `text | raw | data | url` (+
  `metadata, filename, media_type`). **There is NO `kind`/`type` discriminator
  field** in v1 — the _present key_ discriminates.
- v0.3 proto `Message` names the parts field **`content`** (not `parts`), and
  `Part` is oneof `part` over `text | file | data`.

Wire (v0.3 JSON-RPC — what jim serves): a text part is
`{"kind": "text", "text": "..."}`; discriminator field is **`kind`**
(`text` / `data` / `file`). Artifact: `{"artifactId", "name", "parts": [...]}`.

### AgentCard — proto fields + alias behavior

Fields (Python snake_case → JSON `json_name` camelCase):
`name, description, supported_interfaces→supportedInterfaces, provider, version,
documentation_url→documentationUrl, capabilities, security_schemes, security_requirements,
default_input_modes→defaultInputModes, default_output_modes→defaultOutputModes,
skills, signatures, icon_url→iconUrl`.

- `AgentInterface`: `url, protocol_binding→protocolBinding, tenant,
protocol_version→protocolVersion`. (Confirms 1.0's `supportedInterfaces[]`
  replaced `url`/`preferredTransport`/`additionalInterfaces`.)
- `AgentCapabilities`: `streaming, push_notifications→pushNotifications,
extensions[] (AgentExtension), extended_agent_card→extendedAgentCard`.
- `AgentExtension`: `uri, description, required, params`.
- `AgentSkill`: `id, name, description, tags, examples,
input_modes→inputModes, output_modes→outputModes, security_requirements`.

**Alias behavior (proto analog of pydantic `by_alias`):**
`MessageToDict(card)` → camelCase json_name (`supportedInterfaces`,
`protocolBinding`); `MessageToDict(card, preserving_proto_field_name=True)` →
snake_case. The agent-card route and all JSON-RPC responses use the camelCase
(default) form. **proto3 has no required fields** — nothing is validated as
required at the proto layer; A2A-required-field enforcement is jim's job.

### Event models

`TaskStatusUpdateEvent`: `task_id→taskId, context_id→contextId, status, final,
metadata`. `TaskArtifactUpdateEvent`: `task_id→taskId, context_id→contextId,
artifact, append, last_chunk→lastChunk, metadata`. Streaming final event on the
wire is `{"kind": "status-update", "final": true, ...}`.

### JSON-RPC method strings + error codes

- **v0.3 (jim serves):** `message/send`, `message/stream`, `tasks/get`,
  `tasks/cancel`, `tasks/resubscribe`,
  `tasks/pushNotificationConfig/{set,get,list,delete}`,
  `agent/getAuthenticatedExtendedCard`. `tasks/get` params: `{"id": "<taskId>"}`.
  A successful `message/send` returns `result` = the Task object directly, with
  `"kind": "task"`.
- **v1.0 native (NOT served by jim):** `SendMessage`, `SendStreamingMessage`,
  `GetTask`, `CancelTask`, `ListTasks`, `SubscribeToTask`,
  `{Create,Get,List,Delete}TaskPushNotificationConfig`, `GetExtendedAgentCard`.
  Requires `A2A-Version: 1.0` header; `result` wraps the task as
  `{"task": {...}}`.
- **Error codes (observed):** `-32700` parse, `-32600` invalid request,
  `-32601` **method not found** (unknown method), `-32602` invalid params,
  `-32603` internal, `-32009` `VERSION_NOT_SUPPORTED`.

### REST binding paths (mounted under `path_prefix`)

- **v0.3 (`/a2a/rest/v1/…`, Google-API style):** `POST v1/message:send`,
  `POST v1/message:stream`, `POST v1/tasks/{id}:cancel`,
  `GET|POST v1/tasks/{id}:subscribe`, `GET v1/tasks/{id}`,
  `… v1/tasks/{id}/pushNotificationConfigs[/{push_id}]`, `GET v1/card`.
  No task-list route (absent from the v0.3 spec by design).
- **v1.0 native (`/a2a/rest/…`):** `message:send`, `tasks/{id}`, `… /tasks`,
  `/extendedAgentCard`, etc. (needs `A2A-Version: 1.0`).
- **v0.3 REST request body quirk:** parsed through the **v0.3 gRPC proto** with
  `ignore_unknown_fields=True`, so the message's parts key must be **`content`**
  (proto name), NOT the spec's `parts`; role is the proto enum string
  `ROLE_USER`; unknown keys are silently dropped. Non-blocking by default —
  returns the initial submitted task; pass `configuration.blocking=true` to get
  the terminal task.

### Push notifications

- v1 core has **no `PushNotificationConfig`** message; the config is
  `TaskPushNotificationConfig` (in `a2a.types`): `tenant, id, task_id→taskId,
url, token, authentication` where `authentication` is
  `AuthenticationInfo{scheme, credentials}`. (Flattened vs v0.3's nested
  `PushNotificationConfig`.)
- `PushNotificationConfigStore` ABC (`a2a.server.tasks`): `get_info(task_id,
context)`, `set_info(task_id, config, context)`, `delete_info(task_id,
context, config_id=None)`, `get_info_for_dispatch(task_id)`.
  `InMemoryPushNotificationConfigStore` implements it.
- `BasePushNotificationSender(httpx_client, config_store, context=None)` with
  `async send_notification(task_id, event)` where `event` is a `Task |
TaskStatusUpdateEvent | TaskArtifactUpdateEvent`.

### Task stores

`TaskStore` ABC methods all take a `ServerCallContext`: `get(task_id, context)
-> Task|None`, `save(task, context)`, `delete(task_id, context)`,
`list(params, context) -> ListTasksResponse`. `DatabaseTaskStore(engine:
AsyncEngine, create_table=True, table_name='tasks', owner_resolver=<default
resolve_user_scope>, core_to_model_conversion=None, model_to_core_conversion=None)`
and it exposes `async initialize()`. Tasks are **owner/tenant-scoped** by the
`owner_resolver` — a task saved under one call-context scope is not visible to a
`get` under a different scope (relevant to jim's recovery sweep in S7).

### x402 extension literals (a2a-x402 v0.1, fetched from source)

- **Extension URI:** `https://github.com/google-a2a/a2a-x402/v0.1`
  — the spec text uses the **`google-a2a`** org even though the repository now
  lives under **`google-agentic-commerce`**. Use the `google-a2a` string as the
  activation URI (it is the protocol identifier, not a fetch URL).
- **Metadata keys:** `x402.payment.status`, `x402.payment.required`,
  `x402.payment.payload`, `x402.payment.receipts`, `x402.payment.error`.
- **Payment-status strings:** `payment-required`, `payment-submitted`,
  `payment-rejected`, `payment-verified`, `payment-completed`, `payment-failed`.
- **Pause TaskState:** `input-required` (merchant pauses here carrying the
  `x402PaymentRequiredResponse`) — aligns with jim's v0.3 wire.

### Extension-activation header (`A2A-Extensions`)

The SDK **reads** it: `a2a/server/routes/common.py` pulls
`request.headers.getlist('A2A-Extensions')` into the ServerCallContext, exposed
as `context.requested_extensions: set[str]` (comma-splitting handled by
`get_requested_extensions`). v0.3 compat also accepts legacy
`X-A2A-Extensions`. **The SDK does NOT echo** activated extensions back in the
response header — emitting the `A2A-Extensions` response header to confirm
activation is **jim's job** (S1/S3).

### httpx `sni_hostname` (for S5 SSRF-safe IP-pinned push delivery)

**Supported.** httpx 0.28.1 / httpcore 1.0.9. `httpcore/_async/connection.py`
line 107 reads `request.extensions.get("sni_hostname", None)` and passes it as
TLS `server_hostname` (line 151); mirrored in the sync + SOCKS backends. So
`await client.post(url, extensions={"sni_hostname": pinned_host})` after
resolving+validating the IP is a supported, plumbed path — no monkeypatching.

## Surprises (reality vs pre-spike research — read before S1–S4)

1. **The SDK is proto-first, not pydantic.** `a2a.types.AgentCard/Message/Part/
Task/TaskState` are **protobuf** messages (`a2a_pb2`), serialized with
   `google.protobuf.json_format.MessageToDict`. `AgentCard.model_fields` /
   `model_dump(by_alias=True)` **do not exist** on core types — use
   `DESCRIPTOR.fields` and `MessageToDict(..., preserving_proto_field_name=…)`.
   Pydantic models exist only under `a2a.compat.v0_3.types`.
2. **Three TaskState enums, and v1 proto default wire is `TASK_STATE_COMPLETED`,
   not `completed`.** The spec `completed`/`input-required` strings only appear
   on the v0.3 (pydantic) JSON-RPC wire. Pin all three (the test does).
3. **v1.0 native requires an `A2A-Version: 1.0` request header** or it returns
   `-32009 VERSION_NOT_SUPPORTED`. The v0.3 compat routes need no header.
4. **v0.3 JSON-RPC and v0.3 REST emit DIFFERENT response wire.** JSON-RPC v0.3 →
   spec (`completed`, `kind:"text"`). REST v0.3 → **v1-native** proto
   (`TASK_STATE_COMPLETED`, oneof parts with **no `kind`**), because the REST
   adapter serializes responses via v0.3-proto `MessageToDict`. ⇒ **lead x402
   with JSON-RPC**; treat REST as best-effort.
5. **v0.3 REST request body uses `content` (proto name), not `parts`**, with
   `ignore_unknown_fields=True` silently dropping mismatches — a spec-`parts`
   body yields "parts must contain at least one element". And REST `message:send`
   is **non-blocking by default** (returns submitted; needs
   `configuration.blocking=true` for terminal).
6. **No `new_task()` helper.** The executor must construct+enqueue the initial
   `Task` proto itself before any status event, or `InvalidAgentResponseError`.
7. **`Part` has no discriminator field in v1** (oneof by present key); the
   `kind` discriminator exists only on the v0.3 pydantic/JSON wire.
8. **`PushNotificationConfig` is not exported** from `a2a.types`; v1 uses the
   flattened `TaskPushNotificationConfig`.
9. **The SDK reads but never echoes `A2A-Extensions`** — activation
   confirmation is ours.
10. Minor: v0.3 **proto** spells it `TASK_STATE_CANCELLED` (double-L) vs v1
    `TASK_STATE_CANCELED`; both pydantic wires use `canceled`.

## Consequences

- **Positive.** Later stages import verbatim from this document against a pinned,
  test-locked SDK. The v0.3-compat decision means jim writes proto-native
  executors once and speaks the exact dialect x402 clients expect, with a wire
  test that fails loudly on any SDK drift. Offline-first is preserved: the
  transport test is fully in-process (httpx `ASGITransport`), no DB/key/network.
- **Negative / accepted.** jim rides the SDK's v0.3 compat layer, which is
  explicitly a bridge; if a future SDK drops it, jim must move to proto-native
  wire (executors are already proto-native, so the blast radius is the mount
  flag + client-facing literals, and the pin + test contain the risk). The
  JSON-RPC/REST response-wire asymmetry means REST is a second-class x402 path
  for now.
- **Deferred (later ADRs/stages).** The task/context/artifact persistence schema
  and recovery sweep (S1c/S7), the x402 payment coordinator state machine (S2),
  the seller mount + card swap (S3), the real research task state machine +
  streaming + cancel (S4), hardened push delivery (S5), and the monitor bridge
  (S6). This ADR fixes only the substrate.

## Alternatives considered

| Alternative                                                            | Why not                                                                                                                                                                                                                                               |
| ---------------------------------------------------------------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Serve v1.0-native wire (`SendMessage`, `TASK_STATE_*`, version header) | The x402 v0.1 extension and today's A2A clients speak v0.3 (`message/send`, `input-required`, `kind`). Native wire would strand every current payer behind a header+dialect negotiation for zero near-term benefit. Revisit when the ecosystem moves. |
| Hand-roll A2A JSON-RPC without the SDK                                 | Re-implements task lifecycle, event queue, SSE, v0.3⇆v1 translation, and card serialization — exactly the churn-prone surface the pinned SDK absorbs.                                                                                                 |
| Pin a version range (`>=1.1,<2`)                                       | Wire literals are load-bearing across five stages; a minor bump silently changing a string would break conformance without a failing build. Exact pin + literal test is the guardrail.                                                                |
| Add the `[postgresql]`/`[sql]` extra for `DatabaseTaskStore`           | Redundant with jim's existing `sqlalchemy[asyncio]` + `asyncpg`; verified the import works without it. Avoids a second sqlalchemy constraint.                                                                                                         |
| Loosen `tests/conftest.py` to exercise A2A against a DB                | Violates the offline-first invariant. The transport test uses `InMemoryTaskStore`; `DatabaseTaskStore` is covered by signature/import verification only until a live exit-run.                                                                        |

## See also

- [ADR-0007](0007-data-source-economics-multichain-macro.md),
  [ADR-0008](0008-agent-economy-trust-callchain-billing.md) — the x402 buy/sell
  and never-bill-rejected invariants the A2A paid task must compose with.
- `tests/test_a2a_transport.py` — the executable pin for every literal above.
- [a2a-x402 v0.1 spec](https://github.com/google-agentic-commerce/a2a-x402/blob/main/spec/v0.1/spec.md)
  (repo under `google-agentic-commerce`; URI string under `google-a2a`).
