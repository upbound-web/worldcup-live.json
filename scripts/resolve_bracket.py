#!/usr/bin/env python3
"""Fill knockout placeholder team names from finished results.

Knockout fixtures start with placeholder names that reference an earlier
match by number:  "W74" = winner of match 74,  "L101" = loser of match 101.
Once match 74 has a score, "W74" everywhere in the bracket becomes the real
team.  This script scans finished matches, works out each winner/loser, and
substitutes those names into any later fixture that still holds a placeholder.

It only ever replaces placeholders, so it's safe to run repeatedly — already
resolved names and unfinished matches are left untouched.  Keeps the exact
openfootball key order so the file stays a drop-in replacement.

Winner is decided on the last available tie-break: penalties (score.p) beats
extra time (score.et) beats full time (score.ft) — matching how knockouts end.

Usage:
  resolve_bracket.py                 resolve in place, print what changed
  resolve_bracket.py --dry-run       print what would change, write nothing
  resolve_bracket.py --file PATH     operate on a different JSON file
"""
import argparse
import json
import re
import sys
from pathlib import Path

DEFAULT_FILE = Path(__file__).resolve().parent.parent / "2026" / "worldcup.json"
# Canonical upstream key order (identical to scripts/update.py).
KEY_ORDER = ["num", "round", "date", "time", "team1", "team2",
             "score", "goals1", "goals2", "group", "ground"]
PLACEHOLDER = re.compile(r"^([WL])(\d+)$")


def decider(score):
    """The [a, b] pair that actually decides a knockout: pens > extra time > FT."""
    for key in ("p", "et", "ft"):
        pair = score.get(key)
        if isinstance(pair, list) and len(pair) == 2:
            return pair
    return None


def winner_loser(match):
    """(winner_name, loser_name) for a finished match, or (None, None)."""
    score = match.get("score")
    if not score:
        return None, None
    pair = decider(score)
    if not pair or pair[0] == pair[1]:
        return None, None  # unfinished, or a draw that never went to a decider
    if pair[0] > pair[1]:
        return match["team1"], match["team2"]
    return match["team2"], match["team1"]


def build_map(matches):
    """num -> {'W': winner, 'L': loser} for every decided, numbered match."""
    out = {}
    for m in matches:
        num = m.get("num")
        if num is None:
            continue
        w, l = winner_loser(m)
        if w is not None:
            out[int(num)] = {"W": w, "L": l}
    return out


def resolve(matches, results):
    """Replace resolvable W##/L## placeholders in place; return list of changes."""
    changes = []
    for m in matches:
        for side in ("team1", "team2"):
            hit = PLACEHOLDER.match(m[side])
            if not hit:
                continue
            slot, num = hit.group(1), int(hit.group(2))
            name = results.get(num, {}).get(slot)
            if name and not PLACEHOLDER.match(name):
                changes.append((m.get("num"), m["round"], m[side], name))
                m[side] = name
    return changes


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--file", default=str(DEFAULT_FILE))
    args = ap.parse_args()

    path = Path(args.file)
    data = json.loads(path.read_text(encoding="utf-8"))
    matches = data["matches"]

    # Iterate to a fixed point: resolving an early round can unlock the next
    # (e.g. filling match 89's teams lets a later run decide W89). In practice
    # one pass suffices because a source must be *finished* to resolve, but the
    # loop makes the script order-independent and future-proof.
    all_changes = []
    while True:
        changes = resolve(matches, build_map(matches))
        if not changes:
            break
        all_changes.extend(changes)

    if not all_changes:
        print("No placeholders to resolve.")
        return 0

    for num, rnd, before, after in all_changes:
        tag = f"#{num}" if num is not None else "  "
        print(f"{tag:>4}  {rnd:<22}  {before:>5}  ->  {after}")

    if args.dry_run:
        print(f"\n[dry-run] {len(all_changes)} name(s) would be filled; nothing written.")
        return 0

    data["matches"] = [{k: m[k] for k in KEY_ORDER if k in m} for m in matches]
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False) + "\n",
                    encoding="utf-8")
    print(f"\nResolved {len(all_changes)} placeholder name(s).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
