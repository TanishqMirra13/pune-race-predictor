"""Scraper for racingpulse.in Mock Races pages (free; unlike their
Selections/Form Guide/Trackwork, which are subscription-only and not
scraped here).

Mock races are multi-horse practice races run between race days. The page
for a race day lists, for each of today's runners, the mock races they took
part in since their last real run -- horses in FINISHING ORDER, the mock's
overall time, a vs-par figure, and a stewards-style comment ("Won by:- 3Ls;
SNK... 1st & 2nd name impressed.").

Discovery: race-day pages hang off sequential pgIds with no date parameter,
so we scan the homepage's venue blocks for 'Mock Races' links
(rupdate.aspx?pgId=N) and match the page's own printed date afterwards.
"""
import re
import time
from pathlib import Path

import requests
from bs4 import BeautifulSoup

USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
HOME_URL = "https://www.racingpulse.in/"
PAGE_URL = "https://www.racingpulse.in/Code/rupdate.aspx?pgId={pg_id}"

CACHE_DIR = Path(__file__).resolve().parent.parent / "data" / "cache"

VENUE_NAMES = ["Pune", "Bangalore", "Mumbai", "Mysore", "Hyderabad", "Kolkata", "Chennai", "Delhi", "Ooty"]
MONTHS = {m: i for i, m in enumerate(
    ["January", "February", "March", "April", "May", "June", "July",
     "August", "September", "October", "November", "December"], start=1)}

HORSE_CELL_RE = re.compile(r"^(.*?)\s*\(([^)]*)\)")
TIME_RE = re.compile(r"^(\d+):(\d+(?:\.\d+)?)$")
PAGE_DATE_RE = re.compile(r"(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday),\s*(\w+)\s+(\d+)\s*,\s*(\d{4})")
GROUP_DATE_RE = re.compile(r"(\w+)\s+(\d{4}),\s*(\d+)")


def _get(url: str) -> str:
    resp = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=30)
    resp.raise_for_status()
    return resp.text


def _nearest_preceding_venue(html: str, pos: int) -> str | None:
    chunk = html[max(0, pos - 4000):pos]
    nearest, nearest_pos = None, -1
    for v in VENUE_NAMES:
        p = chunk.upper().rfind(v.upper())
        if p > nearest_pos:
            nearest_pos, nearest = p, v
    return nearest


def discover_mock_race_pages(venue: str) -> list[int]:
    """Find the venue's 'Mock Races' pgIds. Two routes: direct links on the
    homepage (present once the race day is live), else via the venue's
    RaceCard page, whose related-links strip includes Mock Races as soon as
    racingpulse publishes it."""
    html = _get(HOME_URL)
    pg_ids = []
    for m in re.finditer(r"href=(?:Code/)?rupdate\.aspx\?pgId=(\d+)[^>]*>\s*Mock Races", html):
        if (_nearest_preceding_venue(html, m.start()) or "").lower() == venue.lower():
            pg_ids.append(int(m.group(1)))
    if pg_ids:
        return pg_ids

    # fall back through the venue's RaceCard page(s)
    racecard_ids = []
    for m in re.finditer(r"href=(?:Code/)?rupdate\.aspx\?pgId=(\d+)[^>]*>\s*RaceCard", html):
        if (_nearest_preceding_venue(html, m.start()) or "").lower() == venue.lower():
            racecard_ids.append(int(m.group(1)))
    for rc_id in racecard_ids[:2]:
        rc_html = fetch_page_html(rc_id, use_cache=False)
        for m in re.finditer(r"href=(?:Code/)?rupdate\.aspx\?pgId=(\d+)[^>]*>\s*Mock Races", rc_html):
            pg_ids.append(int(m.group(1)))
    return sorted(set(pg_ids))


def fetch_page_html(pg_id: int, use_cache: bool = True) -> str:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path = CACHE_DIR / f"rp_page_{pg_id}.html"
    if use_cache and path.exists():
        return path.read_text(encoding="utf-8", errors="ignore")
    html = _get(PAGE_URL.format(pg_id=pg_id))
    path.write_text(html, encoding="utf-8")
    time.sleep(1)
    return html


def parse_page_date(html: str) -> str | None:
    m = PAGE_DATE_RE.search(html)
    if not m:
        return None
    month = MONTHS.get(m.group(1))
    if not month:
        return None
    return f"{m.group(3)}-{month:02d}-{int(m.group(2)):02d}"


def parse_mock_races(html: str, default_venue: str) -> list[dict]:
    """Returns one record per (mock race, horse), finish_pos = listed order.
    Duplicated groups (the page repeats each mock once per today's runner in
    it) are deduped on (date, distance, horse tuple)."""
    soup = BeautifulSoup(html, "lxml")
    candidates = [
        t for t in soup.find_all("table")
        if "Won by" in t.get_text()
        and not [x for x in t.find_all("table") if "Won by" in x.get_text()]
    ]
    seen = set()
    records = []
    for table in candidates:
        rows = table.find_all("tr")
        if not rows:
            continue
        header_cells = [c.get_text(" ", strip=True) for c in rows[0].find_all(["td", "th"])]
        if len(header_cells) < 3:
            continue
        gm = GROUP_DATE_RE.search(header_cells[0])
        if not gm or gm.group(1) not in MONTHS:
            continue
        work_date = f"{gm.group(2)}-{MONTHS[gm.group(1)]:02d}-{int(gm.group(3)):02d}"
        venue = header_cells[1].strip() or default_venue

        horses = []
        time_sec = vs_par = None
        for row in rows[1:]:
            cells = [c.get_text(" ", strip=True) for c in row.find_all("td")]
            if len(cells) == 4 and cells[3]:
                hm = HORSE_CELL_RE.match(cells[3])
                if hm:
                    name = re.sub(r"\s+", " ", hm.group(1)).strip()
                    horses.append((name, hm.group(2).strip() or None))
            elif len(cells) == 3 and re.match(r"^\d{3,4}M$", cells[0]):
                tm = TIME_RE.match(cells[1])
                if tm:
                    time_sec = int(tm.group(1)) * 60 + float(tm.group(2))
                vp = re.search(r"\((-?\d+(?:\.\d+)?)\)", cells[2])
                if vp:
                    vs_par = float(vp.group(1))
        dm = re.search(r"(\d{3,4})M", header_cells[2])
        distance_m = int(dm.group(1)) if dm else None

        cm = re.search(r"Won by:-\s*([^|]{0,400})", table.get_text("|", strip=True))
        comment = cm.group(1).replace("|", " ").strip() if cm else None

        key = (work_date, distance_m, tuple(h[0] for h in horses))
        if not horses or key in seen:
            continue
        seen.add(key)
        for pos, (name, rider) in enumerate(horses, start=1):
            records.append({
                "kind": "mock", "venue": venue, "work_date": work_date,
                "distance_m": distance_m, "horse_name": name, "rider": rider,
                "time_sec": time_sec, "vs_par": vs_par,
                "finish_pos": pos, "field_size": len(horses),
                "comment": comment,
            })
    return records
