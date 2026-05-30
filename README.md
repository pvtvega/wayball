# wayball

A [Waybar](https://github.com/Alexays/Waybar) custom module for **live MLB
scores** with an analytical hover **scorebug** — base-out diamond, run
expectancy, **Leverage Index**, win probability, and the last few plays with
**RE24** and **WPA**. Works for any MLB team. Click to open MLB Gameday.

```
 TOR 3-2 ▲7  ⚡2.1      live — your team first, inning, leverage chip when tense
 @ BAL 7:05p           idle — next scheduled game
```

Hover for the full scorebug:

```
TOR 3   @   BAL 2
Bottom 7
   ◆
◇   ◆
●●○  Runners on 1st & 3rd
Run expectancy  1.21
Leverage  2.10× (elevated)
TOR win  64%

Recent plays
▼ 7 TOR Single ⚡2.1  +0.43 +6.2%
...
```

## How it works

wayball polls the **public MLB Stats API** directly (via the
[`MLB-StatsAPI`](https://pypi.org/project/MLB-StatsAPI/) package) — no server,
no API key. For an in-progress game it joins the play-by-play, the
win-probability feed (win prob + Leverage Index), and the live linescore, then
computes **RE24** locally from a static league-average run-expectancy matrix
(Tom Tango / Retrosheet). It prints one Waybar JSON line per update — every 30s
during a game, every 10 min when idle — and emits nothing (hides) when there's no
game and nothing scheduled.

WPA is shown from your team's perspective (positive = your win probability rose);
RE24 is batting-team-relative.

## Prerequisites

- Python 3.10+
- Waybar
- A **Nerd Font** or `ttf-font-awesome` for the `` baseball glyph (or remove
  `GLYPH` in `wayball.py`)

## Install

```bash
git clone https://github.com/<you>/wayball
cd wayball
WAYBALL_TEAM=TOR ./install.sh     # creates .venv, installs the one dependency
```

`install.sh` prints a ready-to-paste Waybar module block and `on-click` with the
absolute paths already filled in. Add `"custom/mlb"` to your bar's
`modules-right`, paste the block into `~/.config/waybar/config` (into **each**
output block if your config has more than one), append `waybar/style.css` to
`~/.config/waybar/style.css`, then reload:

```bash
killall -SIGUSR2 waybar
```

## Choosing a team

Set it via `--team` (in the Waybar `exec`) or the `WAYBALL_TEAM` env var — any
MLB abbreviation (`TOR`, `NYY`, `LAD`, `BOS`, …) or a numeric team id. Default
is `TOR`. Pass the same abbreviation to `gameday.sh` in `on-click` so the click
handler finds the right state file.

To match the pill color to your team, edit the `#custom-mlb` background in
`waybar/style.css`.

## CLI

```
wayball.py [--team ABBR|ID] [--once] [--game-id N]
  --team       team to follow (abbr or id); default $WAYBALL_TEAM or TOR
  --once       print a single update and exit (default: loop forever)
  --game-id N  render a specific (e.g. finished) game — handy for testing
```

```bash
# Render a finished game without waiting for one to be live:
.venv/bin/python wayball.py --once --team TOR --game-id 824834
```

## Files

| File | Purpose |
|---|---|
| `wayball.py` | The module — formats the Waybar JSON; long-running loop. |
| `mlb.py` | MLB Stats API access (team resolution, live winprob, next game). |
| `re24.py` | Local RE24 / WPA / leverage computation from the play-by-play. |
| `gameday.sh` | `on-click` — opens the current game's MLB Gameday page. |
| `install.sh` | Creates the venv and prints the Waybar config snippet. |
| `waybar/` | The `custom/mlb` config + `#custom-mlb` style snippets. |

## Notes

- Any error emits `{}` and the loop continues, so a network blip hides the module
  rather than crashing the bar.
- Not affiliated with or endorsed by MLB. Uses publicly available MLB Stats API
  data; see MLB's terms regarding use of their content.

## License

MIT — see [LICENSE](LICENSE).
