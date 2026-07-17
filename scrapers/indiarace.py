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

VENUE_IDS = {"Pune": 10, "Bangalore": 3, "Mumbai": None}

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
