#!/bin/bash
# Usage: build.sh <name> ["Title"] ["Description"]   (uses models/<name>.py)
# Builds the GLB + thumbnail into the web dir and updates models.json.
set -euo pipefail
REPO=$(cd "$(dirname "$(readlink -f "$0")")/.." && pwd)
SRC=$REPO/content; OUT=$REPO/web/models; PIPE=$REPO/pipeline
name=$1; title=${2:-$1}; desc=${3:-}
[[ $name =~ ^[a-z0-9][a-z0-9-]{0,63}$ ]] || { echo "bad name: $name (use a-z, 0-9, -)"; exit 2; }
[[ -f $SRC/models/$name.py ]] || { echo "missing $SRC/models/$name.py"; exit 2; }
blender -b --factory-startup -P "$PIPE/runner.py" -- "$SRC/models/$name.py" "$OUT" "$name" > "$SRC/last_build.log" 2>&1 \
  || { tail -30 "$SRC/last_build.log"; exit 1; }
grep -E "^(Error|Traceback)" "$SRC/last_build.log" && { tail -30 "$SRC/last_build.log"; exit 1; }
python3 - "$OUT" "$name" "$title" "$desc" <<'PY'
import json, sys, os, time
out, name, title, desc = sys.argv[1:]
mf = os.path.join(out, "models.json")
items = json.load(open(mf)) if os.path.exists(mf) else []
items = [i for i in items if i["name"] != name]
items.insert(0, {"name": name, "title": title, "description": desc,
                 "glb": f"models/{name}.glb", "thumb": f"models/{name}.png",
                 "updated": int(time.time())})
json.dump(items, open(mf, "w"), indent=1)
PY
chmod o+r "$OUT"/*
echo "OK: ${STUDIO_SITE:-https://hel1.econode.io/3d/}#$name"
