"""Recommendation ROI audit — the ledger half (rec_ledger).

Each test names the audit item it pins: #17 subject tags, #27 implemented,
#36 permanent tracker links and abandoned trackers, #37 superseded, #44
missed detections, and the structured reasons. The db fixture redirects
every bound get_conn (CLAUDE.md) to the test's own database.
"""
import json
import sys
from datetime import datetime, timedelta

import pytest

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
    return db_path


def _rid(db, name="Ledger Co"):
    return create_restaurant(Restaurant(name=name, owner_email=f"{name.split()[0].lower()}@x.test"), db_path=db)


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


def _age(db, rec_id, hours):
    _x(db, "UPDATE rec_instances SET created_at=datetime('now', ?) WHERE rec_id=?", (f"-{hours} hours", rec_id))


# ── #17 subject tags ─────────────────────────────────────────────────────────

@pytest.mark.parametrize("key, expect", [
    ("trim_day:Saturday", {"day:saturday", "daytype:weekend", "topic:staffing", "focus:weekend_staffing"}),
    ("trim_day:Tuesday", {"day:tuesday", "daytype:weekday", "focus:weekday_staffing"}),
    ("standby:2026-09-26:ana", {"day:saturday", "daytype:weekend"}),           # an ISO date's weekday
    ("reprice:Carbonara", {"dish:carbonara", "topic:pricing"}),
    ("cut_waste:Salmon Fillet", {"item:salmon fillet", "topic:waste"}),
    ("top_issue:wait_time", {"category:wait_time", "topic:guest_experience"}),
    ("schedule_coverage:Fill the gap on Friday night: add a server",
     {"day:friday", "daypart:night", "focus:night_staffing", "focus:weekend_staffing"}),
    ("link:reviews_x_labor:service:friday", {"category:service", "day:friday", "topic:staffing"}),
    ("dsr_action:reorder:food/salmon-fillet", {"item:salmon fillet", "topic:ordering"}),
    ("quiet_night:2026-09-27", {"day:sunday", "topic:guest_outreach"}),
])
def test_tags_read_the_subject_of_every_key_shape(key, expect):
    assert expect <= set(rl.tags_for(key))


def test_a_period_date_is_not_a_weekday_and_a_hash_carries_only_its_topic():
    assert rl.tags_for("labor_over:2026-09-26") == ["topic:hours"]            # a pay period's start
    assert rl.tags_for("insight_food:abc1234567") == ["topic:food_cost"]
    # "late night" is consumed before "night" is looked for
    t = rl.tags_for("schedule_hours:Cut the late night bartender on Saturday")
    assert "daypart:late_night" in t and "daypart:night" not in t


def test_tag_labels_read_as_words():
    assert rl.tag_label("focus:weekend_staffing") == "Weekend staffing"
    assert rl.tag_label("focus:late_night_staffing") == "Late night staffing"
    assert rl.tag_label("category:wait_time") == "Wait time"
    assert rl.tag_label("daytype:weekday") == "Weekdays"
    assert rl.tag_label("topic:guest_experience") == "Guest experience"


def test_tags_are_stored_when_shown_with_the_ingredients_own_category(db):
    rid = _rid(db)
    _x(db, "INSERT INTO ingredients (restaurant_id, name, category) VALUES (?, 'Salmon Fillet', 'Protein')", (rid,))
    rl.present(rid, "cut_waste:Salmon Fillet", "food", "home", db_path=db)
    rl.present(rid, "trim_day:Friday", "labor", "home", db_path=db)
    rows = {r["key"]: json.loads(r["tags"]) for r in _q(db, "SELECT key, tags FROM rec_instances")}
    assert "food_category:protein" in rows["cut_waste:Salmon Fillet"]
    assert "focus:weekend_staffing" in rows["trim_day:Friday"]


def test_older_episodes_are_backfilled_bounded(db):
    rid = _rid(db)
    for d in ("Monday", "Saturday", "Sunday"):
        rl.present(rid, f"trim_day:{d}", "labor", "home", db_path=db)
    _x(db, "UPDATE rec_instances SET tags=NULL")
    assert rl.backfill_tags(db_path=db, limit=2) == 2
    assert rl.backfill_tags(db_path=db, limit=2) == 1                 # the rest next pass
    assert rl.backfill_tags(db_path=db) == 0
    assert all(r["tags"] for r in _q(db, "SELECT tags FROM rec_instances"))


# ── #27 implemented ──────────────────────────────────────────────────────────

def test_implemented_is_its_own_state_distinct_from_accepted(db):
    rid = _rid(db)
    rl.present(rid, "reprice:Carbonara", "food", "home", db_path=db)
    rl.record(rid, "reprice:Carbonara", "accepted", surface="home", db_path=db)
    assert _q(db, "SELECT status FROM rec_instances")[0]["status"] == "accepted"
    assert rl.implemented(rid, "reprice:Carbonara", "food", source_ref="px", db_path=db) == 1
    row = _q(db, "SELECT status, implemented_at FROM rec_instances")[0]
    assert row["status"] == "implemented" and row["implemented_at"]
    assert rl.implemented(rid, "reprice:Carbonara", "food", source_ref="px", db_path=db) == 0     # once
    assert rl.silenced(rid, "reprice:Carbonara", db_path=db)


def test_done_stays_done_and_a_change_nobody_recommended_is_not_recorded(db):
    rid = _rid(db)
    rl.present(rid, "post_this_week", "marketing", "home", db_path=db)
    rl.record(rid, "post_this_week", "completed", surface="home", db_path=db)
    rl.implemented(rid, ["post_this_week", "first_post"], "marketing", db_path=db)
    rows = _q(db, "SELECT key, status, implemented_at FROM rec_instances")
    assert [(r["key"], r["status"]) for r in rows] == [("post_this_week", "completed")] and rows[0]["implemented_at"]
    # first_post was never shown: no episode is invented for it
    assert not _q(db, "SELECT 1 FROM rec_instances WHERE key='first_post'")


def test_a_price_applied_from_a_suggestion_is_implemented(db, monkeypatch):
    import menu_intelligence as mi
    rid = _rid(db)
    rl.present(rid, "reprice:Carbonara", "food", "food", db_path=db)
    s = {"dish": "Carbonara", "suggested_price": 19.0}
    assert mi.record_price_change(rid, 1, 17.0, 19.0, suggestion=s, db_path=db) == s
    row = _q(db, "SELECT status, implemented_at FROM rec_instances WHERE key='reprice:Carbonara'")[0]
    assert row["implemented_at"]
    import rec_learning
    ep = rec_learning._load(models.get_conn(db), rid)[0]
    assert ep["state"] == "implemented"                              # answered and applied in one step


def test_a_published_post_implements_the_posting_card(db):
    import marketing
    rid = _rid(db)
    rl.present(rid, "first_post", "marketing", "home", db_path=db)
    marketing.post_went_live(rid, "ig_123", db_path=db)
    assert _q(db, "SELECT status FROM rec_instances WHERE key='first_post'")[0]["status"] == "implemented"


def test_a_supplier_order_implements_each_running_low_line_on_it(db, monkeypatch):
    import client_api
    import emails
    rid = _rid(db)
    rl.present(rid, "stock_low:Salmon", "food", "home", db_path=db)
    monkeypatch.setattr(emails, "send_supplier_order_email", lambda **k: None)
    r = models.get_restaurant(rid)
    sent, failed = client_api._send_supplier_orders(
        rid, r, [{"supplier_email": "s@x.test", "supplier_name": "Sea Co", "total_cost": 40.0,
                  "items": [{"item": "Salmon", "qty": 4}, {"item": "Lemons", "qty": 2}]}], {"id": 1})
    assert sent and not failed
    assert _q(db, "SELECT status FROM rec_instances WHERE key='stock_low:Salmon'")[0]["status"] == "implemented"
    assert not _q(db, "SELECT 1 FROM rec_instances WHERE key='stock_low:Lemons'")


def test_a_schedule_edit_that_carries_a_recommendation_out_implements_it_in_the_same_save(db, monkeypatch):
    import schedule_versions as sv
    import schedule_learning
    from schedule_intel import schedule_rec_key
    from shift_quality import recommendation_kind
    rid = _rid(db)
    rec = "Fill the gap on Friday night: add a server"
    key = schedule_rec_key(recommendation_kind(rec), rec)
    rl.present(rid, key, "schedule", "schedule_review", db_path=db)
    monkeypatch.setattr(schedule_learning, "addressed_recommendations", lambda recs, b, a: list(recs))
    c = models.get_conn(db)
    try:
        c.execute("BEGIN IMMEDIATE")
        assert sv._record_implied_acceptance(c, rid, [rec], [], [], "Sam") == [rec]
        c.commit()
    finally:
        c.close()
    assert _q(db, "SELECT status FROM rec_instances WHERE key=?", (key,))[0]["status"] == "implemented"
    assert _q(db, "SELECT action FROM schedule_recommendation_events")[0]["action"] == "accepted"


def test_clients_cannot_post_server_only_events(db):
    import strategy_routes
    from flask import Flask
    rid = _rid(db)
    for ev in ("implemented", "superseded", "checkin", "abandoned", "outcome", "expired", "shown"):
        with Flask(__name__).test_request_context(json={"key": "trim_day:Monday", "event": ev}):
            _out, st = strategy_routes._do_rec_event({"id": 1, "restaurant_id": rid, "role": "client"})
        assert st == 400, ev


# ── #36 trackers linked for good; abandoned carried ──────────────────────────

def _tracker(db, rid, key, status="tracking", verdict=None, created_days_ago=0, dollars=None):
    c = models.get_conn(db)
    try:
        cur = c.execute(
            "INSERT INTO recommendation_outcomes (restaurant_id, source, source_key, title, metric, started_on, "
            "evaluate_on, status, verdict, dollars_monthly, created_at) VALUES (?, 'recommendation', ?, 't', "
            "'labor_pct', date('now', ?), date('now', ?), ?, ?, ?, datetime('now', ?))",
            (rid, key, f"-{created_days_ago} days", f"-{max(0, created_days_ago - 28)} days", status, verdict,
             dollars, f"-{created_days_ago} days"))
        c.commit()
        return cur.lastrowid
    finally:
        c.close()


def test_the_sync_links_and_carries_trackers_older_than_its_old_window(db):
    rid = _rid(db)
    rec = rl.present(rid, "trim_day:Monday", "labor", "home", db_path=db)
    _x(db, "UPDATE rec_instances SET created_at=datetime('now','-200 days') WHERE rec_id=?", (rec,))
    oid = _tracker(db, rid, "trim_day:Monday", status="evaluated", verdict="unknown", created_days_ago=199)
    out = rl.sync_existing(db_path=db)
    assert out.get("linked") == 1 and out.get("outcomes") == 1
    assert _q(db, "SELECT tracker_id FROM rec_instances")[0]["tracker_id"] == oid
    ev = _q(db, "SELECT meta FROM rec_events WHERE event='outcome'")[0]
    assert json.loads(ev["meta"]) == {"verdict": "unknown", "tracker_id": oid}   # "couldn't measure" is kept as such
    assert rl.sync_existing(db_path=db).get("outcomes") is None                   # once


def test_an_abandoned_tracker_reaches_the_trail(db):
    rid = _rid(db)
    rl.present(rid, "cut_waste:Salmon", "food", "home", db_path=db)
    _x(db, "UPDATE rec_instances SET created_at=datetime('now','-2 days')")
    oid = _tracker(db, rid, "cut_waste:Salmon", status="abandoned", created_days_ago=1)
    assert rl.sync_existing(db_path=db).get("abandoned") == 1
    assert _q(db, "SELECT meta FROM rec_events WHERE event='abandoned'")[0]["meta"] == json.dumps({"tracker_id": oid})


def test_track_links_its_tracker_at_once(db, monkeypatch):
    import metrics
    import strategy_routes
    from flask import Flask
    rid = _rid(db)
    rl.present(rid, "insight_review:abcdef1234", "reviews", "reviews", title="Answer 3-star reviews", db_path=db)
    monkeypatch.setattr(metrics, "measure", lambda r, key, s, e, db_path=None: (4.3, "30 reviews"))
    with Flask(__name__).test_request_context(json={"key": "insight_review:abcdef1234", "event": "accepted",
                                                    "surface": "reviews", "module": "reviews"}):
        out, st = strategy_routes._do_rec_event({"id": 1, "restaurant_id": rid, "role": "client"})
    assert st == 200 and out["tracking"]
    tid = _q(db, "SELECT id FROM recommendation_outcomes")[0]["id"]
    assert _q(db, "SELECT tracker_id FROM rec_instances")[0]["tracker_id"] == tid
    assert rl.link_tracker(rid, "insight_review:abcdef1234", 999, db_path=db) is False   # already linked


# ── #37 superseded, not expired ─────────────────────────────────────────────

def test_a_new_dollar_figure_supersedes_the_open_episode(db):
    rid = _rid(db)
    a = rl.present(rid, "trim_day:Monday", "labor", "home", dollar_value=300, db_path=db)
    assert rl.present(rid, "trim_day:Monday", "labor", "home", dollar_value=900, db_path=db) == a   # same day: held
    _age(db, a, 30)
    b = rl.present(rid, "trim_day:Monday", "labor", "brief_email", dollar_value=900, db_path=db)
    assert b and b != a
    old = _q(db, "SELECT status, superseded_by FROM rec_instances WHERE rec_id=?", (a,))[0]
    assert old["status"] == "superseded" and old["superseded_by"] == b
    ev = json.loads(_q(db, "SELECT meta FROM rec_events WHERE rec_id=? AND event='superseded'", (a,))[0]["meta"])
    assert ev["why"] == "figure_changed" and ev["from"] == 300 and ev["to"] == 900
    # a small wobble is the same recommendation
    _age(db, b, 30)
    assert rl.present(rid, "trim_day:Monday", "labor", "home", dollar_value=940, db_path=db) == b


def test_a_new_target_supersedes_and_a_one_live_kind_replaces_its_other_keys(db):
    rid = _rid(db)
    a = rl.present(rid, "reprice:Carbonara", "food", "home", target=18.5, db_path=db)
    _age(db, a, 30)
    b = rl.present(rid, "reprice:Carbonara", "food", "home", target=19.25, db_path=db)
    assert b != a and _q(db, "SELECT status FROM rec_instances WHERE rec_id=?", (a,))[0]["status"] == "superseded"
    t30 = rl.present(rid, "schedule_to_target:30%", "labor", "brief_email", db_path=db)
    t28 = rl.present(rid, "schedule_to_target:28%", "labor", "brief_email", db_path=db)
    st = {r["rec_id"]: (r["status"], r["superseded_by"]) for r in _q(db, "SELECT * FROM rec_instances")}
    assert st[t30] == ("superseded", t28) and st[t28][0] == "open"


def test_a_new_model_read_supersedes_the_old_reads_open_lines(db):
    import insight_store
    rid = _rid(db)
    old = [{"key": insight_store.line_key("insight_food", t), "text": t} for t in ("Cut salmon", "Recount bread")]
    insight_store.present_recs(rid, "food", "food", old, db_path=db)
    rl.record(rid, old[1]["key"], "dismissed", meta={"kind": "not_for_us"}, db_path=db)
    new = [{"key": insight_store.line_key("insight_food", t), "text": t} for t in ("Cut salmon", "Reprice soup")]
    insight_store.present_recs(rid, "food", "food", new, db_path=db)
    st = {r["key"]: r["status"] for r in _q(db, "SELECT key, status FROM rec_instances")}
    assert st[old[0]["key"]] == "open"                 # still in the read
    assert st[old[1]["key"]] == "dismissed"            # answered stays answered
    assert st[new[1]["key"]] == "open"
    # a reprice batch (keys with a subject) replaces nothing
    insight_store.present_recs(rid, "food", "food", [{"key": "reprice:Soup", "text": "Reprice soup"}], db_path=db)
    assert _q(db, "SELECT status FROM rec_instances WHERE key=?", (new[1]["key"],))[0]["status"] == "open"


def test_superseded_is_neither_answered_nor_ignored_but_expired_is_ignored(db):
    import admin_ops
    rid = _rid(db)
    a = rl.present(rid, "trim_day:Monday", "labor", "home", dollar_value=300, db_path=db)
    _age(db, a, 30)
    rl.present(rid, "trim_day:Monday", "labor", "home", dollar_value=900, db_path=db)
    e = rl.present(rid, "post_this_week", "marketing", "home", db_path=db)
    _x(db, "UPDATE rec_instances SET created_at=datetime('now','-20 days') WHERE rec_id=?", (e,))
    assert rl.expire_stale(db_path=db) == 1
    out = admin_ops.recommendation_acceptance(days=90)
    assert out["total"]["n"] == 2 and out["total"]["ignored"] == 1             # the superseded one is not counted
    assert _q(db, "SELECT status FROM rec_instances WHERE rec_id=?", (e,))[0]["status"] == "expired"


# ── #44 missed detections ────────────────────────────────────────────────────

def test_a_problem_with_no_recommendation_before_it_is_logged_once_a_day(db):
    rid = _rid(db)
    assert rl.note_problem(rid, "alert", "labor_over:2026-09-14", module="labor", db_path=db)
    assert not rl.note_problem(rid, "alert", "labor_over:2026-09-14", module="labor", db_path=db)
    row = _q(db, "SELECT * FROM rec_missed_detections")[0]
    assert row["source"] == "alert" and row["lookback_days"] == rl.MISSED_LOOKBACK_DAYS


def test_a_covering_recommendation_shown_first_means_no_miss_but_the_alert_itself_does_not_count(db):
    rid = _rid(db)
    rl.present(rid, "labor_over:2026-09-14", "labor", "alert_push", db_path=db)      # the alert's own showing
    assert rl.note_problem(rid, "alert", "labor_over:2026-09-14", module="labor", db_path=db)
    other = _rid(db, "Other Co")
    rl.present(other, "trim_day:Friday", "labor", "home", db_path=db)                # a covering kind, on Home
    assert not rl.note_problem(other, "alert", "labor_over:2026-09-14", module="labor", db_path=db)
    # an unmapped kind is covered by the same module
    rl.present(other, "insight_intel:abcdef1234", "intel", "intel", db_path=db)
    assert not rl.note_problem(other, "alert", "ai_visibility_drop", module="intel", db_path=db)


def test_an_issue_and_a_close_out_86_are_checked_for_a_prior_recommendation(db):
    import issues
    rid = _rid(db)
    issues.create_issue(rid, "stock", "Low on salmon", source_key="stock:2026-09-22", notify=False, db_path=db)
    issues.create_issue(rid, "plan", "Plan item", source_key="plan:2026-W39:1", notify=False, db_path=db)
    keys = {r["subject_key"] for r in _q(db, "SELECT subject_key FROM rec_missed_detections")}
    assert keys == {"stock:2026-09-22"}                  # a plan item is itself a recommendation


def test_a_problem_alert_notes_a_miss_and_a_review_alert_does_not(db):
    import notify
    rid = _rid(db)
    notify._note_missed(rid, "labor_over", [{"key": "labor_over:2026-09-14", "module": "labor", "kind": "labor_over"}],
                        ["push"], db_path=db)
    notify._note_missed(rid, "1star", [{"key": "review:5", "module": "reviews", "kind": "1star"}], ["push"],
                        db_path=db)
    notify._note_missed(rid, "labor_over", [{"key": "labor_over:x", "module": "labor"}], [], db_path=db)  # not sent
    assert [r["subject_key"] for r in _q(db, "SELECT subject_key FROM rec_missed_detections")] == ["labor_over:2026-09-14"]


# ── structured reasons ──────────────────────────────────────────────────────

def test_an_unknown_reason_code_is_never_stored(db):
    rid = _rid(db)
    rl.present(rid, "trim_day:Monday", "labor", "home", db_path=db)
    rl.record(rid, "trim_day:Monday", "dismissed", meta={"kind": "not_for_us", "reason_code": "vibes"}, db_path=db)
    assert "reason_code" not in json.loads(_q(db, "SELECT meta FROM rec_events WHERE event='dismissed'")[0]["meta"])
    assert set(rl.REASON_CODES) == {"already_doing", "doesnt_fit", "too_costly", "bad_timing", "dont_trust_data",
                                    "other"}
    assert all(rl.reason_label(c) for c in rl.REASON_CODES)
