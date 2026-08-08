"""Scraper for indiarace.com morning trackwork (gallop) reports.

Verified endpoints (Jul 2026):
- https://www.indiarace.com/Home/allTrackworkData/{venueId}      -> JSON list of dates
- https://www.indiarace.com/Home/trackWorkByVenueAndDate/{venueId}/{YYYY-MM-DD}

Venue ids probed live: Pune=10, Bangalore=3 (Mysore=8, Hyderabad=11 unused
here). Mumbai has no id with data -- RWITC horses do their monsoon trackwork
at Pune anyway, so Mumbai maps to None and callers skip gracefully.

Page structure: one <table> per workout distance (600M/800M/...). Each row:
[rating(s)] [age(s)] [<b>Name</b> (Rider) x1-2 separated by <br>]
[time + optional sectional + comment] [(vs-par)]. A row can hold two horses
working together; the comment ("Former impressed & finished dist ahead")
then describes their relative merit -- we attach the same comment to both
and let the keyword scorer pick up 'former'/'latter'.
"""
import json
import re
import time
from pathlib import Path

import requests
from bs4 import BeautifulSoup

USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
DATES_URL = "https://www.indiarace.com/Home/allTrackworkData/{venue_id}"
DETAIL_URL = "https://www.indiarace.com/Home/trackWorkByVenueAndDate/{venue_id}/{date}"
ODDS_URL = (
    "https://www.indiarace.com/Home/racingCenterEvent"
    "?venueId={venue_id}&event_date={date}&race_type=ODDS"
)

# Trackwork venue ids. Mumbai is None: indiarace publishes no Mumbai trackwork
# feed (RWITC horses do their monsoon work at Pune), so callers skip it.
VENUE_IDS = {"Pune": 10, "Bangalore": 3, "Mumbai": None}

# Odds venue ids -- kept separate from VENUE_IDS because the two feeds don't
# cover the same venues. Only ids verified to return a real odds table belong
# here; an unverified guess would silently show another venue's prices.
ODDS_VENUE_IDS = {"Pune": 10, "Bangalore": 3}

CACHE_DIR = Path(__file__).resolve().parent.parent / "data" / "cache"

DIST_RE = re.compile(r"^(\d{3,4})M$", re.I)
HORSE_RE = re.compile(r"<b>\s*([^<]+?)\s*</b>\s*\(([^)]*)\)", re.S)
VSPAR_RE = re.compile(r"\((-?\d+(?:\.\d+)?)\)")
TIME_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)")


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
    time.sleep(1)
    return html


def fetch_trackwork_dates(venue: str) -> list[str]:
    venue_id = VENUE_IDS.get(venue)
    if venue_id is None:
        return []
    raw = _get(DATES_URL.format(venue_id=venue_id))
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return []
    return sorted({d["event_date"] for d in data if d.get("event_date")})


def fetch_trackwork_html(venue: str, date: str, use_cache: bool = True) -> str | None:
    venue_id = VENUE_IDS.get(venue)
    if venue_id is None:
        return None
    return _fetch_cached(
        DETAIL_URL.format(venue_id=venue_id, date=date),
        f"ir_trackwork_{venue.lower()}_{date}.html", use_cache,
    )


def parse_trackwork(html: str, venue: str, date: str) -> list[dict]:
    soup = BeautifulSoup(html, "lxml")
    records = []
    for table in soup.find_all("table"):
        rows = table.find_all("tr")
        if not rows:
            continue
        header = rows[0].get_text(" ", strip=True).split()
        if not header or not DIST_RE.match(header[0]):
            continue
        distance_m = int(DIST_RE.match(header[0]).group(1))
        for row in rows[1:]:
            tds = row.find_all("td")
            if len(tds) < 5:
                continue
            horses = HORSE_RE.findall(str(tds[2]))
            if not horses:
                continue
            time_cell = tds[3].get_text(" ", strip=True)
            tm = TIME_RE.match(time_cell)
            time_sec = float(tm.group(1)) if tm else None
            # comment = whatever remains after stripping the leading time and
            # any "600 40,"-style sectional splits
            comment = re.sub(r"^[-\d\s.,]*(?:\d{3,4}\s+\d+(?:\.\d+)?[\s,]*)*", "", time_cell)
            comment = re.sub(r"\s+", " ", comment).strip(" ,.-") or None
            if comment and re.fullmatch(r"[-\d\s.,]+", comment):
                comment = None
            vp = VSPAR_RE.search(tds[4].get_text(strip=True))
            vs_par = float(vp.group(1)) if vp else None
            for name, rider in horses:
                records.append({
                    "kind": "trackwork", "venue": venue, "work_date": date,
                    "distance_m": distance_m, "horse_name": re.sub(r"\s+", " ", name).strip(),
                    "rider": rider.strip() or None, "time_sec": time_sec,
                    "vs_par": vs_par, "finish_pos": None, "field_size": None,
                    "comment": comment,
                })
    return records


# --- Pre-race odds -------------------------------------------------------
# RWITC/BTC publish no pre-race prices themselves, but indiarace lists
# Night / Morning / Opening odds per runner in fractional form ("4/1", "18/10",
# "45/100"). These are INDICATIVE forecast prices, not the live tote board --
# useful for spotting value the night before, but always re-check the board
# before staking. One <table class="handi_table"> per race, columns:
# Horse Name ("10. SPOTLIGHT") | Jockey | Night | Morning | Opening.

ODDS_CARD_RE = re.compile(r"^\s*(\d+)\s*\.\s*(.+?)\s*$")


def fetch_odds_html(venue: str, date: str, use_cache: bool = False) -> str | None:
    """Odds move, so this defaults to use_cache=False unlike the trackwork fetch."""
    venue_id = ODDS_VENUE_IDS.get(venue)
    if venue_id is None:
        return None
    return _fetch_cached(
        ODDS_URL.format(venue_id=venue_id, date=date),
        f"ir_odds_{venue.lower()}_{date}.html", use_cache,
    )


def _frac_to_decimal(text: str) -> float | None:
    """'4/1' -> 4.0, '18/10' -> 1.8, '45/100' -> 0.45. Returns odds-to-one."""
    text = (text or "").strip()
    if not text:
        return None
    if "/" in text:
        num, _, den = text.partition("/")
        try:
            d = float(den)
            return float(num) / d if d else None
        except ValueError:
            return None
    try:
        return float(text)
    except ValueError:
        return None


def parse_odds(html: str) -> dict[str, dict]:
    """Map UPPERCASED horse name -> odds info.

    Keyed by name rather than race/table order: the page carries no race
    numbers, and our race cards already know which horses are in which race,
    so joining on name avoids depending on table sequence.
    Prefers the freshest column present (Opening > Morning > Night).
    """
    soup = BeautifulSoup(html, "lxml")
    out: dict[str, dict] = {}
    for table in soup.find_all("table", class_="handi_table"):
        rows = table.find_all("tr")
        if len(rows) < 2:
            continue
        header = [c.get_text(strip=True).lower() for c in rows[0].find_all(["th", "td"])]
        if not header or "horse name" not in header[0]:
            continue
        idx = {name: i for i, name in enumerate(header)}
        for row in rows[1:]:
            cells = [c.get_text(" ", strip=True) for c in row.find_all("td")]
            if len(cells) < 3:
                continue
            m = ODDS_CARD_RE.match(cells[0])
            card_no, name = (m.group(1), m.group(2)) if m else (None, cells[0])
            night = cells[idx["night odds"]] if idx.get("night odds", 99) < len(cells) else ""
            morning = cells[idx["morning odds"]] if idx.get("morning odds", 99) < len(cells) else ""
            opening = cells[idx["opening odds"]] if idx.get("opening odds", 99) < len(cells) else ""
            best_label, best_raw = None, None
            for label, raw in (("opening", opening), ("morning", morning), ("night", night)):
                if raw and raw.strip():
                    best_label, best_raw = label, raw.strip()
                    break
            dec = _frac_to_decimal(best_raw) if best_raw else None
            if dec is None:
                continue
            out[name.strip().upper()] = {
                "horse_name": name.strip(),
                "card_no": int(card_no) if card_no else None,
                "jockey": cells[1] if len(cells) > 1 else None,
                "odds_fraction": best_raw,
                "odds_decimal": dec,
                "odds_stage": best_label,
            }
    return out
