"""Turning a suggestion into a bet you can actually place -- safely.

There is a gap between "this selection is value" and "place this bet", and it
is where most automated betting loses money. The edge was measured against one
book's price. The bet gets placed at another book's price. If the second price
is shorter, the edge is gone -- and an automated system will keep placing
anyway, every day, faster than a human ever could.

The control that closes that gap is a MINIMUM ACCEPTABLE PRICE on every bet.
Never place at "whatever the book is showing". Work out the price below which
the bet stops being worth making, attach it to the bet, and refuse to place if
the book will not meet it. This is standard practice in real betting
automation and it is the single most important safeguard here, because it
converts "my bookmaker has worse prices" from a silent, compounding loss into
a visible, harmless skipped bet.

Concretely: a selection we think is a 20% chance is worth backing at 5.50 and
not worth backing at 4.60. Attach min_acceptable_odds = 5.15 (a 3% edge floor)
and the bot simply does not fire when only 4.60 is on offer. You lose nothing.
You just do not bet.

Nothing in this module places a bet. It prepares, prices, validates and
records them. Placement lives behind an adapter (scrapers/bookmaker.py) that
defaults to dry-run and requires explicit opt-in.
"""
from dataclasses import asdict, dataclass, field

from models.odds import expected_value, kelly_fraction

# Edge required at the price actually on offer, not the price the edge was
# found at. Slightly above the discovery threshold in models/parlay.py so a
# bet that only just qualified does not survive the price moving against it.
DEFAULT_MIN_EDGE_AT_PLACEMENT = 0.03

# Refuse to place if the book's price has dropped more than this below the
# price the selection was found at, even when it still clears the edge floor.
# A big move usually means the market knows something we do not.
MAX_PRICE_DRIFT = 0.25


@dataclass
class PreparedBet:
    """A bet with everything needed to place it, and everything needed to
    refuse to place it."""
    race_id: int | None
    venue: str
    race_no: int
    horse_name: str
    market: str                     # 'win' | 'place'
    stake: float
    probability: float              # our blended estimate
    reference_odds: float           # the price the edge was found at
    reference_source: str           # ...and where that price came from
    min_acceptable_odds: float      # do not place below this, ever
    expected_value_at_reference: float
    notes: str = ""
    status: str = "draft"           # draft -> validated -> placed | skipped
    placed_odds: float | None = None
    skip_reason: str | None = None

    def to_dict(self) -> dict:
        return asdict(self)


def minimum_acceptable_odds(probability: float,
                            min_edge: float = DEFAULT_MIN_EDGE_AT_PLACEMENT) -> float:
    """The shortest price at which this bet still clears the edge floor.

    EV = p * odds - 1, so requiring EV >= min_edge means odds >= (1+min_edge)/p.
    At min_edge = 0 this is simply the fair price."""
    if probability <= 0:
        return float("inf")
    return round((1.0 + min_edge) / probability, 3)


def prepare_single(leg: dict, stake: float,
                   min_edge: float = DEFAULT_MIN_EDGE_AT_PLACEMENT) -> PreparedBet:
    """A qualifying leg from models/parlay.py -> a placeable bet."""
    p = leg["blended_probability"]
    return PreparedBet(
        race_id=leg.get("race_id"),
        venue=leg.get("venue", ""),
        race_no=leg.get("race_no", 0),
        horse_name=leg["horse_name"],
        market=leg["market"],
        stake=round(stake, 2),
        probability=p,
        reference_odds=leg["decimal_odds"],
        reference_source=leg.get("odds_source", "unknown"),
        min_acceptable_odds=minimum_acceptable_odds(p, min_edge),
        expected_value_at_reference=leg["expected_value"],
        notes=leg.get("reasoning") or "",
    )


def prepare_parlay(parlay: dict, stake: float | None = None,
                   min_edge: float = DEFAULT_MIN_EDGE_AT_PLACEMENT) -> dict:
    """A multi -> a placeable slip with a floor on the COMBINED price.

    The floor applies to the whole multi rather than leg by leg: a book that is
    short on one leg and long on another can still offer an acceptable
    combined price, and rejecting per-leg would throw that away."""
    stake = stake if stake is not None else parlay.get("suggested_stake", 0)
    hit = parlay["hit_probability"]
    return {
        "kind": "parlay",
        "label": parlay.get("label", ""),
        "leg_count": parlay["leg_count"],
        "legs": [{
            "venue": l["venue"], "race_no": l["race_no"], "horse_name": l["horse_name"],
            "market": l["market"], "reference_odds": l["decimal_odds"],
        } for l in parlay["legs"]],
        "stake": round(stake, 2),
        "probability": hit,
        "reference_odds": parlay["combined_odds"],
        "min_acceptable_odds": minimum_acceptable_odds(hit, min_edge),
        "expected_value_at_reference": parlay["expected_value"],
        "status": "draft",
    }


def validate_against_live(bet: PreparedBet | dict, live_odds: float | None,
                          max_drift: float = MAX_PRICE_DRIFT) -> dict:
    """Go / no-go against the price the book is ACTUALLY showing right now.

    Three ways a bet gets refused, in order of how often they bite:
      1. No price -- the market is gone, suspended, or the horse is scratched.
      2. Price below the floor -- the edge does not exist at this book.
      3. Price collapsed -- still above the floor, but it has moved so far
         that the market is telling us something our model has not priced.
    """
    is_obj = isinstance(bet, PreparedBet)
    ref = bet.reference_odds if is_obj else bet["reference_odds"]
    floor = bet.min_acceptable_odds if is_obj else bet["min_acceptable_odds"]
    prob = bet.probability if is_obj else bet["probability"]

    if not live_odds or live_odds <= 1.0:
        return {"place": False, "reason": "no live price -- market suspended, closed, or scratched",
                "live_odds": live_odds}

    if live_odds < floor:
        shortfall = (floor - live_odds) / floor * 100
        return {
            "place": False,
            "reason": (f"price {live_odds:.2f} is below the {floor:.2f} floor "
                       f"({shortfall:.0f}% short) -- at this book the edge is gone. "
                       f"EV here would be {expected_value(prob, live_odds) * 100:+.1f}%."),
            "live_odds": live_odds,
            "expected_value": round(expected_value(prob, live_odds), 4),
        }

    drift = (ref - live_odds) / ref if ref else 0
    if drift > max_drift:
        return {
            "place": False,
            "reason": (f"price has shortened from {ref:.2f} to {live_odds:.2f} "
                       f"({drift * 100:.0f}%). It still clears the floor, but a move that big "
                       f"usually means the market knows something the model does not."),
            "live_odds": live_odds,
        }

    return {
        "place": True,
        "live_odds": live_odds,
        "expected_value": round(expected_value(prob, live_odds), 4),
        "kelly": round(kelly_fraction(prob, live_odds), 4),
        "reason": (f"price {live_odds:.2f} clears the {floor:.2f} floor; "
                   f"EV {expected_value(prob, live_odds) * 100:+.1f}%"),
    }


def build_slip(legs: list[dict], bankroll: float, stake_fraction_cap: float = 0.05,
               min_edge: float = DEFAULT_MIN_EDGE_AT_PLACEMENT,
               kelly_multiplier: float = 0.25) -> list[PreparedBet]:
    """Qualifying legs -> a staked, floored slip ready for validation."""
    bets = []
    for leg in legs:
        fraction = min(leg.get("kelly", 0) * kelly_multiplier, stake_fraction_cap)
        stake = round(bankroll * fraction, 2)
        if stake <= 0:
            continue
        bets.append(prepare_single(leg, stake, min_edge))
    return bets


def slip_summary(bets: list[PreparedBet], results: list[dict] | None = None) -> dict:
    """What the slip actually amounts to, including what got refused and why.

    The skipped list is the point of the whole module: it is the record of
    money NOT lost to a book that would not meet the price."""
    results = results or []
    placeable = [r for r in results if r.get("place")]
    skipped = [r for r in results if not r.get("place")]
    total = sum(b.stake for b in bets)
    return {
        "bets": len(bets),
        "total_stake": round(total, 2),
        "validated": len(placeable),
        "skipped": len(skipped),
        "skipped_reasons": [r.get("reason") for r in skipped],
        "stake_placed": round(sum(b.stake for b, r in zip(bets, results) if r.get("place")), 2)
        if results else 0.0,
    }
