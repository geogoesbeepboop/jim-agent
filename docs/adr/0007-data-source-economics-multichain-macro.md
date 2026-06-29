# ADR-0007 — Data-source economics: multi-chain tokens, free macro, a price guard — and what we refused

**Status:** Accepted

## Context

The Phase-5 backlog proposed four data-source expansions: audit the real Graph
gateway price before mainnet, decide subscription-vs-per-query economics, add new
sources (macro, earnings transcripts, forward EPS estimates), and extend token
coverage beyond Uniswap-v3-on-Ethereum. Before writing code we researched, for
each candidate, the three numbers that actually decide it: **per-query cost**,
**commercial redistribution rights**, and **per-query (ideally x402) availability**.

The research produced one organising finding: **jim's core invariant — "every
number traces to a public-domain primary source" — is not just a compliance
stance, it is what makes the economics work.** The candidates split cleanly, and
the split is identical whether you sort by cost, by redistribution rights, or by
invariant-fit:

- **Public-domain / on-chain** (EDGAR, US-gov macro, on-chain DEX data): free or
  ~$0.0002/query, freely redistributable, cite-able to a primary source.
- **Proprietary** (earnings transcripts, I/B/E/S-style EPS estimates, equity index
  levels): $149/mo → $1M+/yr, **redistribution of derived works prohibited**
  without an enterprise license, and not public domain — so they break the
  invariant *and* the unit economics at a $0.25–$0.50 sale price.

## Decision

> Build the public-domain / on-chain lane and refuse the proprietary one. Add a
> deterministic **price guard** so dynamic x402 pricing can't overcharge us.

- **Multi-chain EVM tokens** ([thegraph.py](../../src/jim/sources/thegraph.py)).
  All Uniswap-v3 deployments share one GraphQL schema, so one query + one parser
  now serves Ethereum, Base, Arbitrum, and Polygon via a `ChainSpec` registry. The
  identifier carries the chain (`WETH`, `WETH:base`, `0x…:arbitrum`); the cache key
  is chain-qualified; citations name the chain. Settlement is unchanged (still
  x402 on Base). On-chain data is public and The Graph charges a *service fee, not
  a data license*, so redistribution is clean. Aerodrome (a Solidly fork with a
  different schema) is a registry entry deferred to a follow-up parser.
- **Free macro source** ([macro.py](../../src/jim/sources/macro.py), product
  `macro`). Fed funds (NY Fed EFFR), CPI (BLS), and 2y/10y Treasury yields (U.S.
  Treasury) + a derived 2s10s spread — every figure cited to a **US-government
  primary source** (public domain, redistributable). Deliberately **not FRED**:
  FRED's API ToS forbids caching/redistribution, while the underlying agency data
  is public domain (17 U.S.C. §105), so we go straight to the agencies. Free data
  → pure-margin product, like fundamentals. Best-effort: a down upstream drops its
  reading, never fails the run.
- **Dynamic-price guard** ([client.py](../../src/jim/buyer/client.py)
  `PriceCapExceeded`, threaded via [base.py](../../src/jim/sources/base.py)
  `procure`). The x402 price is set by the seller in the 402 header and is *not*
  pre-published; the unpaid pre-flight now reads it and **refuses before settling**
  if it exceeds the per-query budget. `graph_probe.py` became the pre-mainnet
  audit: it decodes the live price and PASS/FAILs it against the same ceiling.

**Refused (with reasons), not merely deferred:**
- **Earnings transcripts & forward-EPS estimates** — proprietary; every vetted
  vendor prohibits redistribution of derived works without a $75k–$1M+/yr
  enterprise license; none are on x402. At $0.50/query the cheapest option (FMP
  $149/mo) needs ~300 sales/month just to cover the subscription, before the
  redistribution license it still wouldn't grant. They break the invariant.
  (Narrow public-domain exception noted but not built: 8-K Exhibit 99.2 transcripts
  on EDGAR, ~20–30% coverage.)
- **Equity index levels (S&P 500, sector benchmarks)** — proprietary; no
  public-domain path exists. Macro uses gov-statistic context instead.
- **Solana** — not on The Graph; the redistributable providers (CoinGecko/Gecko­
  Terminal) need an Enterprise license, the cheap ones (Birdeye, Jupiter, DefiLlama)
  prohibit redistribution, and none sell over x402.

## The economics (the subscription-vs-per-query question)

Per-query (x402) wins at jim's scale by a wide margin; a flat subscription only
wins at sustained high volume. The break-even is `subscription_fee ÷ per_query_cost`:

| Source | Per-query cost | A flat plan beats per-query above… |
|---|---|---|
| The Graph (x402) | ~$0.0002 (est.; dynamic) | ~250k queries/mo vs a $50/mo plan (and 100k/mo is free) |
| Macro (US gov) | $0 | never — it's free |
| Transcripts / EPS | no per-query offering | n/a — fixed license cost, uneconomic at any volume |

So: **per-query/x402 is correct for jim today.** The token product's real data
cost is ~100× *below* the `$0.01` the config historically assumed and far under
the `$0.10` ceiling — margin on token research is effectively 100%. The guard, not
a subscription, is the thing the buy side actually needed.

## Consequences

**Positive**
- Token coverage goes from one chain to four with no new payment surface and clean
  redistribution; the parser and settlement path are unchanged.
- A genuinely new *free, redistributable, public-domain* source (macro) — pure
  margin, fully inside the invariant.
- The mainnet buy leg is de-risked: a dynamic over-price is refused, not paid, and
  `graph_probe` audits it before cutover.
- We avoided sinking time into proprietary integrations that would have been legally
  unusable for a redistribute-derived-insight product.

**Negative / trade-offs**
- The community Uniswap-v3 subgraphs (Base/Arbitrum/Polygon) are "not official
  Uniswap Labs deployments"; Base's has thin indexer signal. Live mainnet use
  should verify subgraph health (the mock makes this moot on testnet).
- The real x402 price is still confirmed only by a live probe; our estimate
  (~$0.0002) is triangulated, not a contract — hence the guard.
- Macro hits three different gov endpoints with bespoke parsing (JSON + Atom XML);
  each is best-effort, so a feed change degrades coverage rather than crashing.
- No earnings-call or forward-estimate coverage — a real product gap we accept in
  exchange for keeping the invariant and the margins intact.

## ELI5 / what I learned

I went shopping for new data. Some of it — government numbers, on-chain prices — is
free or nearly free, and I'm allowed to republish what I compute from it. The rest —
earnings-call transcripts, Wall-Street profit forecasts, the S&P 500 number — is
expensive *and* comes with a "you may not pass this on" sticker, so I'd be paying a
fortune to build something I'm not even allowed to sell. So I bought the free,
shareable stuff (more chains of token data, plus Fed/inflation/Treasury context) and
walked past the rest. I also learned the on-chain shop doesn't show its price until
you're at the till, so I taught the wallet to check the price first and refuse if
it's too high. The lesson: *the rule "only use sources I can freely cite and resell"
isn't a constraint on the business — it's the part that makes the business make
money.*
