"""The web confidence component (contract K1, confidence audit J1).

One line - a meter, "72% confidence", the weakest dimension's basis, Why? -
and a panel with three rows: Evidence strength, Historical accuracy (or
"Not enough history yet"), Data freshness (as of M/D/YY). The block lives in
dashboard.html as <script id="cav-conf">; these tests run it under node
against fixtures shaped exactly like K1, the bare band string an older
server sends, and garbage, so a rule holds for every payload rather than
the one a page happened to render.

Also pinned here: every surface J1 names draws the component (not a pill of
its own), the strength pill is gone, and nothing on a confidence line is red
or ember."""
import json
import os
import re
import shutil
import subprocess

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DASHBOARD = os.path.join(ROOT, "templates", "dashboard.html")


def _src():
    return open(DASHBOARD, encoding="utf-8").read()


def _block():
    m = re.search(r'<script id="cav-conf">(.*?)</script>', _src(), re.S)
    assert m, "the cav-conf script block is missing"
    return m.group(1)


K1 = {
    "pct": 72, "band": "medium", "label": "72% confidence",
    "reason": "2 of 5 measured changes like this improved here", "score": 0.72, "caution": None,
    "dimensions": {
        "evidence": {"pct": 80, "basis": "12 reviews in 90 days · 26 of 28 days with sales", "n": 12},
        "accuracy": {"pct": 75, "basis": "3 of 4 measured changes like this improved here", "n": 4,
                     "improved": 3, "source": "own", "low": 41, "high": 94},
        "freshness": {"pct": 94, "basis": "POS synced 9/23/26 · reviews fetched today", "as_of": "9/23/26",
                      "as_of_iso": "2026-09-23", "stalest": "pos"},
    },
    "version": 1,
}

NO_TRACK = {
    "pct": 64, "band": "medium", "label": "64% confidence", "reason": "Not enough history yet — 2 measured, needs 5",
    "score": 0.64, "caution": "No track record here yet, so this can't read as high confidence.",
    "dimensions": {
        "evidence": {"pct": 70, "basis": "6 Tuesdays in your shift data", "n": 6},
        "accuracy": {"pct": None, "basis": "Not enough history yet — 2 measured, needs 5", "n": 2,
                     "improved": 1, "source": "none"},
        "freshness": {"pct": 58, "basis": "Shifts through 9/14/26", "as_of_iso": "2026-09-14"},
    },
    "version": 1,
}


def _run(js_body):
    if not shutil.which("node"):
        pytest.skip("node is not installed")
    js = "var window={};\n" + _block() + "\nvar C=window.cavConf;\n" + js_body
    out = subprocess.run(["node", "-e", js], capture_output=True, text=True, timeout=20)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout.strip())


def _eval(expr, **fixtures):
    decl = "".join(f"var {k}={json.dumps(v)};\n" for k, v in fixtures.items())
    return _run(decl + f"console.log(JSON.stringify({expr}));")


# ── normalising what the server sends ─────────────────────────────────────

def test_k1_object_reads_its_percentage_and_tone():
    n = _eval("C.norm(c)", c=K1)
    assert n["pct"] == 72 and n["label"] == "72% confidence" and n["tone"] == "mid" and n["band"] == "medium"


@pytest.mark.parametrize("pct,tone,band", [(75, "good", "high"), (74, "mid", "medium"), (50, "mid", "medium"),
                                           (49, "warn", "low"), (0, "warn", "low"), (100, "good", "high")])
def test_tone_boundaries_are_the_engines(pct, tone, band):
    """The payload's band is the tone; with none, the client's thresholds,
    which are held to confidence_engine.HIGH_AT / MEDIUM_AT (B6 low: the
    tests pinned the literals)."""
    import confidence_engine as ce
    assert ce.band(pct) == band
    n = _eval("C.norm(c)", c=dict(K1, pct=pct, band=ce.band(pct)))
    assert (n["tone"], n["band"]) == (tone, band)
    n = _eval("C.norm(c)", c={k: v for k, v in dict(K1, pct=pct).items() if k != "band"})
    assert (n["tone"], n["band"]) == (tone, band)


def test_client_thresholds_are_the_engines():
    import confidence_engine as ce
    assert _eval("C.AT") == {"high": ce.HIGH_AT, "medium": ce.MEDIUM_AT}


def test_the_server_band_decides_the_tone():
    """If the engine moves its cut-offs, the payload's band moves the tone
    without a client release; a dimension row reads the payload's
    thresholds when sent."""
    c = dict(K1, pct=72, band="high", thresholds={"high": 70, "medium": 40})
    n = _eval("C.norm(c)", c=c)
    assert n["tone"] == "good" and n["band"] == "high"
    rows = _eval("C.rows(C.norm(c))", c=c)
    assert rows[0]["tone"] == "good"                 # evidence 80 >= 70


def test_overall_null_is_not_yet_measurable_never_a_number():
    c = dict(K1, pct=None, label="Confidence not yet measurable", score=0.0)
    n = _eval("C.norm(c)", c=c)
    assert n["pct"] is None and n["label"] == "Confidence not yet measurable" and n["tone"] == "warn"
    line = _eval("C.line(c,{key:'k'})", c=c)
    assert 'class="cf-m none"' in line and "Confidence not yet measurable" in line
    assert not re.search(r"\d+%\s*confidence", line)


def test_legacy_band_string_renders_as_its_band_with_no_why():
    for raw, label, tone in [("high", "High confidence", "good"), ("medium", "Medium confidence", "mid"),
                             ("moderate", "Medium confidence", "mid"), ("low", "Low confidence", "warn")]:
        n = _eval("C.norm(c)", c=raw)
        assert (n["label"], n["tone"], n["pct"]) == (label, tone, None)
        assert "Why?" not in _eval("C.line(c)", c=raw)


def test_older_home_card_object_still_renders():
    old = {"score": 0.55, "band": "medium", "label": "Medium confidence", "reason": "6 reviews in 90 days"}
    n = _eval("C.norm(c)", c=old)
    assert n["label"] == "Medium confidence" and n["tone"] == "mid" and n["pct"] is None
    line = _eval("C.line(c)", c=old)
    text = re.sub(r"<[^>]+>", "", line)
    assert "Medium confidence" in text and "6 reviews in 90 days" in text and "Why?" not in text
    # No percentage to draw, so no meter at all - not an empty one.
    assert "cf-m" not in line


@pytest.mark.parametrize("junk", [None, "", "unknown", "abc", 42, [], {"dimensions": []},
                                  {"pct": "abc", "dimensions": "x"}])
def test_garbage_never_throws(junk):
    out = _eval("[C.norm(c), C.line(c), C.rows(C.norm(c))]", c=junk)
    assert isinstance(out, list)


# ── the Why? panel ────────────────────────────────────────────────────────

def test_three_rows_in_order_with_their_bases():
    rows = _eval("C.rows(C.norm(c))", c=K1)
    assert [r["title"] for r in rows] == ["Evidence strength", "Historical accuracy", "Data freshness"]
    assert [r["value"] for r in rows] == ["80%", "75%", "94%"]
    assert rows[0]["basis"].startswith("12 reviews in 90 days")
    assert rows[1]["detail"] == "3 of 4 measured · likely 41–94%"
    assert rows[2]["detail"] == "as of 9/23/26"
    assert [r["tone"] for r in rows] == ["good", "good", "good"]


def test_accuracy_below_the_floor_is_a_dash_with_what_it_needs():
    rows = _eval("C.rows(C.norm(c))", c=NO_TRACK)
    acc = rows[1]
    assert acc["value"] == "—" and acc["pct"] is None and acc["tone"] == "warn"
    assert acc["basis"] == "Not enough history yet — 2 measured, needs 5"
    # Freshness from the ISO date only: said as M/D/YY, never ISO.
    assert rows[2]["detail"] == "as of 9/14/26"
    no_basis = json.loads(json.dumps(NO_TRACK))
    no_basis["dimensions"]["accuracy"] = {"pct": None, "n": 3}
    acc2 = _eval("C.rows(C.norm(c))[1]", c=no_basis)
    assert acc2["basis"] == "Not enough history yet (3 measured, needs 5)"


def test_footer_says_the_70_cap_only_without_a_track_record():
    assert "70%" in _eval("C.footer(C.norm(c))", c=NO_TRACK)
    assert "70%" not in _eval("C.footer(C.norm(c))", c=K1)


def test_cohort_accuracy_says_where_it_comes_from():
    c = json.loads(json.dumps(K1))
    c["dimensions"]["accuracy"] = {"pct": 80, "basis": "12 of 15 improved", "n": 15, "improved": 12,
                                   "source": "cohort", "low": 58, "high": 92}
    acc = _eval("C.rows(C.norm(c))[1]", c=c)
    assert acc["detail"].startswith("From restaurants like yours")


def test_a_missing_dimension_is_not_measured_not_zero():
    c = json.loads(json.dumps(K1))
    del c["dimensions"]["freshness"]
    fr = _eval("C.rows(C.norm(c))[2]", c=c)
    assert fr["value"] == "—" and fr["basis"] == "Not measured" and fr["pct"] is None


def test_panel_never_prints_an_iso_date_and_numbers_are_in_the_number_face():
    html = _eval("C.panel(C.norm(c))", c=NO_TRACK)
    assert not re.search(r"\d{4}-\d{2}-\d{2}", html)
    assert "as of 9/14/26" in re.sub(r"<[^>]+>", "", html)
    assert '<span class="hb-num">14</span>' in html
    assert "Evidence strength" in html and "Historical accuracy" in html and "Data freshness" in html


# ── the line ──────────────────────────────────────────────────────────────

def test_line_is_one_button_that_opens_the_explain_modal_and_logs_the_key():
    line = _eval("C.line(c,{key:'trim_day:Tuesday',surface:'home',module:'home'})", c=K1)
    assert line.count("<button") == 1
    btn = re.search(r"<button[^>]*>Why\?</button>", line).group(0)
    assert 'class="cbtn cbtn-text cbtn-inline cbtn-sm cf-why"' in btn
    assert 'data-explain="' in btn and 'data-explain-key="trim_day:Tuesday"' in btn
    assert 'data-explain-surface="home"' in btn
    assert '<span class="hb-num">72%</span> confidence' in line
    assert 'style="width:72%"' in line


def test_the_explain_listener_logs_the_surface_the_line_names():
    src = _src()
    assert "t.getAttribute('data-explain-surface')||'home'" in src


# ── where it is drawn ─────────────────────────────────────────────────────

def test_every_j1_surface_draws_the_shared_component():
    src = _src()
    # Home rec cards, attention rows, the one-thing hero.
    # Each prefers `confidence_detail` (the K1 object) where the server keeps
    # `confidence` as the band word for older builds (group E).
    assert "cavConfLine(c,{key:r.key,surface:'home'" in src and "var c=r.confidence_detail||r.confidence" in src
    assert "cavConfLine(a.confidence_detail||a.confidence,{key:a.rec_key||a.key,surface:'home'" in src
    assert "cavConfLine(fconf,{key:fkey,surface:'home'" in src and "fconf=ff.confidence_detail||ff.confidence" in src
    # Diagnosis blocks: reviews, food, labor + marketing (renderDiagnosis).
    # Only a measured object draws: never a literal 'low' or the model's
    # own band as a fallback (B1 L1).
    assert "cavConfLine(cavConf.k1(dg.confidence_detail,dg.confidence),{key:dg.rec_key,surface:'reviews'" in src
    assert "cavConfLine(cavConf.k1(dg.confidence_detail,dg.confidence),{key:dg.rec_key,surface:'food'" in src
    assert "cavConfLine(cavConf.k1(dg.confidence_detail,dg.confidence),{key:dg.rec_key,surface:dsurf" in src
    assert "confidence||'low'" not in src
    # Food drivers, Ask, the daily report's actions.
    assert "cavConfLine(cavConf.k1(x.confidence_detail,x.confidence),{key:x.rec_key||x.key,surface:'food'" in src
    assert "cavConfLine(askConf, {surface: 'ask'" in src
    assert "cavConfLine(cavConf.k1(x.confidence_detail,x.confidence),{key:rk,surface:'dsr'" in src
    # Schedule recommendations (an object with its confidence, on the dark
    # panel), logged on the surface the server presents them on (B4 L6).
    assert "cavConfLine(recConf, {key: recObj.rec_key || recObj.key, surface: 'schedule_review'" in src


def test_the_old_per_module_pills_and_the_strength_pill_are_gone():
    src = _src()
    for gone in ("rv-conf", "fc2-conf", "diag-conf", "ask-eva-chip conf", "STRENGTH[r.strength]",
                 "strong evidence"):
        assert gone not in src, gone


def test_confidence_colours_are_never_red_or_ember():
    css = _src()
    rules = re.findall(r"^\.cf[^{]*\{[^}]*\}|^\.cf-p[^{]*\{[^}]*\}", css, re.M)
    assert rules
    for r in rules:
        assert "--red" not in r and "--ember" not in r and "--hb-bad" not in r, r


def test_money_fallback_keeps_the_range():
    src = _src()
    assert "function hbMoneyRange(m)" in src
    # The old collapse took only the first dollar amount of "$1,200-$2,400".
    assert "var mm=/\\$[\\d,]+(?:\\.\\d+)?/.exec(" not in src


# ── the rest of group J on the web, against the source and under node ───────

def _fn_src(src, name, indent=""):
    m = re.search(r"\n" + indent + r"function " + re.escape(name) + r"\(.*?\n" + indent + r"\}", src, re.S)
    assert m, name
    return m.group(0)


def _node(js):
    if not shutil.which("node"):
        pytest.skip("node is not installed")
    out = subprocess.run(["node", "-e", js], capture_output=True, text=True, timeout=20)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout.strip())


def test_ai_visibility_words_come_from_the_range_not_the_point():
    fn = _fn_src(_src(), "aivRangeReading")
    got = _node(fn + """
console.log(JSON.stringify([aivRangeReading(57, 30, 80), aivRangeReading(80, 70, 95),
  aivRangeReading(40, 35, 60), aivRangeReading(10, 0, 30), aivRangeReading(50, null, null)]));""")
    assert got[0]["label"] == "range too wide to call" and got[0]["tone"] == ""
    assert got[1]["label"] == "comes up often" and got[1]["range"] is True
    assert got[2]["label"] == "comes up sometimes"
    assert got[3]["label"] == "doesn't come up yet"
    assert got[4]["label"] == "comes up sometimes" and got[4]["range"] is False


def test_labor_unverified_line_becomes_the_caveat_not_prose():
    fn = _fn_src(_src(), "laborSplitUnverified")
    got = _node(fn + """
console.log(JSON.stringify([laborSplitUnverified('Labor ran 34%.\\n\\nUNVERIFIED: 12%, $400'),
  laborSplitUnverified('All good.')]));""")
    assert got[0]["html"] == "Labor ran 34%." and got[0]["figs"] == ["12%", "$400"]
    assert got[1]["figs"] is None and got[1]["html"] == "All good."


def test_money_range_is_never_collapsed():
    fn = _fn_src(_src(), "hbMoneyRange", "  ")
    got = _node("function esc(v){return String(v==null?'':v);}"
                "function num2(n){return (Math.round(n||0)).toLocaleString('en-US');}" + fn + """
console.log(JSON.stringify([hbMoneyRange({low:1200,high:2400,label:'Biggest dollar opportunity: rating movement'}),
  hbMoneyRange({low:500,high:500}), hbMoneyRange(null)]));""")
    assert got[0].startswith("$1,200–$2,400<small>/month · biggest dollar opportunity")
    assert got[1] == "$500<small>/month</small>" and got[2] == ""


def test_freshness_strip_and_monitoring_line():
    src = _src()
    assert "h+=renderFreshness(d);" in src
    assert "mo.count_live" in src and "oldest data" in src
    # Unknown age is never drawn as current.
    assert "unknown:'unknown'" in src
    assert "FRESH_WORD={aging:'aging',stale:'stale',off:'not connected',unknown:'age unknown'" in src


def test_attention_rows_show_their_evidence_inline():
    src = _src()
    assert "aev=a.evidence||a.detail||''" in src and '<div class="hb-att-ev">' in src
    assert "<span class=\"t\" title=\"'+esc(a.detail||'')+'\">'+num(a.title)" not in src


def test_labor_headline_says_incomplete_data_like_the_phone():
    src = _src()
    assert "{% elif _partial %}{% set _head = 'Incomplete data' %}" in src
    assert src.index("{% set _partial =") < src.index("{% elif _partial %}")


def test_unsourced_research_copy_is_gone():
    src = _src()
    for gone in ("35% higher return rates", "$125k+/yr", "3–7× more direction requests",
                 "your busiest Sundays follow this pattern", "on average, and a 3.1% sales lift"):
        assert gone not in src, gone


def test_review_trend_needs_three_reviews_a_side_and_names_the_weeks():
    src = _src()
    assert "var RV_MIN_A_SIDE=3;" in src
    assert "this week vs '+weeks.length+' weeks ago" not in src


def test_recoverable_is_an_opportunity_not_a_green_win():
    src = _src()
    assert '<div class="big good"><span id="inv-annual-recoverable"' not in src
    assert "Recoverable / year · opportunity" in src


def test_dsr_footer_counts_estimates_apart():
    src = _src()
    assert "Every figure above traced to a measured fact" not in src
    assert "on an estimate, and says so" in src


def test_shift_quality_confidence_uses_tokens_and_amber_for_low():
    fn = _fn_src(_src(), "renderQualityConfidence")
    assert "#eb5757" not in fn and "#f2994a" not in fn and "#6fcf97" not in fn
    assert "var(--amber)" in fn and "confidence.level === 'moderate'" in fn


def _sq_panel(conf, detail):
    fn = _fn_src(_src(), "renderQualityConfidence")
    js = ("var els={'sq-confidence':{innerHTML:''},'sq-prov':{innerHTML:''}};"
          "var document={getElementById:function(i){return els[i]||null;}};"
          "function _escAttr(v){return String(v==null?'':v);}function num(v){return String(v==null?'':v);}"
          "var cavConf=window.cavConf,cavConfLine=window.cavConfLine;\n" + fn
          + f"\nrenderQualityConfidence({json.dumps(conf)},{json.dumps(detail)});"
          "console.log(JSON.stringify([els['sq-confidence'].innerHTML,els['sq-prov'].innerHTML]));")
    return _run(js)


def test_shift_quality_pill_is_a_percentage_never_a_band_word():
    """B4 L2 / H3: the pill was "Low confidence · provisional" beside items
    reading 67%. It is the read's measured completeness as its %, named for
    what it measures; with the panel's own K1 object it is that line."""
    conf = {"score": 45, "level": "low", "reasons": ["5 of 9 scheduled staff have no Operational Score."],
            "summary": "s"}
    html, prov = _sq_panel(conf, None)
    text = re.sub(r"<[^>]+>", "", html)
    assert "Read completeness 45%" in text and "provisional" in prov
    assert not re.search(r"(High|Medium|Low|Moderate) confidence", text)
    html, prov = _sq_panel(conf, dict(K1, pct=46, band="low"))
    assert 'data-conf-pct="46"' in html and "46% confidence" in html and "provisional" in prov
    assert "Read completeness" not in html


# ── admin: the calibration view (K7) ───────────────────────────────────────

ADMIN = os.path.join(ROOT, "templates", "admin.html")


def _k7_payload():
    """Built by the server's own functions, so the fixture is the shape the
    server sends (B6#7: a hand-written fixture with 0-1 shares and a list per
    dimension passed while the real view never drew a dimension table)."""
    import confidence_engine as ce
    # 70-79: 42 shown at 74, 29 improved -> 69%, its 90% range brackets 74.
    # 40-49: 6 rows, under the floor. 90-100: 30 shown at 95, 3 improved ->
    # 10%, far outside - the warn tick.
    overall = ([(74, 1)] * 29 + [(74, 0)] * 13 + [(45, 1)] * 3 + [(45, 0)] * 3
               + [(95, 1)] * 3 + [(95, 0)] * 27)
    ev = [(84, 1)] * 21 + [(84, 0)] * 9
    return {"ok": True, "floor_n": 20, "bands": ce.reliability(overall, 20), "brier": ce.brier(overall, 20),
            "by_kind": [{"kind": "trim_day", "n": 25, "predicted_mean": 71.0, "observed_rate": 40.0, "enough": True}],
            "by_dimension": {"evidence": {"bands": ce.reliability(ev, 20), "brier": ce.brier(ev, 20), "n": len(ev)},
                             "accuracy": {"bands": [], "brier": None, "n": 0},
                             "freshness": {"bands": [], "brier": None, "n": 0}}}


def _admin_cal_js(payloads):
    a = open(ADMIN, encoding="utf-8").read()
    fns = "".join(_fn_src(a, n) for n in ("_calPct", "_calBar", "_calTable", "confidenceCalibration"))
    return ("function esc(v){return String(v==null?'':v);}function fmtN(n){return String(n);}"
            "function kpi(v,l){return '<kpi>'+v+'|'+l+'</kpi>';}"
            "function table(cols,rows,o){return '<table id=\"'+((o&&o.id)||'')+'\">'+rows.map(function(r){return '<tr>'+cols.map(function(c)"
            "{return '<td>'+(c.cell?c.cell(r):r[c.key])+'</td>';}).join('')+'</tr>';}).join('')+'</table>';}"
            + fns + "\nconsole.log(JSON.stringify(" + payloads + ".map(function(k){return confidenceCalibration(k);})));")


def test_admin_calibration_view_renders_k7():
    k7 = _k7_payload()
    html, empty = _node(_admin_cal_js(json.dumps([k7, None])))
    assert "Confidence calibration" in html and "|Brier score" in html
    assert "calbar ok" in html                        # shown 74 sits inside its range
    assert "calbar warn" in html                      # shown 95, 10% improved
    assert "n&lt;20" in html and "calbar off" in html  # the 6-row band is below the floor
    assert "By kind" in html
    # The per-dimension table renders from {bands, brier, n} (B6#7).
    assert "Evidence strength" in html and 'id="cal-dim-evidence"' in html and "84%" in html
    assert "No calibration yet" in empty


def test_admin_calibration_reads_every_figure_as_0_to_100():
    """A value under 1 is under 1%, never a share times 100 (B6#7)."""
    k7 = {"ok": True, "floor_n": 1, "brier": None, "by_kind": [], "by_dimension": {},
          "bands": [{"range": "0-9", "n": 30, "predicted_mean": 0.9, "observed_rate": 0.5, "low": 0.0,
                     "high": 0.8, "enough": True}]}
    html = _node(_admin_cal_js(json.dumps([k7])))[0]
    html = html[html.index("<table"):]                # the table, not the "90% range" legend
    assert "Predicted 90%" not in html and "50%" not in html and "80%" not in html
    assert "Predicted 1%" in html
    assert "1%" in html


def test_admin_says_setup_completeness():
    a = open(ADMIN, encoding="utf-8").read()
    assert "<h2>Data completeness</h2>" not in a and "<h2>Setup completeness</h2>" in a
    assert "function setupDone(r)" in a
