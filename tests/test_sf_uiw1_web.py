"""Schedule fix round 10/3/26, UI wave W1 — the web Schedule Studio's
generate screen, the draft, the review, the publish check, Shift Quality,
the learning rows and the admin engineering console, built on the fix
round's payloads (the workstreams' UI_NEEDS: M, B2, H1, E, F1, A1, A2, B1,
C2, F2, G, D1a, D1b, D2, H2).

- Behaviour, under node where it is installed: the page's own pure helpers
  (the global `sfw1*` functions in <script id="cav-sfw1">), given the
  payloads the server sends — the owner's words, every date M/D/YY, a
  missing figure left out rather than zero.
- Source rules: each element pinned to the payload field it reads and the
  route it calls.
- Backend: the admin console's schedule generations read.
"""
import json
import os
import re
import shutil
import subprocess

import pytest

import admin_ops
import models

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DASH = os.path.join(ROOT, "templates", "dashboard.html")
ADMIN = os.path.join(ROOT, "templates", "admin.html")
ISO = re.compile(r"\b20\d\d-\d\d-\d\d\b")


def _read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


@pytest.fixture(scope="module")
def page():
    return _read(DASH)


def _block(page, sid):
    i = page.index(f'<script id="{sid}">')
    return page[i:page.index("</script>", i)]


def _helpers(page):
    b = _block(page, "cav-sfw1")
    return b[len('<script id="cav-sfw1">'):b.index("\n/* ── Behaviour ── */")]


def _iife(page):
    b = _block(page, "cav-sfw1")
    return b[b.index("\n/* ── Behaviour ── */"):]


def _node(js):
    if not shutil.which("node"):
        pytest.skip("node is not installed")
    out = subprocess.run(["node", "-"], input=js, capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr[-1500:]
    return json.loads(out.stdout.strip().splitlines()[-1])


def _run(page, calls):
    js = _helpers(page) + "\nvar __o = {};\n"
    for k, expr in calls.items():
        js += f"__o[{json.dumps(k)}] = {expr};\n"
    js += "console.log(JSON.stringify(__o));"
    return _node(js)


def _fn(src, name):
    m = re.search(r"(?:function\s+" + re.escape(name) + r"\s*\(|" + re.escape(name) + r"\s*=\s*function\s*\()", src)
    assert m, f"{name} is gone from the page"
    nxt = re.search(r"\n\s{0,4}(?:function\s+\w+\s*\(|window\.\w+\s*=\s*function|/\* ──|// ──)", src[m.end():])
    return src[m.start():m.end() + (nxt.start() if nxt else len(src) - m.end())]


def _top(src, name):
    """A top-level function, whole: to the next function at the left margin."""
    i = src.index("\nfunction " + name + "(") + 1
    j = src.find("\nfunction ", i + 10)
    return src[i:j if j > 0 else len(src)]


def _no_iso(o):
    for k, v in o.items():
        assert not ISO.search(json.dumps(v)), (k, v)


# ── behaviour: the words an owner reads ─────────────────────────────────────

def test_days_not_written_and_days_nobody_can_work_are_said_by_day(page):
    o = _run(page, {
        "partial": 'sfw1PartialLine([{date: "2026-10-10", day: "Saturday", why: "the model ran out of time"},'
                   ' {date: "2026-10-11", day: "Sunday", why: "the model ran out of time"}])',
        "two": 'sfw1PartialLine([{date: "2026-10-10", why: "the model declined to write them"}, {date: "2026-10-11", why: "x"}])',
        "none": "sfw1PartialLine([])",
        "unstaff": 'sfw1UnstaffableLine([{date: "2026-10-10", day: "Saturday", reasons: ["Ana: time off", "Bo: unavailable"]}])',
    })
    assert o["partial"] == ("Saturday 10/10/26 and Sunday 10/11/26 weren’t written — the model ran out of time. "
                            "The rest of the week is here.")
    assert o["two"].startswith("Saturday 10/10/26 wasn’t written — the model declined to write them; Sunday 10/11/26")
    assert o["none"] == ""
    assert o["unstaff"].startswith("Nobody on the team can work Saturday 10/10/26 (time off or availability)")
    assert "Ana: time off" in o["unstaff"]
    _no_iso(o)


def test_the_manager_plan_reads_as_windows_stretches_and_skipped_shifts(page):
    o = _run(page, {
        "win": 'sfw1PlanWindow({from: "11:00am", to: "11:00pm", source: "hours"})',
        "nowin": "sfw1PlanWindow(null)",
        "unc": 'sfw1UncoveredLine({date: "2026-10-07", day: "Wednesday", from: "11:00am", to: "11:00pm",'
               ' why: "Erik: on approved time off; Jim: at their 55h weekly cap"})',
        "left": 'sfw1LeftLine({date: "2026-10-07", day: "Wednesday", from: "9:00am", to: "10:00am",'
                ' reasons: [{employee: "Max", why: "on approved time off"}], could_act: ["Kay"]})',
        "skip": 'sfw1SkippedLine({employee: "Erik", date: "2026-10-06", why: "their note: no Tuesdays this month"})',
    })
    assert o["win"] == "Manager on 11:00am–11:00pm"
    assert o["nowin"] == ""
    assert o["unc"] == ("No manager can be on Wednesday 10/7/26 from 11:00am to 11:00pm — Erik: on approved time off; "
                        "Jim: at their 55h weekly cap.")
    assert o["left"] == "No manager on Wednesday 10/7/26 9:00am–10:00am: Max — on approved time off."
    assert o["skip"] == "Erik’s standing shift on Tuesday 10/6/26 wasn’t used — their note: no Tuesdays this month."
    _no_iso(o)


def test_the_hours_split_never_invents_a_figure(page):
    o = _run(page, {
        "all": "sfw1HoursLine(312, 330, 86)",
        "nobudget": "sfw1HoursLine(312, 0, 86)",
        "nosal": "sfw1HoursLine(312.25, 330, null)",
        "none": "sfw1HoursLine(null, 330, 86)",
    })
    assert o["all"] == "312h hourly of 330h budget · 86h salaried"
    assert o["nobudget"] == "312h hourly · 86h salaried"
    assert o["nosal"] == "312.3h hourly of 330h budget"
    assert o["none"] == ""


def test_what_the_week_doesnt_meet_groups_by_day_with_this_week_last(page):
    unmet = json.dumps([
        {"kind": "coverage", "date": "2026-10-16", "day": "Friday", "daypart": "night", "what": "Two servers short", "why": "floors ask for 6"},
        {"kind": "manager", "date": "2026-10-14", "day": "Wednesday", "daypart": "morning", "what": "No manager 9–10am", "why": "Max off"},
        {"kind": "budget", "date": "", "day": "", "daypart": "", "what": "12h over the hourly budget", "why": "floors held it"},
        {"kind": "unchecked_rule", "date": "", "what": "Your rule “no doubles”", "why": "no code can check it"},
        {"kind": "min_hours", "date": "2026-10-16", "what": "Cook 2h under 40h", "why": "nobody legal"},
    ])
    o = _run(page, {"g": f"sfw1UnmetGroups({unmet})",
                    "tones": '[sfw1UnmetTone("manager"), sfw1UnmetTone("closer"), sfw1UnmetTone("station"), sfw1UnmetTone("budget"), sfw1UnmetTone("unchecked_rule")]'})
    labels = [g["label"] for g in o["g"]]
    assert labels == ["Wed 10/14/26", "Fri 10/16/26", "This week"]
    fri = o["g"][1]["items"]
    assert [i["tone"] for i in fri] == ["soft", "neutral"] and fri[0]["part"] == "dinner/night"
    assert o["g"][2]["items"][1]["check"] is True
    assert o["tones"] == ["hard", "hard", "soft", "neutral", "neutral"]
    # the group's key is the ISO date it sorts by; what the owner reads is M/D/YY
    _no_iso({"shown": [[g["label"]] + [[i["what"], i["why"], i["part"]] for i in g["items"]] for g in o["g"]]})


def test_stage_failures_lead_with_the_blocking_ones(page):
    o = _run(page, {"f": 'sfw1StageFailures([{stage: "optimizer", blocks_publish: false, line: "Improve didn\'t run."},'
                         ' {stage: "manager", blocks_publish: true, line: "⚠ The manager pass didn\'t run — check every day."},'
                         ' {stage: "x"}])'})
    assert o["f"] == [{"text": "The manager pass didn't run — check every day.", "blocking": True},
                      {"text": "Improve didn't run.", "blocking": False}]


def test_budget_and_section_conflicts_say_what_held_them(page):
    o = _run(page, {
        "held": 'sfw1BudgetHeld({over_by: 14, held: {floor: 3, requirement: 2, kept: 0}})',
        "none": "sfw1BudgetHeld(null)",
        "cap": 'sfw1CapConflictLine({date: "2026-10-17", day: "Saturday", at: "7:00pm", on: 8, cap: 6,'
               ' held_by: [{kind: "floor", role: "Server", daypart: "night", floor: 8}, {kind: "rule", label: "two on the patio"}]})',
    })
    assert o["held"] == "14h over the budget: 3 shifts held by your staffing floors, 2 shifts held by the shift requirements."
    assert o["none"] == ""
    assert o["cap"] == ("Saturday 10/17/26 at 7:00pm: 8 on for 6 sections — held by your floor of 8 Server at "
                        "dinner/night; two on the patio.")


def test_a_fix_line_finds_its_row_by_id_then_index_then_reads_as_the_fix_left_it(page):
    rows = json.dumps([{"_rid": "r1", "employee": "Ana", "date": "2026-10-06", "shift_start": "4:00pm", "shift_end": "10:00pm"},
                       {"_rid": "r2", "employee": "Bo", "date": "2026-10-07", "shift_start": "9:00am", "shift_end": "3:00pm"}])
    o = _run(page, {
        "byid": f'sfw1FixRow({{row_id: "r2", index: 0}}, {rows}).index',
        "byix": f'sfw1FixRow({{index: 0}}, {rows}).index',
        "gone": f'sfw1FixRow({{index: null, row: {{employee: "Cy", date: "2026-10-08", shift_start: "5:00pm", shift_end: "11:00pm"}}}}, {rows})',
        "when": 'sfw1FixWhen({date: "2026-10-08", shift_start: "5:00pm", shift_end: "11:00pm"})',
    })
    assert o["byid"] == 1 and o["byix"] == 0
    assert o["gone"]["index"] is None and o["gone"]["row"]["employee"] == "Cy"
    assert o["when"] == "Thu 10/8/26 5:00pm–11:00pm"


def test_cavnar_ais_row_signature_matches_the_server(page):
    import schedule_versions
    row = {"date": "2026-10-06", "employee": "  Ana   Bell ", "shift_start": "4:00PM ", "shift_end": "10:00pm", "role": "Server"}
    o = _run(page, {"sig": f"sfw1RowSig({json.dumps(row)})"})
    assert o["sig"] == schedule_versions.row_sig(row)


def test_the_requirements_view_says_usual_asks_and_the_late_row(page):
    o = _run(page, {
        "role": 'sfw1ReqRole({role: "Server", required: 5, floor: 3, typical: 4, reason: "+1 asked by last Friday\'s report"})',
        "same": 'sfw1ReqRole({role: "Cook", required: 2, floor: 0, typical: 2})',
        "head": 'sfw1ReqHead({date: "2026-10-16", day: "Friday", daypart: "night"})',
        "late": 'sfw1ReqHead({date: "2026-10-16", day: "Friday", daypart: "late", window: [1320, 1560]})',
    })
    assert o["role"] == "Server 5 (usual 4), at least 3 · +1 asked by last Friday's report"
    assert o["same"] == "Cook 2"
    assert o["head"] == "Fri 10/16/26 · dinner/night"
    assert o["late"] == "Fri 10/16/26 · Late night 10:00pm–2:00am"


def test_shift_quality_words_points_misses_overtime_and_facts(page):
    o = _run(page, {
        "pts": "[sfw1Points(6), sfw1Points(1), sfw1Points(0), sfw1Points(null)]",
        "add": 'sfw1MissAction({role: "Bartender", why: "nobody_on", rule: "1 bartender"})',
        "swap": 'sfw1MissAction({role: "Bartender", why: "not_qualified"})',
        "ot": 'sfw1OvertimeLine({name: "Ana", bucket: "2026-10-05", overtime_hours: 5, premium: 50, avoidable: true, teammate: "Bo"}, true)',
        "ot2": 'sfw1OvertimeLine({name: "Ana", bucket: "", overtime_hours: 2.5, premium: 0, avoidable: false}, false)',
        "st": 'sfw1DimFacts({key: "stations", facts: {required: ["grill", "fry"], assigned: {"grill": "Cy"}, gaps: ["fry"]}})',
        "ct": 'sfw1DimFacts({key: "cross_training", facts: {not_judged: ["host"]}})',
        "exp": 'sfw1DimFacts({key: "experience_balance", facts: {by_default: ["Erik"]}})',
    })
    assert o["pts"] == ["up to +6 points", "up to +1 point", "", ""]
    assert o["add"] == {"kind": "add", "label": "Add a bartender"}
    assert o["swap"] == {"kind": "swap", "label": "Swap in a bartender"}
    assert o["ot"] == ("Ana is 5h into overtime in the payroll week of 10/5/26 — about $50 of overtime premium; "
                       "Bo has room in the same role for one of their shifts.")
    assert o["ot2"].endswith("nobody else in the role has room to take a shift.")
    assert o["st"] == ["Needs grill and fry — grill on Cy; nobody trained on fry."]
    assert o["ct"][0].startswith("Not judged for host")
    assert "counted as experienced" in o["exp"][0]
    _no_iso(o)


def test_learning_lines_count_over_chances_and_never_zero_a_missing_figure(page):
    o = _run(page, {
        "n": "[sfw1MemCount({hits: 2, opportunities: 3}), sfw1MemCount({hits: 0, opportunities: 0})]",
        "out": "sfw1OutcomeBits({avg_hours: 22, avg_actual_hours: 19, missed: 2, stayed_late: 1, labor_pct: 24.5, splh: 80, splh_basis: 'worked'})",
        "bare": "sfw1OutcomeBits({avg_hours: 22, avg_actual_hours: null, missed: 0, labor_pct: null})",
        "rate": "sfw1RatingLine({suggested: 4, sales_per_cover: 40, house_sales_per_cover: 34.62, ratio: 1.155, tickets: 30})",
    })
    assert o["n"] == ["2 of 3", ""]
    assert o["out"] == ["22h scheduled · 19h worked", "2 shifts missed", "1 ran late", "24.5% labor", "sales per worked hour"]
    assert o["bare"] == []
    assert o["rate"] == ("Suggested 4 — sells $40.00 a guest vs $34.62 for the house at the same mealtimes (+16%), "
                         "30 tickets")


def test_the_redo_reasons_are_the_servers(page):
    import schedule_engine
    iife = _iife(page)
    keys = re.findall(r"\['(\w+)', '", iife[iife.index("var REDO = "):iife.index("window.SFW1_REDO")])
    assert keys and set(keys) <= set(schedule_engine.REDO_REASONS)
    assert "maxlength=\"300\"" in iife and str(schedule_engine.REDO_REASON_MAX_CHARS) == "300"


# ── source rules: each element wired to its field and route ─────────────────

def test_the_generation_waits_as_long_as_the_job_can_and_sends_the_owners_words(page):
    gen = _top(page, "generateSchedule")
    for piece in ("startData.wait_seconds", "pollData.seconds_left", "body.reason_chip", "body.reason_text",
                  "body.instruction", "'sfw1-instr'"):
        assert piece in gen, piece
    assert 'id="sfw1-instr" maxlength="500"' in page
    redo = _top(page, "regenerateScheduleDays")
    assert "sfw1RedoSheet(dates)" in redo
    res = _top(page, "_schedHandleResult")
    assert "_schedNotice(_schedBtn, data.error ||" in res and "' Try again.'" not in res
    assert "sfw1Take(data)" in res


def test_the_week_notices_read_the_generation_payload(page):
    iife = _iife(page)
    take = iife[iife.index("window.sfw1Take"):iife.index("window.sfw1State")]
    for f in ("data.manager_plan", "rv.manager_plan", "data.manager_coverage", "rv.manager_coverage", "data.min_hours",
              "data.unwritten_dates", "rv.unwritten_dates", "data.unstaffable_dates", "data.starting_point",
              "data.demand_data_through", "data.requirements", "rv.setup", "data.hours_hourly", "data.hours_salaried"):
        assert f in take, f
    notes = iife[iife.index("function notesHtml"):iife.index("function setupHtml")]
    for piece in ("data-sfw1-redo", "p.failed", "p.question", "S.cover.shortfall", "could_act", "sfw1SkippedLine",
                  "short_by", "S.startLine"):
        assert piece in notes, piece
    setup = iife[iife.index("function setupHtml"):iife.index("function actingActs")]
    assert "leader_rules_inactive" in setup and "it.can_adopt" in setup and "close_times_missing" in setup
    assert "'/api/labor/team/ratings/adopt'" in iife
    assert 'id="sfw1-week"' in page and "sfw1-sum" in _top(page, "ssRenderSummary")


def test_the_managers_question_saves_standing_shifts_and_acting_dates(page):
    iife = _iife(page)
    save = iife[iife.index("function saveStanding"):iife.index("// ── Redo some days")]
    assert "'/api/labor/managers'" in save and "standing_shifts: keep.concat(picked)" in save
    assert "'/api/labor/staff-settings'" in save
    assert "acting_manager: list" in iife and "'/api/labor/roster'" in iife


def test_the_grid_marks_planned_rows_manager_windows_and_day_level_breaches(page):
    grid = _top(page, "swRenderGrid")
    assert "sfw1DayHead(d)" in grid and "sfw1ChipMarks(rw)" in grid
    iife = _iife(page)
    head = iife[iife.index("window.sfw1DayHead"):iife.index("window.sfw1ChipMarks")]
    assert "p.windows" in head and "hard_days" in head and "S.unwritten" in head
    chip = iife[iife.index("window.sfw1ChipMarks"):iife.index("window.sfw1ShiftExtras")]
    assert "_pinned === 'manager_plan'" in chip and "_pin_reason" in chip and "dst_hours" in chip
    assert "sfw1ShiftExtras(r)" in _top(page, "ssRenderShift")
    rv = page[page.index("window.renderScheduleReview=function"):page.index("window.schedMoveOvertime=function")]
    mark = page[page.index("function _rvMarkRows"):page.index("window.renderScheduleReview=function")]
    assert "!v.day_level" in mark, "a day-level breach never flags a person's row"
    assert "gv.day_level" in rv and "sfw1FixRow(f," in rv and "data-sfw1-sel" in rv and "sfw1ReviewHtml(rv)" in rv


def test_the_review_groups_read_their_fields(page):
    iife = _iife(page)
    rv = iife[iife.index("window.sfw1ReviewHtml"):iife.index("window.sfw1HoursHtml")]
    for f in ("rv.stage_failures", "rv.unmet", "rv.budget_conflict", "rv.cap_floor_conflicts", "rv.unmatched_names",
              "data-person-open", "check it yourself", "holds the send"):
        assert f in rv, f


def test_save_keeps_cavnar_ais_changes_and_asks_why(page):
    save = _top(page, "rescoreSchedule")
    assert "body.cavnar_changes" in save and "d.why_questions" in save and "sfw1WhySheet(" in save
    assert "fixes: (d.review && d.review.fixes)" in save
    assert "window._sfw1Removed = []" in _top(page, "discardScheduleEdits")
    fixes = page[page.index("window.applyScheduleFixes=function"):page.index("window.improveScheduleWithCavnar=function")]
    imp = page[page.index("window.improveScheduleWithCavnar=function"):page.index("// One toggle for the four rows")]
    assert "sfw1NoteRemoved(d.cavnar_removed)" in fixes and "sfw1NoteRemoved(d.cavnar_removed)" in imp
    mv = page[page.index("window.schedMoveOvertime=function"):page.index("window.schedAskStandby=function")]
    assert "sfw1MarkOrigin(rows[hit],'overtime_move')" in mv
    iife = _iife(page)
    assert "'/api/labor/schedule/edit-why'" in iife and "history_id: +btn.getAttribute('data-hid')" in iife
    assert "d.counted === false" in iife and "Saved for the owner to confirm" in iife


def test_publish_shows_notes_hours_and_likely_changes(page):
    iife = _iife(page)
    pub = iife[iife.index("window.sfw1PubHtml"):iife.index("window.sfw1RenderPub")]
    for f in ("c.hours", "hr.hourly", "hr.salaried", "c.notes", "Worth a look", "c.likely_to_change", "lk.ready",
              "lk.rows", "lk.note"):
        assert f in pub, f
    assert 'id="sfw1-pub"' in page
    assert "sfw1RenderPub(d)" in page[page.index("window.psRefresh = function"):page.index("/* ── The verdict line")]
    assert "d.notes" in _top(page, "publishScheduleNow")


def test_shift_quality_reads_the_new_fields(page):
    warn = _top(page, "renderQualityWarnings")
    for f in ("b.reason", "b.no_shift_written", "recObj.points", "+a.r.points", "hard rules are never hidden"):
        assert f in warn, f
    kind = _top(page, "_recKind")
    assert "'Cover the gap'" in kind and "'Put a stronger'" in kind and "return 'rules'" in kind
    det = _top(page, "_qualityShiftDetail")
    assert "sfw1ShiftDims(shift)" in det and "shift.held_by.text" in det
    assert "sfw1ConfWhy(confidence)" in _top(page, "renderQualityConfidence")
    assert "sfw1WeekDimExtra(w)" in _top(page, "renderQualityDimensions")
    iife = _iife(page)
    for f in ("c.breakdown", "c.top_reason", "f.people", "f.share_pct", "f.learned_misses", "lm[i].rows",
              "d.floor", "data-sfw1-add-role", "what_if: true", "checked_with", "on_demand"):
        assert f in iife, f
    opt = _top(page, "renderQualityOptimizer")
    assert "c.dollars" in opt and "o.dollars_before" in opt and "c.kind === 'trade'" in opt
    assert "sfw1WhatIfHead(_whatIf)" in _top(page, "renderQualityReasoning")


def test_learning_rows_are_wired_and_navigable(page):
    assert 'data-nav="labor/learned"' in page and 'data-nav="labor/ratings"' in page and 'data-nav="labor/intel"' in page
    assert "{% if is_principal is defined and not is_principal %} hidden{% endif %}" in page
    iife = _iife(page)
    for f in ("'/api/labor/schedule-memory'", "{key: key, action: act}", "class_label", "status_label", "confidence_pct",
              "last_confirmed_by_hand", "held_in_code", "bound_by", "retired_words", "can_keep", "can_let_go",
              "can_be_rule", "consolidated_at", "'/api/labor/ratings/suggested'", "d._status === 403",
              "{name: name, score: sc}", "sec === 'ratings'", "sec === 'intel'"):
        assert f in iife, f
    intel = page[page.index("window.renderIntel=function"):page.index("// ── Reviews ──")]
    assert "sfw1OutcomeBits(o)" in intel
    assert "'quality_profiles_suggested'" in page and "suggested new floors and bars" in page


def test_the_studio_and_sections_suggest_the_usual_section(page):
    sec = _top(page, "_ssSecHtml")
    assert "sfw1UsualSection(r, _ssSec.usual)" in sec and "data-sfw1-sec" in sec
    assert "_ssSec.usual = d.usual || []" in page


def test_the_admin_console_shows_generations_and_experiment_power():
    adm = _read(ADMIN)
    assert "'/admin/api/schedule-generations'" in adm and "function engGenerations(g)" in adm
    eng = adm[adm.index("function engGenerations(g)"):adm.index("function engErrors(S, sE)")]
    for f in ("L.p95", "L.slow", "r.stage_seconds", "r.repair", "converged", "refused", "skipped", "hours_by_stage",
              "stage_failures", "blocks_publish"):
        assert f in eng, f
    assert "e.power" in adm and "paused:'warn'" in adm


# ── backend: the console's read ─────────────────────────────────────────────

def test_schedule_generations_reads_timings_and_the_repair_record(db_path):
    rid = models.create_restaurant(models.Restaurant(name="Gen Test", owner_email="gen@x.test"), db_path=db_path)
    conn = models.get_conn(db_path)
    try:
        review = {"repair": {"cycles": 2, "converged": True, "restored_best": False,
                             "refused": [{"stage": "solver"}], "skipped": [{"stage": "optimizer"}],
                             "budget": {"hourly": 312.0, "budget": 330.0, "over": False},
                             "hours_by_stage": {"manager": 4.5}},
                  "stage_failures": [{"stage": "manager", "blocks_publish": True, "line": "x"}]}
        conn.execute("INSERT INTO schedule_history (restaurant_id, week_start, hours_scheduled, hours_budget, "
                     "stage_seconds_json, total_seconds, review_json) VALUES (?,?,?,?,?,?,?)",
                     (rid, "2026-10-12", 398.0, 420.0, json.dumps({"model": 61.2, "repair": 8.4}), 74.0,
                      json.dumps(review)))
        conn.commit()
    finally:
        conn.close()
    out = admin_ops.schedule_generations(db_path=db_path)
    assert out["ok"] and out["latency"]["n"] == 1 and out["latency"]["p95"] == 74.0
    w = out["weeks"][0]
    assert w["restaurant"] == "Gen Test" and w["total_seconds"] == 74.0
    assert w["stage_seconds"] == {"model": 61.2, "repair": 8.4}
    assert w["repair"]["cycles"] == 2 and w["repair"]["refused"] == 1 and w["repair"]["hours_by_stage"] == {"manager": 4.5}
    assert w["stage_failures"] == [{"stage": "manager", "blocks_publish": True}]


def _run_iife(page, body):
    """The behaviour IIFE in node with an empty page: every render that
    returns markup is called on real payload shapes, so a typo in a field or
    a helper fails here, not on the owner's screen."""
    stub = ("var window = globalThis; var __els = {};"
            "var document = {getElementById: function (id) { return __els[id] || null; },"
            " addEventListener: function () {}, querySelectorAll: function () { return []; }, querySelector: function () { return null; }};"
            "function apiJson(r) { return r; } var _schedRows = []; var _schedWeekDates = []; var _ssReview = null;"
            "var _schedRosterNames = ['Kay']; var _swData = null; var _schedHistoryId = 7; var _schedDirty = false;"
            "function cavTimeOptions() { return '<option></option>'; }"
            "function memPill(t, tone) { return '<span class=\"mem-pill ' + (tone || '') + '\">' + t + '</span>'; }"
            "function __host(id) { __els[id] = {innerHTML: '', insertAdjacentHTML: function () {}, style: {}}; return __els[id]; }")
    js = _helpers(page) + "\n" + stub + "\n" + _iife(page).replace("</script>", "") + "\nvar __o = {};\n" + body \
        + "\nconsole.log(JSON.stringify(__o));"
    return _node(js)


def test_the_renders_draw_real_payloads(page):
    o = _run_iife(page, r"""
__host('sfw1-week');
sfw1Take({manager_plan: {failed: false, question: 'Which days and hours do Erik and Jim work?', unknown_pattern: ['Erik', 'Jim'],
            windows: {'2026-10-07': {from: '11:00am', to: '11:00pm'}}, uncovered: [], skipped: [{employee: 'Erik', date: '2026-10-06', why: 'their note'}]},
          manager_coverage: {left: [{date: '2026-10-07', day: 'Wednesday', from: '9:00am', to: '10:00am', reasons: [{employee: 'Max', why: 'on approved time off'}], could_act: ['Kay']}],
                             shortfall: {text: '1h of the week has no manager on (10/7/26).'}},
          min_hours: {left: [{employee: 'Cook', short_by: 1, min: 40, reason: 'nobody legal'}]},
          unwritten_dates: [{date: '2026-10-10', day: 'Saturday', why: 'the model ran out of time'}],
          unstaffable_dates: [{date: '2026-10-11', day: 'Sunday', reasons: ['Ana: time off']}],
          starting_point: {no_history: true}, demand_data_through: {line: 'Sales through 9/1/26.', stale: true},
          requirements: [{date: '2026-10-07', day: 'Wednesday', daypart: 'night', roles: [{role: 'Server', required: 5, typical: 4, floor: 3}], reasons: ['usual crew +25%'], leader: []}],
          hours_hourly: 312, hours_salaried: 86, hours_budget: 330,
          review: {lines: ['A starting point: Gen has no shift history of its own yet.'],
                   setup: [{kind: 'leader_rules_inactive', line: true, text: 'Your leader rules judge nobody yet.', can_adopt: true},
                           {kind: 'close_times_missing', line: true, text: 'No close time for Monday.'},
                           {kind: 'owner_rule', line: true, text: 'Your rule “two on the patio” is checked as: 2 servers'},
                           {kind: 'managers', line: false, text: 'Managers: Erik'}]}});
__o.notes = __els['sfw1-week'].innerHTML;
__o.day = sfw1DayHead('2026-10-07') + sfw1DayHead('2026-10-10');
__o.chip = sfw1ChipMarks({_pinned: 'manager_plan', _pin_reason: 'Erik usually works Mondays', dst_hours: 1});
__o.pane = sfw1ShiftExtras({_pinned: 'manager_plan', _pin_reason: 'Erik usually works Mondays'});
__o.usual = sfw1UsualSection({employee: 'Ana', date: '2026-10-09', shift_start: '5:00pm'}, [{employee: 'ana', day: 'Friday', daypart: 'night', section: 'Patio'}]);
__o.rv = sfw1ReviewHtml({stage_failures: [{line: 'The manager pass didn’t run.', blocks_publish: true}],
  unmet: [{kind: 'manager', date: '2026-10-07', day: 'Wednesday', daypart: 'morning', what: 'No manager 9–10am', why: 'Max off'}],
  budget_conflict: {over_by: 14, held: {floor: 3}, examples: [{date: '2026-10-07', day: 'Wednesday', daypart: 'night', role: 'Server', required: 5}]},
  cap_floor_conflicts: [{date: '2026-10-10', day: 'Saturday', at: '7:00pm', on: 8, cap: 6, held_by: [{kind: 'rule', label: 'two on the patio'}]}],
  unmatched_names: [{name: 'Gabe', detail: 'Gabe: salaried — matches nobody on the roster (is it Gabe Huerta?)', suggestion: 'Gabe Huerta'}]});
__o.hours = sfw1HoursHtml();
__o.pub = sfw1PubHtml({hours: {hourly: 312, salaried: 86}, notes: [{key: 'quality_weak', text: 'Quality is 58 this week.'}],
  likely_to_change: {ready: true, note: 'Flags like these were right 7 of 10 times.', rows: [{text: 'Ana on Tue 10/6/26 dinner'}]}});
__o.dims = sfw1ShiftDims({date: '2026-10-07', daypart: 'night', dimensions: [
  {key: 'coverage', label: 'Coverage', score: 60, floor: 70, customer_facing: true, facts: {}},
  {key: 'stations', label: 'Kitchen stations', score: 50, floor: 70, customer_facing: true, facts: {required: ['grill', 'fry'], assigned: {grill: 'Cy'}, gaps: ['fry']}},
  {key: 'leadership', label: 'Leadership', score: 40, floor: 60, customer_facing: true, facts: {misses: [{rule: '1 bartender', role: 'Bartender', why: 'nobody_on'}]}}]});
__o.week = sfw1WeekDimExtra({key: 'overtime', facts: {priced: true, share_pct: 3.2, people: [{name: 'Ana', bucket: '2026-10-05', overtime_hours: 5, premium: 50, avoidable: true, teammate: 'Bo'}]}})
  + sfw1WeekDimExtra({key: 'learned', facts: {misses: [{text: 'Bob is on Tuesday dinner; your managers keep taking him off it.', rows: [{employee: 'Bob', date: '2026-10-06', shift_start: '4:00pm'}]}]}});
__o.conf = sfw1ConfWhy({top_reason: 'Half the week has no Operational Score.', breakdown: [{reason: 'Half the week has no Operational Score.', points: 20}],
  reasons: ['Only 1 on the roster can meet the rule “a bartender scoring 4+”, so each shift it covers is held to 1.']});
__o.wi = sfw1WhatIfHead({ran: false, on_demand: true, reason: 'Ask for a better arrangement to look for one.'}) + '|' + sfw1WhatIfHead({ran: true, checked_with: 'availability and hours'});
__o.usd = [sfw1Dollars(84), sfw1Dollars(-12.4), sfw1Dollars(0), sfw1Dollars(null)];
""")
    n = o["notes"]
    plain = {k: re.sub(r"<[^>]+>", "", v) if isinstance(v, str) else v for k, v in o.items()}
    for piece in ("wasn’t written", 'data-sfw1-redo="2026-10-10"', "Nobody on the team can work Sunday", "A question for you",
                  "Which days and hours do Erik and Jim work?", 'data-sfw1-wk="Erik"', 'data-sfw1-wk="Jim"',
                  "1h of the week has no manager on", "Max — on approved time off", "Kay · on that day",
                  "Make acting manager", "Erik’s standing shift", "1h under the", "has no shift history",
                  "Sales through", "Count them as mine", "Set the hours", "How this week was set up", "two on the patio"):
        assert piece in n or piece in plain["notes"], piece
    assert "Managers: Erik" not in n, "a line:false setup item stays where its payload puts it"
    assert "Manager on" in o["day"] and "Not written" in o["day"]
    assert "Manager plan" in o["chip"] and "clocks" in o["chip"] and "Erik usually works Mondays" in o["pane"]
    assert o["usual"]["section"] == "Patio"
    for piece in ("Checks that didn’t run", "holds the send", "What this week doesn’t meet", "Wed", "Over the budget",
                  "held by your staffing floors", "two on the patio", 'data-person-open="Gabe Huerta"'):
        assert piece in o["rv"] or piece in plain["rv"], piece
    assert "hourly of" in o["hours"] and "salaried" in o["hours"]
    for piece in ("Hours", "Worth a look", "Quality is", "never hold the send", "Likely to change", "right"):
        assert piece in plain["pub"], piece
    for piece in ("Kitchen stations", 'class="fl" style="left:70%"', "Under its floor of", "Needs grill and fry",
                  'data-sfw1-add-role="Bartender"', "Add a bartender"):
        assert piece in o["dims"] or piece in plain["dims"], piece
    assert "into overtime" in plain["week"] and "premium is" in plain["week"] and "Bob is on Tuesday dinner" in o["week"]
    assert "What it rests on" in o["conf"] and "Only" in o["conf"]
    assert "Look for a better arrangement" in o["wi"] and "availability and hours only" in o["wi"]
    assert o["usd"] == ["+$84", "−$12", "", ""]
    shown = {k: v for k, v in plain.items() if k != "usual"}
    assert not ISO.search(json.dumps(shown, ensure_ascii=False)), "no ISO date an owner reads"
