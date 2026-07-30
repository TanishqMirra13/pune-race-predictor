"""Daily parlay (multi) builder, with the expected-value test applied first.

A multi is the worst-priced product on any betting board, and the reason is
arithmetic rather than opinion. Every leg is priced with the bookmaker's
margin already inside it, and combining legs multiplies those margins together.
Three legs into a 16% book means betting into a 36% margin; four legs into a
Hong Kong all-up means roughly 44% gone to takeout before anyone has had an
opinion about a horse. There is no staking plan, bankroll rule or selection
method that overcomes a 36% head start.

That leaves exactly one condition under which a multi is worth placing, and
this module enforces it:

    Every single leg must be independently +EV -- that is, the price on offer
    must be longer than our best estimate of that runner's true chance. Then,
    and only then, does multiplying the legs multiply an edge rather than a
    deficit.

Consequences that are features, not bugs:

- Most days produce no qualifying multi. An empty result means the market was
  efficient today, which is the normal state of a market. The tool says so
  rather than manufacturing a suggestion.
- Two runners from the same race are never combined. Their outcomes are not
  independent -- they cannot both win -- so multiplying their probabilities
  overstates the multi's real chance. Same-race combinations belong in the
  exotics (quinella/forecast) planner, which prices them properly via Harville.
- Longer multis are penalised, not rewarded. The engine defaults to two and
  three legs. A six-leg multi has a huge headline price and a tiny probability,
  and the margin drag is what pays for the difference.

The probability each leg is judged on is a blend of this project's rating
model and the de-vigged market price, weighted towards the market. That is not
modesty for its own sake: the repo's own backtest found the tote favourite
beat the model's pick in races where the two disagreed, so the market gets the
larger vote. See models/odds.py.
"""
import math
from itertools import combinations

from models.odds import (
    DEFAULT_MODEL_WEIGHT, blend_probabilities, compound_takeout, devig_power,
    discounted_place_probabilities, expected_value, fair_odds, kelly_fraction,
    margin_percent, overround, parlay_margin_drag, parlay_odds,
    parlay_probability, places_paid,
)

# --- Leg qualification thresholds -----------------------------------------
# A leg must clear ALL of these to be eligible for a multi.
MIN_LEG_EV = 0.03          # 3% edge over the offered price; below this we are inside our own error bars
# Place legs must clear a higher bar than win legs. A win probability is
# measured directly against a quoted win price; a place probability is
# DERIVED, through a placing model whose residual error we cannot see. The
# extra two points are the price of that indirection.
MIN_PLACE_LEG_EV = 0.05
MIN_LEG_PROBABILITY = 0.12  # ignore lottery tickets however juicy the price looks
MIN_LEG_ODDS = 1.20         # nothing to gain from a leg that barely moves the multi
MAX_LEG_ODDS = 12.0         # long prices are where model error is largest
MAX_FIELD_SIZE = 16         # huge fields are close to unforecastable
MIN_FIELD_SIZE = 5

# What share of the declared field must carry a price before the race can be
# assessed at all. This is not fussiness -- a partial book breaks the maths in
# two separate ways:
#
#   De-vigging works by scaling a set of implied probabilities to sum to 1. Do
#   that to a subset and you do not remove a margin, you invent one: three
#   runners out of fourteen have implied probabilities summing to well under 1,
#   so the correction INFLATES them instead of shrinking them, and every one of
#   the three then looks like enormous value.
#
#   A place probability depends on the whole field -- it is the chance of
#   beating everyone else home. Compute it over three priced runners in a race
#   paying three places and all three come out certain to place, at which point
#   the engine will happily report a 220% edge on a horse at 3.20.
#
# Both failures were observed in a live run before this guard existed, which is
# why the threshold is high and why place legs demand more coverage than win
# legs: a place price is judged against a number derived from the entire field,
# so a gap anywhere in the field corrupts it.
MIN_WIN_PRICE_COVERAGE = 0.80
MIN_PLACE_PRICE_COVERAGE = 0.90

# --- Parlay construction ---------------------------------------------------
MIN_LEGS = 2
MAX_LEGS = 3
MAX_LEG_POOL = 14          # cap the candidate pool so combination count stays sane
MAX_SUGGESTIONS = 6
MIN_PARLAY_EV = 0.05       # a multi must clear a higher bar than a single: more ways to be wrong

# --- Staking ---------------------------------------------------------------
KELLY_MULTIPLIER = 0.20    # multis carry more estimation error than singles, so stake more conservatively
MAX_PARLAY_FRACTION = 0.03  # never more than 3% of bankroll on one multi

# Each profile sets TWO independent floors, and they are given explicitly
# rather than derived from one another:
#
#   min_leg_probability -- how likely each individual selection must be
#   min_probability     -- how likely the finished multi must be
#
# An earlier version derived the per-leg floor as min_probability**(1/legs),
# which quietly broke the aggressive profile: a 3-leg multi asked to land 3% of
# the time implies each leg is a 31% chance, i.e. three short-priced
# favourites -- the least aggressive bet on the card, and the one least likely
# to be value. The two floors answer different questions, so they are set
# separately.
PROFILES = {
    "safe": {
        "label": "Safe (place legs)",
        "markets": ("place",),
        "min_legs": 2, "max_legs": 2,
        "min_leg_probability": 0.55,
        "min_probability": 0.30,
        "description": "Two place legs on well-fancied runners. Hits most often, pays least -- the only shape that wins on more days than it loses.",
    },
    "balanced": {
        "label": "Balanced (win + place)",
        "markets": ("win", "place"),
        "min_legs": 2, "max_legs": 3,
        "min_leg_probability": 0.25,
        "min_probability": 0.08,
        "description": "Mixes win and place legs. Lands roughly one day in eight to one in four; a losing week is entirely normal.",
    },
    "aggressive": {
        "label": "Aggressive (win legs)",
        "markets": ("win",),
        "min_legs": 2, "max_legs": 3,
        "min_leg_probability": MIN_LEG_PROBABILITY,
        "min_probability": 0.01,
        "description": "Win legs only, genuine prices rather than favourites. Big headline return, and it will miss far more often than it lands -- expect month-long droughts even when the edge is real.",
    },
}


def _confidence_ok(entry: dict) -> bool:
    """A runner the rating engine itself flagged 'low' (unrated, or no form on
    file) has a probability we do not believe enough to multiply."""
    return entry.get("confidence") != "low"


def qualify_legs(slate: list[dict], model_weight: float = DEFAULT_MODEL_WEIGHT,
                 min_ev: float = MIN_LEG_EV, min_probability: float = MIN_LEG_PROBABILITY,
                 min_odds: float = MIN_LEG_ODDS, max_odds: float = MAX_LEG_ODDS,
                 circuit: str = "Australia") -> tuple[list[dict], list[dict]]:
    """Find every individually +EV selection across the day's races.

    slate entries look like:
        {"race_id", "venue", "race_no", "race_time", "field_size",
         "entries": [...rating_engine entries...],
         "odds": {horse_name: {"win": decimal, "place": decimal or None}}}

    Returns (qualifying_legs, rejected_races_with_reasons). The rejection list
    matters as much as the picks -- it is how the user sees that a quiet day is
    a judgement, not a failure to load data."""
    legs, rejected = [], []

    for race in slate:
        entries = [e for e in race.get("entries", []) if not e.get("scratched")]
        odds_map = race.get("odds") or {}
        field_size = race.get("field_size") or len(entries)

        if not entries:
            rejected.append({"race": _race_label(race), "reason": "no runners parsed"})
            continue
        if not odds_map:
            rejected.append({"race": _race_label(race),
                             "reason": "no market odds captured -- nothing to measure an edge against"})
            continue
        if not (MIN_FIELD_SIZE <= field_size <= MAX_FIELD_SIZE):
            rejected.append({"race": _race_label(race),
                             "reason": f"field of {field_size} outside the {MIN_FIELD_SIZE}-{MAX_FIELD_SIZE} band we model honestly"})
            continue

        priced = [e for e in entries if (odds_map.get(e["horse_name"]) or {}).get("win")]
        coverage = len(priced) / field_size if field_size else 0.0
        if len(priced) < 3:
            rejected.append({"race": _race_label(race), "reason": "fewer than 3 runners have a price"})
            continue
        if coverage < MIN_WIN_PRICE_COVERAGE:
            rejected.append({
                "race": _race_label(race),
                "reason": (f"only {len(priced)} of {field_size} runners priced "
                           f"({coverage * 100:.0f}%) -- a partial book cannot be de-vigged, so any "
                           f"edge it appears to show is an artefact. Add the rest of the prices."),
            })
            continue

        win_odds = [odds_map[e["horse_name"]]["win"] for e in priced]
        book = overround(win_odds)
        if book <= 1.0:
            # A complete book always sums to more than 1. Less means prices are
            # missing, stale, or mistyped -- never a genuine opportunity.
            rejected.append({
                "race": _race_label(race),
                "reason": (f"prices sum to {book:.3f}, below an even book -- something is missing or "
                           f"mistyped. Re-check the prices for this race."),
            })
            continue
        market_probs = devig_power(win_odds)

        # Renormalise the model over the priced subset so the two vectors are
        # comparable before blending.
        model_total = sum(e["win_probability"] for e in priced) or 1.0
        model_probs = [e["win_probability"] / model_total for e in priced]
        blended = blend_probabilities(model_probs, market_probs, model_weight)

        # Place probabilities are only computed when nearly the whole field is
        # priced -- see MIN_PLACE_PRICE_COVERAGE. With a gap in the field the
        # number is not merely noisy, it is wrong in a specific and dangerous
        # direction: too high.
        n_places = places_paid(field_size, circuit)
        place_probs = {}
        if n_places and coverage >= MIN_PLACE_PRICE_COVERAGE:
            win_map = {e["horse_id"]: (e["horse_name"], p) for e, p in zip(priced, blended)}
            place_probs = discounted_place_probabilities(win_map, n_places)

        race_had_a_leg = False
        for entry, p_model, p_market, p_blend in zip(priced, model_probs, market_probs, blended):
            if not _confidence_ok(entry):
                continue
            quotes = odds_map[entry["horse_name"]]

            for market in ("win", "place"):
                price = quotes.get(market)
                if not price or not (min_odds <= price <= max_odds):
                    continue
                probability = p_blend if market == "win" else place_probs.get(entry["horse_id"])
                if not probability or probability < min_probability:
                    continue
                ev = expected_value(probability, price)
                if ev < (min_ev if market == "win" else max(min_ev, MIN_PLACE_LEG_EV)):
                    continue
                race_had_a_leg = True
                legs.append({
                    "race_id": race.get("race_id"),
                    "venue": race.get("venue"),
                    "race_no": race.get("race_no"),
                    "race_time": race.get("race_time"),
                    "circuit": race.get("circuit", circuit),
                    "horse_name": entry["horse_name"],
                    "market": market,
                    "decimal_odds": price,
                    "model_probability": round(p_model, 4),
                    "market_probability": round(p_market, 4) if p_market else None,
                    "blended_probability": round(probability, 4),
                    "fair_odds": round(fair_odds(probability), 3),
                    "expected_value": round(ev, 4),
                    "kelly": round(kelly_fraction(probability, price), 4),
                    "field_size": field_size,
                    "book_overround": round(book, 4),
                    "book_margin_pct": round(margin_percent(win_odds), 2),
                    "places_paid": n_places,
                    "reasoning": entry.get("reasoning"),
                })

        if not race_had_a_leg:
            rejected.append({"race": _race_label(race),
                             "reason": "market prices are at or shorter than our fair prices -- no edge on offer"})

    legs.sort(key=lambda l: l["expected_value"], reverse=True)
    return legs, rejected


def _race_label(race: dict) -> str:
    return f"{race.get('venue', '?')} R{race.get('race_no', '?')}"


def _best_leg_per_race(legs: list[dict]) -> list[dict]:
    """One leg per race, ranked by Kelly fraction rather than raw EV.

    Two selections from one race cannot be multiplied (they are not
    independent), so the pool is flattened before combinations are formed --
    but which one survives matters. Ranking by expected value alone picks the
    wrong horse: when a book is generously priced across the board, every
    runner in the race carries almost the SAME edge, so an EV sort breaks the
    tie essentially at random and tends to surface a 12%-chance outsider as
    the race's representative. Two such legs make a multi that pays well and
    almost never lands.

    Kelly (edge divided by the price) is the right tie-breaker: at equal edge
    it prefers the shorter price, which is both the more probable leg and the
    one whose probability estimate we trust more."""
    best: dict = {}
    for leg in legs:
        key = leg["race_id"] if leg["race_id"] is not None else (leg["venue"], leg["race_no"])
        if key not in best or leg["kelly"] > best[key]["kelly"]:
            best[key] = leg
    return sorted(best.values(), key=lambda l: l["kelly"], reverse=True)


def build_parlays(legs: list[dict], bankroll: float, min_legs: int = MIN_LEGS,
                  max_legs: int = MAX_LEGS, markets: tuple = ("win", "place"),
                  min_probability: float = 0.0, min_leg_probability: float = 0.0,
                  min_ev: float = MIN_PARLAY_EV,
                  max_suggestions: int = MAX_SUGGESTIONS,
                  kelly_multiplier: float = KELLY_MULTIPLIER,
                  label: str = "") -> list[dict]:
    """Combine qualifying legs into ranked multis.

    Only legs from different races are combined, only the requested markets are
    used, and only combinations whose expected value clears min_ev survive.
    min_leg_probability screens individual selections; min_probability screens
    the finished multi."""
    pool = [l for l in legs
            if l["market"] in markets and l["blended_probability"] >= min_leg_probability]
    pool = _best_leg_per_race(pool)[:MAX_LEG_POOL]
    if len(pool) < min_legs:
        return []

    suggestions = []
    for size in range(min_legs, min(max_legs, len(pool)) + 1):
        for combo in combinations(pool, size):
            probs = [l["blended_probability"] for l in combo]
            prices = [l["decimal_odds"] for l in combo]
            hit = parlay_probability(probs)
            if hit < min_probability:
                continue
            combined = parlay_odds(prices)
            ev = expected_value(hit, combined)
            if ev < min_ev:
                continue
            kelly = kelly_fraction(hit, combined)
            fraction = min(kelly * kelly_multiplier, MAX_PARLAY_FRACTION)
            stake = round(bankroll * fraction, 2)
            suggestions.append({
                "label": label,
                "legs": list(combo),
                "leg_count": size,
                "combined_odds": round(combined, 3),
                "hit_probability": round(hit, 4),
                "expected_value": round(ev, 4),
                "fair_combined_odds": round(fair_odds(hit), 2),
                "margin_drag": round(parlay_margin_drag([l["book_overround"] for l in combo]), 4),
                "kelly_fraction": round(fraction, 4),
                "suggested_stake": stake,
                "potential_return": round(stake * combined, 2),
                "potential_profit": round(stake * (combined - 1), 2),
            })

    suggestions.sort(key=lambda s: (s["expected_value"], s["hit_probability"]), reverse=True)
    return _dedupe_overlapping(suggestions)[:max_suggestions]


def _dedupe_overlapping(suggestions: list[dict], max_shared: int = 1) -> list[dict]:
    """Drop near-duplicate multis that share most of their legs.

    Six suggestions that are the same three horses reshuffled give an illusion
    of choice and, worse, tempt a punter into backing all of them -- which is
    one correlated bet at six times the stake."""
    kept: list[dict] = []
    for s in suggestions:
        s_legs = {(l["venue"], l["race_no"], l["horse_name"], l["market"]) for l in s["legs"]}
        if any(len(s_legs & {(l["venue"], l["race_no"], l["horse_name"], l["market"])
                             for l in k["legs"]}) > max_shared for k in kept):
            continue
        kept.append(s)
    return kept


def daily_parlay_card(slate: list[dict], bankroll: float,
                      model_weight: float = DEFAULT_MODEL_WEIGHT,
                      circuit: str = "Australia",
                      profiles: tuple = ("safe", "balanced", "aggressive")) -> dict:
    """The full daily suggestion: qualified legs, then one set of multis per
    risk profile, plus the honest summary of what was rejected and why."""
    legs, rejected = qualify_legs(slate, model_weight=model_weight, circuit=circuit)

    by_profile = {}
    for name in profiles:
        cfg = PROFILES[name]
        by_profile[name] = {
            "label": cfg["label"],
            "description": cfg["description"],
            "parlays": build_parlays(
                legs, bankroll,
                min_legs=cfg["min_legs"], max_legs=cfg["max_legs"],
                markets=cfg["markets"], min_probability=cfg["min_probability"],
                min_leg_probability=cfg["min_leg_probability"],
                label=cfg["label"],
            ),
        }

    total = sum(len(v["parlays"]) for v in by_profile.values())
    return {
        "circuit": circuit,
        "legs": legs,
        "rejected_races": rejected,
        "profiles": by_profile,
        "total_suggestions": total,
        "singles": singles_shortlist(legs, bankroll),
        "verdict": _verdict(legs, total, circuit),
    }


def _verdict(legs: list[dict], total_suggestions: int, circuit: str) -> str:
    if not legs:
        return ("No selection anywhere on today's card is priced longer than our estimate of its "
                "true chance, so there is no multi worth placing. This is the ordinary outcome of "
                "an efficient market, not a data problem -- the correct action is to skip the day.")
    if not total_suggestions:
        return (f"{len(legs)} individual selections look like value, but no combination of them clears "
                f"the multi threshold once the compounded margin is taken out. Back the singles if you "
                f"back anything; skip the multi.")
    return (f"{len(legs)} selections are priced above our fair odds, and {total_suggestions} combinations "
            f"survive the margin test. A three-leg multi on the {circuit} circuit still gives up about "
            f"{compound_takeout(circuit, 3) * 100:.0f}% to takeout, so treat the stake sizes below as "
            f"ceilings, not targets.")


def singles_shortlist(legs: list[dict], bankroll: float, top_n: int = 5,
                      kelly_multiplier: float = 0.25, max_fraction: float = 0.05) -> list[dict]:
    """The same qualifying legs, priced as straight single bets.

    Included on purpose and listed above the multis in the UI: a single on a
    +EV selection has the same edge as the multi leg with a fraction of the
    variance. If the aim is a small, regular return, this list is the honest
    answer and the multis are the entertainment."""
    out = []
    for leg in _best_leg_per_race(legs)[:top_n]:
        fraction = min(leg["kelly"] * kelly_multiplier, max_fraction)
        stake = round(bankroll * fraction, 2)
        out.append({
            **leg,
            "suggested_stake": stake,
            "potential_profit": round(stake * (leg["decimal_odds"] - 1), 2),
        })
    return out


def target_feasibility(parlay: dict, target_profit: float, bankroll: float) -> dict:
    """What it would take for one multi to hit a daily profit target, and what
    that costs in expectation.

    Deliberately blunt about the trade: staking up to reach a target does not
    change the edge, it only scales both the win and the loss. If the expected
    value is negative, a bigger stake buys a bigger expected loss."""
    price = parlay["combined_odds"]
    hit = parlay["hit_probability"]
    required_stake = target_profit / (price - 1) if price > 1 else None
    if required_stake is None:
        return {"achievable": False, "reason": "combined odds do not pay a profit"}

    ev_at_required = required_stake * parlay["expected_value"]
    return {
        "achievable": required_stake <= bankroll,
        "required_stake": round(required_stake, 2),
        "stake_share_of_bankroll": round(required_stake / bankroll, 4) if bankroll else None,
        "probability_of_hitting": round(hit, 4),
        "probability_of_losing_the_stake": round(1 - hit, 4),
        "expected_pnl_at_that_stake": round(ev_at_required, 2),
        "kelly_says_stake": parlay["suggested_stake"],
        # >1 means the target forces a stake larger than Kelly allows (the
        # dangerous direction); <1 means the target is reachable inside it.
        "stake_vs_kelly": (round(required_stake / parlay["suggested_stake"], 2)
                           if parlay["suggested_stake"] else None),
        "exceeds_kelly": bool(parlay["suggested_stake"] and required_stake > parlay["suggested_stake"]),
        "note": (
            f"Hitting this multi returns Rs{target_profit:.0f}, and it does so {hit * 100:.0f}% of the "
            f"time. The other {(1 - hit) * 100:.0f}% of the time the stake is gone. Expected value at "
            f"that stake is Rs{ev_at_required:+.0f} per day."
        ),
    }


def expected_daily_outcome(parlay: dict, stake: float, days: int = 30) -> dict:
    """What backing this shape of multi every day for a month looks like.

    The headline number people underweight is the losing run. A multi that
    hits 25% of the time will, over a month, quite normally go a week without
    landing -- and a plan that cannot survive that emotionally will not survive
    it financially either."""
    hit = parlay["hit_probability"]
    price = parlay["combined_odds"]
    profit = stake * (price - 1)
    ev_day = stake * parlay["expected_value"]
    miss = 1 - hit
    longest_expected_drought = 0
    if 0 < miss < 1:
        longest_expected_drought = int(math.log(1 / max(days, 1)) / math.log(miss))
    return {
        "days": days,
        "stake_per_day": round(stake, 2),
        "total_staked": round(stake * days, 2),
        "profit_per_hit": round(profit, 2),
        "expected_hits": round(hit * days, 1),
        "expected_pnl": round(ev_day * days, 2),
        "p_no_hit_all_period": round(miss ** days, 4),
        "typical_longest_losing_run": max(longest_expected_drought, 1),
        "verdict": ("positive expectation -- but the month-to-month swing will still be several times "
                    "this number" if ev_day > 0 else
                    "negative expectation -- staking more or betting more days makes the expected loss "
                    "bigger, not smaller"),
    }
