"""Load a client's checklist sheets (docs/clients/<client>/checklists.json)
onto a restaurant's staff checklists (models.task_templates).

Run on production after the file is pushed (railway ssh, from /app):

    python3 scripts/load_checklists.py docs/clients/simple-ejs/checklists.json 5           # dry run
    python3 scripts/load_checklists.py docs/clients/simple-ejs/checklists.json 5 --apply

A staff member's checklist is one flat list per job, so each sheet flattens
in its own order: a sub-step reads "Parent - sub-step", and a sheet with a
`prefix` heads each of its lines with it (two sheets on one job). Sheets
under `superseded` are never loaded. Idempotent: a line already on that job's
list is skipped, so a re-run after a new sheet arrives adds only that sheet.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def sheet_lines(sheet) -> list:
    """The sheet's lines in order, flattened for one flat checklist."""
    prefix = (sheet.get("prefix") or "").strip()
    out = []
    for section in sheet.get("sections") or []:
        for item in section.get("items") or []:
            label = (item.get("label") or "").strip()
            subs = [(s.get("label") if isinstance(s, dict) else s) or "" for s in (item.get("items") or [])]
            lines = [f"{label} - {s.strip()}" for s in subs if s.strip()] if subs else [label]
            out.extend(f"{prefix}: {ln}" if prefix else ln for ln in lines if ln)
    return out


def plan(doc: dict, existing: dict) -> list:
    """[(job, line)] to add: every sheet's lines not already on that job's
    list (`existing`: {job lowercased: set of labels})."""
    todo, seen = [], {k: set(v) for k, v in existing.items()}
    for sheet in doc.get("sheets") or []:
        job = (sheet.get("job_code") or "").strip()
        if not job:
            continue
        have = seen.setdefault(job.lower(), set())
        for line in sheet_lines(sheet):
            if line[:200] not in have:
                todo.append((job, line))
                have.add(line[:200])
    return todo


def main(argv) -> int:
    if len(argv) < 3:
        print(__doc__)
        return 2
    path, rid, apply = argv[1], int(argv[2]), "--apply" in argv
    import models
    r = models.get_restaurant(rid)
    if not r:
        print(f"no restaurant {rid}")
        return 1
    doc = json.load(open(path, encoding="utf-8"))
    existing = {}
    for t in models.get_task_templates(rid):
        existing.setdefault(t["role"].strip().lower(), set()).add(t["label"])
    todo = plan(doc, existing)
    print(f"{r.name} (rid {rid}): {len(todo)} line(s) to add" + ("" if apply else " - dry run, --apply to write"))
    for job, line in todo:
        print(f"  [{job}] {line}")
        if apply:
            models.add_task_template(rid, job, line)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
