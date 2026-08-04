#!/usr/bin/env python3
"""Headless tests for mcp_server.mesh_analysis (mesh-to-parametric plan, Todo 2).

Pure-Python geometry analysis: topology (watertight/manifold), volume via the
divergence theorem, bounding box, mirror symmetry, primitive hints (plane
regions / cylinder / box), and the recommended reconstruction strategy.  All
math is stdlib-only (no numpy) and fully deterministic (no random module in
the clustering / RANSAC-style fitting), so identical input always yields an
identical report.
"""

import math
import os
import sys

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from mcp_server.mesh_analysis import analyze_mesh_data, scale_report


# ---------------------------------------------------------------------------
# Synthetic fixtures (all in cm)
# ---------------------------------------------------------------------------

def _unit_cube():
    """Outward-wound unit cube [0,1]^3 as flat node/index/normal lists."""
    nodes = [
        0, 0, 0,  1, 0, 0,  1, 1, 0,  0, 1, 0,
        0, 0, 1,  1, 0, 1,  1, 1, 1,  0, 1, 1,
    ]
    indices = [
        # -z
        0, 2, 1,  0, 3, 2,
        # +z
        4, 5, 6,  4, 6, 7,
        # -y
        0, 1, 5,  0, 5, 4,
        # +y
        3, 7, 6,  3, 6, 2,
        # -x
        0, 4, 3,  3, 4, 7,
        # +x
        1, 2, 6,  1, 6, 5,
    ]
    normals = [
        0, 0, -1,  0, 0, -1,
        0, 0, 1,   0, 0, 1,
        0, -1, 0,  0, -1, 0,
        0, 1, 0,   0, 1, 0,
        -1, 0, 0,  -1, 0, 0,
        1, 0, 0,   1, 0, 0,
    ]
    return nodes, indices, normals


def _open_box():
    """Unit cube with the +z face removed (5 faces, open rim)."""
    nodes, indices, normals = _unit_cube()
    n_indices = list(indices)  # flat copy
    # strip the last 6 entries of indices (the +z two triangles: 4,5,6, 4,6,7)
    stripped = n_indices[:-6]
    return nodes, stripped, normals[:-6]


def _tetra():
    """Outward-wound tetrahedron with vertices at the origin + unit axes."""
    nodes = [
        0, 0, 0,  1, 0, 0,  0, 1, 0,  0, 0, 1,
    ]
    indices = [
        # face (0,1,2) on z=0, normal -z: order (1,2,0)?? use explicit outward set:
        0, 2, 1,
        0, 1, 3,
        0, 3, 2,
        1, 2, 3,
    ]
    normals = []  # exercise the geometric-normal fallback
    return nodes, indices, normals


def _cylinder(radius=1.0, height=2.0, segments=16):
    """Outward-wound cylinder, axis along Z from z=0 to z=height."""
    nodes = []
    for i in range(segments):
        th = 2.0 * math.pi * i / segments
        nodes.extend([radius * math.cos(th), radius * math.sin(th), 0.0])
    for i in range(segments):
        th = 2.0 * math.pi * i / segments
        nodes.extend([radius * math.cos(th), radius * math.sin(th), height])
    # bottom center, top center
    nodes.extend([0.0, 0.0, 0.0,  0.0, 0.0, height])
    b0, t0 = 0, segments
    bc, tc = 2 * segments, 2 * segments + 1
    indices = []
    for i in range(segments):
        j = (i + 1) % segments
        # side quad (b_i, b_j, t_j, t_i) -> outward
        indices.extend([b0 + i, b0 + j, t0 + j,  b0 + i, t0 + j, t0 + i])
        # bottom cap (center, b_j, b_i) -> -z
        indices.extend([bc, b0 + j, b0 + i])
        # top cap (center, t_i, t_j) -> +z
        indices.extend([tc, t0 + i, t0 + j])
    return nodes, indices, []


# ---------------------------------------------------------------------------
# Happy path: unit cube
# ---------------------------------------------------------------------------

def test_unit_cube_watertight_volume_bbox():
    nodes, indices, normals = _unit_cube()
    report = analyze_mesh_data(nodes, indices, normals)
    assert report["watertight"] is True
    assert report["manifold"] is True
    assert report["vertex_count"] == 8
    assert report["triangle_count"] == 12
    assert report["volume_cm3"] == pytest.approx(1.0, abs=1e-6)
    assert report["bounding_box_cm"] == [[0.0, 0.0, 0.0], [1.0, 1.0, 1.0]]
    assert report["recommended_strategy"] == "prismatic"


def test_unit_cube_primitive_hints_box():
    nodes, indices, normals = _unit_cube()
    report = analyze_mesh_data(nodes, indices, normals)
    hints = report["primitive_hints"]
    assert hints["box"]["fitted"] is True
    assert hints["box"]["confidence"] == pytest.approx(1.0, abs=1e-4)
    assert hints["box"]["dims_cm"] == [1.0, 1.0, 1.0]
    assert hints["cylinder"]["fitted"] is False
    # 6 plane regions: +-x, +-y, +-z, two faces each
    assert len(hints["plane_regions"]) == 6
    assert all(r["face_count"] == 2 for r in hints["plane_regions"])


def test_unit_cube_symmetry_axes():
    nodes, indices, normals = _unit_cube()
    report = analyze_mesh_data(nodes, indices, normals)
    sym = report["symmetry"]
    assert len(sym["candidates"]) == 3
    assert sym["dominant_axis"] == "X"
    assert all(c["symmetric"] for c in sym["candidates"])


def test_unit_cube_geometric_normals_fallback():
    """No normals provided -> face normals derived from geometry; same report."""
    nodes, indices, _ = _unit_cube()
    a = analyze_mesh_data(nodes, indices, None)
    b = analyze_mesh_data(nodes, indices, [])
    assert a["watertight"] is True
    assert a["volume_cm3"] == pytest.approx(1.0, abs=1e-6)
    assert a["recommended_strategy"] == "prismatic"
    assert a == b


def test_unit_cube_per_corner_normals_averaged():
    """Per-corner normals (3 per triangle) are averaged to per-face normals."""
    nodes, indices, normals = _unit_cube()
    tri_normals = [[normals[i], normals[i + 1], normals[i + 2]]
                   for i in range(0, len(normals), 3)]
    per_corner = [c for t in tri_normals for _ in range(3) for c in t]
    report = analyze_mesh_data(nodes, indices, per_corner)
    assert report["watertight"] is True
    assert report["volume_cm3"] == pytest.approx(1.0, abs=1e-6)
    assert report["recommended_strategy"] == "prismatic"


def test_nested_triple_input_accepted():
    """Nodes/indices/normals may also be given as lists of triples."""
    nodes, indices, normals = _unit_cube()
    node_t = [[nodes[i], nodes[i + 1], nodes[i + 2]]
              for i in range(0, len(nodes), 3)]
    idx_t = [[indices[i], indices[i + 1], indices[i + 2]]
             for i in range(0, len(indices), 3)]
    norm_t = [[normals[i], normals[i + 1], normals[i + 2]]
              for i in range(0, len(normals), 3)]
    report = analyze_mesh_data(node_t, idx_t, norm_t)
    assert report["watertight"] is True
    assert report["volume_cm3"] == pytest.approx(1.0, abs=1e-6)
    assert report["recommended_strategy"] == "prismatic"


# ---------------------------------------------------------------------------
# Open box / tetra / cylinder
# ---------------------------------------------------------------------------

def test_open_box_not_watertight_but_manifold():
    nodes, indices, normals = _open_box()
    report = analyze_mesh_data(nodes, indices, normals)
    assert report["watertight"] is False
    assert report["manifold"] is True
    assert report["triangle_count"] == 10


def test_tetra_watertight_volume_sixth():
    nodes, indices, normals = _tetra()
    report = analyze_mesh_data(nodes, indices, normals)
    assert report["watertight"] is True
    assert report["manifold"] is True
    assert report["triangle_count"] == 4
    assert report["volume_cm3"] == pytest.approx(1.0 / 6.0, abs=1e-9)


def test_cylinder_revolved_strategy():
    nodes, indices, _ = _cylinder(radius=1.0, height=2.0, segments=16)
    report = analyze_mesh_data(nodes, indices, None)
    assert report["watertight"] is True
    hints = report["primitive_hints"]
    assert hints["cylinder"]["fitted"] is True
    assert hints["cylinder"]["radius_cm"] == pytest.approx(1.0, abs=0.05)
    assert hints["cylinder"]["height_cm"] == pytest.approx(2.0, abs=1e-6)
    assert report["recommended_strategy"] == "revolved"


# ---------------------------------------------------------------------------
# Robustness / determinism
# ---------------------------------------------------------------------------

def test_empty_input_organic_fallback():
    report = analyze_mesh_data([], [], [])
    assert report["watertight"] is False
    assert report["manifold"] is False
    assert report["triangle_count"] == 0
    assert report["volume_cm3"] == 0.0
    assert report["recommended_strategy"] == "organic"


def test_deterministic_repeatable():
    nodes, indices, normals = _unit_cube()
    a = analyze_mesh_data(nodes, indices, normals)
    b = analyze_mesh_data(nodes, indices, normals)
    assert a == b


def test_out_of_range_index_raises_value_error():
    nodes, indices, _ = _unit_cube()
    bad = list(indices)
    bad[0] = 999
    with pytest.raises(ValueError):
        analyze_mesh_data(nodes, bad, None)


def test_scale_report_mm():
    """scale_report(factor=10) converts cm -> mm (bbox x10, volume x1000)."""
    nodes, indices, normals = _unit_cube()
    report = analyze_mesh_data(nodes, indices, normals)
    scaled = scale_report(report, 10.0)
    assert scaled["bounding_box_cm"] == [[0.0, 0.0, 0.0], [10.0, 10.0, 10.0]]
    assert scaled["volume_cm3"] == pytest.approx(1000.0, abs=1e-3)
    assert scaled["recommended_strategy"] == "prismatic"
    assert scaled["watertight"] is True
    assert scaled["vertex_count"] == report["vertex_count"]


# ---------------------------------------------------------------------------
# QA-failure proof (plan acceptance "deliberately wrong expectation")
# ---------------------------------------------------------------------------

@pytest.mark.xfail(
    strict=True,
    reason="deliberately wrong expectation proving the test machinery is live")
def test_qa_failure_proof_machinery_live():
    """Proves the test machinery is live -- this assertion is intentionally
    wrong.  A real analysis of the unit cube reports volume 1.0 cm^3, so this
    assertion FAILS; xfail(strict=True) records it as an expected failure
    (XFAIL) so the headless suite stays green while proving assertions really
    execute.  If it ever XPASSES, the machinery is broken."""
    nodes, indices, normals = _unit_cube()
    report = analyze_mesh_data(nodes, indices, normals)
    assert report["volume_cm3"] == 42.0  # deliberately wrong: real volume is 1.0
