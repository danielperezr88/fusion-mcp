#!/usr/bin/env python3
"""End-to-end pipeline tests for curved-surface recovery (sphere/cone/torus).

Validates that ``decompose_mesh_faces`` correctly recovers sphere, torus, and
cone surfaces from WHOLE synthetic meshes — not just isolated patches.

The competition was already validated on isolated patches in
``test_curved_competition.py`` (14 tests).  These tests prove the routing fix:
small planar groups that are actually tessellated curved surfaces now reach
the full model competition via ``_recover_curved_from_small_groups`` instead
of being absorbed as planar quads and forgotten.

Mesh generators replicate the prototype's tessellation parameters:
  sphere_mesh(2.0, 12, 16), torus_mesh(2.0, 0.8, 24, 16),
  cone_mesh(1.0, 0.3, 3.0, 18, 4).
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
# Mesh generators (flat node/index lists for decompose_mesh_faces)
# ---------------------------------------------------------------------------

def sphere_mesh(radius=2.0, n_lat=12, n_lon=16):
    """UV-sphere as flat (nodes, indices) lists."""
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
    return nodes, faces


def torus_mesh(R=2.0, r=0.8, n_major=24, n_minor=16):
    """Ring torus as flat (nodes, indices) lists."""
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
    return nodes, faces


def cone_mesh(r1=1.0, r2=0.3, height=3.0, n_sections=18, n_rings=4):
    """Open cone frustum (side wall only) as flat (nodes, indices) lists."""
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
    return nodes, faces


def flat_bracket():
    """Planar-only box (6 faces, 2 tris each) — the negative-test fixture."""
    nodes = [
        0, 0, 0, 4, 0, 0, 4, 3, 0, 0, 3, 0,
        0, 0, 1, 4, 0, 1, 4, 3, 1, 0, 3, 1,
    ]
    indices = [
        0, 2, 1, 0, 3, 2,
        4, 5, 6, 4, 6, 7,
        0, 1, 5, 0, 5, 4,
        3, 7, 6, 3, 6, 2,
        0, 4, 3, 3, 4, 7,
        1, 2, 6, 1, 6, 5,
    ]
    return nodes, indices


# ---------------------------------------------------------------------------
# Sphere end-to-end
# ---------------------------------------------------------------------------

def test_sphere_recovered_via_pipeline():
    """A whole UV-sphere through decompose_mesh_faces must yield surface_type
    'sphere' (not freeform, not cylinder)."""
    nodes, indices = sphere_mesh(2.0, 12, 16)
    result = decompose_mesh_faces(nodes, indices)
    surface_types = [p["surface_type"] for p in result["curved_patches"]]
    assert "sphere" in surface_types, (
        f"expected 'sphere' in curved_patches surface types, "
        f"got {surface_types}")


# ---------------------------------------------------------------------------
# Torus end-to-end
# ---------------------------------------------------------------------------

def test_torus_recovered_via_pipeline():
    """A whole ring-torus through decompose_mesh_faces must yield
    surface_type 'torus'."""
    nodes, indices = torus_mesh(2.0, 0.8, 24, 16)
    result = decompose_mesh_faces(nodes, indices)
    surface_types = [p["surface_type"] for p in result["curved_patches"]]
    assert "torus" in surface_types, (
        f"expected 'torus' in curved_patches surface types, "
        f"got {surface_types}")


# ---------------------------------------------------------------------------
# Cone end-to-end
# ---------------------------------------------------------------------------

def test_cone_recovered_via_pipeline():
    """A whole open cone frustum through decompose_mesh_faces must yield
    surface_type 'cone'."""
    nodes, indices = cone_mesh(1.0, 0.3, 3.0, 18, 4)
    result = decompose_mesh_faces(nodes, indices)
    surface_types = [p["surface_type"] for p in result["curved_patches"]]
    assert "cone" in surface_types, (
        f"expected 'cone' in curved_patches surface types, "
        f"got {surface_types}")


# ---------------------------------------------------------------------------
# Flat-bracket negative test (no false curved surfaces)
# ---------------------------------------------------------------------------

def test_flat_bracket_zero_curved():
    """A planar-only box must produce 0 curved patches — no false
    sphere/cone/torus on flat geometry."""
    nodes, indices = flat_bracket()
    result = decompose_mesh_faces(nodes, indices)
    assert len(result["curved_patches"]) == 0, (
        f"expected 0 curved patches on planar-only mesh, "
        f"got {len(result['curved_patches'])}: "
        f"{[p['surface_type'] for p in result['curved_patches']]}")


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------

def test_pipeline_deterministic_sphere():
    """Decomposition of a sphere must produce identical results on repeat."""
    nodes, indices = sphere_mesh(2.0, 12, 16)
    a = decompose_mesh_faces(nodes, indices)
    b = decompose_mesh_faces(nodes, indices)
    assert a == b


def test_pipeline_deterministic_torus():
    """Decomposition of a torus must produce identical results on repeat."""
    nodes, indices = torus_mesh(2.0, 0.8, 24, 16)
    a = decompose_mesh_faces(nodes, indices)
    b = decompose_mesh_faces(nodes, indices)
    assert a == b
