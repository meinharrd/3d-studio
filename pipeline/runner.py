"""Blender-side runner: blender -b -P runner.py -- <model_script.py> <out_dir> <name>
Clears the scene, runs the model script, exports GLB and renders a thumbnail."""
import bpy, sys, os, math, runpy
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))  # so model scripts can `import studio`
from mathutils import Vector

argv = sys.argv[sys.argv.index("--") + 1:]
script, out_dir, name = argv

bpy.ops.wm.read_factory_settings(use_empty=True)
runpy.run_path(script, run_name="__main__")

import zcheck  # noqa: E402 — report coplanar overlaps before export (build.sh surfaces them)
zcheck.report()

bpy.ops.export_scene.gltf(filepath=os.path.join(out_dir, name + ".glb"),
                          export_format="GLB", export_apply=True,
                          export_cameras=False, export_lights=True,
                          export_image_format="WEBP", export_image_quality=85)

# Thumbnail: frame all meshes with a camera + lights (not exported)
scene = bpy.context.scene
meshes = [o for o in scene.objects if o.type == "MESH"]
pts = [o.matrix_world @ Vector(c) for o in meshes for c in o.bound_box]
lo = Vector((min(p.x for p in pts), min(p.y for p in pts), min(p.z for p in pts)))
hi = Vector((max(p.x for p in pts), max(p.y for p in pts), max(p.z for p in pts)))
center, radius = (lo + hi) / 2, max((hi - lo).length / 2, 1e-3)

cam = bpy.data.objects.new("ThumbCam", bpy.data.cameras.new("ThumbCam"))
scene.collection.objects.link(cam)
d = Vector((1, -1.2, 0.8)).normalized()
cam.location = center + d * radius * 3.0
cam.rotation_euler = (center - cam.location).to_track_quat("-Z", "Y").to_euler()
cam.data.lens = 50
scene.camera = cam
for loc, e in [((1, -1, 2), 3.0), ((-2, -1, 1), 1.2), ((0, 2, 1), 1.5)]:
    L = bpy.data.objects.new("L", bpy.data.lights.new("L", "SUN"))
    L.data.energy = e
    L.rotation_euler = (center - (center + Vector(loc))).to_track_quat("-Z", "Y").to_euler()
    scene.collection.objects.link(L)
if not scene.world:
    scene.world = bpy.data.worlds.new("W")
scene.world.color = (0.05, 0.05, 0.06)

scene.render.engine = "CYCLES"
scene.cycles.device = "CPU"
scene.cycles.samples = 64
scene.cycles.use_denoising = True
scene.render.film_transparent = True
scene.render.resolution_x = scene.render.resolution_y = 512
scene.render.filepath = os.path.join(out_dir, name + ".png")
bpy.ops.render.render(write_still=True)
