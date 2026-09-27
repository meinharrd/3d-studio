"""Helpers for model scripts (import studio). Works offline: fetch assets first with ./assets.

Animation: keyframe object location / rotation / scale (or armatures); it exports as looping glTF
clips over scene.frame_start..frame_end at scene.render.fps. Use animation() + loop_keys() for
seamless loops. Name a light (object or light data) or a material with "flicker" to get automatic
brightness flicker in the web viewer. Animated brightness, colour and emission values don't export,
and export_apply=True means shape-key animation doesn't either."""
from pathlib import Path

import bpy
from mathutils import Vector

ROOT = Path(__file__).resolve().parent.parent / "content"
ASSETS = ROOT / "library"


def _need(path: Path, hint: str) -> Path:
    if not path.exists():
        raise FileNotFoundError(f"{path} missing — run: {hint}")
    return path


def material(name, rgb, roughness=0.6, metallic=0.0):
    """Plain Principled BSDF material."""
    m = bpy.data.materials.new(name)
    b = m.node_tree.nodes["Principled BSDF"]
    b.inputs["Base Color"].default_value = (*rgb, 1)
    b.inputs["Roughness"].default_value = roughness
    b.inputs["Metallic"].default_value = metallic
    return m


def pbr_material(aid, res="1k", scale=1.0, name=None):
    """Poly Haven texture set as a glTF-friendly material. `scale` = texture repeats per UV unit.
    Objects need UVs (primitives have them; for custom meshes use box_uv(obj))."""
    d = ASSETS / "polyhaven" / "textures" / aid / res
    hint = f"./assets texture {aid} {res}"
    m = bpy.data.materials.new(name or aid)
    nt = m.node_tree
    bsdf = nt.nodes["Principled BSDF"]
    uv = nt.nodes.new("ShaderNodeTexCoord")
    mapping = nt.nodes.new("ShaderNodeMapping")
    mapping.inputs["Scale"].default_value = (scale, scale, 1)
    nt.links.new(uv.outputs["UV"], mapping.inputs["Vector"])

    def tex(fname, color=True):
        n = nt.nodes.new("ShaderNodeTexImage")
        n.image = bpy.data.images.load(str(_need(d / fname, hint)), check_existing=True)
        if not color:
            n.image.colorspace_settings.name = "Non-Color"
        nt.links.new(mapping.outputs["Vector"], n.inputs["Vector"])
        return n

    nt.links.new(tex("diff.jpg").outputs["Color"], bsdf.inputs["Base Color"])
    if (d / "arm.jpg").exists():  # R=AO, G=roughness, B=metallic — glTF's ORM layout
        sep = nt.nodes.new("ShaderNodeSeparateColor")
        nt.links.new(tex("arm.jpg", False).outputs["Color"], sep.inputs["Color"])
        nt.links.new(sep.outputs["Green"], bsdf.inputs["Roughness"])
        nt.links.new(sep.outputs["Blue"], bsdf.inputs["Metallic"])
    if (d / "nor_gl.jpg").exists():
        nm = nt.nodes.new("ShaderNodeNormalMap")
        nt.links.new(tex("nor_gl.jpg", False).outputs["Color"], nm.inputs["Color"])
        nt.links.new(nm.outputs["Normal"], bsdf.inputs["Normal"])
    return m


def box_uv(obj, size=1.0):
    """Cube-project UVs so tiling textures work on arbitrary meshes (world-scale, `size` m per tile)."""
    bpy.context.view_layer.objects.active = obj
    for o in bpy.context.selected_objects:
        o.select_set(False)
    obj.select_set(True)
    bpy.ops.object.mode_set(mode="EDIT")
    bpy.ops.mesh.select_all(action="SELECT")
    bpy.ops.uv.cube_project(cube_size=size, scale_to_bounds=False)
    bpy.ops.object.mode_set(mode="OBJECT")


def import_glb(path, size=None, location=(0, 0, 0), max_faces=None, name=None):
    """Import a glTF/GLB, parent everything under one empty, optionally scale so the largest
    dimension == size, rest it on z=0 at `location`, and decimate meshes above max_faces.
    Returns the root empty."""
    before = set(bpy.data.objects)
    bpy.ops.import_scene.gltf(filepath=str(path))
    new = [o for o in bpy.data.objects if o not in before]
    root = bpy.data.objects.new(name or Path(path).stem, None)
    bpy.context.scene.collection.objects.link(root)
    for o in new:
        if o.parent is None:
            o.parent = root
    meshes = [o for o in new if o.type == "MESH"]
    if max_faces:
        for o in meshes:
            n = len(o.data.polygons)
            if n > max_faces:
                mod = o.modifiers.new("Decimate", "DECIMATE")
                mod.ratio = max_faces / n
    bpy.context.view_layer.update()
    lo, hi = _bounds(meshes)
    s = size / max((hi - lo)) if size else 1.0
    root.scale = (s, s, s)
    center = (lo + hi) / 2
    root.location = Vector(location) - Vector((center.x, center.y, lo.z)) * s
    bpy.context.view_layer.update()
    return root


def _bounds(meshes):
    pts = [o.matrix_world @ Vector(c) for o in meshes for c in o.bound_box]
    lo = Vector((min(p.x for p in pts), min(p.y for p in pts), min(p.z for p in pts)))
    hi = Vector((max(p.x for p in pts), max(p.y for p in pts), max(p.z for p in pts)))
    return lo, hi


def polyhaven_model(aid, res="1k", **kw):
    p = _need(ASSETS / "polyhaven" / "models" / aid / res / f"{aid}.gltf", f"./assets model {aid} {res}")
    return import_glb(p, name=aid, **kw)


def generated_model(slug, size=2.0, max_faces=60000, **kw):
    """AI-generated mesh from ./assets generate <slug> ... (dimensions are arbitrary, so size is used)."""
    p = _need(ASSETS / "generated" / slug / "mesh.glb", f"./assets generate {slug} \"<prompt>\"")
    return import_glb(p, size=size, max_faces=max_faces, name=slug, **kw)


def animation(frames=48, fps=24):
    """Set the clip length (frames at fps). Every loop period used with loop_keys must divide it."""
    sc = bpy.context.scene
    sc.frame_start, sc.frame_end = 1, frames
    sc.render.fps = fps
    return frames


def loop_keys(obj, path, values, period=None, offset=0, interpolation="BEZIER"):
    """Seamlessly loop `obj.<path>` ("location", "rotation_euler", "scale" or e.g. "location.z")
    through `values` (each a scalar or a 3-tuple) over `period` frames, starting `offset` frames in
    (use different offsets/periods so repeated parts don't move in sync). The first value is repeated
    at the end, and the curve repeats (Cycles modifier) — exported by sampling the scene range, so
    `period` must divide the scene length (see animation())."""
    sc = bpy.context.scene
    length = sc.frame_end - sc.frame_start + 1
    period = period or length
    if length % period:
        raise ValueError(f"loop period {period} doesn't divide the scene length {length}: the loop would jump")
    prop, _, comp = path.partition(".")
    idx = "xyz".index(comp) if comp else -1
    vals = list(values) + [values[0]]
    step = period / (len(vals) - 1)
    for i, v in enumerate(vals):
        f = sc.frame_start + offset + i * step
        if idx >= 0:
            getattr(obj, prop)[idx] = v
        else:
            setattr(obj, prop, v)
        obj.keyframe_insert(prop, index=idx, frame=f)
    act = obj.animation_data.action
    curves = act.fcurves if hasattr(act, "fcurves") else [c for l in act.layers for s in l.strips
                                                           for cb in s.channelbags for c in cb.fcurves]
    for fc in curves:
        if fc.data_path != prop or (idx >= 0 and fc.array_index != idx):
            continue
        for kp in fc.keyframe_points:
            kp.interpolation = interpolation
        if not any(m.type == "CYCLES" for m in fc.modifiers):
            fc.modifiers.new("CYCLES")
    return obj
