"""
F3 -- fresh-process MCP probe (dismantle-one-shot-reconstruction).

Executes in its OWN python process and imports the CURRENT working-tree
``mcp_server/fusion_server.py`` (NOT any pre-existing / long-running MCP server
process).  Every call goes through this process -> the unchanged add-in
(FusionMCP.py on 127.0.0.1:7432).

Asserts the dismantled working tree:
  * MCP registry (fs.mcp._tool_manager._tools) does NOT contain
    ``reconstruct_mesh`` / ``reconstruct_from_faces``.
  * Every foundational tool returns valid JSON / valid envelope (no
    "Cannot reach Fusion 360", no ImportError, no unrecoverable "Unexpected
    error").
  * analyze_mesh output: NO top-level ``recommended_strategy``; ``primitive_hints``
    IS present.
  * structure_graph workstream: NO ``unit_types``, NO ``rebuild_order``;
    ``base_face_per_component`` and ``dag_has_cycles`` ARE present.
  * query_structure_graph returns valid rows.
  * slice_mesh returns loops.
  * compare_mesh_to_brep returns a geometry summary.
  * get_workflow_guide(step="reconstruct") returns the DISMANTLED placeholder.
  * annotate_mesh_parameters / review_reconstruction envelopes have NO
    ``workflow`` dict; image blocks still present.
  * select_parameter_schema behaves unchanged.

Evidence written:
  * .omo/evidence/dismantle-F3-mcp-probe.log   (this probe's full output)
  * .omo/evidence/dismantle-F3-sample.json     (analyze_mesh, structure_graph
    workstream, get_workflow_guide("reconstruct"), MCP registry list)

Run:  py -3.13 .omo/evidence/dismantle-F3-probe.py
"""

import base64
import datetime
import json
import os
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
EVIDENCE_DIR = os.path.join(REPO_ROOT, ".omo", "evidence")
LOG_PATH = os.path.join(EVIDENCE_DIR, "dismantle-F3-mcp-probe.log")
SAMPLE_PATH = os.path.join(EVIDENCE_DIR, "dismantle-F3-sample.json")

# ---- fresh-process import of the WORKING TREE server module -----------------
sys.path.insert(0, REPO_ROOT)
sys.path.insert(0, os.path.join(REPO_ROOT, "mcp_server"))

import fusion_server as fs  # noqa: E402

_EXPECTED_SERVER = os.path.join(REPO_ROOT, "mcp_server", "fusion_server.py")
assert os.path.normcase(os.path.abspath(fs.__file__)) == os.path.normcase(
    os.path.abspath(_EXPECTED_SERVER)), \
    f"probe imported the WRONG fusion_server: {fs.__file__}"

# ---- log plumbing (probe writes the log itself; survives partial crashes) ---
LOG_LINES = []
RESULTS = []  # (name, ok)


def _write_log():
    with open(LOG_PATH, "w", encoding="utf-8") as f:
        f.write("\n".join(LOG_LINES) + "\n")


def log(msg=""):
    line = str(msg)
    LOG_LINES.append(line)
    print(line, flush=True)
    _write_log()


def check_raw_failures(raw):
    """Return detail strings for unrecoverable transport/import failures."""
    outs = []
    if "Cannot reach Fusion 360" in str(raw):
        outs.append("TRANSPORT FAIL: 'Cannot reach Fusion 360' in output")
    if "ImportError" in str(raw):
        outs.append("ImportError in output")
    if str(raw).startswith("Unexpected error"):
        outs.append(f"unrecoverable: {raw}")
    return outs


def json_ok(raw):
    if isinstance(raw, str) and raw.startswith(("Cannot reach", "Unexpected error")):
        return None, raw
    try:
        return json.loads(raw), None
    except Exception as e:  # noqa: BLE001
        return None, f"not valid JSON: {e}"


def finish(name, ok, details):
    RESULTS.append((name, ok))
    log(f"[{'PASS' if ok else 'FAIL'}] {name}")
    for d in details:
        log(f"        {d}")


def main():
    ts = datetime.datetime.now().isoformat(timespec="seconds")
    log("=" * 70)
    log("F3 - fresh-process MCP probe (dismantle-one-shot-reconstruction)")
    log(f"timestamp : {ts}")
    log(f"server    : {fs.__file__}")
    log(f"bridge    : http://127.0.0.1:7432  (add-in is UNCHANGED pre-dismantle)")
    log("")

    # ---------------------------------------------------------------- registry
    log("=" * 70)
    log("MCP REGISTRY CHECK  (fs.mcp._tool_manager._tools)")
    log("=" * 70)
    tools = fs.mcp._tool_manager._tools
    names = sorted(tools.keys())
    log(f"registered tool count : {len(names)}")
    log(f"tool list             : {json.dumps(names)}")
    reg_ok = True
    for forbidden in ("reconstruct_mesh", "reconstruct_from_faces"):
        present = forbidden in tools
        log(f"forbidden key '{forbidden}' present : {present}")
        if present:
            reg_ok = False
    finish("registry_excludes_removed_tools", reg_ok,
           [f"tool_count={len(names)}",
            f"reconstruct_mesh absent={('reconstruct_mesh' not in tools)}",
            f"reconstruct_from_faces absent={('reconstruct_from_faces' not in tools)}"])
    # keep a dedicated registry result separate from the 10-tool sequence
    if not reg_ok:
        # do not continue: the whole probe premise is broken
        finish("OVERALL", False, ["registry still exposes removed tools"])
        _write_log()
        return 1

    # -------------------------------------------------------------- discovery
    log("")
    log("=" * 70)
    log("BODY DISCOVERY (reuse existing bodies; create only if missing)")
    log("=" * 70)

    # BRep discovery via get_bodies_info (BRep bodies only).
    brep_ref = None
    brep_info_raw = fs.get_bodies_info()
    brep_info = None
    try:
        brep_info = json.loads(brep_info_raw)
    except Exception:  # noqa: BLE001
        brep_info = None
    brep_bodies = (brep_info or {}).get("bodies", []) if isinstance(brep_info, dict) else []
    log(f"get_bodies_info -> {len(brep_bodies)} BRep body/bodies")
    for b in brep_bodies:
        log(f"    BRep: index={b.get('index')} name={b.get('name')} "
            f"size_cm={b.get('size_cm')}")
    for pref in ("scad_linear_extrude_2", "scad_linear_extrude"):
        for b in brep_bodies:
            if b.get("name") == pref:
                brep_ref = b["name"]
                break
        if brep_ref:
            break
    if brep_ref is None and brep_bodies:
        brep_ref = str(brep_bodies[0]["index"])

    # Mesh discovery: mesh index 0 first, then the known name.
    mesh_ref = None
    for candidate in ("0", "fusionmcp_render_88c7d113"):
        raw = fs.analyze_mesh(mesh=candidate, units="cm")
        d, err = json_ok(raw)
        if d is not None and "error" not in d and "primitive_hints" in d:
            mesh_ref = candidate
            log(f"mesh discovered: {candidate} (analyze_mesh returned a valid report)")
            break
        log(f"mesh candidate '{candidate}' not usable: {raw[:200]}")
    if mesh_ref is None:
        log("no usable mesh body -> creating fresh document + run_scad cube")
        fs.create_new_document(name="F3 QA Probe")
        scad_raw = fs.run_scad(code="cube([100,100,100]);", units="mm", job_id="sync")
        d, err = json_ok(scad_raw)
        if err or (d is not None and "error" in d):
            finish("mesh_creation", False, [f"run_scad failed: {scad_raw[:400]}"])
            mesh_ref = None
        else:
            mesh_ref = "0"
            log(f"run_scad created mesh: {json.dumps(d) if d else scad_raw[:200]}")
    if mesh_ref is None:
        finish("OVERALL", False, ["no mesh body available"])
        _write_log()
        return 1

    # BRep creation fallback (sketch/extrude per plan).
    if brep_ref is None:
        log("no BRep body -> creating one via sketch + extrude")
        fs.create_sketch(plane="XY", name="F3_brep_sketch")
        fs.draw_center_rectangle(cx=0, cy=0, width=10, height=10)
        fs.finish_sketch()
        ext_raw = fs.extrude_sketch(distance=10)
        try:
            ext = json.loads(ext_raw)
            log(f"extrude result: {json.dumps(ext)}")
        except Exception:  # noqa: BLE001
            log(f"extrude raw: {ext_raw}")
        info2 = json.loads(fs.get_bodies_info())
        b2 = (info2.get("bodies") or [])
        if b2:
            brep_ref = str(b2[-1]["index"])
            log(f"BRep created: index={b2[-1]['index']} name={b2[-1]['name']}")
        else:
            finish("brep_creation", False, ["extrude produced no BRep body"])
    log(f"using mesh_ref={mesh_ref!r} brep_ref={brep_ref!r}")
    log("")

    # ----------------------------------------------------------- 10-tool seq
    log("=" * 70)
    log("FOUNDATIONAL TOOL SEQUENCE")
    log("=" * 70)

    sample = {"timestamp": ts, "server_file": fs.__file__, "mesh_ref": mesh_ref,
              "brep_ref": brep_ref}

    # [1] fusion_status
    raw = fs.fusion_status()
    ok = "Cannot reach" not in raw and "not reachable" not in raw
    finish("fusion_status", ok, [f"output: {raw}"])

    # [2] analyze_mesh
    raw = fs.analyze_mesh(mesh=mesh_ref, units="cm")
    fail = check_raw_failures(raw)
    d, err = json_ok(raw)
    details = [f"json_ok={err is None}"] + ([] if err is None else [err])
    if d is not None:
        details.append(f"top-level keys: {sorted(d.keys())}")
        details.append(f"recommended_strategy present: {'recommended_strategy' in d}")
        details.append(f"primitive_hints present: {'primitive_hints' in d}")
    else:
        fail.append(f"raw[:200]={raw[:200]}")
    ok = (not fail) and (d is not None) and ("recommended_strategy" not in d) and ("primitive_hints" in d)
    finish("analyze_mesh (no recommended_strategy; primitive_hints kept)",
          ok, details + fail)
    if d is not None:
        sample["analyze_mesh"] = d

    # [3] structure_graph
    raw = fs.structure_graph(mesh=mesh_ref, units="cm", preset="accurate")
    fail = check_raw_failures(raw)
    d, err = json_ok(raw)
    details = [f"json_ok={err is None}"] + ([] if err is None else [err])
    ws = {}
    if d is not None:
        ws = d.get("workstream") or {}
        details.append(f"workstream present: {'workstream' in d}")
        details.append(f"workstream keys: {sorted(ws.keys())}")
        details.append(f"unit_types present: {'unit_types' in ws}")
        details.append(f"rebuild_order present: {'rebuild_order' in ws}")
        details.append(f"base_face_per_component present: {'base_face_per_component' in ws}")
        details.append(f"dag_has_cycles present: {'dag_has_cycles' in ws}")
        details.append(f"component_count={d.get('component_count')} face_count={d.get('face_count')}")
    else:
        fail.append(f"raw[:200]={raw[:200]}")
    ok = (not fail) and (d is not None) and ("unit_types" not in ws) and (
        "rebuild_order" not in ws) and ("base_face_per_component" in ws) and (
        "dag_has_cycles" in ws)
    finish("structure_graph (no unit_types/rebuild_order; base_face/dag kept)",
          ok, details + fail)
    if d is not None:
        sample["structure_graph_workstream"] = ws
        sample["structure_graph_counts"] = {
            k: d.get(k) for k in ("component_count", "face_count")
        }

    # [4] query_structure_graph
    sql = ("SELECT node_id, area_cm2 FROM nodes WHERE label='Face' "
           "ORDER BY area_cm2 DESC LIMIT 5")
    raw = fs.query_structure_graph(mesh=mesh_ref, sql=sql)
    fail = check_raw_failures(raw)
    d, err = json_ok(raw)
    details = [f"json_ok={err is None}"] + ([] if err is None else [err])
    rows = (d or {}).get("rows", []) if isinstance(d, dict) else []
    details.append(f"sql: {sql}")
    details.append(f"row_count={len(rows)}")
    if rows:
        details.append(f"first_row: {rows[0]}")
    ok = (not fail) and (d is not None) and isinstance(rows, list) and len(rows) > 0
    finish("query_structure_graph (returns valid rows)", ok, details + fail)
    if d is not None:
        sample["query_structure_graph"] = {"row_count": len(rows),
                                           "columns": d.get("columns")}

    # [5] slice_mesh
    raw = fs.slice_mesh(mesh=mesh_ref, axis="Z", height_cm=3.0, units="cm")
    fail = check_raw_failures(raw)
    d, err = json_ok(raw)
    details = [f"json_ok={err is None}"] + ([] if err is None else [err])
    loops = (d or {}).get("loops", []) if isinstance(d, dict) else []
    details.append(f"loops count: {len(loops)}")
    if loops:
        pts = loops[0].get("pts") or loops[0].get("points") or []
        details.append(f"loop[0] is_hole={loops[0].get('is_hole')} pts={len(pts)}  "
                       f"loop_shapes={[len(lp.get('pts') or lp.get('points') or []) for lp in loops]}")
    ok = (not fail) and (d is not None) and isinstance(loops, list) and len(loops) > 0
    finish("slice_mesh (returns loops)", ok, details + fail)
    if d is not None:
        sample["slice_mesh"] = {"loop_count": len(loops)}

    # [6] compare_mesh_to_brep
    raw = fs.compare_mesh_to_brep(mesh=mesh_ref, body=brep_ref)
    fail = check_raw_failures(raw)
    d, err = json_ok(raw)
    details = [f"json_ok={err is None}"] + ([] if err is None else [err])
    if d is not None:
        details.append(f"keys: {sorted(d.keys())}")
        if "volume_ratio" in d:
            details.append(f"volume_ratio={d.get('volume_ratio')}")
        if "method" in d:
            details.append(f"method={d.get('method')}")
        if "error" in d:
            details.append(f"error: {d.get('error')}")
    else:
        fail.append(f"raw[:200]={raw[:200]}")
    ok = (not fail) and (d is not None) and ("error" not in d) and (
        "volume_ratio" in d or "method" in d)
    finish("compare_mesh_to_brep (geometry summary)", ok, details + fail)
    if d is not None:
        sample["compare_mesh_to_brep"] = d

    # [7] get_workflow_guide(step="reconstruct")
    raw = fs.get_workflow_guide(step="reconstruct")
    fail = check_raw_failures(raw)
    d, err = json_ok(raw)
    details = [f"json_ok={err is None}"] + ([] if err is None else [err])
    note = ""
    if d is not None:
        note = d.get("note", "")
        details.append(f"tool: {d.get('tool')}")
        details.append(f"note contains 'DISMANTLED': {'DISMANTLED' in note}")
        details.append(f"branch: {d.get('branch')}")
        details.append(f"fallback: {d.get('fallback')}")
    else:
        fail.append(f"raw[:200]={raw[:200]}")
    ok = (not fail) and (d is not None) and d.get("tool") == "reconstruct_mesh" and (
        "DISMANTLED" in note)
    finish("get_workflow_guide(step='reconstruct') (DISMANTLED placeholder)",
          ok, details + fail)
    if d is not None:
        sample["get_workflow_guide_reconstruct"] = d

    # [8] annotate_mesh_parameters (JOB tool, sync)
    raw = fs.annotate_mesh_parameters(mesh=mesh_ref, units="cm", job_id="sync")
    fail = check_raw_failures(raw)
    details = []
    envelope = None
    if isinstance(raw, list) and raw:
        envelope_raw = raw[0]
        envelope, err = json_ok(envelope_raw)
        details.append(f"content items: {len(raw)}")
        details.append(f"image blocks present: {sum(1 for x in raw[1:] if not isinstance(x, str))}")
        if envelope is not None:
            details.append(f"envelope keys: {sorted(envelope.keys())}")
            details.append(f"workflow present: {'workflow' in envelope}")
            details.append(f"mesh: {envelope.get('mesh')}  views: {len(envelope.get('views', []))}")
            details.append(f"measured_facts present: {'measured_facts' in envelope}")
        else:
            fail.append(f"envelope_raw[:200]={str(envelope_raw)[:200]}")
    else:
        fail.append(f"expected list envelope, got: {str(raw)[:200]}")
    ok = (not fail) and (envelope is not None) and ("workflow" not in envelope) and (
        "measured_facts" in envelope) and (len(raw) > 1)
    finish("annotate_mesh_parameters (no workflow dict; image block kept)",
          ok, details + fail)
    if envelope is not None:
        sample["annotate_mesh_parameters"] = {
            "content_items": len(raw), "envelope_keys": sorted(envelope.keys()),
            "image_blocks": sum(1 for x in raw[1:] if not isinstance(x, str))}

    # [9] review_reconstruction (JOB tool, sync)
    raw = fs.review_reconstruction(mesh=mesh_ref, body=brep_ref, job_id="sync")
    fail = check_raw_failures(raw)
    details = []
    envelope = None
    if isinstance(raw, list) and raw:
        envelope_raw = raw[0]
        envelope, err = json_ok(envelope_raw)
        details.append(f"content items: {len(raw)}")
        details.append(f"image blocks present: {sum(1 for x in raw[1:] if not isinstance(x, str))}")
        if envelope is not None:
            details.append(f"envelope keys: {sorted(envelope.keys())}")
            details.append(f"workflow present: {'workflow' in envelope}")
            details.append(f"pairs present: {'pairs' in envelope}  geometry present: {'geometry' in envelope}")
            details.append(f"pairs count: {len(envelope.get('pairs', []))}")
        else:
            fail.append(f"envelope_raw[:200]={str(envelope_raw)[:200]}")
    else:
        fail.append(f"expected list envelope, got: {str(raw)[:200]}")
    ok = (not fail) and (envelope is not None) and ("workflow" not in envelope) and (
        "pairs" in envelope) and ("geometry" in envelope) and (len(raw) > 1)
    finish("review_reconstruction (no workflow dict; image block kept)",
          ok, details + fail)
    if envelope is not None:
        sample["review_reconstruction"] = {
            "content_items": len(raw), "envelope_keys": sorted(envelope.keys()),
            "pairs": len(envelope.get("pairs", [])), "has_geometry": "geometry" in envelope}

    # [10] select_parameter_schema
    raw = fs.select_parameter_schema(object_class="generic",
                                     measured_facts={"bbox_cm": [10, 5, 5]})
    fail = check_raw_failures(raw)
    d, err = json_ok(raw)
    details = [f"json_ok={err is None}"] + ([] if err is None else [err])
    if d is not None:
        details.append(f"keys: {sorted(d.keys())}")
        details.append(f"parameters count: {len(d.get('parameters', {}))}")
    else:
        fail.append(f"raw[:200]={raw[:200]}")
    ok = (not fail) and (d is not None) and ("error" not in d) and (
        "parameters" in d)
    finish("select_parameter_schema (unchanged behavior)", ok, details + fail)
    if d is not None:
        sample["select_parameter_schema"] = d

    # ------------------------------------------------------------ sample JSON
    sample["mcp_registry_tool_list"] = names
    with open(SAMPLE_PATH, "w", encoding="utf-8") as f:
        json.dump(sample, f, indent=2, default=str)
    log("")
    log(f"sample JSON written -> {SAMPLE_PATH}")

    # ----------------------------------------------------------------- summary
    log("")
    log("=" * 70)
    log("F3 SUMMARY")
    log("=" * 70)
    all_ok = True
    for name, ok in RESULTS:
        all_ok = all_ok and ok
        log(f"  [{'PASS' if ok else 'FAIL'}] {name}")
    log("")
    verdict = "OVERALL PASS" if all_ok else "OVERALL FAIL"
    log(f"F3 OVERALL: {verdict}")
    log(f"F3 VERDICT: {verdict} -- foundational toolset verified against "
        f"working-tree fusion_server.py ({fs.__file__})")
    _write_log()
    return 0 if all_ok else 1


if __name__ == "__main__":
    try:
        rc = main()
    except Exception as e:  # noqa: BLE001
        import traceback
        LOG_LINES.append("")
        LOG_LINES.append("PROBE CRASHED (unexpected top-level exception):")
        LOG_LINES.append(traceback.format_exc())
        _write_log()
        print("PROBE CRASHED:", e)
        rc = 1
    sys.exit(rc)
