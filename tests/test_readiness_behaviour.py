"""Workstream R of the Data Freshness audit (9/24/26): the readiness gate's
behaviour at the call sites, the registry's data_state in validation, the
one freshness rule, stored-read age, owner changes since the data window,
cross-module link evidence, and the Ask data-health tool.

Each test names the Top-50 item it pins. Source-level adoption (every
create_with_retry passes readiness=) is tests/test_readiness_adoption.py.
"""
import json
import types
from datetime import date, datetime, timedelta, timezone

import pytest

import models
from models import Restaurant, Review, create_restaurant, save_reviews, update_restaurant

import ai_utils  # noqa: E402
import ask_cavnar  # noqa: E402
import ask_cavnar_tools  # noqa: E402
import business_intelligence as bi  # noqa: E402
import client_api  # noqa: E402
import confidence_engine as ce  # noqa: E402
import data_freshness as df  # noqa: E402
import data_health  # noqa: E402
import labor  # noqa: E402
import rec_trust  # noqa: E402
import reporter  # noqa: E402
import response_validation as rv  # noqa: E402
import review_intelligence  # noqa: E402
import strategy_jobs  # noqa: E402


@pytest.fixture(autouse=True)
def _redirect(db_path, monkeypatch):
    real = models.get_conn
    fake = lambda *a, **k: real(db_path)  # noqa: E731
    monkeypatch.setattr(models, "get_conn", fake)
    monkeypatch.setattr(models, "DB_PATH", db_path)
    monkeypatch.setattr(review_intelligence, "DB_PATH", db_path)
    monkeypatch.setattr(review_intelligence, "get_conn", fake, raising=False)
    import webhooks
    monkeypatch.setattr(webhooks, "fire_webhook", lambda *a, **k: None)
    models._invalidate_tenant_names()
    data_health.invalidate()
    labor._NOTE_CACHE.clear()
    yield
    labor._NOTE_CACHE.clear()
    models._invalidate_tenant_names()


def _rid(db_path, name="Probe Bistro", **kw):
    return create_restaurant(Restaurant(name=name, owner_email=kw.pop("owner_email", "o@x.test"), **kw),
                             db_path=db_path)


def _msg(text):
    return types.SimpleNamespace(content=[types.SimpleNamespace(type="text", text=text)], stop_reason="end_turn")


def _set_fetched(rid, stamp):
    c = models.get_conn()
    c.execute("UPDATE restaurants SET last_fetched_at=? WHERE id=?", (stamp, rid))
    c.commit()
    c.close()


def _ago(days):
    return (date.today() - timedelta(days=days)).isoformat()


# ══ #25 one freshness rule ═══════════════════════════════════════════════

def test_state_for_and_is_stale_are_the_registry_rule():
    assert df.state_for("labor", 2) == "current"
    assert df.state_for("labor", 5) == "aging" and not df.is_stale("labor", 5)
    assert df.is_stale("labor", 6) and df.is_stale("labor", None)
    assert df.state_for("labor", None) == "unknown"
    assert df.current_within_days("labor") == 3 and df.stale_after_days("labor") == 6
    # the competitor rule is the old ai_guard "> 14 days" exactly
    assert df.stale_after_days("competitor") == 15


def test_the_labor_prompt_forbids_this_week_once_the_registry_says_not_current():
    """DH5-3: labor 6 days old read 43% "out of date" on the card while the
    prompt (LABOR_FRESH_DAYS 7) called it fresh and allowed "this week"."""
    a = {"date_range": {"start": _ago(19), "end": _ago(5), "days": 14}}
    line, fresh = labor.labor_window_line(a, datetime.now())
    assert fresh is False and "never call them" in line
    ctx = labor.labor_read_context(a, "prompt", now=datetime.now())
    assert ctx.policy["max_data_age_days"] == df.current_within_days("labor")


def test_the_labor_window_age_is_taken_on_the_restaurants_calendar(monkeypatch):
    import time_utils
    monkeypatch.setattr(time_utils, "restaurant_now_by_id",
                        lambda rid, naive=False: datetime(2026, 9, 24, 12, 0))
    a = {"date_range": {"start": "2026-09-10", "end": "2026-09-23", "days": 14}}
    line, fresh = labor.labor_window_line(a, restaurant_id=7)
    assert "1 day before today" in line and fresh is True


def test_ask_staleness_uses_restaurant_local_today_and_the_registry(monkeypatch):
    """#47: the server's date.today() set "yesterday"; a 7-day cut decided
    "current"."""
    import time_utils
    monkeypatch.setattr(time_utils, "restaurant_now_by_id", lambda rid, naive=False: datetime(2026, 9, 24, 20, 0))
    assert ask_cavnar._staleness("2026-09-23", 5) == " — through yesterday"
    assert "not this week's" in ask_cavnar._staleness("2026-09-19", 5)        # 5 days: aging
    assert "not current" in ask_cavnar._staleness("2026-09-14", 5)             # 10 days: stale


def test_digest_labor_counts_by_the_registry():
    assert reporter.labor_is_current(2) and not reporter.labor_is_current(5)
    assert reporter.labor_is_usable(5) and not reporter.labor_is_usable(6)


def test_ai_guard_freshness_and_the_labor_alert_read_the_registry():
    import ai_guard
    import notify
    assert ai_guard.freshness(_ago(12))["stale"] is False
    assert ai_guard.freshness(_ago(17))["stale"] is True
    assert ai_guard.freshness(_ago(16), source="visibility")["stale"] is True
    assert notify._labor_alert_max_age_days() == df.stale_after_days("labor") - 1


def test_admin_modules_have_no_private_day_cuts():
    import inspect
    import admin_ops
    src = inspect.getsource(admin_ops._modules_for)
    assert "is_stale" in src and "else 3" not in src


# ══ #2 / #3 the readiness gate and the registry's data_state ═══════════════

def test_validation_state_names_period_sources_that_are_not_current():
    states = [{"key": "labor", "state": "aging", "pct": 70, "lag_days": 4, "as_of": "9/20/26"},
              {"key": "inventory", "state": "aging", "pct": 70, "lag_days": 10, "as_of": "9/14/26"},
              {"key": "reviews", "state": "current", "pct": 100, "lag_days": 0.2}]
    ds = data_health.validation_state(states)
    assert ds["not_current"] == ["Shifts"]


def test_m1_holds_this_week_to_its_date_while_a_source_is_not_current():
    ctx = rv.ValidationContext(surface="labor_insight", data_state={"not_current": ["Shifts"], "as_of": "9/20/26"})
    v = rv.validate("Labor is running 31% this week.", ctx)
    assert "M1" in v.codes
    ok = rv.validate("Labor ran 31% over the period.", ctx)
    assert "M1" not in ok.codes


def test_the_labor_note_prompt_carries_the_data_state_and_passes_readiness(db_path, monkeypatch):
    rid = _rid(db_path, module_labor=1)
    seen = {}
    monkeypatch.setattr(labor, "create_with_retry",
                        lambda client, **k: seen.update(k) or _msg("Hi, labor ran 31%.\n\nRecommendations:\n"
                                                                    "None — nothing in this period calls for a "
                                                                    "schedule change."))
    monkeypatch.setattr(labor, "get_client", lambda *a, **k: object())
    a = {"is_live": True, "total_sales": 10000.0, "total_labor_cost": 3100.0, "overall_labor_pct": 31.0,
         "labor_target": 30, "overstaffed_days": [], "understaffed_days": [], "overtime_risk": [],
         "dow_summary": {}, "period_days": 14, "date_range": {"start": _ago(9), "end": _ago(8), "days": 2}}
    labor.get_claude_insights(a, restaurant_id=rid)
    assert isinstance(seen.get("readiness"), dict) and seen["readiness"].get("module") == "labor"
    prompt = seen["messages"][0]["content"]
    assert "DATA STATE" in prompt and "Shifts" in prompt


def test_the_digest_holds_a_module_whose_data_is_past_its_horizon(db_path, monkeypatch):
    """Unattended: labor three weeks old is held (its line is not emailed,
    the gap line says why) while the reviews still send."""
    rid = _rid(db_path, module_labor=1)
    monkeypatch.setattr(labor, "analyse_shifts_for_restaurant", lambda *a, **k: {
        "is_live": True, "overall_labor_pct": 31.0, "overtime_risk": [],
        "date_range": {"end": _ago(21), "days": 14}})
    save_reviews([Review(restaurant_id=rid, platform="google", external_id="d1", author="Dana Ray", rating=5,
                         text="Lovely dinner.", review_date=date.today().isoformat())], db_path=db_path)
    report = reporter.build_report_from_db(rid, "Probe Bistro", days=7, db_path=db_path)
    seen = {}
    monkeypatch.setattr(ai_utils, "get_client", lambda *a, **k: object())
    monkeypatch.setattr(ai_utils, "create_with_retry", lambda *a, **k: seen.update(k) or _msg(
        "HEADLINE: Pat, one new review this week.\nREVIEWS: One review came in at 5.0.\n"
        "LABOR: Labor ran 31.0% of revenue this week."))
    out = reporter.generate_ai_digest_summary(report, "Probe Bistro", "Pat", restaurant_id=rid)
    assert out.get("headline") and "labor" not in out
    assert any(g.startswith("Labor: the shift data isn't current") for g in out.get("_data_gaps") or [])
    assert "LABOR" not in seen["messages"][0]["content"].split("You MUST output exactly these lines")[1].split("\n")[0]
    assert isinstance(seen.get("readiness"), dict)


def test_the_weekly_plan_does_not_file_an_item_about_a_held_module():
    holds = {"labor": "Shifts: no dated data on file"}
    assert strategy_jobs.plan_item_held({"title": "Trim Tuesday staffing by one server", "why": "x"}, holds) == "labor"
    assert strategy_jobs.plan_item_held({"title": "Reply to the two 1-star reviews", "why": "x"}, holds) is None


def test_a_diagnosis_is_not_regenerated_on_counts_past_their_horizon(db_path, monkeypatch):
    """Unattended diagnosis: counts of unknown age refuse the call (the prior
    read stands); no counts at all is left to the driver floor."""
    import food_cost_intelligence as fci
    rid = _rid(db_path, module_inventory=1)
    c = models.get_conn()
    c.execute("INSERT INTO ingredients (restaurant_id, name, unit, is_active, last_recount_at) VALUES (?,?,?,1,?)",
              (rid, "Salmon", "lb", _ago(60)))
    c.commit()
    c.close()
    drv = {"available": True, "drivers": [{"kind": "waste", "label": "Salmon waste", "item": "Salmon",
                                           "dollars_monthly": 400.0}], "total_monthly": 400.0}
    monkeypatch.setattr(fci, "build_evidence", lambda r, db_path=None: {"drivers": drv})
    called = []
    monkeypatch.setattr(ai_utils, "create_with_retry", lambda *a, **k: called.append(k) or _msg("{}"))
    out = fci.diagnose(rid, force=True)
    assert out.get("ok") is False and "data not ready" in out.get("reason", "") and not called


def test_ask_answers_carry_the_registrys_data_state(db_path, monkeypatch):
    """DH3-7: Ask's validation got no data_state, so M1 never fired on Ask."""
    stale_fetch = (datetime.now(timezone.utc) - timedelta(days=10)).strftime("%Y-%m-%d %H:%M:%S")
    rid = _rid(db_path, reviews_live=1)
    _set_fetched(rid, stale_fetch)
    ds = ask_cavnar.answer_data_state(rid, ["read_reviews"])
    assert any(str(s).startswith("Reviews") for s in ds.get("stale_sources") or [])
    ctx = ask_cavnar._validation_context(["snapshot"], rid, data_state=ds)
    v = rv.validate("Your reviews this week are strong.", ctx)
    assert "M1" in v.codes


def test_ask_suggestions_are_measured_over_the_tools_sources(monkeypatch):
    seen = {}
    monkeypatch.setattr(rec_trust, "assess", lambda rid, key, **k: seen.update(k) or ce.unknown())
    ask_cavnar.suggestion_confidence(1, "ask_tip:x", {"tools_used": ["read_demand_forecast"],
                                                      "modules_consulted": []})
    assert "weather" in seen["sources"] and "sales" in seen["sources"]


# ══ #28 consumers pass the sources their data rests on ═══════════════════

def test_the_quiet_night_and_the_slow_day_read_the_demand_sources(monkeypatch):
    seen = []
    monkeypatch.setattr(rec_trust, "assess", lambda rid, key, **k: seen.append(k.get("sources")) or ce.unknown())
    strategy_jobs.quiet_night_confidence(1, "quiet_night:x", {"samples": 6, "weekday": "Tuesday"})
    import morning_brief
    morning_brief._attach_confidence(1, [{"key": "slow_day", "rec": "slow_day:tue", "_samples": 6}])
    assert seen[0] == seen[1] == df.sources_for(["demand"]) and "pos" in seen[0]


def test_the_digest_move_rests_on_what_it_names():
    assert reporter.move_sources("Trim one server Tuesday to bring labor down") == df.sources_for(["reviews", "labor"])
    assert reporter.move_sources("Reply to Dana's review today") == ("reviews",)


# ══ #6 the labor note ═══════════════════════════════════════════════════════

def test_the_labor_note_is_rewritten_on_a_new_local_day(monkeypatch):
    """DH3-2: keyed on the data alone, Monday's note was served on Friday."""
    calls = []
    monkeypatch.setattr(labor, "get_claude_insights", lambda a, **k: calls.append(1) or f"note {len(calls)}")
    a = {"period": "p", "date_range": {"end": "2026-09-20"}, "overall_labor_pct": 31.0}
    monkeypatch.setattr(labor, "_note_local_day", lambda rid: "2026-09-21")
    assert labor.labor_note(5, a) == labor.labor_note(5, a) == "note 1"
    monkeypatch.setattr(labor, "_note_local_day", lambda rid: "2026-09-25")
    assert labor.labor_note(5, a) == "note 2"


def test_the_labor_read_is_dated_by_when_the_note_was_written(monkeypatch):
    a = {"period": "p", "date_range": {"end": "2026-09-20"}, "overall_labor_pct": 31.0}
    monkeypatch.setattr(labor, "get_claude_insights", lambda a, **k: "the note")
    labor.labor_note(6, a)
    written = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(days=4)
    key = next(k for k in labor._NOTE_CACHE if k[0] == 6)
    labor._NOTE_CACHE[key] = ("the note", written)
    client_api.labor_cache_put(6, a, "the note")
    state = client_api.labor_read_state(6)
    assert state["age_days"] == 4 and state["as_of"] and state["stale"] is False


def test_the_labor_read_cache_is_keyed_on_the_figures(monkeypatch):
    a = {"period": "p", "date_range": {"end": "2026-09-20"}, "overall_labor_pct": 31.0}
    client_api.labor_cache_put(8, a, "read on 31%")
    assert client_api.labor_cached_read(8, a) == "read on 31%"
    moved = dict(a, overall_labor_pct=34.0)
    assert client_api.labor_cached_read(8, moved) is None


def test_a_pos_sync_clears_the_labor_read(monkeypatch):
    import pos
    cleared = []
    monkeypatch.setattr(client_api, "invalidate_insight_cache", lambda rid, prefixes=None: cleared.append(rid))
    monkeypatch.setattr(labor, "load_shifts", lambda csv_string=None, **k: [])
    monkeypatch.setattr(labor, "drop_future_shifts", lambda rows, restaurant_id=None: rows)
    monkeypatch.setattr(models, "save_client_data", lambda *a, **k: None)
    monkeypatch.setattr(models, "get_client_data", lambda *a, **k: {})
    pos.save_synced_shifts(9, "date,employee\n", "toast")
    assert cleared == [9]


# ══ #7 a stored diagnosis's own age ══════════════════════════════════════

def _dg(hours, **kw):
    at = (datetime.now(timezone.utc) - timedelta(hours=hours)).strftime("%Y-%m-%d %H:%M:%S")
    return dict({"cause": "Parking is hard to find", "generated_at": at, "age_hours": hours,
                 "stale": hours > 24, "as_of": None, "confidence": "medium"}, **kw)


def test_a_day_old_diagnosis_says_the_date_it_was_written():
    """DH3-1: a 25-hour-old read said "written over a week ago"."""
    ev = rec_trust.diagnosis_evidence(_dg(25), 8, "reviews", "8 reviews on this theme")
    assert "over a week" not in ev["basis"] and "written " in ev["basis"]
    written = (datetime.now(timezone.utc) - timedelta(hours=25)).date().isoformat()
    assert ce._mdy(written) in ev["basis"]
    assert "stale_read" in ce.PARTIAL_FLAGS


def test_a_sixty_day_old_diagnosis_is_capped_by_its_own_age(db_path):
    """DH3-1: reviews fetched this morning made a 60-day-old cause read ~70%."""
    rid = _rid(db_path, reviews_live=1)
    _set_fetched(rid, datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S"))
    ev = rec_trust.diagnosis_evidence(_dg(60 * 24), 8, "reviews", "8 reviews on this theme")
    out = rec_trust.assess(rid, "diag_review:parking", evidence=ev, sources=("reviews",))
    fr = out["dimensions"]["freshness"]
    assert fr["pct"] == 0 and fr["stalest"] == "diagnosis"
    assert out["pct"] <= ce.STALE_CAP


def test_a_stale_diagnosis_is_an_association_then_no_anchor():
    assert rec_trust.diagnosis_anchor_strength(_dg(2)) == "likely"
    assert rec_trust.diagnosis_anchor_strength(_dg(3 * 24)) == "association"
    assert rec_trust.diagnosis_anchor_strength(_dg(30 * 24)) is None
    ctx = reporter.digest_context(1, "p", diagnosis=_dg(30 * 24))
    assert not ctx.cause_anchors
    ctx2 = reporter.digest_context(1, "p", diagnosis=_dg(3 * 24))
    assert all(a.get("strength") == "association" for a in ctx2.cause_anchors)


def test_ask_hands_a_stale_diagnosis_over_as_an_association():
    out = ask_cavnar_tools._stale_diagnoses_as_associations([_dg(2), _dg(3 * 24, as_of="9/21/26"), _dg(40 * 24)])
    assert len(out) == 2 and out[0]["cause"] == "Parking is hard to find"
    assert out[1]["cause"].startswith("Possibly associated") and "9/21/26" in out[1]["cause"]


# ══ #24 Ask: read_data_health and every result's data age ════════════════

def test_ask_has_a_read_data_health_tool_over_every_source(db_path):
    rid = _rid(db_path)
    assert "read_data_health" in ask_cavnar_tools._BY_NAME
    assert set(df.TOOL_SOURCES["read_data_health"]) == set(df.SOURCES)
    body = json.loads(ask_cavnar_tools.run_read_tool("read_data_health", rid, {}))
    assert body.get("has_data") is True and "overall" in body


def test_every_ask_read_carries_its_data_as_of_and_sync_error(db_path, monkeypatch):
    rid = _rid(db_path, module_labor=1)
    c = models.get_conn()
    c.execute("INSERT INTO labor_daily_history (restaurant_id, date, sales, labor_cost) VALUES (?,?,?,?)",
              (rid, _ago(4), 1000.0, 300.0))
    c.commit()
    c.close()
    body = json.loads(ask_cavnar_tools.run_read_tool("read_demand_forecast", rid, {}))
    assert "_data_as_of" in body and "_sync_error" in body and "_data_state" in body
    data_health.record_attempt(rid, "sales", False, error="Toast 401 unauthorized")
    body = json.loads(ask_cavnar_tools.run_read_tool("read_demand_forecast", rid, {}))
    assert body["_sync_error"] and "failing" in body["_sync_error"]


# ══ #30 stale weather in the schedule prompt ═════════════════════════════

def test_stale_forecast_rows_are_labelled_and_all_stale_means_no_forecast():
    rows = [{"date": "2026-09-26", "day_name": "Friday", "high_f": 61, "short_forecast": "Rain",
             "as_of": "2026-09-21T10:00:00+00:00", "stale": True},
            {"date": "2026-09-27", "day_name": "Saturday", "high_f": 70, "short_forecast": "Sunny", "stale": False}]
    lines, all_stale = labor.weather_prompt_rows(rows)
    assert "(forecast from 9/21/26, not refreshed)" in lines[0] and "not refreshed" not in lines[1]
    assert all_stale is False
    assert labor.weather_prompt_rows([rows[0]])[1] is True


def test_an_all_stale_forecast_puts_the_no_weather_marker_in_the_schedule_prompt(monkeypatch):
    import types as _t
    seen = {}
    monkeypatch.setattr(labor, "create_with_retry", lambda client, **k: seen.update(k) or _t.SimpleNamespace(
        content=[_t.SimpleNamespace(text="date,day,employee,role,shift_start,shift_end,scheduled_hours,notes\n"
                                         "---SUMMARY---\n- ok")], stop_reason="end_turn"))
    analysis = {"overall_labor_pct": 28.0, "overstaffed_days": [], "understaffed_days": [], "dow_summary": {},
                "date_range": {"start": "2026-06-01", "end": "2026-06-14", "days": 14}}
    shifts = [{"employee": "Alex", "role": "Server", "date": "2026-06-01", "scheduled_hours": 8, "actual_hours": 8}]
    stale = [{"date": "2026-09-26", "day_name": "Friday", "high_f": 61, "short_forecast": "Rain",
              "as_of": "2026-09-21T10:00:00+00:00", "stale": True}]
    labor.generate_optimized_schedule(analysis, shifts, restaurant_name="Test Bistro", weather_forecast=stale)
    prompt = seen["messages"][0]["content"]
    assert labor.NO_WEATHER_MARKER in prompt and "Rain" not in prompt.split(labor.NO_WEATHER_MARKER)[0][-400:]


# ══ #31 marketing freshness and performance claims ═══════════════════════

def test_marketing_is_dated_by_the_last_metrics_sync_not_the_last_post(db_path):
    """DH1-12: posting every two weeks read "stale" the morning after a
    clean metrics sync."""
    rid = _rid(db_path)
    update_restaurant(rid, {"ig_token": "tok"}, db_path=db_path)
    c = models.get_conn()
    c.execute("INSERT INTO marketing_content_log (restaurant_id, content_type, topic, post_id, posted_at) "
              "VALUES (?,?,?,?,?)", (rid, "instagram_post", "tacos", "p1", _ago(20)))
    c.commit()
    c.close()
    synced = (datetime.now(timezone.utc) - timedelta(hours=6)).strftime("%Y-%m-%d %H:%M:%S")
    r = models.get_restaurant(rid)
    s = df.source_state(r, "marketing", context={"metrics_sync": {"last_ok_at": synced}})
    assert s["state"] == "current" and "last post" in s["basis"] and s["as_of_iso"] != _ago(20)


def test_performance_claims_are_dropped_while_the_metrics_are_not_current():
    text = ("Pat, the taco posts worked best last month.\n\n1. Post the brunch special Saturday.\n"
            "2. The patio photo performed well, so repeat it.")
    assert client_api._mkt_drop_performance(text) == ""        # the lead line itself is a performance claim
    text2 = "Pat, lean into brunch this week.\n\n1. Post the brunch special Saturday.\n2. The patio photo performed well."
    out = client_api._mkt_drop_performance(text2)
    assert "brunch special" in out and "performed" not in out


# ══ #44 a change since the data window ═══════════════════════════════════

def test_a_schedule_published_after_the_data_window_caps_and_cautions(db_path):
    rid = _rid(db_path, module_labor=1)
    c = models.get_conn()
    c.execute("INSERT INTO schedule_history (restaurant_id, week_start, hours_scheduled, hours_budget, labor_target, "
              "schedule_csv, published_at) VALUES (?,?,?,?,?,?,?)",
              (rid, date.today().isoformat(), 100, 100, 30, "x", datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")))
    c.commit()
    c.close()
    a = {"is_live": True, "date_range": {"end": _ago(2), "days": 14}}
    ctx = rec_trust.Context(rid, freshness_context={"labor": a})
    out = rec_trust.assess(rid, "trim_day:Tuesday", evidence={"n": 28, "kind": "trading_days", "basis": "b"},
                           sources=("labor",), ctx=ctx)
    assert out.get("changed_since") and "published a schedule" in out["caution"]
    assert out["dimensions"]["evidence"]["pct"] <= ce.PARTIAL_CAP


def test_a_changed_target_is_recorded_once_and_read_as_a_change(db_path):
    rid = _rid(db_path)
    update_restaurant(rid, {"labor_target_pct": 28.0}, db_path=db_path)
    update_restaurant(rid, {"labor_target_pct": 28.0}, db_path=db_path)       # same value: nothing new
    c = models.get_conn()
    n = c.execute("SELECT COUNT(*) FROM activity_log WHERE restaurant_id=? AND event_type='target_change'",
                  (rid,)).fetchone()[0]
    c.close()
    assert n == 1
    changes = rec_trust.owner_changes(rid)
    assert any(ch["what"] == "changed your labor target" and ch["sources"] == ("labor",) for ch in changes)


# ══ #45 cross-module link evidence ═══════════════════════════════════════

def test_link_evidence_is_the_weaker_joined_input():
    link = {"modules": ["reviews", "labor"], "evidence": ["a", "b"],
            "evidence_inputs": [{"n": 3, "kind": "reviews", "basis": "3 reviews"},
                                {"n": 28, "kind": "trading_days", "basis": "28 days"}]}
    ev = bi.link_evidence_input(link)
    assert ev["n"] == 3 and ev["kind"] == "reviews" and "inferred" in ev["flags"]
    strong = dict(link, evidence_inputs=[{"n": 40, "kind": "reviews", "basis": "40"},
                                         {"n": 28, "kind": "trading_days", "basis": "28"}])
    assert ce.evidence(**bi.link_evidence_input(strong))["pct"] > ce.evidence(**ev)["pct"]


def test_a_link_across_different_periods_is_labelled_and_capped():
    c = {"category": "service", "mentions": 9, "window_days": 90, "first_seen": "2026-06-01",
         "last_seen": "2026-06-20", "weekday": {"value": "Saturday"}}
    labor_a = {"is_live": True, "period_days": 28, "date_range": {"start": "2026-08-25", "end": "2026-09-21"},
               "dow_summary": {"Monday": 30.0, "Tuesday": 30.0, "Wednesday": 30.0, "Saturday": 20.0}}
    links = bi.correlations(1, data={"reviews": {"clusters": [c]}, "labor": labor_a, "food_cost": {}})
    link = next(l for l in links if l["kind"] == "reviews_x_labor")
    assert link["periods"] == "different" and "different periods" in link["headline"]
    ev = ce.evidence(**bi.link_evidence_input(link))
    assert ev["pct"] <= bi.DIFFERENT_PERIODS_CAP
