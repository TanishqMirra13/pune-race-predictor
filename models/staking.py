"""Budget/goal -> staking plan, for the price-free view of a card.

Important honesty note: no Indian club publishes a live pre-race board, so
these functions cannot compute a true odds-based expected value or Kelly
stake. They stake on *edge over a random pick* (win_probability vs.
1/field_size), tiered and capped, and skip races where no horse clears a
minimum edge. That is a deliberate feature rather than a gap: not betting a
weak race is part of the discipline. Where a price does exist -- the indiarace
forecast, or one you pasted -- models/parlay.py does the real expected-value
work instead, and this module is the fallback for everything else.

The jackpot planner that used to live here has moved to models/jackpot.py.
It was rebuilt rather than relocated: this version ranked legs by the model's
own probability and spread the budget evenly, and replayed over the archive
both choices cost most of the hit rate. See that module's docstring for the
measurements.
"""

MIN_EDGE_TO_BET = 0.05          # below this, skip the race entirely
EDGE_TIERS = [                   # (min_edge, stake weight multiplier)
    (0.20, 3.0),
    (0.12, 2.0),
    (0.05, 1.0),
]
MAX_STAKE_FRACTION_PER_BET = 0.15   # never stake more than this share of the day's budget on one bet


def _edge_tier_weight(edge: float) -> float:
    for threshold, weight in EDGE_TIERS:
        if edge >= threshold:
            return weight
    return 0.0


def build_win_place_plan(budget: float, race_plans: list[dict]) -> dict:
    """race_plans: [{'race_id', 'race_no', 'entries': [...from rating_engine...]}]
    Returns a plan with per-race stake suggestions, respecting the budget cap."""
    candidates = []
    skipped_races = []
    for rp in race_plans:
        entries = rp["entries"]
        if not entries:
            continue
        field_size = len(entries)
        baseline = 1.0 / field_size
        top = entries[0]
        edge = top["win_probability"] - baseline
        if edge < MIN_EDGE_TO_BET:
            skipped_races.append({"race_no": rp["race_no"], "reason": "no horse clears minimum edge over a random pick"})
            continue
        weight = _edge_tier_weight(edge)
        candidates.append({
            "race_id": rp["race_id"],
            "race_no": rp["race_no"],
            "horse_name": top["horse_name"],
            "win_probability": top["win_probability"],
            "edge": edge,
            "weight": weight,
            "reasoning": top["reasoning"],
            "confidence": top["confidence"],
        })

    if not candidates:
        return {"bets": [], "skipped_races": skipped_races, "total_staked": 0.0,
                "unallocated": budget, "note": "No race today cleared the minimum edge threshold -- sitting out is the correct call, not a failure of the tool."}

    total_weight = sum(c["weight"] for c in candidates)
    per_bet_cap = budget * MAX_STAKE_FRACTION_PER_BET
    bets = []
    for c in candidates:
        raw_stake = budget * (c["weight"] / total_weight)
        stake = min(raw_stake, per_bet_cap)
        bets.append({**c, "stake": round(stake, 2), "bet_type": "win"})

    total_staked = sum(b["stake"] for b in bets)
    if total_staked > budget:  # cap rounding safety
        scale = budget / total_staked
        for b in bets:
            b["stake"] = round(b["stake"] * scale, 2)
        total_staked = sum(b["stake"] for b in bets)

    return {
        "bets": bets,
        "skipped_races": skipped_races,
        "total_staked": round(total_staked, 2),
        "unallocated": round(budget - total_staked, 2),
    }


def harville_forecast_probabilities(win_probs: dict, top_n: int = 3) -> list[dict]:
    """Harville formula: P(A 1st, B 2nd) = p_A * p_B / (1 - p_A).
    win_probs: {horse_id: (horse_name, probability)}. Returns top_n most likely ordered pairs."""
    items = list(win_probs.items())
    pairs = []
    for hid_a, (name_a, p_a) in items:
        if p_a >= 1.0:
            continue
        for hid_b, (name_b, p_b) in items:
            if hid_a == hid_b:
                continue
            p_second_given_first = p_b / (1 - p_a)
            pairs.append({
                "first": name_a, "second": name_b,
                "probability": p_a * p_second_given_first,
            })
    pairs.sort(key=lambda x: x["probability"], reverse=True)
    return pairs[:top_n]


def places_paid_for_field(field_size: int) -> int:
    """Tote places paid, verified empirically against RWITC dividend data:
    8+ runners pay 3, 5-7 pay 2, 4 or fewer have no place pool (win only).
    Field size = declared (non-scratched) runners on the card."""
    if field_size >= 8:
        return 3
    if field_size >= 5:
        return 2
    return 0


def place_probabilities(win_probs: dict, places_paid: int) -> dict:
    """Probability each horse finishes within the paid places, derived from the
    model's WIN probabilities via the Harville order-statistics model:
      P(top1)=p_i; P(top2)=p_i + Σ_j p_j·p_i/(1-p_j);
      P(top3)=P(top2) + Σ_{j≠k} p_j·(p_k/(1-p_j))·(p_i/(1-p_j-p_k)).
    Returns {horse_id: place_probability}. This is our own estimate -- the tote
    doesn't publish pre-race place odds, so compare it to the board yourself."""
    items = list(win_probs.items())
    result = {}
    for hid_i, (_, p_i) in items:
        prob = p_i  # finishing 1st always counts as placing
        if places_paid >= 2:
            for hid_j, (_, p_j) in items:
                if hid_j == hid_i or p_j >= 1.0:
                    continue
                prob += p_j * (p_i / (1 - p_j))
        if places_paid >= 3:
            for hid_j, (_, p_j) in items:
                if hid_j == hid_i:
                    continue
                for hid_k, (_, p_k) in items:
                    if hid_k in (hid_i, hid_j):
                        continue
                    denom = 1 - p_j - p_k
                    if denom <= 0 or (1 - p_j) <= 0:
                        continue
                    prob += p_j * (p_k / (1 - p_j)) * (p_i / denom)
        result[hid_i] = min(prob, 1.0)
    return result


def build_place_shortlist(race_plans: list[dict]) -> list[dict]:
    """Per race, rank runners by place probability and flag 'value' candidates:
    consistent placers the crowd underrates -- high place probability but NOT
    the model's top win pick (the short-priced favourite, whose place dividend
    is usually too cramped to be worth backing). No live place odds exist
    pre-race, so this is a shortlist to price up at the board, not a guaranteed
    bet."""
    out = []
    for rp in race_plans:
        entries = rp.get("entries", [])
        field_size = len(entries)
        places = places_paid_for_field(field_size)
        if places == 0 or not entries:
            out.append({"race_no": rp["race_no"], "places_paid": 0, "field_size": field_size, "picks": []})
            continue
        win_probs = {e["horse_id"]: (e["horse_name"], e["win_probability"]) for e in entries}
        pp = place_probabilities(win_probs, places)
        top_win_id = max(win_probs, key=lambda k: win_probs[k][1])
        picks = []
        for e in entries:
            pplace = pp.get(e["horse_id"], 0.0)
            # "value" heuristic: strong place chance but not the favourite the
            # crowd will over-back into a tiny place dividend.
            is_value = pplace >= 0.55 and e["horse_id"] != top_win_id and e["win_probability"] < 0.30
            picks.append({
                "horse_name": e["horse_name"],
                "place_probability": pplace,
                "win_probability": e["win_probability"],
                "value_flag": is_value,
            })
        picks.sort(key=lambda x: x["place_probability"], reverse=True)
        out.append({
            "race_no": rp["race_no"], "places_paid": places, "field_size": field_size,
            "picks": picks[:5],
        })
    return out


def stop_rules(budget: float, goal: float, cumulative_staked: float, cumulative_pnl: float) -> dict:
    remaining_budget = budget - cumulative_staked
    stop_loss = remaining_budget <= 0
    stop_win = cumulative_pnl >= goal
    if stop_win:
        message = f"Goal reached (+{cumulative_pnl:.0f} vs target {goal:.0f}). Good spot to stop and lock it in."
    elif stop_loss:
        message = f"Today's budget ({budget:.0f}) is fully committed. Time to stop, regardless of how the last race feels."
    else:
        message = f"{remaining_budget:.0f} of budget left, {goal - cumulative_pnl:.0f} short of goal."
    return {
        "remaining_budget": round(remaining_budget, 2),
        "stop_loss": stop_loss,
        "stop_win": stop_win,
        "message": message,
    }
