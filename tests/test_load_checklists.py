"""Erik's checklist sheets load onto his staff checklists (9/28/26):
bar opening -> Bartender AM, server opening + end of morning -> Server AM,
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
    assert set(by_job) == {"Bartender AM", "Server AM", "Server PM"}
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
