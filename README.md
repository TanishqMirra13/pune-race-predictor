# Pune Race Predictor

A local tool for entertainment analysis of Indian horse racing -- Pune,
Mumbai (both RWITC), and Bangalore (BTC): enter a day's budget and goal, get
a ranked, reasoned shortlist per race and a staking plan that respects that
budget. **Not a winner-picker** -- horse racing is genuinely unpredictable.
See the Bankroll & Calibration tab for an honest, ongoing record of how the
model's confidence has actually tracked outcomes.

## Run it

```
venv\Scripts\activate
streamlit run app.py
```

Opens at http://localhost:8501. Use the sidebar to pick a race date/venue
(Pune, Mumbai, or Bangalore) and click **Fetch live** to pull that day's race
card. Off-season or if the site is unreachable, use **Manual paste fallback**
with the saved HTML source of the race card page.

## Bulk historical backfill

Rather than fetching one day at a time through the UI, pull a whole date
range in one go:

```
python scripts/backfill.py --venue Bangalore --start 2026-06-13 --end 2026-07-12
python scripts/backfill.py --venue Mumbai --start 2025-11-01 --end 2026-04-30
python scripts/backfill.py --venue Pune --start 2025-07-18 --end 2025-10-20
```

It fetches both the race card and results for every date in range, skips
non-race days silently (that's normal -- Pune/Mumbai/Bangalore only race a
few days a week), and caches each page under `data/cache/` so re-runs don't
re-hit the server. Already run once for Bangalore's current season-to-date
(10 real race days as of 2026-07-12) and Mumbai's just-completed 2025/26
season (Nov 2025 -- Apr 2026) as a starting dataset to test against -- Mumbai
racing itself doesn't resume live until November 2026.

## Data sources

- **Pune & Mumbai (RWITC, same site, auto-detects venue by date):**
  `https://rwitc.com/new/erp_racecard.php?date=YYYY-MM-DD` for per-horse
  entries (rating, weight, jockey, trainer, last-5-runs form), and
  `https://rwitc.com/erp_raceresult.php?date=YYYY-MM-DD` for finishing
  order, times, and tote dividends (WIN/PLACE/SHP/FOR/QNL/TNL).
- **Bangalore (BTC):** `https://bangaloreraces.com/racing/racecard?d=YYYY-MM-DD`
  and `https://bangaloreraces.com/racing/results?d=YYYY-MM-DD`. Modern
  semantic HTML (vs. RWITC's legacy nested tables), parsed in
  `scrapers/btc.py`. Two known gaps vs. RWITC: BTC's racecard shows recent
  form as letter codes rather than numeric placings (left blank rather than
  guessed), and its results table doesn't list a per-runner trainer (only
  the race winner's).
- `standard_timings.pdf` (`data/standard_timings.pdf`, RWITC only so far)
  -- par times by class and distance, for Phase 2 speed figures. Currently a
  2011-dated file (the most recent RWITC has published at a stable URL);
  treat as a rough reference until replaced by empirically-derived pars from
  our own results archive.

## Scoring: technicals + fundamentals

Composite score per horse blends six things, mapped to within-race win
probability via softmax:

- **35% official handicap rating** (normalized within the field) -- the
  richest single technical signal.
- **15% recency-weighted recent form** (last-5 placings, RWITC only for now).
- **12% jockey strike rate + 13% trainer strike rate** -- official
  season-to-date stats scraped from each club's own published pages
  (`jockeyStatistics.php`/`trainerStatistics.php` on RWITC,
  `Home/JockeyStats`/`home/trainerstats` on BTC), Bayesian-shrunk toward the
  venue's population-average win% so a jockey with 3 rides and 2 wins doesn't
  outrank a proven rider with 150 rides -- see `models/connections.py`.
- **13% owner strike rate + 12% breeder/stud strike rate** -- self-derived
  from our own backfilled results archive (`self_derived_strike_rate` in
  `models/connections.py`), pooled across all three venues since ownership
  and breeding operations aren't venue-local the way jockeys/trainers are.
  Neither club publishes a breeder leaderboard, and only RWITC publishes an
  owner one, so this is the one signal here that isn't from an official
  source -- it's only as good as the seasons we've backfilled, and the same
  Bayesian shrinkage applies. RWITC's official season **Money Leaders**
  (Owners/Jockeys/Horses/Trainers by winnings, `moneyLeaders.php`) is scraped
  and shown in the Connections tab as a reference for which operations are
  established and well-resourced, but deliberately *not* fed into the score,
  to avoid mixing an earnings-based ranking with the win-rate-based signals
  above.
- **+0.08 flat bonus** when an owner's name plausibly overlaps with the
  race's own sponsor/title text (`owner_sponsor_match` in
  `models/connections.py`) -- races in India are routinely named after their
  owner/breeder sponsors, and it's a real, entirely public pattern that
  connections occasionally target "their own" race. Checked and verified
  correct (True/False cases including surname-only matches), but across our
  current 411-race backfilled sample it never actually fired -- sponsor-named
  races are a minority of any card, and it's an opportunistic signal, not a
  constant one. It'll surface for real once it happens to line up on a live
  card; the Connections tab has a dedicated section for it.

Refresh the jockey/trainer/money-leader stats anytime via the sidebar/tab
"Refresh" buttons; owner/breeder stats are always computed live from
whatever's currently backfilled, no refresh needed. Browse full leaderboards
per venue in the **Connections** tab.

Staking is edge-over-random-pick (no pre-race tote odds are published by
either site, so true odds-based Kelly isn't possible yet -- see
`models/staking.py` docstring). Races where no horse clears a minimum edge
are correctly skipped, not force-picked.

## Backtest & weight tuning

`python scripts/backtest.py [--venue Pune] [--tune]` replays every archived
race with results and benchmarks the model's top pick against the **tote
favourite** (the betting public's collective prediction -- the strongest
verifiable benchmark, since racingpulse's selections are paywalled and free
tip blogs keep no checkable archive), the top-rated horse, and a random
pick. Also shown in the app's **Backtest** tab. Findings on 401 races
(favourites/odds parsed from both clubs' results pages; BTC's "Tote Fav"
card number resolved to a horse name -- an earlier bug that conflated it
with the winner is fixed):

- Tote favourite 48.4%, model top pick 41.9% (top-3 75.6%), top-rated horse
  25.4%, random 12.7%.
- **Market-agreement pattern:** model pick == favourite -> won 63.9%; model
  disagreed with market -> model 24.0% vs favourite 35.7%. When the live
  odds board disagrees with the model, the market has historically been
  right -- size down or skip.
- Weights were re-tuned by grid search minimizing winner log-loss (same
  config won on Pune alone and all venues pooled): rating 0.35->0.25, form
  0.15->0.25, connections 0.48->0.40 total, softmax sharpness 3->5. Lifted
  backtested top-pick hit rate ~1.5-2pp and improved calibration.
- Caveats: jockey/trainer stats are current-season snapshots applied
  retroactively and owner/breeder rates derive from the same archive being
  tested (lookahead flatters connection signals); rating/form/favourite
  numbers are point-in-time clean. Rerun after each race weekend.

## Phase 2 (as the season's data accumulates)

- Backfill results across the season to calibrate real speed figures (time
  vs. par, adjusted for weight/going) instead of relying on rating alone.
- Jockey-trainer *combo* strike rates (currently scored independently), draw
  bias, going bias.
- racingpulse.in / indiarace.com as secondary sources if useful gaps remain.
- Calibration dashboard (already scaffolded in the Bankroll tab) will start
  producing meaningful numbers once enough bets are logged with outcomes.
- Owner/breeder self-derived stats will get more reliable as more seasons
  get backfilled -- currently pooled from ~411 races across 3 venues, which
  is still a thin sample for anything but the most prolific operations.

## Known limitations

- No pre-race market odds available from either site -- staking is
  confidence-tiered, not true expected-value.
- Speed figures aren't implemented yet (Phase 2) -- ranking currently uses
  rating + form + connections only.
- Parsers are regex/line-position based against RWITC's legacy HTML; if the
  site's markup changes, `scrapers/rwitc.py` will need re-validating against
  a fresh sample page.
- Jockey/trainer stats are club-wide season snapshots, not filtered to a
  specific meeting or track condition -- refresh them periodically rather
  than trusting a stale pull from weeks ago.
- BTC's racecard doesn't expose a "Breeder" field, only "Stud" -- the
  breeder signal falls back to stud name for Bangalore horses, which is
  usually but not always the same operation.
- Owner/breeder strike rates use whatever's currently in `results` at query
  time, including same-day results for races already run -- there's no
  point-in-time cutoff, so re-scoring a past race day isn't a strict
  backtest (a known simplification, not silently hidden).

## Place bets (top-2 / top-3)

A dedicated **Place Bets** tab derives each horse's probability of finishing
in the paid places from the win model (Harville order statistics), and flags
🎯 **VALUE** — consistent placers (high place %, low win %) the crowd tends to
underprice in the place pool. Places paid are verified empirically from RWITC
tote dividends: **8+ runners → 3 places, 5–7 → 2, ≤4 → win-only**. Lower
variance than win betting, not higher edge — the same fair-odds discipline
applies (fair place odds = 100 ÷ place% − 1), and there are no pre-race place
odds published, so it's a shortlist to price up at the board.
