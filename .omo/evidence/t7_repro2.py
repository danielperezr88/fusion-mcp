"""T7 reproduction v2: harder cases with non-trivial coordinates and float32
perturbation that actually triggers the chaining/simplification bugs.

Key insight from v1: float32(0.0)=0.0, float32(10.0)=10.0, float32(0.5)=0.5 —
these are EXACT in float32, so no perturbation.  We need coords like 1.1, 2.3
where f32(x) != x.
"""
import sys, os, struct, math
REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, REPO)
from mcp_server.mesh_slicer import (slice_mesh_at, _epsilons, _triangle_plane_segment,
    _chain_loops, _build_plane, _simplify_loop, _dot)

def f32(v):
    return struct.unpack('f', struct.pack('f', v))[0]

def report(name, nodes, indices, height=0.25):
    res = slice_mesh_at(nodes, indices, {"axis": "Z", "height_cm": height})
    loops = res["loops"]
    node_list = [(nodes[i],nodes[i+1],nodes[i+2]) for i in range(0,len(nodes),3)]
    de, oe = _epsilons(node_list)
    print(f"\n{'='*60}")
    print(f"TEST: {name}")
    print(f"  nodes={len(node_list)}  dedup_eps={de:.2e}  onplane_eps={oe:.2e}")
    print(f"  loops: {len(loops)}")
    ok = True
    for i, lp in enumerate(loops):
        print(f"    loop[{i}]: is_hole={lp['is_hole']}, pts={len(lp['pts'])}")
        for p in lp["pts"]:
            print(f"      ({p[0]:.10f}, {p[1]:.10f})")
    if len(loops) == 0:
        print("  *** NO LOOPS — SLICE MISSED EVERYTHING ***")
        ok = False
    if len(loops) > 1:
        print(f"  *** {len(loops)} LOOPS — EXPECTED 1 (FALSE LOOPS) ***")
        ok = False
    return loops, ok

def nonindexed_cube_f32(z0, z1, x0, x1, y0, y1):
    """Non-indexed cube with float32-perturbed coordinates (non-trivial values)."""
    def C(x,y,z):
        return f32(x), f32(y), f32(z)
    tris = [
        # -z
        [(x0,y0,z0),(x1,y1,z0),(x1,y0,z0)], [(x0,y0,z0),(x0,y1,z0),(x1,y1,z0)],
        # +z
        [(x0,y0,z1),(x1,y0,z1),(x1,y1,z1)], [(x0,y0,z1),(x1,y1,z1),(x0,y1,z1)],
        # -y
        [(x0,y0,z0),(x1,y0,z0),(x1,y0,z1)], [(x0,y0,z0),(x1,y0,z1),(x0,y0,z1)],
        # +y
        [(x0,y1,z0),(x0,y1,z1),(x1,y1,z1)], [(x0,y1,z0),(x1,y1,z1),(x1,y1,z0)],
        # -x
        [(x0,y0,z0),(x0,y0,z1),(x0,y1,z1)], [(x0,y0,z0),(x0,y1,z1),(x0,y1,z0)],
        # +x
        [(x1,y0,z0),(x1,y1,z0),(x1,y1,z1)], [(x1,y0,z0),(x1,y1,z1),(x1,y0,z1)],
    ]
    nodes, indices = [], []
    for tri in tris:
        base = len(nodes)//3
        for pt in tri:
            nodes.extend(C(*pt))
        indices.extend([base, base+1, base+2])
    return nodes, indices

all_ok = True

# --- Test A: Non-trivial coords, float32 perturbation, mid-band ---
print("### Test A: non-trivial coords f32, z=[0,0.5] slice @ 0.25 ###")
n,i = nonindexed_cube_f32(0.0, 0.5, 1.1, 2.3, 0.7, 1.9)
loops, ok = report("f32-nontrivial", n, i, 0.25)
if not ok: all_ok = False
if loops:
    pts = loops[0]["pts"]
    if len(pts) != 4:
        print(f"  *** EXPECTED 4 POINTS, GOT {len(pts)} — EXTRA VERTICES ***")
        all_ok = False

# --- Test B: Near-plane top face, non-indexed f32, non-trivial coords ---
print("\n### Test B: near-plane top (z=0.2500001), f32, non-trivial ###")
n,i = nonindexed_cube_f32(0.0, 0.2500001, 1.1, 2.3, 0.7, 1.9)
loops, ok = report("nearplane-f32-nontrivial", n, i, 0.25)
if not ok: all_ok = False

# --- Test C: Deep trace for test B — check raw segment count and chain ---
print("\n### Deep trace: near-plane non-indexed f32 ###")
node_list = [(n[k],n[k+1],n[k+2]) for k in range(0,len(n),3)]
tri_list = [(i[k],i[k+1],i[k+2]) for k in range(0,len(i),3)]
plane = _build_plane("Z", 0.25)
de, oe = _epsilons(node_list)
print(f"  dedup_eps={de:.2e}  onplane_eps={oe:.2e}")
raw_segs = []
for idx, (i0,i1,i2) in enumerate(tri_list):
    a,b,c = node_list[i0],node_list[i1],node_list[i2]
    seg = _triangle_plane_segment(a,b,c, plane["_normal"], plane["origin"], plane["basis"], oe)
    if seg is not None:
        raw_segs.append(seg)
        # check distances
        dists = []
        for p in (a,b,c):
            d = _dot((p[0]-plane["origin"][0],p[1]-plane["origin"][1],p[2]-plane["origin"][2]), plane["_normal"])
            dists.append(d)
        on_flags = [abs(d)<=oe for d in dists]
        near = any(on_flags)
        print(f"  tri[{idx}]: seg ({seg[0][0]:.10f},{seg[0][1]:.10f})-({seg[1][0]:.10f},{seg[1][1]:.10f})"
              f"  dists=[{dists[0]:.2e},{dists[1]:.2e},{dists[2]:.2e}] on={on_flags} {'*** NEAR ***' if near else ''}")
print(f"\n  raw segments: {len(raw_segs)}")
chains = _chain_loops(raw_segs, de)
print(f"  chains: {len(chains)} (lengths: {[len(c) for c in chains]})")
for ci, ch in enumerate(chains):
    print(f"  chain[{ci}] raw pts:")
    for p in ch:
        print(f"    ({p[0]:.10f}, {p[1]:.10f})")
    simp = _simplify_loop(ch, de)
    print(f"  chain[{ci}] simplified pts: {len(simp)}")
    for p in simp:
        print(f"    ({p[0]:.10f}, {p[1]:.10f})")

# --- Test D: Explicit near-plane with LARGER offset — distance just under onplane_eps ---
# For a 1.2cm-extent mesh (coords ~1.1-2.3), scale = max(1.0, 1.2) = 1.2
# onplane_eps = 1e-6 * 1.2 = 1.2e-6
# Put top vertices at distance 1e-6 from plane (just under onplane_eps)
print("\n### Test D: top at z=0.25+9e-7 (just under onplane_eps), f32 non-trivial ###")
z_near = 0.25 + 9e-7
n,i = nonindexed_cube_f32(0.0, z_near, 1.1, 2.3, 0.7, 1.9)
loops, ok = report("nearplane-edge-f32", n, i, 0.25)
if not ok: all_ok = False

# Deep trace test D
node_list_D = [(n[k],n[k+1],n[k+2]) for k in range(0,len(n),3)]
tri_list_D = [(i[k],i[k+1],i[k+2]) for k in range(0,len(i),3)]
de_D, oe_D = _epsilons(node_list_D)
print(f"  dedup_eps={de_D:.2e}  onplane_eps={oe_D:.2e}")
raw_D = []
for idx, (i0,i1,i2) in enumerate(tri_list_D):
    a,b,c = node_list_D[i0],node_list_D[i1],node_list_D[i2]
    seg = _triangle_plane_segment(a,b,c, plane["_normal"], plane["origin"], plane["basis"], oe_D)
    if seg is not None:
        raw_D.append(seg)
print(f"  raw segments: {len(raw_D)}")
chains_D = _chain_loops(raw_D, de_D)
print(f"  chains: {len(chains_D)} (lengths: {[len(c) for c in chains_D]})")

# --- Test E: A mesh where near-plane creates a non-collinear spurious point ---
# Use a prism with a TRIANGULAR cross-section, not a rectangle.
# Bottom: triangle at z=0, top: triangle at z=near-plane.
print("\n### Test E: Triangular prism, near-plane top, f32 ###")
def tri_prism_nonindexed_f32(z0, z1):
    """Triangular prism, non-indexed, f32-perturbed."""
    # Triangle vertices (bottom): (0.3,0.4), (1.7,0.2), (0.9,1.8)
    bv = [(0.3,0.4), (1.7,0.2), (0.9,1.8)]
    tv = [(f32(x), f32(y), f32(z1)) for x,y in bv]
    bv = [(f32(x), f32(y), f32(z0)) for x,y in bv]
    # 3 side quads, each split into 2 triangles
    tris = []
    for i in range(3):
        j = (i+1) % 3
        # quad: bv[i], bv[j], tv[j], tv[i]
        tris.append([bv[i], bv[j], tv[j]])  # tri 1
        tris.append([bv[i], tv[j], tv[i]])  # tri 2
    # bottom cap
    tris.append([bv[0], bv[2], bv[1]])
    # top cap
    tris.append([tv[0], tv[1], tv[2]])
    nodes, indices = [], []
    for tri in tris:
        base = len(nodes)//3
        for pt in tri:
            nodes.extend(pt)
        indices.extend([base, base+1, base+2])
    return nodes, indices

for label, dz in [("dz=1e-7", 0.25+1e-7), ("dz=5e-7", 0.25+5e-7), ("dz=9e-7", 0.25+9e-7)]:
    n,i = tri_prism_nonindexed_f32(0.0, dz)
    loops, ok = report(f"triprism-{label}", n, i, 0.25)
    if not ok: all_ok = False
    # check if we get the expected 3-vertex triangle
    if loops and len(loops[0]["pts"]) != 3:
        print(f"  *** EXPECTED 3 POINTS (triangle cross-section), GOT {len(loops[0]['pts'])} ***")
        all_ok = False

print(f"\n\n{'='*60}")
print(f"ALL OK: {all_ok}")
