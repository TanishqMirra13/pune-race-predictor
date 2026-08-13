"""Market-move (steam/drift) signal from indiarace's forecast odds.

The Indian clubs publish no live pre-race board, but indiarace prints three
successive forecast prices per runner -- Night, Morning, Opening. The LEVEL of
those prices is only indicative, but the DIRECTION between them is a real
observation: it is the closest thing the India circuit has to watching money
move. A horse shortening 8/1 -> 3/1 is being backed; one drifting 3/1 -> 8/1
is not wanted.

Why this is worth having even though the prices are only forecasts: the
backtest showed the tote favourite beating the model at every Indian venue,
i.e. the crowd knows things the model doesn't. Odds movement is the cheapest
available proxy for that crowd knowledge.

TWO IMPORTANT LIMITS, both deliberate:
1. This is NOT a market price to compute EV against -- see README. It is a
   momentum feature only. The India circuit still has no bettable pre-race
   market, and nothing here changes that.
2. It is only usable live/forward. indiarace serves the CURRENT odds page for
   a date; there is no historical archive of what the night price was, so
   backtesting this signal on past races is not possible. It therefore stays
   OUT of the tuned weight set and is surfaced to the user as context rather
   than folded into the composite score, exactly like the workout signal's
   "unbacktested" treatment but stricter.
"""

# Below this fractional change, treat the price as unchanged -- forecast
# prices are quoted coarsely and small wobbles are quoting noise, not money.
MOVE_THRESHOLD = 0.12


def movement(stages: dict | None) -> dict:
    """Summarise a runner's Night -> Morning -> Opening path.

    Returns {'direction', 'pct', 'from', 'to', 'label'} where direction is
    'shortening' (backed), 'drifting' (unwanted) or 'steady'/'unknown'.
    """
    if not stages:
        return {"direction": "unknown", "pct": None, "from": None, "to": None, "label": None}

    ordered = [(s, stages[s]) for s in ("night", "morning", "opening") if s in stages]
    if len(ordered) < 2:
        only = ordered[0][1] if ordered else None
        return {"direction": "unknown", "pct": None, "from": only, "to": only, "label": None}

    (first_stage, first), (last_stage, last) = ordered[0], ordered[-1]
    if not first or first <= 0:
        return {"direction": "unknown", "pct": None, "from": first, "to": last, "label": None}

    pct = (last - first) / first
    if pct <= -MOVE_THRESHOLD:
        direction = "shortening"
    elif pct >= MOVE_THRESHOLD:
        direction = "drifting"
    else:
        direction = "steady"

    label = None
    if direction == "shortening":
        label = f"backed {first:.1f}→{last:.1f} ({pct*100:+.0f}%)"
    elif direction == "drifting":
        label = f"drifted {first:.1f}→{last:.1f} ({pct*100:+.0f}%)"

    return {"direction": direction, "pct": pct, "from": first, "to": last, "label": label}


def movement_score(stages: dict | None) -> float:
    """[0,1], 1 = strongly backed. Neutral 0.5 when there's nothing to read.

    Provided for completeness/experimentation; NOT currently summed into the
    composite score, because it can't be backtested (see module docstring).
    """
    m = movement(stages)
    if m["direction"] in ("unknown", "steady") or m["pct"] is None:
        return 0.5
    # A 50% shortening saturates the scale.
    return max(0.0, min(1.0, 0.5 - m["pct"]))
