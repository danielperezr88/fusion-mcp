#!/usr/bin/env python3
"""Extruded-wall sweep recovery tests (2026-08-19).

Validates that ``decompose_mesh_faces`` recovers finely-tessellated
EXTRUDED curved walls — arcs of cylinders sweeping around the extrusion
axis — that previously fragmented into dozens of small planar groups and
were misclassified as freeform.

Two recovery mechanisms are exercised:

  * **chain gate + step-1 competition** (test 2, the annular-sector band):
    sharp 90 deg arc-to-straight junctions split the cluster at the chain
    gate so each arc becomes its own cluster; the competition then fits a
    cylinder on the full 360 deg-symmetric band.
  * **sweep-run recovery** (tests 1 and 7): on OPEN arcs and on
    mixed-radius bands the competition returns freeform, so the sweep
    step splits the cluster into monotonic rotation runs and fits each
    run as its own cylinder (``recovered_via: "sweep"``).

All meshes are synthetic numpy-built flat node/index lists fed to
``decompose_mesh_faces``; no network, no Fusion.
"""

import math
import os
import sys

import numpy as np
import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from mcp_server.mesh_analysis import decompose_mesh_faces


# ---------------------------------------------------------------------------
# Mesh generators
# ---------------------------------------------------------------------------

def extrude_profile(profile_pts, height=1.0, n_rings=4):
    """Extrude a 2D profile polyline (list of (x, y)) along Z.

    A closed polyline (first point == last) extrudes as a tube; an open
    one as a wall band.  Each profile segment produces one quad column
    per z-gap (two triangles).  Coplanar stacked quads merge into a
    single planar group downstream.
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


def arc_points(cx, cy, r, a0_deg, a1_deg, n_seg):
    """n_seg+1 points along a circular arc from a0 to a1 (degrees)."""
    out = []
    for i in range(n_seg + 1):
        th = math.radians(a0_deg + (a1_deg - a0_deg) * i / n_seg)
        out.append((cx + r * math.cos(th), cy + r * math.sin(th)))
    return out


def arc_band_open(r, height, a0_deg, a1_deg, n_seg, n_rings=4):
    """Open arc wall (no caps) extruded along Z."""
    pts = arc_points(0.0, 0.0, r, a0_deg, a1_deg, n_seg)
    return extrude_profile(pts, height, n_rings)


def annular_sector_band(r_out, r_in, half_deg, n_seg_arc,
                        height=1.0, n_rings=4):
    """Closed annular-sector band: outer arc + radial straight + inner
    arc + radial straight.  Arc-to-straight junctions are sharp 90 deg."""
    a0, a1 = -half_deg, half_deg
    profile = []
    profile += arc_points(0.0, 0.0, r_out, a0, a1, n_seg_arc)
    profile += arc_points(0.0, 0.0, r_in, a1, a0, n_seg_arc)
    straight_in = (r_in * math.cos(math.radians(a0)),
                   r_in * math.sin(math.radians(a0)))
    straight_out = (r_out * math.cos(math.radians(a0)),
                    r_out * math.sin(math.radians(a0)))
    profile.append(straight_out)
    return extrude_profile(profile, height, n_rings)


def two_disconnected_arcs(r1, r2, sweep_deg, n1, n2,
                          height=1.0, n_rings=4, sep=5.0):
    """Two open arc walls of different radii as separate components in one
    mesh.  Each becomes its own cluster (chain gate keeps each arc whole,
    sharp gate runs competition which fails on the open arc) so the
    sweep step fires twice and recovers one cylinder per arc."""
    a0, a1 = -sweep_deg / 2.0, sweep_deg / 2.0
    n1_pts = arc_points(sep, 0.0, r1, a0, a1, n1)
    n2_pts = arc_points(-sep, 0.0, r2, a0, a1, n2)

    def band(pts, base):
        closed = False
        n = len(pts)
        levels = [height * k / (n_rings - 1) for k in range(n_rings)]
        nodes = []
        for z in levels:
            for (px, py) in pts:
                nodes.extend([px, py, z])
        indices = []
        for k in range(n_rings - 1):
            for i in range(n - 1):
                v00 = base + k * n + i
                v01 = base + k * n + i + 1
                v10 = base + (k + 1) * n + i
                v11 = base + (k + 1) * n + i + 1
                indices.extend([v00, v10, v11, v00, v11, v01])
        return nodes, indices, base + n_rings * n

    nodes1, idx1, base = band(n1_pts, 0)
    nodes2, idx2, _ = band(n2_pts, base)
    return nodes1 + nodes2, idx1 + idx2


def rectangular_tube(width=2.0, depth=1.0, height=1.0, n_rings=4):
    """Closed rectangular tube (no caps): four flat walls."""
    profile = [(0.0, 0.0), (width, 0.0), (width, depth), (0.0, depth),
               (0.0, 0.0)]
    return extrude_profile(profile, height, n_rings)


def unit_cube():
    """Outward-wound unit cube [0,1]^3 as flat node/index lists."""
    nodes = [0, 0, 0, 1, 0, 0, 1, 1, 0, 0, 1, 0,
             0, 0, 1, 1, 0, 1, 1, 1, 1, 0, 1, 1]
    indices = [0, 2, 1, 0, 3, 2, 4, 5, 6, 4, 6, 7,
               0, 1, 5, 0, 5, 4, 3, 7, 6, 3, 6, 2,
               0, 4, 3, 3, 4, 7, 1, 2, 6, 1, 6, 5]
    return nodes, indices


def capped_cylinder(radius=1.0, height=3.0, segments=23):
    """Outward-wound capped cylinder, axis along Z."""
    nodes = []
    for i in range(segments):
        th = 2.0 * math.pi * i / segments
        nodes.extend([radius * math.cos(th), radius * math.sin(th), 0.0])
    for i in range(segments):
        th = 2.0 * math.pi * i / segments
        nodes.extend([radius * math.cos(th), radius * math.sin(th), height])
    nodes.extend([0.0, 0.0, 0.0, 0.0, 0.0, height])
    b0, t0 = 0, segments
    bc, tc = 2 * segments, 2 * segments + 1
    indices = []
    for i in range(segments):
        j = (i + 1) % segments
        indices.extend([b0 + i, b0 + j, t0 + j, b0 + i, t0 + j, t0 + i])
        indices.extend([bc, b0 + j, b0 + i])
        indices.extend([tc, t0 + i, t0 + j])
    return nodes, indices


def flat_plate_with_slivers():
    """A flat plate plus four disconnected sliver triangles at odd angles.

    The slivers share no edges with the plate (separate components) and
    each is too small (< 6 tris) to form a recoverable cluster, so all
    must remain planar.
    """
    nodes = [
        0.0, 0.0, 0.0, 4.0, 0.0, 0.0, 4.0, 3.0, 0.0, 0.0, 3.0, 0.0,
    ]
    indices = [0, 2, 1, 0, 3, 2]
    base = len(nodes) // 3
    slivers = [
        [(1.0, 0.0, 0.5), (1.2, 0.0, 0.5), (1.1, 0.2, 0.6)],
        [(2.0, 2.5, 0.4), (2.2, 2.5, 0.4), (2.1, 2.7, 0.5)],
        [(3.5, 0.5, -0.3), (3.7, 0.5, -0.3), (3.6, 0.7, -0.2)],
        [(0.5, 2.8, 0.7), (0.7, 2.8, 0.7), (0.6, 3.0, 0.8)],
    ]
    for slv in slivers:
        for p in slv:
            nodes.extend(p)
        a = base
        b = base + 1
        c = base + 2
        indices.extend([a, b, c])
        base += 3
    return nodes, indices


# ---------------------------------------------------------------------------
# Test 1: single open arc wall -> one cylinder via any path
# ---------------------------------------------------------------------------

def test_single_arc_wall_recovered():
    """A quarter-cylinder wall (r=2.0, height 1.0, 24 rings, ~48 tris)
    through full decompose_mesh_faces must yield exactly one cylinder
    patch with radius ~2.0 (tol 5%)."""
    nodes, indices = arc_band_open(2.0, 1.0, 0.0, 90.0, 24, n_rings=4)
    result = decompose_mesh_faces(nodes, indices)
    cylinders = [p for p in result["curved_patches"]
                 if p["surface_type"] == "cylinder"]
    assert len(cylinders) == 1, (
        f"expected 1 cylinder patch, got {len(cylinders)}: "
        f"{[(p['surface_type'], p.get('radius_cm')) for p in result['curved_patches']]}")
    assert cylinders[0]["radius_cm"] == pytest.approx(2.0, abs=0.1), (
        f"expected radius ~2.0, got {cylinders[0]['radius_cm']}")


# ---------------------------------------------------------------------------
# Test 2: two-arc annular-sector band (the money test)
# ---------------------------------------------------------------------------

def test_two_arc_profile_sweep():
    """Closed annular-sector band: outer arc r=2.4 sweeping +110 deg,
    inner arc r=2.0 sweeping -110 deg, joined by two radial straight
    edges (sharp 90 deg junctions).  decompose must recover >= 2 cylinder
    patches with radii ~2.4 and ~2.0 (tol 10%) and the straight edges
    must remain planar (no false cylinder near the straight length)."""
    nodes, indices = annular_sector_band(2.4, 2.0, 55.0, 14)
    result = decompose_mesh_faces(nodes, indices)
    cylinders = [p for p in result["curved_patches"]
                 if p["surface_type"] == "cylinder"]
    radii = sorted(round(p["radius_cm"], 3) for p in cylinders)
    assert len(cylinders) >= 2, (
        f"expected >= 2 cylinder patches, got {len(cylinders)}: "
        f"{radii}")
    assert any(abs(p["radius_cm"] - 2.4) < 0.24 for p in cylinders), (
        f"no cylinder with radius ~2.4 (10% tol); got {radii}")
    assert any(abs(p["radius_cm"] - 2.0) < 0.2 for p in cylinders), (
        f"no cylinder with radius ~2.0 (10% tol); got {radii}")
    straight_len = 2.4 - 2.0
    for p in cylinders:
        assert not (abs(p["radius_cm"] - straight_len) < 0.2 * straight_len), (
            f"false cylinder on a straight edge: r={p['radius_cm']} "
            f"near straight length {straight_len}")


# ---------------------------------------------------------------------------
# Test 3: flat walls only -> 0 curved patches
# ---------------------------------------------------------------------------

def test_flat_wall_no_false_cylinder():
    """An extruded rectangle (flat walls only) must produce 0 curved
    patches — no false cylinder/sphere/torus on flat geometry."""
    nodes, indices = rectangular_tube(2.0, 1.0, 1.0, n_rings=4)
    result = decompose_mesh_faces(nodes, indices)
    assert len(result["curved_patches"]) == 0, (
        f"expected 0 curved patches on flat-only mesh, "
        f"got {len(result['curved_patches'])}: "
        f"{[p['surface_type'] for p in result['curved_patches']]}")


# ---------------------------------------------------------------------------
# Test 4: box unchanged (regression guard for the chain gate)
# ---------------------------------------------------------------------------

def test_box_unchanged():
    """A unit cube must produce 0 curved patches — the chain gate must
    not chain box faces (90 deg dihedrals) into a recoverable cluster."""
    nodes, indices = unit_cube()
    result = decompose_mesh_faces(nodes, indices)
    assert len(result["curved_patches"]) == 0, (
        f"box should have 0 curved patches, got "
        f"{len(result['curved_patches'])}: "
        f"{[p['surface_type'] for p in result['curved_patches']]}")
    assert len(result["planar_faces"]) == 6


# ---------------------------------------------------------------------------
# Test 5: capped cylinder unchanged (cap-retry / sweep-skip regression)
# ---------------------------------------------------------------------------

def test_capped_cylinder_unchanged():
    """A 23-section capped cylinder must still yield exactly 1 cylinder
    patch with radius ~1.0; the sweep step must not break it (it skips
    because the cap normals drop the perp fraction below 80%)."""
    nodes, indices = capped_cylinder(1.0, 3.0, 23)
    result = decompose_mesh_faces(nodes, indices)
    cylinders = [p for p in result["curved_patches"]
                 if p["surface_type"] == "cylinder"]
    assert len(cylinders) == 1, (
        f"expected 1 cylinder patch, got {len(cylinders)}: "
        f"{[(p['surface_type'], p.get('radius_cm')) for p in result['curved_patches']]}")
    assert cylinders[0]["radius_cm"] == pytest.approx(1.0, abs=0.1)
    assert len(result["planar_faces"]) >= 2, (
        f"caps should remain planar (>= 2 planar faces), got "
        f"{len(result['planar_faces'])}")


# ---------------------------------------------------------------------------
# Test 6: sliver megacluster split -> 0 curved, all planar
# ---------------------------------------------------------------------------

def test_sliver_megacluster_split():
    """A flat plate plus disconnected degenerate slivers at odd angles:
    the chain gate must not chain the slivers into a mega-cluster, and
    no curved patch may be produced."""
    nodes, indices = flat_plate_with_slivers()
    result = decompose_mesh_faces(nodes, indices)
    assert len(result["curved_patches"]) == 0, (
        f"expected 0 curved patches on plate+slivers, got "
        f"{len(result['curved_patches'])}: "
        f"{[p['surface_type'] for p in result['curved_patches']]}")
    assert len(result["planar_faces"]) >= 1


# ---------------------------------------------------------------------------
# Test 7: egg profile -> sweep path (two sweep-recovered cylinders)
# ---------------------------------------------------------------------------

def test_egg_profile_sweep_path():
    """Two disconnected open arc walls of different radii in one mesh:
    each forms its own cluster, the competition fails on the open arc
    (vertex-mean axis bias), and the sweep step fires on each to recover
    one cylinder per arc (``recovered_via: "sweep"``) with radii ~2.4
    and ~2.0.

    This is the sweep-exercising companion to test 2 (which exercises the
    chain-gate-split path on a closed annular-sector band).  A
    tangent-continuous single-cluster egg profile was originally
    specified but is geometrically impossible: at a tangent junction the
    arc-end segment and the straight are coplanar within the grouping
    tolerance and merge into one group, so the monotonic run absorbs
    the straight and the next arc instead of stopping at a 0 deg-step.
    Disconnected arcs preserve the design intent (two sweep-recovered
    cylinders of two distinct radii) on a sound geometry.
    """
    nodes, indices = two_disconnected_arcs(2.4, 2.0, 133.0, 14, 12)
    result = decompose_mesh_faces(nodes, indices)
    sweep_cyls = [p for p in result["curved_patches"]
                 if p.get("recovered_via") == "sweep"
                 and p["surface_type"] == "cylinder"]
    radii = sorted(round(p["radius_cm"], 3) for p in sweep_cyls)
    assert len(sweep_cyls) == 2, (
        f"expected exactly 2 sweep-recovered cylinders, got "
        f"{len(sweep_cyls)}: {radii} "
        f"(all curved: {[(p['surface_type'], p.get('recovered_via'), p.get('radius_cm')) for p in result['curved_patches']]})")
    assert any(abs(p["radius_cm"] - 2.4) < 0.24 for p in sweep_cyls), (
        f"no sweep cylinder with radius ~2.4 (10% tol); got {radii}")
    assert any(abs(p["radius_cm"] - 2.0) < 0.2 for p in sweep_cyls), (
        f"no sweep cylinder with radius ~2.0 (10% tol); got {radii}")
