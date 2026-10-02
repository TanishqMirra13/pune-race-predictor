"""Scraper/parser for RWITC's race card and race result pages.

Data source notes (verified live, Oct 2026):
- Race card:   https://www.rwitc.com/rwitc_website_api/Racecard_get_api.php
                   ?date=YYYY-MM-DD&type=raceCard&race_type=pre_race
- Race result: https://www.rwitc.com/rwitc_website_api/raceResults_post_race_get_api.php
                   ?date=YYYY-MM-DD&type=raceResults&race_type=post_race
The club relaunched its site as a JavaScript front end some time between Aug
and Oct 2026. The old erp_racecard.php / erp_raceresult.php pages now answer
with an empty shell or a 404 for every date, including ones they used to
serve, so a fetch against them reads as "no card published" on a race day.
The data moved behind a JSON API that wraps the SAME legacy HTML in an
envelope -- {"success": .., "data": {"html": ..}} -- so only the fetch changed
and the parsers below are untouched. A date with no meeting comes back as a
404 carrying {"success": false}.

That HTML is legacy nested tables with no useful CSS hooks for most fields,
but the text (extracted line-by-line via BeautifulSoup) follows a very
consistent field order per horse/runner, which is what these parsers key off.
"""
import re
import time
from pathlib import Path

import requests
from bs4 import BeautifulSoup

USER_AGENT = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
API_BASE = "https://www.rwitc.com/rwitc_website_api/"
RACECARD_URL = API_BASE + "Racecard_get_api.php?date={date}&type=raceCard&race_type=pre_race"
RACERESULT_URL = (API_BASE + "raceResults_post_race_get_api.php"
                  "?date={date}&type=raceResults&race_type=post_race")
JOCKEY_STATS_URL = API_BASE + "jockey_statistics_get_api.php"
TRAINER_STATS_URL = API_BASE + "trainer_statistics_get_api.php"
# Still a plain page on the old site -- the one route the relaunch left alone.
MONEY_LEADERS_URL = "https://rwitc.com/new/moneyLeaders.php"

# What a "nothing for this date" answer is turned into. Not an empty string:
# parse_raceresult falls back to soup.body, which an empty document lacks.
EMPTY_PAGE = "<html><body></body></html>"

# Mumbai and Pune are one club on one feed, so a page says which course it is
# for only in its heading: "PUNE MEETING 2026, NINTH DAY, ..." or
# "MUMBAI MEETING 2025/26, FOURTH DAY, ...".
MEETING_RE = re.compile(r"\b(PUNE|MUMBAI)\s+MEETING\b", re.I)

CACHE_DIR = Path(__file__).resolve().parent.parent / "data" / "cache"

DATE_RE = re.compile(r"^\d{2}-\d{2}-\d{4}$")
HORSE_IDX_RE = re.compile(r"^\d+\.$")
WEIGHT_RE = re.compile(r"^(\d+(?:\.\d+)?)\.?\s*kg$", re.I)
CLASS_RE = re.compile(r"Class\s+([IVX]+)")
RATED_RANGE_RE = re.compile(r"rated\s+(\d+)\s+to\s+(\d+)", re.I)
RATED_UPWARD_RE = re.compile(r"rated\s+(\d+)\s+and\s+upward", re.I)
RATING_RE = re.compile(r"Rating:\s*(-{1,2}|\d+)")
# RWITC prints the current running rating alongside a periodic reassessment,
# e.g. "Rating: 31 (HRA 42 on 19/10/2025)" -- a gap here (assessed well above
# or below the current mark) is real signal a rating-only model can't see.
# "HRA" isn't spelled out anywhere on the site; the field is stored under
# that name because that's literally what's printed, not because the
# abbreviation is confidently decoded.
HRA_RE = re.compile(r"HRA\s+(\d+)\s+on\s+(\d{2}/\d{2}/\d{4})")
AGE_SEX_RE = re.compile(r"\d+\s*yrs\.?$", re.I)
# Equipment line, e.g. "[9] (TS)(CNB)(A)AFHH-": a run of parenthesized codes
# (TS/CNB/BLK/VISOR/HOOD/EP/A/...) followed by an un-parenthesized letter
# cluster whose meaning isn't documented anywhere RWITC publishes. The coded
# part is parsed into equipment_codes; the letter cluster is kept only in
# equipment_raw rather than guessed at.
EQUIPMENT_LINE_RE = re.compile(r"^\[\d+\]\s*((?:\([A-Z]+\))+)")
EQUIPMENT_CODE_RE = re.compile(r"\(([A-Z]+)\)")


def _get(url: str) -> str:
    resp = requests.get(url, headers={"User-Agent": USER_AGENT}, timeout=30)
    if not url.startswith(API_BASE):
        resp.raise_for_status()
        return resp.text
    # The API answers a date it has nothing for with a 404 whose body is still
    # the JSON envelope, so the status alone cannot tell "no meeting" from
    # "the endpoint has moved again". Read the envelope first.
    try:
        payload = resp.json()
    except ValueError:
        resp.raise_for_status()
        raise ValueError(f"RWITC API returned something that is not JSON for {url}")
    data = payload.get("data") or {}
    if payload.get("success") and data.get("html"):
        return data["html"]
    if resp.status_code in (200, 404):
        return EMPTY_PAGE
    resp.raise_for_status()
    return EMPTY_PAGE


def fetch_racecard_html(date: str, use_cache: bool = True) -> str:
    return _fetch_cached(RACECARD_URL.format(date=date), f"racecard_{date}.html", use_cache)


def fetch_raceresult_html(date: str, use_cache: bool = True) -> str:
    return _fetch_cached(RACERESULT_URL.format(date=date), f"raceresult_{date}.html", use_cache)


def fetch_jockey_stats_html(use_cache: bool = True) -> str:
    return _fetch_cached(JOCKEY_STATS_URL, "jockey_stats.html", use_cache)


def fetch_trainer_stats_html(use_cache: bool = True) -> str:
    return _fetch_cached(TRAINER_STATS_URL, "trainer_stats.html", use_cache)


def _parse_official_stats_table(html: str, name_key: str, total_key: str) -> list[dict]:
    """Shared parser for RWITC's jockeyStatistics.php / trainerStatistics.php --
    same table shape (NAME, WINS, SECOND, THIRD, FOURTH, TOTAL ..., WIN %),
    just a different first/second column header per page."""
    soup = BeautifulSoup(html, "lxml")
    table = soup.find("table")
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
        name = d.get(name_key)
        if not name:
            continue
        out.append({
            "name": name,
            "wins": _to_int(d.get("WINS")),
            "seconds": _to_int(d.get("SECOND")),
            "thirds": _to_int(d.get("THIRD")),
            "fourths": _to_int(d.get("FOURTH")),
            "total_rides": _to_int(d.get(total_key)),
            "win_pct": _to_float_stat(d.get("WIN %")),
        })
    return out


def parse_jockey_stats(html: str) -> list[dict]:
    return _parse_official_stats_table(html, "JOCKEY", "TOTAL MOUNTS")


def parse_trainer_stats(html: str) -> list[dict]:
    return _parse_official_stats_table(html, "TRAINER", "TOTAL RUNNERS")


def fetch_money_leaders_html(use_cache: bool = True) -> str:
    return _fetch_cached(MONEY_LEADERS_URL, "money_leaders.html", use_cache)


def parse_money_leaders(html: str) -> dict[str, list[dict]]:
    """moneyLeaders.php has four tables in a fixed order: Owners, Jockeys,
    Horses, Trainers -- each (Name, Starts, Wins, Second, Third, Winnings).
    This is a season money-leaders reference, not filtered by venue and not
    fed into scoring (unlike jockey/trainer strike rates); shown as-is for
    the user's own read on who the established, well-resourced operations
    are this season."""
    soup = BeautifulSoup(html, "lxml")
    tables = soup.find_all("table", class_="leadersTable")
    if len(tables) < 4:
        return {"owners": [], "jockeys": [], "horses": [], "trainers": []}

    def parse_one(table) -> list[dict]:
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
            out.append({
                "name": d.get("NAME"),
                "starts": _to_int(d.get("STARTS")),
                "wins": _to_int(d.get("WINS")),
                "seconds": _to_int(d.get("SECOND")),
                "thirds": _to_int(d.get("THIRD")),
                "winnings": d.get("WINNINGS"),
            })
        return out

    return {
        "owners": parse_one(tables[0]),
        "jockeys": parse_one(tables[1]),
        "horses": parse_one(tables[2]),
        "trainers": parse_one(tables[3]),
    }


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


def _fetch_cached(url: str, filename: str, use_cache: bool) -> str:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path = CACHE_DIR / filename
    if use_cache and path.exists():
        return path.read_text(encoding="utf-8", errors="ignore")
    html = _get(url)
    # An empty answer is not cached: a card fetched before the club has
    # published it would otherwise read as "no card" for the rest of the day.
    if html != EMPTY_PAGE:
        path.write_text(html, encoding="utf-8")
    time.sleep(1)  # be polite to a small club server
    return html


def _lines_from(node) -> list[str]:
    text = node.get_text("\n", strip=True)
    return [l for l in text.split("\n") if l.strip()]


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


def _parse_recent_runs(lines: list[str], start: int, end: int) -> list[dict]:
    """lines[start:end] is the tail of a horse block after the 'View Runs' header row."""
    runs = []
    i = start
    while i < end:
        if DATE_RE.match(lines[i]) and i + 7 < end:
            date, raceno, jockey, cls, dist, wt, placing, tm = lines[i:i + 8]
            runs.append({
                "date": date, "race_no": raceno, "jockey": jockey, "class": cls,
                "distance": dist, "weight": wt, "placing": placing, "time": tm,
            })
            i += 8
        else:
            i += 1
    return runs


def parse_racecard(html: str) -> list[dict]:
    soup = BeautifulSoup(html, "lxml")
    race_tables = soup.select("table.race_no_data")
    races = []
    for rt in race_tables:
        lines = _lines_from(rt)
        if not lines or not lines[0].startswith("No.:"):
            continue
        race_no = lines[0].split(":", 1)[1].strip()
        race_name = lines[1] if len(lines) > 1 else ""
        class_line_idx = 2
        class_info = _parse_class_info(lines[class_line_idx]) if len(lines) > class_line_idx else {}

        time_idx = next((i for i, l in enumerate(lines) if l.startswith("Time:")), None)
        dist_idx = next((i for i, l in enumerate(lines) if l.startswith("(About)")), None)
        race_time = lines[time_idx].replace("Time:", "").strip() if time_idx is not None else None
        distance_m = None
        if dist_idx is not None:
            dm = re.search(r"(\d+)", lines[dist_idx])
            if dm:
                distance_m = int(dm.group(1))

        # Find where the first horse entry starts (a bare "N." line) after the header
        horse_start_idxs = [i for i, l in enumerate(lines) if HORSE_IDX_RE.match(l)]
        runs = []
        for pos, idx in enumerate(horse_start_idxs):
            block_end = horse_start_idxs[pos + 1] if pos + 1 < len(horse_start_idxs) else len(lines)
            block = lines[idx:block_end]
            run = _parse_horse_block(block)
            if run:
                runs.append(run)

        races.append({
            "race_no": race_no,
            "race_name": race_name,
            "race_time": race_time,
            "distance_m": distance_m,
            **class_info,
            "runs": runs,
        })
    return races


def _parse_horse_block(block: list[str]) -> dict | None:
    # block[0] = "N.", block[1] = NAME, block[2] = weight, block[3] = jockey
    if len(block) < 4:
        return None
    name = block[1]
    wm = WEIGHT_RE.match(block[2].replace(" kg", "kg").replace(" ", "") if False else block[2])
    weight_kg = None
    wm = WEIGHT_RE.match(block[2])
    if wm:
        weight_kg = float(wm.group(1))
    jockey_raw = block[3]
    apprentice_allowance = None
    am = re.search(r"-(\d+(?:\.\d+)?)\s*$", jockey_raw)
    jockey = jockey_raw
    if am:
        apprentice_allowance = float(am.group(1))
        jockey = jockey_raw[:am.start()].strip()

    rating = None
    assessed_rating = None
    assessed_rating_date = None
    trainer = None
    foaled = None
    owner = None
    stud = None
    breeder = None
    stud_idx = None
    for i, l in enumerate(block):
        if l.startswith("Rating:"):
            rm = RATING_RE.search(l)
            if rm and rm.group(1).isdigit():
                rating = int(rm.group(1))
            hm = HRA_RE.search(l)
            if hm:
                assessed_rating = int(hm.group(1))
                assessed_rating_date = hm.group(2)
        elif l.startswith("Foaled:"):
            foaled = l.replace("Foaled:", "").strip()
        elif l.startswith("Stud:"):
            stud = l.replace("Stud:", "").strip() or None
            stud_idx = i
        elif l.startswith("Breeder:"):
            breeder = l.replace("Breeder:", "").strip() or None
        elif AGE_SEX_RE.search(l):
            # trainer is the next line, in parens; owner is the previous line (ends "'s")
            if i + 1 < len(block) and block[i + 1].startswith("(") and block[i + 1].endswith(")"):
                trainer = block[i + 1][1:-1]
            if i - 1 >= 0:
                owner = re.sub(r"'s$", "", block[i - 1]).strip()

    # Sire/dam/equipment have no distinctive prefix to search for like
    # "Stud:" does, but they consistently sit immediately before it (verified
    # across ~50 horses on live racecards, Aug 2026): sire, dam, equipment,
    # then "Stud:". Anchoring on stud_idx survives the jockey/name fields
    # varying in length instead of hard-coding absolute positions.
    sire = dam = equipment_raw = None
    equipment_codes: list[str] = []
    if stud_idx is not None and stud_idx >= 3:
        sire_line, dam_line, equip_line = block[stud_idx - 3], block[stud_idx - 2], block[stud_idx - 1]
        if sire_line.endswith("-"):
            sire = sire_line[:-1].strip() or None
            dam = dam_line.strip() or None
        em = EQUIPMENT_LINE_RE.match(equip_line)
        if em:
            equipment_raw = equip_line
            equipment_codes = EQUIPMENT_CODE_RE.findall(em.group(1))

    view_runs_idx = next((i for i, l in enumerate(block) if l == "View Runs"), None)
    recent_runs = []
    if view_runs_idx is not None:
        header_end = view_runs_idx + 1
        while header_end < len(block) and block[header_end] in (
            "DATE", "RACENO", "JOCKEY", "CLASS", "DISTANCE", "WEIGHT", "PLACING", "TIME", "VIDEO"
        ):
            header_end += 1
        recent_runs = _parse_recent_runs(block, header_end, len(block))

    return {
        "horse_name": name,
        "weight_kg": weight_kg,
        "jockey": jockey,
        "apprentice_allowance": apprentice_allowance,
        "trainer": trainer,
        "owner": owner,
        "stud": stud,
        "breeder": breeder,
        "sire": sire,
        "dam": dam,
        "equipment_raw": equipment_raw,
        "equipment_codes": equipment_codes,
        "official_rating": rating,
        "assessed_rating": assessed_rating,
        "assessed_rating_date": assessed_rating_date,
        "foaled": foaled,
        "recent_runs": recent_runs,
    }


PLACING_RE = re.compile(r"^(\d+|WDRN|WD)$", re.I)


def parse_raceresult(html: str) -> list[dict]:
    soup = BeautifulSoup(html, "lxml")
    body = soup.find("div", id="leftArea") or soup.body
    lines = _lines_from(body)

    header_idxs = [i for i, l in enumerate(lines) if l.startswith("No.:")]
    races = []
    for pos, idx in enumerate(header_idxs):
        end = header_idxs[pos + 1] if pos + 1 < len(header_idxs) else len(lines)
        block = lines[idx:end]
        race = _parse_result_block(block)
        if race:
            races.append(race)
    return races


def _parse_result_block(block: list[str]) -> dict | None:
    race_no = block[0].split(":", 1)[1].strip()
    race_name = block[1] if len(block) > 1 else ""
    class_info = _parse_class_info(block[2]) if len(block) > 2 else {}

    time_idx = next((i for i, l in enumerate(block) if l.startswith("Time:")), None)
    dist_idx = next((i for i, l in enumerate(block) if l.startswith("(About)")), None)
    distance_m = None
    if dist_idx is not None:
        dm = re.search(r"(\d+)", block[dist_idx])
        if dm:
            distance_m = int(dm.group(1))

    header_row_idx = next((i for i, l in enumerate(block) if l == "Placing"), None)
    trailer_idx = next((i for i, l in enumerate(block) if l == "Ownership"), len(block))

    runners = []
    if header_row_idx is not None:
        # skip "Placing Horse Wt Jockey Trainer Odds Time" header tokens
        i = header_row_idx
        while i < trailer_idx and block[i] in (
            "Placing", "Horse", "Wt", "Jockey", "Trainer", "Odds", "Time", "Horse Wt"
        ):
            i += 1
        while i < trailer_idx:
            if PLACING_RE.match(block[i]):
                runner, next_i = _parse_runner_row(block, i, trailer_idx)
                if runner:
                    runners.append(runner)
                i = next_i
            else:
                i += 1

    dividends = _parse_dividends(block, trailer_idx)

    return {
        "race_no": race_no,
        "race_name": race_name,
        "distance_m": distance_m,
        **class_info,
        "runners": runners,
        "dividends": dividends,
    }


def _parse_runner_row(block: list[str], start: int, limit: int):
    """Parse one finisher row starting at the placing token. Returns (dict, next_index)."""
    placing = block[start]
    i = start + 1
    if i >= limit:
        return None, i
    name = block[i]
    i += 1
    # sire/dam span 1-3 lines wrapped in ( ... - ... )
    sire_dam_parts = []
    while i < limit and not re.match(r"^\d+(\.\d+)?$", block[i]):
        sire_dam_parts.append(block[i])
        i += 1
        if len(sire_dam_parts) > 4:
            break
    if i >= limit:
        return None, i
    weight_kg = float(block[i]) if re.match(r"^\d+(\.\d+)?$", block[i]) else None
    i += 1
    if placing.upper() in ("WDRN", "WD"):
        # withdrawn horses: no jockey/time/odds recorded the same way
        return {
            "placing": None, "horse_name": name, "weight_kg": weight_kg,
            "jockey": None, "trainer": None, "odds": None, "time": None,
            "scratched": True,
        }, i
    jockey = block[i] if i < limit else None
    i += 1
    trainer = block[i] if i < limit else None
    i += 1
    odds = block[i] if i < limit else None
    i += 1
    finish_time = block[i] if i < limit else None
    i += 1
    if i < limit and re.match(r"^\d+$", block[i]):
        i += 1  # horse body-weight field, not carried weight; skip
    return {
        "placing": int(placing) if placing.isdigit() else None,
        "horse_name": name,
        "weight_kg": weight_kg,
        "jockey": jockey,
        "trainer": trainer,
        "odds": odds,
        "time": finish_time,
        "scratched": False,
    }, i


def _parse_dividends(block: list[str], start: int) -> dict:
    d = {"favourite": None, "win": None, "place": None, "shp": None, "for": None, "qnl": None, "tnl": None,
         "results_order": None, "margins": None}
    i = start
    while i < len(block):
        l = block[i]
        if l == "Tote Favourite" and i + 1 < len(block):
            d["favourite"] = block[i + 1]
        elif l == "Results as per Card Nos" and i + 1 < len(block):
            d["results_order"] = block[i + 1]
        elif l == "Distance" and i + 1 < len(block):
            d["margins"] = block[i + 1]
        elif l == "WIN :" and i + 1 < len(block):
            d["win"] = block[i + 1]
        elif l == "PLACE :" and i + 1 < len(block):
            d["place"] = block[i + 1]
        elif l == "SHP :" and i + 1 < len(block):
            d["shp"] = block[i + 1]
        elif l == "FOR :" and i + 1 < len(block):
            d["for"] = block[i + 1]
        elif l == "QNL :" and i + 1 < len(block):
            d["qnl"] = block[i + 1]
        elif l == "TNL :" and i + 1 < len(block):
            d["tnl"] = block[i + 1]
        i += 1
    return d


# Multi-leg pools RWITC settles at the foot of a result page. Each sits in its
# own little table whose first <th> is the pool name and whose rows are
# label/value pairs -- Legs, Winners, then either a dividend and ticket count
# or a Carried Forward amount.
POOL_HEADING_RE = re.compile(
    r"^(SUPER JACKPOT|MINI JACKPOT|JACKPOT|FIRST TREBLE|SECOND TREBLE|THIRD TREBLE|TREBLE)$",
    re.I)
# The jackpot pays in two tiers: 70% of the pool to tickets with all legs
# right, 30% to those one leg short. They are different bets with wildly
# different dividends, so they are stored as separate rows rather than
# averaged into one meaningless number.
TIER_RE = re.compile(r"^(\d+)%\s*Div$", re.I)


def parse_pool_dividends(html: str) -> list[dict]:
    """Jackpot / treble settlements from an RWITC result page.

    Returns [{'pool', 'tier', 'legs', 'winners', 'dividend', 'tickets',
    'carried_forward'}]. Two fields here exist nowhere else in the pipeline:

    'legs' is which races actually made up the pool. The jackpot is usually
    the last five races and the trebles consecutive triples, but the club
    chooses per meeting -- on 24 Jul 2026 the Super Jackpot ran races 4-9 and
    on 8 Aug 2026 races 3-8 -- so a planner that assumes the shape is planning
    the wrong bet on some days.

    'tickets' is how many tickets shared the dividend, which is the number
    that makes a pari-mutuel pool honest. A dividend is not a price; it is the
    pool split among whoever held that line. Recorded together they say what a
    winning ticket really returned: 84,160 shared by 70 tickets on 8 Aug 2026,
    52 shared by 3,781 on a day when the favourites all obliged.

    Returns [] on a page without these tables, which is the normal case for a
    card too small to carry a jackpot -- not an error.
    """
    soup = BeautifulSoup(html, "lxml")
    out = []
    for table in soup.find_all("table"):
        head = table.find("th")
        if not head:
            continue
        name = head.get_text(" ", strip=True)
        if not POOL_HEADING_RE.match(name):
            continue
        fields: dict = {}
        for tr in table.find_all("tr")[1:]:
            cells = [c.get_text(" ", strip=True) for c in tr.find_all(["th", "td"])]
            # Rows are label/value, and the dividend row carries two pairs
            # ("70% Div | 84,160 | Tickets | 70") on one line.
            for j in range(0, len(cells) - 1, 2):
                if cells[j]:
                    fields[cells[j]] = cells[j + 1]
        legs = fields.get("Legs")
        winners = fields.get("Winners")
        carried = _money(fields.get("Carried Forward"))
        tiers = [(TIER_RE.match(k).group(1) + "%", v)
                 for k, v in fields.items() if TIER_RE.match(k)]
        if tiers:
            # RWITC prints both tiers' ticket counts under the same "Tickets"
            # label, so a dict keeps only the last. Re-read them positionally
            # from the rows instead of guessing.
            counts = _tier_ticket_counts(table)
            for i, (tier, value) in enumerate(tiers):
                out.append({"pool": name.upper(), "tier": tier, "legs": legs,
                            "winners": winners, "dividend": _money(value),
                            "tickets": counts[i] if i < len(counts) else None,
                            "carried_forward": None})
        else:
            out.append({"pool": name.upper(), "tier": "main", "legs": legs,
                        "winners": winners, "dividend": _money(fields.get("Div")),
                        "tickets": _int(fields.get("Tickets")),
                        "carried_forward": carried})
    return out


def _tier_ticket_counts(table) -> list[int | None]:
    counts = []
    for tr in table.find_all("tr"):
        cells = [c.get_text(" ", strip=True) for c in tr.find_all(["th", "td"])]
        if any(TIER_RE.match(c) for c in cells):
            counts.append(next((_int(cells[i + 1]) for i, c in enumerate(cells[:-1])
                                if c.lower() == "tickets"), None))
    return counts


def _money(text: str | None) -> float | None:
    if not text:
        return None
    m = re.search(r"[\d,]+(?:\.\d+)?", text)
    return float(m.group(0).replace(",", "")) if m else None


def _int(text: str | None) -> int | None:
    v = _money(text)
    return int(v) if v is not None else None


def parse_standard_timings(pdf_path: Path) -> dict:
    """Parse RWITC's standard_timings.pdf into {track: {class: {distance_m: seconds}}}."""
    import pdfplumber

    with pdfplumber.open(pdf_path) as pdf:
        text = pdf.pages[0].extract_text()
    lines = [l for l in text.split("\n") if l.strip()]

    result = {}
    current_track = None
    distances = []
    for line in lines:
        if "MAIN TRACK" in line.upper():
            current_track = "main"
            continue
        if "MONSOON TRACK" in line.upper():
            current_track = "monsoon"
            continue
        m = re.match(r"^((?:\d+\s*M\s*)+)$", line.strip())
        if m:
            distances = [int(d) for d in re.findall(r"(\d+)\s*M", line)]
            result.setdefault(current_track, {})
            continue
        parts = line.split()
        if not parts or current_track is None or not distances:
            continue
        if parts[0] == "Class" and len(parts) > 1 and re.match(r"^[IVX]+$", parts[1]):
            class_label = f"Class {parts[1]}"
            values = parts[2:]
        elif parts[0] == "3" and len(parts) > 1 and parts[1].lower() == "yo":
            class_label = "3yo"
            values = parts[2:]
        else:
            class_label = parts[0]
            values = parts[1:]
        times = {}
        for dist, val in zip(distances, values):
            secs = _time_to_seconds(val)
            if secs is not None:
                times[dist] = secs
        if times:
            result.setdefault(current_track, {})[class_label] = times
    return result


def _time_to_seconds(val: str):
    val = val.replace("-", ".")
    if val == "-":
        return None
    m = re.match(r"^(?:(\d+)\.)?(\d+)\.(\d+)$", val)
    if not m:
        return None
    minutes = int(m.group(1)) if m.group(1) else 0
    seconds = int(m.group(2))
    hundredths = int(m.group(3))
    return minutes * 60 + seconds + hundredths / 100


def meeting_venue(html: str) -> str | None:
    """Which RWITC course a card or result page is for, or None if it does not
    say (a pasted fragment with the heading cut off, say)."""
    m = MEETING_RE.search(html or "")
    return m.group(1).title() if m else None


class ForVenue:
    """Binds one of RWITC's two courses to this module, for the same
    {venue: scraper} dispatch tables indiarace_cards.ForVenue serves.

    The club publishes one page per date, for whichever course is racing, and
    the URL does not name it. Asked for Mumbai on a Pune day, the module-level
    parsers would hand back the Pune card and it would be stored a second time
    under Mumbai -- every race counted twice in the backtest. So a page whose
    heading names the other course parses as empty here, which is what "no
    Mumbai card today" actually means. A page that names no course is let
    through rather than dropped.

    Fetching and the season statistics are club-wide and pass straight through.
    """

    def __init__(self, venue: str):
        if venue not in ("Pune", "Mumbai"):
            raise ValueError(f"RWITC races at Pune and Mumbai, not {venue!r}")
        self.venue = venue

    def _mine(self, html: str) -> bool:
        return meeting_venue(html) in (None, self.venue)

    def parse_racecard(self, html: str) -> list[dict]:
        return parse_racecard(html) if self._mine(html) else []

    def parse_raceresult(self, html: str) -> list[dict]:
        return parse_raceresult(html) if self._mine(html) else []

    def parse_pool_dividends(self, html: str) -> list[dict]:
        return parse_pool_dividends(html) if self._mine(html) else []

    def __getattr__(self, name: str):
        try:
            return globals()[name]
        except KeyError:
            raise AttributeError(name) from None
