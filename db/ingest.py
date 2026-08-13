"""Persist parsed race card / race result / odds data into the SQLite DB.

Originally RWITC-only; now also carries the Australian and Hong Kong circuits,
whose parsers deliberately emit the same shapes so the storage layer barely had
to change. The genuinely new pieces are market_odds (there was no market to
store before) and the parlay tables."""
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
    for race in races:
        conn.execute(
            """INSERT INTO races (race_date, venue, race_number, race_name, class_code,
                                   rating_band_min, rating_band_max, distance_m, source_doc)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'racecard')
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
            """INSERT INTO races (race_date, venue, race_number, race_name, class_code, distance_m, source_doc)
               VALUES (?, ?, ?, ?, ?, ?, 'raceresult')
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


def store_intl_racecard(conn: sqlite3.Connection, race_date: str, venue: str, circuit: str,
                        races: list[dict], country: str | None = None,
                        source_key: str | None = None) -> int:
    """Store an Australian or Hong Kong card.

    Same job as store_racecard, but carries the extra columns those circuits
    give us -- barrier, saddlecloth, race time, track condition -- and tags
    every race with its circuit so the Indian cards stay in their own world."""
    stored = 0
    for race in races:
        if race.get("race_no") is None:
            continue
        conn.execute(
            """INSERT INTO races (race_date, venue, race_number, race_name, class_code,
                                   rating_band_min, rating_band_max, distance_m, going,
                                   prize_money, circuit, country, race_time_local,
                                   track_condition, source_key, source_doc)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'racecard')
               ON CONFLICT(race_date, venue, race_number) DO UPDATE SET
                   race_name=excluded.race_name, class_code=excluded.class_code,
                   rating_band_min=excluded.rating_band_min, rating_band_max=excluded.rating_band_max,
                   distance_m=excluded.distance_m, going=excluded.going,
                   prize_money=excluded.prize_money, circuit=excluded.circuit,
                   country=excluded.country, race_time_local=excluded.race_time_local,
                   track_condition=excluded.track_condition, source_key=excluded.source_key""",
            (race_date, venue, int(race["race_no"]), race.get("race_name"), race.get("class_code"),
             race.get("rating_band_min"), race.get("rating_band_max"), race.get("distance_m"),
             race.get("going"), race.get("prize_money"), circuit, country,
             race.get("race_time_local"), race.get("track_condition"), source_key),
        )
        race_id = conn.execute(
            "SELECT id FROM races WHERE race_date=? AND venue=? AND race_number=?",
            (race_date, venue, int(race["race_no"])),
        ).fetchone()["id"]

        for run in race.get("runs", []):
            horse_id = _get_or_create_horse(conn, run["horse_name"])
            if run.get("trainer"):
                conn.execute("UPDATE horses SET current_trainer=? WHERE id=?", (run["trainer"], horse_id))
            recent_form = ",".join((rr.get("placing") or "?") for rr in run.get("recent_runs", []))
            conn.execute(
                """INSERT INTO runs (race_id, horse_id, jockey, trainer, owner, weight_kg, draw,
                                      barrier, saddlecloth, official_rating, recent_form_text, scratched)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(race_id, horse_id) DO UPDATE SET
                       jockey=excluded.jockey, trainer=excluded.trainer, owner=excluded.owner,
                       weight_kg=excluded.weight_kg, draw=excluded.draw, barrier=excluded.barrier,
                       saddlecloth=excluded.saddlecloth, official_rating=excluded.official_rating,
                       recent_form_text=excluded.recent_form_text, scratched=excluded.scratched""",
                (race_id, horse_id, run.get("jockey"), run.get("trainer"), run.get("owner"),
                 run.get("weight_kg"), run.get("draw"), run.get("draw"), run.get("saddlecloth"),
                 run.get("official_rating"), recent_form, int(bool(run.get("scratched")))),
            )
            stored += 1
    conn.commit()
    return stored


def store_intl_results(conn: sqlite3.Connection, race_date: str, venue: str, circuit: str,
                       races: list[dict], country: str | None = None) -> int:
    """Store AU/HK results, including each runner's starting price.

    The SP goes into runs.odds_sp AND market_odds under source 'sp'. The
    duplication is deliberate: odds_sp keeps the existing backtest code
    working unchanged, while market_odds is the general table the EV engine
    reads, and keeping the two in step here avoids a subtle class of bug where
    calibration and betting disagree about what the market said."""
    stored = 0
    for race in races:
        if race.get("race_no") is None:
            continue
        conn.execute(
            """INSERT INTO races (race_date, venue, race_number, race_name, class_code, distance_m,
                                   going, circuit, country, track_condition, source_doc)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'raceresult')
               ON CONFLICT(race_date, venue, race_number) DO UPDATE SET
                   race_name=COALESCE(excluded.race_name, races.race_name),
                   class_code=COALESCE(excluded.class_code, races.class_code),
                   distance_m=COALESCE(excluded.distance_m, races.distance_m),
                   going=COALESCE(excluded.going, races.going),
                   circuit=excluded.circuit, country=COALESCE(excluded.country, races.country)""",
            (race_date, venue, int(race["race_no"]), race.get("race_name"), race.get("class_code"),
             race.get("distance_m"), race.get("going"), circuit, country, race.get("track_condition")),
        )
        race_id = conn.execute(
            "SELECT id FROM races WHERE race_date=? AND venue=? AND race_number=?",
            (race_date, venue, int(race["race_no"])),
        ).fetchone()["id"]

        fav = (race.get("dividends") or {}).get("favourite")
        if fav:
            conn.execute("UPDATE races SET tote_favourite=? WHERE id=?", (fav, race_id))

        for runner in race.get("runners", []):
            horse_id = _get_or_create_horse(conn, runner["horse_name"])
            decimal_odds = runner.get("decimal_odds")
            conn.execute(
                """INSERT INTO runs (race_id, horse_id, jockey, trainer, weight_kg, draw, barrier,
                                      saddlecloth, odds_sp, scratched)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(race_id, horse_id) DO UPDATE SET
                       jockey=COALESCE(excluded.jockey, runs.jockey),
                       trainer=COALESCE(excluded.trainer, runs.trainer),
                       weight_kg=COALESCE(excluded.weight_kg, runs.weight_kg),
                       draw=COALESCE(excluded.draw, runs.draw),
                       barrier=COALESCE(excluded.barrier, runs.barrier),
                       saddlecloth=COALESCE(excluded.saddlecloth, runs.saddlecloth),
                       odds_sp=COALESCE(excluded.odds_sp, runs.odds_sp),
                       scratched=excluded.scratched""",
                (race_id, horse_id, runner.get("jockey"), runner.get("trainer"),
                 runner.get("weight_kg"), runner.get("draw"), runner.get("draw"),
                 runner.get("saddlecloth"),
                 (decimal_odds - 1) if decimal_odds and decimal_odds > 1 else None,
                 int(bool(runner.get("scratched")))),
            )
            run_id = conn.execute(
                "SELECT id FROM runs WHERE race_id=? AND horse_id=?", (race_id, horse_id)
            ).fetchone()["id"]
            conn.execute(
                """INSERT INTO results (run_id, finish_position, time_sec, dividend_place)
                   VALUES (?, ?, ?, ?)
                   ON CONFLICT(run_id) DO UPDATE SET
                       finish_position=excluded.finish_position,
                       time_sec=COALESCE(excluded.time_sec, results.time_sec),
                       dividend_place=COALESCE(excluded.dividend_place, results.dividend_place)""",
                (run_id, runner.get("placing"), _parse_race_time(runner.get("time")),
                 runner.get("dividend_place")),
            )
            if decimal_odds and decimal_odds > 1:
                _upsert_market_odds(conn, race_id, horse_id, "win", "sp", decimal_odds)
            if runner.get("dividend_place"):
                _upsert_market_odds(conn, race_id, horse_id, "place", "sp", runner["dividend_place"])
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
                     prefer: tuple = ("manual", "api", "live", "tabnz", "sp")) -> dict:
    """{HORSE NAME: {'win': d, 'place': d}} for one race.

    Sources are tried in preference order, and the order encodes whose price
    you can actually get on: 'manual' is what you saw at your own book, 'api'
    your configured book, 'live' HKJC's official odds, 'tabnz' NZ TAB used as
    a proxy for the Australian market, and 'sp' a settled starting price.
    'sp' ranks last because it cannot be bet -- by the time it exists the race
    has run -- so treating it as a live quote would invent an opportunity that
    never existed. 'tabnz' ranks below anything you sourced yourself for the
    same reason in miniature: it is a real price, but not necessarily one your
    bookmaker is offering."""
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
    circuit = conn.execute(
        "SELECT circuit FROM races WHERE id=?", (leg["race_id"],)
    ).fetchone()
    n_places = places_paid(field_size, (circuit["circuit"] if circuit else "Australia") or "Australia")
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
