"""Audit #25 WS6 — AI quality alerting and the per-template comparison
(A07-04, A07-05, A07-11).

A07-04  a surface whose refusals and fallbacks over the last 24 hours run
        more than twice its trailing 14-day rate (n >= 20) warns, naming the
        build and prompt template most of them came from; every quality
        event is tagged with both.
A07-05  the console's AI Quality payload ranks (caller, template_hash) by
        refusal-plus-fallback rate, with each template's first sighting.
A07-11  the schedule generations' fallback share over twice its baseline
        warns.
"""
import json
import sqlite3
from datetime import datetime, timedelta, timezone

import pytest

import admin_ops  # noqa: F401  (binds get_conn at import: imported before the patch)
import ai_quality_watch as aqw
import ai_utils
import models
import ops
import scheduler
import status_manager

NOW = datetime(2026, 10, 26, 18, 0, 0, tzinfo=timezone.utc)


@pytest.fixture(autouse=True)
def db(db_path, monkeypatch):
    real, default = models.get_conn, models.DB_PATH
    monkeypatch.setattr(models, "get_conn", lambda path=None, *a, **k: real(db_path if path in (None, default) else path))
    monkeypatch.setattr(models, "DB_PATH", db_path)
    monkeypatch.setattr(status_manager, "DB_PATH", db_path)
    aqw._cache.clear()
    ops._page_memory.clear()
    yield db_path
    aqw._cache.clear()


def _x(db_path, sql, rows):
    c = sqlite3.connect(db_path)
    try:
        c.executemany(sql, rows)
        c.commit()
    finally:
        c.close()


def _stamp(dt):
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _seed_surface(db_path, surface, action, base_calls, base_events, day_calls, day_events, build="abc123def456",
                  template="tpl-new"):
    """`base_*` spread over the 14 days before the last 24 hours, `day_*` in
    the last 24 hours, relative to NOW."""
    usage, events = [], []
    for i in range(base_calls):
        usage.append((action, _stamp(NOW - timedelta(days=2 + (i % 13), hours=1))))
    for i in range(day_calls):
        usage.append((action, _stamp(NOW - timedelta(hours=1 + (i % 20)))))
    for i in range(base_events):
        events.append((surface, "validation_refused", action, _stamp(NOW - timedelta(days=2 + (i % 13))), "old", "tpl-old"))
    for i in range(day_events):
        events.append((surface, "fallback", action, _stamp(NOW - timedelta(hours=2 + (i % 18))), build, template))
    _x(db_path, "INSERT INTO ai_usage (action, model, vendor, outcome, status, created_at) "
                "VALUES (?, 'claude-sonnet-5-5', 'anthropic', 'ok', 'ok', ?)", usage)
    _x(db_path, "INSERT INTO ai_quality_events (surface, kind, action, created_at, n, build, template_hash) "
                "VALUES (?, ?, ?, ?, 1, ?, ?)", events)


def test_a_quality_event_carries_its_build_and_prompt_template(db, monkeypatch):
    monkeypatch.setenv("RAILWAY_GIT_COMMIT_SHA", "0123456789abcdef")
    _x(db, "INSERT INTO ai_calls (call_id, action, caller, template_hash) VALUES (?, ?, ?, ?)",
       [("call-1", "dsr_narrative", "narrative.run", "tpl-9f")])
    assert ai_utils.record_quality_event("dsr_narrative", "validation_refused", restaurant_id=5,
                                         action="dsr_narrative", call_id="call-1", db_path=db)
    c = sqlite3.connect(db)
    try:
        row = c.execute("SELECT build, template_hash FROM ai_quality_events").fetchone()
    finally:
        c.close()
    assert row == ("0123456789ab", "tpl-9f")


def test_a_surface_refusing_six_times_its_baseline_warns_with_the_change_it_came_from(db):
    # 14 days at 5% (14 of 280 calls), today at 30% (6 of 20).
    _seed_surface(db, "dsr_narrative", "dsr_narrative", 280, 14, 20, 6)
    (r,) = aqw.surface_regressions(now=NOW, use_cache=False)
    assert r["surface"] == "dsr_narrative" and r["calls_24h"] == 20 and r["events_24h"] == 6
    assert r["rate_24h"] == 0.3 and r["rate_base"] == 0.05 and r["ratio"] == 6.0
    assert r["build"] == "abc123def456" and r["template_hash"] == "tpl-new"
    (p,) = aqw.platform_problems(now=NOW)
    assert p["key"] == "ai_quality:dsr_narrative"
    assert "6 of 20 calls" in p["line"] and "build abc123def456" in p["line"] and "template tpl-new" in p["line"]


def test_too_few_calls_or_a_steady_rate_says_nothing(db):
    _seed_surface(db, "dsr_narrative", "dsr_narrative", 280, 14, 19, 6)           # n < 20 today
    _seed_surface(db, "reply_public", "draft_response", 280, 28, 40, 6)          # 10% then 15%: under 2x
    assert aqw.surface_regressions(now=NOW, use_cache=False) == []


def test_the_platform_check_warns_on_an_ai_quality_regression(db, monkeypatch):
    c = sqlite3.connect(db)
    try:
        c.execute("UPDATE scheduler_heartbeat SET beat_at=datetime('now'), loop_completed_at=datetime('now') WHERE id=1")
        c.commit()
    finally:
        c.close()
    monkeypatch.setattr(aqw, "platform_problems",
                        lambda db_path=None, now=None: [{"key": "ai_quality:dsr", "line": "AI quality: dsr ..."}])
    monkeypatch.setattr(scheduler, "scheduling_allowed", lambda: True)
    sent = []
    monkeypatch.setattr(ops, "alert_will", lambda subject, lines: sent.append(lines) or True)
    out = ops.check_platform_sla(send=True)
    assert "AI quality: dsr ..." in out["problems"] and out["alerted"]
    assert ops.last_operator_alert()["key"].startswith("platform_sla_warn:") , "a warning, once a day"
    # /health's read-only call does not read it.
    assert ops.check_platform_sla(send=False)["ai_watch"] == []


def _gen(db_path, rid, n, fell_back, days_ago):
    """`n` saved generations `days_ago` before NOW, the first `fell_back` of
    them drafted by a fallback (the solver raised; carried forward)."""
    rows = []
    for i in range(n):
        ds = ({"chosen": "solver", "source": "carry_forward", "code_source": "carry_forward",
               "code_dates": ["2026-10-27"], "why": {"2026-10-27": "solver_error"}}
              if i < fell_back else {"chosen": "solver", "source": "solver", "code_source": "solver",
                                     "code_dates": ["2026-10-27"], "why": {}})
        rows.append((rid, "2026-10-26", _stamp(NOW - timedelta(days=days_ago, minutes=i)),
                     json.dumps({"draft_source": ds}), 120.0))
    _x(db_path, "INSERT INTO schedule_history (restaurant_id, week_start, generated_at, review_json, total_seconds) "
                "VALUES (?, ?, ?, ?, ?)", rows)


def test_schedule_generations_falling_back_twice_their_baseline_warn(db):
    rid = models.create_restaurant(models.Restaurant(name="Gen Co", owner_email="g@x.test"))
    _gen(db, rid, 30, 1, days_ago=8)          # the baseline: 1 in 30
    _gen(db, rid, 20, 8, days_ago=0)          # the last 20: 8 fell back (the solver raised)
    g = aqw.generation_fallbacks(now=NOW, use_cache=False)
    assert g["n"] == 20 and g["fell_back"] == 8 and g["over"] is True
    assert g["whys"] == {"solver_error": 8}
    line = next(p["line"] for p in aqw.platform_problems(now=NOW) if p["key"] == "schedule_fallbacks")
    assert "8 of the last 20" in line and "solver_error 8" in line


def test_a_steady_fallback_share_says_nothing(db):
    rid = models.create_restaurant(models.Restaurant(name="Gen Co", owner_email="g@x.test"))
    _gen(db, rid, 30, 6, days_ago=8)          # 20%
    _gen(db, rid, 20, 5, days_ago=0)          # 25%
    assert aqw.generation_fallbacks(now=NOW, use_cache=False)["over"] is False


def test_the_model_source_falling_back_to_code_counts():
    assert aqw._fell_back({"chosen": "model", "code_dates": ["2026-10-27"]})
    assert not aqw._fell_back({"chosen": "model", "code_dates": []})
    assert not aqw._fell_back({"chosen": "solver", "code_source": "solver", "code_dates": ["2026-10-27"]})
    assert not aqw._fell_back(None)


def _sql(db_path, sql, rows):
    c = sqlite3.connect(db_path)
    try:
        c.executemany(sql, rows)
        c.commit()
    finally:
        c.close()


def test_the_console_ranks_prompt_templates_by_refusal_and_fallback_rate(db):
    # Stamped by the database's own clock (the console reads the last 30 days of it).
    calls, events, usage = [], [], []
    for i in range(10):          # the old template: 1 refusal in 10, nine days ago
        calls.append((f"o{i}", "drafter.draft_response", "tpl-old", "refused" if i == 0 else "ok",
                      f"-{9 * 24 * 60 + i} minutes"))
    for i in range(10):          # the new one: 2 refusals and 3 fallbacks in 10, yesterday
        calls.append((f"n{i}", "drafter.draft_response", "tpl-new", "refused" if i < 2 else "ok",
                      f"-{24 * 60 + i} minutes"))
        usage.append((f"n{i}", 0.01, f"-{24 * 60 + i} minutes"))
    for i in range(3):
        events.append((f"n{5 + i}", "reply_public", "fallback", "-1440 minutes"))
    _sql(db, "INSERT INTO ai_calls (call_id, action, caller, template_hash, outcome, created_at) "
             "VALUES (?, 'draft_response', ?, ?, ?, datetime('now', ?))", calls)
    _sql(db, "INSERT INTO ai_quality_events (call_id, surface, kind, created_at, n) "
             "VALUES (?, ?, ?, datetime('now', ?), 1)", events)
    _sql(db, "INSERT INTO ai_usage (call_id, cost_usd, created_at, action) "
             "VALUES (?, ?, datetime('now', ?), 'draft_response')", usage)
    rows = admin_ops.ai_quality(days=30)["templates"]
    assert [r["template_hash"] for r in rows] == ["tpl-new", "tpl-old"]
    new, old = rows
    assert new["refusal_rate"] == 0.5 and new["fallbacks"] == 3 and new["current"] is True
    assert old["refusal_rate"] == 0.1 and old["current"] is False
    assert new["cost_usd"] == 0.1 and old["first_seen"] < new["first_seen"]


def test_a_smoke_eval_off_its_baseline_warns(db):
    import ai_eval
    ai_eval.record_run({"surface": "review_reply", "model": "claude-sonnet-5-5", "version": "v1",
                        "scores": {"scored": 8, "refusal_rate": 0.5}}, trigger="smoke",
                       deviations=["refusal rate 50.0% against the baseline's 12.5% (+2 points allowed)"])
    (p,) = [x for x in aqw.platform_problems() if x["key"].startswith("ai_eval:")]
    assert p["key"] == "ai_eval:review_reply" and "50.0%" in p["line"]
