"""The Daily Sales Report after Simple EJ's first night (owner, 9/28/26):

- departments mapped after a report count toward their categories when the
  report and the week are shown;
- the nights before the report started are filled from the POS (figures
  only: no report, no email, no model);
- a Labor block that withheld its dollars for want of a wage rate is costed
  at the POS's own pay where that prices the night.
"""
import json
from datetime import date, timedelta

import dsr
from dsr import access, backfill, rollup, store
from test_dsr_rollup import db, _ejs  # noqa: F401  (fixtures)

import models
import pos

SUN = date(2026, 9, 20)


def _report_before_mapping(db, rid, day=SUN):
    """A night built before any department was mapped: every department
    unmapped, with its dollars, the way block_sales stores it."""
    r = store.create_report(rid, day, trigger="sweep", db_path=db)
    deps = {"Food": 7674.0, "Beer": 2217.0, "Beverage": 409.0, "Discounts": -429.0}
    store.save_block(r["id"], "sales", dsr.block(
        dsr.READY, source="rpower", metrics={"net": 11617.0, "gross": 12102.0, f"cat:{dsr.UNMAPPED}": 9871.0},
        detail={"categories": [], "unmapped": [{"department": k, "net": v} for k, v in deps.items()]}),
        db_path=db)
    store.set_stage(r["id"], "collecting", db_path=db)
    store.set_stage(r["id"], "final", db_path=db)
    return store.get_report_by_id(r["id"], restaurant_id=rid, db_path=db)


def test_a_department_mapped_after_the_report_counts_toward_its_category(db):
    r = _ejs(db)
    rep = _report_before_mapping(db, r.id)
    for dep, cat in (("Food", "Food"), ("Beer", "Beer"), ("Beverage", "NA Beverage")):
        store.set_category(r.id, dep, cat, db_path=db)
    facts = access._live_categories(rep["facts"], rep, r)
    detail = facts["blocks"]["sales"]["detail"]
    assert [c["category"] for c in detail["categories"]] == ["Food", "Beer", "NA Beverage"]
    assert [u["department"] for u in detail["unmapped"]] == ["Discounts"]           # still unmapped: said
    m = facts["blocks"]["sales"]["metrics"]
    assert m["cat:Food"] == 7674.0 and m[f"cat:{dsr.UNMAPPED}"] == -429.0
    # the week grid reads the same night the same way
    w = rollup.week(r, SUN)
    sun = next(d for d in w["days"] if d["date"] == SUN.isoformat())
    assert sun["cats"]["Food"] == 7674.0 and sun["cats"]["NA Beverage"] == 409.0
    assert sun["cats"][dsr.UNMAPPED] == -429.0


def _stub_pos(monkeypatch, per_day):
    monkeypatch.setattr(pos, "connected_provider", lambda rid: ("rpower", object()))

    def day_sales(rid, d):
        v = per_day[d.isoformat()]
        return ({"gross": v * 1.05, "net": v, "transactions": 100, "guests": 150, "discounts": 20.0, "comps": 5.0,
                 "voids": 0.0, "refunds": 0.0, "tax": v * 0.09, "by_department": {"Food": v * 0.7, "Beer": v * 0.3}},
                "rpower")
    monkeypatch.setattr(pos, "fetch_day_sales", day_sales)


def test_the_nights_before_the_report_are_filled_from_the_pos(db, monkeypatch):
    import inventory_ledger
    r = _ejs(db)
    store.set_category(r.id, "Food", "Food", db_path=db)
    today = date(2026, 9, 28)
    monkeypatch.setattr(inventory_ledger, "local_today", lambda rid: today)
    days = [(today - timedelta(days=n)).isoformat() for n in (4, 3, 2, 1)]
    _report_before_mapping(db, r.id, date.fromisoformat(days[-1]))             # 9/27 has its report
    c = models.get_conn(db)
    for d in days:
        c.execute("INSERT INTO labor_daily_history (restaurant_id, date, sales, labor_cost, total_hours, final) "
                  "VALUES (?,?,?,?,?,1)", (r.id, d, 5000.0, 1800.0, 120.0))
    c.execute("UPDATE labor_daily_history SET sales=0 WHERE date=?", (days[0],))  # closed: not a $0 night
    c.commit()
    c.close()
    _stub_pos(monkeypatch, {d: 5000.0 for d in days})
    reports_before = len(store.list_reports(r.id, db_path=db)) if hasattr(store, "list_reports") else None
    out = backfill.backfill(r, days=10, db_path=db)
    assert (out["days"], out["filled"], out["failed"]) == (2, 2, 0)            # 9/25, 9/26
    c = models.get_conn(db)
    rows = {(x["business_date"], x["metric"]): (x["value"], x["source"], x["report_id"]) for x in c.execute(
        "SELECT business_date, metric, value, source, report_id FROM dsr_metrics WHERE restaurant_id=?", (r.id,))}
    n_reports = c.execute("SELECT COUNT(*) FROM dsr_reports WHERE restaurant_id=?", (r.id,)).fetchone()[0]
    c.close()
    assert rows[(days[1], "sales.net")] == (5000.0, "rpower_backfill", None)
    assert rows[(days[2], "sales.dep:Beer")][0] == 1500.0 and rows[(days[2], "sales.cat:Food")][0] == 3500.0
    assert (days[0], "sales.net") not in rows                                  # no trading, nothing filled
    # no labor $ on the assumed $26/hr: the hours only
    assert (days[1], "labor.cost") not in rows and rows[(days[1], "labor.hours")][0] == 120.0
    assert n_reports == 1                                                       # no report was written
    assert reports_before in (None, 1)
    assert backfill.backfill(r, days=10, db_path=db)["days"] == 0              # nothing left to fill
    # the week shows the filled nights beside the reported one
    w = rollup.week(r, date.fromisoformat(days[2]))
    filled = next(d for d in w["days"] if d["date"] == days[2])
    assert filled["net"] == 5000.0 and filled["status"] is None


def _shifts(db, rid, day, paid_share):
    rows = ["date,day,employee,role,shift_start,shift_end,scheduled_hours,actual_hours,sales,notes,pay_rate"]
    for i in range(10):
        rate = "9.0" if i < round(paid_share * 10) else ""
        rows.append(f"{day},Sunday,P{i},Server,10:00,18:00,8,8,,,{rate}")
    models.save_client_data(rid, "shifts", "\n".join(rows) + "\n", source="rpower", db_path=db)


def test_a_withheld_labor_block_is_costed_at_the_pos_pay(db):
    r = _ejs(db)
    rep = store.create_report(r.id, SUN, trigger="sweep", db_path=db)
    store.save_block(rep["id"], "sales", dsr.block(dsr.READY, source="rpower", metrics={"net": 10000.0}), db_path=db)
    store.save_block(rep["id"], "labor", dsr.block(dsr.READY, source="rpower",
                                                   metrics={"cost": None, "pct": None, "hours": 80.0,
                                                            "target_pct": 35.0},
                                                   detail={"cost_basis": "default"}), db_path=db)
    store.set_stage(rep["id"], "collecting", db_path=db)
    store.set_stage(rep["id"], "final", db_path=db)
    rep = store.get_report_by_id(rep["id"], restaurant_id=r.id, db_path=db)
    c = models.get_conn(db)
    c.execute("INSERT INTO labor_daily_history (restaurant_id, date, sales, labor_cost, total_hours, final) "
              "VALUES (?,?,?,?,?,1)", (r.id, SUN.isoformat(), 10000.0, 3600.0, 80.0))
    c.commit()
    c.close()
    _shifts(db, r.id, SUN.isoformat(), 0.3)                                     # mostly unpriced: still withheld
    assert access._live_labor(rep["facts"], rep, r)["blocks"]["labor"]["metrics"]["cost"] is None
    _shifts(db, r.id, SUN.isoformat(), 0.8)
    lb = access._live_labor(rep["facts"], rep, r)["blocks"]["labor"]
    assert (lb["metrics"]["cost"], lb["metrics"]["pct"], lb["metrics"]["vs_target_pts"]) == (3600.0, 36.0, 1.0)
    assert lb["detail"]["cost_basis"] == "pos_wages" and lb["detail"]["costed_after_report"] is True
    assert "20% of the night's hours have no POS rate" in lb["detail"]["cost_note"]


def _src():
    import os
    return open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "templates",
                             "dashboard.html"), encoding="utf-8").read()


def test_a_night_built_in_one_pass_says_its_time_once():
    s = _src()
    fn = s[s.index("function builtHtml(ck,n){"):s.index("// The verification count")]
    assert "var onePass=uniq.length===1;" in fn and "'built in one pass · '+uniq[0]" in fn
    assert "(onePass||st.at===prev)?'':st.at" in fn


def test_the_caution_sits_under_the_confidence_and_why_is_not_inset():
    s = _src()
    fn = s[s.index("  function line(c,o){"):s.index("  // The claim tag (J5)")]
    assert fn.index("class=\"cf-c\"") < fn.index("class=\"cf-r\"") < fn.index("cf-why")
    assert ".cf .cf-why{margin-left:0;padding-left:0!important}" in s


def test_the_labor_target_is_the_one_set_now(db):
    from models import update_restaurant, get_restaurant
    r = _ejs(db)
    facts = {"blocks": {"labor": dsr.block(dsr.READY, source="rpower",
                                           metrics={"pct": 35.7, "target_pct": 30.0, "vs_target_pts": 5.7},
                                           detail={"target_source": "default"})}}
    update_restaurant(r.id, {"labor_target_pct": 35.0}, db_path=db)
    r = get_restaurant(r.id, db_path=db)
    lb = access._live_target(facts, r)["blocks"]["labor"]
    assert (lb["metrics"]["target_pct"], lb["metrics"]["vs_target_pts"]) == (35.0, 0.7)
    assert lb["detail"]["target_source"] == "set"


# ── a pay change re-costs the history, the report and the week (9/30/26) ──

def test_a_manager_made_salaried_leaves_hourly_labor_everywhere_retroactively(db, monkeypatch):
    """Simple EJ's, 9/30/26: three managers made salaried "retroactive".
    Their punches leave every stored day's hourly labor (their salary is
    added when read), the night's report shows the re-costed figure with
    the one it replaced, and the week reads it too."""
    import json as _json
    import labor
    r = _ejs(db)
    day = SUN.isoformat()
    rows = ["date,day,employee,role,shift_start,shift_end,scheduled_hours,actual_hours,sales,notes,pay_rate",
            f"{day},Sunday,Andrew Marola,Manager FOH,10:00,20:00,10,10,,,20.0",
            f"{day},Sunday,Dana Reyes,Server,10:00,18:00,8,8,,,10.0"]
    models.save_client_data(r.id, "shifts", "\n".join(rows) + "\n", source="rpower", db_path=db)
    monkeypatch.setattr(models, "get_client_data", lambda rid, db_path=None: {"shifts_csv": "\n".join(rows) + "\n"})
    rep = store.create_report(r.id, SUN, trigger="sweep", db_path=db)
    store.save_block(rep["id"], "sales", dsr.block(dsr.READY, source="rpower", metrics={"net": 1000.0}), db_path=db)
    store.save_block(rep["id"], "labor", dsr.block(dsr.READY, source="rpower",
                                                   metrics={"cost": 280.0, "pct": 28.0, "hours": 18.0, "target_pct": 30.0},
                                                   detail={"cost_basis": "pos_wages"}), db_path=db)
    store.set_stage(rep["id"], "collecting", db_path=db)
    store.set_stage(rep["id"], "final", db_path=db)
    by_day = labor.full_history_by_day(r.id)
    models.save_labor_daily_history(r.id, by_day, db_path=db)
    before = models.get_conn(db).execute("SELECT labor_cost FROM labor_daily_history WHERE restaurant_id=? AND date=?",
                                         (r.id, day)).fetchone()[0]
    models.update_restaurant(r.id, {"salaried_staff_json": _json.dumps([{"name": "Andrew Marola", "annual": 55000}])},
                             db_path=db)
    assert models.recost_labor_history(r.id, db_path=db) == 1
    after = models.get_conn(db).execute("SELECT labor_cost FROM labor_daily_history WHERE restaurant_id=? AND date=?",
                                        (r.id, day)).fetchone()[0]
    assert after < before and round(before - after) == 200          # his 10 hours at $20 left hourly labor
    rep = store.get_report_by_id(rep["id"], restaurant_id=r.id, db_path=db)
    lb = access._live_recost(rep["facts"], rep)["blocks"]["labor"]
    assert lb["metrics"]["cost"] == round(after, 2) and lb["detail"]["recosted_from"] == 280.0
    assert lb["detail"]["recosted_after_report"] is True
    week = next(d for d in rollup.week(r, SUN)["days"] if d["date"] == day)
    assert week["labor_cost"] == round(after, 2)


def test_a_restaurant_on_sample_shifts_is_never_recosted(db):
    r = _ejs(db)
    assert models.recost_labor_history(r.id, db_path=db) == 0


def test_a_salaried_managers_rpower_overtime_note_is_not_the_nights_overtime(db):
    """Will, 9/30/26: salaried people are never overtime — the report's
    overtime left their punches out as Labor's analysis does."""
    import json as _json
    from dsr import block_labor
    r = _ejs(db)
    models.update_restaurant(r.id, {"salaried_staff_json": _json.dumps([{"name": "Andrew Marola", "annual": 55000}])},
                             db_path=db)
    r = models.get_restaurant(r.id, db_path=db)

    class _Ctx:
        restaurant = r
        restaurant_id = r.id
        day = SUN.isoformat()
    rows = [{"employee": "Andrew Marola", "notes": "OT 2.5h"}, {"employee": "Dana Reyes", "notes": "OT 1.0h"}]
    ot, src = block_labor._overtime(_Ctx(), "rpower", rows, rows)
    ot_all, _ = block_labor._overtime(_Ctx(), "rpower", rows[1:], rows[1:])
    assert src == "rpower" and ot == ot_all
