"""Fix round G — the console's AI data: the AI page (#48, #68, #122, #140),
the AI Quality panel (#69), a client's AI (#48, #122, #124), the call trace
(#117), the breaker reset (#104) and Retry AI (#124)."""
import sqlite3

import pytest
from flask import Flask

import admin_routes
import ai_utils
import auth
import models
from admin_routes import admin_bp
from auth import init_auth
from auth_routes import auth_bp
from models import Restaurant, create_restaurant


@pytest.fixture(autouse=True)
def _db(db_path, monkeypatch):
    init_auth(db_path=db_path)
    real = models.get_conn
    redirect = lambda *a, **k: real(db_path)  # noqa: E731
    import admin_ops
    # admin_ops binds get_conn at import (CLAUDE.md's bound-import hazard).
    for mod in (models, auth, admin_routes, admin_ops):
        monkeypatch.setattr(mod, "get_conn", redirect)
    monkeypatch.setattr(models, "DB_PATH", db_path)
    ai_utils.reset_process_state(db_path)
    yield
    ai_utils.reset_process_state(db_path)


@pytest.fixture
def client():
    app = Flask(__name__, template_folder="../templates")
    app.register_blueprint(admin_bp)
    app.register_blueprint(auth_bp)
    return app.test_client()


def _as(monkeypatch, admin=True):
    user = {"id": 999, "username": "will", "is_admin": 1 if admin else 0, "role": "admin" if admin else "support",
            "restaurant_id": 1}
    monkeypatch.setattr(auth, "get_current_user", lambda: user)
    monkeypatch.setattr(auth, "_admin_two_factor_missing", lambda u: False, raising=False)
    return user


def _rid(db_path, billing_status="active", name="Console Co"):
    rid = create_restaurant(Restaurant(name=name, owner_email=f"{name.split()[0].lower()}@x.test"), db_path=db_path)
    conn = sqlite3.connect(db_path)
    conn.execute("UPDATE restaurants SET billing_status=? WHERE id=?", (billing_status, rid))
    conn.commit()
    conn.close()
    return rid


def test_the_ai_page_splits_vendors_outcomes_and_blocked_calls(client, monkeypatch, db_path):
    _as(monkeypatch)
    rid = _rid(db_path)
    ai_utils.log_ai_usage(rid, "draft_response", "claude-sonnet-5", 1000, 100, latency_ms=900)
    ai_utils.log_ai_usage(rid, "draft_response", "claude-sonnet-5", 1000, 100, outcome="truncated",
                          stop_reason="max_tokens", latency_ms=1200)
    ai_utils.log_api_call(rid, "review_fetch", "google-places-details")
    ai_utils.log_blocked(rid, "draft_response", "claude-sonnet-5", "budget", detail="daily budget")
    out = client.get("/admin/api/ai?days=30").get_json()
    assert out["ok"] and out["window_tz"] == "UTC" and out["labels"]["cost_card"] == "AI & data APIs"
    providers = {p["provider"] for p in out["by_provider"]}
    assert providers == {"Claude", "Google Places"}, "Places was counted as Claude"
    assert out["totals"]["data_api_cost"] == pytest.approx(0.02, abs=0.01)
    assert out["outcomes"]["truncated"] == 1 and out["outcomes"]["blocked"] == 1
    assert out["blocked"][0]["reason"] == "budget"
    row = next(a for a in out["by_action"] if a["action"] == "draft_response")
    assert row["calls"] == 2 and row["outcomes"]["blocked"] == 1 and row["p95_ms"] == 1200
    assert "breakers" not in out["health"] and "anthropic" in out["health"]["vendors"]
    assert out["budget"]["limits"]["trial"]["day"] == ai_utils.AI_TRIAL_DAILY_BUDGET_USD


def test_the_ai_page_names_the_latest_error_not_the_alphabetically_greatest(client, monkeypatch, db_path):
    _as(monkeypatch)
    ai_utils.log_ai_usage(None, "t", "claude-sonnet-5", 0, 0, status="error", error="HTTP 429 rate limited")
    ai_utils.log_ai_usage(None, "t", "claude-sonnet-5", 0, 0, status="error", error="AuthenticationError 401")
    out = client.get("/admin/api/ai").get_json()
    (f,) = [f for f in out["failures"] if f["job"] == "t"]
    assert f["sample"].startswith("AuthenticationError") and f["n"] == 2


def test_budget_watch_flags_a_client_at_eighty_percent_of_its_own_ceiling(client, monkeypatch, db_path):
    _as(monkeypatch)
    trial = _rid(db_path, "trial", name="Trial Co")
    ai_utils.log_ai_usage(trial, "labor_schedule", "claude-sonnet-5", 0, int(4.1 / 10 * 1_000_000))
    out = client.get("/admin/api/ai").get_json()
    (w,) = [w for w in out["budget_watch"] if w["restaurant_id"] == trial]
    assert (w["tier"], w["scope"], w["over"]) == ("trial", "ai_day", False) and w["pct"] >= 80


def test_a_clients_ai_shows_its_ceilings_blocked_calls_and_stalled_reviews(client, monkeypatch, db_path):
    _as(monkeypatch)
    rid = _rid(db_path, "trial")
    ai_utils.log_ai_usage(rid, "ask_cavnar", "claude-sonnet-5", 0, int(4.5 / 10 * 1_000_000))
    ai_utils.log_blocked(rid, "ask_cavnar", "claude-sonnet-5", "budget", detail="daily budget for trial accounts")
    conn = sqlite3.connect(db_path)
    conn.execute("INSERT INTO reviews (restaurant_id, platform, external_id, rating, text, fetched_at, processed, "
                 "analysis_attempts) VALUES (?, 'google', 'x1', 2, 'cold', datetime('now'), 0, 5)", (rid,))
    conn.commit()
    conn.close()
    out = client.get(f"/admin/api/ai/client/{rid}").get_json()
    assert out["budget"]["tier"] == "trial"
    assert out["budget"]["ai"]["day"]["budget"] == ai_utils.AI_TRIAL_DAILY_BUDGET_USD
    assert [w["scope"] for w in out["budget"]["warnings"]] == ["ai_day"]
    assert out["blocked"][0]["reason"] == "budget"
    assert out["stalled_reviews"]["unanalysed"] == 1 and out["stalled_total"] == 1
    assert "validation" in out["quality"]


def test_retry_ai_puts_stalled_reviews_back_and_is_audited(client, monkeypatch, db_path):
    _as(monkeypatch)
    rid = _rid(db_path)
    conn = sqlite3.connect(db_path)
    conn.execute("INSERT INTO reviews (restaurant_id, platform, external_id, rating, text, fetched_at, processed, "
                 "analysis_attempts) VALUES (?, 'google', 'a', 2, 'cold', datetime('now'), 0, 5)", (rid,))
    conn.execute("INSERT INTO reviews (restaurant_id, platform, external_id, rating, text, fetched_at, processed, "
                 "response_status, draft_attempts) VALUES (?, 'google', 'b', 1, 'rude', datetime('now'), 1, "
                 "'pending', 7)", (rid,))
    conn.execute("INSERT INTO reviews (restaurant_id, platform, external_id, rating, text, fetched_at, processed, "
                 "analysis_attempts) VALUES (?, 'google', 'c', 3, 'fine', datetime('now'), 0, 2)", (rid,))
    conn.commit()
    conn.close()
    out = client.post(f"/admin/api/client/{rid}/retry-ai").get_json()
    assert out["ok"] and out["reset"] == {"analysis": 1, "draft": 1}
    assert models.count_stalled_reviews(rid, db_path=db_path) == {"unanalysed": 0, "undrafted": 0}
    assert len(models.get_pending_analysis(rid, db_path=db_path)) == 2
    conn = sqlite3.connect(db_path)
    attempts = dict(conn.execute("SELECT external_id, analysis_attempts FROM reviews WHERE processed=0").fetchall())
    audit = conn.execute("SELECT event_type FROM admin_events WHERE event_type='ai.retry_stalled'").fetchall()
    conn.close()
    assert attempts == {"a": 0, "c": 2}, "a review still under the ceiling keeps its count"
    assert audit


def test_support_can_read_the_ai_pages_but_not_the_prompts_or_writes(client, monkeypatch, db_path):
    _as(monkeypatch, admin=False)
    rid = _rid(db_path)
    assert client.get("/admin/api/ai/quality").status_code == 200
    assert client.get(f"/admin/api/ai/client/{rid}").status_code == 200
    assert client.get("/admin/api/ai/calls/0123456789abcdef").status_code == 403
    assert client.post(f"/admin/api/client/{rid}/retry-ai").status_code == 403
    assert client.post("/admin/api/ai/reset-breaker", json={}).status_code == 403


def test_the_call_trace_lists_and_opens_a_call_with_what_is_linked_to_it(client, monkeypatch, db_path):
    import types
    _as(monkeypatch)
    rid = _rid(db_path)

    class _U:
        input_tokens, output_tokens = 10, 10
        cache_creation_input_tokens = cache_read_input_tokens = 0

    class _C:
        def __init__(self):
            self.messages = self

        def create(self, **kw):
            return types.SimpleNamespace(usage=_U(), stop_reason="end_turn", _request_id="req_9",
                                         content=[types.SimpleNamespace(type="text", text="hi")])
    with ai_utils.ai_context(correlation_id="ask:turn1"):
        m1 = ai_utils.create_with_retry(_C(), model="claude-sonnet-5", max_tokens=5, restaurant_id=rid,
                                        action="ask_cavnar", messages=[{"role": "user", "content": "q"}])
        m2 = ai_utils.create_with_retry(_C(), model="claude-sonnet-5", max_tokens=5, restaurant_id=rid,
                                        action="ask_cavnar", messages=[{"role": "user", "content": "q2"}])
    listed = client.get(f"/admin/api/ai/calls?restaurant_id={rid}").get_json()["calls"]
    assert [c["call_id"] for c in listed] == [m2._cavnar_call_id, m1._cavnar_call_id]
    assert "prompt" not in listed[0]
    detail = client.get(f"/admin/api/ai/calls/{m2._cavnar_call_id}").get_json()
    assert detail["call"]["prompt"].endswith("q2") and detail["usage"][0]["request_id"] == "req_9"
    assert [r["call_id"] for r in detail["related"]] == [m1._cavnar_call_id]
    assert client.get("/admin/api/ai/calls/not-an-id").status_code == 404


def test_the_quality_panel_shows_modes_models_and_rates(client, monkeypatch, db_path):
    _as(monkeypatch)
    rid = _rid(db_path)
    ai_utils.record_quality_event("weekly_digest", "line_dropped", restaurant_id=rid, detail="x")
    ai_utils.log_validation(rid, "labor_insight", "labor_insight", "withhold", rules=["F1"])
    out = client.get("/admin/api/ai/quality?days=7").get_json()
    import response_validation as rv
    assert {m["surface"] for m in out["validation"]["modes"]} == set(rv.SURFACES)
    assert out["validation"]["surfaces"][0]["surface"] == "labor_insight"
    assert any(m["purpose"] == "drafter" for m in out["models"])
    assert out["events"][0]["surface"] == "weekly_digest" and out["events"][0]["kind"] == "line_dropped"
    assert set(out) >= {"drafts", "ask", "safety", "event_trend", "unusable_outputs"}


def test_the_breaker_reset_route_closes_it_and_is_audited(client, monkeypatch, db_path):
    _as(monkeypatch)
    ai_utils.trip_breaker("google_places", "auth")
    out = client.post("/admin/api/ai/reset-breaker", json={"provider": "google_places"}).get_json()
    assert out["ok"] and out["breakers"]["google_places"]["state"] == "closed"
    assert client.post("/admin/api/ai/reset-breaker", json={"provider": "nope"}).status_code == 400
    conn = sqlite3.connect(db_path)
    assert conn.execute("SELECT COUNT(*) FROM admin_events WHERE event_type='ai.breaker_reset'").fetchone()[0] == 1
    conn.close()


def test_health_route_returns_ai_health(client, monkeypatch, db_path):
    _as(monkeypatch)
    out = client.get("/admin/api/ai/health").get_json()
    assert out["ok"] and set(out["vendors"]) == {"anthropic", "perplexity", "google_places"}
    assert out["status"] in ("operational", "degraded", "outage")
