#!/usr/bin/env python3
"""R-5 hardened hole classification tests (Task 11, mesh-graph-pipeline plan)."""

import os
import sys
from unittest.mock import patch

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from mcp_server import mesh_analysis
from mcp_server.mesh_analysis import decompose_mesh_faces


def _annulus_nodes_indices(ox0, oy0, ox1, oy1, ix0, iy0, ix1, iy1):
    """Triangulate a flat square-with-square-hole annulus in z=0."""
    nodes = [
        ox0, oy0, 0.0,   # 0: outer bottom-left
        ox1, oy0, 0.0,   # 1: outer bottom-right
        ox1, oy1, 0.0,   # 2: outer top-right
        ox0, oy1, 0.0,   # 3: outer top-left
        ix0, iy0, 0.0,   # 4: inner bottom-left
        ix1, iy0, 0.0,   # 5: inner bottom-right
        ix1, iy1, 0.0,   # 6: inner top-right
        ix0, iy1, 0.0,   # 7: inner top-left
    ]
    tris = [
        0, 1, 5,   0, 5, 4,
        1, 2, 6,   1, 6, 5,
        2, 3, 7,   2, 7, 6,
        3, 0, 4,   3, 4, 7,
    ]
    return nodes, tris


def _decompose(nodes, indices):
    result = decompose_mesh_faces(nodes, indices)
    assert "error" not in result, f"decompose error: {result.get('error')}"
    return result


# ---------------------------------------------------------------------------
# test 1: opposite-signed area -> hole
# ---------------------------------------------------------------------------

def test_opposite_signed_area_hole():
    """CW inner loop inside CCW outer -> inner classified as a hole."""
    nodes, indices = _annulus_nodes_indices(0, 0, 4, 4, 1, 1, 3, 3)
    result = _decompose(nodes, indices)
    planar = result["planar_faces"]
    assert len(planar) == 1
    face = planar[0]
    assert len(face["holes"]) == 1
    assert len(face["holes"][0]) == 4
    assert face["area"] == pytest.approx(12.0, abs=1e-3)


# ---------------------------------------------------------------------------
# test 2: same-signed area -> merged
# ---------------------------------------------------------------------------

def test_same_signed_merge():
    """Same signed-area sign -> R-5 pre-check merges, never a hole."""
    nodes, indices = _annulus_nodes_indices(0, 0, 4, 4, 1, 1, 3, 3)
    with patch.object(mesh_analysis, "_same_sign_2d", return_value=True):
        result = _decompose(nodes, indices)
    face = result["planar_faces"][0]
    assert face["holes"] == []
    assert face["area"] == pytest.approx(16.0, abs=1e-3)


# ---------------------------------------------------------------------------
# test 3: winding-number fallback
# ---------------------------------------------------------------------------

def _clean_bl_wrapper(orig_bl):
    """Wrap _boundary_loops to clear residual_pairs so winding-number
    guard stays active on the annulus fixture."""
    def _clean(*a, **kw):
        loops = orig_bl(*a, **kw)
        ro = kw.get("residual_out")
        if ro is not None and "residual_pairs" in ro:
            ro["residual_pairs"].clear()
        return loops
    return _clean


def test_winding_number_fallback_merge():
    """Winding number > 0.5 -> merged (filled interior)."""
    nodes, indices = _annulus_nodes_indices(0, 0, 4, 4, 1, 1, 3, 3)
    orig_bl = mesh_analysis._boundary_loops
    with (  # four context managers
        patch.object(mesh_analysis, "_loop_contains_centroid",
                     return_value=True),
        patch.object(mesh_analysis, "_centroid_near_boundary",
                     return_value=True),
        patch.object(mesh_analysis, "_generalized_winding_number",
                     return_value=0.95),
        patch.object(mesh_analysis, "_boundary_loops",
                     _clean_bl_wrapper(orig_bl)),
    ):
        result = _decompose(nodes, indices)

    face = result["planar_faces"][0]
    assert face["holes"] == []
    assert face["area"] == pytest.approx(16.0, abs=1e-3)


def test_winding_number_fallback_hole():
    """Winding number < 0.5 -> hole (empty interior)."""
    nodes, indices = _annulus_nodes_indices(0, 0, 4, 4, 1, 1, 3, 3)
    orig_bl = mesh_analysis._boundary_loops
    with (  # four context managers
        patch.object(mesh_analysis, "_loop_contains_centroid",
                     return_value=True),
        patch.object(mesh_analysis, "_centroid_near_boundary",
                     return_value=True),
        patch.object(mesh_analysis, "_generalized_winding_number",
                     return_value=0.12),
        patch.object(mesh_analysis, "_boundary_loops",
                     _clean_bl_wrapper(orig_bl)),
    ):
        result = _decompose(nodes, indices)

    face = result["planar_faces"][0]
    assert len(face["holes"]) == 1
    assert face["area"] == pytest.approx(12.0, abs=1e-3)


# ---------------------------------------------------------------------------
# test: degenerate hole filtered (plan QA)
# ---------------------------------------------------------------------------

def test_degenerate_hole_filtered():
    """A hole loop with area below _MIN_FACE_AREA is filtered."""
    nodes, indices = _annulus_nodes_indices(
        0, 0, 4, 4, 2, 2, 2.001, 2.001)
    result = _decompose(nodes, indices)
    face = result["planar_faces"][0]
    assert face["holes"] == []
