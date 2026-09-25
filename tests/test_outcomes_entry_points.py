"""Every door a tracker comes through, after the Recommendation Outcome & ROI
audit — and the reply contracts the web and iOS build against.

  #3   one tracker per metric on EVERY entry point: Home Track (/outcomes),
       module Track (/recs/event), Home Done, schedule accept, reprice,
       campaign send, observe() and Ask — a second request is answered
       ("Already measuring labor % until M/D/YY"), never started beside it.
  #11  Marketing Track starts no sales tracker.
  #18  Accept/Done start a tracker when the recommendation carries a metric
       and nothing in the metric's family is being measured.
  #39  DSR actions are measured by kind.

No model, network, email, SMS or push is reached.
"""
import os
from datetime import date, timedelta

import pytest
from flask import Flask

import client_api
import metrics
import models
import outcomes
import rec_ledger
import strategy_routes
from models import Restaurant, create_restaurant, get_conn
from time_utils import mdy

TODAY = date.today()
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@pytest.fixture(autouse=True)
def _redirect_db(monkeypatch, db_path):
    real = models.get_conn
    redirect = lambda *a, **k: real(db_path)  # noqa: E731
    for mod in (models, outcomes, metrics):
        monkeypatch.setattr(mod, "get_conn", redirect, raising=False)
    monkeypatch.setattr(models, "DB_PATH", db_path)
    monkeypatch.setattr(outcomes, "_holidays_between", lambda s, e: {})
    import webhooks
    monkeypatch.setattr(webhooks, "fire_webhook", lambda *a, **k: None)


def _rid(db_path, **kw):
    for m in ("module_labor", "module_inventory", "module_reviews", "module_marketing"):
        kw.setdefault(m, 1)
    rid = create_restaurant(Restaurant(name="Door Co", owner_email="door@x.test", **kw), db_path=db_path)
    # 60 days of labor and sales up to yesterday: every labor/sales metric
    # can be read, so a refusal is about the gate, never about the data.
    conn = get_conn(db_path)
    for i in range(1, 61):
        d = TODAY - timedelta(days=i)
        conn.execute("INSERT INTO labor_daily_history (restaurant_id, date, day_of_week, labor_cost, sales, "
                     "labor_pct) VALUES (?,?,?,?,?,?)", (rid, d.isoformat(), d.strftime("%A"), 300.0, 1000.0, 30.0))
    conn.commit()
    conn.close()
    return rid


def _u(rid):
    return {"id": 1, "restaurant_id": rid, "role": "client"}


def _route(fn, rid, body=None, args=(), query=None):
    app = Flask(__name__)
    with app.test_request_context(json=body or {}, query_string=query or {}):
        out = fn(_u(rid), *args)
    return out


def _shown(rid, *keys, module="labor", surface="home"):
    """What a surface showed this owner — an answer names a recommendation
    it was shown (K2), so every door is walked from a shown card."""
    for k in keys:
        rec_ledger.present(rid, k, module, surface)


def _rec_event(rid, body):
    out, status = _route(strategy_routes._do_rec_event, rid, body)
    assert status == 200
    return out


def _tracking(db_path, rid):
    conn = get_conn(db_path)
    rows = conn.execute("SELECT source_key, metric, module FROM recommendation_outcomes WHERE restaurant_id=? "
                        "AND status='tracking' ORDER BY id", (rid,)).fetchall()
    conn.close()
    return [tuple(r) for r in rows]


# ── #3 Home Track (/outcomes) ───────────────────────────────────────────────

def test_3_a_second_track_on_the_same_metric_is_answered_not_started(db_path):
    rid = _rid(db_path)
    _shown(rid, "trim_day:Monday", "trim_day:Tuesday")
    first, st = _route(strategy_routes._do_outcome_record, rid,
                       {"source": "recommendation", "source_key": "trim_day:Monday", "title": "Trim Monday",
                        "metric": "labor_pct"})
    assert st == 200 and first["tracker"]["metric"] == "labor_pct"
    until = first["tracker"]["evaluate_on"]
    assert first["tracker"]["label_text"] == f"measuring labor % until {mdy(until)}"
    second, st = _route(strategy_routes._do_outcome_record, rid,
                        {"source": "recommendation", "source_key": "trim_day:Tuesday", "title": "Trim Tuesday",
                         "metric": "labor_pct"})
    assert st == 200 and second["ok"] is True and "outcome" not in second
    refused = second["tracker_refused"]
    assert refused["code"] == "in_flight" and refused["in_flight_until"] == until
    assert refused["reason"].startswith(f"Already measuring labor % until {mdy(until)}")
    assert second["warning"] == refused["reason"]            # both clients show `warning`
    assert _tracking(db_path, rid) == [("trim_day:Monday", "labor_pct", "labor")]
    # The owner still took the recommendation: it is answered in the ledger.
    assert rec_ledger.silenced(rid, "trim_day:Tuesday")


def test_3_tracking_the_same_recommendation_twice_returns_the_same_tracker(db_path):
    rid = _rid(db_path)
    _shown(rid, "trim_day:Monday")
    body = {"source": "recommendation", "source_key": "trim_day:Monday", "title": "Trim Monday",
            "metric": "labor_pct"}
    a, _ = _route(strategy_routes._do_outcome_record, rid, body)
    b, _ = _route(strategy_routes._do_outcome_record, rid, body)
    assert a["tracker"]["id"] == b["tracker"]["id"] and "tracker_refused" not in b


# ── #3 / #18 module Track and Done (/recs/event) ────────────────────────────

def test_3_module_track_twice_on_labor_is_refused(db_path):
    rid = _rid(db_path)
    _shown(rid, "insight_labor:a", "insight_labor:b", surface="labor")
    a = _rec_event(rid, {"key": "insight_labor:a", "event": "accepted", "surface": "labor", "module": "labor"})
    assert a["tracker"]["metric"] == "labor_pct" and a["tracking"]["metric"] == "labor_pct"
    b = _rec_event(rid, {"key": "insight_labor:b", "event": "accepted", "surface": "labor", "module": "labor"})
    assert "tracker" not in b and b["tracker_refused"]["code"] == "in_flight"
    assert "hidden for 14 days" in b["message"] and "Already measuring labor %" in b["message"]
    assert len(_tracking(db_path, rid)) == 1


def test_18_done_on_a_recommendation_that_carries_a_metric_starts_its_tracker(db_path):
    rid = _rid(db_path)
    rec_ledger.present(rid, "trim_day:Monday", "labor", "home", title="Trim Monday lunch",
                       expected_metric="labor_pct")
    out = _rec_event(rid, {"key": "trim_day:Monday", "event": "completed", "surface": "home"})
    t = out["tracker"]
    assert t["metric"] == "labor_pct" and t["module"] == "labor"
    assert out["message"] == f"Done — Cavnar AI won’t suggest it again. Now {t['label_text']}"
    assert t["label_text"] == f"measuring labor % until {mdy(t['evaluate_on'])}"
    row = outcomes.get_outcome(t["id"])
    assert row["title"] == "Trim Monday lunch" and row["baseline_value"] == 30.0


def test_18_done_without_a_metric_starts_nothing_and_promises_nothing(db_path):
    rid = _rid(db_path)
    rec_ledger.present(rid, "insight_review:x", "reviews", "reviews", title="Thank the regulars")
    out = _rec_event(rid, {"key": "insight_review:x", "event": "completed", "surface": "reviews",
                           "module": "reviews"})
    assert "tracker" not in out and "tracker_refused" not in out
    assert out["message"] == "Done — Cavnar AI won’t suggest it again"
    assert _tracking(db_path, rid) == []


def test_18_an_automatic_start_is_refused_while_its_family_is_measured(db_path):
    """Overtime is being measured; Done on a labor % recommendation would
    read the same labor-cost move a second time."""
    rid = _rid(db_path)
    ot = outcomes.record(rid, "manual", "ot", "Cap overtime", "overtime_hours", gate=None)
    rec_ledger.present(rid, "trim_day:Monday", "labor", "home", title="Trim Monday", expected_metric="labor_pct")
    out = _rec_event(rid, {"key": "trim_day:Monday", "event": "completed", "surface": "home"})
    refused = out["tracker_refused"]
    assert refused["in_flight"]["id"] == ot["id"] and refused["in_flight_until"] == ot["evaluate_on"]
    assert "overtime hours per week" in refused["reason"] and "moves with it" in refused["reason"]
    assert refused["reason"] in out["message"]
    # An explicit Track on the same metric-level rule is not a family refusal.
    explicit, _ = _route(strategy_routes._do_outcome_record, rid,
                         {"source": "manual", "title": "Trim", "metric": "labor_pct"})
    assert explicit["tracker"]["metric"] == "labor_pct"


def test_18_the_home_done_button_starts_and_refuses_the_same_way(db_path):
    rid = _rid(db_path)
    _shown(rid, "trim_day:Monday", "trim_day:Friday")

    def done(key, title, metric):
        app = Flask(__name__)
        with app.test_request_context(json={"key": key, "kind": "done", "title": title, "metric": metric}):
            return client_api.home_dismiss_api.__wrapped__(current_user=_u(rid)).get_json()

    a = done("trim_day:Monday", "Trim Monday", "labor_pct")
    assert a["ok"] and a["tracker"]["metric"] == "labor_pct"
    assert a["outcome"]["evaluate_on"] == a["tracker"]["evaluate_on"]      # the field the clients read today
    b = done("trim_day:Friday", "Trim Friday", "labor_pct")
    assert b["ok"] and "outcome" not in b and b["tracker_refused"]["code"] == "in_flight"
    assert len(_tracking(db_path, rid)) == 1


# ── #11 Marketing Track ─────────────────────────────────────────────────────

def test_11_marketing_track_starts_no_sales_tracker(db_path):
    rid = _rid(db_path)
    _shown(rid, "insight_marketing:q", module="marketing", surface="marketing")
    out = _rec_event(rid, {"key": "insight_marketing:q", "event": "accepted", "surface": "marketing",
                           "module": "marketing"})
    assert "tracker" not in out and out["tracker_refused"]["code"] == "no_metric"
    assert "hidden for 14 days" in out["message"] and "nothing here Cavnar AI can measure" in out["message"]
    assert _tracking(db_path, rid) == []
    assert "marketing" not in strategy_routes.REC_TRACK_METRICS
    assert 'data-rec-event="accepted"' not in client_api.rec_controls_html("insight_marketing:q", "marketing",
                                                                          "marketing")


def test_11_a_marketing_recommendation_that_carries_a_metric_is_measured_on_it(db_path):
    rid = _rid(db_path)
    rec_ledger.present(rid, "slow_day:Tuesday", "marketing", "marketing", title="Text regulars for Tuesday",
                       expected_metric="weekday_sales:Tuesday")
    out = _rec_event(rid, {"key": "slow_day:Tuesday", "event": "accepted", "surface": "marketing",
                           "module": "marketing"})
    assert out["tracker"]["metric"] == "weekday_sales:Tuesday"
    assert out["tracker"]["module"] == "marketing"                      # #5: not labor


# ── #39 DSR actions ─────────────────────────────────────────────────────────

def test_39_dsr_done_is_measured_by_kind_and_credited_to_its_block(db_path):
    rid = _rid(db_path)
    rec_ledger.present(rid, "dsr_action:control_hours:labor", "labor", "dsr", title="Cut the 3pm overlap",
                       kind="dsr_action")
    out = _rec_event(rid, {"key": "dsr_action:control_hours:labor", "event": "completed", "surface": "dsr",
                           "module": "labor"})
    assert out["tracker"]["metric"] == "labor_pct" and out["tracker"]["module"] == "labor"


def test_39_a_dsr_kind_with_no_honest_metric_measures_nothing(db_path):
    rid = _rid(db_path)
    rec_ledger.present(rid, "dsr_action:reorder:food/brioche-buns", "food", "dsr", title="Order buns",
                       kind="dsr_action")
    out = _rec_event(rid, {"key": "dsr_action:reorder:food/brioche-buns", "event": "accepted", "surface": "dsr",
                           "module": "food"})
    # Not food cost % (the Food module's natural number): the kind decides.
    assert "tracker" not in out and out["tracker_refused"]["code"] == "no_metric"
    done = _rec_event(rid, {"key": "dsr_action:reorder:food/brioche-buns", "event": "completed",
                            "surface": "dsr", "module": "food"})
    assert "tracker" not in done and "tracker_refused" not in done
    assert _tracking(db_path, rid) == []


def test_39_reduce_waste_is_measured_on_waste_and_credited_to_food_cost(db_path, monkeypatch):
    rid = _rid(db_path)
    real = metrics.measure
    monkeypatch.setattr(metrics, "measure", lambda r, key, s, e, db_path=None:
                        (120.0, "stub") if key == "weekly_waste" else real(r, key, s, e, db_path))
    rec_ledger.present(rid, "dsr_action:reduce_waste:food/fish-tacos", "food", "dsr", title="Portion the fish")
    out = _rec_event(rid, {"key": "dsr_action:reduce_waste:food/fish-tacos", "event": "completed",
                           "surface": "dsr", "module": "food"})
    assert out["tracker"]["metric"] == "weekly_waste" and out["tracker"]["module"] == "inventory"


# ── #3 the other doors ──────────────────────────────────────────────────────

def test_3_schedule_accept_is_refused_while_labor_is_measured(db_path):
    rid = _rid(db_path)
    from schedule_intel import schedule_rec_key
    _shown(rid, schedule_rec_key("hours", "Trim about 6h on Monday"), schedule_rec_key("hours", "Trim about 4h on Friday"),
           module="schedule", surface="schedule_review")
    a, st = _route(strategy_routes._do_recommendation_event, rid,
                   {"action": "accepted", "kind": "hours", "key": "Trim about 6h on Monday"})
    assert st == 200 and a["tracker"]["metric"] == "labor_pct" and a["tracker"]["module"] == "labor"
    b, _ = _route(strategy_routes._do_recommendation_event, rid,
                  {"action": "accepted", "kind": "hours", "key": "Trim about 4h on Friday"})
    assert b["tracker_refused"]["code"] == "in_flight"
    assert len(_tracking(db_path, rid)) == 1


def test_3_reprice_is_refused_while_food_cost_is_measured(db_path):
    rid = _rid(db_path)
    # A supplier order is informational now (CA2 #7): it is routine buying,
    # not a change aimed at food cost %, so it no longer claims the number
    # and never blocks a real change on it.
    order = outcomes.observe(rid, "supplier_order_sent", user_id=1)
    assert order and order["module"] == "inventory" and order["informational"]
    live = outcomes.record(rid, "manual", "manual:food", "Tighten portions", "food_cost_pct", user_id=1)
    got = client_api.track_reprice(rid, user_id=1)
    assert client_api.tracker_fields(got)["tracker_refused"]["in_flight"]["id"] == live["id"]
    assert [m for _k, m, _mod in _tracking(db_path, rid)] == ["food_cost_pct", "food_cost_pct"]


def test_3_reprice_alone_starts_and_is_credited_to_food_cost(db_path):
    rid = _rid(db_path)
    got = client_api.track_reprice(rid, user_id=1)
    assert got["tracker"]["metric"] == "food_cost_pct" and got["tracker"]["module"] == "inventory"


def test_3_a_second_campaign_on_the_same_weekday_is_refused(db_path):
    rid = _rid(db_path)
    outcomes.record(rid, "slow_day_campaign", "campaign:Tuesday:2026-01-01", "Earlier text",
                    "weekday_sales:Tuesday", today=TODAY - timedelta(days=3))
    got = client_api._track_campaign_outcome(rid, {"target_day": "tuesday"}, {"ok": True}, 1)
    assert got["tracker_refused"]["code"] == "in_flight"
    # A campaign is an automatic start, so the FAMILY gate (re-audit A18):
    # another weekday's sales is the same sales money while Tuesdays are
    # measured, and the family rule would count only one of the two anyway.
    wed = client_api._track_campaign_outcome(rid, {"target_day": "wednesday"}, {"ok": True}, 1)
    assert wed["tracker_refused"]["code"] == "in_flight"
    conn = get_conn(db_path)
    conn.execute("UPDATE recommendation_outcomes SET status='abandoned' WHERE restaurant_id=?", (rid,))
    conn.commit()
    conn.close()
    wed = client_api._track_campaign_outcome(rid, {"target_day": "wednesday"}, {"ok": True}, 1)
    assert wed["tracker"]["metric"] == "weekday_sales:Wednesday" and wed["tracker"]["module"] == "marketing"


def test_3_ask_is_told_it_is_already_measured(db_path):
    import ask_cavnar_tools as tools
    rid = _rid(db_path)
    first = tools._track_outcome(rid, title="Cut the Tuesday close", metric="labor_pct")
    assert first["tracker"]["metric"] == "labor_pct"
    second = tools._track_outcome(rid, title="Tuesday close trimmed", metric="labor_pct")
    assert second["already_tracking"] and second["tracker_refused"]["code"] == "in_flight"
    assert len(_tracking(db_path, rid)) == 1


def test_3_an_alert_read_never_blocks_a_real_change(db_path):
    rid = _rid(db_path)
    read = outcomes.observe(rid, "alert_labor_over", user_id=1)
    assert read and read["informational"]
    real, _ = _route(strategy_routes._do_outcome_record, rid,
                     {"source": "manual", "title": "Trim", "metric": "labor_pct"})
    assert real["tracker"]["metric"] == "labor_pct"
    # ...and the real one blocks the next observed action on labor %.
    assert outcomes.observe(rid, "schedule_published", user_id=1) is None


def test_3_record_itself_refuses(db_path):
    rid = _rid(db_path)
    outcomes.record(rid, "manual", "a", "A", "labor_pct")
    with pytest.raises(outcomes.TrackerRefused):
        outcomes.record(rid, "manual", "b", "B", "labor_pct")
    assert outcomes.start(rid, "manual", "b", "B", "labor_pct")["tracker_refused"]["code"] == "in_flight"
    # A different metric in the family passes the metric gate, not the family one.
    assert outcomes.start(rid, "manual", "c", "C", "overtime_hours")["ok"] is True
    assert outcomes.start(rid, "manual", "d", "D", "labor_pct", gate="family")["ok"] is False


# ── contracts: GET /outcomes and GET /value ─────────────────────────────────

_ROW_FIELDS = {"module", "baseline_value", "after_value", "delta", "delta_pct", "baseline_kind", "attribution",
               "attribution_label", "concurrent", "recheck_on", "recheck_verdict", "validated"}


def test_contract_outcomes_rows(db_path):
    rid = _rid(db_path)
    t0 = TODAY - timedelta(days=40)
    done = outcomes.record(rid, "manual", "old", "Trim Monday", "labor_pct", today=t0)
    outcomes.evaluate(done["id"], today=t0 + timedelta(days=28))
    outcomes.record(rid, "manual", "new", "Cap overtime", "overtime_hours", today=TODAY - timedelta(days=5))
    out, st = _route(strategy_routes._do_outcomes_list, rid)
    assert st == 200
    rows = {r["source_key"]: r for r in out["outcomes"]}
    for r in rows.values():
        assert _ROW_FIELDS <= set(r), _ROW_FIELDS - set(r)
    ev = rows["old"]
    assert ev["attribution"] in outcomes.ATTRIBUTION and isinstance(ev["concurrent"], list)
    assert ev["baseline_kind"] in outcomes.BASELINE_KINDS and ev["summary"]
    live = rows["new"]
    assert live["attribution"] is None and live["attribution_label"] is None
    assert set(live["interim"]) >= {"value", "delta", "delta_pct", "as_of", "days_in"}
    assert live["interim"]["as_of"] == (TODAY - timedelta(days=1)).isoformat() and live["interim"]["days_in"] == 5


def test_contract_value_payload(db_path):
    import value_delivered
    rid = _rid(db_path)
    out, st = _route(strategy_routes._do_value, rid)
    assert st == 200
    d = out["delivered"]
    assert {"net_monthly", "worsened", "validated_monthly", "cumulative", "rates"} <= set(d)
    assert set(d["worsened"]) == {"count", "monthly", "priced_count"}
    assert {"total", "since", "days", "by_module"} <= set(d["cumulative"])
    assert d["cumulative"]["total"] is None                       # nothing measured yet
    assert d["rates"]["reply_rate"] == value_delivered.REPLY_RATE == out["rates"]["reply_rate"]


def test_metrics_route_lists_the_new_metrics(db_path):
    rid = _rid(db_path)
    out, _ = _route(strategy_routes._do_metrics, rid)
    keys = [m["key"] for m in out["metrics"]]
    assert "overtime_hours" in keys and "response_hours" in keys


# ── the daily job ───────────────────────────────────────────────────────────

def test_the_recheck_job_is_scheduled_registered_and_runs(db_path):
    import admin_ops
    import strategy_jobs
    with open(os.path.join(ROOT, "scheduler.py"), encoding="utf-8") as f:
        src = f.read()
    assert 'claim_period("outcome_rechecks"' in src and "run_outcome_rechecks" in src
    assert admin_ops.RUNNABLE_JOBS["outcome_rechecks"]["target"] == ("strategy_jobs", "run_outcome_rechecks")
    assert not admin_ops.RUNNABLE_JOBS["outcome_rechecks"].get("sends")
    rid = _rid(db_path)
    t0 = TODAY - timedelta(days=100)
    conn = get_conn(db_path)
    for i in range(-28, 100):          # 30% for the 28 days before t0, 25% from t0 on
        d = t0 + timedelta(days=i)
        conn.execute("INSERT OR REPLACE INTO labor_daily_history (restaurant_id, date, day_of_week, labor_cost, "
                     "sales, labor_pct) VALUES (?,?,?,?,?,?)",
                     (rid, d.isoformat(), d.strftime("%A"), 300.0 if i < 0 else 250.0, 1000.0,
                      30.0 if i < 0 else 25.0))
    conn.commit()
    conn.close()
    o = outcomes.record(rid, "manual", "trim", "Trim", "labor_pct", today=t0)
    outcomes.evaluate(o["id"], today=t0 + timedelta(days=28))
    got = strategy_jobs.run_outcome_rechecks(db_path=db_path)
    assert got["rechecked"] == 1 and got["days_accrued"] > 0
    assert outcomes.get_outcome(o["id"])["recheck_verdict"] in outcomes.RECHECK_VERDICTS
    assert strategy_jobs.run_outcome_rechecks(db_path=db_path)["rechecked"] == 0     # once
