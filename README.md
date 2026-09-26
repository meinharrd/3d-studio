# 3D Studio

A small web app for making 3D models by chatting. Describe a model and a Claude agent writes a
Blender Python script for it, builds it headless, looks at the render, and iterates. The result
is published to a public gallery with an interactive viewer.

- **Chat UI** (claude.ai-style): chats per model in the sidebar, agent steps shown as collapsible
  "thinking", follow-ups continue the same agent session, stop button.
- **Viewer** (three.js): orbit/zoom, with *Wire*, *Shaded*, *Textured* and *Lights* modes. Lights mode
  shows only the model's own glTF lights (`KHR_lights_punctual`) and emissive glow.
- **Assets**: [Poly Haven](https://polyhaven.com) CC0 textures and models; optional AI mesh
  generation (text → FLUX.1-schnell image → TRELLIS mesh) through free Hugging Face Spaces.
- **Public gallery, private editing**: anyone can browse; editing needs a login.

## How it works

```
app.py           FastAPI backend: login, chats, job queue, Claude agent (claude-agent-sdk)
pipeline/
  build.sh       content/models/<name>.py  →  web/models/<name>.glb + .png, updates models.json
  runner.py      Blender side: clean scene, run the model script, export GLB, render thumbnail
  studio.py      helpers for model scripts (materials, Poly Haven textures/models, imports)
  tools.py       asset CLI (./assets search|texture|model|generate)
web/             static gallery (index.html, viewer.js); web/models/ holds the built output
content/         model scripts + downloaded assets — kept out of this repo, has its own git
                 history (every agent job is committed there)
```

The agent runs with a tool guard: it can read files, write only `content/models/<name>.py`, and run
only `./build.sh` and `./assets` (no shell syntax). A new chat is locked to the first new model name
it creates; a chat about an existing model can only touch that model. Note that the model scripts
themselves are arbitrary Python executed by Blender, so only give logins to people you trust.

## Setup

Requirements: Linux, Python 3.11+, [Blender](https://www.blender.org/download/) 4.2+ on `PATH`
(tested with 5.2), a Claude login for the agent (`claude` CLI login in `~/.claude`, or
`CLAUDE_CODE_OAUTH_TOKEN` / `ANTHROPIC_API_KEY` in the environment), and a web server.

```sh
./setup.sh                                  # venv, content/ repo, helper symlinks
.venv/bin/python app.py passwd admin        # create a login
.venv/bin/uvicorn app:app --port 8310       # or use deploy/3d-studio.service
```

Serve `web/` at `/3d/` and proxy `/3d/api/*` to the backend — see `deploy/Caddyfile`.
Set `STUDIO_SITE` to the public gallery URL and optionally `STUDIO_MODEL` to pick the Claude model.
For more AI-generation quota, put a free Hugging Face token in `data/hf_token`.
