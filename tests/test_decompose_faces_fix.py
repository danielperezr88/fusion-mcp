#!/usr/bin/env python3
"""Headless tests for the decompose_mesh_faces Bug A-E fixes (2026-08-04).

Each test targets one diagnosed bug:

  (a) Bug A — weld of duplicated-corner vertices (displayMesh pattern).
  (b) Bug B — coplanar-merge: two touching coplanar quads -> ONE face.
  (b')       two separated coplanar quads -> TWO faces.
  (c) Bug C — degenerate zero-area / zero-normal faces filtered.
  (d) Bug E — holes emitted (frame / square-with-hole -> 1 outer + 1 hole).
  (e) Bug D — 2D simplification collapses collinear points.
  (f) Additive contract — existing keys present + new ``holes`` key;
      fallback keeps ``"error"``.
  (g) Simulated 1842-node pattern — multi-component, duplicated corners,
      non-empty result.
"""

import math
import os
import sys

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from mcp_server.mesh_analysis import (
    decompose_mesh_faces,
    _weld_vertices,
    _boundary_loops,
    _simplify_2d_keep,
    _shoelace_area_2d,
    _project_to_2d,
    _detect_components,
)


# ---------------------------------------------------------------------------
# (a) Bug A — weld dedup
# ---------------------------------------------------------------------------

def test_weld_dedup_duplicated_corners():
    nodes = [
        (0, 0, 0), (1, 0, 0), (1, 1, 0),
        (0, 0, 0), (1, 1, 0), (0, 1, 0),
    ]
    tris = [(0, 1, 2), (3, 4, 5)]
    welded_v, welded_t = _weld_vertices(nodes, tris, 1e-6)
    assert len(welded_v) == 4
    assert welded_t == [(0, 1, 2), (0, 2, 3)]


def test_weld_extent_based_eps():
    extent = 50.0
    eps = max(1e-9, 1e-7 * extent)
    assert eps == pytest.approx(5e-6, rel=1e-3)


# ---------------------------------------------------------------------------
# (b) Bug B — coplanar merge: touching quads -> ONE face
# ---------------------------------------------------------------------------

def _two_touching_coplanar_quads():
    nodes = [
        0, 0, 0,  2, 0, 0,  2, 1, 0,  0, 1, 0,
        4, 0, 0,  4, 1, 0,
    ]
    indices = [
        0, 1, 2,   0, 2, 3,
        1, 4, 5,   1, 5, 2,
    ]
    return nodes, indices


def test_coplanar_merge_touching_one_face():
    nodes, indices = _two_touching_coplanar_quads()
    result = decompose_mesh_faces(nodes, indices)
    assert result["components_detected"] >= 1
    planar = result["planar_faces"]
    assert len(planar) == 1, f"expected 1 merged face, got {len(planar)}"
    face = planar[0]
    assert face["area"] == pytest.approx(4.0, abs=1e-4)
    assert face["vertex_count"] == 4
    assert len(face["vertices"]) == 4


def test_coplanar_merge_touching_no_holes():
    nodes, indices = _two_touching_coplanar_quads()
    result = decompose_mesh_faces(nodes, indices)
    face = result["planar_faces"][0]
    assert face["holes"] == []


# ---------------------------------------------------------------------------
# (b') Bug B — coplanar non-merge: separated quads -> TWO faces
# ---------------------------------------------------------------------------

def _two_separated_coplanar_quads():
    nodes = [
        0, 0, 0,  1, 0, 0,  1, 1, 0,  0, 1, 0,
        3, 0, 0,  4, 0, 0,  4, 1, 0,  3, 1, 0,
    ]
    indices = [
        0, 1, 2,   0, 2, 3,
        4, 5, 6,   4, 6, 7,
    ]
    return nodes, indices


def test_coplanar_separated_two_faces():
    nodes, indices = _two_separated_coplanar_quads()
    result = decompose_mesh_faces(nodes, indices)
    planar = result["planar_faces"]
    assert len(planar) == 2, f"expected 2 faces, got {len(planar)}"
    for f in planar:
        assert f["area"] == pytest.approx(1.0, abs=1e-4)
        assert f["vertex_count"] == 4
        assert f["holes"] == []


# ---------------------------------------------------------------------------
# (c) Bug C — degenerate face filtered
# ---------------------------------------------------------------------------

def test_degenerate_zero_area_filtered():
    nodes = [
        0, 0, 0,  2, 0, 0,  2, 1, 0,  0, 1, 0,
        5, 5, 0,  6, 5, 0,  7, 5, 0,
    ]
    indices = [
        0, 1, 2,   0, 2, 3,
        4, 5, 6,
    ]
    result = decompose_mesh_faces(nodes, indices)
    planar = result["planar_faces"]
    assert len(planar) == 1
    assert planar[0]["area"] == pytest.approx(2.0, abs=1e-4)


def test_degenerate_all_collinear_filtered():
    nodes = [0, 0, 0,  1, 0, 0,  2, 0, 0]
    indices = [0, 1, 2]
    result = decompose_mesh_faces(nodes, indices)
    assert result["planar_faces"] == []


# ---------------------------------------------------------------------------
# (d) Bug E — holes emitted (frame / square-with-hole)
# ---------------------------------------------------------------------------

def _frame_mesh():
    nodes = [
        0, 0, 0,   # 0
        4, 0, 0,   # 1
        4, 4, 0,   # 2
        0, 4, 0,   # 3
        1, 1, 0,   # 4
        3, 1, 0,   # 5
        3, 3, 0,   # 6
        1, 3, 0,   # 7
    ]
    indices = [
        0, 1, 5,   0, 5, 4,
        1, 2, 6,   1, 6, 5,
        2, 3, 7,   2, 7, 6,
        3, 0, 4,   3, 4, 7,
    ]
    return nodes, indices


def test_frame_has_one_face_with_hole():
    nodes, indices = _frame_mesh()
    result = decompose_mesh_faces(nodes, indices)
    planar = result["planar_faces"]
    assert len(planar) == 1
    face = planar[0]
    assert face["area"] == pytest.approx(12.0, abs=1e-3)
    assert len(face["holes"]) == 1
    assert len(face["holes"][0]) == 4


# ---------------------------------------------------------------------------
# (e) Bug D — 2D simplification collapses collinear points
# ---------------------------------------------------------------------------

def _quad_with_collinear_midpoint():
    nodes = [
        0, 0, 0,   # 0
        1, 0, 0,   # 1  (collinear midpoint on bottom edge)
        2, 0, 0,   # 2
        2, 1, 0,   # 3
        0, 1, 0,   # 4
    ]
    indices = [
        0, 1, 4,
        1, 3, 4,
        1, 2, 3,
    ]
    return nodes, indices


def test_2d_simplify_collapses_collinear():
    nodes, indices = _quad_with_collinear_midpoint()
    result = decompose_mesh_faces(nodes, indices)
    planar = result["planar_faces"]
    assert len(planar) == 1
    face = planar[0]
    assert face["vertex_count"] == 4, (
        f"expected 4 vertices after simplification, got {face['vertex_count']}")
    assert face["area"] == pytest.approx(2.0, abs=1e-4)


def test_simplify_2d_keep_function():
    pts = [(0, 0), (1, 0), (2, 0), (2, 1), (0, 1)]
    keep = _simplify_2d_keep(pts, 1e-4)
    assert keep == [0, 2, 3, 4]
    assert len(keep) == 4


def test_simplify_2d_keep_triangle_unchanged():
    pts = [(0, 0), (1, 0), (0, 1)]
    keep = _simplify_2d_keep(pts, 1e-4)
    assert keep == [0, 1, 2]


# ---------------------------------------------------------------------------
# (f) Additive contract
# ---------------------------------------------------------------------------

_REQUIRED_FACE_KEYS = {
    "component", "face_index", "triangle_count", "vertex_count",
    "vertices", "normal", "angles_deg", "area", "holes",
}


def test_additive_contract_face_keys():
    nodes, indices = _two_touching_coplanar_quads()
    result = decompose_mesh_faces(nodes, indices)
    for face in result["planar_faces"]:
        assert _REQUIRED_FACE_KEYS.issubset(face.keys()), (
            f"missing keys: {_REQUIRED_FACE_KEYS - face.keys()}")


def test_additive_contract_top_level_keys():
    nodes, indices = _two_touching_coplanar_quads()
    result = decompose_mesh_faces(nodes, indices)
    assert "components_detected" in result
    assert "planar_faces" in result
    assert "curved_patches" in result


def test_fallback_has_error_key():
    result = decompose_mesh_faces([0, 0, 0, 1, 0, 0, 0, 1, 0], [0, 1, 999])
    assert "error" in result
    assert result["planar_faces"] == []


# ---------------------------------------------------------------------------
# (g) Simulated 1842-node pattern (multi-component, duplicated corners)
# ---------------------------------------------------------------------------

def test_multi_component_duplicated_corners_nonempty():
    nodes = []
    indices = []

    def add_quad(p0, p1, p2, p3):
        n0 = len(nodes) // 3
        nodes.extend(list(p0) + list(p1) + list(p2) + list(p3))
        indices.extend([n0, n0 + 1, n0 + 2, n0, n0 + 2, n0 + 3])

    add_quad((0, 0, 0), (5, 0, 0), (5, 3, 0), (0, 3, 0))
    add_quad((0, 0, 1), (5, 0, 1), (5, 0, 0), (0, 0, 0))
    add_quad((0, 3, 1), (5, 3, 1), (5, 3, 0), (0, 3, 0))
    add_quad((0, 0, 1), (0, 3, 1), (0, 3, 0), (0, 0, 0))
    add_quad((5, 0, 1), (5, 3, 1), (5, 3, 0), (5, 0, 0))
    add_quad((0, 0, 1), (5, 0, 1), (5, 3, 1), (0, 3, 1))

    raw_node_count = len(nodes) // 3
    assert raw_node_count > 20  # lots of duplicated corners

    result = decompose_mesh_faces(nodes, indices)
    assert result["components_detected"] >= 1
    assert len(result["planar_faces"]) >= 3
    for face in result["planar_faces"]:
        assert face["area"] > 0
        assert len(face["vertices"]) >= 3


def test_unit_cube_face_decomposition():
    nodes = [
        0, 0, 0,  1, 0, 0,  1, 1, 0,  0, 1, 0,
        0, 0, 1,  1, 0, 1,  1, 1, 1,  0, 1, 1,
    ]
    indices = [
        0, 2, 1,  0, 3, 2,
        4, 5, 6,  4, 6, 7,
        0, 1, 5,  0, 5, 4,
        3, 7, 6,  3, 6, 2,
        0, 4, 3,  3, 4, 7,
        1, 2, 6,  1, 6, 5,
    ]
    result = decompose_mesh_faces(nodes, indices)
    assert result["components_detected"] == 1
    planar = result["planar_faces"]
    assert len(planar) == 6
    total_area = sum(f["area"] for f in planar)
    assert total_area == pytest.approx(6.0, abs=1e-3)
    for f in planar:
        assert f["vertex_count"] == 4
        assert len(f["vertices"]) == 4
        assert f["holes"] == []


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------

def test_decompose_deterministic():
    nodes, indices = _two_touching_coplanar_quads()
    a = decompose_mesh_faces(nodes, indices)
    b = decompose_mesh_faces(nodes, indices)
    assert a == b


# ---------------------------------------------------------------------------
# Helpers — boundary loops + projection
# ---------------------------------------------------------------------------

def test_boundary_loops_single_quad():
    welded_tris = [(0, 1, 2), (0, 2, 3)]
    loops = _boundary_loops([0, 1], welded_tris)
    assert len(loops) == 1
    assert len(loops[0]) == 4


def test_boundary_loops_frame_has_two_loops():
    welded_tris = [
        (0, 1, 5), (0, 5, 4),
        (1, 2, 6), (1, 6, 5),
        (2, 3, 7), (2, 7, 6),
        (3, 0, 4), (3, 4, 7),
    ]
    loops = _boundary_loops(list(range(8)), welded_tris)
    assert len(loops) == 2


def test_shoelace_area_ccw_positive():
    assert _shoelace_area_2d([(0, 0), (2, 0), (2, 1), (0, 1)]) == pytest.approx(2.0)


def test_shoelace_area_cw_negative():
    assert _shoelace_area_2d([(0, 0), (0, 1), (2, 1), (2, 0)]) == pytest.approx(-2.0)


def test_project_to_2d_preserves_shape():
    pts_3d = [(0, 0, 5), (2, 0, 5), (2, 1, 5), (0, 1, 5)]
    pts_2d = _project_to_2d(pts_3d, (0, 0, 1))
    area = abs(_shoelace_area_2d(pts_2d))
    assert area == pytest.approx(2.0, abs=1e-10)


def test_detect_components_disconnected():
    welded_tris = [
        (0, 1, 2), (0, 2, 3),
        (4, 5, 6), (4, 6, 7),
    ]
    comp_ids = _detect_components(welded_tris)
    assert max(comp_ids) + 1 == 2
    assert comp_ids[0] == comp_ids[1]
    assert comp_ids[2] == comp_ids[3]
    assert comp_ids[0] != comp_ids[2]


def test_detect_components_connected():
    welded_tris = [
        (0, 1, 2), (0, 2, 3),
        (1, 4, 5), (1, 5, 2),
    ]
    comp_ids = _detect_components(welded_tris)
    assert max(comp_ids) + 1 == 1


# ---------------------------------------------------------------------------
# Seam-duplicate regression tests (T-FIX-decompose-seam)
# ---------------------------------------------------------------------------

def test_seam_duplicated_touching_quads_merge():
    """Two touching coplanar quads whose shared-edge vertices are duplicated
    at ~1e-4 cm offset (simulating the real displayMesh seam) -> ONE face."""
    offset = 0.00015
    nodes = [
        0, 0, 0,  2, 0, 0,  2, 1, 0,  0, 1, 0,
        2 + offset, 0, 0,  4, 0, 0,  4, 1, 0,
        2 + offset, 1, 0,  2, 1, 0,
    ]
    indices = [
        0, 1, 2,  0, 2, 3,
        4, 5, 6,  4, 6, 7,
    ]
    result = decompose_mesh_faces(nodes, indices)
    planar = result["planar_faces"]
    assert len(planar) == 1, f"expected 1 merged face, got {len(planar)}"
    assert planar[0]["area"] == pytest.approx(4.0, abs=1e-3)
    assert planar[0]["vertex_count"] == 4


def test_seam_duplicated_stacked_strips_merge():
    """Three stacked coplanar strips with duplicated seam vertices -> ONE face."""
    d = 0.0002
    nodes = [
        0, 0, 0,  1, 0, 0,  1, 1, 0,  0, 1, 0,
        1 + d, 0, 0,  2 + d, 0, 0,  2 + d, 1, 0,  1 + d, 1, 0,
        2 + 2 * d, 0, 0,  3 + 2 * d, 0, 0,
        3 + 2 * d, 1, 0,  2 + 2 * d, 1, 0,
    ]
    indices = [
        0, 1, 2,  0, 2, 3,
        4, 5, 6,  4, 6, 7,
        8, 9, 10,  8, 10, 11,
    ]
    result = decompose_mesh_faces(nodes, indices)
    planar = result["planar_faces"]
    assert len(planar) == 1, f"expected 1 merged face, got {len(planar)}"
    assert planar[0]["area"] == pytest.approx(3.0, abs=1e-3)
    assert planar[0]["vertex_count"] == 4


def test_vertex_touching_quads_two_faces():
    """Vertex-touching coplanar quads (share only a corner, no shared edge)
    -> TWO separate faces, NO hole."""
    nodes = [
        0, 0, 0,  1, 0, 0,  1, 1, 0,  0, 1, 0,
        1, 1, 0,  2, 1, 0,  2, 2, 0,  1, 2, 0,
    ]
    indices = [
        0, 1, 2,  0, 2, 3,
        4, 5, 6,  4, 6, 7,
    ]
    result = decompose_mesh_faces(nodes, indices)
    planar = result["planar_faces"]
    assert len(planar) == 2, f"expected 2 faces, got {len(planar)}"
    for f in planar:
        assert f["area"] == pytest.approx(1.0, abs=1e-4)
        assert f["holes"] == []


def test_edge_connected_strips_one_face():
    """Two edge-connected coplanar strips sharing an actual edge -> ONE face."""
    nodes = [
        0, 0, 0,  1, 0, 0,  1, 1, 0,  0, 1, 0,
        2, 0, 0,  2, 1, 0,
    ]
    indices = [
        0, 1, 2,  0, 2, 3,
        1, 4, 5,  1, 5, 2,
    ]
    result = decompose_mesh_faces(nodes, indices)
    planar = result["planar_faces"]
    assert len(planar) == 1, f"expected 1 merged face, got {len(planar)}"
    face = planar[0]
    assert face["area"] == pytest.approx(2.0, abs=1e-3)
    assert face["holes"] == []
    assert face["vertex_count"] == 4


# ---------------------------------------------------------------------------
# Round-3: spurious hole suppression at non-manifold seams
# ---------------------------------------------------------------------------

def test_seam_snap_no_spurious_hole():
    """Two coplanar quads sharing a seam whose vertices are duplicated
    near-coincident (within snap_tol) — after snapping, the seam edges
    produce a 3:1 imbalance that would create a spurious inner loop.
    The filled-hole detection must suppress it: ONE face, holes=0."""
    d = 0.00015
    nodes = [
        0, 0, 0,  4, 0, 0,  4, 4, 0,  0, 4, 0,
        4, 0, 0,  6, 0, 0,  6, 4, 0,  4, 4, 0,
        4 + d, 0, 0,  6, 0, 0,  6, 4, 0,  4 + d, 4, 0,
    ]
    indices = [
        0, 1, 2,  0, 2, 3,
        4, 5, 6,  4, 6, 7,
        8, 9, 10,  8, 10, 11,
    ]
    result = decompose_mesh_faces(nodes, indices)
    planar = result["planar_faces"]
    assert len(planar) == 1, f"expected 1 face, got {len(planar)}"
    face = planar[0]
    assert face["holes"] == [], f"expected no holes, got {len(face['holes'])}"
    assert face["area"] == pytest.approx(24.0, abs=0.5)


def test_genuinely_separated_quads_no_hole_no_merge():
    """Two genuinely separated coplanar quads (gap, not seam) must NOT
    produce a hole or merge — proves the filled-hole detection isn't
    over-merging."""
    nodes = [
        0, 0, 0,  1, 0, 0,  1, 1, 0,  0, 1, 0,
        3, 0, 0,  4, 0, 0,  4, 1, 0,  3, 1, 0,
    ]
    indices = [
        0, 1, 2,  0, 2, 3,
        4, 5, 6,  4, 6, 7,
    ]
    result = decompose_mesh_faces(nodes, indices)
    planar = result["planar_faces"]
    assert len(planar) == 2
    for f in planar:
        assert f["holes"] == []
        assert f["area"] == pytest.approx(1.0, abs=1e-4)
