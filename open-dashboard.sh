#!/usr/bin/env bash
# Waybar on-click target: open the Blue Jays dashboard.
#
# Opens http://localhost:5173 in the browser. If the Vite dev server isn't
# listening, best-effort boots the dashboard (backend + frontend) detached,
# waits briefly, then opens the browser. Logs go to /tmp/bluejays-dashboard.*.log.
set -u

DASH_DIR="$HOME/Projects/bluejays-dashboard"
URL="http://localhost:5173"

port_open() { (exec 3<>"/dev/tcp/127.0.0.1/$1") 2>/dev/null; }

if ! port_open 5173; then
    # Backend (FastAPI on :8000)
    if ! port_open 8000; then
        ( cd "$DASH_DIR/backend" \
            && nohup .venv/bin/uvicorn main:app --port 8000 \
                 >/tmp/bluejays-dashboard.backend.log 2>&1 & )
    fi
    # Frontend (Vite on :5173) — needs nvm-provided node
    ( cd "$DASH_DIR/frontend" \
        && export NVM_DIR="$HOME/.config/nvm" \
        && . "$NVM_DIR/nvm.sh" 2>/dev/null \
        && nohup npm run dev >/tmp/bluejays-dashboard.frontend.log 2>&1 & )
    # Give Vite a moment to come up
    for _ in $(seq 1 20); do port_open 5173 && break; sleep 0.5; done
fi

xdg-open "$URL" >/dev/null 2>&1 &
