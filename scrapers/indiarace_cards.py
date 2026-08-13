"""Scraper/parser for indiarace.com's unified 'Racing Center' race card and
result pages -- the data source for the four Indian venues that don't have
(or don't expose) their own scrapable club website: Hyderabad, Mysore,
Kolkata and Delhi.

Why indiarace.com rather than each club's own site (checked live, Aug 2026):
  - Hyderabad Race Club (hydraces.com) publishes no plain HTML racecard route
    in its nav -- what looks like one requires the separate, login-gated
    "Racing Portal" (d1mowe151t0ail.cloudfront.net/login).
  - Mysore Race Club (mysoreraceclub.com) has clean-looking /Racecard and
    /Results routes, but both 404 without whatever params the site's own JS
    supplies -- not reverse-engineered here.
  - Royal Calcutta Turf Club (rctconline.com) has no racecard in its public
    site at all; live racing data sits behind a separate rctclive.in login.
  - Delhi Race Club (drcraces.com) publishes entries/handicap/acceptance/
    declaration/results as one PDF per race day (/upload/{type}/File*.pdf),
    not as an HTML table.
indiarace.com -- already used by scrapers/indiarace.py for trackwork and
pre-race odds -- turns out to carry a full racecard/result page for every
club under one URL shape, so this module targets that instead of four
separate fragile per-club integrations.

Verified live, Aug 2026:
    https://www.indiarace.com/Home/racingCenterEvent
        ?venueId={id}&event_date=YYYY-MM-DD&race_type=RACECARD|RESULT
Same endpoint family as indiarace.py's ODDS_URL (race_type=ODDS) and
TRACKWORK -- just a different race_type value. A date/venue with no meeting
returns the same near-empty ~53KB template (no `id="race-N"` divs) rather
than an error, so parse_racecard/parse_raceresult just return [] for it --
same "nothing posted yet" UX as the other India scrapers.

Venue ids, read from indiarace's own Home/allRaceFixturesData JSON feed
(2170 fixture rows back to 2018) and cross-checked by grouping each
venue_id's own event_name text:
    1=Kolkata, 2=Mumbai, 3=Bangalore, 4=Chennai, 7=Delhi, 8=Mysore,
    9=Ooty, 10=Pune, 11=Hyderabad
Only Kolkata/Delhi/Mysore/Hyderabad are wired up here -- Pune/Mumbai/
Bangalore keep using rwitc.py/btc.py, whose club-native pages carry more
detail (breeder, stud, foaled date, full per-run history) than indiarace's
version of the same card. Chennai and Ooty are left out: their fixture rows
stop in Oct 2025 and Jun 2024 respectively, suggesting indiarace itself
doesn't have current data for those two either.

Page structure per race, inside `<div id="race-N">`:
  .heading_div      -- race number, name, class/rating band, distance, post time
  table.race_card_tab       (racecard) -- one row per declared runner
  table.result-table-new1   (result)   -- one row per finisher, plus a
                                          'Tote Favourite:' line and a
                                          'Club Dividends' table below it
Column headers are read from each table's own <thead> rather than hard-coded
positions (same approach indiarace.py's parse_odds already uses), so a
reordered column breaks obviously (KeyError-free -- the field is just None)
rather than silently mis-mapping data.
"""
import re
import time
from pathlib import Path

import requests
from bs4 import BeautifulSoup

USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
EVENT_URL = (
    "https://www.indiarace.com/Home/racingCenterEvent"
    "?venueId={venue_id}&event_date={date}&race_type={race_type}"
)
JOCKEY_STATS_URL = "https://www.indiarace.com/Home/jockeyStatistics/{venue_id}"
TRAINER_STATS_URL = "https://www.indiarace.com/Home/trainerStatistics/{venue_id}"

CACHE_DIR = Path(__file__).resolve().parent.parent / "data" / "cache"

# Full known mapping (see module docstring); only the four with no dedicated
# scraper are actually wired up -- see app.py's SCRAPER_BY_VENUE.
VENUE_IDS = {
    "Kolkata": 1, "Mumbai": 2, "Bangalore": 3, "Chennai": 4, "Delhi": 7,
    "Mysore": 8, "Ooty": 9, "Pune": 10, "Hyderabad": 11,
}

CLASS_RE = re.compile(r"Class\s+(\d+)")
RATED_RANGE_RE = re.compile(r"Rated\s+(\d+)\s*-\s*(\d+)")
RATED_UPWARD_RE = re.compile(r"Rated\s+(\d+)\s+and\s+(?:above|upward)", re.I)
LAST5_RE = re.compile(r"Last\s*5\s*runs:\s*(.+)", re.I)
MONEY_RE = re.compile(r"₹\.?\s*")  # indiarace prints amounts as "₹.15000"


def _get(url: str) -> str:
    resp = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=30)
    resp.raise_for_status()
    return resp.text


def _fetch_cached(url: str, filename: str, use_cache: bool) -> str:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path = CACHE_DIR / filename
    if use_cache and path.exists():
        return path.read_text(encoding="utf-8", errors="ignore")
    html = _get(url)
    path.write_text(html, encoding="utf-8")
    time.sleep(1)  # be polite -- same courtesy as the other scrapers
    return html


def _venue_id(venue: str) -> int:
    vid = VENUE_IDS.get(venue)
    if vid is None:
        raise ValueError(f"No indiarace venue id known for {venue!r}; choose from {list(VENUE_IDS)}")
    return vid


def fetch_racecard_html(venue: str, date: str, use_cache: bool = True) -> str:
    url = EVENT_URL.format(venue_id=_venue_id(venue), date=date, race_type="RACECARD")
    return _fetch_cached(url, f"ir_card_racecard_{venue.lower()}_{date}.html", use_cache)


def fetch_raceresult_html(venue: str, date: str, use_cache: bool = True) -> str:
    url = EVENT_URL.format(venue_id=_venue_id(venue), date=date, race_type="RESULT")
    return _fetch_cached(url, f"ir_card_result_{venue.lower()}_{date}.html", use_cache)


def fetch_jockey_stats_html(venue: str, use_cache: bool = True) -> str:
    return _fetch_cached(
        JOCKEY_STATS_URL.format(venue_id=_venue_id(venue)),
        f"ir_card_jockeystats_{venue.lower()}.html", use_cache,
    )


def fetch_trainer_stats_html(venue: str, use_cache: bool = True) -> str:
    return _fetch_cached(
        TRAINER_STATS_URL.format(venue_id=_venue_id(venue)),
        f"ir_card_trainerstats_{venue.lower()}.html", use_cache,
    )


def _parse_official_stats_table(html: str, name_key: str) -> list[dict]:
    """jockeyStatistics/{id} and trainerStatistics/{id} share one table shape:
    (Name, First, Second, Third, Fourth, Total, Win%)."""
    soup = BeautifulSoup(html, "lxml")
    table = soup.find("table", class_="jockeyStatistics")  # same class on both pages
    if not table:
        return []
    rows = table.find_all("tr")
    if not rows:
        return []
    header = [c.get_text(strip=True).upper() for c in rows[0].find_all(["th", "td"])]
    out = []
    for r in rows[1:]:
        cells = [c.get_text(strip=True) for c in r.find_all(["th", "td"])]
        if len(cells) != len(header):
            continue
        d = dict(zip(header, cells))
        name = d.get(name_key.upper())
        if not name:
            continue
        out.append({
            "name": name,
            "wins": _to_int(d.get("FIRST")),
            "seconds": _to_int(d.get("SECOND")),
            "thirds": _to_int(d.get("THIRD")),
            "fourths": _to_int(d.get("FOURTH")),
            "total_rides": _to_int(d.get("TOTAL")),
            "win_pct": _to_float_stat(d.get("WIN%")),
        })
    return out


def parse_jockey_stats(html: str) -> list[dict]:
    return _parse_official_stats_table(html, "Jockey Name")


def parse_trainer_stats(html: str) -> list[dict]:
    return _parse_official_stats_table(html, "Trainer Name")


def _to_int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0


def _to_float_stat(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _to_float(v):
    if not v:
        return None
    try:
        return float(v.strip())
    except ValueError:
        return None


def _clean_money(text: str) -> str:
    return MONEY_RE.sub("", text or "").strip()


def _parse_class_info(text: str) -> dict:
    class_code = None
    m = CLASS_RE.search(text)
    if m:
        class_code = m.group(1)
    rmin = rmax = None
    m = RATED_RANGE_RE.search(text)
    if m:
        rmin, rmax = int(m.group(1)), int(m.group(2))
    else:
        m = RATED_UPWARD_RE.search(text)
        if m:
            rmin, rmax = int(m.group(1)), None
    return {"class_code": class_code, "rating_band_min": rmin, "rating_band_max": rmax, "class_raw": text}


def _race_divs(soup: BeautifulSoup):
    return soup.find_all("div", id=re.compile(r"^race-\d+$"))


def _race_header(div) -> dict:
    heading = div.find("div", class_="heading_div")
    race_no = None
    h1 = heading.find("h1") if heading else None
    if h1 and h1.get_text(strip=True).isdigit():
        race_no = h1.get_text(strip=True)
    race_name = heading.find("h2").get_text(strip=True) if heading and heading.find("h2") else ""
    class_raw = heading.find("h3").get_text(" ", strip=True) if heading and heading.find("h3") else ""
    distance_m = None
    race_time = None
    times = heading.find_all("h4") if heading else []
    if len(times) > 0:
        dm = re.search(r"(\d{3,4})", times[0].get_text(strip=True))
        if dm:
            distance_m = int(dm.group(1))
    if len(times) > 1:
        race_time = times[1].get_text(strip=True)
    return {
        "race_no": race_no, "race_name": race_name, "race_time": race_time,
        "distance_m": distance_m, **_parse_class_info(class_raw),
    }


def _rating_from_cell(td) -> int | None:
    """Rtg cells show a small superscript PREVIOUS rating followed by the
    current one, e.g. <sup><small>19</small></sup>26 -- 26 is today's rating,
    19 is what it was before it last changed. Only the current figure is kept
    (non-mutating: reads the sup's own text rather than deleting it)."""
    if td is None:
        return None
    sup = td.find("sup")
    sup_text = sup.get_text(strip=True) if sup else ""
    full_text = td.get_text(strip=True)
    current = full_text[len(sup_text):] if sup_text and full_text.startswith(sup_text) else full_text
    return int(current) if current.isdigit() else None


def _horse_cell(td) -> tuple:
    """Returns (horse_name, recent_runs, sire, dam) from a Horse/Pedigree cell."""
    if td is None:
        return None, [], None, None
    h5 = td.find("h5")
    name = None
    if h5:
        a = h5.find("a")
        name = (a.get_text(strip=True) if a else h5.get_text(strip=True)) or None
    recent_runs = []
    sire = dam = None
    h6 = td.find("h6")
    if h6:
        # Sire/dam are two <a> tags to their own detail pages, joined by a
        # literal "-" text node -- matched by href rather than position so a
        # missing/reordered link degrades to None instead of mis-assigning.
        sire_a = h6.find("a", href=re.compile(r"allSireDetails"))
        dam_a = h6.find("a", href=re.compile(r"allDamDetails"))
        sire = sire_a.get_text(strip=True) or None if sire_a else None
        dam = dam_a.get_text(strip=True) or None if dam_a else None

        m = LAST5_RE.search(h6.get_text(" ", strip=True))
        if m:
            # Assumed newest-first, matching RWITC/BTC and the recency weighting
            # in models/rating_engine.py -- NOT cross-checked against a dated run
            # the way README's Data sources section verifies the Racing Australia
            # "Last 10" order, so treat this one signal as lower-confidence.
            placings = [p.strip().rstrip(".") for p in m.group(1).split("-")]
            recent_runs = [{"placing": p} for p in placings if p]
    return name, recent_runs, sire, dam


# The "Eq" column uses a dash-joined format ("TS-EP") rather than RWITC's
# parenthesized one; "Sh" is a separate single-letter column that's always
# "A" in every sample seen so far, matching the universal, non-differentiating
# "(A)" code embedded in RWITC's own equipment line -- treated the same way
# (ignored as noise, not stored as a meaningful equipment code).
def _equipment_codes(text: str) -> list[str]:
    return [c.strip() for c in (text or "").split("-") if c.strip()]


def parse_racecard(html: str) -> list[dict]:
    soup = BeautifulSoup(html, "lxml")
    races = []
    for div in _race_divs(soup):
        header = _race_header(div)
        table = div.find("table", class_="race_card_tab")
        runs = []
        thead = table.find("thead") if table else None
        tbody = table.find("tbody") if table else None
        if thead and tbody:
            headers = [c.get_text(strip=True).upper() for c in thead.find("tr").find_all(["th", "td"])]
            idx = {h: i for i, h in enumerate(headers)}

            def cell(cells, col):
                i = idx.get(col)
                return cells[i] if i is not None and i < len(cells) else None

            for row in tbody.find_all("tr"):
                cells = row.find_all("td")
                if len(cells) != len(headers):
                    continue
                name, recent_runs, sire, dam = _horse_cell(cell(cells, "HORSE/PEDIGREE"))
                if not name:
                    continue
                wt_cell = cell(cells, "WT")
                al_cell = cell(cells, "AL")
                jockey_cell = cell(cells, "JOCKEY")
                trainer_cell = cell(cells, "TRAINER")
                owner_cell = cell(cells, "OWNER(S)")
                eq_cell = cell(cells, "EQ")
                eq_raw = eq_cell.get_text(strip=True) if eq_cell else ""
                runs.append({
                    "horse_name": name,
                    "weight_kg": _to_float(wt_cell.get_text(strip=True)) if wt_cell else None,
                    "jockey": jockey_cell.get_text(strip=True) if jockey_cell else None,
                    "apprentice_allowance": _to_float(al_cell.get_text(strip=True)) if al_cell else None,
                    "trainer": trainer_cell.get_text(strip=True) if trainer_cell else None,
                    "owner": owner_cell.get_text(strip=True) if owner_cell else None,
                    "stud": None,  # not broken out separately on this page (unlike RWITC)
                    "breeder": None,  # ditto
                    "sire": sire,
                    "dam": dam,
                    "equipment_raw": eq_raw or None,
                    "equipment_codes": _equipment_codes(eq_raw),
                    "official_rating": _rating_from_cell(cell(cells, "RTG")),
                    "assessed_rating": None,  # no HRA-style reassessment shown on this page
                    "assessed_rating_date": None,
                    "foaled": None,  # only age is shown ("5y b m"), not a foaling date
                    "recent_runs": recent_runs,
                })
        races.append({**header, "runs": runs})
    return races


def _parse_dividends(div) -> dict:
    d = {"favourite": None, "win": None, "place": None, "shp": None, "for": None, "qnl": None, "tnl": None,
         "results_order": None, "margins": None}

    # "<b>Tote Favourite:</b> NAME<br>..." -- NAME is the text node right
    # after the <b>, so read it by DOM position rather than regexing the
    # surrounding blob (which also contains the owner names and a Race Video
    # link, and a horse name containing a common letter would break a naive
    # "capture until next label" pattern).
    fav_label = div.find("b", string=re.compile(r"Tote Favourite"))
    if fav_label and fav_label.next_sibling:
        fav = str(fav_label.next_sibling).strip()
        d["favourite"] = fav or None

    heading = div.find(lambda tag: tag.name == "h4" and "Club Dividends" in tag.get_text())
    if heading:
        table = heading.find_next("table")
        if table:
            rows = table.find_all("tr")
            if len(rows) >= 2:
                headers = [c.get_text(strip=True).upper() for c in rows[0].find_all(["th", "td"])]
                values = [_clean_money(c.get_text(strip=True)) for c in rows[1].find_all(["th", "td"])]
                kv = dict(zip(headers, values))
                d["win"] = kv.get("WIN") or None
                d["shp"] = kv.get("SHP") or None
                d["place"] = kv.get("PLACE") or None
                d["qnl"] = kv.get("QUINELLA") or None
                d["for"] = kv.get("FORECAST") or None
                d["tnl"] = kv.get("TRINALLA") or None
    return d


def parse_raceresult(html: str) -> list[dict]:
    soup = BeautifulSoup(html, "lxml")
    races = []
    for div in _race_divs(soup):
        header = _race_header(div)
        table = div.find("table", class_="result-table-new1")
        runners = []
        thead = table.find("thead") if table else None
        tbody = table.find("tbody") if table else None
        if thead and tbody:
            headers = [c.get_text(strip=True).upper() for c in thead.find("tr").find_all(["th", "td"])]
            idx = {h: i for i, h in enumerate(headers)}

            def cell(cells, col):
                i = idx.get(col)
                return cells[i] if i is not None and i < len(cells) else None

            for row in tbody.find_all("tr"):
                cells = row.find_all("td")
                if len(cells) != len(headers):
                    continue
                name, _, _, _ = _horse_cell(cell(cells, "HORSE/PEDIGREE"))
                if not name:
                    continue
                pl_cell = cell(cells, "PL")
                pl_txt = pl_cell.get_text(strip=True) if pl_cell else ""
                placing = int(pl_txt) if pl_txt.isdigit() else None
                wt_cell = cell(cells, "WT")
                jockey_cell = cell(cells, "JOCKEY")
                trainer_cell = cell(cells, "TRAINER")
                time_cell = cell(cells, "TIME")
                odds_cell = cell(cells, "CL ODDS")
                runners.append({
                    "placing": placing,
                    "horse_name": name,
                    "weight_kg": _to_float(wt_cell.get_text(strip=True)) if wt_cell else None,
                    "jockey": jockey_cell.get_text(strip=True) if jockey_cell else None,
                    "trainer": trainer_cell.get_text(strip=True) if trainer_cell else None,
                    "odds": odds_cell.get_text(strip=True) if odds_cell else None,
                    "time": time_cell.get_text(strip=True) if time_cell else None,
                    "scratched": placing is None,
                })
        races.append({**header, "runners": runners, "dividends": _parse_dividends(div)})
    return races


class ForVenue:
    """Binds one venue name to the module-level functions above so this
    module can drop into a {venue: scraper} dispatch table (app.py's
    SCRAPER_BY_VENUE, scripts/backfill.py's) the same way rwitc.py/btc.py do:
    fetch_*(date, use_cache) rather than fetch_*(venue, date, use_cache)."""

    def __init__(self, venue: str):
        _venue_id(venue)  # fail fast on an unknown venue name
        self.venue = venue

    def fetch_racecard_html(self, date: str, use_cache: bool = True) -> str:
        return fetch_racecard_html(self.venue, date, use_cache)

    def fetch_raceresult_html(self, date: str, use_cache: bool = True) -> str:
        return fetch_raceresult_html(self.venue, date, use_cache)

    def parse_racecard(self, html: str) -> list[dict]:
        return parse_racecard(html)

    def parse_raceresult(self, html: str) -> list[dict]:
        return parse_raceresult(html)

    def fetch_jockey_stats_html(self, use_cache: bool = True) -> str:
        return fetch_jockey_stats_html(self.venue, use_cache)

    def fetch_trainer_stats_html(self, use_cache: bool = True) -> str:
        return fetch_trainer_stats_html(self.venue, use_cache)

    def parse_jockey_stats(self, html: str) -> list[dict]:
        return parse_jockey_stats(html)

    def parse_trainer_stats(self, html: str) -> list[dict]:
        return parse_trainer_stats(html)
