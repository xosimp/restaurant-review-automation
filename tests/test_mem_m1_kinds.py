"""Memory audit 9/29/26, workstream M1 — kinds, plans and support's hands.

  quiet_kinds  a quiet kind (Home) and a suppressed schedule kind are held
               as durable state with a review date; they are re-tested once,
               labelled, instead of staying quiet for good or coming back by
               accident when a pruned log forgets why.
  one_hide     Ask's do-not-propose list counts only "not for us", needs 3 in
               180 days (recency-weighted), and names subjects, not kinds.
  weekly_plan  the Monday plan reads last week's plan with its state and
               verdict, and never files declined, answered or still-open advice.
  view_as      support's approvals, skips, schedule saves and capability
               changes through view-as never train the owner's preferences.
"""
from datetime import datetime, timedelta

import pytest

import models
import rec_ledger as rl
from models import Restaurant, create_restaurant


@pytest.fixture(autouse=True)
def _db(db_path, monkeypatch):
    real = models.get_conn
    monkeypatch.setattr(models, "get_conn", lambda *a, **k: real(db_path))
    monkeypatch.setattr(models, "DB_PATH", db_path)
    yield


def _rid(db_path, name="Kinds Co"):
    return create_restaurant(Restaurant(name=name, owner_email=f"{name.split()[0].lower()}@x.test"), db_path=db_path)


def _sql(db_path, sql, *args):
    c = models.get_conn(db_path)
    c.execute(sql, args)
    c.commit()
    c.close()


def _one(db_path, sql, *args):
    c = models.get_conn(db_path)
    try:
        return c.execute(sql, args).fetchone()
    finally:
        c.close()


# ── quiet kinds (Home) ───────────────────────────────────────────────────────

def _expire_four(db_path, rid, kind="post_this_week"):
    for i in range(4):
        rl.present(rid, f"{kind}:{i}", "marketing", "home", dollar_value=100, db_path=db_path)
    _sql(db_path, "UPDATE rec_instances SET created_at=datetime('now','-20 days') WHERE restaurant_id=?", rid)
    rl.expire_stale(db_path=db_path)


def test_a_quiet_kind_is_held_with_a_review_date_and_re_tested_once(db_path):
    import decisions
    rid = _rid(db_path)
    _expire_four(db_path, rid, "first_post")
    st = decisions.quiet_state(rid, db_path=db_path)["first_post"]
    assert st["retest"] is False and st["review_on"]
    assert decisions.quiet_kinds(rid, db_path=db_path) == {"first_post"}
    # The review date passes: shown again, labelled a re-test.
    _sql(db_path, "UPDATE rec_kind_states SET review_on=datetime('now','-1 day') WHERE restaurant_id=?", rid)
    assert decisions.quiet_state(rid, db_path=db_path)["first_post"]["retest"] is True
    assert decisions.quiet_kinds(rid, db_path=db_path) == set()
    # The re-test is ignored too: quiet again, to the next review date.
    rl.present(rid, "first_post:retest", "marketing", "home", db_path=db_path)
    _sql(db_path, "UPDATE rec_instances SET created_at=datetime('now','-20 days','+1 minute') "
                  "WHERE key='first_post:retest'")
    _sql(db_path, "UPDATE rec_kind_states SET review_on=datetime('now','-21 days') WHERE restaurant_id=?", rid)
    rl.expire_stale(db_path=db_path)
    st = decisions.quiet_state(rid, db_path=db_path)["first_post"]
    assert st["retest"] is False and st["retests"] == 1
    assert st["review_on"] > datetime.utcnow().strftime("%Y-%m-%d")


def test_answering_a_re_test_ends_the_quiet(db_path):
    import decisions
    rid = _rid(db_path)
    _expire_four(db_path, rid, "first_post")
    decisions.quiet_state(rid, db_path=db_path)
    rl.present(rid, "first_post:again", "marketing", "home", db_path=db_path)
    rl.record(rid, "first_post:again", "completed", db_path=db_path)
    assert "first_post" not in decisions.quiet_state(rid, db_path=db_path)
    assert _one(db_path, "SELECT COUNT(*) FROM rec_kind_states WHERE family='home'")[0] == 0


def test_a_restore_is_remembered_beyond_the_vote_window(db_path):
    import decisions
    rid = _rid(db_path)
    _expire_four(db_path, rid, "first_post")
    assert decisions.quiet_kinds(rid, db_path=db_path) == {"first_post"}
    decisions.restore_kind(rid, "first_post", db_path=db_path)
    # Even with the ledger's own restore marker gone, the durable one holds.
    _sql(db_path, "DELETE FROM rec_instances WHERE key LIKE 'restore_kind:%'")
    assert decisions.quiet_kinds(rid, db_path=db_path) == set()


def test_home_ranks_a_re_test_back_in_and_a_doubled_figure_too():
    import home_brief
    recs = [{"key": f"first_post:{i}", "title": "Post", "timeframe": "This week", "dollars_monthly": 100,
             "effort": "low"} for i in range(1)] + \
           [{"key": f"trim_day:{d}", "title": d, "timeframe": "Next schedule", "dollars_monthly": 50, "effort": "low"}
            for d in ("Monday", "Tuesday", "Wednesday")]
    quiet = {"first_post": {"retest": True, "last_dollars": 100}}
    out = home_brief.order_recommendations([dict(r) for r in recs], quiet_kinds=quiet)
    assert out[0]["key"] == "first_post:0" and out[0].get("retest") and not out[0].get("quiet")
    quiet = {"first_post": {"retest": False, "last_dollars": 40}}               # 100 ≥ 2 × 40
    out = home_brief.order_recommendations([dict(r) for r in recs], quiet_kinds=quiet)
    assert out[0].get("retest")
    quiet = {"first_post": {"retest": False, "last_dollars": 90}}
    out = home_brief.order_recommendations([dict(r) for r in recs], quiet_kinds=quiet)
    assert [r["key"] for r in out].index("first_post:0") == 3 and out[3].get("quiet")


# ── schedule suppression ─────────────────────────────────────────────────────

def test_a_suppressed_schedule_kind_survives_the_logs_pruning_and_is_re_tested(db_path):
    import schedule_intel as si
    rid = _rid(db_path)
    si.record_recommendation(rid, "hours", "Trim 4h Tuesday", "dismissed", db_path=db_path)
    si.record_recommendation(rid, "hours", "Trim 4h Friday", "dismissed", db_path=db_path)
    st = si.suppression_state(rid, db_path=db_path)["hours"]
    assert st["state"] == "suppressed" and st["reason"] == "declined 2 times" and "-" not in st["review_on"]
    # The log is pruned at 365 days: the suppression no longer lifts with it.
    _sql(db_path, "DELETE FROM schedule_recommendation_events WHERE restaurant_id=?", rid)
    assert si.suppressed_kinds(rid, db_path=db_path) == {"hours"}
    # From its review date it is re-tested...
    _sql(db_path, "UPDATE rec_kind_states SET review_on=datetime('now','-1 day') WHERE family='schedule'")
    assert si.suppression_state(rid, db_path=db_path)["hours"]["state"] == "retest"
    assert si.suppressed_kinds(rid, db_path=db_path) == set()
    # ...and declined again, suppressed to the next review date.
    si.record_recommendation(rid, "hours", "Trim 4h Monday", "dismissed", db_path=db_path)
    st = si.suppression_state(rid, db_path=db_path)["hours"]
    assert st["state"] == "suppressed" and st["retests"] == 1


def test_a_managers_declines_never_suppress_a_kind_for_the_owner(db_path):
    import schedule_intel as si
    rid = _rid(db_path)
    for i in range(3):
        si.record_recommendation(rid, "hours", f"Trim {i}", "dismissed", authority="delegate", db_path=db_path)
    for i in range(12):
        si.record_recommendation(rid, "leaders", f"Lead {i}", "shown", authority="delegate", db_path=db_path)
    assert si.suppressed_kinds(rid, db_path=db_path) == set()
    si.record_recommendation(rid, "hours", "Trim 9", "dismissed", authority="principal", db_path=db_path)
    si.record_recommendation(rid, "hours", "Trim 10", "dismissed", db_path=db_path)
    assert si.suppressed_kinds(rid, db_path=db_path) == {"hours"}


# ── one hide ─────────────────────────────────────────────────────────────────

def test_one_hide_never_puts_a_kind_on_asks_do_not_propose_list(db_path):
    import decisions
    from intelligence import memory
    rid = _rid(db_path)
    rl.present(rid, "reprice:Soup", "food", "home", db_path=db_path)
    rl.record(rid, "reprice:Soup", "dismissed", meta={"kind": "hide"}, db_path=db_path)
    rl.present(rid, "reprice:Salad", "food", "home", db_path=db_path)
    rl.record(rid, "reprice:Salad", "dismissed", meta={"kind": "not_for_us"}, db_path=db_path)
    assert decisions.declined_subjects(rid, db_path=db_path) == []
    assert memory.own_record(rid, db_path=db_path)["ignored"] == []


def test_three_not_for_us_in_six_months_name_the_subjects(db_path):
    import decisions
    from intelligence import memory
    rid = _rid(db_path)
    for day in ("Tuesday", "Saturday", "Sunday"):
        rl.present(rid, f"trim_day:{day}", "labor", "home", title=f"Trim {day} staffing", db_path=db_path)
        rl.record(rid, f"trim_day:{day}", "dismissed", meta={"kind": "not_for_us"}, db_path=db_path)
    d = decisions.declined_subjects(rid, db_path=db_path)
    assert d and d[0]["kind"] == "trim_day" and d[0]["n"] == 3
    assert {"tuesday", "saturday", "sunday"} <= {x.lower() for x in d[0]["subjects"]}
    lines = memory.lines({"record": {"declined_detail": d}})
    assert any("passed on" in l and "tuesday" in l.lower() for l in lines)
    # A timing answer or "already doing it" is not a no; neither is an old one.
    rid2 = _rid(db_path, "Old Co")
    for day in ("Monday", "Tuesday", "Friday"):
        rl.present(rid2, f"trim_day:{day}", "labor", "home", db_path=db_path)
        rl.record(rid2, f"trim_day:{day}", "dismissed", meta={"kind": "not_for_us"}, db_path=db_path)
    _sql(db_path, "UPDATE rec_events SET at=datetime('now','-170 days') WHERE restaurant_id=?", rid2)
    assert decisions.declined_subjects(rid2, db_path=db_path) == []          # recency-weighted below the floor


# ── weekly plan ──────────────────────────────────────────────────────────────

def test_the_plan_reads_last_weeks_items_with_their_state(db_path):
    import issues
    import strategy_jobs as sj
    rid = _rid(db_path)
    issues.create_issue(rid, "plan", "Retrain expo on Friday", source_key="plan:2026-W39:0", notify=False,
                        db_path=db_path)
    done, _t = issues.create_issue(rid, "plan", "Call the linen supplier", source_key="plan:2026-W39:1",
                                   notify=False, db_path=db_path)
    _sql(db_path, "UPDATE ops_issues SET status='resolved', resolved_at=datetime('now'), resolution_note='done' "
                  "WHERE id=?", done["id"])
    hist = sj.plan_history(rid, "2026-W40", db_path=db_path)
    assert {h["title"] for h in hist} == {"Retrain expo on Friday", "Call the linen supplier"}
    block = sj.plan_memory_block(hist)
    assert "LAST WEEK'S PLAN" in block and "still open" in block and "never file the same item again" in block
    assert "<<<UNTRUSTED" in block                                  # the titles are fenced


def test_the_plan_never_files_declined_answered_or_still_open_advice(db_path):
    import issues
    import strategy_jobs as sj
    rid = _rid(db_path)
    rl.present(rid, "trim_day:Tuesday", "labor", "home", title="Trim Tuesday staffing", db_path=db_path)
    rl.record(rid, "trim_day:Tuesday", "dismissed", meta={"kind": "not_for_us"}, db_path=db_path)
    item = {"title": "Trim Tuesday dinner by one server", "why": "Tuesday labor ran 34%"}
    assert sj.plan_item_repeat(rid, item, history=[], db_path=db_path) == "the owner passed on this advice"
    # An item named in the plan's words is read from the restaurant's own
    # ingredients (insight_store.known_subjects).
    _sql(db_path, "INSERT INTO ingredients (restaurant_id, name, unit) VALUES (?, 'Salmon', 'lb')", rid)
    import insight_store
    insight_store._SUBJECTS_CACHE.clear()
    rl.present(rid, "cut_waste:Salmon", "food", "home", title="Cut the Salmon order", db_path=db_path)
    rl.record(rid, "cut_waste:Salmon", "completed", db_path=db_path)
    assert "already answered" in sj.plan_item_repeat(rid, {"title": "Cut salmon waste this week",
                                                           "why": "waste ran 12%"}, history=[], db_path=db_path)
    issues.create_issue(rid, "plan", "Retrain expo on Friday", source_key="plan:2026-W39:0", notify=False,
                        db_path=db_path)
    hist = sj.plan_history(rid, "2026-W40", db_path=db_path)
    assert "repeats plan item" in sj.plan_item_repeat(rid, {"title": "Retrain expo on Friday!", "why": "x"},
                                                      history=hist, db_path=db_path)
    assert sj.plan_item_repeat(rid, {"title": "Post the brunch special", "why": "reach down 20%"},
                               history=hist, db_path=db_path) is None


# ── view as ──────────────────────────────────────────────────────────────────

def test_supports_approvals_never_train_the_owners_voice_or_trust(db_path, monkeypatch):
    import permissions
    import client_api
    rid = _rid(db_path)
    c = models.get_conn(db_path)
    for i in range(3):
        c.execute("INSERT INTO reviews (restaurant_id, platform, external_id, author, rating, text, response_status, "
                  "draft_response, fetched_at) VALUES (?,?,?,?,?,?,?,?, datetime('now'))",
                  (rid, "google", f"g{i}", "Guest", 5, "Great", "drafted", f"Thanks, guest {i}!"))
    c.commit()
    ids = [r[0] for r in c.execute("SELECT id FROM reviews WHERE restaurant_id=? ORDER BY id", (rid,)).fetchall()]
    c.close()
    monkeypatch.setattr(permissions, "acting_via", lambda user=None: {"admin_id": 9, "admin": "will", "role": "admin"})
    monkeypatch.setattr(client_api, "_post_reply_async", lambda *a, **k: None, raising=False)
    from flask import Flask
    app = Flask("t")
    with app.test_request_context("/"):
        client_api._do_approve(ids[0], rid, auto=False)
        client_api._do_skip(ids[1], rid)
    row = _one(db_path, "SELECT response_action FROM reviews WHERE id=?", ids[0])
    assert row["response_action"] == "support_approved"
    assert _one(db_path, "SELECT response_action FROM reviews WHERE id=?", ids[1])["response_action"] == "support_skipped"
    assert models.get_approved_examples(rid, db_path=db_path) == []
    t = models.auto_approve_trust(rid, db_path=db_path)[5]
    assert t["approved"] == 0 and t["skipped"] == 0


def test_supports_schedule_saves_and_capability_changes_are_marked(db_path, monkeypatch):
    import permissions
    import schedule_versions as sv
    monkeypatch.setattr(permissions, "acting_via", lambda user=None: {"admin_id": 9, "admin": "will", "role": "admin"})
    rid = _rid(db_path)
    models.record_capability_change(rid, "rating", "Ana", before=3, after=4, changed_by="owner", db_path=db_path)
    assert _one(db_path, "SELECT changed_by FROM capability_changes")[0].startswith("support:will")
    assert sv.SUPPORT_PREFIX == "support:" and "saved_by LIKE 'support:%'" in sv.NOT_SUPPORT_TOUCHED_SQL


def test_a_quiet_kind_does_not_lift_by_falling_out_of_the_vote_window(db_path):
    import decisions
    rid = _rid(db_path)
    _expire_four(db_path, rid, "first_post")
    decisions.quiet_state(rid, db_path=db_path)
    # Its episodes gone from the vote's window (here: gone altogether).
    _sql(db_path, "DELETE FROM rec_instances WHERE restaurant_id=?", rid)
    assert "first_post" in decisions.quiet_kinds(rid, db_path=db_path)
    # Only an answer lifts it.
    rl.present(rid, "first_post:new", "marketing", "home", db_path=db_path)
    rl.record(rid, "first_post:new", "completed", db_path=db_path)
    assert "first_post" not in decisions.quiet_kinds(rid, db_path=db_path)
