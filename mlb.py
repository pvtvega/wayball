"""MLB Stats API access for wayball.

Thin wrapper around the ``MLB-StatsAPI`` package (`pip install MLB-StatsAPI`,
imports as ``statsapi``). Everything here is team-generic — the followed team is
passed in as an id. RE24 / WPA / Leverage Index are computed locally in
``re24.py`` from the public play-by-play + win-probability feeds.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any

import statsapi

from re24 import build_play_log, current_base_state

_LIVE_STATUSES = ("In Progress", "Warmup", "Pre-Game")
_FINAL_STATUSES = ("Final", "Game Over", "Completed Early")

# Cached id<->abbreviation map for all MLB teams (built once, lazily).
_abbr_by_id: dict[int, str] | None = None
_id_by_abbr: dict[str, int] | None = None


def _load_teams() -> None:
    global _abbr_by_id, _id_by_abbr
    if _abbr_by_id is not None:
        return
    teams = statsapi.get("teams", {"sportId": 1}).get("teams", [])
    _abbr_by_id = {t["id"]: t.get("abbreviation", "") for t in teams}
    _id_by_abbr = {v.upper(): k for k, v in _abbr_by_id.items() if v}


def team_abbr(team_id: int) -> str:
    """Abbreviation (e.g. 'TOR') for a team id, or '' if unknown."""
    _load_teams()
    assert _abbr_by_id is not None
    return _abbr_by_id.get(team_id, "")


def resolve_team(value: str | int) -> int:
    """Resolve a team abbreviation ('TOR', case-insensitive) or numeric id to an id.

    Raises ValueError with a helpful message if it can't be resolved.
    """
    if isinstance(value, int) or str(value).isdigit():
        tid = int(value)
        _load_teams()
        assert _abbr_by_id is not None
        if tid not in _abbr_by_id:
            raise ValueError(f"Unknown MLB team id {tid}")
        return tid
    _load_teams()
    assert _id_by_abbr is not None
    key = str(value).strip().upper()
    if key not in _id_by_abbr:
        valid = ", ".join(sorted(_id_by_abbr))
        raise ValueError(f"Unknown MLB team '{value}'. Valid abbreviations: {valid}")
    return _id_by_abbr[key]


def _find_live_game(team_id: int) -> tuple[int, bool] | None:
    """Return (game_id, is_team_home) for today's in-progress/upcoming game."""
    today = date.today().strftime("%Y-%m-%d")
    games = statsapi.schedule(team=team_id, start_date=today, end_date=today)
    live = [g for g in games if g["status"] in _LIVE_STATUSES]
    if not live:
        return None
    g = live[0]
    return g["game_id"], g["home_id"] == team_id


def get_live_winprob(team_id: int, game_id: int | None = None) -> dict | None:
    """Play-by-play log with RE24, WPA, and the live base-out state.

    Uses today's in-progress game for ``team_id`` unless an explicit ``game_id``
    is supplied (useful for inspecting a finished game). Returns ``None`` when
    there is nothing to show. Win probability and the play log are from the
    followed team's perspective.
    """
    is_team_home: bool | None = None
    if game_id is None:
        found = _find_live_game(team_id)
        if not found:
            return None
        game_id, is_team_home = found

    try:
        feed = statsapi.get("game", {"gamePk": game_id, "hydrate": "linescore"})
    except Exception:
        return None

    game_data = feed.get("gameData", {})
    live_data = feed.get("liveData", {})
    teams = game_data.get("teams", {})
    if is_team_home is None:
        is_team_home = teams.get("home", {}).get("id") == team_id

    linescore = live_data.get("linescore", {})
    all_plays = live_data.get("plays", {}).get("allPlays", [])

    try:
        win_prob = statsapi.get("game_winProbability", {"gamePk": game_id})
        if not isinstance(win_prob, list):
            win_prob = []
    except Exception:
        win_prob = []

    plays = build_play_log(all_plays, win_prob, is_team_home)

    ls_teams = linescore.get("teams", {})
    return {
        "game_id": game_id,
        "status": game_data.get("status", {}).get("detailedState"),
        "inning": linescore.get("currentInning"),
        "inning_half": linescore.get("inningHalf"),
        "inning_state": linescore.get("inningState"),
        "home_team": teams.get("home", {}).get("name"),
        "away_team": teams.get("away", {}).get("name"),
        "home_abbr": teams.get("home", {}).get("abbreviation"),
        "away_abbr": teams.get("away", {}).get("abbreviation"),
        "home_score": ls_teams.get("home", {}).get("runs"),
        "away_score": ls_teams.get("away", {}).get("runs"),
        "is_team_home": is_team_home,
        "current_state": current_base_state(linescore),
        "plays": plays,
    }


def find_next_game(team_id: int, days_ahead: int = 21) -> dict | None:
    """The soonest upcoming (non-final, future) game for ``team_id``.

    Returns the statsapi schedule dict with an added ``_dt`` (timezone-aware UTC
    datetime), or ``None`` if nothing is scheduled in the window.
    """
    today = date.today()
    try:
        games = statsapi.schedule(
            team=team_id,
            start_date=today.strftime("%Y-%m-%d"),
            end_date=(today + timedelta(days=days_ahead)).strftime("%Y-%m-%d"),
        )
    except Exception:
        return None
    now = datetime.now(timezone.utc)
    upcoming: list[tuple[datetime, dict]] = []
    for g in games:
        if g.get("status") in _FINAL_STATUSES:
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
