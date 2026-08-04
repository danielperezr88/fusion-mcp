#!/usr/bin/env python3
"""
Live integration tests for the OpenSCAD pipeline tools (todo 20 of
.omo/plans/fusion-mcp-enhancements.md):

  run_scad           -- render OpenSCAD/BOSL2 to a mesh body
  update_scad_body   -- re-run a stored .scad source with new params/code
  create_from_scad   -- translate a resolved CSG tree into native Fusion
                        parametric features (mesh fallback for unsupported nodes)

Requires Fusion 360 to be running with the FusionMCP add-in (bridge on
127.0.0.1:7432). The RUNNING add-in copy predates these tools (HTTP probe:
"Unknown command 'run_scad'"), so --mode fresh (default) drives the REPO's own
FusionMCP.py handlers inside Fusion via execute_script + the fresh-module
pattern (importlib spec_from_file_location -> mod.app = app ->
_process_command({"command": ..., "params": ...})). Fixture/verification
commands (create_new_document, clear_design, get_timeline_info, execute_script)
DO exist in the old add-in and go over HTTP in both modes.

The create_from_scad checks resolve the .scad source into a CSG tree on the
HOST (mcp_server.scad_translator.resolve_scad + the bundled OpenSCAD/BOSL2) --
exactly what mcp_server/fusion_server.py does -- and send that tree inside the
fresh-module params, because Fusion's embedded Python has adsk but no
openscad-evaluator. Params are embedded with repr() (BOM-safe, keeps
True/False/None valid, survives Windows backslashes); the execute_script
sandbox forbids closures/comprehensions, so all inner code is linear.

Checks (plan lines 219-221):
   1.  run_scad cube([10,10,10])                     -> mesh body (resolve by NAME)
   2.  run_scad BOSL2 cuboid([10,10,10])             -> BOSL2 include resolves
   3.  update_scad_body cube 10 -> cube 20           -> same name, bbox ~20mm
   4.  Kumiko individual_insert_generator.scad       -> Insert_Pattern=1 mesh
       (KUMIKO_SCAD_PATH env or default path; SKIP if missing)
   5.  create_from_scad difference()                 -> BRep + extrude/combine timeline
   6.  create_from_scad union()                      -> created
   7.  create_from_scad translate([20,0,0])          -> bbox min ~[2,0,0] cm
   8.  create_from_scad linear_extrude()             -> extruded cylinder BRep
   9.  create_from_scad BOSL2 cuboid([20,20,20])     -> BRep box
   10. create_from_scad BOSL2 torus()                -> mesh_fallback
       (Fusion 2026 revolve kernel rejects sketch-line axes on XZ/YZ:
        ASM_PATH_TANGENT -- verified live in todo 14)
   11. create_from_scad xcopies(n=3)                 -> 3 BRep bodies
   12. create_from_scad diff()+edge_profile()        -> BRep box (csg preferred)
   13. create_from_scad hull() fallback_to_mesh=True -> mesh fallback, no partials
   14. create_from_scad prismoid()                   -> Loft (or honest fallback)
   15. create_from_scad spheroid(style="icosa")      -> polyhedron mesh body
   17. create_from_scad scale([2,1,1])               -> ScaleFeature, bbox ~[2,1,1] cm
   18. create_from_scad mirror([1,0,0])              -> MirrorFeature, min.x ~ -1.0 cm
   19. create_from_scad resize([20,20,20])           -> ScaleFeature, bbox ~[2,2,2] cm
   20. create_from_scad rotate([45,0,0])             -> free-move rotation, y/z ~1.414 cm

The Kumiko source is PROPRIETARY (Paper View, confidential header): the test
references it BY PATH ONLY (env var or default) and never copies any of its
content into this repo.

Exit code: 0 if every non-skipped check PASSED, 1 if any FAILED, 2 on usage
error. A dead bridge prints a clear connection-error message and exits 1.

Usage:
    py tests/test_openscad_live.py             # fresh mode (default)
    py tests/test_openscad_live.py --mode http # run bridge commands directly
"""

import os
import sys

import requests
import time

BASE_URL = "http://127.0.0.1:7432/command"
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FUSIONMCP_PATH = os.path.join(REPO_ROOT, "FusionMCP.py")

#: Default Kumiko generator (todo 20 references). Overridable via KUMIKO_SCAD_PATH.
DEFAULT_KUMIKO_PATH = os.path.join(
    r"C:\Users\danie\Downloads\KumikoPatreon\Source Code\Source Code",
    "individual_insert_generator.scad")

TOL_CM = 0.06          # absolute tolerance for bbox checks (Fusion units: cm)
LONG_TIMEOUT = 420     # OpenSCAD renders (esp. Kumiko) can take a while

if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from mcp_server import bundle            # noqa: E402  (host-side CSG resolution)
from mcp_server import scad_translator   # noqa: E402

MODE = "fresh"
_results = []  # (name, ok, detail); ok None == skipped


class BridgeError(Exception):
    """Raised when the Fusion HTTP bridge cannot be reached."""


class SkipCheck(Exception):
    """Raised by a check that should be skipped (Kumiko path missing)."""


def call(command, params=None, timeout=180):
    """POST a command to the Fusion bridge and return the parsed JSON dict."""
    params = params or {}
    try:
        r = requests.post(BASE_URL, json={"command": command, "params": params},
                          timeout=timeout)
    except requests.exceptions.RequestException as e:
        raise BridgeError(
            f"Cannot reach Fusion 360 on http://127.0.0.1:7432 - is Fusion "
            f"open and the FusionMCP add-in running? ({e})")
    try:
        return r.json()
    except ValueError as e:
        raise BridgeError(f"Bridge returned invalid JSON: {e}")


def run_code(code, timeout=180):
    """Run Python inside Fusion via execute_script; returns parsed output.

    Raises RuntimeError when execute_script itself errors inside Fusion.
    """
    resp = call("execute_script", {"code": code}, timeout=timeout)
    if isinstance(resp, dict) and "error" in resp:
        raise RuntimeError(
            "execute_script failed inside Fusion:\n" + str(resp["error"])[:2000])
    return resp.get("output")


def fresh_cmd(command, params, timeout=LONG_TIMEOUT):
    """Drive a repo FusionMCP.py handler in a fresh module inside Fusion.

    Loads the LOCAL FusionMCP.py as 'fusionmcp_dev', sets mod.app = app
    (module-level 'app' is None outside run()), then dispatches through
    _process_command so the full handler path is exercised. Params are embedded
    with repr() -- a valid Python literal (True/False/None survive, Windows
    backslashes are escaped). The handler dict is returned via result['output'].
    """
    inner = (
        "import importlib.util as _ilu\n"
        "import sys as _sys\n"
        "_spec = _ilu.spec_from_file_location('fusionmcp_dev', %s)\n"
        "_mod = _ilu.module_from_spec(_spec)\n"
        "_sys.modules['fusionmcp_dev'] = _mod\n"
        "_spec.loader.exec_module(_mod)\n"
        "_mod.app = app\n"
        "_out = _mod._process_command({'command': %s, 'params': %s})\n"
        "result['output'] = _out\n"
    ) % (repr(FUSIONMCP_PATH), repr(command), repr(params))
    return run_code(inner, timeout=timeout)


def check(name, ok, detail=""):
    status = "PASS" if ok else "FAIL"
    print(f"[{status}] {name}" + (f" -- {detail}" if detail else ""))
    _results.append((name, ok, detail))
    return ok


def run_check(name, fn):
    try:
        detail = fn()
        check(name, True, detail)
    except SkipCheck as e:
        print(f"[SKIP] {name} -- {e}")
        _results.append((name, None, str(e)))
    except BridgeError as e:
        check(name, False, f"connection error: {e}")
    except Exception as e:
        check(name, False, f"exception: {e}")


# ---- state / verification helpers (execute_script, all linear, no closures) ----
#
# Cross-boundary reads can see a not-yet-committed mutation (Wave 2
# read-after-write precedent): a body may be counted (+1) but not yet findable
# by name. Re-poll before concluding; real failures still raise after attempts.

def _poll(fn, what, attempts=6, delay=0.5):
    last = None
    for i in range(attempts):
        try:
            return fn()
        except (AssertionError, RuntimeError) as e:
            last = e
            if i < attempts - 1:
                time.sleep(delay)
    raise AssertionError(f"{what}: still failing after {attempts} attempts: {last}")


def _await_count(reader, expected, what, attempts=6, delay=0.5):
    last = None
    for i in range(attempts):
        last = reader()
        if last == expected:
            return last
        if i < attempts - 1:
            time.sleep(delay)
    raise AssertionError(f"{what}: {last} != expected {expected}")


def _await_min(reader, minimum, what, attempts=6, delay=0.5):
    last = None
    for i in range(attempts):
        last = reader()
        if last >= minimum:
            return last
        if i < attempts - 1:
            time.sleep(delay)
    raise AssertionError(f"{what}: {last} < minimum {minimum}")


def _await_body_change(before_brep, before_mesh, what, attempts=6, delay=0.5):
    b, m = before_brep, before_mesh
    for i in range(attempts):
        b, m = brep_count(), mesh_count()
        if (b - before_brep) + (m - before_mesh) >= 1:
            return b, m
        if i < attempts - 1:
            time.sleep(delay)
    raise AssertionError(
        f"{what}: no body appeared after mutation "
        f"(BRep {before_brep}->{b}, mesh {before_mesh}->{m})")


def bodies_state():
    return _poll(lambda: run_code(
        "result['output'] = [root.bRepBodies.count, root.meshBodies.count]"),
        "bodies_state")


def brep_count():
    return bodies_state()[0]


def mesh_count():
    return bodies_state()[1]


def _bbox_size(bb):
    return [round(bb["max"][i] - bb["min"][i], 3) for i in range(3)]


def mesh_bbox_by_name(name):
    def scan():
        code = (
            "b = None\n"
            "for i in range(root.meshBodies.count):\n"
            "    m = root.meshBodies.item(i)\n"
            "    if m.name == %r:\n"
            "        b = m\n"
            "if b is None:\n"
            "    result['output'] = {'error': 'mesh body not found: ' + %r}\n"
            "else:\n"
            "    bb = b.boundingBox\n"
            "    mn = bb.minPoint\n"
            "    mx = bb.maxPoint\n"
            "    result['output'] = {'min': [mn.x, mn.y, mn.z], "
            "'max': [mx.x, mx.y, mx.z]}\n"
        ) % (name, name)
        out = run_code(code)
        if isinstance(out, dict) and "error" in out:
            raise AssertionError(out["error"])
        return out
    return _poll(scan, f"mesh_bbox_by_name({name!r})")


def brep_bbox(idx):
    def scan():
        code = (
            "b = root.bRepBodies.item(%d)\n"
            "bb = b.boundingBox\n"
            "mn = bb.minPoint\n"
            "mx = bb.maxPoint\n"
            "result['output'] = {'min': [mn.x, mn.y, mn.z], "
            "'max': [mx.x, mx.y, mx.z]}\n"
        ) % idx
        out = run_code(code)
        if isinstance(out, dict) and "error" in out:
            raise AssertionError(out["error"])
        return out
    return _poll(scan, f"brep_bbox({idx})")


def timeline_types():
    def fetch():
        resp = call("get_timeline_info", timeout=60)
        items = resp.get("items") if isinstance(resp, dict) else None
        if items is None:
            raise AssertionError(f"unexpected get_timeline_info shape: {resp}")
        return [it.get("type") for it in items]
    return _poll(fetch, "timeline_types")


# ---- new-tool dispatch ----

def resolve_scad_code(code):
    return scad_translator.resolve_scad(
        code,
        openscad_path=bundle.get_openscad_path(),
        bosl2_path=bundle.get_bosl2_path())


def run_scad_cmd(code, params="", timeout=LONG_TIMEOUT):
    return fresh_cmd("run_scad",
                     {"code": code, "params": params, "quality": 100,
                      "units": "mm"}, timeout=timeout)


def create_from_scad(code, fallback_to_mesh=True):
    """create_from_scad honoring the mode: fresh resolves host-side and drives
    the real _create_from_scad handler; http hits the bridge directly (honestly
    fails against the old add-in)."""
    if MODE == "http":
        return call("create_from_scad",
                    {"code": code, "units": "mm",
                     "fallback_to_mesh": fallback_to_mesh},
                    timeout=LONG_TIMEOUT)
    csg = resolve_scad_code(code)
    return fresh_cmd("create_from_scad",
                     {"code": code, "csg_tree": csg, "units": "mm",
                      "fallback_to_mesh": fallback_to_mesh})


def _assert_created(resp, what):
    if isinstance(resp, dict) and resp.get("error"):
        raise AssertionError(f"{what} failed: {str(resp['error'])[:300]}")
    if resp.get("created") is not True:
        raise AssertionError(f"{what}: created != True: {resp}")
    return resp


# ---- checks ----

def check_fresh_document():
    resp = call("create_new_document", {"name": "openscad_live_test"})
    if isinstance(resp, dict) and resp.get("error"):
        raise AssertionError(str(resp["error"]))
    return f"new document '{resp.get('document')}' created"


def check_run_scad_cube():
    call("clear_design")
    before = mesh_count()
    resp = run_scad_cmd("cube([10,10,10]);")
    if isinstance(resp, dict) and resp.get("error"):
        raise AssertionError(f"run_scad failed: {str(resp['error'])[:300]}")
    if resp.get("rendered") is not True:
        raise AssertionError(f"rendered != True: {resp}")
    name = resp.get("mesh_body")
    if not name:
        raise AssertionError(f"no mesh_body in response: {resp}")
    _await_count(mesh_count, before + 1, "mesh count")
    bb = mesh_bbox_by_name(name)          # resolve by NAME (not creation-ordered)
    size = _bbox_size(bb)
    if any(abs(s - 1.0) > TOL_CM for s in size):
        raise AssertionError(f"bbox size={size}, expected ~[1,1,1] cm (10mm cube)")
    return f"rendered mesh_body={name}, bbox={size} cm"


def check_run_scad_bosl2():
    call("clear_design")
    resp = run_scad_cmd("include <BOSL2/std.scad>\ncuboid([10,10,10]);")
    if isinstance(resp, dict) and resp.get("error"):
        raise AssertionError(f"run_scad BOSL2 failed: {str(resp['error'])[:300]}")
    if resp.get("rendered") is not True:
        raise AssertionError(f"BOSL2 include did not render: {resp}")
    name = resp.get("mesh_body")
    if not name:
        raise AssertionError(f"no mesh_body in response: {resp}")
    mesh_bbox_by_name(name)
    return f"BOSL2 include resolved, mesh_body={name}"


def check_update_scad_body():
    call("clear_design")
    first = run_scad_cmd("cube([10,10,10]);")
    name = first.get("mesh_body")
    if not name:
        raise AssertionError(f"run_scad setup failed: {first}")
    resp = fresh_cmd("update_scad_body", {"body": name, "code": "cube([20,20,20]);"})
    if isinstance(resp, dict) and resp.get("error"):
        raise AssertionError(str(resp["error"])[:300])
    if resp.get("updated") is not True:
        raise AssertionError(f"updated != True: {resp}")
    if resp.get("mesh_body_name") != name:
        raise AssertionError(
            f"mesh_body_name={resp.get('mesh_body_name')}, expected {name!r}")
    bb = mesh_bbox_by_name(name)
    size = _bbox_size(bb)
    if any(abs(s - 2.0) > TOL_CM for s in size):
        raise AssertionError(f"bbox size={size}, expected ~[2,2,2] cm (20mm cube)")
    return f"updated in place mesh_body_name={name!r}, bbox={size} cm"


def check_kumiko():
    path = os.environ.get("KUMIKO_SCAD_PATH") or DEFAULT_KUMIKO_PATH
    if not os.path.isfile(path):
        raise SkipCheck(f"Kumiko SCAD not found at {path}")
    with open(path, "r", encoding="utf-8-sig") as f:
        code = f.read()
    if "Insert_Pattern" not in code:
        raise AssertionError(
            "Kumiko file does not define Insert_Pattern; refusing to render")
    call("clear_design")
    resp = run_scad_cmd(code, params="Insert_Pattern=1")
    if isinstance(resp, dict) and resp.get("error"):
        raise AssertionError(f"Kumiko render failed: {str(resp['error'])[:300]}")
    if resp.get("rendered") is not True:
        raise AssertionError(f"Kumiko render did not render: {resp}")
    name = resp.get("mesh_body")
    if not name:
        raise AssertionError(f"no mesh_body in response: {resp}")
    mesh_bbox_by_name(name)
    return f"Insert_Pattern=1 rendered, mesh_body={name} (path={path})"


def check_create_difference():
    code = "difference(){cube([20,20,20]);cylinder(r=5,h=30);}"
    call("clear_design")
    resp = create_from_scad(code)
    _assert_created(resp, "difference")
    if resp.get("method") != "csg_translation":
        raise AssertionError(
            f"method={resp.get('method')}, expected csg_translation: {resp}")
    _await_min(brep_count, 1, "BRep count")
    types = timeline_types()
    if "ExtrudeFeature" not in types or "CombineFeature" not in types:
        raise AssertionError(
            f"timeline missing ExtrudeFeature/CombineFeature: {types}")
    feats = [t for t in types if "Extrude" in t or "Combine" in t]
    return (f"method=csg_translation bodies={resp.get('bodies')} "
            f"features={resp.get('features')} timeline={feats}")


def check_create_union():
    code = "union(){cube([10,10,10]);cylinder(r=5,h=10);}"
    call("clear_design")
    resp = create_from_scad(code)
    _assert_created(resp, "union")
    return f"method={resp.get('method')} bodies={resp.get('bodies')}"


def check_create_translate():
    code = "translate([20,0,0])cube([10,10,10]);"
    call("clear_design")
    resp = create_from_scad(code)
    _assert_created(resp, "translate")
    bb = brep_bbox(0)
    mn = [round(v, 3) for v in bb["min"]]
    expected = [2.0, 0.0, 0.0]
    if any(abs(mn[i] - expected[i]) > TOL_CM for i in range(3)):
        raise AssertionError(f"bbox min={mn}, expected ~{expected} cm")
    return f"bbox min={mn} (translate([20,0,0]) mm -> 2cm)"


def check_create_linear_extrude():
    code = "linear_extrude(height=10)circle(r=5);"
    call("clear_design")
    resp = create_from_scad(code)
    _assert_created(resp, "linear_extrude")
    if resp.get("method") != "csg_translation":
        raise AssertionError(
            f"method={resp.get('method')}, expected csg_translation: {resp}")
    _await_min(brep_count, 1, "BRep count")
    return f"method=csg_translation bodies={resp.get('bodies')}"


def check_create_bosl2_cuboid():
    code = "include <BOSL2/std.scad>\ncuboid([20,20,20]);"
    call("clear_design")
    resp = create_from_scad(code)
    _assert_created(resp, "BOSL2 cuboid")
    if resp.get("method") != "csg_translation":
        raise AssertionError(
            f"method={resp.get('method')}, expected csg_translation: {resp}")
    _await_min(brep_count, 1, "BRep count")
    bb = brep_bbox(0)
    size = _bbox_size(bb)
    if any(abs(s - 2.0) > TOL_CM for s in size):
        raise AssertionError(f"bbox size={size}, expected ~[2,2,2] cm (20mm cuboid)")
    return f"BOSL2 cuboid -> BRep, bbox={size} cm"


def check_create_torus_mesh_fallback():
    code = "include <BOSL2/std.scad>\ntorus(d_maj=40,d_min=10);"
    call("clear_design")
    before_mesh = mesh_count()
    resp = create_from_scad(code)
    _assert_created(resp, "torus")
    if resp.get("method") != "mesh_fallback":
        raise AssertionError(
            f"method={resp.get('method')}, expected mesh_fallback "
            f"(Fusion 2026 revolve kernel ASM_PATH_TANGENT on XZ/YZ "
            f"sketch-line axes): {resp}")
    uns = resp.get("unsupported_nodes") or []
    if "rotate_extrude" not in uns:
        raise AssertionError(f"unsupported_nodes={uns}, expected ['rotate_extrude']")
    if not resp.get("fallback_reason"):
        raise AssertionError("missing fallback_reason")
    _await_count(mesh_count, before_mesh + 1, "mesh count")
    return (f"method=mesh_fallback unsupported={uns} "
            f"reason={str(resp.get('fallback_reason'))[:60]}")


def check_create_xcopies():
    code = "include <BOSL2/std.scad>\nxcopies(n=3,spacing=12)cuboid([5,5,5]);"
    call("clear_design")
    resp = create_from_scad(code)
    _assert_created(resp, "xcopies")
    n = _await_count(brep_count, 3, "BRep count")
    return f"3 BRep bodies created (bodies={resp.get('bodies')})"


def check_create_diff_edge_profile():
    code = ("include <BOSL2/std.scad>\n"
            "diff() cuboid(30) { edge_profile(TOP) mask2d_roundover(r=5); }")
    call("clear_design")
    resp = create_from_scad(code)
    _assert_created(resp, "diff+edge_profile")
    method = resp.get("method")
    if method == "csg_translation":
        _await_min(brep_count, 1, "BRep count")
        return (f"method=csg_translation bodies={resp.get('bodies')} "
                f"features={resp.get('features')}")
    if method == "mesh_fallback":
        _await_min(mesh_count, 1, "mesh count")
        return (f"method=mesh_fallback (plan prefers csg_translation; see "
                f"summary) reason={str(resp.get('fallback_reason'))[:60]}")
    raise AssertionError(f"unexpected method={method}: {resp}")


def check_hull_fallback():
    code = "hull() { cube(5); sphere(3); }"
    call("clear_design")
    before_brep = brep_count()
    before_mesh = mesh_count()
    resp = create_from_scad(code)   # fallback_to_mesh defaults True
    _assert_created(resp, "hull")
    if resp.get("method") != "mesh_fallback":
        raise AssertionError(
            f"method={resp.get('method')}, expected mesh_fallback: {resp}")
    reason = str(resp.get("fallback_reason") or "")
    if "hull" not in reason.lower():
        raise AssertionError(f"fallback_reason does not mention hull: {reason}")
    _await_count(brep_count, before_brep, "BRep count")   # no partial BRep left
    _await_count(mesh_count, before_mesh + 1, "mesh count")
    return f"mesh fallback, no partial BRep left (reason: {reason[:60]})"


def check_prismoid():
    code = "include <BOSL2/std.scad>\nprismoid(size1=[40,40],size2=[20,20],h=20);"
    call("clear_design")
    resp = create_from_scad(code)
    _assert_created(resp, "prismoid")
    method = resp.get("method")
    if method == "csg_translation":
        types = timeline_types()
        if "LoftFeature" not in types:
            raise AssertionError(f"expected LoftFeature in timeline, got {types}")
        return (f"method=csg_translation, LoftFeature present, "
                f"features={resp.get('features')}")
    if method == "mesh_fallback":
        _await_min(mesh_count, 1, "mesh count")
        return (f"method=mesh_fallback (plan expects Loft; see summary) "
                f"reason={str(resp.get('fallback_reason'))[:60]}")
    raise AssertionError(f"unexpected method={method}: {resp}")


def check_spheroid():
    code = 'include <BOSL2/std.scad>\nspheroid(d=20, style="icosa");'
    call("clear_design")
    before_brep = brep_count()
    before_mesh = mesh_count()
    resp = create_from_scad(code)
    _assert_created(resp, "spheroid")
    method = resp.get("method")
    after_brep, after_mesh = _await_body_change(
        before_brep, before_mesh, "body creation")
    delta = (after_brep - before_brep) + (after_mesh - before_mesh)
    if delta < 1:
        raise AssertionError("no body created (BRep/mesh delta 0)")
    if method == "mesh_fallback":
        return (f"method=mesh_fallback unsupported={resp.get('unsupported_nodes')} "
                f"reason={str(resp.get('fallback_reason'))[:60]}")
    if method == "csg_translation":
        return (f"method=csg_translation bodies={resp.get('bodies')} "
                f"mesh_delta={after_mesh - before_mesh} "
                f"(polyhedron -> in-timeline mesh body)")
    raise AssertionError(f"unexpected method={method}: {resp}")


def check_create_scale():
    code = "scale([2,1,1])cube([10,10,10]);"
    call("clear_design")
    resp = create_from_scad(code)
    _assert_created(resp, "scale")
    if resp.get("method") != "csg_translation":
        raise AssertionError(
            f"method={resp.get('method')}, expected csg_translation: {resp}")
    _await_min(brep_count, 1, "BRep count")
    bb = brep_bbox(0)
    size = _bbox_size(bb)
    if any(abs(size[i] - [2.0, 1.0, 1.0][i]) > TOL_CM for i in range(3)):
        raise AssertionError(
            f"bbox size={size}, expected ~[2,1,1] cm (scale([2,1,1]) of 10mm cube)")
    return f"scale([2,1,1]) -> bbox={size} cm (ScaleFeature, origin at world origin)"


def check_create_mirror():
    code = "mirror([1,0,0])cube([10,10,10]);"
    call("clear_design")
    resp = create_from_scad(code)
    _assert_created(resp, "mirror")
    if resp.get("method") != "csg_translation":
        raise AssertionError(
            f"method={resp.get('method')}, expected csg_translation: {resp}")
    _await_min(brep_count, 1, "BRep count")
    bb = brep_bbox(0)
    mn = bb["min"]
    if abs(mn[0] - (-1.0)) > TOL_CM:
        raise AssertionError(
            f"bbox min={mn}, expected min.x ~ -1.0 (mirror about x=0 plane)")
    return (f"mirror([1,0,0]) -> bbox min.x={mn[0]} cm "
            f"(MirrorFeature through origin plane)")


def check_create_resize():
    code = "resize([20,20,20])cube([10,10,10]);"
    call("clear_design")
    resp = create_from_scad(code)
    _assert_created(resp, "resize")
    if resp.get("method") != "csg_translation":
        raise AssertionError(
            f"method={resp.get('method')}, expected csg_translation: {resp}")
    _await_min(brep_count, 1, "BRep count")
    bb = brep_bbox(0)
    size = _bbox_size(bb)
    if any(abs(s - 2.0) > TOL_CM for s in size):
        raise AssertionError(
            f"bbox size={size}, expected ~[2,2,2] cm (resize to 20mm cube)")
    return f"resize([20,20,20]) -> bbox={size} cm (ScaleFeature with per-axis factors)"


def check_create_rotate():
    code = "rotate([45,0,0])cube([10,10,10]);"
    call("clear_design")
    resp = create_from_scad(code)
    _assert_created(resp, "rotate")
    if resp.get("method") != "csg_translation":
        raise AssertionError(
            f"method={resp.get('method')}, expected csg_translation: {resp}")
    _await_min(brep_count, 1, "BRep count")
    bb = brep_bbox(0)
    size = _bbox_size(bb)
    # 10mm cube rotated 45 deg about X: y/z spans grow to sqrt(2) * 1.0 cm.
    if any(abs(size[i] - 1.4142) > TOL_CM for i in (1, 2)):
        raise AssertionError(
            f"bbox size={size}, expected ~[1.0,1.414,1.414] cm "
            f"(rotate([45,0,0]) of 10mm cube)")
    return f"rotate([45,0,0]) -> bbox={size} cm (rotation matrix via free move)"


def main():
    global MODE
    if "--mode" in sys.argv:
        i = sys.argv.index("--mode")
        if i + 1 >= len(sys.argv):
            print("Usage: py tests/test_openscad_live.py [--mode fresh|http]")
            return 2
        MODE = sys.argv[i + 1].lower()
    if MODE not in ("fresh", "http"):
        print(f"Unknown --mode {MODE!r}; expected 'fresh' or 'http'")
        return 2

    print(f"Fusion OpenSCAD pipeline live test  (mode={MODE}, bridge={BASE_URL})")

    # Connection probe first -- plan line 221 requires a clear error message
    # when Fusion is not reachable instead of a traceback.
    try:
        r = requests.post(BASE_URL, json={"command": "get_info", "params": {}},
                          timeout=10)
        r.raise_for_status()
    except requests.exceptions.RequestException as e:
        print(f"Cannot reach Fusion 360 on http://127.0.0.1:7432: {e}")
        print("Make sure Fusion 360 is open and the FusionMCP add-in is running.")
        return 1

    if MODE == "fresh":
        print(f"Fresh-module source: {FUSIONMCP_PATH}")
        print("NOTE: the RUNNING add-in serves the OLD command set; run_scad /")
        print("      update_scad_body / create_from_scad are driven in-process via")
        print("      execute_script + fresh-module import of the repo FusionMCP.py.")
    print()

    run_check("create fresh document", check_fresh_document)
    run_check("1. run_scad cube([10,10,10]) mesh", check_run_scad_cube)
    run_check("2. run_scad BOSL2 cuboid include", check_run_scad_bosl2)
    run_check("3. update_scad_body re-render cube 20mm", check_update_scad_body)
    run_check("4. Kumiko Insert_Pattern=1 render", check_kumiko)
    run_check("5. create_from_scad difference (BRep+timeline)",
              check_create_difference)
    run_check("6. create_from_scad union", check_create_union)
    run_check("7. create_from_scad translate bbox", check_create_translate)
    run_check("8. create_from_scad linear_extrude", check_create_linear_extrude)
    run_check("9. create_from_scad BOSL2 cuboid BRep", check_create_bosl2_cuboid)
    run_check("10. create_from_scad torus mesh fallback",
              check_create_torus_mesh_fallback)
    run_check("11. create_from_scad xcopies 3 bodies", check_create_xcopies)
    run_check("12. create_from_scad diff+edge_profile",
              check_create_diff_edge_profile)
    run_check("13. create_from_scad hull mesh fallback", check_hull_fallback)
    run_check("14. create_from_scad prismoid", check_prismoid)
    run_check("15. create_from_scad spheroid icosa", check_spheroid)
    run_check("17. create_from_scad scale [2,1,1]", check_create_scale)
    run_check("18. create_from_scad mirror [1,0,0]", check_create_mirror)
    run_check("19. create_from_scad resize 20mm", check_create_resize)
    run_check("20. create_from_scad rotate 45deg X", check_create_rotate)

    executed = [r for r in _results if r[1] is not None]
    passed = sum(1 for _, ok, _ in executed if ok)
    total = len(executed)
    print()
    print(f"{passed}/{total} checks PASSED")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
