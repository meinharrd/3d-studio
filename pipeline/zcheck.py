"""Z-fighting check: find overlapping coplanar faces in the evaluated scene (world space).

Faces whose planes are closer than `tol` and whose projections overlap by a real area will
flicker in a depth-buffered viewer. Materials export double-sided, so opposite-facing pairs count
too (e.g. the bottom of a box resting exactly on a floor)."""
import bpy
from collections import defaultdict
from mathutils import Vector


def _tris(depsgraph):
    for obj in bpy.context.scene.objects:
        if obj.type != "MESH" or obj.hide_render:
            continue
        ev = obj.evaluated_get(depsgraph)
        me = ev.to_mesh()
        me.calc_loop_triangles()
        mw = ev.matrix_world
        verts = [mw @ v.co for v in me.vertices]
        for t in me.loop_triangles:
            a, b, c = (verts[i] for i in t.vertices)
            n = (b - a).cross(c - a)
            area2 = n.length
            if area2 < 1e-12:
                continue
            yield obj.name, (a, b, c), n / area2, area2 / 2
        ev.to_mesh_clear()


def _clip(poly, a, b):
    """Sutherland–Hodgman: keep the part of poly left of edge a->b (2D tuples)."""
    out = []
    def side(p): return (b[0] - a[0]) * (p[1] - a[1]) - (b[1] - a[1]) * (p[0] - a[0])
    for i, p in enumerate(poly):
        q = poly[i - 1]
        sp, sq = side(p), side(q)
        if sp >= 0:
            if sq < 0:
                t = sq / (sq - sp); out.append((q[0] + t * (p[0] - q[0]), q[1] + t * (p[1] - q[1])))
            out.append(p)
        elif sq >= 0:
            t = sq / (sq - sp); out.append((q[0] + t * (p[0] - q[0]), q[1] + t * (p[1] - q[1])))
    return out


def _area(poly):
    return abs(sum(poly[i - 1][0] * p[1] - p[0] * poly[i - 1][1] for i, p in enumerate(poly))) / 2


def _ccw(t):
    return t if (t[1][0] - t[0][0]) * (t[2][1] - t[0][1]) - (t[1][1] - t[0][1]) * (t[2][0] - t[0][0]) >= 0 else t[::-1]


def check(tol=None, min_area=None):
    """Return a list of (obj_a, obj_b, overlap_area, point) for z-fighting face pairs."""
    dg = bpy.context.evaluated_depsgraph_get()
    tris = list(_tris(dg))
    if not tris:
        return []
    pts = [p for _, t, _, _ in tris for p in t]
    lo = Vector((min(p.x for p in pts), min(p.y for p in pts), min(p.z for p in pts)))
    hi = Vector((max(p.x for p in pts), max(p.y for p in pts), max(p.z for p in pts)))
    size = max((hi - lo).length, 1e-3)
    tol = tol or size * 5e-4          # ~ depth precision of a 24-bit buffer at typical viewing distance
    min_area = min_area or (size * 2e-3) ** 2

    # bucket by canonical normal (sign-free, ~1.5 degree cells)
    groups = defaultdict(list)
    for name, t, n, area in tris:
        flip = (n.x, n.y, n.z) < (0, 0, 0)
        cn = -n if flip else n
        key = tuple(round(c * 40) for c in cn)
        groups[key].append((cn.dot(t[0]), name, t, cn, flip, n.z))

    hits = {}
    for items in groups.values():
        if len(items) < 2:
            continue
        items.sort(key=lambda x: x[0])
        n = items[0][3]
        ax = max(range(3), key=lambda i: abs(n[i]))       # project onto the dominant plane
        u, v = [i for i in range(3) if i != ax]
        j0 = 0
        for i, (d, name, t, _, flip, nz) in enumerate(items):
            while items[j0][0] < d - tol:
                j0 += 1
            t2 = _ccw([(p[u], p[v]) for p in t])
            bb = (min(p[0] for p in t2), max(p[0] for p in t2), min(p[1] for p in t2), max(p[1] for p in t2))
            for j in range(j0, i):
                d2, name2, s, _, flip2, nz2 = items[j]
                same = flip == flip2
                # both facing down at the lowest level: never visible (viewer stays above ground)
                if same and nz < -0.9 and abs(min(p.z for p in t) - lo.z) < tol:
                    continue
                s2 = [(p[u], p[v]) for p in s]
                if (max(p[0] for p in s2) <= bb[0] or min(p[0] for p in s2) >= bb[1] or
                        max(p[1] for p in s2) <= bb[2] or min(p[1] for p in s2) >= bb[3]):
                    continue
                poly = s2
                for k in range(3):
                    poly = _clip(poly, t2[k], t2[(k + 1) % 3])
                    if not poly:
                        break
                if poly and (a := _area(poly)) > min_area:
                    key = (*sorted((name, name2)), same)
                    c = sum(t, Vector()) / 3
                    prev = hits.get(key)
                    hits[key] = (key[0], key[1], (prev[2] if prev else 0) + a, prev[3] if prev else c, same)
    return sorted(hits.values(), key=lambda h: (not h[4], -h[2]))


def report():
    """Print same-facing overlaps as Z-FIGHTING (visible flicker) and opposite-facing ones as
    contacts (a face resting on another; usually hidden, only fix if it can be seen)."""
    hits = check()
    fight = [h for h in hits if h[4]]
    touch = [h for h in hits if not h[4]]
    for a, b, area, c, _ in fight[:25]:
        who = f"'{a}' with itself (duplicate/overlapping faces)" if a == b else f"'{a}' and '{b}'"
        print(f"Z-FIGHTING: {who}: {area:.4f} m² of coplanar same-facing overlap near "
              f"({c.x:.3f}, {c.y:.3f}, {c.z:.3f})")
    if len(fight) > 25:
        print(f"Z-FIGHTING: ... and {len(fight) - 25} more pairs")
    for a, b, area, c, _ in touch[:10]:
        print(f"contact: '{a}' rests flush on '{b}' ({area:.4f} m² near {c.x:.2f}, {c.y:.2f}, {c.z:.2f})")
    if len(touch) > 10:
        print(f"contact: ... and {len(touch) - 10} more")
    return fight
