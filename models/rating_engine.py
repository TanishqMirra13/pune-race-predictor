"""Composite rating engine: technicals + fundamentals.

Technicals: each horse's official handicap rating (the richest single
signal, since it already encodes the handicapper's assessment of relative
ability) plus a simple recency-weighted recent-form score.

Fundamentals (models/connections.py):
- Official season-to-date jockey/trainer strike rates (Bayesian-shrunk so a
  hot streak on 3 rides doesn't outweigh a proven trainer on 150).
- Self-derived owner/breeder strike rates from our own accumulated results
  archive (no official leaderboard covers every venue, so this is
  self-sourced -- pooled across venues since ownership/breeding operations
  aren't venue-local the way jockeys/trainers are).
- An owner-sponsor "stable target" flag when an owner's name plausibly
  overlaps with the race's sponsor/title.

No speed figures yet (insufficient backfilled results to calibrate them
honestly) and no draw/going bias -- those are Phase 2 as data accumulates.
Treat outputs as a ranked, reasoned shortlist -- not a certainty.
"""
import math
import sqlite3

from models.connections import (
    owner_sponsor_match, population_avg_win_pct, self_derived_population_avg,
    self_derived_strike_rate, shrunk_win_pct,
)
from models.workouts import workout_score

# Weights tuned by grid search on 401 backtested races across Pune/Mumbai/
# Bangalore (scripts/backtest.py --tune), minimizing log-loss of the actual
# winner. The same config won on Pune alone and on all venues pooled:
# recent form mattered more than originally assumed (0.15 -> 0.25), official
# rating less (0.35 -> 0.25), and a sharper softmax (3 -> 5) was better
# calibrated. Backtest caveats (connection-signal lookahead, small sample)
# are documented in scripts/backtest.py -- rerun it as more results accrue.
RATING_WEIGHT = 0.25
FORM_WEIGHT = 0.25
JOCKEY_WEIGHT = 0.10
TRAINER_WEIGHT = 0.10
OWNER_WEIGHT = 0.10
BREEDER_WEIGHT = 0.10
# Trackwork/mock-race readiness (models/workouts.py). Deliberately small:
# unlike every weight above, this signal has no backtest behind it (bulk
# historical trackwork/mock pages aren't discoverable), so it acts as a
# tiebreaker only. Revisit once a season of workout data has accumulated.
WORKOUT_WEIGHT = 0.08
OWNER_SPONSOR_BONUS = 0.08
SOFTMAX_SHARPNESS = 5.0

MIN_SAMPLE_TO_CITE = 15  # below this, a strike rate is shrunk-to-mean noise, not a real signal to quote

PLACING_POINTS = {1: 5, 2: 3, 3: 2, 4: 1}
RECENCY_WEIGHTS = [0.4, 0.3, 0.2, 0.1]


def _form_score(recent_form_text: str | None) -> tuple[float, int]:
    """Recency-weighted score from a comma-separated placing string, e.g. '2,1,4,?'.
    Returns (score in [0,1], number of usable runs found)."""
    if not recent_form_text:
        return 0.0, 0
    placings = recent_form_text.split(",")
    total, weight_sum, usable = 0.0, 0.0, 0
    for i, p in enumerate(placings[:len(RECENCY_WEIGHTS)]):
        w = RECENCY_WEIGHTS[i]
        p = p.strip()
        if p.isdigit():
            points = PLACING_POINTS.get(int(p), 0)
            total += w * (points / 5.0)
            usable += 1
        weight_sum += w
    if weight_sum == 0:
        return 0.0, 0
    return total / weight_sum, usable


def _normalize_within_field(values: list[float]) -> list[float]:
    vmin, vmax = min(values), max(values)
    span = (vmax - vmin) or 1
    return [(v - vmin) / span for v in values]


def _reasoning(e: dict, field_size: int) -> str:
    parts = []
    if e["official_rating"] is not None:
        parts.append(f"rated {e['official_rating']} ({_ordinal(e['rating_rank'])} of {field_size} in this field)")
    else:
        parts.append("unrated (likely a first-time starter -- genuinely unpredictable)")

    if e["usable_form_runs"] > 0:
        if e["form_score"] >= 0.6:
            parts.append("strong recent form")
        elif e["form_score"] >= 0.3:
            parts.append("moderate recent form")
        else:
            parts.append("modest recent form")
    else:
        parts.append("no recent form on file")

    if e["jockey_sample"] >= MIN_SAMPLE_TO_CITE:
        parts.append(f"jockey {e['jockey']} strike rate {e['jockey_win_pct']:.0f}%")
    if e["trainer_sample"] >= MIN_SAMPLE_TO_CITE:
        parts.append(f"trainer {e['trainer']} strike rate {e['trainer_win_pct']:.0f}%")
    if e["owner_sample"] >= MIN_SAMPLE_TO_CITE:
        parts.append(f"owner's stable strike rate {e['owner_win_pct']:.0f}% (our own results archive)")
    if e["breeder_sample"] >= MIN_SAMPLE_TO_CITE:
        parts.append(f"breeder/stud strike rate {e['breeder_win_pct']:.0f}% (our own results archive)")
    if e.get("workout_n", 0) > 0 and e.get("workout_note"):
        if e["workout_score"] >= 0.62:
            parts.append(f"encouraging recent work ({e['workout_note']}) [unbacktested signal]")
        elif e["workout_score"] <= 0.38:
            parts.append(f"concerning recent work ({e['workout_note']}) [unbacktested signal]")
    if e["owner_sponsor_flag"]:
        parts.append("owner's name overlaps with this race's sponsor -- possible stable-target signal")

    return f"{e['horse_name']}: " + ", ".join(parts) + "."


def _ordinal(n: int) -> str:
    if 10 <= n % 100 <= 20:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(n % 10, "th")
    return f"{n}{suffix}"


def compute_composite_scores(conn: sqlite3.Connection, race_id: int) -> list[dict]:
    race_row = conn.execute("SELECT race_name, venue, race_date FROM races WHERE id=?", (race_id,)).fetchone()
    if not race_row:
        return []
    race_name, venue, race_date = race_row["race_name"], race_row["venue"], race_row["race_date"]

    rows = conn.execute(
        """SELECT r.id AS run_id, h.id AS horse_id, h.name AS horse_name,
                  r.official_rating, r.recent_form_text, r.jockey, r.trainer, r.owner, r.scratched,
                  h.breeder, h.stud
           FROM runs r JOIN horses h ON h.id = r.horse_id
           WHERE r.race_id = ? AND r.scratched = 0""",
        (race_id,),
    ).fetchall()
    if not rows:
        return []

    ratings = [r["official_rating"] for r in rows if r["official_rating"] is not None]
    rmin, rmax = (min(ratings), max(ratings)) if ratings else (0, 0)
    rspan = (rmax - rmin) or 1
    ranked_ratings = sorted({r["official_rating"] for r in rows if r["official_rating"] is not None}, reverse=True)

    jockey_pop_avg = population_avg_win_pct(conn, venue, "jockey")
    trainer_pop_avg = population_avg_win_pct(conn, venue, "trainer")
    owner_pop_avg = self_derived_population_avg(conn, "owner")
    breeder_pop_avg = self_derived_population_avg(conn, "breeder")

    entries = []
    for row in rows:
        rating = row["official_rating"]
        norm_rating = (rating - rmin) / rspan if rating is not None else 0.0
        rating_rank = ranked_ratings.index(rating) + 1 if rating is not None else len(rows)
        form_sc, usable_runs = _form_score(row["recent_form_text"])

        jockey_win_pct, jockey_sample = shrunk_win_pct(conn, venue, "jockey", row["jockey"], jockey_pop_avg)
        trainer_win_pct, trainer_sample = shrunk_win_pct(conn, venue, "trainer", row["trainer"], trainer_pop_avg)
        owner_win_pct, owner_sample = self_derived_strike_rate(conn, "owner", row["owner"], owner_pop_avg)
        breeder_key = row["breeder"] or row["stud"]
        breeder_win_pct, breeder_sample = self_derived_strike_rate(conn, "breeder", breeder_key, breeder_pop_avg)
        sponsor_flag = owner_sponsor_match(row["owner"], race_name)
        workout_sc, workout_n, workout_note = workout_score(conn, row["horse_name"], venue, race_date)

        entries.append({
            "run_id": row["run_id"],
            "horse_id": row["horse_id"],
            "horse_name": row["horse_name"],
            "official_rating": rating,
            "rating_rank": rating_rank,
            "norm_rating": norm_rating,
            "form_score": round(form_sc, 3),
            "usable_form_runs": usable_runs,
            "jockey": row["jockey"],
            "trainer": row["trainer"],
            "owner": row["owner"],
            "breeder": breeder_key,
            "jockey_win_pct": jockey_win_pct,
            "jockey_sample": jockey_sample,
            "trainer_win_pct": trainer_win_pct,
            "trainer_sample": trainer_sample,
            "owner_win_pct": owner_win_pct,
            "owner_sample": owner_sample,
            "breeder_win_pct": breeder_win_pct,
            "breeder_sample": breeder_sample,
            "owner_sponsor_flag": sponsor_flag,
            "workout_score": workout_sc,
            "workout_n": workout_n,
            "workout_note": workout_note,
        })

    jockey_norm = _normalize_within_field([e["jockey_win_pct"] for e in entries])
    trainer_norm = _normalize_within_field([e["trainer_win_pct"] for e in entries])
    owner_norm = _normalize_within_field([e["owner_win_pct"] for e in entries])
    breeder_norm = _normalize_within_field([e["breeder_win_pct"] for e in entries])
    for e, jn, tn, on, bn in zip(entries, jockey_norm, trainer_norm, owner_norm, breeder_norm):
        e["jockey_norm"] = jn
        e["trainer_norm"] = tn
        e["owner_norm"] = on
        e["breeder_norm"] = bn

    apply_weights(entries)
    for e in entries:
        e["reasoning"] = _reasoning(e, len(entries))
        e["confidence"] = "low" if e["official_rating"] is None or e["usable_form_runs"] == 0 else "normal"

    entries.sort(key=lambda e: e["win_probability"], reverse=True)
    return entries


def apply_weights(entries: list[dict], weights: dict | None = None, sharpness: float | None = None) -> None:
    """Compute composite_score and win_probability in place from the
    pre-normalized feature fields. Split out from compute_composite_scores so
    the backtest (scripts/backtest.py) can re-weigh cached features cheaply
    when grid-searching weight configurations."""
    w = weights or {
        "rating": RATING_WEIGHT, "form": FORM_WEIGHT, "jockey": JOCKEY_WEIGHT,
        "trainer": TRAINER_WEIGHT, "owner": OWNER_WEIGHT, "breeder": BREEDER_WEIGHT,
        "workout": WORKOUT_WEIGHT, "sponsor": OWNER_SPONSOR_BONUS,
    }
    k = sharpness if sharpness is not None else SOFTMAX_SHARPNESS
    for e in entries:
        composite = (
            w["rating"] * e["norm_rating"]
            + w["form"] * e["form_score"]
            + w["jockey"] * e["jockey_norm"]
            + w["trainer"] * e["trainer_norm"]
            + w["owner"] * e["owner_norm"]
            + w["breeder"] * e["breeder_norm"]
            + w.get("workout", 0.0) * e.get("workout_score", 0.5)
        )
        if e["owner_sponsor_flag"]:
            composite += w["sponsor"]
        e["composite_score"] = composite

    scores = [e["composite_score"] for e in entries]
    max_score = max(scores)
    exp_scores = [math.exp(k * (s - max_score)) for s in scores]
    total = sum(exp_scores)
    for e, ex in zip(entries, exp_scores):
        e["win_probability"] = ex / total if total else 1 / len(entries)
