"""Load a client's checklist sheets (docs/clients/<client>/checklists.json)
onto a restaurant's task sheets (task_sheets.py).

Since 10/19/26 (scale audit ONB-7) this is an admin-console action —
Customers → Onboarding → the client's Setup → Checklists (dry run, then
Load) — and this script is a thin wrapper over the same functions
(client_setup.plan_checklists / apply_checklists), kept for a shell:

    python3 scripts/load_checklists.py docs/clients/simple-ejs/checklists.json 5           # dry run
    python3 scripts/load_checklists.py docs/clients/simple-ejs/checklists.json 5 --apply

Each sheet lands on the task sheet for its job and shift (opening, closing,
mid, all day), made when there is none, with its title and sign-off; each
line keeps its section and critical flag. A sub-step reads "Parent -
sub-step", and a sheet with a `prefix` heads each of its lines with it.
Sheets under `superseded` are never loaded. Idempotent: a line already on any
of that job's sheets is skipped, so a re-run after a new sheet arrives adds
only that sheet.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from client_setup import sheet_rows, sheet_lines, plan_sheets, plan  # noqa: E402,F401  (the planners live there)


def main(argv) -> int:
    if len(argv) < 3:
        print(__doc__)
        return 2
    path, rid, apply = argv[1], int(argv[2]), "--apply" in argv
    import models
    import client_setup
    r = models.get_restaurant(rid)
    if not r:
        print(f"no restaurant {rid}")
        return 1
    doc = json.load(open(path, encoding="utf-8"))
    dry = client_setup.plan_checklists(rid, doc)
    if not dry.get("ok"):
        print(dry.get("error"))
        return 1
    print(f"{r.name} (rid {rid}): {dry['lines']} line(s) on {len(dry['sheets'])} sheet(s) to add"
          + ("" if apply else " - dry run, --apply to write"))
    for s in dry["sheets"]:
        print(f"  {s['job']} / {s['shift_kind']}: {s['title']} - "
              + (f"adds to sheet {s['target_sheet_id']}" if s["target_sheet_id"] else "new sheet"))
        for row in s["lines"]:
            print(f"    [{row['section'] or ''}] {row['label']}" + (" (critical)" if row["critical"] else ""))
    if apply:
        out = client_setup.apply_checklists(rid, doc)
        print(out)
        return 0 if out.get("ok") else 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
