"""Peer-agent sources (Phase 7): jim buys cited signals from other agents.

The general-contractor move from AGENT_INTEROP.md: jim shouldn't grow a
sentiment model or a legal-NLP stack — it should *buy* those signals from
specialized peer agents over x402 and stay world-class at turning signals into
provably-cited analysis. A peer is just another :class:`Source`: it routes
through the same ``procure()`` → budget → cache path as The Graph, and its
facts face the same sourcing gate as everything else. **The gate is the
composition firewall** — an unverifiable figure from a paid subcontractor
fails exactly like a self-hallucination, so jim can buy from agents it does
not trust and still only ship what it can verify.

Wire format: a peer responds with either a jim-shaped research response (a
``citations`` list) or a bare ``facts`` list — rows of
``{label, value, unit, concept?, accession?, source_url?, filed?}`` with jim's
canonical unit tags. Rows that don't parse are dropped; a payload with no
usable rows is a procurement failure that also debits the peer's trust score.

Safety stack per buy, all deterministic:
  - **trust floor** — a peer whose gate pass-rate fell below ``PEER_TRUST_FLOOR``
    (after ``PEER_TRUST_MIN_EVENTS`` observations) is refused before payment;
  - **budget** — the buy proposes to the same per-query ``BudgetCap`` as every
    upstream, with the dynamic-price cap guard (ADR-0007);
  - **call chain** — the buyer client stamps ``X-Jim-Call-Chain``, so loops and
    over-depth compositions are refused before money moves (jim.interop).
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, replace
from urllib.parse import quote

from jim.buyer.client import pay
from jim.config import get_settings
from jim.research.facts import (
    COUNT,
    INDEX,
    MULTIPLE,
    PERCENT,
    SHARES,
    USD,
    USD_PER_SHARE,
    Fact,
    Snapshot,
)
from jim.sources.base import BudgetExceeded, BuyFn, GatherResult, ProcurementError, procure

PEER_FORM = "peer agent (x402)"

_ALLOWED_UNITS = {USD, USD_PER_SHARE, SHARES, PERCENT, MULTIPLE, COUNT, INDEX}
_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,63}$")
_ID_RE = re.compile(r"^C(\d+)$")


@dataclass(frozen=True)
class PeerSpec:
    """One configured peer agent (from the ``PEER_SOURCES`` env JSON)."""

    name: str  # short slug, e.g. "sentiment-alpha"
    url: str  # x402-gated endpoint; identifier appended as a query param
    identifier_param: str = "identifier"
    price_estimate_usd: float = 0.05  # budget proposal; the 402 names the real price
    products: tuple[str, ...] = ()  # products to compose into; () = all


def parse_peer_specs(raw: str | None) -> list[PeerSpec]:
    """Parse ``PEER_SOURCES`` — a JSON list of peer entries.

    Example::

        PEER_SOURCES='[{"name":"sentiment-alpha","url":"https://peer.example/signals",
                        "price_estimate_usd":0.02,"products":["fundamentals","token"]}]'
    """
    if not raw or not raw.strip():
        return []
    try:
        entries = json.loads(raw)
    except ValueError as e:
        raise ValueError(f"PEER_SOURCES is not valid JSON: {e}") from e
    if not isinstance(entries, list):
        raise ValueError("PEER_SOURCES must be a JSON list of peer objects")

    specs: list[PeerSpec] = []
    for entry in entries:
        name = str(entry.get("name", "")).strip().lower()
        url = str(entry.get("url", "")).strip()
        if not _NAME_RE.match(name):
            raise ValueError(f"PEER_SOURCES: bad peer name {name!r} (lowercase slug required)")
        if not url.startswith(("http://", "https://")):
            raise ValueError(f"PEER_SOURCES: peer {name!r} needs an http(s) url, got {url!r}")
        specs.append(
            PeerSpec(
                name=name,
                url=url,
                identifier_param=str(entry.get("identifier_param", "identifier")),
                price_estimate_usd=float(entry.get("price_estimate_usd", 0.05)),
                products=tuple(entry.get("products", ())),
            )
        )
    return specs


def _facts_from_payload(payload: dict, peer_name: str) -> list[dict]:
    """Extract usable fact rows from a peer payload (jim-shaped or bare)."""
    rows = payload.get("facts")
    if rows is None:
        rows = payload.get("citations")  # a jim-shaped ResearchResponse works as-is
    if not isinstance(rows, list):
        raise ProcurementError(f"peer:{peer_name} payload has no facts/citations list")

    usable: list[dict] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        label, value, unit = row.get("label"), row.get("value"), row.get("unit")
        if (
            isinstance(label, str)
            and label.strip()
            and isinstance(value, (int, float))
            and not isinstance(value, bool)
            and unit in _ALLOWED_UNITS
        ):
            usable.append(row)
    if not usable:
        raise ProcurementError(
            f"peer:{peer_name} returned no usable facts (rows need label + numeric value "
            f"+ a canonical unit tag)"
        )
    return usable


class PeerSource:
    """A :class:`Source` that buys cited facts from one peer agent over x402."""

    is_paid = True

    def __init__(self, spec: PeerSpec, buy_fn: BuyFn = pay) -> None:
        self.spec = spec
        self.name = f"peer:{spec.name}"
        self._buy = buy_fn

    async def _check_trust(self, store) -> None:
        settings = get_settings()
        try:
            scores = await store.trust_scores()
        except Exception:
            return  # trust ledger unavailable → don't block the buy on it
        row = scores.get(self.name)
        if not row:
            return
        events = int(row.get("ok", 0)) + int(row.get("fail", 0))
        if events >= settings.peer_trust_min_events and row["score"] < settings.peer_trust_floor:
            raise ProcurementError(
                f"{self.name} is below the trust floor "
                f"({row['score']:.2f} < {settings.peer_trust_floor:.2f} after {events} gated "
                f"runs) — refusing to pay a source whose data we cannot verify"
            )

    async def gather(self, identifier: str, *, budget, store) -> GatherResult:
        settings = get_settings()
        await self._check_trust(store)

        sep = "&" if "?" in self.spec.url else "?"
        url = f"{self.spec.url}{sep}{self.spec.identifier_param}={quote(identifier)}"
        result = await procure(
            source_name=self.name,
            cache_key=f"{self.spec.name}:{identifier.upper()}",
            url=url,
            method="GET",
            json_body=None,
            network=settings.network,
            price_estimate_usd=self.spec.price_estimate_usd,
            private_key=settings.evm_private_key,
            budget=budget,
            store=store,
            ttl_seconds=settings.purchase_cache_ttl_seconds,
            buy_fn=self._buy,
        )

        try:
            rows = _facts_from_payload(result.payload, self.spec.name)
        except ProcurementError:
            # Paying for garbage is itself a trust signal — debit before failing.
            try:
                await store.record_trust_event(
                    source=self.name, ok=False, context=f"unusable payload for {identifier}"
                )
            except Exception:
                pass
            raise

        service = str(result.payload.get("service") or self.spec.name)
        facts = [
            Fact(
                id=f"C{i}",
                label=str(row["label"]).strip(),
                value=float(row["value"]),
                unit=str(row["unit"]),
                source_label=str(row.get("source_label") or service),
                concept=row.get("concept"),
                accession=str(row.get("accession") or result.tx_hash or f"x402:{self.spec.name}"),
                form=PEER_FORM,
                filed=row.get("filed") or row.get("as_of"),
                source_url=str(row.get("source_url") or self.spec.url),
            )
            for i, row in enumerate(rows, start=1)
        ]
        snapshot = Snapshot(
            ticker=identifier.upper(),
            cik=f"PEER:{self.spec.name}",
            entity_name=f"{identifier.upper()} — {service} signals",
            facts=facts,
            as_of=result.payload.get("as_of"),
            origins={f.id: self.name for f in facts},
        )
        return GatherResult(
            snapshot=snapshot, cost_in_usd=result.cost_in_usd, cache_hit=result.cache_hit
        )


class CompositeSource:
    """The general contractor: a primary source composed with peer signals.

    Gathers the primary snapshot first, then buys each configured peer's facts
    and merges them in with renumbered citation ids and per-fact ``origins``
    (which feed the trust ledger's attribution). A peer that fails — budget
    denied, below the trust floor, unusable payload, network error — is
    *skipped with a note*, never fatal: jim degrades to what it could verify
    and afford, which is the same posture as every other upstream.
    """

    def __init__(self, primary, peers: list[PeerSource]) -> None:
        self.primary = primary
        self.peers = list(peers)
        self.name = primary.name
        self.is_paid = bool(getattr(primary, "is_paid", False) or self.peers)

    async def gather(self, identifier: str, *, budget, store) -> GatherResult:
        base = await self.primary.gather(identifier, budget=budget, store=store)
        snapshot = base.snapshot
        if not snapshot.origins:
            snapshot.origins = {f.id: self.primary.name for f in snapshot.facts}

        cost = base.cost_in_usd
        notes = list(base.notes)
        next_n = 1 + max(
            (int(m.group(1)) for f in snapshot.facts if (m := _ID_RE.match(f.id))), default=0
        )

        for peer in self.peers:
            try:
                bought = await peer.gather(identifier, budget=budget, store=store)
            except (BudgetExceeded, ProcurementError) as e:
                notes.append(f"{peer.name}: skipped — {e}")
                continue
            added = 0
            for fact in bought.snapshot.facts:
                new_id = f"C{next_n}"
                next_n += 1
                snapshot.facts.append(replace(fact, id=new_id))
                snapshot.origins[new_id] = peer.name
                added += 1
            cost += bought.cost_in_usd
            notes.append(
                f"{peer.name}: +{added} facts "
                f"(${bought.cost_in_usd:.4f}{', cached' if bought.cache_hit else ''})"
            )

        return GatherResult(
            snapshot=snapshot, cost_in_usd=cost, cache_hit=base.cache_hit, notes=notes
        )
