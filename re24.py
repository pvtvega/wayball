"""Run Expectancy (RE24) computation for play-by-play data.

MLB Stats API does not expose RE24, so we compute it from a static
league-average run-expectancy matrix. RE24 for a play is:

    RE(end base-out state) - RE(start base-out state) + runs scored

When a play produces the third out, the end-state run expectancy is 0
(the inning is over). RE24 is naturally from the batting team's
perspective: positive means the play increased the offense's expected runs.
"""
from typing import Any

# League-average run expectancy by base-out state.
# Key: (1B occupied, 2B occupied, 3B occupied) -> (0 outs, 1 out, 2 outs).
# Reference: approximate MLB league-average RE24 matrix (Tom Tango / Retrosheet,
# ~2010s).
RE_MATRIX: dict[tuple[int, int, int], tuple[float, float, float]] = {
    (0, 0, 0): (0.481, 0.254, 0.098),
    (1, 0, 0): (0.859, 0.509, 0.224),
    (0, 1, 0): (1.100, 0.664, 0.319),
    (0, 0, 1): (1.350, 0.950, 0.353),
    (1, 1, 0): (1.437, 0.884, 0.429),
    (1, 0, 1): (1.784, 1.130, 0.478),
    (0, 1, 1): (1.964, 1.376, 0.580),
    (1, 1, 1): (2.282, 1.541, 0.752),
}

_BASE_INDEX = {"1B": 0, "2B": 1, "3B": 2}

# Administrative events that don't affect the base-out state and only clutter
# the play log (Baseball Savant's WP chart omits them too).
_SKIP_EVENTS = {
    "Mound Visit", "Pitching Substitution", "Offensive Substitution",
    "Defensive Substitution", "Defensive Switch", "Game Advisory",
    "Ejection", "Pitcher Switch",
}


def run_expectancy(bases: tuple[int, int, int], outs: int) -> float:
    """Run expectancy for a base-out state. 0 once the inning is over."""
    if outs >= 3:
        return 0.0
    return RE_MATRIX[bases][outs]


def base_state_label(bases: tuple[int, int, int]) -> str:
    """Human-readable label, e.g. 'Bases empty', 'Runners on 1st & 3rd'."""
    occupied = [name for name, occ in zip(("1st", "2nd", "3rd"), bases) if occ]
    if not occupied:
        return "Bases empty"
    if len(occupied) == 3:
        return "Bases loaded"
    if len(occupied) == 1:
        return f"Runner on {occupied[0]}"
    return "Runners on " + " & ".join(occupied)


def _bases_tuple(bases: list) -> tuple[int, int, int]:
    return tuple(1 if b else 0 for b in bases)  # type: ignore[return-value]


def build_play_log(all_plays: list[dict], win_prob: list[dict],
                   is_team_home: bool) -> list[dict[str, Any]]:
    """Turn MLB play-by-play + winProbability into a play log with RE24/WPA.

    ``is_team_home`` is whether the *followed* team is the home team; it sets the
    perspective for ``team_win_prob`` and ``batting_is_team``. Returns plays in
    reverse chronological order (most recent first).
    """
    # Map atBatIndex -> win probability entry.
    wp_by_idx = {e.get("atBatIndex"): e for e in win_prob}

    plays: list[dict[str, Any]] = []
    prev_key: tuple[int, str] | None = None
    bases: list = [None, None, None]
    outs_before = 0

    for p in all_plays:
        about = p["about"]
        result = p["result"]
        key = (about["inning"], about["halfInning"])
        # New half-inning: bases clear, outs reset.
        if key != prev_key:
            bases = [None, None, None]
            outs_before = 0
            prev_key = key

        bases_before = _bases_tuple(bases)

        runs = 0
        for r in p.get("runners", []):
            mv = r["movement"]
            start, end = mv.get("start"), mv.get("end")
            if end == "score":
                runs += 1
            if start in _BASE_INDEX:
                bases[_BASE_INDEX[start]] = None
            if end in _BASE_INDEX:
                bases[_BASE_INDEX[end]] = r["details"]["runner"]["id"]

        outs_after = p["count"]["outs"]
        bases_after = _bases_tuple(bases)

        # Skip administrative events, but only after applying any runner
        # movements above so base-out continuity is preserved.
        if result.get("event") in _SKIP_EVENTS:
            outs_before = outs_after
            continue

        re_before = run_expectancy(bases_before, outs_before)
        re_after = run_expectancy(bases_after, outs_after)
        re24 = round(re_after - re_before + runs, 3)

        is_top = about["isTopInning"]
        # Batting team is away on top of the inning, home on the bottom.
        batting_is_team = is_top != is_team_home

        wp = wp_by_idx.get(about["atBatIndex"], {})
        home_wpa = wp.get("homeTeamWinProbabilityAdded")
        home_wp = wp.get("homeTeamWinProbability")
        # WPA from the batting team's perspective (aligns in sign with RE24).
        batting_wpa = None
        if home_wpa is not None:
            batting_wpa = round(home_wpa if not is_top else -home_wpa, 1)
        team_wp = None
        if home_wp is not None:
            team_wp = round(home_wp if is_team_home else 100 - home_wp, 1)

        plays.append({
            "at_bat_index": about["atBatIndex"],
            "inning": about["inning"],
            "half_inning": about["halfInning"],
            "is_top_inning": is_top,
            "batting_is_team": batting_is_team,
            "event": result.get("event"),
            "description": result.get("description"),
            "rbi": result.get("rbi", 0),
            "runs": runs,
            "is_scoring_play": about.get("isScoringPlay", False),
            "away_score": result.get("awayScore"),
            "home_score": result.get("homeScore"),
            "outs_before": outs_before,
            "outs_after": outs_after,
            "bases_before": list(bases_before),
            "bases_after": list(bases_after),
            "base_label_before": base_state_label(bases_before),
            "re_before": round(re_before, 3),
            "re_after": round(re_after, 3),
            "re24": re24,
            "batting_wpa": batting_wpa,
            "team_win_prob": team_wp,
            "leverage": wp.get("leverageIndex"),
        })

        outs_before = outs_after

    plays.reverse()
    return plays


def current_base_state(linescore: dict) -> dict[str, Any]:
    """Live base-out state and its run expectancy from the linescore."""
    offense = linescore.get("offense", {})
    bases = (
        1 if offense.get("first") else 0,
        1 if offense.get("second") else 0,
        1 if offense.get("third") else 0,
    )
    outs = linescore.get("outs") or 0
    men_on = sum(bases)
    return {
        "bases": list(bases),
        "outs": outs,
        "men_on": men_on,
        "label": base_state_label(bases),
        "re": round(run_expectancy(bases, outs), 3),
    }
