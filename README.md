# Indian Race Predictor

A local tool for entertainment analysis of Indian horse racing. Enter a day's
budget and goal, get a ranked, reasoned shortlist per race, a staking plan that
respects that budget, and a jackpot ticket built to actually hit.
**Not a winner-picker** — horse racing is genuinely unpredictable. See the
Bankroll & Calibration tab for an honest, ongoing record of how the model's
confidence has actually tracked outcomes.

The Australian and Hong Kong circuits that used to sit alongside this have been
removed. They were a distraction from the only circuit whose data this project
can actually get at reliably, and covering three jurisdictions badly is worse
than covering one properly.

## The edge, such as it is

Everything below this heading is measured from the archive, and the app
recomputes all of it live rather than quoting these numbers from a constant.

### There are two markets on every race, and one is 8 points cheaper

The result pages carry two independent prices for the same horse. `odds_sp` is
the **on-course bookmaker ring's** starting price; `dividend_win` is the
**tote's** pari-mutuel payout. They are not a transform of one another -- the
tote pays more on 10-22% of winners, and the ratio ranges from 0.37 to over 1.

Backing every runner in every archived race, 4,302 bets across 488 races:

| Market | Return |
|---|---:|
| On-course bookmaker ring | **-38.4%** |
| Tote | **-46.3%** |

**The tote costs an extra 7.9 points of every rupee staked**, on every bet,
before anyone has an opinion about a horse. That is larger than every
handicapping signal in this project combined, it needs no model, and the sample
is past arguing about. Where a bet can go in either market, it goes in the ring.
Jackpots are tote-only by necessity -- Indian clubs run no other pool for them.

### Inside the ring, price is the only thing that has predicted return

| Ring SP | Bets | Won | Price implies | Return |
|---|---:|---:|---:|---:|
| 1.00-2.00 | 167 | **67.1%** | 60.3% | **+11.7%** |
| 2.00-3.00 | 218 | 41.7% | 41.3% | +1.1% |
| 3.00-4.50 | 290 | 29.0% | 28.5% | +1.1% |
| 4.50-7.00 | 488 | 15.4% | 18.4% | -16.1% |
| 7.00-11.0 | 584 | 10.1% | 12.1% | -16.4% |
| 11.0-21.0 | 1,295 | 3.9% | 7.4% | -47.6% |
| 21.0+ | 1,260 | 1.3% | 4.5% | -70.5% |

Favourite-longshot bias, unusually severe. The two longest bands are 2,555 of
the 4,302 bets and account for essentially all of the damage: **not betting
those is worth more than any selection method in this app.**

**The paying band is not statistically significant.** +11.7% carries a 95%
interval of [-0.8%, +23.6%] on 167 bets -- it touches zero. What stops it being
dismissed is that it comes out at +11.8% in the first half of the archive by
date and +11.5% in the second, that it is positive at five of six venues, and
that favourite-longshot bias is the most replicated inefficiency in racing and
is strongest where takeout is heavy and money is unsophisticated. Treat it as a
hypothesis worth small stakes and a long record, not a proven edge.

The **Edge** tab is the whole of this: the band table, a price checker that
gives BET / THIN / SKIP off the ring board, and a bet log that settles against
real results so the hypothesis gets confirmed or killed. It refuses a verdict
under 50 settled bets, which at roughly two qualifying bets a race day is a
season.

Two things deliberately absent. There is **no measured edge for place bets**,
because there is no data -- not one place dividend exists in the archive, so no
place return has ever been computed at any price. And the filter "only back the
ring favourite when the tote disagrees" is **not** implemented: it looked strong
on one cut (-20.7%) and like nothing on another (-3.6%) on ~66 races. A filter
whose sign moves when you look at it differently is not a filter.

### What this cannot do

It cannot produce a daily income. The paying band fires about twice a race day.
At +11% on Rs500 stakes that is roughly Rs110 expected per race day with swings
far larger than that, and the true edge may be zero. Anyone reading the table
above as a salary has misread it.

## Indian racing is not one circuit

It is six active clubs under four regional turf authorities, and the grouping is
not cosmetic. It decides when a venue races, which source answers for it, which
fields come back populated, and how predictable the place is. The app calls
these **verticals**, groups every venue picker by them, and gives them their own
tab. `models/verticals.py` is the single place that knows the structure.

| Vertical | Authority | Venues | Source | Archived | Favourite wins |
|---|---|---|---|---:|---:|
| **Western** | RWITC | Mumbai (Mahalaxmi), Pune | rwitc.com | 371 races | 46.7% |
| **Southern** | BTC / Mysore RC / Hyderabad RC | Bangalore, Mysore, Hyderabad (Malakpet) | bangaloreraces.com, indiarace.com | 135 races | 54.3% |
| **Eastern** | RCTC | Kolkata | indiarace.com | 14 races | 28.6% |
| **Northern** | DRC | Delhi | indiarace.com | 0 races | — |

**Favourite wins** is the number that matters most per vertical, because it is
the benchmark this model has to beat there. It is measured from the archive at
runtime rather than quoted from anywhere, and it moves the practical advice: in
the Southern vertical the favourite wins more than half the time and the market
is very hard to argue with; in the Eastern one there are fourteen races on
record, which is a story, not a statistic.

Three structural facts that fall out of the grouping and are easy to mistake
for bugs:

- **Mumbai and Pune are the same club.** RWITC runs Mumbai over the cool months
  and Pune through the monsoon, so "no Mumbai card in August" is the calendar
  working, not the scraper failing.
- **Venues are not equally well described.** RWITC publishes breeder, stud,
  foaled date and a full per-run history. BTC publishes recent form as letter
  codes rather than numeric placings, so the form signal is left blank rather
  than guessed. The four indiarace-sourced clubs give neither breeder nor
  foaled date. Every gap is listed per venue in the Verticals tab.
- **Chennai and Ooty are deliberately not wired up.** indiarace's own fixture
  feed stops in Oct 2025 and Jun 2024 for those two, so a venue entry would
  promise data that is not there.

## The jackpot planner

A jackpot pays only if you hold the winner of every leg, so the question is not
which horse to back but where to spend the combinations you can afford. The
first version of this planner ranked legs by the model's own probability, took
the top pick alone if it cleared 35% and otherwise spread the top three evenly.
Replayed over the archive, both of those choices were close to the worst
available. Two measured findings drove the rebuild — see `models/jackpot.py`
for the full working.

### 1. The market picks legs far better than the model

Share of races whose winner came from the top N of each ranking, over 431
fully-priced Indian races:

| | top 1 | top 2 | top 3 | top 4 |
|---|---:|---:|---:|---:|
| Starting-price market | 47% | 67% | 79% | 88% |
| This model | 27% | 44% | 58% | 71% |

Compounded across legs, that gap is enormous. Whole tickets, replayed over every
complete archived race day, covering the top three of each ranking in every leg:

| Legs | Combinations | Market-ranked | Model-ranked |
|---:|---:|---:|---:|
| 3 | 27 | 40.3% | 19.4% |
| 4 | 81 | 32.3% | 13.8% |
| 5 | 243 | 25.4% | 7.9% |

Same money, three times the hit rate. This is the same conclusion the win-bet
backtest reached — where model and market disagree, the market is right —
applied where it compounds hardest. So the planner is market-led by default.
The model weight slider starts at zero, and says why.

### 2. Spreading evenly wastes combinations

Legs are not equally hard. One race has a standout; the next has five runners
the market cannot separate. An even spread buys coverage where it was already
cheap and refuses it where it is needed. The planner allocates by marginal value
instead — each extra runner goes wherever it buys the most extra chance per
rupee — then rebalances, because pure marginal efficiency has a standing bias
toward the leg it has already spent on and leaves tickets lopsided.

| Legs | Combinations | Even spread | This planner |
|---:|---:|---:|---:|
| 3 | 27 | 40.6% | 50.7% |
| 4 | 16 | 8.7% | 20.3% |
| 4 | 81 | 31.9% | 46.4% |
| 5 | 32 | 5.8% | 11.6% |
| 5 | 243 | 24.6% | 36.2% |
| 6 | 729 | 22.6% | 32.3% |

The advantage narrows to nothing once the budget covers four runners in every
leg, which is where the shape of a race stops mattering because you have bought
most of it. Below that — every realistic budget — it is worth 6 to 15 points.

Together the two changes take a five-leg jackpot at 243 combinations from
**7.9% to 36.2%**: a bet that lands about one race day in three instead of one
in thirteen. On 69 race days the 95% confidence interval is about ±12 points, so
read the direction rather than the decimals.

### Three things the planner tells you that a hit rate does not

- **A jackpot is pari-mutuel.** The dividend is the pool split among everyone
  holding the same line, and covering the market's favourites is exactly the
  line most other tickets hold — so the combinations that hit most often pay
  least when they do. Hyderabad, 10 Aug 2026: the front of the market won all
  five legs, 1,468 tickets shared the pool, it paid Rs317. The day before, the
  same bet paid Rs32,647 to sixteen tickets. **Hitting it and making money are
  different questions**, and the planner prints the break-even dividend next to
  the hit probability so they can be asked separately.
- **The club chooses the legs, and it varies.** Hyderabad ran its jackpot over
  races 4-8 on 27 Jul 2026 and races 3-7 on 9 Aug. Every result page loaded now
  records which races made up each pool, so the planner offers the club's real
  legs rather than assuming the last five. Where nothing is archived it says it
  is guessing.
- **What the next rupee buys.** The planner names the runner it would add next,
  what that costs, and the dividend that would have to clear for it to be worth
  adding.

### Did it actually pay?

Hit rate is the wrong question on its own. Replaying the planner at 240
combinations against every archived pool settlement -- using the dividend and
the ticket count the club published, with your own ticket diluting the pool and
the 30% consolation tier counted:

| Legs ranked by | Pooled return | 95% interval | Median pool | Excl. best 3 |
|---|---:|---:|---:|---:|
| Starting price (upper bound) | **+236%** | +63% to +466% | -42% | +90% |
| The model (lower bound) | +130% | -18% to +332% | -100% | -11% |

Read the median next to the mean. **Most tickets lose** -- the median pool
returned -42% -- and the profit lives in a long tail, with the single best pool
supplying 27% of all winnings. That is the shape of a pari-mutuel return, and it
means a positive average needs a bankroll that survives the losing weeks to ever
be collected. The sample is 39 settlements from a handful of race days at two
venues; it is not a random sample of Indian racing.

One assumption worth ten seconds at the tote window: the units cancel only if a
dividend is quoted per one combination. If a combination costs twice what the
dividend is quoted per, every figure above is twice as good as reality.

**Carry-forwards are the one genuine tailwind.** Three of 39 settlements carried
(Rs33k, Rs33k, Rs48k) -- money added to the next pool that nobody paid takeout
on, which lowers the effective takeout of that running by its share of the pool.
Every carry-forward is now recorded and the planner flags the venue's most
recent one. It does not make the bet good on its own, but a pool that has just
carried is the one to prefer.

### The honest limit

A jackpot ticket must be submitted before the first leg runs, and a starting
price is only known after its own race. The replay above ranks legs on starting
prices, so it measures how good the method is when its input is good and is an
**upper bound** on what the same method does off race-morning forecast prices.
Closing that gap is what the odds-snapshot archive is for. On the one Kolkata
card where both exist the forecast agreed with the starting price on the
favourite in 6 races of 7 — encouraging, and nowhere near enough to conclude
anything. `scripts/early_price.py` still refuses a verdict under 30 races.

## Getting a price in

No Indian club publishes a live, machine-readable board. What exists:

| Source | Status |
|---|---|
| **indiarace forecast prices** | works — but **indicative**, race-day only, and covering just the front 4-5 runners (42-62% of a field) |
| **Club result pages (starting prices)** | exact, and only available *after* the race |
| A live tote board | exists at the track and on screens; nothing serves it machine-readably |

The partial coverage matters differently for different bets, and this is the
one place the app deliberately holds two standards:

- **The parlay engine refuses a race priced below 80% of its field**, and it is
  right to. De-vigging a subset invents an edge instead of removing a margin —
  a live run with 3 of 14 runners priced reported a *220% edge* before this
  guard existed.
- **The jackpot planner uses a partial book anyway.** A jackpot leg needs the
  market's running order at the front of the book, not a de-vigged book, and
  the front is exactly what indiarace prices. The planner scales a partial book
  by the measured full-book overround rather than normalising it to sum to 1
  (which is the step that would inflate it) and shares the remainder among the
  unpriced runners by model score.

Paste a board or exchange price in the Odds tab and it overrides the forecast
for that runner — exchange back-prices are the best thing to paste, since a much
thinner margin makes them a sharper estimate of true chance. Paste as close to
the off as you can; the app flags a price older than 90 minutes as stale,
because an edge measured against a stale price is fiction.

## Read this before betting a parlay

A multi is the worst-priced product on any board, and the reason is arithmetic,
not opinion. Every leg carries the tote's takeout, and combining legs multiplies
those margins. The median complete Indian starting-price book in this archive
comes to an overround of **1.203** — about 17% — measured over 396 races, so
three legs means betting into roughly a **43%** margin before anyone has an
opinion about a horse.

No staking plan, bankroll rule or selection method overcomes a 43% head start.
There is exactly one condition under which a multi is worth placing, and
`models/parlay.py` enforces it: **every single leg must be independently +EV**,
meaning the price on offer is longer than our best estimate of that runner's
true chance. Then multiplying the legs multiplies an edge instead of a deficit.

Consequences that are features, not bugs:

- **Most days produce no qualifying multi.** An empty result means the market
  was efficient, which is a market's normal state. The tool says so rather than
  manufacturing a tip.
- **Singles are listed above multis.** A single on a value selection has the
  same edge as that leg inside a multi with a fraction of the variance.
- **Two runners from the same race are never combined.** They cannot both win,
  so multiplying their probabilities overstates the multi's real chance.
  Same-race combinations belong in the Forecast/Quinella planner.

### On daily profit targets

Wanting a fixed return per day is the most common way a betting plan fails,
because the target is fixed and the results are not. The Daily Parlays tab
answers the question directly for whatever target you enter: the stake it would
take, the probability it lands, and the expected P&L at that stake. When the
required stake exceeds the Kelly stake it says so, because past that point you
are growing risk faster than return.

A worked example the tool will print for you: a multi paying 6.13 that lands 19%
of the time needs a Rs146 stake to clear Rs750 — and loses that stake on 81% of
days. Even with a genuine +5% edge, the honest expectation is a few hundred
rupees a month with a losing run of a fortnight inside it, not Rs500-1000 banked
daily. The edge is real or it isn't; the *schedule* is never under your control.

## Run it

```
venv\Scripts\activate
streamlit run app.py
```

Opens at http://localhost:8501. The **Edge** tab comes first because it is the
only screen built on a measured return rather than a model opinion, and it is
the one to have open in front of a bookmaker's board. Pick a venue in the sidebar — the picker is
grouped by vertical and marks the meetings already loaded for that date — then
**Fetch live**. Off-season or if the site is unreachable, use **Manual paste
fallback** with the saved HTML source.

**Fetch results** does more than settle the day: it also records the jackpot,
mini-jackpot and treble settlements from the foot of the same page, which is the
only published record of which races made up each pool and how many tickets
shared each dividend.

## The daily routine

Everything the app does is also available as one command, which is the way to
run it day to day:

```
python -m scripts.daily                          # every venue, today
python -m scripts.daily --vertical Western       # just RWITC (Mumbai/Pune)
python -m scripts.daily --venues Hyderabad Mysore
python -m scripts.daily --settle 2026-08-13      # grade yesterday's slips
python -m scripts.daily --results 2026-08-13     # results, SPs and pool dividends
python -m scripts.daily --backfill 21            # archive 3 weeks of results
python -m scripts.daily --snapshot               # record today's forecast prices
```

Morning: it settles yesterday, loads today's fields across the venues you asked
for, pulls the forecast prices, ranks the strongest model opinions so you know
which races are worth pricing up, prints any multi that survives the EV test,
and prints a jackpot plan per meeting. Evening: `--settle` tells you whether the
day made money.

It will not place a bet — nothing in this project talks to a bookmaker — and it
will not invent a suggestion to fill the page.

### The race-day habit (two commands, ~10 seconds)

This is the one routine worth adding, because it collects data that does not
otherwise exist anywhere:

```
python -m scripts.daily --snapshot                  # during the day, before racing
python -m scripts.daily --results 2026-08-13        # after racing
python -m scripts.daily --snapshot 2026-08-13       # again: settles the SPs
python scripts/early_price.py                       # the report, once data accrues
```

**Why it matters.** Every accuracy figure in this project benchmarks the model
against the *final* starting price — the sharpest number in racing, containing
all the late money. The model loses to it badly, but so does almost every model,
and that is not the question that decides whether betting it makes money. The
question that decides that is whether the model beats the price that was **on
offer when you would actually have bet**. Nobody archives Indian forecast
prices, so that has never been answerable. `--snapshot` starts building the
record; `scripts/early_price.py` reports it and deliberately **refuses to give a
verdict under 30 races**, because a noisy number that looks like an edge is
worse than no number.

## Placing the bets

The gap between "this is value" and "bet placed" is where automated betting
usually loses money: the edge is measured at one price and the bet goes on at
another. If the second is shorter, the edge is gone — and an automated system
keeps placing anyway, every day.

So every bet on the slip carries a **minimum acceptable price**, and nothing is
placed below it. A selection we make a 20% chance is worth 5.50 and not worth
4.60; the floor is 5.15 (a 3% edge), and at 4.60 the bet simply does not happen.
A skipped bet costs nothing. A bet at the wrong price costs money every time.

The Daily Parlays tab prints the slip: selection, stake, the price the edge was
found at, and the floor. Take it to your bookmaker, check the price, bet only the
rows that still qualify. There's a price-checker in the same tab that gives a
go/no-go on whatever number you're being shown.

Demonstrated with a book pricing 15% shorter across the board than where the
edges were found:

```
placed=0 skipped=3 staked=Rs0
  SKIP GATWICK: price 4.67 is below the 5.15 floor (9% short) -- at this book the edge is gone
  SKIP MISS BUSSLINGER: price 2.72 is below the 3.03 floor (10% short)
  SKIP RUN HARRY RUN: price 6.80 is below the 7.36 floor (8% short)
```

That is the system working. Those three bets would each have been losers at
those prices.

### Automated placement

`scrapers/bookmaker.py` has a provider-neutral adapter. `DryRunAdapter` is the
default: it runs the full flow, validates every bet, places nothing. Real
placement needs **four** switches — `allow_real_bets=True` in code, plus
`BET_API_ENABLED=true`, `BET_API_URL` and `BET_API_TOKEN` in `.env` — so neither
a stray default nor a copied `.env` can start moving money.

**Stake will not work for this.** Both `stake.com` and the `stake1021.com`
mirror return Cloudflare's bot challenge (`cf-mitigated: challenge`) to every
programmatic request, verified July 2026 — `stake.com` is also ISP-blocked from
Indian connections outright. Getting past a bot challenge needs a browser
fingerprint Cloudflare actively works to detect; this project does not do that.
The adapter recognises a Cloudflare block and says so rather than retrying.
Place manually from the bet slip instead.

### If you have a bookmaker API key

**Do not paste an API key into a chat window, a source file, or anything that
gets committed.** Put it in a `.env` file in the project root — `.gitignore`
already covers it — and the app reads it from there:

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
provider's schema, since those change without notice. The Odds tab shows whether
it's configured without ever displaying the key.

## Bulk historical backfill

Rather than fetching one day at a time through the UI, pull a whole date range
in one go:

```
python scripts/backfill.py --venue Bangalore --start 2026-06-13 --end 2026-07-12
python scripts/backfill.py --venue Mumbai --start 2025-11-01 --end 2026-04-30
python scripts/backfill.py --venue Pune --start 2025-07-18 --end 2025-10-20
python scripts/backfill.py --venue Hyderabad --start 2026-07-01 --end 2026-08-10
```

Works the same way for every venue in every vertical — same `--venue` flag, same
date-range behaviour. It fetches both the race card and results for every date
in range, skips non-race days silently (that's normal — every Indian venue only
races a few days a week, and several run seasonally), and caches each page under
`data/cache/` so re-runs don't re-hit the server.

## Data sources

- **Pune & Mumbai (RWITC, same site, auto-detects venue by date):**
  `https://rwitc.com/new/erp_racecard.php?date=YYYY-MM-DD` for per-horse entries
  (rating, weight, jockey, trainer, last-5-runs form), and
  `https://rwitc.com/erp_raceresult.php?date=YYYY-MM-DD` for finishing order,
  times, tote dividends (WIN/PLACE/SHP/FOR/QNL/TNL) and the multi-leg pool
  settlements (Super Jackpot, Jackpot with its 70%/30% tiers, and the trebles).
  Note RWITC numbers races cumulatively across a season, so an eight-race Pune
  card can be races 26-33 — the pool tables give their legs as positions in the
  day's card, and `models/jackpot.py` translates by position for that reason.
- **Bangalore (BTC):** `https://bangaloreraces.com/racing/racecard?d=YYYY-MM-DD`
  and `.../results?d=YYYY-MM-DD`. Modern semantic HTML (vs. RWITC's legacy
  nested tables), parsed in `scrapers/btc.py`. Two known gaps: recent form comes
  as letter codes rather than numeric placings (left blank rather than guessed),
  and the results table doesn't list a per-runner trainer.
- **Hyderabad, Mysore, Kolkata & Delhi (indiarace.com,
  `scrapers/indiarace_cards.py`):** these four clubs have no scrapable racecard
  of their own — Hyderabad's site has no plain HTML racecard route, Mysore's
  `/Racecard` and `/Results` 404 without params only its own JS supplies, RCTC
  gates racing data behind a separate login (rctclive.in), and Delhi publishes
  entries and results as PDFs only. indiarace carries a full racecard/result
  page for every club at
  `Home/racingCenterEvent?venueId={id}&event_date=YYYY-MM-DD&race_type=RACECARD|RESULT`.
  Two gaps vs. RWITC/BTC: breeder/stud/foaled date aren't broken out, and
  "Last 5 runs" order is assumed newest-first rather than independently
  verified.
- **Pool dividends.** Both sources carry a jackpot/treble settlement table at the
  foot of a result page, and nothing else in the pipeline records what is in it:
  which races made up each pool, and how many tickets shared each dividend.
  indiarace publishes its two-tier jackpot unlabelled ("9390 & 91296 (TKTS 25 &
  06)"); multiplying each dividend by its ticket count showed a 30.0/70.0 split
  of one pool on all five two-tier jackpots in the cache, which is how the tiers
  were identified — RWITC labels its equivalents explicitly and corroborates it.
- `standard_timings.pdf` (`data/standard_timings.pdf`, RWITC only) — par times by
  class and distance, for Phase 2 speed figures. Currently a 2011-dated file;
  treat as a rough reference until replaced by pars derived from our own archive.

## Odds maths (`models/odds.py`)

1. **A book doesn't sum to 100%.** It sums to more, and the excess is the
   margin. Measured over 396 complete Indian starting-price books, the median is
   **1.203**.
2. **Stripping the margin proportionally is wrong.** Racing markets show a
   persistent favourite-longshot bias: longshots are systematically overbet.
   Proportional de-vigging assumes the margin is spread evenly and so flatters
   longshots — exactly where a naive model wants to bet. The **power method**
   (solve for *k* such that the implied probabilities raised to *k* sum to 1)
   shrinks long prices more than short ones.
3. **The power method still under-corrects on an Indian tote.** Summing the top
   three de-vigged probabilities gives 72% where the winner actually came from
   that top three 80% of the time — a persistent gap, not noise. Raising the
   de-vigged probabilities to the power **1.25** and renormalising closes it
   across every N, and holds on both halves of the archive by date and at all
   three well-sampled venues. `models/jackpot.py` applies it. It changes no
   ranking, only the honesty of the "chance it lands" figure, which would
   otherwise read a third too low.
4. **The market is a strong opponent.** This project's own backtest found that
   when the model's pick and the tote favourite disagreed, *the favourite won
   more often*. So the model/market blend defaults to only **0.35** weight on
   our own model, and the jackpot planner starts at **0**.

Place probabilities use the **discounted Harville** model (Lo & Bacon-Shone),
not plain Harville. Plain Harville treats the race as a sequence of independent
draws and therefore *overstates* how often a short-priced horse fills a minor
placing. Place legs also have to clear a higher EV bar than win legs (5% vs 3%),
because a place probability is derived through a model whose residual error we
can't see.

## Scoring: technicals + fundamentals

Composite score per horse blends six things, mapped to within-race win
probability via softmax:

- **35% official handicap rating** (normalized within the field) — the richest
  single technical signal.
- **15% recency-weighted recent form** (last-5 placings).
- **12% jockey strike rate + 13% trainer strike rate** — official season-to-date
  stats scraped from each club's own published pages, Bayesian-shrunk toward the
  venue's population-average win% so a jockey with 3 rides and 2 wins doesn't
  outrank a proven rider with 150 rides — see `models/connections.py`.
- **13% owner strike rate + 12% breeder/stud strike rate** — self-derived from
  our own backfilled results archive, pooled across venues since ownership and
  breeding operations aren't venue-local the way jockeys and trainers are. This
  is the one signal not from an official source. RWITC's official season **Money
  Leaders** is scraped and shown in the Connections tab as a reference, but
  deliberately *not* fed into the score, to avoid mixing an earnings-based
  ranking with win-rate-based signals.
- **+0.08 flat bonus** when an owner's name plausibly overlaps with the race's
  own sponsor/title text — races in India are routinely named after their
  owner/breeder sponsors, and it's a real, public pattern that connections
  occasionally target "their own" race. Verified correct on True/False cases,
  but across the current 511-race sample it has never actually fired.

Refresh jockey/trainer/money-leader stats anytime via the sidebar buttons;
owner/breeder stats are computed live from whatever's backfilled.

## Backtest & weight tuning

`python scripts/backtest.py [--venue Pune] [--tune]` replays every archived race
with results and benchmarks the model's top pick against the **tote favourite**,
the top-rated horse, and a random pick. Also in the app's **Backtest** tab, where
the scope selector offers whole verticals as well as single venues — because
that is the level at which a sample gets big enough to say anything.

### The lookahead leak (fixed Aug 2026) — and why the numbers dropped

Earlier versions reported a model top-pick rate of ~42%. **That number was
inflated by a lookahead bug and is not real.** Jockey and trainer strike rates
came from a current-season snapshot applied retroactively, and owner/breeder
rates were derived from the whole results archive *including the very race being
graded*.

It was caught when four new venues were added: Hyderabad backtested at a
nonsensical **75.7%** top-pick win rate. No handicapping model wins three races
in four. `compute_composite_scores(..., as_of_date=...)` now rebuilds every
derived signal from results strictly before the race being scored. Live scoring
still uses the official current-season snapshot, which is correct — that
genuinely is what a punter knows on race day.

### Honest numbers (511 races, leak-free)

| | Top pick | Top-3 | |
|---|---:|---:|---|
| **Tote favourite** | **48.5%** | — | the benchmark to beat |
| Model top pick | 26.8% | 59.6% | |
| Top-rated horse | 22.1% | — | |
| Random | 12.3% | — | |

Per venue: Pune 28.2%, Mumbai 24.9%, Bangalore 23.9%, Hyderabad 35.1%, Mysore
28.0%, Kolkata 28.6% (only 7 races — ignore it).

**The model does not beat the market on any Indian vertical.** That is the honest
headline, and it is the same conclusion the market-agreement pattern has always
pointed at: when the model agrees with the favourite it wins 54% of the time;
when it disagrees it wins 11% while the favourite still wins 45%. If the board
disagrees with the pick here, trust the board. It is also the reason the jackpot
planner ranks legs by the market rather than by the model.

### On the Aug 2026 signal additions

Weight carried, distance/class-aware form, days-since-run, course & distance,
sire strike rate, rating gap and equipment change were all added in one pass (see
`models/form.py`). Measured effect: **24.7% → 26.4%**, which a McNemar paired
test rates **not statistically significant** (chi-sq 1.36, needs >3.84 for
p<0.05). A grid search found no better weight configuration. They are kept
because they're cheap and principled, but they are not a proven improvement.

With ~500 races the 95% confidence interval on any hit rate is about ±4pp, which
is wider than every effect measured in that pass. **Sample size, not signal
count, is now the binding constraint.**

## Phase 2

- **More archived races.** Every effect worth chasing is currently smaller than
  the ±4pp confidence interval. Backfilling more seasons is the highest-value
  action available, and it's just runtime.
- **Forecast-vs-SP rank agreement.** The jackpot planner's measured hit rates
  rest on starting-price ranks, which are not available at ticket time. The
  odds-snapshot archive answers how much is lost using race-morning forecast
  ranks instead — currently 7 races, which is nothing.
- **More pool dividends.** 46 settlements archived so far. The break-even
  comparison in the planner gets meaningful somewhere around 50-100 per pool
  family, per club.
- **Real speed figures** (time vs. par, adjusted for weight/going).
  `data/standard_timings.pdf` is parsed but never used to build one;
  `runs.recent_runs_json` preserves the per-run times needed. This is the biggest
  untapped signal, because it's an objective performance measure rather than the
  handicapper's opinion.
- Jockey-trainer *combo* strike rates (currently scored independently), draw
  bias, going bias.
- Rolling 30-day trainer/jockey form instead of season-to-date.
- Calibration dashboard (scaffolded in the Bankroll tab) will start producing
  meaningful numbers once enough bets are logged with outcomes.

## Known limitations

- **No live price feed exists**, so EV is only as current as the last price you
  pasted. With nothing pasted, staking falls back to confidence-tiered rather
  than true expected-value. Indian tote takeout is heavy — the engine puts a
  three-leg multi at roughly **43%** to takeout, which is why almost nothing
  clears the margin test.
- **The jackpot replay ranks on starting prices**, which are only known after
  each race. It is an upper bound on what the same method does off race-morning
  forecast prices.
- **Delhi has no archive at all.** Every derived signal there falls back to the
  cross-venue pool. Kolkata (14 races) and Mysore (25) are barely better.
- **Big fields get skipped by the parlay engine.** It only models fields of
  5-16; raise `MAX_FIELD_SIZE` in `models/parlay.py` if you want them, but be
  aware those races are genuinely harder to forecast. The jackpot planner has no
  such limit — a wide field is just an expensive leg.
- **The parlay engine's probabilities are unproven.** The Followup tab's
  calibration table is the thing to watch: if slips predicted to land 25% of the
  time land 12% of the time, the model is overconfident and every stake it
  suggests is too big. It takes 30-50 settled slips before that table says
  anything real.
- `use_container_width` is deprecated in the installed Streamlit and warns on
  every render. Harmless today, but scheduled for removal.
- Speed figures aren't implemented yet — ranking uses rating + form +
  connections only.
- Parsers are regex/line-position based against RWITC's legacy HTML; if the
  site's markup changes, `scrapers/rwitc.py` needs re-validating against a fresh
  sample page.
- Jockey/trainer stats are club-wide season snapshots, not filtered to a track
  condition — refresh them periodically rather than trusting a stale pull.
- Owner/breeder strike rates use whatever's in `results` at query time in live
  scoring, so re-scoring a past race day through the UI isn't a strict backtest.
  `scripts/backtest.py` does apply a point-in-time cutoff.

## Place bets (top-2 / top-3)

A dedicated **Place Bets** tab derives each horse's probability of finishing in
the paid places from the win model (Harville order statistics), and flags 🎯
**VALUE** — consistent placers (high place %, low win %) the crowd tends to
underprice in the place pool. Places paid are verified empirically from RWITC
tote dividends: **8+ runners → 3 places, 5–7 → 2, ≤4 → win-only**. Lower variance
than win betting, not higher edge — the same fair-odds discipline applies (fair
place odds = 100 ÷ place% − 1), and there are no pre-race place odds published,
so it's a shortlist to price up at the board.
