"""Scheduler audit 10/10/26, workstream E — AI-27: Claude Haiku 5.5 priced,
T1 moved to it, canary restaurants first."""
import pytest

import ai_utils
import ai_workflows as wf


def test_haiku55_is_priced_at_its_list_price():
    assert ai_utils._price_for("claude-haiku-5-5") == (0.10, 0.50)
    # an alias or a dated snapshot prices by its family, never at Haiku 4.5's rate
    assert ai_utils._price_for("claude-haiku-5-5-20261001") == (0.10, 0.50)
    assert ai_utils._estimate_cost("claude-haiku-5-5", 1_000_000, 1_000_000) == pytest.approx(0.60)
    # ten times cheaper than the model T1 ran on before
    assert ai_utils._estimate_cost(ai_utils.HAIKU, 1_000_000, 1_000_000) == pytest.approx(6.0)
    assert ai_utils.PRICE_VERSION >= "2026-10-10"


def test_haiku55_thinking_is_off_by_default_and_never_disabled_at_xhigh():
    assert ai_utils.default_thinking("claude-haiku-5-5", {}) == {"type": "disabled"}
    assert ai_utils.default_thinking("claude-haiku-5-5", {"output_config": {"effort": "max"}}) is None


def test_t1_is_haiku55_everywhere_by_default(monkeypatch):
    # The canary hold ended 10/17/26 (owner: "move the haiku ones to haiku 5.5").
    monkeypatch.delenv("AI_TIER_T1_MODEL", raising=False)
    monkeypatch.delenv("AI_TIER_CANARY", raising=False)
    pol = wf.POLICIES["review_analysis"]
    assert wf.route_for(pol, 0, restaurant_id=5).model == ai_utils.HAIKU_55
    assert wf.route_for(pol, 0).model == ai_utils.HAIKU_55


def test_t1_is_haiku55_on_the_canary_and_haiku45_elsewhere(monkeypatch):
    monkeypatch.delenv("AI_TIER_T1_MODEL", raising=False)
    monkeypatch.setenv("AI_TIER_CANARY", "T1")                     # the hold, put back by the operator
    monkeypatch.setenv("AI_CANARY_RESTAURANTS", "4")
    pol = wf.POLICIES["review_analysis"]
    assert wf.route_for(pol, 0, restaurant_id=4).model == ai_utils.HAIKU_55
    assert wf.route_for(pol, 0, restaurant_id=5).model == ai_utils.HAIKU
    # a call made for no one restaurant (a batch built for many) keeps the old model
    assert wf.route_for(pol, 0).model == ai_utils.HAIKU


def test_t1_canary_off_moves_every_restaurant(monkeypatch):
    monkeypatch.delenv("AI_TIER_T1_MODEL", raising=False)
    monkeypatch.setenv("AI_TIER_CANARY", "")
    pol = wf.POLICIES["review_analysis"]
    assert wf.route_for(pol, 0, restaurant_id=5).model == ai_utils.HAIKU_55
    assert wf.route_for(pol, 0).model == ai_utils.HAIKU_55


def test_an_explicit_t1_model_is_the_operators_choice_never_canaried(monkeypatch):
    monkeypatch.setenv("AI_TIER_T1_MODEL", "claude-haiku-4-5-20251001")
    monkeypatch.delenv("AI_TIER_CANARY", raising=False)
    pol = wf.POLICIES["review_analysis"]
    assert wf.route_for(pol, 0, restaurant_id=4).model == "claude-haiku-4-5-20251001"


def test_the_orchestrator_routes_by_the_runs_restaurant(monkeypatch):
    """ai_orchestrator.generate hands route_for its restaurant, so the canary
    restaurant's T1 runs are Haiku 5.5."""
    import inspect
    import ai_orchestrator
    src = inspect.getsource(ai_orchestrator.generate)
    assert "route_for(pol, step, start, restaurant_id=restaurant_id)" in src


def test_the_schedule_small_calls_default_to_haiku55(monkeypatch):
    monkeypatch.delenv("SCHEDULE_RULES_MODEL", raising=False)
    monkeypatch.delenv("SCHEDULE_NOTE_MODEL", raising=False)
    assert ai_utils.model_for("schedule_rules") == "claude-haiku-5-5"
    assert ai_utils.model_for("schedule_note") == "claude-haiku-5-5"
