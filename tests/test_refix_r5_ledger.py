"""Memory re-audit fix round 9/29/26 (workstream R5) — the recommendation
ledger and what learns from it.

  use_again        QUALITY-1: "Use again" / a restored kind is a `reopened`
                   event every reader honours (declined_subjects,
                   decisions.history, rec_learning's state, the memory block).
  distrust         LOOPS-11: "don't trust the data" is in no denominator.
  delivered_seen   LOOPS-10: an episode only an unopened email carried is
                   `unseen`, not ignored, and casts no quiet-kind vote.
  keep_suggesting  LOOPS-6: "Keep suggesting it" silences its question exactly
                   as long as it keeps the kind.
  result_episode   LOOPS-7: a result or tracker recorded by key lands on the
                   episode that was taken; CROSSMODULE-13: only the owner's
                   Home logs the one-thing rank build.
  limits_first     INVENTORY-13: decisions.history picks the newest answer per
                   key in SQL, so an old reasoned decline is never cut.
"""
import json
from datetime import datetime, timedelta

import pytest

import models
from models import Restaurant, create_restaurant


@pytest.fixture(autouse=True)
def _db(monkeypatch, db_path):
    import sys
    real = models.get_conn
    redirect = lambda *a, **k: real(db_path)  # noqa: E731
    for mod in list(sys.modules.values()):
        try:
            if getattr(mod, "get_conn", None) is real:
                monkeypatch.setattr(mod, "get_conn", redirect)
        except Exception:
            pass
    monkeypatch.setattr(models, "get_conn", redirect)
    monkeypatch.setattr(models, "DB_PATH", db_path)
    yield


def _rid(name="Ledger Co"):
    return create_restaurant(Restaurant(name=name, owner_email=f"{name.split()[0].lower()}@x.test"))


def _q(sql, *args):
    conn = models.get_conn()
    try:
        return [dict(r) for r in conn.execute(sql, args).fetchall()]
    finally:
        conn.close()


def _decline_three(rid):
    import rec_ledger
    for day in ("friday", "saturday", "sunday"):
        key = f"trim_day:{day}"
        rec_ledger.record(rid, key, "shown", surface="home")
        rec_ledger.record(rid, key, "dismissed", surface="home", meta={"kind": "not_for_us"},
                          authority="principal")


# ── use_again (QUALITY-1) ────────────────────────────────────────────────────

def test_restoring_a_kind_is_honoured_by_every_reader():
    import decisions
    import rec_learning
    rid = _rid()
    _decline_three(rid)
    assert [d["kind"] for d in decisions.declined_subjects(rid)] == ["trim_day"]
    assert {r["answer"] for r in decisions.history(rid) if r["key"].startswith("trim_day:")} == {"not for us"}
    assert decisions.restore_kind(rid, "trim_day", user_id=1, authority="principal")
    # 1. the reversal is in the trail, with its authority
    rows = _q("SELECT key, authority FROM rec_events WHERE restaurant_id=? AND event='reopened'", rid)
    assert {r["key"] for r in rows} == {"trim_day:friday", "trim_day:saturday", "trim_day:sunday"}
    assert {r["authority"] for r in rows} == {"principal"}
    # 2. Ask's "do not re-propose" list
    assert decisions.declined_subjects(rid) == []
    # 3. WHAT THE OWNER DECIDED
    hist = {r["key"]: r for r in decisions.history(rid)}
    assert {hist[k]["answer"] for k in hist if k.startswith("trim_day:")} == {"asked to see it again"}
    assert not [l for l in decisions.context(rid).splitlines() if l.startswith("- Trim") and "not for us" in l]
    # 4. the ranker, what worked and fatigue read the episode as taken back
    conn = models.get_conn()
    try:
        eps = rec_learning._load(conn, rid)
    finally:
        conn.close()
    assert {e["state"] for e in eps} == {"reopened"}
    assert "reopened" in rec_learning.UNSETTLED_STATES


def test_use_again_on_one_card_takes_back_that_decline_only():
    import decisions
    import home_brief
    rid = _rid()
    _decline_three(rid)
    home_brief.undismiss(rid, "trim_day:friday", subject_id=1)
    hist = {r["key"]: r["answer"] for r in decisions.history(rid)}
    assert hist["trim_day:friday"] == "asked to see it again"
    assert hist["trim_day:saturday"] == "not for us"
    # two answers left: below the three a kind needs to be named declined
    assert decisions.declined_subjects(rid) == []


def test_a_decline_after_the_reversal_counts_again():
    import decisions
    import rec_ledger
    rid = _rid()
    _decline_three(rid)
    decisions.restore_kind(rid, "trim_day", user_id=1, authority="principal")
    _decline_three(rid)
    assert [d["kind"] for d in decisions.declined_subjects(rid)] == ["trim_day"]
    assert {r["answer"] for r in decisions.history(rid) if r["key"].startswith("trim_day:")} == {"not for us"}
    assert rec_ledger.silenced(rid, "trim_day:friday")


def test_a_managers_reversal_never_takes_back_the_owners_decline():
    import decisions
    import rec_ledger
    rid = _rid()
    _decline_three(rid)
    rec_ledger.unsilence_login(rid, "trim_day:friday", 7, authority="delegate")
    assert [d["kind"] for d in decisions.declined_subjects(rid)] == ["trim_day"]


# ── distrust (LOOPS-11) ──────────────────────────────────────────────────────

def test_dont_trust_the_data_is_not_a_rejection():
    import rec_learning
    import rec_ledger
    rid = _rid()
    rec_ledger.record(rid, "cut_waste:Salmon", "shown", surface="home")
    rec_ledger.record(rid, "cut_waste:Salmon", "dismissed", surface="home",
                      meta={"kind": "not_for_us", "reason_code": "dont_trust_data"}, authority="principal")
    conn = models.get_conn()
    try:
        ep = rec_learning._load(conn, rid)[0]
    finally:
        conn.close()
    assert ep["state"] == "distrusted"
    assert not rec_learning._settled(ep)
    assert "distrusted" in rec_learning.UNSETTLED_STATES


# ── delivered_seen (LOOPS-10) ───────────────────────────────────────────────

def _expired(rid, key, surface, opened=False, age_days=20):
    import rec_ledger
    at = (datetime.utcnow() - timedelta(days=age_days)).strftime("%Y-%m-%d %H:%M:%S")
    rec_ledger.present_many(rid, [{"key": key, "module": "food", "title": key}], surface)
    conn = models.get_conn()
    conn.execute("UPDATE rec_instances SET created_at=?, last_event_at=? WHERE restaurant_id=? AND key=?",
                 (at, at, rid, key))
    conn.execute("UPDATE rec_events SET at=? WHERE restaurant_id=? AND key=?", (at, rid, key))
    conn.commit()
    conn.close()
    if opened:
        rec_ledger.record(rid, key, "opened", surface=surface)


def test_an_unopened_email_is_unseen_not_ignored():
    import rec_learning
    rid = _rid()
    _expired(rid, "cut_waste:A", "weekly_email")
    _expired(rid, "cut_waste:B", "weekly_email", opened=True)
    _expired(rid, "cut_waste:C", "home")
    conn = models.get_conn()
    try:
        states = {e["key"]: e["state"] for e in rec_learning._load(conn, rid)}
        lean = {e["key"]: e["state"] for e in rec_learning._load(conn, rid, lean=True)}
    finally:
        conn.close()
    assert states == {"cut_waste:A": "unseen", "cut_waste:B": "ignored", "cut_waste:C": "ignored"}
    assert lean == states


def test_unseen_expiries_cast_no_quiet_kind_vote():
    import decisions
    import rec_ledger
    rid = _rid()
    for i in range(decisions.QUIET_AFTER_EXPIRED):
        _expired(rid, f"cut_waste:E{i}", "weekly_email", age_days=40 - i)
    conn = models.get_conn()
    conn.execute("UPDATE rec_instances SET status='expired' WHERE restaurant_id=?", (rid,))
    conn.commit()
    conn.close()
    assert "cut_waste" not in decisions.quiet_kinds_vote(rid)
    # the same expiries seen on Home do vote
    rid2 = _rid("Seen Co")
    for i in range(decisions.QUIET_AFTER_EXPIRED):
        _expired(rid2, f"cut_waste:E{i}", "home", age_days=40 - i)
    conn = models.get_conn()
    conn.execute("UPDATE rec_instances SET status='expired' WHERE restaurant_id=?", (rid2,))
    conn.commit()
    conn.close()
    assert "cut_waste" in decisions.quiet_kinds_vote(rid2)
    assert rec_ledger.DELIVERY_ONLY_SURFACES


# ── keep_suggesting (LOOPS-6) ───────────────────────────────────────────────

def test_keep_suggesting_silences_its_question_exactly_as_long_as_it_keeps():
    import rec_learning
    import rec_ledger
    rid = _rid()
    rec_ledger.present(rid, "kind_hold:trim_day", "home", "home", title="Keep suggesting trim day?")
    rec_ledger.record(rid, "kind_hold:trim_day", "completed", surface="home", authority="principal")
    row = _q("SELECT silenced_until, silence_rule FROM rec_instances WHERE restaurant_id=? AND key=?",
             rid, "kind_hold:trim_day")[0]
    assert row["silence_rule"] == "kind_hold_keep"
    keep_end = datetime.utcnow() + timedelta(days=rec_learning.KIND_HOLD_KEEP_DAYS)
    assert abs((datetime.strptime(row["silenced_until"], "%Y-%m-%d %H:%M:%S") - keep_end).days) <= 1
    assert rec_learning.KIND_HOLD_KEEP_DAYS == rec_ledger.KIND_HOLD_KEEP_DAYS
    assert "trim_day" in rec_learning.kept_hold_kinds(rid)
    later = datetime.utcnow() + timedelta(days=rec_learning.KIND_HOLD_KEEP_DAYS + 1)
    assert "trim_day" not in rec_learning.kept_hold_kinds(rid, now=later)
    assert row["silenced_until"] < later.strftime("%Y-%m-%d %H:%M:%S")      # the question can be asked again
    assert "asks again" in rec_ledger.silence_message("kind_hold:trim_day", "completed")


# ── result_episode (LOOPS-7, CROSSMODULE-13) ────────────────────────────────

def test_a_key_only_outcome_lands_on_the_taken_episode():
    import rec_ledger
    rid = _rid()
    key = rec_ledger.rec_key("schedule_coverage", "Fill the gap on Friday night: Server short 1 of 3")
    rec_ledger.present_many(rid, [{"key": key, "module": "schedule", "title": "Fill"}], "schedule_review")
    rec_ledger.record(rid, key, "accepted", surface="schedule_review", authority="principal")
    accepted = _q("SELECT rec_id FROM rec_instances WHERE restaurant_id=? AND key=?", rid, key)[0]["rec_id"]
    conn = models.get_conn()
    conn.execute("UPDATE rec_instances SET silenced_until=datetime('now','-1 day'), "
                 "created_at=datetime('now','-50 days') WHERE rec_id=?", (accepted,))
    conn.commit()
    conn.close()
    newer = rec_ledger.present_many(rid, [{"key": key, "module": "schedule", "title": "Fill"}],
                                    "schedule_review")[key]
    assert newer and newer != accepted
    assert rec_ledger.record(rid, key, "outcome", meta={"verdict": "improved"}, source_ref="nights:x")
    on = _q("SELECT rec_id FROM rec_events WHERE restaurant_id=? AND key=? AND event='outcome'", rid, key)
    assert [r["rec_id"] for r in on] == [accepted]


def test_a_tracker_links_to_the_taken_episode_not_a_newer_showing():
    import rec_ledger
    rid = _rid()
    key = "reprice:Salmon"
    rec_ledger.present(rid, key, "food", "home", title="Reprice")
    rec_ledger.record(rid, key, "accepted", surface="home", authority="principal")
    taken = _q("SELECT rec_id FROM rec_instances WHERE restaurant_id=? AND key=?", rid, key)[0]["rec_id"]
    conn = models.get_conn()
    conn.execute("UPDATE rec_instances SET silenced_until=NULL, created_at=datetime('now','-3 days') "
                 "WHERE rec_id=?", (taken,))
    conn.commit()
    conn.close()
    newer = rec_ledger.present_many(rid, [{"key": key, "module": "food", "title": "Reprice"}], "home")[key]
    assert newer != taken
    assert rec_ledger.link_tracker(rid, key, 99)
    got = {r["rec_id"]: r["tracker_id"] for r in _q("SELECT rec_id, tracker_id FROM rec_instances "
                                                    "WHERE restaurant_id=?", rid)}
    assert got == {taken: 99, newer: None}


def test_only_the_owners_home_logs_the_one_thing_rank_build():
    import inspect
    import business_intelligence as bi
    import strategy_routes
    assert inspect.signature(bi.executive_brief).parameters["log_rank"].default is False
    src = inspect.getsource(bi.executive_brief)
    assert "log_rank=log_rank" in src
    route = inspect.getsource(strategy_routes)
    assert "log_rank=_log_rank" in route and '_aa_rank(u) == "principal"' in route


# ── limits_first (INVENTORY-13) ─────────────────────────────────────────────

def test_an_old_reasoned_decline_survives_many_newer_answers():
    import decisions
    import rec_ledger
    rid = _rid()
    old = (datetime.utcnow() - timedelta(days=300)).strftime("%Y-%m-%d %H:%M:%S")
    rec_ledger.record(rid, "trim_day:Monday", "dismissed", surface="home",
                      meta={"kind": "not_for_us", "reason": "We need the cover"}, authority="principal", at=old)
    conn = models.get_conn()
    for i in range(450):
        rid_ = f"n{i}"
        at = (datetime.utcnow() - timedelta(days=1, seconds=i)).strftime("%Y-%m-%d %H:%M:%S")
        conn.execute("INSERT INTO rec_instances (rec_id, restaurant_id, key, module, kind, title, status, tags, "
                     "created_at, last_event_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                     (rid_, rid, "cut_waste:Salmon", "food", "cut_waste", "t", "accepted", "[]", at, at))
        conn.execute("INSERT INTO rec_events (rec_id, restaurant_id, key, event, dedupe, at, authority) "
                     "VALUES (?,?,?,?,?,?,?)", (rid_, rid, "cut_waste:Salmon", "accepted", f"a{i}", at, "principal"))
    conn.commit()
    conn.close()
    hist = {r["key"]: r for r in decisions.history(rid)}
    assert hist["trim_day:Monday"]["answer"] == "not for us"
    assert hist["trim_day:Monday"]["reason"] == "We need the cover"


def test_the_admin_acceptance_funnel_keeps_the_same_states_out(monkeypatch):
    import admin_ops
    import decisions
    import rec_ledger
    monkeypatch.setattr(admin_ops, "get_conn", models.get_conn, raising=False)
    rid = _rid()
    _decline_three(rid)                                            # three declines ...
    decisions.restore_kind(rid, "trim_day", user_id=1, authority="principal")      # ... taken back
    rec_ledger.record(rid, "cut_waste:Salmon", "shown", surface="home")
    rec_ledger.record(rid, "cut_waste:Salmon", "dismissed", surface="home",
                      meta={"kind": "not_for_us", "reason_code": "dont_trust_data"}, authority="principal")
    _expired(rid, "cut_waste:Emailed", "weekly_email")
    rec_ledger.record(rid, "reprice:Soup", "shown", surface="home")
    rec_ledger.record(rid, "reprice:Soup", "dismissed", surface="home", meta={"kind": "not_for_us"},
                      authority="principal")
    since = (datetime.utcnow() - timedelta(days=60)).strftime("%Y-%m-%d %H:%M:%S")
    conn = models.get_conn()
    try:
        keys = {e["key"] for e in admin_ops._episodes(conn, since, restaurant_id=rid)}
    finally:
        conn.close()
    assert keys == {"reprice:Soup"}
