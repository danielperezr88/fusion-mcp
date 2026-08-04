#!/usr/bin/env python3
"""
Live integration tests for the Wave 1 dimensioning tools.

Exercises, against a RUNNING Fusion 360 with the FusionMCP bridge on port 7432:
  1. 5x3x2 cm box fixture (create_sketch + draw_rectangle + extrude)
  2. get_physical_properties  -> volume ~30.0 cm3, area ~62.0 cm2, CoM ~[2.5, 1.5, 1.0]
  3. measure_angle between two adjacent box faces -> ~90.0 deg
  4. get_oriented_bounding_box("0", "X", "Y") -> length/width/height ~5/3/2
  5. cylinder (draw_circle r=3 + extrude 6) + inspect_body -> cylindrical face
     radius ~3.0 and a Circle3D edge
  6. failure path: get_physical_properties(body="999") -> error containing "not found"

Each check prints PASS/FAIL; the script exits non-zero if any check fails and
prints a final "<passed>/<total> checks PASSED" summary. A dead bridge yields a
clear "connection error" FAIL instead of a traceback (plan QA line 207).

MODE:
  --mode fresh (default): the four NEW dimensioning commands
      (get_physical_properties / measure_angle / get_oriented_bounding_box /
      inspect_body) are executed inside Fusion via the execute_script
      fresh-module pattern: the LOCAL FusionMCP.py is loaded as a module
      (mod.app = app first), then the real handler is invoked. Required because
      the RUNNING add-in serves the OLD command set (probe: "Unknown command
      'get_physical_properties'"). Fixture tools (create_sketch, draw_*,
      extrude) and get_face_info DO exist in the old add-in and are called
      directly over HTTP in both modes.
  --mode http: call the new commands directly over the 7432 bridge. This only
      works if the add-in has been synced with the new FusionMCP.py; against
      the current (old) add-in the new-command checks FAIL honestly with
      "Unknown command".

Usage:
  py tests/test_dimensioning_live.py [--mode fresh|http]
"""

import json
import os
import sys

import requests

BASE_URL = "http://127.0.0.1:7432/command"
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FUSION_MCP_PATH = os.path.join(REPO_ROOT, "FusionMCP.py")

# Tolerances (absolute, in the tool's units: cm / cm2 / cm3 / degrees)
TOL = 0.05

_passed = 0
_failed = 0


class BridgeError(Exception):
    """Raised when the Fusion HTTP bridge cannot be reached."""


def call(command, params=None):
    """POST a command to the Fusion bridge and return the parsed JSON dict."""
    params = params or {}
    try:
        r = requests.post(BASE_URL, json={"command": command, "params": params},
                          timeout=60)
    except requests.exceptions.ConnectionError:
        raise BridgeError(
            "Cannot reach Fusion 360 on http://127.0.0.1:7432 - is Fusion open "
            "and the FusionMCP add-in running?")
    except requests.exceptions.RequestException as e:
        raise BridgeError(f"HTTP request to Fusion bridge failed: {e}")
    try:
        return r.json()
    except ValueError as e:
        raise BridgeError(f"Bridge returned invalid JSON: {e}")


def fresh_exec(command, params):
    """Run a NEW-command handler inside Fusion via the fresh-module pattern.

    Loads the LOCAL FusionMCP.py as module 'fusionmcp_dev' inside Fusion,
    sets mod.app = app (module-level 'app' is None outside run()), then
    dispatches through _process_command so the full handler path is exercised.
    """
    payload = json.dumps(params)
    code = (
        "import json as _json\n"
        "import importlib.util as _ilu\n"
        "import sys as _sys\n"
        "_spec = _ilu.spec_from_file_location('fusionmcp_dev', %r)\n"
        "_mod = _ilu.module_from_spec(_spec)\n"
        "_sys.modules['fusionmcp_dev'] = _mod\n"
        "_spec.loader.exec_module(_mod)\n"
        "_mod.app = app\n"
        "_params = _json.loads(%r)\n"
        "_out = _mod._process_command({'command': %r, 'params': _params})\n"
        "result['output'] = _json.dumps(_out)\n"
    ) % (FUSION_MCP_PATH, payload, command)
    resp = call("execute_script", {"code": code})
    if isinstance(resp, dict) and "error" in resp:
        return {"error": resp["error"]}
    try:
        return json.loads(resp.get("output"))
    except (TypeError, ValueError) as e:
        raise BridgeError(f"Could not parse fresh-exec output: {e}")


def run_code(code):
    """Run Python inside Fusion via execute_script; returns parsed output."""
    resp = call("execute_script", {"code": code})
    if isinstance(resp, dict) and "error" in resp:
        raise RuntimeError(
            "execute_script failed inside Fusion:\n" + str(resp["error"])[:2000])
    return resp.get("output")


# ---- document lifecycle helpers (close test-created docs, never leak) ----

def _open_doc_ids():
    """Snapshot currently-open Fusion document ids via execute_script.

    Returns None when the snapshot cannot be taken (bridge hiccup) so the
    caller skips cleanup instead of ever closing pre-existing documents.
    """
    try:
        out = run_code(
            "_ids = []\n"
            "for _i in range(app.documents.count):\n"
            "    _ids.append(app.documents.item(_i).creationId)\n"
            "result['output'] = _ids\n")
    except Exception:
        return None
    return out if isinstance(out, list) else None


def _close_docs_except(pre_ids):
    """Close every open Fusion document whose id is NOT in pre_ids.

    Collects the ids to close FIRST, then re-finds each document before
    closing (indices shift as documents close -- never close while iterating
    by index). Each close is guarded so an already-closed document or an API
    hiccup never fails the test; cleanup never raises. Returns how many
    documents were closed.
    """
    if pre_ids is None:
        return 0
    try:
        n = run_code(
            "_pre = %s\n"
            "_ids = []\n"
            "for _i in range(app.documents.count):\n"
            "    _d = app.documents.item(_i)\n"
            "    if _d.creationId not in _pre:\n"
            "        _ids.append(_d.creationId)\n"
            "_closed = 0\n"
            "for _did in _ids:\n"
            "    for _i in range(app.documents.count):\n"
            "        _d = app.documents.item(_i)\n"
            "        if _d.creationId == _did:\n"
            "            try:\n"
            "                _d.close(False)\n"
            "                _closed += 1\n"
            "            except Exception:\n"
            "                pass\n"
            "            break\n"
            "result['output'] = _closed\n" % repr(pre_ids))
    except Exception as e:
        print(f"[cleanup] document close skipped: {e}")
        return 0
    print(f"[cleanup] closed {n or 0} test document(s)")
    return n or 0


def dimension_call(command, params):
    """Call a dimensioning command, honoring the selected mode."""
    if MODE == "http":
        return call(command, params)
    return fresh_exec(command, params)


def approx(actual, expected, tol=TOL):
    return abs(actual - expected) <= tol


def _assert_ok(resp, what):
    if isinstance(resp, dict) and "error" in resp:
        raise AssertionError(f"{what} failed: {str(resp['error'])[:300]}")
    return resp


def check(name, ok, detail):
    global _passed, _failed
    status = "PASS" if ok else "FAIL"
    print(f"[{status}] {name}: {detail}")
    if ok:
        _passed += 1
    else:
        _failed += 1


def run_check(name, fn):
    try:
        detail = fn()
        check(name, True, detail)
    except BridgeError as e:
        check(name, False, f"connection error: {e}")
    except Exception as e:
        check(name, False, f"exception: {e}")


# ---- Checks ----

def check_fresh_document():
    resp = _assert_ok(call("create_new_document", {"name": "dimensioning_live_test"}),
                      "create_new_document")
    return f"new document '{resp.get('document', '?')}' created"


def check_box_fixture():
    _assert_ok(call("create_sketch", {"plane": "XY"}), "create_sketch")
    _assert_ok(call("draw_rectangle", {"x1": 0, "y1": 0, "x2": 5, "y2": 3}),
               "draw_rectangle")
    resp = _assert_ok(call("extrude", {"distance": 2}), "extrude")
    n = resp.get("bodies")
    if n != 1:
        raise AssertionError(f"expected 1 body after extrude, got {n}")
    return "box fixture created (5x3x2 cm, 1 body)"


def check_volume():
    resp = _assert_ok(dimension_call("get_physical_properties", {"body": "0"}),
                      "get_physical_properties")
    v = resp.get("volume_cm3")
    if v is None:
        raise AssertionError(f"no volume_cm3 in response: {json.dumps(resp)[:300]}")
    if not approx(v, 30.0):
        raise AssertionError(f"volume_cm3={v}, expected ~30.0")
    return f"volume_cm3={v} (expected 30.0)"


def check_area():
    resp = _assert_ok(dimension_call("get_physical_properties", {"body": "0"}),
                      "get_physical_properties")
    a = resp.get("area_cm2")
    if a is None:
        raise AssertionError(f"no area_cm2 in response: {json.dumps(resp)[:300]}")
    if not approx(a, 62.0):
        raise AssertionError(f"area_cm2={a}, expected ~62.0")
    return f"area_cm2={a} (expected 62.0)"


def check_center_of_mass():
    resp = _assert_ok(dimension_call("get_physical_properties", {"body": "0"}),
                      "get_physical_properties")
    com = resp.get("center_of_mass")
    if not isinstance(com, list) or len(com) != 3:
        raise AssertionError(f"bad center_of_mass: {json.dumps(resp)[:300]}")
    expected = [2.5, 1.5, 1.0]
    if any(not approx(c, e) for c, e in zip(com, expected)):
        raise AssertionError(f"center_of_mass={com}, expected ~{expected}")
    return f"center_of_mass={com} (expected [2.5, 1.5, 1.0])"


def check_measure_angle():
    # Discover an adjacent (perpendicular-normal) face pair programmatically.
    faces = _assert_ok(call("get_face_info", {"body": "0"}), "get_face_info")
    fl = faces.get("faces", [])
    pair = None
    for i in range(len(fl)):
        for j in range(i + 1, len(fl)):
            n1, n2 = fl[i].get("normal") or [], fl[j].get("normal") or []
            if len(n1) == 3 and len(n2) == 3:
                if abs(sum(a * b for a, b in zip(n1, n2))) < 1e-6:
                    pair = (i, j)
                    break
        if pair:
            break
    if pair is None:
        raise AssertionError(
            f"no perpendicular face pair found in {[f.get('normal') for f in fl]}")
    resp = _assert_ok(dimension_call(
        "measure_angle",
        {"entity1": f"body:0:face:{pair[0]}", "entity2": f"body:0:face:{pair[1]}"}),
        "measure_angle")
    deg = resp.get("angle_deg")
    if deg is None:
        raise AssertionError(f"no angle_deg in response: {json.dumps(resp)[:300]}")
    if not approx(deg, 90.0, tol=0.5):
        raise AssertionError(f"angle_deg={deg}, expected ~90.0")
    return f"faces {pair[0]}/{pair[1]} -> {deg} deg (expected ~90.0)"


def check_oriented_bounding_box():
    resp = _assert_ok(dimension_call(
        "get_oriented_bounding_box",
        {"body": "0", "length_axis": "X", "width_axis": "Y"}),
        "get_oriented_bounding_box")
    dims = [resp.get("length_cm"), resp.get("width_cm"), resp.get("height_cm")]
    expected = [5.0, 3.0, 2.0]
    if any(v is None for v in dims):
        raise AssertionError(f"missing dims in response: {json.dumps(resp)[:300]}")
    if any(not approx(v, e) for v, e in zip(dims, expected)):
        raise AssertionError(
            f"length/width/height={dims}, expected ~{expected}")
    return (f"length/width/height={dims} (expected [5, 3, 2], "
            f"length=X width=Y convention)")


def check_cylinder_fixture():
    _assert_ok(call("create_sketch", {"plane": "XY"}), "create_sketch (cyl)")
    _assert_ok(call("draw_circle", {"cx": 0, "cy": 0, "radius": 3}),
               "draw_circle")
    resp = _assert_ok(call("extrude", {"distance": 6}), "extrude (cyl)")
    n = resp.get("bodies")
    if n != 2:
        raise AssertionError(f"expected 2 bodies total, got {n}")
    return "cylinder fixture created (r=3 cm, h=6 cm, 2 bodies total)"


def check_cylinder_face():
    resp = _assert_ok(dimension_call(
        "inspect_body", {"body": "1", "detail": "full", "max_items": 200}),
        "inspect_body")
    faces = resp.get("faces", [])
    cyl = [f for f in faces if "Cylinder" in f.get("surface_type", "")]
    if not cyl:
        raise AssertionError(
            f"no cylindrical face found; face types: "
            f"{[f.get('surface_type') for f in faces]}")
    r = cyl[0].get("geometry_params", {}).get("radius_cm")
    if r is None:
        raise AssertionError(f"no radius_cm on cylinder face: {json.dumps(cyl[0])}")
    if not approx(r, 3.0):
        raise AssertionError(f"cylinder face radius_cm={r}, expected ~3.0")
    return f"face {cyl[0]['index']} surface=Cylinder radius_cm={r} (expected 3.0)"


def check_circle_edge():
    resp = _assert_ok(dimension_call(
        "inspect_body", {"body": "1", "detail": "full", "max_items": 200}),
        "inspect_body")
    edges = resp.get("edges", [])
    circles = [e for e in edges if "Circle" in e.get("curve_type", "")]
    if not circles:
        raise AssertionError(
            f"no circle edge found; edge types: "
            f"{[e.get('curve_type') for e in edges]}")
    r = circles[0].get("radius_cm")
    if r is None:
        raise AssertionError(f"no radius_cm on circle edge: {json.dumps(circles[0])}")
    if not approx(r, 3.0):
        raise AssertionError(f"circle edge radius_cm={r}, expected ~3.0")
    return (f"edge {circles[0]['index']} class={circles[0]['curve_type']} "
            f"radius_cm={r} (expected 3.0)")


def check_failure_path():
    resp = dimension_call("get_physical_properties", {"body": "999"})
    err = str(resp.get("error", ""))
    if "not found" not in err.lower():
        raise AssertionError(
            f"expected 'not found' error, got: {err[:200] or json.dumps(resp)[:200]}")
    return "get_physical_properties(body='999') -> \"Body '999' not found.\""


def main():
    print(f"Fusion dimensioning live test  (mode={MODE})")
    print(f"Bridge: {BASE_URL}")
    if MODE == "fresh":
        print(f"Fresh-module source: {FUSION_MCP_PATH}")
        print("NOTE: running add-in serves the OLD command set (probed: "
              "'Unknown command get_physical_properties');")
        print("      new handlers are invoked in-process via execute_script + "
              "fresh-module import.")
    print()
    pre_ids = _open_doc_ids()
    run_check("create fresh document", check_fresh_document)
    run_check("create box fixture (5x3x2)", check_box_fixture)
    run_check("physical properties: volume", check_volume)
    run_check("physical properties: area", check_area)
    run_check("physical properties: center of mass", check_center_of_mass)
    run_check("measure_angle between adjacent faces", check_measure_angle)
    run_check("oriented bounding box dimensions", check_oriented_bounding_box)
    run_check("create cylinder fixture (r=3 h=6)", check_cylinder_fixture)
    run_check("inspect_body: cylindrical face radius", check_cylinder_face)
    run_check("inspect_body: circle edge", check_circle_edge)
    run_check("failure path: bad body reference", check_failure_path)

    _close_docs_except(pre_ids)
    total = _passed + _failed
    print()
    print(f"{_passed}/{total} checks PASSED")
    sys.exit(0 if _failed == 0 else 1)


if __name__ == "__main__":
    MODE = "fresh"
    if "--mode" in sys.argv:
        MODE = sys.argv[sys.argv.index("--mode") + 1]
    if MODE not in ("http", "fresh"):
        print(f"Unknown --mode {MODE!r}; expected 'http' or 'fresh'")
        sys.exit(2)
    main()
