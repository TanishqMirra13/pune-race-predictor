"""Scraper/parser for Bangalore Turf Club's race card and race result pages.

Data source (verified live, Jul 2026):
- Race card: https://bangaloreraces.com/racing/racecard?d=YYYY-MM-DD
- Results:   https://bangaloreraces.com/racing/results?d=YYYY-MM-DD

Unlike RWITC's legacy nested tables, this is a modern Bootstrap site with real
semantic hooks (div.race-num, table.card-table, labelled spans), so this
parser walks the DOM directly instead of regexing flattened text.

Output shape matches scrapers/rwitc.py exactly (race_no, race_name,
distance_m, class_code, runs/runners, dividends, ...) so the same
db/ingest.py and models/rating_engine.py work unmodified for both sources.

Known gap: BTC's racecard shows recent-form as a letter "Sh" column (e.g.
"HA HA HA HA") rather than RWITC's numeric placings, and results don't carry
a per-runner trainer name (only the race winner's). Both are left as None
rather than guessed -- the rating engine already degrades gracefully when
form/trainer data is missing.
"""
import re
import time
from pathlib import Path

import requests
from bs4 import BeautifulSoup

USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
RACECARD_URL = "https://bangaloreraces.com/racing/racecard?d={date}"
RACERESULT_URL = "https://bangaloreraces.com/racing/results?d={date}"
JOCKEY_STATS_URL = "https://www.bangaloreraces.com/Home/JockeyStats"
TRAINER_STATS_URL = "https://www.bangaloreraces.com/home/trainerstats"

CACHE_DIR = Path(__file__).resolve().parent.parent / "data" / "cache"

RATED_RANGE_RE = re.compile(r"rated\s+(\d+)\s+to\s+(\d+)", re.I)
RATED_ABOVE_RE = re.compile(r"rated\s+(\d+)\s+and\s+above", re.I)
GRADE_RE = re.compile(r"Grade\s+([IVX]+)", re.I)


def _get(url: str) -> str:
    resp = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=30)
    resp.raise_for_status()
    return resp.text


def fetch_racecard_html(date: str, use_cache: bool = True) -> str:
    return _fetch_cached(RACECARD_URL.format(date=date), f"btc_racecard_{date}.html", use_cache)


def fetch_jockey_stats_html(use_cache: bool = True) -> str:
    return _fetch_cached(JOCKEY_STATS_URL, "btc_jockey_stats.html", use_cache)


def fetch_trainer_stats_html(use_cache: bool = True) -> str:
    return _fetch_cached(TRAINER_STATS_URL, "btc_trainer_stats.html", use_cache)


def _parse_official_stats_table(html: str, name_key: str) -> list[dict]:
    """BTC's JockeyStats/trainerstats pages: (NAME, Total, 1st, 2nd, 3rd, 4th) --
    no win% column, so it's derived here."""
    soup = BeautifulSoup(html, "lxml")
    table = soup.find("table")
    if not table:
        return []
    rows = table.find_all("tr")
    if not rows:
        return []
    header = [c.get_text(strip=True).lower() for c in rows[0].find_all(["th", "td"])]
    out = []
    for r in rows[1:]:
        cells = [c.get_text(strip=True) for c in r.find_all(["th", "td"])]
        if len(cells) != len(header):
            continue
        d = dict(zip(header, cells))
        name = d.get(name_key.lower())
        if not name:
            continue
        total = _to_int(d.get("total"))
        wins = _to_int(d.get("1st"))
        out.append({
            "name": name,
            "wins": wins,
            "seconds": _to_int(d.get("2nd")),
            "thirds": _to_int(d.get("3rd")),
            "fourths": _to_int(d.get("4th")),
            "total_rides": total,
            "win_pct": round(wins / total * 100, 2) if total else None,
        })
    return out


def parse_jockey_stats(html: str) -> list[dict]:
    return _parse_official_stats_table(html, "Jockey")


def parse_trainer_stats(html: str) -> list[dict]:
    return _parse_official_stats_table(html, "Trainer")


def _to_int(v):
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0


def fetch_raceresult_html(date: str, use_cache: bool = True) -> str:
    return _fetch_cached(RACERESULT_URL.format(date=date), f"btc_raceresult_{date}.html", use_cache)


def _fetch_cached(url: str, filename: str, use_cache: bool) -> str:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path = CACHE_DIR / filename
    if use_cache and path.exists():
        return path.read_text(encoding="utf-8", errors="ignore")
    html = _get(url)
    path.write_text(html, encoding="utf-8")
    time.sleep(1)
    return html


def _parse_class_info(text: str) -> dict:
    rmin = rmax = None
    m = RATED_RANGE_RE.search(text)
    if m:
        rmin, rmax = int(m.group(1)), int(m.group(2))
    else:
        m = RATED_ABOVE_RE.search(text)
        if m:
            rmin, rmax = int(m.group(1)), None
    grade = None
    gm = GRADE_RE.search(text)
    if gm:
        grade = f"Grade {gm.group(1)}"
    return {"class_code": grade, "rating_band_min": rmin, "rating_band_max": rmax, "class_raw": text}


def _race_headers(soup: BeautifulSoup):
    """Yield (race_no, header_container, class_text, distance_time_text) for each race."""
    for num_div in soup.find_all("div", class_="race-num"):
        container = num_div.parent.parent
        race_no = num_div.get_text(strip=True)
        title_el = container.find("h6")
        race_name = title_el.get_text(" ", strip=True) if title_el else ""
        class_el = container.find("span", class_="fs-14")
        class_text = class_el.get_text(" ", strip=True) if class_el else ""
        dt_el = container.find("div", class_="ms-auto")
        dist_time_text = dt_el.get_text(" ", strip=True) if dt_el else ""
        yield race_no, container, race_name, class_text, dist_time_text


def _parse_distance_time(text: str):
    dm = re.search(r"(\d{3,4})", text)
    distance_m = int(dm.group(1)) if dm else None
    tm = re.search(r"(\d{1,2}:\d{2}\s*[AP]M)", text, re.I)
    race_time = tm.group(1) if tm else None
    return distance_m, race_time


def _horse_cell(td) -> dict:
    spans = td.find_all("span")
    name = spans[0].get_text(strip=True) if len(spans) > 0 else td.get_text(" ", strip=True)
    breeding = spans[1].get_text(strip=True) if len(spans) > 1 else ""
    stud_foaled = spans[2].get_text(strip=True) if len(spans) > 2 else ""
    foaled = None
    fm = re.search(r"(\d{2}/\d{2}/\d{4})", stud_foaled)
    if fm:
        foaled = fm.group(1)
    # BTC's racecard only exposes "Stud", not a separate "Breeder" field
    # (unlike RWITC, which has both) -- stud name split off before the date.
    stud = stud_foaled.split(" - ")[0].strip() if " - " in stud_foaled else (stud_foaled or None)
    return {"name": name, "breeding": breeding, "foaled": foaled, "stud": stud}


def _to_float(v):
    if not v:
        return None
    v = v.strip().lstrip("-").strip()
    try:
        return float(v)
    except ValueError:
        return None


def parse_racecard(html: str) -> list[dict]:
    soup = BeautifulSoup(html, "lxml")
    races = []
    for race_no, container, race_name, class_text, dist_time_text in _race_headers(soup):
        distance_m, race_time = _parse_distance_time(dist_time_text)
        class_info = _parse_class_info(class_text or race_name)
        table = container.find_next("table", class_="card-table")
        runs = []
        if table:
            rows = table.find_all("tr")
            for row in rows[1:]:  # skip header row
                tds = row.find_all("td")
                if len(tds) < 10:
                    continue
                horse = _horse_cell(tds[2])
                weight_kg = _to_float(tds[3].get_text(strip=True))
                rating_txt = tds[4].get_text(strip=True)
                official_rating = int(rating_txt) if rating_txt.isdigit() else None
                owner = tds[5].get_text(" ", strip=True) or None
                jockey = tds[6].get_text(" ", strip=True) or None
                apprentice_allowance = _to_float(tds[7].get_text(strip=True)) if len(tds) > 7 else None
                trainer = tds[9].get_text(" ", strip=True) if len(tds) > 9 else None
                runs.append({
                    "horse_name": horse["name"],
                    "weight_kg": weight_kg,
                    "jockey": jockey,
                    "apprentice_allowance": apprentice_allowance,
                    "trainer": trainer or None,
                    "owner": owner,
                    "stud": horse["stud"],
                    "breeder": None,  # not exposed on BTC's racecard, unlike RWITC
                    "official_rating": official_rating,
                    "foaled": horse["foaled"],
                    "recent_runs": [],  # BTC's "Sh" column isn't numeric placings; left empty rather than guessed
                })
        races.append({
            "race_no": race_no,
            "race_name": race_name,
            "race_time": race_time,
            "distance_m": distance_m,
            **class_info,
            "runs": runs,
        })
    return races


def parse_raceresult(html: str) -> list[dict]:
    soup = BeautifulSoup(html, "lxml")
    races = []
    for race_no, container, race_name, class_text, dist_time_text in _race_headers(soup):
        distance_m, _ = _parse_distance_time(dist_time_text)
        class_info = _parse_class_info(class_text or race_name)
        table = container.find_next("table", class_="card-table")
        runners = []
        if table:
            rows = table.find_all("tr")
            for row in rows[1:]:
                tds = row.find_all("td")
                if len(tds) < 12:
                    continue
                name = tds[2].get_text(" ", strip=True)
                pl_txt = tds[10].get_text(strip=True)
                scratched = not pl_txt.isdigit()
                placing = int(pl_txt) if pl_txt.isdigit() else None
                runners.append({
                    "placing": placing,
                    "horse_name": name,
                    "card_no": tds[1].get_text(strip=True) or None,
                    "weight_kg": _to_float(tds[6].get_text(strip=True)),
                    "jockey": tds[9].get_text(" ", strip=True) or None,
                    "trainer": None,  # per-runner trainer isn't listed on the results table, only the winner's
                    "odds": tds[5].get_text(strip=True) or None,
                    "time": tds[11].get_text(strip=True) or None,
                    "scratched": scratched,
                })

        dividends = _parse_dividends(container)
        # BTC lists the favourite as a card number ("Tote Fav: 3"); resolve to the horse name
        fav_card = dividends.get("favourite")
        if fav_card:
            match = next((r for r in runners if r["card_no"] == fav_card), None)
            dividends["favourite"] = match["horse_name"] if match else None
        races.append({
            "race_no": race_no,
            "race_name": race_name,
            "distance_m": distance_m,
            **class_info,
            "runners": runners,
            "dividends": dividends,
        })
    return races


def _parse_dividends(container) -> dict:
    d = {"favourite": None, "win": None, "place": None, "shp": None, "for": None, "qnl": None, "tnl": None,
         "results_order": None, "margins": None}

    # "Tote Fav:" holds the favourite's CARD NUMBER (not a name); caller resolves
    # it against the runner list. (The "Winner:" block nearby is the race winner,
    # which is NOT the favourite -- conflating the two would make any
    # favourite-vs-model benchmark trivially and wrongly 100%.)
    fav_label = container.find_next(string=lambda s: s and s.strip() == "Tote Fav:")
    if fav_label:
        val_el = fav_label.find_parent().find_next_sibling()
        if val_el:
            d["favourite"] = val_el.get_text(strip=True) or None

    distance_label = container.find_next(string=lambda s: s and s.strip() == "Distance:")
    if distance_label:
        val_el = distance_label.find_parent().find_next_sibling()
        if val_el:
            d["margins"] = val_el.get_text(strip=True)

    wnp_header = container.find_next(string=lambda s: s and s.strip() == "WNP")
    if wnp_header:
        div_table = wnp_header.find_parent("table")
        rows = div_table.find_all("tr")
        if len(rows) >= 2:
            headers = [c.get_text(strip=True) for c in rows[0].find_all(["th", "td"])]
            values = [c.get_text(strip=True) for c in rows[1].find_all(["th", "td"])]
            kv = dict(zip(headers, values))
            d["win"] = kv.get("WNP")
            d["place"] = kv.get("PLP")
            d["shp"] = kv.get("SHP")
            d["for"] = kv.get("FRP")
            d["qnl"] = kv.get("QNP")
            d["tnl"] = kv.get("TNP")
    return d
