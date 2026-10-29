#!/usr/bin/env zsh
# manage_nexus.sh — Sovereign Nexus service manager
#
# Usage:
#   ./manage_nexus.sh start       — start server + watchdog
#   ./manage_nexus.sh stop        — gracefully stop both
#   ./manage_nexus.sh restart     — stop + start
#   ./manage_nexus.sh status      — show process health
#   ./manage_nexus.sh logs        — tail live logs
#   ./manage_nexus.sh provision   — run voice asset provisioner

DIR="$(cd "$(dirname "${0:A}")" && pwd)"
PYTHON=$(command -v python3.13 || command -v python3)
SERVER_PID="$DIR/server.pid"
WATCH_PID="$DIR/watchdog.pid"
PLIST_SERVER="$HOME/Library/LaunchAgents/com.local.jarvis-nexus.plist"
PLIST_WATCH="$HOME/Library/LaunchAgents/com.local.jarvis-watchdog.plist"

RED='\033[0;31m'; GRN='\033[0;32m'; YLW='\033[0;33m'; BLU='\033[0;34m'; RST='\033[0m'

hdr() { echo "\n${BLU}══ $1 ══${RST}"; }
ok()  { echo "${GRN}  ✓ $1${RST}"; }
err() { echo "${RED}  ✗ $1${RST}"; }
inf() { echo "  $1"; }

# ── Helpers ───────────────────────────────────────────────────────────────────
is_running() {
    local pid_file="$1" name="$2"
    [[ -f "$pid_file" ]] || return 1
    local pid=$(cat "$pid_file" 2>/dev/null)
    [[ -n "$pid" ]] && kill -0 "$pid" 2>/dev/null && return 0
    # Fallback: search by process name
    pgrep -f "$name" >/dev/null 2>&1
}

server_ok() {
    curl -sf http://localhost:5001/api/health >/dev/null 2>&1
}

kill_gracefully() {
    local pid="$1" label="$2"
    [[ -z "$pid" ]] && return
    inf "Stopping $label (PID $pid)..."
    kill -TERM "$pid" 2>/dev/null
    local i=0
    while kill -0 "$pid" 2>/dev/null && (( i++ < 10 )); do sleep 0.5; done
    kill -0 "$pid" 2>/dev/null && { kill -KILL "$pid" 2>/dev/null; inf "Force-killed $label"; }
    ok "$label stopped"
}

# ── Commands ──────────────────────────────────────────────────────────────────
cmd_start() {
    hdr "Starting Sovereign Nexus"

    # Server
    if is_running "$SERVER_PID" "server.py"; then
        ok "Server already running"
    else
        inf "Starting JARVIS Nexus server..."
        cd "$DIR"
        nohup "$PYTHON" server.py >> "$DIR/jarvis-nexus.out" 2>> "$DIR/jarvis-nexus.err" &
        echo $! > "$SERVER_PID"
        sleep 3
        if server_ok; then
            ok "Server started (PID $(cat $SERVER_PID)) → http://localhost:5001"
        else
            err "Server may not have started — check jarvis-nexus.err"
        fi
    fi

    # Watchdog
    if is_running "$WATCH_PID" "tts_watchdog.py"; then
        ok "Watchdog already running"
    else
        inf "Starting TTS watchdog..."
        cd "$DIR"
        nohup "$PYTHON" tts_watchdog.py --interval 30 \
            >> "$DIR/watchdog.out" 2>> "$DIR/watchdog.err" &
        echo $! > "$WATCH_PID"
        sleep 1
        ok "Watchdog started (PID $(cat $WATCH_PID))"
    fi
}

cmd_stop() {
    hdr "Stopping Sovereign Nexus"

    local spid wpid
    [[ -f "$SERVER_PID" ]] && spid=$(cat "$SERVER_PID")
    [[ -f "$WATCH_PID" ]]  && wpid=$(cat "$WATCH_PID")

    # Also find any stragglers
    [[ -z "$spid" ]] && spid=$(pgrep -f "python.*server\.py" | head -1)
    [[ -z "$wpid" ]] && wpid=$(pgrep -f "tts_watchdog" | head -1)

    kill_gracefully "$wpid" "Watchdog"
    kill_gracefully "$spid" "Server"

    rm -f "$SERVER_PID" "$WATCH_PID"
}

cmd_restart() {
    cmd_stop
    sleep 2
    cmd_start
}

cmd_status() {
    hdr "Nexus Status"

    # Server
    # server.pid is only written by this script's own `start` — the process
    # normally runs under launchd instead, so the file goes stale and a dead
    # PID in it must not block the pgrep fallback (a merely-empty check does).
    local spid=$(cat "$SERVER_PID" 2>/dev/null)
    kill -0 "$spid" 2>/dev/null || spid=$(pgrep -f "python.*server\.py" | head -1)
    if [[ -n "$spid" ]] && kill -0 "$spid" 2>/dev/null; then
        local mem=$(ps -o rss= -p "$spid" 2>/dev/null | awk '{printf "%.0fMB", $1/1024}')
        ok "Server   PID $spid  RAM $mem"
    else
        err "Server   NOT RUNNING"
    fi

    # Watchdog
    local wpid=$(cat "$WATCH_PID" 2>/dev/null)
    kill -0 "$wpid" 2>/dev/null || wpid=$(pgrep -f "tts_watchdog" | head -1)
    if [[ -n "$wpid" ]] && kill -0 "$wpid" 2>/dev/null; then
        ok "Watchdog PID $wpid"
    else
        err "Watchdog NOT RUNNING"
    fi

    # HTTP health
    if server_ok; then
        local health=$(curl -sf http://localhost:5001/api/health)
        local uptime=$(echo "$health" | /opt/homebrew/bin/python3.13 -c "import json,sys; d=json.load(sys.stdin); u=d.get('uptime',0); print(f'{u//3600}h {(u%3600)//60}m')" 2>/dev/null)
        local mem_mb=$(echo "$health" | /opt/homebrew/bin/python3.13 -c "import json,sys; d=json.load(sys.stdin); print(d.get('mem_mb','?'))" 2>/dev/null)
        ok "HTTP     http://localhost:5001  uptime=$uptime  mem=${mem_mb}MB"
    else
        err "HTTP     not responding"
    fi

    # LaunchAgent status
    echo
    inf "LaunchAgent status:"
    launchctl list com.local.jarvis-nexus   2>/dev/null | awk 'NR==1{print "  nexus:   "$0}' || inf "  nexus:   not loaded"
    launchctl list com.local.jarvis-watchdog 2>/dev/null | awk 'NR==1{print "  watchdog: "$0}' || inf "  watchdog: not loaded"

    # Asset check
    echo
    inf "Voice assets:"
    local kok_model="$DIR/assets/models/kokoro/kokoro-v1.0.int8.onnx"
    local cb_model="$DIR/assets/models/chatterbox/t3_cfg.pt"
    [[ -f "$kok_model" ]] && ok "  Kokoro ONNX model  $(du -h "$kok_model" | cut -f1)" || err "  Kokoro ONNX missing — run: provision"
    [[ -f "$cb_model"  ]] && ok "  Chatterbox model   $(du -h "$cb_model"  | cut -f1)" || err "  Chatterbox missing — run: provision"
}

cmd_logs() {
    hdr "Live Logs (Ctrl+C to exit)"
    tail -f \
        "$DIR/jarvis-nexus.out" \
        "$DIR/jarvis-nexus.err" \
        "$DIR/watchdog.out" \
        "$DIR/tts_watchdog.log" 2>/dev/null
}

cmd_provision() {
    hdr "Running Voice Asset Provisioner"
    cd "$DIR"
    "$PYTHON" provision_all_voices.py
}

cmd_install_launchagents() {
    hdr "Installing LaunchAgents"
    local agents_dir="$HOME/Library/LaunchAgents"
    mkdir -p "$agents_dir"

    # Server — plist files ship with __INSTALL_DIR__/__PYTHON__/__HOME__
    # placeholders so the same repo works wherever it's cloned.
    sed -e "s|__INSTALL_DIR__|$DIR|g" -e "s|__PYTHON__|$PYTHON|g" -e "s|__HOME__|$HOME|g" \
        "$DIR/com.local.jarvis-nexus.plist" > "$agents_dir/com.local.jarvis-nexus.plist"
    sed -e "s|__INSTALL_DIR__|$DIR|g" -e "s|__PYTHON__|$PYTHON|g" -e "s|__HOME__|$HOME|g" \
        "$DIR/com.local.jarvis-watchdog.plist" > "$agents_dir/com.local.jarvis-watchdog.plist"

    # Unload then load (idempotent)
    for label in com.local.jarvis-nexus com.local.jarvis-watchdog; do
        launchctl unload "$agents_dir/$label.plist" 2>/dev/null || true
        launchctl load   "$agents_dir/$label.plist"
        ok "Loaded $label"
    done
    ok "LaunchAgents installed — both services start at login automatically"
}

cmd_uninstall_launchagents() {
    hdr "Removing LaunchAgents"
    local agents_dir="$HOME/Library/LaunchAgents"
    for label in com.local.jarvis-nexus com.local.jarvis-watchdog; do
        launchctl unload "$agents_dir/$label.plist" 2>/dev/null && ok "Unloaded $label"
        rm -f "$agents_dir/$label.plist"
    done
}

# ── Dispatch ──────────────────────────────────────────────────────────────────
case "${1:-status}" in
    start)               cmd_start ;;
    stop)                cmd_stop ;;
    restart)             cmd_restart ;;
    status)              cmd_status ;;
    logs)                cmd_logs ;;
    provision)           cmd_provision ;;
    install-agents)      cmd_install_launchagents ;;
    uninstall-agents)    cmd_uninstall_launchagents ;;
    *)
        echo "Usage: $0 {start|stop|restart|status|logs|provision|install-agents|uninstall-agents}"
        exit 1
        ;;
esac
