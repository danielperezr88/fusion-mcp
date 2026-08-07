#!/usr/bin/env python3
"""Headless tests for the workstream computation (T9) in mesh_graph.py.

Covers: _score_base_faces plus full pipeline integration
(decompose -> build -> persist -> SQL).

Hand-built graph tests are used where the real decompose pipeline cannot
cheaply produce all six unit types.  The box fixture goes through the
real decompose pipeline for integration coverage.
"""

import json
import math
import os
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import networkx as nx
import pytest

from mcp_server import mesh_analysis, mesh_graph


@pytest.fixture(autouse=True)
def _clear_graph_db_cache():
    mesh_graph._GRAPH_DBS.clear()
    yield
    mesh_graph._GRAPH_DBS.clear()


# ---------------------------------------------------------------------------
# fixtures
# ---------------------------------------------------------------------------

def _box():
    nodes = [(0, 0, 0), (2, 0, 0), (2, 2, 0), (0, 2, 0),
             (0, 0, 2), (2, 0, 2), (2, 2, 2), (0, 2, 2)]
    tris = [(0, 2, 1), (0, 3, 2), (4, 5, 6), (4, 6, 7),
            (0, 1, 5), (0, 5, 4), (1, 2, 6), (1, 6, 5),
            (2, 3, 7), (2, 7, 6), (3, 0, 4), (3, 4, 7)]
    return nodes, tris


def _graph(nodes, tris):
    res = mesh_analysis.decompose_mesh_faces(nodes, tris)
    return mesh_graph.build_structure_graph(res)


# ---------------------------------------------------------------------------
# hand-built graph helpers (for tests where decompose cannot produce all types)
# ---------------------------------------------------------------------------

def _mk_face(graph, fid, area_cm2, normal_x, normal_y, normal_z, face_index=0,
             component_id=0):
    graph.add_node(fid, node_id=fid, label="Face", component_id=component_id,
                   face_index=face_index, area_cm2=area_cm2,
                   normal_x=normal_x, normal_y=normal_y, normal_z=normal_z,
                   centroid=[0.0, 0.0, 0.0], vertex_count=4,
                   triangle_count=2, interior_angles=[90.0] * 4,
                   convexity="convex", is_base_candidate=False,
                   is_articulation_point=False, mean_curvature=0.0,
                   curve_type="planar")


def _mk_component(graph, cid, face_count=0):
    nid = f"component:{cid}"
    graph.add_node(nid, node_id=nid, label="Component",
                   component_id=cid, face_count=face_count)
    return nid


def _mk_curved(graph, pid, component_id=0, curve_type="freeform", area_cm2=0.0,
               triangle_count=2):
    nid = f"curved:{pid}"
    graph.add_node(nid, node_id=nid, label="CurvedPatch",
                   component_id=component_id, curve_type=curve_type,
                   area_cm2=area_cm2, triangle_count=triangle_count)
    return nid


def _mk_hole(graph, hid, containing_face_id="face:0", area_cm2=1.0):
    nid = f"hole:{hid}"
    graph.add_node(nid, node_id=nid, label="Hole",
                   containing_face_id=containing_face_id,
                   area_cm2=area_cm2, is_filled=False)
    return nid


def _add_edge_adjacent(graph, a, b, convexity="convex",
                       shared_edge_length=2.0, dihedral_angle_deg=90.0,
                       orientation="FORWARD"):
    graph.add_edge(a, b, relation="EDGE_ADJACENT",
                   convexity=convexity,
                   shared_edge_length=shared_edge_length,
                   dihedral_angle_deg=dihedral_angle_deg,
                   orientation=orientation)


def _add_has_base(graph, comp_nid, face_nid):
    graph.add_edge(comp_nid, face_nid, relation="HAS_BASE")


def _add_contains(graph, face_nid, other_nid):
    graph.add_edge(face_nid, other_nid, relation="CONTAINS")


def _add_component_of(graph, curved_nid, comp_nid):
    graph.add_edge(curved_nid, comp_nid, relation="COMPONENT_OF")


# =====================================================================
# _score_base_faces tests
# =====================================================================

def test_composite_score_formula():
    """Hand-built graph with known attrs -> exact composite scores."""
    G = nx.Graph()
    _mk_face(G, "face:0", area_cm2=10.0, normal_x=0, normal_y=0, normal_z=1,
             face_index=0, component_id=0)
    _mk_face(G, "face:1", area_cm2=5.0, normal_x=0, normal_y=0, normal_z=1,
             face_index=1, component_id=0)
    _mk_face(G, "face:2", area_cm2=2.0, normal_x=0, normal_y=0, normal_z=-1,
             face_index=2, component_id=0)
    _mk_component(G, 0, face_count=3)

    # face:0 has 2 EDGE_ADJACENT neighbours, face:1 has 1, face:2 has 0
    _add_edge_adjacent(G, "face:0", "face:1")
    _add_edge_adjacent(G, "face:0", "face:2")

    # face:0 has CONTAINS -> hole
    _mk_hole(G, 0, containing_face_id="face:0")
    _add_contains(G, "face:0", "hole:0")

    for fid in ("face:0", "face:1", "face:2"):
        _add_has_base(G, "component:0", fid)

    # call scoring
    scores = mesh_graph._score_base_faces(G, {})

    # verify scores exactly
    # max_area = 10.0, max_degree = 2
    # face:0: area_norm=1.0, degree_norm=1.0, floor_facing=1.0 (nz=1>0),
    #         contains_holes=1.0, axis_alignment=1.0 (canonical normal (0,0,1) appears
    #         on face:0 AND face:1 -> >=2 -> dominant)
    # score = 0.35*1 + 0.25*1 + 0.15*1 + 0.15*1 + 0.10*1 = 1.0
    assert scores["face:0"] == 1.0

    # face:1: area_norm=0.5, degree_norm=0.5, floor_facing=1.0,
    #         contains_holes=0.0, axis_alignment=1.0
    # score = 0.35*0.5 + 0.25*0.5 + 0.15*1 + 0.15*0 + 0.10*1 = 0.175+0.125+0.15+0+0.10 = 0.55
    assert scores["face:1"] == 0.55

    # face:2: area_norm=0.2, degree_norm=0.5, floor_facing=0.0 (nz=-1<0),
    #         contains_holes=0.0, axis_alignment=1.0
    # score = 0.35*0.2 + 0.25*0.5 + 0.15*0 + 0.15*0 + 0.10*1 = 0.07+0.125+0+0+0.10 = 0.295
    assert scores["face:2"] == 0.295

    # is_base_candidate: face:0 wins (highest score)
    assert G.nodes["face:0"]["is_base_candidate"] is True
    assert G.nodes["face:1"]["is_base_candidate"] is False
    assert G.nodes["face:2"]["is_base_candidate"] is False

    # base_score on face nodes
    assert G.nodes["face:0"]["base_score"] == 1.0
    assert G.nodes["face:1"]["base_score"] == 0.55
    assert G.nodes["face:2"]["base_score"] == 0.295

    # base_score on Component = max member score
    assert G.nodes["component:0"]["base_score"] == 1.0


def test_score_base_faces_empty_graph():
    G = mesh_graph.build_structure_graph({})
    scores = mesh_graph._score_base_faces(G, {})
    assert scores == {}


def test_score_base_faces_no_faces():
    G = nx.Graph()
    _mk_component(G, 0, face_count=0)
    scores = mesh_graph._score_base_faces(G, {})
    assert scores == {}
    # Component with no faces gets base_score 0.0
    assert G.nodes["component:0"]["base_score"] == 0.0


def test_score_base_faces_axis_alignment_empty():
    """All normals unique -> dominant set empty -> axis_alignment=0 for all."""
    G = nx.Graph()
    _mk_face(G, "face:0", area_cm2=10.0, normal_x=1, normal_y=0, normal_z=0,
             face_index=0, component_id=0)
    _mk_face(G, "face:1", area_cm2=8.0, normal_x=0, normal_y=1, normal_z=0,
             face_index=1, component_id=0)
    _mk_face(G, "face:2", area_cm2=6.0, normal_x=0, normal_y=0, normal_z=1,
             face_index=2, component_id=0)
    _mk_component(G, 0, face_count=3)
    for fid in ("face:0", "face:1", "face:2"):
        _add_has_base(G, "component:0", fid)

    scores = mesh_graph._score_base_faces(G, {})

    # axis_alignment=0 for all (all normals unique), max_area=10, max_degree=0
    # face:0 nz=0: 0.35*1 + 0.25*0 + 0.15*0 + 0.15*0 + 0.10*0 = 0.35
    # face:1 nz=0: 0.35*0.8 + 0.15*0 = 0.28
    # face:2 nz=1: 0.35*0.6 + 0.15*1 = 0.21 + 0.15 = 0.36
    assert scores["face:0"] == 0.35
    assert scores["face:1"] == 0.28
    assert scores["face:2"] == 0.36


def test_score_base_faces_tie_break():
    """Multiple faces with same score: tie-break by area, then face_index."""
    G = nx.Graph()
    # all identical area, same normal -> dominant -> axis_alignment=1
    for i in range(3):
        _mk_face(G, f"face:{i}", area_cm2=4.0, normal_x=0, normal_y=0,
                 normal_z=1.0, face_index=i, component_id=0)
    _mk_component(G, 0, face_count=3)
    for i in range(3):
        _add_has_base(G, "component:0", f"face:{i}")

    scores = mesh_graph._score_base_faces(G, {})

    # all same score -> tie-break area (all 4.0) -> lowest face_index
    assert G.nodes["face:0"]["is_base_candidate"] is True
    assert G.nodes["face:1"]["is_base_candidate"] is False
    assert G.nodes["face:2"]["is_base_candidate"] is False


def test_score_base_faces_tie_break_by_area():
    """Same score, different areas: larger area wins."""
    G = nx.Graph()
    # both same normal_z, no edges -> score diff only from area_norm
    _mk_face(G, "face:0", area_cm2=8.0, normal_x=0, normal_y=0, normal_z=1,
             face_index=0, component_id=0)
    _mk_face(G, "face:1", area_cm2=4.0, normal_x=0, normal_y=0, normal_z=1,
             face_index=1, component_id=0)
    _mk_component(G, 0, face_count=2)
    for fid in ("face:0", "face:1"):
        _add_has_base(G, "component:0", fid)

    mesh_graph._score_base_faces(G, {})
    # face:0 has higher score (larger area_norm)
    assert G.nodes["face:0"]["is_base_candidate"] is True
    assert G.nodes["face:1"]["is_base_candidate"] is False


# =====================================================================
# full pipeline integration (box through decompose -> build -> persist -> SQL)
# =====================================================================

def test_box_through_full_pipeline():
    """Real decompose -> build_structure_graph -> _persist_to_duckdb -> SQL queries.
    Verifies all T9 columns are present and queryable."""
    G = _graph(*_box())
    conn = mesh_graph._persist_to_duckdb(G, "0")

    # exactly one is_base_candidate face
    base_faces = conn.execute(
        "SELECT node_id FROM nodes WHERE label='Face' AND is_base_candidate = TRUE"
    ).fetchall()
    assert len(base_faces) == 1
    assert base_faces[0][0] == "face:0"

    # base_score column present on Face nodes (box: top/bottom faces get
    # floor_facing=1 -> score=0.85; side faces floor_facing=0 -> score=0.70)
    face_scores = conn.execute(
        "SELECT node_id, base_score FROM nodes WHERE label='Face' ORDER BY node_id"
    ).fetchall()
    assert len(face_scores) == 6
    score_values = [s for _, s in face_scores]
    # exactly 2 faces have floor_facing=1 (top + bottom, both canonical nz>0)
    assert sum(1 for s in score_values if round(s, 6) == 0.85) == 2
    assert sum(1 for s in score_values if round(s, 6) == 0.70) == 4
    for fid, score in face_scores:
        assert isinstance(score, float)

    # base_score column present on Component nodes
    comp_scores = conn.execute(
        "SELECT node_id, base_score FROM nodes WHERE label='Component' ORDER BY node_id"
    ).fetchall()
    assert len(comp_scores) == 1
    assert comp_scores[0][0] == "component:0"
    assert round(comp_scores[0][1], 6) == 0.85

    # dag_has_cycles False (queried via graph-level - it's in G.graph, not DuckDB)
    assert G.graph["dag_has_cycles"] is False


def test_box_pipeline_column_presence():
    """Verify all T9 columns appear in DuckDB for a box."""
    G = _graph(*_box())
    conn = mesh_graph._persist_to_duckdb(G, "0")

    node_cols = {row[1] for row in conn.execute(
        "PRAGMA table_info(nodes)").fetchall()}
    assert "base_score" in node_cols
    assert "is_base_candidate" in node_cols


def test_box_pipeline_is_base_candidate_per_component():
    """Exactly ONE is_base_candidate face across the whole box."""
    G = _graph(*_box())
    base_faces = [n for n, d in G.nodes(data=True)
                  if d.get("label") == "Face" and d.get("is_base_candidate")]
    assert len(base_faces) == 1
    assert base_faces[0] == "face:0"


def test_box_pipeline_no_regression_on_determinism():
    """Determinism: two box builds produce identical workstream attrs."""
    n1, t1 = _box()
    res1 = mesh_analysis.decompose_mesh_faces(n1, t1)
    G1 = mesh_graph.build_structure_graph(res1)
    G2 = mesh_graph.build_structure_graph(res1)  # same decompose result

    # node attrs must match
    for n in G1.nodes():
        d1 = dict(G1.nodes[n])
        d2 = dict(G2.nodes[n])
        assert d1 == d2, f"node {n} differs"

    # graph-level attrs match
    assert G1.graph == G2.graph
