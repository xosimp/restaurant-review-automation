"""Recommendation ROI audit — the learning half.

#2 the success rate (unknown is not a failure), #8 the ledger as the
engine's input (no double counting, snooze is a snooze, Ask keyed by its
proposal), #24/#47/#29 the per-restaurant effectiveness model in every
ranker (bounded, never over a critical item, a worse result downweights and
decays), #43 calibration, #45 cohort priors in card confidence (the floor
and the anonymity check), #46 the reviewed promotion of an experiment's
winner.
"""
import json
import sys
from datetime import datetime, timedelta

import pytest

import models
import rec_ledger as rl
import rec_learning
from models import Restaurant, create_restaurant

# Imported before the fixture patches get_conn (the bound-import hazard).
import intelligence  # noqa: E402
from intelligence import feedback, scoring, memory, confidence, privacy  # noqa: E402
import home_brief  # noqa: E402
import business_intelligence as bi  # noqa: E402
import schedule_experiments as sx  # noqa: E402


@pytest.fixture
def db(db_path, monkeypatch):
    real = models.get_conn

    def conn(*a, **k):
        return real(db_path)
    for mod in list(sys.modules.values()):
        if mod is not None and getattr(mod, "get_conn", None) is real:
            monkeypatch.setattr(mod, "get_conn", conn)
    monkeypatch.setattr(models, "get_conn", conn)
    monkeypatch.setattr(models, "DB_PATH", db_path)
    monkeypatch.delenv(sx.PIN_ENV, raising=False)
    import auth
    auth.init_auth(db_path=db_path)
    return db_path


def _rid(db, name="Learn Co"):
    return create_restaurant(Restaurant(name=name, owner_email=f"{name.split()[0].lower()}@x.test",
                                        module_labor=1), db_path=db)


def _q(db, sql, args=()):
    c = models.get_conn(db)
    try:
        return c.execute(sql, args).fetchall()
    finally:
        c.close()


def _x(db, sql, args=()):
    c = models.get_conn(db)
    try:
        c.execute(sql, args)
        c.commit()
    finally:
        c.close()


def _tracker(db, rid, key, verdict, dollars=None, days_ago=40):
    c = models.get_conn(db)
    try:
        cur = c.execute(
            "INSERT INTO recommendation_outcomes (restaurant_id, source, source_key, title, metric, started_on, "
            "evaluate_on, status, verdict, dollars_monthly, created_at) VALUES (?, 'recommendation', ?, 't', "
            "'labor_pct', date('now', ?), date('now', ?), 'evaluated', ?, ?, datetime('now', ?))",
            (rid, key, f"-{days_ago} days", f"-{days_ago - 28} days", verdict, dollars, f"-{days_ago} days"))
        c.commit()
        return cur.lastrowid
    finally:
        c.close()


def _taken(db, rid, key, verdict=None, dollars=None, predicted=None, module="labor", days_ago=40):
    """An episode shown, taken (Track), and — when `verdict` — measured."""
    rec = rl.present(rid, key, module, "home", dollar_value=predicted, db_path=db)
    _x(db, "UPDATE rec_instances SET created_at=datetime('now', ?) WHERE rec_id=?", (f"-{days_ago} days", rec))
    _x(db, "UPDATE rec_events SET at=datetime('now', ?) WHERE rec_id=?", (f"-{days_ago} days", rec))
    rl.record(rid, key, "accepted", surface="home", db_path=db)
    if verdict:
        oid = _tracker(db, rid, key, verdict, dollars=dollars, days_ago=days_ago - 1)
        _x(db, "UPDATE rec_instances SET tracker_id=? WHERE rec_id=?", (oid, rec))
    return rec


def _ignored(db, rid, key, module="labor"):
    rec = rl.present(rid, key, module, "home", db_path=db)
    _x(db, "UPDATE rec_instances SET created_at=datetime('now','-20 days') WHERE rec_id=?", (rec,))
    rl.expire_stale(db_path=db)
    return rec


# ── #2 the success rate ─────────────────────────────────────────────────────

def test_unknown_is_not_a_failure_and_no_clear_change_is_reported_on_its_own(db):
    rid = _rid(db)
    for i, v in enumerate(("improved", "improved", "no_clear_change", "unknown", "unknown", "unknown")):
        feedback.record(rid, "trim_day", f"trim_day:{i}", "measured", outcome=v, days_to_effect=14, db_path=db)
    s = scoring.kind_stats("trim_day", restaurant_id=rid, db_path=db)
    assert s["measured"] == 3 and s["unknown"] == 3 and s["no_clear_change"] == 1
    assert s["success_rate"] == round(2 / 3, 3)                # was 2/6 when unknown counted as failure
    # the own-record factor quotes the clear results only
    c = confidence.score(rid, "trim_day", restaurant=models.get_restaurant(rid), db_path=db)
    own = next(f for f in c["factors"] if f["name"] == "restaurant_history")
    assert "2 of 3 measured improved" in own["note"]


def test_one_recommendation_answered_three_ways_is_one_taken_and_ignored_is_in_the_denominator(db):
    rid = _rid(db)
    for a in ("accepted", "done", "implemented"):
        feedback.record(rid, "reprice", "reprice:Soup", a, db_path=db)
    feedback.record(rid, "reprice", "reprice:Pasta", "ignored", db_path=db)
    feedback.record(rid, "reprice", "reprice:Tart", "snoozed", db_path=db)
    s = scoring.kind_stats("reprice", restaurant_id=rid, db_path=db)
    assert s["accepted"] == 1 and s["ignored"] == 1 and s["snoozed"] == 1
    assert s["acceptance_rate"] == 0.5                         # 1 taken of 1 taken + 1 ignored; a snooze is neither


# ── #8 the ledger feeds the engine ──────────────────────────────────────────

def test_answers_from_every_surface_reach_the_engine_once(db):
    rid = _rid(db)
    # A Home "not for us" writes home_dismissals AND the ledger: one event.
    home_brief.dismiss(rid, "trim_day:Monday", kind="not_for_us")
    # Food, DSR and Reviews answers exist only in the ledger.
    for key, mod, surface in (("insight_food:abcdef1234", "food", "food"),
                              ("dsr_action:control_hours:labor", "labor", "dsr"),
                              ("diag_review:service", "reviews", "reviews")):
        rl.present(rid, key, mod, surface, db_path=db)
    rl.record(rid, "insight_food:abcdef1234", "completed", surface="food", db_path=db)
    rl.record(rid, "dsr_action:control_hours:labor", "dismissed", surface="dsr", meta={"kind": "hide"}, db_path=db)
    rl.record(rid, "diag_review:service", "implemented", surface="reviews", db_path=db)
    out = feedback.sync(db_path=db, cohorts={rid: "pizza"})
    got = {(r["source_key"], r["action"]) for r in _q(db, "SELECT source_key, action FROM intel_rec_events")}
    assert got == {("trim_day:Monday", "not_for_us"), ("insight_food:abcdef1234", "done"),
                   ("dsr_action:control_hours:labor", "hidden"), ("diag_review:service", "implemented")}
    assert out["events"] == 4 and out["from_ledger"] == 3
    assert feedback.sync(db_path=db)["events"] == 0             # idempotent, and the cursor moved on


def test_a_snooze_is_learned_as_a_snooze_and_an_old_hidden_one_is_repaired(db):
    rid = _rid(db)
    home_brief.dismiss(rid, "labor_over:2026-09-01", kind="snooze", days=1)
    # what the old sync wrote for it
    feedback.record(rid, "labor_over", "labor_over:2026-09-01", "hidden", synced_from="home_dismissals", db_path=db)
    feedback.sync(db_path=db)
    acts = {r["action"] for r in _q(db, "SELECT action FROM intel_rec_events WHERE source_key='labor_over:2026-09-01'")}
    assert acts == {"snoozed"}
    rl.present(rid, "trim_day:Friday", "labor", "queue", db_path=db)
    rl.record(rid, "trim_day:Friday", "snoozed", surface="queue", db_path=db)
    feedback.sync(db_path=db)
    assert _q(db, "SELECT action FROM intel_rec_events WHERE source_key='trim_day:Friday'")[0]["action"] == "snoozed"


def test_ask_answers_are_keyed_by_their_proposal_like_every_other_reader(db):
    rid = _rid(db)
    c = models.get_conn(db)
    cur = c.execute("INSERT INTO ask_cavnar_actions (restaurant_id, action, summary, outcome) VALUES (?,?,?,?)",
                    (rid, "draft_campaign", "Patio text", "proposed"))
    pid = cur.lastrowid
    c.execute("INSERT INTO ask_cavnar_actions (restaurant_id, action, summary, outcome, proposal_id) VALUES (?,?,?,?,?)",
              (rid, "draft_campaign", "Patio text", "confirmed", pid))
    c.commit(); c.close()
    # the legacy key the old sync wrote for the same answer
    feedback.record(rid, "ask:draft_campaign", "ask:draft_campaign:Patio text", "confirmed",
                    synced_from="ask_cavnar_actions", db_path=db)
    rl.record(rid, f"ask:{pid}", "accepted", surface="ask", db_path=db)       # the ledger's copy
    feedback.sync(db_path=db)
    rows = _q(db, "SELECT source_key, rec_kind, action FROM intel_rec_events")
    assert [(r["source_key"], r["rec_kind"], r["action"]) for r in rows] == [(f"ask:{pid}", "ask:draft_campaign",
                                                                            "confirmed")]


def test_the_ledger_pass_is_bounded_and_resumes_from_its_cursor(db, monkeypatch):
    rid = _rid(db)
    for d in ("Monday", "Tuesday", "Wednesday"):
        rl.present(rid, f"trim_day:{d}", "labor", "home", db_path=db)
        rl.record(rid, f"trim_day:{d}", "completed", surface="home", db_path=db)
    monkeypatch.setattr(feedback, "LEDGER_EVENTS_PER_PASS", 2)
    assert feedback.sync(db_path=db)["from_ledger"] == 2
    assert feedback.sync(db_path=db)["from_ledger"] == 1                # the tail, next pass
    assert feedback.sync(db_path=db)["from_ledger"] == 0


# ── #24 / #47 / #29 the effectiveness model ─────────────────────────────────

def test_with_nothing_learned_every_weight_is_one(db):
    rid = _rid(db)
    m = rec_learning.effectiveness(rid, db_path=db)
    assert m("trim_day:Saturday") == (1.0, [])


def test_what_worked_here_lifts_the_kind_within_the_bound(db):
    rid = _rid(db)
    for d in ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"):
        _taken(db, rid, f"trim_day:{d}", "improved")
    w, why = rec_learning.effectiveness(rid, db_path=db)("trim_day:Monday")
    assert 1.0 < w <= rec_learning.MAX_WEIGHT and why
    # ignored, again and again, pushes a kind down — never below the floor
    other = _rid(db, "Quiet Co")
    for i in range(12):
        _ignored(db, other, f"post_this_week:{i}", module="marketing")
    w2, _ = rec_learning.effectiveness(other, db_path=db)("post_this_week:13")
    assert rec_learning.MIN_WEIGHT <= w2 < 1.0


def test_a_worse_result_downweights_the_key_and_kind_and_decays(db):
    rid = _rid(db)
    _taken(db, rid, "trim_day:Monday", "worsened", days_ago=35)
    m = rec_learning.effectiveness(rid, db_path=db)
    w_key, why = m("trim_day:Monday")
    w_kind, _ = m("trim_day:Friday")
    assert w_key < w_kind < 1.0 + 1e-9 and w_key >= rec_learning.FLOOR_WEIGHT
    assert any("worse" in y for y in why)
    # a year and more later it has all but faded
    later = rec_learning.effectiveness(rid, db_path=db, now=datetime.utcnow() + timedelta(days=330))
    assert later("trim_day:Monday")[0] > w_key


def test_estimates_that_ran_high_are_weighed_down_once_there_are_enough_pairs(db):
    rid = _rid(db)
    for i, d in enumerate(("Monday", "Tuesday", "Wednesday", "Thursday")):
        _taken(db, rid, f"cut_waste:Item{i}", "improved", dollars=100.0, predicted=400.0, module="food")
    m = rec_learning.effectiveness(rid, db_path=db)
    assert len(m.calibration["cut_waste"]) == 4
    w, why = m("cut_waste:Item9")
    assert any("the estimate" in y for y in why)
    assert w <= rec_learning.MAX_WEIGHT


def test_the_cohort_prior_is_used_only_over_the_floor_and_only_anonymous(db, monkeypatch):
    rid = _rid(db)
    models.update_restaurant(rid, {"category": "pizza"})
    calls = []

    def fake(kind, cohort=None, restaurant_id=None, db_path=None):
        calls.append(cohort)
        return {"restaurants": 7, "available": True, "answered": 40, "measured": 20,
                "acceptance_rate_shrunk": 0.8, "success_rate_shrunk": 0.7}
    monkeypatch.setattr(intelligence, "recommendation_success", fake)
    m = rec_learning.effectiveness(rid, db_path=db)
    assert m.prior("trim_day") == (0.8, 0.7) and calls == ["pizza"]
    # below the floor, or carrying an identity, the prior is even
    monkeypatch.setattr(intelligence, "recommendation_success",
                        lambda *a, **k: {"restaurants": 4, "available": False, "answered": 9, "measured": 9})
    assert rec_learning.effectiveness(rid, db_path=db).prior("trim_day") == (0.5, 0.5)
    monkeypatch.setattr(intelligence, "recommendation_success",
                        lambda *a, **k: {"restaurants": 9, "available": True, "restaurant_id": 3, "answered": 9})
    assert rec_learning.effectiveness(rid, db_path=db).prior("trim_day") == (0.5, 0.5)


def test_home_orders_by_the_learned_weight_and_says_why():
    class Learned:
        def __call__(self, key):
            return (1.25, ["trim_day: taken 8 of 10"]) if key.startswith("trim_day") else (0.8, ["ignored"])
    recs = [{"key": "cut_waste:Salmon", "timeframe": "This week", "dollars_monthly": 100, "effort": "low"},
            {"key": "trim_day:Monday", "timeframe": "This week", "dollars_monthly": 90, "effort": "low"}]
    out = home_brief.order_recommendations([dict(r) for r in recs], learned=Learned())
    assert [r["key"] for r in out] == ["trim_day:Monday", "cut_waste:Salmon"]
    assert out[0]["learned"]["weight"] == 1.25
    plain = home_brief.order_recommendations([dict(r) for r in recs])
    assert [r["key"] for r in plain] == ["cut_waste:Salmon", "trim_day:Monday"]       # unchanged without it


def test_nothing_learned_moves_anything_past_or_below_a_critical_one_thing(db):
    rid = _rid(db)
    cands = [{"key": "urgent_reviews", "urgency": "critical", "score": 500.0, "dollars_monthly": None},
             {"key": "cut_waste:Salmon", "urgency": "normal", "score": 480.0, "dollars_monthly": 480},
             {"key": "trim_day:Monday", "urgency": "important", "score": 300.0, "dollars_monthly": 150}]

    def boost(key):
        return (1.25, ["x"]) if key != "urgent_reviews" else (0.6, ["x"])
    assert bi.pick_one_thing(rid, cands, db_path=db, learned=boost)["key"] == "urgent_reviews"
    # below the critical item, the weight reorders
    cands2 = [c for c in cands if c["urgency"] != "critical"]
    pick = bi.pick_one_thing(rid, cands2, db_path=db,
                             learned=lambda k: (1.25, ["x"]) if k.startswith("trim_day") else (0.6, ["y"]))
    assert pick["key"] == "trim_day:Monday" and pick["learned"]["weight"] == 1.25


def test_the_daily_report_ranks_its_actions_with_the_model(db, monkeypatch):
    from dsr import narrative
    seen = []

    class Learned:
        def __call__(self, key):
            seen.append(key)
            return (1.0, [])
    monkeypatch.setattr(rec_learning, "effectiveness", lambda rid, db_path=None, **k: Learned())
    rid = _rid(db)
    ctx = type("Ctx", (), {"restaurant_id": rid, "db_path": db})()
    F = type("F", (), {"entity": staticmethod(lambda subject, cites: None)})()
    acts = [{"text": "Cut a server Tuesday lunch", "kind": "control_hours", "cites": ["labor.pct"],
             "urgency": "this_week", "effort": "low", "dollars_monthly": None}]
    out = narrative.settle_actions(acts, F, ctx, (set(), set(), set()), [])
    assert out and seen == [out[0]["key"]]


# ── #43 calibration (admin only) ─────────────────────────────────────────────

def test_calibration_compares_the_shown_figure_with_what_was_measured(db):
    import admin_ops
    rid = _rid(db)
    _taken(db, rid, "trim_day:Monday", "improved", dollars=150.0, predicted=300.0)
    _taken(db, rid, "trim_day:Friday", "no_clear_change", predicted=200.0)
    _taken(db, rid, "trim_day:Sunday", "improved", dollars=None, predicted=100.0)     # a metric with no $ reading
    out = admin_ops.recommendation_calibration(days=365)
    k = next(r for r in out["by_kind"] if r["kind"] == "trim_day")
    assert k["n"] == 2 and k["unpriced"] == 1 and k["predicted"] == 500.0 and k["realised"] == 150.0
    assert k["ratio"] == 0.3 and k["enough"] is False
    shown = json.loads(_q(db, "SELECT meta FROM rec_events WHERE event='shown' AND key='trim_day:Monday'")[0]["meta"])
    assert shown["dollar_value"] == 300.0                       # the figure each showing carried


# ── #45 cohort priors in card confidence ─────────────────────────────────────

def _prior(value, restaurants=6, measured=12, scope="cohort"):
    return {"factors": [{"name": "platform_evidence", "value": value, "scope": scope, "restaurants": restaurants,
                         "measured": measured, "note": f"9 of {measured} measured across {restaurants} restaurants improved"}]}


def test_the_cohort_prior_moves_a_band_once_the_floor_is_met():
    c = confidence.card_confidence("medium", "four weeks of shifts", _prior(0.82))
    assert c["band"] == "high" and c["basis"] == "cohort" and "restaurants like yours" in c["reason"]
    assert confidence.card_confidence("medium", "x", _prior(0.2))["band"] == "low"
    # below the cohort floor or on too few results it says nothing
    assert confidence.card_confidence("medium", "x", _prior(0.9, restaurants=4))["adjusted"] is None
    assert confidence.card_confidence("medium", "x", _prior(0.9, measured=3))["adjusted"] is None
    # this restaurant's own measured record outranks the cohort
    own = _prior(0.9)
    own["factors"].append({"name": "restaurant_history", "value": 0.2, "note": "this restaurant: 1 of 5 measured improved"})
    c = confidence.card_confidence("medium", "x", own)
    assert c["band"] == "low" and c["basis"] == "own"


def test_the_prior_factor_is_refused_if_it_carries_an_identity():
    bad = _prior(0.9)
    bad["factors"][0]["restaurant_name"] = "Luigi's"
    with pytest.raises(privacy.PrivacyError):
        confidence.card_confidence("medium", "x", bad)


def test_score_builds_the_prior_only_over_the_floor_and_anonymous(db):
    rids = [_rid(db, f"P{i} Co") for i in range(6)]
    for r in rids:
        for i in range(3):
            feedback.record(r, "trim_day", f"trim_day:{i}", "measured", outcome="improved", cohort="pizza",
                            days_to_effect=14, db_path=db)
    c = confidence.score(rids[0], "trim_day", cohort="pizza", restaurant=models.get_restaurant(rids[0]), db_path=db)
    f = next(x for x in c["factors"] if x["name"] == "platform_evidence")
    privacy.assert_anonymous(f)
    assert f["scope"] == "cohort" and f["restaurants"] == 6 and f["measured"] == 18


# ── #46 the reviewed promotion ──────────────────────────────────────────────

def _winner(arm="solver", state="winner"):
    return {"experiments": [{"key": sx.EXPERIMENTS[0]["key"],
                             "verdict": {"call": arm if state == "winner" else None, "state": state,
                                         "text": "Solver assignment leads on acceptance."}}]}


def test_only_the_called_winner_can_be_promoted_and_it_becomes_every_restaurants_arm(db, monkeypatch):
    key = sx.EXPERIMENTS[0]["key"]
    rid = _rid(db)
    assert not sx.promote(key, "solver", promoted_by="will", db_path=db, _readout=_winner(state="insufficient"))["ok"]
    assert not sx.promote(key, "model", promoted_by="will", db_path=db, _readout=_winner("solver"))["ok"]
    out = sx.promote(key, "solver", promoted_by="will", note="90% interval cleared", db_path=db, _readout=_winner())
    assert out["ok"]
    assert not sx.promote(key, "solver", db_path=db, _readout=_winner())["ok"]           # one at a time
    for week in ("2026-10-05", "2026-10-12", "2026-10-19"):
        a = sx.arms_for(rid, week, db_path=db)[0]
        assert a["arm"] == "solver" and a["pinned"] and a["pin_source"] == "promoted"
    row = sx.active_promotions(db_path=db)[key]
    assert row["promoted_by"] == "will" and "leads" in row["verdict"] and row["flags"] == {"solver": True}
    # the kill switches still win
    monkeypatch.setenv(sx.PIN_ENV, "off")
    assert sx.arms_for(rid, "2026-10-05", db_path=db)[0]["arm"] == sx.EXPERIMENTS[0]["control"]
    monkeypatch.delenv(sx.PIN_ENV)
    assert sx.revert(key, reverted_by="will", db_path=db)["ok"]
    assert not sx.arms_for(rid, "2026-10-05", db_path=db)[0]["pinned"]
    assert sx.promotions(key, db_path=db)[0]["reverted_by"] == "will"                  # the trail stays


def test_a_promotion_holds_after_the_experiment_is_retired_in_code(db, monkeypatch):
    key = sx.EXPERIMENTS[0]["key"]
    rid = _rid(db)
    sx.promote(key, "model", promoted_by="will", db_path=db, _readout=_winner("model"))
    retired = tuple(dict(e, active=False) for e in sx.EXPERIMENTS)
    monkeypatch.setattr(sx, "EXPERIMENTS", retired)
    arms = sx.arms_for(rid, "2026-10-05", db_path=db)
    assert arms == [{"experiment": key, "arm": "model", "pinned": True, "pin_source": "promoted",
                     "flags": {"solver": False}}]
    assert sx.flag(arms, "solver") is False                    # the stored setting, not DEFAULT_FLAGS


def test_the_admin_promote_and_revert_routes(db, monkeypatch):
    import auth
    import admin_routes
    from flask import Flask
    monkeypatch.setattr(auth, "get_current_user", lambda: {"id": 1, "is_admin": 1, "role": "admin", "username": "will"})
    monkeypatch.setattr(sx, "readout", lambda db_path=None: _winner())
    app = Flask(__name__, template_folder="../templates")
    app.register_blueprint(admin_routes.admin_bp)
    key = sx.EXPERIMENTS[0]["key"]
    with app.test_request_context(json={"experiment": key, "arm": "solver", "note": "reviewed"}):
        resp = admin_routes.admin_api_schedule_experiment_promote()
    body = (resp.get_json() if hasattr(resp, "get_json") else resp[0].get_json())
    assert body["ok"] and sx.active_promotions(db_path=db)[key]["promoted_by"] == "will"
    with app.test_request_context(json={"experiment": key}):
        resp = admin_routes.admin_api_schedule_experiment_revert()
    assert (resp.get_json() if hasattr(resp, "get_json") else resp[0].get_json())["ok"]
    assert sx.active_promotions(db_path=db) == {}
