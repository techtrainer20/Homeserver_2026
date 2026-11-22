#!/bin/bash
# start_dashboard.sh - Single-instance dashboard launcher with auto-restart and safe mode fallback

export DISPLAY=:0
export XAUTHORITY=${XAUTHORITY:-$HOME/.Xauthority}

# Single instance lock using flock
exec 200>/tmp/start_dashboard.lock
flock -n 200 || exit 0

# Wait ~15s for desktop, X11, and network
sleep 15

# Disable screen blanking & DPMS
xset s off 2>/dev/null
xset s noblank 2>/dev/null
xset -dpms 2>/dev/null

CRASH_FILE="$HOME/.cache/dashboard/crash_count"
mkdir -p $HOME/.cache/dashboard

SCRIPT_TO_RUN="newdash25.py"

while true; do
    CRASHES=0
    if [ -f "$CRASH_FILE" ]; then
        CRASHES=$(cat "$CRASH_FILE" 2>/dev/null || echo 0)
    fi

    # Safe mode check: if 3 consecutive crashes, fallback to .bak
    if [ "$CRASHES" -ge 3 ]; then
        echo "[$(date '+%Y-%m-%d %H:%M:%S')] SAFE MODE ACTIVATED: 3 startup crashes. Falling back to newdash25.py.bak" >> $HOME/dash.log
        SCRIPT_TO_RUN="newdash25.py.bak"
    fi

    START_TIME=$(date +%s)
    
    python3 "$SCRIPT_TO_RUN"
    EXIT_CODE=$?

    END_TIME=$(date +%s)
    RUNTIME=$((END_TIME - START_TIME))

    # Clean exit (Esc key exits with code 0) -> Stay closed as requested
    if [ "$EXIT_CODE" -eq 0 ]; then
        echo "[$(date '+%Y-%m-%d %H:%M:%S')] Dashboard closed by user (exit code 0). Staying closed." >> $HOME/dash.log
        echo 0 > "$CRASH_FILE"
        break
    fi

    # Non-zero exit: App crashed!
    echo "[$(date '+%Y-%m-%d %H:%M:%S')] Dashboard crashed (exit code $EXIT_CODE) after ${RUNTIME}s" >> $HOME/dash.log
    
    # If crashed quickly at startup (<60s), increment counter
    if [ "$RUNTIME" -lt 60 ]; then
        CRASHES=$((CRASHES + 1))
        echo "$CRASHES" > "$CRASH_FILE"
    else
        echo 0 > "$CRASH_FILE"
    fi

    sleep 5
done
