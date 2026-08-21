#!/usr/bin/env python3
"""Revolve & helical band recovery tests (2026-08-21).

Validates the phase-3 surface-characterization ladder:

* ``_accept_revolve_piece`` unit tests — the line (cylinder / cone by
  radial drift), circle (torus / sphere / circle-arc revolve) and
  adaptive-spline rungs, each verified against span vertices, plus the
  scatter rejection.
* ``decompose_mesh_faces`` integration tests — lathes with kinked
  profiles (one cluster, junction walk + corner splits), split-cluster
  kinks, smooth spline lathes, partial revolutions, sphere/torus bands
  (competition path, locking schema parity), helical pipes (springs),
  and negatives (a prismatic tube is not a revolution).

All meshes are synthetic numpy-built flat node/index lists; no network,
no Fusion.
"""

import copy
import math
import os
import sys

import numpy as np

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from mcp_server.mesh_analysis import (  # noqa: E402
    decompose_mesh_faces,
    _accept_revolve_piece,
)

AXIS_Z = np.array([0.0, 0.0, 1.0])
P_AXIS = np.zeros(3)


# ---------------------------------------------------------------------------
# Mesh generators
# ---------------------------------------------------------------------------

def lathe(profile, n_cols=16, phi0=0.0, phi1=None, wrap_profile=False):
    """Revolve an (r, h) profile polyline around Z over [phi0, phi1].

    A full revolution index-welds the azimuth seam column; a partial
    one keeps n_cols + 1 distinct columns.  Cells whose rings collapse
    to the axis (r ~= 0) are skipped.  ``wrap_profile`` closes the
    profile ring with a final row-to-first-row cell (torus profiles).
    """
    if phi1 is None:
        phi1 = phi0 + 2.0 * math.pi
    full = abs((phi1 - phi0) - 2.0 * math.pi) < 1e-9
    nodes = []
    col_ids = []
    for i in range(n_cols + 1):
        if full and i == n_cols:
            col_ids.append(col_ids[0])
            continue
        phi = phi0 + (phi1 - phi0) * i / n_cols
        c, s = math.cos(phi), math.sin(phi)
        ids = []
        for (r, h) in profile:
            base = len(nodes) // 3
            nodes.extend([r * c, r * s, h])
            ids.append(base)
        col_ids.append(ids)
    n_prof = len(profile)
    row_range = range(n_prof) if wrap_profile else range(n_prof - 1)
    indices = []
    for i in range(n_cols):
        a, b = col_ids[i], col_ids[i + 1]
        for j in row_range:
            j2 = (j + 1) % n_prof
            if profile[j][0] <= 1e-9 or profile[j2][0] <= 1e-9:
                continue
            v00, v01 = a[j], a[j2]
            v10, v11 = b[j], b[j2]
            indices.extend([v00, v10, v11, v00, v11, v01])
    return nodes, indices


def helix_pipe(R, rho, pitch, turns, rings_per_turn=24, chords=24):
    """Radial-frame pipe swept along a Z helix.

    Ring k sits at helix azimuth phi_k; the profile circle lies in the
    (r_hat, z) plane, so every surface normal is
    cos(theta) * r_hat + sin(theta) * z — the exact frame the helical
    fit unwraps into a circle."""
    q = pitch / (2.0 * math.pi)
    n_rings = int(round(rings_per_turn * turns))
    nodes = []
    rings = []
    for k in range(n_rings):
        phi = 2.0 * math.pi * k / rings_per_turn
        cph, sph = math.cos(phi), math.sin(phi)
        cx, cy, cz = R * cph, R * sph, q * phi
        ids = []
        for j in range(chords):
            th = 2.0 * math.pi * j / chords
            base = len(nodes) // 3
            nodes.extend([
                cx + rho * math.cos(th) * cph,
                cy + rho * math.cos(th) * sph,
                cz + rho * math.sin(th)])
            ids.append(base)
        rings.append(ids)
    indices = []
    for k in range(n_rings - 1):
        a, b = rings[k], rings[k + 1]
        for j in range(chords):
            j2 = (j + 1) % chords
            indices.extend([a[j], b[j], b[j2], a[j], b[j2], a[j2]])
    return nodes, indices


def bezier3(p0, p1, p2, p3, n):
    """n samples of a cubic Bezier profile in the (r, h) plane."""
    pts = []
    for i in range(n):
        t = i / (n - 1)
        mt = 1.0 - t
        r = (mt ** 3 * p0[0] + 3 * mt ** 2 * t * p1[0]
             + 3 * mt * t ** 2 * p2[0] + t ** 3 * p3[0])
        h = (mt ** 3 * p0[1] + 3 * mt ** 2 * t * p1[1]
             + 3 * mt * t ** 2 * p2[1] + t ** 3 * p3[1])
        pts.append((r, h))
    return pts


def curved_of(result):
    return result["curved_patches"]


def types_of(result):
    return sorted(p.get("surface_type") for p in curved_of(result))


# ---------------------------------------------------------------------------
# Ladder unit tests: _accept_revolve_piece
# ---------------------------------------------------------------------------

def test_ladder_vertical_line_is_cylinder():
    pts = np.array([[2.0, 0.0], [2.0, 1.0]])
    span_h = np.linspace(0.0, 1.0, 41)
    span_r = np.full(41, 2.0)
    out = _accept_revolve_piece(
        pts, False, span_r, span_h, AXIS_Z, P_AXIS, 1e-6)
    assert out is not None and out["surface_type"] == "cylinder"
    assert abs(out["radius_cm"] - 2.0) < 1e-4
    assert abs(out["height_cm"] - 1.0) < 1e-4


def test_ladder_tilted_line_is_cone():
    slope = 0.48 / 0.9
    pts = np.array([[2.0, 1.0], [1.52, 1.9]])
    span_h = np.linspace(1.0, 1.9, 41)
    span_r = 2.0 - slope * (span_h - 1.0)
    out = _accept_revolve_piece(
        pts, False, span_r, span_h, AXIS_Z, P_AXIS, 1e-6)
    assert out is not None and out["surface_type"] == "cone"
    assert abs(out["half_angle_deg"] - math.degrees(math.atan(slope))) < 0.05
    apex = np.array(out["apex_cm"])
    assert float(np.hypot(apex[0], apex[1])) < 1e-4, "apex off axis"
    assert abs(apex[2] - (1.0 + 2.0 / slope)) < 1e-3


def test_ladder_closed_circle_is_torus():
    ang = np.linspace(0.0, 2.0 * math.pi, 17)[:-1]
    pts = np.column_stack([2.0 + 0.8 * np.cos(ang), 0.8 * np.sin(ang)])
    out = _accept_revolve_piece(
        pts, True, pts[:, 0].copy(), pts[:, 1].copy(), AXIS_Z, P_AXIS, 1e-6)
    assert out is not None and out["surface_type"] == "torus"
    assert abs(out["major_radius_cm"] - 2.0) < 1e-3
    assert abs(out["minor_radius_cm"] - 0.8) < 1e-3


def test_ladder_polar_arc_is_sphere():
    ang = np.linspace(math.radians(8.0), math.pi - math.radians(8.0), 13)
    r_sp = 1.5 * np.sin(ang)
    h_sp = 1.5 - 1.5 * np.cos(ang)
    pts = np.column_stack([r_sp, h_sp])
    out = _accept_revolve_piece(
        pts, False, r_sp.copy(), h_sp.copy(), AXIS_Z, P_AXIS, 1e-6)
    assert out is not None and out["surface_type"] == "sphere"
    assert abs(out["radius_cm"] - 1.5) < 1e-3
    assert abs(out["center_cm"][2] - 1.5) < 1e-3


def test_ladder_open_arc_is_circle_revolve():
    ang = np.linspace(math.radians(30.0), math.radians(150.0), 9)
    pts = np.column_stack(
        [3.0 + 0.8 * np.cos(ang), 1.0 + 0.8 * np.sin(ang)])
    out = _accept_revolve_piece(
        pts, False, pts[:, 0].copy(), pts[:, 1].copy(), AXIS_Z, P_AXIS, 1e-6)
    assert out is not None and out["surface_type"] == "revolve"
    assert out["profile_kind"] == "circle"
    assert abs(out["profile_radius_cm"] - 0.8) < 1e-3
    assert out["profile_closed"] is False


def test_ladder_smooth_curve_is_spline_revolve():
    hh = np.linspace(0.0, 4.0, 25)
    rr = 1.0 + 0.7 * np.sin(hh * 0.9)
    pts = np.column_stack([rr, hh])
    out = _accept_revolve_piece(
        pts, False, rr.copy(), hh.copy(), AXIS_Z, P_AXIS, 1e-3)
    assert out is not None and out["surface_type"] == "revolve"
    assert out["profile_kind"] == "spline"
    assert out["profile_closed"] is False


def test_ladder_rejects_scattered_band():
    hh = np.linspace(0.0, 5.0, 73)
    rr = 2.0 + 0.5 * np.cos(np.linspace(0.0, 44.0, 73))
    pts = np.column_stack([rr, hh])
    out = _accept_revolve_piece(
        pts, False, rr.copy(), hh.copy(), AXIS_Z, P_AXIS, 1e-6)
    assert out is None, "scattered band must not fit any ladder rung"


# ---------------------------------------------------------------------------
# Integration tests: decompose_mesh_faces
# ---------------------------------------------------------------------------

def test_waisted_shaft_revolve_band():
    """A 5-span shaft with 28-degree kinks (chains into ONE cluster,
    splits at the revolve corner threshold) recovers as 3 cylinders +
    2 cones, all via the revolve-band walk.  32 columns so the derived
    epsilon lands below the mixed-radius discrimination margin — at
    16 columns the mesh-derived epsilon is coarser than the whole
    cluster's cylinder-fit residual and the competition accepts a
    bogus single cylinder."""
    profile = [(2.0, 0.0), (2.0, 1.0), (1.52, 1.9), (1.52, 2.7),
               (2.0, 3.6), (2.0, 4.4)]
    nodes, indices = lathe(profile, n_cols=32)
    result = decompose_mesh_faces(nodes, indices)
    patches = curved_of(result)
    cones = [p for p in patches if p["surface_type"] == "cone"]
    cyls = [p for p in patches if p["surface_type"] == "cylinder"]
    assert len(cones) == 2, types_of(result)
    for c in cones:
        assert abs(c["half_angle_deg"] - 28.07) < 0.5
        assert c["recovered_via"] == "revolve_band"
    radii = sorted(round(c["radius_cm"], 2) for c in cyls)
    assert radii == [1.52, 2.0, 2.0], radii
    assert all(p["surface_type"] in ("cylinder", "cone")
               for p in patches), types_of(result)


def test_waisted_shaft_revolve_band_16_cols():
    """The 16-column sibling of the test above: with the revolve pass
    running BEFORE the competition, the coarse mesh-derived epsilon no
    longer matters — the walk recovers the true profile (3 cylinders +
    2 cones) instead of the competition accepting a bogus single
    cylinder over the mixed-radius cluster."""
    profile = [(2.0, 0.0), (2.0, 1.0), (1.52, 1.9), (1.52, 2.7),
               (2.0, 3.6), (2.0, 4.4)]
    nodes, indices = lathe(profile, n_cols=16)
    result = decompose_mesh_faces(nodes, indices)
    patches = curved_of(result)
    cones = [p for p in patches if p["surface_type"] == "cone"]
    cyls = [p for p in patches if p["surface_type"] == "cylinder"]
    assert len(cones) == 2, types_of(result)
    assert len(cyls) == 3, types_of(result)
    radii = sorted(round(c["radius_cm"], 2) for c in cyls)
    assert radii == [1.52, 2.0, 2.0], radii
    assert all(p["surface_type"] in ("cylinder", "cone")
               for p in patches), types_of(result)


def test_kinked_shaft_split_clusters():
    """37-degree kinks exceed the 30-degree chaining gate: each span is
    its own cluster.  Whatever pass consumes them (competition or the
    revolve fallback), the composition must be 3 cylinders + 2 cones
    with the right parameters."""
    profile = [(2.0, 0.0), (2.0, 1.0), (1.4, 1.8), (1.4, 2.6),
               (2.0, 3.4), (2.0, 4.2)]
    nodes, indices = lathe(profile, n_cols=32)
    result = decompose_mesh_faces(nodes, indices)
    patches = curved_of(result)
    cones = [p for p in patches if p["surface_type"] == "cone"]
    cyls = [p for p in patches if p["surface_type"] == "cylinder"]
    assert len(cones) == 2, types_of(result)
    for c in cones:
        assert abs(c["half_angle_deg"] - 36.87) < 0.5
    radii = sorted(round(c["radius_cm"], 2) for c in cyls)
    assert radii == [1.4, 2.0, 2.0], radii
    assert all(p["surface_type"] in ("cylinder", "cone")
               for p in patches), types_of(result)


def test_vase_spline_revolve():
    """A smooth cubic-Bezier lathe (no constant-radius rows for the
    sweep, no kinks to split) recovers as ONE spline-profile revolve
    face with a compact control count."""
    profile = bezier3((0.9, 0.0), (2.0, 0.7), (0.5, 2.6), (0.8, 4.4), 24)
    nodes, indices = lathe(profile, n_cols=32)
    result = decompose_mesh_faces(nodes, indices)
    patches = curved_of(result)
    assert len(patches) == 1, types_of(result)
    patch = patches[0]
    assert patch["surface_type"] == "revolve"
    assert patch["profile_kind"] == "spline"
    assert patch["recovered_via"] == "revolve_band"
    assert patch["profile_closed"] is False
    assert len(patch["profile_control_points_cm"]) <= 12
    assert patch["residual_cm"] < 1e-4


def test_partial_revolve_extent_120deg():
    """A 120-degree partial spline revolve keeps its azimuth extent in
    the patch entry (not silently closed to 360)."""
    profile = bezier3((1.8, 0.0), (1.6, 1.2), (0.9, 2.4), (0.7, 3.4), 14)
    nodes, indices = lathe(
        profile, n_cols=12, phi1=math.radians(120.0))
    result = decompose_mesh_faces(nodes, indices)
    patches = curved_of(result)
    assert len(patches) == 1, types_of(result)
    patch = patches[0]
    assert patch["surface_type"] == "revolve"
    assert patch["profile_kind"] == "spline"
    assert 110.0 <= patch["azimuth_extent_deg"] <= 121.0, (
        patch["azimuth_extent_deg"])


def test_sphere_band_via_competition():
    """A fine sphere band recovers as a sphere through the existing
    competition path — the revolve pass must not steal or break it."""
    ang = [math.radians(8.0 + 164.0 * i / 23.0) for i in range(24)]
    profile = [(1.5 * math.sin(a), 1.5 - 1.5 * math.cos(a)) for a in ang]
    nodes, indices = lathe(profile, n_cols=16)
    result = decompose_mesh_faces(nodes, indices)
    patches = curved_of(result)
    assert len(patches) == 1, types_of(result)
    assert patches[0]["surface_type"] == "sphere"
    assert abs(patches[0]["radius_cm"] - 1.5) < 0.02
    assert abs(patches[0]["center_cm"][2] - 1.5) < 0.05


def test_torus_band_via_competition():
    """A full torus band recovers as a torus through the competition
    path (schema parity with the torus rung of the revolve ladder)."""
    profile = [(2.0 + 0.8 * math.cos(2.0 * math.pi * j / 24.0),
                0.8 * math.sin(2.0 * math.pi * j / 24.0))
               for j in range(24)]
    nodes, indices = lathe(profile, n_cols=16, wrap_profile=True)
    result = decompose_mesh_faces(nodes, indices)
    patches = curved_of(result)
    assert len(patches) == 1, types_of(result)
    assert patches[0]["surface_type"] == "torus"
    assert abs(patches[0]["major_radius_cm"] - 2.0) < 0.05
    assert abs(patches[0]["minor_radius_cm"] - 0.8) < 0.05


def test_spring_helical_pipe():
    """A 3-turn spring defeats every prismatic pass (twist, out-of-plane
    axis, overfit) and unwraps to a circle in (r, h - q*phi): one
    helical_pipe face with the design parameters."""
    nodes, indices = helix_pipe(
        R=2.0, rho=0.5, pitch=1.5, turns=3)
    result = decompose_mesh_faces(nodes, indices)
    patches = curved_of(result)
    assert len(patches) == 1, types_of(result)
    patch = patches[0]
    assert patch["surface_type"] == "helical_pipe"
    assert patch["recovered_via"] == "helical_band"
    assert abs(patch["helix_radius_cm"] - 2.0) < 0.05
    assert abs(patch["pitch_cm"] - 1.5) < 0.05
    assert abs(patch["profile_radius_cm"] - 0.5) < 0.05
    assert abs(patch["turns"] - 3.0) < 0.3
    assert patch["azimuth_extent_deg"] > 1000.0


def test_square_tube_not_revolve():
    """A prismatic square tube yields no revolve/helical patches (its
    walls stay planar)."""
    sq = [(1.0, 1.0), (-1.0, 1.0), (-1.0, -1.0), (1.0, -1.0), (1.0, 1.0)]
    levels = [1.5 * k / 2.0 for k in range(3)]
    nodes = []
    for z in levels:
        for (px, py) in sq:
            nodes.extend([px, py, z])
    indices = []
    n = 5
    for k in range(2):
        for i in range(n - 1):
            v00, v01 = k * n + i, k * n + i + 1
            v10, v11 = (k + 1) * n + i, (k + 1) * n + i + 1
            indices.extend([v00, v10, v11, v00, v11, v01])
    result = decompose_mesh_faces(nodes, indices)
    assert curved_of(result) == [], types_of(result)


def test_determinism():
    """Two decompositions of the same kinked shaft produce identical
    curved-patch lists (sorted iteration, sign-normalized fits)."""
    profile = [(2.0, 0.0), (2.0, 1.0), (1.52, 1.9), (1.52, 2.7),
               (2.0, 3.6), (2.0, 4.4)]
    nodes, indices = lathe(profile, n_cols=16)
    r1 = decompose_mesh_faces(nodes, indices)
    r2 = decompose_mesh_faces(copy.deepcopy(nodes),
                              copy.deepcopy(indices))
    assert r1["curved_patches"] == r2["curved_patches"]
