#!/usr/bin/env python3
"""R-8 voxel/SDF fallback detection tests (Task 14, mesh-graph-pipeline plan)."""

import os
import sys

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from mcp_server.mesh_analysis import decompose_mesh_faces


def _decompose(nodes, indices):
    """Call decompose and assert no error."""
    result = decompose_mesh_faces(nodes, indices)
    assert "error" not in result, f"decompose error: {result.get('error')}"
    return result


# ---------------------------------------------------------------------------
# helper: watertight unit-size box (8 verts, 12 tris, closed manifold)
# ---------------------------------------------------------------------------

def _watertight_box():
    """Return (nodes, indices) for a watertight unit cube centred at origin.

    Vertices at all 8 corners of [-0.5, 0.5]³; 12 triangles (2 per face).
    """
    nodes = [
        -1, -1, -1,   1, -1, -1,   1,  1, -1,  -1,  1, -1,
        -1, -1,  1,   1, -1,  1,   1,  1,  1,  -1,  1,  1,
    ]
    indices = [
        0, 1, 2,  0, 2, 3,
        4, 5, 6,  4, 6, 7,
        0, 1, 5,  0, 5, 4,
        2, 3, 7,  2, 7, 6,
        0, 3, 7,  0, 7, 4,
        1, 2, 6,  1, 6, 5,
    ]
    return nodes, indices


# ---------------------------------------------------------------------------
# helper: seam-mismatched strip (two offset patches sharing a far-welded edge)
# ---------------------------------------------------------------------------

def _seam_mismatched_mesh():
    """Three-quad strip with a 0.003 cm seam offset between quads 2 and 3.

    Topology mirrors the proven R-4 offset-seam fixture, but with a tighter
    offset (0.003 cm) that is > snap_tol (~0.002 for a 4-unit model) so the
    seam vertices NEVER snap closed.  The shared far edge (x=6) welds to the
    same vertex IDs, keeping quads 2+3 in the same planar group, while the
    seam edges at x=4 and x=4.003 remain unsnapped — creating residual
    non-manifold edges.

    Returns (nodes, indices).
    """
    offset = 0.003
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
    return nodes, indices


# ---------------------------------------------------------------------------
# test 1: high unpaired ratio → fallback flag set
# ---------------------------------------------------------------------------

def test_high_unpaired_triggers_fallback():
    """Quads with a 0.003 cm offset seam (> snap_tol) produce residual
    non-manifold edges whose count exceeds 5% of total triangle edges.
    decompose_mesh_faces must set strategy_fallback_suggested: "organic"
    and unpaired_pct (rounded to 1 decimal)."""
    nodes, indices = _seam_mismatched_mesh()
    result = _decompose(nodes, indices)

    assert result["has_warnings"] is True, (
        "seam-mismatched mesh must produce warnings")

    assert result.get("strategy_fallback_suggested") == "organic", (
        ">5% unpaired edges must trigger fallback suggestion")
    assert "unpaired_pct" in result, (
        "unpaired_pct must be present when fallback triggered")

    pct = result["unpaired_pct"]
    assert isinstance(pct, float)
    assert pct == pytest.approx(pct, abs=0.05), "pct must be stably rounded"

    unpaired_total = sum(
        face["warnings"][0]["count"]
        for face in result["planar_faces"]
        if face.get("warnings")
    )
    expected_pct = round(100.0 * unpaired_total / (3.0 * 6), 1)
    assert pct == pytest.approx(expected_pct, abs=0.1), (
        f"unpaired_pct {pct} != computed {expected_pct}")

    assert unpaired_total > 0
    ratio = unpaired_total / (3.0 * 6)
    assert ratio > 0.05, (
        f"unpaired ratio {ratio:.4f} must exceed 5% for this test")


# ---------------------------------------------------------------------------
# test 2: clean mesh → no fallback keys
# ---------------------------------------------------------------------------

def test_clean_mesh_no_fallback():
    """A watertight closed box must NOT trigger the fallback.  Neither
    strategy_fallback_suggested nor unpaired_pct may appear in the return
    dict.  The empty-input early-return path also stays unchanged."""
    nodes, indices = _watertight_box()
    result = _decompose(nodes, indices)

    assert "strategy_fallback_suggested" not in result, (
        "clean mesh must NOT have strategy_fallback_suggested")
    assert "unpaired_pct" not in result, (
        "clean mesh must NOT have unpaired_pct")

    assert "components_detected" in result
    assert "planar_faces" in result
    assert "curved_patches" in result
    assert "has_warnings" in result


# ---------------------------------------------------------------------------
# test 3: empty input early-return unchanged
# ---------------------------------------------------------------------------

def test_empty_input_no_fallback_keys():
    """The early-return path (empty nodes/indices) must stay unchanged —
    no new keys added."""
    result = decompose_mesh_faces([], [])
    assert "strategy_fallback_suggested" not in result
    assert "unpaired_pct" not in result
    assert result == {
        "components_detected": 0,
        "planar_faces": [],
        "curved_patches": [],
        "has_warnings": False,
    }, "empty-input return must be byte-identical to pre-R-8"


# ---------------------------------------------------------------------------
# test 4: additive contract — existing keys unchanged when triggered
# ---------------------------------------------------------------------------

def test_additive_contract_fallback_triggered():
    """When the fallback IS triggered, all pre-existing keys must still be
    present with correct types.  Only strategy_fallback_suggested and
    unpaired_pct are added."""
    nodes, indices = _seam_mismatched_mesh()
    result = _decompose(nodes, indices)

    assert isinstance(result["components_detected"], int)
    assert isinstance(result["planar_faces"], list)
    assert isinstance(result["curved_patches"], list)
    assert isinstance(result["has_warnings"], bool)
    assert isinstance(result["strategy_fallback_suggested"], str)
    assert isinstance(result["unpaired_pct"], float)

    for face in result["planar_faces"]:
        assert "component" in face
        assert "face_index" in face
        assert "triangle_count" in face
        assert "vertex_count" in face
        assert "vertices" in face
        assert "normal" in face
        assert "angles_deg" in face
        assert "area" in face
        assert "holes" in face
        assert "warnings" in face
