"""Dual-mode Claude auth: one uniform client, two backends.

jim's engine LLM calls (synthesize / judge / debate) go through a single
factory so the auth mechanism is a configuration choice, not something baked
into five call sites:

  - ``api_key``      → the raw ``anthropic`` SDK (``AsyncAnthropic.messages.create``).
                       The only ToS-sanctioned path for jim's paid, third-party-
                       facing output. The production default.
  - ``subscription`` → the Claude Agent SDK, which spawns the ``claude`` CLI and
                       authenticates with your ``claude login`` session /
                       ``CLAUDE_CODE_OAUTH_TOKEN``. Anthropic sanctions subscription
                       auth **only for your own individual use** — running evals /
                       local ``jim-research`` without spending API credits. It must
                       never back the seller or monitor paths (see ``pin_api_key_mode``).
  - ``auto``         → subscription when a credential is detectable, else api_key.

Both backends expose the same coroutine::

    resp = await client.complete(model=..., system=..., user=..., max_tokens=...)
    resp.text, resp.usage, resp.stop_reason

so callers are agnostic to the transport. ``system`` is passed as a plain string;
the api_key backend wraps it in the ephemeral cache_control block (prompt caching
preserved), the subscription backend passes it as the CLI system prompt.
"""

from __future__ import annotations

import dataclasses
import importlib.util
import os
from dataclasses import dataclass
from typing import Protocol

from anthropic import AsyncAnthropic  # module-level → patchable seam in tests

from jim.config import get_settings
from jim.research.cost import Usage

VALID_MODES = ("api_key", "subscription", "auto")

# Process-level pin. The seller app + monitor scheduler call ``pin_api_key_mode``
# at startup so ``subscription``/``auto`` can never take effect for third-party-
# facing paid output, regardless of how the environment is configured. This is a
# deterministic guard in the spirit of AGENTS.md's "the model proposes,
# deterministic code disposes".
_PINNED_API_KEY = False

# Per-invocation override (e.g. `jim-eval --auth-mode subscription`, `jim-research
# --auth-mode`). Set once at the start of a dev-loop command; the api_key pin still
# wins over it, so it can never re-enable subscription in a pinned production process.
_MODE_OVERRIDE: str | None = None


def pin_api_key_mode() -> None:
    """Force api_key auth for the rest of this process (seller/monitor startup)."""
    global _PINNED_API_KEY
    _PINNED_API_KEY = True


def set_auth_mode(mode: str | None) -> None:
    """Override the configured auth mode for this process. ``None`` clears it."""
    global _MODE_OVERRIDE
    if mode is not None and mode not in VALID_MODES:
        raise ValueError(f"invalid llm auth mode {mode!r}; choose from {VALID_MODES}")
    _MODE_OVERRIDE = mode


def _agent_sdk_importable() -> bool:
    return importlib.util.find_spec("claude_agent_sdk") is not None


def _oauth_token() -> str | None:
    settings = get_settings()
    return settings.claude_code_oauth_token or os.environ.get("CLAUDE_CODE_OAUTH_TOKEN") or None


def subscription_available() -> bool:
    """Best-effort: is a subscription credential usable for auto-mode selection?

    A ``CLAUDE_CODE_OAUTH_TOKEN`` (from ``claude setup-token``) is the reliable,
    cross-platform signal. A plain ``claude login`` session stores creds the CLI
    reads directly — on Linux/Windows that's a ``.credentials.json`` we can see;
    on macOS it lives in the Keychain, which we can't cheaply probe. So on macOS a
    Keychain-only login won't be auto-detected: export a token or pass
    ``--auth-mode subscription`` explicitly. (Explicit subscription mode never
    consults this — it just runs and lets the CLI resolve auth.)
    """
    if _oauth_token():
        return True
    from pathlib import Path

    cfg = os.environ.get("CLAUDE_CONFIG_DIR") or str(Path.home() / ".claude")
    return Path(cfg, ".credentials.json").exists()


def resolve_mode(explicit: str | None = None) -> str:
    """Resolve the effective auth mode. The api_key pin always wins."""
    mode = (explicit or _MODE_OVERRIDE or get_settings().llm_auth_mode or "api_key").lower()
    if mode not in VALID_MODES:
        raise ValueError(f"invalid llm auth mode {mode!r}; choose from {VALID_MODES}")
    if _PINNED_API_KEY:
        return "api_key"
    if mode == "auto":
        return "subscription" if (_agent_sdk_importable() and subscription_available()) else "api_key"
    return mode


def live_llm_available(mode: str | None = None) -> bool:
    """Whether a live LLM call can be made under the resolved mode.

    Used by the eval live-suite gate and the ``jim-research`` CLI in place of a
    bare ``anthropic_api_key`` truthiness check, so subscription-only setups still
    run the live path.
    """
    resolved = resolve_mode(mode)
    if resolved == "subscription":
        return _agent_sdk_importable() and subscription_available()
    return bool(get_settings().anthropic_api_key)


@dataclass
class LLMResponse:
    """Transport-agnostic result of one completion."""

    text: str
    usage: Usage
    stop_reason: str | None = None


class LLMClient(Protocol):
    mode: str

    async def complete(
        self, *, model: str, system: str, user: str, max_tokens: int
    ) -> LLMResponse: ...


@dataclass
class ApiKeyClient:
    """Raw ``anthropic`` SDK backend — jim's original, ToS-clean path."""

    api_key: str
    mode: str = "api_key"

    async def complete(
        self, *, model: str, system: str, user: str, max_tokens: int
    ) -> LLMResponse:
        client = AsyncAnthropic(api_key=self.api_key)
        resp = await client.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=[
                {
                    "type": "text",
                    "text": system,
                    "cache_control": {"type": "ephemeral"},  # static rules → cache across calls
                }
            ],
            messages=[{"role": "user", "content": user}],
        )
        text = "".join(b.text for b in resp.content if b.type == "text").strip()
        usage = Usage(
            model=model,
            input_tokens=resp.usage.input_tokens,
            output_tokens=resp.usage.output_tokens,
        )
        return LLMResponse(text=text, usage=usage, stop_reason=getattr(resp, "stop_reason", None))


def _load_agent_sdk():
    """Import the Claude Agent SDK lazily (it's an optional extra). Patchable seam."""
    try:
        import claude_agent_sdk as sdk
    except ImportError as exc:  # pragma: no cover - exercised via monkeypatched seam
        raise RuntimeError(
            "subscription auth needs the Claude Agent SDK. Install it with "
            "`uv sync --extra subscription`, then run `claude login` (or export "
            "CLAUDE_CODE_OAUTH_TOKEN from `claude setup-token`)."
        ) from exc
    return sdk


@dataclass
class SubscriptionClient:
    """Claude Agent SDK backend — spawns the ``claude`` CLI, uses your subscription.

    Dev-loop / evals only. Best-effort token accounting: the Agent SDK returns a
    ``ResultMessage.usage`` dict when available; when it doesn't, tokens read 0 and
    the derived cost is a notional zero (there is no per-token charge on a
    subscription anyway — eval run docs record the auth mode so cost comparisons
    stay mode-aware).
    """

    mode: str = "subscription"

    async def complete(
        self, *, model: str, system: str, user: str, max_tokens: int
    ) -> LLMResponse:
        sdk = _load_agent_sdk()
        # Strip ANTHROPIC_API_KEY from the subprocess env: when present it silently
        # shadows the subscription OAuth token and bills API credits instead.
        env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}
        token = _oauth_token()
        if token:
            env["CLAUDE_CODE_OAUTH_TOKEN"] = token

        # Pass only options this installed SDK version actually declares — the
        # ClaudeAgentOptions surface shifts between releases.
        wanted = {
            "system_prompt": system,
            "model": model,
            "max_turns": 1,
            "env": env,
        }
        known = {f.name for f in dataclasses.fields(sdk.ClaudeAgentOptions)}
        options = sdk.ClaudeAgentOptions(**{k: v for k, v in wanted.items() if k in known})

        text_parts: list[str] = []
        result_text: str | None = None
        usage_dict: dict = {}
        stop_reason: str | None = None
        async for message in sdk.query(prompt=user, options=options):
            if isinstance(message, sdk.AssistantMessage):
                for block in message.content:
                    if isinstance(block, sdk.TextBlock):
                        text_parts.append(block.text)
            elif isinstance(message, sdk.ResultMessage):
                result_text = message.result
                usage_dict = message.usage or {}
                stop_reason = getattr(message, "stop_reason", None)

        text = (result_text or "".join(text_parts)).strip()
        usage = Usage(
            model=model,
            input_tokens=int(usage_dict.get("input_tokens", 0) or 0),
            output_tokens=int(usage_dict.get("output_tokens", 0) or 0),
        )
        return LLMResponse(text=text, usage=usage, stop_reason=stop_reason)


def build_llm_client(mode: str | None = None) -> LLMClient:
    """Return the LLM client for the resolved auth mode.

    Raises with an actionable message when the resolved mode has no usable
    credential (the callers translate this into a raise/skip as appropriate).
    """
    resolved = resolve_mode(mode)
    if resolved == "subscription":
        return SubscriptionClient()
    key = get_settings().anthropic_api_key
    if not key:
        raise RuntimeError(
            "api_key auth needs ANTHROPIC_API_KEY (or set LLM_AUTH_MODE=subscription "
            "and run `claude login`). The deterministic sourcing gate runs without any key."
        )
    return ApiKeyClient(api_key=key)
