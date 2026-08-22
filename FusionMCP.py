"""
FusionMCP Add-in

Runs inside Autodesk Fusion 360 as an add-in. Starts an HTTP server on
localhost:7432 that receives commands from the MCP server and dispatches
them to the Fusion 360 API on the main thread via custom events.
"""

import adsk.core
import adsk.fusion
import base64
import traceback
import threading
import json
import queue
import uuid
import os
import math
import subprocess
import tempfile
import time
from http.server import HTTPServer, BaseHTTPRequestHandler

app: adsk.core.Application = None
ui: adsk.core.UserInterface = None
_server: HTTPServer = None
_server_thread: threading.Thread = None
_handlers = []
_cmd_queue: queue.Queue = queue.Queue()
_results: dict = {}
_result_events: dict = {}

CUSTOM_EVENT_ID = "FusionMCPTickV4"
PORT = 7432
ADDIN_CMD_TIMEOUT = 300


# ---- HTTP Handler ----

class MCPRequestHandler(BaseHTTPRequestHandler):
    def log_message(self, format, *args):
        pass

    def do_GET(self):
        if self.path == "/ping":
            self._respond(200, {"status": "ok", "message": "Fusion MCP bridge running"})
        else:
            self._respond(404, {"error": "Not found"})

    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)
        try:
            data = json.loads(body)
        except Exception:
            self._respond(400, {"error": "Invalid JSON"})
            return
        self._respond(200, self._dispatch(data))

    def _dispatch(self, data: dict) -> dict:
        cmd_id = str(uuid.uuid4())
        data["id"] = cmd_id
        event = threading.Event()
        _result_events[cmd_id] = event
        _cmd_queue.put(data)
        if app:
            app.fireCustomEvent(CUSTOM_EVENT_ID, cmd_id)
        event.wait(timeout=ADDIN_CMD_TIMEOUT)
        result = _results.pop(cmd_id, {"error": f"Timeout - Fusion did not respond in {ADDIN_CMD_TIMEOUT}s"})
        _result_events.pop(cmd_id, None)
        return result

    def _respond(self, code: int, body: dict):
        payload = json.dumps(body).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


# ---- Helpers ----

def _design():
    return adsk.fusion.Design.cast(app.activeProduct)

def _root():
    return _design().rootComponent

def _get_std_plane(root, name: str):
    n = name.upper()
    if n == "XZ": return root.xZConstructionPlane
    if n == "YZ": return root.yZConstructionPlane
    return root.xYConstructionPlane

def _resolve_plane(root, name: str):
    for i in range(root.constructionPlanes.count):
        cp = root.constructionPlanes.item(i)
        if cp.name.upper() == name.upper():
            return cp
    return _get_std_plane(root, name)

def _last_sketch(root):
    if root.sketches.count == 0:
        raise Exception("No sketch exists. Create one first.")
    return root.sketches.item(root.sketches.count - 1)

def _resolve_sketch(root, p):
    ref = p.get("sketch", "")
    if ref == "" or ref is None:
        return _last_sketch(root)
    return _find_sketch(root, ref)

def _find_body(root, ref):
    bodies = root.bRepBodies
    if isinstance(ref, int) or (isinstance(ref, str) and str(ref).isdigit()):
        idx = int(ref)
        if idx < bodies.count:
            return bodies.item(idx)
    for i in range(bodies.count):
        if bodies.item(i).name == ref:
            return bodies.item(i)
    if bodies.count > 0:
        return bodies.item(0)
    raise Exception("No bodies found in design.")

def _find_sketch(root, ref):
    sketches = root.sketches
    if isinstance(ref, int) or (isinstance(ref, str) and str(ref).isdigit()):
        idx = int(ref)
        if idx < sketches.count:
            return sketches.item(idx)
    for i in range(sketches.count):
        if sketches.item(i).name == ref:
            return sketches.item(i)
    raise Exception(f"Sketch '{ref}' not found.")

def _find_component(root, name):
    design = _design()
    if not name:
        return root
    for comp in design.allComponents:
        if comp.name == name:
            return comp
    raise Exception(f"Component '{name}' not found.")

def _op(name):
    ops = {
        "new_body": adsk.fusion.FeatureOperations.NewBodyFeatureOperation,
        "join":     adsk.fusion.FeatureOperations.JoinFeatureOperation,
        "cut":      adsk.fusion.FeatureOperations.CutFeatureOperation,
        "new_component": adsk.fusion.FeatureOperations.NewComponentFeatureOperation,
        "intersect": adsk.fusion.FeatureOperations.IntersectFeatureOperation,
    }
    return ops.get(name.lower(), adsk.fusion.FeatureOperations.NewBodyFeatureOperation)

def _bool_op(name):
    ops = {
        "join":      adsk.fusion.BooleanTypes.JoinBooleanType,
        "cut":       adsk.fusion.BooleanTypes.CutBooleanType,
        "intersect": adsk.fusion.BooleanTypes.IntersectBooleanType,
    }
    return ops.get(name.lower(), adsk.fusion.BooleanTypes.JoinBooleanType)

def _axis_vector(name):
    n = name.upper()
    if n == "X": return adsk.core.Vector3D.create(1, 0, 0)
    if n == "Y": return adsk.core.Vector3D.create(0, 1, 0)
    return adsk.core.Vector3D.create(0, 0, 1)

def _construction_axis(root, name):
    n = name.upper()
    if n == "X": return root.xConstructionAxis
    if n == "Y": return root.yConstructionAxis
    return root.zConstructionAxis

def _collect_all_sketch_curves(sketch):
    curves = adsk.core.ObjectCollection.create()
    for i in range(sketch.sketchCurves.sketchLines.count):
        curves.add(sketch.sketchCurves.sketchLines.item(i))
    for i in range(sketch.sketchCurves.sketchCircles.count):
        curves.add(sketch.sketchCurves.sketchCircles.item(i))
    for i in range(sketch.sketchCurves.sketchArcs.count):
        curves.add(sketch.sketchCurves.sketchArcs.item(i))
    for i in range(sketch.sketchCurves.sketchFittedSplines.count):
        curves.add(sketch.sketchCurves.sketchFittedSplines.item(i))
    for i in range(sketch.sketchCurves.sketchEllipses.count):
        curves.add(sketch.sketchCurves.sketchEllipses.item(i))
    return curves


# ---- Info Commands ----

def _get_info(design, root):
    comps = [{"name": c.name, "id": c.id} for c in design.allComponents]
    sketches = [{"index": i, "name": root.sketches.item(i).name,
                 "profiles": root.sketches.item(i).profiles.count}
                for i in range(root.sketches.count)]
    bodies = [{"index": i, "name": root.bRepBodies.item(i).name,
               "faces": root.bRepBodies.item(i).faces.count}
              for i in range(root.bRepBodies.count)]
    mesh_bodies = []
    for j in range(root.meshBodies.count):
        m = root.meshBodies.item(j)
        try:
            triangles = m.mesh.triangleCount
        except:
            triangles = None
        mesh_bodies.append({"index": j, "name": m.name,
                            "triangles": triangles})
    params = [{"name": design.allParameters.item(i).name,
               "value": round(design.allParameters.item(i).value, 4),
               "unit": design.allParameters.item(i).unit}
              for i in range(design.allParameters.count)]
    planes = [{"index": i, "name": root.constructionPlanes.item(i).name}
              for i in range(root.constructionPlanes.count)]
    joints = []
    try:
        for i in range(root.joints.count):
            j = root.joints.item(i)
            joints.append({"index": i, "name": j.name, "type": str(j.jointMotion.jointType)})
    except:
        pass
    return {
        "document": app.activeDocument.name,
        "components": comps, "sketches": sketches,
        "bodies": bodies, "mesh_bodies": mesh_bodies,
        "parameters": params,
        "construction_planes": planes, "joints": joints,
    }

def _get_bodies_info(root):
    bodies = []
    for i in range(root.bRepBodies.count):
        b = root.bRepBodies.item(i)
        bb = b.boundingBox
        bodies.append({
            "index": i, "name": b.name, "type": "brep",
            "visible": b.isVisible,
            "faces": b.faces.count, "edges": b.edges.count,
            "size_cm": {
                "x": round(bb.maxPoint.x - bb.minPoint.x, 4),
                "y": round(bb.maxPoint.y - bb.minPoint.y, 4),
                "z": round(bb.maxPoint.z - bb.minPoint.z, 4),
            },
        })
    for j in range(root.meshBodies.count):
        m = root.meshBodies.item(j)
        bb = m.boundingBox
        try:
            payload = m.mesh
            triangles = payload.triangleCount
            vertices = payload.nodeCount
        except:
            triangles = None
            vertices = None
        bodies.append({
            "index": j, "name": m.name, "type": "mesh",
            "visible": m.isVisible,
            "triangles": triangles, "vertices": vertices,
            "size_cm": {
                "x": round(bb.maxPoint.x - bb.minPoint.x, 4),
                "y": round(bb.maxPoint.y - bb.minPoint.y, 4),
                "z": round(bb.maxPoint.z - bb.minPoint.z, 4),
            },
        })
    return {"bodies": bodies}

def _get_face_info(root, p):
    body = _find_body(root, p.get("body", 0))
    faces = []
    for i in range(body.faces.count):
        f = body.faces.item(i)
        geo_type = type(f.geometry).__name__
        try:
            n = f.geometry.normal
            normal = [round(n.x, 3), round(n.y, 3), round(n.z, 3)]
        except:
            normal = None
        faces.append({"index": i, "area_cm2": round(f.area, 4),
                       "type": geo_type, "normal": normal})
    return {"body": body.name, "faces": faces}

def _get_edge_info(root, p):
    body = _find_body(root, p.get("body", 0))
    edges = []
    for i in range(body.edges.count):
        e = body.edges.item(i)
        geo_type = type(e.geometry).__name__
        edges.append({"index": i, "length_cm": round(e.length, 4), "type": geo_type})
    return {"body": body.name, "edges": edges}

def _get_sketch_info(root, p):
    sketch = _find_sketch(root, p.get("sketch", 0))
    lines = [{"index": i, "start": [round(l.startSketchPoint.geometry.x, 4),
                                      round(l.startSketchPoint.geometry.y, 4)],
              "end": [round(l.endSketchPoint.geometry.x, 4),
                       round(l.endSketchPoint.geometry.y, 4)],
              "length": round(l.length, 4),
              "isConstruction": l.isConstruction}
             for i, l in enumerate(sketch.sketchCurves.sketchLines)]
    circles = [{"index": i, "center": [round(c.centerSketchPoint.geometry.x, 4),
                                         round(c.centerSketchPoint.geometry.y, 4)],
                "radius": round(c.radius, 4)}
               for i, c in enumerate(sketch.sketchCurves.sketchCircles)]
    arcs = [{"index": i, "center": [round(a.centerSketchPoint.geometry.x, 4),
                                      round(a.centerSketchPoint.geometry.y, 4)],
             "radius": round(a.radius, 4)}
            for i, a in enumerate(sketch.sketchCurves.sketchArcs)]
    profiles = sketch.profiles.count
    constraints = []
    for i in range(sketch.geometricConstraints.count):
        c = sketch.geometricConstraints.item(i)
        constraints.append({"index": i, "type": type(c).__name__})
    dimensions = []
    for i in range(sketch.sketchDimensions.count):
        d = sketch.sketchDimensions.item(i)
        dimensions.append({"index": i, "type": type(d).__name__,
                            "value": round(d.value, 4) if hasattr(d, 'value') else None})
    return {"sketch": sketch.name, "profiles": profiles,
            "lines": lines, "circles": circles, "arcs": arcs,
            "constraints": constraints, "dimensions": dimensions}

def _get_timeline_info():
    design = _design()
    timeline = design.timeline
    items = []
    for i in range(timeline.count):
        item = timeline.item(i)
        items.append({
            "index": i, "name": item.entity.name if hasattr(item.entity, 'name') else str(i),
            "type": type(item.entity).__name__,
            "isSuppressed": item.isSuppressed,
            "isRolledBack": item.isRolledBack,
        })
    return {"timeline_count": timeline.count, "items": items}

def _measure_body(root, p):
    body = _find_body(root, p.get("body", 0))
    bb = body.boundingBox
    vol = None
    try:
        props = body.physicalProperties
        vol = round(props.volume, 6)
    except:
        pass
    return {
        "body": body.name,
        "size_cm": {
            "x": round(bb.maxPoint.x - bb.minPoint.x, 4),
            "y": round(bb.maxPoint.y - bb.minPoint.y, 4),
            "z": round(bb.maxPoint.z - bb.minPoint.z, 4),
        },
        "volume_cm3": vol,
        "faces": body.faces.count,
        "edges": body.edges.count,
        "vertices": body.vertices.count,
    }

def _measure_between(root, p):
    e1 = p.get("entity1", "")
    e2 = p.get("entity2", "")

    def parse_entity(spec):
        parts = spec.split(":")
        if len(parts) >= 2 and parts[0] == "body":
            body = _find_body(root, parts[1])
            if len(parts) >= 4 and parts[2] == "face":
                return body.faces.item(int(parts[3]))
            if len(parts) >= 4 and parts[2] == "edge":
                return body.edges.item(int(parts[3]))
            return body
        raise Exception(f"Cannot parse entity: {spec}")

    ent1 = parse_entity(e1)
    ent2 = parse_entity(e2)
    mgr = app.measureManager
    result = mgr.measureMinimumDistance(ent1, ent2)
    return {
        "distance_cm": round(result.value, 6),
        "point1": [round(result.pointOne.x, 4), round(result.pointOne.y, 4), round(result.pointOne.z, 4)],
        "point2": [round(result.pointTwo.x, 4), round(result.pointTwo.y, 4), round(result.pointTwo.z, 4)],
    }


# ---- Dimensioning & Inspection Tools ----

def _require_body(root, ref) -> "adsk.fusion.BRepBody":
    bodies = root.bRepBodies
    if isinstance(ref, int) or (isinstance(ref, str) and str(ref).isdigit()):
        idx = int(ref)
        if 0 <= idx < bodies.count:
            return bodies.item(idx)
        raise Exception(f"Body '{ref}' not found.")
    for i in range(bodies.count):
        if bodies.item(i).name == ref:
            return bodies.item(i)
    raise Exception(f"Body '{ref}' not found.")


def _round_xyz(pt) -> list:
    return [round(pt.x, 6), round(pt.y, 6), round(pt.z, 6)]


def _physical_properties_dict(body) -> dict:
    props = body.physicalProperties
    result = {
        "volume_cm3": round(props.volume, 6),
        "area_cm2": round(props.area, 6),
        "mass_kg": round(props.mass, 6),
        "density_kg_cm3": round(props.density, 6),
    }
    com = props.centerOfMass
    result["center_of_mass"] = _round_xyz(com) if com else None

    try:
        vals = props.getXYZMomentsOfInertia()
        vals = vals[1:] if len(vals) == 7 else vals  # strip leading success flag
        xx, yy, zz, xy, yz, xz = vals
        result["moments_of_inertia"] = {
            "Ixx": round(xx, 6), "Iyy": round(yy, 6), "Izz": round(zz, 6),
            "Ixy": round(xy, 6), "Iyz": round(yz, 6), "Ixz": round(xz, 6),
        }
    except Exception:
        result["moments_of_inertia"] = None

    try:
        moms = props.getPrincipalMomentsOfInertia()
        moms = moms[1:] if len(moms) == 4 else moms
        i1, i2, i3 = moms
        axes = props.getPrincipalAxes()
        axes = axes[1:] if len(axes) == 4 else axes
        axis1, axis2, axis3 = axes
        result["principal_axes"] = {
            "I1": round(i1, 6), "I2": round(i2, 6), "I3": round(i3, 6),
            "axis1": _round_xyz(axis1), "axis2": _round_xyz(axis2), "axis3": _round_xyz(axis3),
        }
    except Exception:
        result["principal_axes"] = None
    return result


def _get_physical_properties(root, p: dict) -> dict:
    body = _require_body(root, p.get("body", 0))
    result = _physical_properties_dict(body)
    result["body"] = body.name
    return result


def _parse_entity_spec(root, spec: str):
    parts = spec.split(":")
    if len(parts) >= 2 and parts[0] == "body":
        body = _require_body(root, parts[1])
        if len(parts) >= 4 and parts[2] == "face":
            return body.faces.item(int(parts[3]))
        if len(parts) >= 4 and parts[2] == "edge":
            return body.edges.item(int(parts[3]))
        return body
    raise Exception(f"Cannot parse entity: {spec}")


def _measure_angle(root, p: dict) -> dict:
    e1 = p.get("entity1", "")
    e2 = p.get("entity2", "")
    try:
        ent1 = _parse_entity_spec(root, e1)
        ent2 = _parse_entity_spec(root, e2)
    except Exception as e:
        return {"error": str(e)}
    if e1.count(":") < 3 or e2.count(":") < 3:
        return {"error": "measure_angle requires face or edge entities. Use 'body:0:face:N' or 'body:0:edge:N' specifiers."}
    try:
        result = app.measureManager.measureAngle(ent1, ent2)
    except Exception as e:
        return {"error": f"Could not measure angle: {e}"}
    rad = result.value
    return {"angle_deg": round(math.degrees(rad), 6), "angle_rad": round(rad, 6)}


def _strict_axis_vector(name) -> "adsk.core.Vector3D":
    n = str(name).upper()
    if n == "X":
        return adsk.core.Vector3D.create(1, 0, 0)
    if n == "Y":
        return adsk.core.Vector3D.create(0, 1, 0)
    if n == "Z":
        return adsk.core.Vector3D.create(0, 0, 1)
    raise Exception(f"Axis must be X, Y, or Z (got '{name}')")


def _get_oriented_bounding_box(root, p: dict) -> dict:
    body = _require_body(root, p.get("body", 0))
    try:
        length_vec = _strict_axis_vector(p.get("length_axis", "X"))
        width_vec = _strict_axis_vector(p.get("width_axis", "Y"))
    except Exception as e:
        return {"error": str(e)}
    try:
        obb = app.measureManager.getOrientedBoundingBox(body, length_vec, width_vec)
    except Exception as e:
        return {"error": f"Could not compute oriented bounding box: {e}"}
    center = obb.centerPoint
    return {
        "body": body.name,
        "center": [round(center.x, 6), round(center.y, 6), round(center.z, 6)],
        "length_cm": round(obb.length, 6),
        "width_cm": round(obb.width, 6),
        "height_cm": round(obb.height, 6),
    }


def _class_short_name(obj) -> str:
    try:
        full = obj.classType()
    except Exception:
        full = type(obj).__name__
    return full.split("::")[-1]


def _face_normal(face):
    try:
        ev = face.evaluator
        okp, param = ev.getParameterAtPoint(face.pointOnFace)
        if not okp:
            return None
        okn, normal = ev.getNormalAtParameter(param)
        if not okn:
            return None
        return _round_xyz(normal)
    except Exception:
        return None


def _inspect_body(root, p: dict) -> dict:
    body = _require_body(root, p.get("body", 0))
    detail = str(p.get("detail", "summary")).lower()
    max_items = int(p.get("max_items", 100))

    bb = body.boundingBox
    result = {
        "body": body.name,
        "bounding_box": {
            "min": _round_xyz(bb.minPoint),
            "max": _round_xyz(bb.maxPoint),
            "size_cm": {
                "x": round(bb.maxPoint.x - bb.minPoint.x, 6),
                "y": round(bb.maxPoint.y - bb.minPoint.y, 6),
                "z": round(bb.maxPoint.z - bb.minPoint.z, 6),
            },
        },
        "physical_properties": _physical_properties_dict(body),
        "total_faces": body.faces.count,
        "total_edges": body.edges.count,
        "total_vertices": body.vertices.count,
    }
    if detail != "full":
        return result

    faces, edges, vertices = [], [], []
    truncated = False
    for i in range(body.faces.count):
        if i >= max_items:
            truncated = True
            break
        f = body.faces.item(i)
        stype = _class_short_name(f.geometry)
        entry = {"index": i, "area_cm2": round(f.area, 6), "surface_type": stype}
        normal = _face_normal(f)
        if normal is not None:
            entry["normal"] = normal
        if "Cylinder" in stype:
            try:
                entry["geometry_params"] = {"radius_cm": round(f.geometry.radius, 6)}
            except Exception:
                pass
        faces.append(entry)

    for i in range(body.edges.count):
        if i >= max_items:
            truncated = True
            break
        e = body.edges.item(i)
        ctype = _class_short_name(e.geometry)
        entry = {"index": i, "length_cm": round(e.length, 6), "curve_type": ctype}
        if "Circle" in ctype:
            try:
                entry["radius_cm"] = round(e.geometry.radius, 6)
            except Exception:
                pass
        edges.append(entry)

    for i in range(body.vertices.count):
        if i >= max_items:
            truncated = True
            break
        v = body.vertices.item(i)
        vertices.append({"index": i, "point": _round_xyz(v.geometry)})

    result["faces"] = faces
    result["edges"] = edges
    result["vertices"] = vertices
    result["truncated"] = truncated
    return result


# ---- Execute Script ----

def _execute_script(p):
    code = p.get("code", "")
    if not code.strip():
        return {"error": "No code provided"}
    design = _design()
    root = design.rootComponent
    result = {"output": None}
    local_vars = {
        "app": app, "ui": ui, "design": design, "root": root,
        "adsk": __import__("adsk"),
        "result": result,
        "math": math, "json": json,
    }
    try:
        local_vars["__builtins__"] = __builtins__
        exec(code, local_vars, local_vars)
        output = local_vars.get("result", result).get("output", None)
        return {"success": True, "output": output}
    except Exception:
        return {"error": traceback.format_exc()}


# ---- Document Management ----

def _create_new_document(p):
    doc = app.documents.add(adsk.core.DocumentTypes.FusionDesignDocumentType)
    name = p.get("name", "Untitled")
    return {"created": True, "document": name}

def _clear_design():
    design = _design()
    root = design.rootComponent
    timeline = design.timeline
    for i in range(timeline.count - 1, -1, -1):
        try:
            timeline.item(i).entity.deleteMe()
        except:
            pass
    while root.sketches.count > 0:
        try:
            root.sketches.item(0).deleteMe()
        except:
            break
    while root.constructionPlanes.count > 0:
        try:
            root.constructionPlanes.item(0).deleteMe()
        except:
            break
    return {"cleared": True}


# ---- Sketch Commands ----

def _create_sketch(root, p):
    comp = _find_component(root, p.get("component", ""))
    plane = _resolve_plane(comp, p.get("plane", "XY"))
    sketch = comp.sketches.add(plane)
    name = p.get("name", "")
    if name:
        sketch.name = name
    return {"sketch": sketch.name, "plane": p.get("plane", "XY")}

def _create_sketch_on_face(root, p):
    body = _find_body(root, p.get("body", 0))
    face_index = int(p.get("face_index", 0))
    if face_index >= body.faces.count:
        return {"error": f"Face {face_index} out of range ({body.faces.count} faces)"}
    face = body.faces.item(face_index)
    sketch = root.sketches.add(face)
    name = p.get("name", "")
    if name:
        sketch.name = name
    return {"sketch": sketch.name, "on_body": body.name, "face_index": face_index}

def _finish_sketch(root, p):
    sketch = _resolve_sketch(root, p)
    name = sketch.name
    sketch.isComputeDeferred = False
    return {"finished": name, "profiles": sketch.profiles.count}

def _delete_sketch(root, p):
    sketch = _find_sketch(root, p.get("sketch", 0))
    name = sketch.name
    sketch.deleteMe()
    return {"deleted_sketch": name}

def _draw_rectangle(root, p):
    sketch = _resolve_sketch(root, p)
    x1, y1 = float(p.get("x1", 0)), float(p.get("y1", 0))
    x2, y2 = float(p.get("x2", 10)), float(p.get("y2", 10))
    sketch.sketchCurves.sketchLines.addTwoPointRectangle(
        adsk.core.Point3D.create(x1, y1, 0),
        adsk.core.Point3D.create(x2, y2, 0))
    return {"sketch": sketch.name, "rectangle": f"({x1},{y1}) to ({x2},{y2})",
            "profiles": sketch.profiles.count}

def _draw_center_rectangle(root, p):
    sketch = _resolve_sketch(root, p)
    cx, cy = float(p.get("cx", 0)), float(p.get("cy", 0))
    w, h = float(p.get("width", 10)), float(p.get("height", 10))
    x1, y1 = cx - w / 2, cy - h / 2
    x2, y2 = cx + w / 2, cy + h / 2
    sketch.sketchCurves.sketchLines.addTwoPointRectangle(
        adsk.core.Point3D.create(x1, y1, 0),
        adsk.core.Point3D.create(x2, y2, 0))
    return {"sketch": sketch.name, "center_rectangle": f"center=({cx},{cy}), {w}x{h}",
            "profiles": sketch.profiles.count}

def _draw_circle(root, p):
    sketch = _resolve_sketch(root, p)
    cx, cy, r = float(p.get("cx", 0)), float(p.get("cy", 0)), float(p.get("radius", 5))
    sketch.sketchCurves.sketchCircles.addByCenterRadius(
        adsk.core.Point3D.create(cx, cy, 0), r)
    return {"sketch": sketch.name, "circle": f"center=({cx},{cy}), r={r}",
            "profiles": sketch.profiles.count}

def _draw_line(root, p):
    sketch = _resolve_sketch(root, p)
    x1, y1 = float(p.get("x1", 0)), float(p.get("y1", 0))
    x2, y2 = float(p.get("x2", 10)), float(p.get("y2", 10))
    sketch.sketchCurves.sketchLines.addByTwoPoints(
        adsk.core.Point3D.create(x1, y1, 0),
        adsk.core.Point3D.create(x2, y2, 0))
    return {"sketch": sketch.name, "line": f"({x1},{y1}) to ({x2},{y2})"}

def _draw_arc(root, p):
    sketch = _resolve_sketch(root, p)
    cx, cy = float(p.get("cx", 0)), float(p.get("cy", 0))
    r = float(p.get("radius", 5))
    start_angle = float(p.get("start_angle", 0))
    sweep_angle = float(p.get("sweep_angle", 90))
    center = adsk.core.Point3D.create(cx, cy, 0)
    start_pt = adsk.core.Point3D.create(
        cx + r * math.cos(math.radians(start_angle)),
        cy + r * math.sin(math.radians(start_angle)), 0)
    sketch.sketchCurves.sketchArcs.addByCenterStartSweep(
        center, start_pt, math.radians(sweep_angle))
    return {"sketch": sketch.name, "arc": f"center=({cx},{cy}), r={r}, sweep={sweep_angle}deg"}

def _draw_polygon(root, p):
    sketch = _resolve_sketch(root, p)
    cx, cy = float(p.get("cx", 0)), float(p.get("cy", 0))
    r = float(p.get("radius", 5))
    sides = int(p.get("sides", 6))
    points = [adsk.core.Point3D.create(
        cx + r * math.cos(math.radians(360.0 / sides * i)),
        cy + r * math.sin(math.radians(360.0 / sides * i)), 0)
        for i in range(sides)]
    lines = sketch.sketchCurves.sketchLines
    for i in range(sides):
        lines.addByTwoPoints(points[i], points[(i + 1) % sides])
    return {"sketch": sketch.name, "polygon": f"{sides}-sided, center=({cx},{cy}), r={r}",
            "profiles": sketch.profiles.count}

def _draw_ellipse(root, p):
    sketch = _resolve_sketch(root, p)
    cx, cy = float(p.get("cx", 0)), float(p.get("cy", 0))
    rx, ry = float(p.get("rx", 5)), float(p.get("ry", 3))
    center = adsk.core.Point3D.create(cx, cy, 0)
    major = adsk.core.Point3D.create(cx + rx, cy, 0)
    minor_pt = adsk.core.Point3D.create(cx, cy + ry, 0)
    sketch.sketchCurves.sketchEllipses.add(center, major, minor_pt)
    return {"sketch": sketch.name, "ellipse": f"center=({cx},{cy}), rx={rx}, ry={ry}",
            "profiles": sketch.profiles.count}

def _draw_spline(root, p):
    sketch = _resolve_sketch(root, p)
    raw_pts = p.get("points", [[0, 0], [5, 5], [10, 0]])
    pts = adsk.core.ObjectCollection.create()
    for pt in raw_pts:
        pts.add(adsk.core.Point3D.create(float(pt[0]), float(pt[1]), 0))
    sketch.sketchCurves.sketchFittedSplines.add(pts)
    return {"sketch": sketch.name, "spline": f"{len(raw_pts)} control points"}

def _draw_slot(root, p):
    sketch = _resolve_sketch(root, p)
    x1, y1 = float(p.get("x1", 0)), float(p.get("y1", 0))
    x2, y2 = float(p.get("x2", 10)), float(p.get("y2", 0))
    width = float(p.get("width", 3))
    dx = x2 - x1
    dy = y2 - y1
    length = math.sqrt(dx * dx + dy * dy)
    if length == 0:
        return {"error": "Slot endpoints cannot be the same point"}
    r = width / 2.0
    nx = -dy / length
    ny = dx / length
    p1 = adsk.core.Point3D.create(x1 + nx * r, y1 + ny * r, 0)
    p2 = adsk.core.Point3D.create(x2 + nx * r, y2 + ny * r, 0)
    p3 = adsk.core.Point3D.create(x2 - nx * r, y2 - ny * r, 0)
    p4 = adsk.core.Point3D.create(x1 - nx * r, y1 - ny * r, 0)
    c1 = adsk.core.Point3D.create(x1, y1, 0)
    c2 = adsk.core.Point3D.create(x2, y2, 0)
    lines = sketch.sketchCurves.sketchLines
    arcs = sketch.sketchCurves.sketchArcs
    lines.addByTwoPoints(p1, p2)
    arcs.addByCenterStartSweep(c2, p2, math.pi)
    lines.addByTwoPoints(p3, p4)
    arcs.addByCenterStartSweep(c1, p4, math.pi)
    return {"sketch": sketch.name, "slot": f"({x1},{y1})->({x2},{y2}), width={width}",
            "profiles": sketch.profiles.count}

def _draw_text(root, p):
    sketch = _resolve_sketch(root, p)
    text = p.get("text", "Hello")
    x, y = float(p.get("x", 0)), float(p.get("y", 0))
    height = float(p.get("height", 1.0))
    texts = sketch.sketchTexts
    input_obj = texts.createInput2(text, height)
    input_obj.setAsMultiLine(
        adsk.core.Point3D.create(x, y, 0),
        adsk.core.Point3D.create(x + height * len(text) * 0.6, y + height, 0),
        adsk.core.HorizontalAlignments.LeftHorizontalAlignment,
        adsk.core.VerticalAlignments.BottomVerticalAlignment, 0)
    texts.add(input_obj)
    return {"sketch": sketch.name, "text": text}

def _add_sketch_fillet(root, p):
    sketch = _resolve_sketch(root, p)
    radius = float(p.get("radius", 1.0))
    line1_idx = int(p.get("line1_index", 0))
    line2_idx = int(p.get("line2_index", 1))
    lines = sketch.sketchCurves.sketchLines
    if lines.count < 2:
        return {"error": "Need at least 2 lines in sketch"}
    l1 = lines.item(line1_idx)
    l2 = lines.item(line2_idx)
    sketch.sketchCurves.sketchArcs.addFillet(
        l1, l1.endSketchPoint.geometry,
        l2, l2.startSketchPoint.geometry,
        radius)
    return {"sketch_fillet": f"radius={radius} between lines {line1_idx} and {line2_idx}"}

def _offset_sketch(root, p):
    sketch = _resolve_sketch(root, p)
    curves = _collect_all_sketch_curves(sketch)
    if curves.count == 0:
        return {"error": "No curves in sketch to offset"}
    direction = adsk.core.Point3D.create(
        float(p.get("dx", 1)), float(p.get("dy", 1)), 0)
    sketch.offset(curves, direction, float(p.get("distance", 1)))
    return {"offset": f"distance={p.get('distance', 1)} cm"}

def _mirror_sketch(root, p):
    sketch = _resolve_sketch(root, p)
    axis_idx = int(p.get("axis_line_index", 0))
    lines = sketch.sketchCurves.sketchLines
    if lines.count == 0:
        return {"error": "No lines in sketch (need at least one as mirror axis)"}
    axis_line = lines.item(axis_idx)
    curves = adsk.core.ObjectCollection.create()
    for i in range(lines.count):
        if i != axis_idx:
            curves.add(lines.item(i))
    for i in range(sketch.sketchCurves.sketchCircles.count):
        curves.add(sketch.sketchCurves.sketchCircles.item(i))
    for i in range(sketch.sketchCurves.sketchArcs.count):
        curves.add(sketch.sketchCurves.sketchArcs.item(i))
    for i in range(sketch.sketchCurves.sketchFittedSplines.count):
        curves.add(sketch.sketchCurves.sketchFittedSplines.item(i))
    if curves.count == 0:
        return {"error": "No curves to mirror (only the axis line exists)"}
    sketch.mirror(curves, axis_line)
    return {"mirrored": f"{curves.count} curves"}

def _rectangular_pattern_sketch(root, p):
    sketch = _resolve_sketch(root, p)
    curves = _collect_all_sketch_curves(sketch)
    if curves.count == 0:
        return {"error": "No curves in sketch to pattern"}
    x_count = int(p.get("x_count", 2))
    y_count = int(p.get("y_count", 2))
    x_spacing = float(p.get("x_spacing", 5))
    y_spacing = float(p.get("y_spacing", 5))
    for ix in range(x_count):
        for iy in range(y_count):
            if ix == 0 and iy == 0:
                continue
            transform = adsk.core.Matrix3D.create()
            transform.translation = adsk.core.Vector3D.create(
                ix * x_spacing, iy * y_spacing, 0)
            sketch.copy(curves, transform)
    return {"pattern": f"{x_count}x{y_count}, spacing=({x_spacing},{y_spacing})"}


# ---- Sketch Constraints ----

def _add_constraint(root, p):
    sketch = _resolve_sketch(root, p)
    ctype = p.get("constraint_type", "coincident").lower()
    e1_idx = int(p.get("entity1_index", 0))
    e2_idx = int(p.get("entity2_index", -1))
    e1_type = p.get("entity1_type", "point").lower()
    e2_type = p.get("entity2_type", "point").lower()

    def get_entity(sketch, etype, idx):
        if etype == "point":
            return sketch.sketchPoints.item(idx)
        elif etype == "line":
            return sketch.sketchCurves.sketchLines.item(idx)
        elif etype == "circle":
            return sketch.sketchCurves.sketchCircles.item(idx)
        elif etype == "arc":
            return sketch.sketchCurves.sketchArcs.item(idx)
        elif etype == "curve":
            total = 0
            for coll in [sketch.sketchCurves.sketchLines,
                         sketch.sketchCurves.sketchArcs,
                         sketch.sketchCurves.sketchCircles]:
                if idx < total + coll.count:
                    return coll.item(idx - total)
                total += coll.count
            raise Exception(f"Curve index {idx} out of range")
        raise Exception(f"Unknown entity type: {etype}")

    gc = sketch.geometricConstraints
    ent1 = get_entity(sketch, e1_type, e1_idx)

    if ctype == "horizontal":
        gc.addHorizontal(ent1)
        return {"constraint": "horizontal"}
    elif ctype == "vertical":
        gc.addVertical(ent1)
        return {"constraint": "vertical"}
    elif ctype == "fix":
        gc.addFix(ent1)
        return {"constraint": "fix"}

    if e2_idx < 0:
        return {"error": f"Constraint '{ctype}' requires two entities (entity2_index missing)"}
    ent2 = get_entity(sketch, e2_type, e2_idx)

    if ctype == "coincident":
        gc.addCoincident(ent1, ent2)
    elif ctype == "tangent":
        gc.addTangent(ent1, ent2)
    elif ctype == "perpendicular":
        gc.addPerpendicular(ent1, ent2)
    elif ctype == "parallel":
        gc.addParallel(ent1, ent2)
    elif ctype == "concentric":
        gc.addConcentric(ent1, ent2)
    elif ctype == "equal":
        gc.addEqual(ent1, ent2)
    elif ctype == "collinear":
        gc.addCollinear(ent1, ent2)
    elif ctype == "midpoint":
        gc.addMidPoint(ent1, ent2)
    elif ctype == "smooth":
        gc.addSmooth(ent1, ent2)
    else:
        return {"error": f"Unknown constraint type: {ctype}"}
    return {"constraint": ctype}

def _add_sketch_dimension(root, p):
    sketch = _resolve_sketch(root, p)
    dtype = p.get("dimension_type", "distance").lower()
    e1_idx = int(p.get("entity1_index", 0))
    e2_idx = int(p.get("entity2_index", -1))
    e1_type = p.get("entity1_type", "line").lower()
    e2_type = p.get("entity2_type", "").lower()
    value = float(p.get("value", 5.0))

    def get_entity(sketch, etype, idx):
        if etype == "line":
            return sketch.sketchCurves.sketchLines.item(idx)
        elif etype == "circle":
            return sketch.sketchCurves.sketchCircles.item(idx)
        elif etype == "arc":
            return sketch.sketchCurves.sketchArcs.item(idx)
        elif etype == "point":
            return sketch.sketchPoints.item(idx)
        raise Exception(f"Unknown entity type: {etype}")

    dims = sketch.sketchDimensions
    text_pt = adsk.core.Point3D.create(5, 5, 0)

    if dtype == "distance":
        ent1 = get_entity(sketch, e1_type, e1_idx)
        if e2_idx >= 0 and e2_type:
            ent2 = get_entity(sketch, e2_type, e2_idx)
            dim = dims.addDistanceDimension(ent1, ent2, 0, text_pt)
        else:
            line = ent1
            dim = dims.addDistanceDimension(
                line.startSketchPoint, line.endSketchPoint, 0, text_pt)
        dim.parameter.value = value
        return {"dimension": "distance", "value_cm": value}
    elif dtype == "angle":
        ent1 = get_entity(sketch, e1_type, e1_idx)
        ent2 = get_entity(sketch, e2_type if e2_type else "line", e2_idx)
        dim = dims.addAngularDimension(ent1, ent2, text_pt)
        dim.parameter.value = math.radians(value)
        return {"dimension": "angle", "value_deg": value}
    elif dtype == "diameter":
        ent1 = get_entity(sketch, e1_type, e1_idx)
        dim = dims.addDiameterDimension(ent1, text_pt)
        dim.parameter.value = value
        return {"dimension": "diameter", "value_cm": value}
    elif dtype == "radius":
        ent1 = get_entity(sketch, e1_type, e1_idx)
        dim = dims.addRadialDimension(ent1, text_pt)
        dim.parameter.value = value
        return {"dimension": "radius", "value_cm": value}
    return {"error": f"Unknown dimension type: {dtype}"}


# ---- Feature Commands ----

def _extrude(root, p):
    sketch = _resolve_sketch(root, p)
    if sketch.profiles.count == 0:
        return {"error": "No closed profile in sketch."}
    pidx = min(int(p.get("profile_index", 0)), sketch.profiles.count - 1)
    profile = sketch.profiles.item(pidx)
    distance = float(p.get("distance", 1))
    ext_input = root.features.extrudeFeatures.createInput(profile, _op(p.get("operation", "new_body")))
    ext_input.setDistanceExtent(bool(p.get("symmetric", False)),
                                adsk.core.ValueInput.createByReal(distance))
    root.features.extrudeFeatures.add(ext_input)
    return {"extruded": sketch.name, "distance_cm": distance, "bodies": root.bRepBodies.count}

def _revolve(root, p):
    sketch = _resolve_sketch(root, p)
    if sketch.profiles.count == 0:
        return {"error": "No closed profile in sketch."}
    profile = sketch.profiles.item(int(p.get("profile_index", 0)))
    lines = sketch.sketchCurves.sketchLines
    if lines.count == 0:
        return {"error": "Draw a line in the sketch to use as the revolve axis."}
    axis_line = lines.item(int(p.get("axis_index", 0)))
    angle = float(p.get("angle", 360))
    rev_input = root.features.revolveFeatures.createInput(
        profile, axis_line, _op(p.get("operation", "new_body")))
    rev_input.setAngleExtent(False, adsk.core.ValueInput.createByReal(math.radians(angle)))
    root.features.revolveFeatures.add(rev_input)
    return {"revolved": sketch.name, "angle_deg": angle}

def _loft(root, p):
    sketch_indices = p.get("sketch_indices", [])
    if len(sketch_indices) < 2:
        return {"error": "Loft needs at least 2 sketch indices"}
    loft_input = root.features.loftFeatures.createInput(
        _op(p.get("operation", "new_body")))
    added = 0
    for idx in sketch_indices:
        sk = root.sketches.item(int(idx))
        if sk.profiles.count == 0:
            return {"error": f"Sketch at index {idx} has no closed profile."}
        loft_input.loftSections.add(sk.profiles.item(0))
        added += 1
    root.features.loftFeatures.add(loft_input)
    return {"lofted": f"{added} profiles", "sketch_indices": sketch_indices}

def _sweep(root, p):
    if root.sketches.count < 2:
        return {"error": "Need at least 2 sketches: profile and path."}
    profile_sketch = root.sketches.item(int(p.get("profile_sketch_index", 0)))
    path_sketch = root.sketches.item(int(p.get("path_sketch_index", 1)))
    if profile_sketch.profiles.count == 0:
        return {"error": "Profile sketch has no closed profile."}
    profile = profile_sketch.profiles.item(0)
    first_curve = None
    for coll in [path_sketch.sketchCurves.sketchLines,
                 path_sketch.sketchCurves.sketchArcs,
                 path_sketch.sketchCurves.sketchFittedSplines]:
        if coll.count > 0:
            first_curve = coll.item(0)
            break
    if not first_curve:
        return {"error": "Path sketch has no curves."}
    sweep_path = root.features.createPath(first_curve, True)
    sweep_input = root.features.sweepFeatures.createInput(
        profile, sweep_path, _op(p.get("operation", "new_body")))
    root.features.sweepFeatures.add(sweep_input)
    return {"swept": "profile along path"}

def _helix(root, p):
    sketch = _last_sketch(root)
    if sketch.profiles.count == 0:
        return {"error": "Draw a profile in the active sketch first."}
    profile = sketch.profiles.item(0)
    pitch = float(p.get("pitch", 1.0))
    height = float(p.get("height", 5.0))
    coil_input = root.features.coilFeatures.createInput(
        profile, root.zConstructionAxis,
        adsk.fusion.FeatureOperations.NewBodyFeatureOperation)
    coil_input.height = adsk.core.ValueInput.createByReal(height)
    coil_input.pitch = adsk.core.ValueInput.createByReal(pitch)
    coil_input.isClockwise = bool(p.get("clockwise", True))
    root.features.coilFeatures.add(coil_input)
    return {"helix": f"pitch={pitch}cm, height={height}cm"}

def _create_pipe(root, p):
    path_idx = int(p.get("path_sketch_index", 0))
    path_sketch = root.sketches.item(path_idx)
    section_size = float(p.get("section_size", 0.5))
    wall_thickness = float(p.get("wall_thickness", 0))
    first_curve = None
    for coll in [path_sketch.sketchCurves.sketchLines,
                 path_sketch.sketchCurves.sketchArcs,
                 path_sketch.sketchCurves.sketchFittedSplines]:
        if coll.count > 0:
            first_curve = coll.item(0)
            break
    if not first_curve:
        return {"error": "Path sketch has no curves."}
    sweep_path = root.features.createPath(first_curve, True)
    start_pt = first_curve.startSketchPoint.geometry if hasattr(first_curve, 'startSketchPoint') else adsk.core.Point3D.create(0, 0, 0)
    temp_plane = root.xYConstructionPlane
    temp_sketch = root.sketches.add(temp_plane)
    temp_sketch.sketchCurves.sketchCircles.addByCenterRadius(
        adsk.core.Point3D.create(start_pt.x, start_pt.y, 0), section_size)
    profile = temp_sketch.profiles.item(0)
    sweep_input = root.features.sweepFeatures.createInput(
        profile, sweep_path, _op(p.get("operation", "new_body")))
    feat = root.features.sweepFeatures.add(sweep_input)
    if wall_thickness > 0 and feat.bodies.count > 0:
        pipe_body = feat.bodies.item(0)
        faces = adsk.core.ObjectCollection.create()
        end_faces = []
        for i in range(pipe_body.faces.count):
            f = pipe_body.faces.item(i)
            try:
                if f.geometry.normal:
                    end_faces.append((f.area, f))
            except:
                pass
        if len(end_faces) >= 2:
            end_faces.sort(key=lambda x: x[0])
            faces.add(end_faces[0][1])
            faces.add(end_faces[1][1])
            shell_input = root.features.shellFeatures.createInput(faces, False)
            shell_input.insideThickness = adsk.core.ValueInput.createByReal(wall_thickness)
            root.features.shellFeatures.add(shell_input)
    return {"pipe": f"section_radius={section_size}cm, wall={wall_thickness}cm"}

def _create_hole(root, p):
    body = _find_body(root, p.get("body", 0))
    face_index = int(p.get("face_index", 0))
    face = body.faces.item(face_index)
    diameter = float(p.get("diameter", 1.0))
    depth = float(p.get("depth", 2.0))
    hole_type = p.get("hole_type", "simple").lower()
    x = float(p.get("x", 0))
    y = float(p.get("y", 0))
    point = adsk.core.Point3D.create(x, y, 0)
    holes = root.features.holeFeatures
    hole_input = holes.createSimpleInput(adsk.core.ValueInput.createByReal(diameter))
    hole_input.setPositionByPoint(face, point)
    hole_input.setDistanceExtent(adsk.core.ValueInput.createByReal(depth))
    if hole_type == "counterbore":
        cb_dia = float(p.get("counterbore_diameter", diameter * 1.8))
        cb_depth = float(p.get("counterbore_depth", diameter * 0.5))
        hole_input = holes.createCounterboreInput(
            adsk.core.ValueInput.createByReal(diameter),
            adsk.core.ValueInput.createByReal(cb_dia),
            adsk.core.ValueInput.createByReal(cb_depth))
        hole_input.setPositionByPoint(face, point)
        hole_input.setDistanceExtent(adsk.core.ValueInput.createByReal(depth))
    elif hole_type == "countersink":
        cs_dia = float(p.get("countersink_diameter", diameter * 2.0))
        cs_angle = float(p.get("countersink_angle", 90))
        hole_input = holes.createCountersinkInput(
            adsk.core.ValueInput.createByReal(diameter),
            adsk.core.ValueInput.createByReal(cs_dia),
            adsk.core.ValueInput.createByReal(math.radians(cs_angle)))
        hole_input.setPositionByPoint(face, point)
        hole_input.setDistanceExtent(adsk.core.ValueInput.createByReal(depth))
    holes.add(hole_input)
    return {"hole": hole_type, "diameter_cm": diameter, "depth_cm": depth}

def _shell(root, p):
    body = _find_body(root, p.get("body", 0))
    thickness = float(p.get("thickness", 0.2))
    face_indices = p.get("face_indices", [0])
    faces = adsk.core.ObjectCollection.create()
    for fi in face_indices:
        faces.add(body.faces.item(int(fi)))
    shell_input = root.features.shellFeatures.createInput(faces, False)
    shell_input.insideThickness = adsk.core.ValueInput.createByReal(thickness)
    root.features.shellFeatures.add(shell_input)
    return {"shelled": body.name, "thickness_cm": thickness, "open_faces": face_indices}

def _fillet(root, p):
    body = _find_body(root, p.get("body", 0))
    radius = float(p.get("radius", 0.5))
    edge_indices = p.get("edge_indices", [0])
    edges = adsk.core.ObjectCollection.create()
    for ei in edge_indices:
        edges.add(body.edges.item(int(ei)))
    fi = root.features.filletFeatures.createInput()
    fi.addConstantRadiusEdgeSet(edges, adsk.core.ValueInput.createByReal(radius), True)
    root.features.filletFeatures.add(fi)
    return {"filleted": body.name, "radius_cm": radius, "edges": edge_indices}

def _chamfer(root, p):
    body = _find_body(root, p.get("body", 0))
    distance = float(p.get("distance", 0.3))
    edge_indices = p.get("edge_indices", [0])
    edges = adsk.core.ObjectCollection.create()
    for ei in edge_indices:
        edges.add(body.edges.item(int(ei)))
    ci = root.features.chamferFeatures.createInput(edges, True)
    ci.setToEqualDistance(adsk.core.ValueInput.createByReal(distance))
    root.features.chamferFeatures.add(ci)
    return {"chamfered": body.name, "distance_cm": distance, "edges": edge_indices}

def _mirror_body(root, p):
    body = _find_body(root, p.get("body", 0))
    plane_name = p.get("plane", "XY")
    mirror_plane = _resolve_plane(root, plane_name)
    bodies_col = adsk.core.ObjectCollection.create()
    bodies_col.add(body)
    mi = root.features.mirrorFeatures.createInput(bodies_col, mirror_plane)
    root.features.mirrorFeatures.add(mi)
    return {"mirrored": body.name, "plane": plane_name}

def _rectangular_pattern_body(root, p):
    body = _find_body(root, p.get("body", 0))
    bodies_col = adsk.core.ObjectCollection.create()
    bodies_col.add(body)
    x_count = int(p.get("x_count", 2))
    y_count = int(p.get("y_count", 1))
    x_spacing = float(p.get("x_spacing", 5))
    y_spacing = float(p.get("y_spacing", 5))
    pi = root.features.rectangularPatternFeatures.createInput(
        bodies_col, root.xConstructionAxis,
        adsk.core.ValueInput.createByReal(x_count),
        adsk.core.ValueInput.createByReal(x_spacing),
        adsk.fusion.PatternDistanceType.SpacingPatternDistanceType)
    pi.setDirectionTwo(root.yConstructionAxis,
                       adsk.core.ValueInput.createByReal(y_count),
                       adsk.core.ValueInput.createByReal(y_spacing))
    root.features.rectangularPatternFeatures.add(pi)
    return {"pattern": f"{x_count}x{y_count}", "body": body.name}

def _circular_pattern_body(root, p):
    body = _find_body(root, p.get("body", 0))
    bodies_col = adsk.core.ObjectCollection.create()
    bodies_col.add(body)
    count = int(p.get("count", 4))
    axis = _construction_axis(root, p.get("axis", "Z"))
    pi = root.features.circularPatternFeatures.createInput(bodies_col, axis)
    pi.quantity = adsk.core.ValueInput.createByReal(count)
    pi.totalAngle = adsk.core.ValueInput.createByString("360 deg")
    pi.isSymmetric = True
    root.features.circularPatternFeatures.add(pi)
    return {"circular_pattern": f"{count} instances", "body": body.name}

def _combine_bodies(root, p):
    target = _find_body(root, p.get("target_body", 0))
    tools = adsk.core.ObjectCollection.create()
    for ti in p.get("tool_bodies", [1]):
        tools.add(_find_body(root, ti))
    ci = root.features.combineFeatures.createInput(target, tools)
    ci.operation = _bool_op(p.get("operation", "join"))
    ci.isKeepToolBodies = bool(p.get("keep_tools", False))
    root.features.combineFeatures.add(ci)
    return {"combined": target.name, "operation": p.get("operation", "join")}

def _scale_body(root, p):
    body = _find_body(root, p.get("body", 0))
    bodies_col = adsk.core.ObjectCollection.create()
    bodies_col.add(body)
    scale_x = float(p.get("scale_x", p.get("scale", 2.0)))
    scale_y = float(p.get("scale_y", -1))
    scale_z = float(p.get("scale_z", -1))
    point = adsk.core.Point3D.create(
        float(p.get("cx", 0)), float(p.get("cy", 0)), float(p.get("cz", 0)))
    if scale_y == -1 and scale_z == -1:
        si = root.features.scaleFeatures.createInput(
            bodies_col, point, adsk.core.ValueInput.createByReal(scale_x))
    else:
        if scale_y == -1: scale_y = scale_x
        if scale_z == -1: scale_z = scale_x
        si = root.features.scaleFeatures.createInput(
            bodies_col, point, adsk.core.ValueInput.createByReal(scale_x))
        si.setToNonUniform(
            adsk.core.ValueInput.createByReal(scale_x),
            adsk.core.ValueInput.createByReal(scale_y),
            adsk.core.ValueInput.createByReal(scale_z))
    root.features.scaleFeatures.add(si)
    return {"scaled": body.name, "scale": [scale_x, scale_y, scale_z]}

def _move_body(root, p):
    body = _find_body(root, p.get("body", 0))
    bodies_col = adsk.core.ObjectCollection.create()
    bodies_col.add(body)
    dx = float(p.get("dx", 0))
    dy = float(p.get("dy", 0))
    dz = float(p.get("dz", 0))
    transform = adsk.core.Matrix3D.create()
    transform.translation = adsk.core.Vector3D.create(dx, dy, dz)
    mi = root.features.moveFeatures.createInput2(bodies_col)
    mi.defineAsFreeMove(transform)
    root.features.moveFeatures.add(mi)
    return {"moved": body.name, "translation_cm": {"dx": dx, "dy": dy, "dz": dz}}

def _rotate_body(root, p):
    body = _find_body(root, p.get("body", 0))
    bodies_col = adsk.core.ObjectCollection.create()
    bodies_col.add(body)
    angle = float(p.get("angle", 45))
    axis_vec = _axis_vector(p.get("axis", "Z"))
    cx = float(p.get("cx", 0))
    cy = float(p.get("cy", 0))
    cz = float(p.get("cz", 0))
    origin = adsk.core.Point3D.create(cx, cy, cz)
    transform = adsk.core.Matrix3D.create()
    transform.setToRotation(math.radians(angle), axis_vec, origin)
    mi = root.features.moveFeatures.createInput2(bodies_col)
    mi.defineAsFreeMove(transform)
    root.features.moveFeatures.add(mi)
    return {"rotated": body.name, "axis": p.get("axis", "Z"),
            "angle_deg": angle, "center": [cx, cy, cz]}

def _press_pull(root, p):
    body = _find_body(root, p.get("body", 0))
    face_index = int(p.get("face_index", 0))
    face = body.faces.item(face_index)
    faces = adsk.core.ObjectCollection.create()
    faces.add(face)
    distance = float(p.get("distance", 1))
    pp = root.features.offsetFacesFeatures.createInput(
        faces, adsk.core.ValueInput.createByReal(distance))
    root.features.offsetFacesFeatures.add(pp)
    return {"press_pull": body.name, "face": face_index, "distance_cm": distance}

def _thicken(root, p):
    sketch = _last_sketch(root)
    if sketch.profiles.count == 0:
        return {"error": "No profile in sketch"}
    profile = sketch.profiles.item(0)
    thickness = float(p.get("thickness", 0.5))
    ext_input = root.features.extrudeFeatures.createInput(
        profile, adsk.fusion.FeatureOperations.NewBodyFeatureOperation)
    ext_input.setDistanceExtent(False, adsk.core.ValueInput.createByReal(thickness))
    root.features.extrudeFeatures.add(ext_input)
    return {"thickened": sketch.name, "thickness_cm": thickness}

def _draft_face(root, p):
    body = _find_body(root, p.get("body", 0))
    angle = float(p.get("angle", 3))
    face_index = int(p.get("face_index", 0))
    faces = adsk.core.ObjectCollection.create()
    faces.add(body.faces.item(face_index))
    plane_map = {"XY": root.xYConstructionPlane, "XZ": root.xZConstructionPlane}
    pull_dir = plane_map.get(p.get("pull_plane", "XY").upper(), root.xYConstructionPlane)
    di = root.features.draftFeatures.createInput(
        faces, pull_dir, adsk.core.ValueInput.createByReal(math.radians(angle)), False)
    root.features.draftFeatures.add(di)
    return {"draft": body.name, "angle_deg": angle, "face": face_index}

def _add_thread(root, p):
    body = _find_body(root, p.get("body", 0))
    face_index = int(p.get("face_index", 0))
    face = body.faces.item(face_index)
    is_internal = bool(p.get("is_internal", False))
    threadFeats = root.features.threadFeatures
    threadQuery = threadFeats.threadDataQuery
    threadData = threadQuery.recommendThreadData(face, is_internal)
    if not threadData:
        return {"error": f"Cannot add thread to face {face_index}. Must be cylindrical with standard size."}
    thread_type = p.get("thread_type", "")
    if thread_type:
        threadData.threadType = thread_type
    threadData.isRightHanded = bool(p.get("right_handed", True))
    ti = threadFeats.createInput(face, threadData)
    ti.isFullLength = bool(p.get("full_length", True))
    threadFeats.add(ti)
    return {"thread": f"type={threadData.threadType}, designation={threadData.designation}",
            "body": body.name, "face": face_index, "internal": is_internal}


# ---- Assembly / Joints ----

def _create_component(root, p):
    occ = root.occurrences.addNewComponent(adsk.core.Matrix3D.create())
    comp = occ.component
    comp.name = p.get("name", "Component")
    return {"created": comp.name, "id": comp.id}

def _move_body_to_component(root, p):
    body = _find_body(root, p.get("body", 0))
    comp = _find_component(root, p.get("component", ""))
    body_name = body.name
    body.moveToComponent(comp.occurrences.item(0) if comp != root else root.occurrences.addNewComponent(adsk.core.Matrix3D.create()))
    return {"moved_body": body_name, "to_component": comp.name}

def _create_joint(root, p):
    design = _design()
    comp1_name = p.get("component1", "")
    comp2_name = p.get("component2", "")
    joint_type = p.get("joint_type", "rigid").lower()

    occ1 = occ2 = None
    for i in range(root.occurrences.count):
        occ = root.occurrences.item(i)
        if occ.component.name == comp1_name:
            occ1 = occ
        if occ.component.name == comp2_name:
            occ2 = occ
    if not occ1 or not occ2:
        return {"error": f"Components not found. Available: {[root.occurrences.item(i).component.name for i in range(root.occurrences.count)]}"}

    joint_types = {
        "rigid": adsk.fusion.JointTypes.RigidJointType,
        "revolute": adsk.fusion.JointTypes.RevoluteJointType,
        "slider": adsk.fusion.JointTypes.SliderJointType,
        "cylindrical": adsk.fusion.JointTypes.CylindricalJointType,
        "pin_slot": adsk.fusion.JointTypes.PinSlotJointType,
        "planar": adsk.fusion.JointTypes.PlanarJointType,
        "ball": adsk.fusion.JointTypes.BallJointType,
    }

    ox = float(p.get("offset_x", 0))
    oy = float(p.get("offset_y", 0))
    oz = float(p.get("offset_z", 0))

    geo0 = adsk.fusion.JointGeometry.createByPoint(occ1)
    geo1 = adsk.fusion.JointGeometry.createByPoint(occ2)

    ji = root.joints.createInput(geo0, geo1)
    ji.setAsRigidJointMotion() if joint_type == "rigid" else None
    if joint_type == "revolute":
        ji.setAsRevoluteJointMotion(adsk.fusion.JointDirections.ZAxisJointDirection)
    elif joint_type == "slider":
        ji.setAsSliderJointMotion(adsk.fusion.JointDirections.ZAxisJointDirection)
    elif joint_type == "cylindrical":
        ji.setAsCylindricalJointMotion(adsk.fusion.JointDirections.ZAxisJointDirection)
    elif joint_type == "ball":
        ji.setAsBallJointMotion()
    elif joint_type == "planar":
        ji.setAsPlanarJointMotion(adsk.fusion.JointDirections.ZAxisJointDirection)

    joint = root.joints.add(ji)
    if ox != 0 or oy != 0 or oz != 0:
        joint.jointMotion.slideValue = oz if hasattr(joint.jointMotion, 'slideValue') else None
    return {"joint": joint.name, "type": joint_type,
            "between": [comp1_name, comp2_name]}

def _create_as_built_joint(root, p):
    comp1_name = p.get("component1", "")
    comp2_name = p.get("component2", "")
    joint_type = p.get("joint_type", "rigid").lower()

    occ1 = occ2 = None
    for i in range(root.occurrences.count):
        occ = root.occurrences.item(i)
        if occ.component.name == comp1_name:
            occ1 = occ
        if occ.component.name == comp2_name:
            occ2 = occ
    if not occ1 or not occ2:
        return {"error": "Components not found."}

    ji = root.asBuiltJoints.createInput(occ1, occ2, None)
    if joint_type == "rigid":
        ji.setAsRigidJointMotion()
    elif joint_type == "revolute":
        ji.setAsRevoluteJointMotion(adsk.fusion.JointDirections.ZAxisJointDirection)
    elif joint_type == "slider":
        ji.setAsSliderJointMotion(adsk.fusion.JointDirections.ZAxisJointDirection)
    elif joint_type == "ball":
        ji.setAsBallJointMotion()
    joint = root.asBuiltJoints.add(ji)
    return {"as_built_joint": joint.name, "type": joint_type}


# ---- Body Management ----

def _delete_body(root, p):
    body = _find_body(root, p.get("body", 0))
    name = body.name
    body.deleteMe()
    return {"deleted_body": name}

def _rename_body(root, p):
    body = _find_body(root, p.get("body", 0))
    old = body.name
    body.name = p.get("name", "Body")
    return {"renamed": old, "to": body.name}

def _copy_body(root, p):
    body = _find_body(root, p.get("body", 0))
    bodies_col = adsk.core.ObjectCollection.create()
    bodies_col.add(body)
    transform = adsk.core.Matrix3D.create()
    dx = float(p.get("dx", 2))
    dy = float(p.get("dy", 0))
    dz = float(p.get("dz", 0))
    transform.translation = adsk.core.Vector3D.create(dx, dy, dz)
    mi = root.features.moveFeatures.createInput2(bodies_col)
    mi.defineAsFreeMove(transform)
    mi.isCopy = True
    feature = root.features.moveFeatures.add(mi)
    new_body = feature.bodies.item(0) if feature.bodies.count > 0 else None
    new_name = p.get("name", body.name + "_copy")
    if new_body:
        new_body.name = new_name
    return {"copied": body.name, "new_body": new_name,
            "offset_cm": {"dx": dx, "dy": dy, "dz": dz}}

def _toggle_body_visibility(root, p):
    body = _find_body(root, p.get("body", 0))
    body.isVisible = not body.isVisible
    return {"body": body.name, "visible": body.isVisible}


# ---- Construction Geometry ----

def _add_construction_plane(root, p):
    plane_type = p.get("type", "offset").lower()
    planes = root.constructionPlanes
    if plane_type == "offset":
        base = _resolve_plane(root, p.get("base_plane", "XY"))
        offset = float(p.get("offset", 2))
        pi = planes.createInput()
        pi.setByOffset(base, adsk.core.ValueInput.createByReal(offset))
        plane = planes.add(pi)
        return {"construction_plane": plane.name, "offset_cm": offset}
    elif plane_type == "angle":
        base = _resolve_plane(root, p.get("base_plane", "XY"))
        edge_body = _find_body(root, p.get("body", 0))
        edge = edge_body.edges.item(int(p.get("edge_index", 0)))
        angle = float(p.get("angle", 45))
        pi = planes.createInput()
        pi.setByAngle(edge, adsk.core.ValueInput.createByReal(math.radians(angle)), base)
        plane = planes.add(pi)
        return {"construction_plane": plane.name, "angle_deg": angle}
    elif plane_type == "midplane":
        body = _find_body(root, p.get("body", 0))
        f1 = body.faces.item(int(p.get("face1_index", 0)))
        f2 = body.faces.item(int(p.get("face2_index", 1)))
        pi = planes.createInput()
        pi.setByTwoPlanes(f1, f2)
        plane = planes.add(pi)
        return {"construction_plane": plane.name, "type": "midplane"}
    return {"error": f"Unsupported plane type '{plane_type}'. Use: offset, angle, midplane"}

def _add_construction_axis(root, p):
    axis_type = p.get("axis_type", "edge").lower()
    axes = root.constructionAxes
    if axis_type == "cylinder":
        body = _find_body(root, p.get("body", 0))
        face = body.faces.item(int(p.get("face_index", 0)))
        ai = axes.createInput()
        ai.setByCircularFace(face)
        axis = axes.add(ai)
        return {"construction_axis": axis.name, "type": "cylinder"}
    elif axis_type == "edge":
        body = _find_body(root, p.get("body", 0))
        edge = body.edges.item(int(p.get("edge_index", 0)))
        ai = axes.createInput()
        ai.setByLine(edge)
        axis = axes.add(ai)
        return {"construction_axis": axis.name, "type": "edge"}
    elif axis_type == "perpendicular":
        body = _find_body(root, p.get("body", 0))
        face = body.faces.item(int(p.get("face_index", 0)))
        ai = axes.createInput()
        ai.setByNormalToFaceAtPoint(face, face.pointOnFace)
        axis = axes.add(ai)
        return {"construction_axis": axis.name, "type": "perpendicular"}
    return {"error": f"Unknown axis type: {axis_type}. Use: cylinder, edge, perpendicular"}


# ---- Parameter Tools ----

def _add_parameter(design, p):
    name = p.get("name", "param1")
    value = float(p.get("value", 10))
    unit = p.get("unit", "cm")
    comment = p.get("comment", "")
    design.userParameters.add(
        name, adsk.core.ValueInput.createByString(f"{value} {unit}"), unit, comment)
    return {"parameter": name, "value": value, "unit": unit}

def _update_parameter(design, p):
    name = p.get("name")
    value = float(p.get("value", 10))
    param = design.allParameters.itemByName(name)
    if not param:
        return {"error": f"Parameter '{name}' not found"}
    param.value = value
    return {"updated": name, "new_value": value}

def _list_parameters(design):
    return {"parameters": [
        {"name": design.allParameters.item(i).name,
         "value": round(design.allParameters.item(i).value, 4),
         "unit": design.allParameters.item(i).unit,
         "expression": design.allParameters.item(i).expression}
        for i in range(design.allParameters.count)]}


# ---- Appearance & Materials ----

def _list_appearances(p):
    results = []
    try:
        for i in range(app.materialLibraries.count):
            lib = app.materialLibraries.item(i)
            try:
                for j in range(min(lib.appearances.count, 30)):
                    a = lib.appearances.item(j)
                    results.append({"name": a.name, "library": lib.name})
            except:
                pass
    except Exception as e:
        return {"error": str(e)}
    search = p.get("search", "").lower()
    if search:
        results = [r for r in results if search in r["name"].lower()]
    return {"appearances": results[:50]}

def _apply_appearance(root, p):
    body = _find_body(root, p.get("body", 0))
    search = p.get("appearance", "Steel").lower()
    try:
        for i in range(app.materialLibraries.count):
            lib = app.materialLibraries.item(i)
            try:
                for j in range(lib.appearances.count):
                    a = lib.appearances.item(j)
                    if search in a.name.lower():
                        body.appearance = a
                        return {"applied": a.name, "to": body.name}
            except:
                pass
        return {"error": f"No appearance matching '{search}' found."}
    except Exception as e:
        return {"error": str(e)}

def _set_body_color(root, p):
    body = _find_body(root, p.get("body", 0))
    r = int(p.get("r", 128))
    g = int(p.get("g", 128))
    b_val = int(p.get("b", 255))
    opacity = int(p.get("opacity", 255))
    try:
        design = _design()
        base_lib = app.materialLibraries.item(0)
        base_appearance = base_lib.appearances.item(0)
        custom = design.appearances.addByCopy(base_appearance, f"Color_{body.name}")
        props = custom.appearanceProperties
        for i in range(props.count):
            prop = props.item(i)
            if hasattr(prop, 'value'):
                try:
                    if isinstance(prop.value, adsk.core.Color):
                        prop.value = adsk.core.Color.create(r, g, b_val, opacity)
                except:
                    pass
        body.appearance = custom
        return {"color_set": body.name, "rgb": f"({r},{g},{b_val})", "opacity": opacity}
    except Exception as e:
        return {"error": f"Could not set color: {e}"}


# ---- Import Commands ----

def _import_cad_file(root, p: dict) -> dict:
    try:
        path = p.get("path", "")
        if not path:
            return {"error": "No file path provided. Set 'path' to a STEP/SAT/SMT/IGES/F3D file."}
        if not os.path.exists(path):
            return {"error": f"File not found: {path}"}

        fmt = str(p.get("format", "") or "").strip().lower()
        ext = os.path.splitext(path)[1].lower().lstrip(".")
        if not fmt:
            fmt = ext
        option_creators = {
            "step": "createSTEPImportOptions", "stp": "createSTEPImportOptions",
            "sat": "createSATImportOptions",
            "smt": "createSMTImportOptions",
            "igs": "createIGESImportOptions", "iges": "createIGESImportOptions",
            "f3d": "createFusionArchiveImportOptions",
        }
        creator = option_creators.get(fmt)
        if creator is None:
            return {"error": f"Unsupported CAD format '{fmt}'. Supported: .step/.stp, .sat, .smt, .igs/.iges, .f3d."}
        options = getattr(app.importManager, creator)(path)

        as_component = p.get("as_component", False)
        if isinstance(as_component, str):
            as_component = as_component.lower() in ("1", "true", "yes")

        def _import_into(target, before_count: int) -> None:
            try:
                app.importManager.importToTarget(options, target)
            except Exception as exc:
                # Some Fusion builds raise InternalValidationError after a successful
                # import; the reliable success signal is geometry appearing in the target.
                if target.bRepBodies.count > before_count:
                    return
                if "save" not in str(exc).lower():
                    raise
                # Never-saved documents can block some imports: save first, then retry.
                try:
                    doc = app.activeDocument
                    if doc is not None and not doc.isSaved:
                        data_mgr = app.data
                        hub = data_mgr.activeHub
                        root_folder = hub.dataProjects.item(0).rootFolder
                        doc.saveAs(f"FusionMCP_Import_{os.path.splitext(os.path.basename(path))[0]}",
                                   root_folder, "Imported via FusionMCP", "")
                    app.importManager.importToTarget(options, target)
                except Exception as save_exc:
                    if target.bRepBodies.count > before_count:
                        return
                    raise RuntimeError(
                        f"Import failed ({exc}) and auto-save+retry also failed ({save_exc}). "
                        "Save the document manually and retry.") from exc
                else:
                    if target.bRepBodies.count == before_count:
                        raise RuntimeError(f"Import of '{path}' added no bodies after save+retry.")
            else:
                if target.bRepBodies.count == before_count:
                    raise RuntimeError(f"Import of '{path}' completed without adding any bodies.")

        if as_component:
            occ = root.occurrences.addNewComponent(adsk.core.Matrix3D.create())
            target = occ.component
            _import_into(target, 0)
            bodies_added = target.bRepBodies.count
        else:
            before = root.bRepBodies.count
            _import_into(root, before)
            after = root.bRepBodies.count
            delta = after - before
            bodies_added = delta if delta > 0 else after

        return {"imported": os.path.basename(path), "format": fmt,
                "bodies_added": bodies_added, "path": path}
    except Exception as e:
        return {"error": str(e)}


def _import_mesh_file(root, p: dict) -> dict:
    try:
        path = p.get("path", "")
        if not path:
            return {"error": "No file path provided. Set 'path' to an STL or 3MF file."}
        if not os.path.exists(path):
            return {"error": f"File not found: {path}"}
        ext = os.path.splitext(path)[1].lower().lstrip(".")
        if ext not in ("stl", "3mf"):
            return {"error": f"{ext.upper()} format is not supported. Use STL or 3MF."}

        units = str(p.get("units", "mm") or "mm").lower()
        unit_map = {
            "mm": adsk.fusion.MeshUnits.MillimeterMeshUnit,
            "cm": adsk.fusion.MeshUnits.CentimeterMeshUnit,
            "in": adsk.fusion.MeshUnits.InchMeshUnit,
        }
        unit = unit_map.get(units)
        if unit is None:
            return {"error": f"Unsupported units '{units}'. Use 'mm', 'cm', or 'in'."}

        as_component = p.get("as_component", False)
        if isinstance(as_component, str):
            as_component = as_component.lower() in ("1", "true", "yes")
        target = root
        if as_component:
            occ = root.occurrences.addNewComponent(adsk.core.Matrix3D.create())
            target = occ.component

        base_feature = None
        if _design().designType == adsk.fusion.DesignTypes.ParametricDesignType:
            base_features = target.features.baseFeatures
            base_feature = base_features.item(0) if base_features.count > 0 else base_features.add()

        before = target.meshBodies.count
        if base_feature is not None:
            base_feature.startEdit()
            try:
                mesh_result = target.meshBodies.add(path, unit, base_feature)
            finally:
                base_feature.finishEdit()
        else:
            mesh_result = target.meshBodies.add(path, unit, None)
        after = target.meshBodies.count
        delta = after - before
        imported_name = os.path.basename(path)
        if mesh_result is not None and mesh_result.count > 0:
            imported_name = mesh_result.item(mesh_result.count - 1).name
        return {"imported_mesh": imported_name, "mesh_bodies": delta if delta > 0 else after,
                "path": path}
    except Exception as e:
        return {"error": str(e)}


def _import_sketch_file(root, p: dict) -> dict:
    try:
        path = p.get("path", "")
        if not path:
            return {"error": "No file path provided. Set 'path' to an SVG or DXF file."}
        if not os.path.exists(path):
            return {"error": f"File not found: {path}"}
        fmt = str(p.get("format", "") or "").strip().lower()
        ext = os.path.splitext(path)[1].lower().lstrip(".")
        if not fmt:
            fmt = ext
        imp = app.importManager
        before = root.sketches.count
        if fmt == "svg":
            options = imp.createSVGImportOptions(path)
            sketch = root.sketches.add(root.xYConstructionPlane)
            try:
                imp.importToTarget(options, sketch)
            except Exception:
                if sketch.sketchCurves.count == 0:
                    raise
        elif fmt == "dxf":
            plane_name = str(p.get("plane", "XY") or "XY").upper()
            if plane_name == "XZ":
                plane_entity = root.xZConstructionPlane
            elif plane_name == "YZ":
                plane_entity = root.yZConstructionPlane
            else:
                plane_entity = root.xYConstructionPlane
            options = imp.createDXF2DImportOptions(path, plane_entity)
            try:
                imp.importToTarget(options, root)
            except Exception:
                if root.sketches.count == before:
                    raise
        else:
            return {"error": f"Unsupported format '{fmt}'. Supported: SVG (.svg), DXF (.dxf)."}
        after = root.sketches.count
        delta = after - before
        return {"imported": os.path.basename(path), "format": fmt,
                "sketches_added": delta if delta > 0 else after, "path": path}
    except Exception as e:
        return {"error": str(e)}


# ---- Wave 3: OpenSCAD Mesh Pipeline ----

def _get_bundle_functions():
    """Return (get_openscad_path, get_bosl2_path) from mcp_server.bundle."""
    import importlib.util

    try:
        from mcp_server import bundle
        return bundle.get_openscad_path, bundle.get_bosl2_path
    except ImportError:
        pass

    here = os.path.dirname(os.path.abspath(__file__))
    candidates = (
        os.path.join(here, "mcp_server", "bundle.py"),
        os.path.join(here, os.pardir, "mcp_server", "bundle.py"),
    )
    for path in candidates:
        if not os.path.isfile(path):
            continue
        spec = importlib.util.spec_from_file_location("fusionmcp_bundle", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module.get_openscad_path, module.get_bosl2_path
    raise FileNotFoundError(
        "mcp_server/bundle.py could not be found. Keep the fusion-mcp "
        "repository (or its mcp_server/ folder) importable from FusionMCP.py "
        "so the bundled OpenSCAD + BOSL2 can be located.")


def _get_scad_translator():
    """Return the scad_translator module from the fusion-mcp repo.

    Only ``translate_to_fusion_commands`` + ``UnsupportedSCADNodeError`` are
    used here (the openscad-evaluator packages it imports lazily live in the
    MCP server process, not inside Fusion).  Same importlib pattern as
    ``_get_bundle_functions``: plain ``import mcp_server.scad_translator``
    first, then spec_from_file_location candidates relative to this file.
    """
    import importlib.util

    try:
        from mcp_server import scad_translator
        return scad_translator
    except ImportError:
        pass

    here = os.path.dirname(os.path.abspath(__file__))
    candidates = (
        os.path.join(here, "mcp_server", "scad_translator.py"),
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
        "repository (or its mcp_server/ folder) importable from FusionMCP.py "
        "so create_from_scad can translate CSG trees into Fusion features.")


def _run_scad(root, p: dict) -> dict:
    try:
        code = p.get("code", "")
        if not code:
            return {"error": "No OpenSCAD code provided. Set 'code' to the .scad source to render."}
        params = str(p.get("params", "") or "")
        quality = int(p.get("quality", 100) or 100)
        units = str(p.get("units", "mm") or "mm").lower()
        unit_map = {
            "mm": adsk.fusion.MeshUnits.MillimeterMeshUnit,
            "cm": adsk.fusion.MeshUnits.CentimeterMeshUnit,
            "in": adsk.fusion.MeshUnits.InchMeshUnit,
        }
        unit = unit_map.get(units)
        if unit is None:
            return {"error": f"Unsupported units '{units}'. Use 'mm', 'cm', or 'in'."}

        get_openscad_path, get_bosl2_path = _get_bundle_functions()
        openscad_path = get_openscad_path()
        bosl2_path = get_bosl2_path()

        if quality > 0:
            code = f"$fn={quality};\n{code}"

        token = uuid.uuid4().hex[:8]
        scad_file = os.path.join(tempfile.gettempdir(), f"fusionmcp_render_{token}.scad")
        stl_file = os.path.join(tempfile.gettempdir(), f"fusionmcp_render_{token}.stl")
        start = time.time()
        try:
            with open(scad_file, "w", encoding="utf-8") as f:
                f.write(code)
            cmd = [openscad_path, "-o", stl_file, scad_file]
            for assignment in params.split(";"):
                assignment = assignment.strip()
                if assignment:
                    cmd.extend(["-D", assignment])
            try:
                proc = subprocess.run(
                    cmd,
                    env={**os.environ, "OPENSCADPATH": bosl2_path},
                    timeout=300,
                    capture_output=True,
                )
            except subprocess.TimeoutExpired:
                return {"error": "OpenSCAD render timed out after 300s. Model may be too complex."}
            render_time_s = round(time.time() - start, 3)
            if proc.returncode != 0:
                err = (proc.stderr or b"").decode("utf-8", errors="replace")
                out = (proc.stdout or b"").decode("utf-8", errors="replace")
                detail = "\n".join(part for part in (err, out) if part.strip()).strip()
                return {"error": f"OpenSCAD render failed (exit {proc.returncode}): {detail}"}
            if not os.path.isfile(stl_file) or os.path.getsize(stl_file) == 0:
                return {"error": "OpenSCAD completed but produced no output STL file."}

            base_feature = None
            if _design().designType == adsk.fusion.DesignTypes.ParametricDesignType:
                base_features = root.features.baseFeatures
                base_feature = base_features.item(0) if base_features.count > 0 else base_features.add()
            before = root.meshBodies.count
            if base_feature is not None:
                base_feature.startEdit()
                try:
                    mesh_result = root.meshBodies.add(stl_file, unit, base_feature)
                finally:
                    base_feature.finishEdit()
            else:
                mesh_result = root.meshBodies.add(stl_file, unit, None)
            after = root.meshBodies.count
            if mesh_result is not None and mesh_result.count > 0:
                body = mesh_result.item(mesh_result.count - 1)
            else:
                delta = after - before
                if delta <= 0:
                    return {"error": "OpenSCAD render produced no mesh body."}
                body = root.meshBodies.item(after - 1)
            body.attributes.add("FusionMCP", "scad_source", code)
            return {
                "rendered": True,
                "mesh_body": body.name,
                "scad_source_stored": True,
                "render_time_s": render_time_s,
            }
        finally:
            for path in (scad_file, stl_file):
                try:
                    if os.path.isfile(path):
                        os.remove(path)
                except OSError:
                    pass
    except Exception as e:
        return {"error": str(e)}


def _snapshot_names(collection):
    """Names of every item in a Fusion collection (pre-translation snapshot)."""
    return set(collection.item(i).name for i in range(collection.count))


def _patch_boolean_types_aliases():
    """Alias CombineFeature BooleanTypes members for this Fusion build.

    The SCAD translator assigns ``BooleanTypes.JoinBooleanType`` /
    ``CutBooleanType`` / ``IntersectBooleanType`` to
    ``CombineFeatureInput.operation``.  Live Fusion 2026 exposes only
    ``UnionBooleanType`` / ``DifferenceBooleanType`` / ``IntersectionBooleanType``
    and -- empirically verified on this build -- ``CombineFeatureInput.operation``
    accepts plain integer codes with a ROTATED mapping: ``0`` = join, ``1`` = cut,
    ``2`` = intersect, whereas the enum members are Union=2, Difference=0,
    Intersection=1.  Aliasing to the enum members therefore made every combine
    behave as a join.  Instead, alias the documented names directly to the
    operation codes this build honors, so the translator's difference/intersection
    nodes cut and intersect correctly.  The enum class outlives module reloads, so
    the aliases are overwritten unconditionally (the ``UnionBooleanType`` member
    identifies this build); on builds that natively expose the documented names
    this is a no-op.
    """
    try:
        bt = adsk.fusion.BooleanTypes
        if not hasattr(bt, "UnionBooleanType"):
            return
        setattr(bt, "JoinBooleanType", 0)
        setattr(bt, "CutBooleanType", 1)
        setattr(bt, "IntersectBooleanType", 2)
    except Exception:
        pass


def _cleanup_translation(root, before_bodies, before_sketches, before_planes):
    """Delete every body/sketch/construction plane created during a failed
    partial translation, leaving the design as it was before.

    The translator names every body it creates ``scad_<kind>_<n>`` and every
    sketch ``scad_<kind>_sketch``, so anything whose name is not in the
    pre-translation snapshot is ours and safe to delete.  Iterating in reverse
    keeps indices valid while items disappear.  Returns the body count deleted.
    """
    deleted = 0
    for i in range(root.bRepBodies.count - 1, -1, -1):
        body = root.bRepBodies.item(i)
        if body.name not in before_bodies:
            try:
                body.deleteMe()
                deleted += 1
            except Exception:
                pass
    for i in range(root.sketches.count - 1, -1, -1):
        sketch = root.sketches.item(i)
        if sketch.name not in before_sketches:
            try:
                sketch.deleteMe()
            except Exception:
                pass
    for i in range(root.constructionPlanes.count - 1, -1, -1):
        plane = root.constructionPlanes.item(i)
        if plane.name not in before_planes:
            try:
                plane.deleteMe()
            except Exception:
                pass
    return deleted


def _is_revolve_failure(message: str) -> bool:
    """True when a translation crash is this Fusion 2026 build's revolve kernel
    rejecting a sketch-line revolve axis on an XZ/YZ construction plane
    (verified live: construction-axis revolves work, sketch-line-axis revolves
    on XZ/YZ raise ASM_PATH_TANGENT)."""
    return "ASM_PATH_TANGENT" in message or "revolu" in message.lower()


def _create_from_scad(root, p: dict) -> dict:
    try:
        code = str(p.get("code", "") or "")
        if not code:
            return {"error": "No OpenSCAD code provided. Set 'code' to the .scad source to translate."}
        units = str(p.get("units", "mm") or "mm").lower()
        fallback_to_mesh = bool(p.get("fallback_to_mesh", True))
        csg_tree = p.get("csg_tree")
        if csg_tree is None:
            return {"error": "No CSG tree received. The MCP server must resolve the SCAD code with resolve_scad() before sending it here."}

        translator = _get_scad_translator()
        design = _design()
        if design is None:
            return {"error": "No active Fusion 360 design. Open or create one first."}

        # Snapshot BEFORE translation: cleanup-on-fallback + success deltas.
        before_bodies = _snapshot_names(root.bRepBodies)
        before_body_count = root.bRepBodies.count
        before_mesh_count = root.meshBodies.count
        before_sketches = _snapshot_names(root.sketches)
        before_planes = _snapshot_names(root.constructionPlanes)
        before_timeline = design.timeline.count

        try:
            _patch_boolean_types_aliases()
            created_names = translator.translate_to_fusion_commands(
                csg_tree, root, design, units=units)
        except translator.UnsupportedSCADNodeError as e:
            deleted = _cleanup_translation(
                root, before_bodies, before_sketches, before_planes)
            kind = getattr(e, "kind", None)
            unsupported = [kind] if kind else [str(e).split(" ")[0]]
            if not fallback_to_mesh:
                return {
                    "error": f"Unsupported SCAD node: {e}",
                    "unsupported_nodes": unsupported,
                    "fallback_reason": str(e),
                    "partial_bodies_deleted": deleted,
                }
            # Mesh fallback: partial bodies already deleted above; render the
            # ENTIRE model with the bundled OpenSCAD instead.
            result = _run_scad(root, {"code": code, "units": units})
            if not result.get("rendered"):
                return {
                    "error": f"SCAD translation failed ({e}) and mesh fallback failed: {result.get('error')}",
                    "unsupported_nodes": unsupported,
                }
            return {
                "created": True,
                "method": "mesh_fallback",
                "bodies": root.meshBodies.count - before_mesh_count,
                "features": 0,
                "unsupported_nodes": unsupported,
                "fallback_reason": str(e),
                "mesh_body": result.get("mesh_body"),
            }
        except Exception as e:
            deleted = _cleanup_translation(
                root, before_bodies, before_sketches, before_planes)
            message = str(e)
            if fallback_to_mesh and _is_revolve_failure(message):
                result = _run_scad(root, {"code": code, "units": units})
                if result.get("rendered"):
                    return {
                        "created": True,
                        "method": "mesh_fallback",
                        "bodies": root.meshBodies.count - before_mesh_count,
                        "features": 0,
                        "unsupported_nodes": ["rotate_extrude"],
                        "fallback_reason": message,
                        "mesh_body": result.get("mesh_body"),
                    }
            return {
                "error": f"SCAD translation failed: {e}",
                "partial_bodies_deleted": deleted,
            }

        # Success: csg_translation.  Count deltas are the ground truth.
        bodies = (root.bRepBodies.count - before_body_count
                  + root.meshBodies.count - before_mesh_count)
        features = design.timeline.count - before_timeline
        if len(created_names) <= 0 and bodies <= 0:
            return {"error": "SCAD translation produced no bodies.",
                    "unsupported_nodes": []}
        return {
            "created": True,
            "method": "csg_translation",
            "bodies": bodies,
            "features": features,
            "unsupported_nodes": [],
            "fallback_reason": None,
        }
    except Exception as e:
        return {"error": str(e)}


def _create_from_csg_tree(root, p: dict) -> dict:
    """Translate a CSG tree (built by the server's mesh_csg strategy router)
    into native Fusion features.  Same flow as _create_from_scad minus the
    mesh-fallback path (there is no source code to re-render): the server has
    already resolved the tree, so a translation failure is final.
    """
    try:
        csg_tree = p.get("csg_tree")
        if not isinstance(csg_tree, list) or not csg_tree:
            return {"error": "No CSG tree received. The MCP server must build it with build_csg_tree() before sending it here."}
        units = str(p.get("units", "mm") or "mm").lower()

        translator = _get_scad_translator()
        design = _design()
        if design is None:
            return {"error": "No active Fusion 360 design. Open or create one first."}

        before_bodies = _snapshot_names(root.bRepBodies)
        before_body_count = root.bRepBodies.count
        before_mesh_count = root.meshBodies.count
        before_sketches = _snapshot_names(root.sketches)
        before_planes = _snapshot_names(root.constructionPlanes)
        before_timeline = design.timeline.count

        try:
            _patch_boolean_types_aliases()
            created_names = translator.translate_to_fusion_commands(
                csg_tree, root, design, units=units)
        except translator.UnsupportedSCADNodeError as e:
            deleted = _cleanup_translation(
                root, before_bodies, before_sketches, before_planes)
            return {
                "error": f"Unsupported CSG node: {e}",
                "unsupported_nodes": [getattr(e, "kind", None) or str(e).split(" ")[0]],
                "partial_bodies_deleted": deleted,
            }
        except Exception as e:
            deleted = _cleanup_translation(
                root, before_bodies, before_sketches, before_planes)
            return {
                "error": f"CSG translation failed: {e}",
                "partial_bodies_deleted": deleted,
            }

        bodies = (root.bRepBodies.count - before_body_count
                  + root.meshBodies.count - before_mesh_count)
        features = design.timeline.count - before_timeline
        if len(created_names) <= 0 and bodies <= 0:
            return {"error": "translation produced no bodies"}
        return {
            "created": True,
            "method": "csg_translation",
            "bodies": bodies,
            "features": features,
            "body_names": created_names,
        }
    except Exception as e:
        return {"error": str(e)}


# cm <-> display units for revolve profiles (mirrors scad_translator
# DEFAULT_UNITS; the server sends the profile already scaled to `units`).
_UNIT_TO_CM = {"mm": 0.1, "cm": 1.0, "in": 2.54}


def _revolve_cross_section(root, p: dict) -> dict:
    """Revolve a half cross-section profile into a solid.

    The profile is the x>=0 half of the mesh's intersection with the Y=0 plane
    (points are [x, z] pairs, already scaled to `units` by the server).

    Empirical revolve findings on this Fusion 2026 build (see notepad T6):
      - Construction-axis revolves (root.zConstructionAxis etc.) ALWAYS raise
        ASM_PATH_TANGENT, regardless of profile/plane.
      - Sketch-line-axis revolves map the sketch 2D frame as (u, v) -> (X, Y):
        an axis line along the sketch's v-axis revolves around world Y, and
        along the u-axis around world X.  On the XY plane this is the identity
        mapping and works correctly; on XZ/YZ a v-direction axis (world Z) is
        misread as a world-Y revolve, so world-Z solids can NOT be made
        directly via sketch lines.
      - A closed profile whose axis-side edge lies ON the sketch-line axis
        revolves fine (profiles=1, no ASM_PATH_TANGENT).

    Recipe (proven): draw the half-profile as (u=radius, v=height) on the XY
    plane with the axis line along the sketch v-axis (world Y), revolve 360
    deg, then rotate each resulting body +90 deg about the world X axis so the
    solid axis maps Y -> Z and the solid lands on the mesh's own Z extent.
    """
    try:
        profile = p.get("profile_pts") or p.get("profile")
        if not isinstance(profile, list) or len(profile) < 3:
            return {"error": "No revolve profile received. Provide 'profile_pts' as a list of [x, z] half-cross-section points."}
        units = str(p.get("units", "mm") or "mm").lower()
        if units not in _UNIT_TO_CM:
            return {"error": f"Unsupported units '{units}'. Use 'mm', 'cm', or 'in'."}
        f = _UNIT_TO_CM[units]
        angle_deg = float(p.get("angle", p.get("angle_deg", 360.0)) or 360.0)

        pts = [[float(x) * f, float(z) * f] for x, z in profile]

        sketch = root.sketches.add(root.xYConstructionPlane)
        sketch.name = "revolved_profile_sketch"
        sketch.isComputeDeferred = False
        lines = sketch.sketchCurves.sketchLines
        # Revolve axis = sketch v-axis = world Y (identity mapping on XY).
        # The profile's axis-side edge (x=0) lies ON this line, which is the
        # Config-E/T14-proven working tangency case for sketch-line axes.
        axis_line = lines.addByTwoPoints(
            adsk.core.Point3D.create(0.0, 0.0, 0.0),
            adsk.core.Point3D.create(0.0, 1.0, 0.0))
        # Closed loop: (u, v) = (radius, height) = mesh (x, z).
        for i in range(len(pts)):
            x1, z1 = pts[i]
            x2, z2 = pts[(i + 1) % len(pts)]
            lines.addByTwoPoints(
                adsk.core.Point3D.create(x1, z1, 0.0),
                adsk.core.Point3D.create(x2, z2, 0.0))
        if sketch.profiles.count == 0:
            sketch.deleteMe()
            return {"error": "The revolve profile points did not form a closed loop."}
        rev_input = root.features.revolveFeatures.createInput(
            sketch.profiles.item(0), axis_line, _op(p.get("operation", "new_body")))
        rev_input.setAngleExtent(
            False, adsk.core.ValueInput.createByReal(math.radians(angle_deg)))
        feature = root.features.revolveFeatures.add(rev_input)
        names = []
        # Map the solid axis Y -> Z: rotate each body +90 deg about world X at
        # the origin (moveFeatures.createInput2 + defineAsFreeMove).
        for i in range(feature.bodies.count):
            body = feature.bodies.item(i)
            bodies_col = adsk.core.ObjectCollection.create()
            bodies_col.add(body)
            transform = adsk.core.Matrix3D.create()
            transform.setToRotation(
                math.radians(90.0),
                adsk.core.Vector3D.create(1.0, 0.0, 0.0),
                adsk.core.Point3D.create(0.0, 0.0, 0.0))
            move_input = root.features.moveFeatures.createInput2(bodies_col)
            move_input.defineAsFreeMove(transform)
            root.features.moveFeatures.add(move_input)
            body.name = f"revolved_{i + 1}"
            names.append(body.name)
        return {
            "created": True,
            "method": "revolve",
            "bodies": len(names),
            "features": 1,
            "names": names,
            "profile_pts": len(pts),
            "angle_deg": angle_deg,
        }
    except Exception as e:
        return {"error": str(e)}


def _find_mesh_body(root, ref) -> "adsk.fusion.MeshBody":
    bodies = root.meshBodies
    if isinstance(ref, int) or (isinstance(ref, str) and str(ref).isdigit()):
        idx = int(ref)
        if 0 <= idx < bodies.count:
            return bodies.item(idx)
        raise Exception(f"Body '{ref}' not found.")
    for i in range(bodies.count):
        if bodies.item(i).name == ref:
            return bodies.item(i)
    # A mesh imported into a child component (import_cad_file
    # as_component=True) lives outside the root collection; name-lookup
    # falls back to every component so those bodies stay reachable.
    for comp in _design().allComponents:
        for i in range(comp.meshBodies.count):
            if comp.meshBodies.item(i).name == ref:
                return comp.meshBodies.item(i)
    raise Exception(f"Body '{ref}' not found.")


def _mesh_convert(root, p: dict) -> dict:
    """Convert a mesh body to a BRep solid via the PREVIEW MeshConvertFeature.

    PREVIEW API (July 2025): `design.features.meshConvertFeatures` is only
    present on recent Fusion builds, and even then the `add()` step is gated
    by the license (live 2026 build raises "3 : No rights for mesh
    conversion.").  Absence / gating is detected through BOTH an
    `hasattr(design.features, "meshConvertFeatures")` guard AND try/except
    around `createInput` AND a rights-check on `add()`; every one of those
    paths returns the same exact not-available error so the caller never
    sees a crash.

    params: {mesh, method (prismatic|organic), operation ("parametric"|"base")}
      method    -> MeshConvertMethodTypes: Faceted=0, Prismatic=1, Organic=2
      operation -> MeshConvertOperationTypes: ParametricFeature=0 (timeline),
                   BaseFeature=1 (dumb body)
    """
    try:
        design = _design()
        if design is None:
            return {"error": "No active Fusion 360 design. Open or create one first."}
        method = str(p.get("method", "organic") or "organic").strip().lower()
        if method not in ("prismatic", "organic"):
            return {"error": "Unsupported method '%s'. Use 'prismatic' or 'organic'." % method}
        operation = str(p.get("operation", "parametric") or "parametric").strip().lower()
        if operation not in ("parametric", "base"):
            return {"error": "Unsupported operation '%s'. Use 'parametric' or 'base'." % operation}

        mesh_body = _find_mesh_body(root, p.get("mesh", 0))
        mesh_features = root.features
        # Absence path 1: API not present on this build.
        if not hasattr(mesh_features, "meshConvertFeatures"):
            return {"error": "MeshConvertFeature not available on this Fusion build (PREVIEW API)"}
        # Absence path 2: present but createInput is unusable.
        try:
            convert_input = mesh_features.meshConvertFeatures.createInput([mesh_body])
        except Exception:
            return {"error": "MeshConvertFeature not available on this Fusion build (PREVIEW API)"}
        try:
            convert_input.meshConvertMethodType = 2 if method == "organic" else 1
            convert_input.meshConvertOperationType = 0 if operation == "parametric" else 1
            feature = mesh_features.meshConvertFeatures.add(convert_input)
        except Exception as e:
            # Rights-gated PREVIEW (live 2026: "No rights for mesh
            # conversion." fires when the input's method/operation types are
            # assigned, not only at add()) -- same not-available contract.
            if "rights" in str(e).lower():
                return {"error": "MeshConvertFeature not available on this Fusion build (PREVIEW API)"}
            return {"error": "mesh convert failed: %s" % e}
        names = []
        for i in range(feature.bodies.count):
            names.append(feature.bodies.item(i).name)
        return {
            "converted": True,
            "bodies": len(names),
            "names": names,
            "method": method,
            "operation": operation,
            "preview_api": True,
        }
    except Exception as e:
        return {"error": str(e)}


def _surface_grid_n(area_cm2, area_per_sample=0.5):
    """Adaptive UV grid density: ~1 sample per `area_per_sample` cm^2.

    grid_n = max(3, ceil(sqrt(area / area_per_sample))).  Used by
    _sample_brep_surface_points to size each BRep face's UV sample grid;
    the default 5 covers faces whose area cannot be read.
    """
    if area_cm2 is None or area_cm2 <= 0:
        return 5
    return max(3, int(math.ceil(math.sqrt(area_cm2 / area_per_sample))))


def _cap_grid_ns(grid_ns, max_points=2000):
    """Scale per-face grid sizes so the total sample count <= max_points.

    The scale factor is sqrt(max_points / naive_total) applied per face,
    floored at 2 so no face collapses to a single point.  The input list is
    returned unchanged when the naive total already fits under the cap.
    """
    naive_total = sum(n * n for n in grid_ns)
    if naive_total <= max_points:
        return list(grid_ns)
    scale = math.sqrt(float(max_points) / float(naive_total))
    return [max(2, int(n * scale)) for n in grid_ns]


def _evaluator_uv_bounds(ev, default=(0.0, 1.0, 0.0, 1.0)):
    """Return (umin, umax, vmin, vmax) for an evaluator's param space.

    Prefers the `parametricRange()` method (returns a BoundingBox2D on this
    Fusion build); falls back to the `paramBounds` property; then to the
    [0,1] x [0,1] default when unavailable or degenerate.
    """
    try:
        b = None
        pr = getattr(ev, "parametricRange", None)
        if callable(pr):
            b = pr()
        if b is None:
            pb = getattr(ev, "paramBounds", None)
            b = pb() if callable(pb) else pb
        if b is None:
            return default
        try:
            umin, vmin = float(b.minPoint.x), float(b.minPoint.y)
            umax, vmax = float(b.maxPoint.x), float(b.maxPoint.y)
        except AttributeError:
            umin, umax, vmin, vmax = (float(x) for x in b)
    except Exception:
        return default
    if not (umax > umin and vmax > vmin):
        return default
    return (umin, umax, vmin, vmax)


def _sample_brep_surface_points(body, max_points=2000, area_per_sample=0.5):
    """Sample a BRep body's face surfaces on an adaptive UV grid.

    Each face gets grid_n = _surface_grid_n(face.area, area_per_sample)
    (~1 sample per 0.5 cm^2, default 5 when the area cannot be read), then
    _cap_grid_ns enforces the max_points total.  Every face's sampling is
    guarded by try/except so unsupported face types are skipped.  Returns a
    list of (x, y, z) tuples -- [] when no face could be sampled (callers
    fall back to vertex-to-vertex measurement).
    """
    points = []
    faces = []
    try:
        for i in range(body.faces.count):
            face = body.faces.item(i)
            try:
                area_cm2 = float(face.area)
            except Exception:
                area_cm2 = None
            ev = face.evaluator
            if not hasattr(ev, "getPointAtParameter"):
                continue
            faces.append((ev, area_cm2))
        grid_ns = _cap_grid_ns(
            [_surface_grid_n(area, area_per_sample) for _ev, area in faces],
            max_points=max_points)
    except Exception:
        return points
    for (ev, _area), grid_n in zip(faces, grid_ns):
        try:
            if grid_n < 2:
                grid_n = 2
            umin, umax, vmin, vmax = _evaluator_uv_bounds(ev)
            if grid_n == 1:
                us = [0.5 * (umin + umax)]
                vs = [0.5 * (vmin + vmax)]
            else:
                us = [umin + (umax - umin) * i / (grid_n - 1) for i in range(grid_n)]
                vs = [vmin + (vmax - vmin) * j / (grid_n - 1) for j in range(grid_n)]
            for u in us:
                for v in vs:
                    p2 = adsk.core.Point2D.create(u, v)
                    ret, pt = ev.getPointAtParameter(p2)
                    if ret:
                        points.append((float(pt.x), float(pt.y), float(pt.z)))
        except Exception:
            continue  # skip faces whose evaluator fails
    return points


def _compare_mesh_brep(root, p: dict) -> dict:
    """Compare a mesh body against a BRep body (vision-free fidelity QA).

    Reports both bodies' enclosed volume (cm^3) and bounding-box spans (cm),
    the mesh/BRep volume ratio, the max per-axis bounding-box span deviation,
    and a sampled surface deviation: ~200 mesh vertices, each measured to the
    nearest point on the BRep surface (mean/max/samples).

    Closest-point API: `SurfaceEvaluator.getClosestPointTo` was probed LIVE on
    this Fusion 2026 build (Metis F6) and is NOT available -- the evaluators
    only expose getParameterAtPoint / getPointAtParameter / getNormalAtPoint
    (see the T8 notepad section).  When the API exists the sampled deviation
    uses it and the response reports "method": "surface_evaluator"; on this
    build the fallback samples each BRep face's surface on an adaptive UV grid
    (~1 point per 0.5 cm^2, total capped at 2000) via getPointAtParameter,
    measures every sampled mesh vertex to its nearest surface point, and
    reports "method": "vertex_fallback" (if no face can be sampled at all, the
    original vertex-to-vertex measurement runs as an inner catch-all).
    """
    try:
        mesh_ref = p.get("mesh", "0")
        body_ref = p.get("body", "0")
        mesh_body = _find_mesh_body(root, mesh_ref)
        body = _require_body(root, body_ref)

        mesh_vol = float(mesh_body.volume)
        mesh_bbox = mesh_body.boundingBox
        mesh_min = [mesh_bbox.minPoint.x, mesh_bbox.minPoint.y, mesh_bbox.minPoint.z]
        mesh_max = [mesh_bbox.maxPoint.x, mesh_bbox.maxPoint.y, mesh_bbox.maxPoint.z]

        brep_vol = float(body.physicalProperties.volume)
        brep_bbox = body.boundingBox
        brep_min = [brep_bbox.minPoint.x, brep_bbox.minPoint.y, brep_bbox.minPoint.z]
        brep_max = [brep_bbox.maxPoint.x, brep_bbox.maxPoint.y, brep_bbox.maxPoint.z]

        if brep_vol == 0.0:
            return {"error": "BRep body '%s' has zero volume; cannot compute volume_ratio." % body_ref}
        volume_ratio = mesh_vol / brep_vol

        bbox_max_deviation = max(
            abs((mesh_max[i] - mesh_min[i]) - (brep_max[i] - brep_min[i]))
            for i in range(3))

        # Sampled deviation: stride through the mesh's node list toward ~200
        # samples; each node measured to the nearest point on the BRep.
        mesh = mesh_body.displayMesh
        node_coords = list(mesh.nodeCoordinates)
        count = len(node_coords)
        target = 200
        stride = max(1, int(math.ceil(count / target))) if count else 1
        sample_indices = list(range(0, count, stride))
        if count and sample_indices[-1] != count - 1:
            sample_indices.append(count - 1)

        # Closest-point API availability (probed live: absent on this build).
        face_evaluators = []
        for i in range(body.faces.count):
            ev = body.faces.item(i).evaluator
            if hasattr(ev, "getClosestPointTo"):
                face_evaluators.append(ev)
        method = "surface_evaluator" if face_evaluators else "vertex_fallback"

        # Fallback: sample the BRep face surfaces on an adaptive UV grid and
        # measure mesh vertices against those samples.  On this build the
        # evaluators only expose getPointAtParameter (no getClosestPointTo),
        # so each face is sampled ~1 point per 0.5 cm^2, total capped at 2000.
        brep_surface_points = []
        if not face_evaluators:
            brep_surface_points = _sample_brep_surface_points(body)

        brep_verts = []
        if not face_evaluators and not brep_surface_points:
            # Inner catch-all: no face evaluator could be sampled; fall back
            # to the original vertex-to-vertex measurement.
            for i in range(body.vertices.count):
                v = body.vertices.item(i).geometry
                brep_verts.append((v.x, v.y, v.z))

        distances = []
        for idx in sample_indices:
            pt = node_coords[idx]
            px, py, pz = pt.x, pt.y, pt.z
            try:
                if face_evaluators:
                    best = None
                    for ev in face_evaluators:
                        _ret, cp = ev.getClosestPointTo(pt)
                        d = math.sqrt((px - cp.x) ** 2 + (py - cp.y) ** 2 + (pz - cp.z) ** 2)
                        if best is None or d < best:
                            best = d
                elif brep_surface_points:
                    best = min(
                        math.sqrt((px - bx) ** 2 + (py - by) ** 2 + (pz - bz) ** 2)
                        for bx, by, bz in brep_surface_points)
                else:
                    best = min(
                        math.sqrt((px - bx) ** 2 + (py - by) ** 2 + (pz - bz) ** 2)
                        for bx, by, bz in brep_verts)
            except Exception:
                continue  # per-vertex failures are skipped (counted via samples)
            if best is not None:
                distances.append(best)

        if not distances:
            return {"error": "No mesh vertices could be sampled on mesh body '%s'." % mesh_ref}
        return {
            "mesh": {
                "volume_cm3": round(mesh_vol, 6),
                "bbox_cm": [[round(v, 6) for v in mesh_min],
                            [round(v, 6) for v in mesh_max]],
            },
            "brep": {
                "volume_cm3": round(brep_vol, 6),
                "bbox_cm": [[round(v, 6) for v in brep_min],
                            [round(v, 6) for v in brep_max]],
            },
            "volume_ratio": round(volume_ratio, 6),
            "bbox_max_deviation_cm": round(bbox_max_deviation, 6),
            "sampled_deviation_cm": {
                "mean": round(sum(distances) / len(distances), 6),
                "max": round(max(distances), 6),
                "samples": len(distances),
            },
            "method": method,
        }
    except Exception as e:
        return {"error": str(e)}


def _create_sketch_from_polygon(root, p: dict) -> dict:
    """Create a sketch from polygon vertices and extrude it.

    Receives one or more polygon vertex lists (each a closed loop of
    [x, y, z] points in display units) and creates a native Fusion sketch
    on a construction plane parallel to XY at *plane_height*, then
    optionally extrudes each closed profile.  The x,y components are used
    as sketch-space coordinates; z is ignored (the construction plane
    handles the vertical position).  Coordinates are converted from the
    requested display *units* to Fusion's internal cm.

    params:
      polygons:          list of polygon vertex lists, each [[x,y,z], ...].
      plane_height:      Z height for the sketch plane (display units, default 0).
      extrude_height:    extrusion distance (display units, 0 = no extrude).
      extrude_direction: "positive" (default), "negative", or "symmetric".
      units:             mm / cm / in (display units, converted to cm internally).
      operation:         "new_body" (default), "join", "cut".
      target_body:       body name/index for join/cut operations.
    """
    try:
        polygons = p.get("polygons") or p.get("polygon")
        if not polygons or not isinstance(polygons, list):
            return {"error": "No polygons provided. Provide 'polygons' as a list of vertex lists."}
        # Accept a single polygon (flat vertex list) for convenience.
        if polygons and isinstance(polygons[0], (int, float)):
            polygons = [polygons]

        units = str(p.get("units", "cm") or "cm").lower()
        if units not in _UNIT_TO_CM:
            return {"error": f"Unsupported units '{units}'. Use 'mm', 'cm', or 'in'."}
        f = _UNIT_TO_CM[units]

        plane_height_cm = float(p.get("plane_height", 0)) * f
        extrude_height_raw = float(p.get("extrude_height", 0))
        extrude_direction = str(p.get("extrude_direction", "positive")).strip().lower()
        operation = p.get("operation", "new_body")

        design = _design()
        if design is None:
            return {"error": "No active Fusion 360 design. Open or create one first."}

        before_body_count = root.bRepBodies.count
        before_timeline = design.timeline.count

        if abs(plane_height_cm) > 1e-9:
            planes = root.constructionPlanes
            plane_input = planes.createInput()
            plane_input.setByOffset(
                root.xYConstructionPlane,
                adsk.core.ValueInput.createByReal(plane_height_cm))
            sketch_plane = planes.add(plane_input)
        else:
            sketch_plane = root.xYConstructionPlane

        sketch = root.sketches.add(sketch_plane)
        sketch.name = "face_polygon_sketch"
        sketch.isComputeDeferred = False
        lines = sketch.sketchCurves.sketchLines

        for poly in polygons:
            if not isinstance(poly, list) or len(poly) < 3:
                continue
            pts = []
            for vertex in poly:
                if isinstance(vertex, (int, float)):
                    continue
                x = float(vertex[0]) * f
                y = float(vertex[1]) * f
                pts.append(adsk.core.Point3D.create(x, y, 0.0))
            if len(pts) < 3:
                continue
            for i in range(len(pts)):
                lines.addByTwoPoints(pts[i], pts[(i + 1) % len(pts)])

        if sketch.profiles.count == 0:
            sketch.deleteMe()
            return {"error": "The polygon vertices did not form any closed profile."}

        body_names = []
        if extrude_height_raw > 0:
            height_cm = extrude_height_raw * f
            signed_height = height_cm
            is_symmetric = False
            if extrude_direction == "symmetric":
                is_symmetric = True
            elif extrude_direction == "negative":
                signed_height = -height_cm

            for i in range(sketch.profiles.count):
                profile = sketch.profiles.item(i)
                ext_input = root.features.extrudeFeatures.createInput(
                    profile, _op(operation))
                ext_input.setDistanceExtent(
                    is_symmetric,
                    adsk.core.ValueInput.createByReal(signed_height))
                feature = root.features.extrudeFeatures.add(ext_input)
                for j in range(feature.bodies.count):
                    body_names.append(feature.bodies.item(j).name)

        bodies_created = root.bRepBodies.count - before_body_count
        features_created = design.timeline.count - before_timeline
        body_name = body_names[0] if body_names else ""

        return {
            "created": True,
            "method": "polygon_extrude",
            "bodies": bodies_created,
            "features": features_created,
            "bodies_created": bodies_created,
            "body_name": body_name,
            "sketch_name": sketch.name,
        }
    except Exception as e:
        return {"error": str(e)}


def _update_scad_body(root, p: dict) -> dict:
    try:
        ref = p.get("body", 0)
        body = _find_mesh_body(root, ref)
        attr = body.attributes.itemByName("FusionMCP", "scad_source")
        if attr is None:
            return {"error": "Body does not have stored SCAD source."}
        code = str(p.get("code", "") or "").strip()
        if not code:
            code = attr.value
        params = str(p.get("params", "") or "")
        old_name = body.name
        body.deleteMe()
        result = _run_scad(root, {"code": code, "params": params})
        if not result.get("rendered"):
            return result
        new_name = result.get("mesh_body")
        try:
            new_body = _find_mesh_body(root, new_name)
            new_body.name = old_name
            new_name = old_name
        except Exception:
            pass
        return {
            "updated": True,
            "mesh_body": new_name,
            "mesh_body_name": new_name,
            "params_used": params,
            "index_shift_warning": "Body indices may have shifted after update. Use body name for future references.",
        }
    except Exception as e:
        return {"error": str(e)}


def _import_mesh_data(root, p: dict) -> dict:
    try:
        coordinates = p.get("coordinates", []) or []
        triangle_indices = p.get("triangle_indices", []) or []
        normals = p.get("normals", []) or []
        normal_indices = p.get("normal_indices", []) or []
        if not coordinates:
            return {"error": "No coordinates provided. Provide a flat list [x0,y0,z0,x1,y1,z1,...]."}
        if len(coordinates) % 3 != 0:
            return {"error": "coordinates length must be divisible by 3 (x,y,z triples)."}
        if not triangle_indices:
            return {"error": "No triangle_indices provided. Provide a flat list of vertex index triples."}
        if len(triangle_indices) % 3 != 0:
            return {"error": "triangle_indices length must be divisible by 3 (each triangle is 3 vertex indices)."}
        vertex_count = len(coordinates) // 3
        if min(triangle_indices) < 0 or max(triangle_indices) >= vertex_count:
            return {"error": "triangle_indices reference vertices out of range."}

        if not normals:
            normals = []
            normal_indices = list(triangle_indices)
            for t in range(len(triangle_indices) // 3):
                i0, i1, i2 = triangle_indices[3 * t], triangle_indices[3 * t + 1], triangle_indices[3 * t + 2]
                ax, ay, az = coordinates[3 * i0], coordinates[3 * i0 + 1], coordinates[3 * i0 + 2]
                bx, by, bz = coordinates[3 * i1], coordinates[3 * i1 + 1], coordinates[3 * i1 + 2]
                cx, cy, cz = coordinates[3 * i2], coordinates[3 * i2 + 1], coordinates[3 * i2 + 2]
                ux, uy, uz = bx - ax, by - ay, bz - az
                vx, vy, vz = cx - ax, cy - ay, cz - az
                nx, ny, nz = uy * vz - uz * vy, uz * vx - ux * vz, ux * vy - uy * vx
                length = math.sqrt(nx * nx + ny * ny + nz * nz)
                if length == 0:
                    return {"error": f"Degenerate triangle at index {t}: zero-area triangle."}
                nx, ny, nz = nx / length, ny / length, nz / length
                normals.extend([nx, ny, nz, nx, ny, nz, nx, ny, nz])
        else:
            if len(normals) % 3 != 0:
                return {"error": "normals length must be divisible by 3 (x,y,z triples)."}
            if not normal_indices:
                normal_indices = list(range(len(normals)))
            if len(normal_indices) % 3 != 0:
                return {"error": "normal_indices length must be divisible by 3 (3 indices per triangle)."}

        mesh_body = None
        if _design().designType == adsk.fusion.DesignTypes.ParametricDesignType:
            base_features = root.features.baseFeatures
            base_feature = base_features.item(0) if base_features.count > 0 else base_features.add()
            try:
                base_feature.startEdit()
            except Exception:
                base_feature = None
            if base_feature is not None:
                try:
                    mesh_body = root.meshBodies.addByTriangleMeshData(
                        coordinates, triangle_indices, normals, normal_indices)
                finally:
                    base_feature.finishEdit()
        if mesh_body is None:
            mesh_body = root.meshBodies.addByTriangleMeshData(
                coordinates, triangle_indices, normals, normal_indices)
        name = str(p.get("name", "") or "")
        if name:
            try:
                mesh_body.name = name
            except Exception:
                pass
        return {
            "created_mesh": mesh_body.name,
            "vertex_count": vertex_count,
            "triangle_count": len(triangle_indices) // 3,
        }
    except Exception as e:
        return {"error": str(e)}


def _extract_mesh_data(root, p: dict) -> dict:
    try:
        ref = p.get("mesh", "0")
        body = _find_mesh_body(root, ref)
        mesh = body.displayMesh
        nodes = []
        for pt in mesh.nodeCoordinates:
            nodes.extend([round(pt.x, 6), round(pt.y, 6), round(pt.z, 6)])
        indices = [int(i) for i in mesh.nodeIndices]
        normals = []
        for vec in mesh.normalVectors:
            normals.extend([round(vec.x, 6), round(vec.y, 6), round(vec.z, 6)])
        return {
            "mesh": body.name,
            "vertex_count": int(mesh.nodeCount),
            "triangle_count": int(mesh.triangleCount),
            "nodes": nodes,
            "indices": indices,
            "normals": normals,
        }
    except Exception as e:
        if "not found" in str(e):
            return {"error": f"Mesh body '{ref}' not found."}
        return {"error": str(e)}


# ---- Export & Capture ----

def _export_stl(p):
    design = _design()
    path = p.get("path", os.path.expanduser("~/Desktop/fusion_export.stl"))
    opts = design.exportManager.createSTLExportOptions(design.rootComponent, path)
    opts.meshRefinement = adsk.fusion.MeshRefinementSettings.MeshRefinementHigh
    design.exportManager.execute(opts)
    return {"exported_stl": path}

def _export_step(p):
    design = _design()
    path = p.get("path", os.path.expanduser("~/Desktop/fusion_export.step"))
    opts = design.exportManager.createSTEPExportOptions(path, design.rootComponent)
    design.exportManager.execute(opts)
    return {"exported_step": path}

def _export_3mf(p):
    design = _design()
    path = p.get("path", os.path.expanduser("~/Desktop/fusion_export.3mf"))
    opts = design.exportManager.createC3MFExportOptions(design.rootComponent, path)
    design.exportManager.execute(opts)
    return {"exported_3mf": path}

def _export_f3d(p):
    design = _design()
    path = p.get("path", os.path.expanduser("~/Desktop/fusion_export.f3d"))
    opts = design.exportManager.createFusionArchiveExportOptions(path)
    design.exportManager.execute(opts)
    return {"exported_f3d": path}

def _capture_screenshot(p):
    path = p.get("path", os.path.expanduser("~/Desktop/fusion_screenshot.png"))
    width = int(p.get("width", 1920))
    height = int(p.get("height", 1080))
    app.activeViewport.saveAsImageFile(path, width, height)
    with open(path, "rb") as f:
        png_bytes = f.read()
    return {
        "screenshot": path,
        "size": f"{width}x{height}",
        "image_base64": base64.b64encode(png_bytes).decode("ascii"),
    }


_VIEW_ORIENTATIONS = {
    "isometric": adsk.core.ViewOrientations.IsoTopRightViewOrientation,
    "front": adsk.core.ViewOrientations.FrontViewOrientation,
    "top": adsk.core.ViewOrientations.TopViewOrientation,
    "right": adsk.core.ViewOrientations.RightViewOrientation,
}

def _capture_mesh_views(root, p: dict) -> dict:
    """Capture 4 viewport screenshots of a mesh for vision-guided annotation.

    For each view name (isometric / front / top / right) the viewport camera
    is re-oriented and fit to the model, then saved as a PNG via the same
    saveAsImageFile -> read-bytes -> base64 mechanism as _capture_screenshot.
    The isometric orientation is restored after the loop.  View names are
    validated FIRST (before any viewport churn); the mesh is resolved via
    _find_mesh_body so a missing mesh errors before any capture.  Returns
    {"mesh": <resolved mesh name>, "views": [{"view", "image_base64"}, ...]}.
    """
    try:
        view_names = p.get("views", ["isometric", "front", "top", "right"])
        if isinstance(view_names, str):
            view_names = [view_names]
        for v in view_names:
            if v not in _VIEW_ORIENTATIONS:
                return {"error": f"Unknown view '{v}'. Use isometric, front, top, right"}
        ref = p.get("mesh", "0")
        body = _find_mesh_body(root, ref)
        width = int(p.get("width", 1280))
        height = int(p.get("height", 720))
        viewport = app.activeViewport
        captured = []
        for v in view_names:
            camera = viewport.camera
            camera.viewOrientation = _VIEW_ORIENTATIONS[v]
            camera.isFitView = True
            viewport.camera = camera
            path = os.path.join(tempfile.gettempdir(), f"fusion_view_{v}.png")
            viewport.saveAsImageFile(path, width, height)
            with open(path, "rb") as f:
                png_bytes = f.read()
            captured.append({
                "view": v,
                "image_base64": base64.b64encode(png_bytes).decode("ascii"),
            })
        camera = viewport.camera
        camera.viewOrientation = _VIEW_ORIENTATIONS["isometric"]
        camera.isFitView = True
        viewport.camera = camera
        return {"mesh": body.name, "views": captured}
    except Exception as e:
        if "not found" in str(e):
            return {"error": f"Mesh body '{ref}' not found."}
        return {"error": str(e)}


def _capture_body_views(root, p: dict) -> dict:
    """Capture viewport screenshots of a BRep body (body-targeted variant of
    _capture_mesh_views) for vision QA review.

    Identical shape to _capture_mesh_views: view names are validated FIRST
    (before any viewport churn) with the same exact error string, the body is
    resolved via _require_body (BRep), and the same viewport orientation /
    capture loop runs (sharing _VIEW_ORIENTATIONS and the
    saveAsImageFile -> read-bytes -> base64 mechanism of _capture_screenshot).
    The isometric orientation is restored after the loop.  Returns
    {"body": <resolved body name>, "views": [{"view", "image_base64"}, ...]}.
    """
    try:
        view_names = p.get("views", ["isometric", "front", "top", "right"])
        if isinstance(view_names, str):
            view_names = [view_names]
        for v in view_names:
            if v not in _VIEW_ORIENTATIONS:
                return {"error": f"Unknown view '{v}'. Use isometric, front, top, right"}
        ref = p.get("body", "0")
        body = _require_body(root, ref)
        width = int(p.get("width", 1280))
        height = int(p.get("height", 720))
        viewport = app.activeViewport
        captured = []
        for v in view_names:
            camera = viewport.camera
            camera.viewOrientation = _VIEW_ORIENTATIONS[v]
            camera.isFitView = True
            viewport.camera = camera
            path = os.path.join(tempfile.gettempdir(), f"fusion_body_view_{v}.png")
            viewport.saveAsImageFile(path, width, height)
            with open(path, "rb") as f:
                png_bytes = f.read()
            captured.append({
                "view": v,
                "image_base64": base64.b64encode(png_bytes).decode("ascii"),
            })
        camera = viewport.camera
        camera.viewOrientation = _VIEW_ORIENTATIONS["isometric"]
        camera.isFitView = True
        viewport.camera = camera
        return {"body": body.name, "views": captured}
    except Exception as e:
        if "not found" in str(e):
            return {"error": f"Body '{ref}' not found."}
        return {"error": str(e)}


# ---- History ----

def _undo(p):
    count = int(p.get("steps", 1))
    for _ in range(count):
        app.executeTextCommand("Commands.Undo")
    return {"undone": count}

def _redo(p):
    count = int(p.get("steps", 1))
    for _ in range(count):
        app.executeTextCommand("Commands.Redo")
    return {"redone": count}

def _save_design(p):
    doc = app.activeDocument
    try:
        doc.save(p.get("description", "Saved"))
        return {"saved": doc.name}
    except Exception as e:
        if "saveAs" in str(e) or "save" in str(e).lower():
            return {"error": "Document has never been saved. Use save_as first."}
        return {"error": str(e)}

def _save_as(p):
    doc = app.activeDocument
    name = p.get("name", "My Design")
    try:
        data_mgr = app.data
        active_hub = data_mgr.activeHub
        root_folder = active_hub.dataProjects.item(0).rootFolder
        doc.saveAs(name, root_folder, p.get("description", "Saved"), "")
        return {"saved_as": name}
    except Exception as e:
        return {"error": str(e)}


# ---- Dispatcher ----

def _process_command(data: dict) -> dict:
    try:
        design = _design()
        if not design:
            return {"error": "No active Fusion 360 design. Open or create one first."}
        root = design.rootComponent
        cmd = data.get("command", "")
        p = data.get("params", {})

        dispatch = {
            "get_info":                   lambda: _get_info(design, root),
            "get_bodies_info":            lambda: _get_bodies_info(root),
            "get_face_info":              lambda: _get_face_info(root, p),
            "get_edge_info":              lambda: _get_edge_info(root, p),
            "get_sketch_info":            lambda: _get_sketch_info(root, p),
            "get_timeline_info":          lambda: _get_timeline_info(),
            "measure_body":               lambda: _measure_body(root, p),
            "measure_between":            lambda: _measure_between(root, p),
            "measure_angle":              lambda: _measure_angle(root, p),
            "get_physical_properties":    lambda: _get_physical_properties(root, p),
            "get_oriented_bounding_box":  lambda: _get_oriented_bounding_box(root, p),
            "inspect_body":               lambda: _inspect_body(root, p),
            "execute_script":             lambda: _execute_script(p),
            "create_new_document":        lambda: _create_new_document(p),
            "clear_design":               lambda: _clear_design(),
            "create_sketch":              lambda: _create_sketch(root, p),
            "create_sketch_on_face":      lambda: _create_sketch_on_face(root, p),
            "finish_sketch":              lambda: _finish_sketch(root, p),
            "delete_sketch":              lambda: _delete_sketch(root, p),
            "draw_rectangle":             lambda: _draw_rectangle(root, p),
            "draw_center_rectangle":      lambda: _draw_center_rectangle(root, p),
            "draw_circle":                lambda: _draw_circle(root, p),
            "draw_line":                  lambda: _draw_line(root, p),
            "draw_arc":                   lambda: _draw_arc(root, p),
            "draw_polygon":               lambda: _draw_polygon(root, p),
            "draw_ellipse":               lambda: _draw_ellipse(root, p),
            "draw_spline":                lambda: _draw_spline(root, p),
            "draw_slot":                  lambda: _draw_slot(root, p),
            "draw_text":                  lambda: _draw_text(root, p),
            "add_sketch_fillet":          lambda: _add_sketch_fillet(root, p),
            "offset_sketch":              lambda: _offset_sketch(root, p),
            "mirror_sketch":              lambda: _mirror_sketch(root, p),
            "rectangular_pattern_sketch": lambda: _rectangular_pattern_sketch(root, p),
            "add_constraint":             lambda: _add_constraint(root, p),
            "add_sketch_dimension":       lambda: _add_sketch_dimension(root, p),
            "extrude":                    lambda: _extrude(root, p),
            "revolve":                    lambda: _revolve(root, p),
            "loft":                       lambda: _loft(root, p),
            "sweep":                      lambda: _sweep(root, p),
            "helix":                      lambda: _helix(root, p),
            "create_pipe":                lambda: _create_pipe(root, p),
            "create_hole":                lambda: _create_hole(root, p),
            "shell":                      lambda: _shell(root, p),
            "fillet":                     lambda: _fillet(root, p),
            "chamfer":                    lambda: _chamfer(root, p),
            "mirror_body":                lambda: _mirror_body(root, p),
            "rectangular_pattern_body":   lambda: _rectangular_pattern_body(root, p),
            "circular_pattern_body":      lambda: _circular_pattern_body(root, p),
            "combine_bodies":             lambda: _combine_bodies(root, p),
            "scale_body":                 lambda: _scale_body(root, p),
            "move_body":                  lambda: _move_body(root, p),
            "rotate_body":               lambda: _rotate_body(root, p),
            "press_pull":                 lambda: _press_pull(root, p),
            "thicken":                    lambda: _thicken(root, p),
            "draft_face":                 lambda: _draft_face(root, p),
            "add_thread":                 lambda: _add_thread(root, p),
            "create_component":           lambda: _create_component(root, p),
            "move_body_to_component":     lambda: _move_body_to_component(root, p),
            "create_joint":               lambda: _create_joint(root, p),
            "create_as_built_joint":      lambda: _create_as_built_joint(root, p),
            "delete_body":                lambda: _delete_body(root, p),
            "rename_body":                lambda: _rename_body(root, p),
            "copy_body":                  lambda: _copy_body(root, p),
            "toggle_body_visibility":     lambda: _toggle_body_visibility(root, p),
            "add_construction_plane":     lambda: _add_construction_plane(root, p),
            "add_construction_axis":      lambda: _add_construction_axis(root, p),
            "add_parameter":              lambda: _add_parameter(design, p),
            "update_parameter":           lambda: _update_parameter(design, p),
            "list_parameters":            lambda: _list_parameters(design),
            "list_appearances":           lambda: _list_appearances(p),
            "apply_appearance":           lambda: _apply_appearance(root, p),
            "set_body_color":             lambda: _set_body_color(root, p),
            "export_stl":                 lambda: _export_stl(p),
            "export_step":                lambda: _export_step(p),
            "export_3mf":                 lambda: _export_3mf(p),
            "export_f3d":                 lambda: _export_f3d(p),
            "capture_screenshot":         lambda: _capture_screenshot(p),
            "capture_mesh_views":         lambda: _capture_mesh_views(root, p),
            "capture_body_views":         lambda: _capture_body_views(root, p),
            "undo":                       lambda: _undo(p),
            "redo":                       lambda: _redo(p),
            "save":                       lambda: _save_design(p),
            "save_as":                    lambda: _save_as(p),
            "export_obj":                 lambda: {"error": "OBJ export is not supported by the Fusion 360 API. Use STL, STEP, or 3MF instead."},
            "import_cad_file":            lambda: _import_cad_file(root, p),
            "import_mesh_file":           lambda: _import_mesh_file(root, p),
            "import_sketch_file":         lambda: _import_sketch_file(root, p),
            "run_scad":                   lambda: _run_scad(root, p),
            "update_scad_body":           lambda: _update_scad_body(root, p),
            "import_mesh_data":           lambda: _import_mesh_data(root, p),
            "extract_mesh_data":          lambda: _extract_mesh_data(root, p),
            "create_from_scad":           lambda: _create_from_scad(root, p),
            "create_from_csg_tree":       lambda: _create_from_csg_tree(root, p),
            "revolve_cross_section":      lambda: _revolve_cross_section(root, p),
            "mesh_convert":               lambda: _mesh_convert(root, p),
            "compare_mesh_brep":          lambda: _compare_mesh_brep(root, p),
            "create_sketch_from_polygon": lambda: _create_sketch_from_polygon(root, p),
        }

        if cmd in dispatch:
            return dispatch[cmd]()
        return {"error": f"Unknown command '{cmd}'",
                "available_commands": sorted(dispatch.keys())}
    except Exception:
        return {"error": traceback.format_exc()}


# ---- Event Handler ----

class MCPEventHandler(adsk.core.CustomEventHandler):
    def __init__(self):
        super().__init__()

    def notify(self, args):
        try:
            while not _cmd_queue.empty():
                data = _cmd_queue.get_nowait()
                cmd_id = data.get("id")
                result = _process_command(data)
                _results[cmd_id] = result
                if cmd_id in _result_events:
                    _result_events[cmd_id].set()
        except Exception:
            pass


# ---- Add-in Lifecycle ----

def run(context):
    global app, ui, _server, _server_thread
    try:
        app = adsk.core.Application.get()
        ui = app.userInterface
        custom_event = app.registerCustomEvent(CUSTOM_EVENT_ID)
        handler = MCPEventHandler()
        custom_event.add(handler)
        _handlers.append(handler)
        _server = HTTPServer(("127.0.0.1", PORT), MCPRequestHandler)
        _server_thread = threading.Thread(target=_server.serve_forever, daemon=True)
        _server_thread.start()
        ui.messageBox(
            "FusionMCP bridge is running on port 7432.\n\n"
            "You can now connect any MCP client.",
            "FusionMCP")
    except Exception:
        if ui:
            ui.messageBox(f"FusionMCP failed to start:\n{traceback.format_exc()}")


def stop(context):
    global _server
    try:
        if _server:
            _server.shutdown()
        app.unregisterCustomEvent(CUSTOM_EVENT_ID)
        for h in _handlers:
            del h
        _handlers.clear()
    except Exception:
        pass
