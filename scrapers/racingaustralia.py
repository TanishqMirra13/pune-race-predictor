"""Scraper/parser for Racing Australia's free fields, form and results.

Data source (verified live, Jul 2026): https://www.racingaustralia.horse
- Calendar (upcoming):  /FreeFields/Calendar.aspx?State=NSW
- Calendar (results):   /FreeFields/Calendar_Results.aspx?State=NSW
- Fields + full form:   /FreeFields/Form.aspx?Key=2026Aug01,NSW,Rosehill Gardens
- Results:              /FreeFields/Results.aspx?Key=...
- Scratchings:          /FreeFields/Scratchings.aspx?Key=...

Why this source rather than a tipping/odds site: Racing Australia is the
national industry body, so it's the primary record (nothing sits between it
and the stewards), it's free, and -- unlike TAB's API, punters.com.au and
racenet.com.au, all of which geo-block or 403 an Indian IP -- it actually
answers from here. Verified from this machine, Jul 2026.

What it gives us that the Indian clubs never did:
- an official handicap rating for nearly every runner ("Hcp Rating"),
- a 10-run form string per runner ("Last 10"),
- a barrier draw,
- and, on the results page, a REAL STARTING PRICE in decimal odds.

That last one is the important one. Phase 1 of this app could not compute a
true expected value because RWITC publishes no pre-race market. Here there is
a market to price against, which is what makes models/parlay.py possible.

Two gotchas this parser handles, both verified empirically rather than assumed:
- "Last 10" reads OLDEST-first, left to right (checked by matching
  HELLOVA NATURE's "90x0321121" against its dated run list: the trailing
  "21121" lines up with its Apr->Jul placings 2,1,1,2,1). RWITC prints form
  most-recent-first, and models/rating_engine.py weights position 0 heaviest,
  so parse_racecard reverses the AU string on the way in. Getting this
  backwards would silently invert the form signal.
- Meetings whose name ends in ",Trial" are barrier trials: no prize money, no
  betting market, and form from them is not comparable to race form. They're
  excluded from the meeting list by default.
"""
import re
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

import requests
from bs4 import BeautifulSoup

USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")
BASE = "https://www.racingaustralia.horse"
CALENDAR_URL = BASE + "/FreeFields/Calendar.aspx?State={state}"
CALENDAR_RESULTS_URL = BASE + "/FreeFields/Calendar_Results.aspx?State={state}"
FORM_URL = BASE + "/FreeFields/Form.aspx?Key={key}"
RESULTS_URL = BASE + "/FreeFields/Results.aspx?Key={key}"
SCRATCHINGS_URL = BASE + "/FreeFields/Scratchings.aspx?Key={key}"

STATES = ["NSW", "VIC", "QLD", "WA", "SA", "TAS", "ACT", "NT"]

CACHE_DIR = Path(__file__).resolve().parent.parent / "data" / "cache"

RACE_TITLE_RE = re.compile(
    r"Race\s+(\d+)\s*-\s*([\d:]+\s*[APap][Mm])?\s*(.*?)\s*\((\d+)\s*METRES?\)", re.S)
TRACK_COND_RE = re.compile(r"\b(Good|Soft|Heavy|Firm|Synthetic)\s*\(?(\d+)?\)?", re.I)
PRIZE_RE = re.compile(r"Of\s*\$([\d,]+)", re.I)
WEIGHT_RE = re.compile(r"(\d+(?:\.\d+)?)\s*kg", re.I)
SP_RE = re.compile(r"\$\s*(\d+(?:\.\d+)?)")
APPRENTICE_RE = re.compile(r"\s*\([^)]*\)\s*$")

# Australian class tokens, most specific first -- the first hit wins so
# "MAIDEN PLATE" is classed MAIDEN rather than the generic PLATE.
CLASS_PATTERNS = [
    (re.compile(r"\bGROUP\s*([123])\b", re.I), lambda m: f"G{m.group(1)}"),
    (re.compile(r"\bLISTED\b", re.I), lambda m: "LR"),
    (re.compile(r"\bMAIDEN\b", re.I), lambda m: "MDN"),
    (re.compile(r"\bBM\s*(\d+)\b", re.I), lambda m: f"BM{m.group(1)}"),
    (re.compile(r"\bBENCHMARK\s*(\d+)\b", re.I), lambda m: f"BM{m.group(1)}"),
    (re.compile(r"\bCLASS\s*(\d+)\b", re.I), lambda m: f"CL{m.group(1)}"),
    (re.compile(r"\bOPEN\b", re.I), lambda m: "OPEN"),
    (re.compile(r"\bWELTER\b", re.I), lambda m: "WELTER"),
    (re.compile(r"\bMIDWAY\b", re.I), lambda m: "MIDWAY"),
]


def _get(url: str) -> str:
    resp = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=45)
    resp.raise_for_status()
    return resp.text


def _fetch_cached(url: str, filename: str, use_cache: bool) -> str:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path = CACHE_DIR / filename
    if use_cache and path.exists():
        return path.read_text(encoding="utf-8", errors="ignore")
    html = _get(url)
    path.write_text(html, encoding="utf-8")
    time.sleep(1)  # be a polite guest on someone else's server
    return html


def _safe_name(key: str) -> str:
    return re.sub(r"[^A-Za-z0-9]+", "_", key)


# --------------------------------------------------------------------------
# Meeting discovery
# --------------------------------------------------------------------------

def _key_to_date(key: str) -> str | None:
    """'2026Aug01,NSW,Rosehill Gardens' -> '2026-08-01'."""
    head = key.split(",")[0].strip()
    try:
        return datetime.strptime(head, "%Y%b%d").strftime("%Y-%m-%d")
    except ValueError:
        return None


def parse_calendar(html: str, state: str) -> list[dict]:
    """Rows of the race-fields calendar table -> meeting descriptors.

    Each row carries a separate link per document type (Nominations, Weights,
    Acceptances, Form, Gear, Scratchings, Results); a missing link means that
    document isn't published yet, which is itself the signal for whether a
    meeting is loadable."""
    soup = BeautifulSoup(html, "lxml")
    table = soup.find("table", class_="race-fields")
    if not table:
        return []
    rows = table.find_all("tr")
    if not rows:
        return []
    header = [c.get_text(strip=True) for c in rows[0].find_all(["th", "td"])]
    meetings = []
    for tr in rows[1:]:
        cells = tr.find_all(["th", "td"])
        if len(cells) < 3:
            continue
        venue = cells[1].get_text(" ", strip=True)
        if not venue:
            continue
        links, key = {}, None
        for name, cell in zip(header[2:], cells[2:]):
            a = cell.find("a", href=True)
            if not a:
                continue
            links[name] = BASE + a["href"] if a["href"].startswith("/") else a["href"]
            if key is None:
                m = re.search(r"Key=([^&]+)", a["href"])
                if m:
                    key = requests.utils.unquote(m.group(1))
        if not key:
            continue
        meetings.append({
            "key": key,
            "date": _key_to_date(key),
            "venue": venue.replace(" (Trial)", "").strip(),
            "state": state,
            "is_trial": key.rstrip().endswith(",Trial") or "(Trial)" in venue,
            "links": links,
            "has_form": "Form" in links,
            "has_results": "Results" in links,
        })
    return meetings


def list_meetings(state: str, results_calendar: bool = False, use_cache: bool = True,
                  include_trials: bool = False) -> list[dict]:
    url = (CALENDAR_RESULTS_URL if results_calendar else CALENDAR_URL).format(state=state)
    suffix = "results" if results_calendar else "fields"
    html = _fetch_cached(url, f"ra_calendar_{state}_{suffix}.html", use_cache)
    meetings = parse_calendar(html, state)
    if not include_trials:
        meetings = [m for m in meetings if not m["is_trial"]]
    return meetings


def meetings_for_date(date_str: str, states: list[str] | None = None,
                      results_calendar: bool = False, use_cache: bool = True) -> list[dict]:
    """Every non-trial meeting across the requested states on one date.

    Australia races somewhere every single day of the year, typically at 5-10
    tracks at once, so this is the function that defines "today's slate"."""
    out = []
    for state in (states or STATES):
        try:
            out.extend(m for m in list_meetings(state, results_calendar, use_cache)
                       if m["date"] == date_str)
        except Exception:
            continue  # one state's page being down shouldn't blank the whole slate
    return sorted(out, key=lambda m: (m["state"], m["venue"]))


# --------------------------------------------------------------------------
# Fields + form
# --------------------------------------------------------------------------

def fetch_form_html(key: str, use_cache: bool = True) -> str:
    return _fetch_cached(FORM_URL.format(key=quote(key)), f"ra_form_{_safe_name(key)}.html", use_cache)


def fetch_racecard_html(key: str, use_cache: bool = True) -> str:
    """Alias matching the rwitc/btc scraper interface."""
    return fetch_form_html(key, use_cache)


def fetch_results_html(key: str, use_cache: bool = True) -> str:
    return _fetch_cached(RESULTS_URL.format(key=quote(key)), f"ra_results_{_safe_name(key)}.html", use_cache)


def fetch_scratchings_html(key: str, use_cache: bool = True) -> str:
    return _fetch_cached(SCRATCHINGS_URL.format(key=quote(key)), f"ra_scr_{_safe_name(key)}.html", use_cache)


def _class_code(race_name: str) -> str | None:
    for pattern, render in CLASS_PATTERNS:
        m = pattern.search(race_name)
        if m:
            return render(m)
    return None


def _parse_race_title(table) -> dict | None:
    text = re.sub(r"\s+", " ", table.get_text(" ", strip=True))
    m = RACE_TITLE_RE.search(text)
    if not m:
        return None
    race_no, race_time, name, distance = m.groups()
    name = name.strip(" -")
    prize = PRIZE_RE.search(text)
    return {
        "race_no": int(race_no),
        "race_time_local": (race_time or "").strip().upper() or None,
        "race_name": name,
        "distance_m": int(distance),
        "class_code": _class_code(name),
        "prize_money": int(prize.group(1).replace(",", "")) if prize else None,
    }


def _to_float(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _to_int(v):
    try:
        return int(str(v).strip())
    except (TypeError, ValueError):
        return None


def _clean_person(name: str | None) -> str | None:
    """'Ms Mollie Fitzgerald (a3/52kg)' -> 'Ms Mollie Fitzgerald'.

    The parenthetical is the apprentice's claim, which changes race to race --
    keeping it in the name would fragment one rider's strike-rate history
    across several spellings."""
    if not name:
        return None
    cleaned = APPRENTICE_RE.sub("", name).strip()
    return cleaned or None


def _apprentice_claim(name: str | None) -> float | None:
    if not name:
        return None
    m = re.search(r"\(a(\d+(?:\.\d+)?)", name)
    return float(m.group(1)) if m else None


def au_form_to_recent_runs(last10: str | None) -> list[dict]:
    """'90x0321121' -> [{'placing': '1'}, {'placing': '2'}, ...] most recent FIRST.

    Racing Australia prints oldest-first; models/rating_engine.py weights the
    first entry heaviest, so the string is reversed here. Non-digits ('x' for a
    spell, 'f' fell, 'd' disqualified) are passed through as '?' -- the rating
    engine already treats an unparseable placing as "ran, didn't place", which
    is the honest reading of a spell marker."""
    if not last10:
        return []
    runs = []
    for ch in reversed(last10.strip()):
        if ch.isdigit():
            runs.append({"placing": ch})
        elif ch.isalpha():
            runs.append({"placing": "?"})
    return runs


def _field_rows(strip_table) -> tuple[list[str], list[list]]:
    rows = strip_table.find_all("tr")
    if not rows:
        return [], []
    header = [c.get_text(" ", strip=True) for c in rows[0].find_all(["th", "td"])]
    body = []
    for tr in rows[1:]:
        cells = tr.find_all(["th", "td"])
        if len(cells) != len(header):
            continue
        body.append(cells)
    return header, body


def parse_racecard(html: str) -> list[dict]:
    """Form.aspx -> races in the same shape rwitc/btc emit, so db/ingest.py and
    models/rating_engine.py consume it unmodified."""
    soup = BeautifulSoup(html, "lxml")
    titles = soup.find_all("table", class_="race-title")
    strips = soup.find_all("table", class_="race-strip-fields")
    page_text = soup.get_text(" ")
    cond = TRACK_COND_RE.search(page_text)
    track_condition = (f"{cond.group(1).title()}{cond.group(2) or ''}" if cond else None)

    races = []
    for title_tbl, strip_tbl in zip(titles, strips):
        meta = _parse_race_title(title_tbl)
        if not meta:
            continue
        header, body = _field_rows(strip_tbl)
        if not header:
            continue
        idx = {name: i for i, name in enumerate(header)}

        def cell(cells, *names):
            for n in names:
                if n in idx:
                    return cells[idx[n]].get_text(" ", strip=True)
            return ""

        runs = []
        for cells in body:
            horse = cell(cells, "Horse")
            if not horse:
                continue
            row_text = " ".join(c.get_text(" ", strip=True) for c in cells)
            scratched = bool(re.search(r"\bSCR(ATCHED)?\b", row_text, re.I))
            weight_txt = cell(cells, "Weight", "Probable Weight")
            wm = WEIGHT_RE.search(weight_txt)
            jockey_raw = cell(cells, "Jockey")
            runs.append({
                "horse_name": horse.upper().strip(),
                "saddlecloth": _to_int(cell(cells, "No", "No.")),
                "jockey": _clean_person(jockey_raw),
                "apprentice_claim": _apprentice_claim(jockey_raw),
                "trainer": _clean_person(cell(cells, "Trainer")) or None,
                "owner": None,  # Racing Australia's free form doesn't print owners
                "weight_kg": _to_float(wm.group(1)) if wm else None,
                "draw": _to_int(cell(cells, "Barrier", "Bar.")),
                "official_rating": _to_int(cell(cells, "Hcp Rating", "Rating")),
                "recent_runs": au_form_to_recent_runs(cell(cells, "Last 10")),
                "scratched": scratched,
            })
        races.append({**meta, "track_condition": track_condition, "runs": runs})
    return races


def parse_scratchings(html: str) -> dict[int, list[str]]:
    """{race_no: [HORSE NAME, ...]} from the Scratchings page.

    Scratchings matter more than they look: a late scratching shrinks the
    field, which changes both the model's per-runner probability and how many
    places the pool pays."""
    soup = BeautifulSoup(html, "lxml")
    out: dict[int, list[str]] = {}
    current = None
    for tbl in soup.find_all("table"):
        text = re.sub(r"\s+", " ", tbl.get_text(" ", strip=True))
        m = re.search(r"Race\s+(\d+)", text)
        if m and "race-title" in (tbl.get("class") or []):
            current = int(m.group(1))
            out.setdefault(current, [])
            continue
        if current is None:
            continue
        for tr in tbl.find_all("tr"):
            cells = [c.get_text(" ", strip=True) for c in tr.find_all(["td", "th"])]
            for c in cells:
                if re.fullmatch(r"[A-Z][A-Z '\-\.]{2,}", c) and c not in ("HORSE", "TRAINER", "JOCKEY"):
                    out[current].append(c)
    return {k: sorted(set(v)) for k, v in out.items() if v}


# --------------------------------------------------------------------------
# Results
# --------------------------------------------------------------------------

def parse_raceresult(html: str) -> list[dict]:
    """Results.aspx -> finishing order plus the decimal starting price of every
    runner. The SP is the whole point: it's the market's closing opinion, which
    is the benchmark models/parlay.py has to beat before any bet is worth
    making."""
    soup = BeautifulSoup(html, "lxml")
    titles = soup.find_all("table", class_="race-title")
    strips = soup.find_all("table", class_="race-strip-fields")
    races = []
    for title_tbl, strip_tbl in zip(titles, strips):
        meta = _parse_race_title(title_tbl)
        if not meta:
            continue
        header, body = _field_rows(strip_tbl)
        if not header:
            continue
        idx = {name: i for i, name in enumerate(header)}

        def cell(cells, *names):
            for n in names:
                if n in idx:
                    return cells[idx[n]].get_text(" ", strip=True)
            return ""

        runners, favourite = [], None
        for cells in body:
            horse = cell(cells, "Horse")
            if not horse:
                continue
            finish_txt = cell(cells, "Finish")
            placing = _to_int(finish_txt)
            sp_txt = cell(cells, "Starting Price", "SP")
            sp = SP_RE.search(sp_txt)
            decimal_sp = _to_float(sp.group(1)) if sp else None
            if sp_txt.upper().rstrip().endswith("F") and favourite is None:
                favourite = horse.upper().strip()
            weight_txt = cell(cells, "Weight")
            wm = WEIGHT_RE.search(weight_txt)
            runners.append({
                "horse_name": horse.upper().strip(),
                "placing": placing,
                "saddlecloth": _to_int(cell(cells, "No.", "No")),
                "jockey": _clean_person(cell(cells, "Jockey")),
                "trainer": _clean_person(cell(cells, "Trainer")) or None,
                "weight_kg": _to_float(wm.group(1)) if wm else None,
                "draw": _to_int(cell(cells, "Bar.", "Barrier")),
                "margin": cell(cells, "Margin") or None,
                # Racing Australia quotes SP as a decimal payout including
                # stake ($5.50 returns 5.50 for 1). db/ingest stores odds-to-one,
                # so hand back both and let the caller pick.
                "decimal_odds": decimal_sp,
                "odds": str(decimal_sp - 1) if decimal_sp and decimal_sp > 1 else None,
                "scratched": placing is None,
            })
        races.append({**meta, "runners": runners, "dividends": {"favourite": favourite}})
    return races


# --------------------------------------------------------------------------
# Connection statistics
# --------------------------------------------------------------------------

def jockey_trainer_stats_from_results(results_races: list[dict]) -> dict:
    """Racing Australia publishes no season strike-rate leaderboard the way
    RWITC and BTC do, so for the AU circuit those numbers have to be derived
    from our own accumulated results archive instead. This helper just tallies
    one meeting; scripts/backfill_intl.py aggregates across meetings and
    models/connections.py applies the same Bayesian shrinkage used for India,
    so a jockey with 4 rides on file never outranks one with 200."""
    tally: dict[tuple[str, str], dict] = {}
    for race in results_races:
        for r in race["runners"]:
            if r.get("scratched"):
                continue
            for role in ("jockey", "trainer"):
                name = r.get(role)
                if not name:
                    continue
                rec = tally.setdefault((role, name), {
                    "name": name, "wins": 0, "seconds": 0, "thirds": 0,
                    "fourths": 0, "total_rides": 0, "win_pct": None,
                })
                rec["total_rides"] += 1
                pos = r.get("placing")
                if pos == 1:
                    rec["wins"] += 1
                elif pos == 2:
                    rec["seconds"] += 1
                elif pos == 3:
                    rec["thirds"] += 1
                elif pos == 4:
                    rec["fourths"] += 1
    for rec in tally.values():
        rec["win_pct"] = round(rec["wins"] / rec["total_rides"] * 100, 2) if rec["total_rides"] else None
    return {
        "jockey": [v for (role, _), v in tally.items() if role == "jockey"],
        "trainer": [v for (role, _), v in tally.items() if role == "trainer"],
    }
