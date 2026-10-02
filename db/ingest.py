"""Persist parsed race card / race result / odds data into the SQLite DB.

Every Indian source -- RWITC, BTC and indiarace -- emits the same parsed
shapes, so one storage path serves all seven venues and the layer never needs
to know which club a card came from. The pieces beyond the card/result tables
are market_odds (prices, whether fetched or pasted), odds_snapshots (the same
prices kept by stage so the model can eventually be judged against a bettable
number) and pool_dividends (the multi-leg tote pools, which nothing else in
the pipeline records)."""
import json
import sqlite3


def _get_or_create_horse(conn: sqlite3.Connection, name: str) -> int:
    row = conn.execute("SELECT id FROM horses WHERE name = ?", (name,)).fetchone()
    if row:
        return row["id"]
    cur = conn.execute("INSERT INTO horses (name) VALUES (?)", (name,))
    return cur.lastrowid


def store_connection_stats(conn: sqlite3.Connection, venue: str, role: str, stats: list[dict]) -> None:
    for s in stats:
        conn.execute(
            """INSERT INTO connection_stats (venue, role, name, wins, seconds, thirds, fourths, total_rides, win_pct)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(venue, role, name) DO UPDATE SET
                   wins=excluded.wins, seconds=excluded.seconds, thirds=excluded.thirds, fourths=excluded.fourths,
                   total_rides=excluded.total_rides, win_pct=excluded.win_pct, fetched_at=CURRENT_TIMESTAMP""",
            (venue, role, s["name"], s["wins"], s["seconds"], s["thirds"], s["fourths"],
             s["total_rides"], s["win_pct"]),
        )
    conn.commit()


def store_workouts(conn: sqlite3.Connection, records: list[dict]) -> int:
    n = 0
    for r in records:
        cur = conn.execute(
            """INSERT INTO workouts (kind, venue, work_date, distance_m, horse_name, rider,
                                      time_sec, vs_par, finish_pos, field_size, comment)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(kind, venue, work_date, distance_m, horse_name) DO UPDATE SET
                   rider=excluded.rider, time_sec=excluded.time_sec, vs_par=excluded.vs_par,
                   finish_pos=excluded.finish_pos, field_size=excluded.field_size,
                   comment=excluded.comment""",
            (r["kind"], r["venue"], r["work_date"], r["distance_m"], r["horse_name"],
             r.get("rider"), r.get("time_sec"), r.get("vs_par"), r.get("finish_pos"),
             r.get("field_size"), r.get("comment")),
        )
        n += 1
    conn.commit()
    return n


def store_racecard(conn: sqlite3.Connection, race_date: str, venue: str, races: list[dict]) -> None:
    # circuit is written here rather than left to schema._migrate's backfill.
    # Every reader filters on circuit='India', and the backfill only runs at
    # startup -- so a card loaded for the first time was invisible to the very
    # run that loaded it, and the daily routine reported "nothing loaded".
    for race in races:
        conn.execute(
            """INSERT INTO races (race_date, venue, race_number, race_name, class_code,
                                   rating_band_min, rating_band_max, distance_m, source_doc, circuit)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'racecard', 'India')
               ON CONFLICT(race_date, venue, race_number) DO UPDATE SET
                   race_name=excluded.race_name, class_code=excluded.class_code,
                   rating_band_min=excluded.rating_band_min, rating_band_max=excluded.rating_band_max,
                   distance_m=excluded.distance_m""",
            (race_date, venue, int(race["race_no"]), race["race_name"], race.get("class_code"),
             race.get("rating_band_min"), race.get("rating_band_max"), race.get("distance_m")),
        )
        race_id = conn.execute(
            "SELECT id FROM races WHERE race_date=? AND venue=? AND race_number=?",
            (race_date, venue, int(race["race_no"])),
        ).fetchone()["id"]

        for run in race["runs"]:
            horse_id = _get_or_create_horse(conn, run["horse_name"])
            if run.get("trainer"):
                conn.execute("UPDATE horses SET current_trainer=? WHERE id=?", (run["trainer"], horse_id))
            if run.get("breeder") or run.get("stud"):
                conn.execute(
                    "UPDATE horses SET breeder=COALESCE(?, breeder), stud=COALESCE(?, stud) WHERE id=?",
                    (run.get("breeder"), run.get("stud"), horse_id),
                )
            if run.get("sire") or run.get("dam"):
                conn.execute(
                    "UPDATE horses SET sire=COALESCE(?, sire), dam=COALESCE(?, dam) WHERE id=?",
                    (run.get("sire"), run.get("dam"), horse_id),
                )
            recent_form = ",".join(
                (rr.get("placing") or "?") for rr in run.get("recent_runs", [])
            )
            # recent_form_text (bare placings, e.g. "2,1,4,?") is kept for
            # backward compatibility -- recent_runs_json is the same data
            # without the class/distance/date/time fields collapsed away,
            # which models/rating_engine.py's distance/class-aware form
            # scoring reads instead. Source richness varies: RWITC has all of
            # it, indiarace-sourced venues have placings only, BTC has none.
            recent_runs_json = json.dumps(run.get("recent_runs", []))
            equipment_codes = ",".join(run.get("equipment_codes") or []) or None
            conn.execute(
                """INSERT INTO runs (race_id, horse_id, jockey, trainer, owner, weight_kg, official_rating,
                                      recent_form_text, apprentice_allowance, equipment_raw, equipment_codes,
                                      assessed_rating, assessed_rating_date, recent_runs_json)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(race_id, horse_id) DO UPDATE SET
                       jockey=excluded.jockey, trainer=excluded.trainer, owner=excluded.owner, weight_kg=excluded.weight_kg,
                       official_rating=excluded.official_rating, recent_form_text=excluded.recent_form_text,
                       apprentice_allowance=excluded.apprentice_allowance, equipment_raw=excluded.equipment_raw,
                       equipment_codes=excluded.equipment_codes, assessed_rating=excluded.assessed_rating,
                       assessed_rating_date=excluded.assessed_rating_date, recent_runs_json=excluded.recent_runs_json""",
                (race_id, horse_id, run.get("jockey"), run.get("trainer"), run.get("owner"), run.get("weight_kg"),
                 run.get("official_rating"), recent_form, run.get("apprentice_allowance"), run.get("equipment_raw"),
                 equipment_codes, run.get("assessed_rating"), run.get("assessed_rating_date"), recent_runs_json),
            )
    conn.commit()


def store_raceresult(conn: sqlite3.Connection, race_date: str, venue: str, races: list[dict]) -> None:
    for race in races:
        conn.execute(
            """INSERT INTO races (race_date, venue, race_number, race_name, class_code, distance_m,
                                   source_doc, circuit)
               VALUES (?, ?, ?, ?, ?, ?, 'raceresult', 'India')
               ON CONFLICT(race_date, venue, race_number) DO UPDATE SET
                   race_name=excluded.race_name, class_code=excluded.class_code, distance_m=excluded.distance_m""",
            (race_date, venue, int(race["race_no"]), race["race_name"], race.get("class_code"),
             race.get("distance_m")),
        )
        race_id = conn.execute(
            "SELECT id FROM races WHERE race_date=? AND venue=? AND race_number=?",
            (race_date, venue, int(race["race_no"])),
        ).fetchone()["id"]

        div = race.get("dividends", {})
        if div.get("favourite"):
            conn.execute("UPDATE races SET tote_favourite=? WHERE id=?", (div["favourite"], race_id))
        for runner in race["runners"]:
            horse_id = _get_or_create_horse(conn, runner["horse_name"])
            conn.execute(
                """INSERT INTO runs (race_id, horse_id, jockey, trainer, weight_kg, odds_sp, scratched)
                   VALUES (?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(race_id, horse_id) DO UPDATE SET
                       jockey=excluded.jockey, trainer=excluded.trainer, weight_kg=excluded.weight_kg,
                       odds_sp=COALESCE(excluded.odds_sp, runs.odds_sp), scratched=excluded.scratched""",
                (race_id, horse_id, runner.get("jockey"), runner.get("trainer"),
                 runner.get("weight_kg"), _odds_to_decimal(runner.get("odds")),
                 int(runner.get("scratched", False))),
            )
            run_id = conn.execute(
                "SELECT id FROM runs WHERE race_id=? AND horse_id=?", (race_id, horse_id)
            ).fetchone()["id"]

            win_div = div.get("win") if runner.get("placing") == 1 else None
            time_sec = _parse_race_time(runner.get("time"))
            conn.execute(
                """INSERT INTO results (run_id, finish_position, time_sec, dividend_win)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(run_id) DO UPDATE SET
                       finish_position=excluded.finish_position, time_sec=excluded.time_sec,
                       dividend_win=excluded.dividend_win""",
                (run_id, runner.get("placing"), time_sec, _to_float(win_div)),
            )
    conn.commit()


def store_pool_dividends(conn: sqlite3.Connection, race_date: str, venue: str,
                         pools: list[dict]) -> int:
    """Store the meeting's multi-leg pool settlements (jackpot, trebles).

    Meeting-level rather than per-race, which is why it is a separate call
    from store_raceresult: a jackpot belongs to the card, not to any one race.

    Upsert on (date, venue, pool, tier) so re-loading a day's results just
    refreshes. Rows with neither a dividend nor a carry-forward are dropped --
    those are a parse that found the table but not the numbers, and an empty
    row would quietly pollute the dividend distribution the planner reads."""
    stored = 0
    for p in pools:
        if p.get("dividend") is None and p.get("carried_forward") is None:
            continue
        conn.execute(
            """INSERT INTO pool_dividends
                   (race_date, venue, pool, tier, legs, winners, dividend, tickets, carried_forward)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(race_date, venue, pool, tier) DO UPDATE SET
                   legs=excluded.legs, winners=excluded.winners,
                   dividend=excluded.dividend, tickets=excluded.tickets,
                   carried_forward=excluded.carried_forward""",
            (race_date, venue, p["pool"], p.get("tier", "main"), p.get("legs"),
             p.get("winners"), p.get("dividend"), p.get("tickets"),
             p.get("carried_forward")),
        )
        stored += 1
    conn.commit()
    return stored


def _upsert_market_odds(conn: sqlite3.Connection, race_id: int, horse_id: int,
                        market: str, source: str, decimal_odds: float) -> None:
    conn.execute(
        """INSERT INTO market_odds (race_id, horse_id, market, source, decimal_odds)
           VALUES (?, ?, ?, ?, ?)
           ON CONFLICT(race_id, horse_id, market, source) DO UPDATE SET
               decimal_odds=excluded.decimal_odds, captured_at=CURRENT_TIMESTAMP""",
        (race_id, horse_id, market, source, decimal_odds),
    )


def store_market_odds(conn: sqlite3.Connection, race_date: str, venue: str,
                      rows: list[dict], source: str = "manual") -> dict:
    """Store quoted prices for a day's runners.

    rows: [{'race_no', 'horse_name', 'win': decimal, 'place': decimal or None}]

    Horse names are matched case-insensitively against runners already declared
    in that race; anything unmatched is REPORTED BACK rather than silently
    dropped, because a typo in a pasted name would otherwise quietly remove a
    horse from the day's analysis and nobody would notice."""
    matched, unmatched = 0, []
    for row in rows:
        race = conn.execute(
            "SELECT id FROM races WHERE race_date=? AND venue=? AND race_number=?",
            (race_date, venue, row.get("race_no")),
        ).fetchone()
        if not race:
            unmatched.append({"horse_name": row.get("horse_name"), "reason": f"no race {row.get('race_no')} loaded"})
            continue
        hit = conn.execute(
            """SELECT r.horse_id FROM runs r JOIN horses h ON h.id = r.horse_id
               WHERE r.race_id = ? AND UPPER(h.name) = UPPER(?)""",
            (race["id"], (row.get("horse_name") or "").strip()),
        ).fetchone()
        if not hit:
            unmatched.append({"horse_name": row.get("horse_name"), "reason": "name not in this race's field"})
            continue
        wrote = False
        for market in ("win", "place"):
            price = row.get(market)
            if price and price > 1.0:
                _upsert_market_odds(conn, race["id"], hit["horse_id"], market, source, float(price))
                wrote = True
        if wrote:
            matched += 1
    conn.commit()
    return {"matched": matched, "unmatched": unmatched}


def load_market_odds(conn: sqlite3.Connection, race_id: int,
                     prefer: tuple = ("manual", "api", "indiarace", "sp")) -> dict:
    """{HORSE NAME: {'win': d, 'place': d}} for one race.

    Sources are tried in preference order, and the order encodes whose price
    you could actually have got on. 'manual' is the board or exchange price
    you typed in yourself; 'api' a book you have configured; 'indiarace' the
    race-day forecast, which is INDICATIVE rather than a live board and so
    ranks below anything you sourced yourself -- good for finding races worth
    a look, poor for deciding a stake; 'sp' the settled starting price, last
    because it cannot be bet at all. By the time an SP exists the race has
    run, so treating it as a live quote would invent an opportunity that never
    existed."""
    rows = conn.execute(
        """SELECT h.name, mo.market, mo.source, mo.decimal_odds
           FROM market_odds mo JOIN horses h ON h.id = mo.horse_id
           WHERE mo.race_id = ?""",
        (race_id,),
    ).fetchall()
    rank = {s: i for i, s in enumerate(prefer)}
    best: dict = {}
    for r in rows:
        name = r["name"].upper()
        slot = best.setdefault(name, {"win": None, "place": None, "_rank": {}})
        pos = rank.get(r["source"], len(prefer))
        if slot["_rank"].get(r["market"], 999) >= pos:
            slot[r["market"]] = r["decimal_odds"]
            slot["_rank"][r["market"]] = pos
    for slot in best.values():
        slot.pop("_rank", None)
    return best


def snapshot_odds(conn: sqlite3.Connection, race_date: str, venue: str,
                  odds_map: dict, field_by_race: dict, source: str = "indiarace") -> dict:
    """Record every quoted stage for a runner into odds_snapshots.

    Unlike store_market_odds -- which keeps one current best quote per source
    so the EV engine has a single number to price against -- this keeps the
    whole night/morning/opening path, because the POINT is to compare an early
    price against the settled one later. See the odds_snapshots comment in
    db/schema.py for why that comparison is the open question.

    odds_map: {UPPER NAME: {'stages': {'night': odds-to-one, ...}}} as
    scrapers/indiarace.py's parse_odds returns it.
    field_by_race: {race_no: [horse names on our card]} -- the odds page has
    no race numbers, so the mapping comes from the card we hold.

    Stage values are converted odds-to-one -> decimal here for the same reason
    to_market_rows does it: everything stored is decimal, and mixing the two
    silently corrupts every probability derived from it.
    """
    written, races_seen = 0, 0
    for race_no, horses in (field_by_race or {}).items():
        race = conn.execute(
            "SELECT id FROM races WHERE race_date=? AND venue=? AND race_number=?",
            (race_date, venue, race_no),
        ).fetchone()
        if not race:
            continue
        races_seen += 1
        for horse in horses:
            info = odds_map.get((horse or "").strip().upper())
            if not info:
                continue
            hit = conn.execute(
                """SELECT r.horse_id FROM runs r JOIN horses h ON h.id = r.horse_id
                   WHERE r.race_id = ? AND UPPER(h.name) = UPPER(?)""",
                (race["id"], horse.strip()),
            ).fetchone()
            if not hit:
                continue
            for stage, odds_to_one in (info.get("stages") or {}).items():
                if odds_to_one is None:
                    continue
                conn.execute(
                    """INSERT INTO odds_snapshots (race_id, horse_id, source, stage, decimal_odds)
                       VALUES (?, ?, ?, ?, ?)
                       ON CONFLICT(race_id, horse_id, source, stage) DO UPDATE SET
                           decimal_odds=excluded.decimal_odds, captured_at=CURRENT_TIMESTAMP""",
                    (race["id"], hit["horse_id"], source, stage, float(odds_to_one) + 1.0),
                )
                written += 1
    conn.commit()
    return {"stages_written": written, "races": races_seen}


def snapshot_settle_sp(conn: sqlite3.Connection, race_date: str, venue: str | None = None) -> int:
    """Copy settled starting prices into odds_snapshots as stage='sp'.

    Run after results are loaded. This is what turns a pile of forecast quotes
    into an answerable question: for each runner we then hold both the price
    that was on offer early and the price it actually went off at."""
    q = """SELECT r.race_id, r.horse_id, r.odds_sp FROM runs r
           JOIN races ra ON ra.id = r.race_id
           WHERE ra.race_date = ? AND r.odds_sp IS NOT NULL AND r.scratched = 0"""
    params: list = [race_date]
    if venue:
        q += " AND ra.venue = ?"
        params.append(venue)
    n = 0
    for row in conn.execute(q, params).fetchall():
        conn.execute(
            """INSERT INTO odds_snapshots (race_id, horse_id, source, stage, decimal_odds)
               VALUES (?, ?, 'sp', 'sp', ?)
               ON CONFLICT(race_id, horse_id, source, stage) DO UPDATE SET
                   decimal_odds=excluded.decimal_odds, captured_at=CURRENT_TIMESTAMP""",
            (row["race_id"], row["horse_id"], float(row["odds_sp"]) + 1.0),
        )
        n += 1
    conn.commit()
    return n


def odds_age_minutes(conn: sqlite3.Connection, race_id: int,
                     exclude_sources: tuple = ("sp",)) -> float | None:
    """Age in minutes of the freshest bettable price stored for a race.

    Prices move constantly, and a stale one is worse than none at all: the
    engine will happily report an edge against a number nobody is offering any
    more, and that edge is pure fiction. Settled starting prices are excluded
    because they have no meaningful age -- they are final by definition.
    Returns None when no bettable price is stored."""
    placeholders = ",".join("?" * len(exclude_sources))
    row = conn.execute(
        f"""SELECT (julianday('now') - julianday(MAX(captured_at))) * 24 * 60 AS age
            FROM market_odds
            WHERE race_id = ? AND source NOT IN ({placeholders})""",
        (race_id, *exclude_sources),
    ).fetchone()
    if not row or row["age"] is None:
        return None
    return max(float(row["age"]), 0.0)


# --------------------------------------------------------------------------
# Parlays
# --------------------------------------------------------------------------

def save_parlay(conn: sqlite3.Connection, race_date: str, circuit: str, parlay: dict,
                stake: float | None = None, placed: bool = False, notes: str = "") -> int:
    """Persist a suggested multi and its legs so the followup can settle it."""
    stake = stake if stake is not None else parlay.get("suggested_stake")
    cur = conn.execute(
        """INSERT INTO parlays (race_date, circuit, label, kind, stake, combined_odds,
                                 hit_probability, expected_value, placed, notes)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (race_date, circuit, parlay.get("label"), f"{parlay.get('leg_count')}-leg", stake,
         parlay.get("combined_odds"), parlay.get("hit_probability"),
         parlay.get("expected_value"), int(placed), notes),
    )
    parlay_id = cur.lastrowid
    for i, leg in enumerate(parlay.get("legs", []), start=1):
        conn.execute(
            """INSERT INTO parlay_legs (parlay_id, leg_no, race_id, venue, race_no, horse_name,
                                         market, decimal_odds, model_probability,
                                         market_probability, blended_probability)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (parlay_id, i, leg.get("race_id"), leg.get("venue"), leg.get("race_no"),
             leg.get("horse_name"), leg.get("market"), leg.get("decimal_odds"),
             leg.get("model_probability"), leg.get("market_probability"),
             leg.get("blended_probability")),
        )
    conn.commit()
    return parlay_id


def settle_parlays(conn: sqlite3.Connection, race_date: str) -> dict:
    """Grade every pending multi for a date against stored results.

    A multi is won only when every leg wins; one lost leg settles the whole
    slip immediately. Legs whose race has no result yet leave the slip pending
    rather than resolving it early -- a half-graded slip that looks settled is
    worse than one that plainly is not."""
    summary = {"settled": 0, "won": 0, "lost": 0, "pending": 0, "staked": 0.0, "returned": 0.0}
    parlays = conn.execute(
        "SELECT * FROM parlays WHERE race_date=? AND status='pending'", (race_date,)
    ).fetchall()

    for p in parlays:
        legs = conn.execute(
            "SELECT * FROM parlay_legs WHERE parlay_id=? ORDER BY leg_no", (p["id"],)
        ).fetchall()
        outcomes = []
        for leg in legs:
            outcome = _grade_leg(conn, leg)
            outcomes.append(outcome)
            if outcome != leg["outcome"]:
                conn.execute("UPDATE parlay_legs SET outcome=? WHERE id=?", (outcome, leg["id"]))

        if "lost" in outcomes:
            status, payout = "lost", 0.0
        elif all(o == "won" for o in outcomes) and outcomes:
            status = "won"
            payout = (p["stake"] or 0) * (p["combined_odds"] or 0)
        else:
            summary["pending"] += 1
            continue

        conn.execute("UPDATE parlays SET status=?, payout=? WHERE id=?", (status, payout, p["id"]))
        summary["settled"] += 1
        summary[status] += 1
        summary["staked"] += p["stake"] or 0
        summary["returned"] += payout
    conn.commit()
    summary["pnl"] = round(summary["returned"] - summary["staked"], 2)
    return summary


def _grade_leg(conn: sqlite3.Connection, leg) -> str:
    """'won' / 'lost' / 'pending' for one leg.

    Place legs are graded against the number of places the field actually
    paid, computed from the DECLARED (non-scratched) runner count -- a late
    scratching can drop a race from three places to two, which decides
    borderline slips."""
    if not leg["race_id"]:
        return "pending"
    row = conn.execute(
        """SELECT res.finish_position FROM runs r
           JOIN horses h ON h.id = r.horse_id
           LEFT JOIN results res ON res.run_id = r.id
           WHERE r.race_id = ? AND UPPER(h.name) = UPPER(?)""",
        (leg["race_id"], leg["horse_name"]),
    ).fetchone()
    if not row or row["finish_position"] is None:
        any_result = conn.execute(
            """SELECT COUNT(*) n FROM runs r JOIN results res ON res.run_id = r.id
               WHERE r.race_id = ? AND res.finish_position IS NOT NULL""",
            (leg["race_id"],),
        ).fetchone()["n"]
        # The race has been settled but our horse has no position: scratched or
        # failed to finish. Either way the leg is lost.
        return "lost" if any_result else "pending"

    position = row["finish_position"]
    if leg["market"] == "win":
        return "won" if position == 1 else "lost"

    field_size = conn.execute(
        "SELECT COUNT(*) n FROM runs WHERE race_id=? AND scratched=0", (leg["race_id"],)
    ).fetchone()["n"]
    from models.odds import places_paid
    n_places = places_paid(field_size)
    if n_places == 0:
        return "lost"
    return "won" if position <= n_places else "lost"


def _parse_race_time(t: str | None):
    if not t:
        return None
    parts = t.replace(".", ":").split(":")
    if len(parts) != 3:
        return None
    try:
        minutes, seconds, hundredths = (int(p) for p in parts)
        return minutes * 60 + seconds + hundredths / 100
    except ValueError:
        return None


def _to_float(v):
    if v is None:
        return None
    try:
        return float(v)
    except ValueError:
        return None


def _odds_to_decimal(v: str | None):
    """Normalize the odds text both clubs print. RWITC uses plain decimals
    ('2', '0.70'); BTC uses fractions ('13/10', '95/100') and decimals.
    Returns odds-to-one as a float, or None if unparseable/withdrawn."""
    if not v:
        return None
    v = v.strip()
    if "/" in v:
        num, _, den = v.partition("/")
        try:
            return float(num) / float(den)
        except (ValueError, ZeroDivisionError):
            return None
    try:
        return float(v)
    except ValueError:
        return None
