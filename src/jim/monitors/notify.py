"""Delivery channels — how a material update leaves the system.

The store is always the system of record (every run is persisted for the feed +
stats), so channels here are the *external* push sinks a monitor opts into:

  - ``console``        : print the update (handy for `jim-monitor run`/`serve`).
  - ``webhook:<url>``  : POST the update as JSON, HMAC-SHA256 signed so the
                         subscriber can verify it really came from jim.

Every payload is impersonal and fully cited (it carries the citations behind the
memo + the verbatim disclaimer). Delivery is best-effort: a failing channel is
logged and skipped — it never breaks the monitor run.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import sys
from dataclasses import dataclass
from typing import Protocol

import httpx

from jim.config import get_settings
from jim.monitors.models import Monitor, MonitorRun
from jim.research.synthesize import DISCLAIMER

SIGNATURE_HEADER = "X-Jim-Signature"


def build_payload(monitor: Monitor, run: MonitorRun, citations: list[str]) -> dict:
    """The impersonal, cited JSON envelope pushed to subscribers."""
    return {
        "type": "jim.monitor.update",
        "monitor_id": monitor.id,
        "product": run.product,
        "identifier": run.identifier,
        "severity": run.severity,
        "ran_at": run.ran_at.isoformat() if run.ran_at else None,
        "signals": [
            {
                "kind": s.kind,
                "label": s.label,
                "severity": s.severity,
                "summary": s.summary,
                "citations": s.citation_ids,
            }
            for s in run.signals
        ],
        "memo": run.memo,
        "citations": citations,
        "economics": {
            "price_out_usd": run.price_out_usd,
            "cost_in_data_usd": run.cost_in_data_usd,
            "cost_inference_usd": run.cost_inference_usd,
            "margin_usd": run.margin_usd,
            "cache_hit": run.cache_hit,
        },
        "disclaimer": DISCLAIMER,
    }


def sign_payload(body: bytes, secret: str | None) -> str | None:
    """``sha256=<hex>`` HMAC of the exact body bytes (None if no secret)."""
    if not secret:
        return None
    digest = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


class Channel(Protocol):
    name: str

    async def deliver(self, payload: dict) -> bool: ...


@dataclass
class ConsoleChannel:
    name: str = "console"

    async def deliver(self, payload: dict) -> bool:
        sigs = "\n".join(f"    • {s['summary']}" for s in payload["signals"])
        print(
            f"\n[monitor] {payload['identifier']} ({payload['product']}) "
            f"— {payload['severity'].upper()}\n{sigs}\n  {payload.get('memo', '')}\n",
            flush=True,
        )
        return True


@dataclass
class WebhookChannel:
    url: str
    secret: str | None = None
    timeout: float = 10.0
    name: str = "webhook"

    async def deliver(self, payload: dict) -> bool:
        body = json.dumps(payload, separators=(",", ":"), sort_keys=True).encode()
        headers = {"Content-Type": "application/json"}
        sig = sign_payload(body, self.secret)
        if sig:
            headers[SIGNATURE_HEADER] = sig
        try:
            async with httpx.AsyncClient(timeout=httpx.Timeout(self.timeout)) as c:
                resp = await c.post(self.url, content=body, headers=headers)
            if resp.status_code >= 400:
                print(f"[monitor] webhook {self.url} → HTTP {resp.status_code}", file=sys.stderr)
                return False
            return True
        except httpx.HTTPError as e:
            print(f"[monitor] webhook {self.url} failed: {e}", file=sys.stderr)
            return False


def build_channels(specs: list[str]) -> list[Channel]:
    """Parse channel spec strings into channel objects (feed is implicit)."""
    settings = get_settings()
    channels: list[Channel] = []
    for spec in specs or []:
        spec = spec.strip()
        if spec == "console":
            channels.append(ConsoleChannel())
        elif spec.startswith("webhook:"):
            url = spec[len("webhook:") :]
            if url:
                channels.append(
                    WebhookChannel(
                        url=url,
                        secret=settings.monitor_signing_secret,
                        timeout=settings.monitor_webhook_timeout_seconds,
                    )
                )
        # "store"/"feed"/unknown → no external channel; the run is persisted anyway.
    return channels


async def deliver_all(channels: list[Channel], payload: dict) -> list[str]:
    """Deliver to every channel; return the names that succeeded."""
    delivered: list[str] = []
    for ch in channels:
        if await ch.deliver(payload):
            delivered.append(ch.name)
    return delivered
