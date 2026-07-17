"""Persist parsed RWITC race card / race result data into the SQLite DB."""
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
            recent_form = ",".join(
                (rr.get("placing") or "?") for rr in run.get("recent_runs", [])
            )
            conn.execute(
                """INSERT INTO runs (race_id, horse_id, jockey, trainer, owner, weight_kg, official_rating, recent_form_text)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                   ON CONFLICT(race_id, horse_id) DO UPDATE SET
                       jockey=excluded.jockey, trainer=excluded.trainer, owner=excluded.owner, weight_kg=excluded.weight_kg,
                       official_rating=excluded.official_rating, recent_form_text=excluded.recent_form_text""",
                (race_id, horse_id, run.get("jockey"), run.get("trainer"), run.get("owner"), run.get("weight_kg"),
                 run.get("official_rating"), recent_form),
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
