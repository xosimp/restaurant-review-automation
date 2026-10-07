"""The four page-load reads on the orchestrator and the Restaurant Context
Manager (AI orchestration design Phase 2, owner-approved 10/7/26).

What each test holds:

  labor      the labor_insight ladder: Haiku (T1) first; a T1 text the
             validation layer refuses climbs to Sonnet (T2) once, with the
             engine's reasons appended to the T2 prompt; past the
             interactive deadline it does not climb and the fixed copy is
             served — its fallback recorded once; a run in ai_runs under
             "labor_read:<day>"; the stored read is keyed on the packet's
             fingerprint as well as the read's own data
  food       inventory_insight: Sonnet only, never a second call, the
             fallback recorded once; keyed on the packet
  reviews    review_insight: Sonnet, one call, a run under "review_read:"
  marketing  marketing_insight: Haiku first, Sonnet on a refused brief
  outcomes   an answered line is the read accepted, "Not for us" rejected,
             filed on the read's run; support's answer through view-as and
             another kind of line file nothing; the shared answer handler
             files it (web and phone are one handler)

No model or network is reached: every model call is stubbed.
"""
import inspect
import sys
import types

import pytest

import ai_orchestrator as orch
import ai_utils
import ai_workflows as wf
import insight_store
import inventory
import labor
import models
import restaurant_context as rc
import response_validation as rv  # noqa: F401
from models import Restaurant, create_restaurant


@pytest.fixture(autouse=True)
def _db(db_path, monkeypatch):
    real = models.get_conn

    def conn(*a, **k):
        return real(db_path)
    for mod in list(sys.modules.values()):
        if mod is not None and getattr(mod, "get_conn", None) is real:
            monkeypatch.setattr(mod, "get_conn", conn)
    monkeypatch.setattr(models, "get_conn", conn)
    monkeypatch.setattr(models, "DB_PATH", db_path)
    monkeypatch.setattr(models, "other_tenant_names", lambda rid, db_path=None: set())
    import webhooks
    monkeypatch.setattr(webhooks, "fire_webhook", lambda *a, **k: None)
    monkeypatch.setattr(ai_utils, "ai_rate_limited", lambda *a, **kw: False)
    monkeypatch.setattr(orch, "REPLAY_SAMPLE_RATE", 0.0)
    monkeypatch.setenv("AI_SHADOW_REVIEW", "0")
    orch._OVERRIDES.clear()
    rc.invalidate()
    labor._NOTE_CACHE.clear()
    import client_api
    client_api._insight_cache.clear()
    yield
    labor._NOTE_CACHE.clear()
    client_api._insight_cache.clear()
    rc.invalidate()


def _msg(text, stop="end_turn"):
    return types.SimpleNamespace(content=[types.SimpleNamespace(type="text", text=text)], stop_reason=stop,
                                 usage=types.SimpleNamespace(input_tokens=1, output_tokens=1,
                                                             cache_creation_input_tokens=0,
                                                             cache_read_input_tokens=0))


def _scripted(texts, calls):
    """A create_with_retry stand-in answering `texts` in turn (the last one
    again after), recording every call's kwargs."""
    def fake(*a, **kw):
        calls.append(kw)
        return _msg(texts[min(len(calls) - 1, len(texts) - 1)])
    return fake


def _prompt(kw):
    return kw["messages"][0]["content"]


T1 = lambda: wf._tier_table()["T1"]["model"]  # noqa: E731
T2 = lambda: wf._tier_table()["T2"]["model"]  # noqa: E731


def _runs(rid, workflow):
    c = models.get_conn()
    try:
        return [dict(r) for r in c.execute("SELECT * FROM ai_runs WHERE restaurant_id=? AND workflow=? "
                                           "ORDER BY created_at", (rid, workflow)).fetchall()]
    finally:
        c.close()


def _fallbacks(rid, action):
    c = models.get_conn()
    try:
        r = c.execute("SELECT COALESCE(SUM(n),0) FROM ai_quality_events WHERE restaurant_id=? AND action=? "
                      "AND kind='fallback'", (rid, action)).fetchone()
        return int(r[0] or 0)
    finally:
        c.close()


# ── labor ───────────────────────────────────────────────────────────────────

def _labor_rid():
    return create_restaurant(Restaurant(name="Orchestrated Labor Co", owner_email="l@x.test", module_labor=1))


def _analysis(**kw):
    a = {"is_live": True, "total_sales": 10000.0, "total_labor_cost": 3400.0, "overall_labor_pct": 34.0,
         "labor_target": 30, "period_days": 14, "potential_savings": 400.0,
         "potential_savings_weekly": 200.0, "potential_savings_monthly": 866.67,
         "dow_summary": {"Wednesday": 38.0, "Friday": 29.5},
         "overstaffed_days": [{"date": "9/16/26", "day": "Wednesday", "labor_pct": 38.0, "labor_cost": 760.0,
                               "sales": 2000.0, "over_target_dollars": 160.0}],
         "understaffed_days": [], "overtime_risk": [], "role_summary": {},
         "date_range": {"start": "2026-09-14", "end": "2026-09-27", "days": 14}}
    a.update(kw)
    return a


_LABOR_TEXT = "Sam, labor ran 34% against a 30% target.\n\nRecommendations:\n1. Trim Wednesday by one server."


def _stub_labor(monkeypatch, texts):
    calls = []
    monkeypatch.setattr(labor, "create_with_retry", _scripted(texts, calls))
    monkeypatch.setattr(labor, "get_client", lambda *a, **k: object(), raising=False)
    return calls


def test_the_labor_read_starts_on_t1_and_climbs_to_t2_with_the_reasons(monkeypatch):
    rid = _labor_rid()
    calls = _stub_labor(monkeypatch, ["", _LABOR_TEXT])        # T1's empty text is refused whole
    out = labor.get_claude_insights(_analysis(), restaurant_name="R", owner_name="Sam", restaurant_id=rid)
    assert len(calls) == 2
    assert calls[0]["model"] == T1() and calls[1]["model"] == T2()
    assert "AN EARLIER DRAFT OF THIS WAS SET ASIDE" not in _prompt(calls[0])
    assert "AN EARLIER DRAFT OF THIS WAS SET ASIDE" in _prompt(calls[1])
    assert _prompt(calls[1]).startswith(_prompt(calls[0]))        # the same prompt, the notes added
    assert "34%" in out and labor.LABOR_READ_UNCHECKED not in out
    run = _runs(rid, "labor_insight")[-1]
    assert (run["escalations"], run["final_tier"], run["status"]) == (1, "T2", "ok")
    assert run["subject"].startswith("labor_read:")
    assert _fallbacks(rid, "labor_insight") == 0, "nothing fell back: T2 replaced the refused T1"


def test_past_the_interactive_deadline_the_read_does_not_climb_and_serves_its_fallback(monkeypatch):
    rid = _labor_rid()
    monkeypatch.setattr(orch, "INTERACTIVE_ESCALATION_SECONDS", 0)
    calls = _stub_labor(monkeypatch, ["", _LABOR_TEXT])
    out = labor.get_claude_insights(_analysis(), restaurant_name="R", owner_name="Sam", restaurant_id=rid)
    assert len(calls) == 1 and calls[0]["model"] == T1()
    assert labor.LABOR_READ_UNCHECKED in out
    assert _runs(rid, "labor_insight")[-1]["status"] == "capped"
    assert _fallbacks(rid, "labor_insight") == 1


def test_a_passing_t1_read_is_one_haiku_call(monkeypatch):
    rid = _labor_rid()
    calls = _stub_labor(monkeypatch, [_LABOR_TEXT])
    out = labor.get_claude_insights(_analysis(), restaurant_name="R", owner_name="Sam", restaurant_id=rid)
    assert len(calls) == 1 and calls[0]["model"] == T1() and "34%" in out
    assert _runs(rid, "labor_insight")[-1]["final_tier"] == "T1"


def test_a_cut_off_labor_read_is_never_escalated(monkeypatch):
    rid = _labor_rid()
    calls = []
    monkeypatch.setattr(labor, "create_with_retry",
                        lambda *a, **kw: calls.append(kw) or _msg(_LABOR_TEXT, stop="max_tokens"))
    monkeypatch.setattr(labor, "get_client", lambda *a, **k: object(), raising=False)
    with pytest.raises(ValueError):
        labor.get_claude_insights(_analysis(), restaurant_name="R", owner_name="Sam", restaurant_id=rid)
    assert len(calls) == 1


def test_the_labor_read_takes_its_trend_from_the_context_section(monkeypatch):
    rid = _labor_rid()
    for start, end, pct in (("2026-09-14", "2026-09-20", 31.0), ("2026-09-21", "2026-09-27", 28.5)):
        c = models.get_conn()
        c.execute("INSERT INTO labor_history (restaurant_id, period_start, period_end, labor_pct, total_labor, "
                  "total_sales, basis, days, kind) VALUES (?,?,?,?,?,?,'pos',7,?)",
                  (rid, start, end, pct, pct * 100, 10000.0, models.LABOR_PERIOD_WEEK))
        c.commit()
        c.close()
    calls = _stub_labor(monkeypatch, [_LABOR_TEXT])
    labor.get_claude_insights(_analysis(), restaurant_name="R", owner_name="Sam", restaurant_id=rid)
    section = rc.section(rid, "labor_trend", viewer=labor.TEAM_VIEWER)
    assert section.text and section.text in _prompt(calls[0])
    assert "TREND: Labor % is DOWN 2.5 points" in _prompt(calls[0])


def test_the_stored_labor_read_is_keyed_on_the_packet_too(monkeypatch):
    rid = _labor_rid()
    calls = _stub_labor(monkeypatch, [_LABOR_TEXT])
    labor.labor_note(rid, _analysis(), restaurant_name="R", owner_name="Sam")
    labor._NOTE_CACHE.clear()
    labor.labor_note(rid, _analysis(), restaurant_name="R", owner_name="Sam")
    assert len(calls) == 1, "the same data and the same sections: the stored read"
    real = rc.packet

    def moved(*a, **k):
        pk = real(*a, **k)
        pk.fingerprint = "moved-" + pk.fingerprint
        return pk
    monkeypatch.setattr(rc, "packet", moved)
    labor._NOTE_CACHE.clear()
    labor.labor_note(rid, _analysis(), restaurant_name="R", owner_name="Sam")
    assert len(calls) == 2, "a section that moved is a new read"


# ── food ────────────────────────────────────────────────────────────────────

def _food_analysis():
    return {"total_waste_cost_week": 160.0, "monthly_waste_projection": 693.33,
            "projection_basis": "this week × 52/12", "recoverable_monthly": 520.0, "total_stock_value": 4200.0,
            "waste_rate_pct": 3.2, "total_purchased": 5000.0, "benchmark_state": "measured",
            "benchmark_label": "within the starting target", "purchases_basis": "invoices",
            "week_start": "9/14/26", "week_end": "9/20/26",
            "waste_items": [{"item": "Salmon", "waste_last_week": 6, "waste_cost": 120.0, "waste_pct": 12.0,
                             "par_level": 20, "current_stock": 14, "unit_cost": 20.0, "waste_tolerance_pct": 4,
                             "recoverable_cost": 96.0}],
            "overstock": [], "critical_low": [], "reorder_soon": [], "order_reduction": []}


_FOOD_TEXT = "Waste ran $160 this week, led by Salmon at $120.\n1. Cut the Salmon order — $96 a week opportunity, low effort"


def _stub_food(monkeypatch, texts):
    calls = []
    monkeypatch.setattr(inventory, "create_with_retry", _scripted(texts, calls))
    monkeypatch.setattr(inventory, "get_client", lambda *a, **k: object())
    return calls


def test_the_food_read_is_one_sonnet_call_and_never_climbs(monkeypatch):
    rid = create_restaurant(Restaurant(name="Orchestrated Food Co", owner_email="f@x.test", module_inventory=1))
    models.update_restaurant(rid, {"menu_notes": "Salmon in three dishes"})
    models._invalidate_request_cache(rid)
    calls = _stub_food(monkeypatch, ["", _FOOD_TEXT])
    out = inventory.get_claude_insights(_food_analysis(), restaurant_id=rid, is_live=True)
    assert len(calls) == 1 and calls[0]["model"] == T2()
    assert out == inventory.FOOD_READ_UNCHECKED
    assert _fallbacks(rid, "inventory_insight") == 1
    run = _runs(rid, "inventory_insight")[-1]
    assert run["escalations"] == 0 and run["subject"].startswith("food_read:")
    # The menu notes still reach the prompt — now from the profile section.
    assert "Salmon in three dishes" in _prompt(calls[0])


def test_the_stored_food_read_is_keyed_on_the_packet_too(monkeypatch):
    rid = create_restaurant(Restaurant(name="Keyed Food Co", owner_email="k@x.test", module_inventory=1))
    calls = _stub_food(monkeypatch, [_FOOD_TEXT])
    inventory.get_claude_insights(_food_analysis(), restaurant_id=rid, is_live=True)
    inventory.get_claude_insights(_food_analysis(), restaurant_id=rid, is_live=True)
    assert len(calls) == 1
    real = rc.packet

    def moved(*a, **k):
        pk = real(*a, **k)
        pk.fingerprint = "moved-" + pk.fingerprint
        return pk
    monkeypatch.setattr(rc, "packet", moved)
    inventory.get_claude_insights(_food_analysis(), restaurant_id=rid, is_live=True)
    assert len(calls) == 2


# ── reviews ─────────────────────────────────────────────────────────────────

def test_the_review_read_is_one_sonnet_run(monkeypatch, db_path):
    import uuid
    from datetime import datetime
    import client_api
    from models import Review, save_reviews
    rid = create_restaurant(Restaurant(name="Orchestrated Reviews Co", owner_email="r@x.test", module_reviews=1))
    when = datetime.now().strftime("%Y-%m-%dT%H:%M:%S")
    save_reviews([Review(restaurant_id=rid, platform="google", external_id=f"x-{uuid.uuid4().hex[:10]}",
                         author="Sam P.", rating=5, text="Lovely dinner.", review_date=when) for _ in range(4)])
    c = models.get_conn()
    c.execute("UPDATE reviews SET processed=1, sentiment='positive', response_status='posted' WHERE restaurant_id=?",
              (rid,))
    c.commit()
    c.close()
    calls = []
    monkeypatch.setattr(ai_utils, "create_with_retry", _scripted(
        ["\U0001f4ca This week: Guests keep praising dinner.\n✅ Do today: Thank the closing crew."], calls))
    monkeypatch.setattr(ai_utils, "get_client", lambda *a, **k: object())
    payload, status = client_api._do_review_insight(rid)
    assert status == 200 and "praising dinner" in payload["insight"]
    assert len(calls) == 1 and calls[0]["model"] == T2()
    run = _runs(rid, "review_insight")[-1]
    assert run["subject"].startswith("review_read:") and run["attempts"] == 1


# ── marketing ───────────────────────────────────────────────────────────────

def test_the_marketing_brief_starts_on_haiku_and_climbs_on_a_refused_brief(monkeypatch):
    import client_api
    rid = create_restaurant(Restaurant(name="Orchestrated Marketing Co", owner_email="m@x.test", module_marketing=1))
    calls = []
    monkeypatch.setattr(ai_utils, "create_with_retry", _scripted(
        ["", "Hi, the cooler nights are this week's opening.\n\n1. Post the soup of the day.\n2. Share the patio heaters."],
        calls))
    monkeypatch.setattr(ai_utils, "get_client", lambda *a, **k: object())
    out, status = client_api._do_mkt_insight(rid, raw=True)
    assert status == 200 and "cooler nights" in out["insight"]
    assert len(calls) == 2 and calls[0]["model"] == T1() and calls[1]["model"] == T2()
    assert "AN EARLIER DRAFT OF THIS WAS SET ASIDE" in _prompt(calls[1])
    run = _runs(rid, "marketing_insight")[-1]
    assert run["escalations"] == 1 and run["subject"].startswith("marketing_read:")


# ── outcomes ────────────────────────────────────────────────────────────────

def test_an_answered_line_is_the_read_accepted_and_not_for_us_rejected(monkeypatch):
    rid = _labor_rid()
    _stub_labor(monkeypatch, [_LABOR_TEXT])
    labor.get_claude_insights(_analysis(), restaurant_name="R", owner_name="Sam", restaurant_id=rid)
    assert insight_store.file_read_answer(rid, "insight_labor:abc123", "completed", authority="principal")
    assert _runs(rid, "labor_insight")[-1]["outcome"] == "accepted"
    assert insight_store.file_read_answer(rid, "insight_labor:abc123", "dismissed", authority="delegate")
    run = _runs(rid, "labor_insight")[-1]
    assert run["outcome"] == "rejected" and run["outcome_quality"] == 0.0
    # Support through view-as, a snooze, another kind of line: nothing filed.
    assert not insight_store.file_read_answer(rid, "insight_labor:abc123", "completed", authority="admin")
    assert not insight_store.file_read_answer(rid, "insight_labor:abc123", "snoozed", authority="principal")
    assert not insight_store.file_read_answer(rid, "diag_labor:x", "completed", authority="principal")
    # A workflow with no run for the restaurant files nothing.
    assert not insight_store.file_read_answer(rid, "insight_food:abc", "completed", authority="principal")


def test_the_shared_answer_handler_files_the_read_outcome():
    import strategy_routes
    src = inspect.getsource(strategy_routes._do_rec_event)
    assert "file_read_answer(" in src and 'authority != "admin"' in src
    assert set(insight_store.READ_WORKFLOWS) == {"insight_labor", "insight_food", "insight_review",
                                                 "insight_marketing"}
    for prefix, (workflow, subject) in insight_store.READ_WORKFLOWS.items():
        assert workflow in wf.POLICIES, prefix
    # Each read files its run under the subject the answer looks for.
    import client_api
    assert labor.LABOR_READ_SUBJECT == "labor_read" and inventory.FOOD_READ_SUBJECT == "food_read"
    assert client_api.REVIEW_READ_SUBJECT == "review_read"
    assert client_api.MARKETING_READ_SUBJECT == "marketing_read"


def test_the_engines_findings_become_the_notes():
    v = types.SimpleNamespace(verdict="refuse", findings=[
        {"rule": "F1", "severity": "refuse", "span": "$4,120"},
        {"rule": "N1", "severity": "drop", "span": "Maria"},
        {"rule": "C1", "severity": "rewrite", "span": "definitely"}])
    out = orch.verdict_from_validation(v)
    assert out.trigger == "validation_refuse"
    assert any("$4,120" in r for r in out.reasons)
    assert not any("Maria" in r for r in out.reasons), "a name is never handed back"
    assert not any("definitely" in r for r in out.reasons), "a rewrite is not a reason to set a draft aside"
    assert orch.notes_block([]) == "" and "- " in orch.notes_block(out.reasons)
