import sys
from datetime import date, timedelta
from pathlib import Path

import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parent))

from db.schema import get_connection, init_db
from db.ingest import (
    load_market_odds, odds_age_minutes, save_parlay, settle_parlays,
    store_intl_racecard, store_intl_results, store_market_odds, store_racecard,
    store_raceresult,
)
from scrapers import rwitc, btc, hkjc, odds_import, tabnz, racingaustralia as ra
from models.rating_engine import compute_composite_scores
from models import parlay as parlay_engine
from models.betslip import (
    DEFAULT_MIN_EDGE_AT_PLACEMENT, build_slip, validate_against_live,
)
from models.odds import margin_percent, overround
from models.staking import (
    build_win_place_plan, harville_forecast_probabilities, jackpot_leg_plan, stop_rules,
    build_place_shortlist,
)
from ui.theme import inject_theme
from ui import components as ui

# layout="centered" (not "wide"): the primary use is a phone at the track, and
# "wide" forces a desktop-width grid that squeezes content into a column on
# small screens. Detail tabs still scroll fine inside the centered container.
st.set_page_config(
    page_title="Race Predictor -- India / Australia / Hong Kong",
    page_icon="🐎",
    layout="centered",
    initial_sidebar_state="collapsed",
)
inject_theme()
init_db()
conn = get_connection()

# Both RWITC (Pune/Mumbai -- same site, auto-detects venue by date) and BTC
# (Bangalore, separate site) expose the same fetch_racecard_html /
# fetch_raceresult_html / parse_racecard / parse_raceresult interface, so
# the rest of the app doesn't need to know which source it's talking to.
SCRAPER_BY_VENUE = {"Pune": rwitc, "Mumbai": rwitc, "Bangalore": btc}

# The three circuits differ in one way that matters more than any other: only
# Australia and Hong Kong publish a market price, which is what makes expected
# value -- and therefore the parlay engine -- possible at all.
CIRCUITS = {
    "India": {
        "venues": ["Pune", "Mumbai", "Bangalore"],
        "has_market": False,
        "note": "RWITC and BTC publish no pre-race odds, so India gets ranked picks but no EV or parlays.",
    },
    "Australia": {
        "venues": [],  # discovered from Racing Australia's calendar per date
        "has_market": True,
        "note": "Racing Australia publishes fields, ratings, form and starting prices. Races every day of the year.",
    },
    "Hong Kong": {
        "venues": ["Happy Valley", "Sha Tin"],
        "has_market": True,
        "note": "HKJC publishes everything, but only in season (September to mid-July).",
    },
}


def next_saturday(d: date) -> date:
    days_ahead = (5 - d.weekday()) % 7  # Saturday = 5
    return d + timedelta(days=days_ahead or 7 if d.weekday() == 5 else days_ahead)


st.title("🐎 Race Predictor")
# The full responsible-wagering note stays one tap away rather than consuming the
# whole first screen on a phone -- the short line carries the essential warning,
# the expander keeps the complete text (and the negative-EV parlay caveat) intact.
st.caption("⚠️ Fun-money analysis, not a winner-picker. Racing is genuinely unpredictable.")
with st.expander("Read this before you bet"):
    st.markdown(
        "These are a transparent, data-backed shortlist -- **not a guarantee**. "
        "Only stake what you've set as your budget for the day, and check "
        "**Bankroll & Calibration** regularly to see how the model's confidence has "
        "actually tracked against real outcomes.\n\n"
        "A pick is only a *bet* if the price is right: fair odds = (100 ÷ win%) − 1, "
        "and you want the board paying more than that. The **Race Day** tab checks "
        "this for you.\n\n"
        "On the Australian and Hong Kong circuits, read the **Daily Parlays** "
        "expected-value figure before backing anything: a multi with negative EV "
        "loses money over time however good the horses look."
    )

with st.sidebar:
    st.header("Today's session")
    circuit = st.radio("Circuit", list(CIRCUITS), horizontal=True)
    st.caption(CIRCUITS[circuit]["note"])

    if "race_date_input" not in st.session_state:
        st.session_state["race_date_input"] = next_saturday(date.today())
    col_today, col_sat = st.columns(2)
    if col_today.button("Today", use_container_width=True):
        st.session_state["race_date_input"] = date.today()
    if col_sat.button("Next Saturday", use_container_width=True):
        st.session_state["race_date_input"] = next_saturday(date.today())
    race_date = st.date_input("Race date", key="race_date_input")
    date_str = race_date.strftime("%Y-%m-%d")

    # Venues already loaded into the DB for this circuit/date, so the selector
    # reflects what is actually available rather than a hard-coded list.
    loaded_venues = [r["venue"] for r in conn.execute(
        "SELECT DISTINCT venue FROM races WHERE race_date=? AND circuit=? ORDER BY venue",
        (date_str, circuit),
    ).fetchall()]

    if circuit == "India":
        venue = st.selectbox("Venue", CIRCUITS["India"]["venues"])
    else:
        options = loaded_venues or CIRCUITS[circuit]["venues"]
        venue = st.selectbox("Venue (loaded)", options) if options else None
        if not options:
            st.info("No meeting loaded for this date yet -- load one below.")

    budget = st.number_input("Budget for today (fun money, Rs)", min_value=0.0, value=1000.0, step=100.0)
    goal = st.number_input("Goal / mental target (Rs profit)", min_value=0.0, value=500.0, step=100.0)

    st.divider()

    if circuit == "India":
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

    elif circuit == "Australia":
        st.subheader("Load Australian meetings")
        st.caption(
            "Australia races somewhere every day, usually at 5-10 tracks at once. "
            "Pick the meetings you actually want -- loading all of them is slow and "
            "most country cards are small fields with thin form."
        )
        states = st.multiselect("States", ra.STATES, default=["NSW", "VIC"])
        if st.button("Find meetings", use_container_width=True):
            try:
                found = ra.meetings_for_date(date_str, states, use_cache=False)
                st.session_state["au_meetings"] = found
                if not found:
                    st.warning(f"No meetings found for {date_str} in {', '.join(states)}.")
            except Exception as e:
                st.error(f"Could not read the calendar: {e}")

        meetings = st.session_state.get("au_meetings", [])
        if meetings:
            labels = {f"{m['state']} -- {m['venue']}": m for m in meetings}
            chosen = st.multiselect("Meetings", list(labels))
            c_load, c_res = st.columns(2)
            if c_load.button("Load fields", use_container_width=True, disabled=not chosen):
                total = 0
                for label in chosen:
                    m = labels[label]
                    try:
                        races = ra.parse_racecard(ra.fetch_form_html(m["key"], use_cache=False))
                        total += store_intl_racecard(conn, date_str, m["venue"], "Australia",
                                                     races, country="AU", source_key=m["key"])
                    except Exception as e:
                        st.error(f"{m['venue']}: {e}")
                st.success(f"Loaded {total} runners. Now add odds in the Odds tab.")
                st.rerun()
            if c_res.button("Load results", use_container_width=True, disabled=not chosen):
                total = 0
                for label in chosen:
                    m = labels[label]
                    try:
                        races = ra.parse_raceresult(ra.fetch_results_html(m["key"], use_cache=False))
                        total += store_intl_results(conn, date_str, m["venue"], "Australia",
                                                    races, country="AU")
                    except Exception as e:
                        st.error(f"{m['venue']}: {e}")
                st.success(f"Stored {total} result rows (starting prices included).")
                st.rerun()

    elif circuit == "Hong Kong":
        st.subheader("Load Hong Kong meeting")
        season = hkjc.season_status()
        (st.success if season["in_season"] else st.info)(season["message"])
        course = st.selectbox("Racecourse", ["HV", "ST"],
                              format_func=lambda c: f"{c} -- {hkjc.RACECOURSES[c]}")
        c_card, c_res = st.columns(2)
        if c_card.button("Load card", use_container_width=True):
            try:
                races = hkjc.fetch_meeting_card(date_str, course, use_cache=False)
                if not races:
                    st.warning("No card published for that date. HKJC posts a card a few days "
                               "before a meeting and removes it once the meeting has run.")
                else:
                    n = store_intl_racecard(conn, date_str, hkjc.RACECOURSES[course], "Hong Kong",
                                            races, country="HK")
                    live = hkjc.win_odds_from_card(races)
                    if live:
                        store_market_odds(conn, date_str, hkjc.RACECOURSES[course], live, source="live")
                    st.success(f"Loaded {n} runners, {len(live)} live prices.")
                    st.rerun()
            except Exception as e:
                st.error(f"Fetch failed: {e}")
        if c_res.button("Load results", use_container_width=True):
            try:
                races = hkjc.fetch_meeting_results(date_str, course, use_cache=False)
                if not races:
                    st.warning("No local results for that date.")
                else:
                    n = store_intl_results(conn, date_str, hkjc.RACECOURSES[course], "Hong Kong",
                                           races, country="HK")
                    st.success(f"Stored {n} result rows across {len(races)} races.")
                    st.rerun()
            except Exception as e:
                st.error(f"Fetch failed: {e}")

    st.divider()
    st.subheader("Fundamentals")
    st.caption("Official season-to-date jockey/trainer strike rates -- feeds the connections signal in every pick.")
    if circuit != "India":
        # Trackwork (indiarace) and mock races (racingpulse) are Indian sources,
        # and neither Racing Australia nor HKJC publishes a strike-rate table in
        # the form this app consumes -- for those circuits the same signal is
        # derived from our own results archive instead, via the backfill script.
        st.caption(
            f"These feeds are India-only. On the {circuit} circuit, jockey and trainer strike "
            f"rates build up from the results you load, so archive a few weeks of past meetings "
            f"first -- `python -m scripts.daily --backfill 21` -- to give the connections signal "
            f"something to work with."
        )
    elif st.button("Fetch trackwork + mock races", use_container_width=True):
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
    if circuit == "India" and st.button("Refresh jockey/trainer stats", use_container_width=True):
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


def get_slate(d: str, c: str) -> list[dict]:
    """Every race of a circuit on a date, across ALL its venues, with model
    scores and whatever market prices we hold.

    Separate from get_race_plans because a multi is built ACROSS meetings --
    the whole point of requiring legs from different races is that they be
    independent, and on the Australian circuit the best three independent
    value bets on a given day are rarely at one track."""
    rows = conn.execute(
        """SELECT id, venue, race_number, race_name, class_code, distance_m, race_time_local
           FROM races WHERE race_date=? AND circuit=? ORDER BY venue, race_number""",
        (d, c),
    ).fetchall()
    slate = []
    for row in rows:
        entries = compute_composite_scores(conn, row["id"])
        if not entries:
            continue
        slate.append({
            "race_id": row["id"], "venue": row["venue"], "race_no": row["race_number"],
            "race_name": row["race_name"], "race_time": row["race_time_local"],
            "class_code": row["class_code"], "distance_m": row["distance_m"],
            "circuit": c, "field_size": len(entries), "entries": entries,
            "odds": load_market_odds(conn, row["id"]),
            "odds_age_min": odds_age_minutes(conn, row["id"]),
        })
    return slate


# Beyond this, a stored price is old enough that the market has probably moved
# and any edge measured against it is fiction rather than opportunity.
STALE_ODDS_MINUTES = 90


race_plans = get_race_plans(date_str, venue) if venue else []
slate = get_slate(date_str, circuit)

# Tab order is deliberate: "Race Day" is the phone-at-the-track view and comes
# first. Everything after it is homework you do at home on a laptop (backtest,
# calibration, connections) -- it should not compete for thumb space.
(tab_raceday, tab_today, tab_parlay, tab_odds, tab_followup, tab_place, tab_forecast,
 tab_jackpot, tab_connections, tab_backtest, tab_bankroll) = st.tabs(
    ["Race Day", "Today's Picks", "Daily Parlays", "Odds", "Followup", "Place Bets",
     "Forecast / Quinella", "Jackpot Planner", "Connections", "Backtest", "Bankroll & Calibration"]
)

with tab_raceday:
    if not race_plans:
        st.info("No race card loaded. Open the sidebar (top-left ») and use **Fetch live**.")
    else:
        # One race at a time. Chips instead of 10 stacked expanders -- at the
        # track you care about the race that's about to run, not the whole card.
        race_nos = [rp["race_no"] for rp in race_plans]
        chosen = st.radio(
            "Race", race_nos, horizontal=True, label_visibility="collapsed",
            format_func=lambda n: f"R{n}",
        )
        rp = next(r for r in race_plans if r["race_no"] == chosen)
        entries = rp["entries"]

        st.markdown(
            f'<div class="rp-label">Race {rp["race_no"]} &middot; '
            f'{ui._esc(rp.get("race_name") or "")}</div>',
            unsafe_allow_html=True,
        )

        if not entries:
            st.info("No runners parsed for this race.")
        else:
            top = entries[0]
            field = len(entries)
            baseline = 1.0 / field
            edge_pp = (top["win_probability"] - baseline) * 100
            fair = ui.fair_odds(top["win_probability"])

            # Verdict = model's own confidence, before any market price is known.
            has_edge = edge_pp >= 5
            st.markdown(
                ui.verdict_card(
                    state="bet" if has_edge else "skip",
                    tag="Model pick" if has_edge else "No clear edge",
                    horse=top["horse_name"],
                    subtitle=(
                        f'{ui._esc(top.get("jockey") or "?")} &middot; '
                        f'{ui._esc(top.get("trainer") or "?")}'
                    ),
                    stats=[
                        {"k": "Win chance", "v": f'{top["win_probability"]*100:.0f}%'},
                        {"k": "Fair odds", "v": f"{fair:.1f}/1" if fair else "-", "hl": True},
                        {"k": "Edge", "v": f"{edge_pp:+.0f}pp"},
                    ],
                ),
                unsafe_allow_html=True,
            )

            # The decision tool. Neither club publishes pre-race odds, so the
            # board price is the one input only the user can supply -- typing it
            # here closes the loop between model and counter.
            st.markdown(ui.section_label("Check the board price"), unsafe_allow_html=True)
            board = st.number_input(
                f"Odds showing for {top['horse_name']} (e.g. 3 means 3/1)",
                min_value=0.0, step=0.25, value=0.0, key=f"board_{rp['race_id']}",
            )
            if board > 0:
                v = ui.value_verdict(top["win_probability"], board)
                if v["verdict"] == "BET":
                    st.success(
                        f"**BET** — {board:.2f}/1 beats fair ({v['fair']:.2f}/1) with room to spare. "
                        f"You're getting {v['edge_pct']:+.0f}% over break-even."
                    )
                elif v["verdict"] == "THIN":
                    st.warning(
                        f"**THIN** — {board:.2f}/1 is above fair ({v['fair']:.2f}/1) but under the "
                        f"{v['required']:.2f}/1 you want as cushion for tote rake. Small stake or pass."
                    )
                else:
                    st.error(
                        f"**SKIP** — {board:.2f}/1 is below fair ({v['fair']:.2f}/1). "
                        f"Model says this horse wins {top['win_probability']*100:.0f}% of the time; "
                        f"at this price you lose money long-term even when it wins its share."
                    )
            else:
                st.caption(
                    f"Enter the tote board price to check it. Rule: bet only above "
                    f"**{ui.fair_odds(top['win_probability']):.2f}/1**, ideally "
                    f"**{ui.fair_odds(top['win_probability'])*1.25:.2f}/1**+."
                )

            st.markdown(ui.section_label(f"Full field · {field} runners"), unsafe_allow_html=True)
            st.markdown(ui.runner_rows(entries), unsafe_allow_html=True)
            st.caption("🔥 strong recent gallops · ⚠️ concerning work · 🔗 owner ties to race sponsor")

            finishers = conn.execute(
                """SELECT h.name, res.finish_position FROM runs r
                   JOIN horses h ON h.id = r.horse_id JOIN results res ON res.run_id = r.id
                   WHERE r.race_id = ? AND res.finish_position IS NOT NULL
                   ORDER BY res.finish_position LIMIT 3""",
                (rp["race_id"],),
            ).fetchall()
            if finishers:
                won = finishers[0]["name"].upper() == top["horse_name"].upper()
                placed = any(f["name"].upper() == top["horse_name"].upper() for f in finishers)
                podium = " &middot; ".join(f'{f["finish_position"]}. {ui._esc(f["name"])}' for f in finishers)
                if won:
                    msg, outcome = f"<b>{ui._esc(top['horse_name'])} won.</b><br>{podium}", "won"
                elif placed:
                    msg, outcome = f"<b>{ui._esc(top['horse_name'])} placed.</b><br>{podium}", "neutral"
                else:
                    msg, outcome = f"Pick unplaced. Result: {podium}", "lost"
                st.markdown(ui.result_banner(outcome=outcome, text=msg), unsafe_allow_html=True)

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


# ===========================================================================
# Odds -- nothing downstream works without a market price
# ===========================================================================
with tab_odds:
    st.subheader("Market odds")
    if not CIRCUITS[circuit]["has_market"]:
        st.info(
            "The Indian clubs publish no pre-race odds at all -- prices only appear after the "
            "race, on the results page. That is why the India circuit has ranked picks but no "
            "expected value and no parlays. Switch to Australia or Hong Kong to use those."
        )
    elif not slate:
        st.info("Load a meeting first (sidebar).")
    else:
        priced = sum(1 for r in slate if r["odds"])
        st.caption(
            f"{priced} of {len(slate)} loaded races have prices. Every race without a price is "
            f"invisible to the parlay engine -- there is nothing to measure an edge against."
        )

        if circuit == "Australia":
            st.markdown("#### Fetch live prices automatically (NZ TAB)")
            st.caption(
                "NZ TAB books all the major Australian meetings and, unlike tab.com.au, "
                "punters, racenet, Sportsbet, PointsBet and Betfair, it answers from India. "
                "This pulls fixed win/place odds for every runner in the meetings you have "
                "loaded -- which is also what gets a race past the 80% price-coverage guard. "
                "**These are NZ TAB's prices, not your bookmaker's:** use them to find races "
                "worth a look, then confirm the number where you actually bet. Anything you "
                "paste below overrides them."
            )
            venues_loaded = sorted({r["venue"] for r in slate})
            if st.button("Fetch live odds for loaded meetings", use_container_width=True):
                try:
                    with st.spinner("Fetching from NZ TAB..."):
                        by_venue = tabnz.odds_for_date(date_str, venues=venues_loaded)
                    if not by_venue:
                        st.warning(
                            "No live races found for these meetings. Either they have all run "
                            "(settled races keep stale pre-scratching prices, so they're skipped "
                            "on purpose) or the card isn't open for betting yet."
                        )
                    total, unmatched_total = 0, 0
                    for tab_venue, races in by_venue.items():
                        local = next((v for v in venues_loaded
                                      if tabnz.venues_match(tab_venue, v)), None)
                        if not local:
                            continue
                        rows = tabnz.to_market_rows(races)
                        res = store_market_odds(conn, date_str, local, rows, source="tabnz")
                        total += res["matched"]
                        unmatched_total += len(res["unmatched"])
                        st.write(f"**{local}** -- priced {res['matched']} runners "
                                 f"across {len(races)} live race(s)")
                    if total:
                        st.success(f"Stored prices for {total} runners.")
                        if unmatched_total:
                            st.caption(
                                f"{unmatched_total} runner name(s) didn't match the loaded field -- "
                                f"usually late scratchings or a spelling difference between "
                                f"Racing Australia and NZ TAB."
                            )
                        st.rerun()
                except Exception as e:
                    st.error(f"NZ TAB fetch failed: {e}")
            st.divider()

        st.markdown("#### Paste prices from your bookmaker")
        st.caption(
            "The reliable path. Copy the win (and place, if shown) prices off any screen and paste "
            "them here -- one runner per line. Recognised shapes: `MAGIC MOMENT 3.40`, "
            "`7. Magic Moment $3.40 $1.55`, `Magic Moment 5/2`. Header and junk lines are ignored."
        )
        race_options = {f"{r['venue']} R{r['race_no']} -- {r['race_name'] or ''}"[:60]: r for r in slate}
        pick = st.selectbox("Race", list(race_options))
        target = race_options[pick]
        text = st.text_area("Prices", height=160, placeholder="HORSE NAME 3.40 1.55")
        c1, c2 = st.columns([1, 3])
        market_mode = c2.radio("A single number per line is a...", ["win price", "place price"],
                               horizontal=True)
        if c1.button("Save prices", use_container_width=True) and text.strip():
            rows = odds_import.paste_odds(
                text, target["race_no"],
                default_market="win" if market_mode == "win price" else "place")
            result = store_market_odds(conn, date_str, target["venue"], rows, source="manual")
            if result["matched"]:
                st.success(f"Stored prices for {result['matched']} runners.")
            if result["unmatched"]:
                st.warning(
                    "These names did not match any runner in that race, so they were NOT stored "
                    "(check spelling against the field): "
                    + ", ".join(f"{u['horse_name']} ({u['reason']})" for u in result["unmatched"][:8])
                )
            if result["matched"]:
                st.rerun()

        st.divider()
        st.markdown("#### What's stored for this race")
        held = target["odds"]
        if held:
            names = [e["horse_name"] for e in target["entries"]]
            win_prices = [held.get(n, {}).get("win") for n in names]
            live = [p for p in win_prices if p]
            if len(live) >= 3:
                st.caption(
                    f"Book overround **{overround(live):.3f}** -- the market keeps about "
                    f"**{margin_percent(live):.1f}%** of turnover on this race. A fair book would be 1.000. "
                    f"You start every bet that far behind."
                )
            st.dataframe(
                [{"Horse": n,
                  "Win": held.get(n, {}).get("win") or "-",
                  "Place": held.get(n, {}).get("place") or "-",
                  "Model win %": f"{e['win_probability'] * 100:.1f}%"}
                 for n, e in zip(names, target["entries"])],
                use_container_width=True, hide_index=True,
            )
        else:
            st.info("No prices stored for this race yet.")

        with st.expander("Automatic odds via an API you already have access to"):
            status = odds_import.api_config_status()
            st.markdown(
                "**Never paste an API key into a chat window or a source file.** Put it in a "
                "`.env` file in the project root -- `.gitignore` already covers it -- and this app "
                "reads it from there. The variables it looks for:\n\n"
                "```\nODDS_API_URL=https://.../races/{race}/odds\n"
                "ODDS_API_TOKEN=your-key-here\n"
                "ODDS_API_TOKEN_HEADER=x-access-token\n"
                "ODDS_API_RUNNER_PATH=data.race.runners\n"
                "ODDS_API_NAME_FIELD=name\nODDS_API_WIN_FIELD=winOdds\n"
                "ODDS_API_PLACE_FIELD=placeOdds\n```"
            )
            st.json({k: v for k, v in status.items() if k != "token_length"} |
                    {"token_length": status["token_length"]})
            if not status["ready"]:
                st.caption("Not configured yet -- the paste box above needs nothing and works today.")
            else:
                ref = st.text_input("Race reference to substitute for {race}")
                if st.button("Fetch from API"):
                    try:
                        rows = odds_import.fetch_from_api(ref or None)
                        rows = [{**r, "race_no": target["race_no"]} for r in rows]
                        res = store_market_odds(conn, date_str, target["venue"], rows, source="api")
                        st.success(f"Stored {res['matched']} runners from the API.")
                        if res["unmatched"]:
                            st.warning(f"{len(res['unmatched'])} names did not match the field.")
                    except Exception as e:
                        st.error(str(e))


# ===========================================================================
# Daily parlays
# ===========================================================================
with tab_parlay:
    st.subheader("Daily parlays")

    if not CIRCUITS[circuit]["has_market"]:
        st.info(
            "Parlays need a price to test against, and the Indian clubs publish none pre-race. "
            "Use the Australia or Hong Kong circuit."
        )
    else:
        st.markdown(
            "**A multi is only worth placing when every leg is independently good value.** "
            "Each leg carries the bookmaker's margin, and combining legs multiplies those margins "
            "together -- three legs into a typical 16% book means betting into a 36% margin. No "
            "staking plan beats that. So the engine below qualifies each selection on its own "
            "first, and combines only what survives."
        )
        c1, c2, c3 = st.columns(3)
        bankroll = c1.number_input("Bankroll (Rs)", min_value=100.0, value=10000.0, step=500.0,
                                   help="Your whole betting bankroll, not today's budget. Stake sizes scale off this.")
        model_weight = c2.slider(
            "Weight on our model vs the market", 0.0, 1.0, 0.35, 0.05,
            help="The backtest found the market's favourite beat this model's pick when the two "
                 "disagreed, so the default leans on the market. Raising this makes the engine "
                 "bolder and, on the evidence, worse.",
        )
        target = c3.number_input("Daily profit target (Rs)", min_value=0.0, value=750.0, step=100.0)

        priced_races = [r for r in slate if r["odds"]]
        if not priced_races:
            st.warning(
                f"{len(slate)} races loaded but none has a price attached. Add prices in the Odds "
                f"tab -- without them there is no edge to measure and nothing to suggest."
            )
        else:
            stale = [r for r in priced_races
                     if (r.get("odds_age_min") or 0) > STALE_ODDS_MINUTES]
            if stale:
                oldest = max(r["odds_age_min"] for r in stale)
                st.error(
                    f"⏰ **Stale prices.** {len(stale)} of {len(priced_races)} priced races are "
                    f"working off odds captured up to {oldest / 60:.1f} hours ago. Markets move "
                    f"constantly, so any edge shown against those numbers may no longer exist -- "
                    f"re-fetch or re-paste before staking anything.",
                    icon="⚠️",
                )

            card = parlay_engine.daily_parlay_card(
                priced_races, bankroll=bankroll, model_weight=model_weight, circuit=circuit)

            st.info(card["verdict"])
            m1, m2, m3 = st.columns(3)
            m1.metric("Races priced", f"{len(priced_races)}/{len(slate)}")
            m2.metric("Value selections", len(card["legs"]))
            m3.metric("Multis clearing the bar", card["total_suggestions"])

            if card["singles"]:
                st.markdown("#### Singles first")
                st.caption(
                    "The same selections as straight win/place bets. A single on a value pick has "
                    "the identical edge to that leg inside a multi, with a fraction of the swing. "
                    "If the aim is a small regular return rather than a big day, this table is the "
                    "honest answer and everything below it is entertainment."
                )
                st.dataframe(
                    [{"Race": f"{s['venue']} R{s['race_no']}", "Selection": s["horse_name"],
                      "Bet": s["market"], "Price": s["decimal_odds"],
                      "Our fair price": s["fair_odds"],
                      "Our chance": f"{s['blended_probability'] * 100:.0f}%",
                      "Edge": f"{s['expected_value'] * 100:+.1f}%",
                      "Stake (Rs)": s["suggested_stake"],
                      "Returns (Rs)": s["potential_profit"]}
                     for s in card["singles"]],
                    use_container_width=True, hide_index=True,
                )

            for key, prof in card["profiles"].items():
                st.markdown(f"#### {prof['label']}")
                st.caption(prof["description"])
                if not prof["parlays"]:
                    st.caption("_Nothing today clears this profile's bar._")
                    continue
                for i, pl in enumerate(prof["parlays"]):
                    legs_txt = "  +  ".join(
                        f"**{l['horse_name']}** ({l['venue']} R{l['race_no']}, {l['market']} @ {l['decimal_odds']})"
                        for l in pl["legs"])
                    with st.container(border=True):
                        st.markdown(legs_txt)
                        k1, k2, k3, k4 = st.columns(4)
                        k1.metric("Combined odds", f"{pl['combined_odds']:.2f}")
                        k2.metric("Chance it lands", f"{pl['hit_probability'] * 100:.1f}%")
                        k3.metric("Edge", f"{pl['expected_value'] * 100:+.1f}%")
                        k4.metric("Kelly stake", f"Rs{pl['suggested_stake']:.0f}")
                        st.caption(
                            f"Fair combined price would be {pl['fair_combined_odds']:.2f}; you are being "
                            f"offered {pl['combined_odds']:.2f}. Margin given up across the legs: "
                            f"{pl['margin_drag'] * 100:.0f}%. At the Kelly stake this returns "
                            f"Rs{pl['potential_profit']:.0f} when it lands."
                        )
                        feas = parlay_engine.target_feasibility(pl, target, bankroll)
                        if feas.get("achievable"):
                            warn = ("  ⚠️ that is "
                                    f"{feas['stake_vs_kelly']}x the Kelly stake -- above Kelly you are "
                                    "growing risk faster than return"
                                    if feas.get("exceeds_kelly") else
                                    "  ✅ that sits inside the Kelly stake")
                            st.caption(f"To clear Rs{target:.0f}: stake Rs{feas['required_stake']:.0f}.{warn}")
                            st.caption(feas["note"])
                        outlook = parlay_engine.expected_daily_outcome(pl, pl["suggested_stake"], days=30)
                        st.caption(
                            f"Backed every day for a month at the Kelly stake: about "
                            f"{outlook['expected_hits']:.0f} hits, expected P&L "
                            f"Rs{outlook['expected_pnl']:+.0f}, and a typical longest losing run of "
                            f"{outlook['typical_longest_losing_run']} days. {outlook['verdict']}."
                        )
                        if st.button("Save to followup", key=f"save_{key}_{i}"):
                            save_parlay(conn, date_str, circuit, pl,
                                        stake=pl["suggested_stake"], placed=False,
                                        notes=prof["label"])
                            st.success("Saved -- settle it in the Followup tab once the races have run.")

            if card["singles"]:
                st.divider()
                st.markdown("#### 🎫 Bet slip -- what to place, and the price to refuse below")
                st.caption(
                    "The edge below was measured at the price in the **Found at** column. If the "
                    "book you actually bet with is showing less than **Min price**, the edge does "
                    "not exist there -- skip it. Nothing is lost by skipping; the loss comes from "
                    "placing anyway. Take this to your bookmaker, check the price, and only bet "
                    "the rows that still qualify."
                )
                slip = build_slip(card["singles"], bankroll)
                st.dataframe(
                    [{"Race": f"{b.venue} R{b.race_no}", "Selection": b.horse_name,
                      "Bet": b.market, "Stake (Rs)": b.stake,
                      "Found at": b.reference_odds,
                      "Min price": b.min_acceptable_odds,
                      "Our chance": f"{b.probability * 100:.0f}%",
                      "Edge if you get it": f"{b.expected_value_at_reference * 100:+.1f}%"}
                     for b in slip],
                    use_container_width=True, hide_index=True,
                )
                st.caption(
                    f"Total if every row qualifies: Rs{sum(b.stake for b in slip):.0f}. "
                    f"**Min price** is the shortest odds at which the bet still clears a "
                    f"{DEFAULT_MIN_EDGE_AT_PLACEMENT * 100:.0f}% edge -- below it you are paying "
                    f"the bookmaker for the privilege of being right."
                )
                with st.expander("Check a price before you bet"):
                    st.caption(
                        "Type what your bookmaker is showing and this says go or no-go, "
                        "including whether the price has moved so far that the market probably "
                        "knows something the model doesn't."
                    )
                    names = {f"{b.venue} R{b.race_no} -- {b.horse_name} ({b.market})": b for b in slip}
                    which = st.selectbox("Bet", list(names), key="slip_check")
                    offered = st.number_input("Price your bookmaker is showing", min_value=1.0,
                                              value=float(names[which].min_acceptable_odds),
                                              step=0.05, key="slip_price")
                    verdict = validate_against_live(names[which], offered)
                    (st.success if verdict["place"] else st.error)(verdict["reason"])

            if card["rejected_races"]:
                with st.expander(f"{len(card['rejected_races'])} race(s) produced no value selection"):
                    st.caption(
                        "Worth reading rather than skipping: 'no edge on offer' is the normal, "
                        "correct outcome for most races, and a day with none at all is a day to sit out."
                    )
                    for r in card["rejected_races"]:
                        st.write(f"**{r['race']}** -- {r['reason']}")


# ===========================================================================
# Followup -- settle yesterday, and see whether any of this is working
# ===========================================================================
with tab_followup:
    st.subheader("Daily followup")
    st.caption(
        "The part that decides whether this is a side hustle or an expensive hobby. Settle each "
        "day's slips against the actual results, then read the calibration table: if the multis "
        "that were supposed to land 25% of the time land 12% of the time, the model is "
        "overconfident and every stake it suggests is too big."
    )

    c1, c2 = st.columns([1, 2])
    if c1.button("Settle this date", use_container_width=True):
        res = settle_parlays(conn, date_str)
        if res["settled"]:
            st.success(
                f"Settled {res['settled']} slips -- {res['won']} won, {res['lost']} lost. "
                f"Staked Rs{res['staked']:.0f}, returned Rs{res['returned']:.0f} "
                f"(P&L Rs{res['pnl']:+.0f})."
            )
        else:
            st.info(f"Nothing to settle. {res['pending']} slip(s) still waiting on results -- "
                    f"load the results for those meetings in the sidebar first.")
    c2.caption("Settlement needs results loaded for every leg's meeting. A slip whose races "
               "haven't run stays pending rather than being graded early.")

    rows = conn.execute(
        "SELECT * FROM parlays WHERE race_date=? ORDER BY id DESC", (date_str,)).fetchall()
    if not rows:
        st.info("No parlays saved for this date. Save one from the Daily Parlays tab.")
    else:
        for p in rows:
            legs = conn.execute(
                "SELECT * FROM parlay_legs WHERE parlay_id=? ORDER BY leg_no", (p["id"],)).fetchall()
            icon = {"won": "✅", "lost": "❌", "pending": "⏳"}.get(p["status"], "⏳")
            header = (f"{icon} {p['label'] or p['kind']} -- {p['combined_odds']:.2f} @ "
                      f"Rs{p['stake'] or 0:.0f} -- {p['status']}")
            with st.expander(header, expanded=p["status"] == "pending"):
                st.dataframe(
                    [{"Leg": l["leg_no"], "Race": f"{l['venue']} R{l['race_no']}",
                      "Selection": l["horse_name"], "Bet": l["market"],
                      "Price": l["decimal_odds"],
                      "Our chance": f"{(l['blended_probability'] or 0) * 100:.0f}%",
                      "Result": l["outcome"]} for l in legs],
                    use_container_width=True, hide_index=True,
                )
                if p["status"] == "won":
                    st.success(f"Returned Rs{p['payout'] or 0:.0f} "
                               f"(profit Rs{(p['payout'] or 0) - (p['stake'] or 0):+.0f})")
                elif p["status"] == "lost":
                    st.error(f"Lost Rs{p['stake'] or 0:.0f}")

    st.divider()
    st.markdown("#### Are the parlay probabilities honest?")
    settled = conn.execute(
        "SELECT hit_probability, status, stake, payout FROM parlays WHERE status IN ('won','lost')"
    ).fetchall()
    if len(settled) < 10:
        st.caption(
            f"Only {len(settled)} settled slips so far. Calibration needs 30-50 before it says "
            f"anything real -- until then, treat every number in this app as unproven and stake "
            f"accordingly."
        )
    else:
        buckets: dict = {}
        for s in settled:
            b = round((s["hit_probability"] or 0) * 4) / 4  # 0, 25%, 50%, 75%, 100%
            slot = buckets.setdefault(b, {"n": 0, "won": 0})
            slot["n"] += 1
            slot["won"] += 1 if s["status"] == "won" else 0
        st.dataframe(
            [{"We predicted": f"{k * 100:.0f}%", "Actually landed": f"{v['won'] / v['n'] * 100:.0f}%",
              "Slips": v["n"]} for k, v in sorted(buckets.items())],
            use_container_width=True, hide_index=True,
        )
        staked = sum(s["stake"] or 0 for s in settled)
        returned = sum(s["payout"] or 0 for s in settled)
        m1, m2, m3 = st.columns(3)
        m1.metric("Total staked", f"Rs{staked:.0f}")
        m2.metric("Total returned", f"Rs{returned:.0f}")
        m3.metric("P&L", f"Rs{returned - staked:+.0f}",
                  delta=f"{((returned / staked - 1) * 100) if staked else 0:+.1f}% ROI")
        if staked and returned < staked:
            st.warning(
                "Running at a loss. That is the expected outcome of betting into a margin without "
                "a real edge, and no change of selection method fixes it -- the thing to check is "
                "whether the predicted column above is consistently above the actual one."
            )
