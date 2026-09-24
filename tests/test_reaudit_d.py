"""Re-audit group D (9/24/26): the web dashboard's half of the cross-group
contracts (K1 tracker_id on every check-in, K3 a reason on the win-back and
schedule-review "Not for us", K4 net of what got worse, K6 answers on the
brief's lines, K7 `counts` for colour), the web fixes D2-D22, and the DSR
server fixes D15-D18 (gross days and bases in the grid, the settings route's
400s, the period-scheme picker, listed 53-week years).

The web half is asserted against the source (a rendered fixture only covers
the branches it hits); every rule was also rendered in a static harness with
the real CSS/JS and looked at in dark mode. The server half runs the real
code on a throwaway database.
"""
import json
import os
import re
import sys
from datetime import date
from types import SimpleNamespace as NS

import pytest

import auth
import models
import pos
import dsr
from dsr import fiscal, rollup, store
from models import Restaurant, create_restaurant, get_restaurant, update_restaurant

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = open(os.path.join(ROOT, "templates", "dashboard.html"), encoding="utf-8").read()


def _fn(name, src=SRC):
    """The body of `function name(` up to the next function at either indent."""
    i = src.index("function " + name + "(")
    ends = [x for x in (src.find("\nfunction ", i + 1), src.find("\n  function ", i + 1)) if x > 0]
    return src[i:min(ends) if ends else len(src)]


def _scripts():
    return "\n".join(re.findall(r"<script(?![^>]*src=)[^>]*>(.*?)</script>", SRC, re.S))


# ── D2 / K1: the check-in names its result ──────────────────────────────────

def test_every_check_in_sends_the_tracker_it_is_about():
    post = _fn("_recCheckinPost")
    assert "card.getAttribute('data-ck-tracker')" in post and "body.tracker_id=tid" in post
    assert "JSON.stringify(body)" in post and "conditions_changed:!!changed" in post
    # The card carries the outcome's id, on Home and on the record page alike.
    assert 'data-ck-tracker="\'+recEsc(o.id)+\'"' in _fn("recCheckinHtml")


def test_the_check_in_rereads_its_result_by_id():
    ref = _fn("_recCheckinRefresh")
    assert "'/api/outcomes?ids='+encodeURIComponent(tid)" in ref
    assert "fetch('/api/outcomes',{" not in ref


# ── D13 / K7: a result is coloured by whether it counts ─────────────────────

def test_a_result_that_does_not_count_is_neutral():
    tone = _fn("hbResTone")
    assert "r.counts===false)return 'watch'" in tone
    # Both result lists on Home use it; neither colours by verdict alone now.
    assert SRC.count("'+hbResTone(res[j])+'") == 1 and SRC.count("'+hbResTone(r)+'") == 1
    assert "(res[j].verdict==='improved'?'good'" not in SRC
    assert "(r.verdict==='improved'?'good':(r.verdict==='worsened'?'critical':'watch'))+'\"><span class=\"d\">" not in SRC


# ── D7, D8, D22 / K4: net, priced counts, unpriced wins ─────────────────────

def test_the_net_sentence_counts_only_priced_results():
    s = _fn("hbNetSentence")
    assert "typeof wz.priced_count==='number'?wz.priced_count:all" in s
    assert "' that got worse'" in s and "more got worse with no dollar figure" in s
    rv = _fn("renderValue")
    assert "hbNetSentence(d.monthly,d.wins||0,wz)" in rv
    assert "'+wz.count+' that got worse" not in SRC


def test_unpriced_wins_show_the_card_and_are_listed_without_a_figure():
    rv = _fn("renderValue")
    assert "var uw=d.unpriced_wins||[];" in rv
    assert "if(!d.wins&&!uw.length&&!d.in_flight" in rv            # an unpriced win alone shows the card
    assert "not priced in dollars" in rv and "num(u.line||'')" in rv
    assert "(d.unpriced_wins||[]).length" in _fn("renderWorked")


def test_the_home_hero_shows_net_once_anything_got_worse():
    hero = _fn("renderHero")
    assert "isNet=(+wz.count||0)>0&&typeof v.net_monthly==='number'" in hero
    assert "var big=isNet?v.net_monthly:(+v.total||0)" in hero
    assert "a month, measured'+(isNet?', net':'')" in hero
    # improvements = net + what got worse: right whatever `total` means.
    assert "hbNetSentence(v.net_monthly+(+wz.monthly||0)" in hero
    # Below zero is said as below zero, never floored and never "$-420".
    assert "countUp(document.getElementById('hb-big'),Math.abs(big),big<0?'−$':'$')" in hero
    assert ".hb-hero .big.neg,.hb-hero .delta.bad{color:var(--hb-bad)}" in SRC
    assert "v.unpriced_wins||[]" in hero


# ── D12 / K6: Done / Not for us on the brief's lines ────────────────────────

def test_answerable_brief_lines_carry_done_and_not_for_us_but_not_track():
    follow = _fn("renderFollow")
    assert "l.rec_key&&l.answerable&&typeof recControlsHtml==='function'" in follow
    assert "recControlsHtml(l.rec_key,'home',l.module||BRIEF_MOD[l.key]||'home',{noTrack:1,also:l.rec_keys||[]})" in follow
    m = re.search(r"var BRIEF_MOD=\{([^}]*)\};", SRC)
    assert m and "stock:'food'" in m.group(1) and "slow_day:'marketing'" in m.group(1)
    ctrl = _fn("recControlsHtml")
    assert "!(opts&&opts.noTrack)&&REC_TRACKABLE[m]" in ctrl
    # The reason picker opens under the line, not inside it.
    assert ".hb-tl .it" in _fn("recPickerHost")


# ── D9 / K3: a reason behind the win-back and schedule "Not for us" ─────────

def test_the_win_back_not_for_us_asks_why_and_sends_the_code():
    i = SRC.index("closest('[data-winback-id],[data-winback-dismiss]')")
    h = SRC[i:SRC.index("function loadGuestHistory", i)]
    assert "recReasonPicker(row,{" in h
    assert "if(code)body.reason_code=code;if(note)body.reason=note;" in h
    assert "body:JSON.stringify({kind:'not_for_us'})" not in h        # no more answer without a reason


def test_the_schedule_review_x_asks_why_and_sends_the_code():
    i = SRC.index("while (el && el !== document.body && !(el.getAttribute && el.getAttribute('data-rec-action')))")
    h = SRC[i:SRC.index("function openIntelPanel", i)]
    assert "if (action === 'dismissed')" in h and "recReasonPicker(row || el.parentNode" in h
    assert "_sqRecAnswer(el, action, text, code, note, picker)" in h
    ans = _fn("_sqRecAnswer")
    assert "if (code) body.reason_code = code;" in ans and "if (note) body.reason = note;" in ans


# ── D14: the DSR sales block says gross the way it was built ────────────────

def test_the_dsr_sales_block_follows_the_gross_basis():
    i = SRC.index("    sales:function(b,p){")
    sales = SRC[i:SRC.index("    labor:function(b){", i)]
    assert "def.gross_basis==='all'?'in gross, not net':'not in gross or net'" in sales
    assert "' <b>Net</b> is '+esc(def.net)" in sales
    assert "Gross not measured \u2014 the POS didn\u2019t report '+esc(gm.join(' and '))" in sales


# ── D15: the grid's gross total says what it covers ─────────────────────────

def test_the_grid_gross_total_says_its_nights_and_mixed_bases():
    cell = _fn("totCell")
    assert "t.gross_days<t.days_measured" in cell and "' of '+t.days_measured+' nights'" in cell
    assert "t.gross_mixed" in cell and "mixed bases" in cell
    assert "h+=totCell(t,cols[i]);" in _fn("totalRow")
    assert "h+=totCell(wt,cols[c]);" in SRC                      # each week of a period, too
    assert "mixedNote([t,p])" in SRC and "mixedNote([t])" in SRC


# ── D17: the period-scheme picker keeps what is stored ──────────────────────

def test_the_scheme_picker_has_every_scheme_and_keeps_a_custom_one():
    sel = SRC[SRC.index('id="as-dsr-scheme"'):]
    sel = sel[:sel.index("</select>")]
    for v in ("4x13", "445", "454", "544", "custom"):
        assert 'value="%s"' % v in sel, v
    assert "Custom (from your accounting system)" in sel
    show = _fn("_dsrShowScheme")
    assert "sel.value = named ? v : 'custom'" in show and "inp.value = String(v || '')" in show
    save = _fn("saveDsrSetting")
    assert "if (v === 'custom')" in save and "_dsrLengths(" in save
    assert "field === 'dsr_gross_basis'" in save
    assert 'id="as-dsr-gross"' in SRC and '<option value="all">Everything rung (items, tax and voids)</option>' in SRC
    assert "fiscal_years: list" in _fn("_dsrSaveYears") and 'id="as-dsr-years"' in SRC


# ── D19: one question, one answer ───────────────────────────────────────────

def test_a_stream_that_started_is_never_asked_again():
    i = SRC.index("window.sendAskCavnar = function()")
    ask = SRC[i:SRC.index("\n};", i)]
    assert "if (_askDone) return;\n    _askDone = true;" in ask            # _finish runs once
    assert "_askStarted = true;" in ask
    j = ask.index("}).catch(function() {\n    if (_askDone) return;")
    tail = ask[j:]
    assert tail.index("if (_askStarted)") < tail.index("fetch('/api/ask-cavnar', {")
    # A stream that closes without an answer says so instead of hanging.
    assert "if (res.done) {" in ask and "The connection dropped before the answer finished" in ask


# ── D20: "What was missing?" keeps Send on its line ─────────────────────────

def test_the_ask_note_row_does_not_wrap_send():
    assert ".ask-fb.note{align-self:stretch;max-width:92%;flex-wrap:nowrap}" in SRC
    assert "row.className += ' note';" in SRC


# ── D21: Intel's recommendations come only from the recording read ─────────

def test_intel_does_not_print_its_recommendations_before_the_read():
    i = SRC.index('<div id="in2-recs">')
    block = SRC[i:SRC.index("</div>\n", i)]
    assert "{% for rec in recs %}" not in block and "dr-pulse" in block
    load = _fn("in2LoadRecs")
    assert "if(!d||!d.ok){box.innerHTML='';return;}" in load and ".catch(function(){box.innerHTML='';})" in load


def test_the_page_stays_es5():
    js = _scripts()
    for pat in (r"`", r"\bconst\s", r"\blet\s", r"=>"):
        assert not re.search(pat, js), pat


# ── the DSR server half ─────────────────────────────────────────────────────

@pytest.fixture
def db(db_path, monkeypatch):
    real = models.get_conn

    def conn(*a, **k):
        return real(db_path)
    for mod in list(sys.modules.values()):
        if mod is not None and getattr(mod, "get_conn", None) is real:
            monkeypatch.setattr(mod, "get_conn", conn)
    monkeypatch.setattr(models, "get_conn", conn)
    monkeypatch.setattr(models, "DB_PATH", db_path)
    monkeypatch.setattr(auth, "DB_PATH", db_path)
    monkeypatch.setattr(pos, "PROVIDERS", None)
    return db_path


def _ejs(db, **extra):
    rid = create_restaurant(Restaurant(name="Simple EJ's", owner_email="e@x.com", timezone="America/Chicago"),
                            db_path=db)
    update_restaurant(rid, dict({"fiscal_week_start_dow": 2, "fiscal_year_start": "2025-12-31",
                                 "fiscal_period_scheme": "445"}, **extra), db_path=db)
    return get_restaurant(rid, db_path=db)


def _night(db, rid, day, net, gross, basis):
    r = store.create_report(rid, day, trigger="sweep", db_path=db)
    store.save_block(r["id"], "sales", dsr.block(
        dsr.READY, source="rpower", metrics={"net": net, "gross": gross},
        detail={"definition": {"gross_basis": basis, "gross_missing": [] if gross is not None else ["voids"]}}),
        db_path=db)
    store.set_stage(r["id"], "collecting", db_path=db)
    store.set_stage(r["id"], "final", db_path=db)


def test_the_week_says_gross_was_measured_on_fewer_nights_and_under_two_bases(db):
    r = _ejs(db)
    _night(db, r.id, date(2026, 9, 16), 8900.0, 9800.0, "items")
    _night(db, r.id, date(2026, 9, 17), 8900.0, 9800.0, "items")
    _night(db, r.id, date(2026, 9, 19), 8900.0, None, "all")          # the POS sent no voids
    _night(db, r.id, date(2026, 9, 20), 8900.0, 10832.0, "all")
    w = rollup.week(r, date(2026, 9, 16), db_path=db)
    t = w["totals"]
    assert (t["net"], t["days_measured"]) == (35600.0, 4)
    assert (t["gross"], t["gross_days"]) == (30432.0, 3)                # 3 of 4 nights - said, not hidden
    assert t["gross_bases"] == ["all", "items"] and t["gross_mixed"] is True
    assert [d["gross_basis"] for d in w["days"]] == ["items", "items", None, "all", "all", None, None]
    p = rollup.period(r, date(2026, 9, 16), db_path=db)
    wk = [x for x in p["weeks"] if x["start"] == "2026-09-16"][0]["totals"]
    assert wk["gross_mixed"] is True and p["totals"]["gross_days"] == 3


def test_one_basis_is_not_mixed(db):
    r = _ejs(db)
    _night(db, r.id, date(2026, 9, 16), 8900.0, 9800.0, "items")
    t = rollup.week(r, date(2026, 9, 16), db_path=db)["totals"]
    assert t["gross_bases"] == ["items"] and t["gross_mixed"] is False


def _settings_client(db, monkeypatch, rid):
    from flask import Flask
    from strategy_routes import strategy_bp
    monkeypatch.setattr(auth, "get_current_user", lambda: {"id": 1, "restaurant_id": rid, "role": "client",
                                                           "is_admin": 0, "username": "o", "email": "o@x"})
    app = Flask(__name__)
    app.register_blueprint(strategy_bp)
    return app.test_client()


@pytest.mark.parametrize("body", [
    {"dsr_gross_basis": ["all"]}, {"dsr_gross_basis": {"all": 1}}, {"dsr_gross_basis": 1},
    {"dsr_deadline_hour": True}, {"fiscal_period_scheme": {"x": 1}}, {"fiscal_period_scheme": [4, 4, 6]},
    {"fiscal_years": "2026-12-30"}, {"fiscal_years": [{"start": "2026-12-30", "lengths": [4, 4]}]},
    {"fiscal_years": [{"start": "12/30/26", "lengths": "445"}]},
])
def test_the_settings_route_answers_a_wrong_type_with_a_400(db, monkeypatch, body):
    r = _ejs(db)
    c = _settings_client(db, monkeypatch, r.id)
    resp = c.post("/api/dsr/settings", json=body)
    assert resp.status_code == 400, (body, resp.status_code)
    assert resp.get_json()["ok"] is False and resp.get_json()["error"]


def test_a_body_that_is_not_an_object_is_a_400(db, monkeypatch):
    r = _ejs(db)
    c = _settings_client(db, monkeypatch, r.id)
    for raw in ('"dsr_gross_basis"', "[1, 2]", "3"):
        resp = c.post("/api/dsr/settings", data=raw, content_type="application/json")
        assert resp.status_code == 400, raw


def test_the_settings_route_takes_every_scheme_and_lengths_as_a_list(db, monkeypatch):
    r = _ejs(db)
    c = _settings_client(db, monkeypatch, r.id)
    for s in ("4x13", "445", "454", "544"):
        assert c.post("/api/dsr/settings", json={"fiscal_period_scheme": s}).status_code == 200, s
    ok = c.post("/api/dsr/settings", json={"fiscal_period_scheme": [4, 4, 5, 4, 4, 5, 4, 5, 4, 4, 4, 5]})
    assert ok.status_code == 200 and ok.get_json()["settings"]["fiscal_period_scheme"] == "4,4,5,4,4,5,4,5,4,4,4,5"


# ── D18: listed years (a 53-week year) ──────────────────────────────────────

FY27_53 = [4, 4, 5, 4, 4, 5, 4, 4, 5, 4, 5, 5]      # the long year: period 12 is five weeks


def test_a_listed_53_week_year_is_placed_by_its_own_lengths_and_the_next_year_starts_after_it():
    r = NS(fiscal_week_start_dow=2, fiscal_year_start="2025-12-31", fiscal_period_scheme="445",
           fiscal_years_json=json.dumps([{"start": "2026-12-30", "lengths": FY27_53}]))
    # Before it: the single start and scheme, unchanged.
    assert fiscal.label(r, "2026-09-22") == "Period 9 · Week 4"
    assert fiscal.position(r, "2026-12-29")["fiscal_year"] == 2026
    # Its 53rd week is Period 12, Week 5 - not Period 1 of the next year.
    p = fiscal.position(r, "2027-12-29")
    assert (p["fiscal_year"], p["period"], p["week"]) == (2027, 12, 5)
    assert fiscal.period_span(r, "2027-12-29") == (date(2027, 12, 1), date(2028, 1, 4))
    assert rollup.period_bounds(r, date(2027, 12, 29)) == (date(2027, 12, 1), date(2028, 1, 4))
    # The year after starts the day after it ends, and walks the default scheme.
    p = fiscal.position(r, "2028-01-05")
    assert (p["fiscal_year"], p["period"], p["week"]) == (2028, 1, 1)
    assert fiscal.position(r, "2029-01-03")["period"] == 1 and fiscal.position(r, "2029-01-03")["week"] == 1
    # Without the listed year the same day drifts into the wrong year's P1.
    r.fiscal_years_json = None
    assert fiscal.position(r, "2027-12-29")["period"] == 1


def test_listed_years_alone_anchor_a_calendar_and_bad_json_falls_back():
    r = NS(fiscal_week_start_dow=2, fiscal_year_start=None, fiscal_period_scheme="445",
           fiscal_years_json=json.dumps([{"start": "2026-12-30", "lengths": "445"}]))
    assert fiscal.position(r, "2026-12-30")["period"] == 1
    assert fiscal.position(r, "2026-12-23")["period"] == 13 - 1 and fiscal.position(r, "2026-12-23")["week"] == 5
    r.fiscal_years_json = "{not json"
    assert fiscal.position(r, "2026-12-30")["period"] is None             # nothing guessed
    r.fiscal_year_start = "2025-12-31"
    assert fiscal.label(r, "2026-09-22") == "Period 9 · Week 4"          # the single start rules


def test_parse_years_refuses_overlaps_and_bad_lengths_in_the_owners_dates():
    rows, err = fiscal.parse_years([{"start": "2026-12-30", "lengths": FY27_53},
                                    {"start": "2027-12-29", "lengths": "445"}])
    assert rows == [] and err == "The years starting 12/30/26 and 12/29/27 overlap."
    _rows, err = fiscal.parse_years([{"start": "2026-12-30", "lengths": [4, 4, 6]}])
    assert err.startswith("The year starting 12/30/26 needs")
    assert fiscal.parse_years(None) == ([], None) and fiscal.parse_years([]) == ([], None)


def test_the_fiscal_years_column_has_its_four_touch_points(db):
    r = _ejs(db, fiscal_years_json=json.dumps([{"start": "2026-12-30", "lengths": FY27_53}]))
    assert json.loads(r.fiscal_years_json)[0]["lengths"] == FY27_53          # whitelist + hydration
    assert "fiscal_years_json" in Restaurant.__dataclass_fields__
    conn = models.get_conn(db)
    try:
        cols = [c["name"] for c in conn.execute("PRAGMA table_info(restaurants)").fetchall()]
    finally:
        conn.close()
    assert "fiscal_years_json" in cols                                       # the migration


def test_the_settings_route_stores_lists_and_clears_years(db, monkeypatch):
    r = _ejs(db)
    c = _settings_client(db, monkeypatch, r.id)
    ok = c.post("/api/dsr/settings", json={"fiscal_years": [{"start": "2026-12-30", "lengths": FY27_53}]})
    assert ok.status_code == 200, ok.get_json()
    ys = ok.get_json()["settings"]["fiscal_years"]
    assert ys == [{"start": "2026-12-30", "start_label": "12/30/26", "lengths": FY27_53, "weeks": 53,
                   "fiscal_year": 2027}]
    assert json.loads(get_restaurant(r.id, db_path=db).fiscal_years_json) == [{"start": "2026-12-30", "lengths": FY27_53}]
    # A listed year has to start on the week's first day (Wednesday here)...
    bad = c.post("/api/dsr/settings", json={"fiscal_years": [{"start": "2026-12-31", "lengths": FY27_53}]})
    assert bad.status_code == 400 and "Wednesday" in bad.get_json()["error"] and "12/31/26" in bad.get_json()["error"]
    # ...and moving the week start re-checks the years already listed.
    assert c.post("/api/dsr/settings", json={"fiscal_week_start_dow": 0, "fiscal_year_start": ""}).status_code == 400
    assert c.post("/api/dsr/settings", json={"fiscal_years": []}).status_code == 200
    assert get_restaurant(r.id, db_path=db).fiscal_years_json is None
    assert c.get("/api/dsr/settings").get_json()["settings"]["fiscal_years"] == []
