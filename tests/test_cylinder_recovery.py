#!/usr/bin/env python3
"""Regression tests for delta estimator + cylinder recovery (fix-e, 2026-08-12).

Validates:
  (1) Planar-only mesh -> 0 curved patches (no false positives).
  (2) 23-section cylinder -> exactly 1 cylinder curved patch, correct radius.
  (3) Estimator sanity: delta_est ~ noise floor on planar-only,
      delta_est > 0 on a tessellated cylinder.
  (4) Determinism: repeated calls produce identical results.
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
    _estimate_delta,
    _as_triples,
    _weld_vertices,
    _detect_quantization_step,
    _max_extent_nodes,
    _ToleranceConfig,
)


# ---------------------------------------------------------------------------
# Mesh fixtures
# ---------------------------------------------------------------------------

def _unit_cube():
    """Outward-wound unit cube [0,1]^3 as flat node/index lists."""
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
    return nodes, indices


def _cylinder(radius=1.0, height=3.0, segments=23):
    """Outward-wound cylinder, axis along Z from z=0 to z=height."""
    nodes = []
    for i in range(segments):
        th = 2.0 * math.pi * i / segments
        nodes.extend([radius * math.cos(th), radius * math.sin(th), 0.0])
    for i in range(segments):
        th = 2.0 * math.pi * i / segments
        nodes.extend([radius * math.cos(th), radius * math.sin(th), height])
    nodes.extend([0.0, 0.0, 0.0,  0.0, 0.0, height])
    b0, t0 = 0, segments
    bc, tc = 2 * segments, 2 * segments + 1
    indices = []
    for i in range(segments):
        j = (i + 1) % segments
        indices.extend([b0 + i, b0 + j, t0 + j,  b0 + i, t0 + j, t0 + i])
        indices.extend([bc, b0 + j, b0 + i])
        indices.extend([tc, t0 + i, t0 + j])
    return nodes, indices


def _build_mesh_arrays(nodes_flat, indices_flat):
    """Weld vertices and compute per-triangle normals for _estimate_delta."""
    node_list = _as_triples(nodes_flat)
    raw_tris = [(int(a), int(b), int(c))
                for a, b, c in _as_triples(indices_flat)]
    extent = _max_extent_nodes(node_list)
    quant_step = _detect_quantization_step(node_list)
    tol = _ToleranceConfig.from_(quant_step, extent)
    welded_verts, welded_tris = _weld_vertices(
        node_list, raw_tris, tol.weld_eps)
    v_arr = np.array(welded_verts, dtype=np.float64)
    f_arr = np.array([[a, b, c] for a, b, c in welded_tris], dtype=np.int64)
    if f_arr.ndim == 1 and len(f_arr) >= 3:
        f_arr = f_arr.reshape(-1, 3)
    v0v = v_arr[f_arr[:, 0]]
    v1v = v_arr[f_arr[:, 1]]
    v2v = v_arr[f_arr[:, 2]]
    crosses = np.cross(v1v - v0v, v2v - v0v)
    cross_norms = np.linalg.norm(crosses, axis=1)
    safe = np.where(cross_norms > 1e-15, cross_norms, 1.0)
    tri_normals = crosses / safe[:, None]
    return v_arr, f_arr, tri_normals, quant_step, extent


# ---------------------------------------------------------------------------
# (1) Planar-only mesh -> 0 curved patches
# ---------------------------------------------------------------------------

def test_planar_only_zero_curved_patches():
    """A planar-only mesh (unit cube) must produce 0 curved patches:
    delta_est is machine-zero, eps stays at floor, no fit passes."""
    nodes, indices = _unit_cube()
    result = decompose_mesh_faces(nodes, indices)
    assert len(result["curved_patches"]) == 0, (
        f"expected 0 curved patches on planar-only mesh, "
        f"got {len(result['curved_patches'])}: "
        f"{[p['surface_type'] for p in result['curved_patches']]}")


def test_planar_only_planar_faces_present():
    """The planar-only mesh should still produce its planar faces."""
    nodes, indices = _unit_cube()
    result = decompose_mesh_faces(nodes, indices)
    assert len(result["planar_faces"]) == 6


# ---------------------------------------------------------------------------
# (2) 23-section cylinder -> exactly 1 cylinder curved patch
# ---------------------------------------------------------------------------

def test_23_section_cylinder_one_cylinder_patch():
    """A 23-section cylinder (the d0.01 regression shape, 92 tris) must
    produce exactly 1 cylinder curved patch via cap-contamination retry.

    Before fix-e: 25 planar fragments + 0 curved (the bug).
    After fix-e: ~2 planar caps + 1 cylinder."""
    nodes, indices = _cylinder(radius=1.0, height=3.0, segments=23)
    result = decompose_mesh_faces(nodes, indices)
    curved = result["curved_patches"]
    cylinders = [p for p in curved if p["surface_type"] == "cylinder"]
    assert len(cylinders) == 1, (
        f"expected 1 cylinder patch, got {len(cylinders)} "
        f"(all curved: {[(p['surface_type'], p.get('radius_cm')) for p in curved]})")


def test_23_section_cylinder_correct_radius():
    """The recovered cylinder patch must have radius ~1.0 cm."""
    nodes, indices = _cylinder(radius=1.0, height=3.0, segments=23)
    result = decompose_mesh_faces(nodes, indices)
    cylinders = [p for p in result["curved_patches"]
                 if p["surface_type"] == "cylinder"]
    assert len(cylinders) == 1
    assert cylinders[0]["radius_cm"] == pytest.approx(1.0, abs=0.1), (
        f"expected radius ~1.0, got {cylinders[0]['radius_cm']}")


def test_23_section_cylinder_caps_remain_planar():
    """The cap triangles should remain as planar faces (not curved)."""
    nodes, indices = _cylinder(radius=1.0, height=3.0, segments=23)
    result = decompose_mesh_faces(nodes, indices)
    planar = result["planar_faces"]
    curved = result["curved_patches"]
    cylinders = [p for p in curved if p["surface_type"] == "cylinder"]
    assert len(cylinders) == 1
    assert len(planar) >= 2, (
        f"expected >=2 planar faces (caps), got {len(planar)}")


# ---------------------------------------------------------------------------
# (3) Estimator sanity
# ---------------------------------------------------------------------------

def test_estimator_planar_only_noise_floor():
    """On a planar-only mesh, delta_est should be at machine-zero level
    (all 1-ring neighborhoods are perfectly planar)."""
    nodes, indices = _unit_cube()
    v_arr, f_arr, tri_normals, quant_step, extent = _build_mesh_arrays(
        nodes, indices)
    est = _estimate_delta(v_arr, f_arr, tri_normals, quant_step, extent)
    assert est["delta_est"] < 1e-6, (
        f"planar-only delta_est should be ~0, got {est['delta_est']:.2e}")
    assert est["epsilon"] >= est["floor"], (
        f"epsilon {est['epsilon']:.2e} should be >= floor {est['floor']:.2e}")


def test_estimator_cylinder_positive_delta():
    """On a tessellated cylinder, delta_est should be positive
    (sagitta-level: r*(1-cos(pi/n)) ~ 0.009 for n=23, r=1)."""
    nodes, indices = _cylinder(radius=1.0, height=3.0, segments=23)
    v_arr, f_arr, tri_normals, quant_step, extent = _build_mesh_arrays(
        nodes, indices)
    est = _estimate_delta(v_arr, f_arr, tri_normals, quant_step, extent)
    assert est["delta_est"] > 1e-4, (
        f"cylinder delta_est should be > 0, got {est['delta_est']:.2e}")


def test_estimator_epsilon_has_sensible_floor():
    """The derived epsilon must never go below ~10x quant_step
    so planar-only meshes do not produce false curved patches."""
    nodes, indices = _unit_cube()
    v_arr, f_arr, tri_normals, quant_step, extent = _build_mesh_arrays(
        nodes, indices)
    est = _estimate_delta(v_arr, f_arr, tri_normals, quant_step, extent)
    expected_floor = max(10.0 * quant_step, 1e-6) if quant_step > 0 else 1e-6
    assert est["epsilon"] >= min(expected_floor, 0.1 * est["ceiling"]), (
        f"epsilon {est['epsilon']:.2e} below floor")


# ---------------------------------------------------------------------------
# (4) Determinism
# ---------------------------------------------------------------------------

def test_cylinder_recovery_deterministic():
    """Decomposition with cylinder recovery must be deterministic."""
    nodes, indices = _cylinder(radius=1.0, height=3.0, segments=23)
    a = decompose_mesh_faces(nodes, indices)
    b = decompose_mesh_faces(nodes, indices)
    assert a == b
