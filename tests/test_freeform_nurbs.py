#!/usr/bin/env python3
"""Freeform NURBS recovery tests (2026-08-21).

Phase-3 finale (P5): leftover smooth components that defeat every
structured pass (no quadric, no extrusion axis, no revolve meridians,
no straight rulings, no ring flow) recover as ONE tensor-product
B-spline height field over the component's flattest PCA plane, with a
3D control net lifted at Greville abscissae ready for NURBS
reconstruction.  Steep/overhanging leftovers (walls, tubes) tilt their
normals past the height-field gate and must NOT pass.

All meshes are synthetic numpy-built flat node/index lists; no network,
no Fusion.
"""

import math
import os
import sys

import numpy as np

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from mcp_server.mesh_analysis import decompose_mesh_faces  # noqa: E402


def curved_of(result):
    return result.get("curved_patches", [])


def types_of(result):
    return [p.get("surface_type") for p in curved_of(result)]


def nurbs_of(result):
    return [p for p in curved_of(result)
            if p.get("recovered_via") == "freeform_nurbs"]


# ---------------------------------------------------------------------------
# Mesh generators
# ---------------------------------------------------------------------------

def bump_field(n=24, amp=0.15, size=1.0):
    """Grid-meshed double-bump wall z = amp*sin(2*pi*x)*sin(pi*y).

    Two lobes of opposite sign along x: not a quadric cap, not a
    revolute, no extrusion axis, no straight rulings, no ring flow —
    a genuine tensor-product freeform.  With ``amp`` small the normals
    stay within ~47 deg of +z; with ``amp`` large they tilt past 80
    deg and the height-field gate must reject the surface.
    """
    nodes = []
    ids = [[0] * n for _ in range(n)]
    for i in range(n):
        for j in range(n):
            x = size * i / (n - 1.0)
            y = size * j / (n - 1.0)
            z = amp * math.sin(2.0 * math.pi * x / size) \
                * math.sin(math.pi * y / size)
            ids[i][j] = len(nodes) // 3
            nodes.extend([x, y, z])
    indices = []
    for i in range(n - 1):
        for j in range(n - 1):
            a, b = ids[i][j], ids[i + 1][j]
            c, d = ids[i + 1][j + 1], ids[i][j + 1]
            indices.extend([a, b, c, a, c, d])
    return nodes, indices


# ---------------------------------------------------------------------------
# P5: freeform NURBS recovery
# ---------------------------------------------------------------------------

def test_double_bump_recovers_as_freeform_nurbs():
    """A gentle double bump recovers as ONE freeform face: cubic
    tensor-product B-spline height field, clamped knots, 3D control
    net, whole surface consumed."""
    nodes, indices = bump_field(n=24, amp=0.15)
    result = decompose_mesh_faces(nodes, indices)
    found = nurbs_of(result)
    assert len(found) == 1, types_of(result)
    patch = found[0]
    assert patch["surface_type"] == "freeform"
    assert patch["parameterization"] == "height_field"
    assert patch["degrees"] == [3, 3]

    nu, nv = patch["grid_dims"]
    assert 4 <= nu <= 12 and 4 <= nv <= 12

    cp = np.asarray(patch["control_points_cm"])
    assert cp.shape == (nu * nv, 3)
    assert float(cp[:, 2].min()) > -0.25
    assert float(cp[:, 2].max()) < 0.25

    bn = np.asarray(patch["base_normal_cm"])
    assert abs(float(bn @ [0.0, 0.0, 1.0])) > 0.99

    for kv in (patch["knots_u"], patch["knots_v"]):
        assert kv[:3] == [0.0, 0.0, 0.0]
        assert kv[-3:] == [1.0, 1.0, 1.0]
        assert all(kv[k] <= kv[k + 1] for k in range(len(kv) - 1))

    assert patch["residual_cm"] < 1e-3
    assert patch["coverage"] == 1.0
    assert patch["confidence"] > 0.9
    assert patch["triangle_count"] == 2 * 23 * 23


def test_steep_double_bump_is_not_freeform_nurbs():
    """A steep double bump tilts normals past the height-field gate —
    no freeform NURBS patch may be emitted."""
    nodes, indices = bump_field(n=32, amp=0.8)
    result = decompose_mesh_faces(nodes, indices)
    assert nurbs_of(result) == [], types_of(result)
