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

# --- Palette -----------------------------------------------------------------
C_RED = "#E8291C"      # lit base
C_DIM = "#3B527F"      # empty base
C_BLUE = "#5B9BDE"     # team accent
C_MUTED = "#7B93C0"
C_POS = "#34d399"      # positive RE24/WPA (emerald)
C_NEG = "#f87171"      # negative RE24/WPA
C_AMBER = "#fbbf24"    # elevated leverage
C_MAUVE = "#cba6f7"    # section accent (play log)

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


# --- btop-style box drawing --------------------------------------------------
# The tooltip renders as one <tt> block: a rounded-corner box with lowercase
# section titles embedded in the rules and block meters for the live numbers.
# Pango markup length != visible width, so every piece is a (markup, width)
# Cell and the Box sizes/pads itself at render time.
BOX_MIN_W = 26   # minimum inner width (columns between the side borders)
METER_W = 14     # meter width in cells
EV_W = 17        # event-name column width in the play log

Cell = tuple[str, int]  # (pango markup, visible width)

_METER_PARTIAL = ["", "▏", "▎", "▍", "▌", "▋", "▊", "▉"]


def cell(text: str, color: str | None = None, *, bold: bool = False) -> Cell:
    m = f"<b>{esc(text)}</b>" if bold else esc(text)
    if color:
        m = f"<span foreground='{color}'>{m}</span>"
    return m, len(text)


def _hex_lerp(a: str, b: str, t: float) -> str:
    av = [int(a[i:i + 2], 16) for i in (1, 3, 5)]
    bv = [int(b[i:i + 2], 16) for i in (1, 3, 5)]
    return "#" + "".join(f"{round(x + (y - x) * t):02x}" for x, y in zip(av, bv))


def ramp(t: float, *, reverse: bool = False) -> str:
    """btop-style green→amber→red colour ramp; reverse for red→amber→green."""
    t = max(0.0, min(1.0, t))
    if reverse:
        t = 1.0 - t
    if t < 0.5:
        return _hex_lerp(C_POS, C_AMBER, t * 2)
    return _hex_lerp(C_AMBER, C_NEG, t * 2 - 1)


def meter(frac: float, width: int = METER_W, *, reverse: bool = False) -> Cell:
    """Gradient block meter (1/8-cell resolution): each filled cell is coloured
    along the ramp by its position, btop-style; the unfilled tail is dim."""
    eighths = round(max(0.0, min(1.0, frac)) * width * 8)
    full, rem = divmod(eighths, 8)
    cells = []
    for i in range(width):
        if i < full:
            cells.append(span("█", ramp(i / (width - 1), reverse=reverse)))
        elif i == full and rem:
            cells.append(span(_METER_PARTIAL[rem], ramp(i / (width - 1), reverse=reverse)))
        else:
            cells.append(span("░", C_DIM))
    return "".join(cells), width


class Box:
    """Collects rows of Cells, then renders a bordered box sized to fit."""

    def __init__(self, title: str):
        self._title = title
        self._rows: list[tuple[str, object, object]] = []
        self._hint: str | None = None

    def row(self, *parts: Cell) -> None:
        self._rows.append(("row", list(parts), None))

    def lrow(self, left: list[Cell], right: list[Cell]) -> None:
        """A row whose `right` cells are flushed to the right border."""
        self._rows.append(("row", left, right))

    def sep(self, title: str, color: str = C_MUTED) -> None:
        self._rows.append(("sep", title, color))

    def hint(self, text: str) -> None:
        self._hint = text

    @staticmethod
    def _vis(parts: list[Cell]) -> int:
        return sum(w for _, w in parts)

    @staticmethod
    def _rule(left: str, right: str, w: int, label: str = "",
              color: str = C_MUTED) -> str:
        if not label:
            return span(left + "─" * (w + 2) + right, C_DIM)
        fill = max(0, w - 1 - len(label))
        return (span(f"{left}─ ", C_DIM) + span(esc(label), color, bold=True)
                + span(" " + "─" * fill + right, C_DIM))

    def render(self) -> str:
        w = max(BOX_MIN_W, len(self._title) + 4, len(self._hint or "") + 4)
        for kind, a, b in self._rows:
            if kind == "sep":
                w = max(w, len(a) + 4)
            else:
                w = max(w, self._vis(a) + (self._vis(b) + 2 if b else 0))
        out = [self._rule("╭", "╮", w, self._title, C_BLUE)]
        for kind, a, b in self._rows:
            if kind == "sep":
                out.append(self._rule("├", "┤", w, a, b))
                continue
            markup, vis = "".join(m for m, _ in a), self._vis(a)
            if b:
                markup += " " * (w - vis - self._vis(b)) + "".join(m for m, _ in b)
                vis = w
            out.append(span("│ ", C_DIM) + markup + " " * (w - vis) + span(" │", C_DIM))
        out.append(self._rule("╰", "╯", w, self._hint or "", C_MUTED))
        return "\n".join(out)


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

    # --- tooltip: btop-style boxed scorebug ---
    box = Box(f"{d['away_abbr']} @ {d['home_abbr']}")

    away_bat = in_progress and (d.get("inning_half") or "").lower().startswith("top")

    def score_row(abbr: str, score: int, followed: bool, batting: bool) -> list[Cell]:
        col = C_BLUE if followed else None
        return [cell("▸ ", C_AMBER) if batting else cell("  "),
                cell(f"{abbr:<4}", col, bold=followed),
                cell(f"{score:>2}", col, bold=followed)]

    if in_progress:
        half = (d.get("inning_state") or d.get("inning_half") or "")[:3].lower()
        st = cell(f"{half} {inning}", C_BLUE, bold=True)
    else:
        st = cell(status.lower(), C_BLUE, bold=True)
    box.lrow(score_row(d["away_abbr"], d["away_score"] or 0, not is_home, away_bat),
             [st] if (away_bat or not in_progress) else [])
    box.lrow(score_row(d["home_abbr"], d["home_score"] or 0, is_home,
                       in_progress and not away_bat),
             [st] if (in_progress and not away_bat) else [])

    # field: base diamond + outs
    cs = d["current_state"]
    def base(i: int) -> Cell:
        return (span("◆", C_RED) if cs["bases"][i] else span("◇", C_DIM), 1)
    outs = max(0, min(3, cs["outs"] or 0))
    dots: Cell = ("".join(span("●", C_RED) if i < outs else span("○", C_DIM)
                          for i in range(3)), 3)
    box.sep("field", C_POS)
    box.row(cell("   "), base(1), cell("       "), dots,
            cell(f"  {outs} out", C_MUTED))
    box.row(cell(" "), base(2), cell("   "), base(0), cell("     "),
            cell(cs["label"].lower(), C_MUTED))

    # situation: run expectancy / leverage / win probability
    box.sep("situation", C_AMBER)
    box.row(cell(f"{'re':<5}", C_MUTED), cell(f"{cs['re']:>5.2f}", C_POS, bold=True))
    if cur_li is not None:
        lbl, lcol = li_descriptor(cur_li)
        lfrac = cur_li / 3.0
        box.row(cell(f"{'lev':<5}", C_MUTED),
                cell(f"{cur_li:>5.2f}", ramp(lfrac), bold=True),
                cell("  "), meter(lfrac), cell(f"  {lbl}", lcol))
    team_wp = d["plays"][0].get("team_win_prob") if d["plays"] else None
    if team_wp is not None:
        wfrac = team_wp / 100.0
        box.row(cell(f"{team_ab.lower():<5}", C_MUTED),
                cell(f"{team_wp:>4.0f}%", ramp(wfrac, reverse=True), bold=True),
                cell("  "), meter(wfrac, reverse=True))

    # play log
    if d["plays"]:
        box.sep("last plays", C_MAUVE)
        box.row(cell(f"{'':{8 + EV_W + 1}}{'re24':>6} {'wpa':>6}", C_MUTED))
        for p in d["plays"][:5]:
            team_wpa = p.get("batting_wpa")
            if team_wpa is not None and not p.get("batting_is_team"):
                team_wpa = -team_wpa
            arrow = inning_arrow("top" if p["is_top_inning"] else "bottom")
            li = p.get("leverage")
            pcol = (C_NEG if li is not None and li >= LI_HIGH else
                    C_AMBER if li is not None and li >= LI_ELEVATED else C_MUTED)
            clutch = team_wpa is not None and abs(team_wpa) >= CLUTCH_WPA_MIN
            ev = p.get("event") or ""
            if len(ev) > EV_W:
                ev = ev[:EV_W - 1] + "…"
            re24 = p.get("re24", 0.0)
            wpa_txt = f"{fmt_signed(team_wpa, 1)}%" if team_wpa is not None else "—"
            batting_ab = team_ab if p.get("batting_is_team") else opp_ab
            box.row(
                cell(f"{arrow}{p['inning']:>2}", pcol), cell(" "),
                cell(f"{batting_ab:<3}", C_BLUE if p.get("batting_is_team") else C_MUTED),
                cell(" "),
                cell(f"{ev:<{EV_W}}", C_AMBER if clutch else None, bold=clutch),
                cell(" "),
                cell(f"{fmt_signed(re24, 2):>6}", signed_color(re24)), cell(" "),
                cell(f"{wpa_txt:>6}", signed_color(team_wpa)),
            )

    box.hint("click ⇒ gameday")
    url = _GAMEDAY.format(d["game_id"]) if d.get("game_id") else _SCORES
    return {"text": text, "tooltip": f"<tt>{box.render()}</tt>",
            "class": classes, "_url": url}


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
    day = local.strftime("%a %b %-d").lower()
    opp_name = g["home_name"] if not is_home else g["away_name"]

    box = Box("next game")
    box.row(cell(f"{'vs' if is_home else 'at'} ", C_MUTED), cell(opp_name, bold=True))
    box.row(cell(f"{day} · {local.strftime('%-I:%M %p').lower()}", C_MUTED))
    if g.get("venue_name"):
        box.row(cell(g["venue_name"], C_MUTED))
    if team_pitcher or opp_pitcher:
        box.sep("probables", C_AMBER)
        box.row(cell(f"{team_abbr(team_id) or '':<4}", C_BLUE, bold=True),
                cell(team_pitcher or "TBD"))
        box.row(cell(f"{opp:<4}", C_MUTED), cell(opp_pitcher or "TBD"))
    box.hint("click ⇒ gameday")

    url = _GAMEDAY.format(g["game_id"]) if g.get("game_id") else _SCORES
    return {"text": text, "tooltip": f"<tt>{box.render()}</tt>",
            "class": ["idle"], "_url": url}


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
