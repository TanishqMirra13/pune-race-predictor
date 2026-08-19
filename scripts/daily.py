"""The daily routine, as one command.

    python -m scripts.daily                          # every Indian venue, today
    python -m scripts.daily --vertical Western       # just RWITC (Mumbai/Pune)
    python -m scripts.daily --venues Hyderabad Mysore
    python -m scripts.daily --settle 2026-08-13      # grade yesterday's slips
    python -m scripts.daily --results 2026-08-13     # results, SPs and pool dividends
    python -m scripts.daily --backfill 14            # archive the last 14 days
    python -m scripts.daily --snapshot               # record today's forecast prices

Run it in the morning and it will: settle yesterday, load today's fields
across the venues you asked for, pull indiarace's forecast prices, rank the
strongest model opinions, print any multi that survives the expected-value
test, and print a jackpot plan for each meeting big enough to carry one. Run
it in the evening with --settle to find out whether the day made money.

Venues are addressed by vertical -- the regional turf authorities defined in
models/verticals.py -- because that is how Indian racing is actually
organised. Asking for "Southern" gets Bangalore, Mysore and Hyderabad, and the
ones not racing today simply return nothing, which is the calendar working
rather than a failure.

Two things it deliberately will NOT do:

- It will not place a bet. Nothing in this project talks to a bookmaker.
- It will not invent a suggestion to fill the page. Most days it will print
  "nothing qualifies", because most days nothing does. A tool that always has
  a tip is a tool that is telling you what you want to hear.

On prices: indiarace publishes forecast prices for every Indian venue, but
only ON RACE DAY and only for the front four or five runners of each field.
That is too thin to de-vig into an expected-value figure -- the parlay engine
will say so and refuse the race -- but it is enough to rank a field and to
pick jackpot legs, which is what the jackpot planner uses it for. Pass
--no-odds to skip the fetch and price things by hand in the app's Odds tab.
"""
import argparse
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from db.ingest import (  # noqa: E402
    load_market_odds, odds_age_minutes, settle_parlays, store_market_odds,
    store_pool_dividends, store_racecard, store_raceresult,
)
from db.schema import get_connection, init_db  # noqa: E402
from models import jackpot as jackpot_engine  # noqa: E402
from models import parlay as parlay_engine  # noqa: E402
from models import verticals  # noqa: E402
from models.rating_engine import compute_composite_scores  # noqa: E402
from scrapers import btc, indiarace, indiarace_cards, rwitc  # noqa: E402

# Past this, the market has probably moved and an edge measured against the
# stored price is fiction rather than opportunity.
STALE_ODDS_MINUTES = 90

SCRAPER_BY_VENUE = {
    "Pune": rwitc, "Mumbai": rwitc, "Bangalore": btc,
    "Hyderabad": indiarace_cards.ForVenue("Hyderabad"),
    "Mysore": indiarace_cards.ForVenue("Mysore"),
    "Kolkata": indiarace_cards.ForVenue("Kolkata"),
    "Delhi": indiarace_cards.ForVenue("Delhi"),
}

RULE = "=" * 78


def _h(title: str) -> None:
    print(f"\n{RULE}\n{title}\n{RULE}")


def resolve_venues(args) -> list[str]:
    if args.venues:
        return list(args.venues)
    if args.vertical:
        return list(verticals.VERTICALS[args.vertical]["venues"])
    return verticals.venue_names()


# --------------------------------------------------------------------------

def load_cards(conn, date_str: str, venues: list[str]) -> list[str]:
    """Fetch and store the card for each venue that has a meeting on this date.

    A venue with no meeting returns an empty parse rather than an error -- all
    three Indian sources serve a near-empty template for a date they have
    nothing for -- so "no card" is reported as the ordinary thing it is."""
    loaded = []
    for venue in venues:
        source = SCRAPER_BY_VENUE[venue]
        try:
            races = source.parse_racecard(source.fetch_racecard_html(date_str, use_cache=True))
        except Exception as exc:
            print(f"    {venue:<12} FAILED: {exc}")
            continue
        if not races:
            print(f"    {venue:<12} no card published")
            continue
        store_racecard(conn, date_str, venue, races)
        runners = sum(len(r.get("runs", [])) for r in races)
        print(f"    {venue:<12} {len(races):>2} races, {runners:>3} runners "
              f"({verticals.vertical_of(venue)})")
        loaded.append(venue)
    return loaded


def load_forecast_odds(conn, date_str: str, venues: list[str]) -> int:
    """Pull indiarace's race-day forecast prices for the loaded meetings.

    These are INDICATIVE and partial. They will not get a race past the parlay
    engine's 80% price-coverage guard, and that guard is right -- de-vigging a
    subset of a book invents an edge instead of removing a margin. What they
    are good for is ordering the front of the market, which is what the
    jackpot planner needs and what the value ranking on Race Day shows."""
    total = 0
    for venue in venues:
        races = conn.execute(
            "SELECT id, race_number FROM races WHERE race_date=? AND venue=? ORDER BY race_number",
            (date_str, venue)).fetchall()
        field_by_race = {}
        for r in races:
            names = [row["name"] for row in conn.execute(
                """SELECT h.name FROM runs r JOIN horses h ON h.id = r.horse_id
                   WHERE r.race_id = ? AND r.scratched = 0""", (r["id"],))]
            if names:
                field_by_race[r["race_number"]] = names
        try:
            raw = indiarace.fetch_odds_html(venue, date_str, use_cache=False)
            odds_map = indiarace.parse_odds(raw) if raw else {}
        except Exception as exc:
            print(f"    {venue:<12} odds fetch failed: {exc}")
            continue
        if not odds_map:
            print(f"    {venue:<12} no prices posted yet (indiarace fills these on race-day morning)")
            continue
        res = store_market_odds(conn, date_str, venue,
                                indiarace.to_market_rows(odds_map, field_by_race),
                                source="indiarace")
        total += res["matched"]
        print(f"    {venue:<12} {res['matched']:>3} runners priced "
              f"({len(odds_map)} on the page)")
    if total:
        print("  These are forecast prices, not the board. Confirm the number where you")
        print("  actually bet before staking anything.")
    return total


def build_slate(conn, date_str: str, venues: list[str]) -> list[dict]:
    placeholders = ",".join("?" for _ in venues) or "''"
    rows = conn.execute(
        f"""SELECT id, venue, race_number, race_name, race_time_local
            FROM races WHERE race_date=? AND circuit='India' AND venue IN ({placeholders})
            ORDER BY venue, race_number""",
        (date_str, *venues),
    ).fetchall()
    slate = []
    for row in rows:
        entries = compute_composite_scores(conn, row["id"])
        if not entries:
            continue
        slate.append({
            "race_id": row["id"], "venue": row["venue"], "race_no": row["race_number"],
            "race_name": row["race_name"], "race_time": row["race_time_local"],
            "field_size": len(entries), "entries": entries,
            "odds": load_market_odds(conn, row["id"]),
            "odds_age_min": odds_age_minutes(conn, row["id"]),
        })
    return slate


def report_slate(slate: list[dict]) -> list[dict]:
    priced = [r for r in slate if r["odds"]]
    print(f"  {len(slate)} races loaded, {len(priced)} with prices attached.")
    if not priced:
        print("\n  Nothing can be evaluated without prices. Either indiarace has not posted")
        print("  today's forecast yet, or open the app's Odds tab and paste the board.")
    unpriced = [r for r in slate if not r["odds"]]
    if unpriced and priced:
        print(f"  ({len(unpriced)} race(s) still unpriced and therefore invisible to the engine.)")
    return priced


def report_top_picks(slate: list[dict], limit: int = 8) -> None:
    """Model opinion only -- no prices needed. Useful for deciding which races
    are worth going and getting a price for."""
    ranked = sorted(((r["entries"][0]["win_probability"], r, r["entries"][0]) for r in slate),
                    key=lambda t: -t[0])
    print("  Strongest model opinions on the card (go price these up first):")
    for prob, race, top in ranked[:limit]:
        marker = "$" if race["odds"] else " "
        print(f"   {marker} {race['venue'][:12]:<12} R{race['race_no']:<2} "
              f"{top['horse_name'][:22]:<22} {prob * 100:5.1f}%  (field {race['field_size']})")
    print("   '$' means prices are already stored for that race.")
    print("   Reminder from the backtest: where this model and the market disagree, the")
    print("   market wins more often. These are races to price up, not picks to back.")


def report_parlays(priced: list[dict], bankroll: float, target: float) -> None:
    stale = [r for r in priced if (r.get("odds_age_min") or 0) > STALE_ODDS_MINUTES]
    if stale:
        oldest = max(r["odds_age_min"] for r in stale)
        print(f"  !! STALE PRICES: {len(stale)} of {len(priced)} races are priced off odds up to")
        print(f"     {oldest / 60:.1f} hours old. Markets move -- any edge below may no longer")
        print(f"     exist. Re-run without --no-odds before staking anything.\n")

    card = parlay_engine.daily_parlay_card(priced, bankroll=bankroll)
    print(f"\n  {card['verdict']}\n")

    if card["singles"]:
        print("  SINGLES (same edge as a multi leg, far less variance):")
        for s in card["singles"]:
            print(f"    {s['venue'][:12]:<12} R{s['race_no']:<2} {s['horse_name'][:20]:<20} "
                  f"{s['market']:<5} @{s['decimal_odds']:<6} fair {s['fair_odds']:<6} "
                  f"edge {s['expected_value'] * 100:+5.1f}%  stake Rs{s['suggested_stake']:.0f}")

    for prof in card["profiles"].values():
        print(f"\n  {prof['label'].upper()}")
        if not prof["parlays"]:
            print("    nothing clears this profile's bar today")
            continue
        for pl in prof["parlays"]:
            print("    " + " + ".join(
                f"{l['horse_name']}({l['venue'][:10]} R{l['race_no']} {l['market']} @{l['decimal_odds']})"
                for l in pl["legs"]))
            print(f"      odds {pl['combined_odds']:.2f} (fair {pl['fair_combined_odds']:.2f}) | "
                  f"lands {pl['hit_probability'] * 100:.0f}% | edge {pl['expected_value'] * 100:+.1f}% | "
                  f"margin given up {pl['margin_drag'] * 100:.0f}%")
            print(f"      Kelly stake Rs{pl['suggested_stake']:.0f} -> "
                  f"Rs{pl['potential_profit']:.0f} if it lands")
            feas = parlay_engine.target_feasibility(pl, target, bankroll)
            if feas.get("achievable"):
                flag = "ABOVE KELLY" if feas.get("exceeds_kelly") else "within Kelly"
                print(f"      for Rs{target:.0f}: stake Rs{feas['required_stake']:.0f} ({flag}); "
                      f"{feas['probability_of_hitting'] * 100:.0f}% chance, "
                      f"EV Rs{feas['expected_pnl_at_that_stake']:+.0f}/day")

    if card["rejected_races"]:
        print(f"\n  {len(card['rejected_races'])} race(s) offered no value:")
        for r in card["rejected_races"][:10]:
            print(f"    {r['race']}: {r['reason']}")


def report_jackpot(conn, slate: list[dict], date_str: str, budget: float,
                   unit_cost: float, pool_name: str | None = None) -> None:
    """A jackpot plan per meeting, using whatever prices are stored.

    Printed even when the parlay engine has refused every race, because the
    two need different things from a price: a parlay needs a de-viggable book,
    a jackpot leg needs only the market's running order."""
    by_venue: dict = {}
    for race in slate:
        by_venue.setdefault(race["venue"], []).append(race)

    for venue, races in sorted(by_venue.items()):
        races.sort(key=lambda r: r["race_no"])
        # Prefer the pools the club itself ran at this meeting, biggest first;
        # a card carries several trebles and mini-jackpots over different legs,
        # so a generic "Treble" is a guess where the real list is available.
        published = jackpot_engine.actual_legs(conn, date_str, venue)
        if published:
            options = sorted(published, key=lambda n: (-len(published[n]), n))
        else:
            options = list(reversed(jackpot_engine.pool_options(len(races))))
        if not options:
            continue
        pool = next((o for o in options
                     if pool_name and jackpot_engine.pool_family(o) == pool_name), options[0])
        leg_numbers = published.get(pool) or jackpot_engine.suggest_legs(
            [r["race_no"] for r in races], jackpot_engine.POOLS[pool]["legs"])
        legs = [r for r in races if r["race_no"] in leg_numbers]
        if len(legs) < 2:
            continue

        plan = jackpot_engine.plan(legs, budget=budget, unit_cost=unit_cost)
        print(f"\n  {venue} -- {pool}, races {','.join(str(n) for n in leg_numbers)}"
              + ("  (as published by the club)" if published.get(pool.upper())
                 else "  (assumed: the last legs of the card -- check the club's own list)"))
        if not plan.get("affordable"):
            print(f"    {plan.get('note')}")
            continue
        for leg in plan["legs"]:
            names = ", ".join(f"{r['horse_name']} {r['probability'] * 100:.0f}%"
                              for r in leg["runners"])
            print(f"    R{leg['race_no']:<2} [{leg['shape']}] {leg['coverage'] * 100:3.0f}% covered: {names}")
        print(f"    {plan['combinations']} combinations x Rs{unit_cost:.0f} = Rs{plan['cost']:.0f}, "
              f"lands {plan['hit_probability'] * 100:.1f}% of the time")
        if plan["break_even_dividend"]:
            reality = jackpot_engine.dividend_reality(conn, pool)
            print(f"    Break-even dividend Rs{plan['break_even_dividend']:,.0f}. "
                  + (f"Archived {reality['of_pool'].lower()} pools paid a median "
                     f"Rs{reality['median']:,.0f} (range {reality['low']:,.0f}-"
                     f"{reality['high']:,.0f}, n={reality['n']}"
                     + (", from a shipped sample rather than your archive"
                        if reality["source"] == "sample" else "") + ")."
                     if reality["n"] else "No archived dividends to compare it against yet."))
            print("    Remember it is pari-mutuel: the more likely your line, the more")
            print("    tickets share the pool and the less it pays when it lands.")
        if plan["book_quality"] == "none":
            print("    !! No prices on any leg, so this whole ticket is ranked on the model")
            print("       alone -- which the archive says picks legs less than half as well")
            print("       as the market does. Fetch the forecast prices before using it.")
        elif plan["book_quality"] == "mixed":
            print("    !! Some legs have no price and are ranked on the model alone. Those")
            print("       legs are the weak point of this ticket.")


def do_settle(conn, date_str: str) -> None:
    res = settle_parlays(conn, date_str)
    if not res["settled"] and not res["pending"]:
        print("  No saved parlays for that date.")
        return
    print(f"  Settled {res['settled']} slip(s): {res['won']} won, {res['lost']} lost, "
          f"{res['pending']} still pending.")
    if res["settled"]:
        print(f"  Staked Rs{res['staked']:.0f}, returned Rs{res['returned']:.0f} "
              f"-> P&L Rs{res['pnl']:+.0f}")
    if res["pending"]:
        print("  Pending slips are waiting on results. Load those meetings' results first:")
        print("    python -m scripts.daily --results " + date_str)

    lifetime = conn.execute(
        "SELECT COUNT(*) n, SUM(stake) staked, SUM(COALESCE(payout,0)) ret "
        "FROM parlays WHERE status IN ('won','lost')"
    ).fetchone()
    if lifetime and lifetime["n"]:
        staked, ret = lifetime["staked"] or 0, lifetime["ret"] or 0
        roi = ((ret / staked - 1) * 100) if staked else 0
        print(f"\n  Lifetime: {lifetime['n']} settled slips, staked Rs{staked:.0f}, "
              f"returned Rs{ret:.0f}, P&L Rs{ret - staked:+.0f} ({roi:+.1f}% ROI)")
        if lifetime["n"] < 30:
            print("  (Under 30 slips this number is noise, not a verdict.)")


def do_results(conn, date_str: str, venues: list[str]) -> None:
    """Pull results (and therefore starting prices, and the multi-leg pool
    dividends) for a date."""
    total = pools = 0
    for venue in venues:
        source = SCRAPER_BY_VENUE[venue]
        try:
            html = source.fetch_raceresult_html(date_str, use_cache=False)
            races = source.parse_raceresult(html)
        except Exception as exc:
            print(f"    {venue:<12} FAILED: {exc}")
            continue
        if not races:
            continue
        store_raceresult(conn, date_str, venue, races)
        n = sum(len(r.get("runners", [])) for r in races)
        total += n
        # The jackpot/treble settlements sit in their own tables at the foot of
        # the same page. Nothing else records which races made up each pool, or
        # how many tickets shared each dividend.
        try:
            found = source.parse_pool_dividends(html)
        except AttributeError:
            found = []  # BTC's result page carries no pool table
        except Exception as exc:
            print(f"    {venue:<12} pool dividends failed: {exc}")
            found = []
        if found:
            pools += store_pool_dividends(conn, date_str, venue, found)
        print(f"    {venue:<12} {n:>3} runners across {len(races)} races"
              + (f", {len(found)} pool settlements" if found else ""))
    print(f"  Stored {total} result rows (starting prices included) and {pools} pool dividends.")


def do_snapshot(conn, date_str: str) -> None:
    """Record today's forecast prices, and settle any SPs we now hold.

    This is the data-collection half of the one question the archive cannot
    currently answer -- see scripts/early_price.py and the odds_snapshots
    comment in db/schema.py. Run it on a race day (indiarace only publishes
    these on the day itself, not the night before), then again after the
    racing so the settled SPs land alongside the early quotes.

    Safe to run repeatedly: stages upsert, so a later run just refreshes.
    """
    from db.ingest import snapshot_odds, snapshot_settle_sp

    venues = [r["venue"] for r in conn.execute(
        "SELECT DISTINCT venue FROM races WHERE race_date=? AND circuit='India' ORDER BY venue",
        (date_str,)).fetchall()]
    if not venues:
        print(f"No Indian meeting loaded for {date_str}. Load a card first "
              f"(app sidebar, or scripts/backfill.py).")
        return

    total_stages = 0
    for venue in venues:
        races = conn.execute(
            "SELECT id, race_number FROM races WHERE race_date=? AND venue=? ORDER BY race_number",
            (date_str, venue)).fetchall()
        field_by_race = {}
        for r in races:
            names = [row["name"] for row in conn.execute(
                """SELECT h.name FROM runs r JOIN horses h ON h.id = r.horse_id
                   WHERE r.race_id = ? AND r.scratched = 0""", (r["id"],))]
            if names:
                field_by_race[r["race_number"]] = names
        try:
            raw = indiarace.fetch_odds_html(venue, date_str, use_cache=False)
            odds_map = indiarace.parse_odds(raw) if raw else {}
        except Exception as exc:
            print(f"  {venue}: odds fetch failed -- {exc}")
            continue
        if not odds_map:
            print(f"  {venue}: no prices posted yet "
                  f"(indiarace fills this on race-day morning)")
            continue
        res = snapshot_odds(conn, date_str, venue, odds_map, field_by_race)
        total_stages += res["stages_written"]
        print(f"  {venue}: {res['stages_written']} stage quotes across {res['races']} races "
              f"({len(odds_map)} runners priced on the page)")

    settled = snapshot_settle_sp(conn, date_str)
    print(f"\nStored {total_stages} early-price quotes; {settled} settled SPs recorded.")
    if settled == 0:
        print("No SPs yet -- rerun this after the results are in "
              "(`python -m scripts.daily --results <date>` first).")
    print("Report: python scripts/early_price.py")


def do_backfill(conn, days: int, venues: list[str]) -> None:
    """Archive recent results so the connections signal, the backtest and the
    pool-dividend record have something to work with."""
    today = date.today()
    for i in range(1, days + 1):
        d = (today - timedelta(days=i)).strftime("%Y-%m-%d")
        print(f"  {d}")
        do_results(conn, d, venues)


# --------------------------------------------------------------------------

def main() -> None:
    ap = argparse.ArgumentParser(description="Daily Indian racing routine")
    ap.add_argument("--date", default=date.today().strftime("%Y-%m-%d"))
    ap.add_argument("--vertical", choices=list(verticals.VERTICALS),
                    help="Load every venue of one regional turf authority")
    ap.add_argument("--venues", nargs="*", choices=verticals.venue_names(),
                    help="Specific venues (default: all of them)")
    ap.add_argument("--bankroll", type=float, default=10000.0)
    ap.add_argument("--target", type=float, default=750.0, help="Daily profit target in Rs")
    ap.add_argument("--jackpot-budget", type=float, default=1200.0,
                    help="Rs to spend on the jackpot ticket (0 to skip the plan)")
    ap.add_argument("--jackpot-unit", type=float, default=5.0,
                    help="Cost of one jackpot combination")
    ap.add_argument("--pool", choices=list(jackpot_engine.POOLS),
                    help="Which kind of multi-leg pool to plan (default: the biggest the "
                         "card carries)")
    ap.add_argument("--settle", metavar="DATE", help="Grade saved parlays for a date and exit")
    ap.add_argument("--results", metavar="DATE",
                    help="Fetch results, SPs and pool dividends for a date and exit")
    ap.add_argument("--backfill", type=int, metavar="DAYS",
                    help="Archive N days of past results and exit")
    ap.add_argument("--snapshot", nargs="?", const="", metavar="DATE",
                    help="Record forecast prices (and settle SPs) for a date, then exit. "
                         "Defaults to --date. Run on a race day; indiarace only posts these on the day.")
    ap.add_argument("--no-load", action="store_true", help="Use what's already in the DB")
    ap.add_argument("--no-odds", action="store_true", help="Skip the indiarace forecast fetch")
    args = ap.parse_args()

    init_db()
    conn = get_connection()
    venues = resolve_venues(args)

    if args.settle:
        _h(f"SETTLING {args.settle}")
        do_settle(conn, args.settle)
        return
    if args.results:
        _h(f"RESULTS {args.results}")
        do_results(conn, args.results, venues)
        return
    if args.backfill:
        _h(f"BACKFILLING {args.backfill} DAYS")
        do_backfill(conn, args.backfill, venues)
        return
    if args.snapshot is not None:
        snap_date = args.snapshot or args.date
        _h(f"ODDS SNAPSHOT {snap_date}")
        do_snapshot(conn, snap_date)
        return

    scope = args.vertical or ("selected venues" if args.venues else "all verticals")
    _h(f"INDIA -- {args.date} ({scope})")

    yesterday = (date.fromisoformat(args.date) - timedelta(days=1)).strftime("%Y-%m-%d")
    _h(f"1. Settling yesterday ({yesterday})")
    do_settle(conn, yesterday)

    _h("2. Loading today's fields")
    if args.no_load:
        print("  --no-load: using what is already stored.")
        loaded = [r["venue"] for r in conn.execute(
            "SELECT DISTINCT venue FROM races WHERE race_date=? AND circuit='India'",
            (args.date,))]
    else:
        loaded = load_cards(conn, args.date, venues)

    if loaded and not args.no_odds:
        _h("2b. Fetching forecast prices (indiarace)")
        load_forecast_odds(conn, args.date, loaded)

    slate = build_slate(conn, args.date, loaded or venues)
    if not slate:
        print("\n  Nothing loaded for this date -- no club is racing, or the cards are not up yet.")
        return

    _h("3. The card")
    priced = report_slate(slate)
    report_top_picks(slate)

    _h("4. Today's suggestions")
    if priced:
        report_parlays(priced, args.bankroll, args.target)
    else:
        print("  Skipped -- no prices. See above.")

    if args.jackpot_budget > 0:
        _h("5. Jackpot / treble plan")
        report_jackpot(conn, slate, args.date, args.jackpot_budget,
                       args.jackpot_unit, args.pool)

    print(f"\n{RULE}")
    print("Reminder: this is analysis, not advice, and nothing here places a bet.")
    print("Settle tonight with:  python -m scripts.daily --settle " + args.date)
    print(RULE)


if __name__ == "__main__":
    main()
