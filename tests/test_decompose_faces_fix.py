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

import numpy as np
import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from mcp_server.mesh_analysis import (
    decompose_mesh_faces,
    _as_triples,
    _weld_vertices,
    _boundary_loops,
    _simplify_2d_keep,
    _shoelace_area_2d,
    _project_to_2d,
    _detect_components,
    _detect_quantization_step,
    _max_extent_nodes,
    _group_planar_triangles,
    _snap_group_vertices,
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


# ---------------------------------------------------------------------------
# R-1: quantization-step-derived weld / snap tolerances
# ---------------------------------------------------------------------------

def test_detect_quantization_step_6decimal():
    """Six collinear vertices on a 1e-6 grid (typical 6-decimal STL exporter)
    -> the recurring delta is 1e-6."""
    nodes = [
        (0.0, 0.0, 0.0),
        (0.000001, 0.0, 0.0),
        (0.000002, 0.0, 0.0),
        (0.000003, 0.0, 0.0),
        (0.000004, 0.0, 0.0),
        (0.000005, 0.0, 0.0),
    ]
    assert _detect_quantization_step(nodes) == pytest.approx(1e-6)


def test_detect_quantization_step_4decimal():
    """Five collinear vertices on a 1e-4 grid (4-decimal exporter) -> 1e-4."""
    nodes = [
        (0.0, 0.0, 0.0),
        (0.0001, 0.0, 0.0),
        (0.0002, 0.0, 0.0),
        (0.0003, 0.0, 0.0),
        (0.0004, 0.0, 0.0),
    ]
    assert _detect_quantization_step(nodes) == pytest.approx(1e-4)


def test_detect_quantization_step_uniform():
    """All coordinates identical -> safe default 0.0, and the derived
    tolerances still meet their floors (no empty-histogram crash)."""
    nodes = [(1.5, 2.5, 3.5)] * 6
    quant = _detect_quantization_step(nodes)
    assert quant == 0.0
    extent = _max_extent_nodes(nodes)
    assert extent == 0.0
    eps = max(max(1e-9, 1e-7 * extent), 3 * quant)
    snap_tol = max(1e-5, max(5e-4 * extent, 2 * quant))
    assert eps >= max(1e-9, 1e-7 * extent)
    assert snap_tol >= 1e-5


def test_detect_quantization_step_integer_coords():
    """Integer-coordinate fixture (geometry deltas, not rounding) -> 0.0,
    so weld/snap fall back to their extent-based floors."""
    nodes = [(0, 0, 0), (1, 0, 0), (1, 1, 0), (0, 1, 0)]
    assert _detect_quantization_step(nodes) == 0.0


def test_weld_with_quant_step():
    """Weld eps derived from the detected quantization step (k=3) merges
    displayMesh duplicates AND a hub vertex written at three adjacent
    1e-6-grid points (6-decimal STL seam)."""
    nodes = [
        (0.0, 0.0, 0.0),        # 0
        (1.0, 0.0, 0.0),        # 1
        (1.0, 0.5, 0.0),        # 2
        (0.0, 0.5, 0.0),        # 3
        (0.5, 0.25, 0.0),       # 4  hub (fan center)
        (0.0, 0.0, 0.0),        # 5  dup of 0
        (0.500001, 0.25, 0.0),  # 6  hub at adjacent 1e-6 grid point
        (0.500002, 0.25, 0.0),  # 7  hub at next adjacent grid point
    ]
    tris = [
        (0, 1, 4),
        (1, 2, 6),
        (2, 3, 7),
        (5, 3, 4),
    ]

    quant = _detect_quantization_step(nodes)
    assert quant == pytest.approx(1e-6)

    extent = _max_extent_nodes(nodes)
    assert 3 * quant > max(1e-9, 1e-7 * extent)  # quant term is the binder
    eps = max(max(1e-9, 1e-7 * extent), 3 * quant)
    welded_v, welded_t = _weld_vertices(nodes, tris, eps)

    # 4 corners + 1 merged hub, no leftover duplicates.
    assert len(welded_v) == 5
    assert welded_t == [(0, 1, 2), (1, 3, 2), (3, 4, 2), (0, 4, 2)]

    snap_tol = max(1e-5, max(5e-4 * extent, 2 * quant))
    assert snap_tol >= 1e-5
    assert snap_tol >= 2 * quant


def test_decompose_faces_quantized_quad():
    """End-to-end: a quantized quad fan decomposes to ONE face of the right
    area after quantization-bridged welding."""
    nodes = [
        (0.0, 0.0, 0.0),
        (1.0, 0.0, 0.0),
        (1.0, 0.5, 0.0),
        (0.0, 0.5, 0.0),
        (0.5, 0.25, 0.0),
        (0.0, 0.0, 0.0),
        (0.500001, 0.25, 0.0),
        (0.500002, 0.25, 0.0),
    ]
    tris = [
        (0, 1, 4),
        (1, 2, 6),
        (2, 3, 7),
        (5, 3, 4),
    ]
    result = decompose_mesh_faces(nodes, tris)
    planar = result["planar_faces"]
    assert len(planar) == 1, f"expected 1 face, got {len(planar)}"
    face = planar[0]
    assert face["area"] == pytest.approx(0.5, abs=1e-3)
    assert face["vertex_count"] == 4
    assert face["holes"] == []


# ---------------------------------------------------------------------------
# R-2: connectivity-constrained grouping (primary), global first-match fallback
# ---------------------------------------------------------------------------

def _planar_groups_of(nodes, indices):
    """Grouping-level view of a fixture: weld, vectorise, then call the
    R-2 grouping directly (the public API hides the group structure)."""
    node_list = list(_as_triples(nodes))
    raw_tris = [(int(a), int(b), int(c)) for a, b, c in _as_triples(indices)]
    extent = _max_extent_nodes(node_list)
    quant_step = _detect_quantization_step(node_list)
    eps = max(max(1e-9, 1e-7 * extent), 3 * quant_step)
    welded_verts, welded_tris = _weld_vertices(node_list, raw_tris, eps)
    v_arr = np.array(welded_verts, dtype=np.float64)
    f_arr = np.array([[a, b, c] for a, b, c in welded_tris], dtype=np.int64)
    if f_arr.ndim == 1 and len(f_arr) >= 3:
        f_arr = f_arr.reshape(-1, 3)
    v0v = v_arr[f_arr[:, 0]]
    v1v = v_arr[f_arr[:, 1]]
    v2v = v_arr[f_arr[:, 2]]
    crosses = np.cross(v1v - v0v, v2v - v0v)
    cross_norms = np.linalg.norm(crosses, axis=1)
    safe_norms = np.where(cross_norms > 1e-15, cross_norms, 1.0)
    tri_normals = crosses / safe_norms[:, None]
    return _group_planar_triangles(v_arr, f_arr, tri_normals, 0.5, extent)


def test_connectivity_constrained_touching_coplanar():
    """Two coplanar face patches sharing an actual edge -> merged into
    ONE group (the connectivity pass must not split touching patches)."""
    nodes = [0, 0, 0,  2, 0, 0,  1, 1, 0,  3, 0, 0]
    indices = [0, 1, 2,  1, 3, 2]
    groups = _planar_groups_of(nodes, indices)
    memberships = sorted(sorted(g["tri_indices"]) for g in groups)
    assert memberships == [[0, 1]], (
        f"expected one merged group, got {memberships}")


def test_connectivity_constrained_separated_coplanar():
    """Two coplanar patches on the same plane but disconnected (no shared
    edge) -> TWO separate groups: the connectivity constraint prevents the
    global-path over-merge of separated patches."""
    nodes = [0, 0, 0,  1, 0, 0,  0.5, 1, 0,
             3, 0, 0,  4, 0, 0,  3.5, 1, 0]
    indices = [0, 1, 2,  3, 4, 5]
    groups = _planar_groups_of(nodes, indices)
    memberships = sorted(sorted(g["tri_indices"]) for g in groups)
    assert memberships == [[0], [1]], (
        f"expected two separate groups, got {memberships}")


def test_connectivity_constrained_non_coplanar_adjacent():
    """Edge-adjacent triangles with different normals -> NOT grouped
    (region growing stops at the coplanarity predicate)."""
    nodes = [0, 0, 0,  1, 0, 0,  0, 1, 0,  0, 0, 1]
    indices = [0, 1, 2,  1, 3, 0]
    groups = _planar_groups_of(nodes, indices)
    memberships = sorted(sorted(g["tri_indices"]) for g in groups)
    assert memberships == [[0], [1]], (
        f"expected two separate groups, got {memberships}")


# ---------------------------------------------------------------------------
# R-3: grid-bucketed spatial-hash snap (functionally equivalent to O(n^2))
# ---------------------------------------------------------------------------

def _snap_group_vertices_quadratic_ref(welded_verts, tri_indices, welded_tris,
                                       snap_tol):
    """Reference copy of the pre-R-3 O(n^2) all-pairs snapping, kept here
    test-only so the spatial-hash version can be proven functionally
    equivalent (same cluster membership)."""
    if snap_tol <= 0:
        return {}
    vi_set = set()
    for ti in tri_indices:
        a, b, c = welded_tris[ti]
        vi_set.update((a, b, c))
    vi_list = sorted(vi_set)
    n = len(vi_list)
    if n < 2:
        return {}
    coords = np.array([welded_verts[v] for v in vi_list], dtype=np.float64)
    parent = list(range(n))

    def _find(x):
        root = x
        while parent[root] != root:
            root = parent[root]
        while parent[x] != root:
            parent[x], x = root, parent[x]
        return root

    snap_sq = snap_tol * snap_tol
    for i in range(n):
        pi = coords[i]
        for j in range(i + 1, n):
            diff = coords[j] - pi
            if float(diff @ diff) < snap_sq:
                ri, rj = _find(i), _find(j)
                if ri != rj:
                    if ri < rj:
                        parent[rj] = ri
                    else:
                        parent[ri] = rj

    remap = {}
    for i, v in enumerate(vi_list):
        root = vi_list[_find(i)]
        if root != v:
            remap[v] = root
    return remap


def _snap_partition(remap, vi_list):
    """Canonical cluster partition (groups + sizes) from a snap remap.

    Returns a sorted tuple of sorted tuples of GLOBAL vertex indices, one
    per cluster.  Comparing partitions (not raw remap dicts) is the
    functional-equivalence contract: cluster membership must match while the
    representative per cluster may differ.
    """
    roots = {}
    for v in vi_list:
        roots.setdefault(remap.get(v, v), []).append(v)
    return tuple(sorted(tuple(sorted(m)) for m in roots.values()))


def _random_snap_group(n_verts, rng, tol, cluster_size=5):
    """Build (welded_verts, tri_indices, welded_tris) for a planar group of
    *n_verts* random vertices: points dropped in clusters within tol/3 of a
    random centre, so intra-cluster merging is guaranteed while clusters sit
    on random cell boundaries (floor-key grid gets exercised).  Two anchor
    vertices sit far outside the cluster field."""
    anchors = [(-1.0, -1.0, 0.0), (2.0, 2.0, 0.0)]
    pts = list(anchors)
    n_clusters = max(1, n_verts // cluster_size)
    for _ in range(n_clusters):
        cx = float(rng.uniform(0.0, 1.0))
        cy = float(rng.uniform(0.0, 1.0))
        for _ in range(cluster_size):
            pts.append((cx + float(rng.uniform(-1.0, 1.0)) * tol / 3.0,
                        cy + float(rng.uniform(-1.0, 1.0)) * tol / 3.0,
                        float(rng.uniform(-1.0, 1.0)) * tol / 3.0))
    pts = pts[:2 + n_verts]
    tris = [(0, 2 + i, 3 + i) for i in range(len(pts) - 3)]
    tris.append((0, len(pts) - 1, 1))
    return pts, list(range(len(tris))), tris


def test_snap_spatial_hash_functionally_equivalent():
    """The R-3 spatial-hash snap must produce the SAME cluster partition as
    the O(n^2) all-pairs snap for random inputs of increasing size, and the
    full decompose pipeline must give the same face count through both
    paths."""
    import mcp_server.mesh_analysis as ma

    tol = 0.05
    for n_verts in (10, 100, 500, 2000):
        rng = np.random.default_rng(seed=1000 + n_verts)
        pts, tri_indices, tris = _random_snap_group(n_verts, rng, tol)
        vi_list = sorted({v for ti in tri_indices for v in tris[ti]})
        new = ma._snap_group_vertices(pts, tri_indices, tris, tol)
        ref = _snap_group_vertices_quadratic_ref(pts, tri_indices, tris, tol)
        assert _snap_partition(new, vi_list) == _snap_partition(ref, vi_list), (
            f"n_verts={n_verts}: cluster partitions differ "
            f"new={_snap_partition(new, vi_list)!r} "
            f"ref={_snap_partition(ref, vi_list)!r}")

    # Full pipeline through both snap paths -> same face count and area.
    d = 0.00015
    nodes = [
        0, 0, 0,  2, 0, 0,  2, 1, 0,  0, 1, 0,
        2 + d, 0, 0,  4, 0, 0,  4, 1, 0,
        2 + d, 1, 0,  2, 1, 0,
    ]
    indices = [0, 1, 2,  0, 2, 3,  4, 5, 6,  4, 6, 7]
    orig = ma._snap_group_vertices
    try:
        ma._snap_group_vertices = _snap_group_vertices_quadratic_ref
        ref_result = decompose_mesh_faces(nodes, indices)
    finally:
        ma._snap_group_vertices = orig
    new_result = decompose_mesh_faces(nodes, indices)
    ref_faces = ref_result["planar_faces"]
    new_faces = new_result["planar_faces"]
    assert len(new_faces) == len(ref_faces) == 1, (
        f"face counts differ: new={len(new_faces)} ref={len(ref_faces)}")
    assert new_faces[0]["area"] == pytest.approx(ref_faces[0]["area"],
                                                 abs=1e-3)


def test_snap_gap_barely_exceeding_tol_not_merged():
    """A gap just ABOVE snap_tol must NOT merge; a gap just BELOW it must.
    The exact-tolerance boundary (vertex on the cell boundary) must also not
    merge — the strict ``< snap_sq`` predicate is preserved."""
    tol = 0.01
    just_over = (tol + 1e-9, 0.0, 0.0)
    exact = (tol, 0.0, 0.0)
    just_under = (tol - 1e-9, 0.0, 0.0)

    # gap > snap_tol -> NOT merged (spatial hash must agree with ref).
    pts = [(0.0, 0.0, 0.0), just_over]
    tris = [(0, 1, 0)]
    assert _snap_group_vertices_quadratic_ref(pts, [0], tris, tol) == {}
    assert _snap_group_vertices(pts, [0], tris, tol) == {}, (
        "gap barely exceeding snap_tol must not merge")

    # gap == snap_tol (cell boundary straddle) -> NOT merged.
    pts_exact = [(0.0, 0.0, 0.0), exact]
    assert _snap_group_vertices(pts_exact, [0], [(0, 1, 0)], tol) == {}, (
        "gap exactly snap_tol must not merge (strict predicate)")

    # gap < snap_tol -> merged, smaller index (0) absorbs larger (1).
    pts_under = [(0.0, 0.0, 0.0), just_under]
    merged = _snap_group_vertices(pts_under, [0], [(0, 1, 0)], tol)
    assert merged == {1: 0}, f"gap below tol should merge, got {merged!r}"


def test_snap_spatial_hash_5000_vertices_fast():
    """A 5000-vertex group snaps in well under the 5 s CI budget (the
    O(n^2) all-pairs scan would take ~10x longer at this size)."""
    import time

    rng = np.random.default_rng(seed=20260805)
    tol = 0.01
    pts, tri_indices, tris = _random_snap_group(5000, rng, tol)
    t0 = time.perf_counter()
    remap = _snap_group_vertices(pts, tri_indices, tris, tol)
    elapsed = time.perf_counter() - t0
    assert isinstance(remap, dict)
    assert elapsed < 5.0, f"spatial-hash snap took {elapsed:.3f}s (> 5 s)"
    # Sanity: the clustered input must actually have merged something.
    assert len(remap) > 0, "expected merges from clustered input"


# ---------------------------------------------------------------------------
# R-4: residual non-manifold edge warnings (unpaired_boundary_edges)
# ---------------------------------------------------------------------------

def test_clean_mesh_no_warnings():
    """A perfectly seam-matching mesh (shared edges cancel exactly) must
    report no residual non-manifold edges: has_warnings False and every face
    carries an empty warnings list."""
    nodes, indices = _two_touching_coplanar_quads()
    result = decompose_mesh_faces(nodes, indices)
    assert result["has_warnings"] is False
    assert result["planar_faces"], "expected at least one face"
    for face in result["planar_faces"]:
        assert face["warnings"] == [], (
            f"clean face {face['face_index']} has warnings: {face['warnings']}")


def test_offset_seam_emits_unpaired_warning():
    """A seam whose duplicated fragment is offset by 0.1 cm — far above
    snap_tol, so the seam does NOT snap closed — leaves unbalanced boundary
    edges (the far edge and the T-junction-split seam sub-edges appear twice
    with the same winding and never cancel).  decompose_mesh_faces must
    report has_warnings True with at least one face carrying an
    unpaired_boundary_edges warning."""
    offset = 0.1
    nodes = [
        0, 0, 0,  4, 0, 0,  4, 4, 0,  0, 4, 0,
        4, 0, 0,  6, 0, 0,  6, 4, 0,  4, 4, 0,
        4 + offset, 0, 0,  6, 0, 0,  6, 4, 0,  4 + offset, 4, 0,
    ]
    indices = [
        0, 1, 2,  0, 2, 3,
        4, 5, 6,  4, 6, 7,
        8, 9, 10,  8, 10, 11,
    ]
    result = decompose_mesh_faces(nodes, indices)
    assert result["has_warnings"] is True
    warned = [f for f in result["planar_faces"] if f["warnings"]]
    assert warned, "expected at least one face with warnings"
    for face in warned:
        assert {"type": "unpaired_boundary_edges",
                "count": face["warnings"][0]["count"]} in face["warnings"]
        assert face["warnings"][0]["count"] > 0


def test_warnings_additive_contract():
    """R-4 adds per-face ``warnings`` and top-level ``has_warnings`` WITHOUT
    removing or renaming any existing key — the additive-contract rule."""
    nodes, indices = _two_touching_coplanar_quads()
    result = decompose_mesh_faces(nodes, indices)
    for face in result["planar_faces"]:
        assert _REQUIRED_FACE_KEYS.issubset(face.keys()), (
            f"missing keys: {_REQUIRED_FACE_KEYS - face.keys()}")
        assert "warnings" in face, "face missing new 'warnings' key"
        assert isinstance(face["warnings"], list)
    assert "has_warnings" in result
    assert isinstance(result["has_warnings"], bool)
    assert "components_detected" in result
    assert "planar_faces" in result
    assert "curved_patches" in result

