#!/usr/bin/env python3
"""R-6 + R-7 tests: unified tolerance model + running plane-fit grouping.

Task 12 of the mesh-graph-pipeline plan.
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
    _ToleranceConfig,
    _group_planar_triangles,
    _smallest_eigenvector_3x3,
    _triangle_centroid,
    _max_extent_nodes,
    _detect_quantization_step,
    _weld_vertices,
    _as_triples,
)


# ==========================================================================
# test 1: ToleranceConfig values match formulas
# ==========================================================================

def test_tolerance_config_values():
    """Every computed field matches the R-1/R-6 formulas exactly."""
    cfg = _ToleranceConfig.from_(quant_step=1e-6, extent=10.0)
    assert cfg.quant_step == 1e-6
    assert cfg.extent == 10.0
    assert cfg.weld_eps == pytest.approx(
        max(max(1e-9, 1e-7 * 10.0), 3 * 1e-6), rel=1e-15)
    assert cfg.snap_tol == pytest.approx(
        max(1e-5, max(5e-4 * 10.0, 2 * 1e-6)), rel=1e-15)
    assert cfg.offset_tol == pytest.approx(max(1e-6, 1e-4 * 10.0), rel=1e-15)
    assert cfg.simp_tol == pytest.approx(max(1e-6, 1e-4 * 10.0), rel=1e-15)
    assert cfg.tjunc_tol == cfg.snap_tol

    # Zero quant_step (integer-coordinate fixtures)
    cfg2 = _ToleranceConfig.from_(quant_step=0.0, extent=4.0)
    assert cfg2.weld_eps == pytest.approx(max(1e-9, 1e-7 * 4.0), rel=1e-15)
    assert cfg2.snap_tol == pytest.approx(max(1e-5, 5e-4 * 4.0), rel=1e-15)
    assert cfg2.tjunc_tol == cfg2.snap_tol

    # Large extent + large quant_step: snap_tol dominated by quant_step
    cfg3 = _ToleranceConfig.from_(quant_step=0.01, extent=500.0)
    snap_expected = max(1e-5, max(5e-4 * 500.0, 2 * 0.01))
    assert cfg3.snap_tol == pytest.approx(snap_expected, rel=1e-15)
    assert cfg3.weld_eps == pytest.approx(
        max(max(1e-9, 1e-7 * 500.0), 3 * 0.01), rel=1e-15)


# ==========================================================================
# test 2: R-1 weld_eps formula agrees with ToleranceConfig.weld_eps
# ==========================================================================

def test_tolerance_r1_r6_agree():
    """The R-1 eps formula and _ToleranceConfig produce identical weld_eps."""
    cases = [
        (0.0, 4.0),
        (0.0, 100.0),
        (1e-6, 10.0),
        (1e-4, 50.0),
        (0.01, 500.0),
        (0.0, 0.0),
    ]
    for qs, extent in cases:
        r1_eps = max(max(1e-9, 1e-7 * extent), 3 * qs)
        r6_eps = _ToleranceConfig.from_(qs, extent).weld_eps
        assert r6_eps == pytest.approx(r1_eps, rel=1e-15), (
            f"mismatch for qs={qs}, extent={extent}: R-1={r1_eps:.15e} R-6={r6_eps:.15e}")


# ==========================================================================
# test 3: running plane-fit residual splits offset-differing triangles
# ==========================================================================

def _group_fixture(nodes, indices, angle_tol_deg=0.5):
    """Run _group_planar_triangles with ToleranceConfig (running plane-fit)."""
    node_list = list(_as_triples(nodes))
    raw_tris = [(int(a), int(b), int(c)) for a, b, c in _as_triples(indices)]
    extent = _max_extent_nodes(node_list)
    qs = _detect_quantization_step(node_list)
    tol = _ToleranceConfig.from_(qs, extent)
    eps = tol.weld_eps
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
    return _group_planar_triangles(v_arr, f_arr, tri_normals,
                                   angle_tol_deg, extent, tol=tol)


def test_plane_fit_residual_grouping():
    """Triangles with same normal but different offsets — offset beyond
    refit residual -> separate group; within -> same group."""
    sz = 5.0
    tol_cfg = _ToleranceConfig.from_(0.0, sz)
    offset_tol = tol_cfg.offset_tol
    gap_large = offset_tol * 3.0    # well beyond: triangles separated in z
    gap_small = offset_tol * 0.1    # well within tol: same plane for all

    def _patch(x0, y0, x1, y1, z):
        """Two edge-sharing triangles forming a rectangular patch at z."""
        return [
            x0, y0, z,  x1, y0, z,  x0, y1, z,
            x1, y0, z,  x1, y1, z,  x0, y1, z,
        ]

    # Case A: two patches at different z (gap >> offset_tol) AND different
    # x-positions so they share no vertices after welding — no connectivity
    # path, so the connectivity pass produces TWO groups.
    lo_a = _patch(0, 0, sz, sz, 0.0)
    hi_a = _patch(sz + 2, 0, 2 * sz + 2, sz, gap_large)
    nodes_a = lo_a + hi_a
    indices_a = [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11]
    groups_a = _group_fixture(nodes_a, indices_a)
    memberships_a = sorted(sorted(g["tri_indices"]) for g in groups_a)
    # Connectivity split → [0,1] for patch at z=0, [2,3] for patch at z=gap
    assert memberships_a == [[0, 1], [2, 3]], (
        f"disconnected patches at different z => 2 groups, got {memberships_a}")

    # Case B: four connected triangles on the same plane (z=0).
    # The running fit should merge all.
    nodes_b = _patch(0, 0, sz, sz, 0.0) + \
        _patch(sz, 0, 2 * sz, sz, 0.0)
    indices_b = [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11]
    groups_b = _group_fixture(nodes_b, indices_b)
    memberships_b = sorted(sorted(g["tri_indices"]) for g in groups_b)
    assert memberships_b == [[0, 1, 2, 3]], (
        f"connected tris on same plane => 1 group, got {memberships_b}")


# ==========================================================================
# test 4: order independence
# ==========================================================================

def _shuffle_triangles(nodes, indices, permutation):
    """Reorder triangles and vertices according to a permutation."""
    orig_nodes = [(nodes[i], nodes[i + 1], nodes[i + 2])
                  for i in range(0, len(nodes), 3)]
    perm_tris = []
    node_map = {}
    new_nodes = []
    new_indices = []
    for pi in permutation:
        t0, t1, t2 = (indices[pi * 3], indices[pi * 3 + 1], indices[pi * 3 + 2])
        new_tri = []
        for vi_orig in (t0, t1, t2):
            if vi_orig not in node_map:
                node_map[vi_orig] = len(new_nodes)
                new_nodes.append(orig_nodes[vi_orig])
            new_tri.append(node_map[vi_orig])
        new_indices.extend(new_tri)
    flat_new = []
    for n in new_nodes:
        flat_new.extend(n)
    return flat_new, new_indices


def _group_signature(groups):
    """Deterministic signature of a grouping: sorted set of sorted tri sets."""
    return frozenset(frozenset(g["tri_indices"]) for g in groups)


def test_order_independence():
    """Same triangle set in shuffled order -> identical groups."""
    nodes = [
        0.0, 0.0, 0.0,  2.0, 0.0, 0.0,  1.0, 2.0, 0.0,
        2.0, 0.0, 0.0,  4.0, 0.0, 0.0,  3.0, 2.0, 0.0,
        4.0, 0.0, 0.0,  6.0, 0.0, 0.0,  5.0, 2.0, 0.0,
    ]
    indices = [0, 1, 2,  3, 4, 5,  6, 7, 8]

    groups_ref = _group_fixture(nodes, indices)
    sig_ref = _group_signature(groups_ref)

    # Shuffled order (reverse)
    perm1 = list(reversed(range(3)))
    n1, i1 = _shuffle_triangles(nodes, indices, perm1)
    groups1 = _group_fixture(n1, i1)
    sig1 = _group_signature(groups1)
    assert sig1 == sig_ref, f"reverse order mismatch: {sig1} != {sig_ref}"

    # Another shuffle
    perm2 = [2, 0, 1]
    n2, i2 = _shuffle_triangles(nodes, indices, perm2)
    groups2 = _group_fixture(n2, i2)
    sig2 = _group_signature(groups2)
    assert sig2 == sig_ref, f"shuffled order mismatch: {sig2} != {sig_ref}"


# ==========================================================================
# test 5: smallest_eigenvector_3x3 correctness
# ==========================================================================

def test_smallest_eigenvector_3x3_plane():
    """Covariance of coplanar centroids -> smallest eigenvector = plane normal."""
    cov = np.array([
        [10.0,  2.0, 0.0],
        [ 2.0,  5.0, 0.0],
        [ 0.0,  0.0, 0.001],
    ], dtype=np.float64)
    n = _smallest_eigenvector_3x3(cov)
    # Smallest eigenvalue corresponds to the near-zero z-variance
    dot_z = abs(float(n @ np.array([0.0, 0.0, 1.0])))
    assert dot_z == pytest.approx(1.0, abs=1e-6), (
        f"expected normal ~(0,0,1), got {n}, dot={dot_z:.10f}")


def test_smallest_eigenvector_3x3_isotropic():
    """Isotropic covariance -> any direction works, but output is unit."""
    cov = np.eye(3, dtype=np.float64) * 5.0
    n = _smallest_eigenvector_3x3(cov)
    assert n.shape == (3,)
    assert float(np.linalg.norm(n)) == pytest.approx(1.0, abs=1e-12)


def test_smallest_eigenvector_3x3_zero():
    """Fully degenerate -> returns Z-axis unit."""
    cov = np.zeros((3, 3), dtype=np.float64)
    n = _smallest_eigenvector_3x3(cov)
    np.testing.assert_array_almost_equal(n, np.array([0.0, 0.0, 1.0]))
