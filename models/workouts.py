"""Workout readiness signal: morning trackwork + mock races.

Scores a horse's pre-race fitness evidence from the workouts table
(trackwork parsed from indiarace.com, mock races from racingpulse.in).
This is the classic "morning glass" intel: a horse working notably faster
than par with a positive stewards-style comment, or winning a multi-horse
mock race, is telling you it's ready even if its official rating and last
run say otherwise.

Score is in [0, 1] and deliberately coarse:
- comment keywords: positive ("impressed", "moved well", "stretched out",
  "pleased", "fluent") add; explicitly negative phrases ("took no part",
  "planted", "whipped around", "reluctant") subtract. Maintenance comments
  ("easy", "unextended") are neutral -- an easy gallop is normal prep, not
  a knock.
- vs_par: the source's own faster-than-par figure, scaled.
- mock finish: winning or placing 2nd in a mock adds; the paired-work
  "former/latter ... ahead" convention is resolved to whichever horse the
  record belongs to when determinable.
- recency: full weight within 10 days, linearly fading to 0 by 30 days.

IMPORTANT HONESTY NOTE: unlike rating/form/favourite, this signal has NO
backtest behind it -- historical trackwork/mock pages aren't discoverable in
bulk, so its weight in the engine is kept small and it's labelled
(unbacktested) in the UI. Treat it as a tiebreaker, not a foundation.
"""
import re
import sqlite3
from datetime import date, timedelta

POSITIVE_RE = re.compile(
    r"impress|moved well|moved fluent|stretched out|pleased|fluent|good shape|"
    r"strid(?:es|ing) well|worked well|fit|sharp|eas(?:il)?y ahead|finished .{0,20}ahead", re.I)
NEGATIVE_RE = re.compile(
    r"took no part|planted|whipped around|reluctant|refused|unruly|pulled up|"
    r"finished .{0,20}behind|slow", re.I)
FORMER_RE = re.compile(r"\bformer\b", re.I)
LATTER_RE = re.compile(r"\blatter\b", re.I)

LOOKBACK_DAYS = 30
FULL_WEIGHT_DAYS = 10


def _recency_weight(work_date: str, as_of: str) -> float:
    try:
        d_work = date.fromisoformat(work_date)
        d_ref = date.fromisoformat(as_of)
    except ValueError:
        return 0.0
    age = (d_ref - d_work).days
    if age < 0 or age > LOOKBACK_DAYS:
        return 0.0
    if age <= FULL_WEIGHT_DAYS:
        return 1.0
    return 1.0 - (age - FULL_WEIGHT_DAYS) / (LOOKBACK_DAYS - FULL_WEIGHT_DAYS)


def _comment_score(comment: str | None, is_first_listed: bool | None) -> float:
    if not comment:
        return 0.0
    s = 0.0
    pos, neg = POSITIVE_RE.search(comment), NEGATIVE_RE.search(comment)
    # paired-work convention: "Former impressed & finished dist ahead" praises
    # only the first-listed horse; invert for the latter when we know which
    # side this record is on.
    if FORMER_RE.search(comment) and is_first_listed is not None:
        if pos:
            s += 0.5 if is_first_listed else -0.2
        if neg:
            s += -0.5 if is_first_listed else 0.2
        return s
    if LATTER_RE.search(comment) and is_first_listed is not None:
        if pos:
            s += 0.5 if not is_first_listed else -0.2
        if neg:
            s += -0.5 if not is_first_listed else 0.2
        return s
    if pos:
        s += 0.5
    if neg:
        s -= 0.6
    return s


def workout_score(conn: sqlite3.Connection, horse_name: str, venue: str, as_of_date: str) -> tuple[float, int, str | None]:
    """Returns (score in [0,1], n_workouts_found, best_note). Neutral 0.5 is
    'no information'; above = positive evidence, below = negative."""
    rows = conn.execute(
        """SELECT * FROM workouts
           WHERE UPPER(horse_name) = UPPER(?) AND work_date <= ?
           ORDER BY work_date DESC LIMIT 10""",
        (horse_name, as_of_date),
    ).fetchall()
    if not rows:
        return 0.5, 0, None

    total, weight_sum = 0.0, 0.0
    best_note = None
    best_contrib = 0.0
    for row in rows:
        w = _recency_weight(row["work_date"], as_of_date)
        if w <= 0:
            continue
        contrib = 0.0
        # first-listed side only determinable for paired trackwork rows;
        # mock rows use finish position directly
        if row["kind"] == "mock" and row["finish_pos"] and row["field_size"]:
            if row["finish_pos"] == 1:
                contrib += 0.5
            elif row["finish_pos"] == 2:
                contrib += 0.25
            elif row["finish_pos"] >= max(row["field_size"] - 1, 3):
                contrib -= 0.15
            contrib += _comment_score(row["comment"], None) * 0.3
        else:
            contrib += _comment_score(row["comment"], None)
        if row["vs_par"] is not None:
            contrib += max(min(-row["vs_par"] / 20.0, 0.3), -0.3)  # faster than par (negative) helps

        total += w * contrib
        weight_sum += w
        if abs(contrib) > abs(best_contrib):
            best_contrib = contrib
            kind = "mock race" if row["kind"] == "mock" else "trackwork"
            note_bits = [f"{row['work_date']} {kind}"]
            if row["kind"] == "mock" and row["finish_pos"]:
                note_bits.append(f"finished {row['finish_pos']}/{row['field_size']}")
            if row["distance_m"]:
                note_bits.append(f"{row['distance_m']}m")
            if row["comment"]:
                note_bits.append(f'"{row["comment"][:60]}"')
            best_note = ", ".join(note_bits)

    if weight_sum == 0:
        return 0.5, len(rows), None
    avg = total / weight_sum          # roughly [-0.9, +0.8]
    score = min(max(0.5 + avg * 0.55, 0.0), 1.0)
    return score, len(rows), best_note
