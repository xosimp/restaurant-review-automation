"""Erik's checklist sheets load onto his staff checklists (9/28/26):
bar opening -> Bartender AM, bar closing -> Bartender PM, server opening + end of morning -> Server AM,
server closing -> Server PM; a re-run adds nothing."""
import json
import os

from scripts import load_checklists as lc

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DOC = json.load(open(os.path.join(ROOT, "docs", "clients", "simple-ejs", "checklists.json"), encoding="utf-8"))


def test_each_sheet_lands_on_its_job_in_order():
    todo = lc.plan(DOC, {})
    by_job = {}
    for job, line in todo:
        by_job.setdefault(job, []).append(line)
    assert set(by_job) == {"Bartender AM", "Bartender PM", "Server AM", "Server PM", "Manager FOH"}
    # Stephanie's closing sheet (10/14/26): her 35 lines in her order.
    pm = by_job["Bartender PM"]
    assert len(pm) == 35 and pm[0].startswith("Be sure all dishes & dish tubs")
    assert pm[-1] == "Put bar cart in cooler & lock door."
    assert by_job["Bartender AM"][0] == "Punch in with your personal number"
    assert len(by_job["Bartender AM"]) == 29
    am = by_job["Server AM"]
    assert am[0] == "Check Tock" and len(am) == 18 + 3
    assert am[-3:] == ["Before clocking out: Roll silverware",
                       "Before clocking out: Stock anything that's low from previous shift",
                       "Before clocking out: Wipe down expo area"]
    assert by_job["Server PM"][-1] == "Have closer sign employee financial" and len(by_job["Server PM"]) == 22
    assert not any("Bartender opening checklist" in line for line in by_job["Bartender AM"])  # superseded sheet


def test_a_rerun_adds_nothing_and_a_sub_step_names_its_parent():
    todo = lc.plan(DOC, {})
    existing = {}
    for job, line in todo:
        existing.setdefault(job.lower(), set()).add(line)
    assert lc.plan(DOC, existing) == []
    sheet = {"sections": [{"items": [{"label": "Stock", "items": [{"label": "straws"}, "cups"]}]}]}
    assert lc.sheet_lines(sheet) == ["Stock - straws", "Stock - cups"]


def test_sheets_land_as_task_sheets_with_their_sections_and_critical_lines():
    # The loader writes task sheets (10/15/26): it wrote the flat
    # task_templates list, which nobody's checklist has read since 9/30/26.
    src = open(os.path.join(ROOT, "scripts", "load_checklists.py"), encoding="utf-8").read()
    main = src[src.index("def main("):]
    assert "ts.create_sheet(" in main and "ts.add_line(" in main and "add_task_template" not in main
    mgr = next(s for s in DOC["sheets"] if s["job_code"] == "Manager FOH")
    assert mgr["shift_kind"] == "opening" and mgr["requires_signoff"] is True
    rows = lc.sheet_rows(mgr)
    assert rows[0]["section"] == "Walkthrough" and rows[-1]["section"] == "Doors (final 10 minutes)"
    assert rows[-1]["label"] == "Unlock on time and greet the first guests yourself" and rows[-1]["critical"]
    assert len(rows) == 56 and len({r["label"] for r in rows}) == 56          # no duplicate lines
    assert not any("Canvas" in r["label"] for r in rows)
    # The three sheets carried over on 9/30 already hold their lines: only
    # the bar closing and the manager opening sheets are new.
    existing = {}
    for sh in DOC["sheets"]:
        if sh["job_code"] in ("Bartender AM", "Server AM", "Server PM"):
            existing.setdefault(sh["job_code"].lower(), set()).update(lc.sheet_lines(sh))
    todo = lc.plan_sheets(DOC, existing)
    assert [(s["job_code"], s["shift_kind"], len(rows)) for s, rows in todo] == [
        ("Bartender PM", "closing", 35), ("Manager FOH", "opening", 56)]
    every = {}
    for sh in DOC["sheets"]:
        every.setdefault(sh["job_code"].lower(), set()).update(lc.sheet_lines(sh))
    assert lc.plan_sheets(DOC, every) == []                                    # a re-run adds nothing
