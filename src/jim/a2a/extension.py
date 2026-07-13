"""The x402 A2A extension — one place for its wire literals and activation.

Every later A2A stage (payment coordinator, seller mount, executor) hard-codes
the same handful of strings: the extension URI a client activates, the
``x402.payment.*`` metadata keys that carry the payment state machine on task
metadata, the payment-status vocabulary, and the two header spellings clients
use to activate the extension. Scattering those literals across five stages is
how a protocol identifier drifts by one character and silently strands every
payer. So they live here, imported by name, verified once against ADR-0010.

Activation is a header contract, and the a2a-sdk only half-implements it: it
**reads** the canonical ``A2A-Extensions`` request header into
``RequestContext.requested_extensions`` but **never echoes** an activated
extension back on the response (ADR-0010 §"Extension-activation header"). This
module closes both gaps with a pure-ASGI middleware that (a) normalizes the
legacy ``X-A2A-Extensions`` request header into the canonical one so the SDK's
own parser sees it, and (b) echoes our x402 URI on the response so a client can
confirm the merchant honored the extension. Deterministic, header-only, no SDK
import — just bytes on the wire.
"""

from __future__ import annotations

from enum import Enum

# Extension URI (a2a-x402 v0.1). NOTE: the spec text uses the ``google-a2a`` org
# even though the repository now lives under ``google-agentic-commerce`` — this
# string is a *protocol identifier* used verbatim for activation, NOT a URL to
# fetch. Do not "fix" the org; changing it de-activates the extension. (ADR-0010)
X402_EXT_URI = "https://github.com/google-a2a/a2a-x402/v0.1"

# Metadata keys the extension carries on A2A message / task metadata. The
# merchant advertises requirements under ``required``, the client submits proof
# under ``payload``, and the merchant records the outcome under ``status`` /
# ``receipts`` / ``error`` — the full payment state machine, keyed by string.
MD_PAYMENT_STATUS = "x402.payment.status"
MD_PAYMENT_REQUIRED = "x402.payment.required"
MD_PAYMENT_PAYLOAD = "x402.payment.payload"
MD_PAYMENT_RECEIPTS = "x402.payment.receipts"
MD_PAYMENT_ERROR = "x402.payment.error"


class PaymentStatus(str, Enum):
    """The ``x402.payment.status`` vocabulary (a2a-x402 v0.1, ADR-0010).

    A ``str`` enum so members serialize straight onto task metadata / the wire.
    """

    REQUIRED = "payment-required"
    SUBMITTED = "payment-submitted"
    VERIFIED = "payment-verified"
    REJECTED = "payment-rejected"
    COMPLETED = "payment-completed"
    FAILED = "payment-failed"


# Extension-activation headers. The SDK reads the canonical one into
# ``RequestContext.requested_extensions``; v0.3 clients may send the legacy one.
EXT_HEADER = "A2A-Extensions"
LEGACY_EXT_HEADER = "X-A2A-Extensions"


def parse_extension_header(value: str) -> set[str]:
    """Decode a comma-separated ``A2A-Extensions`` value into a URI set.

    Whitespace-tolerant; empty segments (from trailing/doubled commas) drop out.
    """
    if not value:
        return set()
    return {uri.strip() for uri in value.split(",") if uri.strip()}


def is_activated(requested: set[str] | None) -> bool:
    """True iff a request activated the x402 extension."""
    return bool(requested) and X402_EXT_URI in requested


class X402ExtensionEchoMiddleware:
    """Pure-ASGI middleware that closes the SDK's activation-header gaps.

    Modeled on :class:`jim.interop.callchain.CallChainMiddleware` (same pure-ASGI
    style, no Starlette ``BaseHTTPMiddleware`` overhead). Scoped to the A2A mount
    (paths under ``path_prefix``) so it never touches jim's other routes.

    - **Ingress**: if only the legacy ``X-A2A-Extensions`` header is present, its
      value is injected under the canonical ``A2A-Extensions`` name in the ASGI
      scope, so the SDK's own header parsing (which reads only the canonical
      spelling) sees it. This is what "accept the legacy header" means end to end.
    - **Egress**: if the request activated :data:`X402_EXT_URI` (via either
      header), ``A2A-Extensions: <X402_EXT_URI>`` is appended to the response so
      the client can confirm activation — the SDK does not do this itself. Only
      the x402 URI is echoed; other requested extensions are never reflected, and
      nothing is added when x402 was not activated.
    """

    def __init__(self, app, *, path_prefix: str = "/a2a") -> None:
        self.app = app
        self.path_prefix = path_prefix

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http" or not scope.get("path", "").startswith(self.path_prefix):
            await self.app(scope, receive, send)
            return

        canon_key = EXT_HEADER.lower().encode("latin-1")
        legacy_key = LEGACY_EXT_HEADER.lower().encode("latin-1")
        headers = list(scope.get("headers", []))

        canon_val: bytes | None = None
        legacy_val: bytes | None = None
        for name, value in headers:
            lname = name.lower()
            if lname == canon_key and canon_val is None:
                canon_val = value
            elif lname == legacy_key and legacy_val is None:
                legacy_val = value

        # Ingress normalization: surface the legacy header under the canonical
        # name so the SDK parser (canonical-only) can read it. Unconditional —
        # the legacy header may activate x402 or any other extension.
        if canon_val is None and legacy_val is not None:
            headers = [*headers, (canon_key, legacy_val)]
            scope = {**scope, "headers": headers}
            canon_val = legacy_val

        activated = is_activated(parse_extension_header((canon_val or b"").decode("latin-1")))
        if not activated:
            await self.app(scope, receive, send)
            return

        echo = (canon_key, X402_EXT_URI.encode("latin-1"))

        async def send_with_echo(message):
            if message["type"] == "http.response.start":
                message = {**message, "headers": [*message.get("headers", []), echo]}
            await send(message)

        await self.app(scope, receive, send_with_echo)
