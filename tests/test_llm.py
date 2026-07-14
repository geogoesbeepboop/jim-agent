"""Dual-mode auth factory (jim.llm).

Offline: both backends are exercised through injected fakes — no ``anthropic``
network call and no ``claude`` subprocess. Covers mode resolution, the api_key
pin (the ToS guard that keeps subscription off the seller path), auto-fallback,
credential gating, and that each backend returns text + usage in the shared shape.
"""

from __future__ import annotations

import dataclasses

import pytest

import jim.llm as llm
from jim.config import Settings
from jim.llm import (
    ApiKeyClient,
    LLMResponse,
    build_llm_client,
    live_llm_available,
    resolve_mode,
    set_auth_mode,
)


# --- mode resolution ----------------------------------------------------------


def test_default_mode_is_api_key(monkeypatch) -> None:
    monkeypatch.setattr(llm, "get_settings", lambda: Settings(llm_auth_mode="api_key"))
    assert resolve_mode() == "api_key"


def test_explicit_arg_beats_settings(monkeypatch) -> None:
    monkeypatch.setattr(llm, "get_settings", lambda: Settings(llm_auth_mode="api_key"))
    assert resolve_mode("subscription") == "subscription"


def test_set_auth_mode_override(monkeypatch) -> None:
    monkeypatch.setattr(llm, "get_settings", lambda: Settings(llm_auth_mode="api_key"))
    set_auth_mode("subscription")
    assert resolve_mode() == "subscription"
    set_auth_mode(None)
    assert resolve_mode() == "api_key"


def test_invalid_mode_rejected(monkeypatch) -> None:
    monkeypatch.setattr(llm, "get_settings", lambda: Settings(llm_auth_mode="nonsense"))
    with pytest.raises(ValueError):
        resolve_mode()
    with pytest.raises(ValueError):
        set_auth_mode("nope")


def test_pin_forces_api_key_over_subscription(monkeypatch) -> None:
    """The seller/monitor pin must win over every other signal — the ToS guard."""
    monkeypatch.setattr(llm, "get_settings", lambda: Settings(llm_auth_mode="subscription"))
    set_auth_mode("subscription")
    llm.pin_api_key_mode()
    assert resolve_mode() == "api_key"
    assert resolve_mode("subscription") == "api_key"  # even an explicit arg loses


def test_auto_prefers_subscription_when_available(monkeypatch) -> None:
    monkeypatch.setattr(llm, "get_settings", lambda: Settings(llm_auth_mode="auto"))
    monkeypatch.setattr(llm, "_agent_sdk_importable", lambda: True)
    monkeypatch.setattr(llm, "subscription_available", lambda: True)
    assert resolve_mode() == "subscription"


def test_auto_falls_back_to_api_key(monkeypatch) -> None:
    monkeypatch.setattr(llm, "get_settings", lambda: Settings(llm_auth_mode="auto"))
    monkeypatch.setattr(llm, "_agent_sdk_importable", lambda: True)
    monkeypatch.setattr(llm, "subscription_available", lambda: False)
    assert resolve_mode() == "api_key"


# --- credential gating --------------------------------------------------------


def test_live_available_api_key(monkeypatch) -> None:
    monkeypatch.setattr(
        llm, "get_settings", lambda: Settings(llm_auth_mode="api_key", anthropic_api_key="sk-x")
    )
    assert live_llm_available() is True
    monkeypatch.setattr(
        llm, "get_settings", lambda: Settings(llm_auth_mode="api_key", anthropic_api_key=None)
    )
    assert live_llm_available() is False


def test_live_available_subscription(monkeypatch) -> None:
    monkeypatch.setattr(llm, "get_settings", lambda: Settings(llm_auth_mode="subscription"))
    monkeypatch.setattr(llm, "_agent_sdk_importable", lambda: True)
    monkeypatch.setattr(llm, "subscription_available", lambda: True)
    assert live_llm_available() is True
    monkeypatch.setattr(llm, "subscription_available", lambda: False)
    assert live_llm_available() is False


def test_build_client_raises_without_api_key(monkeypatch) -> None:
    monkeypatch.setattr(
        llm, "get_settings", lambda: Settings(llm_auth_mode="api_key", anthropic_api_key=None)
    )
    with pytest.raises(RuntimeError, match="ANTHROPIC_API_KEY"):
        build_llm_client()


def test_build_client_returns_api_key_client(monkeypatch) -> None:
    monkeypatch.setattr(
        llm, "get_settings", lambda: Settings(llm_auth_mode="api_key", anthropic_api_key="sk-x")
    )
    client = build_llm_client()
    assert isinstance(client, ApiKeyClient) and client.mode == "api_key"


# --- api_key backend ----------------------------------------------------------


class _FakeUsage:
    input_tokens = 11
    output_tokens = 22


class _FakeBlock:
    type = "text"

    def __init__(self, text):
        self.text = text


class _FakeResp:
    def __init__(self, text):
        self.content = [_FakeBlock(text)]
        self.usage = _FakeUsage()
        self.stop_reason = "end_turn"


def _fake_anthropic(capture: dict):
    class _Messages:
        async def create(self, **kwargs):
            capture.update(kwargs)
            return _FakeResp("hello [C1]")

    class _Client:
        def __init__(self, *a, **k):
            capture["api_key"] = k.get("api_key")
            self.messages = _Messages()

    return _Client


async def test_api_key_client_preserves_cache_control_and_usage(monkeypatch) -> None:
    capture: dict = {}
    monkeypatch.setattr(llm, "AsyncAnthropic", _fake_anthropic(capture))
    client = ApiKeyClient(api_key="sk-test")
    resp = await client.complete(model="m", system="RULES", user="facts", max_tokens=1500)

    assert isinstance(resp, LLMResponse)
    assert resp.text == "hello [C1]"
    assert resp.usage.input_tokens == 11 and resp.usage.output_tokens == 22
    assert resp.stop_reason == "end_turn"
    # The static system prompt is still sent as an ephemeral-cache block.
    assert capture["system"][0]["cache_control"] == {"type": "ephemeral"}
    assert capture["system"][0]["text"] == "RULES"
    assert capture["max_tokens"] == 1500
    assert capture["api_key"] == "sk-test"


# --- subscription backend (fake Agent SDK) ------------------------------------


class _FakeSDK:
    """A stand-in claude_agent_sdk module with just the names the backend touches."""

    def __init__(self, capture):
        self.capture = capture

        @dataclasses.dataclass
        class ClaudeAgentOptions:
            system_prompt: str | None = None
            model: str | None = None
            max_turns: int | None = None
            env: dict | None = None

        class TextBlock:
            def __init__(self, text):
                self.text = text

        class AssistantMessage:
            def __init__(self, content):
                self.content = content

        class ResultMessage:
            def __init__(self, result, usage, stop_reason):
                self.result = result
                self.usage = usage
                self.stop_reason = stop_reason

        self.ClaudeAgentOptions = ClaudeAgentOptions
        self.TextBlock = TextBlock
        self.AssistantMessage = AssistantMessage
        self.ResultMessage = ResultMessage

        async def query(*, prompt, options):
            capture["prompt"] = prompt
            capture["options"] = options
            yield AssistantMessage([TextBlock("partial")])
            yield ResultMessage(
                "final memo [C1]", {"input_tokens": 5, "output_tokens": 7}, "end_turn"
            )

        self.query = query


async def test_subscription_client_completes_and_strips_api_key(monkeypatch) -> None:
    from jim.llm import SubscriptionClient

    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-live-should-be-stripped")
    capture: dict = {}
    monkeypatch.setattr(llm, "_load_agent_sdk", lambda: _FakeSDK(capture))

    resp = await SubscriptionClient().complete(
        model="claude-sonnet-4-6", system="RULES", user="facts", max_tokens=1500
    )
    assert resp.text == "final memo [C1]"
    assert resp.usage.input_tokens == 5 and resp.usage.output_tokens == 7
    # ANTHROPIC_API_KEY must be stripped from the subprocess env (else it shadows
    # the subscription token and silently bills API credits).
    assert "ANTHROPIC_API_KEY" not in capture["options"].env
    assert capture["options"].system_prompt == "RULES"
    assert capture["options"].model == "claude-sonnet-4-6"
