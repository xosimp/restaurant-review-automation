"""Admin console, visual fix round (admin audit 10/27/26, stream WS-4).

Structural pins for the visual findings in /Users/simp/admin-audit-1027/01-visual.md
and the lead's live pass (L3-L5): the 1201-1440px laptop band, strips that
wrap by width, the Overview showing each figure once, a 12px floor for
content text, modal error lines in red, the house loading pulse, one ember
action per screen, destructive actions that look destructive, no box-in-box
tables, colours from variables, charts drawn at their own width and the
house clock. Run in node where behaviour matters.
"""
import json
import os
import re
import shutil
import subprocess

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
with open(os.path.join(ROOT, "templates", "admin.html"), encoding="utf-8") as _f:
    PAGE = _f.read()
CSS = PAGE[PAGE.index("<style>") + len("<style>"):PAGE.index("</style>")]
BODY = PAGE[PAGE.index("</style>"):]


def _fn(name):
    """One top-level function's source, to the next top-level function or banner."""
    m = re.search(r"\n(?:async\s+)?function\s+" + re.escape(name) + r"\s*\(", PAGE)
    assert m, f"{name}() is gone from admin.html"
    nxt = re.search(r"\n(?:async\s+)?function\s+\w+\s*\(|\n// ──|\nconst |\nlet |\nvar ", PAGE[m.end():])
    return PAGE[m.start():m.end() + (nxt.start() if nxt else len(PAGE))] + "\n"


def _const(name, kw="const"):
    m = re.search(r"\n(" + kw + " " + re.escape(name) + r" = [^\n]*)", PAGE)
    assert m, f"{kw} {name} is gone from admin.html"
    return m.group(1)


def _rules():
    """(selector, declarations) for every top-level rule in the stylesheet, media blocks flattened."""
    css = re.sub(r"/\*.*?\*/", "", CSS, flags=re.S)
    out = []
    for sel, body in re.findall(r"([^{}@]+)\{([^{}]*)\}", css):
        out.append((" ".join(sel.split()), body))
    return out


def _px(decls, prop="font-size"):
    m = re.search(r"(?:^|;)\s*" + prop + r"\s*:\s*(?:var\(--fig-(\w+)\)|([\d.]+)px)", decls)
    if not m:
        return None
    if m.group(1):
        return {"xl": 40, "l": 28, "m": 22, "s": 16}[m.group(1)]
    return float(m.group(2))


def _node(js):
    if not shutil.which("node"):
        pytest.skip("node is not installed")
    out = subprocess.run(["node", "-"], input=js, capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout.strip().splitlines()[-1])


STUBS = ("const esc = (v) => String(v == null ? '' : v).replace(/[&<>\"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','\"':'&quot;',\"'\":'&#39;'}[c]));"
         "const jsq = esc; const fmtN = n => n == null ? '—' : String(n); function ico(n){ return '<i:' + n + '>'; }"
         "function pill(t, x){ return '<pill ' + t + '>' + x + '</pill>'; } const TONE = {critical:'bad', warning:'warn'};"
         "function issueCat(){ return 'Data'; } function issueActBtn(){ return ''; } function fmtD(s){ return String(s); }"
         "function fmtDT(s){ return String(s); }\n")


# ── V05 / L4: the laptop band ───────────────────────────────────────────────

def test_the_laptop_band_has_its_own_media_query():
    m = re.search(r"@media \(max-width:1440px\)\{(.*?)\}\n", CSS)
    assert m, "no 1201-1440px rule: the hero, the subsystem cards and the Jobs split truncate at laptop widths"
    band = m.group(1)
    for rule in (".ovhero{grid-template-columns:repeat(2,minmax(0,1fr))}", ".g6{grid-template-columns:repeat(3,minmax(0,1fr))}",
                 ".split.wide{grid-template-columns:1fr}", ".split.wide > .stack{display:grid;"):
        assert rule in band, rule
    # The hero is a class a media query reaches, never an inline 12-column grid.
    ov = _fn("overview")
    assert '<section class="ovhero">' in ov
    assert "repeat(12" not in ov and "grid-column:span" not in ov
    assert ".ovhero{display:grid;" in CSS and ".ovhero{grid-template-columns:1fr}" in CSS   # one column on a phone


def test_a_subsystem_cards_unit_wraps_instead_of_overflowing():
    sc = _fn("subCard")
    assert "flex-wrap:wrap" in sc
    unit = re.search(r'<span class="n muted" style="([^"]*)">\$\{unit\}', sc)
    assert unit and "nowrap" not in unit.group(1)


# ── V01: strips wrap by width; RPOWER's strip reads ─────────────────────────

def test_strips_wrap_by_width_with_clean_hairlines():
    assert ".strip{display:flex;flex-wrap:wrap;overflow:hidden}" in CSS
    cell = re.search(r"\.strip > a,\.strip > div\{([^}]*)\}", CSS).group(1)
    assert "flex:1 1 140px" in cell and "margin:0 0 -1px -1px" in cell and "border-bottom:1px solid var(--line)" in cell
    assert ".strip > :first-child" not in CSS and ".strip > :last-child" not in CSS
    assert ".strip{flex-wrap:wrap}" not in CSS                       # no longer only below 1200px


def test_a_strip_cell_with_nowhere_to_go_is_not_a_link():
    out = _node(STUBS + _fn("stripCell") + "console.log(JSON.stringify([stripCell('', 'alert', 'bad', 'Failing', '3', 'stores'), stripCell('#operations/ai', 'spark', 'neu', 'AI', '1', 'x')]));")
    assert out[0].startswith("<div>") and "<a " not in out[0]
    assert out[1].startswith('<a href="#operations/ai">')


def test_the_rpower_strip_is_six_paging_figures_and_more_behind_a_disclosure():
    rp = _fn("rpStrip")
    top = rp[rp.index("var top ="):rp.index("var more =")]
    assert top.count("stripCell(") == 6
    for label in ("'Failing'", "'Token refused'", "'Stale 48h+'", "'Archive gaps'", "'Queue failed / expired'", "'Breaker'"):
        assert label in top, label
    assert "More fleet figures" in rp and '<details class="card">' in rp
    assert "stripCell('#operations/rpower'" not in PAGE                # never a link back to the page already open
    head = re.search(r"\nvar RP_HEAD = (.*?)';\n", PAGE, re.S).group(1)
    for label in ("Health", "Store", "Pull · night", "Coverage", "Gaps", "Req · err"):
        assert ">" + label + "<" in head, label
    assert "RP_HEAD" in _fn("opsRpower")


# ── V03 / L3: the Overview shows each figure once ───────────────────────────

def test_the_overview_watch_tiles_that_repeated_figures_are_gone():
    ov = _fn("overview")
    assert "watchKpi(" not in ov and "grid g8" not in ov
    for gone in ("'Paused'", "'Closing'", "'AI stalled'", "'Fetch behind'", "'Req. errors'", "'AI quality'",
                 "'Bounce 7d'", "'Spam 7d'", "'Critical'", "'Failed jobs'", "'Email delivery'", "'AI activity'"):
        assert gone not in ov, gone
    strip = ov[ov.index('<section class="card strip">'):]
    strip = strip[:strip.index("</section>")]
    assert strip.count("stripCell(") == 3
    # The AI cell counts every vendor, Places included: "AI & data APIs" (round 2, C10).
    for kept in ("'Support queue'", "'AI &amp; data APIs'", "'API latency'"):
        assert kept in strip, kept
    # One critical count: the Needs you badge beside its list, not the sentence too.
    assert "' critical'" not in ov.split("const sentence")[1].split(";")[0]
    assert 'class="cnt bad" title="Critical, customers and platform"' in _fn("needsList")


# ── V15: content text is 12px or more ───────────────────────────────────────

CONTENT = (".fl label", ".list .when", ".li .tt span", ".strip .s", ".kc .s", ".sub", ".card .ch .sub", "td", ".dim",
           ".mono", ".pager", ".hbeat .t", ".empty", ".warnline", ".pulse-line", ".li .age", ".pal-res .d", "table")
TAGS = (".kick", ".lbl", "th", ".pill", ".chip", ".sev", ".pal-res .t", ".brand .tag", ".kbd", ".cnt", ".seg b", ".kpi .l")


def test_no_content_selector_sets_text_under_12px():
    seen = set()
    for sel, decls in _rules():
        size = _px(decls)
        if size is None:
            continue
        for part in (s.strip() for s in sel.split(",")):
            if part in CONTENT:
                seen.add(part)
                assert size >= 12, f"{part} is {size}px — content text is 12px or more"
            if part in TAGS:
                assert size >= 10.5, f"{part} is {size}px — a kicker or tag is 10.5px or more"
    assert {".fl label", ".list .when", ".hbeat .t"} <= seen


def test_inline_text_under_12px_is_only_chart_axes_and_tags():
    for m in re.finditer(r"font-size:\s*([\d.]+)px", BODY):
        size = float(m.group(1))
        if size >= 12:
            continue
        tag = BODY[:m.start()][BODY[:m.start()].rfind("<"):]
        ctx = BODY[max(0, m.start() - 300):m.start() + 200]
        if tag.startswith("<text"):
            ok = size >= 11 or "HEALTHY" in ctx                         # an SVG axis label, never under 11px (V12); the ring's kicker
        elif 'class="pill' in tag:
            ok = size >= 10.5                                           # a tag
        else:
            ok = size >= 11 and "months.map" in ctx                     # the MRR spark's month axis
        assert ok, f"inline font-size:{size}px — content text is 12px or more: …{ctx[200:420]}"
    for gone in ("font-size:11.5px", "font-size:9.5px", "font-size:10px"):
        assert gone not in BODY, gone


# ── V07: modal error lines are red ──────────────────────────────────────────

def test_modal_status_lines_say_their_outcome_in_colour():
    ss = _fn("statusSay")
    assert "var(--red-t)" in ss and "var(--green-t)" in ss and "var(--amber-t)" in ss
    for sid in ("m-toast-status", "m-rpower-status", "b-status", "i-status", "al-status", "setup-cl-out", "setup-dsr-out"):
        assert not re.search(r"\$\('" + re.escape(sid) + r"'\)\.textContent\s*=", PAGE), f"{sid} is written without its colour"
    assert "'✕ " not in BODY and "('✕" not in BODY and "'✓ " not in BODY
    out = _node("const els = {}; const $ = (id) => els[id] || (els[id] = {style:{}, textContent:''});\n" + ss
                + "statusSay('a', false, 'Token refused'); statusSay('b', true, 'Connected'); statusSay('c', null, 'Not connected'); statusSay('d', 'warn', 'Connected · error');"
                + "console.log(JSON.stringify(['a','b','c','d'].map(k => [els[k].textContent, els[k].style.color])));")
    assert out == [["Token refused", "var(--red-t)"], ["Connected", "var(--green-t)"], ["Not connected", ""], ["Connected · error", "var(--amber-t)"]]


# ── V13: the house loading pulse, a busy button ─────────────────────────────

def test_loading_is_the_house_pulse_never_dots():
    assert ".dr-pulse{display:block;position:relative;height:3px;" in CSS
    assert "animation:pulseBarSlide 1.4s linear infinite" in CSS and "@keyframes pulseBarSlide{0%{left:-30%}100%{left:130%}}" in CSS
    assert '.btn[aria-busy="true"]:after{' in CSS
    out = _node(STUBS.split("function ico")[0] + _fn("pulse") + "console.log(JSON.stringify([pulse('Saving'), pulse('Seeding', 'up to two minutes')]));")
    assert '<span class="dr-pulse"><i></i></span>' in out[0] and 'role="status"' in out[0] and 'aria-label="Saving"' in out[0]
    assert out[1].endswith("up to two minutes</span>")
    for gone in ("'Working…'", "'Loading…'", "'Verifying…'", "'Uploading…'", "'Adding…'", "'Creating…'", "'Saving…'",
                 "'Starting…'", "'Loading what is stored…'", "Reading the menu… this can take a minute.",
                 "'Building…'", "palNote('Searching the fleet…')", "row${d.rows===1?'':'s'}…", "'Reading the platform state…'"):
        assert gone not in BODY, gone
    assert "busyBtn(btn, true)" in _fn("formSubmit") and "busyBtn(btn, false)" in _fn("formSubmit")


# ── V10: one ember action per screen ────────────────────────────────────────

def test_rows_carry_secondary_actions_under_the_one_primary():
    ev = _fn("opsEvents")
    assert not re.search(r'class="btn p s[^"]*" onclick="evSave', ev) and re.search(r'class="btn s[^"]*ev-save[^"]*" onclick="evSave', ev)
    assert "classList.add('p')" in _fn("evDirty")                       # lit only once its row is edited
    assert "' p'" not in _fn("runBtn")
    assert "btn s p" not in _fn("custDetail") and "btn s p" not in _fn("clientForms") and "btn s p" not in _fn("bulkCard")
    assert "btn s p" not in _fn("aiRecRow")
    assert "btn s p w\" onclick=\"incidentUpdate" not in _fn("engStatus") and 'class="btn p w" onclick="incidentOpen()"' in _fn("engStatus")


def test_a_failing_jobs_run_now_is_always_shown_but_never_ember():
    out = _node(STUBS + _fn("runBtn") + "const j = {job:'backup_db', runnable:true, sends:false, label:'Backup'};"
                "console.log(JSON.stringify([runBtn(j, {}, true), runBtn(j, {}, false)]));")
    assert 'class="btn s w"' in out[0] and "reveal" not in out[0]
    assert 'class="btn s w reveal"' in out[1]


# ── V06: destructive and fleet-wide actions look destructive ────────────────

def test_destructive_actions_are_danger_buttons():
    assert '<button class="btn s d w" onclick="aiEmergencyStop(true)">Stop all AI</button>' in _fn("opsAI")
    assert '<button class="btn s d w" onclick="intakePurge(' in _fn("intakeHtml")
    assert re.search(r'<button class="btn s d w" onclick="reset2fa\(', _fn("secLoad"))
    assert ".btn.dz{color:var(--red-t)}" in CSS


# ── V08: no box in a box ────────────────────────────────────────────────────

def test_a_table_inside_a_card_is_not_boxed_again():
    assert ".card .tw,.lx .tw,.box .tw{border:0;border-radius:0;box-shadow:none;background:transparent}" in CSS
    assert 'class="tw" style="border:0' not in PAGE


# ── V31: colours come from variables ────────────────────────────────────────

def test_the_rules_name_their_colours():
    for sel, decls in _rules():
        if sel == ":root":
            continue
        assert not re.search(r"#[0-9a-fA-F]{3,6}\b", decls), f"{sel} carries a hex colour: {decls}"
        for d in decls.split(";"):
            if "rgba(255,255,255" in d:
                assert d.strip().startswith("box-shadow"), f"{sel}: a white fill is a variable (--fill-1/--fill/--fill-2): {d}"


# ── V12 / V24: charts drawn at their own width, a round end dot ─────────────

def test_charts_draw_at_the_width_they_are_shown():
    assert "el.clientWidth" in _fn("fitChart")
    assert "fitChart('ov-growth'" in _fn("overview") and 'id="ov-growth"' in _fn("overview")
    assert "const w = w0 || 470" in _fn("growthChart") and "const w = o.w || 680" in _fn("lineChart")
    assert "fitChart('ana-mrr'" in _fn("anaHistory") and "fitChart('eng-lat'" in _fn("engLatency")
    for name in ("growthChart", "lineChart"):
        sizes = [float(x) for x in re.findall(r"font-size:([\d.]+)px", _fn(name))]
        assert sizes and min(sizes) >= 11, name


def test_a_sparks_end_dot_is_html_not_a_stretched_circle():
    out = _node(_fn("spark") + "console.log(JSON.stringify(spark([1, 3, 2, 5], 200, 32, 'var(--green-t)', 'x')));")
    assert "<circle" not in out and 'class="spk-end"' in out and "left:97.00%" in out
    assert ".spk-end{position:absolute;" in CSS


# ── V09: the client page says each figure once ──────────────────────────────

def test_the_client_page_has_one_home_per_figure():
    cv = _fn("clientView")
    assert "' module'+(b.modules" not in cv and "churn risk', false)" not in cv
    assert "stripDiv('Setup'" not in cv and "stripDiv('Churn risk'" in cv
    assert "<h2>Modules</h2>" not in _fn("cOverview") and "<h2>Modules</h2>" in _fn("cData")
    assert "<h2>Billing</h2>${bill(" not in _fn("cBillingMain")
    assert "{here: true}" in _fn("cOverview")
    out = _node(STUBS + _fn("needRow") + "const i = {key:'k', title:'Fetch behind', severity:'warning', restaurant_id:5, restaurant:'EJ', since:'2h'};"
                "console.log(JSON.stringify([needRow(i, {here: true}), needRow(i, 0)]));")
    assert "#client/5" not in out[0] and "/admin/view-as/5" not in out[0]
    assert "Open client" in out[1] and "/admin/view-as/5" in out[1]      # .map(needRow) passes an index: unchanged


# ── V11, V18, V25, V27, V28, V34, L5 ────────────────────────────────────────

def test_the_support_queue_bar_is_one_segment_and_two_selects():
    iss = _fn("issues")
    assert "['issues','Issues','']" in iss
    assert 'aria-label="Severity"' in iss and "[['','All'],['critical','Critical'],['warning','Warning']]" not in iss


def test_the_area_kicker_shows_only_when_it_differs_from_the_title():
    assert "kicker && kicker !== title" in _fn("head")
    for name in ("intelligence", "experiments", "recommendations"):
        assert '<div class="head">' not in _fn(name), name


def test_contract_pills_in_a_contract_column_drop_the_word():
    out = _node(STUBS + _const("CONTRACT") + "\n" + _fn("contractPill")
                + "console.log(JSON.stringify([contractPill('sent', true), contractPill('delivered', true), contractPill('sent')]));")
    assert ">sent<" in out[0] and "contract" in out[0].split(">")[0]     # the full words on hover
    assert ">opened<" in out[1]
    assert ">contract sent, not signed<" in out[2]
    assert "contractPill(r.contract_status, true)" in _fn("billRow")


def test_empty_cards_say_what_would_fill_them():
    assert "No email opened in 30 days." in _fn("opsEmail")
    assert "No alert fired in 7 days." in _fn("opsPush") and "No push delivered in 30 days." in _fn("opsPush")


def test_times_read_the_house_way():
    out = _node(_fn("fmtT") + "console.log(JSON.stringify([fmtT(new Date(2026, 9, 27, 18, 45)), fmtT(new Date(2026, 9, 27, 18, 0)), fmtT(new Date(2026, 9, 27, 0, 5)), fmtT(new Date(2026, 9, 27, 12, 30))]));")
    assert out == ["6:45pm", "6pm", "12:05am", "12:30pm"]
    assert "toLocaleTimeString" not in PAGE


def test_billing_health_is_one_checked_line_when_clean():
    bh = _fn("billingHealth")
    assert "clean.push('Stripe reconciled')" in bh and "clean.push('nobody signed and unpaid')" in bh
    assert '<span class="cv-ok"' in bh                     # the house check disc (round 2, V23)


def test_a_job_row_is_one_line_and_says_what_it_does_when_opened():
    row = _fn("jobRow")
    head, open_ = row.split('<div class="lx">', 1)
    assert "j.what" not in head and "esc(j.what)" in open_


def test_the_design_system_names_the_console_rules_this_round_added():
    with open(os.path.join(ROOT, "DESIGN_SYSTEM.md"), encoding="utf-8") as f:
        ds = f.read()
    sec = ds[ds.index("## 12c."):ds.index("## 12d.")]
    for key in ("1201–1440px", ".ovhero", "stripCell('', …)", "pulse(label", "busyBtn(btn, on)", "statusSay(id, ok, msg)",
                "fitChart(id", ".spk-end", "6:45pm", "contractPill(st, true)", "needRow(i, {here: true})", "--fill-1",
                "`.btn.d`", "`.card .tw`", "evDirty", "kicker shows only when it differs"):
        assert key in sec, key
    assert "`--bg` canvas < `--rail`" not in sec                          # the rail is darker than the canvas
