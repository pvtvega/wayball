#!/usr/bin/env python3
"""wayball — a Waybar custom module for live MLB scores with a scorebug tooltip.

Bar text (live):  ` TOR 3-2 ▲7  ⚡2.1`  — your team's score first, the inning,
and a leverage chip when the situation is tense. Hovering shows a text scorebug
with the base-out diamond, run expectancy, Leverage Index, win probability, and
the last few plays (RE24 / WPA). When no game is live it shows the next scheduled
game. Click opens MLB Gameday.

The followed team is configurable via ``--team`` or the ``WAYBALL_TEAM`` env var
(an abbreviation like ``TOR`` or a numeric id; default ``TOR``).

Output: one JSON object per line on stdout (Waybar ``return-type: json``),
flushed, then a sleep. ``{}`` hides the module. Errors never crash the bar.
RE24 / WPA / Leverage Index are computed locally — see ``re24.py`` / ``mlb.py``.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from mlb import get_live_winprob, find_next_game, resolve_team, team_abbr

# --- Display thresholds ------------------------------------------------------
LI_ELEVATED = 1.5   # show the ⚡ chip at/above this Leverage Index
LI_HIGH = 2.5       # "high" leverage
CLUTCH_WPA_MIN = 7  # |WPA| (win% pts) for a game-changing swing
QUIET_RE24_MIN = 0.6
QUIET_WPA_MAX = 1.5

# --- Palette -----------------------------------------------------------------
C_RED = "#E8291C"      # lit base
C_DIM = "#3B527F"      # empty base
C_BLUE = "#5B9BDE"     # team accent
C_MUTED = "#7B93C0"
C_POS = "#34d399"      # positive RE24/WPA (emerald)
C_NEG = "#f87171"      # negative RE24/WPA
C_AMBER = "#fbbf24"    # elevated leverage

# --- Poll cadence (seconds) --------------------------------------------------
INTERVAL_LIVE = 30
INTERVAL_IDLE = 600
INTERVAL_ERROR = 120

GLYPH = ""  # Font Awesome fa-baseball-ball (needs a Nerd Font / ttf-font-awesome)

_GAMEDAY = "https://www.mlb.com/gameday/{}"
_SCORES = "https://www.mlb.com/scores"

_state_path: Path | None = None  # where the on-click URL is written


def esc(s: object) -> str:
    """Escape text for safe inclusion in Pango markup."""
    return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def fmt_signed(v: float, decimals: int) -> str:
    f = f"{v:.{decimals}f}"
    return f"+{f}" if v > 0 else f


def signed_color(v: float | None) -> str:
    if v is None:
        return C_MUTED
    if v > 0.001:
        return C_POS
    if v < -0.001:
        return C_NEG
    return C_MUTED


def li_descriptor(li: float | None) -> tuple[str, str]:
    """(label, color) for a Leverage Index value."""
    if li is None:
        return ("—", C_MUTED)
    if li >= LI_HIGH:
        return ("high", C_NEG)
    if li >= LI_ELEVATED:
        return ("elevated", C_AMBER)
    return ("normal", C_MUTED)


def span(text: str, color: str, *, bold: bool = False) -> str:
    inner = f"<b>{text}</b>" if bold else text
    return f"<span foreground='{color}'>{inner}</span>"


# --- Scorebug pieces ---------------------------------------------------------
def diamond(bases: list[int]) -> str:
    """Three-line base diamond in monospace; lit bases red, empty dim.

    bases = [1B, 2B, 3B].
    """
    on, off = "◆", "◇"
    def b(i: int) -> str:
        return span(on, C_RED) if bases[i] else span(off, C_DIM)
    second, first, third = b(1), b(0), b(2)
    return "<tt>" f"  {second}\n" f"{third}   {first}" "</tt>"


def outs_dots(n: int) -> str:
    n = max(0, min(3, n or 0))
    return "".join(span("●", C_RED) if i < n else span("○", C_DIM) for i in range(3))


def play_tag(p: dict, team_wpa: float | None) -> str:
    """Inline CLUTCH / LOW-LEVERAGE / ⚡LI tag."""
    li = p.get("leverage")
    parts: list[str] = []
    if li is not None and li >= LI_ELEVATED:
        parts.append(span(f"⚡{li:.1f}", C_NEG if li >= LI_HIGH else C_AMBER))
    if team_wpa is not None:
        if abs(team_wpa) >= CLUTCH_WPA_MIN:
            parts.append(span("CLUTCH", C_AMBER, bold=True))
        elif abs(p.get("re24", 0)) >= QUIET_RE24_MIN and abs(team_wpa) <= QUIET_WPA_MAX:
            parts.append(span("LOW-LEV", C_MUTED))
    return (" " + " ".join(parts)) if parts else ""


def inning_arrow(half: str | None) -> str:
    return "▲" if (half or "").lower().startswith("top") else "▼"


# --- Live game ---------------------------------------------------------------
def build_live(d: dict) -> dict:
    is_home = d["is_team_home"]
    team_ab = d["home_abbr"] if is_home else d["away_abbr"]
    opp_ab = d["away_abbr"] if is_home else d["home_abbr"]
    team_score = (d["home_score"] if is_home else d["away_score"]) or 0
    opp_score = (d["away_score"] if is_home else d["home_score"]) or 0

    status = d.get("status") or ""
    inning = d.get("inning")
    in_progress = status == "In Progress" and inning is not None
    cur_li = d["plays"][0]["leverage"] if d["plays"] else None

    # --- bar text ---
    if in_progress:
        arrow = inning_arrow(d.get("inning_half"))
        text = f"{GLYPH} {team_ab} {team_score}-{opp_score} {arrow}{inning}"
        if cur_li is not None and cur_li >= LI_ELEVATED:
            text += f"  ⚡{cur_li:.1f}"
    else:
        # Warmup / Pre-Game / Final (the latter only via --game-id)
        text = f"{GLYPH} {team_ab} {team_score}-{opp_score} · {status}"

    # --- class (for CSS) ---
    classes = ["live"]
    if team_score > opp_score:
        classes.append("leading")
    elif team_score < opp_score:
        classes.append("trailing")
    else:
        classes.append("tied")
    if cur_li is not None and cur_li >= LI_HIGH:
        classes.append("high-leverage")

    # --- tooltip scorebug ---
    away_lbl = f"{d['away_abbr']} {d['away_score'] or 0}"
    home_lbl = f"{d['home_abbr']} {d['home_score'] or 0}"
    if is_home:
        home_lbl, away_lbl = f"<b>{esc(home_lbl)}</b>", esc(away_lbl)
    else:
        away_lbl, home_lbl = f"<b>{esc(away_lbl)}</b>", esc(home_lbl)

    lines: list[str] = [f"{away_lbl}   {span('@', C_MUTED)}   {home_lbl}"]

    if in_progress:
        state = d.get("inning_state") or d.get("inning_half") or ""
        lines.append(span(f"{esc(state)} {inning}", C_BLUE, bold=True))
    else:
        lines.append(span(esc(status), C_BLUE, bold=True))

    cs = d["current_state"]
    re_str = span(f"{cs['re']:.2f}", C_POS, bold=True)
    lines.append("")
    lines.append(diamond(cs["bases"]))
    lines.append(f"{outs_dots(cs['outs'])}  {span(esc(cs['label']), C_MUTED)}")
    lines.append(f"{span('Run expectancy', C_MUTED)}  {re_str}")
    if cur_li is not None:
        lbl, col = li_descriptor(cur_li)
        li_str = span(f"{cur_li:.2f}×", col, bold=True)
        lines.append(f"{span('Leverage', C_MUTED)}  {li_str} {span(f'({lbl})', C_MUTED)}")
    team_wp = d["plays"][0].get("team_win_prob") if d["plays"] else None
    if team_wp is not None:
        wp_str = span(f"{team_wp:.0f}%", C_BLUE, bold=True)
        lines.append(f"{span(f'{esc(team_ab)} win', C_MUTED)}  {wp_str}")

    # --- recent plays ---
    if d["plays"]:
        lines.append("")
        lines.append(span("Recent plays", C_MUTED, bold=True))
        for p in d["plays"][:5]:
            team_wpa = p.get("batting_wpa")
            if team_wpa is not None and not p.get("batting_is_team"):
                team_wpa = -team_wpa
            arrow = inning_arrow("top" if p["is_top_inning"] else "bottom")
            re24 = p.get("re24", 0.0)
            re_cell = span(fmt_signed(re24, 2), signed_color(re24))
            wpa_cell = (
                span(f"{fmt_signed(team_wpa, 1)}%", signed_color(team_wpa))
                if team_wpa is not None else span("—", C_MUTED)
            )
            batting_ab = team_ab if p.get("batting_is_team") else opp_ab
            ev = esc(p.get("event") or "")
            lines.append(
                f"<tt>{arrow}{p['inning']:>2}</tt> "
                f"{span(esc(batting_ab), C_BLUE if p.get('batting_is_team') else C_MUTED)} "
                f"{ev}{play_tag(p, team_wpa)}  "
                f"<tt>{re_cell} {wpa_cell}</tt>"
            )

    url = _GAMEDAY.format(d["game_id"]) if d.get("game_id") else _SCORES
    return {"text": text, "tooltip": "\n".join(lines), "class": classes, "_url": url}


# --- Idle: next scheduled game -----------------------------------------------
def build_idle(g: dict, team_id: int) -> dict:
    is_home = g["home_id"] == team_id
    opp_id = g["away_id"] if is_home else g["home_id"]
    opp = team_abbr(opp_id) or (g["away_name"] if is_home else g["home_name"])
    local = g["_dt"].astimezone()
    # 12h clock without leading zero, "p"/"a" suffix -> e.g. "7:05p"
    t = local.strftime("%-I:%M%p").lower().rstrip("m")
    vs = "vs" if is_home else "@"
    text = f"{GLYPH} {vs} {opp} {t}"

    team_pitcher = g.get("home_probable_pitcher") if is_home else g.get("away_probable_pitcher")
    opp_pitcher = g.get("away_probable_pitcher") if is_home else g.get("home_probable_pitcher")
    day = local.strftime("%a %b %-d")
    opp_name = g["home_name"] if not is_home else g["away_name"]
    tip = [
        span("Next game", C_BLUE, bold=True),
        f"{esc('vs' if is_home else 'at')} {esc(opp_name)}",
        f"{span(esc(day), C_MUTED)}  {span(local.strftime('%-I:%M %p'), C_MUTED)}",
        f"{span(esc(g.get('venue_name', '')), C_MUTED)}",
    ]
    if team_pitcher or opp_pitcher:
        tip.append("")
        tip.append(span("Probables", C_MUTED, bold=True))
        tip.append(f"{esc(team_abbr(team_id))}  {esc(team_pitcher or 'TBD')}")
        tip.append(f"{esc(opp)}  {esc(opp_pitcher or 'TBD')}")

    url = _GAMEDAY.format(g["game_id"]) if g.get("game_id") else _SCORES
    return {"text": text, "tooltip": "\n".join(tip), "class": ["idle"], "_url": url}


# --- Main loop ---------------------------------------------------------------
def update(team_id: int, game_id: int | None) -> tuple[dict, int]:
    """Return (waybar_payload, sleep_seconds). Payload may carry a private _url."""
    try:
        live = get_live_winprob(team_id, game_id)
        # A real game always has team abbreviations; guard against an empty/junk
        # feed (e.g. a bogus --game-id) so we fall through rather than render "None".
        if live and (live.get("home_abbr") or live.get("away_abbr")):
            return build_live(live), INTERVAL_LIVE
        nxt = find_next_game(team_id)
        if nxt:
            return build_idle(nxt, team_id), INTERVAL_IDLE
        return {"_url": _SCORES}, INTERVAL_IDLE
    except Exception as e:  # never crash the bar
        print(f"wayball: {e}", file=sys.stderr, flush=True)
        return {}, INTERVAL_ERROR


def write_state(payload: dict) -> None:
    """Persist the current on-click URL so gameday.sh can open it instantly."""
    if _state_path is None:
        return
    url = payload.get("_url") or _SCORES
    try:
        _state_path.write_text(url + "\n")
    except OSError:
        pass


def emit(payload: dict) -> None:
    write_state(payload)
    # Strip private keys (leading underscore) before handing JSON to Waybar.
    out = {k: v for k, v in payload.items() if not k.startswith("_")}
    print(json.dumps(out), flush=True)


def _resolve_team_arg(value: str) -> int:
    try:
        return resolve_team(value)
    except ValueError as e:
        print(f"wayball: {e}", file=sys.stderr, flush=True)
        raise SystemExit(2)


def main() -> None:
    ap = argparse.ArgumentParser(description="Waybar live MLB score module")
    ap.add_argument("--team", default=os.environ.get("WAYBALL_TEAM", "TOR"),
                    help="team abbreviation (e.g. TOR) or numeric id; "
                         "default $WAYBALL_TEAM or TOR")
    ap.add_argument("--once", action="store_true", help="print one update and exit")
    ap.add_argument("--game-id", type=int, default=None,
                    help="render a specific/finished game (testing)")
    args = ap.parse_args()

    team_id = _resolve_team_arg(args.team)

    global _state_path
    runtime = os.environ.get("XDG_RUNTIME_DIR") or "/tmp"
    _state_path = Path(runtime) / f"wayball-{team_abbr(team_id) or team_id}.url"

    if args.once:
        payload, _ = update(team_id, args.game_id)
        emit(payload)
        return

    while True:
        payload, sleep_s = update(team_id, args.game_id)
        emit(payload)
        time.sleep(sleep_s)


if __name__ == "__main__":
    main()
