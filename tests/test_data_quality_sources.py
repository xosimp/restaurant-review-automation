"""Data quality and freshness at the source — group G of the AI confidence
calibration audit (conf-audit CA3 F1-F15). Each test replays one of the
audit's probes (scratchpad/audit3/p2-p9, the F10 staleness case) or pins one
source fix, and failed before the fix it names."""
import json
import sys
from datetime import date, datetime, timedelta, timezone

import pytest

# Imported before any patch of models.get_conn (tests/test_alert_holds.py).
import admin_ops
import auth
import client_api
import mobile_api
import models
import notify
import ops
import push
import webhooks
from auth import create_user, init_auth
from models import Restaurant, create_restaurant, get_conn, update_restaurant

SHIFT_HEADER = "date,day,employee,role,shift_start,shift_end,scheduled_hours,actual_hours,sales,notes"


@pytest.fixture(autouse=True)
def _world(monkeypatch, db_path):
    """Every module's get_conn — bound copies included — points at this
    test's database, as tests/test_edge_mod_b_notify.py does."""
    real = models.get_conn
    redirect = lambda *a, **k: real(db_path)
    for mod in list(sys.modules.values()):
        try:
            if getattr(mod, "get_conn", None) is real:
                monkeypatch.setattr(mod, "get_conn", redirect)
        except Exception:
            pass
    for mod in (models, notify, auth, push, client_api, mobile_api, webhooks, ops, admin_ops):
        monkeypatch.setattr(mod, "get_conn", redirect, raising=False)
        monkeypatch.setattr(mod, "DB_PATH", db_path, raising=False)
    init_auth(db_path=db_path)
    push.init_push(db_path=db_path)
    webhooks.init_webhooks(db_path)
    ops.init_ops(db_path)


def _rid(db_path, **kw):
    kw.setdefault("name", "Source Co")
    kw.setdefault("owner_email", "o@x.test")
    kw.setdefault("timezone", "America/Chicago")
    return create_restaurant(Restaurant(**kw), db_path=db_path)


def _shifts_csv(days_back_first, days_back_last, sales_for):
    """One 8-hour server shift x5 per day; `sales_for(back)` → the day's sales
    ("" for none)."""
    rows = [SHIFT_HEADER]
    t = date.today()
    for back in range(days_back_first, days_back_last - 1, -1):
        d = t - timedelta(days=back)
        for e in range(5):
            rows.append(f"{d.isoformat()},{d.strftime('%A')},E{e},Server,11:00,19:00,8,8,{sales_for(back)},")
    return "\n".join(rows) + "\n"


def _q(db_path, sql, args=()):
    c = get_conn(db_path)
    try:
        return [dict(r) for r in c.execute(sql, args).fetchall()]
    finally:
        c.close()


# ── G1 / CA3 F3: a missing sales figure is NULL, never $0 and 0.0% ──────────

def test_p6_missing_sales_are_archived_as_null_not_zero(db_path):
    import labor
    rid = _rid(db_path, module_labor=1, hourly_rate=15)
    models.save_client_data(rid, "shifts", _shifts_csv(30, 1, lambda b: "3000" if b > 20 else ""),
                            source="toast", db_path=db_path)
    models.save_labor_daily_history(rid, labor.full_history_by_day(rid), db_path=db_path)
    zero = _q(db_path, "SELECT COUNT(*) AS n FROM labor_daily_history WHERE restaurant_id=? AND sales=0", (rid,))
    null = _q(db_path, "SELECT COUNT(*) AS n FROM labor_daily_history WHERE restaurant_id=? "
                       "AND sales IS NULL AND labor_pct IS NULL AND labor_cost > 0", (rid,))
    assert zero[0]["n"] == 0
    assert null[0]["n"] == 20
    # The iOS labor chart never plots a 0% day it never measured.
    assert all(r["labor_pct"] for r in models.get_labor_daily(rid, 60, db_path=db_path))


def test_p6_the_demand_forecast_is_blind_when_sales_stopped_arriving(db_path):
    """G13: `sales IS NOT NULL` alone read 20 days of $0 as current."""
    import schedule_engine
    rid = _rid(db_path)
    c = get_conn(db_path)
    for back in range(1, 31):
        d = (date.today() - timedelta(days=back)).isoformat()
        # Rows from before the fix: 0, not NULL.
        c.execute("INSERT INTO labor_daily_history (restaurant_id, date, labor_pct, labor_cost, sales, total_hours) "
                  "VALUES (?,?,?,?,?,?)", (rid, d, 0.0 if back <= 20 else 20.0, 600, 0.0 if back <= 20 else 3000, 40))
    c.commit(); c.close()
    out = schedule_engine._demand_data_through(rid)
    assert out["days_ago"] == 21 and out["blind"] is True


def test_a_later_sync_without_sales_never_erases_a_known_days_sales(db_path):
    rid = _rid(db_path)
    d = (date.today() - timedelta(days=3)).isoformat()
    models.save_labor_daily_history(rid, {d: {"sales": 4000.0, "actual": 40, "labor_cost": 800, "labor_pct": 20.0}},
                                    db_path=db_path)
    models.save_labor_daily_history(rid, {d: {"sales": None, "actual": 44, "labor_cost": 880, "labor_pct": None}},
                                    db_path=db_path)
    row = _q(db_path, "SELECT sales, labor_pct, labor_cost FROM labor_daily_history WHERE restaurant_id=?", (rid,))[0]
    assert row["sales"] == 4000.0 and row["labor_cost"] == 880 and row["labor_pct"] == 22.0


def test_p8_a_period_with_no_sales_is_never_a_labor_snapshot(db_path):
    """A sync with shifts and no sales wrote (…, 0.0%, $0), later fed to the
    labor note as a "previous upload" at 0.0% labor."""
    import pos
    rid = _rid(db_path, module_labor=1, labor_target_pct=25.0, hourly_rate=15)
    pos.save_synced_shifts(rid, _shifts_csv(21, 8, lambda b: ""), "toast")
    assert _q(db_path, "SELECT * FROM labor_history WHERE restaurant_id=?", (rid,)) == []
    pos.save_synced_shifts(rid, _shifts_csv(14, 1, lambda b: "2400"), "toast")
    hist = models.get_labor_history(rid, limit=5, db_path=db_path)
    assert hist and all((h["total_sales"] or 0) > 0 for h in hist)


def test_legacy_zero_sales_snapshots_are_never_read_back(db_path):
    rid = _rid(db_path)
    c = get_conn(db_path)
    c.execute("INSERT INTO labor_history (restaurant_id, period_start, period_end, labor_pct, total_labor, total_sales) "
              "VALUES (?,?,?,?,?,?)", (rid, "2026-09-01", "2026-09-14", 0.0, 5000, 0.0))
    c.commit(); c.close()
    assert models.get_labor_history(rid, db_path=db_path) == []


def test_the_boot_backfill_rewrites_missing_sales_zeros_once_and_leaves_pos_evidence_alone(db_path):
    rid = _rid(db_path)
    c = get_conn(db_path)
    rows = [("2026-09-01", 0.0, 0.0, 600), ("2026-09-02", 0.0, 0.0, 600), ("2026-09-03", 3000, 20.0, 600),
            ("2026-09-04", 0.0, 0.0, 0)]
    for d, s, p, cost in rows:
        c.execute("INSERT INTO labor_daily_history (restaurant_id, date, labor_pct, labor_cost, sales) VALUES (?,?,?,?,?)",
                  (rid, d, p, cost, s))
    # POS evidence of sales on 9/2: that $0 is not "missing", it is wrong —
    # left for a person, not guessed at.
    c.execute("INSERT INTO dsr_metrics (restaurant_id, business_date, metric, value, status) VALUES (?,?,?,?,?)",
              (rid, "2026-09-02", "sales.net", 2500.0, "final"))
    c.commit(); c.close()
    assert models.backfill_missing_sales_null(db_path=db_path) == 1
    got = {r["date"]: r for r in _q(db_path, "SELECT date, sales, labor_pct FROM labor_daily_history "
                                             "WHERE restaurant_id=? ORDER BY date", (rid,))}
    assert got["2026-09-01"]["sales"] is None and got["2026-09-01"]["labor_pct"] is None
    assert got["2026-09-02"]["sales"] == 0.0            # POS evidence: untouched
    assert got["2026-09-03"]["sales"] == 3000
    assert got["2026-09-04"]["sales"] == 0.0            # no labor on the books: not a missing figure
    assert models.backfill_missing_sales_null(db_path=db_path) == 0     # idempotent


def test_the_backfill_runs_at_boot():
    import inspect
    src = inspect.getsource(models.init_db)
    assert "backfill_missing_sales_null(db_path=db_path)" in src


# ── G2 / CA3 F12: labor_pct_28d is sales-weighted; reviews_30d None w/o source ─

def test_p1_labor_pct_28d_is_sales_weighted_and_skips_missing_days(db_path):
    from intelligence import features
    rid = _rid(db_path)
    c = get_conn(db_path)
    today = date.today()
    # Two slow days at 60% on $500, two big days at 20% on $5,000, and two
    # days whose sales never arrived (legacy 0 and new NULL).
    for i, (s, p) in enumerate([(500, 60.0), (500, 60.0), (5000, 20.0), (5000, 20.0), (0.0, 0.0), (None, None)]):
        c.execute("INSERT INTO labor_daily_history (restaurant_id, date, labor_pct, labor_cost, sales, total_hours) "
                  "VALUES (?,?,?,?,?,?)", (rid, (today - timedelta(days=i + 1)).isoformat(), p, 300, s, 20))
    c.commit(); c.close()
    f = features.compute(rid, today=today, db_path=db_path)
    assert f["labor_pct_28d"] == round((60 * 500 * 2 + 20 * 5000 * 2) / 11000, 2)     # 23.64, not 40 or 26.67


def test_reviews_30d_is_unmeasured_without_a_review_source(db_path):
    from intelligence import features
    rid = _rid(db_path)
    assert features.compute(rid, db_path=db_path)["reviews_30d"] is None
    live = _rid(db_path, name="Live", reviews_live=1, google_place_id="ChIJlive")
    assert features.compute(live, db_path=db_path)["reviews_30d"] == 0


# ── G3 / CA3 F6: every provider's sync state, RPOWER included ───────────────

def _rpower(db_path, rid, days_ago, error=None):
    stamp = (datetime.now(timezone.utc) - timedelta(days=days_ago)).replace(tzinfo=None).isoformat(timespec="seconds")
    update_restaurant(rid, {"rpower_token": "t", "rpower_store_mid": "m", "rpower_last_synced": stamp,
                            "rpower_sync_error": error}, db_path=db_path)


def test_admin_integrations_list_rpower_and_call_a_stopped_sync_an_error(db_path):
    rid = _rid(db_path, name="Simple EJ's")
    _rpower(db_path, rid, days_ago=5)
    rec = admin_ops.client_detail(rid)["client"]
    ints = {i["key"]: i for i in rec["integrations"]}
    assert "rpower" in ints and ints["rpower"]["configured"]
    assert ints["rpower"]["sync_state"] == "stale" and ints["rpower"]["error"]
    assert rec["freshness"]["pos_state"]["provider"] == "rpower"
    assert any(i["key"].endswith("int:rpower") and i["severity"] == "critical" for i in rec["issues"])


def test_the_status_page_sees_an_rpower_sync_error(db_path, monkeypatch):
    import status_manager as sm
    monkeypatch.setattr(sm, "_conn", lambda *a, **k: models.get_conn(db_path), raising=False)
    rid = _rid(db_path)
    create_user(rid, "u1", "u1@x.test", "Passw0rd!xyz", db_path=db_path)
    _rpower(db_path, rid, days_ago=0.2, error="401 Unauthorized")
    sm.seed_default_services()
    sm._check_labor_analytics()
    row = _q(db_path, "SELECT status, message FROM service_status WHERE service_key='labor_analytics'")
    assert row and row[0]["status"] == "degraded"


def test_a_pos_failing_two_days_is_an_account_event_once_per_run(db_path):
    import pos
    rid = _rid(db_path)
    _rpower(db_path, rid, days_ago=3, error="RPOWER returned 500")
    assert pos.note_sync_failure(rid) is True
    assert pos.note_sync_failure(rid) is False                  # once per failure run
    acts = models.get_account_activity(rid, db_path=db_path)
    assert [a["type"] for a in acts] == ["pos_sync_failing"] and "RPOWER" in acts[0]["detail"]
    fresh = _rid(db_path, name="Just failed")
    _rpower(db_path, fresh, days_ago=0.5, error="timeout")
    assert pos.note_sync_failure(fresh) is False                 # not two days yet


# ── G4 / CA3 F7: demo restaurants never feed cross-restaurant learning ──────

def test_p7_demo_restaurants_are_out_of_benchmarks_and_the_privacy_floor(db_path):
    from intelligence import jobs, benchmarks, features
    ids = []
    for i in range(5):
        demo = 1 if i >= 3 else 0
        rid = _rid(db_path, name=f"R{i}", billing_status="trial", is_demo=demo)
        ids.append((rid, demo))
        c = get_conn(db_path)
        for b in range(1, 15):
            c.execute("INSERT INTO labor_daily_history (restaurant_id, date, labor_pct, sales, total_hours) "
                      "VALUES (?,?,?,?,?)", (rid, (date.today() - timedelta(days=b)).isoformat(), 20 + i * 3, 3000, 50))
        c.commit(); c.close()
    real = {r for r, d in ids if not d}
    assert {r.id for r in jobs.active_restaurants(db_path)} == real
    jobs.run_features(db_path=db_path)
    assert set(features.latest_by_restaurant(db_path=db_path)) == real
    assert features.latest(ids[4][0], db_path=db_path) is not None     # its own screens keep their row
    benchmarks.compute(db_path=db_path)
    row = _q(db_path, "SELECT n FROM intel_benchmarks WHERE metric='labor_pct_28d'")
    assert all(r["n"] <= 3 for r in row)


def test_cohort_success_rates_leave_demo_answers_out(db_path):
    from intelligence import scoring
    real = _rid(db_path, name="Real")
    demo = _rid(db_path, name="Demo", is_demo=1)
    c = get_conn(db_path)
    for rid in (real, demo):
        c.execute("INSERT INTO intel_rec_events (restaurant_id, rec_kind, source_key, action, outcome, cohort, event_at) "
                  "VALUES (?,?,?,?,?,?,datetime('now'))", (rid, "trim_day", f"k{rid}", "measured", "improved", "bar"))
    c.commit(); c.close()
    stats = scoring.kind_stats("trim_day", db_path=db_path)
    assert stats["restaurants"] == 1 and stats["measured"] == 1
    assert scoring.kind_stats("trim_day", cohort="bar", db_path=db_path)["restaurants"] == 1
    assert scoring.kind_stats("trim_day", restaurant_id=demo, db_path=db_path)["measured"] == 1   # its own


def test_turning_demo_off_tags_the_restaurant_and_keeps_it_out(db_path, monkeypatch):
    from flask import Flask
    import admin_routes
    from intelligence import jobs
    rid = _rid(db_path, name="Was demo", is_demo=1)
    app = Flask(__name__)
    app.secret_key = "x"
    fn = admin_routes.admin_api_set_demo.__wrapped__ if hasattr(admin_routes.admin_api_set_demo, "__wrapped__") \
        else admin_routes.admin_api_set_demo
    with app.test_request_context(json={"is_demo": 0}):
        fn(rid, current_user={"username": "will"})
    r = models.get_restaurant(rid, db_path=db_path)
    assert r.is_demo == 0 and r.demo_cleared_at
    assert rid not in jobs.real_restaurant_ids(db_path) and rid in jobs.seeded_restaurant_ids(db_path)
    c = get_conn(db_path)
    c.execute("UPDATE restaurants SET demo_cleared_at=datetime('now','-100 days') WHERE id=?", (rid,))
    c.commit(); c.close()
    assert rid in jobs.real_restaurant_ids(db_path)


# ── G5 / CA3 F8: "demo" Toast credentials only on a demo restaurant ─────────

def test_p2_demo_toast_credentials_never_load_synthetic_data_into_a_real_restaurant(db_path, monkeypatch):
    import toast
    rid = _rid(db_path, name="Real Client", billing_status="active", is_demo=0)
    update_restaurant(rid, {"toast_client_id": "demo", "toast_client_secret": "demo",
                            "toast_restaurant_guid": "demo"}, db_path=db_path)
    assert toast._is_demo(rid) is False and toast.demo_allowed(rid) is False

    def _no_network(*a, **k):
        raise RuntimeError("network blocked")
    monkeypatch.setattr(toast.requests, "get", _no_network)
    monkeypatch.setattr(toast.requests, "post", _no_network)
    result = toast.sync_to_db(rid)
    assert result["ok"] is False
    assert _q(db_path, "SELECT COUNT(*) AS n FROM labor_daily_history WHERE restaurant_id=?", (rid,))[0]["n"] == 0
    demo = _rid(db_path, name="Demo", is_demo=1)
    update_restaurant(demo, {"toast_client_id": "demo"}, db_path=db_path)
    assert toast._is_demo(demo) is True


def test_the_connect_routes_refuse_demo_credentials_for_a_real_restaurant(db_path, monkeypatch):
    from flask import Flask
    from mobile_api import mobile_bp
    import auth_routes
    monkeypatch.setattr(auth_routes, "get_conn", lambda *a, **k: models.get_conn(db_path), raising=False)
    auth_routes._login_attempts.clear()
    app = Flask(__name__)
    app.register_blueprint(mobile_bp)
    client = app.test_client()
    rid = _rid(db_path)
    create_user(rid, "alice", "alice@x.com", "correct-horse-9", db_path=db_path)
    tok = client.post("/mobile/api/login", json={"username": "alice", "password": "correct-horse-9"}).get_json()["token"]
    resp = client.post("/mobile/api/connections/toast", headers={"Authorization": f"Bearer {tok}"},
                       json={"toast_client_id": "demo", "toast_client_secret": "demo", "toast_restaurant_guid": "demo"})
    assert resp.get_json()["ok"] is False
    assert models.get_restaurant(rid, db_path=db_path).toast_client_id is None
    import inspect, toast_routes
    assert inspect.getsource(toast_routes).count("demo_allowed(") == 2     # admin and owner web routes


# ── G6 / CA3 F9: inventory freshness from the last count ────────────────────

def test_p9_admin_reads_inventory_age_from_the_last_count(db_path):
    rid = _rid(db_path, module_inventory=1, module_labor=0, module_reviews=0, module_marketing=0)
    c = get_conn(db_path)
    c.execute("INSERT INTO ingredients (restaurant_id, name, unit, par_level, current_stock, unit_cost, is_active, "
              "last_recount_at) VALUES (?,?,?,?,?,?,1,date('now','-40 days'))", (rid, "Salmon", "lb", 10, 5, 12))
    c.commit(); c.close()
    rec = admin_ops.client_detail(rid)["client"]
    inv = next(m for m in rec["modules"] if m["key"] == "inventory")
    assert inv["state"] == "stale"
    assert str(rec["freshness"]["inventory"])[:10] == (date.today() - timedelta(days=40)).isoformat()


# ── G7 / CA3 F10: the status page's review staleness is on the slot clock ───

def test_f10_a_chicago_stamp_on_the_cutoffs_date_is_still_stale(db_path, monkeypatch):
    """last_fetched_at is Chicago local with a 'T'; compared as a string
    against a UTC cutoff with a space it never read stale on the cutoff's
    date — 30 and 40 hours old read fresh."""
    import status_manager as sm
    from zoneinfo import ZoneInfo
    monkeypatch.setattr(sm, "_conn", lambda *a, **k: models.get_conn(db_path), raising=False)
    # 23:00Z: the 25-hour cutoff falls on yesterday's date, the same date as
    # a fetch 30 hours ago — the case the string compare got wrong.
    now = datetime(2026, 9, 24, 23, 0, tzinfo=timezone.utc)

    class _Frozen(datetime):
        @classmethod
        def utcnow(cls):
            return now.replace(tzinfo=None)

        @classmethod
        def now(cls, tz=None):
            return now.astimezone(tz) if tz else now.astimezone().replace(tzinfo=None)
    monkeypatch.setattr(sm, "datetime", _Frozen)
    monkeypatch.setattr(admin_ops, "_now_ct", lambda: now.astimezone(ZoneInfo("America/Chicago")))
    rid = _rid(db_path, reviews_live=1, google_place_id="ChIJx")
    create_user(rid, "u1", "u1@x.test", "Passw0rd!xyz", db_path=db_path)
    stamp = (now.astimezone(ZoneInfo("America/Chicago")) - timedelta(hours=30)).strftime("%Y-%m-%dT%H:%M:%S")
    c = get_conn(db_path)
    c.execute("UPDATE restaurants SET last_fetched_at=? WHERE id=?", (stamp, rid))
    c.commit(); c.close()
    sm.seed_default_services()
    sm._check_review_sync()
    assert _q(db_path, "SELECT status FROM service_status WHERE service_key='review_sync'")[0]["status"] != "operational"


# ── G8 / CA3 F11: net sales by business date for every provider ─────────────

class _Resp:
    def __init__(self, status, body):
        self.status_code, self._body = status, body

    def json(self):
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


def test_square_sales_are_net_of_tax_tip_and_service_charge_to_the_cent(db_path, monkeypatch):
    import square
    rid = _rid(db_path)
    update_restaurant(rid, {"square_access_token": "sq", "square_location_id": "L1"}, db_path=db_path)
    order = {"created_at": "2026-09-19T01:00:00Z", "total_money": {"amount": 11855},
             "total_tax_money": {"amount": 855}, "total_tip_money": {"amount": 1000},
             "total_service_charge_money": {"amount": 0}}
    late = {"created_at": "2026-09-19T06:30:00Z", "total_money": {"amount": 1000}}     # 1:30am Chicago
    monkeypatch.setattr(square.requests, "post", lambda url, **k: _Resp(200, {"orders": [order, late]}))
    sales = square._fetch_daily_sales(rid, date(2026, 9, 18), date(2026, 9, 19))
    assert sales == {"2026-09-18": 110.0}       # $100 net + the 1:30am check, last night's service


def test_clover_sales_are_net_local_business_dated_and_shifts_local(db_path, monkeypatch):
    import clover
    rid = _rid(db_path)
    update_restaurant(rid, {"clover_api_token": "tok", "clover_merchant_id": "M"}, db_path=db_path)
    # Mon 9/21 19:30 Chicago = 00:30Z Tue.
    in_ms = int(datetime(2026, 9, 22, 0, 30, tzinfo=timezone.utc).timestamp() * 1000)
    out_ms = in_ms + 6 * 3600 * 1000

    def get(url, **k):
        if url.endswith("/employees"):
            return _Resp(200, {"elements": [{"id": "e1", "name": "Ann", "role": "server"}]})
        if url.endswith("/shifts"):
            return _Resp(200, {"elements": [{"inTime": in_ms, "outTime": out_ms, "employee": {"id": "e1"}}]})
        return _Resp(200, {"elements": [{"createdTime": in_ms, "total": 10800,
                                         "payments": {"elements": [{"taxAmount": 800, "tipAmount": 1500}]}}]})
    monkeypatch.setattr(clover.requests, "get", get)
    csv_str = clover.build_shifts_csv(rid)
    import csv, io
    row = list(csv.DictReader(io.StringIO(csv_str)))[0]
    assert row["date"] == "2026-09-21" and row["day"] == "Monday" and row["shift_start"] == "19:30"
    assert float(row["sales"]) == 100.0


def test_a_clover_page_failure_fails_the_sync_and_saves_nothing(db_path, monkeypatch):
    import clover
    rid = _rid(db_path)
    update_restaurant(rid, {"clover_api_token": "tok", "clover_merchant_id": "M"}, db_path=db_path)
    in_ms = int((datetime.now(timezone.utc) - timedelta(days=3)).timestamp() * 1000)
    pages = iter([_Resp(200, {"elements": [{"id": f"e{i}", "name": "x"} for i in range(3)]}),
                  _Resp(200, {"elements": [{"inTime": in_ms, "outTime": in_ms + 3600000,
                                            "employee": {"id": "e1"}}] * 200}),
                  _Resp(429, {}),                                   # the second shifts page fails
                  _Resp(200, {"elements": []})])                    # orders
    monkeypatch.setattr(clover.requests, "get", lambda url, **k: next(pages))
    assert clover.sync_to_db(rid)["ok"] is False
    assert not (models.get_client_data(rid, db_path=db_path) or {}).get("shifts_csv")


def test_the_pos_sales_contract_is_documented_in_pos():
    import inspect, pos
    src = inspect.getsource(pos)
    for needle in ("NET sales", "BUSINESS DATE", "square._order_net_cents", "clover._order_net_cents"):
        assert needle in src


# ── G9 / CA3 F13: Places is a sample, and its gap is on the account ─────────

def test_places_only_is_labelled_sampled_and_coverage_is_measured(db_path):
    import fetcher
    import scheduler
    rid = _rid(db_path, name="Places Only", reviews_live=1, google_place_id="ChIJp", module_reviews=1)
    rec = admin_ops.client_detail(rid)["client"]
    g = next(i for i in rec["integrations"] if i["key"] == "google_business")
    assert g["source"] == "places_sampled" and "sampled" in g["label"]
    assert fetcher.places_coverage(rid)["share"] is None           # unknown until Google's count moves
    scheduler._record_places_gap(rid, "Places Only", 100, 0)          # first sight: baseline
    scheduler._record_places_gap(rid, "Places Only", 112, 5)          # 12 new on Google, 5 returned
    cov = fetcher.places_coverage(rid)
    assert cov["google_growth"] == 12 and cov["stored"] == 5 and cov["share"] == round(5 / 12, 3)
    acts = models.get_account_activity(rid, db_path=db_path)
    assert acts and acts[0]["type"] == "review_fetch_gap" and "7 reviews missed" in acts[0]["detail"]


# ── G10 / CA3 F14: food cost refuses what it cannot measure ─────────────────

def test_the_live_pos_fallback_applies_the_coverage_rule(db_path, monkeypatch):
    import cogs
    import pos
    rid = _rid(db_path)
    start, end = date.today() - timedelta(days=28), date.today() - timedelta(days=1)
    thin = {(start + timedelta(days=i)).isoformat(): 3000.0 for i in range(0, 28, 3)}      # 10 of 28 days
    monkeypatch.setattr(pos, "fetch_business_days", lambda *a, **k: (thin, "square"))
    total, why = cogs.net_sales_in_window(rid, start.isoformat(), end.isoformat())
    assert total is None and "of" in why and "trading days" in why
    full = {(start + timedelta(days=i)).isoformat(): 3000.0 for i in range(28)}
    monkeypatch.setattr(pos, "fetch_business_days", lambda *a, **k: (full, "square"))
    assert cogs.net_sales_in_window(rid, start.isoformat(), end.isoformat())[0] == 84000.0


def test_food_cost_above_100_percent_is_refused_as_not_measurable(db_path, monkeypatch):
    import cogs
    rid = _rid(db_path)
    monkeypatch.setattr(cogs, "net_sales_in_window", lambda *a, **k: (1000.0, None))
    monkeypatch.setattr(cogs, "purchases_in_window", lambda *a, **k: (5000.0, 3))
    monkeypatch.setattr(cogs, "inventory_value_near",
                        lambda weeks, day: ((4000.0, "2026-08-27") if day < date.today() - timedelta(days=5)
                                            else (2000.0, "2026-09-23")))
    monkeypatch.setattr("waste_trend.load_waste_history", lambda *a, **k: ([], 0))
    out = cogs.build_food_cost_pct(rid)
    assert out["ok"] is False and out["pct"] is None
    assert "not a measurement" in out["missing"][0]["why"]


def test_negative_stock_is_a_count_discrepancy_not_critically_low(db_path):
    import inventory
    import inventory_ledger
    rid = _rid(db_path, module_inventory=1)
    c = get_conn(db_path)
    cur = c.execute("INSERT INTO ingredients (restaurant_id, name, unit, par_level, current_stock, unit_cost, "
                    "avg_daily_usage, is_active, last_recount_at) VALUES (?,?,?,?,?,?,?,1,date('now'))",
                    (rid, "Salmon", "lb", 10, 0, 12, 3))
    iid = cur.lastrowid
    today = date.today().isoformat()
    c.execute("INSERT INTO ingredient_stock_events (restaurant_id, ingredient_id, event_type, qty, event_date, source) "
              "VALUES (?,?,?,?,?,?)", (rid, iid, "recount", 2, today, "manual"))
    c.execute("INSERT INTO ingredient_stock_events (restaurant_id, ingredient_id, event_type, qty, event_date, source) "
              "VALUES (?,?,?,?,?,?)", (rid, iid, "depletion", 9, today, "pos"))
    c.commit()
    inventory_ledger.recompute_rollups(rid, iid, conn=c)
    c.commit()
    row = c.execute("SELECT current_stock, count_discrepancy_qty FROM ingredients WHERE id=?", (iid,)).fetchone()
    c.close()
    assert row["current_stock"] == 0 and row["count_discrepancy_qty"] == -7
    _items, _live, analysis = inventory.analysis_for(rid)
    assert not any(x.get("item") == "Salmon" for x in analysis.get("critical_low") or [])
    assert [d["item"] for d in analysis.get("count_discrepancies") or []] == ["Salmon"]


# ── G11 / CA3 F15: one timestamp parser; weather rows carry their age ───────

@pytest.mark.parametrize("raw,tz,expect", [
    ("2026-09-24T08:00:00", "America/Chicago", "2026-09-24T13:00:00+00:00"),      # last_fetched_at
    ("2026-09-24 13:00:00", "America/Chicago", "2026-09-24T13:00:00+00:00"),      # SQLite: always UTC
    ("2026-09-24T13:00:00Z", "America/Chicago", "2026-09-24T13:00:00+00:00"),
    ("2026-09-24T08:00:00-05:00", "UTC", "2026-09-24T13:00:00+00:00"),
    ("2026-09-24T13:00:00", "UTC", "2026-09-24T13:00:00+00:00"),                  # rpower_last_synced
    ("2026-09-24", "UTC", "2026-09-24T00:00:00+00:00"),
])
def test_parse_stamp_reads_every_stored_format(raw, tz, expect):
    from time_utils import parse_stamp
    assert parse_stamp(raw, naive_tz=tz).isoformat() == expect


def test_parse_stamp_never_calls_garbage_fresh():
    from time_utils import parse_stamp, age_days
    assert parse_stamp("not a date") is None and parse_stamp(None) is None and age_days("") is None


def test_a_three_day_old_forecast_row_says_it_is_stale(db_path, monkeypatch):
    import weather
    periods = [{"startTime": (date.today() + timedelta(days=1)).isoformat() + "T06:00:00-05:00",
                "isDaytime": True, "name": "Tomorrow", "temperature": 70, "shortForecast": "Sunny"}]
    rid = _rid(db_path, latitude=41.9, longitude=-87.6)
    update_restaurant(rid, {"weather_cache_json": json.dumps(periods),
                            "weather_cached_at": (datetime.now(timezone.utc) - timedelta(hours=60)).isoformat()},
                      db_path=db_path)
    monkeypatch.setattr(weather, "_fetch_periods_ex", lambda lat, lon: ([], "transient"))
    monkeypatch.setattr(weather, "_geocode", lambda *a, **k: (41.9, -87.6))
    r = models.get_restaurant(rid, db_path=db_path)
    row = weather.get_forecast_for_week(r, [(date.today() + timedelta(days=1)).isoformat()], db_path=db_path)[0]
    assert row["stale"] is True and row["age_hours"] >= 59


# ── G12 / CA3 F5: no labor alert from a short period; no push off a stale count ─

@pytest.fixture
def sent(monkeypatch):
    out = {"sms": [], "email": [], "push": []}
    monkeypatch.setattr(notify, "send_sms", lambda to, msg, use_case="alert": out["sms"].append((to, msg)) or True)
    monkeypatch.setattr(notify, "_email_alert",
                        lambda rid, to, subject, html, alert_type, db_path=None, **k: out["email"].append((rid, alert_type)))
    monkeypatch.setattr(push, "fire_push",
                        lambda rid, at, title, body, data=None, db_path=None, user_ids=None, on_delivered=None:
                        out["push"].append((rid, at)))
    monkeypatch.setattr(webhooks, "fire_webhook", lambda *a, **k: None)
    monkeypatch.setattr(notify, "rush_release_at", lambda *a, **k: None)
    return out


def _labor_alert_rid(db_path, days):
    rid = _rid(db_path, name=f"Week One {days}")
    update_restaurant(rid, {"urgent_via_sms": 0, "urgent_via_email": 1, "alert_labor_over": 1,
                            "labor_target_pct": 25.0, "al_unres_push": 0, "al_unres_email": 0}, db_path=db_path)
    end = date.today() - timedelta(days=1)
    models.save_labor_snapshot(rid, (end - timedelta(days=days - 1)).isoformat(), end.isoformat(),
                               48.0, 720, 1500, db_path=db_path)
    return rid


def test_p5_one_day_of_shifts_never_texts_labor_over_target(db_path, sent):
    one = _labor_alert_rid(db_path, 1)
    week = _labor_alert_rid(db_path, 7)
    notify.begin_daily_batch()
    notify.check_daily_alerts(db_path=db_path)
    notify.flush_daily_batch(db_path=db_path)
    assert (one, "labor_over") not in sent["push"] and (one, "labor_over") not in sent["email"]
    assert (week, "labor_over") in sent["push"] or (week, "labor_over") in sent["email"]


def test_the_critical_low_push_skips_items_resting_on_a_stale_count(db_path, sent, monkeypatch):
    import inventory
    rid = _rid(db_path, name="Stale Count")
    update_restaurant(rid, {"alert_food_waste": 1, "urgent_via_email": 1}, db_path=db_path)
    crit = [{"item": "Salmon", "days_remaining": 0.5, "count_stale": True},
            {"item": "Chicken", "days_remaining": 0.5, "count_discrepancy": -3.0}]
    monkeypatch.setattr(inventory, "analysis_for", lambda *a, **k: ([{"item": "x"}], True,
                                                                      {"critical_low": crit, "waste_items": []}))
    notify.begin_daily_batch()
    notify.check_extra_daily_alerts(db_path=db_path)
    notify.flush_daily_batch(db_path=db_path)
    assert not any(a == "critical_low" for _r, a in sent["push"] + sent["email"])
    crit.append({"item": "Lemons", "days_remaining": 0.5})
    notify.begin_daily_batch()
    notify.check_extra_daily_alerts(db_path=db_path)
    notify.flush_daily_batch(db_path=db_path)
    assert any(a == "critical_low" for _r, a in sent["push"] + sent["email"])


def test_the_morning_brief_running_low_line_honours_count_stale():
    import inspect, morning_brief
    src = inspect.getsource(morning_brief._critical_low)
    assert "count_stale" in src and "count_discrepancy" in src


# ── G14: "setup completeness", not "data completeness" ──────────────────────

def test_admin_completeness_is_named_setup_completeness(db_path):
    rid = _rid(db_path, module_reviews=1)
    rec = admin_ops.client_detail(rid)["client"]
    assert rec["setup_completeness"]["label"] == "Setup completeness"
    assert rec["data_completeness"] is rec["setup_completeness"]          # alias until admin.html moves
