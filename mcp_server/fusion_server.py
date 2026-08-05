"""
Fusion 360 MCP Server

An MCP (Model Context Protocol) server that exposes Fusion 360 CAD operations
as tools for AI assistants. Communicates with the FusionMCP add-in running
inside Fusion 360 via HTTP on localhost:7432.
"""

import requests
import json
import base64
import importlib.util
import os
import math
import re
from decimal import Decimal
from typing import Optional
import networkx as nx
from mcp.server.fastmcp import FastMCP, Image

FUSION_URL = "http://127.0.0.1:7432"
mcp = FastMCP("Fusion 360")


def _load_scad_translator():
    """Import mcp_server.scad_translator from the repo, wherever it lives.

    The MCP server process is the one with the openscad-evaluator packages, so
    ``resolve_scad()`` runs HERE (process split: the add-in has adsk but no
    openscad packages).  ``import mcp_server.scad_translator`` works when the
    repo root is on sys.path; when this file is launched directly (e.g. by an
    MCP client) the script dir is on sys.path instead, so fall back to loading
    the module by file location.
    """
    try:
        from mcp_server import scad_translator
        return scad_translator
    except ImportError:
        pass

    here = os.path.dirname(os.path.abspath(__file__))
    candidates = (
        os.path.join(here, "scad_translator.py"),
        os.path.join(here, os.pardir, "mcp_server", "scad_translator.py"),
    )
    for path in candidates:
        if not os.path.isfile(path):
            continue
        spec = importlib.util.spec_from_file_location(
            "fusionmcp_scad_translator", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    raise FileNotFoundError(
        "mcp_server/scad_translator.py could not be found. Keep the fusion-mcp "
        "repository importable so create_from_scad can resolve SCAD code.")


def _load_mesh_analysis():
    """Import mcp_server.mesh_analysis from the repo, wherever it lives.

    Same fallback-by-file-location strategy as _load_scad_translator: works
    both when the repo root is on sys.path and when fusion_server.py is
    launched directly by an MCP client.
    """
    try:
        from mcp_server import mesh_analysis
        return mesh_analysis
    except ImportError:
        pass
    here = os.path.dirname(os.path.abspath(__file__))
    candidates = (
        os.path.join(here, "mesh_analysis.py"),
        os.path.join(here, os.pardir, "mcp_server", "mesh_analysis.py"),
    )
    for path in candidates:
        if not os.path.isfile(path):
            continue
        spec = importlib.util.spec_from_file_location(
            "fusionmcp_mesh_analysis", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    raise FileNotFoundError(
        "mcp_server/mesh_analysis.py could not be found. Keep the fusion-mcp "
        "repository importable so analyze_mesh can run mesh analysis.")


def _load_mesh_graph():
    """Import mcp_server.mesh_graph from the repo, wherever it lives.

    Same fallback-by-file-location strategy as _load_mesh_analysis: works
    both when the repo root is on sys.path and when fusion_server.py is
    launched directly by an MCP client.
    """
    try:
        from mcp_server import mesh_graph
        return mesh_graph
    except ImportError:
        pass
    here = os.path.dirname(os.path.abspath(__file__))
    candidates = (
        os.path.join(here, "mesh_graph.py"),
        os.path.join(here, os.pardir, "mcp_server", "mesh_graph.py"),
    )
    for path in candidates:
        if not os.path.isfile(path):
            continue
        spec = importlib.util.spec_from_file_location(
            "fusionmcp_mesh_graph", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    raise FileNotFoundError(
        "mcp_server/mesh_graph.py could not be found. Keep the fusion-mcp "
        "repository importable so structure_graph can run graph analysis.")


def _load_mesh_slicer():
    """Import mcp_server.mesh_slicer from the repo, wherever it lives.

    Same fallback-by-file-location strategy as _load_mesh_analysis: works
    both when the repo root is on sys.path and when fusion_server.py is
    launched directly by an MCP client.
    """
    try:
        from mcp_server import mesh_slicer
        return mesh_slicer
    except ImportError:
        pass
    here = os.path.dirname(os.path.abspath(__file__))
    candidates = (
        os.path.join(here, "mesh_slicer.py"),
        os.path.join(here, os.pardir, "mcp_server", "mesh_slicer.py"),
    )
    for path in candidates:
        if not os.path.isfile(path):
            continue
        spec = importlib.util.spec_from_file_location(
            "fusionmcp_mesh_slicer", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    raise FileNotFoundError(
        "mcp_server/mesh_slicer.py could not be found. Keep the fusion-mcp "
        "repository importable so slice_mesh can run plane intersection.")


def _load_mesh_csg():
    """Import mcp_server.mesh_csg from the repo, wherever it lives.

    Same fallback-by-file-location strategy as _load_mesh_slicer: works both
    when the repo root is on sys.path and when fusion_server.py is launched
    directly by an MCP client.
    """
    try:
        from mcp_server import mesh_csg
        return mesh_csg
    except ImportError:
        pass
    here = os.path.dirname(os.path.abspath(__file__))
    candidates = (
        os.path.join(here, "mesh_csg.py"),
        os.path.join(here, os.pardir, "mcp_server", "mesh_csg.py"),
    )
    for path in candidates:
        if not os.path.isfile(path):
            continue
        spec = importlib.util.spec_from_file_location(
            "fusionmcp_mesh_csg", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    raise FileNotFoundError(
        "mcp_server/mesh_csg.py could not be found. Keep the fusion-mcp "
        "repository importable so reconstruct_mesh can build CSG trees.")


def _load_parameter_schemas():
    """Import mcp_server.parameter_schemas from the repo, wherever it lives.

    Same fallback-by-file-location strategy as _load_mesh_analysis: works
    both when the repo root is on sys.path and when fusion_server.py is
    launched directly by an MCP client.
    """
    try:
        from mcp_server import parameter_schemas
        return parameter_schemas
    except ImportError:
        pass
    here = os.path.dirname(os.path.abspath(__file__))
    candidates = (
        os.path.join(here, "parameter_schemas.py"),
        os.path.join(here, os.pardir, "mcp_server", "parameter_schemas.py"),
    )
    for path in candidates:
        if not os.path.isfile(path):
            continue
        spec = importlib.util.spec_from_file_location(
            "fusionmcp_parameter_schemas", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    raise FileNotFoundError(
        "mcp_server/parameter_schemas.py could not be found. Keep the "
        "fusion-mcp repository importable so select_parameter_schema can "
        "resolve class schemas.")


def _load_workflow_guide():
    """Import mcp_server.workflow_guide from the repo, wherever it lives.

    Same fallback-by-file-location strategy as _load_parameter_schemas:
    works both when the repo root is on sys.path and when fusion_server.py
    is launched directly by an MCP client.
    """
    try:
        from mcp_server import workflow_guide
        return workflow_guide
    except ImportError:
        pass
    here = os.path.dirname(os.path.abspath(__file__))
    candidates = (
        os.path.join(here, "workflow_guide.py"),
        os.path.join(here, os.pardir, "mcp_server", "workflow_guide.py"),
    )
    for path in candidates:
        if not os.path.isfile(path):
            continue
        spec = importlib.util.spec_from_file_location(
            "fusionmcp_workflow_guide", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module
    raise FileNotFoundError(
        "mcp_server/workflow_guide.py could not be found. Keep the "
        "fusion-mcp repository importable so get_workflow_guide can serve "
        "the workflow guide.")


def _get_bosl2_path():
    """Resolve the bundled BOSL2 path, or None when the bundle is unavailable."""
    try:
        from mcp_server.bundle import get_bosl2_path
        return get_bosl2_path()
    except ImportError:
        pass
    try:
        from bundle import get_bosl2_path
        return get_bosl2_path()
    except ImportError:
        return None


def _get_openscad_path():
    """Resolve the bundled OpenSCAD binary path, or None when unavailable."""
    try:
        from mcp_server.bundle import get_openscad_path
        return get_openscad_path()
    except ImportError:
        pass
    try:
        from bundle import get_openscad_path
        return get_openscad_path()
    except ImportError:
        return None


def _call(command: str, params: dict = None, timeout: int = 30) -> str:
    if params is None:
        params = {}
    try:
        r = requests.post(f"{FUSION_URL}/command",
                          json={"command": command, "params": params}, timeout=timeout)
        data = r.json()
        if "error" in data:
            return f"Error: {data['error']}"
        return json.dumps(data, indent=2)
    except requests.exceptions.ConnectionError:
        return "Cannot reach Fusion 360. Make sure Fusion is open and the FusionMCP add-in is running."
    except Exception as e:
        return f"Unexpected error: {e}"


# ---- Status & Info ----

@mcp.tool()
def fusion_status() -> str:
    """Check if Fusion 360 is running and the MCP bridge is active."""
    try:
        r = requests.get(f"{FUSION_URL}/ping", timeout=5)
        return f"Connected: {r.json().get('message', 'OK')}"
    except:
        return "Fusion 360 is not reachable. Open Fusion and run the FusionMCP add-in."

@mcp.tool()
def get_design_info() -> str:
    """Get full info about the current design: components, sketches, bodies, parameters, construction planes, joints."""
    return _call("get_info")

@mcp.tool()
def get_bodies_info() -> str:
    """List all bodies with index, name, face count, edge count, and bounding box size."""
    return _call("get_bodies_info")

@mcp.tool()
def get_face_info(body: str = "0") -> str:
    """
    List all faces of a body with their index, area, surface type, and normal direction.
    Use this to find face indices before calling fillet, chamfer, shell, press_pull, etc.

    Args:
        body: Body name or index number.
    """
    return _call("get_face_info", {"body": body})

@mcp.tool()
def get_edge_info(body: str = "0") -> str:
    """
    List all edges of a body with their index, length, and edge type (line/arc/circle/spline).
    Use this to find edge indices before calling fillet_edges, chamfer_edges.

    Args:
        body: Body name or index number.
    """
    return _call("get_edge_info", {"body": body})

@mcp.tool()
def get_sketch_info(sketch: str = "0") -> str:
    """
    Get detailed info about a sketch: profiles, curves, constraints, dimensions, points.
    Use this before extrude/revolve to know which profile_index to use.

    Args:
        sketch: Sketch name or index number.
    """
    return _call("get_sketch_info", {"sketch": sketch})

@mcp.tool()
def get_timeline_info() -> str:
    """Get the feature timeline: ordered list of all features with names, types, and status."""
    return _call("get_timeline_info")

@mcp.tool()
def measure_body(body: str = "0") -> str:
    """
    Measure a body: bounding box dimensions in cm, volume, face/edge/vertex counts.

    Args:
        body: Body name or index number.
    """
    return _call("measure_body", {"body": body})

@mcp.tool()
def measure_between(entity1: str = "", entity2: str = "") -> str:
    """
    Measure distance between two entities (bodies, faces, edges, points).
    Entities are specified as 'body:0', 'body:0:face:1', 'body:0:edge:2', etc.

    Args:
        entity1: First entity specifier.
        entity2: Second entity specifier.
    """
    return _call("measure_between", {"entity1": entity1, "entity2": entity2})

@mcp.tool()
def measure_angle(entity1: str = "", entity2: str = "") -> str:
    """
    Measure the angle between two faces or edges.
    Entities are specified as 'body:0:face:1', 'body:0:edge:2', etc.

    Args:
        entity1: First entity specifier (face or edge).
        entity2: Second entity specifier (face or edge).
    """
    return _call("measure_angle", {"entity1": entity1, "entity2": entity2})

@mcp.tool()
def get_physical_properties(body: str = "0") -> str:
    """
    Get full physical properties of a body: volume, surface area, mass, density, center of mass, moments of inertia, and principal axes.

    Args:
        body: Body name or index number.
    """
    return _call("get_physical_properties", {"body": body})

@mcp.tool()
def get_oriented_bounding_box(body: str = "0", length_axis: str = "X", width_axis: str = "Y") -> str:
    """
    Get the oriented bounding box of a body along the specified axes.

    Args:
        body:        Body name or index number.
        length_axis: Axis for the box length: X, Y, or Z.
        width_axis:  Axis for the box width: X, Y, or Z.
    """
    return _call("get_oriented_bounding_box", {"body": body, "length_axis": length_axis, "width_axis": width_axis})

@mcp.tool()
def inspect_body(body: str = "0", detail: str = "summary", max_items: int = 100) -> str:
    """
    Get a comprehensive geometry report for a body: bounding box, physical properties, faces, edges, and vertices.

    Args:
        body:      Body name or index number.
        detail:    'summary' (counts + bounding box + physical properties) or 'full' (face/edge/vertex details).
        max_items: Maximum number of faces/edges/vertices to include when detail='full'.
    """
    return _call("inspect_body", {"body": body, "detail": detail, "max_items": max_items})


# ---- Execute Script ----

@mcp.tool()
def execute_script(code: str) -> str:
    """
    Execute arbitrary Python code directly inside Fusion 360.
    This gives you full access to the entire Fusion 360 API (adsk.core, adsk.fusion, adsk.cam).

    Pre-injected variables available in your code:
      - app: adsk.core.Application
      - ui: adsk.core.UserInterface
      - design: adsk.fusion.Design (active product)
      - root: root component
      - result: dict - set result['output'] to return data to the caller

    Example:
      code = '''
      bodies = root.bRepBodies
      result['output'] = f"Found {bodies.count} bodies"
      '''

    WARNING: This has full system access. Use responsibly.

    Args:
        code: Python code to execute inside Fusion 360.
    """
    return _call("execute_script", {"code": code})


# ---- Document Management ----

@mcp.tool()
def create_new_document(name: str = "Untitled") -> str:
    """
    Create a brand new empty Fusion design document.

    Args:
        name: Name for the new document.
    """
    return _call("create_new_document", {"name": name})

@mcp.tool()
def clear_design() -> str:
    """Delete all bodies, sketches, construction geometry, and features from the current design. Use with caution."""
    return _call("clear_design")


# ---- Sketch Tools ----

@mcp.tool()
def create_sketch(plane: str = "XY", name: str = "", component: str = "") -> str:
    """
    Create a new sketch on a plane.

    Args:
        plane: XY (floor), XZ (front), YZ (side), or a named construction plane.
        name:  Optional name for the sketch.
        component: Optional component name to create sketch in (default: root).
    """
    return _call("create_sketch", {"plane": plane, "name": name, "component": component})

@mcp.tool()
def create_sketch_on_face(body: str = "0", face_index: int = 0, name: str = "") -> str:
    """
    Create a sketch directly on a body face.

    Args:
        body:        Body name or index.
        face_index:  Face to sketch on (use get_face_info to find index).
        name:        Optional name for the sketch.
    """
    return _call("create_sketch_on_face", {"body": body, "face_index": face_index, "name": name})

@mcp.tool()
def finish_sketch(sketch: str = "") -> str:
    """
    Finalize/deactivate a sketch so features can be applied to it.
    If no sketch specified, finishes the most recent sketch.

    Args:
        sketch: Sketch name or index (default: last sketch).
    """
    return _call("finish_sketch", {"sketch": sketch})

@mcp.tool()
def delete_sketch(sketch: str = "0") -> str:
    """
    Delete a sketch by name or index.

    Args:
        sketch: Sketch name or index number.
    """
    return _call("delete_sketch", {"sketch": sketch})

@mcp.tool()
def draw_rectangle(x1: float = 0, y1: float = 0, x2: float = 10, y2: float = 10,
                   sketch: str = "") -> str:
    """
    Draw a rectangle in a sketch (two-point corner rectangle). All values in cm.

    Args:
        x1, y1: First corner (cm).
        x2, y2: Opposite corner (cm).
        sketch: Sketch name/index. Default: last created sketch.
    """
    return _call("draw_rectangle", {"x1": x1, "y1": y1, "x2": x2, "y2": y2, "sketch": sketch})

@mcp.tool()
def draw_center_rectangle(cx: float = 0, cy: float = 0,
                          width: float = 10, height: float = 10,
                          sketch: str = "") -> str:
    """
    Draw a rectangle centered at a point. All values in cm.

    Args:
        cx, cy: Center point (cm).
        width:  Total width (cm).
        height: Total height (cm).
        sketch: Sketch name/index. Default: last created sketch.
    """
    return _call("draw_center_rectangle", {"cx": cx, "cy": cy,
                 "width": width, "height": height, "sketch": sketch})

@mcp.tool()
def draw_circle(cx: float = 0, cy: float = 0, radius: float = 5,
                sketch: str = "") -> str:
    """
    Draw a circle in a sketch.

    Args:
        cx, cy: Centre point (cm).
        radius: Radius (cm).
        sketch: Sketch name/index. Default: last created sketch.
    """
    return _call("draw_circle", {"cx": cx, "cy": cy, "radius": radius, "sketch": sketch})

@mcp.tool()
def draw_line(x1: float = 0, y1: float = 0, x2: float = 10, y2: float = 0,
              sketch: str = "") -> str:
    """
    Draw a straight line in a sketch.

    Args:
        x1, y1: Start point (cm).
        x2, y2: End point (cm).
        sketch: Sketch name/index. Default: last created sketch.
    """
    return _call("draw_line", {"x1": x1, "y1": y1, "x2": x2, "y2": y2, "sketch": sketch})

@mcp.tool()
def draw_arc(cx: float = 0, cy: float = 0, radius: float = 5,
             start_angle: float = 0, sweep_angle: float = 90,
             sketch: str = "") -> str:
    """
    Draw an arc in a sketch.

    Args:
        cx, cy:       Centre point (cm).
        radius:       Radius (cm).
        start_angle:  Starting angle in degrees (0 = right).
        sweep_angle:  How far to sweep in degrees.
        sketch: Sketch name/index. Default: last created sketch.
    """
    return _call("draw_arc", {"cx": cx, "cy": cy, "radius": radius,
                               "start_angle": start_angle, "sweep_angle": sweep_angle,
                               "sketch": sketch})

@mcp.tool()
def draw_polygon(cx: float = 0, cy: float = 0, radius: float = 5, sides: int = 6,
                 sketch: str = "") -> str:
    """
    Draw a regular polygon in a sketch.

    Args:
        cx, cy:  Centre point (cm).
        radius:  Circumradius (cm).
        sides:   Number of sides (3=triangle, 4=square, 6=hexagon...).
        sketch: Sketch name/index. Default: last created sketch.
    """
    return _call("draw_polygon", {"cx": cx, "cy": cy, "radius": radius,
                                   "sides": sides, "sketch": sketch})

@mcp.tool()
def draw_ellipse(cx: float = 0, cy: float = 0, rx: float = 5, ry: float = 3,
                 sketch: str = "") -> str:
    """
    Draw an ellipse in a sketch.

    Args:
        cx, cy: Centre point (cm).
        rx:     Half-width along X (cm).
        ry:     Half-height along Y (cm).
        sketch: Sketch name/index. Default: last created sketch.
    """
    return _call("draw_ellipse", {"cx": cx, "cy": cy, "rx": rx, "ry": ry, "sketch": sketch})

@mcp.tool()
def draw_spline(points: list = [[0, 0], [5, 5], [10, 0]], sketch: str = "") -> str:
    """
    Draw a smooth fitted spline through control points.

    Args:
        points: List of [x, y] coordinate pairs in cm.
        sketch: Sketch name/index. Default: last created sketch.
    """
    return _call("draw_spline", {"points": points, "sketch": sketch})

@mcp.tool()
def draw_slot(x1: float = 0, y1: float = 0, x2: float = 10, y2: float = 0,
              width: float = 3, sketch: str = "") -> str:
    """
    Draw a slot shape (rounded-end rectangle / oblong) between two center points.

    Args:
        x1, y1: First center point (cm).
        x2, y2: Second center point (cm).
        width:  Total slot width (cm) - the rounded end diameter.
        sketch: Sketch name/index. Default: last created sketch.
    """
    return _call("draw_slot", {"x1": x1, "y1": y1, "x2": x2, "y2": y2,
                                "width": width, "sketch": sketch})

@mcp.tool()
def draw_text(text: str = "Hello", x: float = 0, y: float = 0,
              height: float = 1.0, sketch: str = "") -> str:
    """
    Add text to a sketch. Creates sketch text geometry.

    Args:
        text:   The text string to draw.
        x, y:   Position (cm).
        height: Text height (cm).
        sketch: Sketch name/index. Default: last created sketch.
    """
    return _call("draw_text", {"text": text, "x": x, "y": y, "height": height, "sketch": sketch})

@mcp.tool()
def add_sketch_fillet(line1_index: int = 0, line2_index: int = 1, radius: float = 1.0,
                      sketch: str = "") -> str:
    """
    Add a rounded fillet between two intersecting lines in a sketch.

    Args:
        line1_index: Index of first line.
        line2_index: Index of second line.
        radius:      Fillet radius (cm).
        sketch: Sketch name/index. Default: last created sketch.
    """
    return _call("add_sketch_fillet", {"line1_index": line1_index,
                                        "line2_index": line2_index, "radius": radius,
                                        "sketch": sketch})

@mcp.tool()
def offset_sketch(distance: float = 1.0, dx: float = 1, dy: float = 1,
                  sketch: str = "") -> str:
    """
    Offset all curves in a sketch outward by a distance.

    Args:
        distance: Offset distance (cm).
        dx, dy:   Direction hint for which side to offset toward.
        sketch: Sketch name/index. Default: last created sketch.
    """
    return _call("offset_sketch", {"distance": distance, "dx": dx, "dy": dy, "sketch": sketch})

@mcp.tool()
def mirror_sketch(axis_line_index: int = 0, sketch: str = "") -> str:
    """
    Mirror all sketch curves about one of the sketch lines.

    Args:
        axis_line_index: Index of the line to use as the mirror axis.
        sketch: Sketch name/index. Default: last created sketch.
    """
    return _call("mirror_sketch", {"axis_line_index": axis_line_index, "sketch": sketch})

@mcp.tool()
def rectangular_pattern_sketch(x_count: int = 2, y_count: int = 2,
                                x_spacing: float = 5, y_spacing: float = 5,
                                sketch: str = "") -> str:
    """
    Create a rectangular grid pattern of the sketch geometry.

    Args:
        x_count, y_count:     Number of copies in each direction.
        x_spacing, y_spacing: Spacing between copies (cm).
        sketch: Sketch name/index. Default: last created sketch.
    """
    return _call("rectangular_pattern_sketch", {
        "x_count": x_count, "y_count": y_count,
        "x_spacing": x_spacing, "y_spacing": y_spacing, "sketch": sketch})


# ---- Sketch Constraints ----

@mcp.tool()
def add_constraint(constraint_type: str = "coincident",
                   sketch: str = "", entity1_index: int = 0, entity2_index: int = -1,
                   entity1_type: str = "point", entity2_type: str = "point") -> str:
    """
    Add a geometric constraint to sketch entities.

    Args:
        constraint_type: One of: coincident, tangent, perpendicular, parallel,
                         horizontal, vertical, concentric, equal, midpoint, fix, collinear, smooth.
        sketch: Sketch name or index (default: last sketch).
        entity1_index: Index of first entity.
        entity2_index: Index of second entity (-1 if not needed, e.g. for horizontal/vertical/fix).
        entity1_type: Type of entity1: 'point', 'line', 'curve', 'circle'.
        entity2_type: Type of entity2: 'point', 'line', 'curve', 'circle'.
    """
    return _call("add_constraint", {
        "constraint_type": constraint_type, "sketch": sketch,
        "entity1_index": entity1_index, "entity2_index": entity2_index,
        "entity1_type": entity1_type, "entity2_type": entity2_type,
    })


@mcp.tool()
def add_sketch_dimension(dimension_type: str = "distance",
                         sketch: str = "", entity1_index: int = 0, entity2_index: int = -1,
                         entity1_type: str = "line", entity2_type: str = "",
                         value: float = 5.0) -> str:
    """
    Add a dimensional constraint to sketch entities.

    Args:
        dimension_type: One of: distance (between two entities or line length),
                        angle (between two lines), diameter (of circle/arc),
                        radius (of circle/arc).
        sketch: Sketch name or index (default: last sketch).
        entity1_index: Index of first entity.
        entity2_index: Index of second entity (-1 if measuring single entity).
        entity1_type: 'line', 'circle', 'arc', 'point'.
        entity2_type: 'line', 'circle', 'arc', 'point' (empty if single entity).
        value: The dimension value in cm (distances) or degrees (angles).
    """
    return _call("add_sketch_dimension", {
        "dimension_type": dimension_type, "sketch": sketch,
        "entity1_index": entity1_index, "entity2_index": entity2_index,
        "entity1_type": entity1_type, "entity2_type": entity2_type,
        "value": value,
    })


# ---- Feature Tools ----

@mcp.tool()
def extrude_sketch(distance: float = 1.0, operation: str = "new_body",
                   profile_index: int = 0, symmetric: bool = False,
                   sketch: str = "") -> str:
    """
    Extrude a closed sketch profile into a 3D body.

    Args:
        distance:      Extrusion depth in centimetres.
        operation:     new_body, join, cut, intersect, or new_component.
        profile_index: Which profile to use if sketch has multiple closed regions.
        symmetric:     If True, extrudes equally in both directions.
        sketch: Sketch name/index. Default: last created sketch.
    """
    return _call("extrude", {"distance": distance, "operation": operation,
                              "profile_index": profile_index, "symmetric": symmetric,
                              "sketch": sketch})

@mcp.tool()
def revolve_sketch(angle: float = 360, profile_index: int = 0,
                   axis_index: int = 0, operation: str = "new_body",
                   sketch: str = "") -> str:
    """
    Revolve a profile around an axis line drawn in the same sketch.

    Args:
        angle:         Degrees to revolve (360 = full revolution).
        profile_index: Which closed profile to revolve.
        axis_index:    Which sketch line is the rotation axis.
        operation:     new_body, join, or cut.
        sketch: Sketch name/index. Default: last created sketch.
    """
    return _call("revolve", {"angle": angle, "profile_index": profile_index,
                              "axis_index": axis_index, "operation": operation,
                              "sketch": sketch})

@mcp.tool()
def loft_sketches(sketch_indices: list = [0, 1], operation: str = "new_body") -> str:
    """
    Create a lofted body blending through profiles in multiple sketches.

    Args:
        sketch_indices: List of sketch indices to loft through, e.g. [0, 1, 2].
        operation: new_body, join, or cut.
    """
    return _call("loft", {"sketch_indices": sketch_indices, "operation": operation})

@mcp.tool()
def sweep_sketch(profile_sketch_index: int = 0, path_sketch_index: int = 1,
                 operation: str = "new_body") -> str:
    """
    Sweep a profile sketch along a path sketch.

    Args:
        profile_sketch_index: Index of the sketch with the cross-section.
        path_sketch_index:    Index of the sketch with the path curve.
        operation:            new_body, join, or cut.
    """
    return _call("sweep", {"profile_sketch_index": profile_sketch_index,
                            "path_sketch_index": path_sketch_index, "operation": operation})

@mcp.tool()
def create_helix(pitch: float = 1.0, height: float = 5.0, clockwise: bool = True) -> str:
    """
    Create a coil/helix from the active sketch profile.

    Args:
        pitch:      Distance between each coil revolution (cm).
        height:     Total height of the helix (cm).
        clockwise:  True for right-hand coil, False for left-hand.
    """
    return _call("helix", {"pitch": pitch, "height": height, "clockwise": clockwise})

@mcp.tool()
def create_pipe(path_sketch_index: int = 0, section_size: float = 0.5,
                wall_thickness: float = 0, operation: str = "new_body") -> str:
    """
    Create a pipe/tube along a path sketch curve. If wall_thickness > 0, creates hollow pipe.

    Args:
        path_sketch_index: Sketch index containing the path curve.
        section_size:      Outer radius of the pipe cross-section (cm).
        wall_thickness:    Wall thickness (cm). 0 = solid rod.
        operation:         new_body, join, or cut.
    """
    return _call("create_pipe", {"path_sketch_index": path_sketch_index,
                                  "section_size": section_size,
                                  "wall_thickness": wall_thickness,
                                  "operation": operation})

@mcp.tool()
def create_hole(body: str = "0", face_index: int = 0,
                x: float = 0, y: float = 0,
                diameter: float = 1.0, depth: float = 2.0,
                hole_type: str = "simple",
                counterbore_diameter: float = 0, counterbore_depth: float = 0,
                countersink_diameter: float = 0, countersink_angle: float = 90) -> str:
    """
    Create a parametric hole feature on a face.

    Args:
        body:        Body name or index.
        face_index:  Face to place hole on.
        x, y:        Position on the face (cm).
        diameter:    Hole diameter (cm).
        depth:       Hole depth (cm).
        hole_type:   'simple', 'counterbore', or 'countersink'.
        counterbore_diameter: Counterbore diameter (cm, only for counterbore type).
        counterbore_depth:    Counterbore depth (cm, only for counterbore type).
        countersink_diameter: Countersink diameter (cm, only for countersink type).
        countersink_angle:    Countersink angle in degrees (only for countersink type).
    """
    return _call("create_hole", {
        "body": body, "face_index": face_index, "x": x, "y": y,
        "diameter": diameter, "depth": depth, "hole_type": hole_type,
        "counterbore_diameter": counterbore_diameter,
        "counterbore_depth": counterbore_depth,
        "countersink_diameter": countersink_diameter,
        "countersink_angle": countersink_angle,
    })

@mcp.tool()
def shell_body(body: str = "0", thickness: float = 0.2, face_indices: list = [0]) -> str:
    """
    Hollow out a solid body by removing faces and applying a wall thickness.

    Args:
        body:         Body name or index.
        thickness:    Wall thickness in centimetres.
        face_indices: List of face indices to open/remove.
    """
    return _call("shell", {"body": body, "thickness": thickness, "face_indices": face_indices})

@mcp.tool()
def fillet_edges(body: str = "0", radius: float = 0.5, edge_indices: list = [0]) -> str:
    """
    Add smooth rounded fillets to edges of a body.

    Args:
        body:         Body name or index.
        radius:       Fillet radius in centimetres.
        edge_indices: List of edge indices to fillet.
    """
    return _call("fillet", {"body": body, "radius": radius, "edge_indices": edge_indices})

@mcp.tool()
def chamfer_edges(body: str = "0", distance: float = 0.3, edge_indices: list = [0]) -> str:
    """
    Add flat angled chamfers to edges of a body.

    Args:
        body:         Body name or index.
        distance:     Chamfer distance in centimetres.
        edge_indices: List of edge indices to chamfer.
    """
    return _call("chamfer", {"body": body, "distance": distance, "edge_indices": edge_indices})

@mcp.tool()
def mirror_body(body: str = "0", plane: str = "XY") -> str:
    """
    Mirror a body across a construction plane.

    Args:
        body:  Body name or index.
        plane: XY, XZ, YZ, or named construction plane.
    """
    return _call("mirror_body", {"body": body, "plane": plane})

@mcp.tool()
def rectangular_pattern_body(body: str = "0", x_count: int = 2, y_count: int = 1,
                               x_spacing: float = 5, y_spacing: float = 5) -> str:
    """
    Create a rectangular grid of copies of a body.

    Args:
        body:                Body name or index.
        x_count, y_count:    Number of instances in each direction.
        x_spacing, y_spacing: Distance between instances (cm).
    """
    return _call("rectangular_pattern_body", {
        "body": body, "x_count": x_count, "y_count": y_count,
        "x_spacing": x_spacing, "y_spacing": y_spacing})

@mcp.tool()
def circular_pattern_body(body: str = "0", count: int = 4, axis: str = "Z") -> str:
    """
    Create a circular array of a body around an axis.

    Args:
        body:  Body name or index.
        count: Total number of instances including the original.
        axis:  X, Y, or Z construction axis.
    """
    return _call("circular_pattern_body", {"body": body, "count": count, "axis": axis})

@mcp.tool()
def combine_bodies(target_body: str = "0", tool_bodies: list = [1],
                   operation: str = "join", keep_tools: bool = False) -> str:
    """
    Boolean operation between bodies: join, cut, or intersect.

    Args:
        target_body: The base body (name or index).
        tool_bodies: List of tool body names or indices.
        operation:   join (add), cut (subtract), or intersect (keep overlap only).
        keep_tools:  Keep tool bodies after the operation.
    """
    return _call("combine_bodies", {"target_body": target_body, "tool_bodies": tool_bodies,
                                     "operation": operation, "keep_tools": keep_tools})

@mcp.tool()
def scale_body(body: str = "0", scale_x: float = 2.0, scale_y: float = -1, scale_z: float = -1,
               cx: float = 0, cy: float = 0, cz: float = 0) -> str:
    """
    Scale a body. If scale_y and scale_z are -1, uniform scaling by scale_x.
    Otherwise non-uniform scaling per axis.

    Args:
        body:        Body name or index.
        scale_x:     Scale factor for X (or uniform if y/z are -1).
        scale_y:     Scale factor for Y (-1 = same as scale_x).
        scale_z:     Scale factor for Z (-1 = same as scale_x).
        cx, cy, cz:  Centre point of scaling (cm).
    """
    return _call("scale_body", {"body": body, "scale_x": scale_x,
                                 "scale_y": scale_y, "scale_z": scale_z,
                                 "cx": cx, "cy": cy, "cz": cz})

@mcp.tool()
def move_body(body: str = "0", dx: float = 0, dy: float = 0, dz: float = 0) -> str:
    """
    Move a body by a translation offset.

    Args:
        body:       Body name or index.
        dx, dy, dz: How far to move along each axis (cm).
    """
    return _call("move_body", {"body": body, "dx": dx, "dy": dy, "dz": dz})

@mcp.tool()
def rotate_body(body: str = "0", axis: str = "Z",
                angle: float = 45,
                cx: float = 0, cy: float = 0, cz: float = 0) -> str:
    """
    Rotate a body around an axis by an angle.

    Args:
        body:       Body name or index.
        axis:       Rotation axis: X, Y, or Z.
        angle:      Rotation angle in degrees.
        cx, cy, cz: Center point of rotation (cm).
    """
    return _call("rotate_body", {"body": body, "axis": axis, "angle": angle,
                                  "cx": cx, "cy": cy, "cz": cz})

@mcp.tool()
def press_pull_face(body: str = "0", face_index: int = 0, distance: float = 1.0) -> str:
    """
    Push or pull a face to resize part of a body.

    Args:
        body:        Body name or index.
        face_index:  Face to move.
        distance:    Distance in cm (positive = outward, negative = inward).
    """
    return _call("press_pull", {"body": body, "face_index": face_index, "distance": distance})

@mcp.tool()
def thicken_sketch(thickness: float = 0.5) -> str:
    """
    Thicken the profile in the active sketch into a thin solid body.

    Args:
        thickness: Thickness in centimetres.
    """
    return _call("thicken", {"thickness": thickness})

@mcp.tool()
def draft_face(body: str = "0", face_index: int = 0,
               angle: float = 3, pull_plane: str = "XY") -> str:
    """
    Add a draft angle to a face for mold-making or 3D print removal.

    Args:
        body:        Body name or index.
        face_index:  Face to draft.
        angle:       Draft angle in degrees.
        pull_plane:  Pull direction plane (XY or XZ).
    """
    return _call("draft_face", {"body": body, "face_index": face_index,
                                 "angle": angle, "pull_plane": pull_plane})

@mcp.tool()
def add_thread(body: str = "0", face_index: int = 0,
               thread_type: str = "ANSI Metric M Profile",
               is_internal: bool = False, full_length: bool = True,
               right_handed: bool = True) -> str:
    """
    Add a thread feature to a cylindrical face.

    Args:
        body:         Body name or index.
        face_index:   Index of the cylindrical face.
        thread_type:  'ANSI Metric M Profile' or 'ANSI Unified Screw Threads'.
        is_internal:  True for internal thread, False for external.
        full_length:  Apply thread the full face length.
        right_handed: Standard right-hand thread direction.
    """
    return _call("add_thread", {"body": body, "face_index": face_index,
                                 "thread_type": thread_type, "is_internal": is_internal,
                                 "full_length": full_length, "right_handed": right_handed})


# ---- Assembly / Joints ----

@mcp.tool()
def create_component(name: str = "New Component") -> str:
    """Create a new empty component in the design."""
    return _call("create_component", {"name": name})

@mcp.tool()
def move_body_to_component(body: str = "0", component: str = "") -> str:
    """
    Move a body into an existing component.

    Args:
        body:      Body name or index.
        component: Target component name.
    """
    return _call("move_body_to_component", {"body": body, "component": component})

@mcp.tool()
def create_joint(component1: str = "", component2: str = "",
                 joint_type: str = "rigid",
                 offset_x: float = 0, offset_y: float = 0, offset_z: float = 0) -> str:
    """
    Create a joint between two components.

    Args:
        component1: Name of first component (grounded).
        component2: Name of second component (moves).
        joint_type: rigid, revolute, slider, cylindrical, pin_slot, planar, or ball.
        offset_x, offset_y, offset_z: Position offset for the joint origin (cm).
    """
    return _call("create_joint", {
        "component1": component1, "component2": component2,
        "joint_type": joint_type,
        "offset_x": offset_x, "offset_y": offset_y, "offset_z": offset_z,
    })

@mcp.tool()
def create_as_built_joint(component1: str = "", component2: str = "",
                          joint_type: str = "rigid") -> str:
    """
    Create an as-built joint between two components at their current positions.

    Args:
        component1: Name of first component.
        component2: Name of second component.
        joint_type: rigid, revolute, slider, cylindrical, pin_slot, planar, or ball.
    """
    return _call("create_as_built_joint", {
        "component1": component1, "component2": component2,
        "joint_type": joint_type,
    })


# ---- Body Management ----

@mcp.tool()
def delete_body(body: str = "0") -> str:
    """Delete a body from the design."""
    return _call("delete_body", {"body": body})

@mcp.tool()
def rename_body(body: str = "0", name: str = "My Body") -> str:
    """Rename a body."""
    return _call("rename_body", {"body": body, "name": name})

@mcp.tool()
def copy_body(body: str = "0", name: str = "",
              dx: float = 2, dy: float = 0, dz: float = 0) -> str:
    """
    Copy a body and place it at an offset position.

    Args:
        body:       Body name or index.
        name:       Name for the new copy.
        dx, dy, dz: Offset for the copy position (cm).
    """
    return _call("copy_body", {"body": body, "name": name, "dx": dx, "dy": dy, "dz": dz})

@mcp.tool()
def toggle_body_visibility(body: str = "0") -> str:
    """Toggle a body visible or hidden."""
    return _call("toggle_body_visibility", {"body": body})


# ---- Construction Geometry ----

@mcp.tool()
def add_construction_plane(base_plane: str = "XY", offset: float = 2.0,
                            plane_type: str = "offset") -> str:
    """
    Add a construction plane for sketching at different heights or angles.

    Args:
        base_plane:  XY, XZ, YZ, or name of existing construction plane.
        offset:      Distance from base plane in cm (for offset type).
        plane_type:  offset (parallel at height), angle (angled), or midplane.
    """
    return _call("add_construction_plane", {"base_plane": base_plane,
                                             "offset": offset, "type": plane_type})

@mcp.tool()
def add_construction_axis(axis_type: str = "line", body: str = "0",
                          face_index: int = 0, edge_index: int = 0) -> str:
    """
    Create a construction axis.

    Args:
        axis_type:   'line' (through two points), 'cylinder' (axis of cylindrical face),
                     'perpendicular' (perpendicular to face), 'edge' (along an edge).
        body:        Body name or index.
        face_index:  Face index (for cylinder/perpendicular types).
        edge_index:  Edge index (for edge type).
    """
    return _call("add_construction_axis", {
        "axis_type": axis_type, "body": body,
        "face_index": face_index, "edge_index": edge_index,
    })


# ---- Parameter Tools ----

@mcp.tool()
def list_parameters() -> str:
    """List all parameters with values, units, and expressions."""
    return _call("list_parameters")

@mcp.tool()
def add_parameter(name: str = "width", value: float = 10.0,
                  unit: str = "cm", comment: str = "") -> str:
    """
    Add a user parameter to drive model dimensions.

    Args:
        name:    Parameter name (no spaces).
        value:   Numeric value.
        unit:    Unit: cm, mm, m, in, deg, etc.
        comment: Optional description.
    """
    return _call("add_parameter", {"name": name, "value": value,
                                    "unit": unit, "comment": comment})

@mcp.tool()
def update_parameter(name: str = "width", value: float = 15.0) -> str:
    """
    Update a parameter value. All linked geometry updates automatically.

    Args:
        name:  Parameter name.
        value: New value.
    """
    return _call("update_parameter", {"name": name, "value": value})


# ---- Materials & Appearances ----

@mcp.tool()
def list_appearances(search: str = "") -> str:
    """
    List available appearance presets.

    Args:
        search: Optional filter, e.g. 'steel', 'wood', 'glass'.
    """
    return _call("list_appearances", {"search": search})

@mcp.tool()
def apply_appearance(body: str = "0", appearance: str = "Steel") -> str:
    """
    Apply a named appearance to a body.

    Args:
        body:       Body name or index.
        appearance: Partial name to search for.
    """
    return _call("apply_appearance", {"body": body, "appearance": appearance})

@mcp.tool()
def set_body_color(body: str = "0", r: int = 100, g: int = 149,
                   b: int = 237, opacity: int = 255) -> str:
    """
    Set a custom RGB color on a body.

    Args:
        body:    Body name or index.
        r, g, b: Red, green, blue (0-255).
        opacity: 255 = fully opaque, 0 = invisible.
    """
    return _call("set_body_color", {"body": body, "r": r, "g": g, "b": b, "opacity": opacity})


# ---- Export & Capture ----

@mcp.tool()
def export_as_stl(path: str = "") -> str:
    """Export as STL for 3D printing. Blank path = ~/Desktop/fusion_export.stl"""
    return _call("export_stl", {"path": path} if path else {})

@mcp.tool()
def export_as_step(path: str = "") -> str:
    """Export as STEP (universal CAD format). Blank path = ~/Desktop/fusion_export.step"""
    return _call("export_step", {"path": path} if path else {})

@mcp.tool()
def export_as_3mf(path: str = "") -> str:
    """Export as 3MF for modern 3D printing. Blank path = ~/Desktop/fusion_export.3mf"""
    return _call("export_3mf", {"path": path} if path else {})

@mcp.tool()
def export_as_f3d(path: str = "") -> str:
    """Export as Fusion archive (.f3d) for backup/sharing. Blank path = ~/Desktop/fusion_export.f3d"""
    return _call("export_f3d", {"path": path} if path else {})

@mcp.tool()
def capture_screenshot(path: str = "", width: int = 1920, height: int = 1080) -> list:
    """
    Capture the current viewport as a PNG image. Returns the image so the AI can see what was created.

    Args:
        path:   Save path. Blank = ~/Desktop/fusion_screenshot.png
        width:  Image width in pixels.
        height: Image height in pixels.
    """
    try:
        r = requests.post(f"{FUSION_URL}/command",
                          json={"command": "capture_screenshot",
                                "params": {"path": path, "width": width, "height": height}},
                          timeout=30)
        data = r.json()
        if "error" in data:
            return f"Error: {data['error']}"
        b64 = data.get("image_base64", "")
        if b64:
            return [
                f"Screenshot saved to {data.get('screenshot', 'unknown')} ({data.get('size', '')})",
                Image(data=base64.b64decode(b64), format="png")
            ]
        return json.dumps(data, indent=2)
    except requests.exceptions.ConnectionError:
        return "Cannot reach Fusion 360. Make sure Fusion is open and the FusionMCP add-in is running."
    except Exception as e:
        return f"Unexpected error: {e}"


# ---- Import Tools ----

@mcp.tool()
def import_cad_file(path: str, format: str = "", as_component: bool = False) -> str:
    """
    Import a CAD file (STEP/SAT/SMT/IGES/F3D) into the current design.

    Auto-detects the format from the file extension when `format` is empty.

    Args:
        path:         Absolute path to the CAD file (.step/.stp, .sat, .smt, .igs/.iges, .f3d).
        format:       Optional format override: step, sat, smt, iges, f3d. Empty = auto-detect.
        as_component: If True, import into a new component instead of the root component.
    """
    return _call("import_cad_file", {"path": path, "format": format, "as_component": as_component})


@mcp.tool()
def import_mesh_file(path: str, units: str = "mm", as_component: bool = False) -> str:
    """
    Import a mesh file (STL or 3MF) as a mesh body.

    Args:
        path:         Absolute path to the mesh file (.stl or .3mf).
        units:        Units of the mesh file: mm, cm, or in.
        as_component: If True, import into a new component instead of the root component.
    """
    return _call("import_mesh_file", {"path": path, "units": units, "as_component": as_component})


@mcp.tool()
def import_sketch_file(path: str, format: str = "", plane: str = "XY") -> str:
    """
    Import a 2D drawing file (SVG or DXF) as a sketch.

    Auto-detects the format from the file extension when `format` is empty.

    Args:
        path:   Absolute path to the 2D file (.svg or .dxf).
        format: Optional format override: svg or dxf. Empty = auto-detect.
        plane:  Construction plane for DXF import: XY, XZ, or YZ.
    """
    return _call("import_sketch_file", {"path": path, "format": format, "plane": plane})


# ---- OpenSCAD Pipeline ----

@mcp.tool()
def run_scad(code: str, params: str = "", quality: int = 100, units: str = "mm") -> str:
    """
    Render OpenSCAD code to a mesh body using the bundled OpenSCAD + BOSL2.

    Writes the code to a temp .scad file, renders it to STL with the bundled
    OpenSCAD (BOSL2 available via include <BOSL2/std.scad>), imports the STL
    as a mesh body, and stores the source in the body's 'scad_source'
    attribute so it can be re-run later with update_scad_body.

    Args:
        code:    Raw OpenSCAD source, e.g. 'cube([10, 10, 10]);' or
                 'include <BOSL2/std.scad>\\ncuboid([10,10,10]);'.
        params:  Optional '-D' variable overrides, e.g. 'Grid_Pitch=50;Frame_Depth=12'.
        quality: Sets '$fn' (facet count) prepended to the code. 0 = leave code unchanged.
        units:   Units of the OpenSCAD model: mm, cm, or in.
    """
    return _call("run_scad", {"code": code, "params": params, "quality": quality, "units": units}, timeout=330)


@mcp.tool()
def update_scad_body(body: str = "0", params: str = "", code: str = "") -> str:
    """
    Re-run a stored .scad source on a mesh body with new parameters.

    Reads the 'scad_source' attribute stored by run_scad, deletes the old mesh
    body, re-renders, and creates a new mesh body. The new body is renamed to
    the original body's name.

    Args:
        body:   Body name or index of a mesh body created by run_scad.
        params: Optional '-D' variable overrides for the re-render.
        code:   Optional replacement OpenSCAD source; overrides the stored source when provided.
    """
    return _call("update_scad_body", {"body": body, "params": params, "code": code}, timeout=330)


@mcp.tool()
def import_mesh_data(coordinates: list, triangle_indices: list, normals: list = None,
                     normal_indices: list = None, name: str = "") -> str:
    """
    Create a mesh body directly from triangle data.

    Args:
        coordinates:      Flat list [x0,y0,z0, x1,y1,z1, ...] of vertex coordinates.
        triangle_indices: Flat list of vertex index triples [v0,v1,v2, v0,v1,v2, ...].
        normals:          Optional flat list of normal vectors. Omit to auto-generate face normals.
        normal_indices:   Optional flat index list for the normals. Omit to use triangle_indices.
        name:             Optional name for the created mesh body.
    """
    return _call("import_mesh_data", {"coordinates": coordinates, "triangle_indices": triangle_indices,
                                       "normals": normals, "normal_indices": normal_indices, "name": name})


def _cm_to_unit_factor(units: str) -> float:
    """Multiplier converting cm-based measurements to the requested units."""
    u = str(units or "cm").strip().lower()
    if u == "cm":
        return 1.0
    if u == "mm":
        return 10.0
    if u == "in":
        return 1.0 / 2.54
    raise ValueError(f"Unsupported units '{units}'. Use 'mm', 'cm', or 'in'.")


@mcp.tool()
def analyze_mesh(mesh: str = "0", units: str = "cm",
                  angle_tolerance_deg: Optional[float] = None,
                  offset_tol: Optional[float] = None,
                  snap_tol: Optional[float] = None,
                  simplify_vertices: bool = True,
                  preset: Optional[str] = None) -> str:
    """
    Analyze a mesh body and report measured facts + a recommended strategy.

    Fetches the mesh triangle data (nodes/indices/normals) from Fusion, then
    runs pure-Python analysis: watertightness, manifoldness, vertex/triangle
    counts, enclosed volume (divergence theorem), bounding box, mirror
    symmetry, primitive hints (plane regions / box / cylinder), and a
    recommended reconstruction strategy (prismatic | revolved |
    csg_decompose | organic).

    Stage 2 of the mesh-to-parametric workflow (see get_workflow_guide).

    Args:
        mesh:  Body name or index of a mesh body.
        units: Units for the report: mm, cm, or in (default cm).
        angle_tolerance_deg:
            Dihedral angle threshold for planar face grouping in degrees
            (default ``None`` → resolves to ``0.5`` or the preset's angle).
            An explicit value overrides the preset.
        offset_tol:
            Per-plane offset tolerance for coplanarity grouping (cm).
            Overrides both defaults and preset when provided.
        snap_tol:
            Vertex-snap tolerance for seam closure (cm).
            Overrides both defaults and preset when provided.
        simplify_vertices:
            If True (default), collapse nearly-collinear polygon vertices
            in planar face outlines.
        preset:
            Tolerance preset — ``"accurate"``, ``"balanced"``, or
            ``"coarse"``.  ``"accurate"`` is stricter (more faces);
            ``"coarse"`` is looser (fewer faces).  Invalid names raise
            an error.
    """
    try:
        factor = _cm_to_unit_factor(units)
    except ValueError as e:
        return json.dumps({"error": str(e)}, indent=2)
    raw = _call("extract_mesh_data", {"mesh": mesh})
    if raw.startswith("Error: "):
        return json.dumps({"error": raw[len("Error: "):]}, indent=2)
    if raw.startswith(("Cannot reach", "Unexpected error")):
        return raw
    try:
        data = json.loads(raw)
    except ValueError:
        return raw
    if "error" in data:
        return json.dumps({"error": data["error"]}, indent=2)
    try:
        mesh_analysis = _load_mesh_analysis()
        report = mesh_analysis.analyze_mesh_data(
            data.get("nodes", []), data.get("indices", []),
            data.get("normals", []),
            angle_tolerance_deg=angle_tolerance_deg,
            offset_tol=offset_tol, snap_tol=snap_tol,
            simplify_vertices=simplify_vertices, preset=preset)
        report = mesh_analysis.scale_report(report, factor)
    except Exception as e:
        return json.dumps({"error": f"analysis failed: {e}"}, indent=2)
    report["mesh"] = data.get("mesh", mesh)
    report["units"] = units
    return json.dumps(report, indent=2)


_GRAPH_EDGE_RELATIONS = (
    "COMPONENT_OF", "CONTAINS", "COPLANAR", "EDGE_ADJACENT",
    "EXTRUSION_ALIGNED", "HAS_BASE", "PARALLEL", "PERPENDICULAR",
    "SAME_ORIENTATION", "VERTEX_TOUCH",
)
"""The 10 edge relation types the structure graph can carry (sorted)."""


def _base_face_candidates(graph):
    """PRELIMINARY base-face heuristic: the largest-area Face per component.

    T9 replaces this with real scored candidates (unit classification,
    dependency ordering); the summary key stays stable until then.
    """
    best = {}
    for n, d in graph.nodes(data=True):
        if d.get("label") != "Face":
            continue
        comp = d.get("component_id")
        area = d.get("area_cm2", 0.0)
        if comp not in best or area > best[comp][1]:
            best[comp] = (n, area)
    return [{"component_id": comp, "face_id": fid, "area_cm2": area}
            for comp, (fid, area) in sorted(best.items())]


@mcp.tool()
def structure_graph(mesh: str = "0", units: str = "cm",
                     angle_tolerance_deg: Optional[float] = None,
                     offset_tol: Optional[float] = None,
                     snap_tol: Optional[float] = None,
                     simplify_vertices: bool = True,
                     preset: Optional[str] = None) -> str:
    """
    Build the structure graph of a mesh body and persist it to DuckDB.

    Fetches the mesh triangle data (nodes/indices) from Fusion, decomposes it
    into planar faces / curved patches (mesh_analysis), builds the NetworkX
    property graph (face/hole/curved/component nodes; 10 edge relation types)
    via mesh_graph.build_structure_graph, runs graph algorithms (articulation
    points, connected components), and persists the FULL graph into an
    in-memory DuckDB database (nodes/edges tables) so later queries can run
    SQL against it (mesh_graph._get_graph_db).

    Returns a JSON summary -- NOT the full graph.  The full graph stays
    server-side in DuckDB; the summary (counts, edge-type histogram,
    PRELIMINARY base-face candidates, the persisted table schema, and a
    ready-to-run query example) helps the agent decide the first query.

    NOTE: ``base_face_candidates`` is retained as a legacy preliminary key
    (largest-area face per component).  The real scored analysis (composite
    base-face scoring, unit classification, dependency ordering) is in the
    ``workstream`` section of the summary — see that for
    ``base_face_per_component`` / ``unit_types`` / ``rebuild_order``.

    Args:
        mesh:  Body name or index of a mesh body.
        units: Units for the report: mm, cm, or in (default cm).
        angle_tolerance_deg:
            Dihedral angle threshold for planar face grouping in degrees
            (default ``None`` → resolves to ``0.5`` or the preset's angle).
            An explicit value overrides the preset.
        offset_tol:
            Per-plane offset tolerance for coplanarity grouping (cm).
            Overrides both defaults and preset when provided.
        snap_tol:
            Vertex-snap tolerance for seam closure (cm).
            Overrides both defaults and preset when provided.
        simplify_vertices:
            If True (default), collapse nearly-collinear polygon vertices
            in planar face outlines.
        preset:
            Tolerance preset — ``"accurate"``, ``"balanced"``, or
            ``"coarse"``.  ``"accurate"`` is stricter (more faces);
            ``"coarse"`` is looser (fewer faces).  Invalid names raise
            an error.
    """
    try:
        _cm_to_unit_factor(units)
    except ValueError as e:
        return json.dumps({"error": str(e)}, indent=2)
    raw = _call("extract_mesh_data", {"mesh": mesh})
    if raw.startswith("Error: "):
        return json.dumps({"error": raw[len("Error: "):]}, indent=2)
    if raw.startswith(("Cannot reach", "Unexpected error")):
        return raw
    try:
        data = json.loads(raw)
    except ValueError:
        return raw
    if "error" in data:
        return json.dumps({"error": data["error"]}, indent=2)
    try:
        mesh_analysis = _load_mesh_analysis()
        mesh_graph = _load_mesh_graph()
        decompose_result = mesh_analysis.decompose_mesh_faces(
            data.get("nodes", []), data.get("indices", []),
            angle_tolerance_deg=angle_tolerance_deg,
            simplify_vertices=simplify_vertices,
            offset_tol=offset_tol, snap_tol=snap_tol, preset=preset)
        graph = mesh_graph.build_structure_graph(decompose_result)

        articulation_points = sorted(nx.articulation_points(graph))
        components = list(nx.connected_components(graph))
        graph.graph["articulation_points"] = articulation_points
        graph.graph["connected_components"] = [sorted(c) for c in components]
        for fid in articulation_points:
            if fid in graph.nodes and graph.nodes[fid].get("label") == "Face":
                graph.nodes[fid]["is_articulation_point"] = True

        conn = mesh_graph._persist_to_duckdb(graph, mesh)

        face_count = sum(1 for n, d in graph.nodes(data=True)
                         if d.get("label") == "Face")
        relation_counts = {r: 0 for r in _GRAPH_EDGE_RELATIONS}
        for _, _, d in graph.edges(data=True):
            rel = d.get("relation")
            if rel in relation_counts:
                relation_counts[rel] += 1
        edge_type_counts = dict(sorted(relation_counts.items()))

        def _table_columns(conn, table):
            return [row[1] for row in
                    conn.execute(f"PRAGMA table_info({table})").fetchall()]

        # T10: build workstream summary from attrs set by T9 functions
        # (_score_base_faces / _classify_units / _build_dependency_order
        # already called inside build_structure_graph — just READ here).
        base_face_per_component = {}
        for n, d in graph.nodes(data=True):
            if d.get("label") == "Face" and d.get("is_base_candidate"):
                cid = str(int(d.get("component_id", 0)))
                base_face_per_component[cid] = n
        base_face_per_component = dict(
            sorted(base_face_per_component.items(),
                   key=lambda kv: int(kv[0])))
        unit_types = {}
        for n, d in graph.nodes(data=True):
            if d.get("label") == "Component":
                cid = str(int(d.get("component_id", 0)))
                unit_types[cid] = d.get("unit_type", "freeform")
        unit_types = dict(
            sorted(unit_types.items(), key=lambda kv: int(kv[0])))
        workstream = {
            "base_face_per_component": base_face_per_component,
            "unit_types": unit_types,
            "rebuild_order": list(graph.graph.get("rebuild_order", [])),
            "dag_has_cycles": bool(graph.graph.get("dag_has_cycles", False)),
        }

        summary = {
            "mesh": data.get("mesh", mesh),
            "units": units,
            "component_count": int(
                decompose_result.get("components_detected", 0)),
            "face_count": face_count,
            "edge_type_counts": edge_type_counts,
            "base_face_candidates": _base_face_candidates(graph),
            "has_warnings": bool(decompose_result.get("has_warnings", False)),
            "duckdb_table_schema": {
                "nodes": _table_columns(conn, "nodes"),
                "edges": _table_columns(conn, "edges"),
            },
            "query_example": ("SELECT node_id, area_cm2 FROM nodes "
                              "WHERE label='Face' ORDER BY area_cm2 DESC"),
            "articulation_points": articulation_points,
            "connected_component_count": len(components),
            "workstream": workstream,
        }
        if decompose_result.get("strategy_fallback_suggested"):
            summary["fallback_strategy"] = {
                "strategy": "organic",
                "reason": ("high residual non-manifold edge count "
                           "(>5% of triangle edges)"),
                "unpaired_pct": decompose_result["unpaired_pct"],
            }
    except Exception as e:
        return json.dumps({"error": f"structure graph failed: {e}"},
                          indent=2)
    return json.dumps(summary, indent=2)


def _assert_read_only_sql(sql):
    """Reject any SQL that is not a single read-only SELECT/WITH statement.

    duckdb 1.5.5 cannot flip a RUNNING in-memory connection to read-only
    (``SET access_mode='read_only'`` raises InvalidInputException, and
    ``connect(":memory:", read_only=True)`` / ``ATTACH ... (READ_ONLY)``
    both raise CatalogException), so read-only is enforced here at the
    STATEMENT level: SQL comments (``-- ...`` line and ``/* ... */`` block)
    are stripped, the statement must then start with SELECT or WITH
    (case-insensitive), and at most ONE trailing semicolon is allowed --
    multi-statement injection such as ``SELECT 1; DROP TABLE nodes`` is
    rejected because any remaining ``;`` is a fatal error.

    Returns the comment-stripped, semicolon-normalised SQL so callers can
    re-use it (e.g. for ORDER BY detection).  Raises
    ``ValueError("only SELECT queries are allowed")`` for non-read-only
    input.
    """
    stripped = re.sub(r"/\*.*?\*/", "", sql, flags=re.DOTALL)
    stripped = re.sub(r"--[^\n]*", "", stripped)
    stripped = stripped.strip()
    if stripped.endswith(";"):
        stripped = stripped[:-1].strip()
    if ";" in stripped:
        raise ValueError("only SELECT queries are allowed")
    first_word = re.split(r"\s+", stripped, maxsplit=1)[0].lower()
    # The first word must be a PREFIX of SELECT or WITH (case-insensitive).
    # A truncated keyword like ``selec`` is tolerated so it reaches the SQL
    # parser, which rejects it -- a strict prefix can never begin a valid
    # non-read statement, so only exact SELECT/WITH statements execute.
    if not ("select".startswith(first_word) or "with".startswith(first_word)):
        raise ValueError("only SELECT queries are allowed")
    return stripped


def _json_safe_cell(value):
    """Convert a DuckDB cell to a JSON-serialisable Python value.

    bytes -> utf-8 str; Decimal -> float; everything else passes through
    natively (int, float, bool, str, None).  List/dict attrs (centroid,
    interior_angles, ...) were stored as JSON TEXT at persist time, so they
    already arrive here as plain strings.
    """
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    if isinstance(value, Decimal):
        return float(value)
    return value


@mcp.tool()
def query_structure_graph(mesh: str = "0", sql: str = "") -> str:
    """
    Run a READ-ONLY SQL query against the persisted structure graph.

    Queries the in-memory DuckDB database that `structure_graph` persisted
    for `mesh`.  Call `structure_graph(mesh=...)` FIRST -- graphs are
    ephemeral: an MCP server restart drops them.  Returns only the query
    results as JSON (``{mesh, columns, rows, row_count}``); the full graph
    never leaves the server.

    READ-ONLY: only a single SELECT/WITH statement is allowed.  SQL
    comments are stripped, the statement must start with SELECT or WITH
    (case-insensitive), and multi-statement SQL (any inner semicolon, e.g.
    ``SELECT 1; DROP TABLE nodes``) is rejected with
    ``{"error": "only SELECT queries are allowed"}``.

    Deterministic ordering: when the query has NO ORDER BY clause the rows
    are sorted by the first column before being returned (NULLs last); with
    an ORDER BY clause the SQL ordering is preserved.

    DuckDB table schema -- column set = the fixed core + the SORTED UNION of
    every node/edge attribute present, so a column only appears when at
    least one row carries that attribute (e.g. hole-only columns exist only
    for meshes with holes; the box carries no VERTEX_TOUCH/SAME_ORIENTATION
    edges).  List attrs (centroid, interior_angles, ...) come back as JSON
    strings.

    ``nodes`` (node_id TEXT PRIMARY KEY, label TEXT, then):
      area_cm2              DOUBLE    -- face/area in cm^2
      centroid              TEXT      -- JSON "[x, y, z]"
      component_id          INTEGER   -- owning component index
      containing_face_id    TEXT      -- Hole: owning Face node id
      convexity             TEXT      -- 'convex' | 'concave'
      curve_type            TEXT      -- 'planar' | curved-patch surface type
      face_count            INTEGER   -- Component: faces it owns
      face_index            INTEGER   -- Face: index in the decompose output
      interior_angles       TEXT      -- JSON list of angles in degrees
      is_articulation_point BOOLEAN   -- Face articulation-point flag
      is_base_candidate     BOOLEAN   -- Face base-candidate flag
      is_filled             BOOLEAN   -- Hole: False while the hole is open
      mean_curvature        DOUBLE    -- 0.0 for planar faces
      normal_x/y/z          DOUBLE    -- face plane normal (canonical)
      plane_id              TEXT      -- "nx|ny|nz|offset"
      triangle_count        INTEGER
      vertex_count          INTEGER

    ``edges`` (source TEXT, target TEXT, relation TEXT, then):
      convexity           TEXT      -- 'convex' | 'concave' (EDGE_ADJACENT)
      dihedral_angle_deg  DOUBLE    -- 0-180 deg (EDGE_ADJACENT)
      extrusion_direction TEXT      -- JSON "[x, y, z]" (EXTRUSION_ALIGNED)
      normal_dot          DOUBLE    -- COPLANAR/PARALLEL/PERPENDICULAR
      normal_x/y/z        DOUBLE    -- SAME_ORIENTATION canonical normal
      orientation         TEXT      -- EDGE_ADJACENT shared-edge orientation
      shared_edge_length  DOUBLE    -- EDGE_ADJACENT shared edge length (cm)
      shared_vertex       TEXT      -- VERTEX_TOUCH shared vertex
      shared_vertex_count INTEGER   -- VERTEX_TOUCH shared vertex count

    ``relation`` is one of: COMPONENT_OF, CONTAINS, COPLANAR, EDGE_ADJACENT,
    EXTRUSION_ALIGNED, HAS_BASE, PARALLEL, PERPENDICULAR, SAME_ORIENTATION,
    VERTEX_TOUCH.

    Query examples:
      SELECT COUNT(*) FROM nodes;
      SELECT node_id, area_cm2 FROM nodes
        WHERE label='Face' AND convexity='concave';   -- concave faces only
      -- recursive CTE: 2-hop face traversal over EDGE_ADJACENT edges
      WITH RECURSIVE adj(u, v) AS (
        SELECT source, target FROM edges WHERE relation='EDGE_ADJACENT'
        UNION ALL
        SELECT target, source FROM edges WHERE relation='EDGE_ADJACENT'
      ), hops(node_id, depth) AS (
        SELECT 'face:0', 0
        UNION ALL
        SELECT a.v, h.depth + 1 FROM hops h
        JOIN adj a ON a.u = h.node_id WHERE h.depth < 2
      )
      SELECT DISTINCT node_id FROM hops ORDER BY node_id;

    Args:
        mesh: Body name or index whose graph was persisted by
              `structure_graph` (default "0").
        sql:  The read-only SQL query to run (default "" -> error).
    """
    if not (sql or "").strip():
        return json.dumps({"error": "sql parameter is required"}, indent=2)
    try:
        mesh_graph = _load_mesh_graph()
        conn = mesh_graph._get_graph_db(mesh)
    except KeyError:
        return json.dumps({"error": f"no graph built for mesh '{mesh}'. "
                                    "Call structure_graph first."}, indent=2)
    try:
        cleaned = _assert_read_only_sql(sql)
    except ValueError as e:
        return json.dumps({"error": str(e)}, indent=2)
    try:
        cur = conn.execute(sql)
        columns = [d[0] for d in (cur.description or [])]
        rows = [tuple(_json_safe_cell(c) for c in row)
                for row in cur.fetchall()]
    except Exception as e:
        return json.dumps({"error": f"SQL error: {e}"}, indent=2)
    if "order by" not in cleaned.lower():
        rows.sort(key=lambda r: (r[0] is None, r[0]))
    return json.dumps({"mesh": mesh, "columns": columns, "rows": rows,
                       "row_count": len(rows)}, indent=2)


@mcp.tool()
def slice_mesh(mesh: str = "0", axis: str = "Z", height_cm: float = 0.0,
               units: str = "cm") -> str:
    """
    Slice a mesh body with a plane and return the 2D cross-section loops.

    Fetches the mesh triangle data from Fusion, then runs pure-Python
    triangle-plane intersection: for each triangle the plane-vs-triangle
    intersection segment is computed, the segments are chained into ordered
    closed 2D loops, and each loop is classified as an outer contour (CCW,
    positive shoelace area) or a hole (CW, negative) by containment.

    The plane is axis-aligned: one of X/Y/Z plus a signed height (cm) along
    that axis.  Loop points are in the requested units.

    Stage 3 of the mesh-to-parametric workflow (see get_workflow_guide).

    Args:
        mesh:      Body name or index of a mesh body.
        axis:      Plane normal axis: X, Y, or Z (default Z).
        height_cm: Signed plane height along the axis, in cm (default 0).
        units:     Units for the loop points: mm, cm, or in (default cm).
    """
    axis = str(axis or "Z").strip().upper()
    if axis not in ("X", "Y", "Z"):
        return json.dumps({"error": "Axis must be X, Y, or Z"}, indent=2)
    try:
        factor = _cm_to_unit_factor(units)
    except ValueError as e:
        return json.dumps({"error": str(e)}, indent=2)
    raw = _call("extract_mesh_data", {"mesh": mesh})
    if raw.startswith("Error: "):
        return json.dumps({"error": raw[len("Error: "):]}, indent=2)
    if raw.startswith(("Cannot reach", "Unexpected error")):
        return raw
    try:
        data = json.loads(raw)
    except ValueError:
        return raw
    if "error" in data:
        return json.dumps({"error": data["error"]}, indent=2)
    try:
        mesh_slicer = _load_mesh_slicer()
        result = mesh_slicer.slice_mesh_at(
            data.get("nodes", []), data.get("indices", []),
            {"axis": axis, "height_cm": float(height_cm)})
        result = mesh_slicer.scale_slice(result, factor)
    except Exception as e:
        return json.dumps({"error": f"slice failed: {e}"}, indent=2)
    result["mesh"] = data.get("mesh", mesh)
    result["units"] = units
    return json.dumps(result, indent=2)


def _count_csg_nodes(nodes):
    """Recursively count the nodes in a CSG tree (reconstruction envelope)."""
    total = 0
    for node in nodes or []:
        total += 1
        total += _count_csg_nodes(node.get("children", []))
    return total


def _envelope_reconstruction(result, strategy, method, csg_nodes):
    """Wrap a handler result in the T6 reconstruct_mesh envelope.

    Handler errors pass through verbatim; successes are normalized to
    {"strategy", "method", "bodies", "features", "csg_nodes"}.
    """
    try:
        data = json.loads(result)
    except ValueError:
        return result
    if not isinstance(data, dict):
        return result
    if "error" in data:
        return json.dumps({"error": data["error"]}, indent=2)
    return json.dumps({
        "strategy": strategy,
        "method": data.get("method", method),
        "bodies": data.get("bodies", 0),
        "features": data.get("features", 0),
        "csg_nodes": csg_nodes,
    }, indent=2)


def _envelope_organic(result, operation):
    """Wrap a mesh_convert handler result in the T7 organic envelope.

    Handler errors pass through verbatim; successes become the PREVIEW-caveat
    envelope {"strategy", "method", "preview_api", "parametric", "note"}.
    """
    try:
        data = json.loads(result)
    except ValueError:
        return result
    if not isinstance(data, dict):
        return result
    if "error" in data:
        return json.dumps({"error": data["error"]}, indent=2)
    return json.dumps({
        "strategy": "organic",
        "method": "mesh_convert",
        "preview_api": True,
        "parametric": bool(operation == "parametric"),
        "note": "Organic conversion is a PREVIEW API result, not a parameterized solid.",
    }, indent=2)


@mcp.tool()
def reconstruct_mesh(mesh: str = "0", strategy: str = "auto", units: str = "mm",
                     params: dict = None) -> str:
    """
    Reconstruct a mesh body as parametric CAD features (CSG tree or revolve).

    Fetches the mesh triangle data from Fusion and runs pure-Python
    reconstruction: `prismatic` slices the mesh at interior heights along an
    axis and emits ONE linear_extrude of the constant cross-section (holes
    become polygon paths); `revolved` computes the half cross-section profile
    through the Z-containing plane and revolves it around the Z axis;
    `csg_decompose` fits boxes/cylinders to planar face regions and emits a
    union tree.  The CSG trees / revolve profiles are scaled cm -> units and
    handed to the add-in's create_from_csg_tree / revolve_cross_section
    handlers, which turn them into native Fusion timeline features.

    `organic` uses Fusion's PREVIEW MeshConvertFeature API instead: the mesh
    is converted in-place by Fusion and the result may be a dumb BaseFeature
    body rather than a parameterized solid (see the envelope's `note`).
    PREVIEW CAVEAT: the API is only present on recent Fusion builds and is
    additionally gated by the license; when unavailable the tool returns a
    graceful not-available error instead of a converted body.

    Stage 6 of the mesh-to-parametric workflow (see get_workflow_guide).

    Args:
        mesh:     Body name or index of a mesh body.
        strategy: auto (routes via the analysis recommendation), prismatic,
                  revolved, csg_decompose, or organic (PREVIEW API).
        units:    Units for the reconstructed model: mm, cm, or in (default mm).
        params:   Optional strategy params, e.g. {"axis": "Z",
                  "num_slices": 3, "angle_deg": 360}.  For organic also
                  {"operation": "parametric"|"base"} (default parametric).
    """
    strategy = str(strategy or "auto").strip().lower()
    if strategy not in ("auto", "prismatic", "revolved", "csg_decompose", "organic"):
        return json.dumps({
            "error": f"Unknown strategy '{strategy}'. Supported: auto, "
                     "prismatic, revolved, csg_decompose, organic"}, indent=2)
    try:
        factor = _cm_to_unit_factor(units)
    except ValueError as e:
        return json.dumps({"error": str(e)}, indent=2)
    raw = _call("extract_mesh_data", {"mesh": mesh})
    if raw.startswith("Error: "):
        return json.dumps({"error": raw[len("Error: "):]}, indent=2)
    if raw.startswith(("Cannot reach", "Unexpected error")):
        return raw
    try:
        data = json.loads(raw)
    except ValueError:
        return raw
    if "error" in data:
        return json.dumps({"error": data["error"]}, indent=2)
    params = dict(params or {})
    try:
        chosen = strategy
        if strategy == "auto":
            try:
                mesh_analysis = _load_mesh_analysis()
                report = mesh_analysis.analyze_mesh_data(
                    data.get("nodes", []), data.get("indices", []),
                    data.get("normals", []))
                chosen = report.get("recommended_strategy", "prismatic")
            except Exception:
                chosen = "prismatic"
        if chosen == "organic":
            operation = str(params.get("operation", "parametric") or "parametric").strip().lower()
            result = _call(
                "mesh_convert",
                {"mesh": mesh, "method": "organic", "operation": operation},
                timeout=330)
            return _envelope_organic(result, operation)
        mesh_csg = _load_mesh_csg()
        if chosen == "revolved":
            try:
                profile = mesh_csg.compute_revolved_profile(data, params)
            except Exception as e:
                return json.dumps({"error": f"revolve profile failed: {e}"},
                                  indent=2)
            profile = [[round(x * factor, 6), round(z * factor, 6)]
                       for x, z in profile]
            result = _call(
                "revolve_cross_section",
                {"profile_pts": profile, "angle": params.get("angle", 360.0),
                 "units": units},
                timeout=330)
            return _envelope_reconstruction(result, "revolved", "revolve", 0)
        try:
            tree = mesh_csg.build_csg_tree(data, chosen, params)
        except mesh_csg.NotPrismaticError:
            if strategy != "auto":
                raise
            tree = mesh_csg.build_csg_tree(data, "csg_decompose", params)
            chosen = "csg_decompose"
        tree = mesh_csg.scale_tree(tree, factor)
        result = _call("create_from_csg_tree", {"csg_tree": tree, "units": units},
                       timeout=330)
        return _envelope_reconstruction(result, chosen, "csg_translation",
                                        _count_csg_nodes(tree))
    except Exception as e:
        return json.dumps({"error": f"reconstruction failed: {e}"}, indent=2)


@mcp.tool()
def reconstruct_from_faces(mesh: str = "0", units: str = "mm") -> str:
    """Reconstruct a mesh body using exact face polygon boundaries.

    Analyzes the mesh, extracts planar face decomposition, identifies the
    dominant extrusion direction, and creates native Fusion sketches +
    extrudes from the actual polygon vertices (not box-fitted
    approximations).  Best for prismatic parts with planar faces.

    Args:
        mesh:  Body name or index of a mesh body.
        units: Units for the reconstructed model: mm, cm, or in
               (default mm).
    """
    try:
        factor = _cm_to_unit_factor(units)
    except ValueError as e:
        return json.dumps({"error": str(e)}, indent=2)
    raw = _call("extract_mesh_data", {"mesh": mesh})
    if raw.startswith("Error: "):
        return json.dumps({"error": raw[len("Error: "):]}, indent=2)
    if raw.startswith(("Cannot reach", "Unexpected error")):
        return raw
    try:
        data = json.loads(raw)
    except ValueError:
        return raw
    if "error" in data:
        return json.dumps({"error": data["error"]}, indent=2)
    try:
        mesh_analysis = _load_mesh_analysis()
        report = mesh_analysis.analyze_mesh_data(
            data.get("nodes", []), data.get("indices", []),
            data.get("normals", []))
    except ImportError:
        return json.dumps({
            "error": "Face decomposition requires trimesh. "
                     "Install it with: pip install trimesh numpy"}, indent=2)
    except Exception as e:
        return json.dumps({"error": f"analysis failed: {e}"}, indent=2)

    face_decomp = report.get("face_decomposition") or {}
    planar_faces = face_decomp.get("planar_faces", [])
    if not planar_faces:
        return json.dumps({
            "error": "No planar faces found in face decomposition. "
                     "This tool is best for prismatic parts with flat faces."
        }, indent=2)

    try:
        axis_areas = {0: 0.0, 1: 0.0, 2: 0.0}
        for face in planar_faces:
            normal = face.get("normal", [0, 0, 1])
            area = float(face.get("area", 0))
            abs_n = [abs(c) for c in normal]
            dom_axis = abs_n.index(max(abs_n))
            axis_areas[dom_axis] += area
        dominant_axis = max(axis_areas, key=axis_areas.get)

        def axis_value(vertex):
            return float(vertex[dominant_axis])

        pos_faces = []
        neg_faces = []
        for face in planar_faces:
            normal = face.get("normal", [0, 0, 1])
            abs_n = [abs(c) for c in normal]
            face_dom = abs_n.index(max(abs_n))
            if face_dom != dominant_axis:
                continue
            if normal[dominant_axis] >= 0:
                pos_faces.append(face)
            else:
                neg_faces.append(face)

        def mean_axis(face):
            verts = face.get("vertices", [])
            if not verts:
                return 0.0
            return sum(axis_value(v) for v in verts) / len(verts)

        def best_face(group):
            if not group:
                return None
            return max(group, key=lambda f: float(f.get("area", 0)))

        top_face = best_face(pos_faces)
        bottom_face = best_face(neg_faces)

        bbox = report.get("bounding_box_cm") or report.get("bbox_cm")
        bbox_height = None
        if isinstance(bbox, list) and len(bbox) >= 2:
            try:
                bbox_height = abs(float(bbox[1][dominant_axis]) -
                                 float(bbox[0][dominant_axis]))
            except (IndexError, TypeError):
                pass

        if top_face and bottom_face:
            top_z = mean_axis(top_face)
            bottom_z = mean_axis(bottom_face)
            extrude_height_cm = abs(top_z - bottom_z)
        elif top_face or bottom_face:
            primary = top_face or bottom_face
            primary_z = mean_axis(primary)
            if bbox_height and bbox_height > 0:
                extrude_height_cm = bbox_height
            else:
                verts = primary.get("vertices", [])
                zs = [axis_value(v) for v in verts]
                extrude_height_cm = max(zs) - min(zs) if zs else 1.0
                if extrude_height_cm < 1e-6:
                    extrude_height_cm = 1.0
        else:
            extrude_height_cm = bbox_height or 1.0

        if extrude_height_cm < 1e-6:
            extrude_height_cm = bbox_height or 1.0

        profile_face = top_face or bottom_face or best_face(planar_faces)
        if not profile_face:
            return json.dumps({"error": "Could not select a profile face."}, indent=2)

        axis_map = {0: (1, 2), 1: (0, 2), 2: (0, 1)}
        sketch_axes = axis_map[dominant_axis]
        polygon_scaled = []
        for vertex in profile_face.get("vertices", []):
            sx = float(vertex[sketch_axes[0]]) * factor
            sy = float(vertex[sketch_axes[1]]) * factor
            sz = float(vertex[dominant_axis]) * factor
            polygon_scaled.append([round(sx, 6), round(sy, 6), round(sz, 6)])

        extrude_height_units = extrude_height_cm * factor

        result = _call("create_sketch_from_polygon", {
            "polygons": [polygon_scaled],
            "plane_height": 0,
            "extrude_height": round(extrude_height_units, 6),
            "units": units,
            "operation": "new_body",
        }, timeout=330)
        return _envelope_reconstruction(
            result, "face_reconstruction", "polygon_extrude", 0)
    except Exception as e:
        return json.dumps({"error": f"face reconstruction failed: {e}"}, indent=2)


@mcp.tool()
def compare_mesh_to_brep(mesh: str = "0", body: str = "0") -> str:
    """
    Compare a mesh body against a BRep body (vision-free fidelity QA).

    Fetches both bodies' enclosed volume (cm^3) and bounding-box spans (cm)
    from Fusion and reports the volume ratio mesh/BRep, the max per-axis
    bounding-box span deviation, and a sampled surface deviation: ~200 mesh
    vertices measured to the nearest point on the BRep surface
    (mean/max/samples).  The method used for the sampled deviation is
    reported in the response: "surface_evaluator" when the closest-point API
    is available on the build, "vertex_fallback" otherwise.

    Stage 7 of the mesh-to-parametric workflow (see get_workflow_guide).

    Args:
        mesh: Body name or index of a mesh body.
        body: Body name or index of a BRep body.
    """
    raw = _call("compare_mesh_brep", {"mesh": mesh, "body": body})
    if raw.startswith("Error: "):
        return json.dumps({"error": raw[len("Error: "):]}, indent=2)
    if raw.startswith(("Cannot reach", "Unexpected error")):
        return raw
    try:
        data = json.loads(raw)
    except ValueError:
        return raw
    if "error" in data:
        return json.dumps({"error": data["error"]}, indent=2)
    return json.dumps(data, indent=2)


@mcp.tool()
def select_parameter_schema(object_class: str = "generic",
                            measured_facts: dict = {},
                            units: str = "cm") -> str:
    """
    Select a parameter schema for an object class and bind measured facts.

    Pure-data tool: no Fusion round-trip. Resolves the class schema from the
    local parameter library (falling back to 'generic' with a 'note' for
    unknown classes), binds each role from the measured facts (bbox dims ->
    width/depth/height, slice loop diameter -> diameter, fit params ->
    radius/thickness), and returns stable named parameters with confidence
    scores. Vision-sourced roles are returned as placeholders (value None,
    confidence 0.3) for the model to fill in later.

    Stage 5 of the mesh-to-parametric workflow (see get_workflow_guide).

    Args:
        object_class:   Object class name, e.g. 'bolt', 'gear', 'generic'.
        measured_facts: Measured facts dict, e.g.
                        {"bbox_cm": [4, 4, 3], "slice_diameter_cm": 4.0,
                         "fit_radius_cm": 0.5}. 'bbox_cm' may be dims
                        [w, d, h] or [[min], [max]]; the analyze_mesh report
                        key 'bounding_box_cm' is accepted too.
        units:          Units for the returned values: mm, cm, or in
                        (default cm).
    """
    try:
        _cm_to_unit_factor(units)
    except ValueError as e:
        return json.dumps({"error": str(e)}, indent=2)
    try:
        parameter_schemas = _load_parameter_schemas()
        result = parameter_schemas.select_schema(
            object_class, measured_facts, units=units)
    except Exception as e:
        return json.dumps({"error": f"schema selection failed: {e}"}, indent=2)
    return json.dumps(result, indent=2)


@mcp.tool()
def annotate_mesh_parameters(mesh: str = "0",
                             views: list = ["isometric", "front", "top", "right"],
                             units: str = "cm") -> list:
    """
    Capture 4 viewport screenshots of a mesh + measured facts for vision
    classification (stage 4 of the mesh-to-parametric workflow).

    Fetches the mesh triangle data from Fusion and runs the pure-Python
    analysis (watertightness, volume, bounding box, symmetry, primitive
    hints, recommended strategy) as `measured_facts`, then orients the
    viewport to each requested view (isometric / front / top / right),
    fits the view, and captures a PNG.  Returns a text envelope with the
    mesh name, the 4 base64 views, the measured facts, and the workflow
    pointer -- followed by the 4 decoded PNG image blocks for the model.

    Classification happens on the MODEL side, not here: the model inspects
    the returned views + measured_facts, picks an object class, and calls
    `select_parameter_schema` with that class and the facts. This tool never
    invokes the matcher.

    Stage 4 of the mesh-to-parametric workflow (see get_workflow_guide).

    Args:
        mesh:  Body name or index of a mesh body.
        views: View names to capture, e.g. ['isometric', 'front', 'top',
               'right']. Valid names: isometric, front, top, right.
        units: Units for the measured facts: mm, cm, or in (default cm).
    """
    try:
        factor = _cm_to_unit_factor(units)
    except ValueError as e:
        return json.dumps({"error": str(e)}, indent=2)
    raw = _call("extract_mesh_data", {"mesh": mesh})
    if raw.startswith("Error: "):
        return json.dumps({"error": raw[len("Error: "):]}, indent=2)
    if raw.startswith(("Cannot reach", "Unexpected error")):
        return raw
    try:
        data = json.loads(raw)
    except ValueError:
        return raw
    if "error" in data:
        return json.dumps({"error": data["error"]}, indent=2)
    try:
        mesh_analysis = _load_mesh_analysis()
        report = mesh_analysis.analyze_mesh_data(
            data.get("nodes", []), data.get("indices", []),
            data.get("normals", []))
        report = mesh_analysis.scale_report(report, factor)
    except Exception as e:
        return json.dumps({"error": f"analysis failed: {e}"}, indent=2)
    # INLINE requests.post (NOT _call): _call json.dumps the response into a
    # text string, which would bury the base64 PNG payloads so MCP clients
    # could not display them. Mirror capture_screenshot's pattern exactly.
    try:
        r = requests.post(f"{FUSION_URL}/command",
                          json={"command": "capture_mesh_views",
                                "params": {"mesh": mesh, "views": views}},
                          timeout=60)
        cap = r.json()
    except requests.exceptions.ConnectionError:
        return "Cannot reach Fusion 360. Make sure Fusion is open and the FusionMCP add-in is running."
    except Exception as e:
        return f"Unexpected error: {e}"
    if "error" in cap:
        return json.dumps({"error": cap["error"]}, indent=2)
    envelope = {
        "mesh": data.get("mesh", mesh),
        "views": cap.get("views", []),
        "measured_facts": report,
        "workflow": {
            "stage": "annotate",
            "next": ("MODEL ACTION: classify the object from the views, then "
                     "call select_parameter_schema with object_class and "
                     "measured_facts"),
        },
    }
    text_envelope = json.dumps(envelope, indent=2)
    out = [text_envelope]
    for v in cap.get("views", []):
        b64 = v.get("image_base64", "")
        if b64:
            out.append(Image(data=base64.b64decode(b64), format="png"))
    return out


@mcp.tool()
def review_reconstruction(mesh: str = "0", body: str = "0",
                          views: list = ["isometric", "front", "top"]) -> list:
    """
    Capture side-by-side mesh vs reconstructed BRep views for vision QA review.

    Captures viewport screenshots of the ORIGINAL mesh and the RECONSTRUCTED
    BRep body for each requested view (isometric / front / top / right) and
    pairs them per view. Returns a text envelope with the per-view base64
    image pairs, a local compare_mesh_to_brep geometry summary, and the
    workflow pointer -- followed by the decoded PNG image blocks interleaved
    per view (mesh image, then brep image).

    The comparison happens on the MODEL side, not here: the model inspects
    each mesh/brep pair + geometry summary, decides whether features are
    missing, and either calls `reconstruct_mesh` again with feedback or
    accepts with `select_parameter_schema`.

    Stage 8 of the mesh-to-parametric workflow (see get_workflow_guide).

    Args:
        mesh:  Body name or index of the ORIGINAL mesh body.
        body:  Body name or index of the RECONSTRUCTED BRep body.
        views: View names to capture, e.g. ['isometric', 'front', 'top'].
               Valid names: isometric, front, top, right.
    """
    # INLINE requests.post for BOTH captures (NOT _call): _call json.dumps the
    # response into a text string, which would bury the base64 PNG payloads so
    # MCP clients could not display them. Mirror annotate_mesh_parameters.
    try:
        r = requests.post(f"{FUSION_URL}/command",
                          json={"command": "capture_mesh_views",
                                "params": {"mesh": mesh, "views": views}},
                          timeout=60)
        mesh_cap = r.json()
    except requests.exceptions.ConnectionError:
        return "Cannot reach Fusion 360. Make sure Fusion is open and the FusionMCP add-in is running."
    except Exception as e:
        return f"Unexpected error: {e}"
    if "error" in mesh_cap:
        return json.dumps({"error": mesh_cap["error"]}, indent=2)
    try:
        r = requests.post(f"{FUSION_URL}/command",
                          json={"command": "capture_body_views",
                                "params": {"body": body, "views": views}},
                          timeout=60)
        brep_cap = r.json()
    except requests.exceptions.ConnectionError:
        return "Cannot reach Fusion 360. Make sure Fusion is open and the FusionMCP add-in is running."
    except Exception as e:
        return f"Unexpected error: {e}"
    if "error" in brep_cap:
        return json.dumps({"error": brep_cap["error"]}, indent=2)
    # Geometry summary from the LOCAL compare_mesh_to_brep (plain call, same
    # module). On error the images remain the primary payload -- "geometry"
    # carries {"error": ...} instead of failing the whole tool.
    try:
        geometry = json.loads(compare_mesh_to_brep(mesh=mesh, body=body))
    except Exception as e:
        geometry = {"error": f"geometry comparison failed: {e}"}
    mesh_views = {v.get("view"): v.get("image_base64", "")
                  for v in mesh_cap.get("views", [])}
    brep_views = {v.get("view"): v.get("image_base64", "")
                  for v in brep_cap.get("views", [])}
    pairs = []
    for v in views:
        pairs.append({
            "view": v,
            "mesh_image_base64": mesh_views.get(v, ""),
            "brep_image_base64": brep_views.get(v, ""),
        })
    envelope = {
        "pairs": pairs,
        "geometry": geometry,
        "workflow": {
            "stage": "review",
            "next": ("MODEL ACTION: compare each pair; if features are missing "
                     "call reconstruct_mesh again with feedback or accept with "
                     "select_parameter_schema"),
        },
    }
    text_envelope = json.dumps(envelope, indent=2)
    out = [text_envelope]
    for v in views:
        for cap in (mesh_cap, brep_cap):
            for vv in cap.get("views", []):
                if vv.get("view") == v and vv.get("image_base64"):
                    out.append(
                        Image(data=base64.b64decode(vv["image_base64"]),
                              format="png"))
    return out


@mcp.tool()
def get_workflow_guide(step: str = "") -> str:
    """
    Return the mesh-to-parametric workflow guide (8 ordered steps).

    Pure-data tool: no Fusion round-trip. With no step (default "") the FULL
    guide is returned as JSON: import -> analyze -> slice -> annotate ->
    select_parameter_schema -> reconstruct -> compare -> review. Each step
    carries its tool name, purpose, expected inputs/outputs, the MODEL ACTION
    the assistant must take (annotate: classify the object; review: compare
    each pair and accept or re-run), the strategy branch at reconstruct, and
    fallbacks. Pass a step name (e.g. "reconstruct" or the tool name
    "reconstruct_mesh") to get that single step.

    Stage 1 of the mesh-to-parametric workflow (see get_workflow_guide).

    Args:
        step: Step name to look up, e.g. "annotate" or "reconstruct_mesh".
              Empty string (the default) returns the full guide.
    """
    step = str(step or "").strip()
    try:
        workflow_guide = _load_workflow_guide()
    except Exception as e:
        return json.dumps({"error": f"workflow guide load failed: {e}"},
                          indent=2)
    if step == "":
        return workflow_guide.GUIDE_JSON
    found = workflow_guide.get_step(step)
    if found is None:
        return json.dumps({"error": f"Unknown workflow step '{step}'"},
                          indent=2)
    return json.dumps(found, indent=2)


@mcp.tool()
def create_from_scad(code: str, units: str = "mm", fallback_to_mesh: bool = True) -> str:
    """
    Translate OpenSCAD/BOSL2 code into native parametric Fusion features.

    Resolves the .scad source into a CSG tree (openscad-evaluator primary path,
    OpenSCAD-binary CSG export fallback) in this server process, then sends the
    tree to Fusion, where it becomes real BRep bodies via native sketch/extrude/
    revolve/combine features (visible in the timeline). If a node cannot be
    translated to native features and fallback_to_mesh is True, any partially
    created bodies are deleted and the whole model is rendered as a mesh body
    instead (same path as run_scad).

    Args:
        code:             Raw OpenSCAD source, e.g.
                          'difference() { cube([20,20,20]); cylinder(r=5, h=30); }'
                          or 'include <BOSL2/std.scad>\\ncuboid([10,10,10]);'.
        units:            Units of the OpenSCAD model: mm, cm, or in.
        fallback_to_mesh: If a CSG node is unsupported, delete partial bodies
                          and fall back to a mesh render (True) or return an
                          error listing the unsupported nodes (False).
    """
    try:
        translator = _load_scad_translator()
        bosl2_path = _get_bosl2_path()
        openscad_path = _get_openscad_path()
    except Exception as e:
        return f"Error: {e}"
    try:
        csg_tree = translator.resolve_scad(
            code, openscad_path=openscad_path, bosl2_path=bosl2_path)
    except Exception as e:
        return f"Error: SCAD resolution failed: {e}"
    return _call("create_from_scad", {
        "csg_tree": csg_tree, "code": code,
        "units": units, "fallback_to_mesh": fallback_to_mesh,
    }, timeout=330)


# ---- History & File ----

@mcp.tool()
def undo(steps: int = 1) -> str:
    """Undo the last action(s). Args: steps = how many to undo."""
    return _call("undo", {"steps": steps})

@mcp.tool()
def redo(steps: int = 1) -> str:
    """Redo previously undone actions. Args: steps = how many to redo."""
    return _call("redo", {"steps": steps})

@mcp.tool()
def save_design(description: str = "Saved") -> str:
    """Save the current design. Use save_as first if the document is brand new."""
    return _call("save", {"description": description})

@mcp.tool()
def save_as(name: str = "My Design", description: str = "Saved") -> str:
    """
    Save document with a new name.

    Args:
        name:        New document name.
        description: Version description.
    """
    return _call("save_as", {"name": name, "description": description})


# ---- Entry Point ----

if __name__ == "__main__":
    print("Fusion 360 MCP Server starting...")
    print(f"Connecting to Fusion add-in at {FUSION_URL}")
    print("Waiting for MCP client to connect via stdio...")
    mcp.run(transport="stdio")
