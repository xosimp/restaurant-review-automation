"""Audit #9 remediation: what reaches a screen, an alert or an inbox.

labor.py has computed sales_data_missing and days_missing_sales since the
savings-formula work, with a code comment saying "the UI should say so
rather than present a partial number as whole". Nothing ever returned
them: no endpoint, no template, no Swift type. This file holds them at the
boundary they were dropped at, along with the sample-data gate on the two
AI paths and the age bound on the labor alert.
"""
import types

import pytest

import client_api
import labor
import models
import mobile_api
import notify


# ── Sample data never reaches an AI path ───────────────────────────────────

def test_the_insight_refuses_to_narrate_the_bundled_sample_week(monkeypatch):
    """load_shifts_for_restaurant substitutes a fictional June 2026 week
    when nothing has been uploaded. Narrating it as the owner's own — with
    real-looking dollar amounts and specific dates — was the single most
    misleading thing this module could do."""
    called = []
    monkeypatch.setattr(labor, "create_with_retry",
                        lambda *a, **kw: called.append(1))
    analysis = labor.analyse_shifts(labor.load_shifts(), hourly_rate=26.0, labor_target=30.0)
    analysis["is_live"] = False
    text = labor.get_claude_insights(analysis, restaurant_name="Somewhere", owner_name="Sam")
    assert not called, "a Claude call was made against sample data"
    assert "no shift data on file" in text.lower()


def test_a_live_upload_is_still_narrated(monkeypatch):
    captured = {}

    def fake(client, **kwargs):
        captured.update(kwargs)
        return types.SimpleNamespace(
            content=[types.SimpleNamespace(text="Sam, labor ran 31%.\nRecommendations:\n1. a\n2. b\n3. c")],
            stop_reason="end_turn")
    monkeypatch.setattr(labor, "create_with_retry", fake)
    monkeypatch.setattr(labor, "verify_figures", lambda *a, **kw: [], raising=False)
    analysis = labor.analyse_shifts(labor.load_shifts(), hourly_rate=26.0, labor_target=30.0)
    analysis["is_live"] = True
    labor.get_claude_insights(analysis, restaurant_name="Somewhere", owner_name="Sam")
    assert captured, "a live upload should still reach the model"


def test_the_schedule_job_refuses_sample_data(monkeypatch, db_path):
    """The existing `if not shifts` guard could never fire, because the
    loader guarantees shifts are non-empty. A brand-new restaurant could
    generate a full week staffed by eight people who do not exist."""
    monkeypatch.setattr(models, "DB_PATH", db_path)
    real = models.get_conn
    for mod in (models, client_api, labor):
        monkeypatch.setattr(mod, "get_conn", lambda *a, **k: real(db_path), raising=False)
    conn = real(db_path)
    conn.execute("INSERT INTO restaurants (id, name, owner_email) VALUES (1,'R','o@x.test')")
    conn.commit()
    conn.close()
    monkeypatch.setattr(client_api, "get_restaurant", lambda rid: models.get_restaurant(rid, db_path))
    with pytest.raises(ValueError, match="No shift data"):
        client_api._build_schedule_result(1)


# ── The partial-data flags reach the payload ───────────────────────────────

def _labor_payload(monkeypatch, analysis):
    monkeypatch.setattr(mobile_api, "analyse_shifts_for_restaurant",
                        lambda rid: analysis, raising=False)
    monkeypatch.setattr("labor.analyse_shifts_for_restaurant", lambda rid: analysis)
    monkeypatch.setattr(mobile_api, "get_restaurant",
                        lambda rid: types.SimpleNamespace(
                            labor_target_pct=30.0, hourly_rate=26.0, timezone="America/Chicago"))
    monkeypatch.setattr(mobile_api, "_staff_constraints_index", lambda rid: {})
    payload, _ = mobile_api._do_mobile_labor(1)
    return payload


def _base_analysis(**over):
    a = {"is_live": True, "overall_labor_pct": 28.0, "total_sales": 60000.0,
         "period_days": 14, "employee_hours": {}, "overtime_risk": [], "role_summary": {},
         "date_range": {"start": "2026-09-01", "end": "2026-09-14", "days": 14},
         "overstaffed_days": [], "understaffed_days": [], "dow_summary": {},
         "potential_savings": 0, "potential_savings_monthly": 0,
         "sales_data_missing": False, "days_missing_sales": [],
         "hours_are_estimated": False, "days_with_conflicting_sales": [],
         "duplicate_rows_ignored": 0, "period_too_short_to_project": False,
         "overtime_hours": 0, "overtime_premium": 0.0}
    a.update(over)
    return a


def test_missing_sales_days_reach_the_client(monkeypatch):
    p = _labor_payload(monkeypatch, _base_analysis(days_missing_sales=["2026-09-03", "2026-09-04"]))
    assert p["days_missing_sales"] == ["2026-09-03", "2026-09-04"]
    assert p["data_complete"] is False
    assert "no sales" in p["data_caveat"]


def test_estimated_hours_reach_the_client(monkeypatch):
    p = _labor_payload(monkeypatch, _base_analysis(hours_are_estimated=True))
    assert p["hours_are_estimated"] is True
    assert "scheduled hours" in p["data_caveat"]


def test_complete_data_carries_no_caveat(monkeypatch):
    p = _labor_payload(monkeypatch, _base_analysis())
    assert p["data_complete"] is True
    assert p["data_caveat"] == ""
    assert p["on_track"] is True


def test_incomplete_data_is_never_reported_as_on_track(monkeypatch):
    p = _labor_payload(monkeypatch, _base_analysis(hours_are_estimated=True, overall_labor_pct=5.0))
    assert p["on_track"] is False


def test_estimated_hours_do_not_become_an_industry_savings_claim(monkeypatch):
    """Measured before the fix: a CSV missing one column reported 0% labor
    and $62,100/month of savings against the industry midpoint."""
    p = _labor_payload(monkeypatch, _base_analysis(hours_are_estimated=True, overall_labor_pct=0.0))
    assert p["savings_breakdown"]["labor_vs_industry_monthly"] == 0
    assert p["savings_breakdown"]["labor_vs_industry_annual"] == 0


def test_a_sub_week_period_makes_no_monthly_claim(monkeypatch):
    p = _labor_payload(monkeypatch, _base_analysis(period_days=2, period_too_short_to_project=True))
    assert p["savings_breakdown"]["labor_vs_industry_monthly"] == 0


def test_a_measured_period_still_makes_the_claim(monkeypatch):
    p = _labor_payload(monkeypatch, _base_analysis(overall_labor_pct=25.0))
    assert p["savings_breakdown"]["labor_vs_industry_monthly"] > 0


def test_a_failed_analysis_does_not_read_as_zero_percent_on_track(monkeypatch):
    def boom(rid):
        raise RuntimeError("db gone")
    monkeypatch.setattr("labor.analyse_shifts_for_restaurant", boom)
    monkeypatch.setattr(mobile_api, "get_restaurant",
                        lambda rid: types.SimpleNamespace(
                            labor_target_pct=30.0, hourly_rate=26.0, timezone="America/Chicago"))
    monkeypatch.setattr(mobile_api, "_staff_constraints_index", lambda rid: {})
    payload, _ = mobile_api._do_mobile_labor(1)
    assert payload["analysis_failed"] is True
    assert payload["on_track"] is False
    assert payload["data_complete"] is False
    assert "couldn't finish" in payload["data_caveat"]


def test_the_overtime_premium_comes_from_the_costing_pass(monkeypatch):
    """It was recomputed in the payload from the first flagged week only,
    and never fed the cost the percentage is derived from."""
    p = _labor_payload(monkeypatch, _base_analysis(overtime_premium=412.5, overtime_hours=15))
    assert p["savings_breakdown"]["labor_overtime"] == 412
    assert p["overtime_hours"] == 15


# ── The labor alert is about a period someone is still working ─────────────

def _alerting_restaurant(db_path):
    from models import update_restaurant
    conn = models.get_conn(db_path)
    conn.execute("INSERT INTO restaurants (id, name, owner_email) VALUES (1,'R','o@x.test')")
    conn.commit()
    conn.close()
    update_restaurant(1, {"urgent_via_sms": 0, "urgent_via_email": 1,
                          "owner_email": "o@x.test", "alert_labor_over": 1,
                          "labor_target_pct": 30.0}, db_path=db_path)
    return 1


def _fire(monkeypatch, db_path):
    sent = []
    monkeypatch.setattr(notify, "_send_alert_email",
                        lambda to, subject, html, **kw: sent.append((subject, html)))
    monkeypatch.setattr("push.fire_push", lambda *a, **kw: None)
    notify.check_daily_alerts(db_path=db_path)
    return sent


def test_a_stale_period_no_longer_alerts_forever(monkeypatch, db_path):
    """Snapshots are written every time an insight is generated, i.e. on
    every Labor tab open. The alert took the most recently SAVED row with
    no bound on the period, so a restaurant that last uploaded in June got
    a fresh text about June every seven days, indefinitely."""
    from datetime import date, timedelta
    rid = _alerting_restaurant(db_path)
    old_end = (date.today() - timedelta(days=90)).isoformat()
    old_start = (date.today() - timedelta(days=96)).isoformat()
    models.save_labor_snapshot(rid, old_start, old_end, 38.0, 3800, 10000, db_path=db_path)
    assert _fire(monkeypatch, db_path) == []


def test_a_recent_period_over_target_still_alerts(monkeypatch, db_path):
    from datetime import date, timedelta
    rid = _alerting_restaurant(db_path)
    end = (date.today() - timedelta(days=2)).isoformat()
    start = (date.today() - timedelta(days=8)).isoformat()
    models.save_labor_snapshot(rid, start, end, 38.0, 3800, 10000, db_path=db_path)
    sent = _fire(monkeypatch, db_path)
    assert len(sent) == 1


def test_the_alert_names_the_period_it_is_about(monkeypatch, db_path):
    """The email printed the dates; the SMS and the push did not, so a
    figure from months ago read as this week's."""
    from datetime import date, timedelta
    rid = _alerting_restaurant(db_path)
    end = (date.today() - timedelta(days=2)).isoformat()
    start = (date.today() - timedelta(days=8)).isoformat()
    models.save_labor_snapshot(rid, start, end, 38.0, 3800, 10000, db_path=db_path)
    texts = []
    monkeypatch.setattr(notify, "_send_alert_email", lambda *a, **kw: None)
    monkeypatch.setattr("push.fire_push",
                        lambda rid_, kind, subject, body, **kw: texts.append(body))
    notify.check_daily_alerts(db_path=db_path)
    assert texts and "for " in texts[0]
    assert "%" in texts[0]


# ── A fresh upload invalidates the narrative written about the old one ─────

def test_uploading_shifts_drops_the_cached_insight():
    client_api._cache_set("labor-insight:7", "old narrative")
    client_api._cache_set("mobile-labor-insight:7", "old narrative")
    client_api._cache_set("labor-insight:8", "someone else's")
    client_api.invalidate_insight_cache(7)
    assert client_api._cache_get("labor-insight:7") is None
    assert client_api._cache_get("mobile-labor-insight:7") is None
    assert client_api._cache_get("labor-insight:8") == "someone else's"


# ── The unverified-figures flag is actually used ───────────────────────────

def test_invented_figures_are_marked_rather_than_shown_as_fact(monkeypatch):
    """verify_figures returns the figures a model stated that are not in
    its input. labor.py called it and threw the return away."""
    monkeypatch.setattr(labor, "create_with_retry",
                        lambda *a, **kw: types.SimpleNamespace(
                            content=[types.SimpleNamespace(text="Sam, labor ran 31%.")],
                            stop_reason="end_turn"))
    monkeypatch.setattr("ai_guard.verify_figures", lambda *a, **kw: ["$9,999"])
    analysis = labor.analyse_shifts(labor.load_shifts(), hourly_rate=26.0, labor_target=30.0)
    analysis["is_live"] = True
    text = labor.get_claude_insights(analysis, restaurant_name="R", owner_name="Sam")
    assert "UNVERIFIED" in text and "$9,999" in text
