"""Light check: render the model the way the gallery's "Lights" mode shows it (only the model's own
lights + emission, near-black surroundings) and measure exposure, so bad light values are caught at
build time instead of in the viewer. Writes a preview PNG the agent can look at."""
import os

import bpy
import numpy as np
from mathutils import Vector


def _set_world(scene, rgb):
    if not scene.world:
        scene.world = bpy.data.worlds.new("World")
    w = scene.world
    bg = w.node_tree.nodes.get("Background") if w.node_tree else None
    if bg:
        bg.inputs["Color"].default_value = (*rgb, 1)
        bg.inputs["Strength"].default_value = 1
    w.color = rgb


def _has_emission(meshes):
    for o in meshes:
        for slot in o.material_slots:
            m = slot.material
            if not (m and m.node_tree):
                continue
            for n in m.node_tree.nodes:
                if n.type == "BSDF_PRINCIPLED":
                    s = n.inputs["Emission Strength"].default_value
                    c = n.inputs["Emission Color"].default_value
                    if s > 0 and max(c[:3]) > 0:
                        return True
                elif n.type == "EMISSION" and n.inputs["Strength"].default_value > 0:
                    return True
    return False


def _enclosing_mesh(pos, radius):
    """Name of a mesh the point sits inside, else None. Tested per object (so intersecting parts
    don't confuse it): inside if rays in all 6 directions hit that object's back faces."""
    dg = bpy.context.evaluated_depsgraph_get()
    dirs = (Vector((1, 0, 0)), Vector((-1, 0, 0)), Vector((0, 1, 0)), Vector((0, -1, 0)),
            Vector((0, 0, 1)), Vector((0, 0, -1)))
    for obj in bpy.context.scene.objects:
        if obj.type != "MESH" or obj.hide_render:
            continue
        pts = [obj.matrix_world @ Vector(c) for c in obj.bound_box]
        if not all(min(p[i] for p in pts) <= pos[i] <= max(p[i] for p in pts) for i in range(3)):
            continue
        ev = obj.evaluated_get(dg)
        inv = ev.matrix_world.inverted()
        local = inv @ pos
        rot = inv.to_3x3()
        inside = True
        for d in dirs:
            ld = (rot @ d).normalized()
            hit, loc, normal, _ = ev.ray_cast(local + ld * 1e-5, ld)
            if not hit or normal.dot(ld) <= 0:
                inside = False
                break
        if inside:
            return obj.name
    return None


def check(center, radius, preview_path):
    """Returns a list of problem strings (empty = fine). Assumes scene.camera is set and that
    no helper/thumbnail lights have been added yet."""
    scene = bpy.context.scene
    lights = [o for o in scene.objects if o.type == "LIGHT" and not o.hide_render]
    meshes = [o for o in scene.objects if o.type == "MESH" and not o.hide_render]
    if not lights and not _has_emission(meshes):
        return []

    problems = []
    for o in lights:
        L = o.data
        if L.type in ("POINT", "SPOT"):
            inside = _enclosing_mesh(o.matrix_world.translation, radius)
            if inside:
                problems.append(f"INSIDE MESH — light '{o.name}' is inside '{inside}': Blender blocks it (dark) "
                                "while the web viewer may let it leak through (too bright). Move it out of the "
                                "mesh (e.g. just below a lamp shade, in front of a bulb), or make the enclosing "
                                "part open/thin.")
            if L.shadow_soft_size > radius * 0.05:
                L.shadow_soft_size = min(L.shadow_soft_size, radius * 0.02)  # big soft radii poke into nearby geometry
        if L.type == "SUN":
            e = L.energy
            print(f"light: '{o.name}' SUN strength {L.energy:g} → {e:.2f} W/m² on the model")
        elif L.type in ("POINT", "SPOT"):
            dist = max((o.matrix_world.translation - center).length, radius * 0.25)
            e = L.energy / (4 * 3.14159 * dist * dist)
            print(f"light: '{o.name}' {L.type} {L.energy:g} W, ~{dist:.2f} m from model centre → ~{e:.3f} W/m² there")
        else:
            print(f"light: '{o.name}' {L.type} — NOT exported to glTF (use POINT, SPOT or SUN)")

    prev = (scene.render.engine, scene.render.film_transparent, scene.render.resolution_x,
            scene.render.resolution_y, scene.render.filepath, scene.render.image_settings.file_format,
            scene.view_settings.view_transform)
    world_prev = tuple(scene.world.color) if scene.world else None
    _set_world(scene, (0.012, 0.012, 0.012))   # ~ the viewer's dim environment in Lights mode
    scene.render.engine = "CYCLES"
    scene.cycles.device = "CPU"
    scene.cycles.samples = 24
    scene.cycles.use_denoising = True
    scene.render.film_transparent = True
    scene.render.resolution_x = scene.render.resolution_y = 256
    exr = preview_path + ".exr"
    scene.render.image_settings.file_format = "OPEN_EXR"
    scene.render.filepath = exr
    bpy.ops.render.render(write_still=True)

    img = bpy.data.images.load(exr)
    px = np.array(img.pixels[:], dtype=np.float32).reshape(-1, 4)
    bpy.data.images.remove(img)
    # preview PNG (Standard view transform ≈ what the viewer shows)
    scene.render.image_settings.file_format = "PNG"
    scene.view_settings.view_transform = "Standard"
    scene.render.filepath = preview_path
    bpy.ops.render.render(write_still=True)
    os.remove(exr)

    (scene.render.engine, scene.render.film_transparent, scene.render.resolution_x,
     scene.render.resolution_y, scene.render.filepath, scene.render.image_settings.file_format,
     scene.view_settings.view_transform) = prev
    if world_prev:
        _set_world(scene, world_prev[:3])

    cover = px[:, 3] > 0.5
    if cover.sum() < 50:
        return problems
    rgb = px[cover, :3]
    lum = rgb @ np.array([0.2126, 0.7152, 0.0722], dtype=np.float32)
    blown = float((rgb.max(axis=1) > 1.0).mean())
    median, p90 = float(np.median(lum)), float(np.percentile(lum, 90))
    print(f"lights view: {blown:.0%} of the model blown out, median brightness {median:.3f}, "
          f"90th percentile {p90:.3f} (preview: {preview_path})")
    if blown > 0.12 or median > 0.7:
        problems.append(f"TOO BRIGHT — {blown:.0%} of the visible model is blown out (median {median:.2f}). "
                        "Lower the light energies (typical: sun 1-4, lamp 10-60 W, spot 30-200 W; scale "
                        "point/spot energy with distance²) or emission strength.")
    elif lights and p90 < 0.02:
        problems.append(f"TOO DIM — the model's own lights barely show (90th percentile {p90:.3f}). "
                        "Raise the energies or move the lights closer to what they should light.")
    return problems
