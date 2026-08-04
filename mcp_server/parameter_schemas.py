"""Parameter schema library + deterministic matcher for the Fusion 360 MCP
server (mesh-to-parametric plan, Todo 4).

`load_schemas()` reads ``parameter_schemas.json`` (21 classes: 20 mechanical
parts + the ``generic`` fallback) and `select_schema()` binds measured facts
to the chosen class's roles, producing stable, named parameters:

    select_schema(class_name, measured_facts, units="cm")
        -> {"class": str, "description": str, "strategy_hint": str,
            "parameters": [{"name", "value", "unit", "confidence"}, ...],
            "unmatched_roles": [str], "note"?: str}

Role resolution order (per role in the schema):
  1. a measured-fact key named exactly like the role (confidence 1.0),
  2. derived from the bounding box - ``bbox_cm`` (dims [w,d,h] OR [[min],
     [max]]) or ``bounding_box_cm`` (the analyze_mesh report key) - mapped to
     width/depth/height by axis convention (confidence 0.8),
  3. the slice loop diameter - ``slice_diameter_cm`` -> diameter / radius
     (confidence 0.7),
  4. fit parameters - ``fit_radius_cm`` / ``fit_height_cm`` /
     ``fit_diameter_cm`` -> radius / thickness / diameter (confidence 0.7),
  5. vision-sourced roles become a ``None`` placeholder with confidence 0.3
     (the model fills them in later; numbers are NEVER fabricated),
  6. anything else lands in ``unmatched_roles``.

An unknown class NEVER raises: it falls back to ``generic`` and adds a
``note`` key.  The only ValueError is an unsupported ``units`` value.

Design constraints (same as mesh_analysis / mesh_slicer):
  * stdlib ONLY (json, os) - no numpy.
  * fully DETERMINISTIC - no random module; identical input always yields an
    identical result.
  * robust - empty / malformed measured facts produce a valid dict.
"""

from __future__ import annotations

import json
import os
from typing import Dict, List, Optional, Sequence

_SCHEMA_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "parameter_schemas.json")

_UNIT_FACTORS = {
    "cm": 1.0,
    "mm": 10.0,
    "in": 1.0 / 2.54,
}

_BBOX_ROLE_AXIS = {"width": 0, "depth": 1, "height": 2}

_cache: Optional[Dict] = None


# --------------------------------------------------------------------------
# units
# --------------------------------------------------------------------------

def _unit_factor(units: str) -> float:
    u = str(units or "cm").strip().lower()
    if u not in _UNIT_FACTORS:
        raise ValueError(
            f"Unsupported units '{units}'. Use 'mm', 'cm', or 'in'.")
    return _UNIT_FACTORS[u]


# --------------------------------------------------------------------------
# schema loading
# --------------------------------------------------------------------------

def load_schemas() -> Dict:
    """Load ``parameter_schemas.json`` as a {class: schema} dict (cached).

    Raises FileNotFoundError with a clear message when the JSON is missing.
    """
    global _cache
    if _cache is not None:
        return _cache
    if not os.path.isfile(_SCHEMA_PATH):
        raise FileNotFoundError(
            f"{_SCHEMA_PATH} could not be found. Keep the mcp_server package "
            "intact so select_parameter_schema can resolve class schemas.")
    with open(_SCHEMA_PATH, "r", encoding="utf-8") as fh:
        entries = json.load(fh)
    schemas: Dict = {}
    for entry in entries:
        cls = str(entry.get("class", "")).strip().lower()
        if cls:
            schemas[cls] = entry
    _cache = schemas
    return schemas


# --------------------------------------------------------------------------
# measured-fact extraction helpers
# --------------------------------------------------------------------------

def _as_number(value) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _bbox_dims(measured_facts: Dict) -> Optional[List[float]]:
    """(width, depth, height) from ``bbox_cm`` or ``bounding_box_cm``.

    Accepts dims ``[w, d, h]``, nested min/max ``[[min],[max]]`` (the
    analyze_mesh report shape), and flat min/max ``[x0,y0,z0,x1,y1,z1]``.
    Returns None when absent or malformed.
    """
    raw = measured_facts.get("bbox_cm")
    if raw is None:
        raw = measured_facts.get("bounding_box_cm")
    if raw is None:
        return None
    try:
        first = raw[0]
    except (TypeError, IndexError):
        return None
    if isinstance(first, (list, tuple)):
        # nested [[minx,miny,minz],[maxx,maxy,maxz]]
        try:
            lo = [float(v) for v in raw[0]]
            hi = [float(v) for v in raw[1]]
        except (TypeError, ValueError):
            return None
        if len(lo) != 3 or len(hi) != 3:
            return None
        return [hi[k] - lo[k] for k in range(3)]
    try:
        vals = [float(v) for v in raw]
    except (TypeError, ValueError):
        return None
    if len(vals) == 3:
        return vals
    if len(vals) == 6:
        # flat min/max [x0,y0,z0,x1,y1,z1]
        return [vals[3] - vals[0], vals[4] - vals[1], vals[5] - vals[2]]
    return None


def _resolve_bbox(role: str, kind: str, param: Dict,
                  dims: Sequence[float]) -> Optional[float]:
    axis = param.get("axis")
    if axis is None:
        axis = _BBOX_ROLE_AXIS.get(role, 2)  # default: z (height) axis
    try:
        idx = int(axis)
    except (TypeError, ValueError):
        idx = 2
    if 0 <= idx < 3:
        return float(dims[idx])
    return None


def _resolve_from_facts(role: str, kind: str, source: str, param: Dict,
                        measured_facts: Dict,
                        dims: Optional[List[float]]) -> Optional[float]:
    """Resolve a role's numeric value from the measured facts, or None."""
    # 1. named fact key directly
    direct = _as_number(measured_facts.get(role))
    if direct is not None:
        return direct
    # 2-4. source-specific derivation
    if source == "bbox" and dims is not None:
        return _resolve_bbox(role, kind, param, dims)
    if source == "slice":
        slice_d = _as_number(measured_facts.get("slice_diameter_cm"))
        if slice_d is not None:
            if kind == "radius":
                return slice_d / 2.0
            return slice_d  # diameter (and length fallthrough)
    if source == "fit":
        if kind == "radius":
            return _as_number(measured_facts.get("fit_radius_cm"))
        if kind == "diameter":
            fit_d = _as_number(measured_facts.get("fit_diameter_cm"))
            if fit_d is not None:
                return fit_d
            fit_r = _as_number(measured_facts.get("fit_radius_cm"))
            if fit_r is not None:
                return fit_r * 2.0
        if kind in ("thickness", "length"):
            fit_h = _as_number(measured_facts.get("fit_height_cm"))
            if fit_h is not None:
                return fit_h
            return _as_number(measured_facts.get("fit_thickness_cm"))
    return None


# --------------------------------------------------------------------------
# public API
# --------------------------------------------------------------------------

def select_schema(class_name: str, measured_facts: Optional[Dict] = None,
                  units: str = "cm") -> Dict:
    """Select a parameter schema and bind measured facts to its roles.

    Never raises for an unknown class: falls back to ``generic`` with a
    ``note`` key.  Raises ValueError only for an unsupported ``units`` value.
    """
    factor = _unit_factor(units)
    unit = str(units or "cm").strip().lower()
    facts = dict(measured_facts or {})
    schemas = load_schemas()

    cls = str(class_name or "generic").strip().lower()
    entry = schemas.get(cls)
    note = None
    if entry is None:
        note = (f"Unknown class '{class_name}'; fell back to 'generic'.")
        cls = "generic"
        entry = schemas[cls]

    dims = _bbox_dims(facts)
    parameters: List[Dict] = []
    unmatched: List[str] = []

    for param in entry.get("parameters", []):
        role = str(param.get("role", ""))
        kind = str(param.get("kind", "length"))
        source = str(param.get("source", "bbox"))
        name = str(param.get("name", f"{cls}_{role}"))

        value = _resolve_from_facts(role, kind, source, param, facts, dims)
        if value is None:
            if source == "vision":
                # placeholder for the model to fill in later - never fabricate
                parameters.append({"name": name, "value": None, "unit": unit,
                                   "confidence": 0.3})
            else:
                unmatched.append(role)
            continue

        # direct fact -> 1.0; bbox-derived -> 0.8; slice/fit-derived -> 0.7
        direct = _as_number(facts.get(role))
        if direct is not None:
            confidence = 1.0
        elif source == "bbox":
            confidence = 0.8
        else:
            confidence = 0.7
        parameters.append({
            "name": name,
            "value": round(value * factor, 6),
            "unit": unit,
            "confidence": confidence,
        })

    result: Dict = {
        "class": cls,
        "description": entry.get("description", ""),
        "strategy_hint": entry.get("strategy_hint", "prismatic"),
        "parameters": parameters,
        "unmatched_roles": unmatched,
    }
    if note is not None:
        result["note"] = note
    return result
