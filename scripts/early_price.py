"""Does the model beat the price you could actually have taken?

THE QUESTION THIS EXISTS TO ANSWER
Every accuracy figure in this project benchmarks the model against the FINAL
starting price. That is the sharpest number in racing -- it contains all the
late money -- and the model loses to it badly (26.8% top-pick vs the
favourite's 48.5%, measured over 503 races). But losing to final SP is the
normal condition of almost every model, professional ones included, and it is
NOT the question that decides whether betting the model makes money.

The question that decides that is: when you bet, at the price on offer THEN,
were you getting more than the horse was worth? A model can be worse than the
closing market and still profitable, if the early price is soft enough. It can
also be better than the early price and still lose after takeout. Neither is
knowable without a record of early prices, and nobody archives Indian ones.

So db/ingest.snapshot_odds() now records indiarace's night/morning/opening
quotes each race day, and snapshot_settle_sp() adds the settled SP afterwards.
This script joins the two and reports the comparison.

USAGE
    python scripts/early_price.py                 # all snapshotted races
    python scripts/early_price.py --stage morning # a single stage
    python scripts/early_price.py --venue Pune

HONESTY RULE, enforced in code below: with very few settled races the numbers
here are noise, and a noisy number that looks like an edge is worse than no
number at all. Below MIN_RACES_FOR_VERDICT the script reports what it has and
explicitly refuses to draw a conclusion.
"""
import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from db.schema import get_connection
from models.rating_engine import compute_composite_scores

# Below this, report the raw counts but refuse a verdict. ~500 races gave a
# 95% CI of about +/-4pp on a hit rate; 30 races is far wider than any effect
# worth acting on, so anything under it is presented as "not yet answerable".
MIN_RACES_FOR_VERDICT = 30

STAGES = ("night", "morning", "opening")


def load_snapshot_races(conn, stage: str, venue: str | None = None) -> list[dict]:
    """Races where we hold BOTH an early quote at `stage` and a settled SP,
    plus a result. Anything missing a leg of that triple is unusable."""
    q = """SELECT DISTINCT ra.id, ra.race_date, ra.venue, ra.race_number
           FROM races ra
           JOIN odds_snapshots s_early ON s_early.race_id = ra.id AND s_early.stage = ?
           JOIN odds_snapshots s_sp    ON s_sp.race_id = ra.id AND s_sp.stage = 'sp'
           WHERE EXISTS (SELECT 1 FROM runs r JOIN results res ON res.run_id = r.id
                         WHERE r.race_id = ra.id AND res.finish_position = 1)"""
    params: list = [stage]
    if venue:
        q += " AND ra.venue = ?"
        params.append(venue)
    out = []
    for row in conn.execute(q, params).fetchall():
        winner = conn.execute(
            """SELECT h.id horse_id FROM runs r JOIN results res ON res.run_id = r.id
               JOIN horses h ON h.id = r.horse_id
               WHERE r.race_id = ? AND res.finish_position = 1""", (row["id"],)).fetchone()
        if not winner:
            continue
        early = {r["horse_id"]: r["decimal_odds"] for r in conn.execute(
            "SELECT horse_id, decimal_odds FROM odds_snapshots WHERE race_id=? AND stage=?",
            (row["id"], stage))}
        sp = {r["horse_id"]: r["decimal_odds"] for r in conn.execute(
            "SELECT horse_id, decimal_odds FROM odds_snapshots WHERE race_id=? AND stage='sp'",
            (row["id"],))}
        entries = compute_composite_scores(conn, row["id"], as_of_date=row["race_date"])
        if not entries or not early:
            continue
        out.append({"race_id": row["id"], "date": row["race_date"], "venue": row["venue"],
                    "race_no": row["race_number"], "winner": winner["horse_id"],
                    "early": early, "sp": sp, "entries": entries})
    return out


def report(races: list[dict], stage: str) -> None:
    n = len(races)
    print(f"\n=== stage '{stage}' -- {n} races with an early price, a settled SP and a result ===")
    if n == 0:
        print("  Nothing yet. Run `python -m scripts.daily --snapshot` on a race day to start\n"
              "  collecting, then again with --results after the racing to settle the SPs.")
        return

    # 1. Does the model's pick get a better price early than at the off?
    #    (i.e. is the model picking horses the market later shortens?)
    shortened = drifted = 0
    staked = returned = 0.0
    wins = 0
    priced_picks = 0
    for r in races:
        pick = next((e for e in r["entries"] if e["horse_id"] in r["early"]), None)
        if not pick:
            continue
        priced_picks += 1
        e_price = r["early"][pick["horse_id"]]
        s_price = r["sp"].get(pick["horse_id"])
        if s_price:
            if s_price < e_price:
                shortened += 1
            elif s_price > e_price:
                drifted += 1
        staked += 1.0
        if pick["horse_id"] == r["winner"]:
            wins += 1
            returned += e_price          # backed at the EARLY price
    if not priced_picks:
        print("  The model's top pick was never among the priced runners -- indiarace only\n"
              "  quotes the front of the market, so this can happen on thin coverage.")
        return

    roi = (returned - staked) / staked * 100 if staked else 0.0
    print(f"  model's top pick was priced in {priced_picks}/{n} races")
    print(f"  backing it at the {stage} price: {wins}/{int(staked)} won, ROI {roi:+.1f}%")
    if shortened + drifted:
        print(f"  that pick then SHORTENED by the off {shortened} times, drifted {drifted} "
              f"({shortened/(shortened+drifted)*100:.0f}% shortened)")
        print("  (shortening = the market moved toward our pick after we could have backed it,"
              " which is the pattern a genuine edge produces)")

    if n < MIN_RACES_FOR_VERDICT:
        print(f"\n  VERDICT: not yet answerable. {n} races is far too few -- at ~500 races the\n"
              f"  95% confidence interval on a hit rate was still about +/-4pp, so anything\n"
              f"  here is noise. Keep snapshotting; revisit past {MIN_RACES_FOR_VERDICT} races.")
    else:
        verdict = ("the model appears to beat the early price" if roi > 0
                   else "the model does NOT beat the early price")
        print(f"\n  VERDICT ({n} races): {verdict} (ROI {roi:+.1f}%).")
        print("  Note this is before tote takeout, which on the Indian circuit is heavy;\n"
              "  a small positive ROI here is not yet a profitable strategy.")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--stage", choices=STAGES, help="Only this stage (default: all three)")
    ap.add_argument("--venue")
    args = ap.parse_args()

    conn = get_connection()
    total = conn.execute("SELECT COUNT(*) n FROM odds_snapshots").fetchone()["n"]
    sp_rows = conn.execute("SELECT COUNT(*) n FROM odds_snapshots WHERE stage='sp'").fetchone()["n"]
    print(f"odds_snapshots holds {total} rows ({sp_rows} settled SPs)")

    for stage in ([args.stage] if args.stage else STAGES):
        report(load_snapshot_races(conn, stage, args.venue), stage)
    conn.close()


if __name__ == "__main__":
    main()
