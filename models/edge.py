"""Where the money actually is: price bands, and which of the two markets.

THE FINDING THIS MODULE EXISTS FOR

Every Indian club's result page publishes two independent prices for the same
race, and this project treated them as one thing until it looked. `odds_sp` is
the ON-COURSE BOOKMAKER RING's starting price. `dividend_win` is the TOTE's
pari-mutuel payout. They are not a transform of one another -- the tote pays
more than the ring on 10-22% of winners, and the ratio between them runs from
0.37 to over 1.

Backing every runner in every archived race, 4,302 bets across 488 races:

    on-course bookmaker ring     -38.4%
    tote                         -46.3%

The tote costs an extra 7.9 points of every rupee staked, on every bet, before
anyone has an opinion about a horse. That is larger than every handicapping
signal in this project put together, it requires no model, and it applies to
every bet you will ever place. It is also the only number here with a sample
big enough to be past arguing about.

Inside the ring, the return depends almost entirely on price, and the pattern
is the oldest one in racing -- favourite-longshot bias, unusually severe:

    ring SP band    bets   win rate   price implies   ROI in the ring
    1.00-2.00        167     67.1%        60.3%           +11.7%
    2.00-3.00        218     41.7%        41.3%            +1.1%
    3.00-4.50        290     29.0%        28.5%            +1.1%
    4.50-7.00        488     15.4%        18.4%           -16.1%
    7.00-11.0        584     10.1%        12.1%           -16.4%
    11.0-21.0       1295      3.9%         7.4%           -47.6%
    21.0+           1260      1.3%         4.5%           -70.5%

WHAT IS AND IS NOT ESTABLISHED, STATED BEFORE ANY OF IT IS ACTED ON

The market-choice number is established: 4,302 bets is a large sample and the
effect is enormous.

The short-price band is NOT established. Its 95% confidence interval is
[-0.8%, +23.6%] on 167 bets -- it touches zero, so the honest reading is "this
looks like an edge and might be nothing". Two things stop it being dismissed:
it comes out at +11.8% in the first half of the archive by date and +11.5% in
the second, and it is positive at five of six venues. Favourite-longshot bias
is also the most replicated inefficiency in racing and is strongest exactly
where takeout is heavy and money is unsophisticated, which describes this
circuit. So: a hypothesis worth staking small money on and logging, not a
proven edge to size up on. log_bet() and settle_bet() exist so that it gets
settled by evidence rather than by feel.

One filter that did NOT survive and is deliberately absent: backing the ring
favourite only when the ring and the tote disagree about who the favourite is
flipped sign between subsets (-20.7% on one cut, -3.6% on another, n=66). It
is reported as information and never as a rule.

PLACE BETS ARE NOT COVERED, and the reason is that there is no data. Not one
`dividend_place` exists in the archive -- the club result pages this project
parses publish a place dividend as a combined string that never made it into
storage per runner. So no place-market ROI has been measured, at any price, in
either market. The Place Bets tab remains a model shortlist to price up by
hand, and this module says nothing about it rather than guessing.

Every number above is recomputed from the current archive by measure_bands()
and market_gap() rather than read from a constant, so the app can never quote
a figure the data no longer supports.
"""
import math

# Band edges in decimal ring odds. Chosen before the ROI was looked at, on the
# natural break points of a bookmaker's board (odds-on, evens to 2/1, and so
# on) rather than fitted to where the returns happened to turn -- a band
# boundary tuned to the outcome is how a backtest invents an edge.
BAND_EDGES = [1.0, 2.0, 3.0, 4.5, 7.0, 11.0, 21.0, 1000.0]

# The band this module will actually recommend, and the one whose confidence
# interval is quoted everywhere it appears.
EDGE_BAND = (1.0, 2.0)

# Below this the archive says returns are negative and worsening with price.
# Not a soft warning: 4.50 upward lost 16% and 11.00 upward lost 48%.
SKIP_ABOVE = 4.5

# A measured band means nothing without the sample behind it. Under this many
# bets the app reports the band as unmeasured rather than quoting a rate.
MIN_BAND_SAMPLE = 40


def _bets_from_archive(conn) -> list[dict]:
    """One row per archived race carrying a full ring book, a result, and both
    markets' payout for the winner.

    Races are the unit, not runners: outcomes inside one race are perfectly
    dependent (exactly one horse wins), so a confidence interval computed over
    runners would be far too narrow. Everything downstream resamples races."""
    rows = conn.execute(
        """SELECT ra.id rid, ra.venue, ra.race_date, ra.tote_favourite,
                  h.name, r.odds_sp, res.finish_position, res.dividend_win
           FROM races ra
           JOIN runs r ON r.race_id = ra.id
           JOIN horses h ON h.id = r.horse_id
           LEFT JOIN results res ON res.run_id = r.id
           WHERE ra.circuit = 'India' AND r.scratched = 0""",
    ).fetchall()
    by_race: dict = {}
    for r in rows:
        by_race.setdefault(r["rid"], []).append(r)

    out = []
    for rid, runners in by_race.items():
        priced = [x for x in runners if x["odds_sp"] and x["odds_sp"] > 0]
        winner = next((x for x in runners if x["finish_position"] == 1), None)
        if not winner or len(priced) < 4:
            continue
        ring_fav = min(priced, key=lambda x: x["odds_sp"])
        tied = sum(1 for x in priced if abs(x["odds_sp"] - ring_fav["odds_sp"]) < 1e-9) > 1
        out.append({
            "race_id": rid, "venue": runners[0]["venue"], "date": runners[0]["race_date"],
            "prices": [x["odds_sp"] + 1.0 for x in priced],
            "winner_ring": (winner["odds_sp"] + 1.0) if winner["odds_sp"] else None,
            "winner_tote": (winner["dividend_win"] / 10.0) if winner["dividend_win"] else None,
            "ring_favourite": ring_fav["name"].upper(),
            "ring_favourite_price": ring_fav["odds_sp"] + 1.0,
            "ring_favourite_tied": tied,
            "tote_favourite": (runners[0]["tote_favourite"] or "").strip().upper(),
            "winner_name": winner["name"].upper(),
        })
    return out


def _roi(races: list[dict], lo: float, hi: float, market: str) -> tuple[int, int, float]:
    """Stake 1 on every runner priced inside [lo, hi). Returns (bets, wins, roi)."""
    bets = wins = 0
    ret = 0.0
    for d in races:
        bets += sum(1 for p in d["prices"] if lo <= p < hi)
        w = d["winner_ring"]
        if w is not None and lo <= w < hi:
            wins += 1
            ret += w if market == "ring" else (d["winner_tote"] or 0.0)
    return bets, wins, ((ret - bets) / bets if bets else 0.0)


def _bootstrap(races: list[dict], lo: float, hi: float, market: str,
               draws: int = 2000, seed: int = 17) -> tuple[float, float]:
    """95% interval on the band's ROI, resampling whole races.

    Deterministic seed so the app does not show a different interval on every
    rerun -- a confidence interval that flickers reads as noise about noise."""
    import random
    rng = random.Random(seed)
    n = len(races)
    if n < 20:
        return (0.0, 0.0)
    out = []
    for _ in range(draws):
        sample = [races[rng.randrange(n)] for _ in range(n)]
        bets, _, roi = _roi(sample, lo, hi, market)
        if bets:
            out.append(roi)
    if not out:
        return (0.0, 0.0)
    out.sort()
    return out[int(0.025 * len(out))], out[int(0.975 * len(out))]


def measure_bands(conn, market: str = "ring", with_intervals: bool = True) -> list[dict]:
    """ROI per price band, recomputed from whatever is currently archived."""
    races = [d for d in _bets_from_archive(conn)
             if d["winner_ring"] and (market == "ring" or d["winner_tote"])]
    bands = []
    for lo, hi in zip(BAND_EDGES, BAND_EDGES[1:]):
        bets, wins, roi = _roi(races, lo, hi, market)
        if not bets:
            continue
        implied = sum(1 / p for d in races for p in d["prices"] if lo <= p < hi) / bets
        band = {
            "lo": lo, "hi": hi, "bets": bets, "wins": wins,
            "win_rate": wins / bets, "implied": implied, "roi": roi,
            "measured": bets >= MIN_BAND_SAMPLE,
        }
        if with_intervals and band["measured"]:
            band["ci_low"], band["ci_high"] = _bootstrap(races, lo, hi, market)
        bands.append(band)
    return bands


def market_gap(conn) -> dict:
    """What the choice of market costs, over every runner in the archive.

    The most useful number this project has produced, and the only one whose
    sample puts it beyond argument."""
    races = [d for d in _bets_from_archive(conn) if d["winner_ring"] and d["winner_tote"]]
    if not races:
        return {"races": 0}
    bets = sum(len(d["prices"]) for d in races)
    ring = sum(d["winner_ring"] for d in races)
    tote = sum(d["winner_tote"] for d in races)
    return {
        "races": len(races), "bets": bets,
        "ring_roi": (ring - bets) / bets,
        "tote_roi": (tote - bets) / bets,
        "gap_pp": (ring - tote) / bets,
    }


def favourite_agreement(conn) -> dict:
    """How often the ring and the tote name the same favourite, and what
    backing the ring favourite returned in each case.

    Reported, never enforced. The disagreement subset is small and its sign is
    unstable between cuts, so it belongs on screen as context and not in a
    rule."""
    races = [d for d in _bets_from_archive(conn)
             if d["winner_ring"] and d["winner_tote"]
             and d["tote_favourite"] and not d["ring_favourite_tied"]]
    if not races:
        return {"races": 0}

    def summarise(sample):
        n = len(sample)
        if not n:
            return None
        won = [d for d in sample if d["winner_name"] == d["ring_favourite"]]
        return {"n": n, "wins": len(won), "win_rate": len(won) / n,
                "ring_roi": (sum(d["winner_ring"] for d in won) - n) / n,
                "tote_roi": (sum(d["winner_tote"] for d in won) - n) / n}

    agree = [d for d in races if d["ring_favourite"] == d["tote_favourite"]]
    return {
        "races": len(races),
        "agreement_rate": len(agree) / len(races),
        "agree": summarise(agree),
        "disagree": summarise([d for d in races if d["ring_favourite"] != d["tote_favourite"]]),
    }


# --- The race-day decision -------------------------------------------------

def qualify(ring_price: float | None, bands: list[dict],
            is_ring_favourite: bool | None = None,
            tote_agrees: bool | None = None) -> dict:
    """Verdict on one price, from the measured bands rather than from the model.

    This deliberately ignores the handicapping model. The model's own top pick
    has never beaten the market in this archive, while price band has a
    measurable relationship with return -- so the decision is made on the
    number the board is showing, and the model is left to decide which races
    are worth walking over to look at.

    'BET' is only ever returned for the band whose measured ROI is positive AND
    whose sample is large enough to mean anything. Everything else is THIN or
    SKIP, because a band that has not been measured is not an opportunity, it
    is an unknown.
    """
    if not ring_price or ring_price <= 1.0:
        return {"verdict": "NO PRICE", "band": None,
                "reason": "Enter the price the ring is showing, as a decimal (5/2 is 3.50)."}

    band = next((b for b in bands if b["lo"] <= ring_price < b["hi"]), None)
    if band is None:
        return {"verdict": "SKIP", "band": None,
                "reason": "Longer than anything the archive has a usable sample for."}

    label = f"{band['lo']:.2f}-{band['hi']:.2f}"
    notes = []
    if is_ring_favourite is False:
        notes.append("not the ring favourite -- almost every runner in the paying band is, "
                     "so this is outside what was measured")
    if tote_agrees is False:
        notes.append("the tote board makes a different runner favourite; that disagreement "
                     "was NOT a reliable filter either way, so it is a reason to look twice, "
                     "not a reason to pass")

    if ring_price >= SKIP_ABOVE:
        return {"verdict": "SKIP", "band": band, "notes": notes,
                "reason": f"At {ring_price:.2f} you are in the {label} band, which returned "
                          f"{band['roi'] * 100:+.0f}% over {band['bets']} archived bets. Long "
                          f"prices here are not value, they are the thing paying for everyone "
                          f"else's edge."}

    if not band["measured"]:
        return {"verdict": "THIN", "band": band, "notes": notes,
                "reason": f"Only {band['bets']} archived bets in the {label} band -- too few to "
                          f"quote a return. No measured opinion either way."}

    lo, hi = EDGE_BAND
    if lo <= ring_price < hi and band["roi"] > 0:
        ci = (f" (95% interval {band['ci_low'] * 100:+.0f}% to {band['ci_high'] * 100:+.0f}%, "
              f"so it may be nothing)" if "ci_low" in band else "")
        return {"verdict": "BET", "band": band, "notes": notes,
                "reason": f"{ring_price:.2f} sits in the one band the archive shows a positive "
                          f"return for: {band['win_rate'] * 100:.0f}% of these won against "
                          f"{band['implied'] * 100:.0f}% implied by the price, worth "
                          f"{band['roi'] * 100:+.0f}% over {band['bets']} bets{ci}. Small stake."}

    return {"verdict": "THIN", "band": band, "notes": notes,
            "reason": f"The {label} band returned {band['roi'] * 100:+.0f}% over "
                      f"{band['bets']} bets -- close enough to break-even that the margin is "
                      f"inside the measurement error. No edge to press."}


# --- Logging, so this gets settled by evidence ------------------------------

def log_bet(conn, race_date: str, venue: str, race_no: int, horse_name: str,
            market: str, price: float, stake: float, band: str,
            verdict: str, notes: str | None = None) -> int:
    """Record a bet placed under this rule.

    The whole point of the table: the +11.5% is a hypothesis with a confidence
    interval that touches zero, and the only thing that will ever settle it is
    a run of real bets at real prices. Logged with the price and band so the
    result can be attributed rather than just totalled."""
    cur = conn.execute(
        """INSERT INTO edge_bets (race_date, venue, race_no, horse_name, market,
                                   price, stake, band, verdict, notes)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (race_date, venue, race_no, horse_name.upper(), market, price, stake,
         band, verdict, notes),
    )
    conn.commit()
    return cur.lastrowid


def settle_bet(conn, bet_id: int, outcome: str, payout: float | None = None) -> None:
    """outcome: 'won' | 'lost' | 'void'. Payout is the total returned, stake
    included, so a losing bet is 0 rather than blank."""
    conn.execute(
        "UPDATE edge_bets SET outcome=?, payout=? WHERE id=?",
        (outcome, payout if payout is not None else (0.0 if outcome == "lost" else None), bet_id),
    )
    conn.commit()


def settle_from_results(conn, race_date: str) -> dict:
    """Grade any pending bet on a date whose race has a stored result.

    Graded against the finishing position rather than the price, and paid at
    the price that was actually taken -- the whole reason for logging the price
    is that settling at a later or different number would measure a bet nobody
    placed."""
    pending = conn.execute(
        "SELECT * FROM edge_bets WHERE race_date=? AND outcome IS NULL", (race_date,),
    ).fetchall()
    settled = won = 0
    for b in pending:
        row = conn.execute(
            """SELECT res.finish_position FROM races ra
               JOIN runs r ON r.race_id = ra.id
               JOIN horses h ON h.id = r.horse_id
               LEFT JOIN results res ON res.run_id = r.id
               WHERE ra.race_date=? AND ra.venue=? AND ra.race_number=?
                 AND UPPER(h.name)=?""",
            (b["race_date"], b["venue"], b["race_no"], b["horse_name"]),
        ).fetchone()
        if not row or row["finish_position"] is None:
            continue
        is_win = row["finish_position"] == 1
        settle_bet(conn, b["id"], "won" if is_win else "lost",
                   (b["stake"] * b["price"]) if is_win else 0.0)
        settled += 1
        won += 1 if is_win else 0
    return {"settled": settled, "won": won, "pending": len(pending) - settled}


def performance(conn) -> dict:
    """Your own record under this rule, and how far it can be trusted yet.

    Deliberately refuses a verdict under MIN_LOGGED_FOR_VERDICT bets, for the
    same reason scripts/early_price.py refuses one under 30 races: a noisy
    number that looks like an edge is worse than no number, because it is the
    one you act on."""
    rows = conn.execute(
        "SELECT * FROM edge_bets WHERE outcome IN ('won','lost')").fetchall()
    if not rows:
        return {"n": 0, "verdict": "Nothing logged yet."}
    staked = sum(r["stake"] or 0 for r in rows)
    returned = sum(r["payout"] or 0 for r in rows)
    by_band: dict = {}
    for r in rows:
        slot = by_band.setdefault(r["band"] or "?", {"n": 0, "won": 0, "staked": 0.0, "ret": 0.0})
        slot["n"] += 1
        slot["won"] += 1 if r["outcome"] == "won" else 0
        slot["staked"] += r["stake"] or 0
        slot["ret"] += r["payout"] or 0
    n = len(rows)
    roi = ((returned - staked) / staked) if staked else 0.0
    # 95% half-width on ROI, from the spread of a win/lose bet at the average
    # price -- crude, but it stops a run of six winners reading as proof.
    avg_price = (sum((r["price"] or 0) for r in rows) / n) if n else 0
    sd = math.sqrt(max(avg_price - 1, 0.01)) if avg_price else 1.0
    half = 1.96 * sd / math.sqrt(n)
    return {
        "n": n, "won": sum(1 for r in rows if r["outcome"] == "won"),
        "staked": staked, "returned": returned, "roi": roi,
        "by_band": by_band,
        "roi_half_width": half,
        "verdict": (
            f"{n} settled bets. Under {MIN_LOGGED_FOR_VERDICT} this says nothing at all -- "
            f"the interval on the ROI is wider than the edge being tested."
            if n < MIN_LOGGED_FOR_VERDICT else
            f"{n} settled bets, ROI {roi * 100:+.1f}% give or take {half * 100:.0f} points. "
            f"The archive predicted about +11%; if your own number sits well below that after "
            f"another fifty, the band edge was not real and the correct response is to stop."
        ),
    }


# Below this many settled bets the logged record is noise. Set at the same
# order as the calibration thresholds elsewhere in the project rather than at
# a number that would let a good fortnight look like a finding.
MIN_LOGGED_FOR_VERDICT = 50
