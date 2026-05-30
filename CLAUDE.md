# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A self-contained Waybar custom module that shows live MLB score info in the bar,
with a hover scorebug (base-out diamond, run expectancy, Leverage Index, win
probability, recent plays with RE24/WPA). Works for any MLB team. No server, no
API key — it polls the public MLB Stats API directly.

## Layering (where to make changes)

Three modules, strictly layered — keep them that way:

- `re24.py` — pure computation. The static `RE_MATRIX` and `build_play_log()` /
  `current_base_state()`. No I/O. Per-play and team-relative fields use the
  `*_team` / `is_team_home` naming (this is league-agnostic perspective logic;
  `build_play_log` takes `is_team_home`).
- `mlb.py` — the only place that imports `statsapi`. Team resolution
  (`resolve_team`, `team_abbr` — cached id↔abbr map), the live feed join
  (`get_live_winprob(team_id, game_id=None)`), and `find_next_game(team_id)`.
  Returns plain dicts; does no formatting.
- `wayball.py` — presentation + the Waybar loop only. Imports from `mlb`/`re24`,
  never `statsapi` directly. Builds bar text, the Pango tooltip, and the `class`
  list.

There is **no dependency on any other project** and there must not be — no
`sys.path` hacks, no imports from outside this repo.

## Output contract (Waybar)

`wayball.py` prints **one JSON object per line** to stdout, flushed, then sleeps
(`return-type: json`). Shape: `{"text", "tooltip", "class"}`.

- `text` — bar label, plain (no markup), leads with the followed team's score.
- `tooltip` — **Pango markup** (`<span foreground=...>`, `<b>`, `<tt>`, newlines);
  Waybar tooltips render Pango, not HTML/SVG. All interpolated dynamic text goes
  through `esc()`. The score/diamond art is built from `<tt>` blocks.
- `class` — list (`live`/`idle`, `leading`/`trailing`/`tied`, `high-leverage`) →
  styled via `#custom-mlb[.class]`.
- Internally, `build_*` attach a private `_url` (the on-click target); `emit()`
  writes it to the state file and **strips underscore-prefixed keys** before
  printing. `{}` (after stripping) hides the module. Never crash: `update()`
  catches everything and emits `{}`; an empty/junk feed (no team abbrs) falls
  through to the next-game path.

Poll cadence is dynamic inside the loop: `INTERVAL_LIVE=30`, `INTERVAL_IDLE=600`,
`INTERVAL_ERROR=120`.

## On-click (decoupled via a state file)

`wayball.py` writes the current Gameday URL (plain text) to
`${XDG_RUNTIME_DIR:-/tmp}/wayball-<TEAM>.url` each update. `gameday.sh <TEAM>`
just `xdg-open`s that file's contents — no Python import on click, instant.

## Team config

`--team` (CLI) ▸ `WAYBALL_TEAM` (env) ▸ default `TOR`. Accepts an abbreviation
(case-insensitive) or a numeric id, resolved via `mlb.resolve_team`.

## Running & testing

```bash
./install.sh                                   # one-time: .venv + MLB-StatsAPI

# Render a known finished game (no live game needed) — best for dev:
.venv/bin/python wayball.py --once --game-id 824834

# Other team / live-or-idle path:
.venv/bin/python wayball.py --once --team NYY
```

`--once` prints one update and exits; default loops forever (how Waybar runs it).
There is no test suite, linter, or build step — validate by eyeballing the JSON
and that the tooltip Pango is balanced (`<span>`/`<b>`/`<tt>`).

## Conventions

- Display thresholds/colors (`LI_ELEVATED`, `LI_HIGH`, `CLUTCH_WPA_MIN`, palette)
  live as constants at the top of `wayball.py`.
- Keep the personal install identical to the public repo — no private variants.
