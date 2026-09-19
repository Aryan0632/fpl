#!/usr/bin/env python3
"""
Refreshes the embedded data in index.html from the live FPL API.

Run this from the repo root: python refresh_data.py
It edits index.html in place, replacing only the contents of the
<script id="fpl-data" type="application/json"> block.

Designed to run unattended (e.g. from a GitHub Actions cron job), so it
retries transient errors and never half-writes the file.
"""
import json, os, re, statistics, sys, time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone, timedelta
import urllib.request
import urllib.error

LEAGUE_ID = 209817
INDEX_HTML = "index.html"
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
    """Peek at the currently-committed index.html to see when it was last refreshed, without touching it."""
    if not os.path.exists(INDEX_HTML):
        return None
    html = open(INDEX_HTML, encoding="utf-8").read()
    m = re.search(r'<script id="fpl-data" type="application/json">(.*?)</script>', html, re.S)
    if not m:
        return None
    try:
        return json.loads(m.group(1))["meta"]
    except (json.JSONDecodeError, KeyError):
        return None


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
    current_candidates = [e for e in events if e["is_current"]]
    if current_candidates:
        current_ev = current_candidates[0]
    else:
        # Edge case: briefly, no event is flagged "current" (e.g. right around a
        # deadline before FPL's own flags catch up). Fall back to the most
        # recently finished gameweek, or the season's first if none has run yet.
        finished = [e for e in events if e["finished"]]
        current_ev = finished[-1] if finished else events[0]
    current_gw = current_ev["id"]
    next_candidates = [e for e in events if e["id"] > current_gw]
    next_ev = next_candidates[0] if next_candidates else current_ev
    horizon_ids = [next_ev["id"] + i for i in range(FIXTURE_HORIZON)]

    # ---------- decide whether this run is even worth doing ----------
    # "Live window": current_gw's matchday span, from its first kickoff to a few
    # hours after its last kickoff (covers the whole matchday, not just the exact
    # minutes a ball is in play). This is keyed off current_gw (FPL's own
    # is_current flag, already resolved above) rather than "the earliest event
    # not yet flagged finished": that finished flag can lag for a while after a
    # gameweek's last match while bonus points are being confirmed, which would
    # otherwise leave this stuck pointing at a gameweek that's already over
    # instead of the one that's actually being played right now.
    now = datetime.now(timezone.utc)
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

    if live_window:
        refresh_interval_for_ui = 15  # matches how often the workflow's cron fires
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
        teams_out[str(tid)] = {"name": t["name"], "short": t["short_name"], "next5": fx,
                                "fixtureCount5": len(fx), "avgFdr5": avg_fdr, "runScore5": run_score}

    # ---------- players ----------
    pos_map = {p["id"]: p["singular_name_short"] for p in bootstrap["element_types"]}
    players_out = []
    for e in bootstrap["elements"]:
        if e.get("removed"):
            continue
        team_id = e["team"]
        fx = team_fixtures.get(team_id, [])
        ppg, form = safe_float(e["points_per_game"]), safe_float(e["form"])
        base_rate = 0.5 * form + 0.5 * ppg
        avail = availability_multiplier(e["status"], e.get("chance_of_playing_next_round"))

        next1_fixtures = [x for x in fx if x["event"] == next_ev["id"]]
        pred1_raw = sum(base_rate * max(0.55, min(1.2, 1.35 - 0.14 * x["fdr"])) for x in next1_fixtures)
        ep_next = safe_float(e.get("ep_next"))
        if e["status"] == "a" and ep_next > 0 and next1_fixtures:
            pred1_raw = 0.5 * pred1_raw + 0.5 * ep_next
        pred1 = round(pred1_raw * avail, 1)

        pred5_raw = sum(base_rate * max(0.55, min(1.2, 1.35 - 0.14 * x["fdr"])) for x in fx)
        pred5 = round(pred5_raw * avail, 1)

        cost = e["now_cost"] / 10.0
        value5 = round(pred5 / cost, 2) if cost > 0 else 0.0

        players_out.append({
            "id": e["id"], "web": e["web_name"], "first": e["first_name"], "second": e["second_name"],
            "team": team_id, "teamShort": teams_by_id[team_id]["short_name"], "pos": pos_map.get(e["element_type"], "?"),
            "cost": cost, "status": e["status"], "news": e.get("news", ""), "chanceNext": e.get("chance_of_playing_next_round"),
            "form": form, "ppg": ppg, "totalPoints": e["total_points"], "selPct": safe_float(e["selected_by_percent"]),
            "epNext": ep_next, "xg": safe_float(e.get("expected_goals")), "xa": safe_float(e.get("expected_assists")),
            "xgi": safe_float(e.get("expected_goal_involvements")), "pred1": pred1, "pred5": pred5, "value5": value5,
            "influence": safe_float(e.get("influence")), "creativity": safe_float(e.get("creativity")),
            "threat": safe_float(e.get("threat")), "ictIndex": safe_float(e.get("ict_index")),
            "fixturesNext5": fx,
        })
    players_by_id = {p["id"]: p for p in players_out}

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
        ph["status"] = "completed" if ph["stop"] <= current_gw else ("current" if ph["start"] <= current_gw <= ph["stop"] else "upcoming")

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
                       "bench": c["points_on_bench"]} for c in hist_d["current"]]

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
    top_ids = {p["id"] for p in sorted(players_out, key=lambda x: -x["totalPoints"])[:TOP_N_GLOBAL_PLAYERS]}
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
        "horizonGws": horizon_ids, "phases": phases_out, "chipWindows": chip_windows, "upcomingDeadlines": upcoming_deadlines,
    }
    payload = {"meta": meta, "teams": teams_out, "players": players_out, "managers": managers_out, "playerHistory": player_history}
    new_json = json.dumps(payload, separators=(",", ":"))

    # ---------- patch it into index.html ----------
    if not os.path.exists(INDEX_HTML):
        print(f"ERROR: {INDEX_HTML} not found next to this script", file=sys.stderr)
        sys.exit(1)
    html = open(INDEX_HTML, encoding="utf-8").read()
    pattern = re.compile(r'(<script id="fpl-data" type="application/json">)(.*?)(</script>)', re.S)
    if not pattern.search(html):
        print("ERROR: could not find the fpl-data script block in index.html — aborting", file=sys.stderr)
        sys.exit(1)
    patched = pattern.sub(lambda m: m.group(1) + new_json + m.group(3), html, count=1)

    tmp_path = INDEX_HTML + ".tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        f.write(patched)
    os.replace(tmp_path, INDEX_HTML)  # atomic-ish: never leaves a half-written index.html

    print(f"Done. {len(players_out)} players, {len(managers_out)} managers, GW{current_gw} -> GW{next_ev['id']}.")


if __name__ == "__main__":
    main()
