"""The real A2A research executor — the durable, paid research task state machine (S4).

This is where ADR-0010's substrate, S1's deterministic parser, and S2's payment
coordinator compose into the actual agent: a client commissions cited research,
the task pauses at ``input-required`` carrying an x402 challenge, the client pays,
and jim gathers → synthesizes → gates → (only then) settles → publishes the memo.

The state machine (``execute``):

1. **Extension gate.** No x402 extension activated → reject; never quote a price.
2. **Payment continuation** (``current_task`` + an ``x402.payment.payload`` on the
   message metadata) → verify the payment, and on success run the engine.
3. **Fresh message** → parse deterministically (S1). Research → quote + pause at
   ``input-required``. Monitor → refused until S6 wires the bridge. Bad input →
   reject with the structured grammar error.
4. **After a verified payment** the engine runs; an *ok* result is **staged
   encrypted first, then settled, then published** (crash between settle and
   publish is recoverable — S7); a *rejected* run is refused with counts-only
   diagnostics and **never settled** (ADR-0008); an error fails without billing.
5. **Cancel** races settlement via the auth CAS: won → cancel is safe; lost →
   the SDK's "task not cancelable" error, and settlement completes.

**Where the payment metadata lives on the wire.** ``TaskUpdater.requires_input``
takes no ``metadata`` argument, and the SDK reduces a ``TaskStatusUpdateEvent``
by *merging* ``event.metadata`` into the persisted ``Task.metadata`` (top level)
while the status message rotates into history on the next update
(``a2a/server/tasks/task_manager.py``). So every payment state is stamped via
``update_status(state, message=…, metadata=…)``: the ``x402.payment.*`` keys land
durably on ``Task.metadata`` and are readable both from the ``message/send``
response and from ``tasks/get`` afterward. Verified empirically against the
pinned SDK (see ``tests/test_a2a_research_flow.py``).

**The model proposes, deterministic code disposes.** The engine only produces a
memo; every money move and every published figure passes the coordinator's CAS
and the sourcing gate first.
"""

from __future__ import annotations

import asyncio
import contextlib
import json

from google.protobuf import json_format

from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.events import EventQueue
from a2a.server.tasks import TaskUpdater
from a2a.types import Message, Part, Task, TaskState, TaskStatus

from jim.a2a.artifacts import artifact_parts, rejection_details, research_artifact
from jim.a2a.extension import (
    EXT_HEADER,
    MD_PAYMENT_ERROR,
    MD_PAYMENT_PAYLOAD,
    MD_PAYMENT_STATUS,
    PaymentStatus,
    is_activated,
)
from jim.a2a.inputs import InputRejected, ParsedMonitor, ParsedResearch, parse_message_parts
from jim.a2a.payments import SettleStatus, SubmitStatus, X402PaymentCoordinator
from jim.a2a.stores import AuthView, PaymentAuths, WithheldArtifacts
from jim.config import Settings
from jim.research import engine

# Running engine coroutines, keyed by task_id, so ``cancel`` can stop the work of
# a task whose payment it just CAS-won. Module-level (not per-executor) so it is
# reachable regardless of which executor instance the SDK routes cancel through;
# every entry is removed in the ``finally`` of the run that created it.
_RUNNING: dict[str, asyncio.Task] = {}

_MONITOR_KINDS = ("monitor_activation", "monitor_release")


class JimAgentExecutor(AgentExecutor):
    """The proto-native research task executor (binding-agnostic, ADR-0010 §3)."""

    def __init__(
        self,
        auths: PaymentAuths,
        withheld: WithheldArtifacts,
        coordinator: X402PaymentCoordinator,
        settings: Settings,
        monitor_handler=None,
    ) -> None:
        self._auths = auths
        self._withheld = withheld
        self._coordinator = coordinator
        self._settings = settings
        # S6 wires this: an async callable ``(context, updater, auth|None, parsed
        # |payload) -> None`` that owns monitor activation/withhold/release. While
        # it is None, monitor tasks are refused (fresh) or reported inert (continue).
        self._monitor_handler = monitor_handler

    # -- dispatch --------------------------------------------------------------

    async def execute(self, context: RequestContext, event_queue: EventQueue) -> None:
        updater = TaskUpdater(event_queue, context.task_id, context.context_id)

        # 1. Extension gate — never proceed, never quote, without x402 activation.
        if not is_activated(context.requested_extensions):
            await self._ensure_task(context, event_queue)
            await updater.update_status(
                TaskState.TASK_STATE_REJECTED,
                message=self._msg(
                    updater,
                    "the x402 payment extension is required: activate it by sending the "
                    f"'{EXT_HEADER}' request header before commissioning a paid task.",
                ),
            )
            return

        # 2. Payment continuation: an existing task carrying a payment payload.
        payload = self._payment_payload(context.message)
        if context.current_task is not None and payload is not None:
            await self._continue_payment(context, updater, payload)
            return

        # 3. Fresh message — deterministic parse (no model in front of the paywall).
        try:
            parsed = parse_message_parts(
                self._wire_parts(context.message),
                monitor_min_interval_seconds=self._settings.a2a_monitor_min_interval_seconds,
            )
        except InputRejected as exc:
            await self._ensure_task(context, event_queue)
            await updater.update_status(
                TaskState.TASK_STATE_REJECTED,
                message=self._msg(
                    updater,
                    json.dumps(
                        {"code": exc.code, "message": exc.message, "grammar": exc.grammar}
                    ),
                ),
            )
            return

        if isinstance(parsed, ParsedMonitor):
            if self._monitor_handler is None:
                await self._ensure_task(context, event_queue)
                await updater.update_status(
                    TaskState.TASK_STATE_REJECTED,
                    message=self._msg(
                        updater, "monitor tasks are not yet enabled on this deployment"
                    ),
                )
                return
            await self._monitor_handler(context, updater, None, parsed)
            return

        # ParsedResearch → quote + pause at input-required carrying the challenge.
        await self._quote_research(context, updater, event_queue, parsed)

    # -- quote path ------------------------------------------------------------

    async def _quote_research(
        self,
        context: RequestContext,
        updater: TaskUpdater,
        event_queue: EventQueue,
        parsed: ParsedResearch,
    ) -> None:
        await self._ensure_task(context, event_queue)
        metadata = await self._coordinator.quote(
            task_id=context.task_id,
            kind="research",
            product=parsed.product,
            identifier=parsed.identifier,
            mode=parsed.mode,
        )
        auth = await self._auths.get(context.task_id)
        price = auth.amount_usd if auth is not None else None
        # Metadata rides on the status event so x402.payment.{status,required} land
        # durably on Task.metadata (readable from message/send AND tasks/get).
        await updater.update_status(
            TaskState.TASK_STATE_INPUT_REQUIRED,
            message=self._msg(
                updater,
                f"payment required: ${price} for {parsed.product} {parsed.identifier}",
            ),
            metadata=metadata,
        )

    # -- payment continuation --------------------------------------------------

    async def _continue_payment(
        self, context: RequestContext, updater: TaskUpdater, payload: dict
    ) -> None:
        auth = await self._auths.get(context.task_id)
        if auth is None:
            # A payload for a task with no pending authorization — malformed or
            # already-terminal continuation. Fail without touching money.
            await updater.update_status(
                TaskState.TASK_STATE_FAILED,
                message=self._msg(updater, "no pending payment for this task"),
                metadata={
                    MD_PAYMENT_STATUS: PaymentStatus.FAILED.value,
                    MD_PAYMENT_ERROR: "no pending payment authorization",
                },
            )
            return

        if auth.kind in _MONITOR_KINDS:
            if self._monitor_handler is None:
                # Leave the task state unchanged (still input-required): a monitor
                # payment can't be honored until S6, but it isn't a hard failure.
                await updater.update_status(
                    TaskState.TASK_STATE_INPUT_REQUIRED,
                    message=self._msg(
                        updater, "monitor payments are not enabled on this deployment yet"
                    ),
                )
                return
            await self._monitor_handler(context, updater, auth, payload)
            return

        await self._research_payment(context, updater, auth, payload)

    async def _research_payment(
        self,
        context: RequestContext,
        updater: TaskUpdater,
        auth: AuthView,
        payload: dict,
    ) -> None:
        task_id = context.task_id
        outcome = await self._coordinator.submit(task_id=task_id, payload=payload)

        if outcome.status is SubmitStatus.FAILED:
            # Retryable: the auth stays "required", so the client can resubmit a
            # corrected payment on the same still-paused task.
            await updater.update_status(
                TaskState.TASK_STATE_INPUT_REQUIRED,
                message=self._msg(
                    updater, f"payment rejected: {outcome.reason or 'verification failed'}"
                ),
                metadata=outcome.metadata,
            )
            return
        if outcome.status is SubmitStatus.EXPIRED:
            await updater.update_status(
                TaskState.TASK_STATE_FAILED,
                message=self._msg(updater, "payment window expired; nothing was billed"),
                metadata=outcome.metadata,
            )
            return

        # VERIFIED → do the paid work. Run the engine as a tracked task so cancel
        # can stop it if it CAS-wins the race before settlement starts.
        await updater.update_status(
            TaskState.TASK_STATE_WORKING,
            message=self._msg(updater, "payment verified; gathering sources and synthesizing"),
            metadata=outcome.metadata,
        )

        run_task = asyncio.ensure_future(
            engine.run_research(
                auth.identifier,
                product=auth.product,
                mode=auth.mode,
                price_out_usd=auth.amount_usd,
            )
        )
        _RUNNING[task_id] = run_task
        try:
            result = await run_task
        except asyncio.CancelledError:
            # cancel() (or a client disconnect) aborted the run before it finished:
            # stop the engine (a bare disconnect never called cancel(), so nothing
            # else would), never settle, drop the withheld staging, mark canceled.
            run_task.cancel()
            await self._coordinator.discard(task_id=task_id, reason="cancelled")
            await self._withheld.discard(task_id)
            with contextlib.suppress(Exception):
                await updater.cancel()
            raise
        finally:
            _RUNNING.pop(task_id, None)

        await self._finish_research(updater, auth, result, task_id)

    async def _finish_research(
        self, updater: TaskUpdater, auth: AuthView, result, task_id: str
    ) -> None:
        if result.status == "ok":
            await self._settle_and_publish(updater, auth, result, task_id)
        elif result.status == "rejected":
            # Gate/judge refused it → never settle; diagnostics counts only, no memo.
            await self._coordinator.discard(task_id=task_id, reason="gate_rejected")
            await updater.update_status(
                TaskState.TASK_STATE_REJECTED,
                message=self._msg(updater, json.dumps(rejection_details(result))),
                metadata={
                    MD_PAYMENT_STATUS: PaymentStatus.REJECTED.value,
                    MD_PAYMENT_ERROR: "research rejected by jim's verification gates; not billed",
                },
            )
        else:  # "error"
            await self._coordinator.discard(task_id=task_id, reason="engine_error")
            await updater.update_status(
                TaskState.TASK_STATE_FAILED,
                message=self._msg(
                    updater, f"research could not be completed: {result.error or 'engine error'}"
                ),
                metadata={
                    MD_PAYMENT_STATUS: PaymentStatus.REJECTED.value,
                    MD_PAYMENT_ERROR: result.error or "engine error",
                },
            )

    async def _settle_and_publish(
        self, updater: TaskUpdater, auth: AuthView, result, task_id: str
    ) -> None:
        # Stage the memo ENCRYPTED before the money moves: if the process dies
        # between settle and publish, the paid artifact is recoverable (S7) and
        # was never exposed unpaid.
        payload = research_artifact(result)
        await self._withheld.hold(
            task_id=task_id,
            monitor_id="",
            severity="",
            as_of=result.as_of,
            price_usd=auth.amount_usd,
            payload=payload,
        )
        await updater.update_status(
            TaskState.TASK_STATE_WORKING,
            message=self._msg(updater, "verification gates passed; settling payment"),
        )

        # Settle + publish is the money-moving critical section. The SDK's
        # ``active_task.cancel`` cancels the producer (this coroutine) BEFORE it
        # calls our ``cancel`` (a2a-sdk ``active_task.py``), so a cancel arriving
        # while the settlement is in flight would otherwise abort a payment that is
        # already moving. Shield it: on producer cancellation the shielded task
        # keeps running, and we await it so the artifact publishes and the task
        # reaches its true terminal state (settled) rather than a torn one.
        critical = asyncio.ensure_future(
            self._settle_publish_critical(updater, auth, result, task_id, payload)
        )
        try:
            await asyncio.shield(critical)
        except asyncio.CancelledError:
            # Producer is being cancelled mid-settlement: let the money move finish.
            with contextlib.suppress(Exception):
                await critical
            # Swallow: settlement won the race; the task terminated via ``critical``.

    async def _settle_publish_critical(
        self, updater: TaskUpdater, auth: AuthView, result, task_id: str, payload: dict
    ) -> None:
        settle = await self._coordinator.settle(task_id=task_id)
        if settle.status is SettleStatus.ALREADY_SETTLING:
            # Another path owns the completion (e.g. a recovery sweep) — do not
            # double-publish; the winner releases the artifact and completes.
            return
        if settle.status is not SettleStatus.SETTLED:
            # Settlement failed/expired AFTER staging → drop the withheld artifact,
            # fail the task, publish nothing, record no receipt.
            await self._withheld.discard(task_id)
            await updater.update_status(
                TaskState.TASK_STATE_FAILED,
                message=self._msg(updater, "payment settlement failed; nothing was billed"),
                metadata=settle.metadata,
            )
            return

        # Settled → release the paid artifact and publish it.
        released = await self._withheld.release(task_id)
        await updater.add_artifact(artifact_parts(released or payload), name="research")
        await updater.update_status(
            TaskState.TASK_STATE_COMPLETED,
            message=self._msg(updater, "research complete; payment settled"),
            metadata=settle.metadata,
        )

    # -- cancel ----------------------------------------------------------------

    async def cancel(self, context: RequestContext, event_queue: EventQueue) -> None:
        """Cancel racing settlement through the auth CAS (ADR-0010 cancel rule).

        The SDK's ``active_task.cancel`` cancels the producer task *first*, then
        calls this — and if this **raises**, the SDK marks the task FAILED and
        surfaces an internal error. So we never raise: we arbitrate purely on the
        auth state and let the shielded settlement (``_settle_and_publish``) win an
        in-flight race.

        - Pre-payment (no auth / still ``required``) → discard + cancel is safe.
        - Payment verified → CAS ``verified→discarded``: won means settlement had
          not started, so cancel the running engine and the task. A lost CAS, or a
          ``settling``/``settled`` auth, means the money is already moving/moved —
          we leave the task alone and the shielded settlement drives it to its true
          terminal (completed, settled), so ``tasks/cancel`` returns that task
          instead of a spurious cancellation. Money integrity is preserved either
          way (never bill a cancel; never abort a committed settlement).
        """
        updater = TaskUpdater(event_queue, context.task_id, context.context_id)
        task_id = context.task_id
        auth = await self._auths.get(task_id)

        if auth is None or auth.status == "required":
            await self._coordinator.discard(task_id=task_id, reason="cancelled")
            await self._withheld.discard(task_id)
            with contextlib.suppress(Exception):
                await updater.cancel()
            return

        if auth.status == "verified" and await self._auths.cas(
            task_id, "verified", "discarded"
        ):
            run_task = _RUNNING.get(task_id)
            if run_task is not None:
                run_task.cancel()
            await self._withheld.discard(task_id)
            with contextlib.suppress(Exception):
                await updater.cancel()
            return

        # settling / settled / CAS-lost verified → money is in flight or moved;
        # do NOT touch the task — the shielded settlement completes it (settled).

    # -- helpers ---------------------------------------------------------------

    async def _ensure_task(self, context: RequestContext, event_queue: EventQueue) -> None:
        """Enqueue the initial ``Task`` proto before any status update — there is
        no ``new_task()`` helper in the SDK and a status event first raises
        ``InvalidAgentResponseError`` (ADR-0010 §3/#6)."""
        if context.current_task is None:
            await event_queue.enqueue_event(
                Task(
                    id=context.task_id,
                    context_id=context.context_id,
                    status=TaskStatus(state=TaskState.TASK_STATE_SUBMITTED),
                )
            )

    @staticmethod
    def _msg(updater: TaskUpdater, text: str) -> Message:
        return updater.new_agent_message([Part(text=text)])

    @staticmethod
    def _payment_payload(message: Message | None) -> dict | None:
        """Extract an ``x402.payment.payload`` dict from the message metadata, or
        ``None`` if absent — the signal that this send is a payment continuation."""
        if message is None or not message.HasField("metadata"):
            return None
        md = json_format.MessageToDict(message.metadata)
        payload = md.get(MD_PAYMENT_PAYLOAD)
        return payload if isinstance(payload, dict) else None

    @staticmethod
    def _wire_parts(message: Message | None) -> list[dict]:
        """Convert a proto ``Message``'s parts to the v0.3 wire dicts the S1 parser
        consumes (``{"kind": "text"|"data"|...}``). The proto ``Part`` is a oneof
        discriminated by the present key; any non-text/data part surfaces with its
        proto key so the parser refuses it deterministically."""
        if message is None:
            return []
        parts: list[dict] = []
        for part in message.parts:
            which = part.WhichOneof("content")
            if which == "text":
                parts.append({"kind": "text", "text": part.text})
            elif which == "data":
                parts.append({"kind": "data", "data": json_format.MessageToDict(part.data)})
            else:
                # raw / url / unset → not an accepted intent carrier; let the parser
                # reject (file parts and unknown kinds are refused there).
                parts.append({"kind": which or "unknown"})
        return parts
