"""Richer form/context signals: distance & class-aware form, weight,
equipment change, course-and-distance record, and sire strike rate.

Why this module exists: the original engine scored form from a bare placings
string ("2,1,4"), which throws away the two things that decide what a placing
is actually worth -- WHAT distance it was over and WHO it was against. A 2nd
over 1200m in Class I is a far better run than a 2nd over 2400m in Class V,
and the old scorer counted them identically. db/ingest.py now preserves the
full per-run detail as runs.recent_runs_json, which is what this module reads.

SOURCE RICHNESS VARIES, and every function here degrades rather than guesses:
- RWITC (Pune/Mumbai): full detail -- date, class, distance, weight, placing, time.
- indiarace-sourced (Hyderabad/Mysore/Kolkata/Delhi): placings only.
- BTC (Bangalore): no numeric form at all (its "Sh" column is letter codes).
So distance/class-aware scoring only actually engages for RWITC venues today;
elsewhere these functions return the neutral value and the engine falls back
to the plain placings score. That is deliberate -- a venue whose source lacks
the data should score as "no information", never as "bad".
"""
import json
import re
import sqlite3
from datetime import date

# A placing is worth this fraction of "a win" before context adjustments.
PLACING_POINTS = {1: 5, 2: 3, 3: 2, 4: 1}
RECENCY_WEIGHTS = [0.4, 0.3, 0.2, 0.1]

# Distance is treated as "same trip" within this tolerance. Indian cards are
# set in 100m steps and a 1200 vs 1400 is a genuinely different test, so the
# window is deliberately tight.
DISTANCE_TOLERANCE_M = 100

DIST_RE = re.compile(r"(\d{3,4})")
DATE_DMY_RE = re.compile(r"^(\d{2})-(\d{2})-(\d{4})$")

# RWITC prints a run's class as a rating band ("20-46") rather than a class
# numeral, sometimes with an age qualifier ("20-46 4Y"). The band's midpoint
# is a usable proxy for "how good was this company".
BAND_RE = re.compile(r"^(\d+)\s*-\s*(\d+)")

# Equipment codes that represent a genuine gear change worth flagging.
# Deliberately conservative: only headgear whose first-time application is a
# widely-recognised angle. (A)/(TS) appear on nearly every runner and carry
# no differentiating information, so they're excluded.
NOTABLE_EQUIPMENT = {"BLK", "VISOR", "HOOD", "EP", "CNB"}


def parse_recent_runs(raw: str | None) -> list[dict]:
    if not raw:
        return []
    try:
        runs = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return []
    return runs if isinstance(runs, list) else []


def _run_distance_m(run: dict) -> int | None:
    m = DIST_RE.search(str(run.get("distance") or ""))
    return int(m.group(1)) if m else None


def _run_band_midpoint(run: dict) -> float | None:
    m = BAND_RE.match(str(run.get("class") or "").strip())
    if not m:
        return None
    lo, hi = int(m.group(1)), int(m.group(2))
    return (lo + hi) / 2


def _run_date(run: dict) -> date | None:
    m = DATE_DMY_RE.match(str(run.get("date") or "").strip())
    if not m:
        return None
    try:
        return date(int(m.group(3)), int(m.group(2)), int(m.group(1)))
    except ValueError:
        return None


def _placing_int(run: dict) -> int | None:
    p = str(run.get("placing") or "").strip()
    return int(p) if p.isdigit() else None


def contextual_form_score(recent_runs: list[dict], today_distance_m: int | None,
                          today_band_mid: float | None) -> tuple[float, int, bool]:
    """Recency-weighted form in [0,1], but each run's value is scaled by how
    relevant it is to today's test.

    Returns (score, usable_run_count, used_context) -- used_context is False
    when the source gave us placings only, so the caller knows this is the
    same information the plain scorer had rather than a richer signal.
    """
    if not recent_runs:
        return 0.0, 0, False

    total = weight_sum = 0.0
    usable = 0
    used_context = False

    for i, run in enumerate(recent_runs[:len(RECENCY_WEIGHTS)]):
        w = RECENCY_WEIGHTS[i]
        weight_sum += w
        placing = _placing_int(run)
        if placing is None:
            continue
        usable += 1
        base = PLACING_POINTS.get(placing, 0) / 5.0

        # Distance relevance: a run over today's trip counts fully; a run at a
        # very different trip is discounted but never zeroed (a good horse is
        # a good horse). Scale bottoms out at 0.6 for a 800m+ difference.
        run_dist = _run_distance_m(run)
        if run_dist is not None and today_distance_m:
            used_context = True
            gap = abs(run_dist - today_distance_m)
            if gap > DISTANCE_TOLERANCE_M:
                base *= max(0.6, 1.0 - (gap - DISTANCE_TOLERANCE_M) / 1750.0)

        # Class relevance: beating better company (a higher rating band than
        # today's) is worth more; a placing earned in much weaker company is
        # worth less. +/-25% at the extremes.
        run_band = _run_band_midpoint(run)
        if run_band is not None and today_band_mid:
            used_context = True
            delta = run_band - today_band_mid
            base *= max(0.75, min(1.25, 1.0 + delta / 100.0))

        total += w * base

    if weight_sum == 0:
        return 0.0, 0, used_context
    return min(1.0, total / weight_sum), usable, used_context


def days_since_last_run(recent_runs: list[dict], race_date: str | None) -> int | None:
    """Days between today's race and the most recent run with a parseable
    date. None when the source doesn't carry dates (indiarace/BTC)."""
    if not race_date:
        return None
    try:
        today = date.fromisoformat(race_date)
    except (ValueError, TypeError):
        return None
    for run in recent_runs:
        d = _run_date(run)
        if d:
            delta = (today - d).days
            return delta if delta >= 0 else None
    return None


def freshness_score(days: int | None) -> float:
    """[0,1], peaking in the 14-45 day window that is the normal, healthy
    turnaround for Indian racing. Both a horse backing up in under a week and
    one returning from a long layoff carry real risk; the curve is gentle
    because both also win regularly. Neutral 0.5 when unknown."""
    if days is None:
        return 0.5
    if days < 7:
        return 0.35
    if days < 14:
        return 0.6
    if days <= 45:
        return 1.0
    if days <= 90:
        return 0.7
    if days <= 180:
        return 0.5
    return 0.35


def weight_score(weight_kg: float | None, allowance: float | None,
                 field_weights: list[float]) -> float:
    """[0,1] where 1 = lightest effective weight in the field.

    Effective weight nets off an apprentice's claim, which is the number that
    actually gets carried -- previously parsed but never stored or used.
    Weight only means something relative to today's field, so this is
    normalised within the race rather than against an absolute scale."""
    if weight_kg is None or not field_weights:
        return 0.5
    effective = weight_kg - (allowance or 0.0)
    lo, hi = min(field_weights), max(field_weights)
    if hi == lo:
        return 0.5
    return 1.0 - (effective - lo) / (hi - lo)


def effective_weight(weight_kg: float | None, allowance: float | None) -> float | None:
    if weight_kg is None:
        return None
    return weight_kg - (allowance or 0.0)


def equipment_change_flag(conn: sqlite3.Connection, horse_id: int, race_id: int,
                          equipment_codes: str | None) -> tuple[bool, str | None]:
    """True when a notable piece of headgear appears today that this horse's
    most recent previous run didn't have -- the classic 'first-time blinkers'
    angle. Compares against the previous race card we hold for this horse, so
    it only fires once we have that horse's history; a horse we're seeing for
    the first time is not treated as a gear change."""
    today = {c for c in (equipment_codes or "").split(",") if c} & NOTABLE_EQUIPMENT
    if not today:
        return False, None
    prev = conn.execute(
        """SELECT r.equipment_codes FROM runs r
           JOIN races ra ON ra.id = r.race_id
           JOIN races today ON today.id = ?
           WHERE r.horse_id = ? AND r.race_id != ? AND ra.race_date < today.race_date
           ORDER BY ra.race_date DESC LIMIT 1""",
        (race_id, horse_id, race_id),
    ).fetchone()
    if not prev:
        return False, None
    previous = {c for c in (prev["equipment_codes"] or "").split(",") if c} & NOTABLE_EQUIPMENT
    added = today - previous
    if not added:
        return False, None
    return True, "+".join(sorted(added))


def course_distance_record(conn: sqlite3.Connection, horse_id: int, venue: str,
                           distance_m: int | None, as_of_date: str | None,
                           exclude_race_id: int | None = None) -> tuple[int, int]:
    """(wins, starts) for this horse at this venue over this trip, from our own
    results archive.

    Two independent guards, because they cover different cases:
    - as_of_date (backtest): excludes everything from the race date onward.
    - exclude_race_id (always): drops the race being scored itself. Needed
      because live scoring passes as_of_date=None, and reviewing an already-run
      race in the app would otherwise count that race's own result in its
      "course & distance" record -- reading C&D 1/1 off the very race you're
      looking at. Harmless for genuinely upcoming races (no result row yet),
      but wrong on screen and confusing, so it's excluded unconditionally."""
    if not distance_m:
        return 0, 0
    q = """SELECT COUNT(*) starts, SUM(CASE WHEN res.finish_position = 1 THEN 1 ELSE 0 END) wins
           FROM runs r JOIN races ra ON ra.id = r.race_id
           JOIN results res ON res.run_id = r.id
           WHERE r.horse_id = ? AND ra.venue = ? AND r.scratched = 0
             AND ra.distance_m BETWEEN ? AND ?"""
    params = [horse_id, venue, distance_m - DISTANCE_TOLERANCE_M, distance_m + DISTANCE_TOLERANCE_M]
    if exclude_race_id is not None:
        q += " AND ra.id != ?"
        params.append(exclude_race_id)
    if as_of_date is not None:
        q += " AND ra.race_date < ?"
        params.append(as_of_date)
    row = conn.execute(q, params).fetchone()
    if not row or not row["starts"]:
        return 0, 0
    return int(row["wins"] or 0), int(row["starts"])


def cd_score(wins: int, starts: int, population_avg_pct: float) -> float:
    """[0,1] course-and-distance suitability, shrunk toward the field base
    rate so one lucky win off one start doesn't dominate."""
    if starts <= 0:
        return 0.5
    prior = 3.0
    rate = (wins + population_avg_pct / 100.0 * prior) / (starts + prior)
    return max(0.0, min(1.0, rate * 2.5))  # ~40% C&D win rate saturates the scale


def sire_strike_rate(conn: sqlite3.Connection, sire: str | None, population_avg_pct: float,
                     as_of_date: str | None = None) -> tuple[float, int]:
    """Win % of this sire's progeny from our own archive, Bayesian-shrunk.

    This is aimed squarely at the model's weakest case: an unrated/lightly
    raced horse, where the engine currently has almost nothing to go on and
    says so ("unrated -- genuinely unpredictable"). Breeding is the one real
    signal available for such a horse."""
    if not sire:
        return population_avg_pct, 0
    q = """SELECT COUNT(*) starts, SUM(CASE WHEN res.finish_position = 1 THEN 1 ELSE 0 END) wins
           FROM runs r JOIN results res ON res.run_id = r.id
           JOIN horses h ON h.id = r.horse_id
           JOIN races ra ON ra.id = r.race_id
           WHERE h.sire = ? AND r.scratched = 0"""
    params = [sire]
    if as_of_date is not None:
        q += " AND ra.race_date < ?"
        params.append(as_of_date)
    row = conn.execute(q, params).fetchone()
    if not row or not row["starts"]:
        return population_avg_pct, 0
    starts, wins = int(row["starts"]), int(row["wins"] or 0)
    prior = 20.0  # sires need a real sample before their rate means anything
    shrunk = 100.0 * (wins + population_avg_pct / 100.0 * prior) / (starts + prior)
    return shrunk, starts


def rating_gap_score(official_rating: int | None, assessed_rating: int | None) -> float:
    """RWITC prints a periodic reassessment alongside the running rating
    ("Rating: 31 (HRA 42 on ...)"). A horse whose assessed mark sits well
    ABOVE its current rating is effectively racing below its assessed ability.
    Neutral 0.5 when either number is missing (i.e. every non-RWITC venue)."""
    if official_rating is None or assessed_rating is None:
        return 0.5
    gap = assessed_rating - official_rating
    return max(0.0, min(1.0, 0.5 + gap / 40.0))
