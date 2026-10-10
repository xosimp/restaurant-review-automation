"""Fix round G — AI quality findings, the call trace and the history rollup.

#58  guard findings, validation refusals, dropped lines and safety
     disagreements land in ai_quality_events, never in job_failures.
#117 every provider call leaves an ai_calls trace — template hash, request
     id, stop reason, output hash, and (for the newest calls) the prompt and
     output with guest contact details and names redacted — linked from its
     ledger row, its validation verdict and the stored read it produced.
#70  ai_usage_daily keeps the history the 120-day prune deletes.
#140 each restaurant's daily cost and each action's rate against its own
     baseline; fallbacks are recorded.
"""
import json
import sqlite3
import types

import pytest

import ai_utils
import models
import ops
from models import Restaurant, create_restaurant


@pytest.fixture(autouse=True)
def _db(db_path, monkeypatch):
    real = models.get_conn
    monkeypatch.setattr(models, "get_conn", lambda *a, **k: real(db_path))
    monkeypatch.setattr(models, "DB_PATH", db_path)
    ai_utils.reset_process_state(db_path)
    yield
    ai_utils.reset_process_state(db_path)


def _rid(db_path, name="Quality Co"):
    return create_restaurant(Restaurant(name=name, owner_email=f"{name.split()[0].lower()}@x.test"), db_path=db_path)


def _row_conn(conn):
    conn.row_factory = sqlite3.Row
    return conn


def _q(db_path, sql, args=()):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute(sql, args)]
    finally:
        conn.close()


class _U:
    input_tokens, output_tokens = 100, 50
    cache_creation_input_tokens = cache_read_input_tokens = 0


class _Client:
    def __init__(self, text="the answer", stop="end_turn"):
        self.messages = self
        self.text, self.stop = text, stop

    def create(self, **kw):
        return types.SimpleNamespace(usage=_U(), stop_reason=self.stop, _request_id="req_1",
                                     content=[types.SimpleNamespace(type="text", text=self.text)])


# ── #58 quality findings are not job failures ──────────────────────────────

def test_a_guard_finding_is_a_quality_event_not_a_job_failure(db_path, monkeypatch):
    import ai_guard
    captured = []
    monkeypatch.setattr(ops, "capture", lambda *a, **k: captured.append(k))
    rid = _rid(db_path)
    bad = ai_guard.verify_figures("Labor ran $9,999 over.", "labor was $120", job="labor_insight", restaurant_id=rid)
    assert bad
    rows = _q(db_path, "SELECT surface, kind, restaurant_id, n FROM ai_quality_events")
    assert rows == [{"surface": "labor_insight", "kind": "figures", "restaurant_id": rid, "n": len(bad)}]
    assert captured == [] and _q(db_path, "SELECT COUNT(*) AS n FROM job_failures")[0]["n"] == 0


def test_a_safety_disagreement_is_counted_from_the_quality_ledger(db_path):
    import notify
    rid = _rid(db_path)
    notify.record_safety_disagreement(["food poisoning"], restaurant_id=rid, review_id=12)
    assert notify.safety_disagreements(days=1) == 1
    assert _q(db_path, "SELECT COUNT(*) AS n FROM job_failures")[0]["n"] == 0


def test_competitor_citation_drops_are_quality_events_with_their_restaurant(db_path):
    import competitor
    rid = _rid(db_path)
    text = "Recommendations:\n1. Do the thing with no citation.\n"
    competitor._validate_recommendation_citations(text, [{"name": "Rival", "reviews": []}], restaurant_id=rid)
    (row,) = _q(db_path, "SELECT surface, kind, restaurant_id FROM ai_quality_events")
    assert row == {"surface": "competitor_insight", "kind": "citation_dropped", "restaurant_id": rid}


def test_the_moved_capture_sites_no_longer_write_job_failures():
    """The sites the packet names (#58) record quality events."""
    import inspect
    import client_api
    import competitor
    import dsr.narrative as narrative
    import labor
    import reporter
    import strategy_jobs
    for fn, needle in ((labor._drop_note_bullets, "schedule note bullet dropped"),
                       (strategy_jobs.run_weekly_plan, "weekly plan item not filed"),
                       (competitor._validate_bullets, "strength/weakness"),
                       (reporter.generate_ai_digest_summary, "line dropped")):
        src = inspect.getsource(fn)
        assert "record_quality_event" in src, fn.__qualname__
        i = src.index(needle)
        assert "ops.capture" not in src[max(0, i - 300):i + 200], fn.__qualname__
    assert "dsr narrative dropped" not in inspect.getsource(narrative._capture)
    assert 'job="review_insight", context' not in inspect.getsource(client_api._do_review_insight) \
        or "record_quality_event" in inspect.getsource(client_api._do_review_insight)


# ── #117 the call trace ─────────────────────────────────────────────────────

def test_a_call_leaves_a_redacted_trace_linked_from_its_ledger_row(db_path):
    rid = _rid(db_path)
    prompt = "Reviewer: Ann Smith\nEmail ann@example.com or call (312) 555-0101.\nThe soup was cold."
    m = ai_utils.create_with_retry(_Client("Sorry, Ann."), model="claude-sonnet-5", max_tokens=10,
                                   restaurant_id=rid, action="draft_response",
                                   system="You write replies.", messages=[{"role": "user", "content": prompt}])
    call = ai_utils.read_call(m._cavnar_call_id)
    assert call["restaurant_id"] == rid and call["action"] == "draft_response"
    assert call["request_id"] == "req_1" and call["stop_reason"] == "end_turn" and call["outcome"] == "ok"
    assert call["template_hash"] and call["prompt_hash"] and call["output_hash"]
    assert call["caller"].endswith("test_a_call_leaves_a_redacted_trace_linked_from_its_ledger_row")
    assert "Ann Smith" not in call["prompt"] and "ann@example.com" not in call["prompt"]
    assert "555-0101" not in call["prompt"] and "The soup was cold." in call["prompt"]
    assert "[system]" in call["prompt"] and call["output"] == "Sorry, [name].", \
        "the guest's name the prompt carried is redacted in the reply too"
    (usage,) = _q(db_path, "SELECT call_id FROM ai_usage WHERE action='draft_response'")
    assert usage["call_id"] == m._cavnar_call_id


def test_a_reviewers_fenced_name_is_redacted_in_the_prompt_and_the_reply(db_path):
    """The drafter fences the reviewer's name on its own and the reply
    greets them by it; neither is kept."""
    from ai_guard import wrap_untrusted
    rid = _rid(db_path)
    prompt = ("Address the reviewer by the first name given in the next block:\n" + wrap_untrusted("Ann Smith")
              + "\nReview:\n" + wrap_untrusted("Cold soup."))
    m = ai_utils.create_with_retry(_Client("Hi Ann, we're sorry the soup was cold."), model="claude-sonnet-5",
                                   max_tokens=10, restaurant_id=rid, action="draft_response",
                                   messages=[{"role": "user", "content": prompt}])
    call = ai_utils.read_call(m._cavnar_call_id)
    assert "Ann" not in call["prompt"] and "Cold soup." in call["prompt"]
    assert call["output"] == "Hi [name], we're sorry the soup was cold."


def test_only_the_newest_calls_keep_their_text(db_path, monkeypatch):
    rid = _rid(db_path)
    monkeypatch.setattr(ai_utils, "AI_TRACE_KEEP_PER_ACTION", 3)
    for i in range(6):
        ai_utils.create_with_retry(_Client(f"answer {i}"), model="claude-sonnet-5", max_tokens=10,
                                   restaurant_id=rid, action="t_keep", messages=[{"role": "user", "content": str(i)}])
    rows = _q(db_path, "SELECT prompt_z IS NOT NULL AS kept FROM ai_calls WHERE action='t_keep' ORDER BY rowid")
    assert [r["kept"] for r in rows] == [0, 0, 0, 1, 1, 1]


def test_a_validation_verdict_and_a_stored_read_link_to_the_call_that_wrote_them(db_path):
    import insight_store
    import response_validation as rv
    rid = _rid(db_path)
    m = ai_utils.create_with_retry(_Client("Reviews were steady."), model="claude-sonnet-5", max_tokens=10,
                                   restaurant_id=rid, action="review_insight")
    v = rv.validate("Reviews were steady.", rv.ValidationContext(restaurant_id=rid, surface="review_insight"))
    rv.log(v, rv.ValidationContext(restaurant_id=rid, surface="review_insight"))
    insight_store.put(rid, "reviews", "fp1", {"insight": "Reviews were steady."})
    assert _q(db_path, "SELECT call_id FROM ai_validation_log")[0]["call_id"] == m._cavnar_call_id
    assert _q(db_path, "SELECT call_id FROM insight_cache WHERE kind='reviews'")[0]["call_id"] == m._cavnar_call_id


def test_a_verdict_is_not_linked_to_an_unrelated_call(db_path):
    import response_validation as rv
    rid = _rid(db_path)
    ai_utils.create_with_retry(_Client("x"), model="claude-sonnet-5", max_tokens=10, restaurant_id=rid,
                               action="review_insight")
    ctx = rv.ValidationContext(restaurant_id=rid, surface="labor_insight")
    rv.log(rv.validate("Labor was fine.", ctx), ctx)
    assert _q(db_path, "SELECT call_id FROM ai_validation_log")[0]["call_id"] is None


def test_asks_meta_is_kept_on_its_final_call(db_path, monkeypatch):
    import ask_cavnar
    rid = _rid(db_path)
    r = models.get_restaurant(rid, db_path=db_path)

    def inner(restaurant, question, **kw):
        m = ai_utils.create_with_retry(_Client("Labor ran 31%."), model="claude-sonnet-5", max_tokens=10,
                                       restaurant_id=restaurant.id, action=kw.get("action", "ask_cavnar"))
        return "Labor ran 31%.", False, [], {"tools_used": ["read_labor"], "depth": "brief", "confidence": "medium",
                                             "validation": {"verdict": "pass", "codes": []}}
    wrapped = ask_cavnar._ai_turn(inner)
    answer, _t, _p, meta = wrapped(r, "How is labor?", action="ask_cavnar")
    assert meta["call_id"] and meta["turn_id"].startswith("ask:")
    call = ai_utils.read_call(meta["call_id"])
    assert call["meta"]["tools_used"] == ["read_labor"] and call["meta"]["validation"]["verdict"] == "pass"
    assert call["correlation_id"] == meta["turn_id"]


# ── #70 the rollup ──────────────────────────────────────────────────────────

def test_the_rollup_keeps_history_past_the_prune(db_path):
    rid = _rid(db_path)
    for lat in (100, 200, 300, 400):
        ai_utils.log_ai_usage(rid, "draft_response", "claude-sonnet-5", 1000, 100, latency_ms=lat)
    ai_utils.log_blocked(rid, "draft_response", "claude-sonnet-5", "budget")
    conn = sqlite3.connect(db_path)
    conn.execute("UPDATE ai_usage SET created_at='2026-01-10 12:00:00'")
    conn.commit()
    conn.close()
    models.prune_operational_logs(db_path=db_path)
    assert _q(db_path, "SELECT COUNT(*) AS n FROM ai_usage")[0]["n"] == 0, "the raw rows are pruned"
    (day,) = _q(db_path, "SELECT * FROM ai_usage_daily WHERE day='2026-01-10'")
    assert (day["calls"], day["n_ok"], day["n_blocked"], day["restaurant_id"]) == (5, 4, 1, rid)
    assert (day["p50_ms"], day["p95_ms"], day["max_ms"]) == (200, 400, 400)
    assert day["cost_usd"] == pytest.approx(4 * ai_utils._estimate_cost("claude-sonnet-5", 1000, 100))
    assert day["vendor"] == "anthropic"


def test_the_rollup_is_idempotent_and_rolls_validation_verdicts_too(db_path):
    rid = _rid(db_path)
    ai_utils.log_ai_usage(rid, "t", "claude-sonnet-5", 10, 10)
    ai_utils.log_validation(rid, "labor_insight", "labor_insight", "withhold", rules=["F1"])
    ai_utils.rollup_usage()
    ai_utils.rollup_usage()
    assert _q(db_path, "SELECT SUM(calls) AS n FROM ai_usage_daily")[0]["n"] == 1
    (v,) = _q(db_path, "SELECT surface, n, n_withhold, rules_json FROM ai_validation_daily")
    assert (v["surface"], v["n"], v["n_withhold"], json.loads(v["rules_json"])) == ("labor_insight", 1, 1, {"F1": 1})


# ── #140 anomalies and fallbacks ───────────────────────────────────────────

def test_a_moderate_runaway_trips_the_cost_and_rate_rules(db_path, monkeypatch):
    import admin_ops
    # admin_ops binds get_conn at import (CLAUDE.md's bound-import hazard).
    real = sqlite3.connect
    monkeypatch.setattr(admin_ops, "get_conn", lambda *a, **k: _row_conn(real(db_path)))
    rid = _rid(db_path)
    conn = sqlite3.connect(db_path)
    for d in range(1, 15):                  # a quiet baseline
        conn.execute("INSERT INTO ai_usage (restaurant_id, action, model, cost_usd, created_at, outcome, vendor) "
                     "VALUES (?, 'review_fetch', 'google-places-details', 0.07, datetime('now', ?), 'ok', 'google_places')",
                     (rid, f"-{d} days"))
    for _ in range(30):                     # today: a geocode loop, $0.51 of it
        conn.execute("INSERT INTO ai_usage (restaurant_id, action, model, cost_usd, created_at, outcome, vendor) "
                     "VALUES (?, 'weather_geocode', 'google-places-details', 0.017, datetime('now'), 'ok', 'google_places')",
                     (rid,))
    conn.execute("INSERT INTO ai_usage (restaurant_id, action, model, cost_usd, created_at, outcome, vendor) "
                 "VALUES (?, 'labor_schedule', 'claude-sonnet-5', 2.5, datetime('now'), 'ok', 'anthropic')", (rid,))
    conn.commit()
    conn.close()
    kinds = {(a["kind"], a.get("action")) for a in admin_ops.ai_anomalies(days=1)}
    assert ("rate", "weather_geocode") in kinds, "19-a-day loops tripped nothing"
    assert ("cost", None) in kinds


def test_an_insight_route_fallback_is_captured_and_recorded(db_path, monkeypatch):
    import client_api
    captured = []
    monkeypatch.setattr(ops, "capture", lambda e, **k: captured.append(k))
    rid = _rid(db_path)
    client_api._record_insight_fallback("review_insight", rid, KeyError("payload"))
    client_api._record_insight_fallback("review_insight", rid, ai_utils.AIBudgetExceeded("paused"))
    assert len(captured) == 1, "a budget stop is not a code failure"
    rows = _q(db_path, "SELECT surface, kind FROM ai_quality_events")
    assert rows == [{"surface": "review_insight", "kind": "fallback"}] * 2


def test_a_backfill_over_different_items_is_a_batch_and_a_repeat_is_a_loop(db_path, monkeypatch):
    """Owner, 10/9/26: Simple EJ's read Critical for "review_analysis ran
    207x" - one run per review of a newly connected listing. 200+ runs over
    different subjects is a batch (said, never an issue); over the same
    few subjects it is still a loop."""
    import admin_ops
    real = sqlite3.connect
    monkeypatch.setattr(admin_ops, "get_conn", lambda *a, **k: _row_conn(real(db_path)))
    rid = _rid(db_path)
    conn = sqlite3.connect(db_path)
    for action, subject in (("review_analysis", lambda i: f"review:{i}"), ("labor_insight", lambda i: "week")):
        for i in range(210):
            conn.execute("INSERT INTO ai_usage (restaurant_id, action, model, cost_usd, created_at, outcome, vendor) "
                         "VALUES (?, ?, 'claude-haiku', 0.001, datetime('now'), 'ok', 'anthropic')", (rid, action))
            conn.execute("INSERT INTO ai_runs (run_id, created_at, restaurant_id, workflow, subject) "
                         "VALUES (?, datetime('now'), ?, ?, ?)", (f"{action}-{i}", rid, action, subject(i)))
    conn.commit()
    conn.close()
    got = {(a["kind"], a.get("action")) for a in admin_ops.ai_anomalies(days=1)}
    assert ("batch", "review_analysis") in got and ("loop", "review_analysis") not in got
    assert ("loop", "labor_insight") in got
    assert not [i for i in admin_ops.ai_anomaly_issues([a for a in admin_ops.ai_anomalies(days=1)
                                                         if a["kind"] == "batch"])]
