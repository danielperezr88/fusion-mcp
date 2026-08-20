#!/usr/bin/env python3
"""Extrusion-band recovery tests: spline profiles (2026-08-20).

Validates that ``decompose_mesh_faces`` recovers finely-tessellated
prismatic walls — arbitrary profiles (arcs, corners, ellipses, fitted
splines) extruded along an axis — as single ``extrusion`` faces with
corner-pinned B-spline profiles, and that non-prismatic lookalikes
(twist, taper, noise) are rejected.

All meshes are synthetic numpy-built flat node/index lists fed to
``decompose_mesh_faces``; no network, no Fusion.
"""

import copy
import math
import os
import sys

import numpy as np
import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from mcp_server.mesh_analysis import (  # noqa: E402
    decompose_mesh_faces,
    _find_extrusion_axis,
)


# ---------------------------------------------------------------------------
# Mesh generators
# ---------------------------------------------------------------------------

def extrude_profile(profile_pts, height=1.0, n_rings=4):
    """Extrude a 2D profile polyline (list of (x, y)) along Z.

    A closed polyline (first point == last) extrudes as a tube; an open
    one as a wall band.  Each profile segment produces one quad column
    per z-gap (two triangles).
    """
    closed = (len(profile_pts) >= 2
              and profile_pts[0] == profile_pts[-1])
    pts = profile_pts[:-1] if closed else list(profile_pts)
    n = len(pts)
    levels = [height * k / (n_rings - 1) for k in range(n_rings)]
    nodes = []
    for z in levels:
        for (px, py) in pts:
            nodes.extend([px, py, z])
    indices = []
    for k in range(n_rings - 1):
        for i in range(n):
            j = (i + 1) % n if closed else i + 1
            if not closed and j >= n:
                continue
            v00, v01 = k * n + i, k * n + j
            v10, v11 = (k + 1) * n + i, (k + 1) * n + j
            indices.extend([v00, v10, v11, v00, v11, v01])
    return nodes, indices


def ellipse_points(rx, ry, a0_deg, a1_deg, n_seg):
    """n_seg+1 points along an ellipse arc from a0 to a1 (degrees)."""
    out = []
    for i in range(n_seg + 1):
        th = math.radians(a0_deg + (a1_deg - a0_deg) * i / n_seg)
        out.append((rx * math.cos(th), ry * math.sin(th)))
    return out


def ellipse_arm(start, start_heading_deg, rx, ry, sweep_deg, n_chord):
    """n_chord+1 points along an ellipse arc leaving *start* with the
    tangent at *start_heading_deg* (the arc's local start tangent at
    theta=180 deg is straight down, so the whole arc is rotated to
    match the requested heading, then translated to *start*)."""
    local = ellipse_points(rx, ry, 180.0, 180.0 + sweep_deg, n_chord)
    rot = math.radians(start_heading_deg - (-90.0))
    cr_, sr_ = math.cos(rot), math.sin(rot)
    pts = [(x * cr_ - y * sr_, x * sr_ + y * cr_) for (x, y) in local]
    dx = start[0] - pts[0][0]
    dy = start[1] - pts[0][1]
    return [(x + dx, y + dy) for (x, y) in pts]


def helix_band(r, turns, strips_per_turn, pitch, strip_width):
    """Helical band around Z: strips tile a screw flank, each strip an
    axial band of one azimuthal step, sharing exactly-vertical edges
    with its azimuthal neighbours.  Normals stay radial (perpendicular
    to Z) but strip azimuth correlates perfectly with height — the
    twist signature the band stage must reject."""
    n = turns * strips_per_turn
    dphi = 2.0 * math.pi / n
    dh = pitch / n
    nodes = []
    indices = []
    for i in range(n):
        phi = i * dphi
        z0 = i * dh
        z1 = z0 + strip_width
        c0, s0 = r * math.cos(phi), r * math.sin(phi)
        c1, s1 = r * math.cos(phi + dphi), r * math.sin(phi + dphi)
        base = len(nodes) // 3
        nodes.extend([c0, s0, z0, c1, s1, z0, c1, s1, z1, c0, s0, z1])
        indices.extend([base, base + 1, base + 2,
                        base, base + 2, base + 3])
    return nodes, indices


def tapered_wall(width, height, draft_deg, n_rings=4):
    """Vertical wall whose plane is tilted *draft_deg* from vertical
    (the wall narrows with height).  Normal has a systematic Z
    component — the taper signature."""
    t = math.tan(math.radians(draft_deg))
    nodes = []
    indices = []
    for k in range(n_rings):
        z = height * k / (n_rings - 1)
        inset = t * z
        nodes.extend([-inset, 0.0, z, width + inset, 0.0, z])
    for k in range(n_rings - 1):
        v0, v1 = 2 * k, 2 * k + 1
        v2, v3 = 2 * (k + 1), 2 * (k + 1) + 1
        indices.extend([v0, v2, v3, v0, v3, v1])
    return nodes, indices


def noisy_star(n_ray, r_base, noise, seed=7):
    """Closed star profile with seeded random radial noise."""
    rng = np.random.default_rng(seed)
    pts = []
    for i in range(n_ray):
        th = 2.0 * math.pi * i / n_ray
        r = r_base + noise * rng.uniform(-1.0, 1.0)
        pts.append((r * math.cos(th), r * math.sin(th)))
    pts.append(pts[0])
    return pts


def curved_patches_of(result):
    return result["curved_patches"]


def extrusions_of(result):
    return [p for p in curved_patches_of(result)
            if p.get("surface_type") == "extrusion"]


def cylinders_of(result):
    return [p for p in curved_patches_of(result)
            if p.get("surface_type") == "cylinder"]


# ---------------------------------------------------------------------------
# Positive tests
# ---------------------------------------------------------------------------

def test_l_profile_arc_arms_corner():
    """Two ELLIPSE-arc arms (non-circular, so the cylinder rungs reject
    them) meeting at a ~90 deg corner recover as ONE open extrusion
    face with the junction pinned as a profile corner."""
    arm1 = ellipse_arm((0.0, 0.0), 30.0, 3.0, 1.2, 75.0, 16)
    corner_pt = arm1[-1]
    arm2 = ellipse_arm(corner_pt, 30.0 - 90.0, 1.5, 2.4, 70.0, 16)
    profile = arm1[:-1] + arm2
    nodes, indices = extrude_profile(profile, height=1.2, n_rings=3)
    result = decompose_mesh_faces(nodes, indices)
    exts = extrusions_of(result)
    assert len(exts) == 1, (
        f"expected 1 extrusion, got {len(exts)}: "
        f"{[p.get('surface_type') for p in curved_patches_of(result)]}")
    patch = exts[0]
    assert patch["profile_closed"] is False
    assert patch["recovered_via"] == "spline_band"
    assert len(patch["profile_corner_params"]) == 1, (
        f"expected exactly the arm corner pinned, got "
        f"{patch['profile_corner_params']}")
    assert patch["residual_cm"] < 0.1


def test_spline_profile_half_ellipse():
    """A fine half-ellipse wall (smooth varying-radius profile; coarse
    versions are stolen by the sweep Kasa rung under loose epsilon)
    recovers as one open extrusion with no pinned corners and a small
    control count."""
    profile = ellipse_points(3.0, 1.8, 0.0, 180.0, 24)
    nodes, indices = extrude_profile(profile, height=1.0, n_rings=3)
    result = decompose_mesh_faces(nodes, indices)
    exts = extrusions_of(result)
    assert len(exts) == 1, (
        f"expected 1 extrusion, got {len(exts)}: "
        f"{[p.get('surface_type') for p in curved_patches_of(result)]}")
    patch = exts[0]
    assert patch["profile_closed"] is False
    assert patch["recovered_via"] == "spline_band"
    assert patch["profile_corner_params"] == []
    n_pts = len(profile)
    n_ctrl = len(patch["profile_control_points_cm"])
    assert 4 <= n_ctrl < n_pts, (
        f"control count {n_ctrl} vs points {n_pts}")
    assert patch["residual_cm"] < 0.1


def test_circle_band_recovers_cylinder():
    """A full-circle band recovers as a cylinder (the existing
    competition path or the band Kasa rung — never a spline band)."""
    profile = []
    n_seg = 24
    for i in range(n_seg):
        th = 2.0 * math.pi * i / n_seg
        profile.append((2.0 * math.cos(th), 2.0 * math.sin(th)))
    profile.append(profile[0])
    nodes, indices = extrude_profile(profile, height=1.0, n_rings=3)
    result = decompose_mesh_faces(nodes, indices)
    cyls = cylinders_of(result)
    assert len(cyls) >= 1, "circle band must recover as cylinder(s)"
    radii = {round(c["radius_cm"], 2) for c in cyls}
    assert any(abs(r - 2.0) < 0.2 for r in radii), radii
    assert extrusions_of(result) == []


def test_ellipse_loop_extrusion():
    """A full ellipse loop (closed, varying radius — NOT a cylinder)
    recovers as one closed periodic-spline extrusion face."""
    profile = []
    n_seg = 24
    for i in range(n_seg):
        th = 2.0 * math.pi * i / n_seg
        profile.append((3.0 * math.cos(th), 1.8 * math.sin(th)))
    profile.append(profile[0])
    nodes, indices = extrude_profile(profile, height=1.0, n_rings=3)
    result = decompose_mesh_faces(nodes, indices)
    exts = extrusions_of(result)
    assert len(exts) == 1, (
        f"expected 1 closed extrusion, got {len(exts)}: "
        f"{[p.get('surface_type') for p in curved_patches_of(result)]}")
    patch = exts[0]
    assert patch["profile_closed"] is True
    assert patch["recovered_via"] == "spline_band"
    assert patch["residual_cm"] < 0.1


def test_fillet_corner_profile_cylinder_first():
    """Two straight walls joined by a fine fillet arc: the fillet IS a
    circular cylinder, so the cylinder-first ladder (sweep/competition)
    extracts it as r=1.0 cylinder — the spline band never sees
    circular geometry; the straight walls stay planar."""
    r = 1.0
    fillet = ellipse_points(r, r, 0.0, 90.0, 8)
    straight_a = [(-3.0, 1.0), (-2.0, 1.0)]
    straight_b = [(1.0, 2.0), (1.0, 3.0)]
    profile = straight_a + fillet + straight_b
    nodes, indices = extrude_profile(profile, height=1.0, n_rings=3)
    result = decompose_mesh_faces(nodes, indices)
    cyls = cylinders_of(result)
    assert len(cyls) >= 1, (
        f"fillet must extract as a cylinder, got "
        f"{[p.get('surface_type') for p in curved_patches_of(result)]}")
    assert any(abs(c["radius_cm"] - 1.0) < 0.1 for c in cyls), (
        [c.get("radius_cm") for c in cyls])


# ---------------------------------------------------------------------------
# Negative tests
# ---------------------------------------------------------------------------

def test_helical_band_rejected():
    """A low-lead helical band (normals radial, azimuth drifting with
    height, exactly-vertical shared edges) is twist, not an extrusion —
    no extrusion or band-cylinder patch may be emitted."""
    nodes, indices = helix_band(
        r=2.0, turns=3, strips_per_turn=10, pitch=1.5, strip_width=0.05)
    result = decompose_mesh_faces(nodes, indices)
    assert extrusions_of(result) == [], (
        "helical band must not recover as an extrusion")
    band_cyls = [c for c in cylinders_of(result)
                 if c.get("recovered_via") == "band"]
    assert band_cyls == []


def test_tapered_wall_no_extrusion():
    """A 10 deg-drafted wall (systematic out-of-plane normal tilt) is
    taper, not an extrusion."""
    nodes, indices = tapered_wall(width=3.0, height=1.5, draft_deg=10.0)
    result = decompose_mesh_faces(nodes, indices)
    assert extrusions_of(result) == []
    assert cylinders_of(result) == []


def test_out_of_plane_gate_unit():
    """Unit check of the eigenvalue out-of-plane gate: 8.5 deg of
    systematic normal tilt clears _BAND_OUT_OF_PLANE_MAX (rejected),
    5 deg does not (axis returned)."""
    rng_phi = np.linspace(0.0, 2.0 * np.pi, 36, endpoint=False)
    tilt_reject = np.stack([
        np.cos(rng_phi) * math.cos(math.radians(8.5)),
        np.sin(rng_phi) * math.cos(math.radians(8.5)),
        np.full_like(rng_phi, math.sin(math.radians(8.5)))], axis=1)
    assert _find_extrusion_axis(tilt_reject, np.arange(36)) is None
    tilt_ok = np.stack([
        np.cos(rng_phi) * math.cos(math.radians(5.0)),
        np.sin(rng_phi) * math.cos(math.radians(5.0)),
        np.full_like(rng_phi, math.sin(math.radians(5.0)))], axis=1)
    axis = _find_extrusion_axis(tilt_ok, np.arange(36))
    assert axis is not None
    assert abs(float(axis @ np.array([0.0, 0.0, 1.0]))) > 0.9


def test_noisy_blob_rejected():
    """A noisy star profile (random radial jitter far above epsilon) is
    not smooth: the overfit guard must reject it."""
    profile = noisy_star(n_ray=16, r_base=2.0, noise=0.12)
    nodes, indices = extrude_profile(profile, height=1.0, n_rings=3)
    result = decompose_mesh_faces(nodes, indices)
    assert extrusions_of(result) == [], (
        "noisy profile must not pass the overfit guard")


def test_box_unchanged_by_bands():
    """A unit cube still yields zero curved patches: its 4-face tubes
    are closed square profiles with every vertex cocircular at 4 points
    (Kasa unfalsifiable, gated) and pinned-square fits are
    underdetermined (k >= n, gated)."""
    nodes = [0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 1.0, 1.0, 0.0, 0.0, 1.0, 0.0,
             0.0, 0.0, 1.0, 1.0, 0.0, 1.0, 1.0, 1.0, 1.0, 0.0, 1.0, 1.0]
    indices = [0, 2, 1, 0, 3, 2,
               4, 5, 6, 4, 6, 7,
               0, 1, 5, 0, 5, 4,
               2, 7, 6, 2, 6, 3,
               1, 2, 6, 1, 6, 5,
               0, 4, 7, 0, 7, 3]
    result = decompose_mesh_faces(nodes, indices)
    assert curved_patches_of(result) == [], (
        f"box must stay planar-only, got "
        f"{[p['surface_type'] for p in curved_patches_of(result)]}")


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------

def test_determinism():
    """Two decompositions of the same mesh produce byte-identical
    curved-patch lists (sign-normalized vectors, sorted iteration)."""
    profile = []
    n_seg = 24
    for i in range(n_seg):
        th = 2.0 * math.pi * i / n_seg
        profile.append((3.0 * math.cos(th), 1.8 * math.sin(th)))
    profile.append(profile[0])
    nodes, indices = extrude_profile(profile, height=1.0, n_rings=3)
    r1 = decompose_mesh_faces(nodes, indices)
    r2 = decompose_mesh_faces(copy.deepcopy(nodes),
                              copy.deepcopy(indices))
    assert r1["curved_patches"] == r2["curved_patches"]
