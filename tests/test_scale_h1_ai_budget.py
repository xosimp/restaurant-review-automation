"""Scheduler audit 10/10/26, workstream H1 — SCALE-11: the absolute $10,000
fleet AI cap is gone. Each tenant's own ceilings bind its spend, a workflow
of one restaurant that runs far past its own hourly rate is paused within
the hour (and nothing else is), the fleet's rate pages, the emergency ceiling
grows with the paying restaurants, and AI_EMERGENCY_STOP is the manual stop."""
import pytest

import ai_utils
import models
from ai_utils import AIBudgetExceeded, create_with_retry, log_ai_usage


@pytest.fixture(autouse=True)
def _isolate(db_path, monkeypatch):
    real = models.get_conn
    monkeypatch.setattr(models, "get_conn", lambda *a, **k: real(db_path))
    monkeypatch.setattr(models, "DB_PATH", db_path)
    monkeypatch.setattr(ai_utils, "_page", lambda *a, **k: None)
    ai_utils._budget_cache.clear()
    ai_utils._rate_cache.clear()
    yield
    ai_utils._budget_cache.clear()
    ai_utils._rate_cache.clear()


class _Usage:
    input_tokens = 100
    output_tokens = 100


class _Message:
    usage = _Usage()
    content = []


def _client(calls):
    class _Messages:
        def create(self, **kwargs):
            calls.append(kwargs)
            return _Message()

    class _Client:
        messages = _Messages()
    return _Client()


def _paying(db_path, *rids):
    conn = models.get_conn(db_path)
    for rid in rids:
        conn.execute("INSERT OR IGNORE INTO restaurants (id, name, owner_email, billing_status) "
                     "VALUES (?,?,?,'active')", (rid, f"Client {rid}", f"c{rid}@example.test"))
    conn.commit()
    conn.close()
    ai_utils.invalidate_budget_memo()


def _spend(rid, action, dollars, ago_minutes=5):
    out_rate = ai_utils._MODEL_PRICING["claude-sonnet-5"][1]
    row = log_ai_usage(rid, action, "claude-sonnet-5", 0, int(dollars / out_rate * 1_000_000))
    conn = models.get_conn()
    conn.execute("UPDATE ai_usage SET created_at=datetime('now', ?) WHERE id=?", (f"-{int(ago_minutes)} minutes", row))
    conn.commit()
    conn.close()
    ai_utils._budget_cache.clear()
    ai_utils._rate_cache.clear()


def _call(rid, action):
    calls = []
    create_with_retry(_client(calls), restaurant_id=rid, action=action, model="claude-sonnet-5",
                      max_tokens=10, messages=[{"role": "user", "content": "x"}])
    return calls


def test_a_runaway_workflow_is_paused_for_its_restaurant_alone(db_path, monkeypatch):
    monkeypatch.setattr(ai_utils, "AI_DAILY_BUDGET_USD", 1000.0)
    monkeypatch.setattr(ai_utils, "AI_MONTHLY_BUDGET_USD", 10000.0)
    _paying(db_path, 1, 2)
    for _ in range(4):
        _spend(1, "review_reply", 3.0)                       # $12 in the last hour; it usually spends nothing
    assert ai_utils.runaway_refusal(1, "review_reply") == "unusual spend on review_reply in the last hour"
    with pytest.raises(AIBudgetExceeded, match="paused"):
        _call(1, "review_reply")
    # The same restaurant's other workflows, and every other restaurant, go on.
    assert _call(1, "labor_insight")
    assert _call(2, "review_reply")
    # An admin's run answers to the global pool only.
    assert ai_utils.runaway_refusal(1, "review_reply", trigger="admin") is None


def test_a_workflows_own_usual_rate_and_run_cap_set_its_line(db_path):
    _paying(db_path, 3)
    # A generation's run cap is $6: four runs in an hour is not a runaway.
    assert ai_utils.runaway_threshold(3, "labor_schedule") >= 4 * 6.0
    _spend(3, "labor_schedule", 20.0)
    assert ai_utils.runaway_refusal(3, "labor_schedule") is None
    # A restaurant that usually spends $10 an hour on a workflow: $40 is not
    # a runaway, $70 is (six times its own rate).
    for d in range(1, 7):
        _spend(3, "ask_cavnar", 240.0, ago_minutes=d * 24 * 60)      # $1,440 a week = ~$8.6/hour
    rate = ai_utils._baseline_hourly(3, "ask_cavnar")
    assert 8 < rate < 9
    _spend(3, "ask_cavnar", 40.0)
    assert ai_utils.runaway_refusal(3, "ask_cavnar") is None
    _spend(3, "ask_cavnar", 30.0)
    assert ai_utils.runaway_refusal(3, "ask_cavnar")


def test_the_emergency_stop_refuses_every_call(db_path, monkeypatch):
    _paying(db_path, 4)
    # Read at call time (re-audit 10/20/26 R4-02), not at import.
    monkeypatch.setenv("AI_EMERGENCY_STOP", "1")
    with pytest.raises(AIBudgetExceeded, match="everyone"):
        _call(4, "review_reply")
    assert ai_utils.runaway_refusal(None, "anything") == "emergency stop"


def test_normal_use_at_a_thousand_restaurants_never_meets_the_fleet_ceiling(db_path, monkeypatch):
    """At $10,000 flat, a thousand paying restaurants spending a normal $20
    each ($20,000) stopped every client's AI mid-month."""
    monkeypatch.setattr(ai_utils, "_paying_client_count", lambda db_path=None: 1000)
    monkeypatch.setattr(ai_utils, "AI_GLOBAL_MAX_MONTHLY_BUDGET_USD", 0.0)
    assert ai_utils.global_monthly_budget() >= 1000 * ai_utils.AI_MONTHLY_BUDGET_USD
    assert ai_utils.global_monthly_budget() > 1000 * 20.0


def test_the_fleet_rate_far_past_its_own_pages_once_an_hour(db_path, monkeypatch):
    pages = []
    monkeypatch.setattr(ai_utils, "_page", lambda key, subject, lines: pages.append(key))
    _paying(db_path, 5, 6)
    for d in range(1, 8):
        _spend(5, "ask_cavnar", 24.0, ago_minutes=d * 24 * 60)          # ~$1 an hour usually
    assert ai_utils.check_fleet_rate()["over"] is False
    for _ in range(6):
        _spend(6, "review_reply", 15.0)                                   # $90 this hour
    out = ai_utils.check_fleet_rate()
    assert out["over"] is True and out["top"][0]["restaurant_id"] == 6
    ai_utils._rate_cache.clear()
    ai_utils.check_fleet_rate()
    assert pages.count("fleet_runaway") == 1
    assert "rate" in ai_utils.ai_health()
