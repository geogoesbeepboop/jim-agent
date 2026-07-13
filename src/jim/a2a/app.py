"""Mount the A2A 1.0 bindings into a FastAPI app (S3).

The S0 transport spike (``tests/test_a2a_transport.py``) proved the SDK route
factories against a *standalone* app with an echo executor. S3 promotes that
pattern into jim's real seller: build the request handler + task store once,
mount both v0.3-compat bindings (JSON-RPC at ``/a2a/jsonrpc``, REST under
``/a2a/rest``), and hand the caller an :class:`A2AComponents` bundle it can
stash on ``app.state`` so later stages (the S4 executor swap, the S7 recovery
sweep) and tests can reach the task store.

Two staging seams live here:

- **The executor.** :func:`build_a2a_components` now defaults to the real
  :class:`~jim.a2a.executor.JimAgentExecutor` (S4) — the research task state
  machine. :class:`FakeEchoExecutor` (the S0 echo shape) stays exported and is
  used verbatim when a caller threads ``executor=FakeEchoExecutor()`` for
  transport-only tests; nothing else about the mount changes, because both are
  proto-native and binding-agnostic (ADR-0010 §3).
- **The task store.** ``InMemoryTaskStore`` by default (offline-first — the
  hermetic suite constructs it with no DB), ``DatabaseTaskStore`` when
  ``settings.database_url`` is set. The DB store needs an ``await initialize()``
  to create its table; that is async, so we do NOT call it here — we flag it via
  :attr:`A2AComponents.needs_db_init` and let the seller lifespan await it.

Every load-bearing literal (import paths, mount kwargs, the enqueue-Task-first
executor contract, the ``enable_v0_3_compat=True`` wire flag) is verbatim from
``docs/adr/0010-a2a-durable-paid-tasks.md``.
"""

from __future__ import annotations

from dataclasses import dataclass

from fastapi import FastAPI
from google.protobuf import json_format

from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.server.request_handlers import DefaultRequestHandler
from a2a.server.routes import (
    add_a2a_routes_to_fastapi,
    create_jsonrpc_routes,
    create_rest_routes,
)
from a2a.server.tasks import (
    DatabaseTaskStore,
    InMemoryTaskStore,
    TaskStore,
    TaskUpdater,
)
from a2a.types import AgentCard, Message, Part, Task, TaskState, TaskStatus

from jim.a2a.card import agent_card
from jim.config import Settings
from jim.store.db import get_engine
from jim.store.repo import get_store

# jim's two A2A mount points. JSON-RPC is the primary/x402 binding (only binding
# that emits a fully v0.3-spec response); REST is best-effort parity (ADR-0010).
JSONRPC_URL = "/a2a/jsonrpc"
REST_PREFIX = "/a2a/rest"


def _text_of(message: Message | None) -> str:
    """The concatenated text of a proto ``Message`` (oneof discriminated by key)."""
    if message is None:
        return ""
    return " ".join(p.text for p in message.parts if p.WhichOneof("content") == "text")


class FakeEchoExecutor(AgentExecutor):
    """Transport-only echo executor — the S0 echo shape, kept for tests.

    Enqueues the initial ``Task`` proto before any status update (there is no
    ``new_task()`` helper in 1.1.0; a status event first raises
    ``InvalidAgentResponseError`` — ADR-0010 §3/#6), then drives the lifecycle
    through a ``TaskUpdater``: ``start_work`` → ``add_artifact`` (echoing the
    input text) → ``complete``. **No longer the default** (S4 swapped in the real
    :class:`~jim.a2a.executor.JimAgentExecutor`); pass ``executor=FakeEchoExecutor()``
    to exercise pure transport parity without the payment state machine.
    """

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
        await updater.add_artifact([Part(text=f"echo: {_text_of(context.message)}")], name="echo")
        await updater.complete()

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        updater = TaskUpdater(event_queue, context.task_id, context.context_id)
        await updater.cancel()


@dataclass
class A2AComponents:
    """The wired A2A surface for one seller app — stashed on ``app.state.a2a``.

    ``needs_db_init`` is True exactly when ``task_store`` is a ``DatabaseTaskStore``
    that still needs its ``await initialize()`` (the seller lifespan owns that
    call, keeping this factory synchronous and the DB-less path await-free).
    """

    task_store: TaskStore
    request_handler: DefaultRequestHandler
    executor: AgentExecutor
    agent_card: AgentCard
    needs_db_init: bool


def _proto_card(settings: Settings) -> AgentCard:
    """The SDK-validated proto ``AgentCard`` the request handler needs.

    Reuses :func:`jim.a2a.card.agent_card` (which the well-known route also
    serves) and parses its JSON dict back into a proto — so the handler's card
    and the served card come from one source of truth. The base URL only shapes
    the URL fields; the served route reflects the request host per call.
    """
    return json_format.ParseDict(agent_card(settings.public_url), AgentCard())


def _build_research_executor(settings: Settings, *, rail=None) -> AgentExecutor:
    """Wire the real research executor over jim's app store + the x402 rail.

    Constructed at build time (like the seller's ``get_store()`` use) so the
    executor, payment coordinator, and withheld-artifact staging all share the one
    process store singleton — which tests reset between cases. ``X402Rail``
    construction is offline (facilitator I/O is lazy), so this stays hermetic.
    """
    # Local imports: keep the module import-light and avoid a payments↔app cycle.
    from jim.a2a.crypto import A2ACrypto
    from jim.a2a.executor import JimAgentExecutor
    from jim.a2a.payments import X402PaymentCoordinator, X402Rail
    from jim.a2a.stores import PaymentAuths, WithheldArtifacts

    store = get_store()
    crypto = A2ACrypto(settings)
    auths = PaymentAuths(store, crypto)
    withheld = WithheldArtifacts(store, crypto)
    rail = rail or X402Rail(settings)
    coordinator = X402PaymentCoordinator(
        auths=auths, rail=rail, store=store, settings=settings
    )
    return JimAgentExecutor(auths, withheld, coordinator, settings)


def build_a2a_components(
    settings: Settings,
    *,
    executor: AgentExecutor | None = None,
    rail=None,
    task_store: TaskStore | None = None,
    push_config_store=None,
    push_sender=None,
) -> A2AComponents:
    """Build (but do not mount) the A2A request handler + task store.

    ``executor`` defaults to the **real** :class:`~jim.a2a.executor.JimAgentExecutor`
    (S4): the research task state machine composing S1's parser, S2's payment
    coordinator, and the withheld-artifact staging over jim's ``get_store()``
    (the same store the seller records receipts to — matching how the seller
    constructs it). ``rail`` swaps the x402 :class:`~jim.a2a.payments.PaymentRail`
    (defaults to the production :class:`~jim.a2a.payments.X402Rail`; tests inject a
    ``FakeRail`` so the suite stays offline). :class:`FakeEchoExecutor` remains
    exported for transport-only tests and is used verbatim when passed as
    ``executor=``. ``task_store`` defaults to ``DatabaseTaskStore`` when
    ``settings.database_url`` is set (its async ``initialize()`` is deferred — see
    :attr:`A2AComponents.needs_db_init`) and ``InMemoryTaskStore`` otherwise. Push
    args are omitted from the handler when ``None`` so push-config CRUD stays inert
    until S5 wires it.
    """
    if executor is None:
        executor = _build_research_executor(settings, rail=rail)

    needs_db_init = False
    if task_store is None:
        if settings.database_url:
            task_store = DatabaseTaskStore(get_engine(settings.database_url))
            needs_db_init = True
        else:
            task_store = InMemoryTaskStore()

    card = _proto_card(settings)

    handler_kwargs: dict = {
        "agent_executor": executor,
        "task_store": task_store,
        "agent_card": card,
    }
    if push_config_store is not None:
        handler_kwargs["push_config_store"] = push_config_store
    if push_sender is not None:
        handler_kwargs["push_sender"] = push_sender
    request_handler = DefaultRequestHandler(**handler_kwargs)

    return A2AComponents(
        task_store=task_store,
        request_handler=request_handler,
        executor=executor,
        agent_card=card,
        needs_db_init=needs_db_init,
    )


def mount_a2a(app: FastAPI, components: A2AComponents) -> None:
    """Mount both v0.3-compat A2A bindings onto ``app`` (ADR-0010 mount recipe).

    No ``agent_card_routes``: jim serves the card from its own well-known route
    (``GET /.well-known/agent-card.json``, base-URL reflected) rather than the
    SDK's static card route.

    Reality-vs-sketch (ADR-0010 said "verify the exact prefix mechanics"):
    ``create_rest_routes`` appends a **greedy ``/{tenant}`` catch-all ``Mount``**
    (the v1-native multi-tenant binding) as its last route. jim advertises only
    ``/a2a/jsonrpc`` + ``/a2a/rest`` and is single-tenant, and that mount would
    shadow *every* non-``/a2a`` path (``/health``, ``/ping``, the well-known
    routes, …). We drop it — confining the whole A2A surface to explicit
    ``/a2a`` paths keeps every legacy route (and unknown-path 404s) byte-identical.
    """
    jsonrpc_routes = create_jsonrpc_routes(
        request_handler=components.request_handler,
        rpc_url=JSONRPC_URL,
        enable_v0_3_compat=True,
    )
    rest_routes = [
        route
        for route in create_rest_routes(
            request_handler=components.request_handler,
            path_prefix=REST_PREFIX,
            enable_v0_3_compat=True,
        )
        if getattr(route, "path", "").startswith(REST_PREFIX)
    ]
    add_a2a_routes_to_fastapi(app, jsonrpc_routes=jsonrpc_routes, rest_routes=rest_routes)
