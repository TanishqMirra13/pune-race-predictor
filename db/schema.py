"""SQLite schema and connection helper for the Pune race predictor."""
import sqlite3
from pathlib import Path

DB_PATH = Path(__file__).resolve().parent.parent / "data" / "pune_racing.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS horses (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    country_bred TEXT,
    color TEXT,
    sex TEXT,
    sire TEXT,
    dam TEXT,
    current_trainer TEXT,
    breeder TEXT,
    stud TEXT,
    UNIQUE(name, sire, dam)
);

CREATE TABLE IF NOT EXISTS races (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    race_date TEXT NOT NULL,
    venue TEXT NOT NULL,
    race_number INTEGER NOT NULL,
    race_name TEXT,
    class_code TEXT,
    rating_band_min INTEGER,
    rating_band_max INTEGER,
    distance_m INTEGER,
    going TEXT,
    prize_money INTEGER,
    standard_time_sec REAL,
    source_doc TEXT,
    tote_favourite TEXT,
    UNIQUE(race_date, venue, race_number)
);

CREATE TABLE IF NOT EXISTS runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    race_id INTEGER NOT NULL REFERENCES races(id),
    horse_id INTEGER NOT NULL REFERENCES horses(id),
    jockey TEXT,
    trainer TEXT,
    owner TEXT,
    weight_kg REAL,
    draw INTEGER,
    official_rating INTEGER,
    recent_form_text TEXT,
    odds_opening REAL,
    odds_sp REAL,
    scratched INTEGER DEFAULT 0,
    UNIQUE(race_id, horse_id)
);

-- Official season-to-date jockey/trainer statistics, as published by each
-- club (RWITC's jockeyStatistics.php/trainerStatistics.php, BTC's
-- Home/JockeyStats and home/trainerstats). Refreshed periodically via
-- scraper.fetch_*_stats_html() -- these are snapshots, not per-race-day
-- history, so each (venue, name, role) pair is just overwritten on refresh.
CREATE TABLE IF NOT EXISTS connection_stats (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    venue TEXT NOT NULL,
    role TEXT NOT NULL CHECK(role IN ('jockey', 'trainer', 'owner', 'breeder')),
    name TEXT NOT NULL,
    wins INTEGER,
    seconds INTEGER,
    thirds INTEGER,
    fourths INTEGER,
    total_rides INTEGER,
    win_pct REAL,
    fetched_at TEXT DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(venue, role, name)
);

-- Morning trackwork (indiarace.com) and mock races (racingpulse.in) --
-- pre-race fitness/readiness evidence. horse_name is stored as printed by
-- the source (matched case-insensitively to horses.name at scoring time);
-- vs_par is the source's own seconds-vs-par figure (negative = faster).
CREATE TABLE IF NOT EXISTS workouts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    kind TEXT NOT NULL CHECK(kind IN ('trackwork', 'mock')),
    venue TEXT NOT NULL,
    work_date TEXT NOT NULL,
    distance_m INTEGER,
    horse_name TEXT NOT NULL,
    rider TEXT,
    time_sec REAL,
    vs_par REAL,
    finish_pos INTEGER,
    field_size INTEGER,
    comment TEXT,
    UNIQUE(kind, venue, work_date, distance_m, horse_name)
);

CREATE TABLE IF NOT EXISTS results (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL UNIQUE REFERENCES runs(id),
    finish_position INTEGER,
    margin_lengths REAL,
    time_sec REAL,
    sectional_times TEXT,
    dividend_win REAL,
    dividend_place REAL
);

CREATE TABLE IF NOT EXISTS ratings_history (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    horse_id INTEGER NOT NULL REFERENCES horses(id),
    as_of_date TEXT NOT NULL,
    official_rating INTEGER,
    speed_figure REAL,
    composite_score REAL,
    source TEXT
);

CREATE TABLE IF NOT EXISTS bankroll_log (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    race_date TEXT NOT NULL,
    venue TEXT NOT NULL,
    budget REAL NOT NULL,
    goal REAL NOT NULL,
    race_id INTEGER REFERENCES races(id),
    race_no INTEGER,
    bet_type TEXT NOT NULL,
    selection TEXT NOT NULL,
    stake REAL NOT NULL,
    odds_taken REAL,
    predicted_probability REAL,
    outcome TEXT DEFAULT 'pending',
    payout REAL,
    notes TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);
"""


def get_connection(db_path: Path = DB_PATH) -> sqlite3.Connection:
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(db_path: Path = DB_PATH) -> None:
    conn = get_connection(db_path)
    try:
        conn.executescript(SCHEMA)
        _migrate(conn)
        conn.commit()
    finally:
        conn.close()


def _migrate(conn: sqlite3.Connection) -> None:
    """Idempotent ALTER TABLEs for columns added after initial release."""
    run_cols = {row["name"] for row in conn.execute("PRAGMA table_info(runs)")}
    if "owner" not in run_cols:
        conn.execute("ALTER TABLE runs ADD COLUMN owner TEXT")

    horse_cols = {row["name"] for row in conn.execute("PRAGMA table_info(horses)")}
    if "breeder" not in horse_cols:
        conn.execute("ALTER TABLE horses ADD COLUMN breeder TEXT")
    if "stud" not in horse_cols:
        conn.execute("ALTER TABLE horses ADD COLUMN stud TEXT")

    race_cols = {row["name"] for row in conn.execute("PRAGMA table_info(races)")}
    if "tote_favourite" not in race_cols:
        conn.execute("ALTER TABLE races ADD COLUMN tote_favourite TEXT")


if __name__ == "__main__":
    init_db()
    print(f"Initialized DB at {DB_PATH}")
