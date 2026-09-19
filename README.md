# Diggers and Riggers — FPL Command Centre

A single self-contained dashboard for FPL mini-league **209817**, built with Vue 3 (via CDN) and Tailwind CSS (Play CDN) — no build step, no npm install. This package also includes a script and GitHub Actions workflow that keep its data fresh automatically.

## What's in here

```
index.html                       the whole dashboard (open it, that's the site)
refresh_data.py                  fetches live FPL data and patches it into index.html
.github/workflows/refresh.yml    runs refresh_data.py on a schedule
```

## 1. Put this in a public GitHub repo

- Create a new repo on GitHub, **public** (nothing sensitive lives in it — it's a script that calls FPL's own public API; your league's names are already visible on the live site regardless).
- Upload all three items above, keeping the folder structure — `refresh_data.py` and `index.html` at the repo root, the `.github/workflows/refresh.yml` path exactly as-is.
- **Public matters here**: GitHub Pages (step 2) only works for free on public repos — private repos need a paid GitHub plan to use Pages. Public also means GitHub Actions minutes are unlimited, so the schedule in step 3 costs you nothing.

## 2. Turn on GitHub Pages

In the repo: **Settings → Pages → Build and deployment → Source: "Deploy from a branch"** → Branch: `main`, folder: `/ (root)` → **Save**.

GitHub gives you a URL like `https://yourusername.github.io/your-repo-name/`. That's your live site — share that link with the group. It redeploys automatically within a minute or so every time `index.html` changes, which brings us to:

## 3. Let it auto-refresh

The workflow in `.github/workflows/refresh.yml` is already wired up to:
- Run every 15 minutes (`workflow_dispatch` is also enabled, so you can trigger it manually from the **Actions** tab any time too)
- Call `refresh_data.py`, which decides for itself whether it's actually worth doing anything
- Commit and push `index.html` only if the data actually changed

**One setting to check first:** go to **Settings → Actions → General → Workflow permissions**, and make sure **"Read and write permissions"** is selected. Without this, the bot can't push its own commits back to the repo (it'll fail silently otherwise). This is a one-time setup step.

### Why every 15 minutes doesn't mean 150+ API calls every 15 minutes

`refresh_data.py` always does 3 cheap calls first (player list, fixtures, your league's standings) and then decides:

- **A gameweek is "live"** (from its first kickoff to a few hours after its last, so it covers the whole matchday, not just exact playing minutes) → do the full refresh (all 6 squads, transfer logs, ~150 players' histories — takes about 10 seconds).
- **Not live, but it's been 2+ hours since the last full refresh** → do the full refresh anyway, so prices/injury news/ownership don't go stale for days.
- **Otherwise** → exit immediately. No commit, no extra load on FPL's API, costs GitHub about a second of runtime.

In practice that's roughly: every 15 minutes during actual gameweeks, roughly every 2 hours the rest of the time. You can tune this — both `BASELINE_REFRESH_MINUTES` and `LIVE_WINDOW_BUFFER_HOURS` are constants right at the top of `refresh_data.py`.

The little **"next data refresh in Xm"** text in the site's header and footer reflects whichever of those two intervals actually applied last time it ran — it's not a fixed promise, just an honest estimate based on the last refresh.

## Updating it manually / testing it yourself

You don't have to wait for the cron: from the repo's **Actions** tab, pick the "Refresh FPL data" workflow and hit **"Run workflow"**. Or run it locally:

```bash
python3 refresh_data.py
```
(needs only the Python standard library — no `pip install` required). It edits `index.html` in place; if nothing needed refreshing, it says so and leaves the file untouched.

## Why it can't be instant, second-by-second live

FPL's API doesn't send CORS headers, so a browser can never call it directly from any other site — that's a wall regardless of how this is hosted. This setup works around that by having GitHub's servers (not your visitors' browsers) do the fetching on a timer, then re-publishing the result. It's "live" in the sense of "refreshes automatically while you're not looking," not "ticks up as a goal goes in while you're watching." True second-by-second live scoring would need a page that polls a backend continuously while open — a meaningfully bigger project (a small always-on proxy plus polling JS in the page) — ask if you ever want to go there.

## Customising

- **Club crests**: by default every club shows as a small coloured circle with its short code (e.g. "BRE"). To use real badges instead, create a `badges/` folder next to `index.html` and drop in a PNG named after each club's short code — `badges/ARS.png`, `badges/BRE.png`, `badges/MCI.png`, and so on (the same codes already shown in the dashboard today, and the ones `refresh_data.py` prints via each team's `short_name`). You don't need all 20 at once — any club without a matching file just falls back to the coloured circle automatically, and player rows pick up the same crest as a small badge in the corner of their avatar. Keep the images reasonably small (a few KB each) since they're fetched over the network. Because badges aren't official club-colour data from the FPL API, sourcing and rights for the images are on you — this just wires up the display.
- **Forfeit tracker eligibility / house rules**: near the top of the big `<script>` block in `index.html`, the `FORFEITS` array and `FORFEIT_ENTRY_IDS` list. Edit directly.
- **Styling**: CSS custom properties (`--purple`, `--pink`, `--green`, etc.) at the top of the `<style>` block. Font is Inter throughout.
- **Prediction model**: the points-projection formula lives in `refresh_data.py` (search for `pred1_raw` / `pred5_raw`) — it's a documented heuristic (form + points-per-game + official fixture difficulty + FPL's own `ep_next`), not a trained model.
- **A different league**: change `LEAGUE_ID` at the top of `refresh_data.py`. Manager IDs are discovered automatically from that league's standings, so you don't need to hunt those down yourself.
