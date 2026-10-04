#!/bin/sh
set -eu
BROWSER_MODE=$(python -c 'import json,pathlib; p=pathlib.Path("/data/options.json"); print(json.loads(p.read_text()).get("browser_mode","headed") if p.exists() else "headed")')
if [ "$BROWSER_MODE" = "headed" ]; then
    export HEADED=1
    exec xvfb-run -a -s '-screen 0 1920x1080x24' python /app/run.py
fi
export HEADED=0
exec python /app/run.py
