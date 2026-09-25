"""dsr.block_sales and dsr.block_labor — the two blocks every night rests on.

The rule that governs both: never fabricate. A comparison whose baseline is
missing is None, not 0; a POS department nobody mapped is listed as
unmapped, not guessed; a block that cannot be measured says why, with the
status the pipeline acts on (awaiting is retried, unavailable is not).
"""
import json
import sys
from datetime import date, datetime

import pytest

import models
import pos
import dsr
from dsr import store, block_sales, block_labor
from models import Restaurant, create_restaurant, update_restaurant, get_restaurant

DAY = date(2026, 9, 22)


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
    monkeypatch.setattr(pos, "PROVIDERS", None)
    return db_path


def _rid(db, **fields):
    rid = create_restaurant(Restaurant(name="Block Co", owner_email="b@x.com"), db_path=db)
    if fields:
        update_restaurant(rid, fields, db_path=db)
    return rid


def _ctx(db, rid, closed="pos"):
    ctx = dsr.Context(get_restaurant(rid, db_path=db), DAY, db_path=db, now_utc=datetime(2026, 9, 23, 5, 0))
    ctx.day_closed = closed
    return ctx


def _day(net=2000.0, by_department=None, items=None, by_hour=None):
    return {"gross": net + 100, "net": net, "transactions": 80, "guests": 120, "discounts": 60.0, "comps": 40.0,
            "voids": 25.0, "refunds": 0.0, "tax": 160.0,
            "by_department": by_department if by_department is not None else {"Food": net * 0.7, "Beer": net * 0.3},
            "by_hour": by_hour if by_hour is not None else {"12": net * 0.4, "19": net * 0.6},
            "items": items or [], "net_deductions": ["discounts", "comps"], "source_checks": {}}


def _pos(monkeypatch, day=None, provider="rpower", error=None):
    monkeypatch.setattr(pos, "connected_provider", lambda rid: (provider, object()) if provider else (None, None))

    def fetch(rid, d):
        if error:
            raise error
        return day or _day(), provider
    monkeypatch.setattr(pos, "fetch_day_sales", fetch)


# ── sales: every comparison needs its own baseline ──────────────────────────

def test_every_sales_comparison_is_none_without_a_baseline(db, monkeypatch):
    rid = _rid(db)
    _pos(monkeypatch)
    b = block_sales.collect(_ctx(db, rid))
    assert b["status"] == dsr.READY and b["source"] == "rpower"
    m = b["metrics"]
    assert (m["net"], m["gross"], m["transactions"], m["avg_ticket"]) == (2000.0, 2100.0, 80, 25.0)
    for key in ("yesterday", "last_week", "last_year", "forecast"):
        assert m[f"{key}_net"] is None and m[f"vs_{key}"] is None and m[f"vs_{key}_pct"] is None, key
    for key in ("budget_gross", "vs_budget_gross", "vs_budget_gross_pct",
                "budget_net", "vs_budget_net", "vs_budget_net_pct"):
        assert m[key] is None, key
    # Unmeasured is absent from the searchable history, never a zero row.
    r = store.create_report(rid, DAY, db_path=db)
    store.save_block(r["id"], "sales", b, db_path=db)
    assert store.metric_series(rid, "sales.vs_yesterday_pct", DAY, DAY, db_path=db) == []


def test_comparisons_use_the_dsrs_own_history_then_the_pos_sync(db, monkeypatch):
    rid = _rid(db)
    _pos(monkeypatch)
    yesterday = store.create_report(rid, date(2026, 9, 21), db_path=db)
    store.save_block(yesterday["id"], "sales", dsr.block(dsr.READY, metrics={"net": 1600}), db_path=db)
    # Last year (364 days back, the same weekday) predates the DSR: the
    # nightly POS sync's archive answers it.
    conn = models.get_conn(db)
    conn.execute("INSERT INTO labor_daily_history (restaurant_id, date, day_of_week, sales) VALUES (?,?,?,?)",
                 (rid, "2025-09-23", "Tuesday", 2500.0))
    conn.commit()
    conn.close()
    store.set_budget(rid, DAY, gross=2000, net=1900, db_path=db)
    m = block_sales.collect(_ctx(db, rid))["metrics"]
    assert (m["yesterday_net"], m["vs_yesterday"], m["vs_yesterday_pct"]) == (1600.0, 400.0, 25.0)
    assert (m["last_year_net"], m["vs_last_year_pct"]) == (2500.0, -20.0)
    assert m["last_week_net"] is None and m["vs_last_week_pct"] is None
    assert (m["vs_budget_gross"], m["vs_budget_net_pct"]) == (100.0, 5.3)


def test_a_zero_baseline_is_a_real_night_with_no_percentage(db, monkeypatch):
    rid = _rid(db)
    _pos(monkeypatch)
    closed_night = store.create_report(rid, date(2026, 9, 15), db_path=db)
    store.save_block(closed_night["id"], "sales", dsr.block(dsr.READY, metrics={"net": 0}), db_path=db)
    m = block_sales.collect(_ctx(db, rid))["metrics"]
    assert m["last_week_net"] == 0.0 and m["vs_last_week"] == 2000.0 and m["vs_last_week_pct"] is None


def test_departments_map_to_categories_or_are_listed_as_unmapped(db, monkeypatch):
    rid = _rid(db)
    store.set_category(rid, "Draft Beer", "Beer", db_path=db)
    store.set_category(rid, "Bottled Beer", "Beer", db_path=db)
    store.set_category(rid, "Kitchen", "Food", db_path=db)
    _pos(monkeypatch, _day(net=1000.0, by_department={"Kitchen": 600.0, "Draft Beer": 200.0,
                                                      "Bottled Beer": 100.0, "Merch": 100.0}))
    b = block_sales.collect(_ctx(db, rid))
    cats = {c["category"]: c for c in b["detail"]["categories"]}
    assert [c["category"] for c in b["detail"]["categories"]] == ["Food", "Beer"]   # Erik's order
    assert cats["Beer"]["net"] == 300.0 and sorted(cats["Beer"]["departments"]) == ["Bottled Beer", "Draft Beer"]
    assert cats["Food"]["share_pct"] == 60.0
    assert b["detail"]["unmapped"] == [{"department": "Merch", "net": 100.0}]
    assert b["metrics"]["cat:Beer"] == 300.0 and b["metrics"]["cat:Unmapped"] == 100.0
    assert b["detail"]["unallocated"] is None


def test_money_on_no_department_is_reported_as_unallocated(db, monkeypatch):
    rid = _rid(db)
    _pos(monkeypatch, _day(net=1000.0, by_department={"Food": 1050.0}))   # a check-level discount
    assert block_sales.collect(_ctx(db, rid))["detail"]["unallocated"] == -50.0


def test_top_and_bottom_items_are_by_net_and_bottom_means_sold(db, monkeypatch):
    rid = _rid(db)
    items = [{"name": f"Dish {i}", "department": "Food", "qty": 1 + i, "net": 10.0 * i} for i in range(1, 13)]
    items.append({"name": "Never sold", "department": "Food", "qty": 0, "net": 0.0})
    _pos(monkeypatch, _day(items=items))
    d = block_sales.collect(_ctx(db, rid))["detail"]
    assert [i["name"] for i in d["top_items"]] == ["Dish 12", "Dish 11", "Dish 10", "Dish 9", "Dish 8"]
    assert [i["name"] for i in d["bottom_items"]] == ["Dish 1", "Dish 2", "Dish 3", "Dish 4", "Dish 5"]


def test_the_hourly_curve_runs_in_service_order(db, monkeypatch):
    rid = _rid(db)
    _pos(monkeypatch, _day(net=1000.0, by_hour={"00": 100.0, "12": 300.0, "19": 400.0, "23": 200.0}))
    b = block_sales.collect(_ctx(db, rid))
    assert [h["hour"] for h in b["detail"]["hourly"]] == ["12", "19", "23", "00"]
    assert b["metrics"]["evening_share_pct"] == 70.0


@pytest.mark.parametrize("setup, status, reason", [
    ({"provider": None}, dsr.NOT_CONNECTED, block_sales.REASON_NO_POS),
    ({"error": pos.POSCapabilityError("square does not report a day")}, dsr.UNAVAILABLE, block_sales.REASON_CANT_REPORT),
    ({"error": pos.POSAuthError("401")}, dsr.UNAVAILABLE, block_sales.REASON_AUTH),
    ({"error": TimeoutError("read timed out")}, dsr.AWAITING, block_sales.REASON_PULL_FAILED),
])
def test_sales_says_why_it_is_not_ready(db, monkeypatch, setup, status, reason):
    rid = _rid(db)
    _pos(monkeypatch, **setup)
    b = block_sales.collect(_ctx(db, rid))
    assert (b["status"], b["reason"]) == (status, reason)
    assert b["metrics"] == {}


def test_sales_waits_for_the_pos_close_and_for_its_tickets(db, monkeypatch):
    rid = _rid(db)
    _pos(monkeypatch)
    b = block_sales.collect(_ctx(db, rid, closed=False))
    assert b["status"] == dsr.AWAITING and "9/22/26" in b["reason"]
    empty = _day(net=0.0, by_department={}, by_hour={})
    empty.update({"gross": 0.0, "transactions": 0})
    _pos(monkeypatch, empty)
    assert block_sales.collect(_ctx(db, rid))["status"] == dsr.AWAITING


# ── labor: from what the POS sync archived ──────────────────────────────────

def _history(db, rid, cost=400.0, hours=30.0, pct=25.0, sales=1600.0, day=DAY):
    conn = models.get_conn(db)
    conn.execute("INSERT INTO labor_daily_history (restaurant_id, date, day_of_week, labor_pct, labor_cost, "
                 "sales, total_hours) VALUES (?,?,?,?,?,?,?)", (rid, day.isoformat(), "Tuesday", pct, cost, sales, hours))
    conn.commit()
    conn.close()


def _shifts(db, rid, rows):
    head = "date,day,employee,role,shift_start,shift_end,scheduled_hours,actual_hours,sales,notes\n"
    body = "".join(f"{r['date']},Tuesday,{r['employee']},{r.get('role', 'Server')},{r['start']},{r['end']},"
                   f"{r['hours']},{r['hours']},,{r.get('notes', '')}\n" for r in rows)
    models.save_client_data(rid, "shifts", head + body, source="test", db_path=db)


def test_labor_not_connected_awaiting_or_unavailable(db, monkeypatch):
    rid = _rid(db)
    monkeypatch.setattr(pos, "connected_provider", lambda r: (None, None))
    assert block_labor.collect(_ctx(db, rid))["status"] == dsr.NOT_CONNECTED
    # Connected but not synced since the night closed: the sync is coming.
    monkeypatch.setattr(pos, "connected_provider", lambda r: ("rpower", object()))
    b = block_labor.collect(_ctx(db, rid))
    assert (b["status"], b["reason"]) == (dsr.AWAITING, block_labor.REASON_SYNC_PENDING)
    # Synced after close (11pm CDT 9/22 = 04:00 UTC 9/23) and still nothing.
    update_restaurant(rid, {"rpower_last_synced": "2026-09-23T08:05:00"}, db_path=db)
    b = block_labor.collect(_ctx(db, rid))
    assert (b["status"], b["reason"]) == (dsr.UNAVAILABLE, block_labor.REASON_NOTHING)


def test_labor_pct_is_over_tonights_net_and_measured_against_the_one_target(db, monkeypatch):
    # The owner's own wage rate (D1-5: the dollars are costed at a rate
    # somebody entered, and say which).
    rid = _rid(db, labor_target_pct=28, labor_target_source="set", hourly_rate=18.0, hourly_rate_source="set")
    monkeypatch.setattr(pos, "connected_provider", lambda r: ("rpower", object()))
    _history(db, rid)
    ctx = _ctx(db, rid)
    ctx.blocks["sales"] = dsr.block(dsr.READY, metrics={"net": 2000.0, "evening_share_pct": 60.0})
    b = block_labor.collect(ctx)
    assert b["status"] == dsr.READY
    m = b["metrics"]
    assert (m["cost"], m["hours"], m["pct"], m["target_pct"], m["vs_target_pts"]) == (400.0, 30.0, 20.0, 28.0, -8.0)
    assert b["detail"]["pct_basis"] == "dsr_net" and b["detail"]["cost_basis"] == "owner_blended"
    # D1-19: the target is named for what it is.
    assert b["detail"]["observations"][0]["text"] == "Labor was 20.0% of sales, at or under your target of 28%."
    assert b["detail"]["target_source"] == "set"
    # Without tonight's sales, the archive's own percentage, labelled.
    b2 = block_labor.collect(_ctx(db, rid))
    assert b2["metrics"]["pct"] == 25.0 and b2["detail"]["pct_basis"] == "labor_history"


def test_labor_at_cavnars_assumed_rate_withholds_the_dollars_and_keeps_the_hours(db, monkeypatch):
    # D1-5: nobody entered a wage, so labor_daily_history's cost is hours ×
    # Cavnar's $26 — not payroll. It is never shown as the night's labor $.
    rid = _rid(db, labor_target_pct=28, labor_target_source="seeded")
    monkeypatch.setattr(pos, "connected_provider", lambda r: ("rpower", object()))
    _history(db, rid)
    ctx = _ctx(db, rid)
    ctx.blocks["sales"] = dsr.block(dsr.READY, metrics={"net": 2000.0})
    b = block_labor.collect(ctx)
    m = b["metrics"]
    assert b["status"] == dsr.READY and m["hours"] == 30.0
    assert (m["cost"], m["pct"], m["vs_target_pts"]) == (None, None, None)
    assert b["detail"]["cost_basis"] == "default" and "wage rates" in b["detail"]["cost_note"]
    assert not [o for o in b["detail"]["observations"] if o["key"] == "vs_target"]
    # The starting target is never called "the target" (D1-19).
    update_restaurant(rid, {"hourly_rate": 18.0, "hourly_rate_source": "set"}, db_path=db)
    ctx = _ctx(db, rid)
    ctx.blocks["sales"] = dsr.block(dsr.READY, metrics={"net": 2000.0})
    b = block_labor.collect(ctx)
    assert b["detail"]["observations"][0]["text"] == \
        "Labor was 20.0% of sales, at or under Cavnar AI's starting target of 28%."


def test_a_labor_row_synced_mid_service_is_never_the_nights_labor(db, monkeypatch):
    # D1-2: final=0 — the archive row was written while the day still traded.
    rid = _rid(db, hourly_rate=18.0, hourly_rate_source="set")
    monkeypatch.setattr(pos, "connected_provider", lambda r: ("rpower", object()))
    _history(db, rid, cost=450.0, hours=30.0)
    conn = models.get_conn(db)
    conn.execute("UPDATE labor_daily_history SET final=0 WHERE restaurant_id=?", (rid,))
    conn.commit()
    conn.close()
    ctx = _ctx(db, rid)
    ctx.blocks["sales"] = dsr.block(dsr.READY, metrics={"net": 6000.0})
    b = block_labor.collect(ctx)
    assert b["status"] == dsr.AWAITING and b["reason"] == block_labor.REASON_SYNC_PENDING
    assert b["metrics"] == {}
    # Past the deadline (4am CDT 9/23 = 09:00 UTC) it says only part synced.
    late = dsr.Context(get_restaurant(rid, db_path=db), DAY, db_path=db, now_utc=datetime(2026, 9, 23, 10, 0))
    late.day_closed = "pos"
    b = block_labor.collect(late)
    assert b["status"] == dsr.UNAVAILABLE and b["reason"] == block_labor.REASON_PARTIAL


def test_labor_overtime_and_hours_after_six_against_sales_after_six(db, monkeypatch):
    rid = _rid(db)
    monkeypatch.setattr(pos, "connected_provider", lambda r: ("rpower", object()))
    _history(db, rid, hours=14.0)
    _shifts(db, rid, [
        {"date": "2026-09-22", "employee": "Ana", "start": "16:00", "end": "00:00", "hours": 8, "notes": "OT 1.5h"},
        {"date": "2026-09-22", "employee": "Bo", "start": "11:00", "end": "17:00", "hours": 6},
        {"date": "2026-09-21", "employee": "Ana", "start": "16:00", "end": "22:00", "hours": 6},
    ])
    ctx = _ctx(db, rid)
    ctx.blocks["sales"] = dsr.block(dsr.READY, metrics={"net": 2000.0, "evening_share_pct": 60.0})
    b = block_labor.collect(ctx)
    m = b["metrics"]
    assert m["overtime_hours"] == 1.5 and b["detail"]["overtime_source"] == "rpower"
    assert (m["hours_after_6pm"], m["hours_after_6pm_share_pct"], m["sales_after_6pm_share_pct"]) == (6.0, 42.9, 60.0)
    texts = [o["text"] for o in b["detail"]["observations"]]
    assert "43% of labor hours were after 6pm, against 60% of sales." in texts


def test_labor_overtime_off_rpower_is_the_hours_past_forty_this_day(db, monkeypatch):
    rid = _rid(db, week_start_day=2)          # payroll week Wed 9/16 – Tue 9/22
    monkeypatch.setattr(pos, "connected_provider", lambda r: ("toast", object()))
    _history(db, rid, hours=10.0)
    # 36h earlier in the week, 10h on Tuesday → the last 6 are overtime.
    # The week before (9/15) does not count toward this one.
    _shifts(db, rid, [
        {"date": "2026-09-15", "employee": "Ana", "start": "08:00", "end": "20:00", "hours": 12},
        {"date": "2026-09-17", "employee": "Ana", "start": "08:00", "end": "20:00", "hours": 12},
        {"date": "2026-09-18", "employee": "Ana", "start": "08:00", "end": "20:00", "hours": 12},
        {"date": "2026-09-19", "employee": "Ana", "start": "08:00", "end": "20:00", "hours": 12},
        {"date": "2026-09-22", "employee": "Ana", "start": "11:00", "end": "21:00", "hours": 10},
        {"date": "2026-09-22", "employee": "Bo", "start": "11:00", "end": "15:00", "hours": 4},
    ])
    b = block_labor.collect(_ctx(db, rid))
    assert b["metrics"]["overtime_hours"] == 6.0 and b["detail"]["overtime_source"] == "cavnar"


def test_labor_coverage_is_counted_only_where_it_was_measured(db, monkeypatch):
    rid = _rid(db)
    monkeypatch.setattr(pos, "connected_provider", lambda r: ("rpower", object()))
    _history(db, rid)
    b = block_labor.collect(_ctx(db, rid))
    # RPOWER has no live clock-in feed: nothing was watched, so no count.
    assert b["metrics"]["no_shows"] is None and b["detail"]["coverage"]["measured"] is False
    conn = models.get_conn(db)
    for who, note, status in (("ana", "Closed automatically: they clocked in.", "resolved"), ("bo", None, "open")):
        conn.execute("INSERT INTO ops_issues (restaurant_id, kind, source_key, title, status, resolution_note, meta_json) "
                     "VALUES (?,?,?,?,?,?,?)", (rid, "coverage", f"coverage:2026-09-22:{who}", f"{who} hasn't clocked in",
                                                status, note, json.dumps({"missing": who.title(), "role": "Server"})))
    conn.commit()
    conn.close()
    b = block_labor.collect(_ctx(db, rid))
    assert (b["metrics"]["no_shows"], b["metrics"]["late_arrivals"]) == (1, 1)
    assert b["detail"]["coverage"]["no_shows"][0]["employee"] == "Bo"


def test_coverage_reads_the_business_date_the_punches_and_whether_the_check_ran(db, monkeypatch):
    # D1-17.
    import intraday
    import issues
    rid = _rid(db, module_labor=1)
    monkeypatch.setattr(pos, "connected_provider", lambda r: ("toast", object()))
    monkeypatch.setattr(pos, "supports", lambda r, cap: True)
    monkeypatch.setattr(issues, "get_routing", lambda r, db_path=None: {"manager": [1]})
    monkeypatch.setattr(intraday, "published_rows", lambda r, d, db_path=None: [{"employee": "Ana"}])
    _history(db, rid)
    # Every precondition holds but the check never ran for the night: no "0".
    b = block_labor.collect(_ctx(db, rid))
    assert b["metrics"]["no_shows"] is None
    assert b["detail"]["coverage"]["reason"] == "The clock-in check didn't run during this shift"
    store.mark_coverage_ran(rid, DAY, db_path=db)
    b = block_labor.collect(_ctx(db, rid))
    assert (b["metrics"]["no_shows"], b["metrics"]["late_arrivals"]) == (0, 0)
    conn = models.get_conn(db)
    rows = (("coverage:2026-09-23:cy", "Cy", "00:30", None),        # after midnight: still 9/22's service
            ("coverage:2026-09-22:dee", "Dee", "00:15", None),      # after midnight of 9/21's service
            ("coverage:2026-09-22:eve", "Eve", "17:00", "Resolved by a manager"))
    for key, who, start, note in rows:
        conn.execute("INSERT INTO ops_issues (restaurant_id, kind, source_key, title, status, resolution_note, meta_json) "
                     "VALUES (?,?,?,?,?,?,?)", (rid, "coverage", key, f"{who} hasn't clocked in", "resolved", note,
                                                json.dumps({"missing": who, "shift_start": start})))
    conn.commit()
    conn.close()
    # Eve worked that day after all (the synced punches say so): late, not a no-show.
    _shifts(db, rid, [{"date": "2026-09-22", "employee": "Eve", "start": "17:40", "end": "23:00", "hours": 5}])
    b = block_labor.collect(_ctx(db, rid))
    cov = b["detail"]["coverage"]
    assert [x["employee"] for x in cov["no_shows"]] == ["Cy"]
    assert [x["employee"] for x in cov["late"]] == ["Eve"]


def test_labor_reads_the_published_days_shift_quality(db, monkeypatch):
    rid = _rid(db)
    monkeypatch.setattr(pos, "connected_provider", lambda r: ("rpower", object()))
    _history(db, rid)
    q = {"score": 80, "shifts": [
        {"date": "2026-09-22", "daypart": "lunch", "scored": True, "score": 85, "band": "Strong", "headline": "x"},
        {"date": "2026-09-22", "daypart": "dinner", "scored": True, "score": 71, "band": "Fair", "headline": "y"},
        {"date": "2026-09-23", "daypart": "dinner", "scored": True, "score": 40, "band": "Weak", "headline": "z"}]}
    conn = models.get_conn(db)
    conn.execute("INSERT INTO schedule_history (restaurant_id, week_start, week_end, schedule_csv, quality_json, "
                 "published_at) VALUES (?,?,?,?,?,datetime('now'))", (rid, "2026-09-21", "2026-09-27", "", json.dumps(q)))
    conn.commit()
    conn.close()
    b = block_labor.collect(_ctx(db, rid))
    assert b["metrics"]["shift_quality"] == 78
    assert [s["daypart"] for s in b["detail"]["shift_quality"]["shifts"]] == ["lunch", "dinner"]
