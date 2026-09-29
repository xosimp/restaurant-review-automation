"""Memory fix round M5, canonical_facts and net_basis (memory audit 9/29/26:
QUALITY-2, -10, -11, -15, -16, -17, -18, -20, CROSS-15).

Each learning reader re-implemented the platform's data rules and several
missed one. canonical_facts holds them once; these tests hold every learner
to them — from the SOURCE where a rule must hold everywhere (the style of
tests/test_readiness_adoption.py: a test on one payload covers only that
fixture's branches), and by behaviour where the rule has a shape.
"""
import ast
import json
import os
from datetime import date, datetime, timedelta

import pytest

import canonical_facts as cf
import models
from models import Restaurant, create_restaurant

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Every learner the audit names (metrics, intelligence/, demand,
# schedule_intel, dsr) plus the other readers that learn from daily history.
LEARNERS = (["metrics.py", "demand.py", "schedule_intel.py", "schedule_economics.py", "forecast_log.py",
             "canonical_facts.py"]
            + sorted(os.path.join("intelligence", f) for f in os.listdir(os.path.join(ROOT, "intelligence"))
                     if f.endswith(".py"))
            + sorted(os.path.join("dsr", f) for f in os.listdir(os.path.join(ROOT, "dsr")) if f.endswith(".py")))

# (module, the SQL's opening words) → why a daily-history read here is not a
# learner's read of a night's figure.
NOT_LEARNING = {
    ("dsr/backfill.py", "SELECT date FROM labor_daily_history"):
        "which past nights TRADED, to backfill a report for them; the report re-reads the POS itself",
    ("intelligence/dna.py", "'SELECT date, sales, total_hours, labor_pct FROM labor_daily_history WHERE restaurant_id=? "
                            "AND date >= ? AND sales > 0 ORDER BY date'"):
        "the fallback for a database without the final column (the try above reads final days); every "
        "init_db adds the column (models.init_db's ALTER list)",
}


@pytest.fixture(autouse=True)
def _db(db_path, monkeypatch):
    real = models.get_conn
    monkeypatch.setattr(models, "get_conn", lambda *a, **k: real(db_path))
    monkeypatch.setattr(models, "DB_PATH", db_path)
    yield


def _sql_calls(path):
    """[(Call node, unparsed first argument)] for every call whose first
    argument is SQL text."""
    tree = ast.parse(open(os.path.join(ROOT, path), encoding="utf-8").read())
    out = []
    for n in ast.walk(tree):
        if isinstance(n, ast.Call) and n.args:
            try:
                text = ast.unparse(n.args[0])
            except Exception:
                continue
            if "SELECT" in text.upper():
                out.append((n, text))
    return out


# ── from the source: every learner reads through the rules ─────────────────

def test_every_learner_reads_final_days_only():
    missing = []
    for mod in LEARNERS:
        for _n, sql in _sql_calls(mod):
            if "labor_daily_history" not in sql:
                continue
            if any(mod == m and head in sql for (m, head) in NOT_LEARNING):
                continue
            if "final" not in sql.lower():
                missing.append((mod, sql[:120]))
    assert not missing, f"daily-history reads without the final-day rule (canonical_facts.FINAL_SQL): {missing}"


def test_the_learners_read_live_reviews_on_the_one_axis():
    for mod in ("intelligence/features.py", "intelligence/dna.py"):
        for _n, sql in _sql_calls(mod):
            if " reviews " not in sql.replace("FROM reviews", " reviews ").replace('"', " "):
                continue
            if "FROM reviews" not in sql:
                continue
            assert "LIVE_REVIEWS_SQL" in sql or "deleted_at IS NULL" in sql, (mod, sql[:120])
            assert "COALESCE(review_date, fetched_at)" not in sql, (mod, sql[:120])


def test_no_writer_counts_reach_plus_impressions():
    for mod in ("intelligence/features.py", "dsr/block_marketing.py", "food_cost_intelligence.py"):
        src = open(os.path.join(ROOT, mod), encoding="utf-8").read().replace(" ", "")
        assert "COALESCE(reach,0)+COALESCE(impressions,0)" not in src, mod
        assert "COALESCE(c.reach,0)+COALESCE(c.impressions,0)" not in src, mod


def test_both_raw_nightly_figures_are_kept_forever():
    import ops
    for table in ("labor_daily_history", "dsr_metrics", "dsr_history_import"):
        assert table not in ops._RETENTION_DAYS, table


# ── behaviour: final days ───────────────────────────────────────────────────

def _rid(name="Canon Co", **cols):
    rid = create_restaurant(Restaurant(name=name, owner_email=f"{name.split()[0].lower()}@x.test"))
    if cols:
        conn = models.get_conn()
        conn.execute(f"UPDATE restaurants SET {', '.join(f'{k}=?' for k in cols)} WHERE id=?", (*cols.values(), rid))
        conn.commit()
        conn.close()
    return rid


def _day(rid, d, sales, final=1, provider=None, labor_cost=None):
    conn = models.get_conn()
    conn.execute("INSERT INTO labor_daily_history (restaurant_id, date, day_of_week, sales, labor_cost, labor_pct, "
                 "final, provider) VALUES (?,?,?,?,?,?,?,?)",
                 (rid, d.isoformat(), d.strftime("%A"), sales, labor_cost,
                  round(labor_cost / sales * 100, 1) if labor_cost and sales else None, final, provider))
    conn.commit()
    conn.close()


def test_a_half_night_the_pos_had_not_closed_is_in_no_median_or_window():
    import demand
    import metrics
    rid = _rid()
    day = date(2026, 9, 29)                                # a Tuesday
    for k in range(1, 5):
        _day(rid, day - timedelta(weeks=k), 4000.0)
    # Synced mid-service last Tuesday… no: the newest Tuesday stayed partial.
    conn = models.get_conn()
    conn.execute("UPDATE labor_daily_history SET sales=900.0, final=0 WHERE restaurant_id=? AND date=?",
                 (rid, (day - timedelta(weeks=1)).isoformat()))
    conn.commit()
    conn.close()
    fc = demand.forecast_day(rid, day)
    assert fc["available"] and fc["samples"] == 3 and fc["typical_sales"] == 4000.0
    v, why = metrics.measure(rid, "sales", (day - timedelta(days=28)).isoformat(), day.isoformat())
    assert v == 4000.0 and "3 days" in why


# ── behaviour: the nightly net by basis (net_basis) ─────────────────────────

def _dsr_net(rid, d, net, provider="toast"):
    conn = models.get_conn()
    conn.execute("INSERT INTO dsr_metrics (restaurant_id, business_date, metric, value, status, source) "
                 "VALUES (?,?,?,?,?,?)", (rid, d.isoformat(), "sales.net", net, "ready", provider))
    conn.commit()
    conn.close()


def test_a_toast_daily_total_is_never_set_beside_the_reports_net(monkeypatch):
    import pos
    rid = _rid("Toast Co")
    monkeypatch.setattr(pos, "connected_provider", lambda r: ("toast", object()))
    d0 = date(2026, 9, 1)
    _day(rid, d0, 5000.0, provider="toast")
    _day(rid, d0 + timedelta(days=1), 5100.0, provider="toast")
    _dsr_net(rid, d0 + timedelta(days=1), 4800.0)
    same = cf.net_series(rid, d0, d0 + timedelta(days=1))
    assert list(same) == [(d0 + timedelta(days=1)).isoformat()]
    assert same[(d0 + timedelta(days=1)).isoformat()] == {"net": 4800.0, "basis": cf.BASIS_DSR, "source": "dsr",
                                                          "provider": "toast"}
    everything = cf.sales_history(rid, d0, d0 + timedelta(days=1))
    assert everything[d0.isoformat()]["basis"] == cf.BASIS_POS and everything[d0.isoformat()]["source"] == "pos_sync"


def test_an_rpower_total_is_the_reports_basis_and_the_provider_is_the_nights_own(monkeypatch):
    import pos
    from dsr import store
    rid = _rid("Moved Co")
    # The restaurant moved from Toast to RPOWER: last year's Toast totals are
    # another basis even though its POS today is RPOWER.
    monkeypatch.setattr(pos, "connected_provider", lambda r: ("rpower", object()))
    ly, now = date(2025, 9, 30), date(2026, 9, 22)
    _day(rid, ly, 7000.0, provider="toast")
    _day(rid, now, 6000.0, provider="rpower")
    got = store.baselines_net(rid, [ly, now], pos_sync=True)
    assert got[ly.isoformat()] == (None, None)
    assert got[now.isoformat()] == (6000.0, "pos_sync")
    # A row with no provider takes the restaurant's POS now.
    _day(rid, now - timedelta(days=7), 6100.0)
    assert store.baseline_net(rid, now - timedelta(days=7))[0] == 6100.0


def test_last_year_reads_an_imported_workbook_first(monkeypatch):
    from dsr import store
    rid = _rid("Import Co")
    day = date(2026, 9, 29)
    ly = day - timedelta(days=364)
    store.import_history(rid, [{"date": ly.isoformat(), "gross": 9000.0, "net": 8200.0}])
    _day(rid, ly, 8500.0, provider="toast")
    got = cf.last_year_net(rid, day)
    assert got == {"net": 8200.0, "basis": cf.BASIS_DSR, "source": "import", "provider": None,
                   "date": ly.isoformat()}
    assert "imported DSR workbooks (1 night)" in cf.sources_said({ly.isoformat(): got})


def test_one_basis_keeps_the_basis_with_more_nights():
    s = {"a": {"net": 1, "basis": cf.BASIS_DSR}, "b": {"net": 2, "basis": cf.BASIS_POS},
         "c": {"net": 3, "basis": cf.BASIS_POS}}
    assert set(cf.one_basis(s)) == {"b", "c"}
    s["d"] = {"net": 4, "basis": cf.BASIS_DSR}
    assert set(cf.one_basis(s)) == {"a", "d"}                # a tie keeps the report's own net


# ── behaviour: the daypart split is measured or absent (QUALITY-11) ─────────

def test_daypart_sales_come_from_the_reports_hourly_split_or_are_unmeasured():
    import schedule_intel as intel
    from dsr import store
    import dsr
    rid = _rid("Split Co")
    sats = [date(2026, 9, 12) - timedelta(weeks=k) for k in range(3)]
    for d in sats:
        rep = store.create_report(rid, d)
        store.save_block(rep["id"], "sales", dsr.block(dsr.READY, metrics={"net": 10000.0}, detail={
            "hourly": [{"hour": 12, "net": 2000.0}, {"hour": 14, "net": 1000.0},
                       {"hour": 19, "net": 6000.0}, {"hour": 0, "net": 1000.0}]}))
        store.set_stage(rep["id"], "final")
    conn = models.get_conn()
    try:
        share = intel._morning_share(conn, rid)
    finally:
        conn.close()
    # 12pm and 2pm are before 3pm; midnight belongs to the night.
    assert share == {"Saturday": pytest.approx(0.3)}


def test_features_skip_unmeasured_daypart_sales_and_count_published_weeks():
    from intelligence import features
    rid = _rid("Feat Co")
    today = date(2026, 9, 29)
    conn = models.get_conn()
    models._ensure_history_columns(conn)
    # Four regenerated drafts of one week and one published week edited
    # before publishing; a second published week only swapped after.
    for i in range(4):
        conn.execute("INSERT INTO schedule_history (restaurant_id, week_start, week_end, schedule_csv, summary_json, "
                     "generated_at) VALUES (?,?,?,?,?,?)", (rid, "2026-09-21", "2026-09-27", "", "[]", "2026-09-19"))
    h1 = conn.execute("INSERT INTO schedule_history (restaurant_id, week_start, week_end, schedule_csv, summary_json, "
                      "generated_at, published_at, edited_at) VALUES (?,?,?,?,?,?,?,?)",
                      (rid, "2026-09-14", "2026-09-20", "", "[]", "2026-09-10", "2026-09-12 10:00:00",
                       "2026-09-11 09:00:00")).lastrowid
    h2 = conn.execute("INSERT INTO schedule_history (restaurant_id, week_start, week_end, schedule_csv, summary_json, "
                      "generated_at, published_at, edited_at) VALUES (?,?,?,?,?,?,?,?)",
                      (rid, "2026-09-07", "2026-09-13", "", "[]", "2026-09-03", "2026-09-05 10:00:00",
                       "2026-09-08 09:00:00")).lastrowid
    conn.execute("INSERT INTO schedule_versions (restaurant_id, history_id, version, reason, schedule_csv, created_at) "
                 "VALUES (?,?,?,?,?,?)", (rid, h1, 2, "edited", "", "2026-09-11 09:00:00"))
    conn.execute("INSERT INTO schedule_versions (restaurant_id, history_id, version, reason, schedule_csv, created_at) "
                 "VALUES (?,?,?,?,?,?)", (rid, h2, 3, "swap", "", "2026-09-08 09:00:00"))
    conn.commit()
    conn.close()
    f = features.compute(rid, today=today)
    assert f["schedules_28d"] == 2 and f["schedule_edits_28d"] == 1 and f["schedule_adjust_rate"] == 0.5


def test_features_read_live_reviews_and_reach_alone():
    from intelligence import features
    rid = _rid("Rev Co")
    today = date(2026, 9, 29)
    conn = models.get_conn()
    for i in range(6):
        conn.execute("INSERT INTO reviews (restaurant_id, platform, external_id, author, rating, text, review_date, "
                     "fetched_at, processed) VALUES (?,?,?,?,?,?,?,?,1)",
                     (rid, "google", f"ok{i}", "G", 5, "great", (today - timedelta(days=3)).isoformat(),
                      (today - timedelta(days=3)).isoformat()))
    for i in range(6):                    # spam Google removed: rated 1
        conn.execute("INSERT INTO reviews (restaurant_id, platform, external_id, author, rating, text, review_date, "
                     "fetched_at, processed, deleted_at) VALUES (?,?,?,?,?,?,?,?,1,?)",
                     (rid, "google", f"spam{i}", "S", 1, "bad", (today - timedelta(days=2)).isoformat(),
                      (today - timedelta(days=2)).isoformat(), "2026-09-28 10:00:00"))
    for i in range(3):                    # measured posts: 1,000 reach, 1,600 impressions, 50 engagements
        conn.execute("INSERT INTO marketing_content_log (restaurant_id, topic, post_id, posted_at, created_at, reach, "
                     "impressions, likes, metrics_synced_at) VALUES (?,?,?,?,?,?,?,?,?)",
                     (rid, "Post", f"p{i}", (today - timedelta(days=5)).isoformat(), (today - timedelta(days=5)).isoformat(),
                      1000, 1600, 50, "2026-09-28 10:00:00"))
    conn.commit()
    conn.close()
    f = features.compute(rid, today=today)
    assert f["avg_rating_30d"] == 5.0 and f["reviews_30d"] == 6
    assert f["post_engagement_rate_28d"] == 0.05             # 150 / 3,000 — never 150 / 7,800


# ── behaviour: forecast weather is not a measurement (QUALITY-17) ───────────

def test_forecast_weather_and_listed_events_are_not_measured():
    from dsr import narrative
    import dsr
    assert narrative.kind_of("intel.weather_precip_pct") == "projection"
    assert narrative.kind_of("intel.weather_high_f") == "projection"
    assert narrative.kind_of("intel.events_listed") == "plan"
    assert narrative.kind_of("intel.observed_high_f") == "measured"
    assert narrative.kind_of("intel.competitor_moves") == "measured"
    facts = {"blocks": {"sales": dsr.block(dsr.READY, metrics={"net": 5000.0}),
                        "intel": dsr.block(dsr.READY, metrics={"weather_high_f": 71.0, "weather_precip_pct": 60.0,
                                                               "events_listed": 1})}}
    able, why = narrative.can_write(facts)
    assert able is False and "sales are the only figures" in why


def test_find_days_names_a_forecast_metric_as_a_forecast(monkeypatch):
    import ask_cavnar_tools as act
    rid = _rid("Find Co")
    conn = models.get_conn()
    for i, p in enumerate((80.0, 20.0, 70.0)):
        conn.execute("INSERT INTO dsr_metrics (restaurant_id, business_date, metric, value, status) VALUES (?,?,?,?,?)",
                     (rid, f"2026-09-2{i}", "intel.weather_precip_pct", p, "ready"))
    conn.commit()
    conn.close()
    out = act._find_days(rid, metric="intel.weather_precip_pct", op=">", value=50,
                         _viewer={"is_admin": True, "role": "owner"})
    assert out["count"] == 2 and out["kind"] == "projection" and "FORECAST" in out["note"]


# ── behaviour: DNA staffing issues count watched nights only (QUALITY-18) ────

def test_dna_staffing_issues_are_withdrawn_without_watched_nights():
    from intelligence import dna
    rid = _rid("Watch Co")
    today = date(2026, 9, 29)
    conn = models.get_conn()
    models._ensure_history_columns(conn)
    for w in range(6):
        hid = conn.execute("INSERT INTO schedule_history (restaurant_id, week_start, week_end, schedule_csv, "
                           "summary_json, published_at) VALUES (?,?,?,?,?,?)",
                           (rid, (today - timedelta(weeks=w + 1)).isoformat(), (today - timedelta(weeks=w)).isoformat(),
                            "", "[]", "2026-09-01 10:00:00")).lastrowid
        for k in range(2):
            d = today - timedelta(weeks=w, days=k + 1)
            conn.execute("INSERT INTO schedule_outcomes (restaurant_id, history_id, date, daypart, hours, people, issues) "
                         "VALUES (?,?,?,?,?,?,0)", (rid, hid, d.isoformat(), "night", 20, 5))
    conn.commit()
    got = dna._staffing_issues(conn, rid, today)
    assert got["raw"] is None and "watched nights" in got["basis"]
    # The clock-in check ran on ten of those nights: now it is a reading.
    for k in range(10):
        conn.execute("INSERT INTO dsr_coverage_runs (restaurant_id, business_date) VALUES (?,?)",
                     (rid, (today - timedelta(weeks=k // 2, days=k % 2 + 1)).isoformat()))
    conn.commit()
    got = dna._staffing_issues(conn, rid, today)
    conn.close()
    assert got["raw"] == 0.0 and got["n"] == 10
