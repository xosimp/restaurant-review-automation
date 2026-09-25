"""Never-say audit, workstream C: owner-facing copy on web and iOS.

The audit (NS1_ui_strings, NS1_inventory, NS3_*) found hand-written client
copy that called an opportunity "savings", drew green "on target / all
clear" over sample, partial or stale data, drew a synthetic curve with no
label, claimed to know what ChatGPT and Google AI read, called one model
call an "optimized" schedule, and framed a model's cause and forecast as
fact. These tests pin the fixes two ways:

- banned owner-facing strings are gone from templates/*.html and every
  Swift source (comments excluded — a comment may name what was removed);
- the rules that replaced them (positive-status contract, money labels,
  tolerant reads of new server keys) are in the code, and the pure JS
  helpers behave, run under node.

DESIGN_SYSTEM.md → "10b. Money labels and positive status" is the rule.
"""
import glob
import json
import os
import re
import shutil
import subprocess

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
IOS = os.path.join(ROOT, "ios", "CavnarAI", "CavnarAI")


def _read(*parts):
    return open(os.path.join(ROOT, *parts), encoding="utf-8").read()


def _dash():
    return _read("templates", "dashboard.html")


def _strip_web_comments(src):
    src = re.sub(r"<!--.*?-->", "", src, flags=re.S)
    src = re.sub(r"\{#.*?#\}", "", src, flags=re.S)
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
    # JS line comments: only whole-line ones, so a URL's // survives.
    return "\n".join(l for l in src.split("\n") if not l.lstrip().startswith("//"))


def _strip_swift_comments(src):
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
    out = []
    for line in src.split("\n"):
        s = line.lstrip()
        if s.startswith("//"):
            continue
        # A trailing comment after code.
        i = line.find(" // ")
        out.append(line[:i] if i >= 0 else line)
    return "\n".join(out)


def _templates():
    for path in sorted(glob.glob(os.path.join(ROOT, "templates", "*.html"))):
        yield os.path.relpath(path, ROOT), _strip_web_comments(open(path, encoding="utf-8").read())


def _swift():
    for path in sorted(glob.glob(os.path.join(IOS, "**", "*.swift"), recursive=True)):
        yield os.path.relpath(path, ROOT), _strip_swift_comments(open(path, encoding="utf-8").read())


# (pattern, why) — case-insensitive, over owner-facing copy.
BANNED = [
    (r"value delivered", "the measured figure is 'Measured results' (NS1 #12, H9)"),
    (r"claw back", "a certainty on an opportunity (NS1 #6)"),
    (r"the one number", "NS1 #6"),
    (r"typical restaurant", "the example curve is from no restaurant (NS1 #13)"),
    (r"grow your score", "a promise nothing measures (NS1 #10)"),
    (r"optimi[sz]ed schedule|AI-optimi[sz]ed|optimized to your|schedule optimized|optimized-scheduling",
     "one model call plus repair passes is a draft (NS1 #11)"),
    (r"every staff constraint", "the review panel lists what it still misses (NS1 #11)"),
    (r"monthly savings|annual savings|saving vs\.? industry|annual advantage",
     "the labor gap is an opportunity, never savings (NS1 #2, #15)"),
    (r"largest saving|money on the table", "labels over model lines name the kind (NS1 #17)"),
    (r"next month is on us", "no billing reference exists (NS1 #19)"),
    (r"will do better", "a guarantee (NS1 #24)"),
    (r"(low|medium|high) confidence —", "confidence is a % or nothing (NS1 #20)"),
    (r"asking AI where to eat", "unsourced (NS1 #9)"),
    (r"(ChatGPT|Google AI)[^.<\"]{0,40}\bread\b|fields AI reads|into AI answers", "unsourced (NS1 #9)"),
    (r"knows who you are", "NS1 #27"),
    (r"recoverable this month|in waste this month", "a one-week projection, not this month (NS3 food #7)"),
    (r"great work", "praise over unchecked data (NS1 #30)"),
    (r"if optimized", "the gap is 'Gap to target / mo' (NS1 #22)"),
    (r"why this is happening", "'What the evidence points to' (NS1 H10)"),
    (r"what should change", "'If this is the cause, you'd expect…' (NS1 H10)"),
    (r"what worked for you", "'Measured alongside your changes' (NS1 #18)"),
    (r"if ignored:", "'Risk if left alone' — a prediction, not a fact (NS1 #28)"),
    (r"/mo saved|of overtime avoided", "a move not yet kept is conditional (NS3 labor #12, #13)"),
    (r"the first bar is the thing to fix", "NS1 #26"),
    (r"THE PROMISE", "the audit estimated; it promised nothing (NS3 M12)"),
]


@pytest.mark.parametrize("pattern,why", BANNED, ids=[b[0][:40] for b in BANNED])
def test_banned_owner_copy_is_gone_from_templates_and_swift(pattern, why):
    rx = re.compile(pattern, re.I)
    hits = []
    for name, src in list(_templates()) + list(_swift()):
        for i, line in enumerate(src.split("\n"), 1):
            if rx.search(line):
                hits.append(f"{name}:{i}: {line.strip()[:120]}")
    assert not hits, why + "\n" + "\n".join(hits[:10])


def test_savings_never_labels_a_figure_in_owner_text():
    """In the text an owner reads — Swift string literals, and the text
    between tags in templates — 'savings' / 'you saved' appear only as the
    kind a figure is NOT ('not savings', 'not money saved')."""
    rx = re.compile(r"\bsavings\b|you saved (about )?\$|saved you|saved \$", re.I)
    ok = re.compile(r"not (money )?sav(ed|ings)|added to the savings|never \"savings\"|^\w+$", re.I)
    hits = []
    for name, src in _swift():
        for i, line in enumerate(src.split("\n"), 1):
            for lit in re.findall(r'"([^"\n]*)"', line):
                if rx.search(lit) and not ok.search(lit):
                    hits.append(f"{name}:{i}: {lit[:120]}")
    for name, src in _templates():
        if name.endswith(("audit_report.html", "audit_tool.html", "admin.html")):
            continue  # the sales audit and Will's console are other workstreams
        for i, line in enumerate(src.split("\n"), 1):
            for txt in re.findall(r">([^<>{}]+)<", line):
                if rx.search(txt) and not ok.search(txt):
                    hits.append(f"{name}:{i}: {txt[:120]}")
    assert not hits, "\n".join(hits[:10])


# ── web: the rules that replaced the strings ─────────────────────────────────

def test_web_labor_money_is_a_gap_never_green_and_never_on_sample_data():
    s = _dash()
    assert 'class="x good stat-n"' not in s
    assert "{% if labor.is_live and savings_breakdown.labor_monthly > 0 %}" in s
    assert "Gap to target / mo" in s and "Per year · projected" in s
    # Benchmarking #34: no "$ under industry" tile at all (this pinned its
    # $0 / no-benchmark guard).
    assert "labor.is_live and _ind and savings_breakdown.labor_vs_industry_monthly > 0" not in s
    assert "Overtime premium · " in s  # names its window
    # ...and not over stale shifts (Data Freshness #33): the registry's labor
    # source withholds the good tone too.
    assert "{% set _status_ok = labor.is_live and not _partial and not labor.period_too_short_to_project and labor.total_sales and not _lab_stale %}" in s


def test_web_gap_chip_only_says_on_target_under_the_contract():
    s = _dash()
    i = s.index("fetch('/api/labor-gap')")
    body = s[i:s.index("});\n}", i)]
    assert "var gapOk=d.is_live!==false&&d.projectable!==false&&!d.sales_data_missing" in body
    on = body.index("'On target ✓'")
    assert body.rfind("else if(gapOk&&d.over_target===false)", 0, on) > 0
    assert "d.reason" in body  # the dash carries the server's reason
    assert "#6fcf97" not in body


def test_web_home_uses_monitoring_all_clear_and_payload_units():
    s = _dash()
    assert "else if(!hbAllClear(d)){lead='Nothing flagged yet.';kick='Watching'" in s
    assert "if(!stale&&!none&&!hbAllClear(d))" in s
    assert "tile('labor','Labor · by day',hbLaborFlag(l)," in s
    assert "<small>recoverable / mo</small>" not in s
    assert "esc(hbFoodUnit(inv))" in s
    assert "var SEC_LABEL={Recoverable:'Gap to target'};" in s
    assert '<div class="hb-kicker">Measured results</div>' in s
    assert "(v.caveat?'<div class=\"delta\"><span>'+esc(v.caveat)" in s


def test_web_dsr_reads_the_renamed_keys_first_and_labels_them_as_opportunities():
    s = _dash()
    assert "['largest_opportunity|largest_money_opportunity|largest_dollar_gap|largest_money_saving','Largest dollar gap · an opportunity, not savings']" in s
    assert "['biggest_financial_opportunity','Biggest opportunity · not captured']" in s
    assert "var rk=isNum(m.drivers_at_stake_monthly)?'drivers_at_stake_monthly':(isNum(m.at_stake_monthly)?'at_stake_monthly':'recoverable_monthly');" in s
    assert "(rd.complete===false?' · partial':'')" in s


def test_web_ask_shows_unsupported_causes_and_names_inline():
    s = _dash()
    i = s.index("function _appendAskCavnarEvidence(d)")
    body = s[i:s.index("\n}\n", i)]
    assert "d.unsupported_causes || []" in body and "d.unsupported_names || []" in body
    assert "'Unsupported cause — " in body and "'Unverified name — " in body
    assert "!badCauses.length && !badNames.length" in body


def test_web_schedule_progress_names_last_year_only_when_it_exists():
    s = _dash()
    assert "window._SCHED_HAS_LAST_YEAR ? \"Reading last year's same days\" : 'Reading your shift and sales history'" in s
    assert "window._SCHED_HAS_LAST_YEAR={{ 'true' if labor.last_year_available else 'false' }};" in s


def test_web_diagnosis_forecast_is_conditional():
    s = _dash()
    assert s.count("If this is the cause, you’d expect…") == 2
    assert s.count("guarantee\\w*|definitely|certainly|will eliminate") == 2
    assert "What the evidence points to" in s


def _fn(src, name):
    i = src.index("function " + name + "(")
    j = src.index("{", i)
    depth = 0
    for k in range(j, len(src)):
        if src[k] == "{":
            depth += 1
        elif src[k] == "}":
            depth -= 1
            if depth == 0:
                return src[i:k + 1]
    raise AssertionError(name)


def _node(js):
    if not shutil.which("node"):
        pytest.skip("node is not installed")
    out = subprocess.run(["node", "-e", js], capture_output=True, text=True, timeout=20)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout.strip())


def test_web_positive_status_helpers_under_node():
    s = _dash()
    js = "\n".join(_fn(s, n) for n in ("hbStaleSources", "hbAllClear", "hbLaborFlag", "hbFoodUnit"))
    js += """
    var out = {
      clear: hbAllClear({monitoring:{count_live:3,all_clear:true},freshness:[{state:'current'}]}),
      stale: hbAllClear({monitoring:{count_live:3,all_clear:false},freshness:[{state:'stale'}]}),
      none: hbAllClear({monitoring:{count_live:0,all_clear:false},freshness:[]}),
      notCurrent: hbAllClear({monitoring:{count_live:2,all_clear:false},attention:[],freshness:[]}),
      flagged: hbAllClear({monitoring:{count_live:2,all_clear:false},attention:[{t:1}],freshness:[]}),
      old: hbAllClear({freshness:[{state:'current'}]}),
      good: hbLaborFlag({state:'good'}),
      short: hbLaborFlag({state:'neutral',below_floor:true}),
      stl: hbLaborFlag({state:'neutral',stale:true}),
      partial: hbLaborFlag({state:'good',coverage_note:'4 of 7 days carry sales'}),
      over: hbLaborFlag({state:'bad'}),
      pct: hbFoodUnit({value:'31.2%',unit:'food cost'}),
      rec: hbFoodUnit({value:'$640',unit:'recoverable / mo'}),
      bare: hbFoodUnit({value:'$640'})
    };
    console.log(JSON.stringify(out));
    """
    r = _node(js)
    assert r["clear"] is True and r["old"] is True
    assert r["stale"] is False and r["none"] is False and r["notCurrent"] is False
    assert r["flagged"] is True  # the flagged item is its own card
    assert r["good"] == "on target"
    assert (r["short"], r["stl"], r["partial"], r["over"]) == ("too few days", "out of date", "partial data", "over target")
    assert r["pct"] == "food cost"
    assert r["rec"] == r["bare"] == "opportunity / mo · projected"


def test_web_money_kind_word_under_node():
    s = _dash()
    i = s.index("var CAV_MONEY_KIND = {")
    js = s[i:s.index("\n}\n", s.index("function cavMoneyKindWord(")) + 2]
    js += """
    console.log(JSON.stringify([cavMoneyKindWord('opportunity','at stake'), cavMoneyKindWord('', 'at stake'),
      cavMoneyKindWord('projection'), cavMoneyKindWord('measured'), cavMoneyKindWord('weird','x')]));
    """
    assert _node(js) == ["at stake · not captured", "at stake", "projected", "measured", "x"]


# ── iOS: the rules that replaced the strings ─────────────────────────────────

def _ios(*parts):
    return open(os.path.join(IOS, *parts), encoding="utf-8").read()


def test_ios_labor_tiles_come_from_owner_copy():
    s = _ios("Features", "Labor", "LaborAnalyticsSection.swift")
    assert "OwnerCopy.laborMoneyTiles(isLive: stats.isLive" in s
    assert "if stats.isLive && b.laborOvertime > 0" in s
    assert "OwnerCopy.laborBucket(" in s and "positiveAllowed: Self.positiveAllowed(stats)" in s
    assert '"Excellent"' not in _strip_swift_comments(s)
    assert "var tone: Color = Color.cavnarInk" in s  # a tile is never green by default


def test_ios_value_band_draws_no_synthetic_curve():
    band = _ios("Features", "Home", "HomeValueBand.swift")
    assert "sampleTrend" not in band
    assert "if hasRealTrend {" in band
    assert '"Nothing measured yet"' in band
    card = _ios("Features", "Home", "ValueChartCard.swift")
    assert "Illustration only" in card
    assert '.accessibilityLabel("Measured results")' in card


def test_ios_running_on_ai_only_over_a_live_source():
    home = _strip_swift_comments(_ios("Features", "Home", "HomeView.swift"))
    lines = [l for l in home.split("\n") if "running on AI" in l]
    assert len(lines) == 1 and "(liveSources ?? 0) > 0 ?" in lines[0]
    assert "AllClearRow(notClearReason: OwnerCopy.allClear(" in home


def test_ios_reads_the_new_server_fields_tolerantly():
    home = _ios("Models", "HomeSummary.swift")
    assert 'case allClear = "all_clear"' in home and "case stale" in home
    assert 'case dollarsKind = "dollars_kind"' in home
    dsr = _ios("Features", "DailyReport", "DailyReportModels.swift")
    for key in ("largest_money_opportunity", "largest_dollar_gap", "largest_money_saving"):
        assert f'"{key}"' in dsr
    view = _ios("Features", "DailyReport", "DailyReportView.swift")
    assert '["drivers_at_stake_monthly", "at_stake_monthly", "recoverable_monthly"]' in view
    ask = _ios("Features", "AskCavnar", "AskCavnarViewModel.swift")
    assert 'case unsupportedCauses = "unsupported_causes"' in ask and 'case unsupportedNames = "unsupported_names"' in ask
    api = open(os.path.join(ROOT, "ios", "CavnarAI", "CavnarAI", "Core", "APIClient.swift"), encoding="utf-8").read()
    assert 'case unsupportedCauses = "unsupported_causes"' in api
    labor = _ios("Features", "Labor", "LaborViewModel.swift")
    assert 'case lastYearAvailable = "last_year_available"' in labor


def test_the_design_system_documents_the_money_and_status_rules():
    ds = _read("DESIGN_SYSTEM.md")
    assert "### 10b. Money labels and positive status" in ds
    for phrase in ("never green, never \"savings\"", "Illustration only", "monitoring.all_clear",
                   "Risk if left alone", "OwnerCopy"):
        assert phrase in ds, phrase
