"""Asset CLI for model scripts (run via ./assets). Downloads into library/ so builds stay offline.

  ./assets search <textures|models> <words...>   find Poly Haven assets (CC0)
  ./assets texture <id> [1k|2k]                  download a PBR texture set
  ./assets model <id> [1k|2k]                    download a glTF model
  ./assets generate <slug> "<prompt>"            text -> image (FLUX) -> textured mesh (TRELLIS)
  ./assets generate <slug> --image <path>        image -> textured mesh
       [--engine trellis|hunyuan]                hunyuan = shape only (no texture), faster
"""
import json
import os
import shutil
import sys
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent / "content"
ASSETS = ROOT / "library"
UA = {"User-Agent": "hel1-3d-studio/1.0 (+https://hel1.econode.io/3d/)"}
API = "https://api.polyhaven.com"


def get(url):
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=60) as r:
        return r.read()


def download(url, dest: Path):
    if dest.exists() and dest.stat().st_size:
        return
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(dest.suffix + ".part")
    tmp.write_bytes(get(url))
    tmp.replace(dest)


def search(kind, words):
    t = {"textures": "textures", "texture": "textures", "models": "models", "model": "models"}[kind]
    assets = json.loads(get(f"{API}/assets?t={t}"))
    words = [w.lower() for w in words]
    scored = []
    for aid, a in assets.items():
        hay = " ".join([aid, a["name"], *a.get("tags", []), *a.get("categories", [])]).lower()
        score = sum(3 if w in a["name"].lower() or w in aid else 1 for w in words if w in hay)
        if score:
            scored.append((score, a.get("download_count", 0), aid, a))
    scored.sort(key=lambda s: (-s[0], -s[1]))
    for _, _, aid, a in scored[:15]:
        dims = a.get("dimensions")
        size = f" {dims[0]/1000:g}x{dims[1]/1000:g}m" if dims and t == "textures" else ""
        print(f"{aid}: {a['name']}{size} — {', '.join(a.get('tags', [])[:8])}")
    if not scored:
        print("no matches")


def texture(aid, res="1k"):
    files = json.loads(get(f"{API}/files/{aid}"))
    out = ASSETS / "polyhaven" / "textures" / aid / res
    for key, suffix in [("Diffuse", "diff"), ("nor_gl", "nor_gl"), ("arm", "arm")]:
        if key in files and res in files[key]:
            download(files[key][res]["jpg"]["url"], out / f"{suffix}.jpg")
    print(f"texture ready: {aid} ({res}) -> {out}")
    print(f'use in a model script:  mat = studio.pbr_material("{aid}", res="{res}", scale=1.0)')


def model(aid, res="1k"):
    files = json.loads(get(f"{API}/files/{aid}"))
    g = files["gltf"][res]["gltf"]
    out = ASSETS / "polyhaven" / "models" / aid / res
    download(g["url"], out / f"{aid}.gltf")
    for rel, f in g.get("include", {}).items():
        download(f["url"], out / rel)
    info = json.loads(get(f"{API}/info/{aid}"))
    print(f"model ready: {aid} ({info.get('name')}) -> {out / (aid + '.gltf')}")
    print(f'use in a model script:  objs = studio.polyhaven_model("{aid}", res="{res}")')


def _space_file(client, value, dest: Path):
    """Gradio returns a local temp path, or a {'value': remote_path} update dict."""
    path = value.get("value") if isinstance(value, dict) else value
    if isinstance(path, dict):
        path = path.get("path") or path.get("value")
    if path and os.path.exists(path):
        shutil.copy(path, dest)
    else:
        urllib.request.urlretrieve(client.src.rstrip("/") + "/gradio_api/file=" + path, dest)


def generate(slug, prompt=None, image=None, engine="trellis"):
    from gradio_client import Client, handle_file  # studio venv only
    token = os.environ.get("HF_TOKEN") or None
    out = ASSETS / "generated" / slug
    out.mkdir(parents=True, exist_ok=True)
    ref = out / "reference.webp"
    try:
        if image:
            shutil.copy(image, ref)
        else:
            flux = Client("black-forest-labs/FLUX.1-schnell", token=token, verbose=False)
            img = flux.predict(
                prompt=f"{prompt}, single object, full view, centered, isolated on plain white "
                       "background, 3d render, soft even studio lighting, no shadows",
                seed=0, randomize_seed=True, width=1024, height=1024,
                num_inference_steps=4, api_name="/infer")
            shutil.copy(img[0], ref)
        print(f"reference image: {ref}")
        if engine == "hunyuan":
            c = Client("tencent/Hunyuan3D-2.1", token=token, verbose=False)
            r = c.predict(image=handle_file(str(ref)), api_name="/shape_generation")
            _space_file(c, r[0], out / "mesh.glb")
            print(f"mesh (untextured, ~{r[2].get('number_of_faces')} faces): {out / 'mesh.glb'}")
        else:
            c = Client("trellis-community/TRELLIS", token=token, verbose=False)
            try:
                c.predict(api_name="/start_session")
            except Exception:
                pass
            pre = c.predict(image=handle_file(str(ref)), api_name="/preprocess_image")
            r = c.predict(image=handle_file(pre), multiimages=[], seed=0, ss_guidance_strength=7.5,
                          ss_sampling_steps=12, slat_guidance_strength=3.0, slat_sampling_steps=12,
                          multiimage_algo="stochastic", mesh_simplify=0.95, texture_size=1024,
                          api_name="/generate_and_extract_glb")
            _space_file(c, r[2] or r[1], out / "mesh.glb")
            print(f"mesh (textured): {out / 'mesh.glb'}")
    except Exception as e:
        msg = str(e)
        if "quota" in msg.lower() or "GPU duration" in msg:
            sys.exit(f"GENERATION UNAVAILABLE (free GPU quota exhausted): {msg[:300]}\n"
                     "Fall back to modelling it procedurally in bpy, or try --engine hunyuan (needs less GPU).")
        sys.exit(f"generation failed: {msg[:500]}")
    print(f'use in a model script:  objs = studio.generated_model("{slug}", size=2.0)')


def main(a):
    if len(a) >= 2 and a[0] == "search":
        return search(a[1], a[2:])
    if len(a) in (2, 3) and a[0] in ("texture", "model"):
        return (texture if a[0] == "texture" else model)(*a[1:])
    if len(a) >= 3 and a[0] == "generate":
        slug, rest = a[1], a[2:]
        engine = "trellis"
        if "--engine" in rest:
            i = rest.index("--engine"); engine = rest[i + 1]; del rest[i:i + 2]
        if rest[:1] == ["--image"]:
            return generate(slug, image=rest[1], engine=engine)
        return generate(slug, prompt=" ".join(rest), engine=engine)
    sys.exit(__doc__)


if __name__ == "__main__":
    import re
    if len(sys.argv) > 2 and sys.argv[1] in ("texture", "model", "generate") \
            and not re.match(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$", sys.argv[2]):
        sys.exit("bad id/slug")
    main(sys.argv[1:])
