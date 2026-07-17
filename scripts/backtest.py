"""Backtest: compare the model's predictions against actual results, and
against the market (tote favourite) -- the strongest verifiable public
prediction available for Indian racing, since tipster archives (racingpulse
selections etc.) are paywalled or unverifiable.

Usage:
    python scripts/backtest.py                # all venues
    python scripts/backtest.py --venue Pune
    python scripts/backtest.py --tune         # also grid-search weights

HONESTY / LOOKAHEAD CAVEATS (read before trusting the numbers):
- Official rating, recent form, and the tote favourite are point-in-time
  clean: they were all knowable before each race ran.
- Jockey/trainer strike rates use CURRENT season snapshots applied
  retroactively, and owner/breeder rates are derived from the full results
  archive INCLUDING the races being backtested. Both leak future information
  into those signals, which flatters them. Treat the rating/form/favourite
  numbers as solid and the connection-signal numbers as upper bounds.
- ~55 race days is a small sample; differences of a few percentage points
  are noise, not signal.
"""
import argparse
import itertools
import math
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from db.schema import get_connection
from models.rating_engine import apply_weights, compute_composite_scores


def load_backtest_races(conn, venue: str | None = None) -> list[dict]:
    """Each item: {'race_id', 'venue', 'race_date', 'favourite', 'winner_horse_id',
    'winner_name', 'entries' (with features + current-weight probabilities)}."""
    q = """SELECT ra.id, ra.venue, ra.race_date, ra.race_number, ra.tote_favourite
           FROM races ra
           WHERE EXISTS (SELECT 1 FROM runs r JOIN results res ON res.run_id = r.id
                         WHERE r.race_id = ra.id AND res.finish_position = 1)"""
    params: tuple = ()
    if venue:
        q += " AND ra.venue = ?"
        params = (venue,)
    races = []
    for row in conn.execute(q, params):
        winner = conn.execute(
            """SELECT h.id horse_id, h.name FROM runs r
               JOIN results res ON res.run_id = r.id JOIN horses h ON h.id = r.horse_id
               WHERE r.race_id = ? AND res.finish_position = 1""",
            (row["id"],),
        ).fetchone()
        entries = compute_composite_scores(conn, row["id"])
        if not winner or len(entries) < 3:
            continue  # walkovers/tiny fields aren't informative
        races.append({
            "race_id": row["id"], "venue": row["venue"], "race_date": row["race_date"],
            "race_number": row["race_number"], "favourite": row["tote_favourite"],
            "winner_horse_id": winner["horse_id"], "winner_name": winner["name"],
            "entries": entries,
        })
    return races


def benchmark(races: list[dict]) -> dict:
    n = len(races)
    model_hits = fav_hits = toprated_hits = 0
    model_top3 = 0
    random_expectation = 0.0
    fav_races = 0
    agree_races = agree_hits = 0
    disagree_races = disagree_model_hits = disagree_fav_hits = 0

    for race in races:
        entries = race["entries"]
        top = entries[0]
        winner_id = race["winner_horse_id"]
        random_expectation += 1 / len(entries)

        model_hit = top["horse_id"] == winner_id
        model_hits += model_hit
        model_top3 += any(e["horse_id"] == winner_id for e in entries[:3])

        best_rated = max(entries, key=lambda e: (e["official_rating"] is not None, e["official_rating"] or -1))
        toprated_hits += best_rated["horse_id"] == winner_id

        fav_name = race["favourite"]
        if fav_name:
            fav_races += 1
            fav_hit = fav_name.strip().upper() == race["winner_name"].strip().upper()
            fav_hits += fav_hit
            if top["horse_name"].strip().upper() == fav_name.strip().upper():
                agree_races += 1
                agree_hits += model_hit
            else:
                disagree_races += 1
                disagree_model_hits += model_hit
                disagree_fav_hits += fav_hit

    return {
        "n_races": n,
        "model_top_pick_win_rate": model_hits / n,
        "model_top3_rate": model_top3 / n,
        "favourite_win_rate": fav_hits / fav_races if fav_races else None,
        "n_races_with_favourite": fav_races,
        "top_rated_win_rate": toprated_hits / n,
        "random_baseline": random_expectation / n,
        "model_agrees_with_market": {"n": agree_races, "win_rate": agree_hits / agree_races if agree_races else None},
        "model_disagrees_with_market": {
            "n": disagree_races,
            "model_win_rate": disagree_model_hits / disagree_races if disagree_races else None,
            "favourite_win_rate": disagree_fav_hits / disagree_races if disagree_races else None,
        },
    }


def calibration(races: list[dict]) -> list[dict]:
    buckets = defaultdict(lambda: {"n": 0, "wins": 0})
    for race in races:
        for e in race["entries"]:
            b = min(int(e["win_probability"] * 10), 5)  # 0-10%,...,40-50%,50%+
            buckets[b]["n"] += 1
            buckets[b]["wins"] += e["horse_id"] == race["winner_horse_id"]
    out = []
    labels = {0: "0-10%", 1: "10-20%", 2: "20-30%", 3: "30-40%", 4: "40-50%", 5: "50%+"}
    for b in sorted(buckets):
        d = buckets[b]
        out.append({"bucket": labels[b], "n_runners": d["n"], "actual_win_rate": d["wins"] / d["n"]})
    return out


def signal_patterns(races: list[dict]) -> dict:
    """Where do winners actually come from, signal by signal?"""
    rank_wins = defaultdict(lambda: {"n": 0, "wins": 0})
    jockey_tiers = defaultdict(lambda: {"n": 0, "wins": 0})
    form_tiers = defaultdict(lambda: {"n": 0, "wins": 0})
    for race in races:
        for e in race["entries"]:
            won = e["horse_id"] == race["winner_horse_id"]
            rank_wins[min(e["rating_rank"], 5)]["n"] += 1
            rank_wins[min(e["rating_rank"], 5)]["wins"] += won
            jt = "high (>=15%)" if e["jockey_win_pct"] >= 15 else ("mid (8-15%)" if e["jockey_win_pct"] >= 8 else "low (<8%)")
            jockey_tiers[jt]["n"] += 1
            jockey_tiers[jt]["wins"] += won
            ft = "strong (>=0.6)" if e["form_score"] >= 0.6 else ("moderate (0.3-0.6)" if e["form_score"] >= 0.3 else "weak (<0.3)")
            form_tiers[ft]["n"] += 1
            form_tiers[ft]["wins"] += won
    fmt = lambda d: {k: {"n": v["n"], "win_rate": round(v["wins"] / v["n"], 3)} for k, v in sorted(d.items(), key=lambda kv: str(kv[0]))}
    return {"by_rating_rank": fmt(rank_wins), "by_jockey_strike_tier": fmt(jockey_tiers), "by_form_tier": fmt(form_tiers)}


def avg_log_loss(races: list[dict], weights: dict, sharpness: float) -> float:
    total = 0.0
    for race in races:
        apply_weights(race["entries"], weights, sharpness)
        p_winner = next(
            (e["win_probability"] for e in race["entries"] if e["horse_id"] == race["winner_horse_id"]), None,
        )
        total += -math.log(max(p_winner, 1e-9))
    return total / len(races)


def tune_weights(races: list[dict]) -> dict:
    """Coarse grid search minimizing average log-loss of the actual winner.
    Weights are relative (softmax normalizes), so we fix rating's grid around
    its current value and vary the rest."""
    grids = {
        "rating": [0.25, 0.35, 0.45, 0.55],
        "form": [0.05, 0.15, 0.25],
        "connections": [0.10, 0.25, 0.40],  # split evenly across jockey/trainer/owner/breeder
        "sharpness": [2.0, 3.0, 4.0, 5.0],
    }
    best = None
    for r, f, c, k in itertools.product(grids["rating"], grids["form"], grids["connections"], grids["sharpness"]):
        w = {"rating": r, "form": f, "jockey": c / 4, "trainer": c / 4,
             "owner": c / 4, "breeder": c / 4, "sponsor": 0.08}
        loss = avg_log_loss(races, w, k)
        hits = sum(
            max(race["entries"], key=lambda e: e["win_probability"])["horse_id"] == race["winner_horse_id"]
            for race in races
        )
        if best is None or loss < best["log_loss"]:
            best = {"weights": w, "sharpness": k, "log_loss": loss, "top_pick_win_rate": hits / len(races)}
    return best


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--venue", choices=["Pune", "Mumbai", "Bangalore"])
    parser.add_argument("--tune", action="store_true")
    args = parser.parse_args()

    conn = get_connection()
    races = load_backtest_races(conn, args.venue)
    print(f"Backtesting {len(races)} races" + (f" at {args.venue}" if args.venue else " across all venues"))
    print()

    b = benchmark(races)
    print("=== BENCHMARKS: who actually picks winners? ===")
    print(f"  Model top pick:     {b['model_top_pick_win_rate']*100:5.1f}%  (top-3: {b['model_top3_rate']*100:.1f}%)")
    print(f"  Tote favourite:     {b['favourite_win_rate']*100:5.1f}%  ({b['n_races_with_favourite']} races with favourite recorded)")
    print(f"  Top official rating:{b['top_rated_win_rate']*100:5.1f}%")
    print(f"  Random pick:        {b['random_baseline']*100:5.1f}%")
    print()
    ag, dg = b["model_agrees_with_market"], b["model_disagrees_with_market"]
    print(f"  Model agrees with favourite in {ag['n']} races -> wins {ag['win_rate']*100:.1f}% of those")
    print(f"  Model disagrees in {dg['n']} races -> model wins {dg['model_win_rate']*100:.1f}%, favourite wins {dg['favourite_win_rate']*100:.1f}%")
    print()

    print("=== CALIBRATION: predicted probability vs reality ===")
    for row in calibration(races):
        print(f"  {row['bucket']:>7}: {row['n_runners']:4d} runners, actual win rate {row['actual_win_rate']*100:5.1f}%")
    print()

    print("=== SIGNAL PATTERNS ===")
    p = signal_patterns(races)
    print("  Winner rate by official-rating rank in field (5 = 5th-or-worse):")
    for k, v in p["by_rating_rank"].items():
        print(f"    rank {k}: {v['win_rate']*100:5.1f}%  (n={v['n']})")
    print("  Winner rate by jockey season strike tier:")
    for k, v in p["by_jockey_strike_tier"].items():
        print(f"    {k}: {v['win_rate']*100:5.1f}%  (n={v['n']})")
    print("  Winner rate by recent-form tier:")
    for k, v in p["by_form_tier"].items():
        print(f"    {k}: {v['win_rate']*100:5.1f}%  (n={v['n']})")
    print()

    current_loss = avg_log_loss(races, weights=None or {
        "rating": 0.35, "form": 0.15, "jockey": 0.12, "trainer": 0.13,
        "owner": 0.13, "breeder": 0.12, "sponsor": 0.08}, sharpness=3.0)
    print(f"Current weights avg log-loss: {current_loss:.4f}")

    if args.tune:
        print("\n=== WEIGHT TUNING (grid search, min log-loss) ===")
        best = tune_weights(races)
        print(f"  Best: {best['weights']}, sharpness={best['sharpness']}")
        print(f"  log-loss {best['log_loss']:.4f} (vs current {current_loss:.4f}), top-pick win rate {best['top_pick_win_rate']*100:.1f}%")

    conn.close()


if __name__ == "__main__":
    main()
