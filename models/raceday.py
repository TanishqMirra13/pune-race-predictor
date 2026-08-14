"""Classify a race day: holiday, feature, weekend or ordinary weekday.

WHY THIS EXISTS, AND WHY IT DOES NOT TOUCH SCORING

A reasonable and widely-held belief in racing is that big-crowd days -- public
holidays, Derby/Oaks/Cup days -- produce more upsets, or that such races are
more likely to be manipulated. It was tested against this archive (Aug 2026)
and the data did not support it, in either of two independent tests:

  favourite strike rate      special 43.4%  vs ordinary 47.3%   (z=-0.63, ns)
  favourite runs UNPLACED    special 16.7%  vs ordinary 20.5%   (z=-0.78, ns)

Neither is significant, and both point the OPPOSITE way to the hypothesis.
Bolters (winner 5th+ in the market) are also rarer on special days, 5.3% vs
11.7%. Feature days show slightly fewer favourite wins AND fewer longshot
wins, which is the signature of a genuinely competitive field -- several good
horses the market cannot separate -- not of a random or rigged one.

So this module classifies days and nothing more. It deliberately feeds no
weight into models/rating_engine.py, because on the measured evidence there is
no effect to encode, and a factor fitted to noise would tell you to back
different horses on exactly the days you are most likely to be betting.

Two honest caveats kept in view:
  1. Holiday races number only 7 in the archive. That is far too few to
     conclude anything either way; the classification exists so the sample
     grows and the question can be settled later rather than argued.
  2. Results data can detect UPSETS. It cannot detect manipulation. If
     interference occurs but is rare or subtle enough to be invisible across
     78 special-day races, no model built on this data could act on it either.

If a real effect ever emerges from a larger sample, the correct response is
still to reduce STAKE, not to change selections: unpredictability tells you to
risk less, never which horse to prefer instead.
"""
import re
from datetime import date

# Indian public holidays that draw a crowd to a racecourse. Deliberately a
# hand-maintained list rather than a holiday library: only the days that
# actually coincide with racing matter, and the movable feasts (Diwali, Holi)
# need their real dates rather than a rule.
HOLIDAYS = {
    "2025-08-15": "Independence Day",
    "2026-08-15": "Independence Day",
    "2025-10-02": "Gandhi Jayanti",
    "2026-10-02": "Gandhi Jayanti",
    "2026-01-26": "Republic Day",
    "2025-10-20": "Diwali",
    "2025-10-21": "Diwali",
    "2026-11-08": "Diwali",
    "2026-11-09": "Diwali",
    "2026-03-04": "Holi",
    "2025-12-25": "Christmas",
    "2026-12-25": "Christmas",
    "2026-01-01": "New Year",
}

# A "feature" race: the black-type and big-sponsor events that draw a crowd.
# 'cup\b' is bounded so it doesn't catch every "...Cup Plate" consolation.
FEATURE_RE = re.compile(
    r"derby|oaks|guineas|invitation|classic|gr\.?\s*[123]\b|champion|"
    r"st\.?\s*leger|million|cup\b", re.I)

# Measured on this archive; quoted in the UI so the context chip states a fact
# rather than a vibe. See the module docstring for the full comparison.
MEASURED = {
    "special_fav_rate": 43.4,
    "ordinary_fav_rate": 47.3,
    "special_n": 78,
    "ordinary_n": 429,
}


def classify(race_date: str | None, race_name: str | None = None) -> dict:
    """Returns {'kind', 'label', 'is_special'}.

    kind: 'holiday' | 'feature' | 'weekend' | 'weekday'
    A holiday outranks a feature when both apply -- the crowd is the thing
    being classified, and a holiday brings the bigger one.
    """
    holiday = HOLIDAYS.get(race_date or "")
    if holiday:
        return {"kind": "holiday", "label": holiday, "is_special": True}
    if FEATURE_RE.search(race_name or ""):
        return {"kind": "feature", "label": "Feature race", "is_special": True}
    try:
        if date.fromisoformat(race_date).weekday() >= 5:
            return {"kind": "weekend", "label": "Weekend", "is_special": False}
    except (ValueError, TypeError):
        pass
    return {"kind": "weekday", "label": "Weekday", "is_special": False}


def context_note(race_date: str | None, race_name: str | None = None) -> str | None:
    """One plain sentence for the UI, or None on an ordinary day.

    Says what was measured rather than implying a caution the data does not
    support -- the common belief is that these days are less predictable, and
    on this archive they simply are not.
    """
    c = classify(race_date, race_name)
    if not c["is_special"]:
        return None
    if c["kind"] == "holiday":
        return (
            f"{c['label']} -- a big-crowd day. Checked against this archive: favourites won "
            f"{MEASURED['special_fav_rate']:.0f}% on special days vs "
            f"{MEASURED['ordinary_fav_rate']:.0f}% on ordinary ones, a difference well inside "
            f"noise, and bolters were rarer, not commoner. Only 7 holiday races are on record "
            f"though, so this is being tracked rather than treated as settled. Nothing in the "
            f"scoring changes today."
        )
    return (
        f"Feature race. On this archive feature days show slightly fewer favourite wins but "
        f"also fewer longshot winners (5.3% vs 11.7%) -- the shape of a competitive field, not "
        f"an unpredictable one. Expect the winner to come from the front of the market rather "
        f"than out of it. Nothing in the scoring changes."
    )
