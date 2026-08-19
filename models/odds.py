"""Market odds -> fair probabilities, edge, and stake size.

This module exists because a price turns a ranking into a decision. Without
one the app can only say which horse it likes; with one it can ask the
question that actually decides whether a bet makes money -- is this price
longer than the horse's true chance? -- and refuse the bet when the answer is
no. Indian racing supplies two prices: indiarace's race-day forecast, which is
indicative and covers only the front of the field, and the settled starting
price every club prints on its result page, which is exact and arrives too
late to bet.

The three ideas everything here rests on:

1. A book does not sum to 100%. It sums to more, and the excess is the margin
   ("overround"). Measured over 396 complete Indian starting-price books the
   median is 1.203 -- about 17% of every rupee gone before anyone has an
   opinion. You must strip that margin before a market price can be compared
   to anything.

2. Stripping it proportionally is the wrong way to do it. Racing markets show
   a persistent favourite-longshot bias: longshots are systematically overbet
   and so are worse value than their price implies, while short-priced horses
   are slightly better value. Proportional de-vigging assumes the margin is
   spread evenly and therefore flatters longshots -- which is exactly where a
   naive model wants to bet. The power method applies more shrinkage to long
   prices than short ones, which is the correction we want, so it is the
   default here.

3. The market is a strong opponent. This project's own backtest (see the
   Backtest tab) found that when the model's pick and the tote favourite
   disagreed, the FAVOURITE won more often than the model's pick. That result
   is the reason blend_probabilities() defaults to weighting the market more
   heavily than the model: the model's job is to nudge a market price, not to
   overrule it.

One caveat measured rather than assumed: on Indian tote books the power
method still leaves the front of the market underpriced. Summing the top three
de-vigged probabilities gives 72% where the winner came from that top three
80% of the time. models/jackpot.py carries the correction and the evidence for
it; anything here that reports a raw de-vigged probability is reporting the
uncorrected number.
"""
import math

# Default weight given to our own model when blending with the market.
# Deliberately below 0.5: the backtest says the market is the better forecaster
# of the two, so the model is a tilt on the price, not a replacement for it.
DEFAULT_MODEL_WEIGHT = 0.35

# Typical bookmaker margin on a place market, used only when we have win odds
# but no quoted place price and have to estimate one.
ASSUMED_PLACE_OVERROUND = 1.18

# What an Indian tote win pool keeps, for warning purposes. Not a published
# figure but a measured one: the median complete starting-price book in this
# archive comes to an overround of 1.203, i.e. 1 - 1/1.203 of turnover, across
# 396 races. Exotic pools (forecast, quinella, jackpot) take out more than
# this, so a multi-leg warning built on it is conservative.
INDIA_WIN_TAKEOUT = 1.0 - 1.0 / 1.203


# --------------------------------------------------------------------------
# Basic conversions
# --------------------------------------------------------------------------

def implied_probability(decimal_odds: float | None) -> float | None:
    """Decimal odds -> the probability the price implies, margin included."""
    if not decimal_odds or decimal_odds <= 1.0:
        return None
    return 1.0 / decimal_odds


def fractional_to_decimal(numerator: float, denominator: float) -> float:
    """'5/2' -> 3.5. Indian and UK boards quote fractions; everything here
    works in decimals."""
    return numerator / denominator + 1.0


def to_decimal(odds_to_one: float | None) -> float | None:
    """Odds-to-one (the form db/ingest.py stores) -> decimal odds."""
    if odds_to_one is None:
        return None
    return odds_to_one + 1.0


def overround(decimal_odds: list[float]) -> float:
    """Sum of implied probabilities. 1.0 is a fair book; 1.20 means the
    bookmaker has built in a 20% margin and you start 20% behind."""
    total = 0.0
    for d in decimal_odds:
        p = implied_probability(d)
        if p:
            total += p
    return total


def margin_percent(decimal_odds: list[float]) -> float:
    """Overround expressed as the percentage bite, e.g. 1.20 -> 16.7%.

    This is the share of every rupee turned over that the market keeps, which
    is the number that actually matters -- not the raw overround."""
    r = overround(decimal_odds)
    if r <= 0:
        return 0.0
    return (r - 1.0) / r * 100.0


# --------------------------------------------------------------------------
# Removing the margin
# --------------------------------------------------------------------------

def devig_multiplicative(decimal_odds: list[float]) -> list[float | None]:
    """Scale every implied probability down by the same factor.

    Simple and fast, but it assumes the margin is spread evenly across the
    field, which racing markets demonstrably do not do. Kept for comparison
    and for markets with only two or three outcomes, where the bias is small."""
    raw = [implied_probability(d) for d in decimal_odds]
    total = sum(p for p in raw if p)
    if total <= 0:
        return [None] * len(decimal_odds)
    return [(p / total) if p else None for p in raw]


def devig_power(decimal_odds: list[float], tolerance: float = 1e-9,
                max_iterations: int = 200) -> list[float | None]:
    """Find k such that sum(p_i ** k) == 1, then return p_i ** k.

    Because every p_i is below 1, raising to a power k > 1 shrinks small
    probabilities proportionally more than large ones -- which is the
    favourite-longshot correction we want. Solved by bisection on k; k is
    bounded well inside [1, 8] for any real racing book, and bisection is
    used rather than Newton's method because it cannot diverge on a
    degenerate book (one runner at odds-on, say)."""
    raw = [implied_probability(d) for d in decimal_odds]
    live = [p for p in raw if p]
    if not live:
        return [None] * len(decimal_odds)
    if abs(sum(live) - 1.0) < tolerance:
        return raw

    def total_at(k: float) -> float:
        return sum(p ** k for p in live)

    lo, hi = 0.2, 8.0
    if total_at(hi) > 1.0:      # pathological book: fall back to proportional
        return devig_multiplicative(decimal_odds)
    for _ in range(max_iterations):
        mid = (lo + hi) / 2
        t = total_at(mid)
        if abs(t - 1.0) < tolerance:
            break
        if t > 1.0:
            lo = mid
        else:
            hi = mid
    k = (lo + hi) / 2
    adjusted = [(p ** k) if p else None for p in raw]
    # Bisection lands within tolerance, not exactly on it; normalise the
    # residual so downstream probabilities sum to exactly 1.
    s = sum(p for p in adjusted if p)
    return [(p / s) if p else None for p in adjusted]


def fair_probabilities(decimal_odds: list[float], method: str = "power") -> list[float | None]:
    """Market prices -> margin-free probabilities. 'power' by default; pass
    'multiplicative' to compare."""
    if method == "multiplicative":
        return devig_multiplicative(decimal_odds)
    return devig_power(decimal_odds)


def fair_odds(probability: float | None) -> float | None:
    """Probability -> the decimal price that would make the bet break even.
    Beat this price and the bet is +EV; take less and it is not."""
    if not probability or probability <= 0:
        return None
    return 1.0 / probability


# --------------------------------------------------------------------------
# Combining our opinion with the market's
# --------------------------------------------------------------------------

def blend_probabilities(model_probs: list[float], market_probs: list[float | None],
                        model_weight: float = DEFAULT_MODEL_WEIGHT) -> list[float]:
    """Weighted average of the model and the (de-vigged) market, renormalised.

    Where the market has no price for a runner, the model's number is used
    unchanged -- but note that a runner with no price is usually one you cannot
    bet anyway, so this mostly affects the normalisation of the others."""
    blended = []
    for pm, pk in zip(model_probs, market_probs):
        if pk is None:
            blended.append(pm)
        else:
            blended.append(model_weight * pm + (1 - model_weight) * pk)
    total = sum(blended)
    if total <= 0:
        n = len(blended)
        return [1 / n] * n if n else []
    return [b / total for b in blended]


# --------------------------------------------------------------------------
# Edge and stake
# --------------------------------------------------------------------------

def expected_value(probability: float, decimal_odds: float) -> float:
    """Expected profit per 1 unit staked. 0.05 means +5%; negative means the
    bet loses money in the long run no matter how it feels."""
    return probability * decimal_odds - 1.0


def edge_percent(probability: float, decimal_odds: float) -> float:
    return expected_value(probability, decimal_odds) * 100.0


def kelly_fraction(probability: float, decimal_odds: float) -> float:
    """Share of bankroll that maximises long-run growth. Returns 0 for a bet
    with no edge -- Kelly's answer to a -EV bet is to not have one."""
    if decimal_odds <= 1.0:
        return 0.0
    b = decimal_odds - 1.0
    f = (probability * decimal_odds - 1.0) / b
    return max(0.0, f)


def staking_plan(probability: float, decimal_odds: float, bankroll: float,
                 kelly_multiplier: float = 0.25, max_fraction: float = 0.05) -> dict:
    """Fractional Kelly with a hard cap.

    Full Kelly is correct only if the probability is correct, and ours is an
    estimate built on a thin archive, so a quarter-Kelly default absorbs a lot
    of estimation error for a small cost in growth rate. The cap is the more
    important of the two: it bounds the damage from a single badly wrong
    probability, which is the realistic failure mode."""
    full = kelly_fraction(probability, decimal_odds)
    fraction = min(full * kelly_multiplier, max_fraction)
    return {
        "full_kelly": round(full, 4),
        "fraction": round(fraction, 4),
        "stake": round(bankroll * fraction, 2),
        "expected_value": round(expected_value(probability, decimal_odds), 4),
    }


# --------------------------------------------------------------------------
# Place markets
# --------------------------------------------------------------------------

def places_paid(field_size: int) -> int:
    """How many places the Indian tote pays.

    8+ runners pay 3, 5-7 pay 2, 4 or fewer have no place pool at all. Not a
    guess: verified against RWITC's own published dividends, where a race with
    a place pool always shows three place figures above eight runners and two
    below. The threshold changes a place probability materially, and it is
    computed from the DECLARED field, so a late scratching can genuinely drop
    a race from three places to two."""
    if field_size >= 8:
        return 3
    if field_size >= 5:
        return 2
    return 0


def discounted_place_probabilities(win_probs: dict, n_places: int,
                                   lam: float = 0.81, mu: float = 0.65) -> dict:
    """Probability each runner finishes in the paid places, corrected for the
    known bias in the plain Harville formula.

    Harville treats the race as a sequence of independent draws: having won,
    the field re-runs for second in proportion to the remaining win
    probabilities. That assumption systematically OVERSTATES how often a
    short-priced horse fills a minor placing -- good horses tend to either win
    or, when things go wrong, finish well beaten, rather than politely
    collecting third. Backing a favourite to place on raw Harville numbers
    therefore looks like value more often than it is.

    The standard correction (Lo & Bacon-Shone) discounts the win probabilities
    by a power before using them for the second and third placings:

        P(i is 2nd | j won)        = p_i^lam / sum over remaining of p^lam
        P(i is 3rd | j 1st, k 2nd) = p_i^mu  / sum over remaining of p^mu

    with lam < 1 and mu < lam, which flattens the field for the minor placings.
    The defaults are the values fitted to thoroughbred racing in that
    literature. Setting lam = mu = 1.0 recovers plain Harville exactly, which
    is what models/staking.py's Place Bets shortlist still uses, so the
    numbers published there do not silently change under it.

    win_probs: {horse_id: (name, probability)}. Returns {horse_id: probability}.
    """
    items = [(hid, p) for hid, (_, p) in win_probs.items()]
    result = {}
    for hid_i, p_i in items:
        prob = p_i  # winning always counts as placing
        if n_places >= 2:
            for hid_j, p_j in items:
                if hid_j == hid_i:
                    continue
                denom = sum(p_k ** lam for hid_k, p_k in items if hid_k != hid_j)
                if denom > 0:
                    prob += p_j * (p_i ** lam / denom)
        if n_places >= 3:
            for hid_j, p_j in items:
                if hid_j == hid_i:
                    continue
                denom2 = sum(p_k ** lam for hid_k, p_k in items if hid_k != hid_j)
                if denom2 <= 0:
                    continue
                for hid_k, p_k in items:
                    if hid_k in (hid_i, hid_j):
                        continue
                    denom3 = sum(p_m ** mu for hid_m, p_m in items
                                 if hid_m not in (hid_j, hid_k))
                    if denom3 <= 0:
                        continue
                    prob += p_j * (p_k ** lam / denom2) * (p_i ** mu / denom3)
        result[hid_i] = min(prob, 1.0)
    return result


def estimate_place_odds(place_probability: float | None,
                        assumed_overround: float = ASSUMED_PLACE_OVERROUND) -> float | None:
    """Fair place probability -> the price a bookmaker would plausibly show.

    ESTIMATE, not a quote. Used only to sanity-check a board price when no
    place market has been captured; never used to claim an edge, because an
    edge measured against an estimated price is circular. If the real board
    pays more than this, the place bet is worth a second look."""
    if not place_probability or place_probability <= 0:
        return None
    return round(1.0 / (place_probability * assumed_overround), 3)


# --------------------------------------------------------------------------
# Multis / parlays
# --------------------------------------------------------------------------

def parlay_odds(leg_decimal_odds: list[float]) -> float:
    """Legs multiply. So does the bookmaker's margin on each leg, which is the
    entire reason multis are a bad product unless every leg is independently
    good value."""
    total = 1.0
    for d in leg_decimal_odds:
        total *= d
    return total


def parlay_probability(leg_probabilities: list[float]) -> float:
    """Product of the leg probabilities -- valid ONLY if the legs are
    independent. Two runners in the same race are the opposite of independent
    (they cannot both win), so the parlay engine never puts two legs from one
    race into the same multi."""
    total = 1.0
    for p in leg_probabilities:
        total *= p
    return total


def parlay_margin_drag(leg_overrounds: list[float]) -> float:
    """What fraction of the stake the margins eat across the whole multi.

    Four legs at a 1.15 book is 1.15^4 = 1.75, i.e. you are betting into a 43%
    margin. This single number is the most useful thing to show a punter who
    is thinking about a big accumulator."""
    product = 1.0
    for r in leg_overrounds:
        product *= max(r, 1.0)
    return (product - 1.0) / product


def compound_takeout(legs: int) -> float:
    """Pari-mutuel equivalent of the above: what a multi loses to takeout alone
    before any opinion is expressed. Three legs into an Indian tote is roughly
    43% gone at the start, which is why almost nothing clears the margin
    test."""
    return 1.0 - (1.0 - INDIA_WIN_TAKEOUT) ** legs


# --------------------------------------------------------------------------
# What a strategy actually does to a bankroll
# --------------------------------------------------------------------------

def outcome_distribution(hit_probability: float, decimal_odds: float,
                         stake: float, days: int = 30) -> dict:
    """Honest summary of what repeating one bet looks like over a month.

    Expected value alone hides the thing that actually breaks a betting plan:
    a bet that hits 30% of the time loses on 70% of days, and the losing runs
    are longer than intuition suggests. P(losing run of 5+) is included
    because that is the point at which most people abandon a plan or chase."""
    profit_if_win = stake * (decimal_odds - 1.0)
    ev_per_bet = stake * expected_value(hit_probability, decimal_odds)
    lose_p = 1.0 - hit_probability
    expected_wins = hit_probability * days
    # Probability of never hitting across the whole period.
    p_no_wins = lose_p ** days if lose_p > 0 else 0.0
    # Rough chance of at least one run of 5 consecutive losses in `days` bets.
    run = 5
    p_run = 1.0 - (1.0 - lose_p ** run) ** max(days - run + 1, 0) if lose_p > 0 else 0.0
    return {
        "profit_if_win": round(profit_if_win, 2),
        "loss_if_lose": round(stake, 2),
        "ev_per_bet": round(ev_per_bet, 2),
        "ev_over_period": round(ev_per_bet * days, 2),
        "expected_wins": round(expected_wins, 1),
        "days": days,
        "p_zero_wins": round(p_no_wins, 4),
        "p_losing_run_of_5": round(min(max(p_run, 0.0), 1.0), 3),
        "breakeven_probability": round(1.0 / decimal_odds, 4),
    }


def required_odds_for_target(target_profit: float, stake: float) -> float | None:
    """What price a single stake must find to clear a profit target.

    Deliberately blunt: it answers 'what would have to be true', which is
    usually enough to show that a daily target is asking for a price nobody
    is offering on anything with a real chance."""
    if stake <= 0:
        return None
    return round(1.0 + target_profit / stake, 3)


def sessions_to_ruin(bankroll: float, stake: float, hit_probability: float,
                     decimal_odds: float) -> dict:
    """Rough risk-of-ruin read for a flat-staking plan.

    Uses the per-bet mean and variance rather than a full random walk, so
    treat it as an order of magnitude, not a guarantee. The purpose is to make
    the shape visible: a -EV strategy has a risk of ruin of 1, given enough
    bets, whatever the short-run luck."""
    ev = expected_value(hit_probability, decimal_odds)
    win_amt = decimal_odds - 1.0
    mean = ev
    variance = (hit_probability * (win_amt - mean) ** 2
                + (1 - hit_probability) * (-1 - mean) ** 2)
    units = bankroll / stake if stake else 0
    if mean <= 0:
        return {"expected_units": units, "ruin_certain": True,
                "note": ("A negative edge means the bankroll goes to zero given enough bets -- "
                         "the only questions are how fast and how bumpy.")}
    # Standard exponential approximation to gambler's ruin for a +EV bettor.
    risk = math.exp(-2 * mean * units / variance) if variance > 0 else 0.0
    return {"expected_units": units, "ruin_certain": False,
            "risk_of_ruin": round(min(risk, 1.0), 4),
            "note": "Assumes flat stakes and a correctly estimated probability."}
