#!/usr/bin/env python3
"""Update a match result in 2026/worldcup.json.

Keeps the exact openfootball schema (score.ft/ht/et/p, goals1/goals2)
so the file stays a drop-in replacement for apps built on upstream.

Usage:
  scripts/update.py TEAM1 TEAM2 FT [--ht H-H] [--et E-E] [--p P-P]
                    [--goal1 "Name MIN"]... [--goal2 "Name MIN"]...
                    [--date YYYY-MM-DD] [--file PATH]

Goal syntax: "Julián Quiñones 9", "Harry Kane 45+2 (pen)", "Someone 30 (og)"

Example:
  scripts/update.py Mexico "South Africa" 2-0 --ht 1-0 \
      --goal1 "Julián Quiñones 9" --goal1 "Raúl Jiménez 67"
"""
import argparse
import json
import re
import sys
from pathlib import Path

DEFAULT_FILE = Path(__file__).resolve().parent.parent / "2026" / "worldcup.json"
KEY_ORDER = ["num", "round", "date", "time", "team1", "team2",
             "score", "goals1", "goals2", "group", "ground"]


def parse_pair(s, label):
    m = re.fullmatch(r"(\d+)-(\d+)", s.strip())
    if not m:
        sys.exit(f"Bad {label} score '{s}' — expected e.g. 2-0")
    return [int(m.group(1)), int(m.group(2))]


def parse_goal(s):
    m = re.fullmatch(r"(.+?)\s+(\d+)(?:\+(\d+))?\s*(?:\((pen|og)\))?", s.strip())
    if not m:
        sys.exit(f"Bad goal '{s}' — expected e.g. 'Name 67', 'Name 45+2 (pen)'")
    goal = {"name": m.group(1), "minute": int(m.group(2))}
    if m.group(3):
        goal["offset"] = int(m.group(3))
    if m.group(4) == "pen":
        goal["penalty"] = True
    if m.group(4) == "og":
        goal["owngoal"] = True
    return goal


def team_match(query, name):
    return query.lower() in name.lower()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("team1")
    ap.add_argument("team2")
    ap.add_argument("ft")
    ap.add_argument("--ht")
    ap.add_argument("--et", help="score after extra time (knockouts)")
    ap.add_argument("--p", help="penalty shoot-out score (knockouts)")
    ap.add_argument("--goal1", action="append", default=[])
    ap.add_argument("--goal2", action="append", default=[])
    ap.add_argument("--date", help="disambiguate if teams meet twice")
    ap.add_argument("--file", default=str(DEFAULT_FILE))
    args = ap.parse_args()

    path = Path(args.file)
    data = json.loads(path.read_text(encoding="utf-8"))

    candidates = [
        m for m in data["matches"]
        if team_match(args.team1, m["team1"]) and team_match(args.team2, m["team2"])
        and (not args.date or m["date"] == args.date)
    ]
    if not candidates:
        sys.exit(f"No match found for {args.team1} vs {args.team2}"
                 f"{' on ' + args.date if args.date else ''}")
    if len(candidates) > 1:
        listing = "\n".join(f"  {m['date']}  {m['team1']} vs {m['team2']} ({m['round']})"
                            for m in candidates)
        sys.exit(f"Ambiguous — pass --date:\n{listing}")

    match = candidates[0]
    score = {}
    if args.p:
        score["p"] = parse_pair(args.p, "--p")
    if args.et:
        score["et"] = parse_pair(args.et, "--et")
    score["ft"] = parse_pair(args.ft, "FT")
    if args.ht:
        score["ht"] = parse_pair(args.ht, "--ht")
    match["score"] = score
    match["goals1"] = [parse_goal(g) for g in args.goal1]
    match["goals2"] = [parse_goal(g) for g in args.goal2]

    # rewrite all matches in canonical upstream key order
    data["matches"] = [
        {k: m[k] for k in KEY_ORDER if k in m} for m in data["matches"]
    ]
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n",
                    encoding="utf-8")
    print(f"Updated: {match['team1']} {score['ft'][0]}-{score['ft'][1]} {match['team2']}"
          f" ({match['round']}, {match['date']})")


if __name__ == "__main__":
    main()
