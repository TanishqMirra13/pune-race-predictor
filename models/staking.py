"""Budget/goal -> staking plan.

Important honesty note: RWITC's race-card page does not publish pre-race tote
odds (odds only appear after the fact, on the results page). So Phase 1 cannot
compute a true odds-based expected-value or Kelly stake -- there's no market
price to compare our model probability against yet. Instead we stake on
*edge over a random pick* (win_probability vs. 1/field_size), tiered and
capped, and we simply skip races where no horse clears a minimum edge
threshold. That's a deliberate feature, not a gap: not betting a weak race is
part of the discipline. If a live-odds source gets wired in later (Phase 2),
this can upgrade to true fractional-Kelly.
"""

MIN_EDGE_TO_BET = 0.05          # below this, skip the race entirely
EDGE_TIERS = [                   # (min_edge, stake weight multiplier)
    (0.20, 3.0),
    (0.12, 2.0),
    (0.05, 1.0),
]
MAX_STAKE_FRACTION_PER_BET = 0.15   # never stake more than this share of the day's budget on one bet
FORECAST_JACKPOT_RESERVE = 0.35     # share of budget set aside for forecast/quinella + jackpot, if the user opts in
BANKER_PROBABILITY_THRESHOLD = 0.35  # top pick strong enough to "banker" a jackpot leg alone


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


def jackpot_leg_plan(legs: list[dict], unit_cost: float = 5.0, budget: float | None = None) -> dict:
    """legs: [{'race_no': int, 'entries': [...rating_engine entries, sorted...]}]
    For each leg: banker (1 horse) if top pick clears BANKER_PROBABILITY_THRESHOLD,
    else spread the top 2-3 by probability. Returns combo count, cost, and whether it fits budget."""
    leg_selections = []
    for leg in legs:
        entries = leg["entries"]
        if not entries:
            leg_selections.append({"race_no": leg["race_no"], "horses": [], "mode": "no data"})
            continue
        top = entries[0]
        if top["win_probability"] >= BANKER_PROBABILITY_THRESHOLD:
            chosen = [top]
            mode = "banker"
        else:
            chosen = entries[:3] if len(entries) >= 3 else entries
            mode = "spread"
        leg_selections.append({
            "race_no": leg["race_no"],
            "mode": mode,
            "horses": [{"horse_name": e["horse_name"], "win_probability": e["win_probability"]} for e in chosen],
        })

    combo_count = 1
    for sel in leg_selections:
        combo_count *= max(len(sel["horses"]), 1)
    total_cost = combo_count * unit_cost

    fits_budget = budget is None or total_cost <= budget
    return {
        "legs": leg_selections,
        "combo_count": combo_count,
        "unit_cost": unit_cost,
        "total_cost": total_cost,
        "fits_budget": fits_budget,
        "note": None if fits_budget else "This combination exceeds the jackpot budget -- narrow spread legs to bankers where possible, or lower the unit stake.",
    }


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
