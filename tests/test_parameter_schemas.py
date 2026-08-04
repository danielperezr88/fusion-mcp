#!/usr/bin/env python3
"""Headless tests for mcp_server.parameter_schemas (mesh-to-parametric plan,
Todo 4).

The schema library + deterministic matcher turn measured facts (bbox dims,
slice loop diameters, fit params) into stable, named parameters for a
reconstruction class.  Pure stdlib (json only), fully deterministic, and the
matcher NEVER raises on an unknown class - it falls back to `generic` with a
`note` key.
"""

import os
import sys

import pytest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from mcp_server.parameter_schemas import load_schemas, select_schema

_KINDS = {"length", "diameter", "radius", "angle", "count", "thickness"}
_SOURCES = {"bbox", "slice", "fit", "vision"}


# ---------------------------------------------------------------------------
# Happy path: bolt bound from measured facts
# ---------------------------------------------------------------------------

def test_bolt_binds_measured_facts():
    result = select_schema(
        "bolt", {"bbox_cm": [4, 4, 3], "slice_diameter_cm": 4.0})
    assert result["class"] == "bolt"
    assert isinstance(result["parameters"], list) and result["parameters"]
    assert isinstance(result["unmatched_roles"], list)
    by_name = {p["name"]: p for p in result["parameters"]}
    # stable named param, value from slice loop diameter
    head = by_name["bolt_head_diameter"]
    assert head["value"] == pytest.approx(4.0, abs=1e-9)
    assert head["confidence"] > 0.0
    # stable named param, value from bbox height (z extent)
    length = by_name["bolt_length"]
    assert length["value"] == pytest.approx(3.0, abs=1e-9)
    assert length["confidence"] > 0.0
    assert all(p["unit"] == "cm" for p in result["parameters"])


# ---------------------------------------------------------------------------
# Generic fallback (never raises)
# ---------------------------------------------------------------------------

def test_unknown_class_falls_back_to_generic():
    result = select_schema("nonexistent_class", {})
    assert result["class"] == "generic"
    assert "note" in result
    assert isinstance(result["parameters"], list)
    assert isinstance(result["unmatched_roles"], list)


# ---------------------------------------------------------------------------
# Unmatched roles (resolution-order rule 6: everything else)
# ---------------------------------------------------------------------------

def test_unresolved_roles_land_in_unmatched_roles():
    """Roles that cannot be resolved from ANY measured fact are reported in
    unmatched_roles.  Vision-sourced roles are the EXCEPTION: they become
    None placeholders in parameters (never fabricated numbers) and are NOT
    unmatched."""
    result = select_schema("bolt", {})
    # head_diameter (slice), head_thickness (bbox), length (bbox) -- no facts
    assert set(result["unmatched_roles"]) == {
        "head_diameter", "head_thickness", "length"}
    names = {p["name"] for p in result["parameters"]}
    assert not names & {"bolt_head_diameter", "bolt_head_thickness",
                        "bolt_length"}
    # vision-sourced roles stay as placeholders, never unmatched
    assert "thread_diameter" not in result["unmatched_roles"]
    assert "thread_pitch" not in result["unmatched_roles"]
    # supplying the facts clears the corresponding unmatched entries
    filled = select_schema(
        "bolt", {"slice_diameter_cm": 4.0, "bbox_cm": [4, 4, 3]})
    assert set(filled["unmatched_roles"]) == set()


# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------

def test_deterministic_repeatable():
    facts = {"bbox_cm": [4, 4, 3], "slice_diameter_cm": 4.0}
    a = select_schema("bolt", facts)
    b = select_schema("bolt", facts)
    assert a == b


# ---------------------------------------------------------------------------
# Value-source mapping
# ---------------------------------------------------------------------------

def test_bbox_dims_map_to_length_roles():
    result = select_schema("generic", {"bbox_cm": [4, 4, 3]})
    by_name = {p["name"]: p for p in result["parameters"]}
    assert by_name["width"]["value"] == pytest.approx(4.0, abs=1e-9)
    assert by_name["depth"]["value"] == pytest.approx(4.0, abs=1e-9)
    assert by_name["height"]["value"] == pytest.approx(3.0, abs=1e-9)
    assert all(p["confidence"] == pytest.approx(0.8, abs=1e-9)
               for p in result["parameters"])


def test_bbox_min_max_shape_accepted():
    """analyze_mesh_data's bounding_box_cm = [[min],[max]] is accepted too."""
    result = select_schema(
        "generic", {"bounding_box_cm": [[1.0, 1.0, 1.0], [5.0, 5.0, 4.0]]})
    by_name = {p["name"]: p for p in result["parameters"]}
    assert by_name["width"]["value"] == pytest.approx(4.0, abs=1e-9)
    assert by_name["height"]["value"] == pytest.approx(3.0, abs=1e-9)


def test_slice_diameter_maps_to_diameter_roles():
    result = select_schema("shaft", {"slice_diameter_cm": 2.0,
                                     "bbox_cm": [2.0, 2.0, 10.0]})
    by_name = {p["name"]: p for p in result["parameters"]}
    assert by_name["shaft_diameter"]["value"] == pytest.approx(2.0, abs=1e-9)
    assert by_name["shaft_diameter"]["confidence"] == pytest.approx(0.7, abs=1e-9)


def test_fit_params_map_to_radius_and_thickness():
    result = select_schema(
        "flange",
        {"bbox_cm": [5.0, 5.0, 1.0], "fit_radius_cm": 0.5,
         "fit_height_cm": 0.4})
    by_name = {p["name"]: p for p in result["parameters"]}
    # fit radius -> radius role
    assert by_name["flange_fillet_radius"]["value"] == pytest.approx(0.5, abs=1e-9)
    assert by_name["flange_fillet_radius"]["confidence"] == pytest.approx(0.7, abs=1e-9)
    # fit height -> thickness role
    assert by_name["flange_thickness"]["value"] == pytest.approx(1.0, abs=1e-9)


def test_direct_fact_key_overrides_source():
    result = select_schema(
        "gear",
        {"bbox_cm": [6.0, 6.0, 2.0], "tooth_count": 24})
    by_name = {p["name"]: p for p in result["parameters"]}
    assert by_name["gear_tooth_count"]["value"] == pytest.approx(24.0, abs=1e-9)
    assert by_name["gear_tooth_count"]["confidence"] == pytest.approx(1.0, abs=1e-9)


def test_vision_roles_are_placeholders_not_fabricated():
    result = select_schema("gear", {"bbox_cm": [6.0, 6.0, 2.0]})
    by_name = {p["name"]: p for p in result["parameters"]}
    # vision-sourced: placeholder value, low confidence, still present
    assert by_name["gear_bore_diameter"]["value"] is None
    assert by_name["gear_bore_diameter"]["confidence"] == pytest.approx(0.3, abs=1e-9)


# ---------------------------------------------------------------------------
# Units
# ---------------------------------------------------------------------------

def test_units_mm_scales_values():
    result = select_schema("generic", {"bbox_cm": [4, 4, 3]}, units="mm")
    by_name = {p["name"]: p for p in result["parameters"]}
    assert by_name["width"]["value"] == pytest.approx(40.0, abs=1e-9)
    assert by_name["width"]["unit"] == "mm"


def test_bad_units_raise_value_error():
    with pytest.raises(ValueError):
        select_schema("generic", {"bbox_cm": [4, 4, 3]}, units="furlong")


# ---------------------------------------------------------------------------
# Schema file integrity
# ---------------------------------------------------------------------------

def test_schema_file_has_all_required_classes():
    schemas = load_schemas()
    required = {
        "knob", "bracket", "housing", "flange", "pulley", "gear",
        "bearing_holder", "handle", "latch", "mount", "cover", "plate",
        "shaft", "bushing", "spacer", "washer", "bolt", "nut", "ring",
        "link", "generic",
    }
    assert required.issubset(set(schemas))
    assert len(schemas) >= 21
    for cls in required:
        entry = schemas[cls]
        assert set(("class", "description", "strategy_hint", "parameters")).issubset(
            set(entry))
        assert entry["class"] == cls
        assert isinstance(entry["parameters"], list) and entry["parameters"]
        for p in entry["parameters"]:
            assert set(("role", "name", "kind", "source")).issubset(set(p))
            assert p["kind"] in _KINDS
            assert p["source"] in _SOURCES
            # stable descriptive names: prefixed by class, except generic's
            # minimal width/depth/height which stay unprefixed
            assert p["name"] in (f"{cls}_{p['role']}", p["role"])
