#!/usr/bin/env bash
# wayball installer: create a local virtualenv with the one dependency, then
# print a ready-to-paste Waybar module config + CSS with absolute paths filled in.
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PY="${PYTHON:-python3}"
TEAM="${WAYBALL_TEAM:-TOR}"

echo ">> Creating virtualenv at $DIR/.venv"
"$PY" -m venv "$DIR/.venv"
"$DIR/.venv/bin/pip" install --quiet --upgrade pip
"$DIR/.venv/bin/pip" install --quiet -r "$DIR/requirements.txt"

echo ">> Verifying"
"$DIR/.venv/bin/python" -c "import statsapi; print('   MLB-StatsAPI OK')"

cat <<EOF

Done. Add wayball to Waybar:

1) Put "custom/mlb" in each bar's "modules-right" array, e.g.:
     "modules-right": [..., "custom/mlb", "clock", ...]

2) Add this module block to ~/.config/waybar/config (to each output block if
   your config has more than one):

    "custom/mlb": {
        "exec": "$DIR/.venv/bin/python $DIR/wayball.py --team $TEAM",
        "return-type": "json",
        "restart-interval": 5,
        "tooltip": true,
        "on-click": "$DIR/gameday.sh $TEAM"
    }

3) Append the styles from $DIR/waybar/style.css to ~/.config/waybar/style.css.

4) Reload Waybar:  killall -SIGUSR2 waybar

Change the team by editing --team / the gameday.sh argument (any MLB
abbreviation like NYY, LAD, BOS), or set WAYBALL_TEAM before running this script.
EOF
