"""Confidence audit — the server-side integration pass over groups E–J.

Each group shipped its half of a handoff; these pin the joins:

  H8   the weekly forecast lines go through forecast_log.record and are
       scored by the same nightly pass as every other kind
  H16  "not for us" to the same advice holds on Home, the one-thing hero,
       the nightly report and the Reviews "Do today" line
  F6   calibrated dollars on Home cards, the hero and DSR actions, the raw
       figure kept as what the ledger snapshots
  E/F  Historical Accuracy never counts a result read against its own
       trigger window — on the taken side or the untaken comparison
  I/F  one noise band for a week-on-week read
  G/E  data_freshness reads the merged G helpers: weather age, metrics sync
  E/I  the Reviews read carries its trend confidence in both places
  8    constants mirrored across layers held in step
"""
import inspect
import json
import sys
import types
from datetime import date, datetime, timedelta, timezone

import pytest

import models
from models import Restaurant, create_restaurant

# Imported before the fixture patches get_conn (the bound-import hazard).
import auth  # noqa: E402
import business_intelligence as bi  # noqa: E402
import client_api  # noqa: E402
import home_brief  # noqa: E402
import insight_store  # noqa: E402
import rec_learning  # noqa: E402
import rec_ledger  # noqa: E402
from dsr import narrative  # noqa: E402


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
    for mod in (client_api, home_brief):
        monkeypatch.setattr(mod, "get_conn", conn)
    auth.init_auth(db_path=db_path)
    models.init_email_log(db_path=db_path)
    import ai_utils
    c = real(db_path)
    c.executescript(ai_utils._USAGE_TABLE_SQL)
    c.commit()
    c.close()
    home_brief.invalidate()
    return db_path


def _rid(db, **kw):
    fields = dict(name="Join Co", owner_email="j@x.com", module_reviews=1, module_labor=1,
                  module_inventory=0, module_marketing=0)
    fields.update(kw)
    return create_restaurant(Restaurant(**fields), db_path=db)


def _q(db, sql, args=()):
    c = models.get_conn(db)
    try:
        return [dict(r) for r in c.execute(sql, args).fetchall()]
    finally:
        c.close()


def _x(db, sql, args=()):
    c = models.get_conn(db)
    try:
        c.execute(sql, args)
        c.commit()
    finally:
        c.close()


def _user(rid):
    return {"id": 1, "restaurant_id": rid, "base_restaurant_id": rid, "username": "owner", "role": "client",
            "is_admin": 0, "email": "o@x.com"}


# A heavy Friday against steady other days: Home names trim_day:Friday with
# monthly dollars (the fixture tests/test_confidence_engine.py pins).
_STEADY = {"Monday": 28.0, "Tuesday": 28.5, "Wednesday": 27.5, "Thursday": 28.0, "Friday": 36.0}
_BY_DAY = {"2026-09-04": {"sales": 1000, "labor_cost": 360}, "2026-09-11": {"sales": 1000, "labor_cost": 360}}


def _labor():
    end = datetime(2026, 9, 22).date()
    return {"is_live": True, "overall_labor_pct": 29.0, "period_days": 28,
            "date_range": {"days": 28, "start": (end - timedelta(days=27)).isoformat(), "end": end.isoformat()},
            "days_missing_sales": [], "potential_savings_weekly": 0.0,
            "total_labor_cost": 7000, "total_sales": 25000, "dow_summary": dict(_STEADY), "by_day": dict(_BY_DAY),
            "overtime_risk": [], "week_start_day": 0, "hours_are_estimated": False}


def _home(rid, monkeypatch):
    import labor
    monkeypatch.setattr(labor, "analyse_shifts_for_restaurant", lambda *a, **k: _labor())
    home_brief.invalidate()
    p, st = home_brief.build_home_brief(_user(rid), fresh=True)
    assert st == 200
    return p


def _learned(rid, pairs):
    """An effectiveness model with `pairs` realised ÷ shown ratios for trim_day
    and dsr_action (and nothing else learned)."""
    m = rec_learning.Effectiveness(rid, [])
    m.calibration = {"trim_day": list(pairs), "dsr_action": list(pairs)}
    return m


def _dsr_facts():
    import dsr
    blocks = {"sales": {"status": dsr.READY, "metrics": {"net": 5000.0, "net_last_week": 5100.0}},
              "labor": {"status": dsr.READY, "metrics": {"pct": 31.0, "target_pct": 26.0}}}
    return narrative.Facts({"blocks": blocks, "business_date": "2026-09-22"})


def _friday_action(dollars=None):
    return {"text": "Cut one server from Friday dinner.", "why": "Labor ran 31% against 26%.",
            "urgency": "next_schedule", "effort": "low", "kind": "control_hours", "subject": None,
            "dollars_monthly": dollars, "cites": ["labor.pct", "labor.target_pct"]}


# ══ H8 — the weekly forecasts go through forecast_log ══════════════════════

def test_h8_record_weekly_forecast_is_forecast_log_record(db, monkeypatch):
    import forecast_log
    rid = _rid(db)
    seen = []
    real = forecast_log.record

    def spy(*a, **k):
        seen.append((a, k))
        return real(*a, **k)
    monkeypatch.setattr(forecast_log, "record", spy)
    monday = date(2026, 9, 21)
    out = insight_store.record_weekly_forecast(rid, "labor_week", 31.0, basis="carried forward", today=monday,
                                               db_path=db)
    assert out["recorded"] and out["horizon_end"] == "2026-10-04"
    (args, kw), = seen
    assert args[:3] == (rid, "labor_week", 31.0) and kw["period_of"] == insight_store.next_week_end(monday)
    again = insight_store.record_weekly_forecast(rid, "labor_week", 29.0, today=monday + timedelta(days=2),
                                                 db_path=db)
    assert again == {"recorded": False, "horizon_end": "2026-10-04", "reason": "already frozen this week"}
    # a kind the weekly adapter does not own is refused before forecast_log
    assert not insight_store.record_weekly_forecast(rid, "waste_week", 10.0, db_path=db)["recorded"]
    assert len(seen) == 2
    # the adapter never raises: a forecast_log failure is "could not be stored"
    monkeypatch.setattr(forecast_log, "record", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))
    assert insight_store.record_weekly_forecast(rid, "review_rating_week", 4.4, db_path=db)["reason"] == \
        "could not be stored"


def test_h8_a_weekly_forecast_is_scored_by_the_nightly_pass(db):
    import forecast_log
    import scheduler
    rid = _rid(db, module_marketing=1)
    monday = date(2026, 8, 3)
    rec = insight_store.record_weekly_forecast(rid, "marketing_reach_week", 500.0, today=monday, db_path=db)
    assert rec["recorded"] and rec["horizon_end"] == "2026-08-16"
    for i, reach in enumerate((300, 100)):
        _x(db, "INSERT INTO marketing_content_log (restaurant_id, content_type, post_id, reach, created_at) "
               "VALUES (?, 'post', ?, ?, ?)", (rid, f"p{i}", reach, f"2026-08-1{i + 1} 12:00:00"))
    assert forecast_log.restaurants_due(today=date(2026, 8, 12), db_path=db) == []      # the week is open
    assert rid in forecast_log.restaurants_due(today=date(2026, 8, 20), db_path=db)
    assert forecast_log.score_due(rid, today=date(2026, 8, 20), db_path=db)["scored"] == 1
    row = forecast_log.frozen(rid, "marketing_reach_week", date(2026, 8, 12), db_path=db)
    assert row["actual"] == 400.0 and row["error_pct"] == 25.0
    # the job walks restaurants_due and scores each through score_due
    src = inspect.getsource(scheduler.run_forecast_scoring)
    assert "forecast_log.restaurants_due()" in src and "forecast_log.score_due(rid)" in src
    for kind in insight_store.WEEKLY_FORECAST_KINDS:
        assert kind in forecast_log.KINDS


# ══ H16 — "not for us" holds on every surface ════════════════════════════

def test_h16_a_decline_on_reviews_holds_on_home_the_hero_and_the_nightly_report(db, monkeypatch):
    rid = _rid(db)
    p = _home(rid, monkeypatch)
    card = next(r for r in p["recommendations"] if r["key"] == "trim_day:Friday")
    assert card["advice_signature"] == "labor:day:friday"
    assert all("advice_signature" in a for a in p["attention"])
    # The Reviews read's Do today line names the same advice, and the owner
    # says "not for us" to it there.
    text = "\U0001f4ca This week: 12 reviews.\n✅ Do today: Move a server off Friday lunch."
    rv = client_api._review_insight_recs(rid, {"insight": text, "figures_verified": True, "names_verified": True})
    key = rv["recs"][0]["key"]
    assert rv["recs"][0]["advice_signature"] == "labor:day:friday"
    assert rec_ledger.record(rid, key, "dismissed", surface="reviews", db_path=db, meta={"kind": "not_for_us"})
    assert "labor:day:friday" in insight_store.declined_signatures(rid, db_path=db)
    # Home: the Friday trim is gone, though its own key was never answered.
    p = _home(rid, monkeypatch)
    assert "trim_day:Friday" not in [r["key"] for r in p["recommendations"]]
    # The one-thing hero: a non-critical candidate with that signature never leads.
    cand = {"key": "trim_day:Friday", "what": "Trim Friday staffing on the next schedule", "why": "w",
            "modules": ["labor"], "urgency": "normal", "dollars_monthly": 400.0, "evidence": [], "score": 5}
    other = dict(cand, key="trim_day:Monday", what="Trim Monday staffing on the next schedule", score=1)
    got = bi.pick_one_thing(rid, [cand, other], db_path=db, learned=rec_learning.Effectiveness(rid, []))
    assert got["key"] == "trim_day:Monday" and got["advice_signature"] == "labor:day:monday"
    # ...but a critical one is never hidden by a decline elsewhere.
    crit = dict(cand, urgency="critical")
    assert bi.pick_one_thing(rid, [crit], db_path=db, learned=rec_learning.Effectiveness(rid, []))["key"] \
        == "trim_day:Friday"
    # The nightly report drops its own Friday cut.
    dropped = []
    ctx = types.SimpleNamespace(restaurant_id=rid, db_path=db)
    kept = narrative.settle_actions([_friday_action()], _dsr_facts(), ctx, (set(), set(), set()), dropped)
    assert kept == [] and "same advice elsewhere" in dropped[0]["why"]


def test_h16_a_decline_on_the_nightly_report_holds_on_home(db, monkeypatch):
    rid = _rid(db)
    rec_ledger.present(rid, "dsr_action:control_hours:labor", "ops", "dsr",
                       title="Cut one server from Friday dinner.", db_path=db)
    rec_ledger.record(rid, "dsr_action:control_hours:labor", "dismissed", surface="dsr", db_path=db,
                      meta={"kind": "not_for_us"})
    p = _home(rid, monkeypatch)
    assert "trim_day:Friday" not in [r["key"] for r in p["recommendations"]]
    # A plain hide (two weeks) is not a decline, and is not carried across.
    rid2 = _rid(db, name="Hide Co", owner_email="h@x.com")
    rec_ledger.present(rid2, "dsr_action:control_hours:labor", "ops", "dsr",
                       title="Cut one server from Friday dinner.", db_path=db)
    rec_ledger.record(rid2, "dsr_action:control_hours:labor", "dismissed", surface="dsr", db_path=db,
                      silence_days=14)
    p2 = _home(rid2, monkeypatch)
    assert "trim_day:Friday" in [r["key"] for r in p2["recommendations"]]


def test_h16_home_reads_the_shared_helpers():
    src = inspect.getsource(home_brief._build)
    assert "home_declined_signatures(rid)" in src and "signature_of(" in src
    assert "insight_store.declined_signatures" in inspect.getsource(home_brief.home_declined_signatures)
    assert "insight_store.advice_signature" in inspect.getsource(home_brief.signature_of)
    assert "declined_signatures(" in inspect.getsource(bi.pick_one_thing)


# ══ F6 — calibrated dollars on every surface that shows dollars ═════════════

def test_f6_attach_dollar_calibration_below_and_above_the_floor():
    below = rec_learning.attach_dollar_calibration({"key": "trim_day:Friday", "dollars_monthly": 400.0},
                                                   _learned(1, [0.5, 0.5]))
    assert below["dollars_adjusted"] is None and below["calibration_n"] == 2 and below["calibration_note"] is None
    above = rec_learning.attach_dollar_calibration({"key": "trim_day:Friday", "dollars_monthly": 400.0},
                                                   _learned(1, [0.5, 0.5, 0.5]))
    # median 0.5, shrunk toward 1 by 3 pseudo-pairs (0.75), bounded at 0.8
    assert above["dollars_adjusted"] == 320.0 and above["calibration_n"] == 3
    assert above["calibration_note"] == "adjusted from 3 measured results"
    assert above["dollars_monthly"] == 400.0                      # the raw estimate stays
    none = rec_learning.attach_dollar_calibration({"key": "x", "dollars_monthly": None}, _learned(1, [0.5] * 3))
    assert none["dollars_adjusted"] is None and none["calibration_n"] == 0
    assert rec_learning.attach_dollar_calibration({"key": "x", "dollars_monthly": 9.0}, None)["dollars_adjusted"] \
        is None


def test_f6_home_card_shows_the_adjusted_figure_and_the_ledger_keeps_the_raw_one(db, monkeypatch):
    rid = _rid(db)
    monkeypatch.setattr(rec_learning, "effectiveness", lambda *a, **k: _learned(rid, [0.5, 0.5, 0.5]))
    p = _home(rid, monkeypatch)
    card = next(r for r in p["recommendations"] if r["key"] == "trim_day:Friday")
    assert card["dollars_monthly"] and card["calibration_n"] == 3
    assert card["dollars_adjusted"] == round(card["dollars_monthly"] * 0.8, 2)
    assert card["calibration_note"] == "adjusted from 3 measured results"
    row = _q(db, "SELECT dollar_value FROM rec_instances WHERE restaurant_id=? AND key=?", (rid, "trim_day:Friday"))
    assert row and row[0]["dollar_value"] == card["dollars_monthly"]
    # every card carries the three fields, with or without dollars
    for r in p["recommendations"]:
        assert {"dollars_adjusted", "calibration_n", "calibration_note"} <= set(r)


def test_f6_the_hero_and_the_dsr_actions_carry_the_calibrated_dollars(db, monkeypatch):
    rid = _rid(db)
    cand = {"key": "trim_day:Friday", "what": "Trim Friday staffing on the next schedule", "why": "w",
            "modules": ["labor"], "urgency": "normal", "dollars_monthly": 400.0, "evidence": [], "score": 5}
    hero = bi.pick_one_thing(rid, [cand], db_path=db, learned=_learned(rid, [0.5] * 3))
    assert hero["dollars_monthly"] == 400.0 and hero["dollars_adjusted"] == 320.0 and hero["calibration_n"] == 3
    monkeypatch.setattr(rec_learning, "effectiveness", lambda *a, **k: _learned(rid, [1.5] * 4))
    ctx = types.SimpleNamespace(restaurant_id=rid, db_path=db)
    act = dict(_friday_action(400.0), text="Cut one server from Friday dinner to save about $400 a month.")
    kept = narrative.settle_actions([act], _dsr_facts(), ctx, (set(), set(), set()), [])
    assert kept[0]["dollars_monthly"] == 400.0
    # median 1.5 over 4 pairs, shrunk (1.5·4 + 3)/7 = 1.286, bounded at 1.2
    assert kept[0]["dollars_adjusted"] == 480.0 and kept[0]["calibration_note"] == "adjusted from 4 measured results"
    # what the report presents to the ledger is still the raw figure
    assert narrative.ledger_items(kept)[0]["dollar_value"] == 400.0


# ══ E accuracy — no trigger-window result ever counts ══════════════════════

def _taken_with_tracker(db, rid, key, verdict, overlaps, days_ago=60, source_key=None):
    rec = rec_ledger.present(rid, key, "labor", "home", db_path=db)
    _x(db, "UPDATE rec_instances SET created_at=datetime('now', ?) WHERE rec_id=?", (f"-{days_ago} days", rec))
    _x(db, "UPDATE rec_events SET at=datetime('now', ?) WHERE rec_id=?", (f"-{days_ago} days", rec))
    rec_ledger.record(rid, key, "accepted", surface="home", db_path=db)
    c = models.get_conn(db)
    try:
        cur = c.execute(
            "INSERT INTO recommendation_outcomes (restaurant_id, source, source_key, title, metric, started_on, "
            "evaluate_on, status, verdict, baseline_overlaps_trigger, created_at) VALUES (?, 'recommendation', ?, "
            "'t', 'labor_pct', date('now', ?), date('now', ?), 'evaluated', ?, ?, datetime('now', ?))",
            (rid, source_key or key, f"-{days_ago} days", f"-{days_ago - 28} days", verdict, int(overlaps),
             f"-{days_ago} days"))
        c.commit()
        oid = cur.lastrowid
    finally:
        c.close()
    _x(db, "UPDATE rec_instances SET tracker_id=? WHERE rec_id=?", (oid, rec))
    return rec


def test_accuracy_reads_kind_record_and_never_counts_a_trigger_window_result(db):
    import rec_trust
    rid = _rid(db)
    for i in range(5):
        _taken_with_tracker(db, rid, f"trim_day:D{i}", "improved", overlaps=True, days_ago=40 + 35 * i)
    acc = rec_trust.assess(rid, "trim_day:Friday", evidence={"n": 8, "kind": "weekdays"},
                           db_path=db)["dimensions"]["accuracy"]
    assert acc["pct"] is None and acc["n"] == 0 and acc["source"] == "none"
    # the same five read against a clean baseline count
    rid2 = _rid(db, name="Clean Co", owner_email="c@x.com")
    for i in range(5):
        _taken_with_tracker(db, rid2, f"trim_day:D{i}", "improved", overlaps=False, days_ago=40 + 35 * i)
    acc2 = rec_trust.assess(rid2, "trim_day:Friday", evidence={"n": 8, "kind": "weekdays"},
                            db_path=db)["dimensions"]["accuracy"]
    assert acc2["source"] == "own" and acc2["n"] == 5 and acc2["improved"] == 5
    # rec_trust's record IS kind_record, the untaken comparison included
    rec = rec_trust.Context(rid2, db_path=db).record("trim_day")
    assert rec == rec_learning.kind_record(rid2, "trim_day", db_path=db) and "untaken" in rec
    assert "rec_learning.kind_record(" in inspect.getsource(rec_trust.Context.record)


def test_the_untaken_comparison_drops_trigger_window_results_too(db):
    rid = _rid(db)
    recs = []
    for i in range(6):
        rec = rec_ledger.present(rid, f"trim_day:U{i}", "labor", "home", db_path=db)
        rec_ledger.record(rid, f"trim_day:U{i}", "dismissed", surface="home", db_path=db)
        recs.append(rec)
    for i, rec in enumerate(recs):
        _x(db, "INSERT INTO recommendation_outcomes (restaurant_id, source, source_key, title, metric, status, "
               "verdict, started_on, evaluate_on, after_start, after_end, baseline_overlaps_trigger) VALUES (?, "
               "'observed', ?, 't', 'labor_pct', 'evaluated', 'improved', ?, ?, ?, ?, ?)",
           (rid, f"observed:untaken:{rec}", f"2026-0{i + 1}-01", f"2026-0{i + 1}-20", f"2026-0{i + 1}-01",
            f"2026-0{i + 1}-20",
            1 if i < 4 else 0))
    u = rec_learning.untaken_comparison(rid, "trim_day", db_path=db)
    assert u["measured"] == 2 and u["improved"] == 2


# ══ I/F — one noise band for a week ════════════════════════════════════════

def test_week_band_is_the_stricter_of_the_scaled_stated_band_and_the_own_band(monkeypatch):
    import metrics
    import weekly_review as wr
    scaled = metrics.fixed_band("labor_pct", 30.0) * wr.band_scale("labor_pct")
    # thin history: the stated band scaled for seven days governs
    monkeypatch.setattr(metrics, "noise_band", lambda *a, **k: {"band": None, "method": "stated"})
    b0 = wr.week_band(1, "labor_pct", 30.0)
    assert b0["band"] == round(scaled, 4) and b0["false_alarm_rate"] is None
    # a restaurant whose weeks swing wider: its own 7-day band, never scaled again
    seen = {}
    monkeypatch.setattr(metrics, "noise_band", lambda *a, **k: (seen.update(k) or
                                                                {"band": scaled * 1.5, "false_alarm_rate": 0.1,
                                                                 "method": "own windows", "basis": "own"}))
    b = wr.week_band(1, "labor_pct", 30.0)
    assert b["band"] == round(scaled * 1.5, 4) and b["false_alarm_rate"] == 0.1 and seen["window_days"] == 7
    # an own band narrower than the scaled floor never narrows it
    monkeypatch.setattr(metrics, "noise_band", lambda *a, **k: {"band": scaled / 3, "false_alarm_rate": 0.1})
    assert wr.week_band(1, "labor_pct", 30.0)["band"] == round(scaled, 4)
    # compare() given the band does not scale it a second time
    assert metrics.compare("labor_pct", 30.0, 30.0 + scaled * 0.9, band=b0["band"])["verdict"] == "no_clear_change"
    assert metrics.compare("labor_pct", 30.0, 30.0 + scaled * 1.1, band=b0["band"])["verdict"] == "worsened"
    assert wr.week_band(1, "labor_pct", None)["band"] is None


def test_the_weekly_review_and_the_digest_use_week_band_only():
    import reporter
    import weekly_review as wr
    src = inspect.getsource(wr.build)
    assert "week_band(restaurant_id, key" in src and "band=wb[\"band\"]" in src and "band_scale=" not in src
    rsrc = inspect.getsource(reporter)
    assert "_wr_lr.week_band(" in rsrc and "band_scale=_wr_lr.band_scale" not in rsrc


# ══ G → E — data_freshness reads the merged helpers ═════════════════════════

def _row(db, rid):
    return _q(db, "SELECT * FROM restaurants WHERE id=?", (rid,))[0]


def test_weather_freshness_carries_age_hours_and_stale(db):
    import data_freshness as dfr
    rid = _rid(db)
    now = datetime(2026, 9, 24, 12, 0, tzinfo=timezone.utc)
    _x(db, "UPDATE restaurants SET weather_cached_at=? WHERE id=?",
       ((now - timedelta(hours=6)).isoformat(), rid))
    s = dfr.source_state(_row(db, rid), "weather", db_path=db, now=now)
    assert s["age_hours"] == 6.0 and s["stale"] is False and s["state"] == "current" and not s["error"]
    _x(db, "UPDATE restaurants SET weather_cached_at=? WHERE id=?",
       ((now - timedelta(hours=30)).isoformat(), rid))
    s = dfr.source_state(_row(db, rid), "weather", db_path=db, now=now)
    assert s["stale"] is True and s["pct"] <= 50 and "30 hours old" in s["error"]
    _x(db, "UPDATE restaurants SET weather_cached_at=? WHERE id=?", ("not a stamp", rid))
    s = dfr.source_state(_row(db, rid), "weather", db_path=db, now=now)
    assert s["state"] == "unknown" and s["stale"] is True and s["pct"] == 0


def test_marketing_freshness_reads_the_metrics_sync_state(db):
    import data_freshness as dfr
    import scheduler
    rid = _rid(db, module_marketing=1)
    now = datetime.now(timezone.utc)
    _x(db, "UPDATE restaurants SET ig_token='t' WHERE id=?", (rid,))
    _x(db, "INSERT INTO marketing_content_log (restaurant_id, content_type, post_id, created_at) "
           "VALUES (?, 'post', 'p1', datetime('now'))", (rid,))
    # never synced: no penalty is invented, but nothing claims a sync either
    s = dfr.source_state(_row(db, rid), "marketing", db_path=db, now=now)
    assert s["state"] == "current" and s["metrics_synced_at"] is None
    scheduler.record_metrics_sync(rid, ok=True)
    s = dfr.source_state(_row(db, rid), "marketing", db_path=db, now=now)
    assert s["state"] == "current" and s["metrics_synced_at"] and "metrics synced" in s["basis"]
    scheduler.record_metrics_sync(rid, ok=False, error="OAuthException 190")
    s = dfr.source_state(_row(db, rid), "marketing", db_path=db, now=now)
    assert s["pct"] <= 50 and "Post metrics sync failing" in s["error"]
    # a sync that last succeeded days ago has missed nights
    s = dfr.source_state(_row(db, rid), "marketing", db_path=db, now=now,
                         context={"metrics_sync": {"last_ok_at": (now - timedelta(days=5)).strftime(
                             "%Y-%m-%d %H:%M:%S"), "error": None}})
    assert s["pct"] <= 50 and "Post metrics last synced" in s["error"]


def test_data_freshness_adapters_call_the_helpers_directly():
    import data_freshness as dfr
    src = inspect.getsource(dfr)
    assert "hasattr(time_utils" not in src and "hasattr(admin_ops" not in src and "hasattr(fetcher" not in src
    assert "time_utils import parse_stamp" in inspect.getsource(dfr._stamp)
    assert "admin_ops.review_source(r)" in inspect.getsource(dfr._review_sampling)
    assert "fetcher.places_coverage(" in inspect.getsource(dfr._review_sampling)


def test_home_ts_reads_through_parse_stamp():
    ts = home_brief._ts
    assert "parse_stamp" in inspect.getsource(ts)
    assert ts("2026-09-24 13:05:00") == datetime(2026, 9, 24, 13, 5, tzinfo=timezone.utc)
    assert ts("2026-09-24T13:05:00Z") == datetime(2026, 9, 24, 13, 5, tzinfo=timezone.utc)
    assert ts("2026-09-24T13:05:00+02:00") == datetime(2026, 9, 24, 11, 5, tzinfo=timezone.utc)
    assert ts("2026-09-24") == datetime(2026, 9, 24, tzinfo=timezone.utc)
    naive = datetime(2026, 9, 24, 13, 5)
    assert ts(naive.isoformat()) == naive.astimezone().astimezone(timezone.utc)      # server-local
    assert ts("2026-09-24 garbage") == datetime(2026, 9, 24, tzinfo=timezone.utc)
    assert ts(None) is None and ts("") is None and ts("nope") is None


# ══ E/I — the Reviews read's trend confidence in both places ════════════════

def test_reviews_trend_confidence_is_in_both_places_on_every_path(db):
    rid = _rid(db)
    # a stored read from before `trend` existed: top-level only
    p = client_api._review_insight_recs(rid, {"insight": "x", "confidence": "medium"})
    assert p["trend"]["confidence"] == "medium" and p["confidence"] == "medium"
    # a trend without the top-level copy
    p = client_api._review_insight_recs(rid, {"insight": "x", "trend": {"direction": "flat", "confidence": "low"}})
    assert p["confidence"] == "low" and p["trend"]["direction"] == "flat"
    # both present: nothing is overwritten
    p = client_api._review_insight_recs(rid, {"insight": "x", "confidence": "high",
                                              "trend": {"confidence": "high"}})
    assert p["confidence"] == p["trend"]["confidence"] == "high"
    # an error payload is left as it is
    p = client_api._review_insight_recs(rid, {"insight": "x", "error": "x"})
    assert "trend" not in p


# ══ 8 — constants mirrored across layers stay in step ══════════════════════

def test_mirrored_floors_and_intervals_stay_in_step():
    import admin_ops
    import confidence_engine as ce
    from intelligence import confidence as icf, scoring
    assert scoring.MIN_MEASURED_FOR_RATE == rec_learning.MIN_MEASURED_FOR_RATE == ce.MIN_MEASURED
    assert icf.PRIOR_MIN_MEASURED == rec_learning.PRIOR_MIN_MEASURED == ce.PRIOR_MIN_MEASURED
    for k, n in ((0, 1), (3, 4), (5, 5), (12, 15), (1, 30)):
        lo, hi = ce.wilson(k, n)
        assert rec_learning.wilson(k, n) == (round(lo, 3), round(hi, 3)) == admin_ops._wilson(k, n)
