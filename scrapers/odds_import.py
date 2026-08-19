"""Getting bookmaker prices into the app.

The whole EV engine is worthless without a price to measure against, and no
Indian club publishes a live machine-readable board. What is actually
available, tested against real meetings in Aug 2026:

  indiarace forecast prices    works -- but INDICATIVE, published only on race
                                        day, and covering only the front 4-5
                                        runners of each field (42-62% of it)
  club result pages (SP)       works -- exact, and available only AFTER the race
  a live tote board            exists only at the track and on screens; nothing
                                        machine-readable serves it

So the price that decides a bet is one you have to look at and type. Rather
than pretend otherwise, this module offers three honest paths, in descending
order of reliability:

  1. paste_odds()      -- copy the prices off any screen -- tote board,
                          exchange, bookmaker app -- and paste them in. Always
                          works, needs nothing, no account, no key.
  2. indiarace forecast -- automatic on race day (see scrapers/indiarace.py).
                          Enough to rank a field and to pick jackpot legs; not
                          enough of the field to de-vig into an EV figure.
  3. fetch_from_api()   -- a generic adapter for a bookmaker API you already
                          have access to, configured entirely by environment
                          variables.

On (4) and API keys: never paste a key into a chat window, a source file, or
anything that gets committed. Put it in a .env file, which .gitignore already
covers. This module reads it from the environment and nothing else -- see
api_config_status() for exactly which variables it looks for.
"""
import json
import os
import re
from pathlib import Path

import requests

ENV_FILE = Path(__file__).resolve().parent.parent / ".env"

# Environment variables the generic adapter reads. Deliberately book-agnostic:
# whatever endpoint you have access to, you describe it here rather than
# hard-coding one bookmaker's schema that may change without notice.
ENV_ENDPOINT = "ODDS_API_URL"
ENV_TOKEN = "ODDS_API_TOKEN"
ENV_TOKEN_HEADER = "ODDS_API_TOKEN_HEADER"   # default: x-access-token
ENV_RUNNER_PATH = "ODDS_API_RUNNER_PATH"     # dotted path to the runner list
ENV_NAME_FIELD = "ODDS_API_NAME_FIELD"
ENV_WIN_FIELD = "ODDS_API_WIN_FIELD"
ENV_PLACE_FIELD = "ODDS_API_PLACE_FIELD"

# "NAME 3.40", "NAME  $3.40", "3. NAME 3.40", "NAME 3.40 1.55" (win + place)
ODDS_LINE_RE = re.compile(
    r"^\s*(?:\d{1,2}[.)]\s*)?"          # optional saddlecloth number
    r"(?P<name>[A-Za-z][A-Za-z0-9''\-\. ]{2,30}?)"
    r"\s*[\s:\-|,]\s*"
    r"\$?(?P<win>\d{1,3}(?:\.\d{1,2})?)"
    r"(?:\s*/?\s*\$?(?P<place>\d{1,3}(?:\.\d{1,2})?))?"
    r"\s*(?:F|FAV|EQF)?\s*$",
    re.I,
)
FRACTION_RE = re.compile(r"^\s*(?:\d{1,2}[.)]\s*)?(?P<name>[A-Za-z][A-Za-z0-9''\-\. ]{2,30}?)"
                         r"\s+(?P<num>\d{1,3})\s*/\s*(?P<den>\d{1,3})\s*$")


def load_dotenv(path: Path = ENV_FILE) -> None:
    """Minimal .env loader so no extra dependency is needed. Existing
    environment variables always win, so a real environment beats a stale file."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key, value = key.strip(), value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


def api_config_status() -> dict:
    """What the generic adapter can see, WITHOUT ever revealing the token.

    Only whether a value is present and how long it is -- enough to debug a
    misconfiguration, never enough to leak the secret into a screenshot, a log
    or a support message."""
    load_dotenv()
    token = os.environ.get(ENV_TOKEN, "")
    return {
        "endpoint_set": bool(os.environ.get(ENV_ENDPOINT)),
        "endpoint": os.environ.get(ENV_ENDPOINT, ""),
        "token_set": bool(token),
        "token_length": len(token),
        "token_header": os.environ.get(ENV_TOKEN_HEADER, "x-access-token"),
        "runner_path": os.environ.get(ENV_RUNNER_PATH, ""),
        "env_file_present": ENV_FILE.exists(),
        "ready": bool(os.environ.get(ENV_ENDPOINT) and token),
    }


# --------------------------------------------------------------------------
# Path 1: paste
# --------------------------------------------------------------------------

def parse_odds_text(text: str, default_market: str = "win") -> list[dict]:
    """Free-text bookmaker prices -> [{'horse_name', 'win', 'place'}].

    Accepts the shapes prices actually get copied in as:
        MAGIC MOMENT 3.40
        7. MAGIC MOMENT $3.40 $1.55
        Magic Moment  3.40 / 1.55
        Magic Moment 5/2                (fractional, converted to decimal)

    Names are upper-cased to match how every scraper in this project stores
    them. A line that does not look like a price is skipped silently rather
    than raising -- pasted screen text is always full of headers and junk."""
    out = []
    for raw in (text or "").splitlines():
        line = raw.strip()
        if not line or len(line) > 90:
            continue
        if re.search(r"\b(race|jockey|trainer|barrier|form|odds|runner|scratch)\b", line, re.I) \
                and not re.search(r"\d\.\d", line):
            continue

        frac = FRACTION_RE.match(line)
        if frac:
            try:
                dec = int(frac.group("num")) / int(frac.group("den")) + 1.0
            except (ValueError, ZeroDivisionError):
                continue
            out.append({"horse_name": _norm_name(frac.group("name")),
                        "win": round(dec, 3) if default_market == "win" else None,
                        "place": None if default_market == "win" else round(dec, 3)})
            continue

        m = ODDS_LINE_RE.match(line)
        if not m:
            continue
        name = _norm_name(m.group("name"))
        if not name or len(name) < 3:
            continue
        win = float(m.group("win"))
        place = float(m.group("place")) if m.group("place") else None
        # A price below 1.01 is not a decimal price -- it is a saddlecloth
        # number or a stray figure that happened to match.
        if win < 1.01 or win > 1000:
            continue
        if default_market == "place" and place is None:
            out.append({"horse_name": name, "win": None, "place": win})
        else:
            out.append({"horse_name": name, "win": win, "place": place})
    return _dedupe_by_name(out)


def _norm_name(name: str) -> str:
    return re.sub(r"\s+", " ", name).strip().upper()


def _dedupe_by_name(rows: list[dict]) -> list[dict]:
    """Keep the last quote for a horse -- when someone pastes twice, the
    second paste is the refreshed board."""
    merged: dict[str, dict] = {}
    for r in rows:
        cur = merged.setdefault(r["horse_name"], {"horse_name": r["horse_name"], "win": None, "place": None})
        if r.get("win"):
            cur["win"] = r["win"]
        if r.get("place"):
            cur["place"] = r["place"]
    return list(merged.values())


def paste_odds(text: str, race_no: int, default_market: str = "win") -> list[dict]:
    """parse_odds_text tagged with the race it belongs to, ready for
    db.ingest.store_market_odds()."""
    return [{**row, "race_no": race_no, "source": "manual"}
            for row in parse_odds_text(text, default_market)]


# --------------------------------------------------------------------------
# Path 4: generic API adapter
# --------------------------------------------------------------------------

def _dig(data, dotted_path: str):
    """Walk 'data.races.0.runners' through nested dicts and lists."""
    node = data
    for part in filter(None, dotted_path.split(".")):
        if isinstance(node, list):
            try:
                node = node[int(part)]
            except (ValueError, IndexError):
                return None
        elif isinstance(node, dict):
            node = node.get(part)
        else:
            return None
        if node is None:
            return None
    return node


def fetch_from_api(race_reference: str | None = None, timeout: int = 20) -> list[dict]:
    """Pull prices from whatever odds API you have credentials for.

    Configured entirely by environment variables so this file never contains a
    secret and never hard-codes one bookmaker's schema:

        ODDS_API_URL          endpoint; '{race}' is substituted with race_reference
        ODDS_API_TOKEN        your key -- from .env only, never committed
        ODDS_API_TOKEN_HEADER header name to send it in (default x-access-token)
        ODDS_API_RUNNER_PATH  dotted path to the runner array, e.g. 'data.race.runners'
        ODDS_API_NAME_FIELD   runner field holding the name  (default 'name')
        ODDS_API_WIN_FIELD    runner field holding win odds  (default 'winOdds')
        ODDS_API_PLACE_FIELD  runner field holding place odds (default 'placeOdds')

    Raises RuntimeError with a plain-English message when unconfigured, so the
    UI can tell the user what is missing rather than showing a traceback."""
    load_dotenv()
    status = api_config_status()
    if not status["ready"]:
        missing = []
        if not status["endpoint_set"]:
            missing.append(ENV_ENDPOINT)
        if not status["token_set"]:
            missing.append(ENV_TOKEN)
        raise RuntimeError(
            f"Odds API not configured -- missing {', '.join(missing)}. Add them to a .env file in the "
            f"project root (it is already gitignored). Never paste a key into a chat or a source file."
        )

    url = os.environ[ENV_ENDPOINT]
    if race_reference:
        url = url.replace("{race}", str(race_reference))
    headers = {
        os.environ.get(ENV_TOKEN_HEADER, "x-access-token"): os.environ[ENV_TOKEN],
        "Accept": "application/json",
    }
    resp = requests.get(url, headers=headers, timeout=timeout)
    resp.raise_for_status()
    try:
        payload = resp.json()
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Odds API returned something that is not JSON: {exc}") from exc

    runners = _dig(payload, os.environ.get(ENV_RUNNER_PATH, "")) or payload
    if not isinstance(runners, list):
        raise RuntimeError(
            f"Could not find a list of runners at ODDS_API_RUNNER_PATH="
            f"'{os.environ.get(ENV_RUNNER_PATH, '')}'. Top-level keys were: "
            f"{list(payload)[:8] if isinstance(payload, dict) else type(payload).__name__}"
        )

    name_field = os.environ.get(ENV_NAME_FIELD, "name")
    win_field = os.environ.get(ENV_WIN_FIELD, "winOdds")
    place_field = os.environ.get(ENV_PLACE_FIELD, "placeOdds")

    out = []
    for r in runners:
        if not isinstance(r, dict):
            continue
        name = r.get(name_field)
        if not name:
            continue
        out.append({
            "horse_name": _norm_name(str(name)),
            "win": _as_float(r.get(win_field)),
            "place": _as_float(r.get(place_field)),
            "source": "api",
        })
    return [r for r in out if r["win"] or r["place"]]


def _as_float(v):
    try:
        f = float(v)
        return f if f > 1.0 else None
    except (TypeError, ValueError):
        return None


def sp_odds_from_results(results_races: list[dict]) -> list[dict]:
    """Starting prices out of a parsed results page, for calibration.

    Stored under the source 'sp' and never used to justify a bet: by the time
    an SP exists the race has been run. Its job is to answer "was the model
    right about the price?", which is the only way to know whether any of this
    is working."""
    out = []
    for race in results_races:
        for r in race.get("runners", []):
            if r.get("decimal_odds"):
                out.append({
                    "race_no": race.get("race_no"),
                    "horse_name": r["horse_name"],
                    "win": r["decimal_odds"],
                    "place": r.get("dividend_place"),
                    "source": "sp",
                })
    return out
