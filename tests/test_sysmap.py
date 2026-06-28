"""Phase 5 live system map — the visualization generator.

Hermetic: ``system_graph`` takes an injected ``Settings``, so the map reflects
exactly the flags we pass, with no env/network dependency.
"""

from __future__ import annotations

from jim.config import BASE_MAINNET, BASE_SEPOLIA, Settings
from jim.marketplace.sysmap import system_graph, to_html, to_json, to_mermaid

_ADDR = "0x" + "1" * 40


def _settings(**kw) -> Settings:
    base = dict(network=BASE_SEPOLIA, evm_address=_ADDR)
    base.update(kw)
    return Settings(**base)


def test_graph_has_layers_and_products() -> None:
    g = system_graph(_settings())
    ids = {n.id for n in g.nodes}
    # The full stack the user asked to see: buyers → discovery → rails → engine →
    # gates → sources → externals → store → monitors → observability.
    for expected in (
        "mcp_agent", "catalog", "mw", "gather", "gate", "graph_src", "edgar",
        "store", "monitor_sched", "langfuse", "route_fundamentals", "route_token",
    ):
        assert expected in ids, expected


def test_mermaid_is_wellformed_and_has_no_dangling_edges() -> None:
    g = system_graph(_settings())
    node_ids = {n.id for n in g.nodes}
    # Every edge must connect two declared nodes (no orphan references).
    for e in g.edges:
        assert e.src in node_ids, e.src
        assert e.dst in node_ids, e.dst

    mer = to_mermaid(g)
    assert mer.startswith("flowchart LR")
    assert "subgraph buyers" in mer
    assert "classDef" in mer
    # Line breaks use <br/> (renders on GitHub + mermaid.js), not raw \n.
    assert "\\n" not in mer


def test_graph_reflects_feature_flags() -> None:
    debate_off = {n.id for n in system_graph(_settings(enable_debate=False)).nodes}
    assert "debate" not in debate_off

    debate_on = {n.id for n in system_graph(_settings(enable_debate=True)).nodes}
    assert "debate" in debate_on

    # GRAPH_LIVE repoints the upstream at Base mainnet.
    live = system_graph(_settings(graph_live=True))
    thegraph = next(n for n in live.nodes if n.id == "thegraph")
    assert BASE_MAINNET in thegraph.label

    mock = system_graph(_settings(graph_live=False))
    thegraph_mock = next(n for n in mock.nodes if n.id == "thegraph")
    assert "Mock" in thegraph_mock.label


def test_store_backend_label_tracks_database_url() -> None:
    pg = system_graph(_settings(database_url="postgresql+asyncpg://x/y"))
    assert any("Postgres" in n.label for n in pg.nodes if n.id == "store")
    mem = system_graph(_settings(database_url=None))
    assert any("In-memory" in n.label for n in mem.nodes if n.id == "store")


def test_json_export_round_trips_structure() -> None:
    data = to_json(settings=_settings())
    assert {"groups", "nodes", "edges"} <= set(data)
    assert len(data["nodes"]) == len({n["id"] for n in data["nodes"]})  # unique ids


def test_html_is_self_contained_page() -> None:
    html = to_html(settings=_settings())
    assert "<!doctype html>" in html.lower()
    assert "mermaid" in html
    assert "flowchart LR" in html
