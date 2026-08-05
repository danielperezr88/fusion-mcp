#!/usr/bin/env python3
"""Headless tests for the DuckDB persistence sink (T6).

``_persist_to_duckdb`` writes a structure graph into an in-memory DuckDB
database (``nodes``/``edges`` tables with dynamically-derived columns from
the union of attrs) and caches the connection in a module-level LRU
(``_GRAPH_DBS``, ``MAX_GRAPHS`` = 16).  These tests cover round-trip
counts, the schema columns, an SQL neighbour query, LRU eviction, and
deterministic read queries (the T8 read-only-mode hook).
"""

import json
import os
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import pytest

from mcp_server import mesh_analysis, mesh_graph


@pytest.fixture(autouse=True)
def _clear_graph_db_cache():
    """Each test starts from an empty LRU so key/eviction state is isolated
    and no connection leaks between tests."""
    mesh_graph._GRAPH_DBS.clear()
    yield
    mesh_graph._GRAPH_DBS.clear()


# ---------------------------------------------------------------------------
# fixtures (same style as test_mesh_graph.py: real decompose pipeline)
# ---------------------------------------------------------------------------

def _box():
    nodes = [(0, 0, 0), (2, 0, 0), (2, 2, 0), (0, 2, 0),
             (0, 0, 2), (2, 0, 2), (2, 2, 2), (0, 2, 2)]
    tris = [(0, 2, 1), (0, 3, 2), (4, 5, 6), (4, 6, 7),
            (0, 1, 5), (0, 5, 4), (1, 2, 6), (1, 6, 5),
            (2, 3, 7), (2, 7, 6), (3, 0, 4), (3, 4, 7)]
    return nodes, tris


def _vertex_touch():
    """Two quads sharing only the origin -> VERTEX_TOUCH edge (JSON attr)."""
    nodes = [(0, 0, 0), (2, 0, 0), (2, 2, 0), (0, 2, 0),
             (-2, 0, 0), (-2, 0, 2), (0, 0, 2)]
    tris = [(0, 1, 2), (0, 2, 3), (0, 4, 5), (0, 5, 6)]
    return nodes, tris


def _graph(nodes, tris):
    res = mesh_analysis.decompose_mesh_faces(nodes, tris)
    return mesh_graph.build_structure_graph(res)


def _columns(conn, table):
    return {row[1] for row in conn.execute(
        f"PRAGMA table_info({table})").fetchall()}


# ---------------------------------------------------------------------------
# persistence behaviour
# ---------------------------------------------------------------------------

def test_persist_to_duckdb_round_trip():
    G = _graph(*_box())
    conn = mesh_graph._persist_to_duckdb(G, "box-rt")
    assert conn.execute("SELECT COUNT(*) FROM nodes").fetchone()[0] == \
        G.number_of_nodes()
    assert conn.execute("SELECT COUNT(*) FROM edges").fetchone()[0] == \
        G.number_of_edges()
    # cached connection is the SAME object (identity)
    assert mesh_graph._get_graph_db("box-rt") is conn
    assert conn is mesh_graph._GRAPH_DBS["box-rt"]


def test_duckdb_schema_columns():
    G = _graph(*_box())
    conn = mesh_graph._persist_to_duckdb(G, "box-schema")
    node_cols = _columns(conn, "nodes")
    edge_cols = _columns(conn, "edges")
    # required core columns
    assert {"node_id", "label", "component_id"} <= node_cols
    assert {"source", "target", "relation"} <= edge_cols
    # per-type attr columns from build_structure_graph
    assert {"face_index", "plane_id", "area_cm2", "normal_x", "normal_y",
            "normal_z", "centroid", "vertex_count", "triangle_count",
            "interior_angles", "convexity", "is_base_candidate",
            "is_articulation_point", "mean_curvature", "curve_type",
            "face_count"} <= node_cols
    assert {"dihedral_angle_deg", "shared_edge_length", "orientation",
            "extrusion_direction", "convexity"} <= edge_cols
    # JSON attrs round-trip through json.loads
    centroid = json.loads(conn.execute(
        "SELECT centroid FROM nodes WHERE node_id = 'face:0'").fetchone()[0])
    assert centroid == G.nodes["face:0"]["centroid"]
    angles = json.loads(conn.execute(
        "SELECT interior_angles FROM nodes WHERE node_id = 'face:0'"
    ).fetchone()[0])
    assert angles == G.nodes["face:0"]["interior_angles"]
    # a VERTEX_TOUCH graph adds its JSON/geometry columns to the union
    G2 = _graph(*_vertex_touch())
    conn2 = mesh_graph._persist_to_duckdb(G2, "vt-schema")
    assert {"shared_vertex", "shared_vertex_count"} <= \
        _columns(conn2, "edges")
    vt = [(u, v, d) for u, v, d in G2.edges(data=True)
          if d.get("relation") == "VERTEX_TOUCH"]
    assert len(vt) == 1
    sv = json.loads(conn2.execute(
        "SELECT shared_vertex FROM edges WHERE relation = 'VERTEX_TOUCH'"
    ).fetchone()[0])
    assert sv == list(vt[0][2]["shared_vertex"])


def test_duckdb_query_neighbors():
    G = _graph(*_box())
    mesh_graph._persist_to_duckdb(G, "box-nbr")
    conn = mesh_graph._get_graph_db("box-nbr")
    rows = conn.execute(
        "SELECT e.source, e.target FROM edges e "
        "WHERE e.relation = 'EDGE_ADJACENT' AND "
        "(e.source = 'face:0' OR e.target = 'face:0')").fetchall()
    # a cube face is edge-adjacent to exactly 4 others
    assert len(rows) == 4
    assert all("face:0" in (r[0], r[1]) for r in rows)
    # SQL neighbours match NetworkX neighbours one-to-one
    nx_neighbors = {n for n in G.neighbors("face:0")
                    if G["face:0"][n].get("relation") == "EDGE_ADJACENT"}
    sql_neighbors = {r[1] if r[0] == "face:0" else r[0] for r in rows}
    assert sql_neighbors == nx_neighbors


def test_duckdb_lru_eviction():
    G = _graph(*_box())
    for i in range(mesh_graph.MAX_GRAPHS + 1):
        mesh_graph._persist_to_duckdb(G, f"mesh:{i}")
    assert len(mesh_graph._GRAPH_DBS) == mesh_graph.MAX_GRAPHS
    # the oldest key (mesh:0) was evicted; _get_graph_db reports it gone
    with pytest.raises(KeyError) as exc:
        mesh_graph._get_graph_db("mesh:0")
    msg = str(exc.value)
    assert "mesh:0" in msg
    assert "Call structure_graph first" in msg
    assert "ephemeral" in msg
    # the newest key is reachable and queryable
    conn = mesh_graph._get_graph_db(f"mesh:{mesh_graph.MAX_GRAPHS}")
    assert conn.execute("SELECT COUNT(*) FROM nodes").fetchone()[0] == \
        G.number_of_nodes()


def test_duckdb_read_only_query():
    G = _graph(*_box())
    conn = mesh_graph._persist_to_duckdb(G, "box-ro")
    # plain SELECTs work and are deterministic (repeatable results)
    first = conn.execute("SELECT node_id, label FROM nodes "
                         "ORDER BY node_id").fetchall()
    second = conn.execute("SELECT node_id, label FROM nodes "
                          "ORDER BY node_id").fetchall()
    assert first == second
    assert conn.execute("SELECT COUNT(*) FROM nodes").fetchone()[0] == \
        G.number_of_nodes()
    # every node_id/label row round-trips verbatim
    graph_rows = sorted((n, d["label"]) for n, d in G.nodes(data=True))
    assert sorted(first) == graph_rows


def test_persist_empty_graph():
    G = mesh_graph.build_structure_graph({})
    conn = mesh_graph._persist_to_duckdb(G, "empty")
    # zero-row tables still exist with the core columns (executemany of an
    # empty row list must be skipped, not run)
    assert conn.execute("SELECT COUNT(*) FROM nodes").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM edges").fetchone()[0] == 0
    assert {"node_id", "label"} <= _columns(conn, "nodes")
    assert {"source", "target", "relation"} <= _columns(conn, "edges")
