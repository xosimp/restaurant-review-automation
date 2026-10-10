"""Schedule fix round 10/10/26, workstream I1 — the web pre-publish review
(RM-6), run through node with a small DOM shim: the payload drawn, Send held
until every must-fix item is fixed or overridden on its own and every
decision that holds the send is answered (TRUST-2, TRUST-3, ARCH-30), the
Send body naming exactly what was read, a fix applied to the rows on screen,
and the source rules the screen keeps (TRUST-4, 6, 7, 16, 17, 22, 23, 31, 32).
"""
import json
import re
import shutil
import subprocess

import pytest

SRC = open("templates/dashboard.html", encoding="utf-8").read()


def _between(a, b):
    i = SRC.index(a)
    return SRC[i:SRC.index(b, i)]


def _pr_script():
    return re.search(r'<script id="cav-pr">(.*?)</script>', SRC, re.S).group(1)


SHIM = r"""
var els = {};
function El(id) { this.id = id; this.innerHTML = ''; this.style = {}; this.hidden = false; }
var document = {
  getElementById: function (id) { return els[id] || null; },
  addEventListener: function (t, f) { (document._h[t] = document._h[t] || []).push(f); }, _h: {}
};
var window = this; window.document = document;
els['pr-review'] = new El('pr-review');
function _escHtml(s) { return String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/"/g, '&quot;'); }
function _escAttr(s) { return _escHtml(s); }
function _numText(s) { return _escHtml(s); }
function _schedMoney(n) { return '$' + Math.round(+n || 0); }
function _schedHrs(n) { return String(n); }
function toast() {} function apiJson(r) { return r; } function cbtnBusy() { return function () {}; }
var _schedHistoryId = 7, _schedRows = [], _schedDirty = false, _ssOpen = false, _ssStage = 'publish';
var _schedOverrides = {}; var fetched = [];
function fetch(u, o) { fetched.push(u); return {then: function () { return {then: function () { return {catch: function () {}}; }}; }}; }
function psLabel() {} function _schedReadOnly() { return false; }
function _schedHoursBetween(a, b) { return '6'; } function _schedDayOf(d) { return 'Tuesday'; }
function _schedPendRecheck(r) { r.review_pending = true; } function _schedSortRows() {}
var edited = 0; function schedAfterEdit() { edited++; }
"""

PAYLOAD = {
    "ok": True, "schedule_id": 7, "week_start": "2026-10-12", "week_end": "2026-10-18",
    "verdict": {"tone": "red", "sentence": "Not ready: 10/13/26 — no manager on Tuesday from 4:00pm to 10:00pm.",
                "subline": "Week of 10/12/26 – 10/18/26"},
    "must_fix": [
        {"key": "rule:no_manager:2026-10-13:960", "kind": "rule:no_manager", "text": "no manager Tue",
         "fixes": [{"label": "Extend Erik to 10:00pm", "action": "edit_rows",
                    "payload": {"change": [{"match": {"shift_id": "abc", "employee": "Erik", "date": "2026-10-13",
                                                      "shift_start": "10:00am"},
                                            "set": {"shift_end": "10:00pm"}}]}}],
         "override_allowed": True, "override": {"needs": "covered_by", "candidates": ["Ana", "Erik"]}},
        {"key": "rule:minor_late:maya:2026-10-16:5:00pm", "kind": "rule:minor_late", "text": "Maya late",
         "fixes": [], "override_allowed": True, "override": {"needs": "reason", "candidates": []}}],
    "decide": [
        {"key": "hours_over", "kind": "hours_over", "text": "Over budget", "holds": True,
         "options": [{"label": "Trim it", "scope": "week", "action": "optimize", "payload": {}},
                     {"label": "Keep it this week", "scope": "week", "action": "acknowledge",
                      "payload": {"key": "hours_over"}}]},
        {"key": "crew:server:Monday:night", "kind": "crew", "text": "Monday dinner lean", "holds": False,
         "options": [{"label": "3 is fine on Monday dinners", "scope": "every", "action": "post",
                      "payload": {"path": "labor/crew-matrix", "body": {"answer": "lean"}}}]}],
    "fyi": [{"key": "soft:meal_break", "kind": "meal_break", "text": "11 shifts: meal", "count": 11}],
    "numbers": {"labor_pct": {"value": 31.2, "low": 29.0, "high": 33.5, "basis": "all_in", "label": "All-in labor",
                              "target": 30, "target_basis": "all_in", "grade": "over",
                              "track_record": "Our sales forecasts here were within 8% on 22 of the last 26 weeks."},
                "cost": {"text": "$18420 hourly"}, "overtime": {"hours": None, "priced": False,
                                                                "text": "Not priced for this week"}},
    "quality": {"score": 82, "grade": "good", "lead": None, "text": "82/100 — better than most of your last 12 weeks."},
    "changed": {"vs_last_week": ["+6 shifts"], "vs_usual": [], "vs_last_draft": [], "cavnar_changes": []},
    "fragile": [{"key": "standby:x", "kind": "standby", "text": "A standby is needed on Fri dinner.",
                 "action": {"label": "Ask Lee", "action": "post", "payload": {"path": "labor/standby/ask", "body": {}}}}],
    "why": {"assignments": [{"text": "Fri close: Ana over Ben: rated 4/5 against 3/5."}],
            "left_off": [{"name": "Rosa", "reason": "time off 10/12/26–10/18/26"}]},
    "sure": {"confidence": {"pct": 88, "basis": ["drafts kept unchanged: 91% over 6 weeks"]},
             "lines": ["Your last 6 drafts went out with 91% of shifts unchanged."]},
    "send": {"reach": {"app": 15, "text": 4, "email": 2, "portal_only": 2, "total": 23, "reachable": 21},
             "label": "Send to 21 staff", "pos_push": None},
    "counts": {"must_fix": 2, "decide": 2, "decide_holding": 1, "fyi": 1, "warnings": 4},
}


def _run(js):
    if not shutil.which("node"):
        pytest.skip("node is not installed")
    out = subprocess.run(["node", "-"], input=js, capture_output=True, text=True)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout.strip().splitlines()[-1])


def test_the_review_draws_every_section_and_holds_send_until_each_item_is_answered():
    js = SHIM + _pr_script() + """
var S = window._prState; S.hid = 7; S.data = %s;
var out = {};
window.prRender(); out.html = els['pr-review'].innerHTML;
out.g0 = prGate();
S.overrides['rule:minor_late:maya:2026-10-16:5:00pm'] = {reason: 'Her parent signed the permit', covered_by: ''};
out.g1 = prGate();
S.overrides['rule:no_manager:2026-10-13:960'] = {reason: '', covered_by: ''};
out.g2 = prGate();
S.overrides['rule:no_manager:2026-10-13:960'] = {reason: '', covered_by: 'Erik'};
S.answers['hours_over'] = 'Keep it this week';
out.g3 = prGate();
out.body = prSendBody();
console.log(JSON.stringify(out));
""" % json.dumps(PAYLOAD)
    got = _run(js)
    h = got["html"]
    for bit in ("pr-verdict red", "Not ready:", "Must fix before sending", "Extend Erik to 10:00pm",
                "Send anyway", "Decide", "standing", "All-in labor of forecast sales", "likely",
                "within 8% on 22 of the last 26 weeks", "Not priced for this week", "What changed",
                "Where this week is fragile", "Ask Lee", "What Cavnar AI weighed", "Left off this week", "Rosa",
                "How sure we are", "88%", "Worth a look, never holding the send"):
        assert bit in h, bit
    assert 'class="hb-num bad">31.2%' in h          # the server's grade: over the target is red (TRUST-7)
    assert got["g0"] == {"loaded": True, "busy": False, "must": 2, "decide": 1, "overriding": 0, "ready": False}
    assert got["g1"]["must"] == 1 and got["g1"]["overriding"] == 1
    # A manager gap is overridden only by naming who runs the floor (ARCH-30).
    assert got["g2"]["must"] == 1
    assert got["g3"]["ready"] is True and got["g3"]["overriding"] == 2
    assert got["body"]["acknowledge"] == ["hours_over"]
    assert {o["key"]: o["covered_by"] for o in got["body"]["overrides"]}["rule:no_manager:2026-10-13:960"] == "Erik"


def test_a_fix_is_an_edit_to_the_rows_on_screen_and_rechecked():
    js = SHIM + _pr_script() + """
_schedRows = [{shift_id: 'abc', employee: 'Erik', date: '2026-10-13', shift_start: '10:00am', shift_end: '4:00pm'}];
var ok = prApplyEdit({change: [{match: {shift_id: 'abc'}, set: {shift_end: '10:00pm'}}],
                      add: [{date: '2026-10-14', employee: 'Gabe', role: 'Manager', shift_start: '11:00am', shift_end: '3:00pm'}]});
console.log(JSON.stringify({ok: ok, rows: _schedRows, edited: edited, over: _schedOverrides}));
"""
    got = _run(js)
    assert got["ok"] and got["edited"] == 1
    assert got["rows"][0]["shift_end"] == "10:00pm" and got["rows"][0]["review_pending"] is True
    assert got["rows"][1]["employee"] == "Gabe" and got["rows"][1]["day"] == "Tuesday"
    assert set(got["over"]) == {"0", "1"}


# ── the source rules ───────────────────────────────────────────────────────

def test_send_names_what_was_read_and_a_409_rereads_the_review():
    send = _between("function publishScheduleNow(ack){", "/* A blocker list as the server sends it")
    assert "var body = {acknowledge: _pb.acknowledge, overrides: _pb.overrides};" in send
    assert "_psRenderBlockers" not in send and "prRefresh()" in send


def test_the_summary_leads_with_the_one_verdict_and_graded_tiles():
    f = _between("function ssRenderSummary() {", "\nfunction ssOptimize")
    assert "Schedule complete" not in f and "prVerdictHtml(pd, 'h1')" in f            # TRUST-6
    assert "tgt + 0.5" not in f and "lv.grade" in f                                   # TRUST-7
    assert "Nobody past" not in f and "'Not priced for this week'" in f and "'No overtime'" in f   # TRUST-32
    assert "pq.grade === 'good'" in f and "sc >= 80" not in f                         # TRUST-29
    assert "cnt.must_fix" in f and "hard + soft" not in f                             # TRUST-22
    assert "(lv && lv.label) || 'Labor'" in f                                          # TRUST-30


def test_build_steps_and_the_rest():
    steps = _between("function renderScheduleSteps(startedAt) {", "\n}\n")
    assert "_schedProgress.stages" in steps and "elapsed" not in steps                 # TRUST-16
    gen = _between("function generateSchedule(btn, opts) {", "  var panel = document.getElementById('schedule-preview-panel');")
    assert "Keep your" in gen and "body.keep_locked = false" in gen                    # TRUST-17
    assert "ssToggleLock(" in SRC and "r.lock = r.locked" in SRC
    rc = _between("function _schedRecheck() {", "\n}\n")
    assert "_schedRecheckSettled(false)" in rc                                          # TRUST-23
    drop = _between("document.addEventListener('drop', function (ev) {", "\n});")
    assert "needs_review = false" not in drop and "_schedPendRecheck(r)" in drop
    assert "var SCHED_PART_WORD = {morning: 'Lunch', night: 'Dinner'};" in SRC         # TRUST-31
    assert "'Daytime'" not in SRC and "_DP_NAMES={morning:'lunch',night:'dinner'}" in SRC
    od = _between("  window.openScheduleDraft = function (hid, opts) {", "  window.schedOpenInEditor")
    assert "standby_days: d.standby_days || []" in od                                  # TRUST-25
    assert "violations: data.rule_violations || []" in SRC
    shift = _between("function ssRenderShift() {", "\nfunction ssToggleLock")
    assert "What Cavnar AI weighed" in shift and "d.weighed" in shift                  # TRUST-14
