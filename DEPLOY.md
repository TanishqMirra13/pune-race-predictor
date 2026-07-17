# Putting the predictor on the web (phone-friendly)

**Why not Vercel:** Vercel only runs serverless functions and static sites.
Streamlit is a long-running server holding a live connection to each visitor,
so it physically cannot run there. The purpose-built free host is **Streamlit
Community Cloud** — that's what these steps use.

Everything in this repo is already deploy-ready (pinned `requirements.txt`,
`.streamlit/config.toml`, a committed seed database with all backfilled races,
workouts, and today's results). You only need to do the account + click steps
below — those require *your* logins, so they can't be automated.

## One-time setup (~5 minutes)

1. **GitHub account** — make one at github.com if you don't have it.
2. **Create an empty repo** there, e.g. `pune-race-predictor` (Private is fine —
   Streamlit Cloud can deploy private repos).
3. **Push this project up.** In a terminal here:
   ```
   cd C:\Users\tanis\Desktop\pune-race-predictor
   git remote add origin https://github.com/<your-username>/pune-race-predictor.git
   git push -u origin main
   ```
   (The repo is already initialised and committed locally — just add the remote
   and push.)
4. **Deploy:** go to https://share.streamlit.io → sign in with GitHub → **New
   app** → pick your repo, branch `main`, main file `app.py` → **Deploy**.
   After ~2 minutes you get a permanent URL like
   `https://pune-race-predictor.streamlit.app` that works on any phone browser.
5. **Lock it to just you (optional but recommended):** in the app's Settings →
   Sharing, set it private and add your email(s). Then only people you list can
   open it — that's your "login".

## The one real limitation, stated plainly

Streamlit Cloud gives each app a **temporary disk**. That means:

- **Everything you READ works perfectly** — all 400+ backfilled races, the
  workout data, jockey/trainer/owner stats, backtest, and today's seeded
  calibration all ship inside the committed `data/pune_racing.db` and are
  there the moment it deploys.
- **New WRITES don't survive a restart.** Bankroll bets you log, and race cards
  you "Fetch live" on the cloud, persist while you're using it but reset to the
  seeded snapshot whenever the container recycles (a redeploy, or after a spell
  of inactivity).

For checking picks and learning odds at the track, that's fine. If you want the
**bankroll log to persist forever** (so calibration keeps growing across
months), the clean fix is a free hosted Postgres (Neon or Supabase) holding
just the `bankroll_log` table — ask and I'll wire that in as a follow-up.

## Updating the deployed app later

Any time we improve the model, just push again:
```
git add -A && git commit -m "describe the change" && git push
```
Streamlit Cloud auto-redeploys within a minute. To refresh the *seed* data
(e.g. after backfilling more results), committing the updated `data/pune_racing.db`
ships it with the next push.
