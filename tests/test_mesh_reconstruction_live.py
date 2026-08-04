#!/usr/bin/env python3
"""
Live integration test suite for the full mesh-to-parametric workflow --
.omo/plans/mesh-to-parametric.md, Todo 12. Exercises ALL 10 plan checks
(lines 160-165) through the REAL server tools (mcp_server/fusion_server.py)
with the REAL Fusion 360 state behind them:

   1.  import_mesh_data cube fixture -> analyze_mesh -> watertight true,
       recommended_strategy prismatic
   2.  slice_mesh mid-height (Z=0.5 of a [0,1]^3 cm cube) -> 1 loop, 4 pts
   3.  reconstruct_mesh(strategy="prismatic") -> csg_translation,
       ExtrudeFeature in the timeline, BRep bbox ~ [1,1,1] cm (units=mm)
   4.  cylinder mesh -> strategy="revolved" -> BRep + RevolveFeature in the
       timeline (Spanish UI: assert on feature TYPE, never localized names)
   5.  select_parameter_schema with bbox facts -> named parameters
   6.  annotate_mesh_parameters -> 4 views, every image_base64 decodes to
       PNG magic (text envelope + 4 Image blocks)
   7.  compare_mesh_to_brep original vs reconstructed -> volume_ratio within
       5%
   8.  get_workflow_guide() -> >= 8 ordered steps
   9.  organic strategy -> converted OR graceful not-available (SKIP when the
       PREVIEW MeshConvertFeature API is absent/license-gated -- the 2026
       build here is license-gated, so the exact not-available error is
       treated as SKIPPED-not-failed)
   10. failure paths: analyze_mesh(mesh="999") -> clear error,
       slice_mesh(axis="W") -> EXACT {"error": "Axis must be X, Y, or Z"},
       unknown strategy -> EXACT unknown-strategy error

The RUNNING add-in copy in Fusion lags the repo (no import_mesh_data /
extract_mesh_data / the new handlers), so --mode fresh (default for the real
suite) loads mcp_server/fusion_server.py headless (stubbed mcp.server.fastmcp)
and routes every repo-lagging handler command through the fresh-module
pattern (importlib spec_from_file_location -> mod.app = app ->
_process_command), mirroring tests/test_openscad_live.py:128-148 and
tests/test_mesh_to_brep_live.py. Bridge-only commands (create_new_document,
clear_design, get_timeline_info, get_bodies_info, execute_script) go over
HTTP in both modes. --mode http leaves the real _call in place: it is the
honest bridge probe (clear connection error + exit 1 when Fusion is down;
against the lagging add-in the new-handler checks fail honestly).

pytest behaviour: the module-scoped `bridge` fixture probes the bridge first
and skips cleanly when Fusion is unreachable, so the headless
`py -m pytest tests -v` run stays green; with the bridge up it runs the SAME
10 checks and asserts none FAILED.

Exit code (standalone): 0 if every non-skipped check PASSED, 1 otherwise
(including a dead bridge in --mode http).

Usage:
    py tests/test_mesh_reconstruction_live.py --mode fresh  # full live suite
    py tests/test_mesh_reconstruction_live.py               # http bridge probe
"""

import base64
import importlib.util
import json
import math
import os
import sys
import time
import types

import requests

BASE_URL = "http://127.0.0.1:7432/command"
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FUSIONMCP_PATH = os.path.join(REPO_ROOT, "FusionMCP.py")
SERVER_PATH = os.path.join(REPO_ROOT, "mcp_server", "fusion_server.py")
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

TOL_CM = 0.06          # absolute bbox tolerance (Fusion units: cm)
LONG_TIMEOUT = 330     # reconstruct/revolve/mesh_convert can be slow

MODE = "http"          # default = bridge probe; --mode fresh runs the suite
_results = []          # (name, ok, detail); ok None == skipped
_FS = None             # fusion_server module loaded headless (see _install_server)


class BridgeError(Exception):
    """Raised when the Fusion HTTP bridge cannot be reached."""


class SkipCheck(Exception):
    """Raised by a check that should be skipped (license-gated organic)."""


def call(command, params=None, timeout=240):
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


def run_code(code, timeout=240):
    """Run Python inside Fusion via execute_script; returns parsed output.

    Raises RuntimeError when execute_script itself errors inside Fusion.
    """
    resp = call("execute_script", {"code": code}, timeout=timeout)
    if isinstance(resp, dict) and "error" in resp:
        raise RuntimeError(
            "execute_script failed inside Fusion:\n" + str(resp["error"])[:2000])
    return resp.get("output")


def fresh_dispatch(command, params, timeout=240):
    """Drive a repo FusionMCP.py handler in a fresh module inside Fusion.

    Loads the LOCAL FusionMCP.py as 'fusionmcp_dev', sets mod.app = app, then
    dispatches through _process_command. Params are embedded with repr().
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


# ---- state / verification helpers (execute_script, linear, no closures) ----
# Cross-boundary reads can lag the mutation (bodies/features appear a tick
# later); retry-poll before concluding, real failures still raise.

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


# ---- mesh fixtures ----

def _unit_cube():
    """Outward-wound unit cube [0,1]^3 cm as flat node/index lists."""
    nodes = [
        0, 0, 0,  1, 0, 0,  1, 1, 0,  0, 1, 0,
        0, 0, 1,  1, 0, 1,  1, 1, 1,  0, 1, 1,
    ]
    indices = [
        0, 2, 1,  0, 3, 2,
        4, 5, 6,  4, 6, 7,
        0, 1, 5,  0, 5, 4,
        3, 7, 6,  3, 6, 2,
        0, 4, 3,  3, 4, 7,
        1, 2, 6,  1, 6, 5,
    ]
    return nodes, indices


def _cylinder(r=1.0, h=2.0, n=24):
    """Open (capped) cylinder mesh, r=1.0 h=2.0 cm, 24 segments (T6 recipe)."""
    nodes = []
    for i in range(n):
        a = 2.0 * math.pi * i / n + math.pi / n
        nodes += [r * math.cos(a), r * math.sin(a), 0.0]
    for i in range(n):
        a = 2.0 * math.pi * i / n + math.pi / n
        nodes += [r * math.cos(a), r * math.sin(a), h]
    nodes += [0.0, 0.0, 0.0, 0.0, 0.0, h]
    indices = []
    for i in range(n):
        j = (i + 1) % n
        indices += [i, j, n + j,  i, n + j, n + i]
    for i in range(n):
        j = (i + 1) % n
        indices += [2 * n, j, i]
        indices += [2 * n + 1, n + i, n + j]
    return nodes, indices


# ---- server-module harness (headless fusion_server.py + _call routing) ----

def _probe_bridge():
    try:
        r = requests.post(BASE_URL, json={"command": "get_info", "params": {}},
                          timeout=10)
        r.raise_for_status()
        return True
    except requests.exceptions.RequestException:
        return False


class _FakeResp:
    def __init__(self, data):
        self._data = data

    def json(self):
        return self._data


#: Handler commands the RUNNING add-in copy does not serve yet (repo-lagging).
_FRESH_ONLY = {
    "import_mesh_data", "extract_mesh_data", "create_from_csg_tree",
    "revolve_cross_section", "mesh_convert", "compare_mesh_brep",
    "capture_mesh_views", "capture_body_views",
}


def _stub_mcp(monkeypatch=None):
    """Stub mcp.server.fastmcp in sys.modules so fusion_server.py imports
    headless (the mcp SDK is absent for the `py` interpreter)."""
    class _Image:
        def __init__(self, data=None, format=None):
            self.data = data
            self.format = format

    class _FastMCP:
        def __init__(self, *a, **k):
            pass

        def tool(self, *a, **k):
            def deco(fn):
                return fn
            return deco

    stub_mcp = types.ModuleType("mcp")
    stub_server = types.ModuleType("mcp.server")
    stub_fastmcp = types.ModuleType("mcp.server.fastmcp")
    stub_fastmcp.FastMCP = _FastMCP
    stub_fastmcp.Image = _Image
    stub_server.fastmcp = stub_fastmcp
    for name, mod in (("mcp", stub_mcp), ("mcp.server", stub_server),
                      ("mcp.server.fastmcp", stub_fastmcp)):
        if monkeypatch is not None:
            monkeypatch.setitem(sys.modules, name, mod)
        else:
            sys.modules[name] = mod


def _install_server(monkeypatch=None):
    """Load mcp_server/fusion_server.py headless and return the module.

    fresh mode: fs._call is replaced by a router that sends every
    repo-lagging handler command through fresh_dispatch (real Fusion
    execution via the repo FusionMCP.py) and mimics _call's envelope
    (success -> json.dumps, error dict -> "Error: <msg>").  http mode:
    the real _call stays in place (direct bridge -> honest probe).
    """
    global _FS
    _stub_mcp(monkeypatch)
    spec = importlib.util.spec_from_file_location("fusionmcp_server_dev_t12",
                                                  SERVER_PATH)
    fs = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fs)
    if MODE == "fresh":
        real_call = fs._call

        def router(command, params=None, timeout=30):
            if command in _FRESH_ONLY:
                data = fresh_dispatch(command, params or {}, timeout=timeout)
                if isinstance(data, dict) and "error" in data:
                    return "Error: " + data["error"]
                return json.dumps(data, indent=2)
            return real_call(command, params, timeout)

        if monkeypatch is not None:
            monkeypatch.setattr(fs, "_call", router)
        else:
            fs._call = router
    _FS = fs
    return fs


def _parse_ok(out, what):
    """Parse a server-tool JSON string and require a non-error dict."""
    try:
        data = json.loads(out)
    except (TypeError, ValueError) as e:
        raise AssertionError(f"{what}: expected JSON, got {out!r} ({e})")
    if not isinstance(data, dict):
        raise AssertionError(f"{what}: expected dict, got {type(data).__name__}")
    if "error" in data:
        raise AssertionError(f"{what} failed: {data['error']}")
    return data


def _import_cube(name="t12_cube"):
    """Import the [0,1]^3 cm cube via the SERVER import_mesh_data tool.
    Returns the mesh ref ("0")."""
    nodes, indices = _unit_cube()
    imp = _FS.import_mesh_data(coordinates=nodes, triangle_indices=indices,
                               name=name)
    if isinstance(imp, str) and imp.startswith("Error: "):
        raise AssertionError(f"import_mesh_data: {imp}")
    data = _parse_ok(imp, "import_mesh_data")
    return "0"


# ---- checks ----

def check_fresh_document():
    resp = call("create_new_document", {"name": "T12_mesh_reconstruction_QA"})
    if isinstance(resp, dict) and resp.get("error"):
        raise AssertionError(str(resp["error"]))
    return f"new document '{resp.get('document')}' created"


def check_1_import_analyze():
    call("clear_design")
    _import_cube("t12_cube")
    out = _FS.analyze_mesh(mesh="0", units="cm")
    data = _parse_ok(out, "analyze_mesh")
    if data.get("watertight") is not True:
        raise AssertionError(f"watertight != True: {data.get('watertight')}")
    if data.get("recommended_strategy") != "prismatic":
        raise AssertionError(
            f"recommended_strategy={data.get('recommended_strategy')}, "
            f"expected prismatic")
    return (f"watertight={data['watertight']} "
            f"strategy={data['recommended_strategy']} "
            f"verts={data.get('vertex_count')} tris={data.get('triangle_count')}")


def check_2_slice():
    call("clear_design")
    _import_cube("t12_slice_cube")
    out = _FS.slice_mesh(mesh="0", axis="Z", height_cm=0.5, units="cm")
    data = _parse_ok(out, "slice_mesh")
    loops = data.get("loops") or []
    if len(loops) != 1:
        raise AssertionError(f"expected 1 loop, got {len(loops)}: {loops}")
    pts = loops[0].get("pts") or []
    if len(pts) != 4:
        raise AssertionError(f"expected 4 loop points, got {len(pts)}: {pts}")
    if loops[0].get("is_hole") is not False:
        raise AssertionError(
            f"expected outer loop, is_hole={loops[0].get('is_hole')}")
    plane = data.get("plane") or {}
    return (f"1 loop, {len(pts)} pts at Z={plane.get('height_cm')} cm "
            f"(cube mid-height)")


def check_3_prismatic():
    call("clear_design")
    _import_cube("t12_prism_cube")
    out = _FS.reconstruct_mesh(mesh="0", strategy="prismatic", units="mm")
    data = _parse_ok(out, "reconstruct_mesh prismatic")
    if data.get("strategy") != "prismatic":
        raise AssertionError(f"strategy={data.get('strategy')}, expected prismatic")
    if data.get("method") != "csg_translation":
        raise AssertionError(
            f"method={data.get('method')}, expected csg_translation")
    if data.get("bodies", 0) < 1:
        raise AssertionError(f"bodies={data.get('bodies')}")
    if data.get("features", 0) < 1:
        raise AssertionError(f"features={data.get('features')}")
    _await_min(brep_count, 1, "BRep count")
    types = timeline_types()
    if "ExtrudeFeature" not in types:
        raise AssertionError(f"timeline missing ExtrudeFeature: {types}")
    bb = brep_bbox(0)
    size = _bbox_size(bb)
    if any(abs(s - 1.0) > TOL_CM for s in size):
        raise AssertionError(
            f"BRep bbox={size}, expected ~[1,1,1] cm (1cm cube, units=mm)")
    return (f"method=csg_translation bodies={data['bodies']} "
            f"bbox={size} cm, ExtrudeFeature in timeline")


def check_4_revolved():
    call("clear_design")
    nodes, indices = _cylinder(r=1.0, h=2.0, n=24)
    imp = _FS.import_mesh_data(coordinates=nodes, triangle_indices=indices,
                               name="t12_cyl")
    if isinstance(imp, str) and imp.startswith("Error: "):
        raise AssertionError(f"import_mesh_data cylinder: {imp}")
    _parse_ok(imp, "import_mesh_data cylinder")
    out = _FS.reconstruct_mesh(mesh="0", strategy="revolved", units="mm")
    data = _parse_ok(out, "reconstruct_mesh revolved")
    if data.get("strategy") != "revolved":
        raise AssertionError(f"strategy={data.get('strategy')}, expected revolved")
    if data.get("method") != "revolve":
        raise AssertionError(f"method={data.get('method')}, expected revolve")
    if data.get("bodies", 0) < 1:
        raise AssertionError(f"bodies={data.get('bodies')}")
    _await_min(brep_count, 1, "BRep count")
    types = timeline_types()
    if "RevolveFeature" not in types:
        raise AssertionError(f"timeline missing RevolveFeature: {types}")
    return (f"method=revolve bodies={data['bodies']}, "
            f"RevolveFeature in timeline")


def check_5_parameter_schema():
    out = _FS.select_parameter_schema(
        object_class="generic",
        measured_facts={"bbox_cm": [1.0, 1.0, 1.0]},
        units="cm")
    data = _parse_ok(out, "select_parameter_schema")
    params = data.get("parameters") or []
    if len(params) < 1:
        raise AssertionError(f"no parameters bound: {data}")
    names = [p.get("name") for p in params]
    if any(not n for n in names):
        raise AssertionError(f"unnamed parameter: {params}")
    if not any(n in names for n in ("width", "depth", "height")):
        raise AssertionError(
            f"generic bbox roles width/depth/height missing: {names}")
    return (f"class={data.get('class')} params={len(params)} "
            f"names={names[:4]}")


def check_6_annotate():
    call("clear_design")
    _import_cube("t12_annotate_cube")
    if MODE == "fresh":
        _real_post = requests.post

        def fake_post(url, json=None, timeout=None):
            if isinstance(json, dict) and json.get("command") == "capture_mesh_views":
                return _FakeResp(fresh_dispatch(
                    "capture_mesh_views", json.get("params") or {}))
            return _real_post(url, json=json, timeout=timeout)

        requests.post = fake_post
        try:
            out = _FS.annotate_mesh_parameters(
                mesh="0",
                views=["isometric", "front", "top", "right"],
                units="cm")
        finally:
            requests.post = _real_post
    else:
        out = _FS.annotate_mesh_parameters(
            mesh="0", views=["isometric", "front", "top", "right"], units="cm")
    if not isinstance(out, list):
        raise AssertionError(f"expected [text, Image x4], got {out!r}")
    if len(out) != 5:
        raise AssertionError(f"expected 5 items, got {len(out)}")
    env = json.loads(out[0])
    if not isinstance(env, dict) or "error" in env:
        raise AssertionError(f"envelope errored: {env}")
    if env.get("workflow", {}).get("stage") != "annotate":
        raise AssertionError(f"workflow.stage={env.get('workflow')}")
    views = env.get("views") or []
    if len(views) != 4:
        raise AssertionError(f"expected 4 views, got {len(views)}")
    for v in views:
        b64 = v.get("image_base64", "")
        if not b64:
            raise AssertionError(f"view {v.get('view')} missing image_base64")
        png = base64.b64decode(b64)
        if not png.startswith(b"\x89PNG"):
            raise AssertionError(
                f"view {v.get('view')} not PNG magic: {png[:8]!r}")
    for img in out[1:]:
        if not img.data.startswith(b"\x89PNG"):
            raise AssertionError(f"Image block not PNG magic: {img.data[:8]!r}")
    return (f"4 views PNG-magic, stage={env['workflow']['stage']}, "
            f"measured_facts.strategy="
            f"{env.get('measured_facts', {}).get('recommended_strategy')}")


def check_7_compare():
    call("clear_design")
    _import_cube("t12_compare_cube")
    rec = _FS.reconstruct_mesh(mesh="0", strategy="prismatic", units="mm")
    rdata = _parse_ok(rec, "reconstruct_mesh (compare setup)")
    if rdata.get("method") != "csg_translation":
        raise AssertionError(f"method={rdata.get('method')} for compare setup")
    _await_min(brep_count, 1, "BRep count")
    out = _FS.compare_mesh_to_brep(mesh="0", body="0")
    data = _parse_ok(out, "compare_mesh_to_brep")
    ratio = data.get("volume_ratio")
    if not isinstance(ratio, (int, float)):
        raise AssertionError(f"volume_ratio missing: {data}")
    if abs(ratio - 1.0) > 0.05:
        raise AssertionError(
            f"volume_ratio={ratio}, expected within 5% of 1.0")
    if not data.get("sampled_deviation_cm", {}).get("samples", 0) > 0:
        raise AssertionError(f"no samples: {data.get('sampled_deviation_cm')}")
    return (f"volume_ratio={ratio} "
            f"method={data.get('method')} "
            f"samples={data.get('sampled_deviation_cm', {}).get('samples')}")


def check_8_workflow_guide():
    out = _FS.get_workflow_guide()
    try:
        data = json.loads(out)
    except (TypeError, ValueError) as e:
        raise AssertionError(f"get_workflow_guide: expected JSON, got {out!r} ({e})")
    if not isinstance(data, list) or len(data) < 8:
        raise AssertionError(
            f"expected >= 8 steps, got {len(data) if isinstance(data, list) else data!r}")
    tools = [s.get("tool") for s in data]
    if not all(tools):
        raise AssertionError(f"step missing tool: {data}")
    if "analyze_mesh" not in tools or "reconstruct_mesh" not in tools:
        raise AssertionError(f"guide missing core steps: {tools}")
    return f"{len(data)} steps: {tools}"


def check_9_organic():
    call("clear_design")
    _import_cube("t12_organic_cube")
    out = _FS.reconstruct_mesh(mesh="0", strategy="organic")
    # The not-available error surfaces EITHER as a JSON {"error": ...}
    # envelope OR as the raw "Error: " string from _call (_envelope_organic
    # returns the _call prefix verbatim when json.loads fails) -- both are
    # the graceful license-gate SKIP on this build.
    if (isinstance(out, str) and out.startswith("Error: ")
            and "not available" in out.lower()):
        raise SkipCheck(
            f"organic MeshConvertFeature is license-gated on this build: {out}")
    try:
        data = json.loads(out)
    except (TypeError, ValueError) as e:
        raise AssertionError(f"organic: expected JSON, got {out!r} ({e})")
    if "error" in data:
        if "not available" in data["error"].lower():
            raise SkipCheck(
                f"organic MeshConvertFeature is license-gated on this build: "
                f"{data['error']}")
        raise AssertionError(f"organic failed: {data['error']}")
    if data.get("strategy") != "organic":
        raise AssertionError(f"strategy={data.get('strategy')}, expected organic")
    if data.get("method") != "mesh_convert":
        raise AssertionError(f"method={data.get('method')}, expected mesh_convert")
    if data.get("preview_api") is not True:
        raise AssertionError(f"preview_api={data.get('preview_api')}")
    if "parametric" not in data:
        raise AssertionError(f"missing parametric flag: {data}")
    return (f"converted via mesh_convert parametric={data.get('parametric')} "
            f"(PREVIEW API)")


def check_10_failures():
    call("clear_design")
    # 10a: analyze_mesh with a missing mesh -> graceful exact error.
    out = _FS.analyze_mesh(mesh="999")
    data = json.loads(out)
    if data != {"error": "Mesh body '999' not found."}:
        raise AssertionError(f"analyze_mesh(mesh=999): {data!r}")
    # 10b: bad slice axis -> EXACT error (validated before any bridge call).
    out = _FS.slice_mesh(mesh="0", axis="W")
    data = json.loads(out)
    if data != {"error": "Axis must be X, Y, or Z"}:
        raise AssertionError(f"slice_mesh(axis=W): {data!r}")
    # 10c: unknown strategy -> EXACT error.
    out = _FS.reconstruct_mesh(strategy="bogus")
    data = json.loads(out)
    if data != {"error": "Unknown strategy 'bogus'. Supported: auto, "
                         "prismatic, revolved, csg_decompose, organic"}:
        raise AssertionError(f"reconstruct_mesh(strategy=bogus): {data!r}")
    return "analyze 999 / slice W / strategy bogus all exact-error"


def _run_all_checks():
    run_check("create fresh document", check_fresh_document)
    run_check("1. import_mesh_data + analyze_mesh (watertight, prismatic)",
              check_1_import_analyze)
    run_check("2. slice_mesh mid-height (1 loop, 4 pts)", check_2_slice)
    run_check("3. reconstruct_mesh prismatic (csg_translation, ExtrudeFeature, "
              "bbox)", check_3_prismatic)
    run_check("4. reconstruct_mesh revolved (RevolveFeature)", check_4_revolved)
    run_check("5. select_parameter_schema (named params)",
              check_5_parameter_schema)
    run_check("6. annotate_mesh_parameters (4 PNG views)", check_6_annotate)
    run_check("7. compare_mesh_to_brep (volume_ratio within 5%)",
              check_7_compare)
    run_check("8. get_workflow_guide (8+ steps)", check_8_workflow_guide)
    run_check("9. organic strategy (convert or SKIP)", check_9_organic)
    run_check("10. failure paths (exact errors)", check_10_failures)


def _summary():
    executed = [r for r in _results if r[1] is not None]
    passed = sum(1 for _, ok, _ in executed if ok)
    skipped = sum(1 for _, ok, _ in _results if ok is None)
    total = len(executed)
    print()
    if skipped:
        print(f"{skipped} check(s) SKIPPED")
    print(f"{passed}/{total} checks PASSED")
    return 0 if passed == total else 1


def main():
    global MODE
    if "--mode" in sys.argv:
        i = sys.argv.index("--mode")
        if i + 1 >= len(sys.argv):
            print("Usage: py tests/test_mesh_reconstruction_live.py "
                  "[--mode fresh|http]")
            return 2
        MODE = sys.argv[i + 1].lower()
    if MODE not in ("fresh", "http"):
        print(f"Unknown --mode {MODE!r}; expected 'fresh' or 'http'")
        return 2

    print(f"Mesh-to-parametric live integration test  "
          f"(mode={MODE}, bridge={BASE_URL})")

    # Connection probe first -- the http probe reports a clear bridge error
    # (and exit 1) when Fusion is not reachable instead of a traceback.
    try:
        r = requests.post(BASE_URL, json={"command": "get_info", "params": {}},
                          timeout=10)
        r.raise_for_status()
    except requests.exceptions.RequestException as e:
        print(f"Cannot reach Fusion 360 on http://127.0.0.1:7432: {e}")
        print("Make sure Fusion 360 is open and the FusionMCP add-in is running.")
        return 1

    if MODE == "fresh":
        print(f"Server source: {SERVER_PATH}")
        print("NOTE: the RUNNING add-in serves the OLD command set; the new")
        print("      handlers are driven in-process via execute_script + the")
        print("      fresh-module import of the repo FusionMCP.py.")
    print()

    _install_server(None)
    _run_all_checks()
    return _summary()


# ---- pytest integration (bridge-gated; skips cleanly when Fusion is down) ----

import pytest  # noqa: E402  (kept out of the standalone-import hot path)


@pytest.fixture(scope="module")
def bridge():
    """Skip every test when Fusion is not reachable (headless CI runs)."""
    if not _probe_bridge():
        pytest.skip(
            "Fusion bridge unreachable on http://127.0.0.1:7432 - is Fusion "
            "open and the FusionMCP add-in running?")
    return True


def test_mesh_reconstruction_live_suite(bridge, monkeypatch):
    """Run the SAME 10 checks as the standalone suite (fresh mode) and fail
    the pytest run if any non-skipped check FAILED."""
    global MODE
    _results.clear()
    monkeypatch.setattr(sys.modules[__name__], "MODE", "fresh")
    _install_server(monkeypatch)
    _run_all_checks()
    executed = [r for r in _results if r[1] is not None]
    failed = [(n, d) for n, ok, d in _results if ok is False]
    assert not failed, (
        f"{len(failed)} of {len(executed)} checks FAILED: {failed}")


if __name__ == "__main__":
    sys.exit(main())
