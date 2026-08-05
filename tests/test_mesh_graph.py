#!/usr/bin/env python3
"""Headless tests for the mesh-graph property-graph builder (T5).

Each test targets one node type, one edge type, or a graph-level contract:

  Nodes:      Face, Hole, CurvedPatch, Component (attrs + labels).
  Edges:      EDGE_ADJACENT, VERTEX_TOUCH, COPLANAR, SAME_ORIENTATION,
              PARALLEL, PERPENDICULAR, CONTAINS, EXTRUSION_ALIGNED,
              COMPONENT_OF, HAS_BASE.
  Graph:      one edge per pair, JSON-serialisable attrs, determinism,
              empty-decompose handling.
"""

import math
import os
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

import networkx as nx

from mcp_server import mesh_analysis, mesh_graph


def _graph(nodes, tris):
    """decompose -> build_structure_graph (real pipeline, not hand-made dicts)."""
    res = mesh_analysis.decompose_mesh_faces(nodes, tris)
    return mesh_graph.build_structure_graph(res)


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


def _vertex_touch():
    """Two quads sharing only the origin -> VERTEX_TOUCH (not adjacent)."""
    nodes = [(0, 0, 0), (2, 0, 0), (2, 2, 0), (0, 2, 0),
             (-2, 0, 0), (-2, 0, 2), (0, 0, 2)]
    tris = [(0, 1, 2), (0, 2, 3), (0, 4, 5), (0, 5, 6)]
    return nodes, tris


def _near_parallel():
    """Square at z=0 and same square tilted 0.8 deg about x, offset z+2."""
    th = math.radians(0.8)
    cos_t, sin_t = math.cos(th), math.sin(th)
    a = [(0, 0, 0), (2, 0, 0), (2, 2, 0), (0, 2, 0)]
    b = [(x, y * cos_t - z * sin_t, y * sin_t + z * cos_t + 2) for x, y, z in a]
    nodes = a + b
    tris = [(0, 1, 2), (0, 2, 3), (4, 5, 6), (4, 6, 7)]
    return nodes, tris


def _perpendicular():
    """Quad in XY plane and separated quad in YZ plane (normal along Y)."""
    nodes = [(0, 0, 0), (2, 0, 0), (2, 2, 0), (0, 2, 0),
             (0, 5, 0), (0, 5, 2), (2, 5, 2), (2, 5, 0)]
    tris = [(0, 1, 2), (0, 2, 3), (4, 5, 6), (4, 6, 7)]
    return nodes, tris


def _coplanar_separated():
    """Two separated 1x1 coplanar quads -> COPLANAR (same plane, no touch)."""
    nodes = [(0, 0, 0), (1, 0, 0), (1, 1, 0), (0, 1, 0),
             (3, 0, 0), (4, 0, 0), (4, 1, 0), (3, 1, 0)]
    tris = [(0, 1, 2), (0, 2, 3), (4, 5, 6), (4, 6, 7)]
    return nodes, tris


def _parallel_offset():
    """Two parallel quads at z=0 and z=2, no connecting walls."""
    nodes = [(0, 0, 0), (2, 0, 0), (2, 2, 0), (0, 2, 0),
             (0, 0, 2), (2, 0, 2), (2, 2, 2), (0, 2, 2)]
    tris = [(0, 1, 2), (0, 2, 3), (4, 5, 6), (4, 6, 7)]
    return nodes, tris


def _frame():
    """Outer square with inner square hole -> one face with one hole."""
    nodes = [(0, 0, 0), (4, 0, 0), (4, 4, 0), (0, 4, 0),
             (1, 1, 0), (3, 1, 0), (3, 3, 0), (1, 3, 0)]
    tris = [(0, 1, 5), (0, 5, 4), (1, 2, 6), (1, 6, 5),
            (2, 3, 7), (2, 7, 6), (3, 0, 4), (3, 4, 7)]
    return nodes, tris


def _curved_patch():
    """Quad plus two degenerate (zero-area) tris -> freeform curved patch."""
    nodes = [(0, 0, 0), (2, 0, 0), (2, 2, 0), (0, 2, 0), (4, 0, 0), (6, 0, 0)]
    tris = [(0, 1, 2), (0, 2, 3), (0, 4, 5), (0, 5, 1)]
    return nodes, tris


# ---------------------------------------------------------------------------
# node types
# ---------------------------------------------------------------------------

def test_face_node_attrs():
    G = _graph(*_box())
    assert G.number_of_nodes() == 7
    faces = [n for n, d in G.nodes(data=True) if d["label"] == "Face"]
    assert len(faces) == 6
    for fid in sorted(faces):
        d = G.nodes[fid]
        assert d["node_id"] == fid
        assert d["label"] == "Face"
        assert d["component_id"] == 0
        assert isinstance(d["face_index"], int)
        assert isinstance(d["plane_id"], str) and d["plane_id"].count("|") == 3
        assert d["area_cm2"] == 4.0
        assert isinstance(d["normal_x"], float)
        assert isinstance(d["normal_y"], float)
        assert isinstance(d["normal_z"], float)
        assert len(d["centroid"]) == 3
        assert d["vertex_count"] == 4
        assert d["triangle_count"] == 2
        assert d["interior_angles"] == [90.0] * 4
        assert d["convexity"] == "convex"
        assert d["is_base_candidate"] is False
        assert d["is_articulation_point"] is False
        assert d["mean_curvature"] == 0.0
        assert d["curve_type"] == "planar"


def test_hole_node_attrs():
    G = _graph(*_frame())
    holes = [n for n, d in G.nodes(data=True) if d["label"] == "Hole"]
    assert len(holes) == 1
    hid = holes[0]
    d = G.nodes[hid]
    assert d["node_id"] == hid
    assert d["label"] == "Hole"
    assert d["containing_face_id"] == "face:0"
    assert d["area_cm2"] == 4.0  # inner 2x2 square (Newell area)
    assert d["is_filled"] is False


def test_curved_patch_node_attrs():
    G = _graph(*_curved_patch())
    patches = [n for n, d in G.nodes(data=True) if d["label"] == "CurvedPatch"]
    assert len(patches) == 1
    cid = patches[0]
    d = G.nodes[cid]
    assert d["node_id"] == cid
    assert d["label"] == "CurvedPatch"
    assert d["component_id"] == 0
    assert d["triangle_count"] == 2
    assert d["curve_type"] == "freeform"
    assert d["area_cm2"] == 0.0


def test_component_node_attrs():
    G = _graph(*_box())
    comps = [n for n, d in G.nodes(data=True) if d["label"] == "Component"]
    assert len(comps) == 1
    assert comps[0] == "component:0"
    d = G.nodes[comps[0]]
    assert d["node_id"] == "component:0"
    assert d["label"] == "Component"
    assert d["component_id"] == 0
    assert d["face_count"] == 6


# ---------------------------------------------------------------------------
# edge types
# ---------------------------------------------------------------------------

def test_edge_adjacent():
    G = _graph(*_box())
    adj = [(a, b, d) for a, b, d in G.edges(data=True)
           if d.get("relation") == "EDGE_ADJACENT"]
    assert len(adj) == 12
    for a, b, d in adj:
        assert d["dihedral_angle_deg"] == 90.0
        assert d["convexity"] == "convex"
        assert d["shared_edge_length"] == 2.0
        assert d["orientation"] in ("FORWARD", "REVERSED")


def test_edge_vertex_touch():
    G = _graph(*_vertex_touch())
    vt = [(a, b, d) for a, b, d in G.edges(data=True)
          if d.get("relation") == "VERTEX_TOUCH"]
    assert len(vt) == 1
    a, b, d = vt[0]
    assert d["shared_vertex"] == (0.0, 0.0, 0.0)
    assert d["shared_vertex_count"] == 1


def test_edge_coplanar():
    G = _graph(*_coplanar_separated())
    co = [(a, b, d) for a, b, d in G.edges(data=True)
          if d.get("relation") == "COPLANAR"]
    assert len(co) == 1
    assert co[0][2]["normal_dot"] == 1.0


def test_edge_same_orientation():
    G = _graph(*_parallel_offset())
    so = [(a, b, d) for a, b, d in G.edges(data=True)
          if d.get("relation") == "SAME_ORIENTATION"]
    assert len(so) == 1
    d = so[0][2]
    assert d["normal_x"] == 0.0 and d["normal_y"] == 0.0 and d["normal_z"] == 1.0


def test_edge_parallel():
    G = _graph(*_near_parallel())
    par = [(a, b, d) for a, b, d in G.edges(data=True)
           if d.get("relation") == "PARALLEL"]
    assert len(par) == 1
    assert abs(par[0][2]["normal_dot"] - math.cos(math.radians(0.8))) < 1e-5


def test_edge_perpendicular():
    G = _graph(*_perpendicular())
    perp = [(a, b, d) for a, b, d in G.edges(data=True)
            if d.get("relation") == "PERPENDICULAR"]
    assert len(perp) == 1
    assert perp[0][2]["normal_dot"] == 0.0


def test_edge_contains_face_hole():
    G = _graph(*_frame())
    con = [(a, b, d) for a, b, d in G.edges(data=True)
           if d.get("relation") == "CONTAINS"]
    assert con == [("face:0", "hole:0", {"relation": "CONTAINS"})]


def test_edge_contains_face_curved():
    G = _graph(*_curved_patch())
    con = [(a, b, d) for a, b, d in G.edges(data=True)
           if d.get("relation") == "CONTAINS"]
    assert con == [("face:0", "curved:0", {"relation": "CONTAINS"})]


def test_edge_extrusion_aligned():
    G = _graph(*_box())
    ex = [(a, b, d) for a, b, d in G.edges(data=True)
          if d.get("relation") == "EXTRUSION_ALIGNED"]
    assert len(ex) == 3
    for a, b, d in ex:
        assert len(d["extrusion_direction"]) == 3
    # top-bottom pair carries the extrusion relation (was SAME_ORIENTATION)
    assert G["face:0"]["face:1"]["relation"] == "EXTRUSION_ALIGNED"


def test_edge_component_of():
    G = _graph(*_curved_patch())
    co = [(a, b, d) for a, b, d in G.edges(data=True)
          if d.get("relation") == "COMPONENT_OF"]
    assert co == [("curved:0", "component:0", {"relation": "COMPONENT_OF"})]


def test_edge_has_base():
    G = _graph(*_box())
    hb = [(a, b) for a, b, d in G.edges(data=True)
          if d.get("relation") == "HAS_BASE"]
    assert len(hb) == 6
    for a, b in hb:
        assert frozenset((a, b)) in {frozenset(("component:0", f"face:{i}"))
                                     for i in range(6)}


# ---------------------------------------------------------------------------
# graph-level contracts
# ---------------------------------------------------------------------------

def test_one_edge_per_pair():
    G = _graph(*_box())
    seen = set()
    for a, b in G.edges():
        key = tuple(sorted((a, b)))
        assert key not in seen, "duplicate node pair"
        seen.add(key)


def test_attrs_json_serialisable():
    import json
    G = _graph(*_box())
    for n, d in G.nodes(data=True):
        json.dumps(d)  # raises if not serialisable
    for a, b, d in G.edges(data=True):
        json.dumps(d)


def test_graph_level_attrs():
    G = _graph(*_box())
    assert G.graph["has_warnings"] is False
    assert G.graph["components_detected"] == 1


def test_determinism():
    n1, t1 = _box()
    res = mesh_analysis.decompose_mesh_faces(n1, t1)
    G1 = mesh_graph.build_structure_graph(res)
    G2 = mesh_graph.build_structure_graph(res)
    assert nx.to_dict_of_dicts(G1) == nx.to_dict_of_dicts(G2)
    assert {n: dict(d) for n, d in G1.nodes(data=True)} == \
        {n: dict(d) for n, d in G2.nodes(data=True)}
    assert G1.graph == G2.graph


def test_empty_decompose():
    G = mesh_graph.build_structure_graph({})
    assert G.number_of_nodes() == 0
    assert G.number_of_edges() == 0
    assert G.graph["has_warnings"] is False
    assert G.graph["components_detected"] == 0
