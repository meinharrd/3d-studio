#!/bin/bash
# Usage: delete.sh <name> — removes a model from the gallery (script stays in git history)
set -euo pipefail
REPO=$(cd "$(dirname "$(readlink -f "$0")")/.." && pwd)
SRC=$REPO/content; OUT=$REPO/web/models; name=$1
[[ $name =~ ^[a-z0-9][a-z0-9-]{0,63}$ ]] || { echo "bad name"; exit 2; }
rm -f "$OUT/$name.glb" "$OUT/$name.png" "$SRC/models/$name.py"
python3 - "$OUT/models.json" "$name" <<'PY'
import json, sys
mf, name = sys.argv[1:]
items = [i for i in json.load(open(mf)) if i["name"] != name]
json.dump(items, open(mf, "w"), indent=1)
PY
