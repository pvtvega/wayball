# wayball — Waybar Blue Jays live-score module

A Waybar custom module that shows live Toronto Blue Jays score info in the bar,
and a full text **scorebug** on hover with the base-out diamond, run expectancy,
**Leverage Index**, win probability, and the last few plays (RE24 / WPA).

```
 TOR 3-2 ▲7  ⚡2.1      (live — score, inning, leverage chip when tense)
 @ BAL 7:05p           (idle — next scheduled game)
```

Click the module to open the dashboard. Hover for the scorebug.

## How it works

All the hard numbers (RE24, WPA, Leverage Index, run expectancy) are computed by
the sibling [`bluejays-dashboard`](../bluejays-dashboard) backend. This module
imports that project's `get_live_winprob()` directly and runs under its
virtualenv — **no dev server needs to be running**. It polls MLB every 30s during
a game and every 10 min when idle.

`bluejays_waybar.py` prints one Waybar JSON object per line. When no Blue Jays
game is live it shows the next scheduled game; when nothing is upcoming it emits
`{}` (the module hides itself).

## Prerequisites

- The `bluejays-dashboard` backend venv exists at
  `~/Projects/bluejays-dashboard/backend/.venv` (it provides `statsapi` and the
  `services` package). First-time setup is in that project's `CLAUDE.md`.
- `ttf-font-awesome` (already used by the existing Waybar config) for the ``
  baseball glyph.

## Install

1. **Test it renders** (no live game needed — render a finished game):

   ```bash
   ~/Projects/bluejays-dashboard/backend/.venv/bin/python \
     ~/Projects/wayball/bluejays_waybar.py --once --game-id 824834
   ```

   You should get a JSON line with `text`, `tooltip`, and `class`. Run without
   `--game-id` to see the live game or the next-game idle line.

2. **Add the module** to `~/.config/waybar/config`. The config is an array of
   output blocks — for **each** block:
   - add `"custom/bluejays"` to `"modules-right"` (e.g. before `"clock"`);
   - add the module object from [`waybar/module.jsonc`](waybar/module.jsonc).

3. **Add the styles** from [`waybar/style.css`](waybar/style.css) to
   `~/.config/waybar/style.css`.

4. **Reload Waybar**:

   ```bash
   killall -SIGUSR2 waybar   # or just restart it
   ```

## Files

| File | Purpose |
|---|---|
| `bluejays_waybar.py` | The module script (long-running; one JSON line per update). |
| `open-dashboard.sh` | `on-click` target — opens localhost:5173, booting the dashboard if it's down. |
| `waybar/module.jsonc` | The `custom/bluejays` config block to merge. |
| `waybar/style.css` | The `#custom-bluejays` styles to merge. |

## CLI

```
bluejays_waybar.py [--once] [--game-id N]
  --once       print a single update and exit (default: loop forever)
  --game-id N  render a specific (e.g. finished) game — handy for testing
```

## Notes

- Thresholds and colors are copied from the dashboard's `LivePlayByPlay.tsx`
  (`LI_ELEVATED=1.5`, `LI_HIGH=2.5`, CLUTCH at |WPA| ≥ 7%, etc.) so the bar stays
  visually consistent with the web module.
- WPA in the tooltip is always shown from the Blue Jays' perspective (positive =
  TOR's win probability rose); RE24 is batting-team-relative, as in the dashboard.
- Any error emits `{}` and the loop continues, so a network blip or bad game id
  hides the module rather than crashing the bar.
