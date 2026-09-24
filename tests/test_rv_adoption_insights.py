"""Workstream A: the Response Validation Layer at the Food and Labor reads,
the schedule's "Cavnar AI's note" and the two stored diagnoses.

Each call site used to run its own subset of ai_guard (a confidence
rewrite, a presence check, a cause check, a binding check, an UNVERIFIED
note). They now build one ValidationContext — typed money facts with their
kinds, the diagnosis and ranked drivers as cause anchors, the data's own
stale / estimated / missing flags — and the engine decides. What each test
holds:

  food insight   an opportunity is never "saved"; an old stock count is
                 disclosed; a read the engine refuses is fixed copy stating
                 no figure; the stored read is re-validated from the model's
                 own text without a model call; the text carries .validation
  labor insight  scheduled (not clocked) hours and an old window are
                 disclosed; the gap is never "saved"; the computed FORECAST
                 line is added after validation and the marker stays last;
                 labor_note's cache keeps the validated text
  schedule note  discipline, a cut below the owner's floor, a sent-home
                 keyholder and a demand claim with no demand history are
                 dropped (ops.capture on each); certainty words are removed;
                 instruction text in the prompt is never a cause anchor
  diagnoses      every text field is validated on its surface; "What should
                 change" must be conditional; a driver's opportunity is not
                 "saved"; a name still refuses the whole diagnosis; the
                 dict carries "validation"; a stored row is re-validated
                 when it is read

No model or network is reached: every model call is stubbed.
"""
import json
import sqlite3
import types

import pytest

import models
import response_validation as rv
from models import Restaurant, create_restaurant

# Imported at collection (CLAUDE.md: bound get_conn imports).
import analyser, food_cost_intelligence, insight_store, inventory, labor, review_intelligence  # noqa: E401,F401

fci = food_cost_intelligence
ri = review_intelligence


@pytest.fixture(autouse=True)
def _redirect(db_path, monkeypatch):
    real = models.get_conn
    fake = lambda *a, **k: real(db_path)  # noqa: E731
    monkeypatch.setattr(models, "get_conn", fake)
    monkeypatch.setattr(models, "DB_PATH", db_path)
    monkeypatch.setattr(ri, "DB_PATH", db_path)
    monkeypatch.setattr(fci, "get_conn", lambda *a, **k: real(db_path), raising=False)
    monkeypatch.setattr(fci, "DB_PATH", db_path, raising=False)
    monkeypatch.setattr(models, "other_tenant_names", lambda rid, db_path=None: set())
    import webhooks
    monkeypatch.setattr(webhooks, "fire_webhook", lambda *a, **k: None)
    labor._NOTE_CACHE.clear()
    yield
    labor._NOTE_CACHE.clear()


def _rid(db_path, **kw):
    return create_restaurant(Restaurant(name=kw.pop("name", "Gia Mia"), owner_email="o@x.test", **kw),
                             db_path=db_path)


def _conn(db_path):
    c = sqlite3.connect(db_path)
    c.row_factory = sqlite3.Row
    return c


def _msg(text):
    return types.SimpleNamespace(content=[types.SimpleNamespace(type="text", text=text)], stop_reason="end_turn")


# ══ Food insight ════════════════════════════════════════════════════════════

def _food_analysis(**kw):
    a = {"total_waste_cost_week": 160.0, "monthly_waste_projection": 693.33,
         "projection_basis": "this week × 52/12", "recoverable_monthly": 520.0, "total_stock_value": 4200.0,
         "waste_rate_pct": 3.2, "total_purchased": 5000.0, "benchmark_state": "measured",
         "benchmark_label": "within the starting target", "purchases_basis": "invoices",
         "week_start": "9/14/26", "week_end": "9/20/26",
         "waste_items": [{"item": "Salmon", "waste_last_week": 6, "waste_cost": 120.0, "waste_pct": 12.0,
                          "par_level": 20, "current_stock": 14, "unit_cost": 20.0, "waste_tolerance_pct": 4,
                          "recoverable_cost": 96.0}],
         "overstock": [{"item": "Romaine", "current_stock": 30, "par_level": 10, "overstock_cost": 60.0}],
         "critical_low": [], "reorder_soon": [],
         "order_reduction": [{"item": "Romaine", "savings_vs_last": 38.0, "suggested_order_qty": 10,
                              "last_order_qty": 20}]}
    a.update(kw)
    return a


def _stub_food(monkeypatch, text, seen=None):
    def fake(*a, **kw):
        if seen is not None:
            seen.setdefault("prompts", []).append(kw["messages"][0]["content"])
            seen["calls"] = seen.get("calls", 0) + 1
        return _msg(text)
    monkeypatch.setattr(inventory, "create_with_retry", fake)
    monkeypatch.setattr(inventory, "get_client", lambda *a, **k: object())


def test_the_food_read_is_a_validated_text(monkeypatch):
    _stub_food(monkeypatch, "Waste ran $160 this week, led by Salmon at $120.\n"
                            "1. Trim the Salmon par — $96 a week, low effort")
    out = inventory.get_claude_insights(_food_analysis(), restaurant_name="R", is_live=True)
    assert isinstance(out, str) and out.startswith("Waste ran $160")
    assert rv.validation_of(out) and rv.validation_of(out)["version"] == rv.VERSION


def test_the_food_read_never_calls_the_recoverable_opportunity_saved(monkeypatch):
    """recoverable_monthly is an opportunity projected from one week: "You
    saved $520 a month" passed the old presence check because $520 is in
    the prompt."""
    _stub_food(monkeypatch, "You saved $520 a month on waste.\n1. Trim the Salmon par — $96 a week, low effort")
    out = inventory.get_claude_insights(_food_analysis(), restaurant_name="R", is_live=True)
    assert "You saved $520" not in out
    assert "not money saved" in out


def test_an_old_stock_count_is_disclosed_when_the_read_leaves_it_out(monkeypatch):
    _stub_food(monkeypatch, "Waste ran $160 this week.\n1. Order 10 Romaine — $38 per order, low effort")
    a = _food_analysis(count_freshness={"last_count_at": "2026-08-01", "age_days": 54, "stale": True})
    out = inventory.get_claude_insights(a, restaurant_name="R", is_live=True)
    assert "UNVERIFIED:" in out and "stock count" in out.split("UNVERIFIED:")[1]
    assert "stock count" in " ".join(rv.validation_of(out)["caveats"])


def test_a_refused_food_read_is_fixed_copy_with_no_figure(monkeypatch):
    """Everything the model wrote names another Cavnar restaurant: nothing is
    left to show, and the fixed copy states no figure."""
    monkeypatch.setattr(models, "other_tenant_names", lambda rid, db_path=None: {"Luigi's Trattoria"})
    _stub_food(monkeypatch, "Luigi's Trattoria runs its waste at $120 a week.")
    out = inventory.get_claude_insights(_food_analysis(), restaurant_name="R", is_live=True)
    assert out == inventory.FOOD_READ_UNCHECKED
    assert "Luigi" not in out and "$" not in out and not any(ch.isdigit() for ch in out)
    assert rv.validation_of(out)["verdict"] == "refuse"


def test_sample_data_still_gets_the_notice_and_no_model_call(monkeypatch):
    seen = {}
    _stub_food(monkeypatch, "anything", seen)
    out = inventory.get_claude_insights(_food_analysis(), restaurant_name="R", is_live=False)
    assert out == inventory.SAMPLE_DATA_NOTICE and not seen.get("calls")


def test_a_stored_food_read_is_revalidated_from_the_model_text_without_a_model_call(db_path, monkeypatch):
    rid = _rid(db_path)
    seen = {}
    _stub_food(monkeypatch, "Waste ran $160 this week.\n1. Trim the Salmon par — $96 a week, low effort", seen)
    first = inventory.get_claude_insights(_food_analysis(), restaurant_id=rid, is_live=True)
    assert seen["calls"] == 1 and rv.validation_of(first)
    fp = insight_store.fingerprint(seen["prompts"][0])
    # A read stored before the engine: its old marker and its "saved".
    insight_store.put(rid, "food", fp, "You saved $520 a month on waste.\n\nUNVERIFIED: old note")
    again = inventory.get_claude_insights(_food_analysis(), restaurant_id=rid, is_live=True)
    assert seen["calls"] == 1, "a stored read is served without a model call"
    assert "You saved $520" not in again and "old note" not in again and "not money saved" in again
    assert rv.validation_of(again)["version"] == rv.VERSION
    # ...and what the call site stores carries the model's own text.
    c = _conn(db_path)
    row = json.loads(c.execute("SELECT payload FROM insight_cache WHERE restaurant_id=? AND kind='food'",
                               (rid,)).fetchone()["payload"])
    c.close()
    assert row["_rv"] == rv.VERSION and row["raw"].startswith("You saved $520")


# ══ Labor insight ═══════════════════════════════════════════════════════════

def _labor_analysis(**kw):
    a = {"is_live": True, "total_sales": 10000.0, "total_labor_cost": 3400.0, "overall_labor_pct": 34.0,
         "labor_target": 30, "period_days": 14, "potential_savings": 400.0,
         "potential_savings_weekly": 200.0, "potential_savings_monthly": 866.67,
         "dow_summary": {"Wednesday": 38.0, "Friday": 29.5},
         "overstaffed_days": [{"date": "9/16/26", "day": "Wednesday", "labor_pct": 38.0, "labor_cost": 760.0,
                               "sales": 2000.0, "over_target_dollars": 160.0}],
         "understaffed_days": [], "overtime_risk": [], "role_summary": {},
         "date_range": {"start": "2026-09-07", "end": "2026-09-20", "days": 14}}
    a.update(kw)
    return a


def _stub_labor(monkeypatch, text, seen=None):
    def fake(*a, **kw):
        if seen is not None:
            seen["prompt"] = kw["messages"][0]["content"]
            seen["calls"] = seen.get("calls", 0) + 1
        return _msg(text)
    monkeypatch.setattr(labor, "create_with_retry", fake)
    monkeypatch.setattr(labor, "get_client", lambda *a, **k: object(), raising=False)


_REC = "\n\nRecommendations:\n1. Trim Wednesday by one server."


def test_the_labor_gap_is_never_called_money_saved(monkeypatch):
    _stub_labor(monkeypatch, "Sam, you saved $867 a month." + _REC)
    out = labor.get_claude_insights(_labor_analysis(), restaurant_name="R", owner_name="Sam")
    assert "you saved $867" not in out.lower()
    assert "not money saved" in out


def test_scheduled_hours_are_disclosed_when_the_read_leaves_it_out(monkeypatch):
    _stub_labor(monkeypatch, "Sam, labor ran 34% against a 30% target." + _REC)
    out = labor.get_claude_insights(_labor_analysis(hours_are_estimated=True), restaurant_name="R",
                                    owner_name="Sam")
    assert "UNVERIFIED:" in out and "scheduled hour" in out.split("UNVERIFIED:")[1]
    # Said in the read itself → nothing to add.
    _stub_labor(monkeypatch, "Sam, labor ran 34% on scheduled hours, not clocked ones." + _REC)
    out = labor.get_claude_insights(_labor_analysis(hours_are_estimated=True), restaurant_name="R",
                                    owner_name="Sam")
    assert "UNVERIFIED:" not in out


def test_an_old_labor_window_is_never_this_week(monkeypatch):
    _stub_labor(monkeypatch, "Sam, labor this week ran 34%." + _REC)
    a = _labor_analysis(date_range={"start": "2026-06-07", "end": "2026-06-20", "days": 14})
    out = labor.get_claude_insights(a, restaurant_name="R", owner_name="Sam")
    assert "UNVERIFIED:" in out and "6/20/26" in out.split("UNVERIFIED:")[1]


def test_the_computed_forecast_is_added_after_validation_and_the_marker_stays_last(db_path, monkeypatch):
    rid = _rid(db_path, module_labor=1)
    monkeypatch.setattr("models.get_labor_history", lambda r, limit=3: [
        {"labor_pct": 31.0, "period_start": "2026-08-24", "period_end": "2026-09-06"},
        {"labor_pct": 30.0, "period_start": "2026-08-10", "period_end": "2026-08-23"}])
    monkeypatch.setattr("models.save_labor_snapshot", lambda *a, **k: None)
    _stub_labor(monkeypatch, "Sam, labor ran 34% because the new hire is slow." + _REC)
    out = labor.get_claude_insights(_labor_analysis(), restaurant_name="R", owner_name="Sam", restaurant_id=rid)
    fc = next(ln for ln in out.split("\n") if ln.startswith("FORECAST:"))
    assert fc == ("FORECAST: Labor ran 34% this period, up 3.0 points on the last upload; if the schedule "
                  "doesn't change, expect next week near 34% (a projection, not a measurement).")
    assert out.index("FORECAST:") < out.index("UNVERIFIED:")
    assert "cause" in out.split("UNVERIFIED:")[1]
    assert rv.validation_of(out)["verdict"] == "caveat"


def test_labor_note_keeps_the_validated_text_in_its_cache(monkeypatch):
    seen = {}
    _stub_labor(monkeypatch, "Sam, labor ran 34% against a 30% target." + _REC, seen)
    first = labor.labor_note(1, _labor_analysis(), restaurant_name="R", owner_name="Sam")
    second = labor.labor_note(1, _labor_analysis(), restaurant_name="R", owner_name="Sam")
    assert seen["calls"] == 1 and first == second
    assert rv.validation_of(second) and rv.validation_of(second)["version"] == rv.VERSION


# ══ Schedule note ═══════════════════════════════════════════════════════════

NOTE_PROMPT = ("CONTEXT:\n- Active staff: Ana (Server), Ben (Cook), Cal (Server).\n"
               "Friday dinner needs 3 servers. Hours ceiling 212. Labor target 28%.")


def test_a_note_bullet_that_disciplines_staff_is_dropped_and_captured(monkeypatch):
    caught = []
    import ops
    monkeypatch.setattr(ops, "capture", lambda e, **k: caught.append(str(e)))
    kept = labor._drop_note_bullets(["Kept Friday dinner at 3 servers.", "Write up Ana for being late."],
                                    NOTE_PROMPT)
    assert kept == ["Kept Friday dinner at 3 servers."]
    assert caught and "schedule note bullet dropped" in caught[0]


def test_a_note_bullet_cutting_below_the_owners_floor_or_sending_home_the_keyholder_is_dropped():
    floors = {"Server": {"morning": 1, "night": 3}}
    kept = labor._drop_note_bullets(["Friday dinner down to 1 server.", "Kept Friday dinner at 3 servers.",
                                     "Send Ben home early on Monday."], NOTE_PROMPT,
                                    role_floors=floors, keyholders=["Ben"])
    assert kept == ["Kept Friday dinner at 3 servers."]


def test_a_note_bullet_about_demand_with_no_demand_history_is_dropped():
    prompt = NOTE_PROMPT + "\n\n" + labor.NO_DEMAND_MARKER + " — there is not enough sales history."
    assert labor.schedule_note_problem("Expect more walk-ins on Friday.", prompt)
    assert labor.schedule_note_problem("Expect more walk-ins on Friday.", NOTE_PROMPT) is None


def test_a_note_bullet_loses_its_certainty_words():
    kept = labor._drop_note_bullets(["Friday dinner will definitely be covered with 3 servers."], NOTE_PROMPT)
    assert kept and "definitely" not in kept[0].lower()


def test_the_prompts_instruction_text_is_never_a_cause_anchor():
    """Only the data blocks anchor a cause: the weather paragraph's rule of
    thumb ("heavy rain typically means fewer walk-ins …") is an instruction
    to the model, not a measurement of this restaurant."""
    prompt = (NOTE_PROMPT + "\n\nWeather forecast for next week — heavy rain typically means fewer walk-ins "
              "because the patio is unusable.\n  2026-07-11 (Friday): 60°F, Rain")
    bullet = "Fewer walk-ins Friday because the patio is unusable."
    assert labor.schedule_note_problem(bullet, prompt, data_blocks=["  2026-07-11 (Friday): 60°F, Rain"])
    # A data block that states the cause does anchor it.
    data = "Friday: fewer walk-ins last 4 Fridays because the patio was unusable (events log)."
    assert labor.schedule_note_problem(bullet, prompt + "\n" + data, data_blocks=[data]) is None


def test_the_generator_validates_its_note_against_the_owners_floors(monkeypatch):
    seen = {}
    monkeypatch.setattr(labor, "create_with_retry", lambda client, **k: seen.update(k) or types.SimpleNamespace(
        content=[types.SimpleNamespace(text="date,day,employee,role,shift_start,shift_end,scheduled_hours,notes\n"
                                            "---SUMMARY---\n- Monday lunch down to one server.\n"
                                            "- Kept Friday dinner staffed.")], stop_reason="end_turn"))
    monkeypatch.setattr(labor, "get_client", lambda *a, **k: object(), raising=False)
    analysis = {"overall_labor_pct": 28.0, "overstaffed_days": [], "understaffed_days": [], "dow_summary": {},
                "date_range": {"start": "2026-06-01", "end": "2026-06-14", "days": 14}}
    shifts = [{"employee": "Alex", "role": "Server", "date": "2026-06-01", "scheduled_hours": 8, "actual_hours": 8}]
    out = labor.generate_optimized_schedule(analysis, shifts, restaurant_name="Test Bistro",
                                            role_floors={"Server": {"morning": 2, "night": 2}})
    assert out["summary"] == ["Kept Friday dinner staffed."]


# ══ Diagnoses ═══════════════════════════════════════════════════════════════

_FOOD_RAW = {"headline": "Salmon waste leads food cost.", "cause": "Salmon is over-ordered for weekday demand.",
             "alternative_cause": "A miscount on the last inventory.", "what_would_confirm": "Recount Friday.",
             "recommended_action": "Trim the weekday Salmon order.",
             "expected_outcome": "If the cause is right, Salmon waste falls within two weeks.",
             "confidence": "medium"}
_DRIVERS = {"drivers": [{"label": "Salmon waste above tolerance", "item": "Salmon", "kind": "waste",
                         "value_kind": "opportunity", "dollars_monthly": 640.0,
                         "evidence": "12% waste against a 4% band"}],
            "total_monthly": 640.0, "total_monthly_deduplicated": 640.0}


def test_the_food_diagnosis_carries_its_validation():
    out = fci._validate_diagnosis(dict(_FOOD_RAW), ["Salmon"], "Salmon over par $640/month.", 1, op_lines={})
    v = out["validation"]
    assert v["version"] == rv.VERSION and v["verdict"] == "pass" and v["controls"] is True


def test_what_should_change_must_be_conditional():
    raw = dict(_FOOD_RAW, expected_outcome="Salmon waste falls within two weeks.")
    out = fci._validate_diagnosis(raw, ["Salmon"], "Salmon over par $640/month.", 1, op_lines={})
    assert out["validation"]["controls"] is False
    assert any("conditional" in c for c in out["validation"]["caveats"])


def test_a_diagnosis_loses_its_certainty_words():
    raw = dict(_FOOD_RAW, cause="Salmon is definitely over-ordered for weekday demand.")
    out = fci._validate_diagnosis(raw, ["Salmon"], "Salmon over par $640/month.", 1, op_lines={})
    assert "definitely" not in out["cause"] and "Salmon" in out["cause"]


def test_a_drivers_opportunity_is_never_saved_in_a_diagnosis():
    raw = dict(_FOOD_RAW, recommended_action="Trim the weekday Salmon order and you saved $640 a month.")
    out = fci._validate_diagnosis(raw, ["Salmon"], "Salmon over par $640/month.", 1, op_lines={},
                                  facts=fci.typed_facts(drivers=_DRIVERS))
    assert "you saved $640" not in out["recommended_action"].lower()
    assert "not money saved" in out["recommended_action"]


def test_a_diagnosis_naming_another_tenant_loses_that_sentence(monkeypatch):
    monkeypatch.setattr(models, "other_tenant_names", lambda rid, db_path=None: {"Luigi's Trattoria"})
    raw = dict(_FOOD_RAW, what_would_confirm="Recount Friday. Luigi's Trattoria fixed this with a smaller par.")
    out = fci._validate_diagnosis(raw, ["Salmon"], "Salmon over par $640/month.", 1, op_lines={})
    assert "Luigi" not in out["what_would_confirm"] and out["what_would_confirm"].startswith("Recount Friday")


def test_a_stored_food_diagnosis_is_revalidated_when_read(db_path):
    rid = _rid(db_path)
    c = _conn(db_path)
    c.execute("INSERT INTO food_cost_diagnoses (restaurant_id, window_days, headline, cause, drivers_json, "
              "confidence, recommended_action, expected_outcome, generated_at) VALUES "
              "(?,?,?,?,?,?,?,?,datetime('now'))",
              (rid, fci.DIAGNOSIS_WINDOW_DAYS, "Salmon waste leads food cost.",
               "Salmon is definitely over-ordered.", json.dumps(_DRIVERS["drivers"]), "medium",
               "Trim the Salmon order and you saved $640 a month.", "Waste falls in two weeks."))
    c.commit()
    c.close()
    d = fci.get_diagnosis(rid, db_path=db_path)
    assert "definitely" not in d["cause"]
    assert "you saved $640" not in d["recommended_action"].lower()
    assert d["validation"]["version"] == rv.VERSION and d["validation"]["controls"] is False


def _review_raw(**kw):
    raw = {"cause": "The pass backs up on Friday dinner.", "alternative_cause": "Hold times at the window.",
           "what_would_confirm": "Watch two Friday services.", "evidence_review_ids": [1, 2],
           "operational_evidence": [], "confidence": "medium", "recommended_action": "Put a manager on the pass.",
           "expected_outcome": "If the cause is right, cold-food mentions fall within two weeks."}
    raw.update(kw)
    return raw


def test_the_review_diagnosis_carries_its_validation():
    out = ri._validate_diagnosis(_review_raw(), {1, 2}, "8 complaints over 90 days about slow service", 1)
    assert out["validation"]["version"] == rv.VERSION and out["validation"]["verdict"] == "pass"
    out = ri._validate_diagnosis(_review_raw(expected_outcome="Cold-food mentions fall within two weeks."),
                                 {1, 2}, "8 complaints over 90 days", 1)
    assert out["validation"]["controls"] is False


def test_a_review_diagnosis_cause_stated_as_certain_is_softened():
    out = ri._validate_diagnosis(_review_raw(cause="The pass certainly backs up on Friday dinner."),
                                 {1, 2}, "8 complaints over 90 days", 1)
    assert "certainly" not in out["cause"]


def test_a_review_diagnosis_still_refuses_an_invented_person():
    with pytest.raises(ValueError, match="named"):
        ri._validate_diagnosis(_review_raw(cause="Marco, the new weekend server, is slow on Friday dinner."),
                               {1, 2}, "8 complaints over 90 days about slow service", 1)


def test_a_stored_review_diagnosis_is_revalidated_when_read(db_path):
    rid = _rid(db_path)
    c = _conn(db_path)
    c.execute("INSERT INTO review_diagnoses (restaurant_id, category, window_days, mention_count, cause, "
              "recommended_action, expected_outcome, confidence, generated_at) "
              "VALUES (?,?,?,?,?,?,?,?,datetime('now'))",
              (rid, "service", 90, 3, "The pass certainly backs up on Fridays.", "Put a manager on the pass.",
               "Mentions fall in two weeks.", "medium"))
    c.commit()
    c.close()
    d = ri.get_diagnoses(rid, db_path=db_path, include_retired=True)[0]
    assert "certainly" not in d["cause"]
    assert d["validation"]["version"] == rv.VERSION and d["validation"]["controls"] is False
