"""Scraper/parser for the Hong Kong Jockey Club's racing information pages.

Data source (verified live from India, Jul 2026): https://racing.hkjc.com
- Results:  /racing/information/English/Racing/LocalResults.aspx
              ?RaceDate=YYYY/MM/DD&Racecourse=HV&RaceNo=N
- Racecard: /racing/information/English/Racing/RaceCard.aspx
              ?RaceDate=YYYY/MM/DD&Racecourse=ST&RaceNo=N

Hong Kong is the single best-documented racing jurisdiction in the world for
this purpose: a closed pool of roughly 1,200 horses, every one of them rated,
two courses, and the club publishes the finishing time, the sectional running
positions, the win odds of every runner and the full dividend table for every
pool. Two consequences for this app:

- The RESULTS parser below is verified against real meetings and is the
  richest calibration data we have anywhere -- it carries a market price
  (Win Odds) for every single runner, plus WIN/PLACE/QUINELLA dividends.
- The RACECARD parser is written to HKJC's published card layout but is
  marked provisional: HKJC takes a race card down once the meeting has run,
  and the Hong Kong season runs September to mid-July, so between mid-July
  and early September there is no card anywhere to parse. It was NOT
  verifiable at the time of writing (last meeting: Happy Valley, 15 Jul 2026).
  Call season_status() before trusting an empty result -- "no card" during the
  off-season is expected, not a bug.

Two important structural facts about HK betting, both of which the parlay
engine depends on:
- Dividends are quoted per HK$10 stake, not per HK$1. A WIN dividend of
  111.00 is a decimal price of 11.1, not 111. dividend_to_decimal() does that
  conversion in one place so nobody gets it wrong twice.
- HKJC is a pari-mutuel monopoly with roughly a 17.5% win-pool takeout. That
  is a very expensive market to bet into and it matters enormously for whether
  a multi is worth placing -- see models/odds.py.
"""
import re
import time
from datetime import date, datetime
from pathlib import Path

import requests
from bs4 import BeautifulSoup

USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")
BASE = "https://racing.hkjc.com/racing/information/English/Racing"
RESULTS_URL = BASE + "/LocalResults.aspx?RaceDate={date}"
RACECARD_URL = BASE + "/RaceCard.aspx?RaceDate={date}"

CACHE_DIR = Path(__file__).resolve().parent.parent / "data" / "cache"

# HKJC's two courses. Happy Valley races midweek under lights, Sha Tin at the
# weekend; Conghua is a mainland training centre and does not stage betting
# meetings, so it is deliberately absent.
RACECOURSES = {"HV": "Happy Valley", "ST": "Sha Tin"}

# The HK season runs from early September to mid-July -- the final meeting of
# a season is the Sha Tin/Happy Valley finale around the second or third
# Sunday of July. Outside that window there is no local racing at all: not a
# reduced card, none. Used only to explain an empty slate to the user, never
# to block a fetch.
SEASON_START_MONTH = 9
SEASON_END_MONTH = 7
SEASON_END_DAY = 20

# HKJC quotes every dividend per HK$10 stake.
DIVIDEND_UNIT = 10.0
# Published takeout on the win pool. Quoted in models/odds.py when it explains
# why a Hong Kong multi has to clear a much higher bar than an Australian one.
WIN_POOL_TAKEOUT = 0.175

RACE_META_RE = re.compile(r"RACE\s+(\d+)\s*(?:\((\d+)\))?", re.I)
CLASS_DIST_RE = re.compile(r"(Class\s*\d+|Group\s*[123]|Griffin Race|Restricted Race)\s*-\s*(\d+)M", re.I)
RATING_BAND_RE = re.compile(r"\((\d+)\s*-\s*(\d+)\)")
# Going has to be matched against HKJC's fixed vocabulary rather than a
# generic [A-Z ]+ run: the label sits immediately before an all-caps race name
# on the same line, so a greedy character class swallows the race name too.
GOING_TERMS = ("GOOD TO FIRM", "GOOD TO YIELDING", "YIELDING TO SOFT", "WET SLOW",
               "WET FAST", "GOOD", "FIRM", "FAST", "YIELDING", "SOFT", "HEAVY", "SLOW")
GOING_RE = re.compile(r"Going\s*:\s*(" + "|".join(GOING_TERMS) + r")\b", re.I)
COURSE_RE = re.compile(r"Course\s*:\s*(TURF[^:]*?|ALL WEATHER TRACK)\s*(?:$|Race|Going)", re.I)
HORSE_CODE_RE = re.compile(r"\s*\(([A-Z]\d{3})\)\s*$")


def _get(url: str) -> str:
    resp = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=40)
    resp.raise_for_status()
    return resp.text


def _fetch_cached(url: str, filename: str, use_cache: bool) -> str:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path = CACHE_DIR / filename
    if use_cache and path.exists():
        return path.read_text(encoding="utf-8", errors="ignore")
    html = _get(url)
    path.write_text(html, encoding="utf-8")
    time.sleep(1)
    return html


def _hk_date(date_str: str) -> str:
    """'2026-07-15' -> '2026/07/15' (HKJC's URL format)."""
    return date_str.replace("-", "/")


def season_status(today: date | None = None) -> dict:
    """Is Hong Kong racing on right now? Used to tell the difference between
    'the scraper broke' and 'there is genuinely no Hong Kong racing today'."""
    today = today or date.today()
    in_season = (today.month >= SEASON_START_MONTH
                 or today.month < SEASON_END_MONTH
                 or (today.month == SEASON_END_MONTH and today.day <= SEASON_END_DAY))
    if in_season:
        return {"in_season": True,
                "message": "Hong Kong season is running (Sep -- mid-Jul): cards appear a few days before each meeting."}
    resume_year = today.year if today.month < SEASON_START_MONTH else today.year + 1
    return {
        "in_season": False,
        "resumes": f"{resume_year}-09",
        "message": (f"Hong Kong is between seasons -- the last meeting of the season runs in mid-July and "
                    f"racing resumes in September {resume_year}. There are no HK cards or odds to fetch until "
                    f"then; use the Australia circuit in the meantime."),
    }


def _to_int(v):
    try:
        return int(str(v).strip())
    except (TypeError, ValueError):
        return None


def _to_float(v):
    try:
        return float(str(v).strip().replace(",", ""))
    except (TypeError, ValueError):
        return None


def _clean_horse(name: str) -> tuple[str, str | None]:
    """'FASHION LEGEND (J080)' -> ('FASHION LEGEND', 'J080').

    The bracketed code is HKJC's permanent horse ID and is far more reliable
    than the name for matching across seasons (HK horses get renamed on
    import), so it's kept alongside."""
    name = re.sub(r"\s+", " ", (name or "")).strip()
    m = HORSE_CODE_RE.search(name)
    if m:
        return HORSE_CODE_RE.sub("", name).strip().upper(), m.group(1)
    return name.upper(), None


def dividend_to_decimal(dividend: float | None) -> float | None:
    """HKJC dividends are per HK$10 stake; decimal odds are per 1."""
    if dividend is None:
        return None
    return round(dividend / DIVIDEND_UNIT, 4)


# --------------------------------------------------------------------------
# Meeting discovery
# --------------------------------------------------------------------------

def parse_meeting_dates(html: str) -> list[str]:
    """The results page carries a dropdown of every date with a published
    result. Note it includes dates with no LOCAL meeting (HKJC also lists
    simulcast days), so callers should treat a date as confirmed only once
    parse_raceresult actually returns races for it."""
    soup = BeautifulSoup(html, "lxml")
    dates = []
    for opt in soup.find_all("option"):
        text = opt.get_text(strip=True)
        try:
            dates.append(datetime.strptime(text, "%d/%m/%Y").strftime("%Y-%m-%d"))
        except ValueError:
            continue
    return dates


def list_meeting_dates(use_cache: bool = False) -> list[str]:
    html = _fetch_cached(BASE + "/LocalResults.aspx", "hkjc_results_index.html", use_cache)
    return parse_meeting_dates(html)


def _meeting_racecourse(soup) -> tuple[str | None, str | None]:
    """The js_racecard strip names the course, e.g. 'Happy Valley:'."""
    strip = soup.find("table", class_="js_racecard")
    if strip:
        text = strip.get_text(" ", strip=True)
        for code, name in RACECOURSES.items():
            if name.lower() in text.lower():
                return code, name
    return None, None


def _race_count(soup) -> int:
    """How many races the meeting has, read off the race-number strip."""
    strip = soup.find("table", class_="js_racecard")
    if not strip:
        return 0
    nums = [int(n) for n in re.findall(r"\b(\d{1,2})\b", strip.get_text(" ", strip=True))]
    return max(nums) if nums else 0


# --------------------------------------------------------------------------
# Results
# --------------------------------------------------------------------------

def fetch_raceresult_html(date_str: str, race_no: int | None = None,
                          racecourse: str | None = None, use_cache: bool = True) -> str:
    url = RESULTS_URL.format(date=_hk_date(date_str))
    if racecourse:
        url += f"&Racecourse={racecourse}"
    if race_no:
        url += f"&RaceNo={race_no}"
    name = f"hkjc_result_{date_str}_{racecourse or 'auto'}_{race_no or 1}.html"
    return _fetch_cached(url, name, use_cache)


def _parse_race_meta(soup) -> dict:
    """The unclassed table above the finishing order carries race number,
    class, distance, rating band, going and course."""
    meta = {"race_no": None, "race_name": None, "class_code": None, "distance_m": None,
            "rating_band_min": None, "rating_band_max": None, "going": None, "track_condition": None}
    for tbl in soup.find_all("table"):
        text = re.sub(r"\s+", " ", tbl.get_text(" ", strip=True))
        m = RACE_META_RE.search(text)
        if not m or "Pla." in text:
            continue
        meta["race_no"] = int(m.group(1))
        cd = CLASS_DIST_RE.search(text)
        if cd:
            meta["class_code"] = re.sub(r"\s+", " ", cd.group(1)).strip()
            meta["distance_m"] = int(cd.group(2))
        band = RATING_BAND_RE.search(text)
        if band:
            meta["rating_band_min"] = int(band.group(2))
            meta["rating_band_max"] = int(band.group(1))
        going = GOING_RE.search(text)
        if going:
            meta["going"] = going.group(1).strip().title()
            meta["track_condition"] = meta["going"]
        # Race name: the all-caps line that isn't the class/going/course line.
        for line in tbl.stripped_strings:
            s = line.strip()
            if (len(s) > 6 and s == s.upper() and not s.startswith("RACE")
                    and "GOING" not in s and "COURSE" not in s and "CLASS" not in s):
                meta["race_name"] = s.title()
                break
        break
    return meta


def _table_by_header(soup, *required):
    for tbl in soup.find_all("table"):
        rows = tbl.find_all("tr")
        if not rows:
            continue
        header = [c.get_text(" ", strip=True) for c in rows[0].find_all(["th", "td"])]
        if all(any(r.lower() == h.lower() for h in header) for r in required):
            return tbl, header, rows[1:]
    return None, [], []


def parse_dividends(soup) -> dict:
    """The dividend table -> {'WIN': {'combination': '3', 'dividend': 111.0}, ...}.

    Kept whole rather than flattened to a win price because the PLACE and
    QUINELLA rows are what tell us what a place bet or an exotic actually paid,
    which is the only way to check the place model against reality."""
    out: dict[str, list[dict]] = {}
    tbl, header, rows = _table_by_header(soup, "Pool", "Dividend (HK$)")
    if not tbl:
        for t in soup.find_all("table"):
            if "Dividend" in t.get_text(" ", strip=True):
                rows = t.find_all("tr")
                header = []
                break
        else:
            return out
    current_pool = None
    for tr in rows:
        cells = [c.get_text(" ", strip=True) for c in tr.find_all(["td", "th"])]
        cells = [c for c in cells if c != ""]
        if len(cells) == 3:
            pool, combo, div = cells
            current_pool = pool.upper() or current_pool
        elif len(cells) == 2 and current_pool:
            combo, div = cells
            pool = current_pool
        else:
            continue
        if pool.upper() in ("POOL", "DIVIDEND"):
            continue
        value = _to_float(div)
        if value is None:
            continue
        out.setdefault(pool.upper(), []).append({"combination": combo, "dividend": value})
    return out


def parse_raceresult(html: str) -> list[dict]:
    """One HKJC results page -> a single race, in the shape db/ingest.py wants.

    HKJC serves one race per page, so callers iterate RaceNo; the list return
    type is only for interface parity with the other scrapers."""
    soup = BeautifulSoup(html, "lxml")
    tbl, header, rows = _table_by_header(soup, "Pla.", "Horse", "Win Odds")
    if not tbl:
        return []
    meta = _parse_race_meta(soup)
    code, course = _meeting_racecourse(soup)
    idx = {h.lower(): i for i, h in enumerate(header)}

    def cell(cells, *names):
        for n in names:
            i = idx.get(n.lower())
            if i is not None and i < len(cells):
                return cells[i]
        return ""

    dividends = parse_dividends(soup)
    win_div = dividends.get("WIN", [{}])[0].get("dividend") if dividends.get("WIN") else None
    place_divs = {d["combination"]: d["dividend"] for d in dividends.get("PLACE", [])}

    runners, favourite, best_odds = [], None, None
    for tr in rows:
        cells = [c.get_text(" ", strip=True) for c in tr.find_all(["td", "th"])]
        if len(cells) < 4:
            continue
        horse_raw = cell(cells, "Horse")
        if not horse_raw or horse_raw.lower() == "horse":
            continue
        horse_name, horse_code = _clean_horse(horse_raw)
        placing = _to_int(cell(cells, "Pla."))
        win_odds = _to_float(cell(cells, "Win Odds"))
        saddlecloth = cell(cells, "Horse No.")
        if win_odds and (best_odds is None or win_odds < best_odds):
            best_odds, favourite = win_odds, horse_name
        runners.append({
            "horse_name": horse_name,
            "horse_code": horse_code,
            "placing": placing,
            "saddlecloth": _to_int(saddlecloth),
            "jockey": cell(cells, "Jockey") or None,
            "trainer": cell(cells, "Trainer") or None,
            "weight_kg": _to_float(cell(cells, "Act. Wt.")),
            "declared_weight": _to_float(cell(cells, "Declar. Horse Wt.")),
            "draw": _to_int(cell(cells, "Dr.")),
            "margin": cell(cells, "LBW") or None,
            "running_position": cell(cells, "Running Position") or None,
            "time": cell(cells, "Finish Time") or None,
            # HKJC's "Win Odds" column is already a decimal price per 1 unit.
            "decimal_odds": win_odds,
            "odds": str(win_odds - 1) if win_odds and win_odds > 1 else None,
            "dividend_place": dividend_to_decimal(place_divs.get(saddlecloth)),
            "scratched": placing is None,
        })

    return [{
        "race_no": meta["race_no"],
        "race_name": meta["race_name"],
        "class_code": meta["class_code"],
        "distance_m": meta["distance_m"],
        "rating_band_min": meta["rating_band_min"],
        "rating_band_max": meta["rating_band_max"],
        "going": meta["going"],
        "track_condition": meta["track_condition"],
        "racecourse_code": code,
        "venue": course,
        "runners": runners,
        "dividends": {
            "favourite": favourite,
            "win": dividend_to_decimal(win_div),
            "pools": dividends,
        },
    }]


def fetch_meeting_results(date_str: str, racecourse: str | None = None,
                          use_cache: bool = True, max_races: int = 12) -> list[dict]:
    """Walk every race of one meeting. Stops as soon as a race number returns
    nothing, so a 8-race card costs 8 requests, not max_races."""
    first_html = fetch_raceresult_html(date_str, None, racecourse, use_cache)
    soup = BeautifulSoup(first_html, "lxml")
    count = _race_count(soup) or max_races
    races = parse_raceresult(first_html)
    for n in range(2, min(count, max_races) + 1):
        html = fetch_raceresult_html(date_str, n, racecourse, use_cache)
        parsed = parse_raceresult(html)
        if not parsed:
            break
        races.extend(parsed)
    return [r for r in races if r.get("race_no")]


# --------------------------------------------------------------------------
# Race card (provisional -- see module docstring)
# --------------------------------------------------------------------------

def fetch_racecard_html(date_str: str, race_no: int | None = None,
                        racecourse: str | None = None, use_cache: bool = True) -> str:
    url = RACECARD_URL.format(date=_hk_date(date_str))
    if racecourse:
        url += f"&Racecourse={racecourse}"
    if race_no:
        url += f"&RaceNo={race_no}"
    name = f"hkjc_card_{date_str}_{racecourse or 'auto'}_{race_no or 1}.html"
    return _fetch_cached(url, name, use_cache)


def parse_racecard(html: str) -> list[dict]:
    """RaceCard.aspx -> races in the shape db/ingest.py wants.

    PROVISIONAL. HKJC withdraws a card once its meeting has run and the season
    was already over when this was written, so this matches the club's
    published card layout (same header vocabulary as the results table, plus
    'Wt.+/-' and 'Rtg.' columns) but has not been run against a live card. It
    fails soft -- an unrecognised page returns [] -- so the app shows 'no card'
    rather than a stack trace. Verify it on the first meeting of the new
    season before trusting a number that comes out of it."""
    soup = BeautifulSoup(html, "lxml")
    tbl, header, rows = _table_by_header(soup, "Horse", "Jockey", "Trainer")
    if not tbl:
        return []
    meta = _parse_race_meta(soup)
    code, course = _meeting_racecourse(soup)
    idx = {h.lower(): i for i, h in enumerate(header)}

    def cell(cells, *names):
        for n in names:
            i = idx.get(n.lower())
            if i is not None and i < len(cells):
                return cells[i]
        return ""

    runs = []
    for tr in rows:
        cells = [c.get_text(" ", strip=True) for c in tr.find_all(["td", "th"])]
        if len(cells) < 4:
            continue
        horse_raw = cell(cells, "Horse")
        if not horse_raw or horse_raw.lower() == "horse":
            continue
        horse_name, horse_code = _clean_horse(horse_raw)
        row_text = " ".join(cells)
        runs.append({
            "horse_name": horse_name,
            "horse_code": horse_code,
            "saddlecloth": _to_int(cell(cells, "Horse No.", "No.")),
            "jockey": cell(cells, "Jockey") or None,
            "trainer": cell(cells, "Trainer") or None,
            "owner": None,
            "weight_kg": _to_float(cell(cells, "Wt.", "Act. Wt.")),
            "draw": _to_int(cell(cells, "Draw", "Dr.")),
            "official_rating": _to_int(cell(cells, "Rtg.", "Rating")),
            # HK cards print form as a run-by-run string of recent placings,
            # most recent LAST -- same convention as Racing Australia, opposite
            # of RWITC -- so it is reversed here for the rating engine.
            "recent_runs": [{"placing": ch} for ch in reversed(cell(cells, "Last 6 Runs", "Form"))
                            if ch.isdigit()],
            "current_win_odds": _to_float(cell(cells, "Win Odds", "Odds")),
            "scratched": bool(re.search(r"\bWD\b|\bSCR\b|withdrawn", row_text, re.I)),
        })
    if not runs:
        return []
    return [{
        "race_no": meta["race_no"],
        "race_name": meta["race_name"],
        "class_code": meta["class_code"],
        "distance_m": meta["distance_m"],
        "rating_band_min": meta["rating_band_min"],
        "rating_band_max": meta["rating_band_max"],
        "going": meta["going"],
        "track_condition": meta["track_condition"],
        "racecourse_code": code,
        "venue": course,
        "runs": runs,
    }]


def fetch_meeting_card(date_str: str, racecourse: str | None = None,
                       use_cache: bool = True, max_races: int = 12) -> list[dict]:
    try:
        first_html = fetch_racecard_html(date_str, None, racecourse, use_cache)
    except requests.RequestException:
        return []
    soup = BeautifulSoup(first_html, "lxml")
    count = _race_count(soup) or max_races
    races = parse_racecard(first_html)
    for n in range(2, min(count, max_races) + 1):
        parsed = parse_racecard(fetch_racecard_html(date_str, n, racecourse, use_cache))
        if not parsed:
            break
        races.extend(parsed)
    return [r for r in races if r.get("race_no")]


def win_odds_from_card(races: list[dict]) -> list[dict]:
    """Pull the live win-odds column out of a parsed card into the flat shape
    db/ingest.store_market_odds() expects."""
    out = []
    for race in races:
        for run in race.get("runs", []):
            if run.get("current_win_odds"):
                out.append({
                    "race_no": race["race_no"],
                    "horse_name": run["horse_name"],
                    "market": "win",
                    "decimal_odds": run["current_win_odds"],
                })
    return out
