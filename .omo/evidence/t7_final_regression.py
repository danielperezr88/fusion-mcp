"""T7 final regression: z=0/z=0.5 mesh sliced at z=0.25."""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
# Actually we need the repo root, which is 2 dirs up from .omo/evidence/
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
from mcp_server.mesh_slicer import slice_mesh_at

# Indexed cube z=[0, 0.5], slice at z=0.25
nodes = [0,0,0,  1,0,0,  1,1,0,  0,1,0,
         0,0,0.5, 1,0,0.5, 1,1,0.5, 0,1,0.5]
indices = [0,2,1,0,3,2, 4,5,6,4,6,7,
           0,1,5,0,5,4, 3,7,6,3,6,2,
           0,4,3,3,4,7, 1,2,6,1,6,5]
res = slice_mesh_at(nodes, indices, {"axis": "Z", "height_cm": 0.25})
loops = res["loops"]
print(f"loops={len(loops)}")
for i, lp in enumerate(loops):
    print(f"  loop[{i}]: is_hole={lp['is_hole']}, pts={len(lp['pts'])}")
    print(f"    {lp['pts']}")

ok = (len(loops) == 1 and not loops[0]["is_hole"] and len(loops[0]["pts"]) == 4)
expected = [[0.0, 0.0], [1.0, 0.0], [1.0, 1.0], [0.0, 1.0]]
print(f"expected: {expected}")
print(f"REGRESSION z=0/0.5 @ 0.25: {'PASS' if ok else 'FAIL'}")

# Also test the thin-band case (the actual bug)
nodes2 = [0,0,0,  10,0,0,  10,10,0,  0,10,0,
          0,0,0.249999, 10,0,0.249999, 10,10,0.249999, 0,10,0.249999]
res2 = slice_mesh_at(nodes2, indices, {"axis": "Z", "height_cm": 0.25})
loops2 = res2["loops"]
print(f"\nThin-band 10cm cube z=[0, 0.249999] @ z=0.25:")
print(f"  loops={len(loops2)}")
for i, lp in enumerate(loops2):
    print(f"  loop[{i}]: pts={len(lp['pts'])} {lp['pts']}")
ok2 = (len(loops2) == 1 and len(loops2[0]["pts"]) == 4)
print(f"REGRESSION thin-band near-plane: {'PASS' if ok2 else 'FAIL'}")
