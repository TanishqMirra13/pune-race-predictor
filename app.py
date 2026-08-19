import sys
from datetime import date, timedelta
from pathlib import Path

import streamlit as st

sys.path.insert(0, str(Path(__file__).resolve().parent))

from db.schema import get_connection, init_db
from db.ingest import (
    load_market_odds, odds_age_minutes, save_parlay, settle_parlays,
    store_market_odds, store_pool_dividends, store_racecard, store_raceresult,
)
from scrapers import rwitc, btc, odds_import
from scrapers import indiarace_cards
from models.rating_engine import compute_composite_scores
from models import parlay as parlay_engine
from models.betslip import (
    DEFAULT_MIN_EDGE_AT_PLACEMENT, build_slip, validate_against_live,
)
from models.odds import margin_percent, overround
from models import edge
from models import jackpot as jackpot_engine
from models import raceday
from models import verticals
from models.staking import (
    build_win_place_plan, harville_forecast_probabilities, stop_rules,
    build_place_shortlist,
)
from ui.theme import inject_theme
from ui import components as ui

# layout="centered" (not "wide"): the primary use is a phone at the track, and
# "wide" forces a desktop-width grid that squeezes content into a column on
# small screens. Detail tabs still scroll fine inside the centered container.
st.set_page_config(
    page_title="Indian Race Predictor",
    page_icon="🐎",
    layout="centered",
    initial_sidebar_state="collapsed",
)
inject_theme()
init_db()
conn = get_connection()

# RWITC (Pune/Mumbai -- same site, auto-detects venue by date), BTC
# (Bangalore, separate site), and indiarace_cards.ForVenue (Hyderabad/Mysore/
# Kolkata/Delhi -- these four clubs have no scrapable racecard of their own;
# see indiarace_cards.py's module docstring) all expose the same
# fetch_racecard_html / fetch_raceresult_html / parse_racecard /
# parse_raceresult interface, so the rest of the app doesn't need to know
# which source it's talking to.
SCRAPER_BY_VENUE = {
    "Pune": rwitc, "Mumbai": rwitc, "Bangalore": btc,
    "Hyderabad": indiarace_cards.ForVenue("Hyderabad"),
    "Mysore": indiarace_cards.ForVenue("Mysore"),
    "Kolkata": indiarace_cards.ForVenue("Kolkata"),
    "Delhi": indiarace_cards.ForVenue("Delhi"),
}

# Venue order and grouping come from models/verticals.py, which is the one
# place that knows which turf authority runs what, which scraper answers for
# it, and what that scraper cannot supply. Two properties matter here: the
# ordering is by vertical rather than alphabetical, so the picker reads like
# the sport is actually organised, and every venue name in this app comes from
# that list, so a venue can never be offered that nothing knows how to fetch.
#
# On prices, measured rather than assumed (Aug 2026): indiarace's forecast
# prices are INDICATIVE rather than a live board, they are published only on
# RACE DAY -- the same Kolkata card returned 0 priced runners at 09:36 IST and
# 31 at 15:31 IST -- and they cover only the front four or five runners of a
# field. The Odds tab says all three plainly, so an empty fetch reads as "too
# early" rather than "broken", and a pasted board price always overrides a
# forecast.
VENUES = verticals.venue_names()


def next_saturday(d: date) -> date:
    days_ahead = (5 - d.weekday()) % 7  # Saturday = 5
    return d + timedelta(days=days_ahead or 7 if d.weekday() == 5 else days_ahead)


st.title("🐎 Indian Race Predictor")
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
        "Read the **Daily Parlays** expected-value figure before backing any multi: "
        "one with negative EV loses money over time however good the horses look. "
        "The **Jackpot Planner** is the exception that proves it -- it maximises the "
        "chance of hitting, and prints the dividend you would need for that to be "
        "worth doing."
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
    date_str = race_date.strftime("%Y-%m-%d")

    # Venues already loaded into the DB for this date, marked in the picker so
    # you can see at a glance which meetings exist without opening each one.
    loaded_venues = {r["venue"] for r in conn.execute(
        "SELECT DISTINCT venue FROM races WHERE race_date=? AND circuit='India'",
        (date_str,),
    ).fetchall()}

    # Grouped by vertical rather than listed flat. Which authority a venue
    # belongs to decides its season, its data source and which fields come back
    # populated, so it is the first thing worth seeing next to the name.
    venue = st.selectbox(
        "Venue", VENUES,
        format_func=lambda v: ("● " if v in loaded_venues else "")
        + verticals.label(v),
    )
    _vprofile = verticals.profile(venue)
    st.caption(
        f"**{_vprofile['vertical']}** &middot; {_vprofile['authority']}"
        f" &middot; {verticals.SOURCE_LABEL[_vprofile['source']]}"
    )
    if loaded_venues:
        st.caption(f"● loaded for {date_str}: {', '.join(sorted(loaded_venues))}")

    budget = st.number_input("Budget for today (fun money, Rs)", min_value=0.0, value=1000.0, step=100.0)
    goal = st.number_input("Goal / mental target (Rs profit)", min_value=0.0, value=500.0, step=100.0)

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
                    st.error(
                        f"No card published for {venue} on {date_str}. Indian clubs race a few "
                        f"days a week and several run seasonally, so an empty card is usually "
                        f"the calendar rather than a fault -- check the Verticals tab for when "
                        f"this venue was last seen racing."
                    )
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
                    # The jackpot/treble settlements live in their own table at
                    # the foot of the same page. Nothing else records which
                    # races made up each pool or how many tickets shared each
                    # dividend, and the Jackpot Planner reads both.
                    pools = []
                    try:
                        pools = source.parse_pool_dividends(html)
                    except AttributeError:
                        pass  # BTC's result page carries no pool table
                    except Exception:
                        pools = []
                    n_pools = store_pool_dividends(conn, date_str, venue, pools) if pools else 0
                    st.success(f"Loaded results for {len(races)} races"
                               + (f", plus {n_pools} pool settlements." if n_pools else "."))
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


def ui_odds_map(race_id: int, session_map: dict | None) -> dict:
    """Odds for one race in the shape the Race Day components expect.

    Two routes now supply Indian prices -- the Race Day fetch button (which
    holds them in session state) and the Odds tab, which persists to
    market_odds -- so this reads both, session state first because it is the
    fresher of the two within a run.

    UNIT NOTE: market_odds stores TRUE DECIMAL (4/1 -> 5.0) while these
    components take ODDS-TO-ONE (4/1 -> 4.0), since ui.implied_probability
    computes 1/(odds+1). Hence the -1.0 on the way out. Getting this backwards
    would quietly overstate every runner's implied chance.
    """
    if session_map:
        return session_map
    out = {}
    for name, v in load_market_odds(conn, race_id).items():
        win = v.get("win")
        if not win or win <= 1.0:
            continue
        to_one = win - 1.0
        out[name] = {"odds_decimal": to_one, "odds_fraction": f"{to_one:.2f}/1"}
    return out


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


def get_slate(d: str) -> list[dict]:
    """Every Indian race on a date, across ALL venues and verticals, with model
    scores and whatever market prices we hold.

    Separate from get_race_plans because a multi is built ACROSS meetings --
    the whole point of requiring legs from different races is that they be
    independent -- and Indian clubs in different verticals routinely race on
    the same day, so the best independent value bets are often at two tracks.

    Filtered on circuit='India' rather than left open, so that an older
    database carrying rows from a circuit this app no longer supports cannot
    leak into a slate, a strike rate or a backtest."""
    rows = conn.execute(
        """SELECT id, venue, race_number, race_name, class_code, distance_m, race_time_local
           FROM races WHERE race_date=? AND circuit='India' ORDER BY venue, race_number""",
        (d,),
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
            "vertical": verticals.vertical_of(row["venue"]),
            "field_size": len(entries), "entries": entries,
            "odds": load_market_odds(conn, row["id"]),
            "odds_age_min": odds_age_minutes(conn, row["id"]),
        })
    return slate


# Beyond this, a stored price is old enough that the market has probably moved
# and any edge measured against it is fiction rather than opportunity.
STALE_ODDS_MINUTES = 90


race_plans = get_race_plans(date_str, venue) if venue else []
slate = get_slate(date_str)

# Tab order is deliberate. "Edge" comes first because it is the only screen in
# the app built on a measured return rather than on a model opinion, and it is
# the one you open standing in front of a bookmaker's board. "Race Day" is the
# rest of the phone-at-the-track view. Everything after those is homework you do
# at home on a laptop (backtest, calibration, connections, the vertical
# profiles) -- it should not compete for thumb space.
(tab_edge, tab_raceday, tab_today, tab_parlay, tab_jackpot, tab_odds, tab_followup,
 tab_place, tab_forecast, tab_verticals, tab_connections, tab_backtest,
 tab_bankroll) = st.tabs(
    ["Edge", "Race Day", "Today's Picks", "Daily Parlays", "Jackpot Planner", "Odds",
     "Followup", "Place Bets", "Forecast / Quinella", "Verticals", "Connections",
     "Backtest", "Bankroll & Calibration"]
)

with tab_edge:
    st.subheader("Where the edge is")

    # Measured live from the archive rather than quoted from a constant, so a
    # number on this screen can never outlive the data that produced it.
    _gap = edge.market_gap(conn)
    _bands = edge.measure_bands(conn)

    if not _gap.get("races"):
        st.info("No archived races with both a ring price and a tote dividend yet.")
    else:
        st.markdown(
            f"**Two markets run on every Indian race, and one of them is "
            f"{_gap['gap_pp'] * 100:.1f} points cheaper.** The on-course bookmaker ring and the "
            f"tote price the same horses independently. Backing every runner in "
            f"{_gap['races']} archived races — {_gap['bets']:,} bets — returned "
            f"**{_gap['ring_roi'] * 100:.1f}%** in the ring and **{_gap['tote_roi'] * 100:.1f}%** "
            f"on the tote. That gap costs you on every bet you place, needs no model, and is the "
            f"only number in this project with a sample past arguing about."
        )
        st.success(
            "**Your win and place bets already go through the ring, which is the right side of "
            "that.** Keep them there. Your jackpots have to go through the tote — Indian clubs "
            "run no other pool for them — so that one is forced, not a choice."
        )

        st.markdown(ui.section_label("What each price band actually returned"), unsafe_allow_html=True)
        st.dataframe(
            [{"Ring price": f"{b['lo']:.2f} – {b['hi']:.2f}" if b["hi"] < 900 else f"{b['lo']:.2f}+",
              "Bets": b["bets"],
              "Won": f"{b['win_rate'] * 100:.1f}%",
              "Price implies": f"{b['implied'] * 100:.1f}%",
              "Return": f"{b['roi'] * 100:+.1f}%",
              "95% interval": (f"{b['ci_low'] * 100:+.0f}% to {b['ci_high'] * 100:+.0f}%"
                               if "ci_low" in b else "too few bets")}
             for b in _bands],
            use_container_width=True, hide_index=True,
        )
        st.caption(
            "Favourite–longshot bias, and unusually severe. Short prices win more often than "
            "their price implies; long ones win far less. Note where the money is lost: the two "
            "longest bands are 2,555 of the 4,302 bets and account for essentially all of the "
            "damage. **Not betting those is worth more than any selection method in this app.**"
        )
        _edge_band = next((b for b in _bands
                           if (b["lo"], b["hi"]) == edge.EDGE_BAND), None)
        if _edge_band and "ci_low" in _edge_band:
            st.warning(
                f"**Read the interval before sizing anything.** The one paying band is "
                f"{_edge_band['roi'] * 100:+.1f}% over {_edge_band['bets']} bets, but its 95% "
                f"interval runs {_edge_band['ci_low'] * 100:+.0f}% to "
                f"{_edge_band['ci_high'] * 100:+.0f}% — it touches zero, so this may be nothing. "
                f"What keeps it alive is that it comes out about the same in both halves of the "
                f"archive by date and is positive at five of six venues, and that "
                f"favourite–longshot bias is the most replicated inefficiency in racing. Treat it "
                f"as a hypothesis you are staking small money on, and log every bet below so it "
                f"gets settled by evidence."
            )

        _fa = edge.favourite_agreement(conn)
        if _fa.get("races"):
            with st.expander("Does it matter when the ring and the tote disagree on the favourite?"):
                st.caption(
                    f"They name the same favourite in {_fa['agreement_rate'] * 100:.0f}% of "
                    f"{_fa['races']} races. Backing the ring favourite:"
                )
                st.dataframe(
                    [{"Case": "Both markets agree", "Races": _fa["agree"]["n"],
                      "Won": f"{_fa['agree']['win_rate'] * 100:.1f}%",
                      "Return in ring": f"{_fa['agree']['ring_roi'] * 100:+.1f}%"}]
                    + ([{"Case": "They disagree", "Races": _fa["disagree"]["n"],
                         "Won": f"{_fa['disagree']['win_rate'] * 100:.1f}%",
                         "Return in ring": f"{_fa['disagree']['ring_roi'] * 100:+.1f}%"}]
                       if _fa["disagree"] else []),
                    use_container_width=True, hide_index=True,
                )
                st.caption(
                    "**This is reported, not enforced.** Disagreement looked like a strong "
                    "negative signal on one cut of the data and almost nothing on another, on a "
                    "sample of about 66 races. A filter whose sign moves when you look at it "
                    "differently is not a filter. Worth a second look at the horse; not worth a "
                    "rule."
                )

        # ------------------------------------------------------------------
        st.divider()
        st.markdown(ui.section_label("Check a price at the board"), unsafe_allow_html=True)

        if not race_plans:
            st.info("Load a race card in the sidebar and this will pre-fill the field for you.")
        else:
            ec1, ec2 = st.columns([1, 2])
            e_race = ec1.selectbox("Race", [rp["race_no"] for rp in race_plans],
                                   format_func=lambda n: f"R{n}", key="edge_race")
            _rp = next(r for r in race_plans if r["race_no"] == e_race)
            _names = [e["horse_name"] for e in _rp["entries"]] or ["(no runners parsed)"]
            e_horse = ec2.selectbox("Runner", _names, key="edge_horse")

            p1, p2, p3 = st.columns([1, 1, 1])
            e_price = p1.number_input(
                "Price in the ring (decimal)", min_value=0.0, value=0.0, step=0.05,
                key="edge_price",
                help="Decimal, so 5/2 is 3.50 and evens is 2.00. Take it off the ring board, "
                     "not the tote screen.")
            e_isfav = p2.checkbox("Shortest price in the ring", value=True, key="edge_isfav")
            e_agree = p3.checkbox("Tote board agrees it's favourite", value=True, key="edge_agree")

            _v = edge.qualify(e_price or None, _bands,
                              is_ring_favourite=e_isfav, tote_agrees=e_agree)
            if _v["verdict"] == "BET":
                st.success(f"**BET** — {_v['reason']}")
            elif _v["verdict"] == "THIN":
                st.warning(f"**THIN** — {_v['reason']}")
            elif _v["verdict"] == "SKIP":
                st.error(f"**SKIP** — {_v['reason']}")
            else:
                st.info(_v["reason"])
            for _n in _v.get("notes", []):
                st.caption(f"· {_n}")

            if _v["verdict"] in ("BET", "THIN"):
                _model = next((e for e in _rp["entries"] if e["horse_name"] == e_horse), None)
                if _model:
                    st.caption(
                        f"For context only: the model makes {e_horse} a "
                        f"{_model['win_probability'] * 100:.0f}% chance, ranked "
                        f"{_rp['entries'].index(_model) + 1} of {len(_rp['entries'])}. **The "
                        f"verdict above ignores that on purpose** — the model's own pick has "
                        f"never beaten the market in this archive, while the price band has a "
                        f"measurable relationship with return."
                    )
                s1, s2 = st.columns([1, 2])
                e_stake = s1.number_input("Stake (Rs)", min_value=0.0, value=200.0, step=50.0,
                                          key="edge_stake")
                s2.caption(
                    "Keep it small and keep it the same size every time. A varying stake on an "
                    "unproven edge makes the record unreadable: you cannot tell a real return "
                    "from having happened to bet more on the winners."
                )
                if st.button("Log this bet", use_container_width=True, key="edge_log"):
                    _band = _v["band"]
                    edge.log_bet(
                        conn, date_str, venue, e_race, e_horse, "ring", e_price, e_stake,
                        band=f"{_band['lo']:.2f}-{_band['hi']:.2f}" if _band else "?",
                        verdict=_v["verdict"],
                        notes=("ring fav" if e_isfav else "not ring fav")
                              + ("; tote agrees" if e_agree else "; tote disagrees"))
                    st.success("Logged. Settle it below once the results are in.")
                    st.rerun()

        # ------------------------------------------------------------------
        st.divider()
        st.markdown(ui.section_label("The record — is any of this real?"), unsafe_allow_html=True)

        lc1, lc2 = st.columns([1, 2])
        if lc1.button("Settle this date", use_container_width=True, key="edge_settle"):
            _res = edge.settle_from_results(conn, date_str)
            if _res["settled"]:
                st.success(f"Settled {_res['settled']} bet(s), {_res['won']} won.")
                st.rerun()
            else:
                st.info(f"Nothing to settle — {_res['pending']} bet(s) still waiting on results. "
                        f"Load them from the sidebar first.")
        lc2.caption("Grades each logged bet against the stored finishing position and pays it at "
                    "the price you actually took, which is the whole reason the price is stored.")

        _today = conn.execute(
            "SELECT * FROM edge_bets WHERE race_date=? ORDER BY id DESC", (date_str,)).fetchall()
        if _today:
            st.dataframe(
                [{"Race": f"R{b['race_no']}", "Runner": b["horse_name"],
                  "Price": f"{b['price']:.2f}", "Stake": f"Rs{b['stake']:.0f}",
                  "Band": b["band"], "Called": b["verdict"],
                  "Result": b["outcome"] or "pending",
                  "Returned": f"Rs{b['payout']:.0f}" if b["payout"] is not None else "—"}
                 for b in _today],
                use_container_width=True, hide_index=True,
            )

        _perf = edge.performance(conn)
        if _perf["n"]:
            m1, m2, m3 = st.columns(3)
            m1.metric("Settled bets", _perf["n"])
            m2.metric("Won", f"{_perf['won']} ({_perf['won'] / _perf['n'] * 100:.0f}%)")
            m3.metric("Return", f"{_perf['roi'] * 100:+.1f}%",
                      delta=f"Rs{_perf['returned'] - _perf['staked']:+,.0f}")
            st.caption(_perf["verdict"])
            if len(_perf["by_band"]) > 1:
                st.dataframe(
                    [{"Band": k, "Bets": v["n"], "Won": v["won"],
                      "Staked": f"Rs{v['staked']:.0f}",
                      "Return": f"{((v['ret'] - v['staked']) / v['staked'] * 100):+.1f}%"
                      if v["staked"] else "—"}
                     for k, v in sorted(_perf["by_band"].items())],
                    use_container_width=True, hide_index=True,
                )
        else:
            st.caption(
                f"Nothing settled yet. It takes about {edge.MIN_LOGGED_FOR_VERDICT} bets before "
                f"this record says anything — at roughly two qualifying bets a race day that is "
                f"a season, which is the honest timescale for finding out whether the band edge "
                f"is real."
            )

        st.divider()
        st.caption(
            "**What is deliberately not here.** There is no measured edge for place bets, because "
            "there is no data: not one place dividend exists in the archive, so no place return "
            "has ever been computed at any price. The Place Bets tab stays a model shortlist to "
            "price up by hand. And nothing here says a jackpot is a good bet — see the Jackpot "
            "Planner, which maximises the chance of hitting and then tells you what dividend that "
            "would need to be worth doing."
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
            f'<div class="rp-label">{ui._esc(venue)} &middot; '
            f'{ui._esc(verticals.vertical_of(venue) or "")} &middot; '
            f'Race {rp["race_no"]} &middot; '
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

            # Whether the model and the market agree predicts the result far
            # better than the model's own confidence does (53% vs 12%), so it
            # sits directly under the pick rather than in the Backtest tab.
            # Needs a price, so it appears once odds have been fetched/pasted.
            # Race-day context. Shown because the "big-crowd days are less
            # predictable" belief is common and worth answering with the
            # archive rather than leaving unaddressed -- but it changes no
            # scoring, because the effect was measured and isn't there.
            # See models/raceday.py.
            _day_note = raceday.context_note(date_str, rp.get("race_name"))
            if _day_note:
                st.caption(f"📅 {_day_note}")

            _odds_now = ui_odds_map(rp["race_id"],
                                    st.session_state.get(f"odds_{date_str}_{venue}"))
            # Resolved via getattr rather than called directly. Streamlit Cloud
            # kept an already-imported ui.components in memory across a
            # redeploy, so app.py was the new version while the module was the
            # old one -- and a missing attribute on an OPTIONAL badge took the
            # whole app down with an AttributeError. A reboot clears the cache,
            # but no display extra should ever be able to break the page, so
            # this degrades to "no badge" instead.
            _mk_agreement = getattr(ui, "market_agreement", None)
            _mk_banner = getattr(ui, "market_agreement_banner", None)
            if _mk_agreement and _mk_banner:
                _agreement = _mk_agreement(entries, _odds_now)
                if _agreement:
                    st.markdown(_mk_banner(_agreement), unsafe_allow_html=True)

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

            # Value view. Ranking by win chance answers "which horse is best";
            # ranking by edge over the market answers "which horse is worth
            # backing" -- the question that actually decides profit.
            odds_map = _odds_now or None
            st.markdown(ui.section_label("Best value in this race"), unsafe_allow_html=True)
            if odds_map is None:
                st.caption(
                    "indiarace publishes forecast prices for Indian races. Pull them to see "
                    "which runners are **underpriced** rather than just which are fastest. "
                    "Prices fetched or pasted in the **Odds** tab show up here too."
                )
                if st.button("Get odds from indiarace", use_container_width=True, key="get_odds"):
                    try:
                        from scrapers import indiarace as ir
                        raw = ir.fetch_odds_html(venue, date_str, use_cache=False)
                        if not raw:
                            st.warning(f"indiarace has no odds feed for {venue}.")
                        else:
                            fetched = ir.parse_odds(raw)
                            if fetched:
                                st.session_state[f"odds_{date_str}_{venue}"] = fetched
                                # Persist as well as hold in session. A price
                                # fetched here must be visible to the Jackpot
                                # Planner and the parlay engine too -- a price
                                # that exists in one tab and not another is the
                                # kind of inconsistency that gets a bet placed
                                # on the wrong number.
                                store_market_odds(
                                    conn, date_str, venue,
                                    ir.to_market_rows(
                                        fetched,
                                        {r["race_no"]: [e["horse_name"] for e in r["entries"]]
                                         for r in race_plans}),
                                    source="indiarace")
                                st.rerun()
                            else:
                                st.warning("No odds posted for this date yet -- indiarace fills "
                                           "this feed on race-day morning, not the night before.")
                    except Exception as e:
                        st.error(f"Odds fetch failed: {e}")
            else:
                matched = sum(1 for e in entries if (e["horse_name"] or "").upper() in odds_map)
                st.markdown(ui.value_rows(entries, odds_map), unsafe_allow_html=True)
                st.caption(
                    f"Sorted by edge = model% − market%. **BET** clears fair odds with cushion, "
                    f"**THIN** is marginal, **SKIP** is overpriced. {matched}/{field} runners priced. "
                    f"These are indicative forecast prices — re-check the board before staking."
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
    st.subheader("Jackpot / treble planner")
    st.markdown(
        "**This is the one bet on the card where covering more runners is the "
        "whole game.** A jackpot pays only if you hold the winner of every leg, "
        "so the question is not which horse to back but where to spend the "
        "combinations you can afford. Two things decide that, and both were "
        "measured on this archive rather than assumed."
    )
    _cov = jackpot_engine.MEASURED_LEG_COVERAGE
    st.caption(
        f"**1. Rank legs by the market, not by the model.** Over "
        f"{jackpot_engine.MEASURED_LEG_COVERAGE_SAMPLE} races the winner came from the "
        f"market's top three {_cov[3]['market'] * 100:.0f}% of the time and from this "
        f"model's top three {_cov[3]['model'] * 100:.0f}% of the time. Compounded across "
        f"five legs at the same 243 combinations that is a "
        f"{jackpot_engine.MEASURED_STRATEGY[5]['market'] * 100:.0f}% hit rate against "
        f"{jackpot_engine.MEASURED_STRATEGY[5]['model'] * 100:.0f}%."
    )
    st.caption(
        "**2. Spend where the race is hard.** Legs differ enormously: one race has a "
        "standout, the next has five runners the market cannot separate. An even spread "
        "buys coverage where it was already cheap. The planner below allocates by marginal "
        "value instead — each extra runner goes wherever it buys the most extra chance per "
        "rupee — which was worth another 6 to 15 points at the same cost."
    )

    if not race_plans:
        st.info("Load a race card first (sidebar, then Fetch live).")
    else:
        race_nos = [rp["race_no"] for rp in race_plans]
        # A card carries several of these at once -- typically three trebles,
        # two mini-jackpots and a jackpot, each over a different set of legs.
        # Where the club's own settlements have been archived, offer exactly
        # what it ran; otherwise offer the generic shapes with a last-N guess.
        published = jackpot_engine.actual_legs(conn, date_str, venue)
        # Biggest pool first, so the default selection is the one people mean
        # when they say "the jackpot" rather than whichever sorts last.
        pool_choices = (sorted(published, key=lambda n: (-len(published[n]), n))
                        if published
                        else list(reversed(jackpot_engine.pool_options(len(race_nos)))))

        if not pool_choices:
            st.info(f"This card has {len(race_nos)} races — too few for any multi-leg pool.")
        else:
            c1, c2, c3 = st.columns(3)
            pool = c1.selectbox(
                "Pool", pool_choices, index=0,
                format_func=lambda name: (
                    f"{name.title()} ({','.join(str(n) for n in published[name])})"
                    if name in published else name),
                help=" · ".join(f"{k}: {v['note']}"
                                for k, v in jackpot_engine.POOLS.items()))
            jackpot_budget = c2.number_input("Budget for this ticket (Rs)", min_value=0.0,
                                             value=1000.0, step=100.0)
            unit_cost = c3.number_input("Cost per combination (Rs)", min_value=1.0,
                                        value=5.0, step=1.0,
                                        help="The club's minimum unit for this pool. Check your "
                                             "own tote card — it is not the same everywhere.")

            club_legs = published.get(pool)
            n_legs = (len(club_legs) if club_legs
                      else jackpot_engine.POOLS[pool]["legs"])
            default_legs = club_legs or jackpot_engine.suggest_legs(race_nos, n_legs)
            if club_legs:
                st.success(
                    f"Legs taken from **{venue}'s own published result page** for this meeting: "
                    f"races {', '.join(str(n) for n in club_legs)}. "
                    f"This is the {jackpot_engine.pool_family(pool).lower()} pool."
                )
            else:
                st.caption(
                    f"Legs default to the last {n_legs} races, which is where Indian clubs "
                    f"usually put this pool — but the club chooses per meeting (Hyderabad ran "
                    f"its jackpot over races 4-8 on 27 Jul 2026 and races 3-7 on 9 Aug), so "
                    f"check the day's own list and adjust below."
                )

            leg_choices = st.multiselect(
                "Legs (in running order)", options=race_nos, default=default_legs,
                format_func=lambda n: f"Race {n}",
            )
            model_weight = st.slider(
                "Weight on our model vs the market", 0.0, 1.0, 0.0, 0.05,
                help="Starts at zero because the archive says the market picks legs two to "
                     "three times better than this model does. Raising it makes the ticket "
                     "more contrarian and, on the evidence, less likely to hit.",
            )

            if len(leg_choices) < 2:
                st.info("Pick at least two legs.")
            else:
                legs = [{"race_no": rp["race_no"], "race_name": rp["race_name"],
                         "venue": venue, "entries": rp["entries"],
                         "odds": load_market_odds(conn, rp["race_id"])}
                        for rp in race_plans if rp["race_no"] in leg_choices]
                plan = jackpot_engine.plan(legs, budget=jackpot_budget, unit_cost=unit_cost,
                                           model_weight=model_weight)

                if not plan.get("affordable"):
                    st.error(plan.get("note"))
                else:
                    m1, m2, m3, m4 = st.columns(4)
                    m1.metric("Combinations", f"{plan['combinations']:,}")
                    m2.metric("Ticket cost", f"Rs{plan['cost']:,.0f}")
                    m3.metric("Chance it lands", f"{plan['hit_probability'] * 100:.1f}%")
                    m4.metric("Break-even dividend",
                              f"Rs{plan['break_even_dividend']:,.0f}"
                              if plan["break_even_dividend"] else "-")

                    if plan["book_quality"] == "none":
                        st.error(
                            "**No prices on any leg.** Every leg below is ranked on the model "
                            "alone, which this archive says is by far the weaker selector. "
                            "Fetch the indiarace forecast in the Odds tab before using this."
                        )
                    elif plan["book_quality"] == "mixed":
                        st.warning(
                            "Some legs carry no price and are ranked on the model alone. Those "
                            "legs are the weak point of this ticket."
                        )
                    elif plan["book_quality"] == "partial":
                        st.info(
                            "Prices cover the front of each field rather than all of it, which "
                            "is what indiarace publishes and what a jackpot leg needs. The "
                            "running order at the front is the part that matters here; the "
                            "unpriced tail is shared out by model score."
                        )

                    st.markdown(ui.section_label("Where the money goes"), unsafe_allow_html=True)
                    for leg in plan["legs"]:
                        fielded = leg["width"] >= leg["field"]
                        with st.container(border=True):
                            st.markdown(
                                f"**Race {leg['race_no']}** &middot; "
                                f"{ui._esc(leg.get('race_name') or '')}  \n"
                                f"*{leg['shape']}* — covering **{leg['width']} of {leg['field']}** "
                                f"for **{leg['coverage'] * 100:.0f}%** of the leg"
                                + ("  ·  *fielded, so this leg is effectively a bye*"
                                   if fielded else ""),
                                unsafe_allow_html=True,
                            )
                            st.dataframe(
                                # Every cell a string: a numeric column with a
                                # "-" in it fails Arrow conversion and Streamlit
                                # silently re-types the whole column.
                                [{"Runner": r["horse_name"],
                                  "Chance": f"{r['probability'] * 100:.0f}%",
                                  "Price": (f"{r['decimal_odds']:.2f}"
                                            if r["decimal_odds"] else "—"),
                                  "Ranked on": r["source"]}
                                 for r in leg["runners"]],
                                use_container_width=True, hide_index=True,
                            )
                            if leg["next_out"] and not fielded:
                                st.caption(
                                    f"First runner left out: **{leg['next_out']['horse_name']}** at "
                                    f"{leg['next_out']['probability'] * 100:.0f}%. That is what "
                                    f"this leg costs you when it goes wrong."
                                )

                    if plan.get("next_step"):
                        nxt = plan["next_step"]
                        st.caption(
                            f"💡 The next Rs{nxt['extra_cost']:,.0f} would add "
                            f"**{nxt['horse_name']}** in race {nxt['race_no']}, lifting the "
                            f"ticket's chance by {nxt['gain'] * 100:.1f} points — worth it only "
                            f"if the dividend clears Rs{nxt['extra_cost'] / nxt['gain']:,.0f}."
                        )

                    # The section that stops a high hit rate being mistaken for
                    # a good bet.
                    st.markdown(ui.section_label("Is it worth it, though"), unsafe_allow_html=True)
                    reality = jackpot_engine.dividend_reality(conn, pool)
                    if reality["n"] and plan["break_even_dividend"]:
                        verdict = ("**above**" if reality["median"] < plan["break_even_dividend"]
                                   else "**below**")
                        if reality["source"] == "archive":
                            source_note = (
                                f"{reality['n']} archived "
                                f"{jackpot_engine.pool_family(pool).lower()} settlements from "
                                f"club result pages"
                            )
                        else:
                            source_note = (
                                f"too few {jackpot_engine.pool_family(pool).lower()} "
                                f"settlements are archived to say "
                                f"anything, so this compares against {reality['n']} "
                                f"{reality['of_pool'].lower()} settlements instead -- a "
                                f"different pool, and only a rough guide"
                            )
                        st.markdown(
                            f"This ticket needs a dividend of "
                            f"**Rs{plan['break_even_dividend']:,.0f}** to break even. Against "
                            f"{source_note}: median **Rs{reality['median']:,.0f}**, "
                            f"low Rs{reality['low']:,.0f}, high Rs{reality['high']:,.0f}. "
                            f"The break-even sits {verdict} that median."
                        )
                    st.warning(
                        "**A jackpot is pari-mutuel, and that cuts against you here.** The "
                        "dividend is the pool divided among everyone holding the same line, so "
                        "covering the market's favourites — the line most other tickets also "
                        "hold — means the combinations that hit most often pay least when they "
                        "do. A real example from this archive: Hyderabad, 10 Aug 2026, the front "
                        "of the market won all five legs, 1,468 tickets shared the pool and it "
                        "paid Rs317. The day before, the same bet paid Rs32,647 to sixteen "
                        "tickets. Hitting it and making money are two different questions."
                    )

                    with st.expander("How this plan would have done, replayed over the archive"):
                        bt = jackpot_engine.backtest_strategy(
                            conn, n_legs=len(leg_choices), unit_cost=unit_cost,
                            budget=jackpot_budget)
                        if not bt.get("days"):
                            st.caption("Not enough complete archived race days to replay yet.")
                        else:
                            even = bt["even_spread"]
                            st.dataframe(
                                [{"Method": "This planner",
                                  "Combinations": f"{bt['average_combinations']:.0f}",
                                  "Hit rate": f"{bt['hit_rate'] * 100:.1f}%"},
                                 {"Method": f"Even spread, top {even['runners_per_leg']} each leg",
                                  "Combinations": f"{even['combinations']}",
                                  "Hit rate": f"{even['hit_rate'] * 100:.1f}%"}],
                                use_container_width=True, hide_index=True,
                            )
                            st.caption(
                                f"{bt['days']} complete race days, {len(leg_choices)} legs, "
                                f"Rs{jackpot_budget:,.0f} budget. The planner predicted "
                                f"{bt['predicted_rate'] * 100:.1f}% across those days and hit "
                                f"{bt['hit_rate'] * 100:.1f}%. The confidence interval at this "
                                f"sample is about ±{bt['confidence_pp']:.0f} points, so read the "
                                f"direction rather than the decimals."
                            )
                            st.caption(
                                "The even-spread row is lumpy by nature: it can only buy a whole "
                                "extra runner in *every* leg at once, so it usually leaves a "
                                "large part of the budget unspent. Not being able to spend part "
                                "of the budget is itself part of why it does worse."
                            )
                            st.caption(
                                "⚠️ **The honest limit of this replay.** A jackpot ticket has to "
                                "be submitted before the first leg runs, and the starting prices "
                                "this replay ranks on are only known afterwards. It measures how "
                                "good the method is when its input is good, and it is an upper "
                                "bound on what the same method does off race-morning forecast "
                                "prices. Closing that gap is what the odds snapshots are "
                                "collecting; on the one Kolkata card where both exist, the "
                                "forecast agreed with the starting price on the favourite in 6 "
                                "races of 7 — encouraging, and nowhere near enough to conclude "
                                "anything."
                            )


    # ----------------------------------------------------------------------
    # The three things that decide whether a jackpot is worth playing at all,
    # kept outside the planner because they are true of the pool rather than
    # of any one ticket.
    # ----------------------------------------------------------------------
    st.divider()
    st.markdown(ui.section_label("Is this pool worth playing this week?"),
                unsafe_allow_html=True)

    _carry = jackpot_engine.carryover_watch(conn, venue=venue)
    if _carry:
        _latest = _carry[0]
        st.info(
            f"💰 **Carry-forward on record: Rs{_latest['amount']:,.0f}** in {venue}'s "
            f"{_latest['pool']} on {_latest['race_date']} — nobody hit it and the money rolled "
            f"into the next running of that pool. This is the one place in pari-mutuel betting "
            f"where money appears that nobody bet for: it is added to the pool without paying "
            f"takeout, so it lowers the effective takeout of the next running by its share of "
            f"it. It does not make the bet good on its own — you would need the carried amount "
            f"to outweigh the takeout on all the new money, and clubs do not publish pool "
            f"sizes — but it is the only tailwind on offer, and a pool that has just carried is "
            f"the one to prefer."
        )
        if len(_carry) > 1:
            st.caption("Also carried recently: " + "; ".join(
                f"{r['race_date']} {r['venue']} {r['pool']} Rs{r['amount']:,.0f}"
                for r in _carry[1:4]))
    else:
        st.caption(
            f"No carry-forward recorded for {venue} yet. Load more result pages and any pool "
            f"nobody hit gets logged here — those are the weeks worth playing."
        )

    _agree = jackpot_engine.forecast_rank_agreement(conn)
    with st.expander("⚠️ The measurement everything in this tab is waiting on", expanded=False):
        st.markdown(
            "**Every hit rate on this page ranks legs by the starting price, and a starting "
            "price does not exist when a jackpot ticket has to be handed over.** The ticket goes "
            "in before the first leg runs; the prices form during betting on each race. So those "
            "numbers measure how good the method is when its input is good, and are an upper "
            "bound on what it does off the race-morning forecast, which is what you would "
            "actually have."
        )
        if not _agree.get("races"):
            st.caption(
                "Nothing collected yet. `python -m scripts.daily --snapshot` on a race-day "
                "morning records the forecast; run it again after the results and it settles "
                "the starting prices alongside. A few weeks of that answers the question."
            )
        else:
            a1, a2, a3 = st.columns(3)
            a1.metric("Races with both", _agree["races"])
            a2.metric("Same favourite", f"{_agree['same_favourite_rate'] * 100:.0f}%")
            a3.metric("Shared top 3", f"{_agree['mean_top3_overlap']:.1f} of 3")
            if _agree.get("forecast_top3_covers") is not None:
                st.caption(
                    f"Winner came from the forecast's top three "
                    f"{_agree['forecast_top3_covers'] * 100:.0f}% of the time, against "
                    f"{_agree['sp_top3_covers'] * 100:.0f}% for the starting price, over "
                    f"{_agree['graded']} graded races."
                )
            st.caption(_agree["verdict"])

    with st.expander("💵 Did it actually pay? Replayed against real dividends"):
        st.caption(
            "Hit rate is the wrong question on its own. A jackpot is pari-mutuel, so the tickets "
            "that hit most often are the ones sharing the pool with the most people. This "
            "replays the planner against every archived settlement using the dividend AND the "
            "ticket count the club published, with your own ticket diluting the pool and the "
            "30% consolation tier counted when the ticket finishes one leg short."
        )
        vb_c1, vb_c2 = st.columns([1, 1])
        vb_budget = vb_c1.number_input("Combinations per ticket", min_value=8, max_value=2000,
                                       value=240, step=8, key="vb_combos")
        vb_model = vb_c2.checkbox(
            "Also replay with model-ranked legs (slow)", value=False, key="vb_model",
            help="Legs ranked on information genuinely available before the first race. This "
                 "is the lower bound; the starting-price row is the upper bound.")
        if st.button("Run the money replay", use_container_width=True, key="vb_run"):
            with st.spinner("Replaying archived pool settlements..."):
                out = {"sp": jackpot_engine.value_backtest(
                    conn, max_combinations=int(vb_budget), rank_by="sp")}
                if vb_model:
                    out["model"] = jackpot_engine.value_backtest(
                        conn, max_combinations=int(vb_budget), rank_by="model",
                        scorer=lambda cn, rid, d: compute_composite_scores(cn, rid, as_of_date=d))
                st.session_state["vb"] = out

        _vb = st.session_state.get("vb")
        if _vb:
            rows = []
            for key, label in (("sp", "Legs ranked by starting price (upper bound)"),
                               ("model", "Legs ranked by the model (lower bound)")):
                r = _vb.get(key)
                if not r or not r.get("pools"):
                    continue
                ci = r.get("ci")
                rows.append({
                    "Method": label, "Pools": r["pools"], "Hit": r["hits"],
                    "Return": f"{r['roi'] * 100:+.0f}%",
                    "95% interval": (f"{ci[0] * 100:+.0f}% to {ci[1] * 100:+.0f}%"
                                     if ci else "—"),
                    "Median pool": f"{r['median_pool_roi'] * 100:+.0f}%",
                    "Excl. best 3": (f"{r['roi_excluding_top3'] * 100:+.0f}%"
                                     if r.get("roi_excluding_top3") is not None else "—"),
                })
            if rows:
                st.dataframe(rows, use_container_width=True, hide_index=True)
                _sp = _vb.get("sp") or {}
                st.caption(
                    f"**Read the median next to the mean.** Most tickets lose — the median pool "
                    f"returned {_sp.get('median_pool_roi', 0) * 100:+.0f}% — and the profit lives "
                    f"in a long tail, with the single best pool supplying "
                    f"{_sp.get('biggest_pool_share', 0) * 100:.0f}% of all winnings. That is the "
                    f"shape of a pari-mutuel return, and it means a positive average needs a "
                    f"bankroll that survives the losing weeks to ever be collected."
                )
                st.caption(
                    f"Sample: {_sp.get('pools', 0)} settlements from the meetings whose results "
                    f"have been archived — a handful of race days at two venues. Not a random "
                    f"sample of Indian racing, and nothing here is established until the "
                    f"forecast-rank question above is answered."
                )
                st.caption(
                    "**One assumption worth ten seconds at the tote window:** the units cancel "
                    "only if a dividend is quoted per one combination. If a combination costs "
                    "twice what the dividend is quoted per, every figure above is twice as good "
                    "as reality. Check the club's own card."
                )


with tab_verticals:
    st.subheader("The Indian racing map")
    st.markdown(
        "Indian racing is not one circuit. It is six active clubs under four regional turf "
        "authorities, and the grouping is not cosmetic: it decides **when** a venue races, "
        "**which source** answers for it, **which fields come back populated**, and — as the "
        "table below shows — **how predictable the place actually is.** A signal that carries "
        "a Pune card may not exist at Mysore."
    )

    _stats = verticals.archive_stats(conn)
    _summary = verticals.vertical_summary(conn)

    st.dataframe(
        [{"Vertical": s["vertical"],
          "Clubs": ", ".join(s["venues"]),
          "Races archived": s["races"],
          "Favourite wins": (f"{s['favourite_win_rate'] * 100:.1f}%"
                             if s["favourite_win_rate"] is not None else "—"),
          "Last seen racing": s["last_seen"] or "—"}
         for s in _summary],
        use_container_width=True, hide_index=True,
    )
    st.caption(
        "**Favourite wins** is the benchmark this model has to beat, measured per vertical from "
        "the archive rather than quoted from anywhere. It is the single most useful number on "
        "this page: where the favourite wins more often, the market is harder to beat and the "
        "model's disagreements are worth less. Read it alongside the sample size below — a rate "
        "over fourteen races is a story, not a statistic."
    )

    for _s in _summary:
        with st.expander(f"{_s['vertical']} — {_s['authority']}",
                         expanded=verticals.vertical_of(venue) == _s["vertical"]):
            st.markdown(_s["blurb"])
            for _v in _s["venues"]:
                _p = verticals.profile(_v)
                _st = _stats[_v]
                st.markdown(
                    f"**{_v}**" + (f" · {_p['course']}" if _p["course"] != _v else "")
                    + (" ← currently selected" if _v == venue else "")
                )
                st.caption(f"Source: {verticals.SOURCE_LABEL[_p['source']]}. {_p['season_note']}")
                if _st["races"]:
                    st.caption(
                        f"Archive: {_st['races']} races, {_st['settled']} with results, "
                        f"{_st['first_seen']} to {_st['last_seen']}. "
                        + (f"Favourite won {_st['favourite_win_rate'] * 100:.1f}% "
                           f"of {_st['favourite_sample']} races. "
                           if _st["favourite_win_rate"] is not None else "")
                        + verticals.evidence_note(_st)
                    )
                else:
                    st.caption(f"Archive: {verticals.evidence_note(_st)}")
                if _p["gaps"]:
                    st.caption("Known gaps in what this source publishes:")
                    for _g in _p["gaps"]:
                        st.caption(f"  · {_g}")
                else:
                    st.caption("No known gaps — this source fills every field the model reads.")

    st.divider()
    st.markdown("#### Multi-leg pools, per club")
    st.caption(
        "Which pools a club offers and which races make them up is published only on the day's "
        "own result page, and it varies by meeting. Every settlement loaded through **Fetch "
        "results** is recorded here, which is what lets the Jackpot Planner use the club's real "
        "legs instead of assuming the last five races."
    )
    _pools = conn.execute(
        """SELECT venue, pool, COUNT(*) n, MAX(race_date) last_seen,
                  SUM(CASE WHEN carried_forward IS NOT NULL THEN 1 ELSE 0 END) carried
           FROM pool_dividends GROUP BY venue, pool ORDER BY venue, pool""",
    ).fetchall()
    if _pools:
        st.dataframe(
            [{"Vertical": verticals.vertical_of(r["venue"]) or "—", "Venue": r["venue"],
              "Pool": r["pool"].title(), "Settlements archived": r["n"],
              "Carried forward": r["carried"], "Last seen": r["last_seen"]}
             for r in _pools],
            use_container_width=True, hide_index=True,
        )
        st.caption(
            "**Carried forward** counts the days nobody hit it and the pool rolled into the next "
            "meeting. A pool that carries often is a pool that is hard to hit — and one whose "
            "dividend, when it finally goes, is inflated by everything that rolled in."
        )
    else:
        st.info(
            "No pool settlements archived yet. Load a meeting's results from the sidebar and "
            "any jackpot, mini-jackpot or treble on that card gets recorded."
        )

    if verticals.DORMANT:
        st.divider()
        st.caption(
            "**Not wired up:** " + " ".join(f"{k} — {v}" for k, v in verticals.DORMANT.items())
            + " A venue entry for either would promise data that is not there."
        )


with tab_connections:
    st.subheader(f"Official jockey/trainer strike rates -- {venue} "
                 f"({verticals.vertical_of(venue)})")
    st.caption(
        "Season-to-date, as published by the club itself (not derived from our own thin sample). "
        "This is the 'fundamentals' layer: who's actually winning right now, regardless of what "
        "any single horse's rating says. Click 'Refresh jockey/trainer stats' in the sidebar to update."
    )
    def _pct_cell(value) -> str:
        """A club's own leaderboard can carry a blank Win% -- two Bangalore
        jockeys do -- and formatting None crashed the whole page rather than
        one cell. Blank is a fact about the source, so it renders as one."""
        return f"{value:.1f}%" if value is not None else "-"

    col_j, col_t = st.columns(2)
    with col_j:
        st.markdown("**Jockeys**")
        jockeys = conn.execute(
            "SELECT name, wins, total_rides, win_pct FROM connection_stats "
            "WHERE venue=? AND role='jockey' ORDER BY win_pct DESC", (venue,),
        ).fetchall()
        if jockeys:
            st.dataframe(
                [{"Jockey": r["name"], "Wins": r["wins"], "Rides": r["total_rides"],
                  "Win %": _pct_cell(r["win_pct"])}
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
                [{"Trainer": r["name"], "Wins": r["wins"], "Runners": r["total_rides"],
                  "Win %": _pct_cell(r["win_pct"])}
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
    # Grouped by vertical, and offering a whole vertical as a unit, because
    # that is the level at which a sample gets big enough to say anything:
    # Kolkata alone is 14 races, the Eastern vertical is the same 14, but
    # Southern is 135 and Western 371.
    bt_scope = st.selectbox(
        "Backtest scope",
        ["All venues"] + [f"{v} vertical" for v in verticals.VERTICALS]
        + [verticals.label(v) for v in VENUES],
    )
    if bt_scope == "All venues":
        bt_venues = None
    elif bt_scope.endswith(" vertical"):
        bt_venues = list(verticals.VERTICALS[bt_scope[:-len(" vertical")]]["venues"])
    else:
        bt_venues = [bt_scope.split(" -- ")[0]]
    bt_venue = bt_scope
    if st.button("Run backtest"):
        from scripts.backtest import benchmark as bt_benchmark, calibration as bt_calibration, \
            load_backtest_races, signal_patterns, by_day_type as bt_by_day_type
        with st.spinner("Replaying archived races..."):
            if bt_venues is None:
                bt_races = load_backtest_races(conn)
            else:
                bt_races = [r for v in bt_venues for r in load_backtest_races(conn, v)]
            st.session_state["bt"] = {
                "venue": bt_venue, "n": len(bt_races),
                "benchmark": bt_benchmark(bt_races),
                "calibration": bt_calibration(bt_races),
                "patterns": signal_patterns(bt_races),
                "day_types": bt_by_day_type(bt_races),
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
            "These patterns are already folded into the model: weights were re-tuned by grid search "
            "on this archive. Note the headline numbers here are the honest, leak-free ones — an "
            "earlier version of this tab reported ~40% because connection stats were computed from "
            "the whole archive including the race being graded. Rerun after each race weekend."
        )

        st.divider()
        st.markdown("**Are big-crowd days less predictable?**")
        # .get() rather than [] : a session that ran the backtest before this
        # breakdown existed still holds a result dict without the key.
        _day_types = bt.get("day_types") or {}
        st.caption(
            "A common belief is that holidays and feature days produce more upsets, or that such "
            "races are more likely to be manipulated. Tested on this archive it does not hold: "
            "favourites won 43% on special days vs 47% on ordinary ones (z=−0.63, not "
            "significant), and favourites ran *unplaced* less often on special days, not more "
            "(16.7% vs 20.5%, z=−0.78). Bolters were rarer too. Nothing in the scoring changes "
            "for these days — there is no measured effect to encode, and fitting one to noise "
            "would be worse than leaving it alone. This table is here so the question can be "
            "re-answered as the archive grows."
        )
        st.dataframe(
            [{"Day type": k,
              "Races": v["n"],
              "Model top pick": f"{v['model_win_rate']*100:.1f}%" if v["model_win_rate"] is not None else "n/a",
              "Tote favourite": f"{v['favourite_win_rate']*100:.1f}%" if v["favourite_win_rate"] is not None else "n/a"}
             for k, v in sorted(_day_types.items(), key=lambda kv: -kv[1]["n"])],
            use_container_width=True, hide_index=True,
        )
        _hol = _day_types.get("holiday", {}).get("n", 0)
        if _hol < 30:
            st.caption(
                f"⚠️ Only {_hol} holiday race(s) on record — far too few to conclude anything "
                f"either way. Treat that row as a placeholder that fills up over time, not as "
                f"evidence. Note also that results data can show *upsets*; it cannot show "
                f"manipulation. And if a real effect ever emerges, the right response is a "
                f"smaller stake, never a different horse."
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
    if not slate:
        st.info("Load a meeting first (sidebar).")
    else:
        st.markdown("#### Fetch forecast prices (indiarace)")
        st.caption(
            "indiarace publishes Night / Morning / Opening forecast prices for every "
            "Indian venue, across every vertical. This pulls them for **every meeting "
            "loaded on this date** and stores them, so the value ranking on the Race Day "
            "tab and the Jackpot Planner start working. **They are indicative forecasts, "
            "not the live tote board** -- use them to find races worth a look, then confirm "
            "the number on the board before staking. Anything you paste below overrides them."
        )
        st.caption(
            "⏰ These populate **on race day**, not the night before -- a card two days "
            "out will legitimately return nothing."
        )
        st.caption(
            "⚠️ **This feed alone will not unlock EV or parlays.** Measured on a real "
            "Kolkata card: indiarace prices only the front 4-5 runners, i.e. 42-62% of "
            "each field, and the parlay engine needs 80% before it will trust a book. That "
            "guard is right -- de-vigging a partial book invents an edge rather than "
            "removing a margin. To get EV, paste a full set of prices below."
        )
        st.caption(
            "✅ **The Jackpot Planner uses it anyway, and that is not a contradiction.** A "
            "jackpot leg needs the market's running order at the front of the book, not a "
            "de-vigged book -- and the front is exactly the part indiarace prices. Selecting "
            "legs by market rank instead of model rank took a five-leg jackpot from 7.9% to "
            "25.4% on this archive at the same 243 combinations."
        )
        india_venues = sorted({r["venue"] for r in slate})
        if st.button(f"Fetch indiarace odds for {len(india_venues)} loaded meeting(s)",
                     use_container_width=True, key="fetch_india_odds"):
            from scrapers import indiarace as ir
            total, empty_venues = 0, []
            for v in india_venues:
                try:
                    raw = ir.fetch_odds_html(v, date_str, use_cache=False)
                    if not raw:
                        st.write(f"**{v}** -- no odds feed for this venue")
                        continue
                    odds_map = ir.parse_odds(raw)
                    if not odds_map:
                        empty_venues.append(v)
                        continue
                    # The odds page carries no race numbers, so the race a
                    # price belongs to comes from the card we already hold.
                    field_by_race = {}
                    for r in slate:
                        if r["venue"] != v:
                            continue
                        field_by_race[r["race_no"]] = [e["horse_name"] for e in r["entries"]]
                    rows = ir.to_market_rows(odds_map, field_by_race)
                    res = store_market_odds(conn, date_str, v, rows, source="indiarace")
                    total += res["matched"]
                    st.write(f"**{v}** -- priced {res['matched']} runners "
                             f"({len(odds_map)} on the page)")
                    if res["unmatched"]:
                        st.caption(f"{len(res['unmatched'])} name(s) on the odds page "
                                   f"didn't match the loaded field -- usually late scratchings.")
                except Exception as e:
                    st.error(f"{v}: {e}")
            if empty_venues:
                st.warning(
                    f"No prices posted yet for {', '.join(empty_venues)}. indiarace fills "
                    f"this feed on race-day morning, so try again closer to the first race."
                )
            if total:
                st.success(f"Stored forecast prices for {total} runners.")
                st.rerun()
        st.divider()

        priced = sum(1 for r in slate if r["odds"])
        st.caption(
            f"{priced} of {len(slate)} loaded races have prices. Every race without a price is "
            f"invisible to the parlay engine -- there is nothing to measure an edge against."
        )


        st.markdown("#### Paste prices from your bookmaker")
        st.caption(
            "The reliable path, and the only one that works everywhere. Copy the win (and place, "
            "if shown) prices off any screen -- tote board, exchange, bookmaker app -- and paste "
            "them here, one runner per line. Recognised shapes: `MAGIC MOMENT 3.40`, "
            "`7. Magic Moment $3.40 $1.55`, `Magic Moment 5/2`. Header and junk lines are ignored."
        )
        st.caption(
            "**Exchange or board prices beat the indiarace forecast above.** Paste the BACK "
            "price (the one you can actually bet at); an exchange has a much thinner margin "
            "than a tote, so it is a sharper estimate of true chance. A pasted price "
            "overrides the fetched forecast for that runner. Paste as close to the off as "
            "you can -- a price from three hours ago is a different race."
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

    if not any(r["odds"] for r in slate):
        st.info(
            "Parlays need a price to test against, and no race on this slate has one yet. "
            + "Fetch the indiarace forecast prices in the **Odds** tab, or paste a board or "
              "exchange price -- either works the same downstream. Note that indiarace only "
              "posts these on race day."
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
                priced_races, bankroll=bankroll, model_weight=model_weight)

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
                            save_parlay(conn, date_str, "India", pl,
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
