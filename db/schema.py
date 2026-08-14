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

-- Market prices, one row per (race, horse, market, source). Unlike the Indian
-- clubs -- which publish no pre-race odds at all -- Racing Australia prints a
-- starting price for every runner and HKJC publishes live win odds, so for
-- the AU/HK circuits we finally have a market to price ourselves against.
-- 'source' distinguishes a live bookmaker board from a settled starting
-- price, because only the former is bettable and only the latter is honest
-- for backtesting.
CREATE TABLE IF NOT EXISTS market_odds (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    race_id INTEGER NOT NULL REFERENCES races(id),
    horse_id INTEGER NOT NULL REFERENCES horses(id),
    market TEXT NOT NULL CHECK(market IN ('win', 'place')),
    source TEXT NOT NULL,
    decimal_odds REAL NOT NULL,
    captured_at TEXT DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(race_id, horse_id, market, source)
);

-- Price history per runner, as opposed to market_odds which holds only the
-- current best quote per source. This table exists to answer ONE question the
-- archive currently cannot: does this model beat the price you could actually
-- have taken?
--
-- Every accuracy measurement so far benchmarks the model against the FINAL
-- starting price, which is the sharpest number in existence -- it contains all
-- the late money. Losing to it (26.8% vs 48.5%) is expected of almost any
-- model. The open question is whether the model beats the EARLY price, the one
-- actually on offer when you would bet. Nobody archives Indian forecast prices,
-- so that comparison has never been possible; from now on each race day's
-- night/morning/opening quotes are kept here alongside the settled SP, and
-- scripts/early_price.py reports the comparison once enough days accumulate.
--
-- stage is the source's own label ('night'/'morning'/'opening'), plus 'sp'
-- for the settled starting price copied across after results land. One row
-- per named stage per runner, upserted, since the labels are fixed rather
-- than arbitrary capture times.
CREATE TABLE IF NOT EXISTS odds_snapshots (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    race_id INTEGER NOT NULL REFERENCES races(id),
    horse_id INTEGER NOT NULL REFERENCES horses(id),
    source TEXT NOT NULL,
    stage TEXT NOT NULL,
    decimal_odds REAL NOT NULL,
    captured_at TEXT DEFAULT CURRENT_TIMESTAMP,
    UNIQUE(race_id, horse_id, source, stage)
);

-- A suggested (and possibly placed) multi/parlay. Stored whole so the daily
-- followup can settle it leg by leg and the calibration view can compare the
-- hit rate we predicted against the hit rate we got.
CREATE TABLE IF NOT EXISTS parlays (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    race_date TEXT NOT NULL,
    circuit TEXT,
    label TEXT,
    kind TEXT,
    stake REAL,
    combined_odds REAL,
    hit_probability REAL,
    expected_value REAL,
    status TEXT DEFAULT 'pending',
    payout REAL,
    placed INTEGER DEFAULT 0,
    notes TEXT,
    created_at TEXT DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS parlay_legs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    parlay_id INTEGER NOT NULL REFERENCES parlays(id) ON DELETE CASCADE,
    leg_no INTEGER,
    race_id INTEGER REFERENCES races(id),
    venue TEXT,
    race_no INTEGER,
    horse_name TEXT NOT NULL,
    market TEXT NOT NULL,
    decimal_odds REAL,
    model_probability REAL,
    market_probability REAL,
    blended_probability REAL,
    outcome TEXT DEFAULT 'pending'
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
    # Added when the Australian and Hong Kong circuits were wired in. 'circuit'
    # is the coarse grouping the app filters on (India / Australia / Hong Kong)
    # while 'venue' stays the individual track, so a Pune card and a Rosehill
    # card can coexist in one table without either query seeing the other.
    for col, decl in (
        ("circuit", "TEXT"),
        ("country", "TEXT"),
        ("race_time_local", "TEXT"),
        ("track_condition", "TEXT"),
        ("source_key", "TEXT"),
    ):
        if col not in race_cols:
            conn.execute(f"ALTER TABLE races ADD COLUMN {col} {decl}")
    conn.execute("UPDATE races SET circuit='India' WHERE circuit IS NULL")

    if "barrier" not in run_cols:
        conn.execute("ALTER TABLE runs ADD COLUMN barrier INTEGER")
    if "saddlecloth" not in run_cols:
        conn.execute("ALTER TABLE runs ADD COLUMN saddlecloth INTEGER")

    # Added for the accuracy pass (Aug 2026): fields the scrapers were already
    # parsing off the page but the ingest layer threw away before they ever
    # reached the DB -- see models/rating_engine.py for how each is used.
    # apprentice_allowance: claim in kg, so 'effective weight' = weight_kg -
    #   apprentice_allowance can be computed without re-scraping anything.
    # equipment_raw/equipment_codes: raw bracket string RWITC/indiarace print
    #   (e.g. "[9] (TS)(CNB)(A)AFHH-") and the parenthesized codes pulled out
    #   of it (e.g. "TS,CNB") -- kept both because the un-parenthesized tail
    #   isn't confidently decoded (see rwitc.py's parser comment) and is
    #   preserved raw rather than guessed at.
    # assessed_rating/assessed_rating_date: RWITC prints "Rating: 31 (HRA 42
    #   on 19/10/2025)" alongside the official running rating -- a gap here
    #   (assessed above/below the current mark) is a real signal the current-
    #   rating-only model was blind to. Source-specific: only RWITC exposes
    #   this today, so it's null everywhere else.
    # recent_runs_json: the full per-run history (date/class/distance/
    #   jockey/weight/placing/time where the source provides it) that used to
    #   get collapsed down to a bare placings string before storage. Source
    #   richness varies -- RWITC has all of it, indiarace-sourced venues have
    #   placings only, BTC has none (documented gap, unchanged) -- so this is
    #   the raw JSON list and callers degrade gracefully field-by-field.
    for col, decl in (
        ("apprentice_allowance", "REAL"),
        ("equipment_raw", "TEXT"),
        ("equipment_codes", "TEXT"),
        ("assessed_rating", "INTEGER"),
        ("assessed_rating_date", "TEXT"),
        ("recent_runs_json", "TEXT"),
    ):
        if col not in run_cols:
            conn.execute(f"ALTER TABLE runs ADD COLUMN {col} {decl}")

    conn.execute("CREATE INDEX IF NOT EXISTS idx_races_date_circuit ON races(race_date, circuit)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_market_odds_race ON market_odds(race_id, market)")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_odds_snapshots_race ON odds_snapshots(race_id, stage)")


if __name__ == "__main__":
    init_db()
    print(f"Initialized DB at {DB_PATH}")
