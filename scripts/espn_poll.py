#!/usr/bin/env python3
"""Poll ESPN's public scoreboard and auto-publish finished World Cup 2026 results.

Designed for a 5-minute cron during match-finish windows. Flow per run:
  1. Find fixtures in 2026/worldcup.json with no score whose kickoff was
     100+ minutes ago (and < 24 h, older gaps are left to the AI backstop).
  2. Look the match up in ESPN's scoreboard feed; only act on an explicit
     full-time/final status — in-progress games are left alone.
  3. Apply the result through scripts/update.py (never touches JSON directly),
     commit, push to master, and ping ntfy.

Skips knockout placeholder fixtures (team names like "1A", "W73") — those
need real team names in the JSON before any score can be recorded.

Usage:
  espn_poll.py                  normal cron mode (applies, commits, pushes)
  espn_poll.py --dry-run        print update.py commands, change nothing
  espn_poll.py --dry-run --date 2026-06-11   re-derive already-scored matches
"""
import argparse
import json
import re
import subprocess
import sys
import unicodedata
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
DATA = REPO / "2026" / "worldcup.json"
UPDATE = REPO / "scripts" / "update.py"
SCOREBOARD = ("https://site.api.espn.com/apis/site/v2/sports/soccer/"
              "fifa.world/scoreboard?dates={date}")
NTFY = "https://ntfy.sh/worldcup-tipping-upbound-a95e4613391d54fb"
FINAL_STATUSES = {"STATUS_FULL_TIME", "STATUS_FINAL", "STATUS_FINAL_AET",
                  "STATUS_FINAL_PEN"}
MIN_AGE_MIN = 100          # don't query ESPN before a game could be over
MAX_AGE_H = 24             # leave older gaps to the AI backstop / manual

# ESPN displayName -> fixture name, applied after normalisation
ALIASES = {
    "czechia": "czech republic",
    "united states": "usa",
    "turkiye": "turkey",
    "cote divoire": "ivory coast",
    "cabo verde": "cape verde",
    "korea republic": "south korea",
    "ir iran": "iran",
    "democratic republic of the congo": "dr congo",
    "congo dr": "dr congo",
    "bosnia herzegovina": "bosnia and herzegovina",
}


def norm(name):
    s = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    s = s.lower().replace("&", "and").replace("-", " ")
    s = re.sub(r"[^a-z ]", "", s).strip()
    s = re.sub(r"\s+", " ", s)
    return ALIASES.get(s, s)


def is_placeholder(name):
    return bool(re.fullmatch(r"([1-3][A-L](/[A-L])*|[WL]\d+|3[A-L/]+)", name))


def kickoff_utc(m):
    tm = re.fullmatch(r"(\d{1,2}):(\d{2}) UTC([+-]\d+)", m["time"])
    if not tm:
        return None
    local = datetime.fromisoformat(m["date"]).replace(
        hour=int(tm.group(1)), minute=int(tm.group(2)), tzinfo=timezone.utc)
    return local - timedelta(hours=int(tm.group(3)))


def fetch_events(dates):
    events = {}
    for d in dates:
        url = SCOREBOARD.format(date=d.strftime("%Y%m%d"))
        with urllib.request.urlopen(url, timeout=30) as r:
            for e in json.load(r).get("events", []):
                events[e["id"]] = e
    return list(events.values())


def goal_args(details, side_norm, team_ids):
    """Build update.py --goalN strings for one side from ESPN play details."""
    out = []
    for det in details:
        text = det.get("type", {}).get("text", "")
        if "Goal" not in text and "Penalty - Scored" not in text:
            continue
        if "Card" in text or "Missed" in text or "Shootout" in text:
            continue
        if team_ids.get(det.get("team", {}).get("id")) != side_norm:
            continue
        ath = det.get("athletesInvolved") or [{}]
        name = ath[0].get("displayName", "Unknown")
        clock = det.get("clock", {}).get("displayValue", "0'")
        cm = re.match(r"(\d+)'(?:\+(\d+))?", clock)
        minute = cm.group(1) if cm else "0"
        if cm and cm.group(2):
            minute += "+" + cm.group(2)
        suffix = ""
        if det.get("penaltyKick") or "Penalty - Scored" in text:
            suffix = " (pen)"
        elif det.get("ownGoal"):
            suffix = " (og)"
        out.append((int(cm.group(1)) if cm else 0, f"{name} {minute}{suffix}"))
    return [g for _, g in sorted(out)]


def ht_from_goals(goal_strs):
    return sum(1 for g in goal_strs if int(re.match(r"\S.*? (\d+)", g).group(1)) <= 45)


def notify(title, msg, tags="soccer", click=None):
    req = urllib.request.Request(NTFY, data=msg.encode(), method="POST")
    req.add_header("Title", title)
    req.add_header("Tags", tags)
    if click:
        req.add_header("Click", click)
    try:
        urllib.request.urlopen(req, timeout=15)
    except OSError as e:
        print(f"ntfy failed: {e}", file=sys.stderr)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--date", help="with --dry-run: process this date even if scored")
    args = ap.parse_args()

    if not args.dry_run:
        subprocess.run(["git", "pull", "--rebase", "origin", "master"],
                       check=True, cwd=REPO, capture_output=True)

    now = datetime.now(timezone.utc)
    data = json.loads(DATA.read_text(encoding="utf-8"))

    pending = []
    for m in data["matches"]:
        if args.date:
            if m["date"] == args.date:
                pending.append(m)
            continue
        if "score" in m or is_placeholder(m["team1"]) or is_placeholder(m["team2"]):
            continue
        ko = kickoff_utc(m)
        if ko and timedelta(minutes=MIN_AGE_MIN) <= now - ko <= timedelta(hours=MAX_AGE_H):
            pending.append(m)
    if not pending:
        return

    # ESPN files a match under its venue-local date, which can be the day
    # before the UTC kickoff date for after-midnight-UTC kickoffs (e.g. North
    # American evenings). Query a +/-1 day window around the UTC kickoff plus
    # the fixture's own listed date so none of those filings are missed.
    dates = set()
    for m in pending:
        k = kickoff_utc(m)
        if k:
            dates |= {k.date() - timedelta(days=1), k.date(),
                      k.date() + timedelta(days=1)}
        dates.add(datetime.fromisoformat(m["date"]).date())
    try:
        events = fetch_events(sorted(dates))
    except OSError as e:
        print(f"ESPN fetch failed: {e}", file=sys.stderr)
        return

    applied = []
    for m in pending:
        t1, t2 = norm(m["team1"]), norm(m["team2"])
        ev = next((e for e in events if
                   {norm(c["team"]["displayName"])
                    for c in e["competitions"][0]["competitors"]} == {t1, t2}), None)
        if not ev:
            continue
        status = ev["status"]["type"]["name"]
        if status not in FINAL_STATUSES:
            continue  # still playing — try again next run

        comp = ev["competitions"][0]
        comps = {norm(c["team"]["displayName"]): c for c in comp["competitors"]}
        team_ids = {c["team"]["id"]: n for n, c in comps.items()}
        c1, c2 = comps[t1], comps[t2]
        details = comp.get("details", [])
        g1 = goal_args(details, t1, team_ids)
        g2 = goal_args(details, t2, team_ids)
        ft = [int(c1["score"]), int(c2["score"])]

        cmd = [sys.executable, str(UPDATE), m["team1"], m["team2"],
               f"{ft[0]}-{ft[1]}", "--date", m["date"],
               "--ht", f"{ht_from_goals(g1)}-{ht_from_goals(g2)}"]
        if status in ("STATUS_FINAL_AET", "STATUS_FINAL_PEN"):
            # ET/pens need score breakdowns ESPN doesn't expose cleanly here;
            # leave knockout overtime games to the AI backstop.
            notify("World Cup score needs manual check",
                   f"{m['team1']} vs {m['team2']} ended {status} — "
                   "extra-time/shootout result left for AI backstop or manual entry.",
                   tags="warning")
            continue
        for g in g1:
            cmd += ["--goal1", g]
        for g in g2:
            cmd += ["--goal2", g]

        if args.dry_run:
            print(" ".join(f'"{c}"' if " " in c else c for c in cmd[1:]))
            continue
        subprocess.run(cmd, check=True, cwd=REPO)
        applied.append(f"{m['team1']} {ft[0]}-{ft[1]} {m['team2']}")

    if not applied or args.dry_run:
        return

    n = json.loads(DATA.read_text(encoding="utf-8"))
    assert len(n["matches"]) == 104, "match count changed — aborting push"
    summary = ", ".join(applied)
    subprocess.run(["git", "add", "2026/worldcup.json"], check=True, cwd=REPO)
    subprocess.run(["git", "commit", "-m", f"Results (auto, ESPN): {summary}"],
                   check=True, cwd=REPO)
    subprocess.run(["git", "push", "origin", "master"], check=True, cwd=REPO)
    notify("World Cup score auto-published", summary,
           click="https://tipping.upbound.com.au")
    print(f"Published: {summary}")


if __name__ == "__main__":
    main()
