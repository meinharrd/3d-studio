"""3D studio backend (serves /3d/api/*; the static gallery in web/ is served by the web server).

Public: the static gallery (served by Caddy). Logged in: submit prompts that
create or change models. Each prompt runs a Claude agent in 3d-src/ that may
only edit models/*.py and run build.sh; jobs run one at a time.
"""
import asyncio
import hashlib
import hmac
import json
import os
import re
import secrets
import shlex
import subprocess
import time
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, Response
from pydantic import BaseModel

from claude_agent_sdk import (AssistantMessage, ClaudeAgentOptions, ClaudeSDKClient,
                              PermissionResultAllow, PermissionResultDeny, ResultMessage,
                              TextBlock, ThinkingBlock, ToolUseBlock)

HERE = Path(__file__).resolve().parent
SRC = HERE / "content"      # model scripts + asset library (its own git repo)
WEB = HERE / "web" / "models"  # published GLBs, thumbnails, models.json
SITE = os.environ.get("STUDIO_SITE", "https://hel1.econode.io/3d/")
DATA = HERE / "data"
DATA.mkdir(exist_ok=True)
USERS = DATA / "users.json"        # {"name": {"salt": hex, "hash": hex}}
SESSIONS = DATA / "sessions.json"  # {"token": {"user": name, "exp": ts}}
JOBS = DATA / "jobs.json"
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
SESSION_TTL = 30 * 86400
COOKIE = "studio_session"

app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)


# ---------- storage ----------

def load(path, default):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return default


def save(path, obj):
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        json.dump(obj, fh, indent=1)
    tmp.replace(path)


def hash_pw(pw: str, salt: bytes) -> str:
    return hashlib.scrypt(pw.encode(), salt=salt, n=2**14, r=8, p=1).hex()


def set_password(user: str, pw: str):
    users = load(USERS, {})
    salt = secrets.token_bytes(16)
    users[user] = {"salt": salt.hex(), "hash": hash_pw(pw, salt)}
    save(USERS, users)


CONVS = DATA / "convs.json"   # {id: {id, model, title, created, updated, session_id}}
jobs: list[dict] = load(JOBS, [])
convs: dict[str, dict] = load(CONVS, {})
for j in jobs:  # anything in flight died with the previous process
    if j["status"] in ("queued", "running"):
        j["status"], j["error"] = "failed", "server restarted"
if not convs and jobs:  # one-time migration: group old jobs into one chat per model
    by_model = {}
    for j in jobs:
        key = j.get("result") or j.get("target") or j["id"]
        c = by_model.get(key)
        if not c:
            c = by_model[key] = {"id": secrets.token_hex(6), "model": j.get("result") or j.get("target"),
                                 "title": j["prompt"][:80], "created": j["created"],
                                 "updated": j["created"], "session_id": None}
        j["conv"] = c["id"]
        c["updated"] = j.get("finished") or j["created"]
    convs = {c["id"]: c for c in by_model.values()}
for m in load(WEB / "models.json", []):  # every model gets a chat, even ones built by hand
    if not any(c["model"] == m["name"] for c in convs.values()):
        cid = secrets.token_hex(6)
        convs[cid] = {"id": cid, "model": m["name"], "title": m["title"], "created": m.get("updated", 0),
                      "updated": m.get("updated", 0), "session_id": None}
queue: asyncio.Queue = asyncio.Queue()
running: dict[str, ClaudeSDKClient] = {}  # job id -> client, for Stop
login_fails: dict[str, list[float]] = {}


def save_jobs():
    save(JOBS, jobs[-1000:])


def save_convs():
    save(CONVS, convs)


def manifest() -> list[dict]:
    return load(WEB / "models.json", [])


# ---------- auth ----------

def current_user(req: Request) -> str | None:
    tok = req.cookies.get(COOKIE)
    if not tok:
        return None
    s = load(SESSIONS, {}).get(tok)
    if not s or s["exp"] < time.time():
        return None
    return s["user"]


def require_user(req: Request) -> str:
    user = current_user(req)
    if not user:
        raise HTTPException(401, "login required")
    return user


class Login(BaseModel):
    username: str
    password: str


@app.post("/3d/api/login")
async def login(body: Login, req: Request, resp: Response):
    ip = req.headers.get("x-forwarded-for", req.client.host).split(",")[0].strip()
    recent = [t for t in login_fails.get(ip, []) if t > time.time() - 900]
    if len(recent) >= 10:
        raise HTTPException(429, "too many attempts, try again later")
    u = load(USERS, {}).get(body.username)
    ok = u and hmac.compare_digest(hash_pw(body.password, bytes.fromhex(u["salt"])), u["hash"])
    if not ok:
        login_fails[ip] = recent + [time.time()]
        await asyncio.sleep(1)
        raise HTTPException(401, "wrong username or password")
    tok = secrets.token_urlsafe(32)
    sessions = {k: v for k, v in load(SESSIONS, {}).items() if v["exp"] > time.time()}
    sessions[tok] = {"user": body.username, "exp": time.time() + SESSION_TTL}
    save(SESSIONS, sessions)
    resp.set_cookie(COOKIE, tok, max_age=SESSION_TTL, path="/3d", httponly=True,
                    secure=True, samesite="strict")
    return {"user": body.username}


@app.post("/3d/api/logout")
async def logout(req: Request, resp: Response):
    sessions = load(SESSIONS, {})
    sessions.pop(req.cookies.get(COOKIE, ""), None)
    save(SESSIONS, sessions)
    resp.delete_cookie(COOKIE, path="/3d")
    return {}


@app.get("/3d/api/me")
async def me(req: Request):
    return {"user": current_user(req)}


# ---------- chats ----------

class MsgIn(BaseModel):
    prompt: str


def conv_view(c):
    m = next((m for m in manifest() if m["name"] == c["model"]), None) if c["model"] else None
    last = next((j for j in reversed(jobs) if j.get("conv") == c["id"]), None)
    return {**c, "model_info": m, "status": last and last["status"]}


def get_conv(cid):
    c = convs.get(cid)
    if not c:
        raise HTTPException(404, "no such chat")
    return c


async def enqueue(c, user, prompt):
    prompt = prompt.strip()
    if not prompt or len(prompt) > 4000:
        raise HTTPException(400, "message must be 1-4000 characters")
    if any(j.get("conv") == c["id"] and j["status"] in ("queued", "running") for j in jobs):
        raise HTTPException(409, "this chat is still working")
    job = {"id": secrets.token_hex(6), "conv": c["id"], "user": user, "prompt": prompt,
           "target": c["model"], "status": "queued", "created": time.time(), "log": [],
           "result": None, "error": None}
    jobs.append(job)
    c["updated"] = time.time()
    save_jobs(); save_convs()
    await queue.put(job)
    return job


@app.get("/3d/api/convs")
async def list_convs(req: Request):
    require_user(req)
    names = {m["name"] for m in manifest()}
    stale = [cid for cid, c in convs.items()  # model deleted outside the UI (delete.sh) and no chat history
             if c["model"] and c["model"] not in names and not any(j.get("conv") == cid for j in jobs)]
    for cid in stale:
        convs.pop(cid)
    if stale:
        save_convs()
    return sorted((conv_view(c) for c in convs.values()), key=lambda c: -c["updated"])


@app.post("/3d/api/convs")
async def new_conv(body: MsgIn, req: Request):
    user = require_user(req)
    c = {"id": secrets.token_hex(6), "model": None, "title": body.prompt.strip()[:80],
         "created": time.time(), "updated": time.time(), "session_id": None}
    convs[c["id"]] = c
    await enqueue(c, user, body.prompt)
    return conv_view(c)


@app.get("/3d/api/convs/{cid}")
async def read_conv(cid: str, req: Request):
    require_user(req)
    c = get_conv(cid)
    return {**conv_view(c), "jobs": [j for j in jobs if j.get("conv") == cid]}


@app.post("/3d/api/convs/{cid}/messages")
async def post_message(cid: str, body: MsgIn, req: Request):
    user = require_user(req)
    return await enqueue(get_conv(cid), user, body.prompt)


@app.post("/3d/api/convs/{cid}/stop")
async def stop(cid: str, req: Request):
    require_user(req)
    for j in jobs:
        if j.get("conv") == cid and j["status"] in ("queued", "running"):
            j["stop"] = True
            if cl := running.get(j["id"]):
                await cl.interrupt()
    save_jobs()
    return {}


@app.delete("/3d/api/convs/{cid}")
async def delete_conv(cid: str, req: Request):
    require_user(req)
    c = get_conv(cid)
    if any(j.get("conv") == cid and j["status"] in ("queued", "running") for j in jobs):
        raise HTTPException(409, "stop the chat first")
    if c["model"] and NAME_RE.match(c["model"]):
        subprocess.run([str(SRC / "delete.sh"), c["model"]], check=True)
        git_commit(f"Delete {c['model']}")
    convs.pop(cid)
    jobs[:] = [j for j in jobs if j.get("conv") != cid]
    save_convs(); save_jobs()
    return {}


class ModelMeta(BaseModel):
    title: str
    description: str = ""


@app.patch("/3d/api/models/{name}")
async def edit_model(name: str, body: ModelMeta, req: Request):
    require_user(req)
    title, desc = body.title.strip(), body.description.strip()
    if not title or len(title) > 120 or len(desc) > 2000:
        raise HTTPException(400, "title 1-120 characters, description up to 2000")
    import fcntl
    mf = WEB / "models.json"
    with open(str(mf) + ".lock", "w") as lock:  # build.sh takes the same lock
        fcntl.flock(lock, fcntl.LOCK_EX)
        items = manifest()
        m = next((i for i in items if i["name"] == name), None)
        if not m:
            raise HTTPException(404, "no such model")
        m["title"], m["description"] = title, desc
        tmp = mf.with_suffix(".tmp")
        tmp.write_text(json.dumps(items, indent=1))
        os.chmod(tmp, 0o644)
        tmp.replace(mf)
    return m


def git_commit(msg: str):
    subprocess.run(["git", "add", "-A"], cwd=SRC, check=True)
    subprocess.run(["git", "-c", "user.name=3d-studio", "-c", "user.email=3d@hel1.econode.io",
                    "commit", "-qm", msg], cwd=SRC)  # no-op (exit 1) when nothing changed


# ---------- agent ----------

SYSTEM = f"""You chat with the owner of a 3D gallery and create/modify models for it.
The user may also just ask questions — answer briefly; only build when asked for a change.
You create and modify 3D models for a public gallery at {SITE}.
You work in {SRC}. Each model is a Blender 5.2 Python script at models/<name>.py
(name: lowercase a-z, 0-9, dashes). The script only builds the model with bpy: the scene is
already empty; do NOT add cameras or export code, and don't call read_factory_settings.
Lights: the gallery has a "Lights" view mode that shows ONLY the model's own lights (dark
environment, bloom). When a model has light sources (lamps, lanterns, windows at night, screens,
candles, neon, engines), add real Blender lights (POINT / SPOT / SUN — AREA lights don't export)
at the source and give the glowing parts an emissive material (Emission Color + Strength 3-15).
Typical energies: candle 1-5 W, lamp bulb 20-60 W, spot 50-200 W, sun 2-5. Don't add generic
studio/fill lights — the other view modes provide studio lighting.
ANIMATION — models may animate: keyframe object location/rotation/scale (or armatures). Call
studio.animation(frames=48) to set the clip length (24 fps), then studio.loop_keys(obj, "scale",
[v0, v1, ...], period=24, offset=5) for seamless loops (period must divide the length; vary
periods/offsets so repeated parts don't move in sync). Name a light or material with "flicker"
(e.g. campfire_flicker, flame_flicker_1) for automatic brightness flicker in the viewer — animated
brightness/colour/emission values do NOT export. Checks and the thumbnail use the first frame.
Only animate when it suits the model (fire, water, machines, flags...) or when asked.

LIGHT CHECK — every build renders the model the way Lights mode shows it (only its own lights)
into last_lights.png and prints "light:" / "lights view:" lines. "LIGHTS ..." lines (exit code 4)
MUST be fixed: TOO BRIGHT / TOO DIM (adjust energies; point/spot fall off with distance²), or
INSIDE MESH (a light placed inside geometry — blocked in Blender, leaks in the viewer; move it out,
e.g. under the shade or in front of the bulb, and keep shadow_soft_size small). For models with
lights, Read last_lights.png and judge it like the thumbnail.
Don't use `Material.use_nodes` (deprecated; new materials already have "Principled BSDF").
Keep a sensible real-world scale in metres, resting on z=0. Use Principled BSDF materials.

Helpers — `import studio` in the model script (read ../pipeline/studio.py for details):
- studio.material(name, rgb, roughness, metallic)
- studio.pbr_material(texture_id, res="1k", scale=1.0)  realistic Poly Haven PBR textures
  (wood, metal, stone, fabric, brick, ground...). Needs UVs; use studio.box_uv(obj) on custom meshes.
- studio.polyhaven_model(model_id, size=None, location=(x,y,z))  ready-made CC0 models
  (furniture, props, plants, rocks...). Returns the root empty.
- studio.generated_model(slug, size=2.0, location=...)  AI-generated mesh (see below).

Asset CLI (downloads into library/; builds are offline, so fetch first):
  ./assets search textures <words>     ./assets texture <id> [1k|2k]
  ./assets search models <words>       ./assets model <id> [1k|2k]
  ./assets generate <slug> "<prompt>"  text -> image -> textured 3D mesh via free Hugging Face
      GPUs (TRELLIS). Takes 1-3 min: call Bash with timeout 600000. Read
      library/generated/<slug>/reference.webp to see what it was based on. Add
      `--engine hunyuan` for an untextured but more detailed shape (then apply materials).
  Free GPU quota is small (a few generations per day) and may be exhausted — if it fails,
  fall back to other methods. Don't retry generation more than twice per job.

Choosing an approach: procedural bpy modelling for geometric/designed things (buildings,
furniture, vehicles, machines, low-poly/stylised); Poly Haven models when a stock object fits;
Poly Haven textures to make surfaces realistic; AI generation for organic subjects
(animals, characters, food, sculptures) that are hard to build from primitives.
Mix freely (e.g. a generated statue on a procedural plinth with a marble texture).

Z-FIGHTING — check it on every build, it is part of the design process: build.sh detects
overlapping coplanar faces. "Z-FIGHTING:" lines (exit code 3) are visible flicker and MUST be fixed
before you finish — separate the surfaces by a real gap (>= 0.5% of the model size, e.g. 1-2 cm on
a 3 m model), inset decals/bands/trim so they stand proud of the surface, sink parts into the
surface they rest on instead of placing them flush, and never stack two ground/floor planes at
the same height. Joined meshes can overlap themselves (duplicate faces): don't place boxes so their
faces coincide; merge or offset them. "contact:" lines (a face resting flush on another, facing
the opposite way) are usually hidden and informational. Mention in your final message that the
z-fighting check passed.

Build with:  ./build.sh <name> "<Title>" "<one-sentence description>"
That exports the GLB, renders a thumbnail to {WEB}/<name>.png and publishes it.
On failure it prints the Blender error; fix the script and rebuild.

After each build, Read the thumbnail PNG and judge it critically against the request.
Iterate (edit + rebuild) until it looks good — usually 1-4 builds. Look at existing
scripts in models/ for style. Tools: Read/Glob/Grep anywhere, Write/Edit only models/*.py,
Bash only for ./build.sh and ./assets (no pipes or redirects). Finish with one short
sentence describing what you made."""


def job_prompt(job, resumed=False) -> str:
    if job["target"]:
        m = next((m for m in manifest() if m["name"] == job["target"]), None)
        if resumed:
            return f"(Model `{job['target']}`.) {job['prompt']}"
        title = f", current title {m['title']!r}, description {m.get('description', '')!r}" if m else ""
        return (f"This chat is about the existing model `{job['target']}` (script "
                f"models/{job['target']}.py{title}). Keep the name and the current title/description "
                f"(the owner may have edited them) unless the change makes them wrong.\n\nUser: {job['prompt']}")
    taken = ", ".join(m["name"] for m in manifest()) or "none"
    if resumed:
        return f"(No model has been built in this chat yet; names taken: {taken}.) {job['prompt']}"
    return (f"Create a new model. Pick a short new name (taken: {taken}).\n\n"
            f"Request: {job['prompt']}")


def make_guard(job):
    models_dir = str(SRC / "models") + "/"
    taken = {m["name"] for m in manifest()}
    lock = {"name": job["target"]}  # new chats claim the first fresh name they write

    def claim(name):
        if lock["name"]:
            return None if name == lock["name"] else f"This chat may only change {lock['name']}."
        if name in taken:
            return f"{name} belongs to another chat; pick a new name."
        lock["name"] = name
        return None

    async def guard(tool, inp, ctx):
        res = await _guard(tool, inp)
        if isinstance(res, PermissionResultDeny):
            log(job, "deny", f"{tool} blocked: {res.message}")
        return res

    async def _guard(tool, inp):
        if tool in ("Read", "Glob", "Grep"):
            return PermissionResultAllow()
        if tool in ("Write", "Edit"):
            p = os.path.realpath(os.path.join(SRC, inp.get("file_path", "")))
            name = p[len(models_dir):-3] if p.startswith(models_dir) and p.endswith(".py") else ""
            if not NAME_RE.match(name):
                return PermissionResultDeny(message="Only models/<name>.py may be written.")
            if err := claim(name):
                return PermissionResultDeny(message=err)
            return PermissionResultAllow()
        if tool == "Bash":
            cmd = inp.get("command", "").strip()
            try:
                argv = shlex.split(cmd)
            except ValueError:
                argv = []
            if re.search(r"[;&|`$<>\n\\]", cmd) or not argv:
                return PermissionResultDeny(message="No pipes, redirects or shell syntax.")
            if argv[0] in ("./assets", str(SRC / "assets")) and len(argv) >= 2:
                return PermissionResultAllow()
            if argv[0] in ("./build.sh", str(SRC / "build.sh")) and 2 <= len(argv) <= 4:
                if err := claim(argv[1]):
                    return PermissionResultDeny(message=err)
                return PermissionResultAllow()
            return PermissionResultDeny(message='Only `./build.sh <name> "<Title>" "<Description>"` and `./assets ...` are allowed.')
        return PermissionResultDeny(message=f"{tool} is not available.")

    return guard


def log(job, kind, text):
    job["log"].append({"t": time.time(), "kind": kind, "text": text[:8000 if kind == "text" else 2000]})
    save_jobs()


async def run_job(job):
    c = convs.get(job["conv"])
    if not c or job.get("stop"):
        job["status"], job["error"] = "stopped", None
        save_jobs()
        return
    job["status"], job["started"] = "running", time.time()
    job["target"] = c["model"]
    save_jobs()
    before = {m["name"]: m.get("updated") for m in manifest()}
    resume = c.get("session_id")
    if resume and not (Path.home() / ".claude/projects" / re.sub(r"[^A-Za-z0-9]", "-", str(SRC))
                       / f"{resume}.jsonl").is_file():
        resume = None
    opts = ClaudeAgentOptions(
        cwd=str(SRC), system_prompt=SYSTEM, setting_sources=[],
        tools=["Read", "Write", "Edit", "Glob", "Grep", "Bash"],
        permission_mode="default", can_use_tool=make_guard(job), resume=resume,
        mcp_servers={}, strict_mcp_config=True, env={"ENABLE_CLAUDEAI_MCP_SERVERS": "false"},
        max_turns=60, model=os.environ.get("STUDIO_MODEL") or None,
        max_buffer_size=64 * 1024 * 1024,
    )
    try:
        async with ClaudeSDKClient(options=opts) as client:
            running[job["id"]] = client
            await client.query(job_prompt(job, resumed=bool(resume)))
            async for msg in client.receive_response():
                if isinstance(msg, AssistantMessage):
                    for b in msg.content:
                        if isinstance(b, TextBlock) and b.text.strip():
                            log(job, "text", b.text.strip())
                        elif isinstance(b, ThinkingBlock) and b.thinking.strip():
                            log(job, "thinking", b.thinking.strip())
                        elif isinstance(b, ToolUseBlock):
                            i = b.input
                            desc = i.get("command") or i.get("file_path") or i.get("pattern") or ""
                            log(job, "tool", f"{b.name}: {desc}")
                elif isinstance(msg, ResultMessage):
                    job["cost"] = msg.total_cost_usd
                    if msg.session_id:
                        c["session_id"] = msg.session_id
                    if msg.is_error and not job.get("stop"):
                        raise RuntimeError(msg.result or "agent error")
        if job.get("stop"):
            job["status"] = "stopped"
        else:
            changed = [m["name"] for m in manifest() if before.get(m["name"]) != m.get("updated")]
            if changed:
                job["result"] = changed[0]
                c["model"] = c["model"] or changed[0]
            job["status"] = "done"
    except Exception as e:  # noqa: BLE001 — surface anything to the UI
        job["status"] = "stopped" if job.get("stop") else "failed"
        job["error"] = None if job.get("stop") else str(e)[:1000]
    finally:
        running.pop(job["id"], None)
    verb = "Change " + job["target"] if job["target"] else "Create " + (c["model"] or "?")
    git_commit(f"{verb}: {job['prompt'][:200]}" + ("" if job["status"] == "done" else f" [{job['status']}]"))
    job["finished"] = time.time()
    c["updated"] = time.time()
    save_jobs(); save_convs()


async def worker():
    while True:
        job = await queue.get()
        await run_job(job)


@app.on_event("startup")
async def start():
    save_jobs(); save_convs()
    asyncio.create_task(worker())


if __name__ == "__main__":
    import getpass, sys
    if len(sys.argv) == 3 and sys.argv[1] == "passwd":
        set_password(sys.argv[2], getpass.getpass("New password: "))
        print("saved")
