#!/usr/bin/env python3
"""Regression tests for 5-model curved-patch competition (sphere/cone/torus port).

Validates that ``_classify_curved_patch`` correctly classifies genuinely
spherical, conical, and toroidal patches via the model competition
(cylinder / sphere / cone / torus / freeform), and that the existing
cylinder path through ``decompose_mesh_faces`` remains regression-free.
"""

import math
import os
import sys

import numpy as np
import pytest
import trimesh

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from mcp_server.mesh_analysis import (
    decompose_mesh_faces,
    _classify_curved_patch,
)


# ---------------------------------------------------------------------------
# Mesh generators
# ---------------------------------------------------------------------------

def _make_sphere_trimesh(radius=2.0, n_lat=12, n_lon=16):
    """UV-sphere as a trimesh.Trimesh (outward-wound)."""
    nodes = []
    for i in range(n_lat + 1):
        phi = math.pi * i / n_lat
        for j in range(n_lon):
            theta = 2.0 * math.pi * j / n_lon
            nodes.extend([
                radius * math.sin(phi) * math.cos(theta),
                radius * math.sin(phi) * math.sin(theta),
                radius * math.cos(phi),
            ])
    faces = []
    for i in range(n_lat):
        for j in range(n_lon):
            a = i * n_lon + (j % n_lon)
            b = (i + 1) * n_lon + (j % n_lon)
            c = (i + 1) * n_lon + ((j + 1) % n_lon)
            d = i * n_lon + ((j + 1) % n_lon)
            faces.extend([a, b, c, a, c, d])
    verts = np.array(nodes, dtype=np.float64).reshape(-1, 3)
    faces_arr = np.array(faces, dtype=np.int64).reshape(-1, 3)
    return trimesh.Trimesh(vertices=verts, faces=faces_arr, process=False)


def _make_torus_trimesh(R=2.0, r=0.8, n_major=24, n_minor=16):
    """Ring torus as a trimesh.Trimesh (outward-wound)."""
    nodes = []
    for i in range(n_major):
        u = 2.0 * math.pi * i / n_major
        cos_u, sin_u = math.cos(u), math.sin(u)
        for j in range(n_minor):
            v = 2.0 * math.pi * j / n_minor
            cos_v, sin_v = math.cos(v), math.sin(v)
            nodes.extend([
                (R + r * cos_v) * cos_u,
                (R + r * cos_v) * sin_u,
                r * sin_v,
            ])
    faces = []
    for i in range(n_major):
        for j in range(n_minor):
            i_next = (i + 1) % n_major
            j_next = (j + 1) % n_minor
            v00 = i * n_minor + j
            v01 = i * n_minor + j_next
            v10 = i_next * n_minor + j
            v11 = i_next * n_minor + j_next
            faces.extend([v00, v10, v11, v00, v11, v01])
    verts = np.array(nodes, dtype=np.float64).reshape(-1, 3)
    faces_arr = np.array(faces, dtype=np.int64).reshape(-1, 3)
    return trimesh.Trimesh(vertices=verts, faces=faces_arr, process=False)


def _make_cone_trimesh(r1=1.0, r2=0.3, height=3.0, n_sections=18,
                       n_rings=4):
    """Open cone frustum (side wall only) as a trimesh.Trimesh.

    >=3 rings ensure the vertex set is NOT co-spherical, forcing
    the sphere fit residual well above zero.
    """
    nodes = []
    for ri in range(n_rings):
        t = ri / (n_rings - 1)
        z = t * height
        r = r1 + (r2 - r1) * t
        for si in range(n_sections):
            theta = 2.0 * math.pi * si / n_sections
            nodes.extend([r * math.cos(theta), r * math.sin(theta), z])
    faces = []
    for ri in range(n_rings - 1):
        for si in range(n_sections):
            si_next = (si + 1) % n_sections
            v00 = ri * n_sections + si
            v01 = ri * n_sections + si_next
            v10 = (ri + 1) * n_sections + si
            v11 = (ri + 1) * n_sections + si_next
            faces.extend([v00, v10, v11, v00, v11, v01])
    verts = np.array(nodes, dtype=np.float64).reshape(-1, 3)
    faces_arr = np.array(faces, dtype=np.int64).reshape(-1, 3)
    return trimesh.Trimesh(vertices=verts, faces=faces_arr, process=False)


def _cylinder_flat(radius=1.0, height=3.0, segments=23):
    """Outward-wound cylinder as flat node/index lists."""
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


# ---------------------------------------------------------------------------
# Sphere classification (direct)
# ---------------------------------------------------------------------------

def test_sphere_classified_as_sphere():
    """A UV-sphere patch must classify as sphere, not freeform."""
    patch = _make_sphere_trimesh(radius=2.0, n_lat=12, n_lon=16)
    result = _classify_curved_patch(patch, epsilon=0.1)
    assert result["surface_type"] == "sphere", (
        f"expected sphere, got {result['surface_type']}")


def test_sphere_correct_radius():
    """The sphere fit must recover radius ~2.0 cm."""
    patch = _make_sphere_trimesh(radius=2.0, n_lat=12, n_lon=16)
    result = _classify_curved_patch(patch, epsilon=0.1)
    assert result["surface_type"] == "sphere"
    assert result["radius_cm"] == pytest.approx(2.0, abs=0.3), (
        f"expected radius ~2.0, got {result['radius_cm']}")


def test_sphere_not_misclassified_as_cylinder():
    """A genuine sphere must NOT classify as cylinder."""
    patch = _make_sphere_trimesh(radius=2.0, n_lat=12, n_lon=16)
    result = _classify_curved_patch(patch, epsilon=0.1)
    assert result["surface_type"] != "cylinder"


# ---------------------------------------------------------------------------
# Torus classification (direct)
# ---------------------------------------------------------------------------

def test_torus_classified_as_torus():
    """A ring-torus patch must classify as torus."""
    patch = _make_torus_trimesh(R=2.0, r=0.8, n_major=24, n_minor=16)
    result = _classify_curved_patch(patch, epsilon=0.1)
    assert result["surface_type"] == "torus", (
        f"expected torus, got {result['surface_type']}")


def test_torus_correct_radii():
    """The torus fit must recover major ~2.0 and minor ~0.8 cm."""
    patch = _make_torus_trimesh(R=2.0, r=0.8, n_major=24, n_minor=16)
    result = _classify_curved_patch(patch, epsilon=0.1)
    assert result["surface_type"] == "torus"
    assert result["major_radius_cm"] == pytest.approx(2.0, abs=0.4), (
        f"expected major R ~2.0, got {result['major_radius_cm']}")
    assert result["minor_radius_cm"] == pytest.approx(0.8, abs=0.2), (
        f"expected minor r ~0.8, got {result['minor_radius_cm']}")


# ---------------------------------------------------------------------------
# Cone classification (direct)
# ---------------------------------------------------------------------------

def test_cone_classified_as_cone():
    """An open cone frustum patch must classify as cone."""
    patch = _make_cone_trimesh(r1=1.0, r2=0.3, height=3.0,
                               n_sections=18, n_rings=4)
    result = _classify_curved_patch(patch, epsilon=0.1)
    assert result["surface_type"] == "cone", (
        f"expected cone, got {result['surface_type']}")


def test_cone_correct_half_angle():
    """The cone fit must recover half-angle ~13.13 deg."""
    expected_ha = math.degrees(math.atan((1.0 - 0.3) / 3.0))
    patch = _make_cone_trimesh(r1=1.0, r2=0.3, height=3.0,
                               n_sections=18, n_rings=4)
    result = _classify_curved_patch(patch, epsilon=0.1)
    assert result["surface_type"] == "cone"
    assert result["half_angle_deg"] == pytest.approx(expected_ha, abs=3.0), (
        f"expected half-angle ~{expected_ha:.1f}, "
        f"got {result['half_angle_deg']}")


def test_cone_not_misclassified_as_sphere():
    """A 4-ring cone frustum (non-co-spherical) must NOT classify as sphere."""
    patch = _make_cone_trimesh(r1=1.0, r2=0.3, height=3.0,
                               n_sections=18, n_rings=4)
    result = _classify_curved_patch(patch, epsilon=0.1)
    assert result["surface_type"] != "sphere"


# ---------------------------------------------------------------------------
# Preference rules (validated competition ordering)
# ---------------------------------------------------------------------------

def test_cylinder_preference_over_sphere():
    """A cylinder patch with high-confidence cylinder fit must pick
    cylinder, not sphere.  The sphere normal-radial alignment gate
    should reject the sphere fit on a tall cylinder (h >> r).
    """
    nodes, indices = [], []
    radius, height, segments = 1.0, 6.0, 24
    for ring in range(7):
        z = ring * height / 6
        for si in range(segments):
            theta = 2.0 * math.pi * si / segments
            nodes.extend([radius * math.cos(theta),
                          radius * math.sin(theta), z])
    for ri in range(6):
        for si in range(segments):
            si_next = (si + 1) % segments
            v00 = ri * segments + si
            v01 = ri * segments + si_next
            v10 = (ri + 1) * segments + si
            v11 = (ri + 1) * segments + si_next
            indices.extend([v00, v10, v11, v00, v11, v01])
    verts = np.array(nodes, dtype=np.float64).reshape(-1, 3)
    faces = np.array(indices, dtype=np.int64).reshape(-1, 3)
    patch = trimesh.Trimesh(vertices=verts, faces=faces, process=False)
    result = _classify_curved_patch(patch, epsilon=0.1)
    assert result["surface_type"] == "cylinder", (
        f"expected cylinder, got {result['surface_type']}")


# ---------------------------------------------------------------------------
# Freeform fallback + residual_cm additive field
# ---------------------------------------------------------------------------

def test_freeform_when_no_model_fits():
    """A random noisy mesh must classify as freeform."""
    rng = np.random.RandomState(42)
    verts = rng.randn(30, 3)
    faces = np.array([[i, i + 1, i + 2] for i in range(0, 28)])
    patch = trimesh.Trimesh(vertices=verts, faces=faces, process=False)
    result = _classify_curved_patch(patch, epsilon=0.001)
    assert result["surface_type"] == "freeform"


def test_winning_model_has_residual_cm():
    """The winning model dict must include the additive residual_cm field."""
    patch = _make_sphere_trimesh(radius=2.0, n_lat=12, n_lon=16)
    result = _classify_curved_patch(patch, epsilon=0.1)
    assert "residual_cm" in result, (
        f"residual_cm missing from {result['surface_type']} result")


# ---------------------------------------------------------------------------
# epsilon=None backward compat
# ---------------------------------------------------------------------------

def test_epsilon_none_sequential_fallback():
    """With epsilon=None, the old sequential first-fit-wins must work."""
    patch = _make_sphere_trimesh(radius=2.0, n_lat=12, n_lon=16)
    result = _classify_curved_patch(patch)
    assert result["surface_type"] == "sphere"


# ---------------------------------------------------------------------------
# End-to-end cylinder regression through decompose_mesh_faces
# ---------------------------------------------------------------------------

def test_cylinder_still_recovers_via_decompose():
    """The cylinder recovery path through decompose_mesh_faces must
    remain regression-free (23-section cylinder -> 1 cylinder patch)."""
    nodes, indices = _cylinder_flat(radius=1.0, height=3.0, segments=23)
    result = decompose_mesh_faces(nodes, indices)
    cylinders = [p for p in result["curved_patches"]
                 if p["surface_type"] == "cylinder"]
    assert len(cylinders) == 1, (
        f"expected 1 cylinder, got {len(cylinders)}: "
        f"{[(p['surface_type'],) for p in result['curved_patches']]}")


def test_curved_competition_deterministic():
    """Repeated classification of a sphere must produce identical results."""
    patch = _make_sphere_trimesh(radius=2.0, n_lat=12, n_lon=16)
    a = _classify_curved_patch(patch, epsilon=0.1)
    b = _classify_curved_patch(patch, epsilon=0.1)
    assert a == b
