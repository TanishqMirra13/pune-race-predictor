"""'Fundamentals' layer: trainer/jockey form and owner-sponsor connections.

Ratings and speed/form are the "technicals" -- how fast/well-placed the horse
itself has been. This module adds the "fundamentals" the user specifically
asked for: is this trainer/jockey combination in form right now (official,
publicly-published season strike rates, not something we're guessing at),
and is there a plausible "stable target" signal -- an owner's horse running
in a race sponsored by, or named after, that same owner or their family/
company. Races in India are routinely named after big owners/breeders who
sponsor them (e.g. "The Zavaray S. Poonawalla Eve Champion Trophy"), and it's
a well-known, entirely public pattern that connections sometimes target
"their own" race. This is pattern-matching on public race names and public
ownership listings -- not insider information.
"""
import re
import sqlite3

# Bayesian shrinkage: blend a name's own strike rate with the venue/role
# population average, weighted as if the population average were worth this
# many "extra" rides. Keeps a jockey with 3 rides and 2 wins from looking
# like the season's best rider -- their score gets pulled toward the mean
# until their sample size actually says something.
SHRINKAGE_PRIOR_RIDES = 15

OWNER_STOPWORDS = {
    "mr", "mrs", "ms", "miss", "dr", "m/s", "smt", "shri",
    "rep", "rep.", "by", "and", "&", "the", "of", "for", "family",
    "ltd", "ltd.", "pvt", "pvt.", "llp", "co", "co.", "company",
    "stud", "farm", "farms", "racing", "breeders", "group", "estate", "estates",
}


def population_avg_win_pct(conn: sqlite3.Connection, venue: str, role: str) -> float:
    row = conn.execute(
        "SELECT SUM(wins) w, SUM(total_rides) t FROM connection_stats WHERE venue=? AND role=?",
        (venue, role),
    ).fetchone()
    if not row or not row["t"]:
        return 15.0  # plausible generic fallback if we have no stats at all yet
    return 100.0 * row["w"] / row["t"]


def shrunk_win_pct(conn: sqlite3.Connection, venue: str, role: str, name: str,
                    population_avg: float | None = None) -> tuple[float, int]:
    """Returns (shrunk win % in [0,100], actual sample size). Sample size 0
    means we have no record for this name -- caller should treat as low confidence."""
    if not name:
        return population_avg or population_avg_win_pct(conn, venue, role), 0
    if population_avg is None:
        population_avg = population_avg_win_pct(conn, venue, role)
    row = conn.execute(
        "SELECT wins, total_rides FROM connection_stats WHERE venue=? AND role=? AND name=?",
        (venue, role, name),
    ).fetchone()
    if not row or not row["total_rides"]:
        return population_avg, 0
    wins, total = row["wins"], row["total_rides"]
    shrunk = 100.0 * (wins + population_avg / 100.0 * SHRINKAGE_PRIOR_RIDES) / (total + SHRINKAGE_PRIOR_RIDES)
    return shrunk, total


def self_derived_population_avg(conn: sqlite3.Connection, role: str, as_of_date: str | None = None) -> float:
    """role: 'owner' or 'breeder' (pooled across all venues -- ownership and
    breeding operations aren't venue-local), or 'jockey'/'trainer' when
    called in backtest mode (see self_derived_strike_rate's as_of_date note).

    as_of_date, when given, restricts to races strictly before that date --
    this is what scripts/backtest.py passes so a race's own signals can't be
    computed from results that hadn't happened yet when it ran."""
    q = """SELECT COUNT(*) starts, SUM(CASE WHEN res.finish_position = 1 THEN 1 ELSE 0 END) wins
           FROM runs r JOIN results res ON res.run_id = r.id"""
    params: tuple = ()
    if as_of_date is not None:
        q += " JOIN races ra ON ra.id = r.race_id WHERE r.scratched = 0 AND ra.race_date < ?"
        params = (as_of_date,)
    else:
        q += " WHERE r.scratched = 0"
    row = conn.execute(q, params).fetchone()
    if not row or not row["starts"]:
        return 8.0  # plausible generic fallback (typical field-of-8-10 base rate) if we have no results yet
    return 100.0 * (row["wins"] or 0) / row["starts"]


def self_derived_strike_rate(conn: sqlite3.Connection, role: str, name: str | None,
                              population_avg: float | None = None,
                              as_of_date: str | None = None) -> tuple[float, int]:
    """Bayesian-shrunk win rate derived from our own accumulated results
    archive rather than an official leaderboard.

    role='owner'/'breeder': there's no official owner/breeder leaderboard
    covering every venue (RWITC has one, BTC doesn't), so this is always
    self-sourced, live use included.

    role='jockey'/'trainer': live scoring normally uses the official
    club-published season snapshot instead (models/rating_engine.py,
    shrunk_win_pct) -- this self-derived path exists so BACKTESTING has an
    honest alternative. connection_stats has no history, only a current
    snapshot, so grading a March race with "today's" jockey standings bakes
    in results from races that hadn't run yet. Pass as_of_date (the race
    being graded) to restrict this query to strictly earlier races instead.

    Treat a small sample size as genuinely low-confidence, not just noise
    smoothed away by shrinkage."""
    if role == "owner":
        group_expr = "r.owner"
    elif role == "breeder":
        group_expr = "COALESCE(h.breeder, h.stud)"
    elif role in ("jockey", "trainer"):
        group_expr = f"r.{role}"
    else:
        raise ValueError(f"role must be 'owner', 'breeder', 'jockey' or 'trainer', got {role!r}")

    if population_avg is None:
        population_avg = self_derived_population_avg(conn, role, as_of_date=as_of_date)
    if not name:
        return population_avg, 0

    q = f"""SELECT COUNT(*) starts, SUM(CASE WHEN res.finish_position = 1 THEN 1 ELSE 0 END) wins
            FROM runs r JOIN results res ON res.run_id = r.id JOIN horses h ON h.id = r.horse_id"""
    params: tuple = (name,)
    if as_of_date is not None:
        q += " JOIN races ra ON ra.id = r.race_id"
        q += f" WHERE r.scratched = 0 AND {group_expr} = ? AND ra.race_date < ?"
        params = (name, as_of_date)
    else:
        q += f" WHERE r.scratched = 0 AND {group_expr} = ?"
    row = conn.execute(q, params).fetchone()
    if not row or not row["starts"]:
        return population_avg, 0
    starts, wins = row["starts"], row["wins"] or 0
    shrunk = 100.0 * (wins + population_avg / 100.0 * SHRINKAGE_PRIOR_RIDES) / (starts + SHRINKAGE_PRIOR_RIDES)
    return shrunk, starts


def _significant_tokens(text: str) -> set[str]:
    words = re.findall(r"[A-Za-z']+", text)
    return {w.lower() for w in words if len(w) > 2 and w.lower() not in OWNER_STOPWORDS}


def owner_sponsor_match(owner_text: str | None, race_name: str | None) -> bool:
    """True if a distinctive owner name/surname also appears in the race's
    sponsor/title text -- a public 'this looks like a stable target' signal."""
    if not owner_text or not race_name:
        return False
    owner_tokens = _significant_tokens(owner_text)
    race_tokens = _significant_tokens(race_name)
    return bool(owner_tokens & race_tokens)
