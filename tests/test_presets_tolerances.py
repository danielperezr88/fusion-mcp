#!/usr/bin/env python3
"""R-9 tests: tolerance presets + individual overrides on decompose_mesh_faces.

Task 15 of the mesh-graph-pipeline plan.
"""

import json
import math
import os
import sys

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from mcp_server.mesh_analysis import (
    decompose_mesh_faces,
    analyze_mesh_data,
    _ToleranceConfig,
)


# ==========================================================================
# helpers
# ==========================================================================

def _make_bent_fixture(h_val):
    """Two triangles sharing edge v0-v1, tri2 tilted by *h_val* (cm).

    v0=(0,0,0), v1=(100,0,0), v2=(50,10,0), v3=(50,10, h_val)
    The small perpendicular distance (10 cm from the shared edge) keeps
    the centroid offset small enough to pass the running plane-fit
    check at balanced / coarse tolerance levels.
    """
    nodes = [
        0, 0, 0,
        100, 0, 0,
        50, 10, 0,
        50, 10, h_val,
    ]
    indices = [
        0, 1, 2,   # tri1 (flat, z=0)
        0, 1, 3,   # tri2 (tilted)
    ]
    return nodes, indices


def _count_planar_faces(result):
    """Number of planar faces in a decompose result dict."""
    assert "error" not in result, f"unexpected error: {result.get('error')}"
    return len(result["planar_faces"])


# ==========================================================================
# test 1: preset "accurate" -> more planar faces than balanced
# ==========================================================================

def test_preset_accurate_more_faces():
    """A bent mesh at ~0.17 deg dihedral: balanced merges (angle < 0.5 deg,
    centroid offset < 1e-4*extent), accurate splits (offset > 1e-6*extent
    AND angle > 0.1 deg).

    h=0.0299 -> centroid dist h/3=0.009967 < 0.01 (balanced) but > 0.0001
    (accurate).  Dihedral ~0.17 deg (between 0.1 and 0.5 thresholds).
    """
    nodes, indices = _make_bent_fixture(0.0299)

    balanced = decompose_mesh_faces(nodes, indices, preset="balanced")
    accurate = decompose_mesh_faces(nodes, indices, preset="accurate")

    n_b = _count_planar_faces(balanced)
    n_a = _count_planar_faces(accurate)

    assert n_b == 1, f"balanced expected 1 face, got {n_b}"
    assert n_a == 2, f"accurate expected 2 faces, got {n_a}"
    assert n_a > n_b


# ==========================================================================
# test 2: preset "coarse" -> fewer planar faces than balanced
# ==========================================================================

def test_preset_coarse_fewer_faces():
    """A mesh at ~0.69 deg dihedral: balanced splits (centroid offset
    0.0204 > 0.01 balanced offset_tol), coarse merges (offset < 0.1
    coarse offset_tol AND angle < 1.0 deg).

    h=0.122 -> centroid dist = h/3 ~ 0.0407.  Angle ~0.69 deg.
    """
    nodes, indices = _make_bent_fixture(0.122)

    balanced = decompose_mesh_faces(nodes, indices, preset="balanced")
    coarse = decompose_mesh_faces(nodes, indices, preset="coarse")

    n_b = _count_planar_faces(balanced)
    n_c = _count_planar_faces(coarse)

    assert n_b == 2, f"balanced expected 2 faces, got {n_b}"
    assert n_c == 1, f"coarse expected 1 face, got {n_c}"
    assert n_c < n_b


# ==========================================================================
# test 3: individual override (snap_tol) changes per-face vertex count
# ==========================================================================

def test_individual_override():
    """Inside a single coplanar face, a near-duplicate vertex (gap=0.02 cm)
    is NOT merged by default snap_tol (~0.005) but IS merged by snap_tol=0.5,
    reducing the planar face vertex count.

    All vertices are at z=0 (single planar group).  v4 and v5 are at
    horizontal distance 0.02 cm - weld_eps (~5e-7) does not catch them;
    default snap_tol (~0.005) is too tight; snap_tol=0.5 merges them.
    """
    nodes = [
        0, 0, 0,     # v0
        10, 0, 0,    # v1
        10, 10, 0,   # v2
        0, 10, 0,    # v3
        3, 0, 0,     # v4 (on bottom edge)
        3, 0.02, 0,  # v5 (near v4, gap 0.02)
    ]
    indices = [
        0, 4, 5,     # tri a
        0, 5, 3,     # tri b
        5, 1, 2,     # tri c
        5, 2, 3,     # tri d
    ]

    default = decompose_mesh_faces(nodes, indices)
    overridden = decompose_mesh_faces(nodes, indices, snap_tol=0.5)

    v_default = default["planar_faces"][0]["vertex_count"]
    v_override = overridden["planar_faces"][0]["vertex_count"]

    assert v_default > v_override, (
        f"default vertex_count ({v_default}) should be > "
        f"override vertex_count ({v_override}) - snap should merge v4+v5")


# ==========================================================================
# test 4: preset + individual override -> individual wins
# ==========================================================================

def test_preset_and_individual_override():
    """preset='accurate' + snap_tol=0.5 -> individual snap_tol wins.
    Same fixture as test_individual_override; accurate's tiny default
    snap_tol is overridden by the explicit snap_tol=0.5, so vertices
    still merge.
    """
    nodes = [
        0, 0, 0,
        10, 0, 0,
        10, 10, 0,
        0, 10, 0,
        3, 0, 0,
        3, 0.02, 0,
    ]
    indices = [
        0, 4, 5,  0, 5, 3,
        5, 1, 2,  5, 2, 3,
    ]

    result = decompose_mesh_faces(nodes, indices,
                                  preset="accurate", snap_tol=0.5)
    v = result["planar_faces"][0]["vertex_count"]
    # Without snap override, accurate would keep 6 vertices;
    # with snap_tol=0.5, fewer.
    assert v < 6, f"expected vertex_count < 6 (snap overrides accurate), got {v}"


# ==========================================================================
# test 5: invalid preset name -> error in return dict
# ==========================================================================

def test_invalid_preset_raises():
    """preset='ultra' is caught by decompose_mesh_faces and returned as
    an error dict (the function catches ValueError)."""
    nodes, indices = _make_bent_fixture(0.05)
    result = decompose_mesh_faces(nodes, indices, preset="ultra")
    assert "error" in result, "expected error key in result dict"
    assert "unknown preset" in result["error"], (
        f"error message should mention allowed presets, got: {result['error']}")


# ==========================================================================
# test 6: default no-params -> identical output
# ==========================================================================

def test_default_no_params_unchanged():
    """Calling with zero new params returns same dict as calling with
    only the two required positional args (nodes, indices)."""
    nodes, indices = _make_bent_fixture(0.05)
    explicit = decompose_mesh_faces(nodes, indices)
    with_params = decompose_mesh_faces(
        nodes, indices,
        angle_tolerance_deg=None,
        simplify_vertices=True,
        offset_tol=None, snap_tol=None, simp_tol=None, preset=None)

    assert json.dumps(explicit) == json.dumps(with_params)


# ==========================================================================
# test 7: analyze_mesh_data preset passthrough (Bug B regression)
# ==========================================================================

def test_analyze_mesh_data_preset_passthrough():
    """analyze_mesh_data(nodes, indices, normals, preset='accurate')
    produces more planar faces than default (no preset), proving
    the None angle sentinel flows through the intermediate layer
    to decompose's preset resolution."""
    nodes, indices = _make_bent_fixture(0.0299)
    normals = []  # auto-derived in analyze_mesh_data

    default = analyze_mesh_data(nodes, indices, normals)
    accurate = analyze_mesh_data(nodes, indices, normals, preset="accurate")

    n_default = len(default["face_decomposition"]["planar_faces"])
    n_accurate = len(accurate["face_decomposition"]["planar_faces"])

    assert n_default == 1, f"default expected 1 face, got {n_default}"
    assert n_accurate == 2, f"accurate expected 2 faces, got {n_accurate}"
    assert n_accurate > n_default


# ==========================================================================
# test 8: explicit angle overrides preset (Bug B regression)
# ==========================================================================

def test_analyze_mesh_data_angle_override_wins():
    """Explicit angle_tolerance_deg=0.05 overrides preset='coarse' (1.0 deg),
    producing more planar faces than preset='coarse' alone."""
    nodes, indices = _make_bent_fixture(0.0299)
    normals = []

    coarse = analyze_mesh_data(nodes, indices, normals, preset="coarse")
    override = analyze_mesh_data(
        nodes, indices, normals, preset="coarse", angle_tolerance_deg=0.05)

    n_coarse = len(coarse["face_decomposition"]["planar_faces"])
    n_override = len(override["face_decomposition"]["planar_faces"])

    assert n_coarse == 1, f"coarse expected 1 face, got {n_coarse}"
    assert n_override == 2, f"override (0.05 deg) expected 2 faces, got {n_override}"
    assert n_override > n_coarse
