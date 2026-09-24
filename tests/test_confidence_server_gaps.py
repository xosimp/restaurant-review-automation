"""Server fields the client integration found missing (confidence audit, integration pass)."""
import inspect

import admin_ops
import mobile_api
import schedule_engine
import strategy_routes


def test_mobile_food_cost_carries_the_waste_state_and_recoverable_basis():
    src = inspect.getsource(mobile_api)
    for f in ("benchmark_state=", "recoverable_kind=", "annual_recoverable_basis="):
        assert f in src, f


def test_mobile_labor_insight_serves_the_stale_read_with_its_age():
    src = inspect.getsource(mobile_api.mobile_labor_insight)
    assert "stale=True" in src and "stale_note" in src and "age_days" in src


def test_admin_fleet_integrations_carry_the_pos_sync_state():
    src = inspect.getsource(admin_ops.integrations)
    assert '"sync_state"' in src and '"age_days"' in src


def test_demand_accuracy_reaches_the_schedule_review_and_labor_intel():
    src = inspect.getsource(schedule_engine)
    assert 'result["demand_accuracy"]' in src and "demand_accuracy=result.get(\"demand_accuracy\")" in src
    assert "week_projection_accuracy=result.get(\"week_projection_accuracy\")" in src
    src = inspect.getsource(strategy_routes._do_schedule_intel)
    assert '"demand_accuracy"' in src and '"week_projection_accuracy"' in src


def test_schedule_quality_recommendations_each_carry_k1(monkeypatch):
    import rec_trust
    q = {"recommendations": ["Fill the gap on Friday dinner: one short on line.", ""],
         "confidence": {"score": 80, "reasons": ["2 of 10 scheduled staff have no Operational Score."]}}
    monkeypatch.setattr(rec_trust, "Context", lambda *a, **k: None)
    seen = []
    monkeypatch.setattr(rec_trust, "assess", lambda rid, key, evidence=None, sources=None, ctx=None:
                        seen.append((key, evidence, sources)) or {"pct": 60, "dimensions": {}})
    items = rec_trust.schedule_quality_items(1, q)
    assert len(items) == 1 and items[0]["text"].startswith("Fill the gap") and items[0]["confidence"]["pct"] == 60
    key, ev, src = seen[0]
    assert key.startswith("schedule_") and ev["coverage"] == 0.8 and src == ("labor",)
    assert "Operational Score" in ev["basis"]


def test_schedule_quality_items_without_recommendations_is_empty():
    import rec_trust
    assert rec_trust.schedule_quality_items(1, {"recommendations": []}) == []
