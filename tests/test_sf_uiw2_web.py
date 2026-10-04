"""Schedule fix round 10/3/26, UI wave W2 — the web owner dashboard's
scheduling setup, team, rules, Forecast tab, Account and Home coverage card,
built on the fix round's payloads (the workstreams' UI_NEEDS: F1, F2, G, A1,
A2, D1a, D1b, E, H1).

Four halves, as the memory round's UI tests are built:

- Behaviour, run under node where it is installed: the page's own pure
  helpers (the global `sfw2*` functions in <script id="cav-sfw2">, beside
  the memory round's `mem*` ones they lean on), given the payloads the
  server sends — the words an owner reads, every date M/D/YY, a confidence
  or a late rate "—" below its floor, never "$X at 35%".
- Source rules, read from the templates: each element pinned to the field
  it reads and the route it calls.
- Backend: the small payload additions the screens needed — each salaried
  person's cap (owner only), a pairing rule said back as checked, how the
  schedule checks each staffing rule in Account → memory, a call-off's gaps
  with one cover each, the person record's certificate labels and lateness,
  the team's recent roles and strength crews, the profiles' default floors
  and a calibration's tuning.
- The issue page (/i/<token>) rendered from its template: each gap and one
  cover button per open gap.
"""
import json
import os
import re
import shutil
import subprocess
import sys
from datetime import date, timedelta

import pytest
from flask import Flask

import auth
import issues
import models
import owner_memory
import schedule_rules as sr
import schedule_setup as setup
import staff_settings as ss
import strategy_routes
from models import Restaurant, create_restaurant

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DASH = os.path.join(ROOT, "templates", "dashboard.html")
ISSUE = os.path.join(ROOT, "templates", "issue.html")
WEEK = [(date(2026, 10, 12) + timedelta(days=i)).isoformat() for i in range(7)]
OWNER = {"id": 1, "role": "client", "is_admin": 0, "username": "erik"}
MANAGER = {"id": 2, "role": "manager", "is_admin": 0, "username": "dana"}
ISO = re.compile(r"\b20\d\d-\d\d-\d\d\b")
HISTORY = {}


def _read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


@pytest.fixture(scope="module")
def page():
    return _read(DASH)


def _block(page, sid):
    i = page.index(f'<script id="{sid}">')
    return page[i:page.index("</script>", i)]


def _globals(page):
    """The memory round's helpers and this wave's: everything before each
    block's behaviour IIFE."""
    mem = _block(page, "cav-mem-wb")
    mem = mem[len('<script id="cav-mem-wb">'):mem.index("\n(function () {")]
    w2 = _block(page, "cav-sfw2")
    w2 = w2[len('<script id="cav-sfw2">'):w2.index("\n/* ── Behaviour ── */")]
    return mem + "\n" + w2


def _node(js):
    if not shutil.which("node"):
        pytest.skip("node is not installed")
    out = subprocess.run(["node", "-e", js], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr[-1500:]
    return json.loads(out.stdout.strip().splitlines()[-1])


def _run(page, calls):
    js = _globals(page) + "\nvar __o = {};\n"
    for k, expr in calls.items():
        js += f"__o[{json.dumps(k)}] = {expr};\n"
    js += "console.log(JSON.stringify(__o));"
    return _node(js)


def _fn(src, name):
    m = re.search(r"(?:function\s+" + re.escape(name) + r"\s*\(|" + re.escape(name) + r"\s*=\s*function\s*\()", src)
    assert m, f"{name} is gone from the page"
    nxt = re.search(r"\n\s{0,4}(?:function\s+\w+\s*\(|window\.\w+\s*=\s*function|/\* ──|// ──)", src[m.end():])
    return src[m.start():m.end() + (nxt.start() if nxt else len(src) - m.end())]


# ── behaviour: the words an owner reads ─────────────────────────────────────

def test_who_runs_the_floor_says_whose_word_it_is_and_why(page):
    o = _run(page, {
        "yes": 'sfw2FloorWhy({set: true, counts: true, why: "you set them as a floor manager"})',
        "no": 'sfw2FloorWhy({set: false, counts: false})',
        "auto": 'sfw2FloorWhy({set: null, counts: true, why: "their role is Owner"})',
        "dept": 'sfw2FloorWhy({set: null, counts: false, why: "Kitchen Manager manages a department, not the floor"})',
        "states": '[sfw2FloorState({set: true}), sfw2FloorState({set: false}), sfw2FloorState({set: null}), sfw2FloorState(null)]',
    })
    assert o["yes"].startswith("You set them to run the floor")
    assert o["no"].startswith("You set them as not a floor manager")
    assert o["auto"] == "Automatic: counts — their role is Owner."
    assert o["dept"].startswith("Automatic: doesn’t count — Kitchen Manager manages a department")
    assert o["states"] == ["yes", "no", "auto", "auto"]


def test_a_persons_dated_facts_read_in_mdy(page):
    o = _run(page, {
        "range": 'sfw2RangeLabel({from: "2026-10-07", until: "2026-10-09", note: "Erik away"})',
        "day": 'sfw2RangeLabel({from: "2026-10-07", until: "2026-10-07"})',
        "standing": 'sfw2StandingLabel({day: "Monday", start: "10:00am", end: "6:00pm", role: "Manager FOH", until: "2026-12-15"})',
        "both": 'sfw2StandingLabel({day: "Tuesday", start: "4:00pm", end: "11:00pm", from: "2026-10-01", until: "2026-12-15"})',
        "trainee": 'sfw2TraineeLine({target_role: "Bartender", trainer: "Ana", until: "2026-11-01", from: "2026-10-03"})',
        "live": 'sfw2TraineeActive({target_role: "Bartender", until: "2026-11-01"}, "2026-10-03")',
        "over": 'sfw2TraineeActive({target_role: "Bartender", until: "2026-09-01"}, "2026-10-03")',
    })
    assert o["range"] == "10/7/26 – 10/9/26 · Erik away"
    assert o["day"] == "10/7/26"
    assert o["standing"] == "Mon 10:00am–6:00pm · Manager FOH · until 12/15/26"
    assert o["both"] == "Tue 4:00pm–11:00pm · 10/1/26 – 12/15/26"
    assert o["trainee"].startswith("Training as Bartender until 11/1/26, with Ana (from 10/3/26)")
    assert o["live"] is True and o["over"] is False
    for k, v in o.items():
        assert not ISO.search(str(v)), (k, v)


def test_reliability_says_missed_called_out_and_late_over_clocked_shifts(page):
    o = _run(page, {
        "full": 'sfw2ReliabilityLine({shifts: 20, no_shows: 3, called_out: 1, late: 3, late_shifts: 12, late_rate: 0.25})',
        "thin": 'sfw2ReliabilityLine({shifts: 9, no_shows: 0, called_out: 0, late: 1, late_shifts: 4, late_rate: null})',
        "none": 'sfw2ReliabilityLine(null)',
        "old": 'sfw2ReliabilityLine({shifts: 9, no_shows: 2})',
        "sheet": 'memAttendanceLine({known: true, shifts: 12, missed: 1, late: 2, called_out: 1, late_shifts: 10, late_rate: null, no_show_rate: 0.083})',
        "sheet_old": 'memAttendanceLine({known: true, shifts: 12, missed: 1, late: 0, no_show_rate: 0.083, last_miss: "2026-09-12"})',
    })
    assert o["full"] == ("Missed 3 of 20 watched shifts · called out 1 time · late to 3 of 12 clocked shifts"
                         " · late rate 25%")
    assert o["thin"].endswith("late to 1 of 4 clocked shifts · late rate —"), "no rate below the floor, never 0%"
    assert "never called out" in o["thin"]
    assert o["none"].startswith("Not watched yet")
    assert o["old"] == "Missed 2 of 9 watched shifts", "an older server's payload reads as before"
    assert o["sheet"] == ("12 shifts watched · 1 missed (1 call-out) · late to 2 of 10 clocked shifts · late rate —"
                          " · 8% no-show rate")
    assert o["sheet_old"] == "12 shifts watched · 1 missed · 0 late · 8% no-show rate · last missed 9/12/26"


def test_a_maximum_past_the_ceiling_says_overtime_is_allowed(page):
    o = _run(page, {
        "ot": 'sfw2MaxNote(45, 40)', "under": 'sfw2MaxNote(38, 40)', "none": 'sfw2MaxNote(null, 40)',
        "past_low_ceiling": 'sfw2MaxNote(38, 35)', "at": 'sfw2MaxNote(40, 40)',
    })
    assert o["ot"] == "Overtime allowed up to 45h"
    assert o["under"] == o["none"] == o["at"] == ""
    assert o["past_low_ceiling"] == "Past your 35h ceiling, up to 38h"


def test_a_strength_target_is_said_per_person_against_the_largest_crew(page):
    o = _run(page, {
        "known": 'sfw2CrewLine("Bartender", 8, 2, true)',
        "guess": 'sfw2CrewLine("Server", 9, 2, false)',
        "clamped": 'sfw2PerPerson(30, 2)',
        "blank": 'sfw2CrewLine("Server", "", 2, true)',
    })
    assert o["known"] == "8 across your largest bartender crew of 2 — about 4 a person"
    assert o["guess"].startswith("about 4.5 a person on a crew of 2")
    assert o["clamped"] == 5, "held to the 1–5 scale"
    assert o["blank"] == ""


def test_what_the_draft_learned_says_how_often_and_how_sure(page):
    o = _run(page, {
        "sure": 'sfw2PatternEvidence({times: 2, opportunities: 3, confidence: 0.61})',
        "thin": 'sfw2PatternEvidence({times: 1, opportunities: 1, confidence: 0.2})',
        "retest": 'sfw2StandingEvidence({status: "retest", retest_since: "2026-10-01", opportunities: 6, hits: 5, confidence: 0.7, last_hand: "2026-09-12"})',
        "said": 'sfw2StandingEvidence({source: "owner_said", status: "active"})',
        "retired": 'sfw2StandingEvidence({status: "retired", retired_reason: "decayed", retired_at: "2026-09-30"})',
        "reasons": '[sfw2RetiredWords("reversed"), sfw2RetiredWords("retest"), sfw2RetiredWords("decayed"), sfw2RetiredWords("owner")]',
    })
    assert o["sure"] == "2 of 3 weeks · 61% confidence"
    assert o["thin"] == "1 of 1 week · confidence —", "a % only past two weeks it could have happened in"
    assert o["retest"][0] == "Left out of the next draft to check you still want it (since 10/1/26)"
    assert "5 of 6 weeks · 70% confidence" in o["retest"] and "last confirmed by hand 9/12/26" in o["retest"]
    assert o["said"] == ["You said always"]
    assert o["retired"] == ["Retired — faded, never confirmed 9/30/26"]
    assert o["reasons"] == ["reversed by hand", "not put back when tested", "faded, never confirmed", "you set it aside"]


def test_the_budget_is_the_hourly_crews_never_dollars_at_the_target(page):
    o = _run(page, {
        "basis": 'sfw2BudgetNote({text: "Your 35% labor target counts salaries: the hourly budget is the target\\u2019s dollars less the 2 salaried people\\u2019s pay."}, 35)',
        "older": 'sfw2BudgetNote(null, 35)',
        "salary": 'sfw2SalaryNote({salaried_week_cost: 5769, target_dollars: 12250})',
        "no_salary": 'sfw2SalaryNote({kind: "all_in_less_salaries"})',
        "demand": 'sfw2DemandLine({pct: 30, reasons: ["Homecoming"], weekday: "Friday"})',
        "down": 'sfw2DemandLine({pct: -15.4, reasons: ["rain forecast"], weekday: "Saturday"})',
        "flat": 'sfw2DemandLine({pct: 0.2, reasons: []})',
    })
    assert o["basis"].startswith("Your 35% labor target counts salaries")
    assert o["older"] == "Your 35% labor target, for the hourly crew"
    assert " at 35%" not in o["older"]
    assert o["salary"] == "Your target is $12,250 for the week; the salaried pay, $5,769, comes off it first."
    assert o["no_salary"] == "", "salaries' dollars only where the owner's payload carries them"
    assert o["demand"] == "+30% against a typical Friday — Homecoming"
    assert o["down"] == "−15% against a typical Saturday — rain forecast"
    assert o["flat"] == ""


def test_a_booking_export_says_what_it_read_and_left_out(page):
    o = _run(page, {"r": 'sfw2ExportReport({dates: 3, bookings: 41, covers: 1032, skipped_status: 4, skipped_unreadable: 1, late_covers: {"2026-10-09": 30, "2026-10-10": 16}})'})
    assert o["r"] == ("Read 41 bookings — 1,032 covers on 3 dates; 4 cancelled or no-show bookings left out, "
                      "1 row unreadable, 46 covers booked for 10pm or later.")


def test_a_call_off_gap_reads_its_status_and_its_one_cover(page):
    o = _run(page, {
        "stay": 'sfw2CoverLabel({employee: "Ana Bell", shift_start: "5:00pm", cover: {name: "Lu Park", kind: "stay"}})',
        "off": 'sfw2CoverLabel({employee: "Bo Cole", shift_start: "5:00pm", cover: {name: "Pat Diaz", kind: "off"}})',
        "none": 'sfw2CoverLabel({employee: "Bo Cole", cover: null})',
        "covered": 'sfw2GapStatus({status: "covered", covered_by: "Lu"})',
        "missing": 'sfw2GapStatus({status: "missing"})',
    })
    assert o["stay"] == "Ask Lu to stay on for Ana’s 5:00pm"
    assert o["off"] == "Ask Pat to cover Bo’s 5:00pm"
    assert o["none"] == "" and o["covered"] == "covered by Lu" and o["missing"] == "missing"


def test_the_rules_screen_names_its_conflicts_in_words(page):
    o = _run(page, {
        "cap": 'sfw2CapConflictLine({day: "Saturday", daypart: "night", floor: 8, cap: 6, roles: ["Server PM"]})',
        "close": 'sfw2CloseConflictLine({role: "Bartender", stays: 30, may_run: 60})',
        "basis": 'sfw2CloserBasis("history", ["Bartender", "Barback"])',
        "nobody": 'sfw2CloserBasis("all", [])',
        "check": 'sfw2RuleCheck({checked: false})',
    })
    assert o["cap"].startswith("Saturday dinner: your floors ask for 8 (Server PM) but you have 6 sections")
    assert o["close"].startswith("Bartender: stays 30 minutes after close, may run 60")
    assert o["basis"] == ("The closer rule holds for Bartender and Barback — from your punches — choose the roles "
                          "that close to make it yours.")
    assert o["nobody"].startswith("Nobody is marked to close yet")
    assert o["check"].startswith("Not checked by the schedule")


# ── source rules: each element pinned to its field and its route ───────────

def test_the_roster_row_carries_the_persons_scheduling_facts(page):
    r = _fn(page, "renderRoster")
    for piece in ("window._sfw2Roster=d", "ch.certification_labels", "can_edit_owner_facts", "sfw2RosterFacts(e,ro,ownerRo)",
                  "sfw2RosterPills(e)", "sfw2RosterAlso(e)", "sfw2RosterAfter(e,ro)", "sfw2MaxNote(st.max_hours,ceiling)",
                  "weekly_hours_ceiling"):
        assert piece in r, piece
    b = _block(page, "cav-sfw2")
    facts = _fn(b, "sfw2RosterFacts")
    for piece in ("r.floor_manager", "st.acting_manager", "st.standing_shifts", "st.trainee", "st.closes_for",
                  "r.reliability", "data-sfw2-fm", "cavTimeOptions(", 'type="date"'):
        assert piece in facts, piece
    assert "r.dormant_text" in _fn(b, "sfw2RosterAfter") and "data-sfw2-dorm" in _fn(b, "sfw2RosterAfter")
    assert "r.recent_roles" in _fn(b, "sfw2RosterAlso")
    for piece in ("'/api/labor/managers', {name: name, floor_manager:", "'/api/labor/staff-settings', patch",
                  "{acting_manager: list}", "{standing_shifts: list}", "{trainee: tr}", "{trainee: {}}",
                  "{closes_for: next}", "{employee_name: name, active: !off}"):
        assert piece in b, piece
    assert "<input type=\"time\"" not in b and "type=time" not in b, "a time of day is a 15-minute select"


def test_the_rules_screen_says_back_the_setup_and_saves_the_new_settings(page):
    r = _fn(page, "renderRules")
    for piece in ("sfw2RulesTop(d,ro)", "sfw2RulesClosed(d)", "sfw2RulesFamilies()", "sfw2RulesFloorsHead(d,ro)",
                  "sfw2RulesStays(d)", "sfw2RulesSalaried(d,ro)", "sfw2RulesStandards()", "sfw2RulesAfter(d,ro)"):
        assert piece in r, piece
    assert "['min_shift_hours','Shortest shift'" in page and "['full_time_min_hours','Full-time means at least'" in page
    save = page[page.index("window.saveRules=function"):]
    save = save[:save.index("\n  };")]
    # A blank goes back to the default by name: a save changes only what it
    # sends (schedule re-audit 10/4/26 UI-2), so leaving it out now keeps
    # the stored value instead of resetting it.
    assert "if(v===''){if(rk==='min_shift_hours')rules[rk]=null;else reset.push(rk);continue;}" in save, \
        "a blank goes back to the default"
    assert "rules_default:reset" in save
    assert "body.role_close_mins=sm" in save and "body.salaried_cap=" in save
    b = _block(page, "cav-sfw2")
    top = _fn(b, "sfw2RulesTop")
    for piece in ("d.managers", "ms.not_counted", "ms.acting", "ms.ask_standing", "data-sfw2-mgr"):
        assert piece in top, piece
    for piece in ("d.close_times_missing", "d.floor_cap_conflicts", "'/api/labor/floors/suggest'", "d.role_close_conflicts",
                  "d.role_close_mins", "d.salaried_cap", "d.salaried_caps", "'/api/labor/role-families'",
                  "'/api/labor/role-families', {families: map}", "'/api/labor/labor-standards'",
                  "'/api/labor/labor-standards', body", "data-nav-go=\"account/restaurant\""):
        assert piece in b, piece


def test_the_closers_row_reads_and_applies_the_cleanup(page):
    assert 'data-nav="labor/closers"' in page and 'id="closers-body"' in page and "toggleClosersPanel()" in page
    b = _block(page, "cav-sfw2")
    for piece in ("'/api/labor/closers'", "d.warning", "d.closer_roles_basis", "d.closer_roles_in_force", "d.by_role",
                  "d.suggestions", "d.pending_admin", "d.outside_roles", "'/api/labor/closers', body",
                  "{name: s.name, can_close: s.action === 'add'}", "body.closer_roles = roles",
                  "'/api/labor/team/ratings/adopt'"):
        assert piece in b, piece


def test_the_team_panel_rates_by_role_and_says_each_targets_crew(page):
    assert '<option value="4" selected>4</option>' in page, "a new leader rule's bar starts at 4 (D-11)"
    load = _fn(page, "loadTeamPanel")
    for piece in ("d.leader_rule_defaults", "lrd.min_score", "d.strength_crews", "d.leader_rules_status"):
        assert piece in load, piece
    i = page.index("function _teamRoleRows(m, idx)")
    rows = page[i:page.index("\n}\n", i)]
    for piece in ("m.rating_due_text", "m.rated_label", "m.role_scores", "m.recent_roles", "data-sfw2-still"):
        assert piece in rows, piece
    assert "body.role = role" in page and "'/api/labor/team/rating'" in page
    assert "cov.due_for_rerate" in _fn(page, "renderTeamCoverage")
    assert "_teamCrewLine(role, current)" in _fn(page, "renderTeamThresholds")
    save = _fn(page, "saveTeamTargets")
    assert "d.leader_rule_warnings" in save and "d.strength_crews" in save
    assert "_teamRuleStatus.lines" in _fn(page, "renderTeamRules")
    assert "The combined score of everyone in a role on one shift" not in page, "the old sum copy is gone"


def test_the_profile_editor_edits_floors_and_says_a_calibration_tuned_it(page):
    assert "_critFloors = d.critical_floors" in _fn(page, "loadShiftProfiles")
    assert "_spFloors(p)" in _fn(page, "_profileEditor")
    assert "floors: _spCollectFloors(existing)" in _fn(page, "collectShiftProfile")
    lst = _fn(page, "renderProfileList")
    assert "p.tuned" in lst and "Tuned by calibration" in lst and "p.floors" in lst
    for key in ("coverage", "operational_strength", "leadership", "coverage_curve", "stations"):
        assert f"'{key}'" in page[page.index("var _FLOOR_KEYS"):page.index("var _FLOOR_KEYS") + 200]


def test_what_the_draft_learned_shows_its_evidence_and_supports_work(page):
    lrn = page[page.index("function _lrnRender"):page.index("function _rstActive")]
    for piece in ("sfw2PatternEvidence(p)", "p.dismissed_by_admin", "data-sfw2-adopt-dis", "sfw2LearnedHead(d)"):
        assert piece in lrn, piece
    b = _block(page, "cav-sfw2")
    for piece in ("d.admin_saves", "d.can_adopt", "'/api/labor/schedule/adopt-admin-saves'",
                  "'/api/labor/learned-patterns/adopt'", "{key: t.getAttribute('data-key'), keep: keep}"):
        assert piece in b, piece
    mem = _block(page, "cav-mem-wb")
    kept = mem[mem.index("window.memLearnedHtml"):mem.index("/* ── Labor: rating and capability history")]
    for piece in ("sfw2StandingEvidence(s)", "s.status === 'retest'", "data-sfw2-keep=\"1\"", "data-sfw2-keep=\"0\"",
                  "s.rule.note", "s.can_be_rule"):
        assert piece in kept, piece
    assert "d.rule ? 'Now a rule: ' + d.rule" in mem


def test_the_calibration_shows_floors_and_bars_and_applies_them_in_one_tap(page):
    learn = page[page.index("function _intLearnHtml(d)"):page.index("window.renderIntel=function")]
    for piece in ("cal.profiles", "cal.moving_profiles", "cal.fit", "left_out", "sfw2CalMove(pe.bar)",
                  "data-sfw2-cal-apply", "'/api/labor/quality/calibration/apply'", "d.profiles"):
        assert piece in learn, piece
    intel = page[page.index("window.renderIntel=function"):page.index("// ── Reviews ──")]
    assert "from_punches" in intel and "rot.from_punches" in intel


def test_the_forecast_tab_and_par_say_the_hourly_budget(page):
    fc = _fn(page, "ssForecastHtml")
    for piece in ("d.budget_basis", "sfw2BudgetNote(", "sfw2SalaryNote(bb)", "bb.caveat", "x.demand", "x.demand.pct",
                  "d.daily_target_reasons", "ddt.line", "ddt.message", "data-nav-go=\"account/restaurant\""):
        assert piece in fc, piece
    assert "'</div><div class=\"n\">at ' + tgt" not in fc, "never \"$X at 35%\""
    assert "Labor budget (<span class=\"hb-num\">" not in page
    par = page[page.index("var par = document.getElementById('sw-par')"):]
    par = par[:par.index("swLive();")]
    assert "hourly budget" in par and "data.budget_basis" in par and "</span> at <span" not in par


def test_events_import_a_booking_export_and_say_what_it_read(page):
    assert 'id="sg-export"' in page and "Import your reservation system\\u2019s booking export" in page
    b = _block(page, "cav-sfw2")
    for piece in ("t.id !== 'sg-export'", "new FileReader()", "rd.readAsText(f)", "{csv: String(rd.result || '')}",
                  "sfw2ExportReport(d.report)"):
        assert piece in b, piece
    assert "d.report&&window.sfw2ExportReport" in _fn(page, "window.importSignals")


def test_account_says_how_each_staffing_rule_is_checked_and_links_salaried_names(page):
    mem = _fn(page, "loadMemory")
    assert "d.schedule_checks" in mem and "sfw2MemCheckHtml(chk)" in mem
    assert "d.schedule_rule.text" in _fn(page, "acctRemember")
    sal = _fn(page, "tgRenderSalaried")
    for piece in ("sal[i].warning", "sal[i].suggestion", "t.salaried_warnings", "data-tg-sal-link"):
        assert piece in sal, piece
    assert "salaried_remove: lk.getAttribute('data-tg-sal-link'), salaried_add: {name: lk.getAttribute('data-to')" in page


def test_home_lists_a_call_offs_gaps_with_one_cover_each(page):
    rows = _fn(page, "hbIssueRows")
    assert "sfw2GapsHtml(x,j>=4)" in rows
    assert "(x.cover_gaps||[]).length>1" in _fn(page, "hbCoverButtons")
    gaps = _fn(_block(page, "cav-sfw2"), "sfw2GapsHtml")
    for piece in ("x.cover_gaps", "g.cover", "data-cover-issue", "data-cover-name", "sfw2CoverLabel(g)", "hb-iss-more"):
        assert piece in gaps, piece


def test_time_off_reads_the_part_of_the_day(page):
    assert "q.span_label||" in _fn(page, "lb2LoadTimeOff")
    wait = page[page.index("function waitRender()"):page.index("function waitFocus(")]
    assert "q.span_label ||" in wait


def test_the_person_sheet_labels_certificates_and_says_overtime(page):
    render = page[page.index("function personRender"):page.index("function ppSave")]
    assert "ch.certification_labels" in render and "sfw2MaxNote(hr.max_hours" in render


def test_new_patterns_are_documented():
    ds = _read(os.path.join(ROOT, "DESIGN_SYSTEM.md"))
    for piece in ("Scheduling setup (schedule fix round, 10/3/26)", "Runs the floor", "Closers", "cav-sfw2"):
        assert piece in ds, piece


# ── backend: what the screens read ─────────────────────────────────────────

@pytest.fixture
def _db(db_path, monkeypatch):
    real = models.get_conn

    def conn(*a, **k):
        return real(db_path)
    for mod in list(sys.modules.values()):
        if mod is not None and getattr(mod, "get_conn", None) is real:
            monkeypatch.setattr(mod, "get_conn", conn)
    monkeypatch.setattr(models, "get_conn", conn)
    monkeypatch.setattr(models, "DB_PATH", db_path)
    monkeypatch.setattr(auth, "DB_PATH", db_path)
    auth.init_auth(db_path=db_path)
    HISTORY.clear()
    monkeypatch.setattr(models, "_cached_shifts", lambda r: HISTORY.get(r, []))
    monkeypatch.setattr(setup, "coming_week", lambda rid: list(WEEK))
    yield db_path


def _rid(**cols):
    rid = create_restaurant(Restaurant(name="W2 Grill", owner_email="w2@x.test", timezone="America/Chicago"))
    if cols:
        models.update_restaurant(rid, cols)
    return rid


def _u(base, rid):
    return dict(base, restaurant_id=rid)


def _call(fn, u, body=None, *args, query=None):
    with Flask(__name__).test_request_context(json=body or {}, query_string=query or {}):
        return fn(u, *args)


def _team(rid, people):
    for n, role in people:
        models.add_manual_team_member(rid, n, role=role)


def test_the_rules_screen_lists_each_salaried_persons_cap_for_the_owner_only(_db):
    rid = _rid(salaried_staff_json=json.dumps([{"name": "Erik", "annual": 150000}, {"name": "Jim", "annual": 150000}]))
    _team(rid, [("Erik", "Owner"), ("Jim", "Owner"), ("Ana", "Server")])
    ss.upsert(rid, "Jim", max_hours=60)
    out, status = _call(strategy_routes._do_compliance_get, _u(OWNER, rid))
    assert status == 200
    caps = {c["name"]: c for c in out["salaried_caps"]}
    assert caps["Erik"] == {"name": "Erik", "cap": 55.0, "own": False}
    assert caps["Jim"] == {"name": "Jim", "cap": 60.0, "own": True}
    assert "Ana" not in caps
    out, status = _call(strategy_routes._do_compliance_get, _u(MANAGER, rid))
    assert status == 200 and "salaried_caps" not in out, "who is paid a salary is the owner's"


def test_a_pairing_rule_is_said_back_as_checked(_db):
    rid = _rid()
    _team(rid, [("Ana Bell", "Server"), ("Bo Cole", "Server")])
    p = setup.owner_rule_preview(rid, "Keep Ana Bell and Bo Cole apart")
    assert p == {"checked": True, "reads_as": "Ana Bell and Bo Cole kept apart",
                 "text": "Checked on every draft as: Ana Bell and Bo Cole kept apart."}
    assert setup.owner_rule_preview(rid, "Always schedule Ana Bell with Bo Cole")["reads_as"] == \
        "Ana Bell and Bo Cole on together where the week allows"
    assert setup.owner_rule_preview(rid, "Keep it fair for the new hires")["checked"] is False


def test_account_memory_says_how_the_schedule_checks_each_staffing_rule(_db):
    rid = _rid(module_labor=1)
    _team(rid, [("A", "Server AM"), ("B", "Server PM")])
    owner = _u(OWNER, rid)
    held = owner_memory.remember(rid, "Always two servers on Saturday night", kind="constraint",
                                 modules=["schedule"], user=owner, audience="team", audience_chosen=True)
    private = owner_memory.remember(rid, "Keep the schedule friendly for the new hires", kind="constraint",
                                    modules=["labor"], user=owner, audience="principals", audience_chosen=True)
    background = owner_memory.remember(rid, "We cater on Sundays", kind="context", modules=["labor"], user=owner)
    out, status = _call(strategy_routes._do_memory_list, owner)
    assert status == 200
    checks = out["schedule_checks"]
    assert checks[str(held["id"])]["checked"] is True
    assert checks[str(held["id"])]["reads_as"] == "at least 2 Server PM on Sat at dinner/night"
    assert checks[str(private["id"])]["checked"] is False, "the owner-only rule the schedule can't read is said"
    assert str(background["id"]) not in checks, "only the owner's staffing rules"
    out, status = _call(strategy_routes._do_memory_list, owner, query={"archive_before": "999999"})
    assert out["schedule_checks"] == {}, "an archive page reads no rules"


def _coverage_issue(rid, people, covers, asked=None, status="open"):
    meta = {"business_date": "2026-10-09", "role": "server", "family": "server", "people": people, "covers": covers,
            "scheduled_in_role": 6, "missing": people[0]["employee"], "shift_start": people[0]["shift_start"]}
    if asked:
        meta["asked"] = asked
    return {"id": 7, "kind": "coverage", "status": status, "meta": meta,
            "source_key": issues.coverage_key("2026-10-09", "server")}


def test_a_call_off_offers_one_cover_per_open_gap():
    iss = _coverage_issue(
        rid=1,
        people=[{"employee": "Ana Bell", "shift_start": "5:00pm", "status": "missing"},
                {"employee": "Bo Cole", "shift_start": "5:00pm", "status": "missing"},
                {"employee": "Cy Dunn", "shift_start": "4:00pm", "status": "covered", "covered_by": "Lu"},
                {"employee": "Di Eve", "shift_start": "4:00pm", "status": "arrived"}],
        covers=[{"name": "Lu Park", "kind": "stay", "how": "ends at 5pm", "for": "Ana Bell", "shift_start": "5:00pm"},
                {"name": "Pat Diaz", "kind": "off", "for": "Bo Cole", "shift_start": "5:00pm"},
                {"name": "Mo Fox", "kind": "off", "for": "Ana Bell", "shift_start": "5:00pm"},
                {"name": "Kay Gil", "kind": "off", "for": "Bo Cole", "shift_start": "5:00pm"}],
        asked=[{"name": "Pat Diaz"}])
    gaps = issues.cover_gaps(iss)
    by = {g["employee"]: g for g in gaps}
    assert [g["employee"] for g in gaps] == ["Ana Bell", "Bo Cole", "Cy Dunn", "Di Eve"]
    assert by["Ana Bell"]["cover"]["name"] == "Lu Park" and by["Ana Bell"]["cover"]["kind"] == "stay"
    assert by["Bo Cole"]["cover"]["name"] == "Kay Gil", "the next not yet asked, for this gap"
    assert by["Bo Cole"]["asked"] == ["Pat Diaz"]
    assert by["Cy Dunn"]["cover"] is None and by["Cy Dunn"]["covered_by"] == "Lu"
    assert by["Di Eve"]["cover"] is None and by["Di Eve"]["status"] == "arrived"
    shown = strategy_routes.askable_covers(iss)
    assert [c["name"] for c in shown] == ["Lu Park", "Kay Gil"], "one per open gap, not the first two names"
    assert issues.cover_gaps(dict(iss, status="resolved"))[0]["cover"] is None
    assert strategy_routes.askable_covers(dict(iss, status="resolved")) == []


def test_a_one_person_issue_still_offers_its_two_covers():
    iss = {"id": 3, "kind": "coverage", "status": "open", "source_key": "coverage:2026-10-09:Ana Bell",
           "meta": {"missing": "Ana Bell", "shift_start": "5:00pm",
                    "covers": [{"name": "Lu Park"}, {"name": "Mo Fox"}, {"name": "Kay Gil"}]}}
    gaps = issues.cover_gaps(iss)
    assert len(gaps) == 1 and gaps[0]["cover"]["name"] == "Lu Park"
    assert [c["name"] for c in strategy_routes.askable_covers(iss)] == ["Lu Park", "Mo Fox"]
    assert issues.cover_gaps({"kind": "manual"}) == []


def test_the_issue_page_lists_each_gap_with_one_cover_button():
    from jinja2 import Environment, FileSystemLoader
    env = Environment(loader=FileSystemLoader(os.path.join(ROOT, "templates")), autoescape=True)
    env.filters["format_date"] = lambda v: "10/9/26"
    iss = _coverage_issue(
        rid=1,
        people=[{"employee": "Ana Bell", "shift_start": "5:00pm", "status": "missing"},
                {"employee": "Bo Cole", "shift_start": "5:00pm", "status": "missing"},
                {"employee": "Cy Dunn", "shift_start": "4:00pm", "status": "covered", "covered_by": "Lu"}],
        covers=[{"name": "Lu Park", "kind": "stay", "how": "ends at 5pm", "for": "Ana Bell", "shift_start": "5:00pm"},
                {"name": "Pat Diaz", "kind": "off", "for": "Bo Cole", "shift_start": "5:00pm"}])
    iss.update(title="2 of 6 servers haven't clocked in", restaurant_name="W2 Grill", severity="high",
               detail="", created_at="2026-10-09 22:10:00", assignee_name="Dana")
    html = env.get_template("issue.html").render(issue=iss, token="tok", done=None, gaps=issues.cover_gaps(iss))
    assert "Ask Lu to stay on for Ana&#39;s 5:00pm" in html or "Ask Lu to stay on for Ana's 5:00pm" in html
    assert "Ask Pat to cover Bo&#39;s 5:00pm" in html or "Ask Pat to cover Bo's 5:00pm" in html
    assert html.count('name="action" value="ask_cover"') == 2
    assert "covered by Lu" in html and "ends at 5pm" in html
    one = _coverage_issue(1, [{"employee": "Ana Bell", "shift_start": "5:00pm", "status": "missing"}],
                          [{"name": "Lu Park", "for": "Ana Bell"}])
    one.update(title="Ana Bell hasn't clocked in", restaurant_name="W2 Grill", severity="high", detail="",
               created_at="2026-10-09 22:10:00")
    html = env.get_template("issue.html").render(issue=one, token="tok", done=None, gaps=issues.cover_gaps(one))
    assert "Ask Lu Park to cover" in html, "a one-person issue keeps its list"


def test_the_route_and_the_page_read_the_same_gaps():
    src = _read(os.path.join(ROOT, "strategy_routes.py"))
    lst = src[src.index("def _do_issues_list"):src.index("def _do_issue_create")]
    assert 'row["cover_gaps"] = issues.cover_gaps(row)' in lst
    page = src[src.index("def issue_page"):]
    assert "gaps = issues.cover_gaps(issue)" in page and "gaps=gaps" in page


def test_the_person_record_labels_certificates_and_carries_lateness(_db, monkeypatch):
    import people
    rid = _rid()
    _team(rid, [("Ana Bell", "Server")])
    monkeypatch.setattr(ss, "reliability", lambda *a, **k: {"Ana Bell": {
        "shifts": 12, "no_shows": 1, "called_out": 1, "late": 2, "late_shifts": 10, "late_rate": None,
        "no_show_rate": 0.08, "unreliable": False, "last_miss": "2026-09-12"}})
    p = people.get_person(rid, "ana-bell")
    assert p["choices"]["certification_labels"]["manager"] == "Floor manager (can run the shift)"
    a = p["attendance"]
    assert (a["called_out"], a["late"], a["late_shifts"], a["late_rate"]) == (1, 2, 10, None)


def test_strength_crews_are_read_as_the_scorer_reads_them(_db):
    rid = _rid(role_floors_json=json.dumps({"Bartender": {"morning": 1, "night": 2}}))
    _team(rid, [("Ann", "Bartender"), ("Bo", "Bartender"), ("Cy", "Server")])
    out = setup.strength_crews_view(rid, {"Bartender": 8})
    assert out["Bartender"]["crew"] == 2 and out["Bartender"]["per_person"] == 4.0 and out["Bartender"]["known"]
    guess = setup.strength_crews_view(rid, {"Host": 9})
    assert guess["Host"]["known"] is False and guess["Host"]["crew"] == 2, "the fewest people who could reach it"
    assert setup.strength_crews_view(rid, {}) == {}


def test_the_team_and_profiles_payloads_carry_what_the_editors_read():
    src = _read(os.path.join(ROOT, "mobile_api.py"))
    team = src[src.index("def mobile_labor_team"):src.index("def mobile_adopt_ratings")]
    assert '"recent_roles": e["recent_roles"]' in team and "strength_crews=_crews" in team
    thr = src[src.index("def mobile_set_thresholds"):]
    thr = thr[:thr.index("\n@mobile_bp")]
    assert "strength_crews=crews" in thr
    prof = src[src.index("def mobile_shift_profiles"):src.index("def mobile_save_shift_profile")]
    assert "tuning=get_quality_tuning(rid)" in prof
    assert "critical_floors=dict(_sq.CRITICAL_FLOORS, stations=_sq.STATIONS_FLOOR)" in prof


def test_a_tuned_built_in_says_so_and_shows_its_floors():
    import shift_quality as sq
    built = sq.profiles_from_config(None, tuning={"default": {"min_quality": 74, "floors": {"coverage": 65}}})
    d = [sq.profile_to_dict(p) for p in built if p.key == "default"]
    if not d:
        pytest.skip("no built-in keyed default in this build")
    assert d[0]["tuned"] == {"min_quality": 74, "floors": {"coverage": 65}}
    assert d[0]["floors"]["coverage"] == 65 and d[0]["min_quality"] == 74
