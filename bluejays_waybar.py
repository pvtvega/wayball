#!/usr/bin/env python3
"""Waybar custom module: live Toronto Blue Jays score + scorebug tooltip.

Bar text (live):  ` TOR 3-2 ▲7  ⚡2.1`  — TOR-first score, inning, and a
leverage chip when the situation is tense. Hovering shows a text scorebug with
the base-out diamond, run expectancy, Leverage Index, win probability, and the
last few plays (RE24 / WPA). When no game is live it shows the next scheduled
game instead.

Data comes straight from the bluejays-dashboard backend's ``get_live_winprob``
(which already computes RE24 / WPA / Leverage Index), so this script must run
under that project's virtualenv python — see the Waybar ``exec`` in the README.
No dev server needs to be running.

Output: one JSON object per line on stdout (Waybar ``return-type: json``),
flushed, then a sleep. ``{}`` hides the module. Errors never crash the bar.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

# --- Reuse the dashboard backend (no reimplementation of RE24/WPA/leverage) ---
BACKEND_DIR = Path.home() / "Projects" / "bluejays-dashboard" / "backend"
sys.path.insert(0, str(BACKEND_DIR))

import statsapi  # noqa: E402  (provided by the backend venv)
from services.mlb_api import get_live_winprob, TOR_TEAM_ID  # noqa: E402

# --- Thresholds mirrored from frontend/src/components/LivePlayByPlay.tsx -----
LI_ELEVATED = 1.5   # show the ⚡ chip at/above this Leverage Index
LI_HIGH = 2.5       # "high" leverage
CLUTCH_WPA_MIN = 7  # |WPA| (win% pts) for a game-changing swing
QUIET_RE24_MIN = 0.6
QUIET_WPA_MAX = 1.5

# --- Palette (matches the dashboard live module) -----------------------------
C_RED = "#E8291C"      # lit base / Blue Jays red
C_DIM = "#3B527F"      # empty base
C_BLUE = "#5B9BDE"     # TOR accent
C_MUTED = "#7B93C0"
C_POS = "#34d399"      # positive RE24/WPA (emerald)
C_NEG = "#f87171"      # negative RE24/WPA
C_AMBER = "#fbbf24"    # elevated leverage

# --- Poll cadence (seconds) --------------------------------------------------
INTERVAL_LIVE = 30
INTERVAL_IDLE = 600
INTERVAL_ERROR = 120

GLYPH = ""  # Font Awesome fa-baseball-ball (ttf-font-awesome is installed)


def esc(s: object) -> str:
    """Escape text for safe inclusion in Pango markup."""
    return (
        str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    )


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


# --- Team abbreviation map (built once; live payload already has abbrs) -------
_abbr_cache: dict[int, str] | None = None


def team_abbr(team_id: int) -> str:
    global _abbr_cache
    if _abbr_cache is None:
        try:
            teams = statsapi.get("teams", {"sportId": 1}).get("teams", [])
            _abbr_cache = {t["id"]: t.get("abbreviation", "") for t in teams}
        except Exception:
            _abbr_cache = {}
    return _abbr_cache.get(team_id, "")


# --- Scorebug pieces ---------------------------------------------------------
def diamond(bases: list[int]) -> str:
    """Three-line base diamond in monospace; lit bases red, empty dim.

    bases = [1B, 2B, 3B].
    """
    on, off = "◆", "◇"
    def b(i: int) -> str:
        return span(on, C_RED) if bases[i] else span(off, C_DIM)
    second, first, third = b(1), b(0), b(2)
    return (
        "<tt>"
        f"  {second}\n"
        f"{third}   {first}"
        "</tt>"
    )


def outs_dots(n: int) -> str:
    n = max(0, min(3, n or 0))
    return "".join(span("●", C_RED) if i < n else span("○", C_DIM) for i in range(3))


def play_tag(p: dict, tor_wpa: float | None) -> str:
    """Inline CLUTCH / LOW-LEVERAGE / ⚡LI tag, mirroring the dashboard."""
    li = p.get("leverage")
    parts: list[str] = []
    if li is not None and li >= LI_ELEVATED:
        parts.append(span(f"⚡{li:.1f}", C_NEG if li >= LI_HIGH else C_AMBER))
    if tor_wpa is not None:
        if abs(tor_wpa) >= CLUTCH_WPA_MIN:
            parts.append(span("CLUTCH", C_AMBER, bold=True))
        elif abs(p.get("re24", 0)) >= QUIET_RE24_MIN and abs(tor_wpa) <= QUIET_WPA_MAX:
            parts.append(span("LOW-LEV", C_MUTED))
    return (" " + " ".join(parts)) if parts else ""


def inning_arrow(half: str | None) -> str:
    return "▲" if (half or "").lower().startswith("top") else "▼"


# --- Live game ---------------------------------------------------------------
def build_live(d: dict) -> dict:
    is_home = d["is_tor_home"]
    tor_abbr = d["home_abbr"] if is_home else d["away_abbr"]
    opp_abbr = d["away_abbr"] if is_home else d["home_abbr"]
    tor_score = (d["home_score"] if is_home else d["away_score"]) or 0
    opp_score = (d["away_score"] if is_home else d["home_score"]) or 0

    status = d.get("status") or ""
    inning = d.get("inning")
    in_progress = status == "In Progress" and inning is not None
    cur_li = d["plays"][0]["leverage"] if d["plays"] else None

    # --- bar text ---
    if in_progress:
        arrow = inning_arrow(d.get("inning_half"))
        text = f"{GLYPH} {tor_abbr} {tor_score}-{opp_score} {arrow}{inning}"
        if cur_li is not None and cur_li >= LI_ELEVATED:
            text += f"  ⚡{cur_li:.1f}"
    else:
        # Warmup / Pre-Game / Final (the latter only via --game-id)
        text = f"{GLYPH} {tor_abbr} {tor_score}-{opp_score} · {status}"

    # --- class (for CSS) ---
    classes = ["live"]
    if tor_score > opp_score:
        classes.append("leading")
    elif tor_score < opp_score:
        classes.append("trailing")
    else:
        classes.append("tied")
    if cur_li is not None and cur_li >= LI_HIGH:
        classes.append("high-leverage")

    # --- tooltip scorebug ---
    away_lbl = f"{d['away_abbr']} {d['away_score'] or 0}"
    home_lbl = f"{d['home_abbr']} {d['home_score'] or 0}"
    # bold the Blue Jays' side
    if is_home:
        home_lbl = f"<b>{esc(home_lbl)}</b>"
        away_lbl = esc(away_lbl)
    else:
        away_lbl = f"<b>{esc(away_lbl)}</b>"
        home_lbl = esc(home_lbl)

    lines: list[str] = []
    lines.append(f"{away_lbl}   {span('@', C_MUTED)}   {home_lbl}")

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
    tor_wp = d["plays"][0].get("tor_win_prob") if d["plays"] else None
    if tor_wp is not None:
        wp_str = span(f"{tor_wp:.0f}%", C_BLUE, bold=True)
        lines.append(f"{span('TOR win', C_MUTED)}  {wp_str}")

    # --- recent plays ---
    if d["plays"]:
        lines.append("")
        lines.append(span("Recent plays", C_MUTED, bold=True))
        for p in d["plays"][:5]:
            tor_wpa = p.get("batting_wpa")
            if tor_wpa is not None and not p.get("batting_is_tor"):
                tor_wpa = -tor_wpa
            arrow = inning_arrow("top" if p["is_top_inning"] else "bottom")
            re24 = p.get("re24", 0.0)
            re_str = span(fmt_signed(re24, 2), signed_color(re24))
            wpa_str = (
                span(f"{fmt_signed(tor_wpa, 1)}%", signed_color(tor_wpa))
                if tor_wpa is not None else span("—", C_MUTED)
            )
            batting = p.get("batting_is_tor") and tor_abbr or opp_abbr
            ev = esc(p.get("event") or "")
            lines.append(
                f"<tt>{arrow}{p['inning']:>2}</tt> "
                f"{span(esc(batting), C_BLUE if p.get('batting_is_tor') else C_MUTED)} "
                f"{ev}{play_tag(p, tor_wpa)}  "
                f"<tt>{re_str} {wpa_str}</tt>"
            )

    return {"text": text, "tooltip": "\n".join(lines), "class": classes}


# --- Idle: next scheduled game -----------------------------------------------
def find_next_game() -> dict | None:
    from datetime import date, timedelta
    today = date.today()
    try:
        games = statsapi.schedule(
            team=TOR_TEAM_ID,
            start_date=today.strftime("%Y-%m-%d"),
            end_date=(today + timedelta(days=21)).strftime("%Y-%m-%d"),
        )
    except Exception:
        return None
    now = datetime.now(timezone.utc)
    upcoming = []
    for g in games:
        if g.get("status") in ("Final", "Game Over", "Completed Early"):
            continue
        dt = _parse_dt(g.get("game_datetime"))
        if dt is None or dt < now:
            continue
        upcoming.append((dt, g))
    if not upcoming:
        return None
    upcoming.sort(key=lambda x: x[0])
    dt, g = upcoming[0]
    return {**g, "_dt": dt}


def _parse_dt(s: str | None) -> datetime | None:
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None


def build_idle(g: dict) -> dict:
    is_home = g["home_id"] == TOR_TEAM_ID
    opp_id = g["away_id"] if is_home else g["home_id"]
    opp = team_abbr(opp_id) or (g["away_name"] if is_home else g["home_name"])
    local = g["_dt"].astimezone()
    # 12h clock without leading zero, lowercase am/pm -> e.g. "7:05p"
    t = local.strftime("%-I:%M%p").lower().rstrip("m")
    vs = "vs" if is_home else "@"
    text = f"{GLYPH} {vs} {opp} {t}"

    tor_pitcher = g.get("home_probable_pitcher") if is_home else g.get("away_probable_pitcher")
    opp_pitcher = g.get("away_probable_pitcher") if is_home else g.get("home_probable_pitcher")
    day = local.strftime("%a %b %-d")
    tip = [
        span("Next Blue Jays game", C_BLUE, bold=True),
        f"{esc('vs' if is_home else 'at')} {esc(g['home_name'] if not is_home else g['away_name'])}",
        f"{span(esc(day), C_MUTED)}  {span(local.strftime('%-I:%M %p'), C_MUTED)}",
        f"{span(esc(g.get('venue_name', '')), C_MUTED)}",
    ]
    if tor_pitcher or opp_pitcher:
        tip.append("")
        tip.append(span("Probables", C_MUTED, bold=True))
        tip.append(f"TOR  {esc(tor_pitcher or 'TBD')}")
        tip.append(f"{esc(opp)}  {esc(opp_pitcher or 'TBD')}")
    return {"text": text, "tooltip": "\n".join(tip), "class": ["idle"]}


# --- Main loop ---------------------------------------------------------------
def update(game_id: int | None) -> tuple[dict, int]:
    """Return (waybar_payload, sleep_seconds)."""
    try:
        live = get_live_winprob(game_id) if game_id else get_live_winprob()
        # A real game always has team abbreviations; guard against an empty/junk
        # feed (e.g. a bogus --game-id) so we fall through rather than render "None".
        if live and (live.get("home_abbr") or live.get("away_abbr")):
            return build_live(live), INTERVAL_LIVE
        nxt = find_next_game()
        if nxt:
            return build_idle(nxt), INTERVAL_IDLE
        return {}, INTERVAL_IDLE
    except Exception as e:  # never crash the bar
        print(f"bluejays_waybar: {e}", file=sys.stderr, flush=True)
        return {}, INTERVAL_ERROR


def emit(payload: dict) -> None:
    print(json.dumps(payload), flush=True)


def main() -> None:
    ap = argparse.ArgumentParser(description="Waybar Blue Jays live-score module")
    ap.add_argument("--once", action="store_true", help="print one update and exit (testing)")
    ap.add_argument("--game-id", type=int, default=None, help="render a specific/finished game")
    args = ap.parse_args()

    if args.once:
        payload, _ = update(args.game_id)
        emit(payload)
        return

    while True:
        payload, sleep_s = update(args.game_id)
        emit(payload)
        time.sleep(sleep_s)


if __name__ == "__main__":
    main()
