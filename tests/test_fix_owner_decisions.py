"""The owner's decisions after the 9/29/26 fix round (Will, 9/29/26).

  * A past-due client keeps the paid AI ceiling while Stripe retries the
    card — it is still a contracted, in-service customer. ai_utils had its
    own "paid" list (active, internal) that disagreed with
    models.PAYING_BILLING_STATES, so past due fell to the $2/day unpaid tier.
    One list now, for the tier, the paying pool's spend and its client count.
  * Every trial together answers to one trial pool, apart from the paying
    pool: each trial had its own ceiling but nothing bounded how many trials
    there are. 80% of it pages, and a stop pages.
"""
import os

import pytest

import admin_ops
import ai_utils
import models
from models import Restaurant, create_restaurant

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@pytest.fixture(autouse=True)
def _db(db_path, monkeypatch):
    real = models.get_conn
    monkeypatch.setattr(models, "get_conn", lambda *a, **k: real(db_path))
    monkeypatch.setattr(models, "DB_PATH", db_path)
    ai_utils.reset_process_state(db_path)
    yield
    ai_utils.reset_process_state(db_path)


def _rid(billing_status, is_demo=0, name="Decision Co"):
    rid = create_restaurant(Restaurant(name=name, owner_email=f"{name.split()[0].lower()}@x.test"))
    conn = models.get_conn()
    conn.execute("UPDATE restaurants SET billing_status=?, is_demo=? WHERE id=?", (billing_status, is_demo, rid))
    conn.commit()
    conn.close()
    ai_utils._budget_cache.clear()
    return rid


def _spend(rid, dollars, trigger=None, keep_cache=False):
    if trigger:
        with ai_utils.ai_context(trigger=trigger):
            ai_utils.log_ai_usage(rid, "t", "claude-sonnet-5", 0, 0, cost_usd=dollars)
    else:
        ai_utils.log_ai_usage(rid, "t", "claude-sonnet-5", 0, 0, cost_usd=dollars)
    if not keep_cache:
        ai_utils._budget_cache.clear()


# ── past due keeps the paid ceiling ──────────────────────────────────────────

def test_the_ai_paid_list_is_the_one_paying_list_plus_internal():
    assert ai_utils._PAID_BILLING_STATES == frozenset(models.PAYING_BILLING_STATES) | {"internal"}
    assert "'past_due'" in ai_utils._PAID_STATES_SQL


def test_a_past_due_client_keeps_the_paid_ai_ceiling():
    rid = _rid("past_due")
    st = ai_utils.ai_budget_status(rid)
    assert (st["tier"], st["paid"]) == ("paid", True)
    assert (st["day"]["budget"], st["month"]["budget"]) == (ai_utils.AI_DAILY_BUDGET_USD,
                                                            ai_utils.AI_MONTHLY_BUDGET_USD)
    _spend(rid, 4.0)            # past the $2 unpaid and $5 trial days, well inside the paid one
    assert ai_utils.ai_budget_exceeded(rid) is None
    assert "trial_pool_day" not in ai_utils.ai_budget_status(rid)


def test_past_due_spend_draws_on_the_paying_pool_and_is_counted_in_it():
    before = ai_utils._paying_client_count()
    month = ai_utils._current_windows()[1]
    pool_before = ai_utils._spend_since(month, paid_only=True)
    rid = _rid("past_due")
    assert ai_utils._paying_client_count() == before + 1
    _spend(rid, 3.0)
    assert ai_utils._spend_since(month, paid_only=True) == pytest.approx(pool_before + 3.0)


def test_the_console_puts_a_past_due_client_on_the_paid_ceiling():
    rid = _rid("past_due", name="Late Co")
    _spend(rid, 0.85 * ai_utils.AI_DAILY_BUDGET_USD)
    conn = models.get_conn()
    try:
        (w,) = [w for w in admin_ops.budget_watch(conn) if w["restaurant_id"] == rid and w["scope"] == "ai_day"]
    finally:
        conn.close()
    assert (w["tier"], w["budget"], w["over"]) == ("paid", ai_utils.AI_DAILY_BUDGET_USD, False)


# ── one pool for every trial together ───────────────────────────────────────

def test_every_trial_together_answers_to_the_trial_pool(monkeypatch):
    monkeypatch.setattr(ai_utils, "AI_TRIAL_POOL_DAILY_USD", 6.0)
    a = _rid("trial", name="Alpha Trial")
    b = _rid("trial", name="Beta Trial")
    demo = _rid("trial", is_demo=1, name="Demo Co")
    paying = _rid("active", name="Paying Co")
    _spend(a, 3.5)                       # each trial well inside its own $5 day
    _spend(demo, 1.5)                    # a demo is not a trial: not in the pool
    _spend(paying, 4.0)                  # a paying client never is
    _spend(b, 4.0, trigger="admin")      # an operator's run spends no client ceiling, the pool included
    assert ai_utils.ai_budget_exceeded(b) is None
    _spend(b, 2.6)                       # 3.5 + 2.6: the trials together pass $6
    pool_day = ai_utils._TRIAL_POOL_SCOPES["trial_pool_day"]
    assert ai_utils.ai_budget_exceeded(b) == pool_day
    assert ai_utils.ai_budget_exceeded(a) == pool_day
    assert ai_utils.ai_budget_exceeded(b, trigger="admin") is None
    assert ai_utils.ai_budget_exceeded(paying) is None and ai_utils.ai_budget_exceeded(demo) is None
    st = ai_utils.ai_budget_status(a)
    assert st["trial_pool_day"]["spend"] == pytest.approx(6.1) and st["trial_pool_day"]["over"]
    assert st["day"]["spend"] == pytest.approx(3.5) and not st["day"]["over"]
    assert "trial_pool_day" not in ai_utils.ai_budget_status(paying)
    assert ai_utils.ai_budget_status()["trial_pool_day"]["spend"] == pytest.approx(6.1)


def test_a_burst_inside_the_cache_window_still_trips_the_trial_pool(monkeypatch):
    monkeypatch.setattr(ai_utils, "AI_TRIAL_POOL_DAILY_USD", 6.0)
    a = _rid("trial", name="Alpha Trial")
    b = _rid("trial", name="Beta Trial")
    _spend(a, 3.5)
    assert ai_utils.ai_budget_exceeded(a) is None          # the pool total is cached now
    _spend(b, 2.6, keep_cache=True)                        # no re-read: note_ai_spend must move it
    assert ai_utils.ai_budget_exceeded(b) == ai_utils._TRIAL_POOL_SCOPES["trial_pool_day"]


def test_the_trial_pool_pages_at_eighty_percent_and_when_it_stops(monkeypatch):
    pages = []
    monkeypatch.setattr(ai_utils, "_page", lambda key, subject, lines: pages.append((key, subject)))
    monkeypatch.setattr(ai_utils, "AI_TRIAL_POOL_DAILY_USD", 6.0)
    a = _rid("trial", name="Alpha Trial")
    _spend(a, 4.9)                                         # 82% of the pool (and 98% of its own day)
    ai_utils._note_budget_warnings(ai_utils.ai_budget_status(a), a)
    assert ("budget_warn:trial_pool:anthropic" in [k for k, _ in pages])
    ai_utils._record_budget_stop(ai_utils._TRIAL_POOL_SCOPES["trial_pool_day"], a)
    assert any(k == "budget_stop:trial_pool:anthropic" and "every trial" in s for k, s in pages)


def test_the_console_shows_the_trial_pool():
    lim = admin_ops._budget_limits()
    assert lim["trial_pool"] == {"day": ai_utils.AI_TRIAL_POOL_DAILY_USD, "month": ai_utils.AI_TRIAL_POOL_MONTHLY_USD}
    assert (ai_utils.AI_TRIAL_POOL_DAILY_USD, ai_utils.AI_TRIAL_POOL_MONTHLY_USD) == (50.0, 500.0)
    page = open(os.path.join(ROOT, "templates", "admin.html")).read()
    assert "meter('All trials · AI today', b.trial_pool_day)" in page
    assert "meter('All trials · AI this month', b.trial_pool_month)" in page
