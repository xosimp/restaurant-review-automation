"""Memory round, UI wave — the admin console shows what the eight memory
workstreams learn (templates/admin.html + the admin_ops / admin_routes reads):

- M1 rank learning: Analytics → Recommendations reads
  /admin/api/recommendations/learning (groups by model version and weight
  bucket, the taken rate with its 90% range, what builds left unshown);
- M4: the client's AI tab draws the monthly learning scorecards
  (ai_client.learning + learning_curves) and the model's own confidence
  (quality.model_confidence, also on Operations → AI); learning issues
  (`<rid>:learning:<curve>`, `:learning:fatigue`) sort under AI and read in
  months and percents;
- M7: the brand form's learning override (learning_override, include /
  exclude / automatic, learning_history) sent only when it changed — and the
  server no longer re-starts an include's clock on a re-send; the Jobs page
  says which retention windows are refused now (jobs.retention) and what a
  prune run refused or capped (kept in its stored counts by ops.run_outcome);
- M8: the Intelligence page's weeks held and a pattern's weekly record
  (/admin/api/intelligence/pattern-history), trends "as measured each week",
  the confidence log's meaning version and owners, backfilled feature weeks
  (and ISO weeks walked as weeks, not read as January 1st); the Schedule
  experiments page's weekly verdicts, method, the per-restaurant week cap
  and the clusters behind each difference.

Source rules read from the template; behaviour run under node (the page's own
functions over the payloads the server sends); a render check over the Flask
app for every read the new panels make.
"""
import json
import os
import re
import shutil
import subprocess
import sys

import pytest
from flask import Flask

import admin_ops
import admin_routes
import models
import ops
from auth import create_session, create_user, init_auth, upsert_membership
from models import Restaurant, create_restaurant, update_restaurant

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CSRF = "memui-csrf"


@pytest.fixture(scope="module")
def page():
    with open(os.path.join(ROOT, "templates", "admin.html"), encoding="utf-8") as f:
        return f.read()


def _fn(src, name):
    """One top-level function's source, to the next top-level function or banner."""
    m = re.search(r"\n(?:async\s+)?function\s+" + re.escape(name) + r"\s*\(", src)
    assert m, f"{name}() is gone from admin.html"
    nxt = re.search(r"\n(?:async\s+)?function\s+\w+\s*\(|\n// ──|\nconst |\nlet ", src[m.end():])
    return src[m.start():m.end() + (nxt.start() if nxt else len(src))]


def _const(src, name):
    m = re.search(r"\n(const " + re.escape(name) + r" = [^\n]*)", src)
    assert m, f"const {name} is gone from admin.html"
    return m.group(1)


# ── where each new field is read ─────────────────────────────────────────────

def test_the_clients_ai_tab_draws_the_learning_curve_and_the_models_confidence(page):
    ai = _fn(page, "aiClientHtml")
    assert "learningCurveHtml(d.learning, d.learning_curves)" in ai
    assert "modelConfHtml(q.model_confidence)" in ai
    lc = _fn(page, "learningCurveHtml")
    for key in ("meta.curves", "spec.min_n", "spec.better", "spec.unit", "last.flags", "f.state", "f.latest", "f.before",
                "last.fatigue", "fat.share", "fat.fatigued", "r.metrics", "m.n", "monLabel(rows[0].month)"):
        assert key in lc, key
    mc = _fn(page, "modelConfHtml")
    for key in ("mc.scored", "mc.by_model_band", "mc.by_capped_band", "_ordVerdict(mc)", "s.held", "s.rate",
                "MC_MIN", "unknownV("):
        assert key in mc, key
    # The fleet's AI quality panel carries the same check.
    assert "modelConfHtml(q.model_confidence)" in _fn(page, "aiQuality")


def test_learning_issues_sort_under_ai(page):
    cat = re.search(r"\nconst ISSUE_CAT = \{.*?\};", page, re.S).group(0)
    out = _node(cat + "\n" + _fn(page, "issueCat") + "\nconsole.log(JSON.stringify([issueCat({category: 'learning', "
                "key: '5:learning:acceptance'}), issueCat({category: 'learning', key: '5:learning:fatigue'})]));")
    assert out == ["AI", "AI"]                  # before: 'Account', by the key's fallback


def test_the_recommendations_page_reads_the_rank_learning_after_first_paint(page):
    rec = _fn(page, "recommendations")
    assert 'id="rec-rank"' in rec and "lazy('rec-rank', () => rankLearningHtml(rid))" in rec
    rl = _fn(page, "rankLearningHtml")
    assert "'/admin/api/recommendations/learning?days=90'" in rl and "restaurant_id=" in rl
    for key in ("d.groups", "d.unshown_by_surface", "d.min_n", "d.note", "r.version", "r.bucket", "r.shown",
                "r.settled", "r.accept_rate", "r.accept_ci90", "r.enough", "r.measured", "r.outcome_rate",
                "un[s].unshown", "un[s].builds"):
        assert key in rl, key
    assert "accept_ci90" in _fn(page, "rankBar") and "calbar" in _fn(page, "rankBar")
    # The bucket labels are the server's own.
    assert all(label in _const(page, "RANK_BUCKET_ORDER") for _lo, _hi, label in admin_ops.RANK_WEIGHT_BUCKETS)


def test_the_brand_form_has_the_learning_override_and_sends_it_only_when_changed(page):
    modal = page[page.index('<div class="mo" id="m-brand">'):page.index('<div class="mo" id="m-import">')]
    assert 'id="b-lov"' in modal and 'value="include"' in modal and 'value="exclude"' in modal
    assert 'id="b-lhist"' in modal and "Teaches Cavnar AI's learning" in modal
    fill = _fn(page, "brandFill")
    assert "p.learning.override" in fill and "learnStatus(p.learning)" in fill
    st = _fn(page, "learnStatus")
    for key in ("l.eligible", "l.label", "l.reason", "l.automatic", "l.override", "l.since", "brandLearnInclude()"):
        assert key in st, key
    assert re.search(r'<button class="[^"]*\bw\b[^"]*"[^>]*onclick="brandLearnInclude\(\)"', st)   # support can't
    sv = _fn(page, "brandSave")
    assert "if(lov !== lov0){ body.learning_override = lov;" in sv and "body.learning_history = true" in sv
    assert "brandReset" in _fn(page, "brandClose") and "$('b-lov').value=''" in _fn(page, "brandReset")


def test_the_jobs_page_says_what_retention_refused_and_capped(page):
    oj = _fn(page, "opsJobs")
    assert "retentionNow(d.retention)" in oj
    rn = _fn(page, "retentionNow")
    assert "r === null" in rn and "r.refused" in rn and "x.floor" in rn
    row = _fn(page, "jobRow")
    assert "runHeld(j)" in row and "heldLines(held)" in row
    rh = _fn(page, "runHeld")
    for key in ("result_json", "res.refused", "res.capped", "res.refused_n", "res.capped_n", "r.finished_at"):
        assert key in rh, key


def test_the_intelligence_page_reads_the_weekly_record(page):
    ig = _fn(page, "intelligence")
    for key in ("isoWeekDay(L.weeks[0].week)", "isoWeekKey(x)", "backfilled", "featureWeekBars(weeks)",
                "as measured each week", "t.n_joined", "key:'trust_version'", "key:'orgs'", "weekLabel(r.week)",
                "key:'weeks_held'", "patHistory('${jsq(r.key)}')", 'id="int-ph"'):
        assert key in ig, key
    assert "dayOf(L.weeks" not in ig                     # an ISO week read as January 1st
    assert "'/admin/api/intelligence/pattern-history?key=' + encodeURIComponent(key)" in _fn(page, "patHistoryHtml")


def test_the_experiments_page_reads_the_verdict_history_and_method(page):
    ex = _fn(page, "experiments")
    for key in ("verdictHistory(e.verdict_history, state)", "d.method", "d.max_weeks_per_restaurant", "x.clusters"):
        assert key in ex, key
    vh = _fn(page, "verdictHistory")
    for key in ("v.week", "v.state", "v.call", "v.method", "weekLabel("):
        assert key in vh, key


def test_the_design_system_names_the_new_patterns():
    with open(os.path.join(ROOT, "DESIGN_SYSTEM.md"), encoding="utf-8") as f:
        ds = f.read()
    sec = ds[ds.index("## 12c."):ds.index("## 13.")]
    for key in ("learningCurveHtml", "curveSpark", "weekLabel", "monLabel", "featureWeekBars", ".calbar"):
        assert key in sec, key


# ── behaviour, run in node ───────────────────────────────────────────────────

def _node(js):
    if not shutil.which("node"):
        pytest.skip("node is not installed")
    env = dict(os.environ, LC_ALL="en_US.UTF-8", LANG="en_US.UTF-8", TZ="America/Chicago")
    out = subprocess.run(["node", "-e", js], capture_output=True, text=True, timeout=30, env=env)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout.strip().splitlines()[-1])


BASE_FNS = ("unknownV", "readErr", "pill", "parseTs", "dayOf", "mdy", "fmtD", "fmtT", "fmtDT", "okRes", "errText",
            "isoWeekDay", "isoWeekKey", "weekLabel", "monLabel", "_ordVerdict")
TABLE_STUB = ("function table(cols, rows, opts){ return '<table>' + rows.map(function(r){ return '<tr>' + "
              "cols.map(function(c){ return '<td>' + (c.cell ? c.cell(r) : esc(r[c.key])) + '</td>'; }).join('') + "
              "'</tr>'; }).join('') + '</table>'; }\n")


def _js(page, fns, consts=()):
    head = "\n".join(_const(page, n) for n in ("esc", "jsq", "fmtN") + tuple(consts))
    body = "".join(_fn(page, n) for n in BASE_FNS + tuple(fns))
    return "function ico(n){ return '<i:' + n + '>'; }\nfunction skel(){ return ''; }\n" + TABLE_STUB + head + "\n" + body + "\n"


def _text(html):
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", html)).strip()


def test_iso_weeks_read_as_their_monday_and_walk_week_by_week(page):
    out = _node(_js(page, ()) + """
const r = {};
r.w39 = mdy(isoWeekDay('2026-W39')); r.w01 = mdy(isoWeekDay('2026-W01')); r.w53 = mdy(isoWeekDay('2020-W53'));
r.day = mdy(isoWeekDay('2026-09-21')); r.bad = isoWeekDay('soon');
r.keys = [new Date(2026, 8, 21), new Date(2026, 8, 27), new Date(2025, 11, 29), new Date(2021, 0, 3), new Date(2026, 2, 9)].map(isoWeekKey);
const walk = []; const end = isoWeekDay('2026-W02'); for(let x = isoWeekDay('2025-W51'); x <= end; x.setDate(x.getDate() + 7)) walk.push(isoWeekKey(x));
r.walk = walk; r.label = weekLabel('2026-W39'); r.mon = monLabel('2026-08');
console.log(JSON.stringify(r));
""")
    assert out["w39"] == "9/21/26" and out["w01"] == "12/29/25" and out["w53"] == "12/28/20"
    assert out["day"] == "9/21/26" and out["bad"] is None
    assert out["keys"] == ["2026-W39", "2026-W39", "2026-W01", "2020-W53", "2026-W11"]    # across DST too
    assert out["walk"] == ["2025-W51", "2025-W52", "2026-W01", "2026-W02"]
    assert out["label"] == "week of 9/21/26" and out["mon"] == "Aug 2026"


META = {"curves": {"acceptance": {"label": "recommendation acceptance", "better": "up", "min_n": 10, "unit": "share"},
                   "forecast_error": {"label": "forecast error", "better": "down", "min_n": 2, "unit": "pct"}},
        "fatigue_share": 0.75, "fatigue_min_n": 20}
MONTHS = [
    {"month": "2026-06", "metrics": {"acceptance": {"value": 0.6, "n": 30}, "forecast_error": {"value": 8.0, "n": 4}},
     "flags": [], "fatigued": False},
    {"month": "2026-07", "metrics": {"acceptance": {"value": 0.0, "n": 3}, "forecast_error": {"value": 9.5, "n": 4}},
     "flags": [], "fatigued": False},
    {"month": "2026-08", "metrics": {"acceptance": {"value": 0.4, "n": 25}, "forecast_error": {"value": 9.0, "n": 1}},
     "fatigue": {"share": 0.81, "n": 34, "fatigued": True}, "fatigued": True,
     "flags": [{"curve": "acceptance", "label": "recommendation acceptance", "state": "worsening", "latest": 0.4,
                "before": 0.59, "months": ["2026-06", "2026-07", "2026-08"]}]},
]


def test_a_learning_curve_gaps_a_thin_month_and_takes_the_servers_flag(page):
    out = _node(_js(page, ("curveFig", "curveSpark", "learningCurveHtml"), ("CURVE_TONE",))
                + "const M = " + json.dumps(MONTHS) + ", META = " + json.dumps(META) + ";\n"
                + "console.log(JSON.stringify([learningCurveHtml(M, META), learningCurveHtml([], META), "
                  "learningCurveHtml(M.slice(0, 2).map(function(m){ return Object.assign({}, m, {flags: []}); }), META)]));")
    html, empty, quiet = out
    rows = html.split('border-bottom:1px solid var(--line)"')
    acc = next(r for r in rows if "recommendation acceptance" in r)
    fe = next(r for r in rows if "forecast error" in r)
    # July's 3 settled is under its floor of 10: a gap (two separate strokes), never a 0% point.
    path = re.search(r'<path d="([^"]+)"', acc).group(1)
    assert path.count("M") == 2 and "L" not in path.split("M")[1]
    assert "var(--red)" in acc and 'class="pill bad"' in acc and "Worsening" in acc
    assert "40% against 59% the two months before" in _text(acc)
    assert ">40%<" in acc and "n=25" in acc and "higher is better" in acc
    # August's forecast error has 1 scored forecast, under its floor of 2: not read, no figure.
    assert "—" in fe and "under 2" in fe and "lower is better" in fe and "too few" in fe and "9% miss" not in fe
    assert "Fatigued" in html and "81%" in html and "34" in html and "show fewer" in html
    assert "Jun 2026" in html and "Aug 2026" in html and "2026-06" not in _text(html)
    assert "No month scored yet" in empty
    assert "Holding" in quiet and "var(--red)" not in quiet


def test_the_models_confidence_is_read_only_above_its_floor_and_says_its_order(page):
    mc = {"by_model_band": [{"band": "high", "scored": 12, "held": 6, "rate": 0.5},
                            {"band": "medium", "scored": 8, "held": 6, "rate": 0.75},
                            {"band": "low", "scored": 3, "held": 3, "rate": 1.0}],
          "by_capped_band": [{"band": "medium", "scored": 20, "held": 12, "rate": 0.6}],
          "scored": 23, "ordered": False, "days": 365}
    out = _node(_js(page, ("modelConfHtml",), ("MC_MIN",)) + "const MC = " + json.dumps(mc) + ";\n"
                + "console.log(JSON.stringify([modelConfHtml(MC), modelConfHtml(null), "
                  "modelConfHtml({scored: 0, by_model_band: [], by_capped_band: [], ordered: null})]));")
    html, failed, none = out
    assert "out of order" in html and "23" in html and "365 days" in html
    high = html[html.index(">high<"):html.index(">medium<")]
    assert "50%" in high and "6 of 12" in high
    low = html[html.index(">low<"):]
    assert "n&lt;5" in low and "opacity:.5" in html and "100%" not in low          # 3 scored: not read
    assert "The band the owner saw" in html and "60%" in html
    assert ">unknown<" in failed and "No claim scored yet" in none


RANK = {"ok": True, "days": 90, "min_n": 20, "note": "Before and after, not proof: a bucket's rates compare what the "
        "model lifted with what it sank.",
        "groups": [{"version": 2, "bucket": "raised (over 1.1x)", "shown": 60, "taken": 21, "settled": 50,
                    "measured": 10, "improved": 6, "accept_rate": 0.42, "accept_ci90": [0.31, 0.54],
                    "outcome_rate": 0.6, "enough": True},
                   {"version": 2, "bucket": "lowered (under 0.9x)", "shown": 40, "taken": 6, "settled": 33,
                    "measured": 4, "improved": 1, "accept_rate": 0.182, "accept_ci90": [0.09, 0.31],
                    "outcome_rate": 0.25, "enough": True},
                   {"version": None, "bucket": "no weight logged", "shown": 9, "taken": 1, "settled": 5,
                    "measured": 0, "improved": 0, "accept_rate": 0.2, "accept_ci90": [0.04, 0.55],
                    "outcome_rate": None, "enough": False}],
        "unshown_by_surface": {"home": {"builds": 34, "unshown": 120}}}


def test_rank_learning_compares_raised_with_lowered_only_when_both_are_read(page):
    thin = dict(RANK, groups=[dict(g, enough=False) if g["bucket"].startswith("lowered") else g for g in RANK["groups"]])
    out = _node(_js(page, ("rankBar", "rankSentence", "rankLearningHtml"), ("RANK_BUCKET_ORDER",))
                + "let answers = [], gets = []; async function need(p){ gets.push(p); return answers.shift(); }\n"
                + "(async () => { answers.push(" + json.dumps(RANK) + ", " + json.dumps(thin) + ", "
                + json.dumps(dict(RANK, groups=[], unshown_by_surface={})) + ");\n"
                + "const a = await rankLearningHtml(5), b = await rankLearningHtml(null), c = await rankLearningHtml(null);\n"
                + "console.log(JSON.stringify({a: a, b: b, c: c, gets: gets})); })();")
    assert out["gets"] == ["/admin/api/recommendations/learning?days=90&restaurant_id=5",
                           "/admin/api/recommendations/learning?days=90",
                           "/admin/api/recommendations/learning?days=90"]
    a = _text(out["a"])
    assert "what the model raised was taken 42% of the time, what it lowered 18%" in a
    assert "60% against 25% improved" in a
    assert "home&nbsp;·&nbsp;120&nbsp;left out over&nbsp;34&nbsp;builds" in _text(out["a"]) and "Before and after, not proof" in a
    # The unread group: its rate is not given, its bar is dimmed.
    assert "n&lt;20" in out["a"] and 'class="calbar off"' in out["a"] and 'class="calbar ok"' in out["a"]
    assert "left:31%" in out["a"] and "left:42%" in out["a"]                   # the range and the point
    assert "not enough settled in both a raised and a lowered group" in _text(out["b"])
    assert "No shown recommendation carried a logged rank" in _text(out["c"]) and "no build logged yet" in out["c"]


def test_the_jobs_page_names_what_a_prune_refused_and_capped(page):
    blob = ops.run_outcome({"attempted": 3, "ok": 2, "failed": 1, "skipped": 0, "hit_bound": True,
                            "refused": [{"table": "alert_log", "days": 18, "floor": 90}], "capped": ["rec_events"]})[1]
    job = {"job": "prune_ledgers", "history": [
        {"started_at": "2026-09-29T08:10:00Z", "finished_at": None, "state": "running"},
        {"started_at": "2026-09-28T08:10:00Z", "finished_at": "2026-09-28T08:12:00Z", "state": "partial",
         "result_json": blob}]}
    clean = {"job": "value_snapshots", "history": [{"started_at": "2026-09-28T11:00:00Z",
                                                    "finished_at": "2026-09-28T11:01:00Z",
                                                    "result_json": '{"attempted": 3, "ok": 3}'}]}
    out = _node(_js(page, ("runHeld", "heldLines", "retentionNow"))
                + "const J = " + json.dumps(job) + ", C = " + json.dumps(clean) + ";\n"
                + "console.log(JSON.stringify([heldLines(runHeld(J)), runHeld(C), heldLines(runHeld({history: []})), "
                  "retentionNow(undefined), retentionNow(null), retentionNow({refused: []}), "
                  "retentionNow({refused: [{table: 'alert_log', days: 18, floor: 90}]})]));")
    held, clean_held, none, missing, failed, fine, refused = out
    t = _text(held)
    assert "Refused on 9/28/26" in t and "alert_log 18 days (floor 90)" in t and "var(--red-t)" in held
    assert "Capped on 9/28/26" in t and "rec_events" in t and "the rest go on the next nights" in t
    assert clean_held is None and none == "" and missing == "" and fine == ""
    assert "unknown" in failed and "warnline" in failed
    assert "alert_log" in refused and "floor" in refused and "RETAIN_*" in refused and 'class="err"' in refused


def test_the_weekly_verdicts_show_when_the_call_flipped(page):
    state = {"winner": "ok", "insufficient": "warn", "conflicting": "bad", "no_difference": "", "retired": ""}
    hist = [{"week": "2026-W39", "state": "winner", "call": "solver", "method": "cluster_robust_v1", "text": "t"},
            {"week": "2026-W37", "state": "insufficient", "call": None, "method": "pooled_v0", "text": "few"},
            {"week": "2026-W38", "state": "no_difference", "call": None, "method": "cluster_robust_v1", "text": ""}]
    out = _node(_js(page, ("verdictHistory",)) + "const S = " + json.dumps(state) + ";\n"
                + "console.log(JSON.stringify([verdictHistory(" + json.dumps(hist) + ", S), verdictHistory([], S), "
                  "verdictHistory(null, S)]));")
    html, empty, missing = out
    assert re.findall(r'<i class="([a-z]*)"', html) == ["p", "e", ""]         # oldest first, each in its state
    assert "week of 9/7/26" in html and "week of 9/21/26" in html and "2026-W3" not in _text(html)
    assert "read by pooled_v0" in html                                     # an older method is marked
    assert "the call changed 1 time" in _text(html)
    assert "No weekly verdict stored yet" in empty and "No weekly verdict stored yet" in missing


def test_backfilled_weeks_show_grey_and_a_pattern_record_reads_week_by_week(page):
    rows = [{"week": "2026-W37", "status": "active", "n_with": 6, "n_without": 7, "effect": 0.3, "effect_unit": "★",
             "cohen_d": 0.41, "p_value": 0.01, "q_value": 0.04, "prospective": 1},
            {"week": "2026-W38", "status": "active", "n_with": 6, "n_without": 7, "effect": 0.28, "effect_unit": "★",
             "cohen_d": 0.39, "p_value": 0.02, "q_value": 0.05, "prospective": 0},
            {"week": "2026-W39", "status": "retired", "n_with": 5, "n_without": 7, "effect": 0.1, "effect_unit": "★",
             "cohen_d": 0.1, "p_value": 0.4, "q_value": 0.5, "prospective": 0}]
    out = _node(_js(page, ("featureWeekBars", "patHistoryHtml"), ("PH_SQ",))
                + "let answers = [], gets = []; async function need(p){ gets.push(p); return answers.shift(); }\n"
                + "(async () => { answers.push({ok: true, rows: " + json.dumps(rows) + "}, {ok: true, rows: []});\n"
                + "const bars = featureWeekBars([{week: '2026-W38', n: 4, backfilled: 1}, {week: '2026-W39', n: 0, backfilled: 0}, "
                  "{week: '2026-W40', n: 2, backfilled: 0}]);\n"
                + "const h = await patHistoryHtml('labor_x:platform'), e = await patHistoryHtml('none');\n"
                + "console.log(JSON.stringify({bars: bars, h: h, e: e, gets: gets})); })();")
    bars = out["bars"]
    assert "var(--ink4) 25%,var(--ember2) 25%" in bars and "1 backfilled" in bars and "week of 9/14/26" in bars
    assert 'class="z"' in bars                                             # an empty week is a slot, not skipped
    assert out["gets"][0] == "/admin/api/intelligence/pattern-history?key=labor_x%3Aplatform"
    h = out["h"]
    assert re.findall(r'<i class="([a-z]*)" title', h) == ["", "", "x"]
    assert "Held 2 weeks of 3 on record, since week of 9/7/26" in _text(h)
    assert "prospective" in h and "6+7" in h and "2026-W3" not in _text(h)
    assert h.index("week of 9/21/26") < h.index("week of 9/7/26", h.index("<table>"))   # newest row first
    assert "No weekly record yet" in out["e"]


def _brand_js(page):
    ids = ("b-name", "b-color", "b-logo", "b-sm", "b-concept", "b-own", "b-opened", "b-bar", "b-excl", "b-lov",
           "b-lhist", "b-lhist-l", "b-learn", "b-lhint", "b-status", "b-load", "b-save")
    return (_js(page, ("learnStatus", "brandLearnInclude", "brandLearnHint", "brandReset", "brandFill", "brandSave"),
                ("BRAND_FIELDS", "PROFILE_FIELDS"))
            + "let _bRid = 5, _b0 = null; const els = {}; const posts = [];\n"
            + "const $ = (id) => { if(!els[id]) els[id] = {value:'', checked:false, hidden:false, textContent:'', innerHTML:''}; return els[id]; };\n"
            + json.dumps(list(ids)) + ".forEach($);\n"
            + "let answers = []; async function post(u, b){ posts.push([u, JSON.parse(JSON.stringify(b))]); return answers.shift(); }\n")


STORED = {"ok": True, "version": 7, "brand_name": "Simple EJ's", "brand_color": None, "brand_logo_url": None,
          "exclude_from_learning": 0, "profile": {"service_model": "full_service"},
          "learning": {"eligible": False, "reason": "test_name", "label": "a test account (by its name)",
                       "automatic": True, "override": None, "since": None}}


def test_the_learning_override_is_sent_only_when_it_changed(page):
    included = dict(STORED, version=8, learning={"eligible": True, "reason": None, "label": None, "automatic": False,
                                                 "override": "include", "since": "2026-09-29 14:00:00"})
    js = _brand_js(page) + """
(async () => {
  const o = {};
  brandFill(""" + json.dumps(STORED) + """);
  o.status = els['b-learn'].innerHTML; o.lov = els['b-lov'].value;
  await brandSave(); o.nothing = [posts.length, els['b-status'].textContent];
  brandLearnInclude(); o.hint = els['b-lhint'].textContent; o.hist_shown = !els['b-lhist-l'].hidden;
  answers.push(""" + json.dumps(included) + """);
  await brandSave(); o.first = posts[0][1]; o.after = els['b-learn'].innerHTML;
  // Re-saving another field leaves the override out of the body.
  els['b-name'].value = "Simple EJ's Grill";
  answers.push(Object.assign({}, """ + json.dumps(included) + """, {version: 9, brand_name: "Simple EJ's Grill"}));
  await brandSave(); o.second = posts[1][1];
  // Include with its history, and back to automatic.
  brandFill(""" + json.dumps(STORED) + """); els['b-lov'].value = 'include'; els['b-lhist'].checked = true;
  answers.push(""" + json.dumps(included) + """); await brandSave(); o.hist = posts[2][1];
  brandFill(""" + json.dumps(included) + """); els['b-lov'].value = ''; brandLearnHint(); o.auto_hint = els['b-lhint'].textContent;
  answers.push(""" + json.dumps(STORED) + """); await brandSave(); o.auto = posts[3][1];
  // A demo never teaches; a test-or-internal account type keeps it out.
  brandFill(Object.assign({}, """ + json.dumps(STORED) + """, {learning: {eligible: false, reason: 'demo', label: 'a demo account', automatic: false, override: null}}));
  o.demo_status = els['b-learn'].innerHTML; els['b-lov'].value = 'include'; brandLearnHint(); o.demo_hint = els['b-lhint'].textContent;
  brandFill(Object.assign({}, """ + json.dumps(STORED) + """, {exclude_from_learning: 1})); els['b-lov'].value = 'include'; brandLearnHint();
  o.excl_hint = els['b-lhint'].textContent;
  brandFill(Object.assign({}, """ + json.dumps(STORED) + """, {learning: null})); o.unknown = els['b-learn'].innerHTML;
  console.log(JSON.stringify(o));
})();
"""
    o = _node(js)
    assert "No" in o["status"] and "a test account (by its name)" in o["status"] and "Include it" in o["status"]
    assert o["lov"] == "" and o["nothing"] == [0, "Nothing changed."]
    assert o["hist_shown"] and "starts its teaching today" in o["hint"]
    assert o["first"] == {"expected_version": 7, "learning_override": "include"}
    assert "Yes" in o["after"] and "9/29/26" in o["after"] and "included by an admin" in o["after"]
    assert o["second"] == {"expected_version": 8, "brand_name": "Simple EJ's Grill"}      # no override re-sent
    assert o["hist"] == {"expected_version": 7, "learning_override": "include", "learning_history": True}
    assert "automatic rule" in o["auto_hint"] and o["auto"] == {"expected_version": 8, "learning_override": ""}
    assert "Include it" not in o["demo_status"] and "A demo never teaches" in o["demo_hint"]
    assert "account type above" in o["excl_hint"]
    assert ">unknown<" in o["unknown"]


# ── the server side ──────────────────────────────────────────────────────────

def test_a_runs_stored_counts_keep_what_it_held_back_as_valid_json():
    state, blob = ops.run_outcome({"attempted": 3, "ok": 2, "failed": 1, "skipped": 0, "hit_bound": True,
                                   "alert_log": 5, "refused": [{"table": "alert_log", "days": 18, "floor": 90}],
                                   "capped": ["rec_events"]})
    b = json.loads(blob)
    assert state == ops.RUN_PARTIAL
    assert b["refused"] == [{"table": "alert_log", "days": 18, "floor": 90}] and b["refused_n"] == 1
    assert b["capped"] == ["rec_events"] and b["capped_n"] == 1 and b["failed"] == 1
    many = [{"table": f"a_rather_long_table_name_{i}", "days": 1, "floor": 30} for i in range(40)]
    _s, blob = ops.run_outcome({"attempted": 40, "ok": 0, "failed": 40, "refused": many, "capped": []})
    b = json.loads(blob)                                                   # cut, never truncated mid-JSON
    assert len(blob) <= 500 and b["refused_n"] == 40 and 0 < len(b["refused"]) < 40 and "capped" not in b
    _s, blob = ops.run_outcome({"attempted": 1, "ok": 1, "failed": 0})
    assert "refused" not in json.loads(blob)


def test_the_learning_curve_meta_is_the_scorecards_own_constants():
    import learning_scorecard as lsc
    meta = admin_ops._learning_curve_meta()
    assert set(meta["curves"]) == set(lsc.CURVES)
    for name, spec in meta["curves"].items():
        assert spec["min_n"] == lsc.MIN_N[name] and spec["better"] == lsc.CURVES[name][0]
        assert spec["label"] == lsc.CURVE_LABELS[name]
    assert meta["curves"]["forecast_error"]["unit"] == "pct" and meta["curves"]["acceptance"]["unit"] == "share"
    assert (meta["fatigue_share"], meta["fatigue_min_n"]) == (lsc.FATIGUE_SHARE, lsc.FATIGUE_MIN_N)


# ── the render check ─────────────────────────────────────────────────────────

@pytest.fixture
def world(monkeypatch, db_path):
    real = models.get_conn
    redirect = lambda *a, **k: real(db_path)  # noqa: E731
    for mod in list(sys.modules.values()):
        try:
            if getattr(mod, "get_conn", None) is real:
                monkeypatch.setattr(mod, "get_conn", redirect)
            if getattr(mod, "DB_PATH", None) == models.DB_PATH and mod is not models:
                monkeypatch.setattr(mod, "DB_PATH", db_path)
        except Exception:
            pass
    monkeypatch.setattr(models, "get_conn", redirect)
    monkeypatch.setattr(models, "DB_PATH", db_path)
    monkeypatch.setattr(admin_ops, "get_conn", redirect, raising=False)
    init_auth(db_path=db_path)
    models.init_email_log(db_path=db_path)
    models.init_staff_notes(db_path=db_path)
    models.init_two_fa_backup_codes(db_path=db_path)
    import push, webhooks, guest_marketing, sales_audits, platform_monitor, provider_health, ai_utils
    push.init_push(db_path)
    webhooks.init_webhooks(db_path)
    guest_marketing.init_guest_marketing(db_path)
    sales_audits.init_sales_audits(db_path=db_path)
    for init in (ops.init_ops, admin_ops.init_admin_ops, platform_monitor.init_platform_tables,
                 provider_health.init_provider_health, ai_utils.init_ai_ops):
        init(db_path)
    admin_ops.invalidate_fleet_cache()
    from intelligence import dashboard as _dash
    _dash.invalidate()

    home = create_restaurant(Restaurant(name="Cavnar AI Admin", owner_email="will@memui.test", billing_status="internal"),
                             db_path=db_path)
    admin = create_user(home, "will", "will@memui.test", "Admin-pass-2026", is_admin=True, db_path=db_path)
    support = create_user(home, "helper", "helper@memui.test", "Helper-pass-2026", role="support", db_path=db_path)
    rid = create_restaurant(Restaurant(name="Harbor Grill", owner_email="owner@memui.test"), db_path=db_path)
    owner = create_user(rid, "harbor", "owner@memui.test", "Owner-pass-2026", db_path=db_path)
    upsert_membership(owner, rid, "client", db_path=db_path)
    update_restaurant(rid, {"billing_status": "active", "module_reviews": 1}, db_path=db_path)

    import rec_ledger as rl
    import schedule_experiments as sx
    rl.present_many(rid, [{"key": "trim_day:Monday", "module": "labor",
                           "rank": {"weight": 1.2, "version": 2, "rung": "own"}}], "home", db_path=db_path)
    rl.record(rid, "trim_day:Monday", "completed", db_path=db_path)
    rl.log_rank_build(rid, "home", shown=[], not_shown=[{"key": "reprice:Soup", "weight": 0.8}], version=2,
                      db_path=db_path)
    conn = real(db_path)
    try:
        x = conn.execute
        x("INSERT INTO learning_scorecards (restaurant_id, month, metrics_json, flags_json, fatigued, version) "
          "VALUES (?, '2026-08', ?, ?, 1, 1)",
          (rid, json.dumps({"metrics": {"acceptance": {"value": 0.4, "k": 10, "n": 25}}, "forecast_error_by_kind": {},
                            "fatigue": {"share": 0.81, "n": 34, "fatigued": True}}),
           json.dumps([{"curve": "acceptance", "label": "recommendation acceptance", "state": "worsening",
                        "latest": 0.4, "before": 0.59, "months": ["2026-06", "2026-07", "2026-08"]}])))
        for i, (band, verdict) in enumerate([("high", "held")] * 4 + [("high", "not_held"), ("low", "held")]):
            x("INSERT INTO ai_claims (restaurant_id, surface, subject, claim_type, text, model_band, capped_band, "
              "verdict, created_at) VALUES (?, 'reviews', ?, 'cause', 'x', ?, 'medium', ?, datetime('now','-3 days'))",
              (rid, f"s{i}", band, verdict))
        x("INSERT INTO intel_patterns (key, cohort, hypothesis, n_with, n_without, effect, effect_unit, cohen_d, p_value, "
          "q_value, confidence, sentence, evidence_json, status, first_seen, last_confirmed, computed_at) VALUES "
          "('reply_x:platform', 'platform', 'reply_x', 6, 7, 0.3, 'stars', 0.41, 0.01, 0.04, 0.7, "
          "'Restaurants that reply within a day gain 0.3 stars.', '{}', 'active', '2026-09-01', '2026-09-28', datetime('now'))")
        for wk in ("2026-W37", "2026-W38"):
            x("INSERT INTO intel_pattern_history (week, key, cohort, hypothesis, status, n_with, n_without, effect, "
              "effect_unit, cohen_d, p_value, q_value) VALUES (?, 'reply_x:platform', 'platform', 'reply_x', 'active', "
              "6, 7, 0.3, 'stars', 0.41, 0.01, 0.04)", (wk,))
        from intelligence import features as _f
        x("INSERT INTO intel_features (restaurant_id, week, features_json, completeness, computed_at, version, backfilled) "
          "VALUES (?, '2026-W38', '{}', 0.5, datetime('now'), ?, 1)", (rid, _f.FEATURES_VERSION))
        x("INSERT INTO intel_confidence_log (week, cohort, rec_kind, n, mean_confidence, acceptance_rate, success_rate, "
          "computed_at, trust_version, orgs) VALUES ('2026-W39', 'platform', 'trim_day', 12, 0.6, 0.5, 0.4, "
          "datetime('now'), 2, 6)")
        x("INSERT INTO schedule_experiment_verdicts (week, experiment, state, call, text, method, arms_json, computed_at) "
          "VALUES ('2026-W39', ?, 'insufficient', NULL, 'Too few weeks.', ?, '[]', datetime('now'))",
          (sx.EXPERIMENTS[0]["key"], sx.VERDICT_METHOD))
        blob = ops.run_outcome({"attempted": 2, "ok": 1, "failed": 1, "refused": [{"table": "alert_log", "days": 18,
                                                                                    "floor": 90}], "capped": []})[1]
        x("INSERT INTO job_runs (job, started_at, finished_at, duration_ms, ok, result_json) VALUES "
          "('prune_ledgers', datetime('now','-2 hours'), datetime('now','-2 hours'), 900, 2, ?)", (blob,))
        conn.commit()
    finally:
        conn.close()

    app = Flask(__name__, template_folder=os.path.join(ROOT, "templates"))
    app.jinja_env.filters["format_num"] = lambda v: v
    app.jinja_env.filters["format_date"] = lambda v: v
    from auth_routes import auth_bp
    for bp in (admin_routes.admin_bp, auth_bp):
        app.register_blueprint(bp)

    def signed_in(uid):
        c = app.test_client()
        c.set_cookie("session_token", create_session(uid, db_path=db_path))
        c.set_cookie("csrf_js", CSRF)
        return c
    yield {"c": signed_in(admin), "support": signed_in(support), "rid": rid, "db": db_path}
    admin_ops.invalidate_fleet_cache()
    _dash.invalidate()


def test_the_console_page_renders_with_the_new_panels(world):
    r = world["c"].get("/admin")
    assert r.status_code == 200
    html = r.get_data(as_text=True)
    for fn in ("function learningCurveHtml(", "function modelConfHtml(", "async function rankLearningHtml(",
               "function runHeld(", "function retentionNow(", "function learnStatus(", "function patHistory(",
               "function verdictHistory(", "function featureWeekBars(", "function isoWeekDay("):
        assert fn in html, fn
    assert "{{" not in html and "{%" not in html


def _has(obj, path):
    """A dotted path, where `[]` steps into a list's first item."""
    cur = obj
    for part in path.split("."):
        if part.endswith("[]"):
            cur = cur[part[:-2]]
            assert isinstance(cur, list) and cur, f"{path}: {part[:-2]} is an empty list here"
            cur = cur[0]
        else:
            assert isinstance(cur, dict) and part in cur, f"{path}: no '{part}'"
            cur = cur[part]
    return True


READS = [
    ("/admin/api/recommendations/learning?days=90",
     ["groups[]." + k for k in ("version", "bucket", "shown", "settled", "taken", "accept_rate", "accept_ci90",
                                "measured", "outcome_rate", "enough")]
     + ["unshown_by_surface.home.builds", "unshown_by_surface.home.unshown", "min_n", "note", "days"]),
    ("/admin/api/ai/client/{rid}?days=30",
     ["learning[]." + k for k in ("month", "metrics", "flags", "fatigue", "fatigued")]
     + ["learning_curves.curves.acceptance." + k for k in ("label", "better", "min_n", "unit")]
     + ["learning_curves.fatigue_share", "learning_curves.fatigue_min_n"]
     + ["quality.model_confidence." + k for k in ("scored", "ordered", "days")]
     + ["quality.model_confidence.by_model_band[]." + k for k in ("band", "scored", "held", "rate")]
     + ["quality.model_confidence.by_capped_band[].band"]),
    ("/admin/api/ai/quality?days=30", ["model_confidence.scored", "model_confidence.by_model_band[].rate"]),
    ("/admin/api/jobs", ["retention.refused", "retention.cap_rows", "jobs[].history"]),
    ("/admin/api/brand/{rid}", ["learning." + k for k in ("eligible", "reason", "label", "automatic", "override",
                                                          "since")] + ["exclude_from_learning", "version"]),
    ("/admin/api/intelligence", ["patterns.rows[]." + k for k in ("key", "weeks_held", "status", "sentence")]
     + ["learning.weeks[]." + k for k in ("week", "n", "backfilled", "completeness")]
     + ["confidence_over_time[]." + k for k in ("week", "trust_version", "orgs", "n")] + ["emerging"]),
    ("/admin/api/intelligence/pattern-history?key=reply_x:platform",
     ["rows[]." + k for k in ("week", "status", "n_with", "n_without", "effect", "effect_unit", "cohen_d", "p_value",
                              "q_value", "prospective")] + ["key"]),
    ("/admin/api/schedule-experiments", ["method", "max_weeks_per_restaurant", "rule",
                                         "experiments[].verdict_history[]." + "week",
                                         "experiments[].verdict_history[].state",
                                         "experiments[].verdict_history[].method"]),
]


@pytest.mark.parametrize("path,fields", READS, ids=[p.split("?")[0] for p, _f in READS])
def test_every_read_the_new_panels_make_answers_with_the_fields_they_read(world, path, fields):
    url = path.format(rid=world["rid"])
    r = world["c"].get(url)
    assert r.status_code == 200, (url, r.status_code, r.get_data(as_text=True)[:300])
    body = r.get_json()
    for f in fields:
        _has(body, f)


def test_the_reads_carry_what_the_panels_draw(world, monkeypatch):
    c, rid = world["c"], world["rid"]
    rk = c.get(f"/admin/api/recommendations/learning?days=90&restaurant_id={rid}").get_json()
    by = {(g["version"], g["bucket"]): g for g in rk["groups"]}
    assert by[(2, "raised (over 1.1x)")]["taken"] == 1 and rk["unshown_by_surface"]["home"]["unshown"] == 1
    ai = c.get(f"/admin/api/ai/client/{rid}?days=30").get_json()
    assert ai["learning"][-1]["flags"][0]["state"] == "worsening" and ai["learning"][-1]["fatigued"] is True
    mc = ai["quality"]["model_confidence"]
    assert mc["scored"] == 6 and {b["band"]: b["held"] for b in mc["by_model_band"]} == {"high": 4, "low": 1}
    intel = c.get("/admin/api/intelligence").get_json()
    assert intel["patterns"]["rows"][0]["weeks_held"] == 2
    assert intel["learning"]["weeks"][0]["week"] == "2026-W38" and intel["learning"]["weeks"][0]["backfilled"] == 1
    assert intel["confidence_over_time"][0]["trust_version"] == 2 and intel["confidence_over_time"][0]["orgs"] == 6
    ph = c.get("/admin/api/intelligence/pattern-history?key=reply_x:platform").get_json()
    assert [r["week"] for r in ph["rows"]] == ["2026-W37", "2026-W38"]
    ex = c.get("/admin/api/schedule-experiments").get_json()
    assert ex["method"] == "cluster_robust_v1" and ex["max_weeks_per_restaurant"] == 8
    assert ex["experiments"][0]["verdict_history"][0]["week"] == "2026-W39"
    jobs = c.get("/admin/api/jobs").get_json()
    assert "value_snapshots" in {j["job"] for j in jobs["jobs"]}          # the new job is on the page
    prune = next(j for j in jobs["jobs"] if j["job"] == "prune_ledgers")
    assert json.loads(prune["history"][0]["result_json"])["refused"][0]["table"] == "alert_log"
    assert jobs["retention"]["refused"] == []                              # nothing set under its floor here
    monkeypatch.setitem(ops._RETENTION_DAYS, "alert_log", 18)
    admin_ops.invalidate_fleet_cache()
    assert {"table": "alert_log", "days": 18, "floor": 90} in c.get("/admin/api/jobs?fresh=1").get_json()["retention"]["refused"]


def test_a_learning_issue_reads_in_months_and_percents_under_ai(world):
    got = [i for i in admin_ops.issues()["issues"] if i["restaurant_id"] == world["rid"]]
    acc = next(i for i in got if i["key"] == f"{world['rid']}:learning:acceptance")
    assert acc["category"] == "learning"
    assert acc["detail"] == "Aug 2026: 40%, against 59% over Jun 2026 and Jul 2026."
    assert f"{world['rid']}:learning:fatigue" in {i["key"] for i in got}


def test_the_pattern_history_is_for_admins_only_and_needs_a_key(world):
    assert world["support"].get("/admin/api/intelligence/pattern-history?key=reply_x:platform").status_code == 403
    r = world["c"].get("/admin/api/intelligence/pattern-history")
    assert r.status_code == 400 and r.get_json()["error"]
    assert world["c"].get("/admin/api/intelligence/pattern-history?key=nothing").get_json()["rows"] == []


def test_re_sending_include_does_not_restart_its_teaching(world):
    c, rid = world["c"], world["rid"]
    post = lambda body: c.post(f"/admin/api/brand/{rid}", json=body, headers={"X-CSRF": CSRF})  # noqa: E731
    v = c.get(f"/admin/api/brand/{rid}").get_json()["version"]
    r = post({"expected_version": v, "learning_override": "include"})
    assert r.status_code == 200, r.get_json()
    d = r.get_json()
    since = d["learning"]["since"]
    assert d["learning"]["override"] == "include" and d["learning"]["eligible"] and since
    conn = models.get_conn()
    try:
        conn.execute("UPDATE restaurants SET learning_since='2026-01-01 00:00:00' WHERE id=?", (rid,))
        conn.commit()
    finally:
        conn.close()
    v = c.get(f"/admin/api/brand/{rid}").get_json()["version"]
    r = post({"expected_version": v, "learning_override": "include", "brand_name": "Harbor"})
    assert r.status_code == 200 and r.get_json()["learning"]["since"] == "2026-01-01 00:00:00"
    # Back to automatic, then included with its history: the clock is left where it was.
    v = r.get_json()["version"]
    v = post({"expected_version": v, "learning_override": ""}).get_json()["version"]
    r = post({"expected_version": v, "learning_override": "include", "learning_history": True})
    assert r.status_code == 200 and r.get_json()["learning"]["since"] == "2026-01-01 00:00:00"
    # A change to include without it starts the clock now.
    v = post({"expected_version": r.get_json()["version"], "learning_override": "exclude"}).get_json()["version"]
    r = post({"expected_version": v, "learning_override": "include"})
    assert r.get_json()["learning"]["since"] not in (None, "2026-01-01 00:00:00")
