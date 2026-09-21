"""The automation audit's implementation (Sep 2026).

The rule under every test here: an automation graduates from "propose" to
"do" only on the owner's own measured record, with an undo window, an
audit line naming the automation as the actor, and a switch that defaults
off. And the negative bands never graduate at all.
"""
import json
from datetime import datetime, timedelta, timezone

import pytest

import models
from models import Restaurant, create_restaurant, get_conn, update_restaurant


@pytest.fixture(autouse=True)
def _redirect(monkeypatch, db_path):
    real = models.get_conn
    import delayed, issues, home_brief, notify, activity, labor_replacements, scheduler
    # scheduler binds get_conn at import (CLAUDE.md's hazard): patch its copy too.
    for mod in (models, delayed, issues, home_brief, notify, activity, labor_replacements, scheduler):
        monkeypatch.setattr(mod, "get_conn", lambda *a, **k: real(db_path), raising=False)
        monkeypatch.setattr(mod, "DB_PATH", db_path, raising=False)
    monkeypatch.setattr(models, "DB_PATH", db_path)
    import auth
    auth.init_auth(db_path=db_path)


def _rid(db_path, **kw):
    kw.setdefault("module_reviews", 1)
    return create_restaurant(Restaurant(name="Auto Co", owner_email="a@x.com", **kw), db_path=db_path)


def _utc(**delta):
    return (datetime.now(timezone.utc) - timedelta(**delta)).strftime("%Y-%m-%d %H:%M:%S")


def _approved(db_path, rid, star, n, edited=0):
    conn = get_conn(db_path)
    for i in range(n):
        conn.execute("INSERT INTO reviews (restaurant_id, platform, external_id, author, rating, text, review_date, "
                     "fetched_at, processed, draft_response, response_status, approved_at, draft_edited) "
                     "VALUES (?,?,?,?,?,?,?,?,1,?,?,?,?)",
                     (rid, "google", f"s{star}n{i}e{edited}", "A", star, "t", _utc(days=3), _utc(days=3), "Thanks",
                      "approved", _utc(days=2), 1 if i < edited else 0))
    conn.commit(); conn.close()


# ── #1 graduated auto-approve ─────────────────────────────────────────────────

def test_trust_is_earned_per_band_from_the_owners_own_edits(db_path):
    rid = _rid(db_path)
    _approved(db_path, rid, 5, 12, edited=1)      # 8% edited → trusted
    _approved(db_path, rid, 4, 12, edited=3)      # 25% edited → not
    _approved(db_path, rid, 3, 4)                 # too few
    t = models.auto_approve_trust(rid, db_path=db_path)
    assert t[5]["trusted"] and t[5]["approved"] == 12
    assert not t[4]["trusted"] and t[4]["edit_rate"] == 0.25
    assert not t[3]["trusted"] and t[3]["needed"] == 6
    assert set(t) == {3, 4, 5}                     # 1★ and 2★ are never in the answer


def test_earned_bands_widen_the_rule_only_when_the_switch_is_on(db_path, monkeypatch):
    import scheduler
    rid = _rid(db_path)
    _approved(db_path, rid, 3, 15)                 # 3★ trusted
    seen = {}
    def _cands(r, ratings=(5,), **k):
        seen["ratings"] = ratings
        return []
    monkeypatch.setattr(models, "auto_approve_candidates", _cands)
    monkeypatch.setattr(models, "count_auto_approved_today", lambda *a, **k: 0)
    update_restaurant(rid, {"auto_approve_5star": 1}, db_path=db_path)
    scheduler.auto_approve_five_stars(rid, models.get_restaurant(rid, db_path=db_path))
    assert seen["ratings"] == (5,)
    seen.clear()
    update_restaurant(rid, {"auto_approve_earned": 1}, db_path=db_path)
    scheduler.auto_approve_five_stars(rid, models.get_restaurant(rid, db_path=db_path))
    assert seen["ratings"] == (3, 5)


def test_the_setting_round_trips_with_the_trust_record(db_path, monkeypatch):
    import client_api
    rid = _rid(db_path)
    client_api._do_auto_approve(rid, {"enabled": True, "earned": True, "daily_cap": 8})
    r = models.get_restaurant(rid, db_path=db_path)
    assert r.auto_approve_earned == 1
    client_api._do_auto_approve(rid, {"enabled": False, "earned": True})
    assert models.get_restaurant(rid, db_path=db_path).auto_approve_earned == 0   # off means off
    assert set(client_api._auto_approve_trust_safe(rid)) == {"3", "4", "5"}


# ── the delayed queue: the undo window ────────────────────────────────────────

def test_a_delayed_action_waits_can_be_undone_and_runs_once(db_path, monkeypatch):
    import delayed
    rid = _rid(db_path)
    ran = []
    monkeypatch.setitem(delayed.HANDLERS, "schedule_publish", lambda r, p, d: ran.append(p) or {"ok": True})
    a = delayed.schedule(rid, "schedule_publish", {"schedule_id": 7}, 120, label="Publishing the week of 9/28")
    assert a["status"] == "pending" and a["execute_at"].endswith("Z")
    assert delayed.pending(rid, db_path=db_path)[0]["label"] == "Publishing the week of 9/28"
    # Not due yet: nothing runs.
    assert delayed.run_due(db_path=db_path) == {"ran": 0, "failed": 0} and ran == []
    # Due: runs exactly once, even if asked twice.
    later = datetime.now(timezone.utc) + timedelta(hours=3)
    assert delayed.run_due(db_path=db_path, now=later)["ran"] == 1
    assert delayed.run_due(db_path=db_path, now=later)["ran"] == 0
    assert ran == [{"schedule_id": 7}]
    # Undo after the fact is refused honestly.
    assert delayed.cancel(rid, a["id"], db_path=db_path) is False


def test_undo_cancels_before_the_window_closes(db_path, monkeypatch):
    import delayed
    rid = _rid(db_path)
    ran = []
    monkeypatch.setitem(delayed.HANDLERS, "order_send", lambda r, p, d: ran.append(1) or {"ok": True})
    a = delayed.schedule(rid, "order_send", {"supplier_email": "s@x.com"}, 10)
    assert delayed.cancel(rid, a["id"], actor={"username": "o"}, db_path=db_path) is True
    assert delayed.cancel(rid + 1, a["id"], db_path=db_path) is False          # another restaurant cannot
    assert delayed.run_due(db_path=db_path, now=datetime.now(timezone.utc) + timedelta(hours=1))["ran"] == 0
    assert ran == [] and delayed.pending(rid, db_path=db_path) == []


def test_a_failing_handler_is_recorded_not_retried(db_path, monkeypatch):
    import delayed, ops
    rid = _rid(db_path)
    monkeypatch.setitem(delayed.HANDLERS, "order_send", lambda r, p, d: (_ for _ in ()).throw(RuntimeError("smtp down")))
    monkeypatch.setattr(ops, "capture", lambda *a, **k: None)
    a = delayed.schedule(rid, "order_send", {}, 1)
    out = delayed.run_due(db_path=db_path, now=datetime.now(timezone.utc) + timedelta(hours=1))
    assert out == {"ran": 0, "failed": 1}
    conn = get_conn(db_path)
    row = conn.execute("SELECT status, result_json FROM delayed_actions WHERE id=?", (a["id"],)).fetchone()
    conn.close()
    assert row["status"] == "failed" and "smtp down" in row["result_json"]


def test_the_feed_shows_what_is_queued(db_path):
    import delayed, activity
    rid = _rid(db_path)
    delayed.schedule(rid, "schedule_publish", {"schedule_id": 1}, 60, label="Publishing the week of 9/28 to staff")
    q = activity.build(rid, db_path=db_path)["queued"]
    assert q and q[0]["text"] == "Publishing the week of 9/28 to staff" and q[0]["kind"] == "schedule_publish"


# ── #3 publish when unedited ──────────────────────────────────────────────────

def _schedule(db_path, rid, week_start, edited=False, shared=True, csv="employee,role\nA,server"):
    conn = get_conn(db_path)
    cur = conn.execute("INSERT INTO schedule_history (restaurant_id, week_start, week_end, schedule_csv, edited_at) "
                       "VALUES (?,?,?,?,?)", (rid, week_start, week_start, csv, _utc(days=1) if edited else None))
    sid = cur.lastrowid
    if shared:
        conn.execute("INSERT INTO schedule_shares (restaurant_id, schedule_id, employee_name, token) VALUES (?,?,?,?)",
                     (rid, sid, "A", f"tok{sid}"))
    conn.commit(); conn.close()
    return sid


def test_publish_trust_counts_consecutive_unedited_publishes(db_path):
    rid = _rid(db_path, module_labor=1)
    assert models.schedule_publish_trust(rid, db_path=db_path) == 0
    _schedule(db_path, rid, "2026-08-31", edited=True)
    _schedule(db_path, rid, "2026-09-07")
    _schedule(db_path, rid, "2026-09-14")
    _schedule(db_path, rid, "2026-09-21")
    assert models.schedule_publish_trust(rid, db_path=db_path) == 3
    _schedule(db_path, rid, "2026-09-28", edited=True)
    assert models.schedule_publish_trust(rid, db_path=db_path) == 0     # an edit resets it


def test_auto_publish_queues_only_with_the_switch_and_the_record(db_path, monkeypatch):
    import scheduler, delayed, strategy_jobs
    rid = _rid(db_path, module_labor=1)
    for w in ("2026-09-07", "2026-09-14", "2026-09-21"):
        _schedule(db_path, rid, w)
    draft = _schedule(db_path, rid, "2099-01-04", shared=False)          # next week's, untouched, unshared
    monkeypatch.setattr(scheduler, "local_due", lambda *a, **k: True)
    reached = []
    monkeypatch.setattr(strategy_jobs, "_reach", lambda *a, **k: reached.append(a[1]) or 1)
    from time_utils import restaurant_now
    friday = datetime(2026, 9, 25, 9, 30)                                 # a Friday
    import time_utils
    monkeypatch.setattr(time_utils, "restaurant_now", lambda *a, **k: friday)
    monkeypatch.setattr(scheduler, "restaurant_now", lambda *a, **k: friday, raising=False)
    monkeypatch.setattr(models, "get_all_restaurants", lambda *a, **k: [models.get_restaurant(rid, db_path=db_path)])
    assert scheduler.run_auto_publish_schedules()["queued"] == 0         # switch off
    update_restaurant(rid, {"auto_publish_schedule": 1}, db_path=db_path)
    monkeypatch.setattr(models, "get_all_restaurants", lambda *a, **k: [models.get_restaurant(rid, db_path=db_path)])
    out = scheduler.run_auto_publish_schedules()
    assert out["queued"] == 1 and reached == ["schedule_publish_pending"]
    q = delayed.pending(rid, db_path=db_path)
    assert q[0]["payload"] == {"schedule_id": draft} and "Publishing the week of 2099-01-04" in q[0]["label"]


def test_auto_publish_never_touches_an_edited_or_already_shared_draft(db_path, monkeypatch):
    import scheduler, delayed, strategy_jobs, time_utils
    rid = _rid(db_path, module_labor=1, auto_publish_schedule=1)
    for w in ("2026-09-07", "2026-09-14", "2026-09-21"):
        _schedule(db_path, rid, w)
    _schedule(db_path, rid, "2099-01-04", edited=True, shared=False)
    monkeypatch.setattr(scheduler, "local_due", lambda *a, **k: True)
    monkeypatch.setattr(strategy_jobs, "_reach", lambda *a, **k: 1)
    monkeypatch.setattr(time_utils, "restaurant_now", lambda *a, **k: datetime(2026, 9, 25, 9, 30))
    monkeypatch.setattr(models, "get_all_restaurants", lambda *a, **k: [models.get_restaurant(rid, db_path=db_path)])
    assert scheduler.run_auto_publish_schedules()["queued"] == 0
    assert delayed.pending(rid, db_path=db_path) == []


def test_the_shared_publish_body_names_the_automation_as_actor(db_path, monkeypatch):
    import client_api, delayed
    rid = _rid(db_path, module_labor=1)
    conn = get_conn(db_path)
    conn.execute("INSERT INTO staff_contacts (restaurant_id, employee_name, email) VALUES (?,?,?)", (rid, "A", "a@x.com"))
    conn.commit(); conn.close()
    sid = _schedule(db_path, rid, "2099-01-04", shared=False)
    import emails
    monkeypatch.setattr(emails, "send_staff_schedule_email", lambda **kw: None)
    logged = []
    monkeypatch.setattr(client_api, "log_account_event", lambda r, t, u=None, detail=None, **k: logged.append((t, (u or {}).get("username"))))
    monkeypatch.setattr(client_api, "get_restaurant", lambda r, **k: models.get_restaurant(r, db_path=db_path))
    monkeypatch.setattr(client_api, "get_conn", lambda *a, **k: models.get_conn(db_path))
    out, status = client_api._publish_schedule(rid, sid, delayed.AUTOMATION_ACTOR)
    assert status == 200 and out["ok"] and out["sent"][0]["employee_name"] == "A"
    assert ("schedule_published", "Cavnar AI") in logged


# ── #5 callout → who can cover, in the issue ─────────────────────────────────

def test_replacements_are_ranked_free_and_not_already_working(db_path):
    import labor_replacements
    rid = _rid(db_path, module_labor=1)
    conn = get_conn(db_path)
    for n in ("Ana", "Ben", "Cal", "Dee"):
        conn.execute("INSERT INTO staff_contacts (restaurant_id, employee_name) VALUES (?,?)", (rid, n))
    conn.commit(); conn.close()
    models.init_staff_availability(db_path)
    models.init_staff_capabilities(db_path)
    models.save_staff_availability(rid, "Ben", ["Monday"], ["Friday"], db_path=db_path)
    conn = get_conn(db_path)
    conn.execute("INSERT INTO staff_capabilities (restaurant_id, employee_name, attribute, score) VALUES (?,?,?,?)",
                 (rid, "Cal", "overall", 4.5))
    conn.commit(); conn.close()
    fits = labor_replacements.for_gap(rid, "server", "Friday", exclude={"Ana"}, db_path=db_path)
    names = [f["name"] for f in fits]
    assert "Ana" not in names and "Ben" not in names          # missing person; unavailable Friday
    assert names[0] == "Cal" and fits[0]["score"] is not None  # rated first
    assert names[1] == "Dee" and fits[1]["score"] is None      # unrated is not a low score
    line = labor_replacements.sentence([{"name": "Cal", "score": 4.5}, {"name": "Dee", "score": None}])
    assert line == " Free today and best placed to cover: Cal (4.5), Dee."


# ── #11 an issue resolved answers the recommendation ─────────────────────────

def test_resolving_an_issue_closes_the_recommendation_that_raised_it(db_path, monkeypatch):
    import issues, home_brief
    rid = _rid(db_path)
    done = []
    monkeypatch.setattr(home_brief, "dismiss", lambda r, key, kind="recommendation", **k: done.append((key, kind)))
    issue, _ = issues.create_issue(rid, "recommendation", "Cut Tuesday lunch", source_key="trim_day:Tuesday", db_path=db_path)
    issues.resolve(rid, issue["id"], "done", db_path=db_path)
    assert done == [("trim_day:Tuesday", "done")]
    issue2, _ = issues.create_issue(rid, "coverage", "X hasn't clocked in", source_key="coverage:2026-09-25:x", db_path=db_path)
    issues.resolve(rid, issue2["id"], db_path=db_path)
    assert len(done) == 1                                                  # operational keys have no card


# ── #12 thresholds from the restaurant's own band ────────────────────────────

def test_default_thresholds_come_from_the_baseline_and_the_owner_still_wins(db_path, monkeypatch):
    import notify, metrics
    rid = _rid(db_path)
    monkeypatch.setattr(metrics, "trailing", lambda r, key, **k: {"value": {"avg_rating": 4.7, "labor_pct": 24.0}[key]})
    assert notify.baseline_rating_floor(rid, db_path) == 4.5
    assert notify.baseline_labor_target(rid, db_path) == 26.0
    monkeypatch.setattr(metrics, "trailing", lambda r, key, **k: {"value": None})
    assert notify.baseline_rating_floor(rid, db_path) is None             # no data, no invented default
    monkeypatch.setattr(metrics, "trailing", lambda r, key, **k: {"value": {"avg_rating": 2.5, "labor_pct": 60.0}[key]})
    assert notify.baseline_rating_floor(rid, db_path) == 3.0 and notify.baseline_labor_target(rid, db_path) == 45.0


# ── #10 provisioning from checkout ───────────────────────────────────────────

def test_a_checkout_with_no_match_provisions_the_restaurant_and_welcomes_the_owner(db_path, monkeypatch):
    import provisioning, emails
    welcomed = {}
    monkeypatch.setattr(emails, "send_welcome_email", lambda **kw: welcomed.update(kw))
    import auth
    monkeypatch.setattr(auth, "get_conn", lambda *a, **k: models.get_conn(db_path), raising=False)
    sess = {"customer": "cus_new", "customer_details": {"email": "New.Owner@x.com", "name": "New Owner"},
            "metadata": {"restaurant": "The New Place", "module_keys": "reviews,labor"}}
    rid = provisioning.provision_from_checkout(sess, db_path=db_path)
    r = models.get_restaurant(rid, db_path=db_path)
    assert r.name == "The New Place" and r.owner_email == "new.owner@x.com"
    assert r.module_reviews == 1 and r.module_labor == 1 and r.module_inventory == 0
    assert r.billing_status == "active" and r.stripe_customer_id == "cus_new"
    assert welcomed["username"] == "new.owner" and welcomed["password"] == r.temp_password
    # The same email again is a reconciliation, not a provisioning.
    assert provisioning.provision_from_checkout(sess, db_path=db_path) is None
    # No email: refuse rather than guess.
    assert provisioning.provision_from_checkout({"metadata": {"restaurant": "X"}}, db_path=db_path) is None


def test_routes_and_switches_exist_on_both_sides():
    import strategy_routes
    paths = {(p, tuple(m)) for p, m, *_ in strategy_routes._ROUTES}
    for want in (("/labor/auto-publish", ("GET",)), ("/labor/auto-publish", ("POST",)),
                 ("/actions/pending", ("GET",)), ("/actions/<int:action_id>/cancel", ("POST",))):
        assert want in paths, want
    import notify
    assert "schedule_publish_pending" in notify.BRIEFING_CALM and "schedule_publish_pending" in models.NON_ALERT_TYPES
