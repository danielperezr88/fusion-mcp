"""T7: verify the _chain_loops first-hop-drop bug.

_chain_loops walks a loop: start -> nxt -> ... -> start.
But it only appends reps[start] and reps[nxt2] — the first hop vertex (nxt)
is NEVER appended.  When midpoints exist (normal meshes), the dropped vertex
is a collinear midpoint that gets simplified away.  When NO midpoints exist
(thin-band near-plane case), a REAL corner is dropped.
"""
import sys, os
REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, REPO)
from mcp_server.mesh_slicer import _chain_loops, _simplify_loop

# 4 segments forming a rectangle: (0,0)-(10,0)-(10,10)-(0,10)-(0,0)
# NO midpoints (what the thin-band case produces)
segs_no_midpoints = [
    ((10.0, 0.0), (0.0, 0.0)),    # bottom edge
    ((0.0, 10.0), (10.0, 10.0)),  # top edge
    ((0.0, 0.0), (0.0, 10.0)),    # left edge
    ((10.0, 10.0), (10.0, 0.0)),  # right edge
]
dedup_eps = 1e-8  # scale=10
chains = _chain_loops(segs_no_midpoints, dedup_eps)
print(f"NO MIDPOINTS case: {len(chains)} chain(s), lengths={[len(c) for c in chains]}")
for ch in chains:
    simp = _simplify_loop(ch, dedup_eps)
    print(f"  raw: {len(ch)} pts -> simplified: {len(simp)} pts")
    print(f"  raw pts: {[(round(p[0],4), round(p[1],4)) for p in ch]}")
    print(f"  sim pts: {[(round(p[0],4), round(p[1],4)) for p in simp]}")
    if len(simp) != 4:
        print(f"  *** BUG: expected 4 rectangle corners, got {len(simp)} ***")

# Same rectangle WITH midpoints (what a normal mid-band slice produces)
segs_with_midpoints = [
    ((10.0, 0.0), (5.0, 0.0)),     # bottom-right half
    ((5.0, 0.0), (0.0, 0.0)),      # bottom-left half
    ((0.0, 10.0), (5.0, 10.0)),    # top-left half
    ((5.0, 10.0), (10.0, 10.0)),   # top-right half
    ((0.0, 0.0), (0.0, 5.0)),      # left-bottom half
    ((0.0, 5.0), (0.0, 10.0)),     # left-top half
    ((10.0, 10.0), (10.0, 5.0)),   # right-top half
    ((10.0, 5.0), (10.0, 0.0)),    # right-bottom half
]
chains2 = _chain_loops(segs_with_midpoints, dedup_eps)
print(f"\nWITH MIDPOINTS case: {len(chains2)} chain(s), lengths={[len(c) for c in chains2]}")
for ch in chains2:
    simp = _simplify_loop(ch, dedup_eps)
    print(f"  raw: {len(ch)} pts -> simplified: {len(simp)} pts")
    print(f"  sim pts: {[(round(p[0],4), round(p[1],4)) for p in simp]}")

# Triangle: 3 segments, no midpoints
segs_triangle = [
    ((0.0, 0.0), (1.0, 0.0)),
    ((1.0, 0.0), (0.5, 1.0)),
    ((0.5, 1.0), (0.0, 0.0)),
]
chains3 = _chain_loops(segs_triangle, 1e-9)
print(f"\nTRIANGLE (no midpoints): {len(chains3)} chain(s), lengths={[len(c) for c in chains3]}")
if len(chains3) == 0:
    print("  *** BUG: triangle dropped entirely (first-hop vertex missing -> len < 3) ***")
for ch in chains3:
    simp = _simplify_loop(ch, 1e-9)
    print(f"  raw: {len(ch)} pts -> simplified: {len(simp)} pts")
