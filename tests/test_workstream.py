#!/usr/bin/env python3
"""Headless tests for the workstream computation (T9) in mesh_graph.py.

Covers: _score_base_faces, _classify_units, _build_dependency_order,
plus full pipeline integration (decompose -> build -> persist -> SQL).

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
# _classify_units tests
# =====================================================================

def test_classify_base_box():
    """Box (real pipeline): exactly one base component."""
    G = _graph(*_box())
    # workstream already ran via build_structure_graph wiring
    comp = G.nodes["component:0"]
    assert comp["unit_type"] == "base"


def test_classify_protrusion():
    """Hand-built: non-base component with convex edges to base."""
    G = nx.Graph()
    # component 0 = base
    _mk_face(G, "face:base", area_cm2=10.0, normal_x=0, normal_y=0, normal_z=1,
             face_index=0, component_id=0)
    _mk_component(G, 0, face_count=1)
    _add_has_base(G, "component:0", "face:base")
    G.nodes["face:base"]["is_base_candidate"] = True

    # component 1 = protrusion (convex-attached to base)
    _mk_face(G, "face:protr", area_cm2=4.0, normal_x=1, normal_y=0, normal_z=0,
             face_index=1, component_id=1)
    _mk_component(G, 1, face_count=1)
    _add_has_base(G, "component:1", "face:protr")
    _add_edge_adjacent(G, "face:base", "face:protr", convexity="convex")

    mesh_graph._score_base_faces(G, {})
    mesh_graph._classify_units(G, {})

    assert G.nodes["component:0"]["unit_type"] == "base"
    assert G.nodes["component:1"]["unit_type"] == "protrusion"


def test_classify_depression():
    """Non-base component with concave edges to base."""
    G = nx.Graph()
    _mk_face(G, "face:base", area_cm2=10.0, normal_x=0, normal_y=0, normal_z=1,
             face_index=0, component_id=0)
    _mk_component(G, 0, face_count=1)
    _add_has_base(G, "component:0", "face:base")
    G.nodes["face:base"]["is_base_candidate"] = True

    _mk_face(G, "face:dep", area_cm2=4.0, normal_x=1, normal_y=0, normal_z=0,
             face_index=1, component_id=1)
    _mk_component(G, 1, face_count=1)
    _add_has_base(G, "component:1", "face:dep")
    _add_edge_adjacent(G, "face:base", "face:dep", convexity="concave")

    mesh_graph._score_base_faces(G, {})
    mesh_graph._classify_units(G, {})

    assert G.nodes["component:0"]["unit_type"] == "base"
    assert G.nodes["component:1"]["unit_type"] == "depression"


def test_classify_hole():
    """Component with cylindrical curved patch -> hole."""
    G = nx.Graph()
    _mk_face(G, "face:base", area_cm2=10.0, normal_x=0, normal_y=0, normal_z=1,
             face_index=0, component_id=0)
    _mk_component(G, 0, face_count=1)
    _add_has_base(G, "component:0", "face:base")
    G.nodes["face:base"]["is_base_candidate"] = True

    # hole component has CurvedPatch with curve_type="cylindrical"
    _mk_component(G, 1, face_count=0)
    _mk_curved(G, 0, component_id=1, curve_type="cylindrical", area_cm2=0.5)
    _add_component_of(G, "curved:0", "component:1")

    mesh_graph._score_base_faces(G, {})
    mesh_graph._classify_units(G, {})

    assert G.nodes["component:0"]["unit_type"] == "base"
    assert G.nodes["component:1"]["unit_type"] == "hole"


def test_classify_hole_conical():
    """Component with conical curved patch -> hole."""
    G = nx.Graph()
    _mk_face(G, "face:base", area_cm2=10.0, normal_x=0, normal_y=0, normal_z=1,
             face_index=0, component_id=0)
    _mk_component(G, 0, face_count=1)
    _add_has_base(G, "component:0", "face:base")
    G.nodes["face:base"]["is_base_candidate"] = True

    _mk_component(G, 1, face_count=0)
    _mk_curved(G, 0, component_id=1, curve_type="conical")
    _add_component_of(G, "curved:0", "component:1")

    mesh_graph._score_base_faces(G, {})
    mesh_graph._classify_units(G, {})

    assert G.nodes["component:1"]["unit_type"] == "hole"


def test_classify_fillet():
    """Small curved component with 'fillet' curve_type -> fillet."""
    G = nx.Graph()
    _mk_face(G, "face:base", area_cm2=100.0, normal_x=0, normal_y=0, normal_z=1,
             face_index=0, component_id=0)
    _mk_component(G, 0, face_count=1)
    _add_has_base(G, "component:0", "face:base")
    G.nodes["face:base"]["is_base_candidate"] = True

    _mk_component(G, 1, face_count=0)
    _mk_curved(G, 0, component_id=1, curve_type="fillet_round", area_cm2=1.0,
               triangle_count=10)
    _add_component_of(G, "curved:0", "component:1")

    mesh_graph._score_base_faces(G, {})
    mesh_graph._classify_units(G, {})

    assert G.nodes["component:1"]["unit_type"] == "fillet"


def test_classify_chamfer():
    """Small curved component without 'fillet' -> chamfer."""
    G = nx.Graph()
    _mk_face(G, "face:base", area_cm2=100.0, normal_x=0, normal_y=0, normal_z=1,
             face_index=0, component_id=0)
    _mk_component(G, 0, face_count=1)
    _add_has_base(G, "component:0", "face:base")
    G.nodes["face:base"]["is_base_candidate"] = True

    # a face in the chamfer component (so component has non-zero area)
    _mk_face(G, "face:ch", area_cm2=5.0, normal_x=1, normal_y=0, normal_z=0,
             face_index=1, component_id=1)
    _mk_component(G, 1, face_count=1)
    _add_has_base(G, "component:1", "face:ch")
    _mk_curved(G, 0, component_id=1, curve_type="freeform", area_cm2=0.5)
    _add_component_of(G, "curved:0", "component:1")

    # must also be EDGE_ADJACENT to base to be classified at all (hmm, no -
    # fillet/chamfer is checked BEFORE edge majority)
    # Actually fillet/chamfer condition doesn't require EDGE_ADJACENT - it's
    # based on area + curved patches

    mesh_graph._score_base_faces(G, {})
    mesh_graph._classify_units(G, {})

    assert G.nodes["component:1"]["unit_type"] == "chamfer"


def test_classify_freeform():
    """Component with no edges to base and no curved patches -> freeform."""
    G = nx.Graph()
    _mk_face(G, "face:base", area_cm2=10.0, normal_x=0, normal_y=0, normal_z=1,
             face_index=0, component_id=0)
    _mk_component(G, 0, face_count=1)
    _add_has_base(G, "component:0", "face:base")
    G.nodes["face:base"]["is_base_candidate"] = True

    _mk_face(G, "face:ff", area_cm2=5.0, normal_x=0, normal_y=1, normal_z=0,
             face_index=1, component_id=1)
    _mk_component(G, 1, face_count=1)
    _add_has_base(G, "component:1", "face:ff")
    # no EDGE_ADJACENT between face:base and face:ff

    mesh_graph._score_base_faces(G, {})
    mesh_graph._classify_units(G, {})

    assert G.nodes["component:0"]["unit_type"] == "base"
    assert G.nodes["component:1"]["unit_type"] == "freeform"


def test_classify_empty_component_freeform():
    """Component with no faces, no patches, no edges -> freeform."""
    G = nx.Graph()
    _mk_face(G, "face:base", area_cm2=10.0, normal_x=0, normal_y=0, normal_z=1,
             face_index=0, component_id=0)
    _mk_component(G, 0, face_count=1)
    _add_has_base(G, "component:0", "face:base")
    G.nodes["face:base"]["is_base_candidate"] = True

    _mk_component(G, 99, face_count=0)
    # no edges at all

    mesh_graph._score_base_faces(G, {})
    mesh_graph._classify_units(G, {})

    assert G.nodes["component:99"]["unit_type"] == "freeform"


def test_classify_fifty_fifty_tie_protrusion():
    """50/50 convex/concave -> tie-break by largest shared_edge_length (convex)."""
    G = nx.Graph()
    _mk_face(G, "face:base", area_cm2=10.0, normal_x=0, normal_y=0, normal_z=1,
             face_index=0, component_id=0)
    _mk_component(G, 0, face_count=1)
    _add_has_base(G, "component:0", "face:base")
    G.nodes["face:base"]["is_base_candidate"] = True

    _mk_face(G, "face:a", area_cm2=4.0, normal_x=1, normal_y=0, normal_z=0,
             face_index=1, component_id=1)
    _mk_face(G, "face:b", area_cm2=4.0, normal_x=-1, normal_y=0, normal_z=0,
             face_index=2, component_id=1)
    _mk_component(G, 1, face_count=2)
    _add_has_base(G, "component:1", "face:a")
    _add_has_base(G, "component:1", "face:b")

    # 1 convex, 1 concave, but convex has larger shared_edge_length
    _add_edge_adjacent(G, "face:base", "face:a", convexity="convex",
                       shared_edge_length=5.0)
    _add_edge_adjacent(G, "face:base", "face:b", convexity="concave",
                       shared_edge_length=2.0)

    mesh_graph._score_base_faces(G, {})
    mesh_graph._classify_units(G, {})

    assert G.nodes["component:1"]["unit_type"] == "protrusion"


def test_classify_fifty_fifty_tie_depression():
    """50/50 convex/concave -> tie-break by largest shared_edge_length (concave)."""
    G = nx.Graph()
    _mk_face(G, "face:base", area_cm2=10.0, normal_x=0, normal_y=0, normal_z=1,
             face_index=0, component_id=0)
    _mk_component(G, 0, face_count=1)
    _add_has_base(G, "component:0", "face:base")
    G.nodes["face:base"]["is_base_candidate"] = True

    _mk_face(G, "face:a", area_cm2=4.0, normal_x=1, normal_y=0, normal_z=0,
             face_index=1, component_id=1)
    _mk_face(G, "face:b", area_cm2=4.0, normal_x=-1, normal_y=0, normal_z=0,
             face_index=2, component_id=1)
    _mk_component(G, 1, face_count=2)
    _add_has_base(G, "component:1", "face:a")
    _add_has_base(G, "component:1", "face:b")

    _add_edge_adjacent(G, "face:base", "face:a", convexity="convex",
                       shared_edge_length=2.0)
    _add_edge_adjacent(G, "face:base", "face:b", convexity="concave",
                       shared_edge_length=5.0)

    mesh_graph._score_base_faces(G, {})
    mesh_graph._classify_units(G, {})

    assert G.nodes["component:1"]["unit_type"] == "depression"


def test_classify_hole_priority_over_fillet():
    """Hole classification takes priority over fillet/chamfer even if both apply."""
    G = nx.Graph()
    _mk_face(G, "face:base", area_cm2=100.0, normal_x=0, normal_y=0, normal_z=1,
             face_index=0, component_id=0)
    _mk_component(G, 0, face_count=1)
    _add_has_base(G, "component:0", "face:base")
    G.nodes["face:base"]["is_base_candidate"] = True

    _mk_component(G, 1, face_count=0)
    # Both cylindrical (hole signal) AND fillet (small area + curved)
    _mk_curved(G, 0, component_id=1, curve_type="cylindrical", area_cm2=0.1)
    _add_component_of(G, "curved:0", "component:1")

    mesh_graph._score_base_faces(G, {})
    mesh_graph._classify_units(G, {})

    # hole wins over fillet per priority order
    assert G.nodes["component:1"]["unit_type"] == "hole"


# =====================================================================
# _build_dependency_order tests
# =====================================================================

def test_dependency_order_box():
    """Box: one component -> order 0, no cycles."""
    G = _graph(*_box())
    rebuild = G.graph["rebuild_order"]
    assert rebuild == ["component:0"]
    assert G.graph["dag_has_cycles"] is False
    assert G.nodes["component:0"]["rebuild_order"] == 0


def test_dependency_order_multi_component():
    """Base + protrusion + depression -> valid topological sort."""
    G = nx.Graph()
    _mk_face(G, "face:base", area_cm2=100.0, normal_x=0, normal_y=0, normal_z=1,
             face_index=0, component_id=0)
    _mk_component(G, 0, face_count=1)
    _add_has_base(G, "component:0", "face:base")
    G.nodes["face:base"]["is_base_candidate"] = True
    G.nodes["component:0"]["unit_type"] = "base"

    _mk_face(G, "face:protr", area_cm2=10.0, normal_x=1, normal_y=0, normal_z=0,
             face_index=1, component_id=1)
    _mk_component(G, 1, face_count=1)
    _add_has_base(G, "component:1", "face:protr")
    _add_edge_adjacent(G, "face:base", "face:protr", convexity="convex")
    G.nodes["component:1"]["unit_type"] = "protrusion"

    _mk_face(G, "face:dep", area_cm2=5.0, normal_x=-1, normal_y=0, normal_z=0,
             face_index=2, component_id=2)
    _mk_component(G, 2, face_count=1)
    _add_has_base(G, "component:2", "face:dep")
    _add_edge_adjacent(G, "face:base", "face:dep", convexity="concave")
    G.nodes["component:2"]["unit_type"] = "depression"

    rebuild, has_cycles = mesh_graph._build_dependency_order(G)

    assert not has_cycles
    # base is first
    assert rebuild[0] == "component:0"
    # base has order 0
    assert G.nodes["component:0"]["rebuild_order"] == 0
    # protrusion and depression come after base
    assert G.nodes["component:1"]["rebuild_order"] > 0
    assert G.nodes["component:2"]["rebuild_order"] > 0
    # all three in rebuild list
    assert set(rebuild) == {"component:0", "component:1", "component:2"}
    assert len(rebuild) == 3


def test_dependency_order_fillets_last():
    """Fillet/chamfer components always come last in rebuild order."""
    G = nx.Graph()
    _mk_face(G, "face:base", area_cm2=100.0, normal_x=0, normal_y=0, normal_z=1,
             face_index=0, component_id=0)
    _mk_component(G, 0, face_count=1)
    _add_has_base(G, "component:0", "face:base")
    G.nodes["face:base"]["is_base_candidate"] = True
    G.nodes["component:0"]["unit_type"] = "base"

    _mk_face(G, "face:protr", area_cm2=10.0, normal_x=1, normal_y=0, normal_z=0,
             face_index=1, component_id=1)
    _mk_component(G, 1, face_count=1)
    _add_has_base(G, "component:1", "face:protr")
    _add_edge_adjacent(G, "face:base", "face:protr", convexity="convex")
    G.nodes["component:1"]["unit_type"] = "protrusion"

    # fillet component
    _mk_curved(G, 0, component_id=2, curve_type="fillet_edge")
    _mk_component(G, 2, face_count=0)
    _add_component_of(G, "curved:0", "component:2")
    G.nodes["component:2"]["unit_type"] = "fillet"

    rebuild, has_cycles = mesh_graph._build_dependency_order(G)

    assert not has_cycles
    assert G.nodes["component:0"]["rebuild_order"] == 0
    # fillet is after protrusion
    assert G.nodes["component:2"]["rebuild_order"] > G.nodes["component:1"]["rebuild_order"]


def test_dependency_order_empty_graph():
    G = mesh_graph.build_structure_graph({})
    rebuild, has_cycles = mesh_graph._build_dependency_order(G)
    assert rebuild == []
    assert not has_cycles
    assert G.graph["rebuild_order"] == []


def test_cycle_detection_in_dependency_order(caplog):
    """Hand-built component DAG with a cycle -> dag_has_cycles=True,
    rebuild_order still valid."""
    G = nx.Graph()
    _mk_face(G, "face:base", area_cm2=100.0, normal_x=0, normal_y=0, normal_z=1,
             face_index=0, component_id=0)
    _mk_component(G, 0, face_count=1)
    _add_has_base(G, "component:0", "face:base")
    G.nodes["face:base"]["is_base_candidate"] = True
    G.nodes["component:0"]["unit_type"] = "base"

    # Two components, both protrusions, both EDGE_ADJACENT to base AND to each other
    # The dependency DAG will have: both depend on base, but also on each other
    for cid in (1, 2):
        _mk_face(G, f"face:{cid}", area_cm2=10.0, normal_x=1 if cid == 1 else -1,
                 normal_y=0, normal_z=0, face_index=cid, component_id=cid)
        _mk_component(G, cid, face_count=1)
        _add_has_base(G, f"component:{cid}", f"face:{cid}")
        _add_edge_adjacent(G, "face:base", f"face:{cid}", convexity="convex",
                           shared_edge_length=2.0)
    # Add cycle: component:1 and component:2 are EDGE_ADJACENT
    _add_edge_adjacent(G, "face:1", "face:2", convexity="convex",
                       shared_edge_length=1.0)
    G.nodes["component:1"]["unit_type"] = "protrusion"
    G.nodes["component:2"]["unit_type"] = "protrusion"

    # The dependency DAG adds dependency edges from each non-base to base.
    # But component:1 and component:2 have no additional dependency on each
    # other (they're not holes or fillets). So the DAG is acyclic!
    # We need a fillet or hole to create a real cycle.

    # Let's create a cycle: component:1 depends on component:2 via hole link,
    # and component:2 depends on component:1 also via hole link. But that's
    # not possible with our graph structure without real HOLE nodes.

    # Alternative: make two fillet components that both depend on each other.
    G2 = nx.Graph()
    _mk_face(G2, "face:base", area_cm2=100.0, normal_x=0, normal_y=0, normal_z=1,
             face_index=0, component_id=0)
    _mk_component(G2, 0, face_count=1)
    _add_has_base(G2, "component:0", "face:base")
    G2.nodes["face:base"]["is_base_candidate"] = True
    G2.nodes["component:0"]["unit_type"] = "base"

    for cid in (1, 2):
        _mk_face(G2, f"face:{cid}", area_cm2=5.0, normal_x=1 if cid == 1 else -1,
                 normal_y=0, normal_z=0, face_index=cid, component_id=cid)
        _mk_component(G2, cid, face_count=1)
        _add_has_base(G2, f"component:{cid}", f"face:{cid}")
        _mk_curved(G2, cid - 1, component_id=cid, curve_type="fillet_edge")
        _add_component_of(G2, f"curved:{cid-1}", f"component:{cid}")
        G2.nodes[f"component:{cid}"]["unit_type"] = "fillet"
    # Both fillets are EDGE_ADJACENT to each other AND to base
    _add_edge_adjacent(G2, "face:base", "face:1", convexity="convex",
                       shared_edge_length=2.0)
    _add_edge_adjacent(G2, "face:base", "face:2", convexity="convex",
                       shared_edge_length=2.0)
    _add_edge_adjacent(G2, "face:1", "face:2", convexity="convex",
                       shared_edge_length=0.5)

    with caplog.at_level("WARNING", logger="mesh_graph"):
        rebuild, has_cycles = mesh_graph._build_dependency_order(G2)

    assert has_cycles
    assert "cycle" in caplog.text.lower()
    # rebuild_order still valid (all components present)
    assert len(rebuild) == 3
    assert set(rebuild) == {"component:0", "component:1", "component:2"}
    for c in rebuild:
        assert isinstance(G2.nodes[c]["rebuild_order"], int)
        assert G2.nodes[c]["rebuild_order"] >= 0
    assert G2.graph["dag_has_cycles"] is True


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

    # unit_type == "base"
    unit = conn.execute(
        "SELECT unit_type FROM nodes WHERE node_id = 'component:0'"
    ).fetchone()[0]
    assert unit == "base"

    # rebuild_order == 0
    order = conn.execute(
        "SELECT rebuild_order FROM nodes WHERE node_id = 'component:0'"
    ).fetchone()[0]
    assert order == 0

    # dag_has_cycles False (queried via graph-level - it's in G.graph, not DuckDB)
    assert G.graph["dag_has_cycles"] is False
    assert G.graph["rebuild_order"] == ["component:0"]


def test_box_pipeline_column_presence():
    """Verify all T9 columns appear in DuckDB for a box."""
    G = _graph(*_box())
    conn = mesh_graph._persist_to_duckdb(G, "0")

    node_cols = {row[1] for row in conn.execute(
        "PRAGMA table_info(nodes)").fetchall()}
    assert "base_score" in node_cols
    assert "is_base_candidate" in node_cols
    assert "unit_type" in node_cols
    assert "rebuild_order" in node_cols


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
