"""Placing a bet: adapter interface, dry-run by default.

This is the layer between "the model says back this" and money leaving an
account. It is deliberately provider-neutral -- you describe your bookmaker's
endpoint in .env rather than this file hard-coding one book's private schema.

WHAT THIS WILL NOT DO, and why:

- It will not defeat bot protection. Both stake.com and the stake1021.com
  mirror answer every programmatic request with Cloudflare's challenge
  (`cf-mitigated: challenge`), verified July 2026. Getting past that needs a
  browser fingerprint that Cloudflare actively works to detect; it is also a
  line this project does not cross. If your bookmaker sits behind a bot
  challenge, automated placement is not available and the honest answer is to
  place manually from the bet slip, which takes about thirty seconds and
  costs nothing.
- It will not store or log a credential. The token is read from the
  environment at call time and never written to the database, the console, or
  an error message.
- It will not place anything by default. DryRunAdapter is the default and
  real placement requires both a configured endpoint AND an explicit
  allow_real_bets=True.

THE CONTROL THAT MATTERS. Every bet carries a minimum acceptable price from
models/betslip.py, and place_bet() refuses below it. This is what makes
automated placement survivable when your execution venue prices worse than the
venue the edge was found at: instead of silently taking 4.60 on a bet that
needed 5.15, the adapter skips and tells you. A skipped bet costs nothing. A
bet placed at the wrong price costs money every single time, forever.
"""
import os
from abc import ABC, abstractmethod

import requests

from models.betslip import PreparedBet, validate_against_live
from scrapers.odds_import import load_dotenv

# Environment variables for the generic placement adapter. Separate from the
# ODDS_API_* set because reading prices and moving money are different
# permissions and should be configurable independently.
ENV_BET_URL = "BET_API_URL"
ENV_BET_TOKEN = "BET_API_TOKEN"
ENV_BET_TOKEN_HEADER = "BET_API_TOKEN_HEADER"
ENV_BET_ENABLED = "BET_API_ENABLED"


class BookmakerAdapter(ABC):
    """Minimal surface: check a price, place a bet, read the balance."""

    name = "abstract"

    @abstractmethod
    def get_price(self, bet: PreparedBet) -> float | None:
        """The book's current price for this selection, or None if unavailable."""

    @abstractmethod
    def _submit(self, bet: PreparedBet, price: float) -> dict:
        """Actually send the bet. Only ever called after validation passes."""

    def place_bet(self, bet: PreparedBet, live_price: float | None = None) -> dict:
        """Validate, then place -- never the other way round.

        live_price may be passed in when you already have the number (from the
        UI, say); otherwise the adapter is asked for it. A bet that fails
        validation is returned as skipped with the reason, and no request is
        made."""
        price = live_price if live_price is not None else self.get_price(bet)
        verdict = validate_against_live(bet, price)
        if not verdict["place"]:
            bet.status = "skipped"
            bet.skip_reason = verdict["reason"]
            return {"placed": False, "reason": verdict["reason"], "bet": bet.to_dict()}

        result = self._submit(bet, verdict["live_odds"])
        bet.status = "placed" if result.get("ok") else "skipped"
        bet.placed_odds = verdict["live_odds"] if result.get("ok") else None
        if not result.get("ok"):
            bet.skip_reason = result.get("error", "submission failed")
        return {"placed": bool(result.get("ok")), "odds": bet.placed_odds,
                "reason": result.get("error") or verdict["reason"],
                "bet": bet.to_dict(), "raw": result.get("raw")}


class DryRunAdapter(BookmakerAdapter):
    """The default. Runs the whole flow, validates every bet, places nothing.

    Use this until the numbers in the Followup tab say the model is actually
    calibrated. Automating an unproven edge just loses money faster."""

    name = "dry-run"

    def __init__(self, prices: dict | None = None):
        # {(venue, race_no, horse_name, market): price} -- lets you rehearse
        # against real numbers without an account.
        self.prices = prices or {}

    def get_price(self, bet: PreparedBet) -> float | None:
        return self.prices.get((bet.venue, bet.race_no, bet.horse_name, bet.market),
                               bet.reference_odds)

    def _submit(self, bet: PreparedBet, price: float) -> dict:
        return {"ok": True, "raw": {"dry_run": True,
                                    "would_place": f"{bet.horse_name} {bet.market} "
                                                   f"Rs{bet.stake} @ {price}"}}


class GenericHttpAdapter(BookmakerAdapter):
    """Placement against any bookmaker with a real HTTP API.

    Configured entirely by environment variables so no credential ever touches
    this file:

        BET_API_URL           endpoint that accepts a bet
        BET_API_TOKEN         your session/API token -- .env only
        BET_API_TOKEN_HEADER  header to send it in (default x-access-token)
        BET_API_ENABLED       must be exactly "true" to place real bets

    The payload shape below is the common one (selection id, stake, price). If
    your book wants something different, override _payload() in a subclass --
    that is the only method that should need changing."""

    name = "generic-http"

    def __init__(self, allow_real_bets: bool = False, timeout: int = 20):
        load_dotenv()
        self.timeout = timeout
        self.url = os.environ.get(ENV_BET_URL, "")
        self.token = os.environ.get(ENV_BET_TOKEN, "")
        self.token_header = os.environ.get(ENV_BET_TOKEN_HEADER, "x-access-token")
        env_enabled = os.environ.get(ENV_BET_ENABLED, "").strip().lower() == "true"
        # Both switches must be on. One of them is in code, the other in the
        # environment, so neither a stray default nor a copied .env can start
        # placing real money on its own.
        self.enabled = bool(allow_real_bets and env_enabled and self.url and self.token)

    def status(self) -> dict:
        """Config visibility that never reveals the token."""
        return {
            "endpoint_set": bool(self.url),
            "token_set": bool(self.token),
            "token_length": len(self.token),
            "env_enabled": os.environ.get(ENV_BET_ENABLED, "").strip().lower() == "true",
            "will_place_real_bets": self.enabled,
        }

    def _headers(self) -> dict:
        return {self.token_header: self.token, "Content-Type": "application/json",
                "Accept": "application/json"}

    def _payload(self, bet: PreparedBet, price: float) -> dict:
        return {
            "selection": bet.horse_name,
            "venue": bet.venue,
            "race": bet.race_no,
            "market": bet.market,
            "stake": bet.stake,
            "price": price,
            "minPrice": bet.min_acceptable_odds,
        }

    def get_price(self, bet: PreparedBet) -> float | None:
        # Price discovery belongs to the odds layer -- see scrapers/indiarace.py
        # and scrapers/odds_import.py. Returning None here forces the caller to
        # supply the price it actually saw, which is the safer default: it
        # cannot silently place against a price nobody checked.
        return None

    def _submit(self, bet: PreparedBet, price: float) -> dict:
        if not self.enabled:
            return {"ok": False,
                    "error": ("real placement is off. It needs allow_real_bets=True in code AND "
                              f"{ENV_BET_ENABLED}=true plus {ENV_BET_URL}/{ENV_BET_TOKEN} in .env. "
                              "Until all four are set this adapter refuses to move money.")}
        try:
            resp = requests.post(self.url, json=self._payload(bet, price),
                                 headers=self._headers(), timeout=self.timeout)
        except requests.RequestException as exc:
            # Deliberately not including the request headers in the message --
            # they contain the token.
            return {"ok": False, "error": f"request failed: {type(exc).__name__}"}
        if resp.status_code == 403 and "cloudflare" in (resp.headers.get("server", "").lower()):
            return {"ok": False,
                    "error": ("blocked by Cloudflare bot protection. This bookmaker does not "
                              "permit automated placement; use the bet slip and place manually.")}
        if not resp.ok:
            return {"ok": False, "error": f"bookmaker returned HTTP {resp.status_code}"}
        try:
            return {"ok": True, "raw": resp.json()}
        except ValueError:
            return {"ok": True, "raw": {"status": resp.status_code}}


def place_slip(adapter: BookmakerAdapter, bets: list[PreparedBet],
               live_prices: dict | None = None) -> dict:
    """Run a whole slip through validation and placement.

    Returns placed and skipped separately, because the skipped list is the
    useful one: it is the record of bets your bookmaker would not price
    properly, which is exactly the information that tells you whether that
    bookmaker is worth using at all."""
    live_prices = live_prices or {}
    placed, skipped = [], []
    for bet in bets:
        key = (bet.venue, bet.race_no, bet.horse_name, bet.market)
        outcome = adapter.place_bet(bet, live_prices.get(key))
        (placed if outcome["placed"] else skipped).append(outcome)
    return {
        "adapter": adapter.name,
        "placed": placed,
        "skipped": skipped,
        "n_placed": len(placed),
        "n_skipped": len(skipped),
        "staked": round(sum(p["bet"]["stake"] for p in placed), 2),
        "note": (f"{len(skipped)} bet(s) were not placed because the price on offer did not meet "
                 f"the minimum. That is the system working -- each one would have been a losing "
                 f"bet at that price." if skipped else "All bets met their minimum price."),
    }
