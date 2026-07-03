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


def test_no_node_id_collides_with_a_subgraph_id() -> None:
    """Regression: a node id equal to its subgraph's id ("store" used to be
    both) makes Mermaid 11 fail with "Syntax error in text" at render time —
    this is invisible to a plain "are ids unique among nodes" check."""
    g = system_graph(_settings())
    node_ids = {n.id for n in g.nodes}
    group_ids = {gid for gid, _ in g.groups}
    assert node_ids & group_ids == set()


def test_edge_labels_with_punctuation_survive_mermaid_quoting() -> None:
    """Regression: an unquoted `-->|label|` edge label containing "(" / ")"
    (e.g. the memo-cache hit label) doesn't parse in Mermaid 11 even though the
    identical text is fine inside a quoted node label — every edge label must
    render quoted."""
    mer = to_mermaid(system_graph(_settings()))
    for line in mer.splitlines():
        if "-->|" in line:
            label = line.split("-->|", 1)[1].rsplit("|", 1)[0]
            assert label.startswith('"') and label.endswith('"'), line


def test_peer_sources_appear_as_composed_nodes() -> None:
    g = system_graph(
        _settings(
            peer_sources='[{"name":"mock-sentiment","url":"http://localhost:4021/mock-peer/research"}]'
        )
    )
    ids = {n.id for n in g.nodes}
    # The peer slug is sanitized (no "-") so it can't be confused with the
    # mermaid "-->" arrow token when embedded in an edge.
    assert "peer_mock_sentiment" in ids
    node_ids = ids
    for e in g.edges:
        assert e.src in node_ids, e.src
        assert e.dst in node_ids, e.dst

    no_peers = {n.id for n in system_graph(_settings(peer_sources=None)).nodes}
    assert not any(i.startswith("peer_") for i in no_peers)


def test_agent_economy_and_horizon1_nodes_present() -> None:
    """The map should show what's actually wired up: cross-agent call-chain
    guard, the trust ledger, and the Horizon 1 proof/agent-card surfaces."""
    ids = {n.id for n in system_graph(_settings()).nodes}
    for expected in ("call_chain", "trust_ledger", "proof", "agent_card", "resilience"):
        assert expected in ids, expected


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
