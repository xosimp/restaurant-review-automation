"""Recommendation ROI audit — the owner's own record, over HTTP.

GET /recs/summary, GET /recs/timeline, POST /recs/checkin (web /api and
mobile /mobile/api through strategy_routes._ROUTES), and the structured
reason on POST /recs/event and /api/home/dismiss. Restaurant-scoped, and
redacted per viewer: a manager never sees a loss, a food-cost or an
owner-only recommendation.
"""
import json
import sys

import pytest
from flask import Flask

import auth
import models
import rec_ledger as rl
from models import Restaurant, create_restaurant


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
    monkeypatch.setattr(auth, "DB_PATH", db_path)
    auth.init_auth(db_path=db_path)
    return db_path


@pytest.fixture
def client(db):
    from strategy_routes import strategy_bp, strategy_mobile_bp
    app = Flask(__name__, template_folder="../templates")
    app.register_blueprint(strategy_bp)
    app.register_blueprint(strategy_mobile_bp)
    return app.test_client()


def _as(monkeypatch, rid, role="client", uid=7):
    """The same login on the web (session) and the phone (bearer token)."""
    user = {"id": uid, "restaurant_id": rid, "base_restaurant_id": rid, "is_admin": 0, "role": role,
            "username": "u", "email": "u@x.com"}
    monkeypatch.setattr(auth, "get_current_user", lambda: user)
    monkeypatch.setattr(auth, "get_session_user", lambda token, *a, **k: user if token == "t" else None)
    return user


PHONE = {"Authorization": "Bearer t"}


def _rid(db, name="Route Co"):
    return create_restaurant(Restaurant(name=name, owner_email=f"{name.split()[0].lower()}@x.test"), db_path=db)


def _x(db, sql, args=()):
    c = models.get_conn(db)
    try:
        c.execute(sql, args)
        c.commit()
    finally:
        c.close()


def _q(db, sql, args=()):
    c = models.get_conn(db)
    try:
        return c.execute(sql, args).fetchall()
    finally:
        c.close()


def _ignored(db, rid, key, module):
    rec = rl.present(rid, key, module, "home", db_path=db)
    _x(db, "UPDATE rec_instances SET created_at=datetime('now','-20 days') WHERE rec_id=?", (rec,))
    rl.expire_stale(db_path=db)


def _measured(db, rid, key, module, verdict, slot=0):
    """A taken recommendation measured over its own window: `slot` n reads
    the 28 days ending 30n days earlier — results over the same weeks on one
    number are one change (re-audit B13), so distinct results need distinct
    windows."""
    rec = rl.present(rid, key, module, "home", db_path=db)
    rl.record(rid, key, "accepted", surface="home", db_path=db)
    c = models.get_conn(db)
    cur = c.execute("INSERT INTO recommendation_outcomes (restaurant_id, source, source_key, title, metric, started_on, "
                    "evaluate_on, status, verdict) VALUES (?, 'recommendation', ?, 't', 'labor_pct', date('now', ?), "
                    "date('now', ?), 'evaluated', ?)",
                    (rid, key, f"-{30 + 30 * slot} days", f"-{2 + 30 * slot} days", verdict))
    c.execute("UPDATE rec_instances SET tracker_id=? WHERE rec_id=?", (cur.lastrowid, rec))
    c.commit(); c.close()


# ── GET /recs/summary ────────────────────────────────────────────────────────

def test_the_summary_contract_with_ignored_in_the_denominator(client, db, monkeypatch):
    rid = _rid(db)
    _as(monkeypatch, rid)
    for d in ("Monday", "Tuesday", "Wednesday"):
        rl.present(rid, f"trim_day:{d}", "labor", "home", db_path=db)
        rl.record(rid, f"trim_day:{d}", "accepted", db_path=db)
    rl.present(rid, "trim_day:Thursday", "labor", "home", db_path=db)
    rl.record(rid, "trim_day:Thursday", "dismissed", meta={"kind": "not_for_us"}, db_path=db)
    _ignored(db, rid, "trim_day:Friday", "labor")
    rl.present(rid, "trim_day:Sunday", "labor", "home", db_path=db)            # open, fresh: not settled yet
    body = client.get("/api/recs/summary?days=30").get_json()
    assert set(body) >= {"ok", "days", "since", "by_module", "by_tag", "most_effective"}
    lab = body["by_module"]["labor"]
    assert {k: lab[k] for k in ("shown", "answered", "accepted", "dismissed", "ignored", "n")} == \
        {"shown": 6, "answered": 4, "accepted": 3, "dismissed": 1, "ignored": 1, "n": 5}
    assert lab["accept_rate"] == 0.6 and lab["accept_rate_low"] < 0.6 < lab["accept_rate_high"]
    assert lab["enough"] is False                                 # 5 settled, fewer than MIN_SETTLED_FOR_RATE
    assert client.get("/mobile/api/recs/summary?days=90", headers=PHONE).get_json()["days"] == 90       # the mobile twin
    assert client.get("/api/recs/summary?days=45").status_code == 400


def test_success_per_tag_leaves_unknown_out_and_names_the_most_effective(client, db, monkeypatch):
    rid = _rid(db)
    _as(monkeypatch, rid)
    for i, v in enumerate(["improved"] * 5 + ["worsened", "unknown", "unknown"]):
        day = ("Saturday", "Sunday", "Friday")[i % 3]
        _measured(db, rid, f"schedule_coverage:{day} dinner gap {i}", "schedule", v, slot=i)
    for i, v in enumerate(["improved", "no_clear_change", "worsened", "worsened", "improved"]):
        _measured(db, rid, f"trim_day:{('Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Monday')[i]}#{i}", "labor", v,
                  slot=10 + i)
    body = client.get("/api/recs/summary?days=30").get_json()
    wk = next(t for t in body["by_tag"] if t["tag"] == "focus:weekend_staffing")
    assert wk["label"] == "Weekend staffing" and wk["module"] == "schedule"
    assert (wk["measured"], wk["improved"], wk["worsened"], wk["unknown"]) == (6, 5, 1, 2)
    assert wk["success_rate"] == round(5 / 6, 3) and wk["enough"] is True
    wd = next(t for t in body["by_tag"] if t["tag"] == "focus:weekday_staffing")
    assert wd["no_clear_change"] == 1 and wd["success_rate"] == 0.4
    best = body["most_effective"]
    assert best["success_rate"] >= 0.5 and best["measured"] >= 5 and best["module"] == "schedule"


def test_a_manager_never_sees_loss_food_or_owner_only_recommendations(client, db, monkeypatch):
    rid = _rid(db)
    rl.present(rid, "trim_day:Monday", "labor", "home", db_path=db)
    rl.present(rid, "cut_waste:Salmon", "food", "home", db_path=db)
    rl.present(rid, "reprice:Soup", "home", "brief_email", db_path=db)                  # food by its kind
    rl.present(rid, "loss:2026-W38:comps:ana", "ops", "issue_sms", db_path=db)
    rl.present(rid, "dsr_action:adjust_pricing:sales", "ops", "dsr", owner_only=True, db_path=db)
    rl.present(rid, "money:food_cost", "home", "weekly_email", db_path=db)
    _as(monkeypatch, rid, role="manager")
    keys = {i["key"] for i in client.get("/api/recs/timeline").get_json()["items"]}
    assert keys == {"trim_day:Monday"}
    assert set(client.get("/api/recs/summary?days=30").get_json()["by_module"]) == {"labor"}
    _as(monkeypatch, rid, role="client")
    assert len(client.get("/api/recs/timeline").get_json()["items"]) == 6


def test_another_restaurants_record_never_leaks(client, db, monkeypatch):
    a, b = _rid(db, "Alpha Co"), _rid(db, "Bravo Co")
    rl.present(a, "trim_day:Monday", "labor", "home", db_path=db)
    rl.present(b, "cut_waste:Salmon", "food", "home", db_path=db)
    _as(monkeypatch, a)
    assert [i["key"] for i in client.get("/api/recs/timeline").get_json()["items"]] == ["trim_day:Monday"]
    assert set(client.get("/api/recs/summary?days=30").get_json()["by_module"]) == {"labor"}
    r = client.post("/api/recs/checkin", json={"key": "cut_waste:Salmon", "did_it": "yes"})
    assert r.status_code == 404
    assert not _q(db, "SELECT 1 FROM rec_events WHERE event='checkin'")


# ── GET /recs/timeline ───────────────────────────────────────────────────────

def test_the_timeline_newest_first_with_answers_reasons_and_trackers(client, db, monkeypatch):
    rid = _rid(db)
    _as(monkeypatch, rid)
    _ignored(db, rid, "post_this_week", "marketing")
    rl.present(rid, "trim_day:Monday", "labor", "home", db_path=db)
    rl.record(rid, "trim_day:Monday", "dismissed", meta={"kind": "not_for_us", "reason_code": "too_costly",
                                                          "reason": "short staffed"}, db_path=db)
    _measured(db, rid, "cut_waste:Salmon", "food", "improved")
    rl.implemented(rid, "cut_waste:Salmon", "food", db_path=db)
    rl.present(rid, "reprice:Soup", "food", "home", db_path=db)
    rl.record(rid, "reprice:Soup", "snoozed", snooze_until="2999-01-01 00:00:00", db_path=db)
    _x(db, "UPDATE rec_instances SET created_at=datetime('now', '-' || rowid || ' minutes')")
    items = client.get("/api/recs/timeline?limit=10").get_json()["items"]
    by = {i["key"]: i for i in items}
    assert [i["key"] for i in items] == ["post_this_week", "trim_day:Monday", "cut_waste:Salmon", "reprice:Soup"]
    assert set(items[0]) == {"key", "title", "module", "tags", "first_shown_at", "surfaces", "answer", "answered_at",
                             "reason_code", "reason", "implemented_at", "tracker_id"}
    assert by["post_this_week"]["answer"] == "expired"
    assert by["trim_day:Monday"]["answer"] == "dismissed" and by["trim_day:Monday"]["reason_code"] == "too_costly"
    assert by["trim_day:Monday"]["reason"] == "short staffed" and "focus:weekday_staffing" in by["trim_day:Monday"]["tags"]
    assert by["cut_waste:Salmon"]["answer"] == "implemented" and by["cut_waste:Salmon"]["tracker_id"]
    assert by["cut_waste:Salmon"]["implemented_at"] and by["cut_waste:Salmon"]["surfaces"] == ["home"]
    assert by["reprice:Soup"]["answer"] == "snoozed"


def test_the_timeline_pages_with_its_cursor_and_shows_superseded(client, db, monkeypatch):
    rid = _rid(db)
    _as(monkeypatch, rid)
    for i in range(7):
        rl.present(rid, f"slow_day:Day{i}", "marketing", "home", db_path=db)
    _x(db, "UPDATE rec_instances SET created_at=datetime('now', '-' || rowid || ' hours')")
    first = client.get("/api/recs/timeline?limit=3").get_json()
    assert len(first["items"]) == 3 and first["next_before"]
    second = client.get("/api/recs/timeline?limit=3&before=" + first["next_before"]).get_json()
    third = client.get("/api/recs/timeline?limit=3&before=" + second["next_before"]).get_json()
    keys = [i["key"] for p in (first, second, third) for i in p["items"]]
    assert len(keys) == 7 and len(set(keys)) == 7 and third["next_before"] is None
    assert client.get("/api/recs/timeline?before=yesterday").status_code == 400
    a = rl.present(rid, "trim_day:Monday", "labor", "home", dollar_value=100, db_path=db)
    _x(db, "UPDATE rec_instances SET created_at=datetime('now','-2 days') WHERE rec_id=?", (a,))
    rl.present(rid, "trim_day:Monday", "labor", "home", dollar_value=400, db_path=db)
    answers = [i["answer"] for i in client.get("/api/recs/timeline?limit=50").get_json()["items"]
               if i["key"] == "trim_day:Monday"]
    assert sorted(answers) == ["open", "superseded"]


# ── POST /recs/checkin ───────────────────────────────────────────────────────

def test_a_checkin_is_recorded_and_marks_the_trackers_attribution(client, db, monkeypatch):
    rid = _rid(db)
    _as(monkeypatch, rid)
    _measured(db, rid, "trim_day:Monday", "labor", "improved")
    tid = _q(db, "SELECT tracker_id FROM rec_instances")[0]["tracker_id"]
    r = client.post("/api/recs/checkin", json={"key": "trim_day:Monday", "did_it": "no", "conditions_changed": True,
                                               "note": "a holiday week"})
    body = r.get_json()
    assert r.status_code == 200 and body["checkin"]["attribution"] == {"implemented": "no", "confounded": True,
                                                                       "discount": True}
    assert body["checkin"]["tracker_id"] == tid
    assert rl.latest_checkin(rid, tracker_id=tid, db_path=db)["note"] == "a holiday week"
    # the discounted result reads unknown in the owner's success figures
    import rec_learning
    ep = rec_learning._load(models.get_conn(db), rid)[0]
    assert ep["verdict"] == "unknown"
    # "yes" also records the change as made
    r = client.post("/mobile/api/recs/checkin", json={"key": "trim_day:Monday", "did_it": "yes"}, headers=PHONE)
    assert r.status_code == 200
    assert _q(db, "SELECT implemented_at FROM rec_instances")[0]["implemented_at"]


@pytest.mark.parametrize("body", [{"key": "trim_day:Monday", "did_it": "maybe"},
                                  {"key": "trim_day:Monday", "did_it": "yes", "conditions_changed": "yes"},
                                  {"did_it": "yes"}, {"key": "trim_day:Monday", "did_it": "yes", "note": 4}])
def test_a_malformed_checkin_is_refused(client, db, monkeypatch, body):
    rid = _rid(db)
    _as(monkeypatch, rid)
    rl.present(rid, "trim_day:Monday", "labor", "home", db_path=db)
    assert client.post("/api/recs/checkin", json=body).status_code == 400


def test_a_manager_cannot_check_in_on_what_they_cannot_see(client, db, monkeypatch):
    rid = _rid(db)
    rl.present(rid, "cut_waste:Salmon", "food", "home", db_path=db)
    _as(monkeypatch, rid, role="manager")
    assert client.post("/api/recs/checkin", json={"key": "cut_waste:Salmon", "did_it": "yes"}).status_code == 404


# ── structured reasons on every "no" ────────────────────────────────────────

def test_recs_event_takes_a_reason_code_and_refuses_an_unknown_one(client, db, monkeypatch):
    rid = _rid(db)
    _as(monkeypatch, rid)
    rl.present(rid, "trim_day:Monday", "labor", "home", db_path=db)
    bad = client.post("/api/recs/event", json={"key": "trim_day:Monday", "event": "dismissed", "reason_code": "meh"})
    assert bad.status_code == 400
    ok = client.post("/mobile/api/recs/event", json={"key": "trim_day:Monday", "event": "dismissed",
                                                     "kind": "not_for_us", "reason_code": "already_doing",
                                                     "reason": "we cut Mondays in May"}, headers=PHONE)
    assert ok.status_code == 200
    meta = json.loads(_q(db, "SELECT meta FROM rec_events WHERE event='dismissed'")[0]["meta"])
    assert meta["reason_code"] == "already_doing" and meta["reason"] == "we cut Mondays in May"
    import decisions
    h = decisions.history(rid, db_path=db)
    assert h[0]["reason_code"] == "already_doing"
    ctx = decisions.context(rid, db_path=db)
    assert "because: already doing it; we cut Mondays in May" in ctx


def test_home_dismiss_takes_a_reason_code_on_web_and_phone(db, monkeypatch):
    import client_api
    import mobile_api
    rid = _rid(db)
    user = _as(monkeypatch, rid)
    # What Home showed this owner — an answer names something shown (K2).
    rl.present(rid, "trim_day:Monday", "labor", "home", db_path=db)
    rl.present(rid, "labor_over:2026-09-01", "labor", "home", db_path=db)
    app = Flask(__name__)
    with app.test_request_context(json={"key": "trim_day:Monday", "kind": "not_for_us", "reason_code": "nope"}):
        resp = client_api.home_dismiss_api.__wrapped__(current_user=user)
    assert resp[1] == 400
    with app.test_request_context(json={"key": "trim_day:Monday", "kind": "not_for_us", "reason_code": "doesnt_fit"}):
        resp = mobile_api.mobile_home_dismiss.__wrapped__(current_user=user)
    assert (resp.get_json() if hasattr(resp, "get_json") else resp[0].get_json())["ok"]
    with app.test_request_context(json={"key": "labor_over:2026-09-01", "kind": "snooze", "reason_code": "bad_timing"}):
        client_api.home_dismiss_api.__wrapped__(current_user=user)
    metas = {r["event"]: json.loads(r["meta"]) for r in _q(db, "SELECT event, meta FROM rec_events")}
    assert metas["dismissed"]["reason_code"] == "doesnt_fit" and metas["snoozed"]["reason_code"] == "bad_timing"
