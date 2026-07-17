"""Bulk-download race cards + results for a date range into the local DB.

Usage:
    python scripts/backfill.py --venue Bangalore --start 2026-06-28 --end 2026-07-12
    python scripts/backfill.py --venue Pune --start 2025-07-18 --end 2025-10-20
    python scripts/backfill.py --venue Mumbai --start 2025-11-01 --end 2026-04-30

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
from db.ingest import store_racecard, store_raceresult
from scrapers import rwitc, btc

SCRAPER_BY_VENUE = {"Pune": rwitc, "Mumbai": rwitc, "Bangalore": btc}


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

    cards_loaded = results_loaded = days_empty = 0
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
                    print(f"[{date_str}] results: {len(races)} races")
            except Exception as e:
                print(f"[{date_str}] result fetch failed: {e}")

        if not got_something:
            days_empty += 1

    conn.close()
    print(f"\nDone. {venue}: {cards_loaded} race-card days, {results_loaded} result days, "
          f"{days_empty} days with nothing found.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--venue", required=True, choices=list(SCRAPER_BY_VENUE))
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
