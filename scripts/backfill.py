"""Bulk-download race cards + results for a date range into the local DB.

Usage:
    python scripts/backfill.py --venue Bangalore --start 2026-06-28 --end 2026-07-12
    python scripts/backfill.py --venue Pune --start 2025-07-18 --end 2025-10-20
    python scripts/backfill.py --venue Mumbai --start 2025-11-01 --end 2026-04-30
    python scripts/backfill.py --venue Hyderabad --start 2026-07-01 --end 2026-08-10

Iterates every date in [start, end], fetching both the race card and the
results for each date. Dates with no card/results (weekdays, off-season) are
skipped silently -- that's expected, not an error. Politely rate-limited
(scrapers already sleep ~1s between live requests) since these are small club
servers, not a CDN.
"""
import argparse
import sys
from datetime import date, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from db.schema import get_connection, init_db
from db.ingest import store_pool_dividends, store_racecard, store_raceresult
from scrapers import rwitc, btc, indiarace_cards
from models import verticals

# Hyderabad/Mysore/Kolkata/Delhi have no scrapable club site of their own --
# see indiarace_cards.py's module docstring -- so they go through indiarace's
# unified racing-center pages instead of a dedicated per-club scraper. The
# grouping into verticals lives in models/verticals.py; this table is just the
# fetch dispatch.
SCRAPER_BY_VENUE = {
    "Pune": rwitc.ForVenue("Pune"), "Mumbai": rwitc.ForVenue("Mumbai"), "Bangalore": btc,
    "Hyderabad": indiarace_cards.ForVenue("Hyderabad"),
    "Mysore": indiarace_cards.ForVenue("Mysore"),
    "Kolkata": indiarace_cards.ForVenue("Kolkata"),
    "Delhi": indiarace_cards.ForVenue("Delhi"),
}


def daterange(start: date, end: date):
    d = start
    while d <= end:
        yield d
        d += timedelta(days=1)


def backfill(venue: str, start: date, end: date, fetch_cards: bool = True, fetch_results: bool = True):
    if venue not in SCRAPER_BY_VENUE:
        raise ValueError(f"Unknown venue {venue!r}; choose from {list(SCRAPER_BY_VENUE)}")
    source = SCRAPER_BY_VENUE[venue]
    init_db()
    conn = get_connection()

    cards_loaded = results_loaded = days_empty = pools_loaded = 0
    for d in daterange(start, end):
        date_str = d.strftime("%Y-%m-%d")
        got_something = False

        if fetch_cards:
            try:
                html = source.fetch_racecard_html(date_str, use_cache=True)
                races = source.parse_racecard(html)
                if races:
                    store_racecard(conn, date_str, venue, races)
                    cards_loaded += 1
                    got_something = True
                    print(f"[{date_str}] card: {len(races)} races")
            except Exception as e:
                print(f"[{date_str}] card fetch failed: {e}")

        if fetch_results:
            try:
                html = source.fetch_raceresult_html(date_str, use_cache=True)
                races = source.parse_raceresult(html)
                if races:
                    store_raceresult(conn, date_str, venue, races)
                    results_loaded += 1
                    got_something = True
                    # The jackpot/treble settlements sit in a separate table on
                    # the same page and are the only record of which races made
                    # up each pool and how many tickets shared each dividend.
                    # BTC publishes no such table, hence the AttributeError arm.
                    try:
                        pools = source.parse_pool_dividends(html)
                    except AttributeError:
                        pools = []
                    n_pools = store_pool_dividends(conn, date_str, venue, pools) if pools else 0
                    pools_loaded += n_pools
                    print(f"[{date_str}] results: {len(races)} races"
                          + (f", {n_pools} pool settlements" if n_pools else ""))
            except Exception as e:
                print(f"[{date_str}] result fetch failed: {e}")

        if not got_something:
            days_empty += 1

    conn.close()
    print(f"\nDone. {venue} ({verticals.vertical_of(venue)}): {cards_loaded} race-card days, "
          f"{results_loaded} result days, {pools_loaded} pool settlements, "
          f"{days_empty} days with nothing found.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--venue", required=True, choices=verticals.venue_names())
    parser.add_argument("--start", required=True, help="YYYY-MM-DD")
    parser.add_argument("--end", required=True, help="YYYY-MM-DD")
    parser.add_argument("--cards-only", action="store_true")
    parser.add_argument("--results-only", action="store_true")
    args = parser.parse_args()

    start = date.fromisoformat(args.start)
    end = date.fromisoformat(args.end)
    backfill(
        args.venue, start, end,
        fetch_cards=not args.results_only,
        fetch_results=not args.cards_only,
    )
