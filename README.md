# Race Predictor -- India / Australia / Hong Kong

A local tool for entertainment analysis of horse racing across three circuits:
India (Pune and Mumbai via RWITC, Bangalore via BTC), Australia (Racing
Australia), and Hong Kong (HKJC). Enter a day's budget and goal, get a ranked,
reasoned shortlist per race and a staking plan that respects that budget.
**Not a winner-picker** -- horse racing is genuinely unpredictable. See the
Bankroll & Calibration tab for an honest, ongoing record of how the model's
confidence has actually tracked outcomes.

The three circuits are not equivalent, and the difference decides what the
tool can honestly do on each:

| | Fields & form | Official rating | Pre-race odds | EV & parlays |
|---|---|---|---|---|
| **India** | yes | yes | **no** | no |
| **Australia** | yes | yes | manual paste | yes |
| **Hong Kong** | in season | yes | yes (live) | yes |

The Indian clubs publish no pre-race odds at all -- prices appear only after
the race, on the results page. Without a market price there is nothing to
measure an edge against, so the India circuit gets ranked picks and a staking
plan but no expected value and no parlays. That is a limitation of the data,
not of the model.

## Read this before betting a parlay

A multi is the worst-priced product on any board, and the reason is
arithmetic, not opinion. Every leg is priced with the bookmaker's margin
already inside it, and combining legs multiplies those margins:

- Three legs into a typical 16% Australian book = betting into a **36%**
  margin.
- A three-leg Hong Kong all-up gives up about **44%** to tote takeout before
  anyone has an opinion about a horse.

No staking plan, bankroll rule or selection method overcomes a 36% head start.
There is exactly one condition under which a multi is worth placing, and
`models/parlay.py` enforces it: **every single leg must be independently +EV**,
meaning the price on offer is longer than our best estimate of that runner's
true chance. Then multiplying the legs multiplies an edge instead of a deficit.

Consequences that are features, not bugs:

- **Most days produce no qualifying multi.** An empty result means the market
  was efficient, which is a market's normal state. The tool says so rather
  than manufacturing a tip.
- **Singles are listed above multis.** A single on a value selection has the
  same edge as that leg inside a multi with a fraction of the variance. If the
  goal is a small regular return, the singles table is the honest answer.
- **Two runners from the same race are never combined.** They are not
  independent -- they cannot both win -- so multiplying their probabilities
  overstates the multi's real chance. Same-race combinations belong in the
  Forecast/Quinella planner, which prices them properly.

### On daily profit targets

Wanting a fixed return per day is the most common way a betting plan fails,
because the target is fixed and the results are not. The Daily Parlays tab
answers the question directly for whatever target you enter: the stake it
would take, the probability it lands, and the expected P&L at that stake. When
the required stake exceeds the Kelly stake it says so, because past that point
you are growing risk faster than return.

A worked example the tool will print for you: a multi paying 6.13 that lands
19% of the time needs a Rs146 stake to clear Rs750 -- and loses that stake on
81% of days. Even with a genuine +5% edge, the honest expectation is a few
hundred rupees a month with a losing run of a fortnight inside it, not
Rs500-1000 banked daily. The edge is real or it isn't; the *schedule* is
never under your control.

## Run it

```
venv\Scripts\activate
streamlit run app.py
```

Opens at http://localhost:8501. Pick a **circuit** in the sidebar first, then a
date:

- **India** -- pick a venue and click **Fetch live**. Off-season or if the site
  is unreachable, use **Manual paste fallback** with the saved HTML source.
- **Australia** -- pick states, click **Find meetings**, tick the ones you want
  and **Load fields**. Racing happens somewhere every day of the year, usually
  at 5-10 tracks at once, so load only what you'll actually look at.
- **Hong Kong** -- pick the racecourse and click **Load card**. In season
  (September to mid-July) this also pulls live win odds automatically.

## The daily routine

Everything the app does is also available as one command, which is the way to
run it day to day:

```
python -m scripts.daily                             # today's Australian slate
python -m scripts.daily --date 2026-08-01 --states NSW VIC
python -m scripts.daily --circuit "Hong Kong"
python -m scripts.daily --settle 2026-07-30         # grade yesterday's slips
python -m scripts.daily --results 2026-07-30        # pull results + starting prices
python -m scripts.daily --backfill 21               # archive 3 weeks of results
```

Morning: it settles yesterday, loads today's fields, ranks the strongest model
opinions so you know which races are worth pricing up, and prints any multi
that survives the EV test. Evening: `--settle` tells you whether the day made
money.

It will not place a bet -- nothing in this project talks to a bookmaker -- and
it will not invent a suggestion to fill the page.

## Getting odds in

Every Australian source is walled off from an Indian IP -- except one. Tested
from this machine, July 2026:

| Source | Status |
|---|---|
| **NZ TAB affiliate API** | **works -- live fixed win/place odds, whole field** |
| HKJC win odds and dividends | works, in season |
| Racing Australia results (starting prices) | works, but only *after* the race |
| TAB.com.au public API | geo-blocked -- region-unavailable page |
| punters.com.au, racenet.com.au | 403 -- CloudFront |
| Sportsbet | 403 -- Akamai "Access Denied" |
| PointsBet | Cloudflare challenge |
| Betfair (api, identity, AU site) | 403 at the edge |
| Neds / Ladbrokes (Entain) | 500 from their gateway |

**NZ TAB is the answer for Australian odds.** It books all the major
Australian meetings and isn't geo-fenced the way tab.com.au is:

```
https://api.tab.co.nz/affiliates/v1/racing/meetings?date_from=&date_to=
https://api.tab.co.nz/affiliates/v1/racing/events/{race_id}
```

Every runner carries `fixed_win`, `fixed_place`, `pool_win`, `pool_place`,
plus barrier, jockey, trainer, weight, form and price fluctuations. In a real
run it priced **189 runners across 16 live races in two meetings**, taking
price coverage from 0% to 100% and getting every race past the coverage guard.
No key, no account, no registration.

`scrapers/tabnz.py` implements it. Two traps it handles, both found the hard
way:

- **The parameter is `date_from`/`date_to`, not `date`.** A `date` parameter is
  accepted, silently ignored, and you get a valid 200 full of real odds *for
  the wrong day*. The endpoint echoes its parsed parameters back, so the
  scraper asserts they match what was asked for and raises if not.
- **Settled races keep stale, pre-scratching fixed odds.** A real Eagle Farm
  race showed a normal 1.203 book across all ten runners but 0.906 across the
  seven that started, because three were scratched and the price was never
  revised. A sub-1.0 book is meaningless to de-vig, so only live races are
  fetched.

**Whose price is it, though.** These are NZ TAB's prices. If you place bets
somewhere else, the edge that matters is measured against *that* book's price.
Use this feed to find races worth a look and as a fair-price benchmark, then
confirm the number where you actually bet. Prices from here are stored under
the source `tabnz`, which deliberately ranks *below* anything you enter
yourself, so a pasted price always wins.

The other paths still exist:

1. **Paste them** (Odds tab) -- overrides the automatic feed. Recognised
   shapes: `MAGIC MOMENT 3.40`, `7. Magic Moment $3.40 $1.55`, `Magic Moment
   5/2`. Header and junk lines are ignored.
2. **HKJC** -- scraped automatically in season.
3. **Racing Australia SPs** -- automatic but post-race. Useless for betting,
   essential for checking whether the model is any good.
4. **An API you already have access to** -- a generic adapter configured
   entirely by environment variables.

### If you have a bookmaker API key

**Do not paste an API key into a chat window, a source file, or anything that
gets committed.** Put it in a `.env` file in the project root -- `.gitignore`
already covers it -- and the app reads it from there:

```
ODDS_API_URL=https://.../races/{race}/odds
ODDS_API_TOKEN=your-key-here
ODDS_API_TOKEN_HEADER=x-access-token
ODDS_API_RUNNER_PATH=data.race.runners
ODDS_API_NAME_FIELD=name
ODDS_API_WIN_FIELD=winOdds
ODDS_API_PLACE_FIELD=placeOdds
```

The adapter is deliberately book-agnostic rather than hard-coded to one
provider's schema, since those change without notice. The Odds tab shows
whether it's configured without ever displaying the key.

**Prices for a race must cover at least 80% of the field** (90% for place
bets), or the engine refuses the race. This is not fussiness. De-vigging works
by scaling implied probabilities to sum to 1; do that to a subset and you
don't remove a margin, you invent one, and every priced runner looks like
enormous value. A live run with 3 of 14 runners priced reported a *220% edge*
before this guard existed.

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
- **Australia (Racing Australia)** -- `scrapers/racingaustralia.py`.
  `/FreeFields/Calendar.aspx?State=NSW` for the fixture list,
  `/FreeFields/Form.aspx?Key=2026Aug01,NSW,Rosehill Gardens` for fields plus
  full per-horse form, `/FreeFields/Results.aspx?Key=...` for the finishing
  order and every runner's decimal starting price. Chosen over the tipping and
  odds sites because it's the national industry body (nothing sits between it
  and the stewards), it's free, and it's the only one that answers from India.
  It gives us three things the Indian clubs never did: an official handicap
  rating for nearly every runner, a 10-run form string, and a real market
  price.

  One gotcha, verified rather than assumed: **Racing Australia's "Last 10"
  reads oldest-first**, left to right -- the opposite of RWITC. Confirmed by
  matching HELLOVA NATURE's `90x0321121` against its dated run list, where the
  trailing `21121` lines up with its Apr-Jul placings 2,1,1,2,1. The rating
  engine weights the *first* entry heaviest, so the parser reverses the string
  on the way in. Getting this backwards would have silently inverted the form
  signal on every Australian runner.
- **Hong Kong (HKJC)** -- `scrapers/hkjc.py`.
  `/racing/information/English/Racing/LocalResults.aspx?RaceDate=YYYY/MM/DD&Racecourse=HV&RaceNo=N`
  for results and `RaceCard.aspx` for the card. The best-documented racing
  jurisdiction anywhere for this purpose: a closed pool of about 1,200 rated
  horses, two courses, and the club publishes finishing times, sectional
  running positions, every runner's win odds and the full dividend table for
  every pool.

  Two structural facts the code depends on: **dividends are quoted per HK$10
  stake**, not per HK$1 (a WIN dividend of 111.00 is a decimal price of 11.1),
  handled in one place by `dividend_to_decimal()`; and **the season runs
  September to mid-July**, so between mid-July and September there is no
  Hong Kong racing at all -- not a reduced card, none. `season_status()` exists
  so the app can tell "no card published" apart from "the scraper broke".

  The results parser is verified against real meetings. The **race-card parser
  is provisional**: HKJC withdraws a card once its meeting has run, and the
  season was already over when it was written, so it matches the club's
  published layout but has not been run against a live card. It fails soft
  (returns nothing rather than raising). Verify it on the first meeting of the
  new season before trusting a number that comes out of it.

## Odds maths (`models/odds.py`)

Three ideas everything on the AU/HK circuits rests on:

1. **A book doesn't sum to 100%.** It sums to more, and the excess is the
   margin. Measured on real data: an Australian country SP book came to 1.210
   (a 17.4% bite), and a Happy Valley win pool to 1.221 -- against HKJC's
   *published* 17.5% takeout, which is a useful independent check that both the
   parser and the maths are right.
2. **Stripping the margin proportionally is wrong.** Racing markets show a
   persistent favourite-longshot bias: longshots are systematically overbet.
   Proportional de-vigging assumes the margin is spread evenly and so flatters
   longshots -- exactly where a naive model wants to bet. The **power method**
   (solve for *k* such that the implied probabilities raised to *k* sum to 1)
   shrinks long prices more than short ones. On that real Australian race it
   moved the favourite 38.4% -> 41.8% and the 71.00 outsider 1.2% -> 0.8%.
3. **The market is a strong opponent.** This project's own backtest found that
   when the model's pick and the tote favourite disagreed, *the favourite won
   more often*. So the model/market blend defaults to only **0.35** weight on
   our own model. Raising it makes the engine bolder and, on the evidence,
   worse. The slider in the Daily Parlays tab says so.

Place probabilities use the **discounted Harville** model (Lo &
Bacon-Shone), not plain Harville. Plain Harville treats the race as a sequence
of independent draws and therefore *overstates* how often a short-priced horse
fills a minor placing -- good horses tend to either win or finish well beaten
rather than politely collecting third. Backing a favourite to place on raw
Harville numbers looks like value more often than it is. Place legs also have
to clear a higher EV bar than win legs (5% vs 3%), because a place probability
is derived through a model whose residual error we can't see, where a win
probability is measured straight against a quoted win price.

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

- **India only:** no pre-race market odds available from either site --
  staking is confidence-tiered, not true expected-value.
- **Australia:** live prices come from NZ TAB, which is a *different book* from
  wherever you place bets. An edge measured against NZ TAB's price is not an
  edge at your bookmaker unless their price is as long -- always confirm before
  staking. Prices also move, so a fetch from three hours ago is a fictional
  edge; re-run before betting.
- **Big fields get skipped.** The engine only models fields of 5-16, and
  Australian country meetings routinely card 17-18 runners. On a real Port
  Macquarie card that excluded 4 of 8 races. Raise `MAX_FIELD_SIZE` in
  `models/parlay.py` if you want them, but be aware those races are genuinely
  harder to forecast.
- Racing Australia publishes no jockey/trainer strike-rate leaderboard,
  so on the AU and HK circuits those signals build up from your own archived
  results; run `python -m scripts.daily --backfill 21` before expecting the
  connections signal to mean anything.
- **Hong Kong:** in season only (September to mid-July), and the race-card
  parser is unverified until the new season opens -- see Data sources.
- **The parlay engine's probabilities are unproven.** The Followup tab's
  calibration table is the thing to watch: if slips predicted to land 25% of
  the time land 12% of the time, the model is overconfident and every stake it
  suggests is too big. It takes 30-50 settled slips before that table says
  anything real, and until then every number in the app should be treated as a
  hypothesis.
- `use_container_width` is deprecated in the installed Streamlit and warns on
  every render. Harmless today, but it's scheduled for removal and the whole
  file will need `width='stretch'` at some point.
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
