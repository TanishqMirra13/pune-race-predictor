"""The Indian racing map, organised as verticals.

WHY THIS EXISTS

"India" is not a circuit. It is six active clubs under separate turf
authorities, each with its own season, its own data source, its own gaps in
that data, and its own tote pool structure. Treating them as one bucket hid
three things that matter for betting:

  - They are not in season at the same time. Mumbai and Pune are the SAME
    club (RWITC) running two halves of one year, so "no Mumbai card in
    August" is the calendar working, not the scraper failing.
  - They are not equally well described. RWITC publishes breeder, stud and a
    full per-run history; BTC publishes recent form as letter codes rather
    than placings; the four indiarace-sourced clubs publish neither breeder
    nor foaled date. A signal that is strong at Pune may not exist at Mysore.
  - They are not equally predictable, and the archive says so per venue.

So the app groups venues by vertical, and every venue carries a profile
stating what is actually known about it. A vertical here means one regional
turf authority and the venues it runs -- the grouping that decides which
scraper answers, which season applies, and which fields come back populated.

WHAT IS HARD-CODED HERE AND WHAT IS NOT

Only structural facts are written down: which club owns a venue, which
scraper serves it, and which fields that scraper cannot fill. Those were read
off the scrapers themselves and the clubs' own pages.

Everything measurable is measured from the archive at runtime instead --
race counts, observed date ranges, favourite strike rate. A season window in
particular is deliberately NOT hard-coded as a date range: Indian clubs move
their fixtures, and a stale range claiming "out of season" while a card is on
would be worse than saying nothing. The season notes below are prose, and
`archive_stats()` reports what the archive actually contains.
"""
from collections import OrderedDict

# One entry per active club. `source` names the scraper family that serves the
# venue -- see app.py's SCRAPER_BY_VENUE, which is keyed off the same names.
#
# `gaps` is the honest part: every line lists a field the source does NOT
# provide, verified against the parsers rather than assumed. A gap is not a
# bug to be fixed here; it is a reason a signal is weaker at that venue.
VERTICALS: "OrderedDict[str, dict]" = OrderedDict([
    ("Western", {
        "authority": "Royal Western India Turf Club (RWITC)",
        "blurb": "One club, two racecourses, two halves of the year: Mumbai "
                 "over the cool months and Pune through the monsoon. The "
                 "best-documented Indian racing there is -- the only source "
                 "publishing breeder, stud, foaled date and a full per-run "
                 "history, plus its own owner money-leaders table.",
        "venues": OrderedDict([
            ("Mumbai", {
                "course": "Mahalaxmi",
                "source": "rwitc",
                "season_note": "Winter half of the RWITC year, roughly November to April.",
                "gaps": [],
            }),
            ("Pune", {
                "course": "Pune",
                "source": "rwitc",
                "season_note": "Monsoon half of the RWITC year, roughly July to October.",
                "gaps": [],
            }),
        ]),
    }),
    ("Southern", {
        "authority": "Bangalore, Mysore and Hyderabad race clubs",
        "blurb": "The busiest vertical by fixture count, and the one that "
                 "keeps something running most of the year since the three "
                 "clubs' seasons interlock. Bangalore has its own modern "
                 "site; Mysore and Hyderabad publish nothing scrapable of "
                 "their own and arrive through indiarace.",
        "venues": OrderedDict([
            ("Bangalore", {
                "course": "Bangalore",
                "source": "btc",
                "season_note": "Two seasons a year, a summer one and a winter one.",
                "gaps": [
                    "recent form is published as letter codes, not numeric placings, "
                    "so the form signal is left blank rather than guessed",
                    "the results table names only the winning trainer, not one per runner",
                    "no breeder field; the stud name stands in, which is usually but "
                    "not always the same operation",
                ],
            }),
            ("Mysore", {
                "course": "Mysore",
                "source": "indiarace",
                "season_note": "Club site has no scrapable card; served via indiarace.",
                "gaps": [
                    "breeder, stud and foaled date are not broken out -- age, colour "
                    "and sex arrive as one string",
                    "'Last 5 runs' order is assumed newest-first, matching RWITC and "
                    "BTC, rather than independently verified",
                ],
            }),
            ("Hyderabad", {
                "course": "Malakpet",
                "source": "indiarace",
                "season_note": "Club gates its racecard behind a login; served via indiarace.",
                "gaps": [
                    "breeder, stud and foaled date are not broken out -- age, colour "
                    "and sex arrive as one string",
                    "'Last 5 runs' order is assumed newest-first, matching RWITC and "
                    "BTC, rather than independently verified",
                ],
            }),
        ]),
    }),
    ("Eastern", {
        "authority": "Royal Calcutta Turf Club (RCTC)",
        "blurb": "The oldest club in the country and the thinnest slice of "
                 "this archive. Its live racing data sits behind a separate "
                 "rctclive.in login, so everything arrives via indiarace, and "
                 "there are too few races on record to say how predictable "
                 "the place is.",
        "venues": OrderedDict([
            ("Kolkata", {
                "course": "Kolkata",
                "source": "indiarace",
                "season_note": "Monsoon and winter meetings; club data is login-gated, served via indiarace.",
                "gaps": [
                    "breeder, stud and foaled date are not broken out",
                    "'Last 5 runs' order is assumed newest-first rather than verified",
                ],
            }),
        ]),
    }),
    ("Northern", {
        "authority": "Delhi Race Club (DRC)",
        "blurb": "Wired up but not yet backfilled. Delhi publishes entries "
                 "and results as PDFs only, so indiarace is the only route "
                 "in, and nothing has been archived from it yet -- treat "
                 "every number the app shows for Delhi as unmeasured.",
        "venues": OrderedDict([
            ("Delhi", {
                "course": "Delhi",
                "source": "indiarace",
                "season_note": "Club publishes PDFs only; served via indiarace.",
                "gaps": [
                    "breeder, stud and foaled date are not broken out",
                    "nothing archived yet, so every derived signal runs on the pooled "
                    "cross-venue average rather than local evidence",
                ],
            }),
        ]),
    }),
])

# Two clubs indiarace knows about that this app deliberately does not wire up:
# its own fixture feed stops in Oct 2025 for Chennai and Jun 2024 for Ooty, so
# a venue entry for either would promise data that isn't there.
DORMANT = {
    "Chennai": "indiarace's fixture feed for Chennai stops in Oct 2025.",
    "Ooty": "indiarace's fixture feed for Ooty stops in Jun 2024.",
}

SOURCE_LABEL = {
    "rwitc": "rwitc.com -- the club's own site",
    "btc": "bangaloreraces.com -- the club's own site",
    "indiarace": "indiarace.com -- the club has no scrapable card of its own",
}


def venue_names() -> list[str]:
    """Every wired-up venue, in vertical order rather than alphabetical."""
    return [v for vert in VERTICALS.values() for v in vert["venues"]]


def vertical_of(venue: str) -> str | None:
    for name, vert in VERTICALS.items():
        if venue in vert["venues"]:
            return name
    return None


def profile(venue: str) -> dict | None:
    """Structural facts about one venue, with its vertical attached."""
    name = vertical_of(venue)
    if not name:
        return None
    vert = VERTICALS[name]
    return {"venue": venue, "vertical": name, "authority": vert["authority"],
            **vert["venues"][venue]}


def label(venue: str) -> str:
    """'Pune -- Western' -- for a flat selector that still shows the grouping."""
    vert = vertical_of(venue)
    return f"{venue} -- {vert}" if vert else venue


# --------------------------------------------------------------------------
# Measured, not declared. Everything below reads the archive.
# --------------------------------------------------------------------------

def archive_stats(conn) -> dict:
    """Per-venue archive facts: races held, races settled, the date range
    actually observed, and how often the tote favourite won.

    The favourite strike rate is the number that matters most per venue,
    because it is the benchmark the model has to beat there and it varies
    more between venues than any signal the model itself computes."""
    rows = conn.execute(
        """SELECT ra.venue,
                  COUNT(*)                                              AS races,
                  MIN(ra.race_date)                                     AS first_seen,
                  MAX(ra.race_date)                                     AS last_seen,
                  SUM(CASE WHEN w.winner IS NOT NULL THEN 1 ELSE 0 END) AS settled,
                  SUM(CASE WHEN w.winner IS NOT NULL
                            AND ra.tote_favourite IS NOT NULL
                            AND UPPER(TRIM(ra.tote_favourite)) = UPPER(TRIM(w.winner))
                           THEN 1 ELSE 0 END)                           AS fav_wins,
                  SUM(CASE WHEN w.winner IS NOT NULL AND ra.tote_favourite IS NOT NULL
                           THEN 1 ELSE 0 END)                           AS fav_known
           FROM races ra
           LEFT JOIN (
               SELECT r.race_id, h.name AS winner
               FROM runs r
               JOIN results res ON res.run_id = r.id
               JOIN horses h ON h.id = r.horse_id
               WHERE res.finish_position = 1
           ) w ON w.race_id = ra.id
           GROUP BY ra.venue""",
    ).fetchall()
    out = {}
    for r in rows:
        if not vertical_of(r["venue"]):
            continue
        fav_known = r["fav_known"] or 0
        out[r["venue"]] = {
            "races": r["races"], "settled": r["settled"] or 0,
            "first_seen": r["first_seen"], "last_seen": r["last_seen"],
            "favourite_win_rate": (r["fav_wins"] / fav_known) if fav_known else None,
            "favourite_sample": fav_known,
        }
    for v in venue_names():
        out.setdefault(v, {"races": 0, "settled": 0, "first_seen": None,
                           "last_seen": None, "favourite_win_rate": None,
                           "favourite_sample": 0})
    return out


def vertical_summary(conn) -> list[dict]:
    """One row per vertical, with its venues' measured numbers rolled up."""
    stats = archive_stats(conn)
    summary = []
    for name, vert in VERTICALS.items():
        venues = list(vert["venues"])
        fav_n = sum(stats[v]["favourite_sample"] for v in venues)
        fav_w = sum((stats[v]["favourite_win_rate"] or 0) * stats[v]["favourite_sample"]
                    for v in venues)
        summary.append({
            "vertical": name, "authority": vert["authority"], "blurb": vert["blurb"],
            "venues": venues,
            "races": sum(stats[v]["races"] for v in venues),
            "settled": sum(stats[v]["settled"] for v in venues),
            "last_seen": max((stats[v]["last_seen"] for v in venues
                              if stats[v]["last_seen"]), default=None),
            "favourite_win_rate": (fav_w / fav_n) if fav_n else None,
            "favourite_sample": fav_n,
        })
    return summary


def evidence_note(stats: dict) -> str:
    """One sentence on how far a venue's numbers can be trusted.

    Sample size is the binding constraint on everything this project claims,
    so it is stated per venue rather than buried in a README caveat. The
    thresholds match the ones used elsewhere in the app: under 30 settled
    races nothing is a finding, and it takes about 100 before a rate starts
    separating from noise."""
    n = stats.get("settled", 0)
    if n == 0:
        return "Nothing archived here yet -- every derived signal falls back to the cross-venue pool."
    if n < 30:
        return f"Only {n} settled races on record. Read any number below as an anecdote."
    if n < 100:
        return f"{n} settled races -- enough to notice a pattern, not enough to bet one."
    return f"{n} settled races, so roughly +/-{98 / n ** 0.5:.0f}pp of noise on any rate quoted here."
