"""The daily routine, as one command.

    python -m scripts.daily                     # today's Australian slate
    python -m scripts.daily --date 2026-08-01 --states NSW VIC
    python -m scripts.daily --circuit "Hong Kong"
    python -m scripts.daily --settle 2026-07-30 # grade yesterday's slips
    python -m scripts.daily --backfill 14       # archive the last 14 days of results

Run it in the morning and it will: settle yesterday, load today's fields, tell
you which races have prices, and print any multi that survives the
expected-value test. Run it in the evening with --settle to find out whether
the day actually made money.

Two things it deliberately will NOT do:

- It will not place a bet. Nothing in this project talks to a bookmaker.
- It will not invent a suggestion to fill the page. Most days it will print
  "nothing qualifies", because most days nothing does. A tool that always has
  a tip is a tool that is telling you what you want to hear.

On the Australian circuit it also fetches live fixed odds from NZ TAB, which
is the one Australian-racing price feed that answers from India (tab.com.au,
punters, racenet, Sportsbet, PointsBet and Betfair all block or challenge the
request). Those are NZ TAB's prices though, not your bookmaker's -- treat them
as a way to find races worth a look and as a fair-price benchmark, and confirm
the number where you actually bet before staking. Pass --no-odds to skip the
fetch and price things by hand in the app's Odds tab instead.
"""
import argparse
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from db.ingest import (  # noqa: E402
    load_market_odds, odds_age_minutes, settle_parlays, store_intl_racecard,
    store_intl_results,
)

# Past this, the market has probably moved and an edge measured against the
# stored price is fiction rather than opportunity.
STALE_ODDS_MINUTES = 90
from db.schema import get_connection, init_db  # noqa: E402
from models import parlay as parlay_engine  # noqa: E402
from models.rating_engine import compute_composite_scores  # noqa: E402
from db.ingest import store_market_odds  # noqa: E402
from scrapers import hkjc, tabnz, racingaustralia as ra  # noqa: E402

RULE = "=" * 78


def _h(title: str) -> None:
    print(f"\n{RULE}\n{title}\n{RULE}")


# --------------------------------------------------------------------------

def load_australia(conn, date_str: str, states: list[str], max_meetings: int) -> list[str]:
    meetings = ra.meetings_for_date(date_str, states, use_cache=False)
    if not meetings:
        print(f"  No Australian meetings found for {date_str} in {', '.join(states)}.")
        return []
    print(f"  {len(meetings)} meeting(s) found; loading up to {max_meetings}.")
    loaded = []
    for m in meetings[:max_meetings]:
        try:
            races = ra.parse_racecard(ra.fetch_form_html(m["key"], use_cache=True))
            n = store_intl_racecard(conn, date_str, m["venue"], "Australia", races,
                                    country="AU", source_key=m["key"])
            print(f"    {m['state']:4s} {m['venue']:<26} {len(races):>2} races, {n:>3} runners")
            loaded.append(m["venue"])
        except Exception as exc:
            print(f"    {m['state']:4s} {m['venue']:<26} FAILED: {exc}")
    return loaded


def load_hong_kong(conn, date_str: str) -> list[str]:
    season = hkjc.season_status()
    if not season["in_season"]:
        print(f"  {season['message']}")
        return []
    loaded = []
    for code, name in hkjc.RACECOURSES.items():
        try:
            races = hkjc.fetch_meeting_card(date_str, code, use_cache=True)
        except Exception as exc:
            print(f"    {name}: fetch failed: {exc}")
            continue
        if not races:
            continue
        n = store_intl_racecard(conn, date_str, name, "Hong Kong", races, country="HK")
        print(f"    {name:<16} {len(races)} races, {n} runners")
        loaded.append(name)
    if not loaded:
        print("  No Hong Kong card published for that date.")
    return loaded


def load_odds_australia(conn, date_str: str, venues: list[str]) -> int:
    """Pull live fixed odds from NZ TAB for the meetings we've loaded.

    Only live races are fetched -- a settled race keeps its last fixed odds
    without re-pricing for scratchings, which leaves a book summing to under 1
    and nothing sensible to de-vig."""
    if not venues:
        return 0
    try:
        by_venue = tabnz.odds_for_date(date_str, venues=venues)
    except Exception as exc:
        print(f"  NZ TAB fetch failed: {exc}")
        return 0
    if not by_venue:
        print("  No live races to price -- they have all run, or betting isn't open yet.")
        return 0
    total = 0
    for tab_venue, races in by_venue.items():
        local = next((v for v in venues if tabnz.venues_match(tab_venue, v)), None)
        if not local:
            continue
        res = store_market_odds(conn, date_str, local, tabnz.to_market_rows(races), source="tabnz")
        total += res["matched"]
        print(f"    {local:<26} {res['matched']:>3} runners priced across {len(races)} live race(s)")
        if res["unmatched"]:
            print(f"      ({len(res['unmatched'])} name(s) unmatched -- late scratchings or spelling)")
    print(f"  Priced {total} runners. These are NZ TAB's prices, not your bookmaker's --")
    print("  confirm the number where you actually bet before staking.")
    return total


def build_slate(conn, date_str: str, circuit: str) -> list[dict]:
    rows = conn.execute(
        """SELECT id, venue, race_number, race_name, race_time_local
           FROM races WHERE race_date=? AND circuit=? ORDER BY venue, race_number""",
        (date_str, circuit),
    ).fetchall()
    slate = []
    for row in rows:
        entries = compute_composite_scores(conn, row["id"])
        if not entries:
            continue
        slate.append({
            "race_id": row["id"], "venue": row["venue"], "race_no": row["race_number"],
            "race_name": row["race_name"], "race_time": row["race_time_local"],
            "circuit": circuit, "field_size": len(entries), "entries": entries,
            "odds": load_market_odds(conn, row["id"]),
            "odds_age_min": odds_age_minutes(conn, row["id"]),
        })
    return slate


def report_slate(slate: list[dict]) -> list[dict]:
    priced = [r for r in slate if r["odds"]]
    print(f"  {len(slate)} races loaded, {len(priced)} with prices attached.")
    if not priced:
        print("\n  Nothing can be evaluated without prices. Open the app's Odds tab, paste the")
        print("  win/place prices for the races you care about, then run this again.")
    unpriced = [r for r in slate if not r["odds"]]
    if unpriced and priced:
        print(f"  ({len(unpriced)} race(s) still unpriced and therefore invisible to the engine.)")
    return priced


def report_top_picks(slate: list[dict], limit: int = 8) -> None:
    """Model opinion only -- no prices needed. Useful for deciding which races
    are worth going and getting a price for."""
    ranked = []
    for race in slate:
        top = race["entries"][0]
        ranked.append((top["win_probability"], race, top))
    ranked.sort(key=lambda t: t[0], reverse=True)
    print("  Strongest model opinions on the card (go price these up first):")
    for prob, race, top in ranked[:limit]:
        marker = "$" if race["odds"] else " "
        print(f"   {marker} {race['venue'][:18]:<18} R{race['race_no']:<2} "
              f"{top['horse_name'][:22]:<22} {prob * 100:5.1f}%  "
              f"(field {race['field_size']})")
    print("   '$' means prices are already stored for that race.")


def report_parlays(priced: list[dict], bankroll: float, target: float, circuit: str) -> None:
    stale = [r for r in priced if (r.get("odds_age_min") or 0) > STALE_ODDS_MINUTES]
    if stale:
        oldest = max(r["odds_age_min"] for r in stale)
        print(f"  !! STALE PRICES: {len(stale)} of {len(priced)} races are priced off odds up to")
        print(f"     {oldest / 60:.1f} hours old. Markets move -- any edge below may no longer")
        print(f"     exist. Re-run without --no-odds before staking anything.\n")

    card = parlay_engine.daily_parlay_card(priced, bankroll=bankroll, circuit=circuit)
    print(f"\n  {card['verdict']}\n")

    if card["singles"]:
        print("  SINGLES (same edge as a multi leg, far less variance):")
        for s in card["singles"]:
            print(f"    {s['venue'][:16]:<16} R{s['race_no']:<2} {s['horse_name'][:20]:<20} "
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


def do_results(conn, date_str: str, states: list[str], circuit: str) -> None:
    """Pull results (and therefore starting prices) for a date."""
    total = 0
    if circuit == "Australia":
        for m in ra.meetings_for_date(date_str, states, results_calendar=True, use_cache=False):
            try:
                races = ra.parse_raceresult(ra.fetch_results_html(m["key"], use_cache=False))
                n = store_intl_results(conn, date_str, m["venue"], "Australia", races, country="AU")
                if n:
                    print(f"    {m['venue']:<26} {n} runners")
                total += n
            except Exception as exc:
                print(f"    {m['venue']:<26} FAILED: {exc}")
    else:
        for code, name in hkjc.RACECOURSES.items():
            try:
                races = hkjc.fetch_meeting_results(date_str, code, use_cache=False)
            except Exception as exc:
                print(f"    {name}: {exc}")
                continue
            if races:
                n = store_intl_results(conn, date_str, name, "Hong Kong", races, country="HK")
                print(f"    {name:<26} {n} runners across {len(races)} races")
                total += n
    print(f"  Stored {total} result rows (starting prices included).")


def do_snapshot(conn, date_str: str) -> None:
    """Record today's Indian forecast prices, and settle any SPs we now hold.

    This is the data-collection half of the one question the archive cannot
    currently answer -- see scripts/early_price.py and the odds_snapshots
    comment in db/schema.py. Run it on a race day (indiarace only publishes
    these on the day itself, not the night before), then again after the
    racing so the settled SPs land alongside the early quotes.

    Safe to run repeatedly: stages upsert, so a later run just refreshes.
    """
    from db.ingest import snapshot_odds, snapshot_settle_sp
    from scrapers import indiarace as ir

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
            raw = ir.fetch_odds_html(venue, date_str, use_cache=False)
            odds_map = ir.parse_odds(raw) if raw else {}
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


def do_backfill(conn, days: int, states: list[str], circuit: str) -> None:
    """Archive recent results so the connections signal and the backtest have
    something to work with. The model's jockey/trainer/owner strike rates on
    the AU and HK circuits come entirely from this archive -- there is no
    published leaderboard to scrape -- so a fresh install needs a few weeks of
    this before those signals mean anything."""
    today = date.today()
    for i in range(1, days + 1):
        d = (today - timedelta(days=i)).strftime("%Y-%m-%d")
        print(f"  {d}")
        do_results(conn, d, states, circuit)


# --------------------------------------------------------------------------

def main() -> None:
    ap = argparse.ArgumentParser(description="Daily racing routine")
    ap.add_argument("--date", default=date.today().strftime("%Y-%m-%d"))
    ap.add_argument("--circuit", default="Australia", choices=["Australia", "Hong Kong"])
    ap.add_argument("--states", nargs="*", default=["NSW", "VIC"],
                    help="Australian states to scan (default NSW VIC)")
    ap.add_argument("--max-meetings", type=int, default=4)
    ap.add_argument("--bankroll", type=float, default=10000.0)
    ap.add_argument("--target", type=float, default=750.0, help="Daily profit target in Rs")
    ap.add_argument("--settle", metavar="DATE", help="Grade saved parlays for a date and exit")
    ap.add_argument("--results", metavar="DATE", help="Fetch results/SPs for a date and exit")
    ap.add_argument("--backfill", type=int, metavar="DAYS", help="Archive N days of past results and exit")
    ap.add_argument("--snapshot", nargs="?", const="", metavar="DATE",
                    help="Record Indian forecast prices (and settle SPs) for a date, then exit. "
                         "Defaults to --date. Run on a race day; indiarace only posts these on the day.")
    ap.add_argument("--no-load", action="store_true", help="Use what's already in the DB")
    ap.add_argument("--no-odds", action="store_true",
                    help="Skip the NZ TAB odds fetch (Australia only)")
    args = ap.parse_args()

    init_db()
    conn = get_connection()

    if args.settle:
        _h(f"SETTLING {args.settle}")
        do_settle(conn, args.settle)
        return
    if args.results:
        _h(f"RESULTS {args.results} ({args.circuit})")
        do_results(conn, args.results, args.states, args.circuit)
        return
    if args.backfill:
        _h(f"BACKFILLING {args.backfill} DAYS ({args.circuit})")
        do_backfill(conn, args.backfill, args.states, args.circuit)
        return
    if args.snapshot is not None:
        snap_date = args.snapshot or args.date
        _h(f"ODDS SNAPSHOT {snap_date} (India)")
        do_snapshot(conn, snap_date)
        return

    _h(f"{args.circuit.upper()} -- {args.date}")

    yesterday = (date.fromisoformat(args.date) - timedelta(days=1)).strftime("%Y-%m-%d")
    _h(f"1. Settling yesterday ({yesterday})")
    do_settle(conn, yesterday)

    _h("2. Loading today's fields")
    loaded_venues: list[str] = []
    if args.no_load:
        print("  --no-load: using what is already stored.")
        loaded_venues = [r["venue"] for r in conn.execute(
            "SELECT DISTINCT venue FROM races WHERE race_date=? AND circuit=?",
            (args.date, args.circuit))]
    elif args.circuit == "Australia":
        loaded_venues = load_australia(conn, args.date, args.states, args.max_meetings)
    else:
        loaded_venues = load_hong_kong(conn, args.date)

    if args.circuit == "Australia" and loaded_venues and not args.no_odds:
        _h("2b. Fetching live odds (NZ TAB)")
        load_odds_australia(conn, args.date, loaded_venues)

    slate = build_slate(conn, args.date, args.circuit)
    if not slate:
        print("\n  Nothing loaded for this date -- no card published, or the meeting is over.")
        return

    _h("3. The card")
    priced = report_slate(slate)
    report_top_picks(slate)

    _h("4. Today's suggestions")
    if priced:
        report_parlays(priced, args.bankroll, args.target, args.circuit)
    else:
        print("  Skipped -- no prices. See above.")

    print(f"\n{RULE}")
    print("Reminder: this is analysis, not advice, and nothing here places a bet.")
    print("Settle tonight with:  python -m scripts.daily --settle " + args.date)
    print(RULE)


if __name__ == "__main__":
    main()
