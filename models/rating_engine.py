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
from models.form import (
    contextual_form_score, course_distance_record, cd_score, days_since_last_run,
    effective_weight, equipment_change_flag, freshness_score, parse_recent_runs,
    rating_gap_score, sire_strike_rate, weight_score,
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
# Signals added in the Aug 2026 accuracy pass (models/form.py).
#
# MEASURED RESULT, so nobody re-derives this the hard way: on the 511-race
# leak-free archive these collectively moved the top-pick hit rate 24.7% ->
# 26.4%, which a McNemar paired test says is NOT significant (chi-sq 1.36 vs
# the 3.84 needed for p<0.05). A grid search over them found no better
# configuration than these values, so they are kept at these levels -- small,
# honest, and not tuned to noise. Re-run `scripts/backtest.py --tune` once
# the archive is materially bigger; with ~500 races the confidence interval
# (+/-4pp) is wider than any effect measured here.
#
# Several carry information only for some sources (see models/form.py's
# source-richness note), where they return a neutral 0.5 and wash out.
WEIGHT_WEIGHT = 0.08        # effective weight carried, normalised within field
FRESHNESS_WEIGHT = 0.05     # days since last run
CD_WEIGHT = 0.06            # course & distance record
SIRE_WEIGHT = 0.06          # sire's progeny strike rate (matters most for unrated horses)
RATING_GAP_WEIGHT = 0.05    # assessed-vs-current rating gap (RWITC only)
EQUIPMENT_BONUS = 0.04      # first-time notable headgear
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
    # Signals from the Aug 2026 accuracy pass. Each is only mentioned when it
    # actually carries information for this runner/source, so a card from a
    # thinner source doesn't get padded with neutral filler.
    if e.get("cd_starts", 0) > 0:
        parts.append(f"course & distance {e['cd_wins']}/{e['cd_starts']}")
    if e.get("days_since_run") is not None:
        parts.append(f"{e['days_since_run']}d since last run")
    if e.get("effective_weight") is not None and e.get("apprentice_allowance"):
        parts.append(f"carries {e['effective_weight']:.1f}kg after {e['apprentice_allowance']:.0f}kg claim")
    if e.get("equipment_change") and e.get("equipment_note"):
        parts.append(f"first-time {e['equipment_note']}")
    # Breeding is quoted only for horses with little/no form of their own --
    # exactly the case the rest of the model is weakest on. For an established
    # horse its own record is better evidence than its sire's average.
    if (e["official_rating"] is None or e["usable_form_runs"] == 0) and e.get("sire_sample", 0) >= 20:
        parts.append(f"sire {e['sire']} progeny strike rate {e['sire_win_pct']:.0f}%")
    if e.get("assessed_rating") is not None and e["official_rating"] is not None:
        gap = e["assessed_rating"] - e["official_rating"]
        if gap >= 5:
            parts.append(f"assessed {gap} above current mark -- racing below assessed ability")
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


def compute_composite_scores(conn: sqlite3.Connection, race_id: int,
                             as_of_date: str | None = None) -> list[dict]:
    """Score a race's field.

    as_of_date is the LOOKAHEAD GUARD, and the distinction matters:
    - Live use (app.py, scripts/daily.py) passes None. Connection strike
      rates then come from connection_stats, the official club-published
      season snapshot -- correct, because that is genuinely what a punter
      knows on race day.
    - Backtesting (scripts/backtest.py) passes the race's own date. That
      snapshot is useless there: it has no history, so grading a March race
      with today's standings bakes in results that hadn't happened yet.
      With as_of_date set, every derived signal is recomputed from results
      strictly BEFORE that date instead.
    Without this, backtest hit rates were badly inflated -- most visibly at
    venues where the entire archive was also the test set (Hyderabad showed
    a nonsensical ~76% top-pick win rate).
    """
    race_row = conn.execute(
        "SELECT race_name, venue, race_date, distance_m, rating_band_min, rating_band_max "
        "FROM races WHERE id=?", (race_id,),
    ).fetchone()
    if not race_row:
        return []
    race_name, venue, race_date = race_row["race_name"], race_row["venue"], race_row["race_date"]
    distance_m = race_row["distance_m"]
    band_lo, band_hi = race_row["rating_band_min"], race_row["rating_band_max"]
    today_band_mid = ((band_lo + band_hi) / 2) if (band_lo is not None and band_hi is not None) else None

    rows = conn.execute(
        """SELECT r.id AS run_id, h.id AS horse_id, h.name AS horse_name,
                  r.official_rating, r.recent_form_text, r.jockey, r.trainer, r.owner, r.scratched,
                  r.weight_kg, r.apprentice_allowance, r.equipment_codes, r.assessed_rating,
                  r.recent_runs_json, h.breeder, h.stud, h.sire
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

    owner_pop_avg = self_derived_population_avg(conn, "owner", as_of_date=as_of_date)
    breeder_pop_avg = self_derived_population_avg(conn, "breeder", as_of_date=as_of_date)
    if as_of_date is None:
        jockey_pop_avg = population_avg_win_pct(conn, venue, "jockey")
        trainer_pop_avg = population_avg_win_pct(conn, venue, "trainer")
    else:
        jockey_pop_avg = self_derived_population_avg(conn, "jockey", as_of_date=as_of_date)
        trainer_pop_avg = self_derived_population_avg(conn, "trainer", as_of_date=as_of_date)

    field_eff_weights = [
        w for w in (effective_weight(r["weight_kg"], r["apprentice_allowance"]) for r in rows)
        if w is not None
    ]

    entries = []
    for row in rows:
        rating = row["official_rating"]
        norm_rating = (rating - rmin) / rspan if rating is not None else 0.0
        rating_rank = ranked_ratings.index(rating) + 1 if rating is not None else len(rows)

        # Prefer the context-aware score when the source actually gave us
        # per-run distance/class; otherwise fall back to the plain placings
        # scorer so placings-only venues are unaffected rather than penalised.
        recent_runs = parse_recent_runs(row["recent_runs_json"])
        ctx_form, ctx_usable, used_context = contextual_form_score(
            recent_runs, distance_m, today_band_mid)
        if used_context and ctx_usable:
            form_sc, usable_runs = ctx_form, ctx_usable
        else:
            form_sc, usable_runs = _form_score(row["recent_form_text"])

        if as_of_date is None:
            jockey_win_pct, jockey_sample = shrunk_win_pct(conn, venue, "jockey", row["jockey"], jockey_pop_avg)
            trainer_win_pct, trainer_sample = shrunk_win_pct(conn, venue, "trainer", row["trainer"], trainer_pop_avg)
        else:
            jockey_win_pct, jockey_sample = self_derived_strike_rate(
                conn, "jockey", row["jockey"], jockey_pop_avg, as_of_date=as_of_date)
            trainer_win_pct, trainer_sample = self_derived_strike_rate(
                conn, "trainer", row["trainer"], trainer_pop_avg, as_of_date=as_of_date)
        owner_win_pct, owner_sample = self_derived_strike_rate(
            conn, "owner", row["owner"], owner_pop_avg, as_of_date=as_of_date)
        breeder_key = row["breeder"] or row["stud"]
        breeder_win_pct, breeder_sample = self_derived_strike_rate(
            conn, "breeder", breeder_key, breeder_pop_avg, as_of_date=as_of_date)
        sponsor_flag = owner_sponsor_match(row["owner"], race_name)
        workout_sc, workout_n, workout_note = workout_score(conn, row["horse_name"], venue, race_date)

        eff_wt = effective_weight(row["weight_kg"], row["apprentice_allowance"])
        wt_sc = weight_score(row["weight_kg"], row["apprentice_allowance"], field_eff_weights)
        days_off = days_since_last_run(recent_runs, race_date)
        fresh_sc = freshness_score(days_off)
        cd_wins, cd_starts = course_distance_record(
            conn, row["horse_id"], venue, distance_m, as_of_date, exclude_race_id=race_id)
        cd_sc = cd_score(cd_wins, cd_starts, owner_pop_avg)
        sire_pct, sire_sample = sire_strike_rate(conn, row["sire"], owner_pop_avg, as_of_date=as_of_date)
        gap_sc = rating_gap_score(rating, row["assessed_rating"])
        equip_flag, equip_note = equipment_change_flag(
            conn, row["horse_id"], race_id, row["equipment_codes"])

        entries.append({
            "run_id": row["run_id"],
            "horse_id": row["horse_id"],
            "horse_name": row["horse_name"],
            "official_rating": rating,
            "assessed_rating": row["assessed_rating"],
            "rating_rank": rating_rank,
            "norm_rating": norm_rating,
            "form_score": round(form_sc, 3),
            "usable_form_runs": usable_runs,
            "form_used_context": used_context,
            "jockey": row["jockey"],
            "trainer": row["trainer"],
            "owner": row["owner"],
            "breeder": breeder_key,
            "sire": row["sire"],
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
            "weight_kg": row["weight_kg"],
            "apprentice_allowance": row["apprentice_allowance"],
            "effective_weight": eff_wt,
            "weight_score": wt_sc,
            "days_since_run": days_off,
            "freshness_score": fresh_sc,
            "cd_wins": cd_wins,
            "cd_starts": cd_starts,
            "cd_score": cd_sc,
            "sire_win_pct": sire_pct,
            "sire_sample": sire_sample,
            "rating_gap_score": gap_sc,
            "equipment_change": equip_flag,
            "equipment_note": equip_note,
        })

    jockey_norm = _normalize_within_field([e["jockey_win_pct"] for e in entries])
    trainer_norm = _normalize_within_field([e["trainer_win_pct"] for e in entries])
    owner_norm = _normalize_within_field([e["owner_win_pct"] for e in entries])
    breeder_norm = _normalize_within_field([e["breeder_win_pct"] for e in entries])
    sire_norm = _normalize_within_field([e["sire_win_pct"] for e in entries])
    for e, jn, tn, on, bn, sn in zip(entries, jockey_norm, trainer_norm, owner_norm, breeder_norm, sire_norm):
        e["jockey_norm"] = jn
        e["trainer_norm"] = tn
        e["owner_norm"] = on
        e["breeder_norm"] = bn
        e["sire_norm"] = sn

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
        "weight": WEIGHT_WEIGHT, "freshness": FRESHNESS_WEIGHT, "cd": CD_WEIGHT,
        "sire": SIRE_WEIGHT, "rating_gap": RATING_GAP_WEIGHT, "equipment": EQUIPMENT_BONUS,
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
            + w.get("weight", 0.0) * e.get("weight_score", 0.5)
            + w.get("freshness", 0.0) * e.get("freshness_score", 0.5)
            + w.get("cd", 0.0) * e.get("cd_score", 0.5)
            + w.get("sire", 0.0) * e.get("sire_norm", 0.5)
            + w.get("rating_gap", 0.0) * e.get("rating_gap_score", 0.5)
        )
        if e["owner_sponsor_flag"]:
            composite += w["sponsor"]
        if e.get("equipment_change"):
            composite += w.get("equipment", 0.0)
        e["composite_score"] = composite

    scores = [e["composite_score"] for e in entries]
    max_score = max(scores)
    exp_scores = [math.exp(k * (s - max_score)) for s in scores]
    total = sum(exp_scores)
    for e, ex in zip(entries, exp_scores):
        e["win_probability"] = ex / total if total else 1 / len(entries)
