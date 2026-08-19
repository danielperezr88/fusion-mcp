"""T7 reproduction script: test slice_mesh_at on thin-band and near-plane cases.

Tests 4 scenarios:
  1. Indexed cube z=[0,0.5] slice at z=0.25 (baseline)
  2. NON-INDEXED cube z=[0,0.5] slice at z=0.25 (Fusion displayMesh style)
  3. NON-INDEXED cube with a near-plane vertex (d within onplane_eps)
  4. NON-INDEXED cube with float32-perturbed coincident vertices
"""
import sys, os, struct, math
REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, REPO)
from mcp_server.mesh_slicer import slice_mesh_at, _epsilons, _triangle_plane_segment, _chain_loops, _build_plane, _project

def f32(v):
    """Round-trip a float through IEEE 754 single precision (float32)."""
    return struct.unpack('f', struct.pack('f', v))[0]

def cube_indexed(z0=0.0, z1=0.5, size=1.0):
    """8 shared vertices, 12 triangles — the indexed case."""
    nodes = [
        0, 0, z0,   size, 0, z0,   size, size, z0,   0, size, z0,
        0, 0, z1,   size, 0, z1,   size, size, z1,   0, size, z1,
    ]
    indices = [
        0,2,1, 0,3,2,    # -z
        4,5,6, 4,6,7,    # +z
        0,1,5, 0,5,4,    # -y
        3,7,6, 3,6,2,    # +y
        0,4,3, 3,4,7,    # -x
        1,2,6, 1,6,5,    # +x
    ]
    return nodes, indices

def cube_nonindexed(z0=0.0, z1=0.5, size=1.0, perturb_f32=False):
    """Every triangle has its own 3 vertices — the NON-INDEXED case.

    If perturb_f32=True, each vertex coordinate is round-tripped through
    float32 independently, so 'coincident' vertices from different triangles
    get slightly different values (simulating Fusion displayMesh float32 storage).
    """
    def corner(x, y, z):
        if perturb_f32:
            return f32(x), f32(y), f32(z)
        return float(x), float(y), float(z)

    # 12 triangles, each with 3 distinct vertices = 36 vertices
    tris_3d = [
        # -z face (z=z0)
        [(0,0,z0),(size,size,z0),(size,0,z0)],
        [(0,0,z0),(0,size,z0),(size,size,z0)],
        # +z face (z=z1)
        [(0,0,z1),(size,0,z1),(size,size,z1)],
        [(0,0,z1),(size,size,z1),(0,size,z1)],
        # -y face (y=0)
        [(0,0,z0),(size,0,z0),(size,0,z1)],
        [(0,0,z0),(size,0,z1),(0,0,z1)],
        # +y face (y=size)
        [(0,size,z0),(0,size,z1),(size,size,z1)],
        [(0,size,z0),(size,size,z1),(size,size,z0)],
        # -x face (x=0)
        [(0,0,z0),(0,0,z1),(0,size,z1)],
        [(0,0,z0),(0,size,z1),(0,size,z0)],
        # +x face (x=size)
        [(size,0,z0),(size,size,z0),(size,size,z1)],
        [(size,0,z0),(size,size,z1),(size,0,z1)],
    ]
    nodes = []
    indices = []
    for tri in tris_3d:
        base = len(nodes) // 3
        for (x, y, z) in tri:
            cx, cy, cz = corner(x, y, z)
            nodes.extend([cx, cy, cz])
        indices.extend([base, base+1, base+2])
    return nodes, indices

def report(name, nodes, indices, height=0.25):
    res = slice_mesh_at(nodes, indices, {"axis": "Z", "height_cm": height})
    loops = res["loops"]
    print(f"\n{'='*60}")
    print(f"TEST: {name}")
    de, oe = _epsilons([(nodes[i],nodes[i+1],nodes[i+2]) for i in range(0,len(nodes),3)])
    print(f"  dedup_eps={de:.2e}  onplane_eps={oe:.2e}")
    print(f"  loops: {len(loops)}")
    for i, lp in enumerate(loops):
        print(f"    loop[{i}]: is_hole={lp['is_hole']}, pts={len(lp['pts'])}")
        for p in lp["pts"]:
            print(f"      ({p[0]:.10f}, {p[1]:.10f})")
    if len(loops) == 0:
        print("  *** NO LOOPS — SLICE MISSED EVERYTHING ***")
    return loops

print("### Scenario 1: Indexed cube z=[0,0.5] slice at z=0.25 ###")
n, i = cube_indexed()
report("indexed-z0.25", n, i, 0.25)

print("\n### Scenario 2: Non-indexed cube z=[0,0.5] slice at z=0.25 ###")
n, i = cube_nonindexed()
report("nonindexed-z0.25", n, i, 0.25)

print("\n### Scenario 3: Non-indexed cube z=[0,0.5] slice at z=0.25, float32 perturbation ###")
n, i = cube_nonindexed(perturb_f32=True)
report("nonindexed-f32-z0.25", n, i, 0.25)

print("\n### Scenario 4: Near-plane vertex — cube z=[0, 0.2500001] slice at z=0.25 ###")
# Top face almost exactly AT the slice plane
n, i = cube_nonindexed(z0=0.0, z1=0.2500001)
report("nearplane-z0.25", n, i, 0.25)

print("\n### Scenario 5: Near-plane vertex — indexed ###")
n, i = cube_indexed(z0=0.0, z1=0.2500001)
report("nearplane-indexed-z0.25", n, i, 0.25)

print("\n### Scenario 6: Thin band, large XY extent (10x10x0.5) non-indexed f32 ###")
n, i = cube_nonindexed(z0=0.0, z1=0.5, size=10.0, perturb_f32=True)
report("thinband-10cm-f32-z0.25", n, i, 0.25)

print("\n### Scenario 7: Thin band, large XY extent (10x10x0.5) non-indexed EXACT ###")
n, i = cube_nonindexed(z0=0.0, z1=0.5, size=10.0, perturb_f32=False)
report("thinband-10cm-exact-z0.25", n, i, 0.25)

# Deep trace for the near-plane case
print("\n\n### DEEP TRACE: near-plane vertex segment generation ###")
n, i = cube_indexed(z0=0.0, z1=0.2500001)
node_list = [(n[k],n[k+1],n[k+2]) for k in range(0,len(n),3)]
tri_list = [(i[k],i[k+1],i[k+2]) for k in range(0,len(i),3)]
plane = _build_plane("Z", 0.25)
de, oe = _epsilons(node_list)
print(f"dedup_eps={de:.2e}  onplane_eps={oe:.2e}")
raw_segs = []
for idx, (i0,i1,i2) in enumerate(tri_list):
    a, b, c = node_list[i0], node_list[i1], node_list[i2]
    seg = _triangle_plane_segment(a, b, c, plane["_normal"], plane["origin"], plane["basis"], oe)
    if seg is not None:
        raw_segs.append(seg)
        print(f"  tri[{idx}] ({i0},{i1},{i2}): seg=({seg[0][0]:.10f},{seg[0][1]:.10f})-({seg[1][0]:.10f},{seg[1][1]:.10f})")
        # show distances
        from mcp_server.mesh_slicer import _dot
        for label, p in [("a",a),("b",b),("c",c)]:
            d = _dot((p[0]-plane["origin"][0],p[1]-plane["origin"][1],p[2]-plane["origin"][2]), plane["_normal"])
            on = abs(d) <= oe
            print(f"    {label}: d={d:.2e} on={on}")
print(f"\n  total raw segments: {len(raw_segs)}")
chains = _chain_loops(raw_segs, de)
print(f"  chains: {len(chains)}")
for ch in chains:
    print(f"    chain len={len(ch)}: {ch}")
