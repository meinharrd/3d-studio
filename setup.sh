#!/bin/bash
# One-time setup: venv, content dir (own git repo for model history), helper symlinks.
set -euo pipefail
cd "$(dirname "$(readlink -f "$0")")"
[ -d .venv ] || python3 -m venv .venv
.venv/bin/pip install -q -r requirements.txt
mkdir -p content/models web/models data
[ -d content/.git ] || git -C content init -q
for f in build.sh assets delete.sh; do ln -sfn ../pipeline/$f content/$f; done
[ -f content/.gitignore ] || printf '__pycache__/\nlast_build.log\nlibrary/polyhaven/\nbuild.sh\nassets\ndelete.sh\n' > content/.gitignore
[ -f web/models/models.json ] || echo '[]' > web/models/models.json
echo "Now set a login:  .venv/bin/python app.py passwd <username>"
