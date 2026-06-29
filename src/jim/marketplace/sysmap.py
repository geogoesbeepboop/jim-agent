"""Live system map (Phase 5) — see jim as a whole, as it's configured *right now*.

``docs/SYSTEM_MAP.md`` has the hand-drawn architecture diagrams; this module is
their living companion. It introspects the actual runtime — the catalog, prices,
the active network, which sources are paid, the store backend, feature flags
(debate, prices, GRAPH_LIVE, monitor autostart), the facilitator, the MCP
endpoint — and emits a Mermaid graph of the entire system: from MCP / HTTP / human
buyers, through discovery, the x402 payment rails, the seller, the LangGraph
engine and its deterministic trust gates, the sources and the external tools they
draw on, the store, the monitor crew, and observability.

Three render targets, one structured graph:
  - ``to_mermaid()``  → a Mermaid flowchart (renders natively on GitHub).
  - ``to_html()``     → a self-contained page (mermaid.js from CDN) for the browser.
  - ``to_json()``     → the raw node/edge graph (served at ``/map.json``).

Because it reads config, ``jim-map`` after an env change shows the new wiring —
e.g. flip ``GRAPH_LIVE=true`` and the token source repoints at Base mainnet.
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict, dataclass, field

from jim.config import Settings, get_settings
from jim.marketplace.catalog import build_catalog


@dataclass
class Node:
    id: str
    label: str
    group: str
    kind: str


@dataclass
class Edge:
    src: str
    dst: str
    label: str = ""


@dataclass
class SystemGraph:
    nodes: list[Node] = field(default_factory=list)
    edges: list[Edge] = field(default_factory=list)
    # Ordered (id, title) of subgraph groups.
    groups: list[tuple[str, str]] = field(default_factory=list)

    def add(self, node: Node) -> str:
        self.nodes.append(node)
        return node.id

    def link(self, src: str, dst: str, label: str = "") -> None:
        self.edges.append(Edge(src, dst, label))

    def to_dict(self) -> dict:
        return {
            "groups": [{"id": gid, "title": title} for gid, title in self.groups],
            "nodes": [asdict(n) for n in self.nodes],
            "edges": [asdict(e) for e in self.edges],
        }


def system_graph(settings: Settings | None = None) -> SystemGraph:
    """Build the structured system graph from live config + the catalog."""
    s = settings or get_settings()
    g = SystemGraph()
    g.groups = [
        ("buyers", "Buyers / clients"),
        ("discovery", "Discovery (Phase 5)"),
        ("payments", "x402 payment rails"),
        ("seller", "Seller — paid routes"),
        ("engine", "Research engine (LangGraph)"),
        ("trust", "Deterministic trust gates"),
        ("sources", "Sources"),
        ("external", "External tools / upstreams"),
        ("store", "Store + margin ledger"),
        ("monitors", "Monitors (Phase 4)"),
        ("obs", "Observability"),
    ]

    net = "Base mainnet" if s.is_mainnet else "Base Sepolia (testnet)"

    # --- Buyers ---
    g.add(Node("mcp_agent", "MCP agents<br/>(Claude, IDEs)", "buyers", "buyer"))
    g.add(Node("http_agent", "HTTP / agent buyers<br/>(x402 clients)", "buyers", "buyer"))
    g.add(Node("human", "Human UI<br/>(free Preview · wallet paywall)", "buyers", "buyer"))

    # --- Discovery ---
    g.add(Node("catalog", "GET /catalog", "discovery", "discovery"))
    g.add(Node("wellknown", "GET /.well-known/x402<br/>(manifest)", "discovery", "discovery"))
    g.add(Node("bazaar", "Bazaar index<br/>(auto on 1st settle)", "discovery", "discovery"))
    g.add(Node("mcp_srv", f"MCP server<br/>{s.public_url}/mcp", "discovery", "discovery"))

    # --- Payment rails ---
    g.add(Node("mw", "x402 middleware<br/>(402 → verify → settle)", "payments", "payment"))
    g.add(Node("facilitator", f"Facilitator<br/>{_short_host(s.facilitator_url)}", "payments", "payment"))
    g.add(Node("usdc", f"USDC on {net}<br/>{_short_addr(s.usdc_address)}", "payments", "payment"))
    g.add(Node("seller_wallet", f"Seller wallet<br/>{_short_addr(s.evm_address)}", "payments", "payment"))

    # --- Seller routes (live products + prices) ---
    listings = build_catalog()
    for listing in listings:
        g.add(
            Node(
                f"route_{listing.product}",
                f"{listing.route_key}<br/>${listing.price_usd:.2f}",
                "seller",
                "seller",
            )
        )

    # --- Engine nodes ---
    g.add(Node("gather", "gather", "engine", "engine"))
    if s.memo_cache_enabled:
        g.add(Node("memo_cache", "memo cache<br/>(identical → $0 inference)", "engine", "engine"))
    if s.enable_debate:
        g.add(Node("debate", "debate<br/>(bull ∥ bear → judge)", "engine", "engine"))
    g.add(Node("synthesize", "synthesize<br/>(LLM memo)", "engine", "engine"))
    g.add(Node("judge", "faithfulness judge<br/>(per-claim · Sonnet tier)", "engine", "engine"))

    # --- Trust gates ---
    g.add(Node("gate", "sourcing gate<br/>(no LLM)", "trust", "gate"))
    g.add(Node("completeness", "completeness<br/>(omission signal)", "trust", "gate"))
    g.add(Node("impersonal", "impersonal guard", "trust", "gate"))
    g.add(Node("budget", "budget cap<br/>(propose/dispose)", "trust", "gate"))

    # --- Sources ---
    g.add(Node("fundamentals_src", "FundamentalsSource<br/>(free)", "sources", "source"))
    graph_badge = "PAID · live" if s.graph_live else "PAID · mock"
    g.add(Node("graph_src", f"GraphSource<br/>({graph_badge})", "sources", "source"))

    # --- External tools ---
    g.add(Node("edgar", "SEC EDGAR<br/>(public domain)", "external", "external"))
    if s.enable_prices:
        g.add(Node("yahoo", "Yahoo charts<br/>(price/technicals)", "external", "external"))
    if s.graph_live:
        g.add(Node("thegraph", f"The Graph gateway<br/>{s.graph_buy_network}", "external", "external"))
    else:
        g.add(Node("thegraph", "Mock Graph vendor<br/>(POST /mock-graph)", "external", "external"))

    # --- Store ---
    backend = "Postgres + pgvector" if s.database_url else "In-memory (dev)"
    g.add(Node("store", f"Store<br/>{backend}", "store", "store"))
    g.add(Node("cache", "cache (data_purchases)<br/>buy once · resell many", "store", "store"))
    g.add(Node("ledger", "margin ledger<br/>(query_records)", "store", "store"))
    g.add(Node("receipts", "audit log<br/>(payment_receipts)", "store", "store"))
    g.add(Node("admin", "GET /admin<br/>(revenue · buyers · tx)", "seller", "seller"))

    # --- Monitors ---
    sched = "scheduler (in-seller)" if s.monitor_autostart else "scheduler (jim-monitor serve)"
    g.add(Node("monitor_sched", sched, "monitors", "monitor"))
    g.add(Node("triggers", "trigger crew<br/>+ materiality gate", "monitors", "monitor"))
    g.add(Node("notify", "notify<br/>console · HMAC webhook · feed", "monitors", "monitor"))

    # --- Observability ---
    import os

    obs_on = "configured" if os.getenv("LANGFUSE_PUBLIC_KEY") else "best-effort (no-op if unset)"
    g.add(Node("langfuse", f"Langfuse traces<br/>{obs_on}", "obs", "obs"))

    # --- Edges ---
    # Buyers reach discovery + the rails.
    g.link("mcp_agent", "mcp_srv", "discover")
    g.link("http_agent", "wellknown", "discover")
    g.link("http_agent", "catalog", "browse")
    g.link("human", "route_fundamentals" if listings else "mw", "checkout")
    g.link("mcp_srv", "mw", "x402-gated")
    g.link("catalog", "bazaar", "indexes")
    g.link("wellknown", "bazaar", "indexes")

    # Pay → settle.
    for listing in listings:
        g.link("mw", f"route_{listing.product}", "after settle")
    g.link("mw", "facilitator", "verify / settle")
    g.link("facilitator", "usdc", "EIP-3009")
    g.link("facilitator", "seller_wallet", "pays")
    g.link("bazaar", "http_agent", "found you")

    # Routes → engine.
    for listing in listings:
        g.link(f"route_{listing.product}", "gather", "run_research")

    # Engine flow.
    head = "memo_cache" if s.memo_cache_enabled else None
    if head:
        g.link("gather", "memo_cache", "fingerprint")
        g.link("memo_cache", "judge", "hit → serve ($0)")
    nxt = head or "gather"
    if s.enable_debate:
        g.link(nxt, "debate", "miss" if head else "")
        g.link("debate", "synthesize")
    else:
        g.link(nxt, "synthesize", "miss" if head else "")
    g.link("synthesize", "gate", "verify figures")
    g.link("gate", "synthesize", "fail → retry")
    g.link("gate", "judge", "pass")
    g.link("judge", "completeness", "omission check")

    # Gather → sources → externals.
    g.link("gather", "fundamentals_src")
    g.link("gather", "graph_src")
    g.link("fundamentals_src", "edgar")
    if s.enable_prices:
        g.link("fundamentals_src", "yahoo")
    g.link("graph_src", "budget", "propose")
    g.link("budget", "thegraph", "dispose → buy (x402)")
    g.link("graph_src", "cache", "cache-first")

    # Store wiring.
    g.link("store", "cache")
    g.link("store", "ledger")
    g.link("store", "receipts")
    g.link("gather", "store", "record")
    g.link("judge", "ledger", "margin")
    # Settlement audit: the payment middleware records a receipt per paid call,
    # which the admin dashboard reads back (revenue · buyers · on-chain tx).
    g.link("mw", "receipts", "settle → audit")
    g.link("receipts", "admin", "read")

    # Monitors reuse the engine.
    g.link("monitor_sched", "triggers", "diff → crew")
    g.link("triggers", "gather", "re-run on cadence")
    g.link("triggers", "notify", "material → push")
    g.link("notify", "impersonal", "guarded")
    g.link("synthesize", "impersonal", "monitor updates")

    # Observability.
    g.link("judge", "langfuse", "scores")
    g.link("gate", "langfuse", "coverage")

    return g


# --- rendering --------------------------------------------------------------

_KIND_STYLE = {
    "buyer": "fill:#e3f2fd,stroke:#1565c0,color:#0d47a1",
    "discovery": "fill:#ede7f6,stroke:#5e35b1,color:#311b92",
    "payment": "fill:#fff3e0,stroke:#ef6c00,color:#e65100",
    "seller": "fill:#e8f5e9,stroke:#2e7d32,color:#1b5e20",
    "engine": "fill:#e1f5fe,stroke:#0277bd,color:#01579b",
    "gate": "fill:#fce4ec,stroke:#c2185b,color:#880e4f",
    "source": "fill:#f1f8e9,stroke:#558b2f,color:#33691e",
    "external": "fill:#f5f5f5,stroke:#616161,color:#212121",
    "store": "fill:#fffde7,stroke:#f9a825,color:#f57f17",
    "monitor": "fill:#e0f2f1,stroke:#00897b,color:#004d40",
    "obs": "fill:#efebe9,stroke:#6d4c41,color:#3e2723",
}


def to_mermaid(graph: SystemGraph | None = None, *, settings: Settings | None = None) -> str:
    g = graph or system_graph(settings)
    by_group: dict[str, list[Node]] = {}
    for n in g.nodes:
        by_group.setdefault(n.group, []).append(n)

    lines = ["flowchart LR"]
    for gid, title in g.groups:
        members = by_group.get(gid, [])
        if not members:
            continue
        lines.append(f'  subgraph {gid}["{title}"]')
        for n in members:
            lines.append(f'    {n.id}["{n.label}"]')
        lines.append("  end")

    lines.append("")
    for e in g.edges:
        if e.label:
            lines.append(f'  {e.src} -->|{e.label}| {e.dst}')
        else:
            lines.append(f"  {e.src} --> {e.dst}")

    lines.append("")
    for kind, style in _KIND_STYLE.items():
        lines.append(f"  classDef {kind} {style};")
    # Assign classes.
    for kind in _KIND_STYLE:
        ids = [n.id for n in g.nodes if n.kind == kind]
        if ids:
            lines.append(f"  class {','.join(ids)} {kind};")

    return "\n".join(lines)


def to_html(graph: SystemGraph | None = None, *, settings: Settings | None = None) -> str:
    import html as _html

    s = settings or get_settings()
    # Escape so the browser's textContent reconstructs the exact Mermaid source
    # (otherwise raw <br/> inside <pre> is parsed as HTML and the line breaks are
    # lost before mermaid.js reads it).
    mermaid = _html.escape(to_mermaid(graph, settings=s))
    net = "Base mainnet" if s.is_mainnet else "Base Sepolia (testnet)"
    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width, initial-scale=1"/>
<title>jim — system map</title>
<style>
  body {{ font-family: -apple-system, system-ui, sans-serif; margin: 0; background: #0f1419; color: #e6e6e6; }}
  header {{ padding: 18px 24px; border-bottom: 1px solid #233; }}
  header h1 {{ margin: 0; font-size: 20px; }}
  header p {{ margin: 6px 0 0; color: #9bb; font-size: 13px; }}
  .badges {{ margin-top: 10px; }}
  .badge {{ display:inline-block; background:#1b2733; border:1px solid #2c3e50; color:#cde;
            padding:3px 9px; border-radius:12px; font-size:12px; margin-right:6px; }}
  .wrap {{ padding: 16px 24px; }}
  .mermaid {{ background:#fff; border-radius: 10px; padding: 16px; overflow:auto; }}
</style>
</head>
<body>
<header>
  <h1>jim — live system map</h1>
  <p>{s.service_description}</p>
  <div class="badges">
    <span class="badge">network: {net}</span>
    <span class="badge">facilitator: {_short_host(s.facilitator_url)}</span>
    <span class="badge">store: {"Postgres+pgvector" if s.database_url else "in-memory"}</span>
    <span class="badge">debate: {"on" if s.enable_debate else "off"}</span>
    <span class="badge">prices: {"on" if s.enable_prices else "off"}</span>
    <span class="badge">graph: {"live" if s.graph_live else "mock"}</span>
  </div>
</header>
<div class="wrap">
  <pre class="mermaid">
{mermaid}
  </pre>
</div>
<script type="module">
  import mermaid from "https://cdn.jsdelivr.net/npm/mermaid@11/dist/mermaid.esm.min.mjs";
  mermaid.initialize({{ startOnLoad: true, theme: "default", flowchart: {{ curve: "basis" }} }});
</script>
</body>
</html>"""


def to_json(graph: SystemGraph | None = None, *, settings: Settings | None = None) -> dict:
    g = graph or system_graph(settings)
    return g.to_dict()


def _short_addr(addr: str | None) -> str:
    if not addr:
        return "(unset)"
    return f"{addr[:6]}…{addr[-4:]}" if len(addr) > 12 else addr


def _short_host(url: str) -> str:
    return url.replace("https://", "").replace("http://", "").split("/")[0]


# --- CLI: jim-map -----------------------------------------------------------


def main() -> int:
    p = argparse.ArgumentParser(
        prog="jim-map", description="Render the live jim system map (Mermaid)."
    )
    p.add_argument(
        "--format", choices=["mermaid", "html", "json"], default="mermaid", help="Output format."
    )
    p.add_argument("--output", "-o", help="Write to this file instead of stdout.")
    args = p.parse_args()

    settings = get_settings()
    if args.format == "mermaid":
        out = to_mermaid(settings=settings)
    elif args.format == "html":
        out = to_html(settings=settings)
    else:
        out = json.dumps(to_json(settings=settings), indent=2)

    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            f.write(out + ("\n" if not out.endswith("\n") else ""))
        print(f"Wrote {args.format} → {args.output}", file=sys.stderr)
    else:
        print(out)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
