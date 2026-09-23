#!/usr/bin/env python3
"""
Refreshes the league data from the live FPL API.

Run this from the repo root: python refresh_data.py
It writes two files and never touches index.html:
  data.json        everything the site shows (the site loads it on open)
  model_log.json   each gameweek's final pre-deadline predictions, and how
                   they scored once the gameweek finished (model vs FPL's own)

Keeping data out of index.html means uploading a new version of the site can
never roll the data back to an old snapshot.

Designed to run unattended (e.g. from a GitHub Actions cron job), so it
retries transient errors and never half-writes a file.
"""
import json, math, os, re, statistics, sys, time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone, timedelta
import urllib.request
import urllib.error

LEAGUE_ID = 209817
INDEX_HTML = "index.html"          # only read to migrate from the old embedded-data setup
DATA_JSON = "data.json"
MODEL_LOG = "model_log.json"
MODEL_VERSION = 2
BASELINE_REFRESH_MINUTES = 120  # do a full refresh at least this often even when nothing is live
LIVE_WINDOW_BUFFER_HOURS = 3     # keep treating a gameweek as "live" for this long after its last kickoff
TOP_N_GLOBAL_PLAYERS = 140      # plus every player owned in the league, for the deep-dive modal
FIXTURE_HORIZON = 5             # gameweeks ahead to compute fixture runs / predictions over
MAX_WORKERS = 8                 # concurrency for the per-player history fetches

UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
BASE = "https://fantasy.premierleague.com/api"


def get_json(path, retries=4, backoff=2.0):
    url = f"{BASE}{path}"
    last_err = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=20) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except (urllib.error.HTTPError, urllib.error.URLError, json.JSONDecodeError) as e:
            last_err = e
            time.sleep(backoff * (attempt + 1))
    print(f"WARNING: giving up on {url}: {last_err}", file=sys.stderr)
    return None


def safe_float(v, default=0.0):
    try:
        return default if v is None else float(v)
    except (ValueError, TypeError):
        return default


def availability_multiplier(status, chance_next):
    if status == "a":
        return 1.0
    if status == "d":
        return safe_float(chance_next, 50) / 100.0
    if status in ("i", "s"):
        return 0.1
    return 0.05


def read_existing_meta():
    """When was the data last refreshed? Reads data.json, or the old embedded block on the very first run."""
    if os.path.exists(DATA_JSON):
        try:
            return json.load(open(DATA_JSON, encoding="utf-8"))["meta"]
        except (json.JSONDecodeError, KeyError, OSError):
            pass
    if os.path.exists(INDEX_HTML):
        html = open(INDEX_HTML, encoding="utf-8").read()
        m = re.search(r'<script id="fpl-data" type="application/json">(.*?)</script>', html, re.S)
        if m and m.group(1).strip():
            try:
                return json.loads(m.group(1))["meta"]
            except (json.JSONDecodeError, KeyError):
                pass
    return None


def write_json_atomic(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, separators=(",", ":"))
    os.replace(tmp, path)  # never leaves a half-written file behind


# =====================================================================
# Prediction model (v2)
#
# Instead of "he scored a lot, so he'll score a lot", each player's next
# fixtures are broken into the things FPL actually pays for:
#   minutes -> appearance points
#   xG / xA per 90 -> goals and assists, scaled by this opponent and venue
#   team defence vs opponent attack -> clean-sheet chance, goals conceded
#   bonus, goalkeeper saves, defensive contributions
# Per-player rates are shrunk toward a prior (position average, scaled by
# price) until the player has enough minutes, so one big game can't make a
# fringe player look like a star.
# =====================================================================
LEAGUE_GOALS_PER_TEAM = 1.4          # average goals per team per PL match
SHRINK_90S = 5.0                     # how many "full matches" of prior each rate starts with
TEAM_SHRINK_MATCHES = 5.0            # same idea for team attack/defence ratings
VENUE = {True: 1.06, False: 0.94}    # home / away nudge on expected goals

GOAL_PTS = {"GKP": 10, "DEF": 6, "MID": 5, "FWD": 4}
CS_PTS = {"GKP": 4, "DEF": 4, "MID": 1, "FWD": 0}
PRIOR_XG90 = {"GKP": 0.0, "DEF": 0.05, "MID": 0.14, "FWD": 0.34}
PRIOR_XA90 = {"GKP": 0.01, "DEF": 0.07, "MID": 0.13, "FWD": 0.11}
PRIOR_BONUS90 = {"GKP": 0.25, "DEF": 0.25, "MID": 0.30, "FWD": 0.35}
PRIOR_DC90 = {"GKP": 0.0, "DEF": 9.0, "MID": 8.0, "FWD": 4.0}
DC_THRESHOLD = {"DEF": 10, "MID": 12, "FWD": 12}
PRIOR_SAVES90 = 2.6


def poisson_cdf(k, lam):
    """P(X <= k) for a Poisson(lam)."""
    if lam <= 0:
        return 1.0
    term = total = math.exp(-lam)
    for i in range(1, k + 1):
        term *= lam / i
        total += term
    return min(1.0, total)


def expected_half_goals_conceded(lam):
    """E[floor(G/2)] for G ~ Poisson(lam): FPL takes 1 point per 2 goals conceded."""
    total, term = 0.0, math.exp(-lam)
    for g in range(0, 15):
        if g > 0:
            term *= lam / g
        total += (g // 2) * term
    return total


def shrunk_rate(total, n90, prior):
    return (total + prior * SHRINK_90S) / (n90 + SHRINK_90S)


def fixture_availability(status, chance_next, k):
    """Availability for the k-th upcoming fixture (0 = next). Injured players are assumed to drift back."""
    if status == "a":
        return 1.0
    if status in ("u", "n"):          # left the club / on loan / not available
        return 0.0
    base = safe_float(chance_next, 0 if status in ("i", "s") else 50) / 100.0
    if status == "s":                  # suspensions are short and fixed
        return base if k == 0 else 1.0
    return min(1.0, base + 0.25 * k)


def build_team_ratings(bootstrap, fixtures):
    """Attack and 'leakiness' multipliers per team (1.0 = league average), home and away."""
    teams = bootstrap["teams"]
    elements = bootstrap["elements"]
    played = {t["id"]: 0 for t in teams}
    for f in fixtures:
        if f.get("finished"):
            played[f["team_h"]] = played.get(f["team_h"], 0) + 1
            played[f["team_a"]] = played.get(f["team_a"], 0) + 1

    def mean(key):
        vals = [safe_float(t.get(key)) for t in teams if safe_float(t.get(key)) > 0]
        return statistics.mean(vals) if vals else 1.0

    m_att = {True: mean("strength_attack_home"), False: mean("strength_attack_away")}
    m_def = {True: mean("strength_defence_home"), False: mean("strength_defence_away")}

    xg_for = {t["id"]: 0.0 for t in teams}
    for e in elements:
        xg_for[e["team"]] = xg_for.get(e["team"], 0.0) + safe_float(e.get("expected_goals"))
    # Team xG against: the goalkeeper who has played the most, per 90
    xga_rate = {}
    for t in teams:
        gks = [e for e in elements if e["team"] == t["id"] and e["element_type"] == 1 and safe_float(e.get("minutes")) > 0]
        if gks:
            gk = max(gks, key=lambda e: safe_float(e.get("minutes")))
            xga_rate[t["id"]] = safe_float(gk.get("expected_goals_conceded")) / (safe_float(gk["minutes"]) / 90.0)

    ratings = {}
    for t in teams:
        tid, g = t["id"], played.get(t["id"], 0)
        w = g / (g + TEAM_SHRINK_MATCHES)
        obs_att = (xg_for[tid] / g / LEAGUE_GOALS_PER_TEAM) if g else 1.0
        obs_def = (xga_rate.get(tid, LEAGUE_GOALS_PER_TEAM) / LEAGUE_GOALS_PER_TEAM) if g else 1.0
        r = {}
        for home in (True, False):
            # FPL sometimes leaves a strength at 0 (e.g. promoted clubs early on): treat that as league average
            sa = safe_float(t.get("strength_attack_home" if home else "strength_attack_away")) or m_att[home]
            sd = safe_float(t.get("strength_defence_home" if home else "strength_defence_away")) or m_def[home]
            # FPL strength ratings sit in a narrow band, so stretch them to realistic goal ranges
            prior_att = (sa / m_att[home]) ** 2.5
            prior_def = (m_def[home] / sd) ** 2.5
            r[("att", home)] = (prior_att ** (1 - w)) * (max(obs_att, 0.2) ** w)
            r[("def", home)] = (prior_def ** (1 - w)) * (max(obs_def, 0.2) ** w)
        r["played"] = g
        ratings[tid] = r
    return ratings


def expected_minutes(e, pos, team_played, history_rows):
    """Expected minutes per match and the chances of playing 60+ or a short cameo."""
    mins = safe_float(e.get("minutes"))
    starts = safe_float(e.get("starts"))
    if team_played > 0:
        season_mpm = min(90.0, mins / team_played)
        season_start = min(1.0, starts / team_played)
    else:
        season_mpm, season_start = 60.0, 0.6
    recent = sorted(history_rows or [], key=lambda h: h["event"])[-4:]
    if recent:
        rec_mpm = statistics.mean(min(90, h["minutes"]) for h in recent)
        rec_start = statistics.mean(1.0 if h["minutes"] >= 60 else 0.0 for h in recent)
        exp_mins = 0.65 * rec_mpm + 0.35 * season_mpm
        start_rate = 0.65 * rec_start + 0.35 * season_start
    else:
        exp_mins, start_rate = season_mpm, season_start
    if mins == 0 and team_played > 0 and e.get("status") == "a":
        exp_mins, start_rate = 20.0, 0.15   # new signing or fringe player: give a small, honest chance
    p60 = max(0.0, min(1.0, start_rate))
    p_cameo = max(0.0, min(1.0 - p60, (exp_mins - 80.0 * p60) / 25.0))
    return exp_mins, p60, p_cameo


def build_predictions(bootstrap, fixtures, horizon_ids, next_gw, player_history):
    """Returns {player_id: {"pred1", "pred5", "why"}} using the component model above."""
    teams_by_id = {t["id"]: t for t in bootstrap["teams"]}
    pos_map = {p["id"]: p["singular_name_short"] for p in bootstrap["element_types"]}
    elements = [e for e in bootstrap["elements"] if not e.get("removed")]
    ratings = build_team_ratings(bootstrap, fixtures)
    has_dc = any("defensive_contribution" in e for e in elements)

    # Price-scaled priors: FPL prices encode a lot about expected quality
    avg_cost = {}
    for pos in ("GKP", "DEF", "MID", "FWD"):
        costs = [e["now_cost"] / 10.0 for e in elements if pos_map.get(e["element_type"]) == pos and safe_float(e.get("minutes")) > 0]
        avg_cost[pos] = statistics.mean(costs) if costs else 5.0

    upcoming = {}  # team -> list of (event, opp, home)
    for f in fixtures:
        if f.get("event") in horizon_ids:
            upcoming.setdefault(f["team_h"], []).append((f["event"], f["team_a"], True))
            upcoming.setdefault(f["team_a"], []).append((f["event"], f["team_h"], False))

    out = {}
    for e in elements:
        pos = pos_map.get(e["element_type"], "MID")
        tid = e["team"]
        tr = ratings.get(tid, {("att", True): 1, ("att", False): 1, ("def", True): 1, ("def", False): 1, "played": 0})
        n90 = safe_float(e.get("minutes")) / 90.0
        price_k = (e["now_cost"] / 10.0) / avg_cost.get(pos, 5.0)
        xg90 = shrunk_rate(safe_float(e.get("expected_goals")), n90, PRIOR_XG90[pos] * price_k ** 1.6)
        xa90 = shrunk_rate(safe_float(e.get("expected_assists")), n90, PRIOR_XA90[pos] * price_k ** 1.6)
        bonus90 = shrunk_rate(safe_float(e.get("bonus")), n90, PRIOR_BONUS90[pos] * price_k)
        saves90 = shrunk_rate(safe_float(e.get("saves")), n90, PRIOR_SAVES90) if pos == "GKP" else 0.0
        dc90 = shrunk_rate(safe_float(e.get("defensive_contribution")), n90, PRIOR_DC90[pos]) if (has_dc and pos in DC_THRESHOLD) else 0.0

        exp_mins, p60, p_cameo = expected_minutes(e, pos, tr["played"], player_history.get(str(e["id"])))
        frac = exp_mins / 90.0
        team_avg_att = (tr[("att", True)] + tr[("att", False)]) / 2.0

        pred1 = pred5 = 0.0
        why = None
        fx = sorted(upcoming.get(tid, []), key=lambda x: x[0])
        for k, (ev, opp, home) in enumerate(fx):
            orr = ratings.get(opp, tr)
            lam_for = LEAGUE_GOALS_PER_TEAM * tr[("att", home)] * orr[("def", not home)] * VENUE[home]
            lam_against = LEAGUE_GOALS_PER_TEAM * orr[("att", not home)] * tr[("def", home)] * VENUE[not home]
            scale = lam_for / (LEAGUE_GOALS_PER_TEAM * team_avg_att) if team_avg_att > 0 else 1.0

            xg_m, xa_m = xg90 * frac * scale, xa90 * frac * scale
            cs_prob = math.exp(-lam_against)
            pts = 2.0 * p60 + 1.0 * p_cameo
            pts += GOAL_PTS[pos] * xg_m + 3.0 * xa_m
            pts += CS_PTS[pos] * cs_prob * p60
            if pos in ("GKP", "DEF"):
                pts -= expected_half_goals_conceded(lam_against) * p60
            if pos == "GKP":
                pts += saves90 * frac * (lam_against / LEAGUE_GOALS_PER_TEAM) / 3.0 * 0.85
            pts += bonus90 * frac
            if dc90 > 0:
                mean_dc = dc90 * frac
                pts += 2.0 * (1.0 - poisson_cdf(DC_THRESHOLD[pos] - 1, mean_dc))

            pts *= fixture_availability(e["status"], e.get("chance_of_playing_next_round"), k)
            pred5 += pts
            if ev == next_gw:
                pred1 += pts
                if why is None:
                    avail0 = fixture_availability(e["status"], e.get("chance_of_playing_next_round"), 0)
                    why = {"mins": round(exp_mins * avail0), "goal": round(100 * (1 - math.exp(-xg_m)) * avail0),
                           "assist": round(100 * (1 - math.exp(-xa_m)) * avail0),
                           "cs": round(100 * cs_prob) if pos != "FWD" else None}
        out[e["id"]] = {"pred1": round(pred1, 1), "pred5": round(pred5, 1), "why": why}
    return out


def fallback_predictions(bootstrap, team_fixtures, next_gw):
    """The old form + points-per-game formula. Only used if the main model hits unexpected data."""
    out = {}
    for e in bootstrap["elements"]:
        base = 0.5 * safe_float(e.get("form")) + 0.5 * safe_float(e.get("points_per_game"))
        fx = team_fixtures.get(e["team"], [])
        avail = [fixture_availability(e["status"], e.get("chance_of_playing_next_round"), k) for k in range(len(fx))]
        per = [base * max(0.55, min(1.2, 1.35 - 0.14 * x["fdr"])) * avail[k] for k, x in enumerate(sorted(fx, key=lambda x: x["event"]))]
        p1 = sum(v for v, x in zip(per, sorted(fx, key=lambda x: x["event"])) if x["event"] == next_gw)
        out[e["id"]] = {"pred1": round(p1, 1), "pred5": round(sum(per), 1), "why": None}
    return out


# ---------- tracking how good the model actually is ----------
def load_model_log():
    if os.path.exists(MODEL_LOG):
        try:
            return json.load(open(MODEL_LOG, encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            pass
    return {"version": MODEL_VERSION, "snapshots": {}, "results": {}}


def update_model_log(log, events, next_gw, players_out):
    """Keep the latest pre-deadline prediction for the upcoming gameweek, and score any finished ones."""
    log.setdefault("snapshots", {})
    log.setdefault("results", {})
    log["snapshots"][str(next_gw)] = {
        "savedAt": datetime.now(timezone.utc).isoformat(), "modelVersion": MODEL_VERSION,
        "preds": {str(p["id"]): [p["pred1"], p["epNext"]] for p in players_out if p["pred1"] > 0 or p["epNext"] > 0},
    }
    finished = {e["id"] for e in events if e.get("finished")}
    for gw_s, snap in list(log["snapshots"].items()):
        gw = int(gw_s)
        if gw_s in log["results"] or gw not in finished:
            continue
        live = get_json(f"/event/{gw}/live/")
        if not live:
            continue
        actual = {el["id"]: el["stats"]["total_points"] for el in live.get("elements", [])}
        rows = [(int(pid), v[0], v[1], actual.get(int(pid), 0)) for pid, v in snap["preds"].items()]
        relevant = [r for r in rows if r[1] >= 2 or r[2] >= 2 or r[3] > 0]
        if not relevant:
            continue
        top_model = sorted(rows, key=lambda r: -r[1])[:10]
        top_fpl = sorted(rows, key=lambda r: -r[2])[:10]
        log["results"][gw_s] = {
            "gw": gw, "players": len(relevant), "modelVersion": snap.get("modelVersion", 1),
            "maeModel": round(statistics.mean(abs(r[1] - r[3]) for r in relevant), 2),
            "maeFpl": round(statistics.mean(abs(r[2] - r[3]) for r in relevant), 2),
            "top10Model": round(statistics.mean(r[3] for r in top_model), 1),
            "top10Fpl": round(statistics.mean(r[3] for r in top_fpl), 1),
            "captainModel": {"id": top_model[0][0], "points": top_model[0][3]},
            "captainFpl": {"id": top_fpl[0][0], "points": top_fpl[0][3]},
        }
        print(f"Scored GW{gw}: model MAE {log['results'][gw_s]['maeModel']} vs FPL {log['results'][gw_s]['maeFpl']}")
    # keep the log small: only the last 10 snapshots
    for gw_s in sorted(log["snapshots"], key=int)[:-10]:
        if gw_s in log["results"]:
            del log["snapshots"][gw_s]
    return log


def main():
    print("Fetching bootstrap-static, fixtures, league standings...")
    bootstrap = get_json("/bootstrap-static/")
    fixtures = get_json("/fixtures/")
    league = get_json(f"/leagues-classic/{LEAGUE_ID}/standings/")
    if not (bootstrap and fixtures and league):
        print("ERROR: core endpoints failed, aborting without touching index.html", file=sys.stderr)
        sys.exit(1)

    manager_ids = [r["entry"] for r in league["standings"]["results"]]
    teams_by_id = {t["id"]: t for t in bootstrap["teams"]}
    events = bootstrap["events"]
    now = datetime.now(timezone.utc)
    # The "current" gameweek is the one whose deadline has most recently passed —
    # i.e. squads are locked in and it's being (or about to be) played. We key
    # this off each event's own deadline_time (fixed schedule data) rather than
    # FPL's is_current flag: that flag can lag behind the real deadline by hours
    # or more (it appears to wait on the *previous* gameweek being fully wrapped
    # up), which would otherwise leave this whole app pointed at last week's
    # gameweek — wrong squad, wrong picks, wrong predictions — even while the
    # live one is already locked in and being played.
    past_deadline_events = [e for e in events if e.get("deadline_time")
                             and datetime.fromisoformat(e["deadline_time"].replace("Z", "+00:00")) <= now]
    current_ev = past_deadline_events[-1] if past_deadline_events else events[0]
    current_gw = current_ev["id"]
    next_candidates = [e for e in events if e["id"] > current_gw]
    next_ev = next_candidates[0] if next_candidates else current_ev
    horizon_ids = [next_ev["id"] + i for i in range(FIXTURE_HORIZON)]

    # ---------- decide whether this run is even worth doing ----------
    # "Live window": current_gw's matchday span, from its first kickoff to a few
    # hours after its last kickoff (covers the whole matchday, not just the exact
    # minutes a ball is in play). current_gw itself is now deadline-based (see
    # above), so this naturally tracks the real current gameweek even when FPL's
    # own flags haven't caught up yet.
    kickoffs = [datetime.fromisoformat(f["kickoff_time"].replace("Z", "+00:00"))
                for f in fixtures if f.get("event") == current_gw and f.get("kickoff_time")]
    live_window = False
    if kickoffs:
        live_window = min(kickoffs) <= now <= max(kickoffs) + timedelta(hours=LIVE_WINDOW_BUFFER_HOURS)

    prev_meta = read_existing_meta()
    minutes_since_last = None
    if prev_meta and prev_meta.get("generatedAt"):
        try:
            prev_dt = datetime.fromisoformat(prev_meta["generatedAt"])
            minutes_since_last = (now - prev_dt).total_seconds() / 60
        except ValueError:
            pass

    forced = os.environ.get("FORCE_REFRESH") == "1"
    if live_window:
        refresh_interval_for_ui = 15  # matches how often the workflow's cron fires
    elif forced:
        refresh_interval_for_ui = BASELINE_REFRESH_MINUTES
        print("Manual run: forcing a full refresh.")
    elif minutes_since_last is None or minutes_since_last >= BASELINE_REFRESH_MINUTES:
        refresh_interval_for_ui = BASELINE_REFRESH_MINUTES
    else:
        mins_left = BASELINE_REFRESH_MINUTES - minutes_since_last
        print(f"Not live, and only {minutes_since_last:.0f} min since last refresh "
              f"(next baseline refresh in ~{mins_left:.0f} min). Skipping the heavy fetch.")
        sys.exit(0)

    print(f"Proceeding with a full refresh (live_window={live_window}).")

    # ---------- teams / fixture runs ----------
    team_fixtures = {tid: [] for tid in teams_by_id}
    for f in fixtures:
        ev = f.get("event")
        if ev not in horizon_ids:
            continue
        th, ta = f["team_h"], f["team_a"]
        team_fixtures[th].append({"event": ev, "opp": teams_by_id[ta]["short_name"], "home": True, "fdr": f["team_h_difficulty"]})
        team_fixtures[ta].append({"event": ev, "opp": teams_by_id[th]["short_name"], "home": False, "fdr": f["team_a_difficulty"]})

    teams_out = {}
    for tid, t in teams_by_id.items():
        fx = sorted(team_fixtures[tid], key=lambda x: x["event"])
        avg_fdr = round(statistics.mean([x["fdr"] for x in fx]), 2) if fx else 5.0
        run_score = round(sum(6 - x["fdr"] for x in fx), 1)
        teams_out[str(tid)] = {"name": t["name"], "short": t["short_name"], "code": t.get("code"), "next5": fx,
                                "fixtureCount5": len(fx), "avgFdr5": avg_fdr, "runScore5": run_score}

    # ---------- phases (merge August + September into one "Aug / Sep" block) ----------
    raw_phases = [p for p in bootstrap["phases"] if p["name"] != "Overall"]
    phases_out, skip_next = [], False
    for i, p in enumerate(raw_phases):
        if skip_next:
            skip_next = False
            continue
        if p["name"] == "August":
            sep = raw_phases[i + 1]
            phases_out.append({"key": "augsep", "label": "Aug / Sep", "start": p["start_event"], "stop": sep["stop_event"]})
            skip_next = True
        else:
            phases_out.append({"key": p["name"].lower()[:3], "label": p["name"], "start": p["start_event"], "stop": p["stop_event"]})
    for ph in phases_out:
        if ph["start"] <= current_gw <= ph["stop"]:
            ph["status"] = "current"
        elif current_gw > ph["stop"]:
            ph["status"] = "completed"
        else:
            ph["status"] = "upcoming"

    # ---------- chip windows ----------
    name_counts, chip_windows = {}, []
    label_map = {"wildcard": "Wildcard", "freehit": "Free Hit", "bboost": "Bench Boost", "3xc": "Triple Captain"}
    for c in bootstrap["chips"]:
        name_counts[c["name"]] = name_counts.get(c["name"], 0) + 1
        idx = name_counts[c["name"]]
        chip_windows.append({"key": f"{c['name']}{idx}", "name": c["name"], "label": f"{label_map.get(c['name'], c['name'])} #{idx}",
                              "start": c["start_event"], "stop": c["stop_event"]})

    upcoming_deadlines = [{"event": e["id"], "deadline": e["deadline_time"]} for e in events if e["id"] >= next_ev["id"]][:4]

    # ---------- per-manager data ----------
    print(f"Fetching data for {len(manager_ids)} managers...")
    managers_out = []
    for mid in manager_ids:
        picks_d = get_json(f"/entry/{mid}/event/{current_gw}/picks/")
        hist_d = get_json(f"/entry/{mid}/history/")
        transfers_d = get_json(f"/entry/{mid}/transfers/")
        entry_d = get_json(f"/entry/{mid}/")
        st = next(r for r in league["standings"]["results"] if r["entry"] == mid)
        if not (picks_d and hist_d):
            print(f"WARNING: skipping manager {mid}, core data unavailable", file=sys.stderr)
            continue

        fav_team_id = entry_d.get("favourite_team") if entry_d else None
        favourite_team = teams_by_id.get(fav_team_id, {}).get("short_name") if fav_team_id else None

        squad = [{"id": p["element"], "slot": p["position"], "starter": p["multiplier"] > 0,
                  "captain": p["is_captain"], "vice": p["is_vice_captain"]} for p in picks_d["picks"]]
        gw_points_map = {c["event"]: c["points"] for c in hist_d["current"]}
        gw_history = [{"event": c["event"], "points": c["points"], "overallRank": c["overall_rank"], "value": c["value"]/10.0,
                       "bench": c["points_on_bench"], "total": c.get("total_points")} for c in hist_d["current"]]

        monthly = []
        for ph in phases_out:
            pts = sum(gw_points_map.get(ev, 0) for ev in range(ph["start"], min(ph["stop"], current_gw) + 1))
            gws_played = sum(1 for ev in range(ph["start"], min(ph["stop"], current_gw) + 1) if ev in gw_points_map)
            monthly.append({"key": ph["key"], "points": pts, "gwsPlayed": gws_played})

        chip_status = []
        for cw in chip_windows:
            used_event = next((c["event"] for c in hist_d["chips"] if c["name"] == cw["name"] and cw["start"] <= c["event"] <= cw["stop"]), None)
            status = "used" if used_event else ("expired" if current_gw > cw["stop"] else "available")
            chip_status.append({"key": cw["key"], "name": cw["name"], "label": cw["label"], "start": cw["start"],
                                 "stop": cw["stop"], "status": status, "usedEvent": used_event})

        if isinstance(transfers_d, list):
            tlist = sorted(transfers_d, key=lambda x: x["time"])
            transfers = [{"event": t["event"], "inId": t["element_in"], "outId": t["element_out"],
                          "inCost": t["element_in_cost"]/10.0, "outCost": t["element_out_cost"]/10.0, "time": t["time"]} for t in tlist]
        else:
            transfers = None

        eh = picks_d["entry_history"]
        managers_out.append({
            "entryId": mid, "managerName": st["player_name"], "teamName": st["entry_name"], "rank": st["rank"],
            "lastRank": st["last_rank"], "total": st["total"], "eventTotal": st["event_total"],
            "bank": eh["bank"]/10.0, "teamValue": eh["value"]/10.0, "activeChip": picks_d.get("active_chip"),
            "favouriteTeam": favourite_team,
            "chipsUsed": hist_d["chips"], "gwHistory": gw_history, "squad": squad,
            "monthlyPoints": monthly, "chipStatus": chip_status, "transfers": transfers,
        })
    managers_out.sort(key=lambda m: m["rank"])

    # ---------- per-player history (owned players + top N globally) ----------
    owned_ids = {s["id"] for m in managers_out for s in m["squad"]}
    top_ids = {e["id"] for e in sorted(bootstrap["elements"], key=lambda x: -x["total_points"])[:TOP_N_GLOBAL_PLAYERS]}
    fetch_ids = owned_ids | top_ids
    print(f"Fetching per-gameweek history for {len(fetch_ids)} players...")

    player_history = {}
    def fetch_one(pid):
        d = get_json(f"/element-summary/{pid}/")
        if not d:
            return pid, []
        rows = []
        for h in d.get("history", []):
            opp = teams_by_id.get(h["opponent_team"], {}).get("short_name", "?")
            rows.append({"event": h["round"], "points": h["total_points"], "minutes": h["minutes"], "goals": h["goals_scored"],
                         "assists": h["assists"], "bonus": h["bonus"], "bps": h["bps"], "ict": safe_float(h["ict_index"]),
                         "value": h["value"]/10.0, "opponent": opp, "home": h["was_home"]})
        return pid, rows

    with ThreadPoolExecutor(max_workers=MAX_WORKERS) as pool:
        futures = [pool.submit(fetch_one, pid) for pid in fetch_ids]
        for fut in as_completed(futures):
            pid, rows = fut.result()
            if rows:
                player_history[str(pid)] = rows

    # ---------- players + predictions (model v2) ----------
    print("Running prediction model...")
    pos_map = {p["id"]: p["singular_name_short"] for p in bootstrap["element_types"]}
    try:
        preds = build_predictions(bootstrap, fixtures, horizon_ids, next_ev["id"], player_history)
    except Exception as ex:
        import traceback
        traceback.print_exc()
        print(f"WARNING: prediction model failed ({ex}); using the simple fallback formula this run.", file=sys.stderr)
        preds = fallback_predictions(bootstrap, team_fixtures, next_ev["id"])
    players_out = []
    for e in bootstrap["elements"]:
        if e.get("removed"):
            continue
        team_id = e["team"]
        pr = preds.get(e["id"], {"pred1": 0.0, "pred5": 0.0, "why": None})
        cost = e["now_cost"] / 10.0
        players_out.append({
            "id": e["id"], "code": e.get("code"), "web": e["web_name"], "first": e["first_name"], "second": e["second_name"],
            "team": team_id, "teamShort": teams_by_id[team_id]["short_name"], "pos": pos_map.get(e["element_type"], "?"),
            "cost": cost, "status": e["status"], "news": e.get("news", ""), "chanceNext": e.get("chance_of_playing_next_round"),
            "form": safe_float(e["form"]), "ppg": safe_float(e["points_per_game"]), "totalPoints": e["total_points"],
            "selPct": safe_float(e["selected_by_percent"]), "epNext": safe_float(e.get("ep_next")),
            "xg": safe_float(e.get("expected_goals")), "xa": safe_float(e.get("expected_assists")),
            "xgi": safe_float(e.get("expected_goal_involvements")),
            "pred1": pr["pred1"], "pred5": pr["pred5"], "value5": round(pr["pred5"] / cost, 2) if cost > 0 else 0.0,
            "why": pr["why"],
            "influence": safe_float(e.get("influence")), "creativity": safe_float(e.get("creativity")),
            "threat": safe_float(e.get("threat")), "ictIndex": safe_float(e.get("ict_index")),
            "fixturesNext5": sorted(team_fixtures.get(team_id, []), key=lambda x: x["event"]),
        })

    # ---------- model accuracy log ----------
    model_log = update_model_log(load_model_log(), events, next_ev["id"], players_out)
    model_check = [model_log["results"][k] for k in sorted(model_log["results"], key=int)][-6:]

    # ---------- assemble ----------
    meta = {
        "leagueId": LEAGUE_ID, "leagueName": league["league"]["name"],
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "refreshIntervalMinutes": refresh_interval_for_ui,
        "currentGw": current_gw, "nextGw": next_ev["id"], "nextDeadline": next_ev.get("deadline_time"),
        "gwFinished": current_ev["finished"], "gwLive": live_window,
        "currentGwAvg": current_ev["average_entry_score"], "currentGwHighest": current_ev["highest_score"],
        "totalFplManagers": bootstrap["total_players"], "mostSelectedId": current_ev.get("most_selected"),
        "mostCaptainedId": current_ev.get("most_captained"), "mostTransferredInId": current_ev.get("most_transferred_in"),
        "horizonGws": horizon_ids, "modelVersion": MODEL_VERSION, "modelCheck": model_check, "phases": phases_out, "chipWindows": chip_windows, "upcomingDeadlines": upcoming_deadlines,
    }
    payload = {"meta": meta, "teams": teams_out, "players": players_out, "managers": managers_out, "playerHistory": player_history}
    write_json_atomic(DATA_JSON, payload)
    write_json_atomic(MODEL_LOG, model_log)

    print(f"Done. {len(players_out)} players, {len(managers_out)} managers, GW{current_gw} -> GW{next_ev['id']}.")


if __name__ == "__main__":
    main()
