"""
Mesh-to-parametric workflow guide (pure data, served locally).

Ordered 8-step pipeline that chains the mesh tools into a complete
reconstruction flow:

    import -> analyze -> slice -> annotate -> select_parameter_schema
           -> reconstruct -> compare -> review

Each step describes the server tool to call, what it needs, what it returns,
the MODEL ACTION the assistant must take (where the loop is interactive),
the strategy branch (reconstruct's auto routing), and any fallback.

Pure stdlib (json only). No numpy, no requests, no Fusion imports -- this
module is headless-importable and deterministic, which keeps the guide
testable without a running Fusion bridge.
"""

import json
from typing import Dict, Optional

# Ordered pipeline steps.  `tool` matches the actual server tool name
# (import_mesh_file / import_mesh_data for the import step), so get_step()
# can resolve lookups against real tool names.  Every step carries exactly
# the keys: tool, purpose, inputs, outputs, model_action, branch, fallback.
GUIDE = [
    {
        "tool": "import_mesh_file",
        "purpose": ("Import the source mesh into the current Fusion document. "
                    "Use import_mesh_file for an STL/3MF on disk, or "
                    "import_mesh_data for raw triangle coordinates + indices."),
        "inputs": [
            "file path + units (import_mesh_file)",
            "coordinates + triangle_indices (import_mesh_data)",
        ],
        "outputs": ["mesh body name"],
        "model_action": None,
        "branch": None,
        "fallback": None,
    },
    {
        "tool": "analyze_mesh",
        "purpose": ("Analyze the mesh geometry and report measured facts "
                    "(watertightness, volume, bounding box, symmetry, primitive "
                    "hints) plus a recommended reconstruction strategy."),
        "inputs": ["mesh", "units"],
        "outputs": ["analysis report incl. recommended_strategy"],
        "model_action": None,
        "branch": None,
        "fallback": None,
    },
    {
        "tool": "slice_mesh",
        "purpose": ("Slice the mesh with an axis-aligned plane at a chosen "
                    "height to extract 2D cross-section loops (outer contours "
                    "and holes) that characterize the profile."),
        "inputs": ["mesh", "axis", "height_cm", "units"],
        "outputs": [
            "2D loops (pts + is_hole)",
            "plane (axis, height_cm, origin, basis)",
        ],
        "model_action": None,
        "branch": None,
        "fallback": None,
    },
    {
        "tool": "annotate_mesh_parameters",
        "purpose": ("Capture viewport screenshots of the mesh plus the measured "
                    "facts, giving the model the visual input needed to classify "
                    "the object and bind its parameters."),
        "inputs": ["mesh", "views", "units"],
        "outputs": [
            "text envelope (mesh, views, measured_facts, workflow)",
            "4 PNG images (one per view)",
        ],
        "model_action": ("classify the object from the views, then call "
                         "select_parameter_schema with object_class and "
                         "measured_facts"),
        "branch": None,
        "fallback": None,
    },
    {
        "tool": "select_parameter_schema",
        "purpose": ("Select a parameter schema for the classified object class "
                    "and bind the measured facts into stable named parameters "
                    "with confidence scores."),
        "inputs": ["object_class", "measured_facts", "units"],
        "outputs": [
            "parameter schema (parameters, unmatched_roles, strategy_hint)",
        ],
        "model_action": None,
        "branch": None,
        "fallback": "unknown class -> generic schema with a note",
    },
    {
        "tool": "reconstruct_mesh",
        "purpose": ("Reconstruct the mesh as native parametric CAD features: "
                    "prismatic emits a linear_extrude of the sliced profile, "
                    "revolved revolves the half cross-section, csg_decompose "
                    "fits boxes/cylinders into a union tree, and organic uses "
                    "Fusion's PREVIEW MeshConvertFeature API."),
        "inputs": ["mesh", "strategy", "units", "params"],
        "outputs": [
            "reconstruction envelope (strategy, method, bodies, features, "
            "csg_nodes)",
        ],
        "model_action": None,
        "branch": {"auto": "prismatic | revolved | csg_decompose | organic"},
        "fallback": ("organic -> mesh_convert; unsupported -> mesh_fallback "
                     "(mesh body instead of parametric features)"),
    },
    {
        "tool": "compare_mesh_to_brep",
        "purpose": ("Vision-free fidelity QA: compare the original mesh against "
                    "the reconstructed BRep body by volume ratio, bounding-box "
                    "deviation, and sampled surface deviation."),
        "inputs": ["mesh", "body"],
        "outputs": [
            "fidelity report (mesh, brep, volume_ratio, "
            "bbox_max_deviation_cm, sampled_deviation_cm, method)",
        ],
        "model_action": None,
        "branch": None,
        "fallback": None,
    },
    {
        "tool": "review_reconstruction",
        "purpose": ("Vision QA loop: capture side-by-side mesh vs reconstructed "
                    "BRep views (per requested view) plus a local geometry "
                    "summary, so the model can judge whether the reconstruction "
                    "is faithful or needs another pass."),
        "inputs": ["mesh", "body", "views"],
        "outputs": [
            "text envelope (pairs, geometry, workflow)",
            "PNG images interleaved per view (mesh then brep)",
        ],
        "model_action": ("compare each pair; if features are missing call "
                         "reconstruct_mesh again with feedback or accept with "
                         "select_parameter_schema"),
        "branch": None,
        "fallback": None,
    },
]

# Serialized form of the full guide (served by get_workflow_guide()).
GUIDE_JSON = json.dumps(GUIDE, indent=2)


def get_step(name: str) -> Optional[Dict]:
    """Return a single workflow step dict, or None when the name is unknown.

    Lookup is by tool name (or the short step name, e.g. "reconstruct" for
    reconstruct_mesh), NOT by list index.  Empty/whitespace input returns
    None so the server tool can treat it as "full guide".
    """
    if not name:
        return None
    name = str(name).strip()
    for step in GUIDE:
        if step["tool"] == name or step["tool"].split("_")[0] == name:
            return step
    return None
