"""Confidence re-audit round 2, group U: the web client (dashboard.html,
admin.html). Each test fails on the code before the fix and names the blind
report item it holds (B4 / B6 / B1).

Where a rule lives in a function, the function runs under node against
payloads shaped the way the server sends them; where it is a wiring rule
(which field a renderer reads), it is pinned against the source."""
import json
import os
import re
import shutil
import subprocess

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DASHBOARD = os.path.join(ROOT, "templates", "dashboard.html")
ADMIN = os.path.join(ROOT, "templates", "admin.html")


def _src():
    return open(DASHBOARD, encoding="utf-8").read()


def _block():
    m = re.search(r'<script id="cav-conf">(.*?)</script>', _src(), re.S)
    assert m
    return m.group(1)


def _fn(src, name, indent=r"\s*"):
    m = re.search(r"\n(" + indent + r")function " + re.escape(name) + r"\(.*?\n\1\}", src, re.S)
    assert m, name
    return m.group(0)


def _node(js):
    if not shutil.which("node"):
        pytest.skip("node is not installed")
    out = subprocess.run(["node", "-e", js], capture_output=True, text=True, timeout=20)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout.strip())


def _conf(expr, **fixtures):
    decl = "".join(f"var {k}={json.dumps(v)};\n" for k, v in fixtures.items())
    return _node("var window={};\n" + _block() + "\nvar C=window.cavConf;\n" + decl
                 + f"console.log(JSON.stringify({expr}));")


def _text(html):
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", html or "")).strip()


K1 = {
    "pct": 62, "band": "medium", "label": "62% confidence", "reason": "5 of 5 measured changes like this improved here",
    "score": 0.62, "caution": "The data under this is out of date (POS synced 9/12/26).",
    "dimensions": {
        "evidence": {"pct": 80, "basis": "the state on file", "n": 12, "kind": "reviews"},
        "accuracy": {"pct": 75, "basis": "5 of 5 measured changes like this improved here", "n": 5,
                     "improved": 5, "source": "own", "low": 60, "high": 100},
        "freshness": {"pct": 40, "basis": "POS synced 9/12/26", "as_of": "9/12/26", "as_of_iso": "2026-09-12"},
    },
    "version": 1,
}


# ── U1 ────────────────────────────────────────────────────────────────────

def test_streamed_ask_answer_carries_every_field_the_server_sends():
    """B6#3: the streamed path rebuilt the answer from a hand-picked list and
    dropped confidence_detail, so the normal path showed a band word."""
    src = _src()
    i = src.index("} else if (evt.type === 'answer') {")
    seg = src[i:i + 900]
    assert "for (var _k in evt)" in seg and "_k !== 'type'" in seg
    assert "confidence: evt.confidence," not in seg


def test_home_data_as_of_reads_where_the_web_payload_puts_it():
    """B6#8: the web payload nests the date at brief.data_as_of and
    monitoring.stalest_as_of; only mobile lifts it to the top."""
    fn = _fn(_src(), "renderFreshness")
    js = ("function esc(v){return String(v==null?'':v);}function num(v){return String(v==null?'':v);}"
          "function mdy(v){return 'M('+v+')';}var MODLABEL={};"
          "var FRESH_STATE={current:'current'};var FRESH_WORD={};\n" + fn
          + "\nconsole.log(JSON.stringify([renderFreshness({freshness:[],brief:{data_as_of:'9/9/26'}}),"
            "renderFreshness({freshness:[],monitoring:{stalest_as_of:'2026-09-09'}}),"
            "renderFreshness({freshness:[]})]));")
    a, b, none = _node(js)
    assert "Data as of" in a and "9/9/26" in a
    assert "Data as of" in b and "M(2026-09-09)" in b
    assert none == ""


def test_demand_inside_range_is_a_share_of_the_ranged_nights():
    """B6#10 / B1 H7: 1 of 1 ranged night out of 10 scored read "100% of 10
    nights". B6#2: a positive actual-vs-forecast means nights came in above
    the forecast; it was printed as the forecast "running high"."""
    thin = {"n_nights": 10, "n_ranged": 1, "inside_range_pct": 100, "mean_error_pct": 25, "bias_pct": 8}
    t = _text(_conf("C.demand(a)", a=thin))
    assert "100% of the 1 night that had a range" in t and "10 nights measured" in t
    assert "of 10 nights" not in t
    assert "nights came in 8% above the forecast" in t and "running" not in t
    renamed = dict(thin, actual_vs_forecast_pct=-6)
    assert "6% below the forecast" in _text(_conf("C.demand(a)", a=renamed))
    # An older payload with no n_ranged makes no inside-range claim.
    older = {"n_nights": 21, "inside_range_pct": 64, "mean_error_pct": 12}
    assert "inside" not in _text(_conf("C.demand(a)", a=older))
    src = _src()
    assert "_rvDemandLine" in src and "demandAccuracyText(a)" in src
    assert "(+a.bias_pct>0?'high':'low')" not in src


def test_cached_labor_read_keeps_its_older_read_flag():
    """B6#12: the five-minute cache kept the text and dropped stale/stale_note."""
    src = _src()
    assert "cavSet('labor_insight_meta'" in src          # restaurant-scoped (Data Freshness #14)
    assert "laborApplyCaveats(el,cs.figs,cmeta,cs.note)" in src
    assert "laborApplyCaveats(el,cs.figs,null,cs.note)" not in src


# ── U4 / U5: the shared component ─────────────────────────────────────────

def test_caution_rides_on_the_line_like_ios():
    """B4 M6: the web hid "the data under this is out of date" behind Why?."""
    line = _conf("C.line(c,{key:'k'})", c=K1)
    assert 'class="cf-c"' in line and "out of date" in line
    assert "cf-c" not in _conf("C.line(c,{key:'k',noCaution:1})", c=K1)


def test_why_shows_the_sample_and_explains_the_pull_toward_even():
    """B4 M7: the evidence count and the shrinkage were never said."""
    rows = _conf("C.rows(C.norm(c))", c=K1)
    assert rows[0]["detail"] == "Sample: 12"
    # Group P item 11: accuracy is the lift against doing nothing now — the
    # "pulled toward 50%" note described the shrink-to-even it replaced.
    assert "note" not in rows[1] and "likely to beat doing nothing" in rows[1]["detail"]
    with_full = json.loads(json.dumps(K1))
    with_full["dimensions"]["evidence"]["n_full"] = 8
    assert _conf("C.rows(C.norm(c))", c=with_full)[0]["detail"] == "Sample: 12 of the 8 a full read needs"
    panel = _text(_conf("C.panel(C.norm(c))", c=K1))
    assert "Sample: 12" in panel and "pulled toward 50%" not in panel
    assert panel.startswith("How well supported this is — not the chance it works.")
    # The stale ceiling is explained in words, from the payload's caps when sent.
    assert "Data under 50% fresh holds it at 49% or below" in panel
    capped = dict(K1, caps={"no_track_record": 65, "stale": 45, "stale_below": 55})
    assert "Data under 55% fresh holds it at 45% or below" in _text(_conf("C.panel(C.norm(c))", c=capped))


def test_sample_data_reads_not_yet_measurable_never_zero():
    """B4 L1: sample data rendered "0% confidence — Sample data — not scored"."""
    sample = {"pct": 0, "band": "low", "label": "0% confidence", "reason": "Sample data — not scored",
              "dimensions": {"evidence": {"pct": 0, "basis": "Sample data — not scored", "n": 0},
                             "accuracy": {"pct": None, "n": 0, "source": "none"},
                             "freshness": {"pct": 90, "basis": "fresh"}}, "version": 1}
    n = _conf("C.norm(c)", c=sample)
    assert n["pct"] is None and n["label"] == "Confidence not yet measurable" and n["tone"] == "warn"
    line = _text(_conf("C.line(c)", c=sample))
    assert "0%" not in line and "not yet measurable" in line
    assert _conf("C.rows(C.norm(c))", c=sample)[0]["value"] == "—"
    # A real zero with a counted sample is still a measurement.
    real = json.loads(json.dumps(sample))
    real["dimensions"]["evidence"] = {"pct": 0, "basis": "12 days", "n": 12}
    assert _conf("C.norm(c)", c=real)["pct"] == 0


def test_only_a_measured_object_draws_a_confidence_line():
    """B1 L1: a diagnosis with no confidence_detail drew "Low confidence"
    from a literal 'low' or the model's own band."""
    assert _conf("C.k1(null,'high')") is None
    assert _conf("C.k1(undefined,'low')") is None
    assert _conf("C.k1(null,c)", c=K1)["pct"] == 62
    assert _conf("C.k1(c,'high')", c=K1)["pct"] == 62
    assert "confidence||'low'" not in _src()


# ── U2: Shift Quality ─────────────────────────────────────────────────────

def test_shift_quality_why_is_logged_on_a_known_surface():
    """B4 L6: surface 'schedule' is not a rec_ledger surface (logged unknown);
    the server presents these items on schedule_review."""
    import rec_ledger
    src = _src()
    assert "surface: 'schedule_review', module: 'schedule'" in src
    assert "surface: 'schedule', module: 'labor'" not in src
    assert "schedule_review" in rec_ledger.SURFACES and "schedule" in rec_ledger.MODULES


def test_shift_quality_panel_has_no_raw_hex():
    """B4 L9: the panel's bullet used a raw #f2994a."""
    fn = _fn(_src(), "renderQualityWarnings", indent="")
    assert "#f2994a" not in fn


def test_schedule_history_pill_says_no_band_word():
    """B4 L2: the history pill's title read "low confidence"."""
    fn = _fn(_src(), "_schedHistRowHtml")
    assert "' confidence'" not in fn and "read completeness" in fn


# ── U3: what each dollar figure covers ────────────────────────────────────

def _home_fns(*names):
    src = _src()
    return "".join(_fn(src, n) for n in names)


def test_dollar_figures_say_what_they_cover():
    """B4 H7: three labor figures on one Home, none saying its scope."""
    js = ("function esc(v){return String(v==null?'':v);}function num(v){return String(v==null?'':v);}"
          "function num2(v){return String(Math.round(v));}\n" + _home_fns("hbDollarsBasis", "hbRecDollars", "hbMoneyRange")
          + "\nconsole.log(JSON.stringify([hbRecDollars({dollars_monthly:520,dollars_basis:'one Tuesday\\'s overstaffing, per month'}),"
            "hbRecDollars({dollars_monthly:520,dollars_adjusted:400,calibration_n:6,dollars_basis:'the whole schedule\\'s gap to your target, per month.'}),"
            "hbMoneyRange({low:1200,high:2400,label:'Labor — opportunity, $1,200-$2,400/month'})]));")
    rec, adj, rng = _node(js)
    assert "covers one Tuesday's overstaffing, per month" in _text(rec)
    assert "covers the whole schedule's gap to your target, per month" in _text(adj)
    t = _text(rng)
    # B6: the range's label no longer repeats the figure.
    assert t.count("1200") == 1 and "labor — opportunity" in t
    src = _src()
    assert "+hbDollarsBasis(ff.dollars_basis)" in src
    assert "hbDollarsBasis(a.dollars_basis)" in src


def test_one_thing_lead_keeps_the_attention_items_evidence_and_confidence():
    """B4 L3: when the one thing leads, the named attention item arrived as a
    bare title and button."""
    src = _src()
    i = src.index("if(att.length&&att[0].action){var a=att[0];var leadIsAtt=(lead===a.title);")
    seg = src[i:i + 1400]
    assert "aEv=leadIsAtt?'':String(a.evidence||'')" in seg
    assert "cavConfLine(a.confidence_detail||a.confidence" in seg


def test_all_clear_respects_stale_sources():
    """B4 M4 (client half): "All clear" on stale or undated data."""
    fn = _fn(_src(), "hbStaleSources") + _fn(_src(), "hbAttnClearHtml")
    js = fn + ("\nconsole.log(JSON.stringify([hbAttnClearHtml(null,{freshness:[{state:'current'}],monitoring:{count_live:1,sources:1}}),"
               "hbAttnClearHtml(null,{freshness:[{state:'stale'},{state:'current'}],monitoring:{count_live:1,sources:2}}),"
               "hbAttnClearHtml(null,{freshness:[],monitoring:{count_live:0,sources:2}})]));")
    ok, stale, none = _node(js)
    assert "All clear" in ok
    assert "All clear" not in stale and "out of date" in stale
    assert "All clear" not in none and "no source" in none


# ── U5: the rest ──────────────────────────────────────────────────────────

def test_rating_trend_reads_its_strength_not_a_band_word():
    """B4 L2 / B1 H8: "medium confidence" for a zigzag and a steady slide alike."""
    fn = _fn(_src(), "renderReviewClaims", indent="")
    assert "' confidence'" not in fn and "trend_strength_pct" in fn and "trend strength" in fn


def test_waste_trend_note_reads_a_percentage_not_a_band_word():
    fn = _fn(_src(), "wtObNote", indent="")
    got = _node(fn + "\nvar _wtStrength=null;\nconsole.log(JSON.stringify([wtObNote({confidence:'moderate confidence · 5 weeks'}),"
                     "wtObNote({confidence:'high confidence · 8 weeks',trend_strength_pct:71.6}),wtObNote({})]));")
    assert got == ["5 weeks", "trend strength 72% · 8 weeks", ""]


def test_ember_is_never_a_status_tile():
    """B4 L7: "Still on the table" used the ember as its status."""
    src = _src()
    assert 'class="hb-stat ember"' not in src and ".hb-stat.ember" not in src
    assert '<div class="hb-stat warn"><div class="k">Still on the table</div>' in src


def test_dsr_footer_says_why_each_line_was_left_out():
    """B6 sub-audit: settle_actions drops answered and repeated lines too; the
    footer called every one a failed figure."""
    src = _src()
    assert "dropped because a figure didn’t trace" not in src
    assert "left out because you already answered" in src


def test_recheck_date_is_the_local_day():
    """B6 sub-audit: rechecked_at (a local YYYY-MM-DD) was parsed as UTC
    midnight and shown a day early."""
    assert "/^\\d{4}-\\d{2}-\\d{2}$/.test(String(o.rechecked_at))?md(String(o.rechecked_at))" in _src()


def test_old_recipe_drafts_are_not_called_transcribed():
    src = _src()
    assert "dtrans?'<span class=\"ck-tag\" data-claim=\"transcribed\">From your card</span>'" in src
    assert "(dr.is_estimate===false?'<span class=\"ck-tag\" data-claim=\"transcribed\">" not in src


def test_a_review_read_error_is_not_tagged_or_cached_as_a_read():
    fn = _fn(_src(), "loadReviewInsight", indent="")
    i = fn.index("fetch('/api/review-insight')")
    after = fn[i:]
    assert after.index("d.error") < after.index("cavSet('review_insight'")   # scoped cache (Data Freshness #14)


def test_a_unit_warning_dish_gets_no_cut_or_reprice():
    src = _src()
    assert "uw&&(x.action==='cut'||x.action==='reprice')" in src and "Check units first" in src


def test_prime_cost_projection_is_not_drawn_when_held_back_and_t_is_not_read_early():
    """B6#4 (client half) and the fc2LoadCfo `t` read before assignment."""
    fn = _fn(_src(), "fc2LoadCfo", indent="")
    before = "\n".join(l for l in fn.split("var t=b.trust||{};")[0].split("\n") if not l.strip().startswith("//"))
    assert "t.prime_cost_accuracy" not in before.replace("bt.prime_cost_accuracy", "")
    assert "pcHeld" in fn and "if(!pcHeld&&p.projected_prime_cost!=null)" in fn


def test_ai_visibility_tooltips_are_mdy():
    src = _src()
    assert "String(runs[j].at||'').slice(0,10)" not in src
    assert "mdy(String(runs[j].at).slice(0,10))" in src


def test_dsr_food_tile_counts_units_not_dishes():
    src = _src()
    assert "of dishes costed" not in src and "of units sold costed" in src


def test_stale_marketing_read_is_flagged():
    src = _src()
    assert "function mktStaleCaveat(el,d)" in src
    assert src.count("mktStaleCaveat(el,") >= 3


def test_admin_fleet_data_cell_uses_the_shared_pos_state():
    """B6 sub-audit: the fleet table kept its own "older than 3 days" rule."""
    a = open(ADMIN, encoding="utf-8").read()
    m = re.search(r"\nconst FLEET_STATE_CHIP.*?\nfunction _fleetDataCell\(r\)\{.*?\n\}", a, re.S)
    assert m
    js = ("function esc(v){return String(v==null?'':v);}function ago(t){return 'ago('+t+')';}"
          "function parseTs(t){return t?Date.parse(t):null;}\n" + m.group(0)
          + "\nconsole.log(JSON.stringify([_fleetDataCell({freshness:{pos_state:{state:'aging',last_synced:'2026-09-20'}}}),"
            "_fleetDataCell({freshness:{pos_state:{state:'stale',last_synced:'2026-09-20'}}}),"
            "_fleetDataCell({freshness:{pos_state:{state:'not_connected'},reviews:null}})]));")
    aging, stale, none = _node(js)
    assert 'class="chip "' in aging and "chip warn" in stale and "never" in none


def test_client_cut_offs_and_ceilings_are_the_engines():
    """B6 low: the 75/50 cut-offs were copied into JS and Swift and their
    tests pinned the literals. Both clients read the payload's band (and
    thresholds / caps when sent); their fallbacks are held to the engine."""
    import confidence_engine as ce
    swift = open(os.path.join(ROOT, "ios", "CavnarAI", "CavnarAI", "Models", "TrustConfidence.swift"),
                 encoding="utf-8").read()
    m = re.search(r"static let engine = Thresholds\(high: (\d+), medium: (\d+)\)", swift)
    assert m and (int(m.group(1)), int(m.group(2))) == (ce.HIGH_AT, ce.MEDIUM_AT)
    for lit, val in (("caps?.noTrackRecord ?? ", ce.NO_TRACK_RECORD_CAP), ("caps?.stale ?? ", ce.STALE_CAP),
                     ("caps?.staleBelow ?? ", ce.STALE_BELOW)):
        assert f"{lit}{val}" in swift, lit
    block = _block()
    assert f"var AT={{high:{ce.HIGH_AT},medium:{ce.MEDIUM_AT}}};" in block
    assert f"cp.no_track_record:{ce.NO_TRACK_RECORD_CAP}" in block and f"cp.stale:{ce.STALE_CAP}" in block
    assert f"cp.stale_below:{ce.STALE_BELOW}" in block
    # Neither client derives the overall tone from its own copy any more.
    assert "pct>=75?'high'" not in block
