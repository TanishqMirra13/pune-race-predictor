"""Jackpot / treble planning: which runners to cover in each leg, and how many.

WHAT CHANGED, AND WHY -- THE MEASUREMENT THAT DROVE IT

The first version of this planner ranked each leg by the model's own win
probability, took the top pick alone if it cleared 35% and otherwise spread
the top three, and multiplied out. Replayed over this archive that is close
to the worst way to spend the money. Two findings, both measured on 431 fully
priced Indian races and 63-69 complete race days (see MEASURED_* below):

1. THE MARKET PICKS LEGS FAR BETTER THAN THE MODEL DOES.
   Share of races where the winner came from the top N of the starting-price
   market, versus the top N of this model:

        top 1     market 47%   model 27%
        top 2     market 68%   model 44%
        top 3     market 79%   model 58%
        top 4     market 87%   model 71%

   Compounded across legs that gap is enormous. Whole tickets, replayed over
   every complete archived race day, covering the top N of each ranking in
   every leg:

        legs  runners/leg  combos    market   model
          3        3           27     40.3%    19.4%
          4        3           81     32.3%    13.8%
          5        3          243     25.4%     7.9%
          5        2           32      6.3%     1.6%

   Same combinations, same money, three times the hit rate. This is the same
   conclusion the repo's win-betting backtest reached -- when model and market
   disagree, the market is right -- applied where it compounds hardest.

   So leg selection is market-led by default. The model is still on the
   slider, and still shown, but it starts at zero weight because that is what
   the evidence supports.

2. SPREADING EVENLY WASTES COMBINATIONS.
   Legs are not equally hard. A leg where the favourite is 2.00 and the rest
   are 8.00+ needs one runner; a leg where five runners are 5.00 needs five.
   Splitting the budget evenly buys coverage where it is cheap and refuses it
   where it is needed. Allocating by marginal value instead -- repeatedly
   adding whichever runner, in whichever leg, buys the most extra hit
   probability per extra rupee -- at the SAME combination count:

        legs  combos    even spread    this planner
          3       27         40.6%          50.7%
          4       16          8.7%          20.3%
          4       81         31.9%          46.4%
          5       32          5.8%          11.6%
          5      243         24.6%          36.2%
          6      729         22.6%          32.3%

   The advantage narrows to nothing once the budget is wide enough to cover
   four runners in every leg, which is the point at which the shape of a race
   stops mattering because you have bought most of it. Below that -- which is
   every realistic budget -- it is worth between 6 and 15 points.

Together those two changes take a five-leg jackpot at 243 combinations from
7.9% to 36.2%: a bet that lands about one race day in three instead of one in
thirteen. On 69 race days the 95% confidence interval on any of these rates is
about +/-12 points, so read the direction, not the decimals.

WHAT THIS DOES NOT DO

It does not make a jackpot a good bet. Hitting more often is not the same as
winning money, and a jackpot is pari-mutuel: the dividend is the pool split
between everyone who had the same line. Covering the market's favourites is
precisely the line most other tickets also hold, so the combinations that hit
most often are the ones that pay least when they do. That is not a reason to
avoid them -- it is a reason the planner prints the break-even dividend next
to the hit probability, and prints what dividends this archive has actually
seen, so the two can be compared before any money moves.

CALIBRATION

Raw de-vigged market probabilities understate the front of an Indian tote
book. Summing the top three de-vigged probabilities gave 72% where the winner
actually came from that top three 80% of the time -- a persistent gap, not
noise. Raising the de-vigged probabilities to the power 1.25 and renormalising
closes it across every k and holds on both halves of the archive by date and
at all three well-sampled venues. FAVOURITE_BIAS_EXPONENT applies that. It
changes no ranking (the transform is monotone) -- only the honesty of the
"chance it lands" figure, which would otherwise read a third too low.
"""
import math

from models.odds import devig_power

# --- Measured constants ----------------------------------------------------

# Correction applied to de-vigged market probabilities before they are used as
# real probabilities. Fitted on 416 fully priced Indian races and checked on
# both halves of the archive by date; see the module docstring.
FAVOURITE_BIAS_EXPONENT = 1.25

# Median overround of a complete Indian starting-price book, measured over 396
# races. Used to place a PARTIAL book on a probability scale -- with only the
# front of the market quoted there is nothing to normalise against, so the
# priced runners are scaled by this instead of being inflated to sum to 1.
INDIA_WIN_OVERROUND = 1.203

# Share of races whose winner came from the top N of each ranking. Quoted in
# the UI so the leg-width choice is made against evidence rather than feel.
MEASURED_LEG_COVERAGE = {
    1: {"market": 0.466, "model": 0.267},
    2: {"market": 0.666, "model": 0.436},
    3: {"market": 0.791, "model": 0.580},
    4: {"market": 0.884, "model": 0.712},
}
MEASURED_LEG_COVERAGE_SAMPLE = 431

# Whole-ticket hit rates by leg count, at the combination count an even
# 3-runners-per-leg spread costs, replayed over every complete archived race
# day. 'model' and 'market' are even spreads ranked each way; 'planned' is what
# plan() produces for the same money.
#
# Two sample sizes rather than one, because the comparisons need different
# things: ranking legs by the model needs every runner re-scored, which drops
# the handful of days with an incomplete card, while 'planned' is market-only
# and runs over all 69. Quoted separately rather than averaged, since a rate
# and the sample it came from belong together. Roughly +/-12pp either way --
# backtest_strategy() recomputes 'planned' live against the current archive.
MEASURED_STRATEGY = {
    3: {"combos": 27, "days": 67, "model": 0.194, "market": 0.403,
        "planned_days": 69, "planned": 0.507},
    4: {"combos": 81, "days": 65, "model": 0.138, "market": 0.323,
        "planned_days": 69, "planned": 0.464},
    5: {"combos": 243, "days": 63, "model": 0.079, "market": 0.254,
        "planned_days": 69, "planned": 0.362},
}

# (dividend, tickets that shared it) for the FULL tier -- every leg right --
# read off real result pages at RWITC and Hyderabad, Jul-Aug 2026. A fallback
# only: dividend_reality() prefers the pool_dividends table, which grows every
# time results are loaded.
#
# The spread is the point. Five jackpots in this handful paid 317, 1203, 1221,
# 32647 and 91296 for the same bet, and the reason is the ticket count beside
# each: 1468 winners on the day the favourites obliged, 6 on the day they did
# not. A jackpot dividend is not a price. Treat any single figure as a draw
# from a distribution this wide.
OBSERVED_DIVIDEND_SAMPLE = {
    "JACKPOT": [(91296, 6), (1203, 300), (32647, 16), (317, 1468), (1221, 419),
                (84160, 6), (5437, 3)],
    "TREBLE": [(2708, 11), (4244, 6), (1117, 57), (86, 370), (199, 92), (56, 739),
               (3354, 10), (1348, 31), (1642, 17), (57, 437), (65, 409), (348, 109),
               (1733, 10), (1849, 15), (191, 47), (309, 47), (1642, 14), (16347, 1),
               (458, 33), (193, 162)],
}

# Indian jackpot pools pay in two tiers: 70% of the pool to tickets with every
# leg, 30% to those one leg short. RWITC labels them; indiarace does not, and
# the labels there were recovered arithmetically -- dividend x tickets for the
# two figures came to a 30.0/70.0 split of one pool on all five two-tier
# jackpots in the cache. Worth knowing when planning: a ticket that misses one
# leg is not always a losing ticket.
CONSOLATION_SHARE = 0.30

# Pool shapes seen on real Indian result pages. The leg list is whatever the
# club announces on the day -- these are the defaults to offer, not a rule.
# Verified from RWITC and Hyderabad result pages, Jul-Aug 2026:
#   RWITC        Super Jackpot = last 6 races, Jackpot = last 5, trebles are
#                consecutive triples (2-3-4 and 5-6-7 on an eight-race card).
#   Hyderabad    Jackpot = last 5, two Mini Jackpots of 4, three trebles.
POOLS = {
    "Treble": {"legs": 3, "note": "Three consecutive races. The cheapest way in, and the one that hits."},
    "Mini Jackpot": {"legs": 4, "note": "Four legs. Offered at Hyderabad and several southern clubs."},
    "Jackpot": {"legs": 5, "note": "Five legs, usually the last five races. The standard Indian jackpot."},
    "Super Jackpot": {"legs": 6, "note": "Six legs, RWITC. Carries forward more often than it pays."},
}


def pool_options(n_races: int) -> list[str]:
    """Pools that fit on a card of this many races."""
    return [name for name, p in POOLS.items() if p["legs"] <= n_races]


def pool_family(name: str) -> str:
    """'SECOND TREBLE' -> 'Treble'. One card carries several trebles and often
    two mini-jackpots, each over different legs but all paying out of pools of
    the same kind, so dividends are compared by family rather than by the
    club's ordinal label.

    Order matters: 'SUPER JACKPOT' and 'MINI JACKPOT' both contain 'JACKPOT',
    so the specific families are tested first and the bare one last."""
    upper = (name or "").upper()
    if "SUPER JACKPOT" in upper:
        return "Super Jackpot"
    if "MINI JACKPOT" in upper:
        return "Mini Jackpot"
    if "TREBLE" in upper:
        return "Treble"
    if "JACKPOT" in upper:
        return "Jackpot"
    return name.title()


def suggest_legs(race_numbers: list[int], n_legs: int) -> list[int]:
    """The last n_legs races of the card -- where Indian clubs put the jackpot.

    A starting point for the picker, not an assertion. The club's own leg list
    is printed on the day and is the only authority; once a result page has
    been archived, actual_legs() reads it back."""
    return sorted(race_numbers)[-n_legs:] if len(race_numbers) >= n_legs else sorted(race_numbers)


def actual_legs(conn, race_date: str, venue: str) -> dict:
    """{pool_name: [race numbers]} as the club itself published them, from any
    archived result page for this meeting. Empty until results are loaded.

    A club prints its legs as POSITIONS in the day's card -- "Legs 4,5,6,7,8"
    means the fourth through eighth race of the meeting. That is not always the
    race number this database stores: RWITC numbers races cumulatively across a
    season, so the eight-race Pune card of 8 Aug 2026 is races 26-33 here while
    its jackpot legs read 4-8. Translating by position rather than by number is
    what makes the two agree; taking the printed digits literally would plan the
    ticket around races that are not in the pool, or none at all."""
    rows = conn.execute(
        "SELECT pool, legs FROM pool_dividends WHERE race_date=? AND venue=? ORDER BY pool",
        (race_date, venue),
    ).fetchall()
    card = [r["race_number"] for r in conn.execute(
        "SELECT race_number FROM races WHERE race_date=? AND venue=? ORDER BY race_number",
        (race_date, venue))]
    out = {}
    for r in rows:
        positions = [int(x) for x in (r["legs"] or "").replace(" ", "").split(",") if x.isdigit()]
        legs = [card[p - 1] for p in positions if 1 <= p <= len(card)]
        if len(legs) == len(positions) and legs:
            out[r["pool"]] = legs
    return out


# --- Leg probabilities -----------------------------------------------------

def leg_probabilities(entries: list[dict], odds: dict | None,
                      model_weight: float = 0.0) -> dict:
    """Best available win probability for every runner in one leg.

    Returns {'runners': [{'horse_name', 'probability', 'decimal_odds',
    'source'}...] sorted best first, 'book': 'full'|'partial'|'none',
    'priced': int, 'field': int}.

    THE PARTIAL-BOOK POINT, which is what makes this usable on an Indian card:
    the parlay engine refuses a race priced below 80% of its field, and it is
    right to -- de-vigging a subset invents an edge instead of removing a
    margin. A jackpot leg does not need that. It needs the market's ORDER at
    the front of the book plus a rough size for each chance, and indiarace
    quoting only the front four or five runners supplies exactly that. So a
    partial book is used here rather than rejected, scaled by the measured
    full-book overround instead of being normalised to sum to 1 (which is the
    step that would inflate it), with whatever probability is left over shared
    among the unpriced runners by their model score.
    """
    field = len(entries)
    if not field:
        return {"runners": [], "book": "none", "priced": 0, "field": 0}

    odds = odds or {}
    decs = []
    for e in entries:
        raw = odds.get(e["horse_name"]) or odds.get((e["horse_name"] or "").upper()) or {}
        win = raw.get("win")
        decs.append(win if (win and win > 1.0) else None)
    priced = sum(1 for d in decs if d)

    model = [max(e.get("win_probability") or 0.0, 1e-6) for e in entries]
    m_total = sum(model) or 1.0
    model = [m / m_total for m in model]

    if priced == 0:
        market = [None] * field
        book = "none"
    elif priced >= max(3, math.ceil(0.9 * field)):
        # Near-complete book: de-vig properly, then correct the
        # favourite-longshot bias the power method leaves behind on Indian tote
        # prices. De-vigging normalises the PRICED runners to sum to 1, so any
        # stragglers have to be made room for rather than simply added on top --
        # otherwise the leg's probabilities sum above 1 and every priced runner
        # is quietly shrunk by the renormalisation at the end. When the whole
        # field is priced, which is the case FAVOURITE_BIAS_EXPONENT was fitted
        # on, unpriced_share is zero and this changes nothing.
        fair = _sharpen([f if f else None for f in devig_power([d for d in decs])])
        unpriced_share = min(sum(m for m, d in zip(model, decs) if not d), 0.25)
        unpriced_model = sum(m for m, d in zip(model, decs) if not d) or 1.0
        market = [
            (f * (1 - unpriced_share)) if f is not None
            else unpriced_share * (m / unpriced_model)
            for f, m in zip(fair, model)
        ]
        book = "full"
    else:
        # Partial book: scale by the measured overround rather than
        # normalising, and leave the remainder for the unpriced runners.
        implied = [(1.0 / d) / INDIA_WIN_OVERROUND if d else None for d in decs]
        taken = sum(x for x in implied if x)
        if taken >= 0.98:  # a front-of-book that already fills the card
            implied = [(x / taken * 0.98) if x else None for x in implied]
            taken = 0.98
        residual = max(1.0 - taken, 0.0)
        unpriced_model = sum(m for m, d in zip(model, decs) if not d) or 1.0
        market = [
            x if x is not None else residual * (m / unpriced_model)
            for x, m in zip(implied, model)
        ]
        market = _sharpen(market)
        book = "partial"

    runners = []
    for e, m, mk, d in zip(entries, model, market, decs):
        # 'source' says where this runner's number came from, and it is keyed
        # off whether the runner itself carried a price -- not off whether the
        # leg had a book. An unpriced runner in a mostly-priced leg still gets
        # its share from the model, and labelling that "market" would tell the
        # reader the market has an opinion it does not have.
        if mk is None or d is None:
            p, src = (m if mk is None else mk), "model"
        elif model_weight <= 0:
            p, src = mk, "market"
        else:
            p = model_weight * m + (1 - model_weight) * mk
            src = "blend"
        runners.append({
            "horse_id": e.get("horse_id"), "horse_name": e["horse_name"],
            "probability": p, "decimal_odds": d, "source": src,
            "model_probability": m,
        })
    total = sum(r["probability"] for r in runners) or 1.0
    for r in runners:
        r["probability"] /= total
    runners.sort(key=lambda r: -r["probability"])
    return {"runners": runners, "book": book, "priced": priced, "field": field}


def _sharpen(probs: list[float | None]) -> list[float | None]:
    """Apply FAVOURITE_BIAS_EXPONENT and renormalise the priced entries."""
    vals = [p for p in probs if p is not None]
    if not vals:
        return probs
    raised = [(p ** FAVOURITE_BIAS_EXPONENT) if p is not None else None for p in probs]
    total = sum(x for x in raised if x is not None) or 1.0
    scale = sum(vals) / total
    return [(x * scale) if x is not None else None for x in raised]


# --- The planner -----------------------------------------------------------

def plan(legs: list[dict], budget: float, unit_cost: float = 5.0,
         model_weight: float = 0.0, max_combinations: int = 20000,
         target_dividend: float | None = None) -> dict:
    """Spend `budget` on jackpot combinations to maximise the chance of hitting.

    legs: [{'race_no', 'entries', 'odds'}] in running order.

    The allocation is greedy on marginal value. Every leg starts on its single
    strongest runner. Then, repeatedly, the runner that would add the most hit
    probability per extra rupee is added to its leg, until the budget cannot
    take another step. Because cost is the PRODUCT of the leg widths, widening
    a leg gets steadily more expensive as the ticket grows, which is exactly
    what stops the plan sprawling: the third runner in a five-leg ticket costs
    the same as everything bought so far.

    If target_dividend is given, the run also stops once a step's extra
    combinations cost more than the extra chance is worth at that dividend --
    the point past which more coverage is buying a worse bet, not a better one.
    """
    prepared = []
    for leg in legs:
        lp = leg_probabilities(leg.get("entries") or [], leg.get("odds"), model_weight)
        prepared.append({**lp, "race_no": leg.get("race_no"),
                         "race_name": leg.get("race_name"), "venue": leg.get("venue")})

    usable = [l for l in prepared if l["runners"]]
    if len(usable) < 2:
        return {"legs": prepared, "combinations": 0, "cost": 0.0,
                "hit_probability": 0.0, "affordable": False,
                "note": "Need at least two legs with a field loaded before anything can be planned."}

    widths = [1] * len(usable)
    max_affordable = int(budget // unit_cost) if unit_cost > 0 else 0
    if max_affordable < 1:
        return {"legs": prepared, "combinations": 0, "cost": 0.0,
                "hit_probability": 0.0, "affordable": False,
                "note": f"A single combination costs Rs{unit_cost:.0f}; the budget does not cover one."}
    ceiling = min(max_affordable, max_combinations)

    steps = []
    while True:
        current = _combos(widths)
        best = None
        for i, leg in enumerate(usable):
            if widths[i] >= len(leg["runners"]):
                continue
            trial = widths[:]
            trial[i] += 1
            new_combos = _combos(trial)
            if new_combos > ceiling:
                continue
            added = leg["runners"][widths[i]]["probability"]
            others = 1.0
            for j, other in enumerate(usable):
                if j == i:
                    continue
                others *= sum(r["probability"] for r in other["runners"][:widths[j]])
            gain = added * others
            extra_cost = (new_combos - current) * unit_cost
            if extra_cost <= 0:
                continue
            value = gain / extra_cost
            if best is None or value > best["value"]:
                best = {"value": value, "leg_index": i, "gain": gain,
                        "extra_cost": extra_cost, "combos": new_combos,
                        "runner": leg["runners"][widths[i]]}
        if best is None:
            break
        if target_dividend and best["gain"] * target_dividend < best["extra_cost"]:
            break
        widths[best["leg_index"]] += 1
        steps.append(best)

    widths = _rebalance(usable, widths, ceiling)

    combos = _combos(widths)
    hit = 1.0
    out_legs = []
    for i, leg in enumerate(usable):
        chosen = leg["runners"][:widths[i]]
        coverage = sum(r["probability"] for r in chosen)
        hit *= coverage
        out_legs.append({
            "race_no": leg["race_no"], "race_name": leg.get("race_name"),
            "venue": leg.get("venue"), "book": leg["book"],
            "priced": leg["priced"], "field": leg["field"],
            "width": widths[i], "coverage": coverage,
            "runners": chosen,
            "next_out": leg["runners"][widths[i]] if widths[i] < len(leg["runners"]) else None,
            "shape": _leg_shape(leg["runners"]),
        })

    cost = combos * unit_cost
    marginal = steps[-1] if steps else None
    return {
        "legs": out_legs,
        "skipped_legs": [l["race_no"] for l in prepared if not l["runners"]],
        "combinations": combos,
        "unit_cost": unit_cost,
        "cost": cost,
        "hit_probability": hit,
        "break_even_dividend": (cost / hit) if hit > 0 else None,
        "affordable": True,
        "budget_used": cost,
        "budget_left": budget - cost,
        "book_quality": _book_quality(out_legs),
        "marginal_step": ({
            "race_no": usable[marginal["leg_index"]]["race_no"],
            "horse_name": marginal["runner"]["horse_name"],
            "gain": marginal["gain"],
            "extra_cost": marginal["extra_cost"],
            "break_even_dividend": marginal["extra_cost"] / marginal["gain"]
            if marginal["gain"] > 0 else None,
        } if marginal else None),
        "next_step": _next_step(usable, widths, unit_cost),
    }


def _combos(widths: list[int]) -> int:
    n = 1
    for w in widths:
        n *= max(w, 1)
    return n


def _hit(legs: list[dict], widths: list[int]) -> float:
    p = 1.0
    for leg, w in zip(legs, widths):
        p *= sum(r["probability"] for r in leg["runners"][:w])
    return p


def _rebalance(legs: list[dict], widths: list[int], ceiling: int) -> list[int]:
    """Fix the one way a greedy fill goes wrong.

    Marginal cost is the product of the OTHER legs' widths, so widening a leg
    that is already wide raises the ticket price by a smaller FRACTION than
    opening up a narrow one -- going 8 to 9 runners is a 12% rise where 1 to 2
    is a doubling. Efficiency per rupee is the right measure, but it leaves the
    greedy with a standing bias toward the leg it has already spent on, and
    tickets come out lopsided: one race fielded, another left on a single
    runner that then wins at 9.00.

    So after the fill, try every one-runner move from one leg to another and
    keep any that raises the hit probability within the same budget. Cheap --
    a handful of passes over a few legs -- and it recovers most of what an
    exhaustive search would find without the exhaustive search."""
    best = _hit(legs, widths)
    improved = True
    while improved:
        improved = False
        for i in range(len(legs)):
            if widths[i] <= 1:
                continue
            for j in range(len(legs)):
                if i == j or widths[j] >= len(legs[j]["runners"]):
                    continue
                trial = widths[:]
                trial[i] -= 1
                trial[j] += 1
                if _combos(trial) > ceiling:
                    continue
                value = _hit(legs, trial)
                if value > best + 1e-12:
                    widths, best, improved = trial, value, True
    return widths


def _leg_shape(runners: list[dict]) -> str:
    """Plain-language read on how hard a leg is, from the shape of its market.

    This is what decides where combinations go, so it is worth stating rather
    than leaving implicit in the allocation."""
    if not runners:
        return "no field"
    top = runners[0]["probability"]
    second = runners[1]["probability"] if len(runners) > 1 else 0.0
    if top >= 0.45 and top - second >= 0.20:
        return "standout -- one runner carries the leg"
    if top >= 0.35:
        return "solid favourite"
    if top - second <= 0.05 and top < 0.25:
        return "wide open -- the expensive kind of leg"
    return "competitive"


def _book_quality(legs: list[dict]) -> str:
    books = [l["book"] for l in legs]
    if all(b == "none" for b in books):
        return "none"
    if any(b == "none" for b in books):
        return "mixed"
    if all(b == "full" for b in books):
        return "full"
    return "partial"


def _next_step(usable: list[dict], widths: list[int], unit_cost: float) -> dict | None:
    """What the next rupee would buy, so the budget number can be argued with."""
    current = _combos(widths)
    best = None
    for i, leg in enumerate(usable):
        if widths[i] >= len(leg["runners"]):
            continue
        trial = widths[:]
        trial[i] += 1
        gain = leg["runners"][widths[i]]["probability"]
        for j, other in enumerate(usable):
            if j == i:
                continue
            gain *= sum(r["probability"] for r in other["runners"][:widths[j]])
        extra = (_combos(trial) - current) * unit_cost
        if extra <= 0:
            continue
        cand = {"race_no": leg["race_no"],
                "horse_name": leg["runners"][widths[i]]["horse_name"],
                "gain": gain, "extra_cost": extra, "value": gain / extra}
        if best is None or cand["value"] > best["value"]:
            best = cand
    return best


def dividend_reality(conn, pool: str, venue: str | None = None) -> dict:
    """What this pool has actually paid for a complete ticket, from archived
    result pages.

    A break-even dividend means nothing without something to compare it to,
    and a jackpot dividend is not a fixed price -- it is the pool divided by
    however many tickets held the same line, so the same bet can pay 317 one
    week and 91,296 the next. Only the full tier is counted: the 30%
    consolation is a different bet.

    Falls back to the small sample baked into this module while the archive is
    still filling up, and says which it used."""
    family = pool_family(pool)
    pairs = []
    try:
        q = ("SELECT pool, dividend, tickets FROM pool_dividends "
             "WHERE dividend IS NOT NULL AND tier != '30%'")
        params: tuple = ()
        if venue:
            q += " AND venue = ?"
            params += (venue,)
        # Filtered by family in Python rather than by SQL LIKE, because a LIKE
        # on 'JACKPOT' also matches 'MINI JACKPOT' and 'SUPER JACKPOT' -- three
        # pools with wildly different dividends that must not be averaged.
        pairs = [(r["dividend"], r["tickets"]) for r in conn.execute(q, params)
                 if pool_family(r["pool"]) == family]
    except Exception:
        pairs = []

    source, of_pool = "archive", family
    if len(pairs) < 4:
        # Too thin to say anything. Fall back to the nearest family with a real
        # sample -- and name it, because "what a treble pays" is not an answer
        # to "what a super jackpot pays" and the UI must not imply it is.
        of_pool = "Treble" if family == "Treble" else "Jackpot"
        pairs = OBSERVED_DIVIDEND_SAMPLE[of_pool.upper()]
        source = "sample"
    if not pairs:
        return {"n": 0, "source": "none", "of_pool": pool}

    divs = sorted(d for d, _ in pairs)
    tks = sorted(t for _, t in pairs if t)
    return {
        "n": len(divs), "source": source, "of_pool": of_pool,
        "median": divs[len(divs) // 2],
        "low": divs[0], "high": divs[-1],
        "median_tickets": tks[len(tks) // 2] if tks else None,
    }


# --- Replaying the strategy over the archive -------------------------------

def backtest_strategy(conn, n_legs: int = 5, unit_cost: float = 5.0,
                      budget: float = 1200.0, venue: str | None = None) -> dict:
    """Replay this planner over every complete archived race day.

    Deliberately market-only and SQL-fast: the selection this planner makes is
    market-led, so replaying it needs starting prices and winners, not a
    re-score of every runner.

    THE ONE THING THIS CANNOT DO, stated plainly: a jackpot ticket has to be
    submitted before the FIRST leg runs, and a starting price is only known
    after its own race. So this replay ranks legs on prices that were not
    available at the moment the bet had to be struck. It measures how good the
    method is when its input is good, and it is an upper bound on what the
    same method achieves off race-morning forecast prices. What closes that
    gap is the odds-snapshot archive (`--snapshot` in scripts/daily.py); on
    the one Kolkata card where both exist, the forecast agreed with the
    starting price on the favourite in 6 of 7 races, which is encouraging and
    nothing like enough to conclude anything.
    """
    q = """SELECT ra.id, ra.race_date, ra.venue, ra.race_number FROM races ra
           WHERE ra.circuit='India'"""
    params: tuple = ()
    if venue:
        q += " AND ra.venue=?"
        params = (venue,)
    q += " ORDER BY ra.race_date, ra.venue, ra.race_number"

    days: dict = {}
    for row in conn.execute(q, params):
        runners = conn.execute(
            """SELECT h.id hid, r.odds_sp, res.finish_position
               FROM runs r JOIN horses h ON h.id=r.horse_id
               LEFT JOIN results res ON res.run_id = r.id
               WHERE r.race_id=? AND r.scratched=0""", (row["id"],)).fetchall()
        winner = next((x["hid"] for x in runners if x["finish_position"] == 1), None)
        priced = [(x["hid"], x["odds_sp"]) for x in runners if x["odds_sp"] and x["odds_sp"] > 0]
        if winner is None or len(priced) < 4:
            continue
        order = [hid for hid, _ in sorted(priced, key=lambda t: t[1])]
        probs = _probs_from_sp([sp for _, sp in sorted(priced, key=lambda t: t[1])])
        days.setdefault((row["race_date"], row["venue"]), {})[row["race_number"]] = {
            "order": order, "probs": probs, "winner": winner,
        }

    results = []
    for (race_date, v), races in days.items():
        nos = sorted(races)
        if len(nos) < n_legs:
            continue
        legs = [races[n] for n in nos[-n_legs:]]
        outcome = _replay_greedy(legs, budget, unit_cost)
        if outcome:
            results.append({"race_date": race_date, "venue": v, **outcome})

    if not results:
        return {"days": 0}
    hits = sum(1 for r in results if r["hit"])
    even = _replay_even(days, n_legs, budget, unit_cost)
    return {
        "days": len(results),
        "hit_rate": hits / len(results),
        "hits": hits,
        "average_combinations": sum(r["combinations"] for r in results) / len(results),
        "average_cost": sum(r["cost"] for r in results) / len(results),
        "predicted_rate": sum(r["predicted"] for r in results) / len(results),
        "even_spread": even,
        "confidence_pp": 98 / math.sqrt(len(results)),
    }


def _probs_from_sp(sps: list[float]) -> list[float]:
    fair = devig_power([sp + 1.0 for sp in sps])
    vals = [f if f else 0.0 for f in fair]
    raised = [v ** FAVOURITE_BIAS_EXPONENT for v in vals]
    total = sum(raised) or 1.0
    return [r / total for r in raised]


def _replay_greedy(legs: list[dict], budget: float, unit_cost: float) -> dict | None:
    ceiling = int(budget // unit_cost)
    if ceiling < 1:
        return None
    widths = [1] * len(legs)
    while True:
        current = _combos(widths)
        best = None
        for i, leg in enumerate(legs):
            if widths[i] >= len(leg["probs"]):
                continue
            trial = widths[:]
            trial[i] += 1
            if _combos(trial) > ceiling:
                continue
            gain = leg["probs"][widths[i]]
            for j, other in enumerate(legs):
                if j != i:
                    gain *= sum(other["probs"][:widths[j]])
            extra = (_combos(trial) - current) * unit_cost
            value = gain / extra
            if best is None or value > best[0]:
                best = (value, i)
        if best is None:
            break
        widths[best[1]] += 1
    widths = _rebalance(
        [{"runners": [{"probability": p} for p in leg["probs"]]} for leg in legs],
        widths, ceiling)
    predicted = 1.0
    for i, leg in enumerate(legs):
        predicted *= sum(leg["probs"][:widths[i]])
    hit = all(leg["winner"] in leg["order"][:widths[i]] for i, leg in enumerate(legs))
    return {"hit": hit, "combinations": _combos(widths),
            "cost": _combos(widths) * unit_cost, "predicted": predicted,
            "widths": widths}


def _replay_even(days: dict, n_legs: int, budget: float, unit_cost: float) -> dict:
    """The old behaviour -- the same number of runners in every leg -- at the
    widest spread the same budget allows, for a like-for-like comparison."""
    ceiling = int(budget // unit_cost)
    k = 1
    while (k + 1) ** n_legs <= ceiling:
        k += 1
    tried = hits = 0
    for races in days.values():
        nos = sorted(races)
        if len(nos) < n_legs:
            continue
        legs = [races[n] for n in nos[-n_legs:]]
        tried += 1
        if all(leg["winner"] in leg["order"][:k] for leg in legs):
            hits += 1
    return {"runners_per_leg": k, "combinations": k ** n_legs,
            "hit_rate": (hits / tried) if tried else 0.0, "days": tried}
