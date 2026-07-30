"""Live Australian odds via the TAB New Zealand affiliate API.

This is the answer to the one thing Racing Australia cannot give us: a price
you can actually bet into, before the race, fetched automatically.

Why New Zealand for Australian racing. Everything Australian is walled off
from an Indian IP -- tab.com.au's API serves a region-unavailable page,
punters.com.au and racenet.com.au return 403 from CloudFront, Sportsbet
returns an Akamai "Access Denied", PointsBet throws a Cloudflare challenge,
Betfair 403s at the edge, and Entain's Neds/Ladbrokes gateway 500s. NZ TAB is
not geo-fenced the same way, and it books all the major Australian meetings,
so it answers from here. Verified from this machine, July 2026.

    https://api.tab.co.nz/affiliates/v1/racing/meetings?date_from=&date_to=
    https://api.tab.co.nz/affiliates/v1/racing/events/{race_id}

What each runner carries:

    "odds": {"fixed_win": 11, "fixed_place": 2.6, "pool_win": 12.9, "pool_place": 1.8}

Fixed odds are a bookmaker's price -- what you would be paid if you took it
now. Pool odds are the tote's current approximate dividend, which moves until
the jump. The parlay engine wants fixed odds, because those are the only ones
you can actually lock in; pool prices are stored alongside for comparison.

Two things that cost time to work out, recorded so nobody re-derives them:

- The parameter is `date_from`/`date_to`, NOT `date`. A `date` parameter is
  accepted, silently ignored, and the API returns whatever is running now.
  That is a nasty failure mode -- you get a valid 200 with real odds for the
  wrong day. The endpoint helpfully echoes its parsed parameters back under
  `params`, which is how this was caught, and fetch_meetings() asserts the
  echoed dates match what was asked for.
- `category`/`type` is "T" for thoroughbreds, "H" harness, "G" greyhounds. The
  unfiltered feed is mostly greyhound and harness meetings.
- A SETTLED race keeps its last fixed odds without re-pricing for scratchings,
  so dropping scratched runners leaves a book that sums to less than 1. A real
  Eagle Farm race showed 1.203 across all ten runners and 0.906 across the
  seven that started, because three were scratched at 4.20, 17.00 and one
  unpriced. Nothing is wrong with the feed -- but a sub-1.0 book is
  meaningless to de-vig, which is why odds_for_date() skips finished races by
  default. Only live races carry a book worth pricing against.

IMPORTANT -- whose price is it. These are NZ TAB's prices. If you place your
bets somewhere else, the edge that matters is measured against THAT book's
price, not this one. Use this feed to find races worth looking at and as a
fair-price benchmark; confirm the number at the book you actually bet with
before staking. Prices from here are stored under the source name 'tabnz',
which ranks below 'manual' precisely so a price you typed in yourself always
wins.
"""
import time
from pathlib import Path

import requests

USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")
BASE = "https://api.tab.co.nz/affiliates/v1/racing"
MEETINGS_URL = BASE + "/meetings"
EVENT_URL = BASE + "/events/{race_id}"

CACHE_DIR = Path(__file__).resolve().parent.parent / "data" / "cache"

CATEGORY_THOROUGHBRED = "T"
SOURCE_NAME = "tabnz"

# Track names TAB NZ spells differently from Racing Australia. Only genuine
# mismatches belong here -- most names agree exactly.
VENUE_ALIASES = {
    "ROSEHILL": "ROSEHILL GARDENS",
    "ROYAL RANDWICK": "RANDWICK",
    "THE VALLEY": "MOONEE VALLEY",
    "SUNSHINE COAST": "SUNSHINE COAST POLY",
    "GOLD COAST": "AQUIS PARK GOLD COAST",
}


def _get_json(url: str, params: dict | None = None, timeout: int = 25) -> dict:
    resp = requests.get(url, params=params,
                        headers={"User-Agent": USER_AGENT, "Accept": "application/json"},
                        timeout=timeout)
    resp.raise_for_status()
    return resp.json()


def fetch_meetings(date_str: str, country: str = "AUS",
                   category: str = CATEGORY_THOROUGHBRED) -> list[dict]:
    """Meetings for one date. Filters to Australian thoroughbred racing by default.

    Guards against the silently-ignored-date trap described in the module
    docstring: the API echoes the dates it actually used, and if they do not
    match what was asked for we raise rather than hand back today's odds
    labelled as another day's."""
    payload = _get_json(MEETINGS_URL, {"date_from": date_str, "date_to": date_str})
    echoed = (payload.get("params") or {})
    for key in ("date_from", "date_to"):
        got = str(echoed.get(key, ""))[:10]
        if got and got != date_str:
            raise RuntimeError(
                f"TAB NZ ignored the requested date: asked for {date_str}, it used {got}. "
                f"Refusing to return odds for the wrong day."
            )

    meetings = ((payload.get("data") or {}).get("meetings")) or []
    out = []
    for m in meetings:
        if country and m.get("country") != country:
            continue
        if category and m.get("category") != category:
            continue
        out.append({
            "meeting_id": m.get("meeting"),
            "venue": (m.get("name") or "").strip(),
            "state": m.get("state"),
            "country": m.get("country"),
            "track_condition": m.get("track_condition"),
            "races": [{
                "race_id": r.get("id"),
                "race_no": r.get("race_number"),
                "name": r.get("name"),
                "status": r.get("status"),
                "start_time": r.get("start_time"),
                "distance_m": r.get("distance"),
            } for r in (m.get("races") or [])],
        })
    return out


def fetch_event(race_id: str) -> dict:
    """One race: runners with odds, plus results and dividends once it has run."""
    return (_get_json(EVENT_URL.format(race_id=race_id)).get("data")) or {}


def _num(v):
    try:
        f = float(v)
        return f if f > 1.0 else None
    except (TypeError, ValueError):
        return None


def runner_odds(event: dict) -> list[dict]:
    """Event payload -> one row per runner with both markets.

    Scratched runners are skipped: a price on a scratched horse is stale, and
    letting it through would inflate the field's apparent price coverage,
    which is exactly the condition the parlay engine's coverage guard exists
    to catch."""
    out = []
    for r in (event.get("runners") or []):
        if r.get("is_scratched"):
            continue
        if r.get("primary_market") is False:
            continue  # side markets (e.g. "Top 4") are not the win/place book
        odds = r.get("odds") or {}
        name = (r.get("name") or "").strip().upper()
        if not name:
            continue
        out.append({
            "horse_name": name,
            "runner_number": r.get("runner_number"),
            "barrier": r.get("barrier"),
            "jockey": r.get("jockey"),
            "trainer": r.get("trainer_name"),
            "weight": r.get("weight"),
            "win": _num(odds.get("fixed_win")),
            "place": _num(odds.get("fixed_place")),
            "pool_win": _num(odds.get("pool_win")),
            "pool_place": _num(odds.get("pool_place")),
        })
    return out


def normalise_venue(name: str) -> str:
    n = (name or "").strip().upper()
    return VENUE_ALIASES.get(n, n)


def venues_match(tab_venue: str, local_venue: str) -> bool:
    """TAB NZ and Racing Australia mostly agree on track names, but not always.
    Matches on the alias table first, then on one name containing the other
    ('Rosehill' vs 'Rosehill Gardens')."""
    a, b = normalise_venue(tab_venue), normalise_venue(local_venue)
    return a == b or a in b or b in a


def odds_for_date(date_str: str, venues: list[str] | None = None,
                  include_finished: bool = False, pause: float = 0.3) -> dict:
    """Every priced Australian thoroughbred race on a date.

    Returns {venue: [{race_no, odds: [...], status, ...}]}. `venues` filters to
    the tracks you have actually loaded fields for, so a full slate does not
    cost 60 requests to price 2 meetings."""
    result: dict[str, list[dict]] = {}
    for meeting in fetch_meetings(date_str):
        if venues and not any(venues_match(meeting["venue"], v) for v in venues):
            continue
        races = []
        for race in meeting["races"]:
            if not include_finished and race["status"] in ("Final", "Abandoned"):
                continue
            if not race["race_id"]:
                continue
            try:
                event = fetch_event(race["race_id"])
            except requests.RequestException:
                continue
            odds = runner_odds(event)
            if odds:
                races.append({**race, "odds": odds})
            time.sleep(pause)  # be a polite guest
        if races:
            result[meeting["venue"]] = races
    return result


def to_market_rows(races: list[dict], market_source: str = "fixed") -> list[dict]:
    """Race list -> the flat shape db.ingest.store_market_odds() consumes.

    market_source 'fixed' uses the bookmaker price (bettable, the default);
    'pool' uses the tote's projected dividend, which is worth storing for
    comparison but is not a price you can lock in."""
    win_key, place_key = (("win", "place") if market_source == "fixed"
                          else ("pool_win", "pool_place"))
    rows = []
    for race in races:
        for r in race["odds"]:
            if not r.get(win_key) and not r.get(place_key):
                continue
            rows.append({
                "race_no": race["race_no"],
                "horse_name": r["horse_name"],
                "win": r.get(win_key),
                "place": r.get(place_key),
            })
    return rows
