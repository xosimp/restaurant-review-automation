"""Load a client's checklist sheets (docs/clients/<client>/checklists.json)
onto a restaurant's task sheets (task_sheets.py).

Run on production after the file is pushed (railway ssh, from /app):

    python3 scripts/load_checklists.py docs/clients/simple-ejs/checklists.json 5           # dry run
    python3 scripts/load_checklists.py docs/clients/simple-ejs/checklists.json 5 --apply

Each sheet lands on the task sheet for its job and shift (opening, closing,
mid, all day), made when there is none, with its title and sign-off; each
line keeps its section and critical flag. A sub-step reads "Parent -
sub-step", and a sheet with a `prefix` heads each of its lines with it.
Sheets under `superseded` are never loaded. Idempotent: a line already on any
of that job's sheets is skipped, so a re-run after a new sheet arrives adds
only that sheet.

Until 10/15/26 this wrote the flat task_templates list, which task sheets
replaced on 9/30/26 (carried over once, for a restaurant with no sheets): a
sheet loaded after that — Simple EJ's bar closing duties, 10/14/26 — reached
nobody's checklist.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def sheet_rows(sheet) -> list:
    """The sheet's lines in order: [{label, section, critical}]."""
    prefix = (sheet.get("prefix") or "").strip()
    out = []
    for section in sheet.get("sections") or []:
        title = (section.get("title") or "").strip() or None
        for item in section.get("items") or []:
            label = (item.get("label") or "").strip()
            subs = [(s.get("label") if isinstance(s, dict) else s) or "" for s in (item.get("items") or [])]
            lines = [f"{label} - {s.strip()}" for s in subs if s.strip()] if subs else [label]
            for ln in lines:
                if ln:
                    out.append({"label": f"{prefix}: {ln}" if prefix else ln, "section": title,
                                "critical": bool(item.get("critical"))})
    return out


def sheet_lines(sheet) -> list:
    """The sheet's lines in order, as labels."""
    return [r["label"] for r in sheet_rows(sheet)]


def plan_sheets(doc: dict, existing: dict) -> list:
    """[(sheet, [row])] to add: each sheet with the rows not already on that
    job's sheets (`existing`: {job lowercased: set of labels})."""
    out, seen = [], {k: set(v) for k, v in existing.items()}
    for sheet in doc.get("sheets") or []:
        job = (sheet.get("job_code") or "").strip()
        if not job:
            continue
        have = seen.setdefault(job.lower(), set())
        rows = []
        for r in sheet_rows(sheet):
            if r["label"][:200] not in have:
                rows.append(r)
                have.add(r["label"][:200])
        if rows:
            out.append((sheet, rows))
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
    import task_sheets as ts
    r = models.get_restaurant(rid)
    if not r:
        print(f"no restaurant {rid}")
        return 1
    doc = json.load(open(path, encoding="utf-8"))
    sheets = ts.list_sheets(rid)
    existing = {}
    for sh in sheets:
        existing.setdefault(sh["job_code"].strip().lower(), set()).update(l["label"] for l in sh["lines"])
    todo = plan_sheets(doc, existing)
    print(f"{r.name} (rid {rid}): {sum(len(rows) for _s, rows in todo)} line(s) on {len(todo)} sheet(s) to add"
          + ("" if apply else " - dry run, --apply to write"))
    for sheet, rows in todo:
        job, kind = sheet["job_code"].strip(), sheet.get("shift_kind") or "any"
        target = next((sh for sh in sheets if sh["job_code"].strip().lower() == job.lower()
                       and sh["shift_kind"] == kind), None)
        print(f"  {job} / {kind}: {sheet.get('title')} - {'adds to sheet ' + str(target['id']) if target else 'new sheet'}")
        for row in rows:
            print(f"    [{row['section'] or ''}] {row['label']}" + (" (critical)" if row["critical"] else ""))
        if apply:
            if target is None:
                target = ts.create_sheet(rid, job, kind, title=sheet.get("title"),
                                         requires_signoff=bool(sheet.get("requires_signoff")), who="loaded from file")
                sheets.append(target)
            for row in rows:
                ts.add_line(rid, target["id"], row, who="loaded from file")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
