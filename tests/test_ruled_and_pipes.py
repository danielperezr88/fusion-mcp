#!/usr/bin/env python3
"""Ruled-band (loft) and general-pipe recovery tests (2026-08-21).

Phase-3 continuation:

* **P2 ruled bands** — a linear-blend loft (square-to-round transition)
  fragments into small planar groups that no earlier pass claims (not a
  revolute, no extrusion axis, no quadric).  Its vertex tracks are
  STRAIGHT lines, so collinear edge chains detect it; the two rail
  polylines are the track endpoints.  A smoothstep-blended wall (curved
  tracks) must NOT pass.
* **P4 general pipes** — a circular-profile tube swept along a planar
  spline spine defeats every prismatic pass.  Removing the
  longitudinal (tangent-parallel) edges leaves congruent round rings
  whose centroids trace the spine.  A tapered tube (non-constant
  radius) must NOT pass.

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


# ---------------------------------------------------------------------------
# Mesh generators
# ---------------------------------------------------------------------------

def square_point(t, half):
    """Perimeter point of a square at parameter t in [0, 1).

    Starts at corner (-half, -half) and walks the boundary CCW; corners
    land on t = 0, 0.25, 0.5, 0.75.
    """
    u = (t % 1.0) * 4.0
    e = int(math.floor(u))
    f = u - e
    if e == 0:
        return (-half + 2.0 * half * f, -half)
    if e == 1:
        return (half, -half + 2.0 * half * f)
    if e == 2:
        return (half - 2.0 * half * f, half)
    return (-half, half - 2.0 * half * f)


def blend_loft(n_pts=16, n_rings=5, half=1.0, r_top=0.7, height=2.0,
               smoothstep=False, bow=0.0):
    """Wall of a square-to-round transition as a ring blend.

    Track i interpolates the square perimeter point and the circle
    point in the SAME radial direction; with ``smoothstep=False`` and
    ``bow=0`` the interpolation is linear, so every track is a
    straight segment (a true ruled surface).  With ``smoothstep=True``
    a radial bulge ``bow * s * (1 - s)`` bows every track off its
    chord and the wall is not ruled.
    """
    nodes = []
    rings = []
    for k in range(n_rings):
        u = k / (n_rings - 1.0)
        s = (u * u * (3.0 - 2.0 * u)) if smoothstep else u
        rad = bow * s * (1.0 - s)
        ids = []
        for i in range(n_pts):
            t = i / n_pts
            sx, sy = square_point(t, half)
            rn = math.hypot(sx, sy)
            cx, cy = r_top * sx / rn, r_top * sy / rn
            base = len(nodes) // 3
            nodes.extend([(1.0 - s) * sx + s * cx + rad * sx / rn,
                          (1.0 - s) * sy + s * cy + rad * sy / rn,
                          s * height])
            ids.append(base)
        rings.append(ids)
    indices = []
    for k in range(n_rings - 1):
        a, b = rings[k], rings[k + 1]
        for j in range(n_pts):
            j2 = (j + 1) % n_pts
            indices.extend([a[j], b[j], b[j2], a[j], b[j2], a[j2]])
    return nodes, indices


def _bezier3(p0, p1, p2, p3, t):
    mt = 1.0 - t
    return (mt ** 3 * p0 + 3.0 * mt ** 2 * t * p1
            + 3.0 * mt * t ** 2 * p2 + t ** 3 * p3)


def _bezier3_tan(p0, p1, p2, p3, t):
    mt = 1.0 - t
    d = (3.0 * mt ** 2 * (p1 - p0) + 6.0 * mt * t * (p2 - p1)
         + 3.0 * t ** 2 * (p3 - p2))
    return d / np.linalg.norm(d)


def bent_pipe(rho=0.5, n_stations=24, chords=16, taper=False):
    """Circular-profile tube swept along a planar S-curve spline spine.

    The spine is a cubic Bezier in the XZ plane with tangent varying
    continuously (not circular, not straight, no pitch).  ``taper``
    grows the profile radius along the spine — a cone-like tube that is
    NOT a constant-section pipe.
    """
    p0 = np.array([0.0, 0.0, 0.0])
    p1 = np.array([6.0, 0.0, 0.0])
    p2 = np.array([0.0, 0.0, 6.0])
    p3 = np.array([6.0, 0.0, 6.0])
    up = np.array([0.0, 1.0, 0.0])
    nodes = []
    rings = []
    for k in range(n_stations):
        t = k / (n_stations - 1.0)
        c = _bezier3(p0, p1, p2, p3, t)
        tan = _bezier3_tan(p0, p1, p2, p3, t)
        n_hat = np.cross(up, tan)
        n_hat = n_hat / np.linalg.norm(n_hat)
        b_hat = np.cross(tan, n_hat)
        r_k = (0.3 + 0.4 * t) if taper else rho
        ids = []
        for j in range(chords):
            th = 2.0 * math.pi * j / chords
            base = len(nodes) // 3
            pt = c + r_k * (math.cos(th) * n_hat + math.sin(th) * b_hat)
            nodes.extend([float(pt[0]), float(pt[1]), float(pt[2])])
            ids.append(base)
        rings.append(ids)
    indices = []
    for k in range(n_stations - 1):
        a, b = rings[k], rings[k + 1]
        for j in range(chords):
            j2 = (j + 1) % chords
            indices.extend([a[j], b[j], b[j2], a[j], b[j2], a[j2]])
    return nodes, indices


# ---------------------------------------------------------------------------
# P2: ruled-band (loft) recovery
# ---------------------------------------------------------------------------

def test_square_to_round_is_ruled_loft():
    """A linear-blend square-to-round wall recovers as ONE loft face
    with straight rulings between the square rail and the circle
    rail."""
    nodes, indices = blend_loft(n_pts=16, n_rings=5)
    result = decompose_mesh_faces(nodes, indices)
    patches = curved_of(result)
    lofts = [p for p in patches if p["surface_type"] == "loft"]
    assert len(lofts) == 1, types_of(result)
    patch = lofts[0]
    assert patch["recovered_via"] == "ruled_band"
    assert patch["ruling"] == "line"

    rail_a = np.asarray(patch["rail_a_points_cm"])
    rail_b = np.asarray(patch["rail_b_points_cm"])
    assert len(rail_a) == 16 and len(rail_b) == 16
    assert patch["rails_closed"] is True

    # rail A: the square ring at z = 0, half-width 1
    assert float(np.abs(rail_a[:, 2]).max()) < 1e-6
    assert abs(float(np.abs(rail_a[:, 0]).max()) - 1.0) < 1e-6
    assert abs(float(np.abs(rail_a[:, 1]).max()) - 1.0) < 1e-6

    # rail B: the circle ring at z = height, radius r_top
    assert abs(float(rail_b[:, 2].mean()) - 2.0) < 1e-6
    radii = np.hypot(rail_b[:, 0], rail_b[:, 1])
    assert float(np.abs(radii - 0.7).max()) < 1e-6

    assert patch["residual_cm"] < 1e-4
    assert patch["coverage"] > 0.95


def test_smoothstep_blend_is_not_ruled():
    """A smoothstep-blended wall with a radial bulge bows every track
    off its chord — no straight rulings, so no loft patch (the wall
    stays unclassified/freeform)."""
    nodes, indices = blend_loft(n_pts=16, n_rings=6, smoothstep=True,
                                bow=0.5)
    result = decompose_mesh_faces(nodes, indices)
    lofts = [p for p in curved_of(result)
             if p["surface_type"] == "loft"]
    assert lofts == [], types_of(result)


# ---------------------------------------------------------------------------
# P4: general pipe recovery
# ---------------------------------------------------------------------------

def test_spline_spine_pipe():
    """A constant-radius tube along a spline S-curve spine recovers as
    ONE pipe face: round congruent rings, spine traced by ring
    centroids, profile radius preserved."""
    nodes, indices = bent_pipe(rho=0.5, n_stations=24, chords=16)
    result = decompose_mesh_faces(nodes, indices)
    patches = curved_of(result)
    pipes = [p for p in patches if p["surface_type"] == "pipe"]
    assert len(pipes) == 1, types_of(result)
    patch = pipes[0]
    assert patch["recovered_via"] == "pipe_band"

    spine = np.asarray(patch["spine_points_cm"])
    assert len(spine) == 24
    assert patch["stations"] == 24
    assert abs(patch["profile_radius_cm"] - 0.5) < 0.02

    # spine endpoints are the Bezier endpoints (ring centroids sit on
    # the spine exactly for symmetric sampling)
    assert float(np.linalg.norm(spine[0])) < 1e-6, spine[0]
    assert float(np.linalg.norm(spine[-1] - [6.0, 0.0, 6.0])) < 1e-6

    assert patch["residual_cm"] < 1e-4


def test_tapered_pipe_is_not_pipe():
    """A tube whose radius grows along the spine has no constant
    profile — no pipe patch may be emitted."""
    nodes, indices = bent_pipe(rho=0.5, n_stations=24, chords=16,
                               taper=True)
    result = decompose_mesh_faces(nodes, indices)
    pipes = [p for p in curved_of(result)
             if p["surface_type"] == "pipe"]
    assert pipes == [], types_of(result)
