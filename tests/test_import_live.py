#!/usr/bin/env python3
"""
Live integration tests for the file import tools added in Wave 2 (todos 7-9):
  import_cad_file    -- STEP/SAT/SMT/IGES/F3D
  import_mesh_file   -- STL/3MF
  import_sketch_file -- SVG/DXF

Requires Fusion 360 to be running with the FusionMCP add-in (bridge on
127.0.0.1:7432). The RUNNING add-in copy may be older than the repo and not
have the three import commands in its HTTP dispatch -- so the default `fresh`
mode drives the repo's own FusionMCP.py handlers inside Fusion via
execute_script (fresh-module pattern with mod.app = app). `http` mode uses
the running bridge for whatever commands it exposes (honestly fails on any
import command the old add-in does not dispatch).

Usage:
    py tests/test_import_live.py             # fresh mode (default)
    py tests/test_import_live.py --mode http

Exit code: 0 if every check PASSED, 1 if any FAILED, 2 on usage error.
"""

import json
import os
import sys
import tempfile
import urllib.request

FUSION_URL = "http://127.0.0.1:7432"
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FUSIONMCP_PATH = os.path.join(REPO_ROOT, "FusionMCP.py")

_results = []  # (name, ok, detail)


def check(name, ok, detail=""):
    status = "PASS" if ok else "FAIL"
    print(f"[{status}] {name}" + (f" -- {detail}" if detail else ""))
    _results.append((name, ok, detail))
    return ok


def http_post(payload):
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        FUSION_URL + "/command", data=data,
        headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=90) as resp:
        return json.loads(resp.read().decode("utf-8"))


def call(command, params=None):
    """POST a command to the running bridge; raises RuntimeError on failure."""
    try:
        return http_post({"command": command, "params": params or {}})
    except Exception as exc:
        raise RuntimeError(
            f"Cannot reach Fusion 360 at {FUSION_URL}: {exc}. "
            "Make sure Fusion 360 is open and the FusionMCP add-in is running."
        ) from exc


def run_code(code):
    """Execute Python inside Fusion via execute_script; returns parsed output."""
    resp = call("execute_script", {"code": code})
    if "error" in resp:
        raise RuntimeError("execute_script failed inside Fusion:\n" + resp["error"])
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


def _fresh_module_code():
    """Shared loader: re-import FusionMCP.py fresh inside Fusion."""
    return (
        "import importlib.util\n"
        "spec = importlib.util.spec_from_file_location('fusionmcp_dev', "
        + repr(FUSIONMCP_PATH) + ")\n"
        "mod = importlib.util.module_from_spec(spec)\n"
        "spec.loader.exec_module(mod)\n"
        "mod.app = app\n"
    )


def fresh_import(handler, params):
    """Drive a FusionMCP.py import handler in a fresh module inside Fusion."""
    inner = _fresh_module_code() + (
        "out = mod." + handler + "(root, " + repr(params) + ")\n"
        "result['output'] = out\n"
    )
    return run_code(inner)


def fresh_root_call(handler):
    """Drive a root-only FusionMCP.py handler (no params) in a fresh module."""
    inner = _fresh_module_code() + (
        "out = mod." + handler + "(root)\n"
        "result['output'] = out\n"
    )
    return run_code(inner)


def fresh_state():
    """Design state counts via execute_script (fresh-mode ground truth)."""
    return run_code(
        "result['output'] = {'bodies': root.bRepBodies.count, "
        "'sketches': root.sketches.count, 'mesh_bodies': root.meshBodies.count}"
    )


def http_state():
    info = call("get_info")
    return {
        "bodies": len(info.get("bodies", [])),
        "sketches": len(info.get("sketches", [])),
        "mesh_bodies": len(info.get("mesh_bodies", [])),
    }


def state(mode):
    return fresh_state() if mode == "fresh" else http_state()


def do_import(mode, http_cmd, fresh_handler, params):
    if mode == "fresh":
        return fresh_import(fresh_handler, params)
    return call(http_cmd, params)


def write_stl(path):
    """Minimal valid ASCII STL: a tetrahedron (4 facets). BOM-free."""
    verts = [(0, 0, 0), (10, 0, 0), (0, 10, 0), (0, 0, 10)]
    faces = [(0, 1, 2), (0, 1, 3), (0, 2, 3), (1, 2, 3)]
    with open(path, "w") as f:
        f.write("solid tetra\n")
        for i, j, k in faces:
            f.write("  facet normal 0 0 0\n    outer loop\n")
            for idx in (i, j, k):
                x, y, z = verts[idx]
                f.write(f"      vertex {x} {y} {z}\n")
            f.write("    endloop\n  endfacet\n")
        f.write("endsolid tetra\n")


def write_svg(path):
    """Minimal SVG containing a rectangle path. BOM-free."""
    with open(path, "w", encoding="utf-8") as f:
        f.write('<?xml version="1.0" encoding="UTF-8"?>\n')
        f.write('<svg xmlns="http://www.w3.org/2000/svg" '
                'width="100" height="100" viewBox="0 0 100 100">\n')
        f.write('  <rect x="10" y="10" width="80" height="60"/>\n')
        f.write('</svg>\n')


def main():
    mode = "fresh"
    if "--mode" in sys.argv:
        i = sys.argv.index("--mode")
        if i + 1 >= len(sys.argv):
            print("Usage: py tests/test_import_live.py [--mode http|fresh]")
            return 2
        mode = sys.argv[i + 1].lower()
    if mode not in ("http", "fresh"):
        print(f"Unknown mode '{mode}' (use --mode http|fresh)")
        return 2

    print(f"FusionMCP import live tests  (mode={mode}, bridge={FUSION_URL})")

    # Connection probe first -- plan line 214 requires a clear error message
    # when Fusion is not reachable instead of a traceback.
    try:
        req = urllib.request.urlopen(FUSION_URL + "/ping", timeout=5)
        ping = json.loads(req.read().decode("utf-8"))
        print(f"Connected: {ping.get('message')}")
    except Exception as exc:
        print(f"Cannot reach Fusion 360 at {FUSION_URL}: {exc}")
        print("Make sure Fusion 360 is open and the FusionMCP add-in is running.")
        return 1

    # Snapshot pre-existing documents so the doc this suite creates can be
    # closed afterwards (the name is only echoed; the doc stays open forever).
    pre_ids = _open_doc_ids()

    tmp = tempfile.mkdtemp(prefix="fusionmcp_import_live_")
    step_path = os.path.join(tmp, "roundtrip.step")
    stl_path = os.path.join(tmp, "tetra.stl")
    svg_path = os.path.join(tmp, "rect.svg")
    obj_path = os.path.join(tmp, "bad.obj")
    pdf_path = os.path.join(tmp, "bad.pdf")
    missing_step = os.path.join(tmp, "does_not_exist.step")
    missing_svg = os.path.join(tmp, "does_not_exist.svg")
    print(f"Fixtures dir: {tmp}")

    try:
        # --- setup: fresh doc + 10x10x10 box (HTTP commands available in both modes) ---
        call("create_new_document", {"name": "import_live_test"})
        call("create_sketch", {"plane": "XY"})
        call("draw_rectangle", {"x1": 0, "y1": 0, "x2": 10, "y2": 10})
        ext = call("extrude", {"distance": 10})
        check("setup box (10x10x10)", ext.get("bodies", 0) >= 1,
              f"bodies={ext.get('bodies')}")

        # --- 1. STEP roundtrip: export -> clear -> import -> body exists ---
        exp = call("export_step", {"path": step_path})
        exported = exp.get("exported_step")
        check("export STEP", bool(exported) and os.path.exists(step_path),
              f"exported_step={exported}")

        call("clear_design")
        st = state(mode)
        check("clear design (bodies=0)", st["bodies"] == 0, f"bodies={st['bodies']}")

        imp = do_import(mode, "import_cad_file", "_import_cad_file",
                        {"path": step_path})
        if "error" in imp:
            check("import STEP (bodies_added>=1)", False, str(imp["error"]))
        else:
            check("import STEP (bodies_added>=1)", imp.get("bodies_added", 0) >= 1,
                  f"bodies_added={imp.get('bodies_added')}")
        st = state(mode)
        check("body exists after STEP import", st["bodies"] >= 1,
              f"bodies={st['bodies']}")

        # --- 2. Mesh import: STL tetrahedron -> mesh body exists ---
        call("clear_design")
        write_stl(stl_path)
        imp = do_import(mode, "import_mesh_file", "_import_mesh_file",
                        {"path": stl_path, "units": "mm"})
        if "error" in imp:
            check("import STL mesh (mesh_bodies>=1)", False, str(imp["error"]))
        else:
            check("import STL mesh (mesh_bodies>=1)", imp.get("mesh_bodies", 0) >= 1,
                  f"mesh_bodies={imp.get('mesh_bodies')}")
        st = state(mode)
        check("mesh body exists in design",
              st.get("mesh_bodies") is not None and st["mesh_bodies"] >= 1,
              f"mesh_bodies={st.get('mesh_bodies')}")

        # --- 2b. Mesh visibility: get_bodies_info lists mesh bodies ---
        bi = (fresh_root_call("_get_bodies_info") if mode == "fresh"
              else call("get_bodies_info"))
        mesh_entries = [b for b in bi.get("bodies", [])
                        if b.get("type") == "mesh"]
        check("get_bodies_info lists mesh body (type=mesh)",
              len(mesh_entries) >= 1
              and mesh_entries[0].get("triangles", 0) >= 1,
              f"mesh entries={mesh_entries}")

        # --- 3. SVG import: rectangle -> sketch exists ---
        call("clear_design")
        write_svg(svg_path)
        imp = do_import(mode, "import_sketch_file", "_import_sketch_file",
                        {"path": svg_path})
        if "error" in imp:
            check("import SVG sketch (sketches_added>=1)", False, str(imp["error"]))
        else:
            check("import SVG sketch (sketches_added>=1)",
                  imp.get("sketches_added", 0) >= 1,
                  f"sketches_added={imp.get('sketches_added')}")
        st = state(mode)
        check("sketch exists after SVG import", st["sketches"] >= 1,
              f"sketches={st['sketches']}")

        # --- 4. Failure paths (plan line 214) ---
        imp = do_import(mode, "import_cad_file", "_import_cad_file",
                        {"path": missing_step})
        err = str(imp.get("error", ""))
        check("import_cad_file missing file -> 'not found'",
              "not found" in err.lower(), f"error={err!r}")

        # Extension check runs AFTER the existence check, so the file must exist.
        with open(obj_path, "w") as f:
            f.write("placeholder")
        imp = do_import(mode, "import_mesh_file", "_import_mesh_file",
                        {"path": obj_path})
        err = str(imp.get("error", ""))
        check("import_mesh_file .obj -> 'not supported'",
              "not supported" in err.lower(), f"error={err!r}")

        with open(pdf_path, "w") as f:
            f.write("placeholder")
        imp = do_import(mode, "import_sketch_file", "_import_sketch_file",
                        {"path": pdf_path})
        err = str(imp.get("error", ""))
        check("import_sketch_file .pdf -> 'unsupported'",
              "unsupported" in err.lower(), f"error={err!r}")

        imp = do_import(mode, "import_sketch_file", "_import_sketch_file",
                        {"path": missing_svg})
        err = str(imp.get("error", ""))
        check("import_sketch_file missing file -> 'not found'",
              "not found" in err.lower(), f"error={err!r}")
    except RuntimeError as exc:
        print(f"\nFATAL: {exc}")
        _close_docs_except(pre_ids)
        return 1

    _close_docs_except(pre_ids)
    passed = sum(1 for _, ok, _ in _results if ok)
    total = len(_results)
    print(f"\n{passed}/{total} checks PASSED")
    return 0 if passed == total else 1


if __name__ == "__main__":
    sys.exit(main())
