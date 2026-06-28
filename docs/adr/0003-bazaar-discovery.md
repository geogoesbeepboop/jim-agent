# ADR-0003 — Bazaar discovery via the native x402 extension + a deterministic manifest

**Status:** Accepted

## Context

Phase 5's first goal is "other agents auto-discover and pay us." There are two
distinct discovery problems and it's tempting to conflate them:

1. **Index discovery** — how does a *facilitator/marketplace* catalog jim without
   anyone manually submitting a listing?
2. **Pull discovery** — how does a *specific agent* that already knows our host
   learn exactly how to call each product (params, output shape, price) and where
   our MCP endpoint is?

The build plan is explicit: *"Add Bazaar metadata (`accepts` array + input/output
JSON schemas); first successful settlement auto-indexes us."* The naive approach
— stand up our own registry, or hand-roll a bespoke metadata blob — duplicates
machinery x402 already ships and risks producing listings real indexers reject
(they cap `service_name` length, tag counts, icon URL shape, and validate the
discovery extension against a JSON schema).

A subtlety surfaced while wiring it: the catalog's natural output schema is the
Pydantic `ResearchResponse.model_json_schema()`, which uses `$ref`/`$defs`. Nested
under a Bazaar extension's `output.example`, those `$ref` pointers dangle and the
indexer's validator rejects the whole extension — silently dropping us from
discovery.

## Decision

> Use the official x402 Bazaar extension for index discovery, a deterministic
> self-describing manifest for pull discovery, and a **ref-free** output schema so
> both validate.

- **Index:** each paid route attaches `declare_discovery_extension(...)` (from
  `x402.extensions.bazaar`) plus `service_name`/`tags`/`icon_url` on its
  `RouteConfig` ([catalog.py](../../src/jim/marketplace/catalog.py),
  [seller/app.py](../../src/jim/seller/app.py)). The extension rides on the 402
  challenge, so the **first successful settlement** carries our discovery data to
  the facilitator with zero manual submission. We import the helper directly and
  fall back to a hand-built dict of the identical v2 shape if the optional
  `jsonschema` dep is absent — the same graceful degradation jim uses for
  langfuse/anthropic.
- **Pull:** [`GET /.well-known/x402`](../../src/jim/marketplace/discovery.py)
  returns a deterministic manifest (no timestamps/run state, so it's byte-stable
  and cacheable) covering identity, network, USDC asset, pay-to, every product's
  call shape + price, and the MCP endpoint. `GET /catalog` and `GET /pricing`
  expose the same source of truth.
- **One catalog, no drift:** [catalog.py](../../src/jim/marketplace/catalog.py)
  is the single source the routes, the manifest, the MCP tools, the human UI, and
  the system map all read from. The advertised output schema is a compact,
  **self-contained (ref-free)** JSON Schema that still mirrors the response
  contract field-for-field — discovery wants the *shape*, not a strict validator,
  and inline definitions keep it valid wherever it's nested.
- We declare the route's HTTP `method` up front in the extension (the server
  extension would otherwise enrich it only at request time) so the at-rest
  declaration validates against its own schema and startup is warning-free.

## Consequences

**Positive**
- Zero bespoke registry: we ride x402's own discovery rails, so we're indexable
  by any Bazaar-speaking facilitator the moment we settle once.
- Pull + index discovery share one catalog, so a price or schema change shows up
  everywhere at once and can't drift between the 402 challenge and the manifest.
- The manifest is deterministic, so it can be cached/signed and diffed in CI.

**Negative / trade-offs**
- The advertised output schema is hand-curated, not auto-derived from the Pydantic
  model, so a new response field must be added in two places (a test asserts the
  field set still matches the contract).
- `jsonschema` becomes a real dependency to use the official helper with startup
  validation (the fallback path keeps discovery working without it, unvalidated).
- We don't run our *own* facilitator, so index discovery's freshness is bounded by
  when our first mainnet settlement happens and how the chosen facilitator indexes.

## ELI5 / what I learned

I wanted other AI agents to find jim and pay it without me emailing a directory.
x402 already has a "Bazaar" — a shared phone book that facilitators fill in
automatically. So instead of building my own phone book, I attach a little
"here's how to call me and what you'll get back" card to the bill I hand every
customer; the first time someone actually pays, the facilitator copies that card
into the phone book for me. For agents that already know my address, I also post
a single `/.well-known/x402` page that says everything in one place. The gotcha I
hit: my "what you'll get back" description used internal shortcuts (`$ref`s) that
made sense at home but pointed nowhere once pasted onto the card, so the phone
book rejected it — I had to write a self-contained version. Lesson: *use the
ecosystem's existing discovery rail, keep one source of truth behind it, and make
the metadata stand on its own the moment it leaves your process.*
