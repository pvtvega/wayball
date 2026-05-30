#!/usr/bin/env bash
# wayball on-click: open the current game's MLB Gameday page in the browser.
#
# wayball.py writes the target URL to a per-team state file each update; this
# just opens it (falling back to mlb.com/scores). Pass the team abbreviation as
# $1 (or set WAYBALL_TEAM) so it reads the right file.
team="${1:-${WAYBALL_TEAM:-TOR}}"
state="${XDG_RUNTIME_DIR:-/tmp}/wayball-${team}.url"
url="$(cat "$state" 2>/dev/null)"
xdg-open "${url:-https://www.mlb.com/scores}" >/dev/null 2>&1 &
