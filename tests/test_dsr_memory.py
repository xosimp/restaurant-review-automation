"""DSR phase 6 — memory: the nightly reports as Ask, the morning brief and
the Last Year import read them (dsr/memory.py, dsr/history_import.py, the
read_dsr / find_days / read_week / read_period Ask tools).

The property under test everywhere: role-based redaction holds in Ask
exactly as on screen. A manager never reads the budget, prime cost, the loss
lines (without LOSS_VIEW) or food cost (without FOOD_COST_VIEW) — not in a
tool result, not in Ask's context, not from a cache warmed by the owner.
And every read is the asking restaurant's own.
"""
import io
import json
import sys
import zipfile
from datetime import date, timedelta

import pytest
from flask import Flask

import auth
import models
import pos
import dsr
import ask_cavnar
import ask_cavnar_tools as tools
from dsr import access, history_import, memory, rollup, store
from models import Restaurant, create_restaurant, get_restaurant, update_restaurant

WED = date(2026, 9, 16)
TUE = date(2026, 9, 22)
OWNER = {"id": 1, "role": "client", "is_admin": 0}
MANAGER = {"id": 2, "role": "manager", "is_admin": 0}


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
    ask_cavnar._CONTEXT_CACHE.clear()
    return db_path


def _ejs(db, name="Simple EJ's"):
    rid = create_restaurant(Restaurant(name=name, owner_email="e@x.com", timezone="America/Chicago"), db_path=db)
    update_restaurant(rid, {"fiscal_week_start_dow": 2, "fiscal_year_start": "2026-01-14",
                            "fiscal_period_scheme": "4x13"}, db_path=db)
    return get_restaurant(rid, db_path=db)


LEAD = {"text": "Net sales were $5,000, $200 under the $5,200 budget. Labor ran 27.1%.",
        "cites": ["sales.net", "sales.vs_budget_net", "sales.budget_net", "labor.pct"]}
OPS = {"text": "Net sales were $5,000 on 180 checks and labor ran 27.1%. Watch the Tuesday schedule.",
       "cites": ["sales.net", "sales.transactions", "labor.pct"]}


def _night(db, rid, day, net=5000.0, labor_pct=27.1, status="final", influence="Rain all night, patio closed.",
           narrative=True):
    r = store.create_report(rid, day, trigger="sweep", db_path=db)
    store.save_block(r["id"], "sales", dsr.block(dsr.READY, source="rpower", metrics={
        "net": net, "gross": net + 300, "transactions": 180, "comps": 45.0, "voids": 12.0,
        "budget_net": 5200.0, "vs_budget_net": net - 5200, "vs_budget_net_pct": round((net - 5200) / 52, 1),
        "forecast_net": 4000.0, "vs_forecast_pct": round((net - 4000) / 40, 1), "cat:Food": net * 0.6},
        detail={"budget": {"net": 5200.0}, "top_items": [{"name": "Smash Burger", "qty": 41, "net": 615.0}]}),
        db_path=db)
    store.save_block(r["id"], "labor", dsr.block(dsr.READY, source="rpower", metrics={
        "pct": labor_pct, "cost": round(net * labor_pct / 100, 2), "target_pct": 26.0, "prime_cost_pct": 61.0}),
        db_path=db)
    store.save_block(r["id"], "food", dsr.block(dsr.READY, source="cavnar", metrics={"est_food_cost_pct": 31.5}),
                     db_path=db)
    store.save_block(r["id"], "closeout", dsr.block(dsr.READY, source="manager", metrics={"filed": 1},
                                                    detail={"fields": {"went_wrong": "Walk-in door sticking.",
                                                                       "influence": influence},
                                                            "labels": {"went_wrong": "What went wrong",
                                                                       "influence": "Influence/Result"},
                                                            "order": ["went_wrong", "influence"]}), db_path=db)
    from dsr import fiscal
    store.save_fiscal(r["id"], fiscal.position(get_restaurant(rid, db_path=db), day), db_path=db)
    if narrative:
        store.save_narrative(r["id"], {
            "executive_summary": LEAD, "operations_summary": OPS,
            "went_well": [{"text": "Food cost held at 31.5%.", "cites": ["food.est_food_cost_pct"]}],
            "needs_attention": [{"text": "Comps were $45.", "cites": ["sales.comps"]},
                                {"text": "Labor ran 27.1%.", "cites": ["labor.pct"]}],
            "actions_tomorrow": [{"text": "Trim one server Tuesday.", "why": "Labor ran 27.1%.",
                                  "urgency": "next_schedule", "cites": ["labor.pct"]}]}, db_path=db)
    store.set_stage(r["id"], "collecting", db_path=db)
    store.set_stage(r["id"], status, db_path=db)
    return r


def _run(name, rid, args, user, restaurant):
    viewer = tools.viewer_restaurant(restaurant, user)
    return json.loads(tools.run_read_tool(name, rid, args, restaurant=viewer))


def _text(payload):
    return json.dumps(payload).lower()


# ── registry ────────────────────────────────────────────────────────────────

def test_the_dsr_tools_are_registered_viewer_aware_reads():
    for name in ("read_dsr", "find_days", "read_week", "read_period"):
        t = tools._BY_NAME[name]
        assert t["kind"] == "read" and t.get("wants_viewer") is True, name
    assert tools.reads_public_text("read_dsr") and tools.reads_public_text("read_week")


def test_a_location_with_the_dsr_switched_off_is_not_offered_them(db):
    r = _ejs(db)
    names = {s["name"] for s in tools.tool_specs(r)}
    assert {"read_dsr", "find_days"} <= names
    update_restaurant(r.id, {"dsr_enabled": 0}, db_path=db)
    names = {s["name"] for s in tools.tool_specs(get_restaurant(r.id, db_path=db))}
    assert not names & tools._DSR_TOOLS


# ── read_dsr ────────────────────────────────────────────────────────────────

def test_read_dsr_gives_the_owner_everything_and_the_manager_the_manager_dsr(db):
    r = _ejs(db)
    _night(db, r.id, TUE)
    owner = _run("read_dsr", r.id, {"date": "2026-09-22"}, OWNER, r)
    assert owner["view"] == "owner" and owner["label"] == "Tue 9/22/26" and owner["status"] == "final"
    assert owner["blocks"]["sales"]["metrics"]["budget_net"] == 5200.0
    assert owner["blocks"]["sales"]["metrics"]["comps"] == 45.0 and "food" in owner["blocks"]
    assert owner["summary"]["lead"] == LEAD["text"] and "lead_from" not in owner["summary"]

    mgr = _run("read_dsr", r.id, {"date": "2026-09-22"}, MANAGER, r)
    assert mgr["view"] == "manager"
    blob = _text(mgr)
    for gone in ("budget", "prime_cost", "comps", "voids", "31.5", "5,200", "5200"):
        assert gone not in blob, gone
    assert "food" not in mgr["blocks"] and "food" in mgr["withheld"]
    assert mgr["blocks"]["sales"]["metrics"]["net"] == 5000.0 and mgr["blocks"]["labor"]["metrics"]["pct"] == 27.1
    # The lead cited the budget, so the manager's opening is the operations summary.
    assert mgr["summary"]["lead"] == OPS["text"] and mgr["summary"]["lead_from"] == "operations_summary"
    assert mgr["summary"]["needs_attention"] == ["Labor ran 27.1%."]


def test_a_manager_granted_comps_and_voids_reads_them_but_never_the_budget(db):
    r = _ejs(db)
    _night(db, r.id, TUE)
    granted = dict(MANAGER, grants=frozenset({"loss.view"}))
    mgr = _run("read_dsr", r.id, {"date": "2026-09-22"}, granted, r)
    assert mgr["blocks"]["sales"]["metrics"]["comps"] == 45.0
    assert "budget" not in _text(mgr)


def test_read_dsr_fences_the_managers_words_and_says_provisional(db):
    r = _ejs(db)
    _night(db, r.id, TUE, status="provisional")
    out = _run("read_dsr", r.id, {"date": "2026-09-22"}, OWNER, r)
    from ai_guard import UNTRUSTED_OPEN
    assert out["provisional"] is True and "provisional_note" in out
    assert all(n.startswith(UNTRUSTED_OPEN) or UNTRUSTED_OPEN in n for n in out["notes"])
    assert any("Walk-in door sticking." in n for n in out["notes"]) and "_warning" in out


def test_read_dsr_reads_the_finished_version_while_a_rerun_collects(db):
    r = _ejs(db)
    _night(db, r.id, TUE)
    store.create_report(r.id, TUE, trigger="manual", db_path=db)        # v2, still scheduled
    out = _run("read_dsr", r.id, {"date": "2026-09-22"}, OWNER, r)
    assert out["version"] == 1 and out["status"] == "final"


def test_read_dsr_says_when_a_night_has_no_report_and_refuses_a_bad_date(db):
    r = _ejs(db)
    _night(db, r.id, TUE)
    out = _run("read_dsr", r.id, {"date": "2026-09-01"}, OWNER, r)
    assert out["exists"] is False and "Tue 9/22/26 (2026-09-22)" in out["nights_with_reports"]
    assert "YYYY-MM-DD" in _run("read_dsr", r.id, {"date": "9/22/26"}, OWNER, r)["error"]
    # No date: the latest report.
    assert _run("read_dsr", r.id, {}, OWNER, r)["date"] == "2026-09-22"


def test_an_employee_login_reads_no_dsr_through_ask(db):
    r = _ejs(db)
    _night(db, r.id, TUE)
    out = _run("read_dsr", r.id, {"date": "2026-09-22"}, {"id": 3, "role": "employee"}, r)
    assert "error" in out and "blocks" not in out


# ── find_days ───────────────────────────────────────────────────────────────

def test_find_days_answers_every_night_labor_ran_over_25(db):
    r = _ejs(db)
    _night(db, r.id, WED, labor_pct=22.0)
    _night(db, r.id, WED + timedelta(days=1), labor_pct=26.4)
    _night(db, r.id, TUE, labor_pct=27.1)
    out = _run("find_days", r.id, {"metric": "labor.pct", "op": ">", "value": 25}, MANAGER, r)
    assert [n["date"] for n in out["nights"]] == ["2026-09-22", "2026-09-17"]
    assert out["count"] == 2 and out["measured_nights"] == 3 and out["nights"][0]["label"] == "Tue 9/22/26"
    ranged = _run("find_days", r.id, {"metric": "labor.pct", "op": ">", "value": 25, "end": "2026-09-20"}, OWNER, r)
    assert [n["date"] for n in ranged["nights"]] == ["2026-09-17"]


@pytest.mark.parametrize("metric", ["sales.budget_net", "sales.vs_budget_net", "sales.comps", "sales.voids",
                                    "labor.prime_cost_pct", "food.est_food_cost_pct", "sales.budget_gross"])
def test_find_days_refuses_a_manager_every_owner_only_metric(db, metric):
    r = _ejs(db)
    _night(db, r.id, TUE)
    out = _run("find_days", r.id, {"metric": metric, "op": ">", "value": 0}, MANAGER, r)
    assert out.get("refused") is True and "nights" not in out
    # The owner may ask — when it was recorded at all.
    owner = _run("find_days", r.id, {"metric": metric, "op": ">", "value": -1e9}, OWNER, r)
    assert owner.get("refused") is None


def test_find_days_validates_the_metric_against_what_was_recorded(db):
    r = _ejs(db)
    _night(db, r.id, TUE)
    out = _run("find_days", r.id, {"metric": "labor.percent", "op": ">", "value": 25}, MANAGER, r)
    assert "error" in out and "labor.pct" in out["metrics"]
    assert not [m for m in out["metrics"] if "budget" in m or "comps" in m or m.startswith("food.")]
    assert "op must be" in _run("find_days", r.id, {"metric": "labor.pct", "op": "over", "value": 25}, OWNER, r)["error"]
    assert "number" in _run("find_days", r.id, {"metric": "labor.pct", "op": ">", "value": "lots"}, OWNER, r)["error"]


# ── read_week / read_period ─────────────────────────────────────────────────

def test_the_week_and_period_through_ask_keep_the_budget_the_owners(db):
    r = _ejs(db)
    _night(db, r.id, WED)
    store.set_budget(r.id, WED, gross=5500, net=5200, db_path=db)
    owner = _run("read_week", r.id, {"date": "2026-09-16"}, OWNER, r)
    assert owner["label"] == "Period 9 · Week 4" and owner["days"][0]["budget_net"] == 5200.0
    from ai_guard import UNTRUSTED_OPEN
    assert UNTRUSTED_OPEN in owner["days"][0]["notes"]                 # the closer's words, fenced
    assert "net" not in owner["days"][1]                                # unmeasured is absent, never 0
    mgr = _run("read_week", r.id, {"date": "2026-09-16"}, MANAGER, r)
    # Gross goes with the comps it would reveal (D2-2).
    assert mgr["withheld"] == ["budget", "gross"] and mgr["days"][0]["net"] == 5000.0
    assert "gross" not in mgr["days"][0]
    assert "budget" not in _text(dict(mgr, withheld=None))
    per = _run("read_period", r.id, {"date": "2026-09-16"}, MANAGER, r)
    assert per["label"] == "Period 9" and len(per["weeks"]) == 4 and "budget" not in _text(dict(per, withheld=None))
    assert _run("read_period", r.id, {"date": "2026-09-16"}, OWNER, r)["totals"]["budget_net"] == 5200.0


def test_a_login_without_labor_gets_no_labor_columns_in_the_grid():
    grid = {"days": [{"date": "2026-09-16", "net": 10.0, "labor_cost": 3.0, "labor_pct": 30.0, "budget_net": 9.0}],
            "totals": {"labor_pct": 30.0, "net": 10.0}}
    no_labor = {"role": "manager", "grants": frozenset()}
    import permissions
    orig = permissions.has_permission
    try:
        permissions.has_permission = lambda u, p: p != permissions.LABOR_VIEW and orig(u, p)
        g = access.redact_grid(grid, no_labor)
    finally:
        permissions.has_permission = orig
    assert g["days"][0] == {"date": "2026-09-16", "net": 10.0} and g["withheld"] == ["budget", "labor", "gross"]
    # The workbook leaves the withheld columns out — no column of dashes (D2-11).
    from dsr import xlsx
    heads = [h for h, _k, _t in xlsx.columns(g)]
    assert "Labor %" not in heads and "Gross" not in heads and "Net" in heads


# ── tenancy ─────────────────────────────────────────────────────────────────

def test_every_dsr_tool_reads_only_the_asking_restaurant(db):
    ejs = _ejs(db)
    other = _ejs(db, name="Elsewhere")
    _night(db, ejs.id, TUE, labor_pct=40.0)
    # The other restaurant's owner, asking about the same night and metric.
    assert _run("read_dsr", other.id, {"date": "2026-09-22"}, OWNER, other)["exists"] is False
    out = _run("find_days", other.id, {"metric": "labor.pct", "op": ">", "value": 25}, OWNER, other)
    assert "error" in out and out["metrics"] == []
    assert _run("read_week", other.id, {"date": "2026-09-22"}, OWNER, other)["totals"].get("net") is None
    # Model input can never carry the viewer or another restaurant.
    smuggled = _run("read_dsr", other.id, {"date": "2026-09-22", "_viewer": "x", "restaurant_id": ejs.id}, OWNER, other)
    assert "blocks" not in smuggled


# ── Ask's context ───────────────────────────────────────────────────────────

def _today_after(monkeypatch, day):
    import time_utils
    from datetime import datetime
    monkeypatch.setattr(time_utils, "restaurant_now_by_id",
                        lambda rid, naive=False: datetime.combine(day + timedelta(days=1), datetime.min.time()))


def test_asks_context_carries_last_night_redacted_for_the_viewer(db, monkeypatch):
    r = _ejs(db)
    _night(db, r.id, TUE)
    _today_after(monkeypatch, TUE)
    owner_ctx = ask_cavnar.build_context(tools.viewer_restaurant(r, OWNER))
    assert "LAST NIGHT'S DAILY SALES REPORT (Tue 9/22/26 · Period 9 · Week 4; final)" in owner_ctx
    assert "sales.net 5,000" in owner_ctx and "sales.vs_budget_net -200" in owner_ctx
    assert LEAD["text"] in owner_ctx and "To do: Trim one server Tuesday." in owner_ctx
    # Same restaurant, a minute later, a manager: never the owner's cached copy.
    mgr_ctx = ask_cavnar.build_context(tools.viewer_restaurant(r, MANAGER))
    section = mgr_ctx[mgr_ctx.index("LAST NIGHT'S DAILY SALES REPORT"):]
    section = section[:section.index("\n\n")] if "\n\n" in section else section
    assert "sales.net 5,000" in section and "labor.pct 27.1%" in section
    for gone in ("budget", "comps", "prime_cost", "food.", LEAD["text"]):
        assert gone not in section, gone
    assert OPS["text"] in section


def test_asks_context_leaves_out_a_stale_or_missing_report(db, monkeypatch):
    r = _ejs(db)
    _today_after(monkeypatch, TUE)
    assert memory.context_block(r.id, OWNER, TUE + timedelta(days=1)) == ""
    _night(db, r.id, TUE - timedelta(days=10))
    assert memory.context_block(r.id, OWNER, TUE + timedelta(days=1)) == ""
    _night(db, r.id, TUE, status="provisional")
    assert "provisional" in memory.context_block(r.id, OWNER, TUE + timedelta(days=1))


def test_the_context_cache_key_carries_the_dsr_view():
    r = Restaurant(id=5, name="K", owner_email="k@x.com")
    assert tools.dsr_view_key(tools.viewer_restaurant(r, OWNER)) == "owner"
    assert tools.dsr_view_key(tools.viewer_restaurant(r, MANAGER)) == "manager"
    assert tools.dsr_view_key(r) == "owner"              # no login behind it: the owner's view


# ── the morning brief ───────────────────────────────────────────────────────

def test_the_morning_brief_reads_last_nights_dsr(db):
    import morning_brief
    r = _ejs(db)
    _night(db, r.id, TUE, net=5000.0)
    brief = morning_brief.build(r.id, restaurant=r, today=TUE + timedelta(days=1), db_path=db)
    (y,) = [l for l in brief["lines"] if l["key"] == "yesterday"]
    assert y["source"] == "dsr" and y["dsr_date"] == "2026-09-22"
    # The report's own figures: net, its forecast comparison (25% over $4,000), labor.
    assert y["text"] == "Last night: $5,000 net sales, 25% above a typical Tuesday ($4,000); labor 27.1% against a 26% target."
    mgr = morning_brief.build(r.id, restaurant=r, today=TUE + timedelta(days=1), db_path=db, viewer=dict(MANAGER, restaurant_id=r.id))
    (my,) = [l for l in mgr["lines"] if l["key"] == "yesterday"]
    assert "budget" not in my["text"].lower()


def test_the_morning_brief_says_provisional_and_falls_back_without_a_report(db):
    import morning_brief
    r = _ejs(db)
    _night(db, r.id, TUE, status="provisional")
    brief = morning_brief.build(r.id, restaurant=r, today=TUE + timedelta(days=1), db_path=db)
    (y,) = [l for l in brief["lines"] if l["key"] == "yesterday"]
    assert y["text"].endswith("Provisional — some data was still syncing.")
    # Two days on there is no report for "last night": no DSR line.
    later = morning_brief.build(r.id, restaurant=r, today=TUE + timedelta(days=2), db_path=db)
    assert not [l for l in later["lines"] if l.get("source") == "dsr"]


# ── the Last Year import ────────────────────────────────────────────────────

def _xlsx(rows, date1904=False):
    """A minimal real .xlsx: shared strings for text, numbers as numbers."""
    shared, sheet_rows = [], []

    def ref(ci, ri):
        return f"{chr(65 + ci)}{ri}"
    for ri, row in enumerate(rows, start=1):
        if row is None:
            continue                                   # Excel leaves empty rows out
        cells = []
        for ci, v in enumerate(row):
            if v is None:
                continue
            if isinstance(v, str):
                if v not in shared:
                    shared.append(v)
                cells.append(f'<c r="{ref(ci, ri)}" t="s"><v>{shared.index(v)}</v></c>')
            else:
                cells.append(f'<c r="{ref(ci, ri)}"><v>{v}</v></c>')
        sheet_rows.append(f'<row r="{ri}">{"".join(cells)}</row>')
    ns = 'xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"'
    rns = 'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"'
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("[Content_Types].xml", '<?xml version="1.0"?><Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types"/>')
        z.writestr("xl/workbook.xml", f'<?xml version="1.0"?><workbook {ns} {rns}>'
                   + (f'<workbookPr date1904="1"/>' if date1904 else "")
                   + '<sheets><sheet name="P9 W1" sheetId="1" r:id="rId1"/></sheets></workbook>')
        z.writestr("xl/_rels/workbook.xml.rels", '<?xml version="1.0"?><Relationships '
                   'xmlns="http://schemas.openxmlformats.org/package/2006/relationships"><Relationship Id="rId1" '
                   'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" '
                   'Target="worksheets/sheet1.xml"/></Relationships>')
        z.writestr("xl/sharedStrings.xml", f'<?xml version="1.0"?><sst {ns}>'
                   + "".join(f"<si><t>{s}</t></si>" for s in shared) + "</sst>")
        z.writestr("xl/worksheets/sheet1.xml", f'<?xml version="1.0"?><worksheet {ns}><sheetData>'
                   + "".join(sheet_rows) + "</sheetData></worksheet>")
    return buf.getvalue()


def _serial(d, date1904=False):
    return (d - (date(1904, 1, 1) if date1904 else date(1899, 12, 30))).days


def test_the_xlsx_template_imports_with_serial_dates_currency_and_blanks():
    rows = [["Wed Sep 16 2025 import"],                                 # a title row above the header
            ["  date ", "GROSS SALES", "Net", "Food", "Liquor", "Beer", "Wine", "Retail", "NA Bev", "Notes"],
            [_serial(date(2025, 9, 17)), "$7,812.40", 7400.5, 4100, "$1,200.00", None, 600, None, 88, "rainy"],
            None,                                                        # an empty row Excel left out
            [_serial(date(2025, 9, 18)), None, None, None],               # a closed day: skipped, not $0
            [_serial(date(2025, 9, 19)), "$abc", 10, None],               # a bad figure: an error
            ["9/20/25", "(12.50)", "$9,001", None]]
    out = history_import.parse("ly.xlsx", _xlsx(rows), today=date(2026, 9, 23))
    assert out["layout"] == "template" and out["skipped"] == 1
    assert out["rows"][0] == {"date": "2025-09-17", "gross": 7812.4, "net": 7400.5,
                              "categories": {"Food": 4100.0, "Liquor": 1200.0, "Wine": 600.0, "NA Beverage": 88.0}}
    assert out["rows"][1] == {"date": "2025-09-20", "gross": -12.5, "net": 9001.0, "categories": {}}
    assert out["errors"] == ["P9 W1 row 6: Gross '$abc' isn't a number."]


def test_the_1904_date_system_is_honoured():
    rows = [["Day", "Net"], [_serial(date(2025, 9, 17), True), 100]]
    out = history_import.parse("x.xlsx", _xlsx(rows, date1904=True), today=date(2026, 9, 23))
    assert out["rows"][0]["date"] == "2025-09-17"


def test_the_csv_template_imports_and_refuses_what_it_should():
    csv_text = ("Date,Gross,Net,Food,Liquor,Beer,Wine,Retail,NA Beverage\n"
                "9/17/2025,\"$7,812.40\",7400.50,,,,,,\n"
                "2025-09-17,1,1,,,,,,\n"                    # a repeat: the first wins
                "Thu 9/18/25,,,,,,,,\n"                     # blank: skipped
                "12/1/2026,5,5,,,,,,\n"                      # the future
                "not a date,5,5,,,,,,\n"
                ",,,,,,,,\n")
    out = history_import.parse("ly.csv", csv_text.encode(), today=date(2026, 9, 23))
    assert [r["date"] for r in out["rows"]] == ["2025-09-17"] and out["rows"][0]["gross"] == 7812.4
    assert out["skipped"] == 1 and len(out["errors"]) == 3
    assert any("already on Row 2" in e for e in out["errors"]) and any("future" in e for e in out["errors"])
    assert any("'not a date' isn't a date" in e for e in out["errors"])


@pytest.mark.parametrize("name, data, needle", [
    ("ly.txt", b"Date,Net\n", ".xlsx or .csv"),
    ("ly.xlsx", b"PK\x03\x04not really", "readable"),
    ("ly.csv", b"Week,Total\n1,2\n", "Couldn't find the columns"),
    ("ly.csv", b"", "empty"),
    ("ly.csv", b"x" * (history_import.MAX_BYTES + 1), "over 2 MB"),
])
def test_an_unreadable_file_is_refused_whole(name, data, needle):
    with pytest.raises(history_import.HistoryImportError) as e:
        history_import.parse(name, data, today=date(2026, 9, 23))
    assert needle in str(e.value)


def test_a_workbook_with_a_dtd_is_refused():
    bomb = _xlsx([["Date", "Net"]]).replace(b"", b"")
    buf = io.BytesIO()
    with zipfile.ZipFile(io.BytesIO(bomb)) as src, zipfile.ZipFile(buf, "w") as dst:
        for item in src.infolist():
            data = src.read(item)
            if item.filename == "xl/sharedStrings.xml":
                data = b'<?xml version="1.0"?><!DOCTYPE lol [<!ENTITY a "aaaa">]>' + data.split(b"?>", 1)[1]
            dst.writestr(item, data)
    with pytest.raises(history_import.HistoryImportError):
        history_import.parse("x.xlsx", buf.getvalue(), today=date(2026, 9, 23))


@pytest.fixture
def client(db):
    from strategy_routes import strategy_bp, strategy_mobile_bp
    app = Flask(__name__, template_folder="../templates")
    app.register_blueprint(strategy_bp)
    app.register_blueprint(strategy_mobile_bp)
    return app.test_client()


def _as(monkeypatch, rid, role):
    monkeypatch.setattr(auth, "get_current_user", lambda: {
        "id": 7, "restaurant_id": rid, "is_admin": 0, "role": role, "username": "u", "email": "u@x.com"})


def test_the_import_route_is_the_owners_and_fills_last_year(client, db, monkeypatch):
    import ai_utils
    monkeypatch.setattr(ai_utils, "ai_rate_limited", lambda *a, **k: False)
    r = _ejs(db)
    other = _ejs(db, name="Elsewhere")
    body = _xlsx([["Date", "Gross", "Net", "Food"],
                  [_serial(WED - timedelta(days=364)), 8000, 7600, 5000],
                  [_serial(WED - timedelta(days=363)), None, None, None]])
    _as(monkeypatch, r.id, "manager")
    res = client.post("/api/dsr/history/import", data={"file": (io.BytesIO(body), "ly.xlsx")},
                      content_type="multipart/form-data")
    assert res.status_code == 403
    _as(monkeypatch, r.id, "client")
    res = client.post("/api/dsr/history/import", data={"file": (io.BytesIO(body), "ly.xlsx")},
                      content_type="multipart/form-data")
    assert res.status_code == 200
    assert res.get_json() == {"ok": True, "imported": 1, "skipped": 1, "errors": []}
    # Last Year in the week grid now reads it — for this restaurant only.
    week = rollup.week(get_restaurant(r.id, db_path=db), WED)
    assert week["days"][0]["last_year_net"] == 7600.0 and week["days"][0]["last_year_source"] == "import"
    assert store.history_for(other.id, "2025-01-01", "2025-12-31", db_path=db) == {}
    # The mobile twin, and the refusals.
    res = client.post("/mobile/api/dsr/history/import", data={"file": (io.BytesIO(b"nope"), "ly.pdf")},
                      content_type="multipart/form-data")
    assert res.status_code in (400, 401)
    bad = client.post("/api/dsr/history/import", data={"file": (io.BytesIO(b"Week,Total\n"), "ly.csv")},
                      content_type="multipart/form-data")
    assert bad.status_code == 400 and bad.get_json()["ok"] is False and bad.get_json()["imported"] == 0
    assert client.post("/api/dsr/history/import", data={}, content_type="multipart/form-data").status_code == 400


def test_the_template_download(client, db, monkeypatch):
    r = _ejs(db)
    _as(monkeypatch, r.id, "client")
    res = client.get("/api/dsr/history/template.csv")
    assert res.status_code == 200 and res.mimetype == "text/csv"
    assert res.get_data(as_text=True).strip() == "Date,Gross,Net,Food,Liquor,Beer,Wine,Retail,NA Beverage"
    assert "attachment" in res.headers["Content-Disposition"]
    _as(monkeypatch, r.id, "employee")
    assert client.get("/api/dsr/history/template.csv").status_code == 403
