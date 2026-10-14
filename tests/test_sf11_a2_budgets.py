"""Schedule fix round 10/11/26, workstream A2 — routing, budgets and the
model door (blind re-audit AI-19, AI-20, AI-26, AI-27, SCALE-25, SCALE-30,
STRESS-23). No model is called here."""
import sys
import types
from datetime import datetime, timezone

import pytest

import ai_utils
import ai_workflows as wf
import models
import schedule_engine as se
from models import Restaurant, create_restaurant

WEEK = ["2026-11-23", "2026-11-24", "2026-11-25", "2026-11-26", "2026-11-27", "2026-11-28", "2026-11-29"]


@pytest.fixture
def db(db_path, monkeypatch):
    real = models.get_conn

    def conn(*a, **k):
        return real(db_path)
    for mod in list(sys.modules.values()):
        bound = getattr(mod, "get_conn", None) if mod is not None else None
        if bound is real:
            monkeypatch.setattr(mod, "get_conn", conn)
    monkeypatch.setattr(models, "get_conn", conn)
    monkeypatch.setattr(models, "DB_PATH", db_path)
    for k in ("GATEWAY_ORG_OTPM", "GATEWAY_ORG_ITPM", "GATEWAY_TENANT_OTPM"):
        monkeypatch.delenv(k, raising=False)
    ai_utils.reset_process_state(db_path)
    yield db_path
    ai_utils.reset_process_state(db_path)


def _rid(billing_status, name="Trial Grill"):
    rid = create_restaurant(Restaurant(name=name, owner_email=f"{name.split()[0].lower()}@x.test"))
    conn = models.get_conn()
    conn.execute("UPDATE restaurants SET billing_status=? WHERE id=?", (billing_status, rid))
    conn.commit()
    conn.close()
    ai_utils._budget_cache.clear()
    ai_utils.invalidate_budget_memo()
    return rid


def _spend(rid, dollars, action):
    ai_utils.log_ai_usage(rid, action, "claude-sonnet-5-5", 0, 0, cost_usd=dollars)
    ai_utils._budget_cache.clear()


def _over(rid, action):
    token = ai_utils._BUDGET_ACTION.set(action)
    try:
        return ai_utils.ai_budget_exceeded(rid)
    finally:
        ai_utils._BUDGET_ACTION.reset(token)


# ── AI-27: a kind of week goes to T4 only on its eval verdict ─────────────

def test_hard_week_signals_describe_and_only_evidence_routes():
    signals = se.hard_week_signals(WEEK, prior_generations=0, last_run_escalated=True, owner_rules=20)
    assert [t for t, _w in signals] == ["holiday", "many_rules", "first_week", "escalated"]   # Thanksgiving
    # Without evidence nothing routes ...
    assert se.hard_week_reasons(WEEK, prior_generations=0, last_run_escalated=True, owner_rules=20,
                                evidence=lambda tag: None) == []
    # ... and only the kinds of week the eval backs do, saying so.
    got = se.hard_week_reasons(WEEK, prior_generations=0, evidence=lambda tag: "proof" if tag == "holiday" else None)
    assert len(got) == 1 and got[0].startswith("holiday in the week: Thanksgiving") and got[0].endswith("— proof")
    # The description (no evidence argument) is unchanged.
    assert "the restaurant's first schedule" in se.hard_week_reasons(WEEK, prior_generations=0)


# ── AI-20: a trial's schedule has its own day ─────────────────────────────

def test_a_trials_second_draft_is_not_paused_by_its_first(db):
    rid = _rid("trial")
    assert ai_utils.trial_schedule_daily_budget() >= wf.POLICIES["labor_schedule"].caps.usd
    _spend(rid, 5.5, "labor_schedule")        # a first-week Opus draft and its gate rewrite
    assert _over(rid, "labor_schedule") is None                 # the next draft still runs
    assert _over(rid, "ask_cavnar") is None                     # and the rest of the trial's AI is untouched
    st = ai_utils.ai_budget_status(rid)
    assert st["schedule_day"]["spend"] == pytest.approx(5.5) and st["day"]["spend"] == 0.0
    _spend(rid, 5.0, "ask_cavnar")
    assert _over(rid, "ask_cavnar") == "daily budget for trial accounts"
    assert _over(rid, "labor_schedule") is None
    _spend(rid, 7.0, "labor_schedule")
    assert _over(rid, "labor_schedule") == "daily schedule budget for trial accounts"


def test_a_paying_restaurant_has_no_separate_schedule_day(db):
    rid = _rid("active", name="Paid Grill")
    assert "schedule_day" not in ai_utils.ai_budget_status(rid)


# ── AI-19: the platform pool pages on its forecast ─────────────────────────

def test_the_month_is_projected_and_the_pool_pages_on_its_forecast(db, monkeypatch):
    assert ai_utils.month_forecast(100.0, now=datetime(2026, 10, 2, tzinfo=timezone.utc)) is None   # too early
    assert ai_utils.month_forecast(100.0, now=datetime(2026, 10, 11, tzinfo=timezone.utc)) == pytest.approx(310.0)
    pages = []
    monkeypatch.setattr(ai_utils, "_page", lambda key, subject, lines: pages.append((key, subject)))
    import ops
    monkeypatch.setattr(ops, "claim_period", lambda *a, **k: True)
    ai_utils._warned_memo.clear()
    status = {"global_month": {"spend": 600.0, "budget": 1500.0, "warn": False, "over": False,
                               "forecast": 1900.0, "forecast_over": True}}
    ai_utils._note_budget_warnings(status, None)
    assert pages and pages[0][0] == "budget_forecast:anthropic" and "projected" in pages[0][1]
    st = ai_utils.ai_budget_status(None)
    assert "forecast" in st["global_month"] and "forecast_over" in st["global_month"]


def test_the_global_ceiling_grows_with_paying_restaurants():
    # The $1,500 is a floor; each paying restaurant adds its own ceiling.
    assert ai_utils.AI_GLOBAL_PER_CLIENT_USD == ai_utils.AI_MONTHLY_BUDGET_USD
    assert ai_utils.AI_GLOBAL_MAX_MONTHLY_BUDGET_USD == 0


# ── AI-26: the schedule's small calls honour the T1 canary ────────────────

def test_the_schedule_small_calls_reach_haiku55_only_on_the_canary(monkeypatch):
    for k in ("SCHEDULE_RULES_MODEL", "SCHEDULE_NOTE_MODEL", "AI_TIER_CANARY", "AI_TIER_T1_PREVIOUS_MODEL"):
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("AI_CANARY_RESTAURANTS", "4")
    assert ai_utils.model_for_run("schedule_rules", 4) == ai_utils.HAIKU_55
    assert ai_utils.model_for_run("schedule_rules", 5) == ai_utils.HAIKU
    assert ai_utils.model_for_run("schedule_note", None) == ai_utils.HAIKU
    import owner_rules
    import schedule_narrative
    assert owner_rules.build_request(5, ["Ana closes Friday"], ["Ana"], ["Server"])["model"] == ai_utils.HAIKU
    assert schedule_narrative.build_request(4, [])["model"] == ai_utils.HAIKU_55
    assert wf.route_for(wf.POLICIES["schedule_note"], 0, restaurant_id=5).model == ai_utils.HAIKU
    monkeypatch.setenv("AI_TIER_CANARY", "")                       # the canary is over: everyone moves
    assert ai_utils.model_for_run("schedule_rules", 5) == ai_utils.HAIKU_55
    monkeypatch.setenv("SCHEDULE_RULES_MODEL", "claude-x")         # an explicit pin is the operator's
    assert ai_utils.model_for_run("schedule_rules", 5) == "claude-x"


# ── SCALE-30 / STRESS-23: no key, no call ─────────────────────────────────

def test_with_no_key_nothing_is_sent_and_the_ledger_says_no_key(db, monkeypatch):
    import anthropic
    client = anthropic.Anthropic(api_key="", max_retries=0)
    sent = []
    monkeypatch.setattr(ai_utils, "_send", lambda *a, **k: sent.append(1))
    with pytest.raises(ai_utils.AINotConfigured):
        ai_utils.create_with_retry(client, restaurant_id=None, action="schedule_note", readiness=None,
                                   model="claude-haiku-5-5", max_tokens=10,
                                   messages=[{"role": "user", "content": "x"}])
    assert not sent
    conn = models.get_conn()
    row = conn.execute("SELECT outcome, reason FROM ai_usage ORDER BY id DESC LIMIT 1").fetchone()
    conn.close()
    assert (row["outcome"], row["reason"]) == ("blocked", "no_key")
    # A test double is never "unconfigured".
    assert not ai_utils.client_unconfigured(types.SimpleNamespace(api_key=""))


def test_a_missing_key_reads_as_configuration_never_failed_partway():
    assert se._failure_kind(ai_utils.AINotConfigured("x")) == "config"
    assert se._failure_kind(TypeError('Could not resolve authentication method. Expected either api_key or '
                                      'auth_token to be set.')) == "config"
    import schedule_fallback as sfb
    assert sfb._WHY_WORDS["config"] != sfb._WHY_WORDS["other"]


# ── SCALE-25: the gateway counts what goes anyway, and waits off its slot ──

def test_settle_inserts_a_minute_it_has_no_row_for(db, monkeypatch):
    import model_gateway as mg
    monkeypatch.setenv("GATEWAY_ORG_OTPM", "100000")
    t = mg.Ticket("scheduled", mg._minute(), 0, 0, 3)
    msg = types.SimpleNamespace(usage=types.SimpleNamespace(input_tokens=70, output_tokens=900,
                                                            cache_creation_input_tokens=0))
    mg.settle(t, msg)
    row = mg.usage_now()["classes"]["scheduled"]
    assert row["output"] == 900 and row["input"] == 70


def test_a_queue_job_waiting_on_the_gateway_gives_up_its_slot(db, monkeypatch):
    import job_queue as jq
    import model_gateway as mg
    monkeypatch.setenv("QUEUE_GENERATION_CONCURRENCY", "1")
    monkeypatch.setattr(jq, "kick", lambda *a, **k: None)
    rids = [_rid("active", name=f"Q{i} Grill") for i in range(2)]
    for rid in rids:
        jq.enqueue("schedule_generate", {}, restaurant_id=rid)
    a = jq.claim("w1", groups=("generation",))
    assert a and jq.claim("w2", groups=("generation",)) is None          # the one slot is taken
    token = jq._CURRENT.set(a)
    seen = []
    try:
        monkeypatch.setenv("GATEWAY_ORG_OTPM", "1000")
        mg.admit(1, {"max_tokens": 1000, "messages": []}, klass="scheduled", sleep=lambda s: None)

        def nap(seconds):
            seen.append(jq.claim("w2", groups=("generation",)))        # while it waits, the slot is free
        mg.admit(1, {"max_tokens": 1000, "messages": []}, klass="scheduled", sleep=nap)
    finally:
        jq._CURRENT.reset(token)
    assert seen and seen[0] is not None and seen[0]["restaurant_id"] == rids[1]
    conn = models.get_conn()
    assert conn.execute("SELECT parked_until FROM jobs WHERE id=?", (a["id"],)).fetchone()[0] is None
    conn.close()
