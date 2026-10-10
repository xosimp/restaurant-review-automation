"""Translate a restaurant's staffing floors into people on at once.

Since the schedule fix round of 10/10/26 (contract #2) every floor is held
as people ON AT ONCE at every half hour of the core of the shift (11am-2:30pm,
5:30-8:30pm). A floor set earlier as a count of different people — the
Crew stage's Keep usual before then, or a usual crew typed in — can ask for
more on at once than the restaurant's own usual night runs. This reports,
per role, weekday and shift, where a role family's floors add up past what
its usual night holds on at once, and the floor that shift should carry
(crew_matrix.floors_on_at_once). It never raises a floor.

Dry run by default. With --apply it writes the changed shifts as day
overrides and restates any Crew hold on them, in one compare-and-swap.

    python scripts/crew_floors_on_at_once.py --restaurant 5
    python scripts/crew_floors_on_at_once.py --restaurant 5 --roles "Kitchen,Dishwasher,Server PM"
    python scripts/crew_floors_on_at_once.py --restaurant 5 --apply

On production: railway ssh "/opt/venv/bin/python scripts/crew_floors_on_at_once.py --restaurant 5"
(dry run first; the lead applies it).
"""
import argparse
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--restaurant", type=int, required=True)
    ap.add_argument("--roles", default="", help="comma-separated job codes; default every floor")
    ap.add_argument("--apply", action="store_true", help="write the result (default: dry run)")
    ap.add_argument("--json", action="store_true", help="print the result as JSON")
    a = ap.parse_args(argv)
    import crew_matrix
    roles = [x.strip() for x in a.roles.split(",") if x.strip()] or None
    out = crew_matrix.floors_on_at_once(a.restaurant, roles=roles, apply=a.apply)
    if a.json:
        print(json.dumps(out, indent=2, default=str))
        return 0
    word = {"morning": "lunch", "night": "dinner"}
    if not out["changes"]:
        print("No floor to lower: every floor the history can read already holds on the restaurant's own usual "
              "night, on at once.")
    for c in out["changes"]:
        print(f"{c['role']:<20} {c['day']:<10} {word[c['part']]:<7} {c['old']:>2} -> {c['new']:>2}"
              f"   (usual on at once: this code "
              f"{c['code_usual'] if c['code_usual'] is not None else '-'}, the whole family {c['family_usual']})")
    for u in out["untouched"]:
        print(f"{u['role']:<20} {u['day']:<10} {word[u['part']]:<7} {u['floor']:>2}    left: no usual crew on "
              "at once in the history to translate it by")
    print("APPLIED" if out["applied"] else "Dry run: nothing written. Add --apply to write it.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
