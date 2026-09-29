"""Memory round, UI wave B — the web owner dashboard's Labor & Schedule,
people, Reviews, Marketing, Food Cost, Intel, What Connects and Events
screens show what Cavnar AI now remembers (memory audit 9/29/26, M1–M7).

Four halves:

- Behaviour, run under node where it is installed: the page's own helpers
  (the global `mem*` functions in <script id="cav-mem-wb">), given the
  payloads the server sends — the words an owner reads, every date M/D/YY.
- Source rules, read from the templates (the suite has no JS engine for
  the rest): each new element is pinned to the field it reads and the route
  it calls, the decline reads "Not for us" in this wave's regions, and the
  Fix tags vocabulary is the analyser's own.
- Backend: the three small payload changes this wave made — a cover answer
  kept on the coverage issue, the queue items that land on their sections,
  the labor trend's week fields, a stale food diagnosis unanswerable.
- A render check: the real app (hosted_dashboard, in a subprocess against a
  scratch volume) renders the dashboard with the new sections, and every
  JSON read the new UI makes answers with the fields it reads.
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

import pytest
from flask import Flask

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DASH = os.path.join(ROOT, "templates", "dashboard.html")
CARD = os.path.join(ROOT, "templates", "_review_card.html")
PORTAL = os.path.join(ROOT, "templates", "staff_portal.html")


@pytest.fixture(scope="module")
def page():
    with open(DASH, encoding="utf-8") as f:
        return f.read()


def _read(path):
    with open(path, encoding="utf-8") as f:
        return f.read()


def _block(page):
    i = page.index('<script id="cav-mem-wb">')
    return page[i:page.index("</script>", i)]


def _globals(page):
    """The block's global helpers: everything before its behaviour IIFE."""
    b = _block(page)
    return b[len('<script id="cav-mem-wb">'):b.index("\n(function () {")]


def _fn(src, name, js=True):
    """One function's source (a `function name(` or `name = function (` form)
    to the next top-level-looking function."""
    m = re.search(r"(?:\n\s*function\s+" + re.escape(name) + r"\s*\(|" + re.escape(name) + r"\s*=\s*function\s*\()", src)
    assert m, f"{name} is gone from the page"
    nxt = re.search(r"\n\s{0,4}(?:function\s+\w+\s*\(|window\.\w+\s*=\s*function|/\* ──|// ──)", src[m.end():])
    return src[m.start():m.end() + (nxt.start() if nxt else len(src) - m.end())]


def _node(js):
    if not shutil.which("node"):
        pytest.skip("node is not installed")
    out = subprocess.run(["node", "-e", js], capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr[-1500:]
    return json.loads(out.stdout.strip().splitlines()[-1])


def _run(page, calls):
    """Evaluate each `expr` in `calls` ({name: expr}) with the helpers loaded."""
    js = _globals(page) + "\nvar __o = {};\n"
    for k, expr in calls.items():
        js += f"__o[{json.dumps(k)}] = {expr};\n"
    js += "console.log(JSON.stringify(__o));"
    return _node(js)


ISO = re.compile(r"\b20\d\d-\d\d-\d\d\b")


# ── behaviour: the words an owner reads ─────────────────────────────────────

def test_a_scheduling_note_says_when_it_was_noted_and_when_it_ends(page):
    o = _run(page, {
        "live": 'memNotePartMeta({noted: "9/2/26", noted_on: "2026-09-02", expires: "10/1/26", expires_on: "2026-10-01"})',
        "ended": 'memNotePartMeta({noted: "5/2/26", expires: "6/1/26", ended: true})',
        "iso_only": 'memNotePartMeta({noted_on: "2026-09-02"})',
        "none": 'memNotePartMeta({})',
    })
    assert o["live"] == 'noted <span class="hb-num">9/2/26</span> · ends <span class="hb-num">10/1/26</span>'
    assert "ended" in o["ended"] and "ends" not in o["ended"]
    assert o["iso_only"] == 'noted <span class="hb-num">9/2/26</span>'
    assert o["none"] == ""


def test_who_is_who_asks_one_question_and_names_where_each_record_came_from(page):
    o = _run(page, {
        "q": 'memIdentityQ({a: {name: "Kim T."}, b: {name: "Kim Tran"}})',
        "side": 'memIdentitySide({name: "Kim T.", sources: ["toast", "rpower_payroll"], rated: true})',
        "mention": 'memMentionLine({name: "Dana K.", date: "9/2/26", polarity: 1})',
        "complaint": 'memMentionLine({name: "Dana K.", polarity: -1})',
        "named": 'memMentionLine({name: "Dana K.", polarity: null})',
    })
    assert o["q"] == "Is Kim T. the same person as Kim Tran?"
    assert o["side"] == "Kim T.: from Toast, RPOWER payroll, rated"
    assert _run(page, {"x": 'memIdentitySide({name: "Kim T.", sources: []})'})["x"] == ""
    assert o["mention"] == "A guest praised Dana K. on 9/2/26"
    assert o["complaint"].startswith("A guest complained about Dana K.")
    assert o["named"] == "A guest named Dana K."


def test_the_person_record_says_not_watched_yet_never_a_clean_record(page):
    o = _run(page, {
        "unknown": 'memAttendanceLine({known: false})',
        "missing": 'memAttendanceLine(null)',
        "known": 'memAttendanceLine({known: true, shifts: 12, missed: 1, late: 0, no_show_rate: 0.083, last_miss: "2026-09-12"})',
        "covers": 'memCoversLine({taken: 3, declined: 1, days: 180})',
        "no_covers": 'memCoversLine({taken: 0, declined: 0, days: 180})',
        "role": 'memRoleLine({role: "Bartender", since: "2026-09-01", primary: true})',
    })
    assert o["unknown"] == o["missing"] == "Not watched yet"
    assert o["known"] == "12 shifts watched · 1 missed · 0 late · 8% no-show rate · last missed 9/12/26"
    assert o["covers"] == "Took 3 covers, turned down 1 in the last 180 days"
    assert o["no_covers"] == "No covers asked of them in the last 180 days"
    assert o["role"] == "Bartender · main role · since 9/1/26"


def test_a_standing_pattern_says_who_taught_it_and_when(page):
    o = _run(page, {
        "meta": 'memStandingMeta({first_learned: "8/3/26", last_confirmed: "9/21/26", times_applied: 5, times_overridden: 1, editors: {"will": 4, "unknown": 1}})',
        "ruled": 'memStandingState({status: "ruled"})',
        "retired": 'memStandingState({status: "retired"})',
        "active": 'memStandingState({status: "active"})',
    })
    assert o["meta"] == "learned 8/3/26 · last kept 9/21/26 · used in 5 drafts · undone 1 time · from will’s edits"
    assert (o["ruled"], o["retired"], o["active"]) == ("a rule", "retired", "standing")


def test_a_soft_requirement_says_whether_the_draft_met_it(page):
    o = _run(page, {
        "yes": 'memSoftReqMeta({applied: true, scheduled: 3, typical: 2, source: "reviews", expires: "2026-10-04"})',
        "no": 'memSoftReqMeta({applied: false, scheduled: 2, source: "dsr"})',
    })
    assert o["yes"] == "Applied · 3 on (usually 2) · from your reviews · until 10/4/26"
    assert o["no"] == "Not applied · 2 on · from the nightly reports"


def test_a_running_or_recosted_week_is_never_read_as_a_trend(page):
    o = _run(page, {
        "running": 'memTrendNote({complete: false, basis: "rates"}, {complete: true, basis: "rates"})',
        "recosted": 'memTrendNote({complete: true, basis: "pos"}, {complete: true, basis: "rates"})',
        "fine": 'memTrendNote({complete: true, basis: "rates"}, {complete: true, basis: "rates"})',
        "first": 'memTrendNote({complete: true, basis: "rates"}, null)',
    })
    assert o["running"] == "Still in progress — not read as a trend"
    assert o["recosted"] == "Recosted — not comparable with the week before"
    assert o["fine"] == o["first"] == ""


def test_over_target_says_whether_its_margin_was_fitted_to_the_restaurant(page):
    o = _run(page, {
        "fitted": 'memOverMarginLine(4.25, "1.645× this restaurant\\u2019s own spread of daily labor %")',
        "stated": 'memOverMarginLine(3, "the stated 3-point margin — too little history")',
        "none": 'memOverMarginLine(null, "")',
    })
    assert o["fitted"] == "A day counts as over target past 4.3 points, fitted to your own swing"
    assert o["stated"] == "A day counts as over target past 3 points"
    assert o["none"] == ""


def test_a_hidden_kind_says_when_it_is_tried_again(page):
    o = _run(page, {
        "off": 'memSuppressionNote("hours", {state: "suppressed", review_on: "11/3/26"})',
        "retest": 'memSuppressionNote("hours", {state: "retest", review_on: "11/17/26"})',
        "bare": 'memSuppressionNote("hours", null)',
    })
    assert o["off"] == "hours (tried again from 11/3/26)"
    assert o["retest"] == "hours (being tried again until 11/17/26)"
    assert o["bare"] == "hours"


def test_capability_history_names_the_change_who_and_when(page):
    o = _run(page, {
        "rating": 'memCapLine({kind: "rating", subject: "Dana K. · overall", before: {score: 3}, after: {score: 4}, changed_by: "will", changed_at: "2026-09-12 14:02:00"})',
        "first": 'memCapLine({kind: "rating", subject: "Dana K. · overall", before: null, after: {score: 4}})',
        "target": 'memCapLine({kind: "threshold", subject: "per-role targets", changed_at: "2026-09-01 10:00:00"})',
    })
    assert o["rating"] == "Dana K.: 3 → 4, by will on 9/12/26"
    assert o["first"] == "Dana K.: not rated → 4"
    assert o["target"] == "Shift strength targets changed on 9/1/26"


def test_a_link_says_how_long_it_has_stood_and_is_marked_once_it_recurs(page):
    o = _run(page, {
        "rec": 'memLinkLine({label: "Found 3 weeks running, since 9/7/26", recurring: true})',
        "first": 'memLinkLine({label: "First found 9/21/26", recurring: false})',
        "none": 'memLinkLine(null)',
    })
    assert o["rec"].startswith('<span class="mem-pill">Recurring</span> Found <span class="hb-num">3</span>')
    assert "9/7/26" in o["rec"]
    assert "mem-pill" not in o["first"] and o["none"] == ""


def test_request_conversion_is_a_dash_below_its_floor(page):
    o = _run(page, {
        "rate": 'memConversionLine({asked: 10, reviewed: 3, pct: 30, window_days: 14})',
        "dash": 'memConversionLine({asked: 3, reviewed: 1, pct: null, window_days: 14})',
        "none": 'memConversionLine({asked: 0, reviewed: 0, pct: null})',
    })
    assert o["rate"] == "3 of 10 guests you asked left a review within 14 days (30%)"
    assert o["dash"].startswith("1 of 3 guests you asked left a review within 14 days (—")
    assert o["none"] == ""


def test_marketing_says_what_past_texts_brought_back_and_when_results_were_read(page):
    o = _run(page, {
        "ret": 'memReturnsLine("lapsed_60", {lapsed_60: {label: "Haven\'t been in 60+ days", back_per_100: 9.0, campaigns: 2}})',
        "other": 'memReturnsLine("all", {lapsed_60: {label: "x", back_per_100: 9, campaigns: 2}})',
        "asof": 'memResultsAsOf("2026-09-14 06:00:00")',
    })
    assert o["ret"] == "Haven't been in 60+ days: 9 came back per 100 texted (2 campaigns)"
    assert o["other"] == ""
    assert o["asof"] == "Opens and clicks as of 9/14/26 (30 days after sending)."


def test_food_cost_lines_say_how_they_were_fitted_to_the_owner(page):
    o = _run(page, {
        "order": 'memOrderAdjLine({base_qty: 12, qty: 10, owner_adjusted: {factor: 0.86, orders: 5, basis: "your last 5 orders…"}})',
        "plain": 'memOrderAdjLine({qty: 10})',
        "habit": 'memRepriceHabit({owner_ratio: {ratio: 0.5, decisions: 6}, typical_price: 22, typical_monthly: 150.4})',
        "no_habit": 'memRepriceHabit({owner_ratio: null, typical_price: null})',
        "full": 'memRatioWords(1)',
        "prime": 'memPrimeNote({labor_basis_text: "labor measured on 12 nights by your nightly reports", projection_correction: {note: "already corrected because earlier forecasts here ran 12% high"}})',
    })
    assert o["order"] == "Adjusted to how you order (your last 5 orders) · the formula said 12"
    assert o["plain"] == ""
    assert o["habit"] == "You usually raise about half the suggested rise — $22.00 would recover $150/mo"
    assert o["no_habit"] == ""
    assert o["full"] == "the full suggested rise"
    assert o["prime"] == ("Labor: labor measured on 12 nights by your nightly reports. "
                          "The projection is already corrected because earlier forecasts here ran 12% high.")


def test_intel_history_reads_in_m_d_yy_from_iso_weeks(page):
    o = _run(page, {
        "wk": 'memIsoWeekDate("2026-W39")',
        "wk1": 'memIsoWeekDate("2026-W01")',
        "own": 'memOwnRatingLine({available: true, first: {week: "2026-W10", rating: 4.3}, latest: {week: "2026-W39", rating: 4.6}, change: 0.3, weeks: 30})',
        "move": 'memMarketLine({name: "Luigi\'s", kind: "rating_down", from_rating: 4.5, to_rating: 4.1, observed_on: "2026-06-02"})',
        "arrived": 'memMarketLine({name: "Bella", kind: "arrived", observed_on: "2026-03-14"})',
    })
    assert o["wk"] == "2026-09-21" and o["wk1"] == "2025-12-29"
    assert o["own"] == ("Your Google rating went from 4.3★ in the week of 3/2/26 to 4.6★ in the week of 9/21/26 "
                        "(+0.3★ over 30 weeks on file)")
    assert o["move"] == "Rating down 4.5★ → 4.1★ · 6/2/26"
    assert o["arrived"] == "Started showing nearby · 3/14/26"


def test_fix_tags_sends_only_what_the_owner_changed(page):
    o = _run(page, {
        "same": 'memRetagBody({categories: "service,value", sentiment: "negative", severity: "service", dishes: "fries"}, {categories: ["value", "service"], sentiment: "negative", severity: "service", dishes: "Fries"})',
        "cats": 'memRetagBody({categories: "service", sentiment: "negative", severity: "", dishes: ""}, {categories: ["service", "wait_time"], sentiment: "negative", severity: "", dishes: ""})',
        "sev_blank_kept": 'memRetagBody({categories: "service", sentiment: "negative", severity: "", dishes: ""}, {categories: ["service"], sentiment: "negative", severity: "", dishes: ""})',
        "dish": 'memRetagBody({categories: "service", dishes: ""}, {categories: ["service"], dishes: "burger, fries"})',
    })
    assert o["same"] is None
    assert o["cats"] == {"categories": ["service", "wait_time"]}
    assert o["sev_blank_kept"] is None          # an unknown severity is never "corrected" to a default
    assert o["dish"] == {"dishes": ["burger", "fries"]}


def test_a_coverage_issue_asks_did_they_take_it_once_per_ask(page):
    o = _run(page, {
        "ask": 'memCoverAsks({id: 7, status: "open", meta: {asked: [{name: "Zed Q."}, {name: "Ben C.", answer: "took"}]}})',
        "resolved": 'memCoverAsks({id: 7, status: "resolved", meta: {asked: [{name: "Zed Q."}]}})',
        "rec": 'memCoverRecord({name: "Zed Q.", covers_taken: 3, covers_declined: 1})',
        "none": 'memCoverRecord({name: "Zed Q."})',
    })
    assert "Did Zed take it?" in o["ask"] and "Ben" not in o["ask"]
    assert o["ask"].count('data-cover-ans="7"') == 2 and 'data-took="1"' in o["ask"] and 'data-took="0"' in o["ask"]
    assert o["resolved"] == ""
    assert o["rec"] == "took 3 of 4 covers when asked" and o["none"] == ""


def test_an_event_that_has_not_cleared_its_floor_says_so(page):
    o = _run(page, {
        "applies": 'memMeasuredLine({text: "Street fair: nights here ran a median 18% above a typical same weekday", applies: true})',
        "thin": 'memMeasuredLine({text: "Street fair: one night", applies: false})',
    })
    assert o["applies"].endswith("same weekday")
    assert o["thin"].endswith("— not enough nights to lean on yet")


def test_numbers_are_in_the_number_face_and_punctuation_is_not(page):
    o = _run(page, {"n": 'memNum("Ana B.: 3 → 4, by memo on 9/12/26 · $1,200/mo · +0.3★")'})
    assert '<span class="hb-num">4</span>,' in o["n"]
    assert '<span class="hb-num">9/12/26</span>' in o["n"] and '<span class="hb-num">$1,200</span>' in o["n"]
    assert '<span class="hb-num">+0.3★</span>' in o["n"]


def test_what_the_nights_taught_is_one_line_each_dimmed_below_its_floor(page):
    b = _block(page)
    teach = b[b.index("window.memTeachHtml"):b.index("/* ── Intel")]
    assert "list[i].applies ? '' : ' dim'" in teach and "<span>' + memNum(list[i].text) + '</span>" in teach


def test_no_helper_prints_an_iso_date(page):
    o = _run(page, {
        "a": 'memNotePartMeta({noted_on: "2026-09-02", expires_on: "2026-10-01"})',
        "b": 'memAttendanceLine({known: true, shifts: 2, missed: 0, late: 0, last_miss: "2026-09-12"})',
        "c": 'memRoleLine({role: "Bar", since: "2026-09-01"})',
        "d": 'memCapLine({kind: "weights", changed_at: "2026-09-01 10:00:00"})',
        "e": 'memResultsAsOf("2026-09-14")',
        "f": 'memMarketLine({kind: "gone", observed_on: "2026-06-02"})',
        "g": 'memSoftReqMeta({applied: true, expires: "2026-10-04"})',
        "h": 'memThinnedNote({detail_thinned_at: "2026-09-01 04:00:00"})',
    })
    for k, v in o.items():
        assert not ISO.search(v or ""), (k, v)
    assert o["h"] == "Details trimmed after 30 days — the score, band, schedule and economics are kept."


# ── source: each new element reads its field and calls its route ────────────

def test_waiting_on_you_carries_who_is_who_and_guests_naming_staff(page):
    b = _block(page)
    assert 'id="lb2-wait-people" class="ac-rows" data-nav="labor/people"' in page
    assert "'/api/people/identity'" in b and "'/api/people/mentions'" in b
    assert "'/api/people/identity/' + qid, {same: same}" in b
    assert "'/api/people/mentions/' + mid, {confirm: yes}" in b
    assert ">Same person</button>" in b and ">Different people</button>" in b
    assert "d.can_answer" in b and "d.can_confirm" in b
    wait = page[page.index("function waitRender"):page.index("function waitFocus")]
    assert "memWaitCount()" in wait                    # the card shows while only these wait
    assert "memLoadPeopleQs()" in page[page.index("window.lb2LoadWaiting"):page.index("window.lb2WaitSend")]


def test_scheduling_notes_are_listed_dated_and_asked_about(page):
    b = _block(page)
    assert 'data-nav="labor/notes"' in page and 'id="notes-body"' in page
    assert "getJ('/api/labor/staff-notes'" in b and "postJ('/api/labor/staff-notes', body" in b
    assert "{action: 'confirm'}" in b and "action: 'expire'" in b and "action: 'remove'" in b
    assert "Still true?" in b and "p.stale && !p.ended" in b and "_notes.can_edit" in b
    # A removal leaves at once and goes for good after the Undo (tier 1).
    assert "window.cavUndoable('Note removed'" in b
    # The queue's "Review them" lands on the notes; "Answer" on the questions.
    assert "cavNavRegister('labor'" in b and "p.rest[0] === 'notes'" in b and "p.rest[0] === 'people'" in b


def test_the_person_sheet_shows_roles_covers_guests_and_attendance(page):
    b = _block(page)
    for field in ("p.roles_held", "p.covers", "p.guest_mentions", "p.attendance", "p.can_manage_login"):
        assert field in b, field
    assert "'/roles', body" in b and "'/rename', {name: v}" in b and "postJ('/api/people/merge', {from: key, into: tgt}" in b
    render = page[page.index("function personRender"):page.index("function ppSave")]
    assert "memPersonHtml(p)" in render and "memPersonAfter(p)" in render


def test_what_the_draft_keeps_offers_make_it_a_rule_and_settles_clashes(page):
    b = _block(page)
    assert "d.standing" in b and "d.conflicts" in b and "{key: t.getAttribute('data-lrn-rule'), rule: true}" in b
    assert "s.can_be_rule" in b and "c.off_by" in b and "c.on_by" in b
    lrn = page[page.index("function _lrnRender"):page.index("function _rstActive")]
    assert "memLearnedHtml(d,ro)" in lrn and "h+kept" in lrn


def test_the_studio_shows_what_the_draft_was_asked_to_do(page):
    b = _block(page)
    assert 'data-sw="asked" data-ss-of="overview"' in page and 'id="sw-asked"' in page
    assert "data.soft_requirements" in b and "data.pattern_conflicts" in b and "memThinnedNote(data)" in b
    assert "if (k === 'asked') return full('sw-asked');" in page
    assert "memSchedExtras(data)" in page[page.index("function _schedHandleResult"):]
    assert "detail_thinned_at: d.detail_thinned_at" in page             # a reopened week says it was trimmed
    assert "data.labor_target_label" in page[page.index("var par = document.getElementById('sw-par')"):]


def test_hidden_schedule_kinds_say_when_they_are_tried_again(page):
    rq = page[page.index("function renderQualityWarnings"):page.index("var _recState")]
    assert "memSuppressionNote(hidden[i], (window._sqSupp || {})[hidden[i]])" in rq
    assert "if (d.suppression) window._sqSupp = d.suppression;" in page
    assert "window._sqSupp=d.recommendation_suppression" in page
    assert "memSuppressionNote(supp[si],suppSt[supp[si]])" in page


def test_the_decline_reads_not_for_us_in_this_waves_regions(page):
    rq = page[page.index("function renderQualityWarnings"):page.index("var _recState")]
    assert 'title="Pass"' not in rq and "'passed'" not in rq and 'aria-label="Not for us"' in rq
    reprice = page[page.index("fetch('/api/food-cost/reprice'"):page.index("var _fcInv=null")]
    assert ">Pass</button>" not in reprice and ">Not for us</button>" in reprice
    assert 'data-winback-dismiss="\' + w.id + \'">Not for us</button>' in page


def test_labor_reads_capability_history_the_trend_and_the_over_target_margin(page):
    b = _block(page)
    assert "getJ('/api/labor/capability-changes'" in b and 'id="team-cap-history"' in page
    assert "memLoadCapChanges()" in page[page.index("function toggleTeamPanel"):page.index("function loadTeamPanel")]
    assert "complete:w.complete,basis:w.basis" in page and "memTrendNote(p,i>0?pts[i-1]:null)" in page
    assert "labor.over_margin" in page and "labor.over_margin_basis" in page


def test_events_show_their_measured_record_and_what_the_nights_taught(page):
    assert "window._memTeach=d.what_nights_teach||[]" in page
    assert "memTeachHtml(window._memTeach)" in page
    assert "sg.measured&&window.memMeasuredLine" in page
    assert "What your nights have taught" in _block(page)


def test_a_coverage_issue_asks_who_took_it_and_shows_their_record(page):
    cov = page[page.index("function hbCoverButtons"):page.index("function hbAskCover")]
    assert "memCoverAsks(x)" in cov and "memCoverTitle(covers[i])" in cov
    assert "'/api/issues/' + id + '/cover-answer', {name: who, accepted: took}" in _block(page)


def test_reviews_fix_tags_conversion_and_an_older_diagnosis(page):
    card = _read(CARD)
    for attr in ('data-retag="{{ r.id }}"', 'data-rt-cats="{{ (r.categories or [])|join(\',\') }}"',
                 'data-rt-sent="{{ r.sentiment or \'\' }}"', 'data-rt-sev="{{ r.severity or \'\' }}"',
                 'data-rt-dishes="{{ _rt_dishes|join(\', \') }}"', ">Fix tags</button>"):
        assert attr in card, attr
    b = _block(page)
    assert "postJ('/api/reviews/' + id + '/retag', body" in b
    assert 'id="rr-conversion"' in page and "memConversionLine(d.conversion)" in page
    diag = page[page.index("var cfl=cavConfLine(cavConf.k1(dg.confidence_detail,dg.confidence),{key:dg.rec_key,surface:'reviews'"):]
    diag = diag[:diag.index("var more=")]
    assert "dg.controls_withheld==='stale'" in diag and "dg.answerable!==false" in diag and "Older read" in diag


def test_fix_tags_uses_the_analysers_own_vocabulary(page):
    import analyser
    b = _block(page)

    def _map(name):
        m = re.search(r"var " + name + r" = (\{[^}]*\});", b)
        assert m, name
        return json.loads(re.sub(r"(\w+):", r'"\1":', m.group(1)).replace("'", '"'))
    assert _map("MEM_RT_CATS") == analyser.CATEGORY_LABELS
    assert _map("MEM_RT_SEV") == analyser.SEVERITY_LABELS
    assert set(_map("MEM_RT_SENT")) == set(analyser.SENTIMENTS)


def test_marketing_sends_draft_refs_and_shows_its_own_results(page):
    assert "window._mktDraftRef=d.draft_ref||null;" in page
    save = page[page.index("function saveMktDraft"):page.index("function loadMktDrafts")]
    assert "content_log_id: window._mktContentLogId || null, draft_ref: window._mktDraftRef || null" in save
    snap = page[page.index("function cpSnapshot"):page.index("function cpMarkSent")]
    assert "draft_ref: (_cp.ref || {}).text || null" in snap and "draft_ref: (_cp.ref || {}).email || null" in snap
    assert "b.content_log_id = _cp.clid" in snap
    assert "_cp.ref[k] = d.draft_ref || null" in page and "_cp.rets = d.returns_by_segment" in page
    assert 'id="cp-aud-ret"' in page and "memReturnsLine(_cp.seg, _cp.rets)" in page
    assert "memResultsAsOf(c.results_as_of)" in page
    assert "o.learned_weight" in page and "Ranked by your results" in page
    assert "_cp.ref = {}; _cp.clid = null;" in page          # a new draft names no earlier one


def test_food_cost_reads_its_corrections(page):
    assert "it.owner_adjusted&&window.memOrderAdjLine" in page
    assert "l.matched_by==='your_match'" in page and "Matched as you did last time" in page
    reprice = page[page.index("fetch('/api/food-cost/reprice'"):page.index("var _fcInv=null")]
    assert "x.guard&&x.guard.text" in reprice and "guarded?'cbtn-secondary':'cbtn-primary'" in reprice
    assert "x.value_note" in reprice and "memRepriceHabit(x)" in reprice and "x.typical_price" in reprice
    assert 'id="fc2-par-sug" data-nav="inventory/pars"' in page and "memLoadPars()" in page
    b = _block(page)
    assert "getJ('/api/food-cost/par-suggestions'" in b and "'/api/food-cost/par-suggestions/' + id + '/accept'" in b
    assert "recNotForUsHtml(s.key, 'food', 'food')" in b
    cfo = page[page.index("function fc2LoadCfo"):page.index("function fc2AccLine")]
    assert "p.projection_correction" in cfo and "pcc.projected_prime_cost" in cfo and "memPrimeNote(" in cfo
    assert "dg.controls_withheld!=='stale'" in cfo


def test_intel_and_what_connects_read_their_history(page):
    moves = page[page.index("function in2LoadMoves"):page.index("function in2LoadRecs")]
    assert "memIntelHistoryHtml(d)" in moves
    b = _block(page)
    assert "d.own_rating_history" in b and "d.market_history" in b and "C.glowLine(vals" in b
    links = page[page.index("function hbLinkRows"):page.index("function hbExtraRow")]
    assert "sub:x.memory||null" in links
    assert "a.sub&&window.memLinkLine" in page[page.index("function hbExtraRow"):]
    assert "ff.recurring&&window.memPill" in page[page.index("function renderFocus"):]
    assert "dsr:'Daily report'" in page and "food_cost:'Food Cost'" in page


def test_the_staff_portal_tags_a_promotion(page):
    assert "promotion: 'Promotion'" in _read(PORTAL)


def test_new_markup_follows_the_house_rules(page):
    b = _block(page)
    for banned in ("toLocaleDateString", "<input type=\"time\"", "type=time", "Loading…", "Loading...", "spinner"):
        assert banned not in b, banned
    assert not re.search(r"(?<![\w-])Cavnar(?! AI)(?!['’]s)", re.sub(r"cav-mem-wb|cavNav\w*|cavUndoable|CavnarCharts|cbtn\w*|cavConf\w*", "", b)), \
        "the product is Cavnar AI in words an owner reads"
    # Every colour is a token.
    css = page[page.index('<style id="cav-mem-wb-css">'):page.index("</style>", page.index('<style id="cav-mem-wb-css">'))]
    assert not re.search(r"(?<![-\w])color:\s*#", css) and not re.search(r"background:\s*#", css)


# ── backend: the payload changes this wave made ─────────────────────────────

@pytest.fixture
def _db(db_path, monkeypatch):
    import action_queue
    import intraday
    import models
    import staff_settings
    real = models.get_conn
    fake = lambda *a, **k: real(db_path)  # noqa: E731
    monkeypatch.setattr(models, "get_conn", fake)
    monkeypatch.setattr(models, "DB_PATH", db_path)
    monkeypatch.setattr(action_queue, "get_conn", fake)
    monkeypatch.setattr(staff_settings, "get_conn", fake)
    monkeypatch.setattr(intraday, "get_conn", fake)
    yield db_path


def _rid(**kw):
    from models import Restaurant, create_restaurant
    fields = dict(name="Memory UI Co", owner_email="m@x.test", module_labor=1)
    fields.update(kw)
    return create_restaurant(Restaurant(**fields))


def test_a_cover_answer_is_kept_on_the_issue_so_home_stops_asking(_db, monkeypatch):
    import auth
    import models
    import strategy_routes
    rid = _rid()
    conn = models.get_conn()
    iid = conn.execute("INSERT INTO ops_issues (restaurant_id, kind, source_key, title, status, meta_json) VALUES "
                       "(?,?,?,?,?,?)", (rid, "coverage", "coverage:2026-09-20:tom b.", "Tom can't make it", "open",
                                         '{"missing": "Tom B.", "asked": [{"name": "Zed Q."}, {"name": "Ben C."}]}')).lastrowid
    conn.commit()
    conn.close()
    user = {"id": 7, "restaurant_id": rid, "base_restaurant_id": rid, "username": "owner", "role": "owner",
            "is_admin": 0, "email": "o@x.test"}
    monkeypatch.setattr(auth, "get_current_user", lambda: user)
    monkeypatch.setattr(auth, "get_session_user", lambda *a, **k: user, raising=False)
    app = Flask(__name__)
    app.register_blueprint(strategy_routes.strategy_bp)
    c = app.test_client()
    r = c.post(f"/api/issues/{iid}/cover-answer", json={"name": "Zed Q.", "accepted": True})
    assert r.status_code == 200 and r.get_json()["answer"] == "took"
    conn = models.get_conn()
    meta = json.loads(conn.execute("SELECT meta_json FROM ops_issues WHERE id=?", (iid,)).fetchone()["meta_json"])
    conn.close()
    asked = {a["name"]: a.get("answer") for a in meta["asked"]}
    assert asked == {"Zed Q.": "took", "Ben C.": None}          # Ben is still asked about
    import intraday
    assert intraday.mark_cover_answer(rid, iid, "Nobody", False) is False


def test_the_queue_items_land_on_their_sections(_db):
    from datetime import date
    import action_queue
    import models
    rid = _rid()
    models.save_staff_note(rid, "Maria G.", "no Sundays", today=date(2026, 5, 1))
    q = action_queue.items(rid, today=date(2026, 9, 1), present=False)["items"]
    item = next(i for i in q if i["key"] == "staff_note:stale")
    assert item["nav"] == "labor/notes" and item["action"]["nav"] == "labor/notes"
    src = _read(os.path.join(ROOT, "action_queue.py"))
    assert '"label": "Answer", "module": "labor", "nav": "labor/people"' in src


def test_the_labor_trend_carries_each_weeks_dates_and_basis():
    for path, fn in (("client_api.py", "def labor_trend_api"), ("mobile_api.py", "def mobile_labor_trend")):
        src = _read(os.path.join(ROOT, path))
        body = src[src.index(fn):]
        body = body[:body.index("\n@")]
        assert '"complete": bool(h.get("complete"))' in body
        assert '"comparable": bool(h.get("comparable"))' in body and '"basis": h.get("basis")' in body
    web = _read(os.path.join(ROOT, "client_api.py"))
    web = web[web.index("def labor_trend_api"):]
    assert '"start": h.get("period_start")' in web[:web.index("\n@")]


def test_a_food_diagnosis_too_old_to_lean_on_offers_no_controls():
    src = _read(os.path.join(ROOT, "mobile_api.py"))
    cfo = src[src.index("def mobile_food_cost_cfo"):]
    cfo = cfo[:cfo.index("\n@mobile_bp")]
    assert "diagnosis_anchor_strength(_dg) is not None" in cfo
    assert 'shown=1 if _dg_live else 0' in cfo
    assert '_dg["answerable"] = False' in cfo and '_dg["controls_withheld"] = "stale"' in cfo


# ── render: the real app draws the sections and answers every read ──────────

RENDER = r'''
import json, os, re, sqlite3, sys
vol = sys.argv[1]
sqlite3.connect(os.path.join(vol, "reviews.db")).close()
os.environ.update(RAILWAY_VOLUME_MOUNT_PATH=vol, RUN_SCHEDULER_IN_WEB="0", ANTHROPIC_API_KEY="",
                  RESEND_API_KEY="", TWILIO_AUTH_TOKEN="", TWILIO_ACCOUNT_SID="", STRIPE_SECRET_KEY="",
                  DOCUSIGN_INTEGRATION_KEY="", GOOGLE_PLACES_API_KEY="", PERPLEXITY_API_KEY="",
                  SECRET_KEY="test-secret", CAVNAR_PIN_PEPPER="test-pepper", HIBP_DISABLED="1", ADMIN_REQUIRE_2FA="0")
import hosted_dashboard as h
import auth, models, people, shift_facts
from models import Restaurant
rid = models.create_restaurant(Restaurant(name="Memory Co", owner_email="mem@x.test", module_reviews=1,
                                          module_labor=1, module_inventory=1, module_marketing=1))
auth.create_user(rid, "memo", "mem@x.test", "correct-horse-battery", is_admin=False)
c = models.get_conn()
c.execute("INSERT INTO reviews (restaurant_id, platform, external_id, author, rating, text, review_date, fetched_at, "
          "sentiment, severity, categories, processed) VALUES (?, 'google', 'r1', 'Ann Lee', 2, 'slow and cold', "
          "date('now','-2 days'), datetime('now'), 'negative', 'service', '[\"service\"]', 1)", (rid,))
c.commit(); c.close()
shift_facts.ingest(rid, [{"date": "2026-09-01", "day": "Tuesday", "employee": "Ana B.", "role": "Server",
                          "shift_start": "16:00", "shift_end": "22:00", "scheduled_hours": "6",
                          "actual_hours": "6", "sales": "1000"}], "upload")
cl = h.app.test_client()
page = cl.get("/login").get_data(as_text=True)
token = re.search(r'name="csrf_token" value="([^"]+)"', page).group(1)
r = cl.post("/login", data={"username": "memo", "password": "correct-horse-battery", "csrf_token": token})
assert r.status_code in (302, 303), r.status_code
html = cl.get("/").get_data(as_text=True)
marks = ['id="lb2-wait-people"', 'data-nav="labor/notes"', 'id="notes-body"', 'data-sw="asked"',
         'id="team-cap-history"', 'id="fc2-par-sug"', 'id="rr-conversion"', 'id="cp-aud-ret"',
         'id="cav-mem-wb"', 'data-retag=']
print("MARKS", json.dumps({m: (m in html) for m in marks}))
try:
    ck = cl.get_cookie("csrf_js")
    jsval = ck.value if ck else ""
except Exception:
    jsval = next((k.value for k in getattr(cl, "cookie_jar", []) if k.name == "csrf_js"), "")
hdrs = {"X-CSRF": jsval}                    # the page's double-submit header (_csrf_fetch.html)
post = cl.post("/api/labor/staff-notes", json={"employee_name": "Ana B.", "notes": "no Tuesdays", "expires_on": "12/31/26"}, headers=hdrs)
print("NOTEPOST", post.status_code)
assert post.status_code == 200, post.get_data(as_text=True)[:300]
reads = {
  "/api/people/identity": ["questions", "can_answer"],
  "/api/people/mentions": ["mentions", "can_confirm"],
  "/api/labor/staff-notes": ["notes", "stale", "can_edit", "stale_after_days"],
  "/api/labor/learned-patterns": ["patterns", "standing", "conflicts", "can_edit"],
  "/api/labor/capability-changes": ["changes"],
  "/api/labor/demand-signals": ["signals", "what_nights_teach"],
  "/api/labor-trend": ["weeks"],
  "/api/food-cost/par-suggestions": ["suggestions"],
  "/api/intel/movement": ["market_history", "own_rating_history"],
  "/api/review-request-stats": ["conversion"],
  "/api/people/ana-b": ["person"],
}
out = {}
for url, fields in reads.items():
    rr = cl.get(url)
    d = rr.get_json(silent=True) or {}
    out[url] = [rr.status_code, [f for f in fields if f not in d]]
p = (cl.get("/api/people/ana-b").get_json(silent=True) or {}).get("person") or {}
out["person_fields"] = [f for f in ("roles_held", "covers", "guest_mentions", "attendance", "can_manage_login") if f not in p]
notes = (cl.get("/api/labor/staff-notes").get_json(silent=True) or {}).get("notes") or []
out["note_parts"] = sorted((notes[0]["parts"][0] if notes and notes[0].get("parts") else {}).keys())
print("READS", json.dumps(out))
'''


def test_the_dashboard_renders_the_new_sections_and_every_read_answers():
    vol = tempfile.mkdtemp(prefix="cavnar-memui-")
    out = subprocess.run([sys.executable, "-c", RENDER, vol], cwd=ROOT, capture_output=True, text=True, timeout=240)
    assert out.returncode == 0, out.stdout[-1500:] + "\n" + out.stderr[-3000:]
    marks = json.loads(next(l for l in out.stdout.splitlines() if l.startswith("MARKS "))[6:])
    assert all(marks.values()), marks
    reads = json.loads(next(l for l in out.stdout.splitlines() if l.startswith("READS "))[6:])
    for url in [k for k in reads if k.startswith("/")]:
        status, missing = reads[url]
        assert status == 200 and not missing, (url, status, missing)
    assert reads["person_fields"] == []
    assert {"index", "text", "noted", "expires", "ended", "stale"} <= set(reads["note_parts"])
