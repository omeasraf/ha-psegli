#!/bin/sh
set -eu
BROWSER_MODE=$(python -c 'import json,pathlib; p=pathlib.Path("/data/options.json"); print(json.loads(p.read_text()).get("browser_mode","headed") if p.exists() else "headed")')
if [ "$BROWSER_MODE" = "headed" ]; then
    export HEADED=1
    export DISPLAY=:99
    # Start the display explicitly: xvfb-run's signal-based readiness handshake
    # can stall when it is PID 1 in an add-on with init disabled.
    Xvfb "$DISPLAY" -screen 0 1920x1080x24 -nolisten tcp &
    display_pid=$!
    attempts=0
    while [ ! -S /tmp/.X11-unix/X99 ]; do
        if ! kill -0 "$display_pid" 2>/dev/null || [ "$attempts" -ge 100 ]; then
            echo "Virtual display failed to start" >&2
            exit 1
        fi
        attempts=$((attempts + 1))
        sleep 0.1
    done
    echo "Virtual display ready; starting PSEG service"
    exec python /app/run.py
fi
export HEADED=0
exec python /app/run.py
