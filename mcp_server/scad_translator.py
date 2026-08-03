"""CSG translator: resolve OpenSCAD/BOSL2 code into a JSON-serializable CSG tree,
then translate that tree into native Autodesk Fusion 360 parametric features.

Process-split design (see .omo/notepads/fusion-mcp-enhancements/decisions.md,
entry "A11-impl: Translator process split"):

  * ``resolve_scad()`` runs in the MCP server process (system Python 3.14.2)
    which HAS ``openscad-lalr-parser`` / ``openscad-evaluator`` / ``manifold3d``
    and does NOT have ``adsk``.
  * ``translate_to_fusion_commands()`` runs inside the Fusion 360 add-in
    process (embedded Python) which HAS ``adsk`` and does NOT have the
    openscad packages.

Consequently ALL third-party imports are lazy and guarded:

  * ``openscad_lalr_parser`` / ``openscad_evaluator`` are imported only inside
    ``resolve_scad()`` (guarded by try/except ImportError -> CSG-export path).
  * ``adsk`` is imported only inside ``translate_to_fusion_commands()``.

The CSG tree crossing the HTTP bridge is plain data (dicts/lists/floats) --
no dataclasses, no numpy -- so it round-trips through JSON untouched.

This module imports cleanly with the standard library alone, in BOTH processes.
"""

from __future__ import annotations

import contextlib
import io
import math
import os
import subprocess
import tempfile
from typing import Any, Callable, Dict, List, Optional, Sequence, Tuple

# ---------------------------------------------------------------------------
# Public constants / exceptions
# ---------------------------------------------------------------------------

#: OpenSCAD is unitless; Fusion 360 works in centimeters internally.  The scale
#: factor is applied to EVERY dimensional value extracted from the CSG tree
#: (sizes, radii, heights, translations, offsets, fillet radii, ...) before a
#: Fusion API call is made.  Angles (degrees/radians) are NOT scaled.
DEFAULT_UNITS: Dict[str, float] = {
    "mm": 0.1,   # 1 OpenSCAD unit (1 mm)  -> 0.1 cm
    "cm": 1.0,   # 1 OpenSCAD unit (1 cm)  -> 1.0 cm
    "in": 2.54,  # 1 OpenSCAD unit (1 in)  -> 2.54 cm
}

_UNKNOWN_UNIT_FALLBACK = "mm"


class UnsupportedSCADNodeError(Exception):
    """Raised when the CSG tree contains a node the Fusion translator cannot
    (yet) express as native parametric features."""

    def __init__(self, kind: str, message: Optional[str] = None):
        self.kind = kind
        if message is None:
            message = f"Unsupported SCAD node kind: {kind!r}"
        super().__init__(message)


class SCADParseError(ValueError):
    """Raised when the OpenSCAD source (or the .csg produced by the fallback
    OpenSCAD binary) cannot be parsed.  The message includes the line/column
    reported by the parser."""


# ---------------------------------------------------------------------------
# Node-kind tables (single source of truth for validation + dispatch)
# ---------------------------------------------------------------------------

#: 3D primitives with dedicated handlers.
_PRIMITIVE_KINDS = frozenset({"cube", "cylinder", "sphere", "polyhedron"})

#: Transform / placement operations.  Handled generically via move / scale
#: features applied to the subtree's resulting bodies.
_TRANSFORM_KINDS = frozenset({"translate", "rotate", "scale", "mirror",
                              "resize", "multmatrix"})

#: Boolean / CSG operations.
_BOOLEAN_KINDS = frozenset({"union", "difference", "intersection",
                            "intersection_for"})

#: Transparent wrappers -- recurse children, contribute no geometry.
_PASSTHROUGH_KINDS = frozenset({"color", "render", "group"})

#: 2D -> 3D extrusion operations.  The 2D profiles under them (polygon /
#: square / circle) are drawn into a single sketch and extruded / revolved
#: as native Fusion features.
_EXTRUSION_KINDS = frozenset({"linear_extrude", "rotate_extrude"})

#: 2D primitives.  Valid only as children of ``linear_extrude`` /
#: ``rotate_extrude`` (possibly through transform / passthrough wrappers such
#: as BOSL2's ``mask2d_*`` chains) -- enforced by the validation pass.  They
#: are executed by drawing the profile(s) into the extrude's sketch.
_2D_PRIMITIVE_KINDS = frozenset({"polygon", "circle", "square"})

#: Kinds rejected during the validation pass, before any adsk call happens.
#: These have no faithful native-Fusion parametric equivalent yet.
_HARD_UNSUPPORTED = frozenset({"hull", "text", "offset", "projection",
                               "surface", "import"})

#: Kinds with dedicated (conditional) handlers.
_SPECIAL_KINDS = frozenset({"minkowski"})

_SUPPORTED_KINDS = (
    _PRIMITIVE_KINDS | _TRANSFORM_KINDS | _BOOLEAN_KINDS | _PASSTHROUGH_KINDS
    | _EXTRUSION_KINDS | _2D_PRIMITIVE_KINDS | _SPECIAL_KINDS
)


# ---------------------------------------------------------------------------
# JSON-serialization helpers (server side)
# ---------------------------------------------------------------------------

def _clean_value(value: Any) -> Any:
    """Recursively convert a raw evaluator param value into pure JSON data.

    numpy arrays / scalars become plain lists / Python scalars; everything
    else (floats, ints, bool, str, None, nested dicts/lists) passes through.
    Raises TypeError if a genuinely non-serializable object is encountered.
    """
    if isinstance(value, dict):
        return {k: _clean_value(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_clean_value(v) for v in value]
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    mod = type(value).__module__
    if mod.startswith("numpy"):
        if hasattr(value, "tolist"):
            return _clean_value(value.tolist())
        if hasattr(value, "item"):
            return _clean_value(value.item())
    raise TypeError(
        "Non-JSON-serializable CSG param value: "
        f"{type(value).__name__} {value!r}"
    )


def _is_identity_matrix(m: Any) -> bool:
    """True for a 4x4 identity matrix (within float tolerance)."""
    if not isinstance(m, list) or len(m) != 4:
        return False
    for i in range(4):
        row = m[i]
        if not isinstance(row, list) or len(row) != 4:
            return False
        for j in range(4):
            expected = 1.0 if i == j else 0.0
            if abs(float(row[j]) - expected) > 1e-9:
                return False
    return True


def _sanitize_node(node: Any) -> Optional[Dict[str, Any]]:
    """Convert one evaluator CSGNode into a plain JSON-safe dict.

    Collapses pure-noise wrappers on the way down:

      * identity ``multmatrix`` around a single child -> the child itself;
      * ``group()`` -> its child (single) or an implicit ``union`` (many),
        and empty groups vanish.

    Node shape out: ``{"kind": str, "params": dict, "children": [node, ...]}``
    with ``params`` values JSON-serializable.
    """
    kind = getattr(node, "kind", None)
    raw_params = getattr(node, "params", None) or {}
    params = {k: _clean_value(v) for k, v in raw_params.items()}

    children: List[Dict[str, Any]] = []
    for child in getattr(node, "children", None) or []:
        cleaned = _sanitize_node(child)
        if cleaned is not None:
            children.append(cleaned)

    if kind == "multmatrix":
        args = params.get("args")
        m = None
        if isinstance(args, dict):
            # The evaluator stores positional args under int keys; the .csg
            # walker uses string keys.  Accept both.
            m = args.get("0")
            if m is None and 0 in args:
                m = args[0]
        if m is not None and _is_identity_matrix(m) and len(children) == 1:
            return children[0]

    if kind == "group":
        if not children:
            return None
        if len(children) == 1:
            return children[0]
        return {"kind": "union", "params": {}, "children": children}

    return {"kind": kind, "params": params, "children": children}


# ---------------------------------------------------------------------------
# Lightweight .csg AST walker (fallback path only)
# ---------------------------------------------------------------------------

def _csg_literal(expr: Any) -> Any:
    """Evaluate a literal expression node from a parsed OpenSCAD .csg file.

    .csg output only ever contains literals (numbers, booleans, strings and
    nested vectors), so a tiny evaluator is enough -- no full evaluator needed
    for the fallback path.
    """
    from openscad_lalr_parser import nodes as _N  # lazy: fallback only

    if isinstance(expr, _N.NumberLiteral):
        return float(expr.val)
    if isinstance(expr, _N.BooleanLiteral):
        return bool(expr.val)
    if isinstance(expr, _N.StringLiteral):
        return str(expr.val)
    if isinstance(expr, _N.UndefinedLiteral):
        return None
    if isinstance(expr, _N.UnaryMinusOp):
        return -_csg_literal(expr.expr)
    if hasattr(expr, "elements") and isinstance(expr.elements, list):
        return [_csg_literal(e) for e in expr.elements]
    if isinstance(expr, _N.Identifier):
        # Identifiers only appear in .csg for things we do not model ($fn...).
        return None
    raise SCADParseError(f"Unsupported literal in .csg: {type(expr).__name__}")


def _csg_ast_to_node(node: Any) -> Optional[Dict[str, Any]]:
    """Convert one ModularCall AST node from a parsed .csg into a plain dict.

    Applies the same noise-collapsing rules as ``_sanitize_node``: identity
    multmatrix and single-child group() wrappers are unwrapped.
    """
    from openscad_lalr_parser import nodes as _N  # lazy: fallback only

    if not isinstance(node, _N.ModularCall):
        return None
    kind = node.name.name

    params: Dict[str, Any] = {}
    for idx, arg in enumerate(node.arguments):
        if isinstance(arg, _N.NamedArgument):
            params[arg.name.name] = _csg_literal(arg.expr)
        elif isinstance(arg, _N.PositionalArgument):
            params[str(idx)] = _csg_literal(arg.expr)

    children: List[Dict[str, Any]] = []
    for child in node.children:
        converted = _csg_ast_to_node(child)
        if converted is not None:
            children.append(converted)

    if kind == "multmatrix":
        m = params.get("0")
        if m is not None and _is_identity_matrix(m) and len(children) == 1:
            return children[0]

    if kind == "group":
        if not children:
            return None
        if len(children) == 1:
            return children[0]
        return {"kind": "union", "params": {}, "children": children}

    return {"kind": kind, "params": params, "children": children}


# ---------------------------------------------------------------------------
# resolve_scad(): server-side OpenSCAD/BOSL2 -> CSG tree
# ---------------------------------------------------------------------------

def _set_openscad_path(bosl2_path: Optional[str]) -> None:
    """Make ``include <BOSL2/std.scad>``-style resolution work by extending
    OPENSCADPATH with the BOSL2 directory AND its parent (includes are written
    both as ``<BOSL2/....scad>`` and as ``<....scad>`` relative to the root)."""
    if not bosl2_path:
        return
    existing = os.environ.get("OPENSCADPATH", "")
    parts = [existing] if existing else []
    root_dir = os.path.abspath(bosl2_path)
    for candidate in (root_dir, os.path.dirname(root_dir)):
        if candidate and candidate not in parts:
            parts.append(candidate)
    os.environ["OPENSCADPATH"] = os.pathsep.join(parts)


def _resolve_with_evaluator(scad_file: str,
                            bosl2_path: Optional[str]) -> List[Dict[str, Any]]:
    """Primary path: parse + evaluate with openscad-lalr-parser / evaluator.

    ``include`` statements are resolved against OPENSCADPATH (set from
    ``bosl2_path``); ``use`` statements are resolved by the evaluator itself.
    The resolved tree is read from ``evaluator.csg_tree`` after
    ``Evaluator.evaluate(nodes, root_scope)``.
    """
    # Lazy, guarded import -- this is the whole point of the process split.
    from openscad_lalr_parser import getASTfromFile
    from openscad_lalr_parser.scope import build_scopes
    from openscad_evaluator import Evaluator, EvalError

    _set_openscad_path(bosl2_path)

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        nodes = getASTfromFile(scad_file)
    if nodes is None:
        detail = buf.getvalue().strip() or "syntax error"
        raise SCADParseError(f"OpenSCAD parse error:\n{detail}")

    root_scope = build_scopes(nodes)
    evaluator = Evaluator()
    try:
        evaluator.evaluate(nodes, root_scope)
    except EvalError as exc:
        # The evaluator's error IS the truth about this code -- do not
        # silently fall back to the binary (documented in issues.md).
        raise RuntimeError(f"OpenSCAD evaluation error: {exc}") from exc

    tree = []
    for node in evaluator.csg_tree:
        cleaned = _sanitize_node(node)
        if cleaned is not None:
            tree.append(cleaned)
    return tree


def _resolve_with_openscad_binary(scad_file: str,
                                  openscad_path: Optional[str],
                                  bosl2_path: Optional[str]
                                  ) -> List[Dict[str, Any]]:
    """Fallback path: let the real OpenSCAD binary bake everything into a
    ``.csg`` file, then parse that (it is plain OpenSCAD source)."""
    if not openscad_path or not os.path.exists(openscad_path):
        raise ImportError(
            "openscad-evaluator is not installed in this Python AND no "
            "OpenSCAD binary was provided via openscad_path=; cannot resolve "
            "SCAD code."
        )
    out_csg = os.path.splitext(scad_file)[0] + ".csg"

    env = dict(os.environ)
    if bosl2_path:
        parts = [env.get("OPENSCADPATH", "")] if env.get("OPENSCADPATH") else []
        root_dir = os.path.abspath(bosl2_path)
        for candidate in (root_dir, os.path.dirname(root_dir)):
            if candidate and candidate not in parts:
                parts.append(candidate)
        env["OPENSCADPATH"] = os.pathsep.join(parts)

    try:
        proc = subprocess.run(
            [openscad_path, "-o", out_csg, scad_file],
            env=env, timeout=300, capture_output=True, text=True,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("OpenSCAD binary timed out after 300s") from exc

    if proc.returncode != 0 or not os.path.exists(out_csg):
        raise RuntimeError(
            f"OpenSCAD binary failed (exit {proc.returncode}):\n{proc.stderr}"
        )
    return _parse_csg_file(out_csg)


def _parse_csg_file(csg_file: str) -> List[Dict[str, Any]]:
    """Parse a ``.csg`` file into the plain CSG tree format.

    Prefers the full evaluator pipeline (a .csg is valid OpenSCAD source and
    goes through the same ``csg_tree`` machinery); if the evaluator is not
    installed, falls back to the lightweight AST walker above.
    """
    try:
        return _resolve_with_evaluator(csg_file, None)
    except ImportError:
        pass

    from openscad_lalr_parser import getASTfromFile

    buf = io.StringIO()
    with contextlib.redirect_stdout(buf):
        nodes = getASTfromFile(csg_file)
    if nodes is None:
        raise SCADParseError(
            "Failed to parse OpenSCAD .csg output:\n" + buf.getvalue().strip()
        )
    tree = []
    for node in nodes:
        converted = _csg_ast_to_node(node)
        if converted is not None:
            tree.append(converted)
    return tree


def resolve_scad(code: str,
                 openscad_path: Optional[str] = None,
                 bosl2_path: Optional[str] = None) -> List[Dict[str, Any]]:
    """Resolve OpenSCAD (or BOSL2) source into a JSON-serializable CSG tree.

    Args:
        code: OpenSCAD source.  ``include``/``use`` of BOSL2 libraries is
            supported when ``bosl2_path`` points at the BOSL2 directory.
        openscad_path: Path to the OpenSCAD binary (``openscad.com`` on
            Windows).  Used only by the fallback path, when the evaluator
            packages are not installed.
        bosl2_path: Directory containing the BOSL2 library (``std.scad``),
            e.g. ``...\\BOSL2-master\\BOSL2``.

    Returns:
        A list of nodes ``{"kind": str, "params": dict, "children": [...]}``.
        ``params`` are concrete floats/lists/bools -- this structure is fully
        JSON-serializable and is what crosses the HTTP bridge to Fusion.

    Raises:
        SCADParseError: the source could not be parsed (message has line info).
        RuntimeError: evaluation failed (e.g. missing module / missing file).
        ImportError: no evaluator packages AND no usable OpenSCAD binary.
    """
    code = code or ""
    with tempfile.TemporaryDirectory(prefix="scad_resolve_") as tmpdir:
        scad_file = os.path.join(tmpdir, "model.scad")
        with open(scad_file, "w", encoding="utf-8") as handle:
            handle.write(code)
        try:
            return _resolve_with_evaluator(scad_file, bosl2_path)
        except ImportError:
            # Evaluator packages missing -> OpenSCAD-binary .csg fallback.
            return _resolve_with_openscad_binary(scad_file, openscad_path,
                                                 bosl2_path)

# ---------------------------------------------------------------------------
# translate_to_fusion_commands(): Fusion-side CSG tree -> parametric features
# ---------------------------------------------------------------------------

def _import_adsk():
    try:
        import adsk.core
        import adsk.fusion
        return adsk
    except ImportError as exc:  # pragma: no cover - Fusion process only
        raise RuntimeError(
            "adsk is not available -- translate_to_fusion_commands() must run "
            "inside the Fusion 360 add-in process"
        ) from exc


# --- pure-math helpers (no adsk, unit-testable headless) --------------------

def _mat_mul_3x3(a, b):
    return [[sum(a[i][k] * b[k][j] for k in range(3)) for j in range(3)]
            for i in range(3)]


def _mat_det_3x3(a):
    return (a[0][0] * (a[1][1] * a[2][2] - a[1][2] * a[2][1])
            - a[0][1] * (a[1][0] * a[2][2] - a[1][2] * a[2][0])
            + a[0][2] * (a[1][0] * a[2][1] - a[1][1] * a[2][0]))


def _mat_inv_3x3(a):
    det = _mat_det_3x3(a)
    if abs(det) < 1e-12:
        raise ValueError("singular 3x3 matrix")
    inv_det = 1.0 / det
    cof = [
        [a[1][1] * a[2][2] - a[1][2] * a[2][1],
         a[0][2] * a[2][1] - a[0][1] * a[2][2],
         a[0][1] * a[1][2] - a[0][2] * a[1][1]],
        [a[1][2] * a[2][0] - a[1][0] * a[2][2],
         a[0][0] * a[2][2] - a[0][2] * a[2][0],
         a[0][2] * a[1][0] - a[0][0] * a[1][2]],
        [a[1][0] * a[2][1] - a[1][1] * a[2][0],
         a[0][1] * a[2][0] - a[0][0] * a[2][1],
         a[0][0] * a[1][1] - a[0][1] * a[1][0]],
    ]
    return [[cof[i][j] * inv_det for j in range(3)] for i in range(3)]


def _sym_eig_3x3(a, max_sweeps: int = 64, tol: float = 1e-12):
    """Jacobi eigendecomposition of a symmetric 3x3 matrix.

    Returns ``(eigenvalues, eigenvectors)`` where eigenvectors are the
    COLUMNS of the returned 3x3 matrix (``v[i][k]`` = i-th component of the
    k-th eigenvector).
    """
    mat = [list(row) for row in a]
    vec = [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]]
    for _ in range(max_sweeps):
        off = math.sqrt(mat[0][1] ** 2 + mat[0][2] ** 2 + mat[1][2] ** 2)
        if off < tol:
            break
        p, q = 0, 1
        if abs(mat[0][2]) > abs(mat[p][q]):
            p, q = 0, 2
        if abs(mat[1][2]) > abs(mat[p][q]):
            p, q = 1, 2
        if abs(mat[p][q]) < tol:
            break
        theta = (mat[q][q] - mat[p][p]) / (2.0 * mat[p][q])
        t = 1.0 if theta >= 0 else -1.0
        t = t / (abs(theta) + math.sqrt(theta * theta + 1.0))
        c = 1.0 / math.sqrt(t * t + 1.0)
        s = t * c
        app, aqq, apq = mat[p][p], mat[q][q], mat[p][q]
        mat[p][p] = c * c * app - 2.0 * s * c * apq + s * s * aqq
        mat[q][q] = s * s * app + 2.0 * s * c * apq + c * c * aqq
        mat[p][q] = mat[q][p] = 0.0
        for i in range(3):
            if i == p or i == q:
                continue
            aip, aiq = mat[i][p], mat[i][q]
            mat[i][p] = mat[p][i] = c * aip - s * aiq
            mat[i][q] = mat[q][i] = s * aip + c * aiq
        for i in range(3):
            vip, viq = vec[i][p], vec[i][q]
            vec[i][p] = c * vip - s * viq
            vec[i][q] = s * vip + c * viq
    return [mat[0][0], mat[1][1], mat[2][2]], vec


def _polar_decompose_3x3(a):
    """Polar-decompose a 3x3 matrix: ``a = R . S`` with R orthogonal and S
    symmetric PSD.  Returns ``(R, S, scales)`` where ``scales`` are the
    diagonal scale factors of S.

    Raises ValueError if the decomposition implies shear (S not diagonal) or
    a reflection (det(R) < 0) -- neither is expressible with Fusion's rigid
    move / uniform scale features.
    """
    # S = sqrt(A^T A) via symmetric eigendecomposition.
    ata = [[sum(a[k][i] * a[k][j] for k in range(3)) for j in range(3)]
           for i in range(3)]
    evals, evecs = _sym_eig_3x3(ata)
    sq = [math.sqrt(max(0.0, e)) for e in evals]
    # S = Q . diag(sqrt(lam)) . Q^T   (eigenvectors are columns of evecs)
    s_mat = [[sum(evecs[i][k] * sq[k] * evecs[j][k] for k in range(3))
              for j in range(3)] for i in range(3)]
    r_mat = _mat_mul_3x3(a, _mat_inv_3x3(s_mat))

    for i in range(3):
        for j in range(3):
            if i != j and abs(s_mat[i][j]) > 1e-6:
                raise ValueError(
                    "multmatrix encodes shear -- no native Fusion equivalent")
    if _mat_det_3x3(r_mat) < 0.0:
        raise ValueError(
            "multmatrix encodes a reflection -- no native Fusion equivalent")
    return r_mat, s_mat, [s_mat[0][0], s_mat[1][1], s_mat[2][2]]


def _decompose_multmatrix(m):
    """Decompose a 4x4 multmatrix into (translation, scale, rotation).

    ``M = T . R . S`` -- apply scale, then rotation, then translation to a
    body to reproduce the transform.  ``scale`` is the diagonal factor vector
    (identity -> [1,1,1]); ``rotation`` is a 3x3 matrix.
    """
    translation = [float(m[i][3]) for i in range(3)]
    linear = [[float(m[i][j]) for j in range(3)] for i in range(3)]
    r_mat, _s_mat, scales = _polar_decompose_3x3(linear)
    return translation, scales, r_mat


def _matrix_near_identity(m, tol: float = 1e-6) -> bool:
    for i in range(3):
        for j in range(3):
            expected = 1.0 if i == j else 0.0
            if abs(float(m[i][j]) - expected) > tol:
                return False
    return True


# --- pure 2D affine matrix helpers (no adsk, unit-testable headless) --------

def _mat3_identity():
    """3x3 2D affine identity matrix."""
    return [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0], [0.0, 0.0, 1.0]]


def _mat3_mul(a, b):
    """3x3 matrix product ``a . b`` -- apply ``b`` first, then ``a``."""
    return [[sum(a[i][k] * b[k][j] for k in range(3)) for j in range(3)]
            for i in range(3)]


def _mat3_apply(m, pt):
    """Apply a 3x3 2D affine matrix to an (x, y) point."""
    x, y = float(pt[0]), float(pt[1])
    return [m[0][0] * x + m[0][1] * y + m[0][2],
            m[1][0] * x + m[1][1] * y + m[1][2]]


def _mat3_is_similarity(m) -> bool:
    """True when the 2x2 linear part is a rotation + uniform scale (no shear
    / non-uniform scale) -- the only case where a circle stays a circle."""
    c0x, c0y = m[0][0], m[1][0]
    c1x, c1y = m[0][1], m[1][1]
    len0 = math.hypot(c0x, c0y)
    len1 = math.hypot(c1x, c1y)
    if len0 < 1e-12 or len1 < 1e-12:
        return False
    return (abs(c0x * c1x + c0y * c1y) < 1e-9
            and abs(len0 - len1) < 1e-9)


def _mat3_scale_factor(m) -> float:
    """Uniform scale factor of a similarity matrix."""
    return math.hypot(m[0][0], m[1][0])


# --- Fusion-side executor ----------------------------------------------------

class _FusionExecutor:
    """Executes a validated CSG tree against a live Fusion design.

    Only constructed AFTER the two-phase validation pass, so all adsk usage
    happens here, never during tree validation.
    """

    def __init__(self, root, design, scale: float):
        self.root = root
        self.design = design
        self.scale = scale
        self.adsk = _import_adsk()
        self._counter = 0
        self.created_names: List[str] = []

        self._new_body_op = self.adsk.fusion.FeatureOperations.NewBodyFeatureOperation

    # -- registry / naming ---------------------------------------------------

    def _next_name(self, kind: str) -> str:
        self._counter += 1
        return f"scad_{kind}_{self._counter}"

    def _body_by_name(self, name: str):
        for i in range(self.root.bRepBodies.count):
            body = self.root.bRepBodies.item(i)
            if body.name == name:
                return body
        raise RuntimeError(f"SCAD translator: body {name!r} not found")

    def _bodies_collection(self, names):
        col = self.adsk.core.ObjectCollection.create()
        for name in names:
            col.add(self._body_by_name(name))
        return col

    # -- sketches ------------------------------------------------------------

    def _new_sketch(self, plane, name: str):
        sketch = self.root.sketches.add(plane)
        sketch.name = name
        return sketch

    def _sketch_rectangle(self, sketch, x1, y1, x2, y2) -> None:
        sketch.sketchCurves.sketchLines.addTwoPointRectangle(
            self.adsk.core.Point3D.create(x1, y1, 0),
            self.adsk.core.Point3D.create(x2, y2, 0))

    def _sketch_circle(self, sketch, cx, cy, radius) -> None:
        sketch.sketchCurves.sketchCircles.addByCenterRadius(
            self.adsk.core.Point3D.create(cx, cy, 0), radius)

    def _sketch_polygon(self, sketch, pts) -> None:
        """Closed polygon from an ordered list of (x, y) points."""
        lines = sketch.sketchCurves.sketchLines
        for i in range(len(pts)):
            x1, y1 = pts[i]
            x2, y2 = pts[(i + 1) % len(pts)]
            lines.addByTwoPoints(
                self.adsk.core.Point3D.create(x1, y1, 0),
                self.adsk.core.Point3D.create(x2, y2, 0))

    def _profile(self, sketch):
        sketch.isComputeDeferred = False
        if sketch.profiles.count == 0:
            raise RuntimeError(
                f"SCAD translator: sketch {sketch.name!r} has no closed profile")
        return sketch.profiles.item(0)

    def _offset_plane(self, name: str, z: float):
        planes = self.root.constructionPlanes
        plane_input = planes.createInput()
        plane_input.setByOffset(
            self.root.xYConstructionPlane,
            self.adsk.core.ValueInput.createByReal(z))
        plane = planes.add(plane_input)
        plane.name = name
        return plane

    def _plane_at_z(self, name: str, z: float):
        if abs(z) < 1e-9:
            return self.root.xYConstructionPlane
        return self._offset_plane(name, z)

    # -- features ------------------------------------------------------------

    def _extrude(self, sketch, distance: float, symmetric: bool = False):
        profile = self._profile(sketch)
        extent = self.adsk.core.ValueInput.createByReal(distance)
        extrude_input = self.root.features.extrudeFeatures.createInput(
            profile, self._new_body_op)
        extrude_input.setDistanceExtent(symmetric, extent)
        return self.root.features.extrudeFeatures.add(extrude_input)

    def _revolve(self, sketch, axis_line, angle_deg: float):
        profile = self._profile(sketch)
        revolve_input = self.root.features.revolveFeatures.createInput(
            profile, axis_line, self._new_body_op)
        revolve_input.setAngleExtent(
            False,
            self.adsk.core.ValueInput.createByReal(math.radians(angle_deg)))
        return self.root.features.revolveFeatures.add(revolve_input)

    def _loft(self, sketches):
        loft_input = self.root.features.loftFeatures.createInput(
            self._new_body_op)
        for sketch in sketches:
            loft_input.loftSections.add(self._profile(sketch))
        return self.root.features.loftFeatures.add(loft_input)

    def _free_move(self, bodies, matrix) -> None:
        move_input = self.root.features.moveFeatures.createInput2(bodies)
        move_input.defineAsFreeMove(matrix)
        self.root.features.moveFeatures.add(move_input)

    def _scale_bodies(self, bodies, factors) -> None:
        origin = self.adsk.core.Point3D.create(0, 0, 0)
        sx, sy, sz = factors
        scale_input = self.root.features.scaleFeatures.createInput(
            bodies, origin, self.adsk.core.ValueInput.createByReal(sx))
        if abs(sx - sy) > 1e-9 or abs(sx - sz) > 1e-9:
            scale_input.setToNonUniform(
                self.adsk.core.ValueInput.createByReal(sx),
                self.adsk.core.ValueInput.createByReal(sy),
                self.adsk.core.ValueInput.createByReal(sz))
        self.root.features.scaleFeatures.add(scale_input)

    def _combine(self, target, tools, operation) -> None:
        combine_input = self.root.features.combineFeatures.createInput(
            target, tools)
        combine_input.operation = operation
        combine_input.isKeepToolBodies = False
        self.root.features.combineFeatures.add(combine_input)

    def _fillet_edges(self, body, radius: float, edge_filter=None) -> None:
        edges = self.adsk.core.ObjectCollection.create()
        for i in range(body.edges.count):
            edge = body.edges.item(i)
            if edge_filter is None or edge_filter(edge):
                edges.add(edge)
        if edges.count == 0:
            return
        fillet_input = self.root.features.filletFeatures.createInput()
        fillet_input.addConstantRadiusEdgeSet(
            edges, self.adsk.core.ValueInput.createByReal(radius), True)
        self.root.features.filletFeatures.add(fillet_input)

    def _get_base_feature(self):
        """Best-effort timeline base feature so mesh bodies appear in history."""
        try:
            base_features = self.root.features.baseFeatures
            if base_features.count > 0:
                return base_features.item(0)
            return base_features.add()
        except Exception:  # pragma: no cover - API differences across versions
            return None

    def _add_mesh(self, verts, tris):
        flat_verts = [float(c) for pt in verts for c in pt]
        flat_tris = [int(i) for tri in tris for i in tri]
        try:
            mesh = self.root.meshBodies.addByTriangleMeshData(
                flat_verts, flat_tris, self._get_base_feature())
        except Exception as exc:  # pragma: no cover - live Fusion only
            raise UnsupportedSCADNodeError(
                "polyhedron",
                f"mesh fallback failed ({exc}) -- loftable topology required") \
                from exc
        return mesh

    # -- primitive handlers ---------------------------------------------------

    def _eval_cube(self, node):
        params = node["params"]
        size = _vec_or_num(params, "size", default=1.0)
        w, d, h = [float(v) * self.scale for v in _as3(size)]
        center = bool(params.get("center", False))

        sketch = self._new_sketch(self.root.xYConstructionPlane,
                                  self._next_name("cube_sketch"))
        if center:
            self._sketch_rectangle(sketch, -w / 2, -d / 2, w / 2, d / 2)
        else:
            self._sketch_rectangle(sketch, 0, 0, w, d)
        feature = self._extrude(sketch, h / 2 if center else h,
                                symmetric=center)
        body = feature.bodies.item(0)
        name = self._next_name("cube")
        body.name = name
        self.created_names.append(name)
        return [name]

    def _eval_cylinder(self, node):
        params = node["params"]
        h = float(_num(params, ("h", "height"), default=1.0)) * self.scale
        # evaluator resolves d/d1/d2 to r/r1/r2; accept both shapes.
        r1 = float(_num(params, ("r1", "r"), default=1.0)) * self.scale
        r2 = float(_num(params, ("r2", "r"), default=1.0)) * self.scale
        center = bool(params.get("center", False))

        if abs(r1 - r2) < 1e-9:
            # Uniform cylinder: circle + extrude.
            sketch = self._new_sketch(self.root.xYConstructionPlane,
                                      self._next_name("cylinder_sketch"))
            self._sketch_circle(sketch, 0, 0, r1)
            feature = self._extrude(sketch, h / 2 if center else h,
                                    symmetric=center)
            body = feature.bodies.item(0)
            name = self._next_name("cylinder")
            body.name = name
            self.created_names.append(name)
            return [name]

        # Tapered cylinder (cone): loft between two circle profiles.
        z0, z1 = (-h / 2, h / 2) if center else (0.0, h)
        sketch0 = self._new_sketch(self._plane_at_z("cone_bottom_plane", z0),
                                   self._next_name("cone_bottom_sketch"))
        self._sketch_circle(sketch0, 0, 0, r1)
        sketch1 = self._new_sketch(self._plane_at_z("cone_top_plane", z1),
                                   self._next_name("cone_top_sketch"))
        self._sketch_circle(sketch1, 0, 0, r2)
        feature = self._loft([sketch0, sketch1])
        body = feature.bodies.item(0)
        name = self._next_name("cone")
        body.name = name
        self.created_names.append(name)
        return [name]

    def _eval_sphere(self, node):
        params = node["params"]
        radius = float(_num(params, ("r",), default=1.0)) * self.scale

        sketch = self._new_sketch(self.root.xYConstructionPlane,
                                  self._next_name("sphere_sketch"))
        # Axis line along Y through the origin, then a left-side semicircle
        # from (0, r) through (-r, 0) to (0, -r).  Revolving the closed
        # half-disc 360 deg around the axis line sweeps out the sphere.
        sketch.sketchCurves.sketchLines.addByTwoPoints(
            self.adsk.core.Point3D.create(0, -radius, 0),
            self.adsk.core.Point3D.create(0, radius, 0))
        sketch.sketchCurves.sketchArcs.addByCenterStartSweep(
            self.adsk.core.Point3D.create(0, 0, 0),
            self.adsk.core.Point3D.create(0, radius, 0),
            math.pi)
        axis_line = sketch.sketchCurves.sketchLines.item(0)
        feature = self._revolve(sketch, axis_line, 360.0)
        body = feature.bodies.item(0)
        name = self._next_name("sphere")
        body.name = name
        self.created_names.append(name)
        return [name]

    def _eval_polyhedron(self, node):
        params = node["params"]
        verts, tris = _polyhedron_mesh(params)
        scaled = [[float(x) * self.scale for x in pt] for pt in verts]

        name = self._next_name("polyhedron")
        if _is_loftable(scaled):
            zs = sorted({round(pt[2], 4) for pt in scaled})
            if len(zs) == 2:
                rings = {z: [pt[:2] for pt in scaled if round(pt[2], 4) == z]
                         for z in zs}
                if len(rings[zs[0]]) >= 3 and len(rings[zs[0]]) == len(rings[zs[1]]):
                    sketch0 = self._new_sketch(
                        self._plane_at_z(f"poly_bottom_plane_{name}", zs[0]),
                        f"{name}_bottom_sketch")
                    self._sketch_polygon(sketch0, rings[zs[0]])
                    sketch1 = self._new_sketch(
                        self._plane_at_z(f"poly_top_plane_{name}", zs[1]),
                        f"{name}_top_sketch")
                    self._sketch_polygon(sketch1, rings[zs[1]])
                    feature = self._loft([sketch0, sketch1])
                    body = feature.bodies.item(0)
                    body.name = name
                    self.created_names.append(name)
                    return [name]

        # Non-loftable topology: mesh fallback (appears in timeline via the
        # base feature).  Live verification deferred to the Todo 14 lane.
        mesh = self._add_mesh(scaled, tris)
        mesh.name = name
        self.created_names.append(name)
        return [name]

    # -- transform handler ----------------------------------------------------

    def _eval_transform(self, node):
        kind = node["kind"]
        child_names = self._eval_children(node["children"])
        if not child_names:
            return []
        bodies = self._bodies_collection(child_names)

        if kind == "translate":
            vec = _transform_vector(node["params"], default=[0.0, 0.0, 0.0])
            tx, ty, tz = [float(v) * self.scale for v in _as3(vec)]
            matrix = self.adsk.core.Matrix3D.create()
            matrix.translation = self.adsk.core.Vector3D.create(tx, ty, tz)
            self._free_move(bodies, matrix)

        elif kind == "rotate":
            matrix = self._rotation_matrix(self._rotate_angles(node["params"]))
            if not _matrix_near_identity(_matrix_from_adsk(matrix), tol=1e-9):
                self._free_move(bodies, matrix)

        elif kind == "scale":
            vec = _transform_vector(node["params"], default=[1.0, 1.0, 1.0])
            self._scale_bodies(bodies, [float(v) for v in _as3(vec)])

        elif kind == "mirror":
            vec = _transform_vector(node["params"], default=[1.0, 0.0, 0.0])
            self._scale_bodies(bodies, _mirror_factors(vec))

        elif kind == "resize":
            target = _transform_vector(node["params"], default=[0.0, 0.0, 0.0])
            for name in child_names:
                factors = self._resize_factors(self._body_by_name(name), target)
                if factors is not None:
                    self._scale_bodies(self._bodies_collection([name]), factors)

        elif kind == "multmatrix":
            matrix = _transform_matrix(node["params"])
            if matrix is None:
                raise UnsupportedSCADNodeError(
                    "multmatrix", "no matrix found in params")
            self._apply_multmatrix(bodies, matrix)

        return child_names

    def _rotate_angles(self, params):
        """Normalize rotate params to a ``[rx, ry, rz]`` degrees vector.

        OpenSCAD semantics: a scalar angle rotates about Z; a 1-vector is
        treated the same way; a 3-vector is ``[rx, ry, rz]``.  Numeric shape
        was already enforced by ``_validate_rotate`` before execution.
        """
        angle = _transform_vector(params, default=[0.0, 0.0, 0.0])
        if isinstance(angle, (int, float)):
            return [0.0, 0.0, float(angle)]
        vec = [float(v) for v in angle]
        if len(vec) == 1:
            return [0.0, 0.0, vec[0]]
        return (vec + [0.0, 0.0, 0.0])[:3]

    def _rotation_matrix(self, angles):
        adsk = self.adsk
        origin = adsk.core.Point3D.create(0, 0, 0)
        axes = [
            adsk.core.Vector3D.create(1, 0, 0),
            adsk.core.Vector3D.create(0, 1, 0),
            adsk.core.Vector3D.create(0, 0, 1),
        ]
        matrix = adsk.core.Matrix3D.create()  # identity
        for axis, angle in zip(axes, angles):
            rot = adsk.core.Matrix3D.create()
            rot.setToRotation(math.radians(angle), axis, origin)
            # this = rot * this  =>  final = Rz . Ry . Rx  (OpenSCAD order)
            matrix.preTransformBy(rot)
        return matrix

    def _resize_factors(self, body, target):
        """Per-axis factor so the body bbox matches the OpenSCAD resize target
        (0 in the target keeps the dimension unchanged)."""
        bbox = body.boundingBox
        lo, hi = bbox.minPoint, bbox.maxPoint
        lo_arr, hi_arr = lo.asArray(), hi.asArray()
        factors = []
        for i in range(3):
            span = hi_arr[i] - lo_arr[i]
            want = float(target[i]) * self.scale if i < len(target) else 0.0
            if want <= 1e-9 or span <= 1e-12:
                factors.append(1.0)
            else:
                factors.append(want / span)
        return factors

    def _apply_multmatrix(self, bodies, matrix) -> None:
        translation, scales, rotation = _decompose_multmatrix(matrix)
        if not all(abs(s - 1.0) < 1e-6 for s in scales):
            self._scale_bodies(bodies, scales)
        if not _matrix_near_identity(rotation):
            rot_matrix = self.adsk.core.Matrix3D.create()
            rot_matrix.setWithArray([
                rotation[0][0], rotation[0][1], rotation[0][2], 0.0,
                rotation[1][0], rotation[1][1], rotation[1][2], 0.0,
                rotation[2][0], rotation[2][1], rotation[2][2], 0.0,
                0.0, 0.0, 0.0, 1.0,
            ])
            self._free_move(bodies, rot_matrix)
        if any(abs(t) > 1e-9 for t in translation):
            tx, ty, tz = [t * self.scale for t in translation]
            translate_matrix = self.adsk.core.Matrix3D.create()
            translate_matrix.translation = self.adsk.core.Vector3D.create(
                tx, ty, tz)
            self._free_move(bodies, translate_matrix)

    # -- boolean handlers -----------------------------------------------------

    def _eval_boolean(self, node):
        kind = node["kind"]
        child_names = self._eval_children(node["children"])
        if len(child_names) <= 1:
            return child_names

        target_name = child_names[0]
        tool_names = child_names[1:]
        target = self._body_by_name(target_name)
        tools = self._bodies_collection(tool_names)

        operation_map = {
            "union": self.adsk.fusion.BooleanTypes.JoinBooleanType,
            "difference": self.adsk.fusion.BooleanTypes.CutBooleanType,
            "intersection": self.adsk.fusion.BooleanTypes.IntersectBooleanType,
            "intersection_for":
                self.adsk.fusion.BooleanTypes.IntersectBooleanType,
        }
        self._combine(target, tools, operation_map[kind])
        # Tool bodies are consumed (isKeepToolBodies=False); the target keeps
        # its name.  Consumed names stay in created_names so Todo 14 cleanup
        # can tolerate them (missing bodies are simply skipped).
        return [target_name]

    # -- passthrough / extrusion / minkowski ---------------------------------

    def _eval_passthrough(self, node):
        # color()/render()/group(): recurse children.  Applying a color from
        # the evaluator's rgba to a Fusion appearance is deliberately not done
        # here (appearance library mapping is a later lane); the evaluator
        # already bakes `color` into child params, so nothing is lost.
        return self._eval_children(node["children"])

    def _eval_linear_extrude(self, node):
        children = node["children"]
        if not children:
            return []  # empty ghost extrude (BOSL2 diff() artifacts)
        params = node["params"]
        twist = float(_num(params, ("twist",), default=0.0))
        if abs(twist) > 1e-9:
            raise UnsupportedSCADNodeError(
                "linear_extrude", "twist is not supported yet")
        scale_top = params.get("scale_top")
        if (isinstance(scale_top, list) and len(scale_top) == 2
                and (abs(float(scale_top[0]) - 1.0) > 1e-9
                     or abs(float(scale_top[1]) - 1.0) > 1e-9)):
            raise UnsupportedSCADNodeError(
                "linear_extrude", "tapering scale is not supported yet")
        height = float(_num(params, ("height", "h"), default=100.0)) * self.scale
        center = bool(params.get("center", False))
        sketch = self._new_sketch(self.root.xYConstructionPlane,
                                  self._next_name("linear_extrude_sketch"))
        profiles = self._draw_2d_children(children, sketch)
        if not profiles:
            self._discard_sketch(sketch)
            return []
        feature = self._extrude_profiles(
            profiles, height / 2 if center else height, symmetric=center)
        return self._name_bodies(feature, "linear_extrude")

    def _eval_rotate_extrude(self, node):
        children = node["children"]
        if not children:
            return []
        params = node["params"]
        angle = float(_num(params, ("angle",), default=360.0))
        sketch = self._new_sketch(self.root.xZConstructionPlane,
                                  self._next_name("rotate_extrude_sketch"))
        # Revolve axis = the world Z axis, which lies along the sketch y-axis
        # of the XZ construction plane: the line (0,0,0) -> (0,0,1).
        axis_line = sketch.sketchCurves.sketchLines.addByTwoPoints(
            self.adsk.core.Point3D.create(0, 0, 0),
            self.adsk.core.Point3D.create(0, 0, 1))
        profiles = self._draw_2d_children(children, sketch)
        if not profiles:
            self._discard_sketch(sketch)
            return []
        profiles = self._profiles_avoiding_axis(profiles, axis_line)
        feature = self._revolve_profiles(profiles, axis_line, angle)
        return self._name_bodies(feature, "rotate_extrude")

    def _discard_sketch(self, sketch) -> None:
        """Best-effort cleanup of an empty ghost sketch (no bodies created)."""
        try:
            sketch.deleteMe()
        except Exception:  # pragma: no cover - cosmetic, API differences
            pass

    def _draw_2d_children(self, children, sketch):
        """Draw every 2D child of an extrude node into ONE sketch.

        Returns the list of closed Profile objects to extrude/revolve.
        """
        sketch.isComputeDeferred = False
        profiles = []
        for child in children:
            profiles.extend(self._draw_2d_node(child, sketch, None))
        return profiles

    def _draw_2d_node(self, node, sketch, matrix):
        """Draw a 2D subtree (primitive / transform chain / passthrough).

        Transforms compose a 2D affine matrix applied to the drawn geometry
        (OpenSCAD order: the innermost transform applies first).  Returns the
        closed Profile list this subtree contributes.
        """
        kind = node["kind"]
        if kind in _TRANSFORM_KINDS:
            matrix = self._compose_2d_transform(matrix, node)
            profiles = []
            for child in node["children"]:
                profiles.extend(self._draw_2d_node(child, sketch, matrix))
            return profiles
        if kind in _PASSTHROUGH_KINDS:
            profiles = []
            for child in node["children"]:
                profiles.extend(self._draw_2d_node(child, sketch, matrix))
            return profiles
        if kind in _2D_PRIMITIVE_KINDS:
            return self._eval_2d_primitive(node, sketch, matrix)
        raise UnsupportedSCADNodeError(
            kind, f"{kind} is not supported inside a 2D extrude")

    def _compose_2d_transform(self, matrix, node):
        """Compose ``node``'s 2D transform onto ``matrix`` (None = identity).

        Returns ``matrix . node_matrix`` so the accumulated (outer) transform
        applies after the new (inner) one, matching OpenSCAD semantics where
        the transform closest to the primitive applies first.
        """
        kind = node["kind"]
        params = node["params"]
        if matrix is None:
            matrix = _mat3_identity()
        m = _mat3_identity()
        if kind == "translate":
            vec = _transform_vector(params, default=[0.0, 0.0, 0.0])
            tx, ty = _as3(vec)[0], _as3(vec)[1]
            m[0][2] = float(tx) * self.scale
            m[1][2] = float(ty) * self.scale
        elif kind == "rotate":
            angle = _transform_vector(params, default=[0.0, 0.0, 0.0])
            if isinstance(angle, (int, float)):
                a = float(angle)
            else:
                vec = [float(v) for v in angle]
                # 2D rotate: scalar / 1-vector rotate about Z; a 3-vector
                # contributes only its Z (out-of-plane) component.
                a = vec[0] if len(vec) == 1 else vec[2]
            rad = math.radians(a)
            c, s = math.cos(rad), math.sin(rad)
            m = [[c, -s, 0.0], [s, c, 0.0], [0.0, 0.0, 1.0]]
        elif kind == "scale":
            vec = _transform_vector(params, default=[1.0, 1.0, 1.0])
            sx, sy = _as3(vec)[0], _as3(vec)[1]
            m = [[float(sx), 0.0, 0.0], [0.0, float(sy), 0.0],
                 [0.0, 0.0, 1.0]]
        elif kind == "mirror":
            vec = _transform_vector(params, default=[1.0, 0.0, 0.0])
            vx, vy = _as3(vec)[0], _as3(vec)[1]
            norm2 = float(vx) ** 2 + float(vy) ** 2
            if norm2 < 1e-12:
                raise UnsupportedSCADNodeError(
                    "mirror", "zero mirror vector inside a 2D extrude")
            # Reflection across the line through the origin perpendicular to
            # the mirror vector: p' = p - 2 (p.v)/|v|^2 v.
            m = [[1.0 - 2.0 * float(vx) * float(vx) / norm2,
                  -2.0 * float(vx) * float(vy) / norm2, 0.0],
                 [-2.0 * float(vx) * float(vy) / norm2,
                  1.0 - 2.0 * float(vy) * float(vy) / norm2, 0.0],
                 [0.0, 0.0, 1.0]]
        elif kind == "multmatrix":
            mm = _transform_matrix(params)
            if mm is None:
                raise UnsupportedSCADNodeError(
                    "multmatrix", "no matrix found in params")
            # Keep the 2D affine part of the 4x4 matrix (x/y linear + x/y
            # translation); z rows/columns only matter to 3D geometry.
            m = [[float(mm[0][0]), float(mm[0][1]),
                  float(mm[0][3]) * self.scale],
                 [float(mm[1][0]), float(mm[1][1]),
                  float(mm[1][3]) * self.scale],
                 [0.0, 0.0, 1.0]]
        elif kind == "resize":
            raise UnsupportedSCADNodeError(
                "resize", "resize inside a 2D extrude is not supported yet")
        else:
            raise UnsupportedSCADNodeError(
                kind, f"{kind} inside a 2D extrude is not supported yet")
        return _mat3_mul(matrix, m)

    def _eval_2d_primitive(self, node, sketch, matrix=None):
        """Draw one 2D primitive into ``sketch``, transformed by ``matrix``.

        Returns the Profile objects this primitive contributes.  A polygon
        with explicit inner paths (holes) contributes only its ring profile(s);
        the inner disc profiles would fill the holes and are dropped.
        """
        kind = node["kind"]
        params = node["params"]
        before = sketch.profiles.count
        has_holes = False

        if kind == "circle":
            radius = self._circle_radius(params)
            if matrix is not None and not _mat3_is_similarity(matrix):
                pts = [[radius * math.cos(2 * math.pi * i / 64),
                        radius * math.sin(2 * math.pi * i / 64)]
                       for i in range(64)]
                self._sketch_polygon(
                    sketch, [_mat3_apply(matrix, p) for p in pts])
            else:
                cx, cy, r = 0.0, 0.0, radius
                if matrix is not None:
                    cx, cy = _mat3_apply(matrix, (0.0, 0.0))
                    r = radius * _mat3_scale_factor(matrix)
                self._sketch_circle(sketch, cx, cy, r)
        elif kind == "square":
            size = _vec_or_num(params, "size", default=1.0)
            if isinstance(size, (int, float)):
                w = h = float(size)
            else:
                w, h = float(size[0]), float(size[1])
            w *= self.scale
            h *= self.scale
            if bool(params.get("center", False)):
                corners = [(-w / 2, -h / 2), (w / 2, -h / 2),
                           (w / 2, h / 2), (-w / 2, h / 2)]
            else:
                corners = [(0.0, 0.0), (w, 0.0), (w, h), (0.0, h)]
            if matrix is not None:
                corners = [_mat3_apply(matrix, c) for c in corners]
            self._sketch_polygon(sketch, corners)
        elif kind == "polygon":
            points = params.get("pts")
            if points is None:
                points = params.get("points")  # .csg named-argument shape
            if not isinstance(points, list) or len(points) < 3:
                raise UnsupportedSCADNodeError(
                    "polygon", "polygon requires points (list of [x,y])")
            scaled = [[float(x) * self.scale, float(y) * self.scale]
                      for x, y in points]
            paths = params.get("paths")
            has_holes = bool(paths) and len(paths) > 1
            if not paths:
                paths = [list(range(len(points)))]
            for path in paths:
                loop = [scaled[int(i)] for i in path]
                if matrix is not None:
                    loop = [_mat3_apply(matrix, p) for p in loop]
                self._sketch_polygon(sketch, loop)
        else:
            raise UnsupportedSCADNodeError(kind)

        sketch.isComputeDeferred = False
        new_profiles = [sketch.profiles.item(i)
                        for i in range(before, sketch.profiles.count)]
        if has_holes:
            # Outer + inner loops bound ONE ring profile (2+ loops); the inner
            # discs are separate 1-loop profiles that would fill the holes.
            rings = [p for p in new_profiles if p.profileLoops.count >= 2]
            if rings:
                return rings
        return new_profiles

    def _circle_radius(self, params):
        """circle() radius in cm (r, d/2, or radius), applying self.scale."""
        r = params.get("r")
        if r is not None:
            return float(r) * self.scale
        d = params.get("d")
        if d is not None:
            return float(d) / 2.0 * self.scale
        return float(_num(params, ("radius",), default=1.0)) * self.scale

    def _extrude_profiles(self, profiles, distance, symmetric=False):
        collection = self.adsk.core.ObjectCollection.create()
        for profile in profiles:
            collection.add(profile)
        extent = self.adsk.core.ValueInput.createByReal(distance)
        extrude_input = self.root.features.extrudeFeatures.createInput(
            collection, self._new_body_op)
        extrude_input.setDistanceExtent(symmetric, extent)
        return self.root.features.extrudeFeatures.add(extrude_input)

    def _revolve_profiles(self, profiles, axis_line, angle_deg):
        collection = self.adsk.core.ObjectCollection.create()
        for profile in profiles:
            collection.add(profile)
        revolve_input = self.root.features.revolveFeatures.createInput(
            collection, axis_line, self._new_body_op)
        revolve_input.setAngleExtent(
            False,
            self.adsk.core.ValueInput.createByReal(math.radians(angle_deg)))
        return self.root.features.revolveFeatures.add(revolve_input)

    def _name_bodies(self, feature, kind):
        names = []
        for i in range(feature.bodies.count):
            body = feature.bodies.item(i)
            name = self._next_name(kind)
            body.name = name
            self.created_names.append(name)
            names.append(name)
        return names

    def _profile_touches_line(self, profile, line) -> bool:
        """True when ``line`` is a boundary curve of ``profile`` (i.e. the
        revolve axis splits the profile into pieces)."""
        for li in range(profile.profileLoops.count):
            loop = profile.profileLoops.item(li)
            for ci in range(loop.profileCurves.count):
                if loop.profileCurves.item(ci).sketchEntity == line:
                    return True
        return False

    def _profiles_avoiding_axis(self, profiles, axis_line):
        """Drop duplicate pieces of shapes split by the revolve axis.

        When the axis line crosses a profile, Fusion reports one profile per
        piece; revolving any one piece reproduces the full solid, so only the
        first axis-split piece is kept (the rest would overlap it).  Profiles
        that do not touch the axis are kept unchanged.
        """
        kept = []
        for profile in profiles:
            if self._profile_touches_line(profile, axis_line):
                if any(self._profile_touches_line(p, axis_line) for p in kept):
                    continue
            kept.append(profile)
        return kept

    def _eval_minkowski(self, node):
        children = node["children"]
        kinds = {c.get("kind") for c in children}
        if len(children) == 2 and kinds == {"cube", "sphere"}:
            cube_node = next(c for c in children if c["kind"] == "cube")
            sphere_node = next(c for c in children if c["kind"] == "sphere")
            radius = float(_num(sphere_node["params"], ("r",), default=1.0))
            radius *= self.scale
            [box_name] = self._eval(cube_node)
            box = self._body_by_name(box_name)
            # cube + sphere => rounded box: fillet every edge with the sphere r.
            self._fillet_edges(box, radius)
            return [box_name]

        if len(children) == 2 and kinds == {"cylinder", "sphere"}:
            cylinder_node = next(
                c for c in children if c["kind"] == "cylinder")
            sphere_node = next(c for c in children if c["kind"] == "sphere")
            radius = float(_num(sphere_node["params"], ("r",), default=1.0))
            radius *= self.scale
            cyl_r = float(_num(cylinder_node["params"], ("r1", "r"),
                               default=1.0)) * self.scale
            [cyl_name] = self._eval(cylinder_node)
            cyl = self._body_by_name(cyl_name)
            # cylinder + sphere => capsule: fillet the two circular end edges.
            circumference = 2.0 * math.pi * cyl_r
            self._fillet_edges(
                cyl, radius,
                edge_filter=lambda e: abs(e.length - circumference) < 1e-3)
            return [cyl_name]

        raise UnsupportedSCADNodeError(
            "minkowski",
            "only cube+sphere and cylinder+sphere minkowski sums are "
            "supported (children: " + ", ".join(sorted(kinds)) + ")")

    # -- dispatch -------------------------------------------------------------

    def _eval_children(self, nodes):
        names: List[str] = []
        for child in nodes:
            names.extend(self._eval(child))
        return names

    def _eval(self, node):
        kind = node.get("kind")
        if kind in _PRIMITIVE_KINDS:
            handler = {
                "cube": self._eval_cube,
                "cylinder": self._eval_cylinder,
                "sphere": self._eval_sphere,
                "polyhedron": self._eval_polyhedron,
            }[kind]
            return handler(node)
        if kind in _TRANSFORM_KINDS:
            return self._eval_transform(node)
        if kind in _BOOLEAN_KINDS:
            return self._eval_boolean(node)
        if kind in _PASSTHROUGH_KINDS:
            return self._eval_passthrough(node)
        if kind == "linear_extrude":
            return self._eval_linear_extrude(node)
        if kind == "rotate_extrude":
            return self._eval_rotate_extrude(node)
        if kind in _2D_PRIMITIVE_KINDS:
            # Unreachable after validation: 2D primitives are only valid under
            # linear_extrude / rotate_extrude, which draw them into a sketch
            # directly and never route through the generic dispatch.
            raise UnsupportedSCADNodeError(
                kind,
                "2D primitive requires linear_extrude or rotate_extrude "
                "parent.")
        if kind == "minkowski":
            return self._eval_minkowski(node)
        # Validation should already have rejected everything else.
        raise UnsupportedSCADNodeError(kind)


def _matrix_from_adsk(matrix):
    """Read the 3x3 rotation portion of an adsk Matrix3D as plain lists."""
    arr = matrix.asArray()  # 16 entries, row-major
    return [[arr[i * 4 + j] for j in range(3)] for i in range(3)]


# --- param extraction helpers (shape-agnostic across evaluator / .csg) -------

def _num(params, keys, default: float) -> float:
    for key in keys:
        value = params.get(key)
        if value is not None:
            return float(value)
    return default


def _vec_or_num(params, key: str, default: float):
    """cube()'s size may be a float (uniform) or a 3-vector."""
    value = params.get(key, default)
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, (list, tuple)):
        return [float(v) for v in value]
    return default


def _as3(value):
    """Pad/trim any scalar or vector to 3 components."""
    if isinstance(value, (int, float)):
        return [float(value), float(value), float(value)]
    out = [float(v) for v in value]
    while len(out) < 3:
        out.append(0.0)
    return out[:3]


def _transform_vector(params, default):
    """Transforms carry ``args: {"0": vec}`` (evaluator) or ``"0": vec``
    (.csg walker).  Older OpenSCAD .csg files emit named arguments instead:
    ``translate(v=...)``, ``scale(v=...)``, ``mirror(v=...)``, ``rotate(a=...)``
    -- those named keys are accepted too."""
    args = params.get("args")
    if isinstance(args, dict):
        if "0" in args:
            return args["0"]
        if 0 in args:
            return args[0]
    if "0" in params:
        return params["0"]
    if "v" in params:
        return params["v"]
    if "a" in params:
        return params["a"]
    return list(default)


def _transform_matrix(params):
    args = params.get("args")
    if isinstance(args, dict):
        if "0" in args:
            return args["0"]
        if 0 in args:
            return args[0]
    if "0" in params:
        return params["0"]
    if "m" in params:  # .csg named-argument shape: multmatrix(m = [...])
        return params["m"]
    return None


def _mirror_factors(vec):
    """mirror(v) is a reflection through the plane normal to v through the
    origin: axis-aligned v becomes a -1 scale on that axis.  Non-axis-aligned
    mirrors are rejected (no faithful Fusion feature without a reflection)."""
    v = [float(x) for x in _as3(vec)]
    nonzero = [i for i in range(3) if abs(v[i]) > 1e-9]
    if len(nonzero) != 1:
        raise UnsupportedSCADNodeError(
            "mirror",
            f"non-axis-aligned mirror vector {v} is not supported yet")
    factors = [1.0, 1.0, 1.0]
    factors[nonzero[0]] = -1.0
    return factors


def _polyhedron_mesh(params):
    """Normalize polyhedron params to (verts, tris).

    Evaluator shape: ``verts`` (N,3) + ``tri_arr`` (M,3).
    .csg shape: ``points`` + ``faces`` (fan-triangulated here).
    """
    verts = params.get("verts")
    if verts is None:
        verts = params.get("points")
    if verts is None or not isinstance(verts, list):
        raise UnsupportedSCADNodeError(
            "polyhedron", "no verts/points in polyhedron params")

    tris = params.get("tri_arr")
    if tris is None:
        tris = params.get("faces")
    if tris is None or not isinstance(tris, list):
        raise UnsupportedSCADNodeError(
            "polyhedron", "no tri_arr/faces in polyhedron params")

    verts = [[float(c) for c in pt] for pt in verts]
    if params.get("tri_arr") is not None:
        tris = [[int(i) for i in tri] for tri in tris]
    else:
        # faces -> triangle fan per face
        tris_out = []
        for face in tris:
            face = [int(i) for i in face]
            if len(face) < 3:
                continue
            for k in range(1, len(face) - 1):
                tris_out.append([face[0], face[k], face[k + 1]])
        tris = tris_out
    return verts, tris


def _is_loftable(verts) -> bool:
    """Two z-rings of equal vertex count => a prism loftable between two
    profile sketches.  Anything else falls back to the mesh path."""
    z_groups: Dict[float, int] = {}
    for pt in verts:
        z = round(float(pt[2]), 4)
        z_groups[z] = z_groups.get(z, 0) + 1
    if len(z_groups) != 2:
        return False
    counts = list(z_groups.values())
    return counts[0] == counts[1] and counts[0] >= 3


# --- two-phase validation ----------------------------------------------------

def _rotate_angle_value(params):
    """Raw rotate angle value from params (scalar or vector), or None when the
    angle argument is absent.  Handles evaluator (``args`` int/str keys) and
    .csg (``"0"`` / named ``a``) shapes."""
    args = params.get("args")
    if isinstance(args, dict):
        if "0" in args:
            return args["0"]
        if 0 in args:
            return args[0]
    if "0" in params:
        return params["0"]
    return params.get("a")


def _validate_rotate(node) -> None:
    """Validate a rotate node's angle shape before execution.

    OpenSCAD accepts a scalar (rotate about Z) or a 3-vector ``[rx, ry, rz]``.
    Anything else -- non-numeric values, wrong-length vectors -- raises a clear
    error instead of crashing later with a bare ``ValueError``.
    """
    angle = _rotate_angle_value(node.get("params", {}))
    if angle is None:
        return  # rotate() with no angle -> identity [0,0,0]
    if isinstance(angle, (int, float)):
        return
    if isinstance(angle, (list, tuple)):
        if len(angle) not in (1, 3):
            raise UnsupportedSCADNodeError(
                "rotate",
                f"rotate angle vector must have 1 or 3 components, "
                f"got {len(angle)}: {angle!r}")
        for value in angle:
            if not isinstance(value, (int, float)):
                raise UnsupportedSCADNodeError(
                    "rotate",
                    f"rotate angle values must be numbers, got {angle!r}")
        return
    raise UnsupportedSCADNodeError(
        "rotate",
        f"rotate angle must be a number or a 3-vector, got "
        f"{type(angle).__name__}: {angle!r}")


def _validate_tree(nodes, allow_2d: bool = False) -> None:
    """Phase 1: recursive validation of the ENTIRE tree.  Pure -- never touches
    adsk or root.  Unsupported kinds raise here, before any Fusion API call,
    which is what makes the negative-path tests runnable headless.

    ``allow_2d`` tracks whether a 2D primitive (polygon/circle/square) may
    appear at the current depth: True only inside ``linear_extrude`` /
    ``rotate_extrude`` sub-trees.  Transforms and passthrough wrappers
    (color/render/group) keep the flag -- a 2D profile may be transformed
    before it is extruded (e.g. BOSL2's ``mask2d_*`` chains).  Any 2D
    primitive anywhere else is a structural error.
    """
    for node in nodes:
        kind = node.get("kind")
        if kind in _HARD_UNSUPPORTED:
            _raise_hard_unsupported(kind)
        if kind not in _SUPPORTED_KINDS:
            raise UnsupportedSCADNodeError(kind)
        if kind == "rotate":
            _validate_rotate(node)
        children = node.get("children", [])
        if kind in _EXTRUSION_KINDS:
            _validate_tree(children, allow_2d=True)
        elif kind in _TRANSFORM_KINDS or kind in _PASSTHROUGH_KINDS:
            _validate_tree(children, allow_2d=allow_2d)
        else:
            if kind in _2D_PRIMITIVE_KINDS and not allow_2d:
                raise UnsupportedSCADNodeError(
                    kind,
                    "2D primitive requires linear_extrude or rotate_extrude "
                    "parent.")
            _validate_tree(children, allow_2d=False)


def _raise_hard_unsupported(kind: str) -> None:
    messages = {
        "hull": ("hull has no faithful native Fusion equivalent yet; "
                 "decompose it into explicit geometry"),
        "text": ("text glyph extrusion is not supported yet "
                 "(evaluator decomposes text into glyph paths)"),
        "offset": "offset is a 2D operation -- handled in the 2D pass (Todo 15)",
        "projection": ("projection is a 2D operation -- handled in the "
                       "2D pass (Todo 15)"),
        "surface": "surface mesh import is not supported",
        "import": "import() of external meshes is not supported",
    }
    raise UnsupportedSCADNodeError(kind, messages.get(kind))


def translate_to_fusion_commands(csg_nodes,
                                 root,
                                 design,
                                 units: str = "mm") -> List[str]:
    """Walk a CSG tree and emit native Fusion parametric features.

    TWO PHASES (required for headless testability + Todo 14 cleanup):

      1. ``_validate_tree`` recursively validates the whole tree FIRST,
         raising ``UnsupportedSCADNodeError`` for unsupported node kinds
         before ANY adsk / root access.  Negative-path tests therefore run
         headless against a stub root that must never be touched.
      2. Only then is ``adsk`` imported and the tree executed against the
         live design.

    Args:
        csg_nodes: JSON-serializable CSG tree from ``resolve_scad()``.
        root: Fusion root component (``design.rootComponent``).
        design: Fusion design (``app.activeProduct``).
        units: one of ``DEFAULT_UNITS`` keys ("mm", "cm", "in"); the scale
            factor is applied to all dimensional params.

    Returns:
        ``created_body_names``: names of every body this translation created,
        in creation order -- used by Todo 14's cleanup-on-fallback.
    """
    scale = DEFAULT_UNITS.get(units, DEFAULT_UNITS[_UNKNOWN_UNIT_FALLBACK])
    _validate_tree(csg_nodes or [])
    executor = _FusionExecutor(root, design, scale)
    executor._eval_children(csg_nodes or [])
    return executor.created_names

