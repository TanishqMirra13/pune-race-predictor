import sys
from datetime import date, timedelta
from pathlib import Path

import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parent))

from db.schema import get_connection, init_db
from db.ingest import store_racecard, store_raceresult
from scrapers import rwitc, btc
from models.rating_engine import compute_composite_scores
from models.staking import (
    build_win_place_plan, harville_forecast_probabilities, jackpot_leg_plan, stop_rules,
    build_place_shortlist,
)

st.set_page_config(page_title="Pune Race Predictor", layout="wide")
init_db()
conn = get_connection()

# Both RWITC (Pune/Mumbai -- same site, auto-detects venue by date) and BTC
# (Bangalore, separate site) expose the same fetch_racecard_html /
# fetch_raceresult_html / parse_racecard / parse_raceresult interface, so
# the rest of the app doesn't need to know which source it's talking to.
SCRAPER_BY_VENUE = {"Pune": rwitc, "Mumbai": rwitc, "Bangalore": btc}


def next_saturday(d: date) -> date:
    days_ahead = (5 - d.weekday()) % 7  # Saturday = 5
    return d + timedelta(days=days_ahead or 7 if d.weekday() == 5 else days_ahead)


st.title("🐎 Pune Race Predictor")
st.warning(
    "This is entertainment analysis for fun-money wagering, not a winner-picker. "
    "Horse racing is genuinely unpredictable -- the ranked picks below are a transparent, "
    "data-backed shortlist, not a guarantee. Only stake what you've told the app is your "
    "budget for the day, and check the Bankroll & Calibration tab regularly to see how the "
    "model's confidence has actually tracked against real outcomes.",
    icon="⚠️",
)

with st.sidebar:
    st.header("Today's session")
    if "race_date_input" not in st.session_state:
        st.session_state["race_date_input"] = next_saturday(date.today())
    col_today, col_sat = st.columns(2)
    if col_today.button("Today", use_container_width=True):
        st.session_state["race_date_input"] = date.today()
    if col_sat.button("Next Saturday", use_container_width=True):
        st.session_state["race_date_input"] = next_saturday(date.today())
    race_date = st.date_input("Race date", key="race_date_input")
    venue = st.selectbox("Venue", ["Pune", "Mumbai", "Bangalore"])
    budget = st.number_input("Budget for today (fun money, Rs)", min_value=0.0, value=1000.0, step=100.0)
    goal = st.number_input("Goal / mental target (Rs profit)", min_value=0.0, value=500.0, step=100.0)
    date_str = race_date.strftime("%Y-%m-%d")

    st.divider()
    st.subheader("Load race card")
    col1, col2 = st.columns(2)
    source = SCRAPER_BY_VENUE[venue]
    with col1:
        if st.button("Fetch live", use_container_width=True):
            try:
                html = source.fetch_racecard_html(date_str, use_cache=False)
                races = source.parse_racecard(html)
                if not races:
                    st.error("No races found for this date -- the site may not have posted a card yet.")
                else:
                    store_racecard(conn, date_str, venue, races)
                    st.success(f"Loaded {len(races)} races for {date_str}.")
            except Exception as e:
                st.error(f"Fetch failed: {e}. Try the manual paste fallback below.")
    with col2:
        if st.button("Fetch results", use_container_width=True):
            try:
                html = source.fetch_raceresult_html(date_str, use_cache=False)
                races = source.parse_raceresult(html)
                if not races:
                    st.error("No results found for this date yet.")
                else:
                    store_raceresult(conn, date_str, venue, races)
                    st.success(f"Loaded results for {len(races)} races.")
            except Exception as e:
                st.error(f"Fetch failed: {e}")

    with st.expander("Manual paste fallback"):
        st.caption("If live fetch fails (site down, blocked, or off-season), paste the race card HTML source here.")
        pasted = st.text_area("Race card HTML", height=100)
        if st.button("Parse pasted HTML"):
            try:
                races = source.parse_racecard(pasted)
                store_racecard(conn, date_str, venue, races)
                st.success(f"Parsed and stored {len(races)} races.")
            except Exception as e:
                st.error(f"Could not parse pasted HTML: {e}")

    st.divider()
    st.subheader("Fundamentals")
    st.caption("Official season-to-date jockey/trainer strike rates -- feeds the connections signal in every pick.")
    if st.button("Fetch trackwork + mock races", use_container_width=True):
        try:
            from db.ingest import store_workouts
            from scrapers import indiarace, racingpulse
            n_tw = 0
            for d in indiarace.fetch_trackwork_dates(venue)[-15:]:
                html = indiarace.fetch_trackwork_html(venue, d, use_cache=True)
                if html:
                    n_tw += store_workouts(conn, indiarace.parse_trackwork(html, venue, d))
            n_mock = 0
            for pg_id in racingpulse.discover_mock_race_pages(venue):
                html = racingpulse.fetch_page_html(pg_id, use_cache=False)
                n_mock += store_workouts(conn, racingpulse.parse_mock_races(html, venue))
            msg = f"Stored {n_tw} trackwork records, {n_mock} mock-race records for {venue}."
            if n_mock == 0:
                msg += " (Mock races page may not be published yet -- it usually appears on race-day morning.)"
            st.success(msg)
        except Exception as e:
            st.error(f"Workout fetch failed: {e}")
    if st.button("Refresh jockey/trainer stats", use_container_width=True):
        try:
            from db.ingest import store_connection_stats
            j = source.parse_jockey_stats(source.fetch_jockey_stats_html(use_cache=False))
            t = source.parse_trainer_stats(source.fetch_trainer_stats_html(use_cache=False))
            store_connection_stats(conn, venue, "jockey", j)
            store_connection_stats(conn, venue, "trainer", t)
            st.success(f"Refreshed {len(j)} jockeys, {len(t)} trainers for {venue}.")
        except Exception as e:
            st.error(f"Refresh failed: {e}")


def get_race_plans(d: str, v: str) -> list[dict]:
    rows = conn.execute(
        "SELECT id, race_number, race_name, class_code, distance_m FROM races "
        "WHERE race_date=? AND venue=? ORDER BY race_number", (d, v),
    ).fetchall()
    plans = []
    for row in rows:
        entries = compute_composite_scores(conn, row["id"])
        plans.append({
            "race_id": row["id"], "race_no": row["race_number"], "race_name": row["race_name"],
            "class_code": row["class_code"], "distance_m": row["distance_m"], "entries": entries,
        })
    return plans


race_plans = get_race_plans(date_str, venue)

tab_today, tab_place, tab_forecast, tab_jackpot, tab_connections, tab_backtest, tab_bankroll = st.tabs(
    ["Today's Picks", "Place Bets", "Forecast / Quinella", "Jackpot Planner", "Connections", "Backtest", "Bankroll & Calibration"]
)

with tab_today:
    if not race_plans:
        st.info("No race card loaded for this date/venue yet. Use the sidebar to fetch or paste one.")
    else:
        plan = build_win_place_plan(budget, race_plans)
        st.subheader(f"Win plan -- Rs{plan['total_staked']} staked of Rs{budget:.0f} budget")
        if plan["bets"]:
            st.dataframe(
                [{"Race": b["race_no"], "Pick": b["horse_name"], "Stake (Rs)": b["stake"],
                  "Win prob": f"{b['win_probability']*100:.0f}%", "Edge": f"{b['edge']*100:.0f}pp",
                  "Confidence": b["confidence"]} for b in plan["bets"]],
                use_container_width=True, hide_index=True,
            )
        if plan["skipped_races"]:
            with st.expander(f"{len(plan['skipped_races'])} race(s) skipped -- no clear edge"):
                for s in plan["skipped_races"]:
                    st.write(f"Race {s['race_no']}: {s['reason']}")

        results_by_race = {}
        for rp in race_plans:
            rows = conn.execute(
                """SELECT h.name, res.finish_position FROM runs r JOIN horses h ON h.id = r.horse_id
                   JOIN results res ON res.run_id = r.id
                   WHERE r.race_id = ? AND res.finish_position IS NOT NULL ORDER BY res.finish_position""",
                (rp["race_id"],),
            ).fetchall()
            if rows:
                results_by_race[rp["race_no"]] = rows

        if results_by_race:
            st.divider()
            st.subheader(f"Results so far -- {len(results_by_race)}/{len(race_plans)} races run")
            scorecard = []
            wins = places = 0
            for rp in race_plans:
                rows = results_by_race.get(rp["race_no"])
                if not rows or not rp["entries"]:
                    continue
                pick = rp["entries"][0]["horse_name"]
                pos = next((r["finish_position"] for r in rows if r["name"].upper() == pick.upper()), None)
                if pos == 1:
                    outcome, wins, places = "✅ WON", wins + 1, places + 1
                elif pos and pos <= 3:
                    outcome, places = f"🥈 {pos}", places + 1
                elif pos:
                    outcome = f"❌ {pos}th"
                else:
                    outcome = "❌ unplaced/scratched"
                scorecard.append({"Race": rp["race_no"], "Our pick": pick, "Winner": rows[0]["name"], "Result": outcome})
            if scorecard:
                st.dataframe(scorecard, use_container_width=True, hide_index=True)
                st.caption(f"Top picks: {wins}/{len(scorecard)} won, {places}/{len(scorecard)} placed top-3. Full detail and dividends are in each race's expander below.")
            if len(results_by_race) < len(race_plans):
                st.caption("Not all races have results yet -- click 'Fetch results' in the sidebar to update as the day progresses.")

        st.divider()
        st.subheader("Race-by-race breakdown")
        for rp in race_plans:
            title = f"Race {rp['race_no']}: {rp['race_name']}"
            if rp["class_code"]:
                title += f" (Class {rp['class_code']})"
            with st.expander(title):
                if not rp["entries"]:
                    st.write("No runners parsed for this race.")
                    continue
                def _pct(val, sample):
                    return f"{val:.0f}%" if sample >= 15 else f"~{val:.0f}%"

                def _work_flag(e):
                    if not e.get("workout_n"):
                        return ""
                    if e["workout_score"] >= 0.62:
                        return "🔥"
                    if e["workout_score"] <= 0.38:
                        return "⚠️"
                    return "•"

                st.dataframe(
                    [{"Rank": i + 1, "Horse": e["horse_name"], "Rating": e["official_rating"],
                      "Win prob": f"{e['win_probability']*100:.1f}%",
                      "Work": _work_flag(e),
                      "Jockey": e["jockey"], "Jockey %": _pct(e["jockey_win_pct"], e["jockey_sample"]),
                      "Trainer": e["trainer"], "Trainer %": _pct(e["trainer_win_pct"], e["trainer_sample"]),
                      "Owner %": _pct(e["owner_win_pct"], e["owner_sample"]),
                      "Breeder": e["breeder"], "Breeder %": _pct(e["breeder_win_pct"], e["breeder_sample"]),
                      "Sponsor tie": "🔗" if e["owner_sponsor_flag"] else "",
                      "Confidence": e["confidence"]}
                     for i, e in enumerate(rp["entries"])],
                    use_container_width=True, hide_index=True,
                )
                finishers = conn.execute(
                    """SELECT h.name, res.finish_position, res.dividend_win, res.dividend_place,
                              r.jockey, r.odds_sp
                       FROM runs r JOIN horses h ON h.id = r.horse_id
                       JOIN results res ON res.run_id = r.id
                       WHERE r.race_id = ? AND res.finish_position IS NOT NULL
                       ORDER BY res.finish_position""",
                    (rp["race_id"],),
                ).fetchall()
                if finishers:
                    st.markdown("**Actual result**")
                    st.dataframe(
                        [{"Pos": f["finish_position"], "Horse": f["name"], "Jockey": f["jockey"],
                          "Odds": f["odds_sp"], "Win div (Rs/10)": f["dividend_win"]}
                         for f in finishers[:5]],
                        use_container_width=True, hide_index=True,
                    )
                    winner = finishers[0]["name"]
                    fav_row = conn.execute("SELECT tote_favourite FROM races WHERE id=?", (rp["race_id"],)).fetchone()
                    fav = fav_row["tote_favourite"] if fav_row else None
                    top_pick = rp["entries"][0]
                    pick_result = next((f["finish_position"] for f in finishers if f["name"].upper() == top_pick["horse_name"].upper()), None)
                    if pick_result == 1:
                        verdict = f"✅ Our top pick **{top_pick['horse_name']}** WON."
                    elif pick_result and pick_result <= 3:
                        verdict = f"🥈 Our top pick **{top_pick['horse_name']}** placed ({pick_result}{'nd' if pick_result==2 else 'rd'}), didn't win."
                    elif pick_result:
                        verdict = f"❌ Our top pick **{top_pick['horse_name']}** finished {pick_result}th, unplaced."
                    else:
                        verdict = f"Our top pick **{top_pick['horse_name']}** did not finish/was scratched."
                    if fav:
                        verdict += f" Tote favourite was **{fav}** ({'won' if fav.upper()==winner.upper() else 'did not win'})."
                    st.info(verdict)
                else:
                    st.caption("No result recorded yet for this race -- use 'Fetch results' in the sidebar once it's run.")

                worked = [e for e in rp["entries"] if e.get("workout_note")]
                if worked:
                    with st.expander(f"Recent trackwork / mock races ({len(worked)} runners with recorded work)"):
                        st.caption(
                            "Morning gallops from indiarace.com and mock races from racingpulse.in. "
                            "Work column: 🔥 encouraging, ⚠️ concerning, • neutral/maintenance. "
                            "This signal has no backtest behind it yet (bulk historical workout pages "
                            "aren't available) -- it's weighted as a tiebreaker, not a foundation."
                        )
                        for e in worked:
                            st.write(f"**{e['horse_name']}** ({e['workout_n']} works, score {e['workout_score']:.2f}): {e['workout_note']}")
                st.caption(
                    "% columns are strike rates (Bayesian-shrunk toward the field average when the sample "
                    "is thin -- a '~' prefix means fewer than 15 rides/starts on record, so treat it as "
                    "noisy). Jockey/Trainer % are official club season stats. Owner/Breeder % are derived "
                    "from our own accumulated results archive (no official leaderboard covers every venue)."
                )
                for e in rp["entries"][:3]:
                    st.caption(e["reasoning"])

with tab_place:
    st.subheader("Place bets — top-2 / top-3 finish")
    st.markdown(
        "**Lower variance, not higher edge.** A place bet hits far more often "
        "than a win bet but pays a fraction as much, so the same rule applies: "
        "only bet when the board's place dividend beats the horse's fair place "
        "price. The real money-maker here is the **consistent placer** — a horse "
        "that runs 2nd/3rd a lot but rarely wins, which the crowd underbets in "
        "the place pool. Those are tagged 🎯 **VALUE** below."
    )
    st.caption(
        "Places paid (verified from RWITC tote data): 8+ runners → 3 places, "
        "5–7 → 2 places, 4 or fewer → win-only (no place pool). Place % is our "
        "own estimate from the win model (Harville) — the tote doesn't publish "
        "pre-race place odds, so price it at the board with: fair place odds = "
        "(100 ÷ place%) − 1."
    )
    if not race_plans:
        st.info("Load a race card first.")
    else:
        shortlist = build_place_shortlist(race_plans)
        for sl in shortlist:
            rp = next(r for r in race_plans if r["race_no"] == sl["race_no"])
            title = f"Race {sl['race_no']}: {rp['race_name']}"
            if sl["places_paid"] == 0:
                with st.expander(f"{title} — win-only ({sl['field_size']} runners, no place pool)"):
                    st.write("Too few runners for a place pool.")
                continue
            with st.expander(f"{title} — {sl['places_paid']} places paid ({sl['field_size']} runners)"):
                st.dataframe(
                    [{"Horse": p["horse_name"],
                      "Place %": f"{p['place_probability']*100:.0f}%",
                      "Fair place odds": f"{(100/(p['place_probability']*100) - 1):.2f}/1" if p["place_probability"] > 0 else "-",
                      "Win %": f"{p['win_probability']*100:.0f}%",
                      "Value": "🎯 VALUE" if p["value_flag"] else ""}
                     for p in sl["picks"]],
                    use_container_width=True, hide_index=True,
                )
                value_picks = [p for p in sl["picks"] if p["value_flag"]]
                if value_picks:
                    for p in value_picks:
                        st.caption(
                            f"🎯 **{p['horse_name']}** — {p['place_probability']*100:.0f}% to place but only "
                            f"{p['win_probability']*100:.0f}% to win: a consistent-placer profile the crowd "
                            f"tends to underprice in the place pool. Bet to place only if the board pays more "
                            f"than {(100/(p['place_probability']*100) - 1):.2f}/1."
                        )

with tab_forecast:
    if not race_plans:
        st.info("Load a race card first.")
    else:
        race_choice = st.selectbox(
            "Race", options=[rp["race_no"] for rp in race_plans],
            format_func=lambda n: f"Race {n}",
        )
        rp = next(rp for rp in race_plans if rp["race_no"] == race_choice)
        if not rp["entries"]:
            st.info("No runners for this race.")
        else:
            win_probs = {e["horse_id"]: (e["horse_name"], e["win_probability"]) for e in rp["entries"]}
            pairs = harville_forecast_probabilities(win_probs, top_n=5)
            st.subheader(f"Most likely forecast/quinella combinations -- Race {race_choice}")
            st.dataframe(
                [{"1st": p["first"], "2nd": p["second"], "Probability": f"{p['probability']*100:.1f}%"}
                 for p in pairs],
                use_container_width=True, hide_index=True,
            )
            st.caption(
                "For quinella (order doesn't matter), combine each pair with its reverse. "
                "Computed via the Harville formula from win probabilities -- no forecast-specific "
                "market data is available pre-race, so treat this as a ranked shortlist, not a certainty."
            )

with tab_jackpot:
    if not race_plans:
        st.info("Load a race card first.")
    else:
        leg_choices = st.multiselect(
            "Select races as jackpot/treble legs (in order)",
            options=[rp["race_no"] for rp in race_plans],
            format_func=lambda n: f"Race {n}",
        )
        unit_cost = st.number_input("Unit stake per combination (Rs)", min_value=1.0, value=5.0, step=1.0)
        jackpot_budget = st.number_input("Budget reserved for this jackpot/treble (Rs)", min_value=0.0, value=200.0, step=50.0)
        if leg_choices:
            legs = [next(rp for rp in race_plans if rp["race_no"] == n) for n in leg_choices]
            legs = [{"race_no": l["race_no"], "entries": l["entries"]} for l in legs]
            plan = jackpot_leg_plan(legs, unit_cost=unit_cost, budget=jackpot_budget)
            st.subheader(f"{plan['combo_count']} combinations x Rs{plan['unit_cost']} = Rs{plan['total_cost']}")
            if not plan["fits_budget"]:
                st.error(plan["note"])
            for leg in plan["legs"]:
                names = ", ".join(f"{h['horse_name']} ({h['win_probability']*100:.0f}%)" for h in leg["horses"])
                st.write(f"**Race {leg['race_no']}** ({leg['mode']}): {names}")

with tab_connections:
    st.subheader(f"Official jockey/trainer strike rates -- {venue}")
    st.caption(
        "Season-to-date, as published by the club itself (not derived from our own thin sample). "
        "This is the 'fundamentals' layer: who's actually winning right now, regardless of what "
        "any single horse's rating says. Click 'Refresh jockey/trainer stats' in the sidebar to update."
    )
    col_j, col_t = st.columns(2)
    with col_j:
        st.markdown("**Jockeys**")
        jockeys = conn.execute(
            "SELECT name, wins, total_rides, win_pct FROM connection_stats "
            "WHERE venue=? AND role='jockey' ORDER BY win_pct DESC", (venue,),
        ).fetchall()
        if jockeys:
            st.dataframe(
                [{"Jockey": r["name"], "Wins": r["wins"], "Rides": r["total_rides"], "Win %": f"{r['win_pct']:.1f}%"}
                 for r in jockeys],
                use_container_width=True, hide_index=True, height=400,
            )
        else:
            st.info("No jockey stats loaded yet -- use 'Refresh jockey/trainer stats' in the sidebar.")
    with col_t:
        st.markdown("**Trainers**")
        trainers = conn.execute(
            "SELECT name, wins, total_rides, win_pct FROM connection_stats "
            "WHERE venue=? AND role='trainer' ORDER BY win_pct DESC", (venue,),
        ).fetchall()
        if trainers:
            st.dataframe(
                [{"Trainer": r["name"], "Wins": r["wins"], "Runners": r["total_rides"], "Win %": f"{r['win_pct']:.1f}%"}
                 for r in trainers],
                use_container_width=True, hide_index=True, height=400,
            )
        else:
            st.info("No trainer stats loaded yet -- use 'Refresh jockey/trainer stats' in the sidebar.")

    st.divider()
    st.subheader("Owner / breeder strike rates (self-derived, all venues pooled)")
    st.caption(
        "Neither club publishes a per-venue breeder leaderboard, and only RWITC publishes an owner one "
        "(see Money Leaders below) -- so this is computed from our own backfilled results archive instead, "
        "pooled across venues since ownership/breeding operations aren't venue-local. Only as good as the "
        "seasons we've backfilled so far; a low 'Starts' count means treat the % as noise, not signal."
    )
    col_o, col_b = st.columns(2)
    with col_o:
        st.markdown("**Owners**")
        owners = conn.execute(
            """SELECT r.owner name, COUNT(*) starts, SUM(CASE WHEN res.finish_position=1 THEN 1 ELSE 0 END) wins
               FROM runs r JOIN results res ON res.run_id=r.id
               WHERE r.owner IS NOT NULL AND r.scratched=0
               GROUP BY r.owner HAVING starts >= 10 ORDER BY 1.0*wins/starts DESC LIMIT 25"""
        ).fetchall()
        if owners:
            st.dataframe(
                [{"Owner": r["name"], "Starts": r["starts"], "Wins": r["wins"],
                  "Win %": f"{100*r['wins']/r['starts']:.1f}%"} for r in owners],
                use_container_width=True, hide_index=True, height=400,
            )
        else:
            st.info("Not enough results archived yet to rank owners (need 10+ starts).")
    with col_b:
        st.markdown("**Breeders / studs**")
        breeders = conn.execute(
            """SELECT COALESCE(h.breeder, h.stud) name, COUNT(*) starts,
                      SUM(CASE WHEN res.finish_position=1 THEN 1 ELSE 0 END) wins
               FROM runs r JOIN results res ON res.run_id=r.id JOIN horses h ON h.id=r.horse_id
               WHERE COALESCE(h.breeder, h.stud) IS NOT NULL AND r.scratched=0
               GROUP BY name HAVING starts >= 10 ORDER BY 1.0*wins/starts DESC LIMIT 25"""
        ).fetchall()
        if breeders:
            st.dataframe(
                [{"Breeder/Stud": r["name"], "Starts": r["starts"], "Wins": r["wins"],
                  "Win %": f"{100*r['wins']/r['starts']:.1f}%"} for r in breeders],
                use_container_width=True, hide_index=True, height=400,
            )
        else:
            st.info("Not enough results archived yet to rank breeders (need 10+ starts).")

    if venue in ("Pune", "Mumbai"):
        st.divider()
        st.subheader("RWITC Money Leaders (official, reference only -- not fed into scoring)")
        st.caption(
            "Season money leaders as published by RWITC itself. Not filtered by venue (Pune/Mumbai share "
            "the same jockey/trainer/owner pool) and not used in the composite score, to avoid mixing "
            "an earnings-based ranking with the win-rate-based signals above -- shown here purely as "
            "a read on which operations are the established, well-resourced ones this season."
        )
        if st.button("Refresh money leaders", key="refresh_money_leaders"):
            st.session_state["money_leaders"] = rwitc.parse_money_leaders(
                rwitc.fetch_money_leaders_html(use_cache=False)
            )
        leaders = st.session_state.get("money_leaders")
        if leaders is None:
            try:
                leaders = rwitc.parse_money_leaders(rwitc.fetch_money_leaders_html(use_cache=True))
                st.session_state["money_leaders"] = leaders
            except Exception as e:
                st.error(f"Could not load money leaders: {e}")
                leaders = None
        if leaders:
            ml_cols = st.columns(2)
            for i, category in enumerate(["owners", "trainers"]):
                with ml_cols[i]:
                    st.markdown(f"**{category.capitalize()}**")
                    st.dataframe(
                        [{"Name": r["name"], "Starts": r["starts"], "Wins": r["wins"], "Winnings (Rs)": r["winnings"]}
                         for r in leaders[category]],
                        use_container_width=True, hide_index=True,
                    )

    if race_plans:
        st.divider()
        st.subheader("Sponsor-tie flags on today's card")
        flagged = [
            (rp["race_no"], e) for rp in race_plans for e in rp["entries"] if e["owner_sponsor_flag"]
        ]
        if flagged:
            for race_no, e in flagged:
                st.write(f"Race {race_no}: **{e['horse_name']}**, owned by {e['owner']} -- "
                         f"race sponsor/name overlaps with the owner's name.")
        else:
            st.caption("None on today's card -- this flag only fires when an owner's name plausibly "
                       "overlaps with the race's own sponsor/title text, which is uncommon on any given day.")

with tab_backtest:
    st.subheader("Model vs. market vs. reality")
    st.caption(
        "Replays every archived race with results and compares three predictors: this model's top pick, "
        "the tote favourite (the betting public's collective prediction -- the strongest verifiable "
        "benchmark, since tipster sites like racingpulse keep their selections behind a paywall and "
        "free tip blogs keep no checkable archive), and the top-rated horse. "
        "Caveats: jockey/trainer stats are current-season snapshots applied retroactively and "
        "owner/breeder rates derive from this same archive, so connection signals are flattered by "
        "lookahead; rating, form, and favourite numbers are point-in-time clean."
    )
    bt_venue = st.selectbox("Backtest venue", ["All venues", "Pune", "Mumbai", "Bangalore"])
    if st.button("Run backtest"):
        from scripts.backtest import benchmark as bt_benchmark, calibration as bt_calibration, \
            load_backtest_races, signal_patterns
        with st.spinner("Replaying archived races..."):
            bt_races = load_backtest_races(conn, None if bt_venue == "All venues" else bt_venue)
            st.session_state["bt"] = {
                "venue": bt_venue, "n": len(bt_races),
                "benchmark": bt_benchmark(bt_races),
                "calibration": bt_calibration(bt_races),
                "patterns": signal_patterns(bt_races),
            }
    bt = st.session_state.get("bt")
    if bt:
        b = bt["benchmark"]
        st.markdown(f"**{bt['n']} races replayed ({bt['venue']})**")
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Model top pick", f"{b['model_top_pick_win_rate']*100:.1f}%")
        c2.metric("Tote favourite", f"{b['favourite_win_rate']*100:.1f}%" if b["favourite_win_rate"] else "n/a")
        c3.metric("Top-rated horse", f"{b['top_rated_win_rate']*100:.1f}%")
        c4.metric("Random pick", f"{b['random_baseline']*100:.1f}%")
        st.caption(f"Model top-3 hit rate: {b['model_top3_rate']*100:.1f}%")

        ag, dg = b["model_agrees_with_market"], b["model_disagrees_with_market"]
        st.info(
            f"**The market-agreement pattern (most actionable finding):** when this model's pick was ALSO "
            f"the tote favourite ({ag['n']} races), it won {ag['win_rate']*100:.0f}% of the time. When the "
            f"model disagreed with the market ({dg['n']} races), the model won only "
            f"{dg['model_win_rate']*100:.0f}% while the favourite still won {dg['favourite_win_rate']*100:.0f}%. "
            f"Practical takeaway: at the track, if the live odds board strongly disagrees with the pick here, "
            f"the market has historically been the one to trust -- size down or skip."
        )

        st.markdown("**Calibration** -- when the model says X%, how often does it actually happen?")
        st.dataframe(
            [{"Predicted": r["bucket"], "Runners": r["n_runners"], "Actual win rate": f"{r['actual_win_rate']*100:.1f}%"}
             for r in bt["calibration"]],
            use_container_width=True, hide_index=True,
        )

        p = bt["patterns"]
        st.markdown("**Where winners actually come from**")
        pc1, pc2, pc3 = st.columns(3)
        with pc1:
            st.caption("By official-rating rank (5 = 5th or worse)")
            st.dataframe(
                [{"Rank": k, "Win rate": f"{v['win_rate']*100:.1f}%", "n": v["n"]}
                 for k, v in p["by_rating_rank"].items()],
                use_container_width=True, hide_index=True,
            )
        with pc2:
            st.caption("By jockey season strike tier")
            st.dataframe(
                [{"Tier": k, "Win rate": f"{v['win_rate']*100:.1f}%", "n": v["n"]}
                 for k, v in p["by_jockey_strike_tier"].items()],
                use_container_width=True, hide_index=True,
            )
        with pc3:
            st.caption("By recent-form tier")
            st.dataframe(
                [{"Tier": k, "Win rate": f"{v['win_rate']*100:.1f}%", "n": v["n"]}
                 for k, v in p["by_form_tier"].items()],
                use_container_width=True, hide_index=True,
            )
        st.caption(
            "These patterns are already folded into the model: weights were re-tuned on this archive "
            "(rating 0.35→0.25, form 0.15→0.25, sharper probability spread), which lifted the "
            "backtested top-pick hit rate from 38.7% to 40.1% on Pune. Rerun after each race weekend "
            "as the archive grows."
        )

with tab_bankroll:
    st.subheader("Log a bet")
    with st.form("log_bet"):
        c1, c2, c3 = st.columns(3)
        with c1:
            bet_race = st.number_input("Race number", min_value=1, step=1)
            bet_type = st.selectbox("Bet type", ["win", "place", "forecast", "quinella", "jackpot"])
        with c2:
            selection = st.text_input("Selection (horse or combo)")
            stake = st.number_input("Stake (Rs)", min_value=0.0, step=10.0)
        with c3:
            predicted_prob = st.number_input("Model win probability", min_value=0.0, max_value=1.0, step=0.01)
            notes = st.text_input("Notes (optional)")
        submitted = st.form_submit_button("Log bet")
        if submitted and selection:
            conn.execute(
                """INSERT INTO bankroll_log (race_date, venue, budget, goal, race_no, bet_type, selection, stake,
                                              predicted_probability, notes)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (date_str, venue, budget, goal, bet_race, bet_type, selection, stake, predicted_prob, notes),
            )
            conn.commit()
            st.success("Logged.")

    st.divider()
    st.subheader("Today's log & stop rules")
    logs = conn.execute(
        "SELECT * FROM bankroll_log WHERE race_date=? AND venue=? ORDER BY id", (date_str, venue),
    ).fetchall()
    cumulative_staked = sum(l["stake"] for l in logs)
    cumulative_pnl = sum((l["payout"] or 0) - l["stake"] for l in logs if l["outcome"] != "pending")
    rules = stop_rules(budget, goal, cumulative_staked, cumulative_pnl)
    st.info(rules["message"])

    if logs:
        for l in logs:
            cols = st.columns([1, 1, 2, 1, 1, 1, 2])
            cols[0].write(f"R{l['race_no'] if l['race_no'] else '-'}")
            cols[1].write(l["bet_type"])
            cols[2].write(l["selection"])
            cols[3].write(f"Rs{l['stake']:.0f}")
            outcome = cols[4].selectbox(
                "Outcome", ["pending", "win", "lose"], key=f"outcome_{l['id']}",
                index=["pending", "win", "lose"].index(l["outcome"] or "pending"),
            )
            payout = cols[5].number_input("Payout", min_value=0.0, value=l["payout"] or 0.0, key=f"payout_{l['id']}")
            if cols[6].button("Save", key=f"save_{l['id']}"):
                conn.execute(
                    "UPDATE bankroll_log SET outcome=?, payout=? WHERE id=?", (outcome, payout, l["id"]),
                )
                conn.commit()
                st.rerun()

    st.divider()
    st.subheader("Model calibration (across all logged history)")
    settled = conn.execute(
        "SELECT predicted_probability, outcome FROM bankroll_log WHERE outcome != 'pending' AND predicted_probability IS NOT NULL"
    ).fetchall()
    if len(settled) < 5:
        st.caption(f"Only {len(settled)} settled bets logged so far -- calibration needs more history to be meaningful. Keep logging outcomes as the season goes on.")
    else:
        buckets = {}
        for s in settled:
            bucket = round(s["predicted_probability"], 1)
            buckets.setdefault(bucket, {"n": 0, "wins": 0})
            buckets[bucket]["n"] += 1
            if s["outcome"] == "win":
                buckets[bucket]["wins"] += 1
        st.dataframe(
            [{"Predicted prob. bucket": f"{k*100:.0f}%", "Actual win rate": f"{v['wins']/v['n']*100:.0f}%", "N": v["n"]}
             for k, v in sorted(buckets.items())],
            use_container_width=True, hide_index=True,
        )
        st.caption("If actual win rate consistently tracks below the predicted bucket, the model is overconfident -- weight it accordingly.")

    st.divider()
    all_time = conn.execute(
        "SELECT race_date, SUM(stake) staked, SUM(CASE WHEN outcome != 'pending' THEN payout - stake ELSE 0 END) pnl "
        "FROM bankroll_log GROUP BY race_date ORDER BY race_date"
    ).fetchall()
    if all_time:
        st.subheader("Season running P&L")
        st.dataframe(
            [{"Date": r["race_date"], "Staked": r["staked"], "P&L": round(r["pnl"], 2)} for r in all_time],
            use_container_width=True, hide_index=True,
        )
        st.metric("Total P&L to date", f"Rs{sum(r['pnl'] for r in all_time):.0f}")
