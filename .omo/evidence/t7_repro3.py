"""T7 reproduction v3: the ACTUAL bug — near-plane vertex from 6-dp rounding.

FusionMCP.py L2842: nodes.extend([round(pt.x, 6), round(pt.y, 6), round(pt.z, 6)])
This means coordinates are at exact 6-dp precision. _ONPLANE_EPS_BASE = 1e-6.

When a vertex is at z=0.249999 (6-dp), its distance to plane z=0.25 is:
  0.249999 - 0.25 = -9.999999999747044e-07
  abs(d) = 9.999999999747044e-07 < 1e-6 → "on" the plane!

This triggers DOUBLE COUNTING in _triangle_plane_segment:
  1. Edge-crossing loop: edge (V_near, V_above) has di*dj < 0 → spurious crossing
  2. On-plane-corner loop: V_near is added as a genuine endpoint
  3. The function returns pts[0], pts[1] — which includes the SPURIOUS crossing
     and EXCLUDES V_near's own position (it's pts[2]).

The spurious crossing point is near V_near but not AT V_near, and differs
between adjacent triangles (different edge directions → different t values),
so the points DON'T DEDUP → chain breaks → false/missed loops.
"""
import sys, os
REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, REPO)
from mcp_server.mesh_slicer import (slice_mesh_at, _epsilons, _triangle_plane_segment,
    _chain_loops, _build_plane, _simplify_loop, _dot)

# Check the floating-point distances
for zval in [0.249999, 0.250000, 0.250001]:
    d = zval - 0.25
    print(f"  z={zval:.6f}  d={d:.20e}  abs(d)<1e-6: {abs(d) < 1e-6}")

# Build a cube with top face at z=0.249999 (near-plane vertex case)
# This simulates a thin band: bottom at z=0, top at z=0.249999
def indexed_cube(z0, z1, size=1.0):
    nodes = [
        0, 0, z0,   size, 0, z0,   size, size, z0,   0, size, z0,
        0, 0, z1,   size, 0, z1,   size, size, z1,   0, size, z1,
    ]
    indices = [
        0,2,1, 0,3,2,    4,5,6, 4,6,7,
        0,1,5, 0,5,4,    3,7,6, 3,6,2,
        0,4,3, 3,4,7,    1,2,6, 1,6,5,
    ]
    return nodes, indices

def deep_trace(nodes, indices, height, label):
    node_list = [(nodes[k],nodes[k+1],nodes[k+2]) for k in range(0,len(nodes),3)]
    tri_list = [(indices[k],indices[k+1],indices[k+2]) for k in range(0,len(indices),3)]
    plane = _build_plane("Z", height)
    de, oe = _epsilons(node_list)
    print(f"\n{'='*65}")
    print(f"DEEP TRACE: {label}")
    print(f"  dedup_eps={de:.2e}  onplane_eps={oe:.2e}")
    raw_segs = []
    for idx, (i0,i1,i2) in enumerate(tri_list):
        a,b,c = node_list[i0],node_list[i1],node_list[i2]
        seg = _triangle_plane_segment(a,b,c, plane["_normal"], plane["origin"], plane["basis"], oe)
        if seg is not None:
            raw_segs.append(seg)
            dists = [_dot((p[0]-plane["origin"][0],p[1]-plane["origin"][1],p[2]-plane["origin"][2]), plane["_normal"]) for p in (a,b,c)]
            on_flags = [abs(d)<=oe for d in dists]
            near = any(on_flags)
            print(f"  tri[{idx:2d}] ({i0},{i1},{i2}): "
                  f"({seg[0][0]:.12f},{seg[0][1]:.12f})-({seg[1][0]:.12f},{seg[1][1]:.12f})  "
                  f"d=[{dists[0]:.4e},{dists[1]:.4e},{dists[2]:.4e}] on={on_flags}"
                  f"{' ***' if near else ''}")
    print(f"  raw segments: {len(raw_segs)}")
    chains = _chain_loops(raw_segs, de)
    print(f"  chains: {len(chains)} (lengths: {[len(c) for c in chains]})")
    for ci, ch in enumerate(chains):
        simp = _simplify_loop(ch, de)
        print(f"  chain[{ci}]: raw={len(ch)}pts -> simplified={len(simp)}pts")
    res = slice_mesh_at(nodes, indices, {"axis": "Z", "height_cm": height})
    loops = res["loops"]
    print(f"  RESULT: {len(loops)} loop(s)")
    for i, lp in enumerate(loops):
        print(f"    loop[{i}]: is_hole={lp['is_hole']}, pts={len(lp['pts'])}")
        for p in lp["pts"]:
            print(f"      ({p[0]:.10f}, {p[1]:.10f})")
    if len(loops) != 1:
        print(f"  *** EXPECTED 1 LOOP, GOT {len(loops)} ***")
    elif loops[0] and len(loops[0]["pts"]) != 4:
        print(f"  *** EXPECTED 4 POINTS, GOT {len(loops[0]['pts'])} ***")
    return loops

# Test 1: top at z=0.249999 (near-plane, negative side)
print("\n##### Test 1: cube z=[0, 0.249999] slice at z=0.25 #####")
n, i = indexed_cube(0.0, 0.249999)
deep_trace(n, i, 0.25, "top=0.249999")

# Test 2: top at z=0.2500009 (near-plane, negative side, within eps)
print("\n##### Test 2: cube z=[0, 0.2500009] slice at z=0.25 #####")
n, i = indexed_cube(0.0, 0.2500009)
deep_trace(n, i, 0.25, "top=0.2500009")

# Test 3: top at z=0.2500005 (near-plane, negative side, within eps)
print("\n##### Test 3: cube z=[0, 0.2500005] slice at z=0.25 #####")
n, i = indexed_cube(0.0, 0.2500005)
deep_trace(n, i, 0.25, "top=0.2500005")

# Test 4: control — top at z=0.5 (clean mid-band, should work)
print("\n##### Test 4: cube z=[0, 0.5] slice at z=0.25 (CONTROL) #####")
n, i = indexed_cube(0.0, 0.5)
loops = deep_trace(n, i, 0.25, "top=0.5 (control)")

# Test 5: thin band with SCALE > 1 — extent=10cm, onplane_eps=1e-5
# Top face at z=0.24999 (distance ~1e-5 from plane)
print("\n##### Test 5: cube 10x10x0.25, top=0.24999, scale=10 #####")
n, i = indexed_cube(0.0, 0.24999, size=10.0)
deep_trace(n, i, 0.25, "10cm top=0.24999")

# Test 6: thin band, scale=10, top face exactly at 0.249999
print("\n##### Test 6: cube 10x10x0.25, top=0.249999 #####")
n, i = indexed_cube(0.0, 0.249999, size=10.0)
deep_trace(n, i, 0.25, "10cm top=0.249999")
