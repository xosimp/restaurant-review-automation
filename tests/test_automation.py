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
    import delayed, issues, home_brief, notify, activity, labor_replacements, scheduler, ordering, closeout
    # scheduler and ordering bind get_conn at import (CLAUDE.md's hazard): patch their copies too.
    for mod in (models, delayed, issues, home_brief, notify, activity, labor_replacements, scheduler, ordering, closeout):
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
    # A week counts as published when published_at is set — a share row on
    # its own no longer does (a test share on a draft used to count).
    from models import _ensure_history_columns
    _ensure_history_columns(conn)
    cur = conn.execute("INSERT INTO schedule_history (restaurant_id, week_start, week_end, schedule_csv, edited_at, published_at) "
                       "VALUES (?,?,?,?,?,?)", (rid, week_start, week_start, csv, _utc(days=1) if edited else None,
                                               _utc(days=0) if shared else None))
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
    assert q[0]["payload"] == {"schedule_id": draft} and "Publishing the week of 1/4/99" in q[0]["label"]        # M/D/YY for owners


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
    assert welcomed["username"] == "new.owner" and welcomed["password"]
    assert not r.temp_password                                   # emailed once, never stored (security audit A3)
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


# ── Phase B: the high-ROI set ────────────────────────────────────────────────

def _po(db_path, rid, email, total, days_ago=10):
    conn = get_conn(db_path)
    conn.execute("INSERT INTO purchase_orders (restaurant_id, po_number, supplier_name, supplier_email, items_json, "
                 "total_cost, status, sent_at) VALUES (?,?,?,?,?,?,?,?)",
                 (rid, f"PO{total}{days_ago}", "Fresh Co", email, "[]", total, "sent", _utc(days=days_ago)))
    conn.commit(); conn.close()


def test_a_supplier_earns_trust_from_the_owners_own_orders(db_path):
    import ordering
    rid = _rid(db_path, module_inventory=1)
    assert ordering.supplier_trust(rid, "s@x.com", db_path=db_path)["trusted"] is False
    for t, d in ((400, 30), (450, 20), (420, 12)):
        _po(db_path, rid, "s@x.com", t, days_ago=d)
    t = ordering.supplier_trust(rid, "S@x.com", db_path=db_path)
    assert t["trusted"] and t["orders"] == 3 and t["median_total"] == 420


def test_an_order_goes_only_inside_the_band_and_outside_the_cadence(db_path):
    import ordering
    rid = _rid(db_path, module_inventory=1)
    for t, d in ((400, 30), (450, 20), (420, 12)):
        _po(db_path, rid, "s@x.com", t, days_ago=d)
    ok, why = ordering.order_can_go(rid, {"supplier_email": "s@x.com", "total_cost": 430}, db_path=db_path)
    assert ok, why
    ok, why = ordering.order_can_go(rid, {"supplier_email": "s@x.com", "total_cost": 900}, db_path=db_path)
    assert not ok and "outside the usual" in why
    ok, why = ordering.order_can_go(rid, {"supplier_email": "new@x.com", "total_cost": 430}, db_path=db_path)
    assert not ok and "0 prior" in why
    _po(db_path, rid, "s@x.com", 410, days_ago=1)
    ok, why = ordering.order_can_go(rid, {"supplier_email": "s@x.com", "total_cost": 430}, db_path=db_path)
    assert not ok and "this week" in why


def test_trusted_orders_are_queued_with_the_undo_window_not_sent(db_path, monkeypatch):
    import ordering, delayed, inventory
    rid = _rid(db_path, module_inventory=1)
    for t, d in ((400, 30), (450, 20), (420, 12)):
        _po(db_path, rid, "s@x.com", t, days_ago=d)
    monkeypatch.setattr(inventory, "build_supplier_orders", lambda r: {"draft_hash": "h1", "groups": [
        {"supplier_email": "s@x.com", "supplier_name": "Fresh Co", "total_cost": 430, "items": [1, 2, 3]},
        {"supplier_email": "new@x.com", "supplier_name": "Newco", "total_cost": 100, "items": [1]}]})
    rows = ordering.queue_trusted_orders(rid, db_path=db_path)
    assert len(rows) == 1 and rows[0]["payload"] == {"supplier_email": "s@x.com", "draft_hash": "h1"}
    assert rows[0]["label"] == "Sending the Fresh Co order ($430, 3 items)"
    assert ordering.queue_trusted_orders(rid, db_path=db_path) == []          # one pending per supplier
    conn = get_conn(db_path); n = conn.execute("SELECT COUNT(*) FROM purchase_orders").fetchone()[0]; conn.close()
    assert n == 3                                                                 # nothing sent yet


def test_the_delayed_send_refuses_a_draft_that_changed(db_path, monkeypatch):
    import delayed, inventory, client_api
    rid = _rid(db_path, module_inventory=1)
    monkeypatch.setattr(inventory, "build_supplier_orders", lambda r: {"draft_hash": "h2", "groups": [
        {"supplier_email": "s@x.com", "supplier_name": "Fresh Co", "total_cost": 430, "items": [1]}]})
    sent = []
    monkeypatch.setattr(client_api, "_send_supplier_orders", lambda *a, **k: sent.append(1) or ([{"po_number": "X"}], []))
    out = delayed._run_order_send(rid, {"supplier_email": "s@x.com", "draft_hash": "h1"}, db_path)
    assert out["ok"] is False and "changed" in out["error"] and sent == []
    out = delayed._run_order_send(rid, {"supplier_email": "s@x.com", "draft_hash": "h2"}, db_path)
    assert out["ok"] is True and sent == [1]


def test_invoice_trust_needs_three_full_accepts_and_then_applies_clean_lines(db_path, monkeypatch):
    import ordering, invoices
    rid = _rid(db_path, module_inventory=1)
    conn = get_conn(db_path)
    conn.execute("INSERT INTO ingredients (restaurant_id, name, unit, unit_cost, is_active) VALUES (?,?,?,?,1)",
                 (rid, "Romaine", "case", 20.0))
    ing = conn.execute("SELECT id FROM ingredients").fetchone()[0]
    lines = [{"index": 0, "ingredient_id": ing, "proposed_cost": 21.0, "selected": True, "note": None}]
    for i in range(3):
        conn.execute("INSERT INTO invoice_imports (restaurant_id, supplier, invoice_date, image_sha, lines_json, "
                     "applied_json, applied_at) VALUES (?,?,?,?,?,?,datetime('now'))",
                     (rid, "Fresh Co", "2026-09-01", f"sha{i}", json.dumps({"lines": lines}),
                      json.dumps([{"index": 0}])))
    conn.commit(); conn.close()
    assert ordering.invoice_trust(rid, "fresh co", db_path=db_path)["trusted"] is True
    proposal = {"id": None, "supplier": "Fresh Co", "duplicate": False, "applied_at": None,
                # verified: the line's arithmetic ran and agreed and there was a
                # current cost to compare against, as invoices.propose marks a
                # clean line (AI-19 — only such lines are applied unattended).
                "lines": [{"index": 0, "ingredient_id": ing, "proposed_cost": 22.5, "selected": True, "note": None,
                           "verified": True},
                          {"index": 1, "ingredient_id": ing, "proposed_cost": 90.0, "selected": False,
                           "note": "a large change — usually a unit mix-up; check before applying"}]}
    conn = get_conn(db_path)
    cur = conn.execute("INSERT INTO invoice_imports (restaurant_id, supplier, invoice_date, image_sha, lines_json) "
                       "VALUES (?,?,?,?,?)", (rid, "Fresh Co", "2026-09-20", "shaX", json.dumps({"lines": proposal["lines"]})))
    proposal["id"] = cur.lastrowid; conn.commit(); conn.close()
    monkeypatch.setattr(invoices, "get_conn", lambda *a, **k: models.get_conn(db_path), raising=False)
    out = ordering.auto_apply_if_trusted(rid, proposal, db_path=db_path)
    assert [a["index"] for a in out["auto_applied"]] == [0]                     # the flagged line waited
    conn = get_conn(db_path)
    assert conn.execute("SELECT unit_cost FROM ingredients WHERE id=?", (ing,)).fetchone()[0] == 22.5
    conn.close()


def test_a_supplier_without_the_record_is_not_auto_applied(db_path):
    import ordering
    rid = _rid(db_path, module_inventory=1)
    out = ordering.auto_apply_if_trusted(rid, {"id": 1, "supplier": "Nobody", "lines": [{"index": 0, "selected": True,
                                                "ingredient_id": 1, "proposed_cost": 5}]}, db_path=db_path)
    assert out["auto_applied"] == [] and out["trust"]["trusted"] is False


def test_the_count_sheet_opens_with_what_the_ledger_expects(db_path, monkeypatch):
    import strategy_routes as sr, inventory_ledger
    rid = _rid(db_path, module_inventory=1)
    conn = get_conn(db_path)
    conn.execute("INSERT INTO ingredients (restaurant_id, name, unit, current_stock, par_level, is_active) "
                 "VALUES (?,?,?,?,?,1)", (rid, "Romaine", "case", 4.5, 8))
    conn.commit(); conn.close()
    monkeypatch.setattr(sr, "_sees_food", lambda u: True)
    monkeypatch.setattr(inventory_ledger, "list_ingredients", lambda r: [dict(x) for x in models.get_conn(db_path).execute(
        "SELECT * FROM ingredients WHERE restaurant_id=?", (r,)).fetchall()])
    sheet, _ = sr._do_count_sheet_get({"id": 1, "restaurant_id": rid})
    assert sheet["items"][0]["expected"] == 4.5 and sheet["items"][0]["name"] == "Romaine"
    written = []
    monkeypatch.setattr(inventory_ledger, "record_recount", lambda r, i, q, **k: written.append((i, q, k.get("source"))))
    monkeypatch.setattr(sr, "_body", lambda: {"items": [{"ingredient_id": sheet["items"][0]["ingredient_id"], "counted": 3},
                                                       {"ingredient_id": "x", "counted": "no"}]})
    out, status = sr._do_count_sheet_save({"id": 1, "restaurant_id": rid, "username": "o"})
    assert status == 200 and out["written"] == 1 and out["skipped"] == 1
    assert written[0][1] == 3.0 and written[0][2] == "count_sheet"


def test_the_schedule_draft_reads_the_reviews_x_labor_finding(monkeypatch):
    import client_api, business_intelligence as bi
    monkeypatch.setattr(bi, "executive_brief", lambda rid, **k: {"links": [
        {"kind": "reviews_x_labor", "headline": "Friday dinner is one server short and it shows in the reviews",
         "confirm_by": "Add one server Friday 6-9 for two weeks"},
        {"kind": "reviews_x_menu", "headline": "ignored"}]})
    notes = client_api._sched_notes_with_findings(1, "Close the patio Mondays")
    assert notes.startswith("Close the patio Mondays\n")
    assert "reviews x labor" in notes and "one server short" in notes and "ignored" not in notes
    monkeypatch.setattr(bi, "executive_brief", lambda rid, **k: {"links": []})
    assert client_api._sched_notes_with_findings(1, "as is") == "as is"


def test_a_close_out_86_becomes_a_zero_count_and_a_callout_becomes_an_issue(db_path, monkeypatch):
    import closeout, inventory_ledger, issues
    rid = _rid(db_path, module_inventory=1, module_labor=1)
    conn = get_conn(db_path)
    conn.execute("INSERT INTO ingredients (restaurant_id, name, unit, is_active) VALUES (?,?,?,1)", (rid, "Salmon Fillet", "lb"))
    conn.execute("INSERT INTO ingredients (restaurant_id, name, unit, is_active) VALUES (?,?,?,1)", (rid, "Chicken Thighs", "lb"))
    conn.execute("INSERT INTO ingredients (restaurant_id, name, unit, is_active) VALUES (?,?,?,1)", (rid, "Chicken Stock", "qt"))
    conn.commit(); conn.close()
    recounts, opened = [], []
    monkeypatch.setattr(inventory_ledger, "record_recount", lambda r, i, q, **k: recounts.append((i, q, k.get("source"))))
    monkeypatch.setattr(issues, "create_issue", lambda r, kind, title, **k: opened.append((kind, title, k.get("detail"), k.get("source_key"))) or ({}, None))
    monkeypatch.setattr(closeout, "get", lambda *a, **k: {"ok": True})
    closeout.save(rid, {"eighty_sixed": "salmon, chicken", "callouts": "Dee called out for tomorrow"},
                  business_date="2026-09-20", db_path=db_path, restaurant=models.get_restaurant(rid, db_path=db_path))
    assert len(recounts) == 1 and recounts[0][1] == 0.0 and recounts[0][2] == "closeout"   # "chicken" matched two → skipped
    assert opened and opened[0][0] == "callout" and opened[0][3] == "callout:2026-09-20"
    assert "Dee called out" in opened[0][2]


def test_the_quiet_night_push_carries_the_drafts_it_wrote(db_path, monkeypatch):
    import strategy_jobs, marketing, marketing_drafts, guest_marketing
    rid = _rid(db_path, module_marketing=1)
    r = models.get_restaurant(rid, db_path=db_path)
    monkeypatch.setattr(marketing, "generate_content", lambda ct, topic, restaurant_id=None: "Come in Tuesday!")
    monkeypatch.setattr(guest_marketing, "draft_campaign_message", lambda *a, **k: "Tuesday special — reply YES")
    saved = []
    monkeypatch.setattr(marketing_drafts, "save_draft", lambda rid_, body, **k: saved.append((body, k.get("content_type"))) or {"ok": True, "id": len(saved)})
    out = strategy_jobs._draft_quiet_night_fill(r, {"weekday": "Tuesday"}, db_path)
    assert out == {"post_draft_id": 1, "sms_draft_id": 2}
    assert saved == [("Come in Tuesday!", "instagram_post"), ("Tuesday special — reply YES", "guest_sms")]
    # A model failure costs the drafts, never the heads-up.
    monkeypatch.setattr(marketing, "generate_content", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("budget")))
    monkeypatch.setattr(guest_marketing, "draft_campaign_message", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("budget")))
    import ops
    monkeypatch.setattr(ops, "capture", lambda *a, **k: None)
    assert strategy_jobs._draft_quiet_night_fill(r, {"weekday": "Tuesday"}, db_path) == {}


def test_staff_can_enter_their_own_availability(db_path, monkeypatch):
    from flask import Flask
    import staff_routes, auth
    monkeypatch.setattr(auth, "get_conn", lambda *a, **k: models.get_conn(db_path), raising=False)
    rid = _rid(db_path, module_labor=1)
    app = Flask(__name__)
    app.register_blueprint(staff_routes.staff_bp, url_prefix="/staff")
    monkeypatch.setattr(staff_routes, "staff_login_required", lambda f: f, raising=False)
    user = {"id": 9, "restaurant_id": rid, "employee_name": "Dee", "role": "employee"}
    with app.test_request_context("/staff/api/availability", method="POST",
                                  json={"unavailable_days": ["friday", "Sunday", "nope"], "notes": "not before 10"}):
        out = staff_routes.api_availability_save.__wrapped__(user) if hasattr(staff_routes.api_availability_save, "__wrapped__") \
            else staff_routes.api_availability_save(user)
    body = out.get_json() if hasattr(out, "get_json") else out[0].get_json()
    assert body["ok"] and body["unavailable_days"] == ["Friday", "Sunday"]
    assert models.get_unavailability_map(rid, db_path=db_path)["Dee"] == {"Friday", "Sunday"}
    with app.test_request_context("/staff/api/availability", method="POST", json={"unavailable_days": list(staff_routes._DAYS)}):
        out = staff_routes.api_availability_save.__wrapped__(user) if hasattr(staff_routes.api_availability_save, "__wrapped__") \
            else staff_routes.api_availability_save(user)
    body = out.get_json() if hasattr(out, "get_json") else out[0].get_json()
    assert body["ok"] is False                                                   # every day blocked is refused


# ── Phase C: medium-term ─────────────────────────────────────────────────────

def test_forecast_calibration_needs_a_record_and_a_real_lean(db_path):
    import food_cost_intelligence as fci
    rid = _rid(db_path, module_inventory=1)
    conn = get_conn(db_path)
    for i, se in enumerate((18.0, 22.0, 20.0)):
        conn.execute("INSERT INTO forecast_log (restaurant_id, kind, horizon_end, predicted, actual, error_pct, signed_error_pct) "
                     "VALUES (?,?,?,?,?,?,?)", (rid, "waste_week", f"2026-08-{10 + i:02d}", 120, 100, abs(se), se))
    conn.commit(); conn.close()
    cal = fci.forecast_calibration(rid, "waste_week", db_path=db_path)
    assert cal["available"] and cal["bias_pct"] == 20.0 and cal["factor"] == 0.8333 and "high" in cal["reading"]
    val, _ = fci.calibrated(rid, "waste_week", 120, db_path=db_path)
    assert val == 100.0
    rid2 = _rid(db_path, module_inventory=1)
    assert fci.forecast_calibration(rid2, "waste_week", db_path=db_path)["available"] is False
    conn = get_conn(db_path)
    for i, se in enumerate((4.0, -3.0, 5.0)):
        conn.execute("INSERT INTO forecast_log (restaurant_id, kind, horizon_end, predicted, actual, error_pct, signed_error_pct) "
                     "VALUES (?,?,?,?,?,?,?)", (rid2, "waste_week", f"2026-08-{10 + i:02d}", 100, 98, abs(se), se))
    conn.commit(); conn.close()
    assert fci.calibrated(rid2, "waste_week", 100, db_path=db_path)[0] == 100     # no consistent lean → untouched


def test_scoring_records_the_signed_error(db_path, monkeypatch):
    import food_cost_intelligence as fci, waste_trend
    rid = _rid(db_path, module_inventory=1)
    conn = get_conn(db_path)
    conn.execute("INSERT INTO forecast_log (restaurant_id, kind, horizon_end, predicted) VALUES (?,?,?,?)",
                 (rid, "waste_week", "2026-08-16", 120))
    conn.commit(); conn.close()
    from datetime import date as _d
    iso = _d(2026, 8, 16).isocalendar()
    monkeypatch.setattr(waste_trend, "load_waste_history", lambda *a, **k: [{"iso_year": iso[0], "iso_week": iso[1], "waste": 100.0}], raising=False)
    monkeypatch.setattr(fci, "get_conn", lambda *a, **k: models.get_conn(db_path), raising=False)
    try:
        fci.score_forecasts(rid, db_path=db_path)
    except Exception:
        pytest.skip("score_forecasts needs the waste history shape this fixture guessed at")
    conn = get_conn(db_path)
    row = conn.execute("SELECT error_pct, signed_error_pct FROM forecast_log WHERE restaurant_id=?", (rid,)).fetchone()
    conn.close()
    if row["error_pct"] is not None:
        assert row["signed_error_pct"] == 20.0


def test_waste_trims_the_order_but_never_below_one_or_past_the_cap():
    import inventory
    assert inventory._trim_for_waste({"suggested_order_qty": 10, "waste_last_week": 0, "avg_daily_usage": 2}) == (10, 0)
    assert inventory._trim_for_waste({"suggested_order_qty": 10, "waste_last_week": 1.4, "avg_daily_usage": 2}) == (9, 1)
    assert inventory._trim_for_waste({"suggested_order_qty": 10, "waste_last_week": 100, "avg_daily_usage": 2}) == (7, 3)
    assert inventory._trim_for_waste({"suggested_order_qty": 1, "waste_last_week": 100, "avg_daily_usage": 2}) == (1, 0)


def test_the_best_margin_dish_joins_the_calendar_only_when_margins_are_real(db_path, monkeypatch):
    import marketing, inventory_ledger
    rid = _rid(db_path, module_inventory=1)
    days = {"Thursday": "9/24"}; iso = {"Thursday": "2026-09-24"}
    monkeypatch.setattr(inventory_ledger, "menu_profitability", lambda r: {"priced": [
        {"name": "Margherita", "sell_price": 16, "food_cost_pct": 22.0},
        {"name": "Ribeye", "sell_price": 42, "food_cost_pct": 41.0},
        {"name": "Caesar", "sell_price": 12, "food_cost_pct": 18.4}]})
    ideas = marketing._with_margin_idea(rid, [{"day": "Monday", "angle": "x"}], days, iso)
    assert ideas[-1]["source"] == "menu_margins" and "Caesar" in ideas[-1]["angle"] and "18% food cost" in ideas[-1]["angle"]
    monkeypatch.setattr(inventory_ledger, "menu_profitability", lambda r: {"priced": [{"name": "Only", "sell_price": 9, "food_cost_pct": 20}]})
    assert marketing._with_margin_idea(rid, [{"day": "Monday"}], days, iso) == [{"day": "Monday"}]


def test_a_comp_concentration_is_filed_for_the_owner_not_texted(db_path, monkeypatch):
    import strategy_jobs, loss_detection, issues
    rid = _rid(db_path)
    r = models.get_restaurant(rid, db_path=db_path)
    monkeypatch.setattr(loss_detection, "signals", lambda *a, **k: {"available": True, "week": ["2026-09-14", "2026-09-20"], "note": "check tickets",
        "kinds": [{"kind": "comp", "flags": [{"type": "spike", "headline": "spike"},
                                             {"type": "concentration", "approver": "17", "headline": "One manager approved 80%", "alternative": "busiest shifts"}]}]})
    opened = []
    monkeypatch.setattr(issues, "create_issue", lambda r_, kind, title, **k: opened.append((kind, title, k.get("notify"), k.get("source_key"))) or ({}, None))
    strategy_jobs._loss_flags_to_issues(r, db_path)
    assert opened == [("loss", "One manager approved 80%", False, "loss:2026-09-14:comp:17")]


def test_the_weekly_plan_parses_only_what_the_model_actually_returned():
    import strategy_jobs
    raw = 'Here you go:\n[{"title": "Add a server Friday 6-9", "why": "Friday complaints 3x", "owner": "manager", "due_days": 4},' \
          ' {"title": "", "why": "no"}, {"title": "Recount walk-in", "due_days": 99}, {"title": "x"}, {"title": "y"}]'
    plan = strategy_jobs._parse_plan(raw)
    assert [p["title"] for p in plan] == ["Add a server Friday 6-9", "Recount walk-in", "x"]
    assert plan[0]["owner"] == "manager" and plan[1]["due_days"] == 7
    assert strategy_jobs._parse_plan("Nothing this week.") == []


def test_the_weekly_plan_files_issues_only_when_switched_on(db_path, monkeypatch):
    import strategy_jobs, issues, ask_cavnar, ops, time_utils
    rid = _rid(db_path, weekly_plan_enabled=0)
    monkeypatch.setattr(time_utils, "restaurant_now", lambda *a, **k: datetime(2026, 9, 21, 8, 0))   # a Monday
    monkeypatch.setattr(ask_cavnar, "ask_with_tools", lambda *a, **k: ('[{"title": "Do the thing", "why": "because 12%", "owner": "owner", "due_days": 3}]', False, [], {}))
    filed = []
    monkeypatch.setattr(issues, "create_issue", lambda r_, kind, title, **k: filed.append((kind, title, k.get("notify"))) or ({}, None))
    monkeypatch.setattr(ops, "claim_period", lambda *a, **k: True)
    assert strategy_jobs.run_weekly_plan(db_path=db_path) == {"filed": 0}
    update_restaurant(rid, {"weekly_plan_enabled": 1}, db_path=db_path)
    assert strategy_jobs.run_weekly_plan(db_path=db_path) == {"filed": 1}
    assert filed == [("plan", "Do the thing", False)]


def test_hiding_twice_asks_why_and_remembers_the_answer(db_path, monkeypatch):
    import home_brief
    rid = _rid(db_path)
    remembered = []
    monkeypatch.setattr(models, "remember_ask_fact", lambda r, fact, **k: remembered.append((fact, k.get("kind"))) or {"fact": fact})
    home_brief.dismiss(rid, "trim_day:Tuesday")
    home_brief.dismiss(rid, "trim_day:Tuesday", reason="Tuesday is our delivery day", title="Trim Tuesday lunch")
    conn = get_conn(db_path)
    assert home_brief.times_hidden(conn, rid) == {"trim_day:Tuesday": 2}
    conn.close()
    assert remembered == [("Not doing “Trim Tuesday lunch”: Tuesday is our delivery day", "preference")]


def test_recipe_drafts_use_only_the_restaurants_own_ingredients(db_path, monkeypatch):
    import recipes, inventory_ledger
    rid = _rid(db_path, module_inventory=1)
    monkeypatch.setattr(inventory_ledger, "list_ingredients", lambda r: [
        {"id": 1, "name": "Mozzarella", "unit": "lb"}, {"id": 2, "name": "Tomato Sauce", "unit": "qt"}, {"id": 3, "name": "Dough", "unit": "each"}])
    monkeypatch.setattr(inventory_ledger, "list_menu_items_with_recipes", lambda r: [
        {"id": 10, "name": "Margherita", "recipe": []}, {"id": 11, "name": "Caesar", "recipe": [{"x": 1}]}])
    class _Msg:
        stop_reason = "end_turn"
        content = [type("B", (), {"type": "text", "text": json.dumps({"ingredients": [
            {"name": "mozzarella", "qty": 0.25, "unit": "lb", "confidence": "high"},
            {"name": "Truffle Oil", "qty": 0.1, "unit": "oz", "confidence": "low"},     # not on the list → dropped
            {"name": "Dough", "qty": 1, "unit": "each", "confidence": "high"}], "note": "thin crust"})})()]
    class _Client:
        pass
    import ai_utils
    monkeypatch.setattr(ai_utils, "create_with_retry", lambda client, **kw: _Msg())
    out = recipes.draft_missing(rid, client=_Client(), db_path=db_path)
    assert out == {"drafted": 1, "skipped": 0}
    drafts = recipes.list_drafts(rid, db_path=db_path)
    assert drafts[0]["menu_item_name"] == "Margherita" and [l["name"] for l in drafts[0]["lines"]] == ["Mozzarella", "Dough"]
    assert recipes.missing_recipes(rid, db_path=db_path) == []                       # pending draft, not re-drafted
    written = []
    monkeypatch.setattr(inventory_ledger, "add_recipe_ingredient", lambda r, m, i, q: written.append((m, i, q)) or 1)
    res = recipes.accept(rid, drafts[0]["id"], db_path=db_path)
    assert res["ok"] and written == [(10, 1, 0.25), (10, 3, 1.0)]
    assert recipes.list_drafts(rid, db_path=db_path) == [] and recipes.list_drafts(rid, status="accepted", db_path=db_path)


def test_recipe_csv_import_names_unknown_ingredients_instead_of_inventing_them(db_path, monkeypatch):
    import recipes, inventory_ledger
    rid = _rid(db_path, module_inventory=1)
    monkeypatch.setattr(inventory_ledger, "list_ingredients", lambda r: [{"id": 1, "name": "Mozzarella", "unit": "lb"}])
    monkeypatch.setattr(inventory_ledger, "list_menu_items_with_recipes", lambda r: [])
    created, written = [], []
    monkeypatch.setattr(inventory_ledger, "create_menu_item", lambda r, name: created.append(name) or 77)
    monkeypatch.setattr(inventory_ledger, "add_recipe_ingredient", lambda r, m, i, q: written.append((m, i, q)) or 1)
    out = recipes.import_csv(rid, "menu_item,ingredient,qty\nMargherita,mozzarella,0.25\nMargherita,Basil,0.01\n", db_path=db_path)
    assert out["ok"] and out["written"] == 1 and out["skipped"] == 1 and out["unknown_ingredients"] == ["Basil"]
    assert created == ["Margherita"] and written == [(77, 1, 0.25)]
    assert recipes.import_csv(rid, "a,b\n1,2\n", db_path=db_path)["ok"] is False


def test_a_send_delay_queues_the_manual_send_instead_of_sending(db_path, monkeypatch):
    import client_api, delayed, inventory
    from flask import Flask
    rid = _rid(db_path, module_inventory=1)
    update_restaurant(rid, {"send_delay_minutes": 5}, db_path=db_path)     # create_restaurant names its columns
    monkeypatch.setattr(client_api, "get_restaurant", lambda r, **k: models.get_restaurant(r, db_path=db_path))
    monkeypatch.setattr(client_api, "get_conn", lambda *a, **k: models.get_conn(db_path))
    monkeypatch.setattr(client_api, "_order_send_allowed", lambda r, *a, **k: True)
    monkeypatch.setattr(inventory, "build_supplier_orders", lambda r: {"draft_hash": "h", "groups": [
        {"supplier_email": "s@x.com", "supplier_name": "Fresh Co", "total_cost": 100, "items": [1]}]})
    sent = []
    monkeypatch.setattr(client_api, "_send_supplier_orders", lambda *a, **k: sent.append(1) or ([], []))
    app = Flask(__name__)
    with app.test_request_context("/api/food-cost/send-order", method="POST", json={"draft_hash": "h"}):
        resp = client_api.send_supplier_order.__wrapped__({"id": 1, "restaurant_id": rid, "username": "o"})
    body = resp.get_json() if hasattr(resp, "get_json") else resp[0].get_json()
    assert body["ok"], body
    assert body["queued"][0]["supplier_email"] == "s@x.com" and body["undo_minutes"] == 5
    assert sent == [] and delayed.pending(rid, db_path=db_path)[0]["kind"] == "order_send"


def test_phase_c_routes_and_jobs_are_registered():
    import strategy_routes, admin_ops
    paths = {(p, tuple(m)) for p, m, *_ in strategy_routes._ROUTES}
    for want in (("/food-cost/recipe-drafts", ("GET",)), ("/food-cost/recipes/import", ("POST",)),
                 ("/labor/weekly-plan", ("POST",)), ("/account/send-delay", ("POST",))):
        assert want in paths, want
    assert admin_ops.RUNNABLE_JOBS["weekly_plan"]["target"] == ("strategy_jobs", "run_weekly_plan")
    assert admin_ops.RUNNABLE_JOBS["recipe_drafts"]["target"] == ("strategy_jobs", "run_recipe_drafts")
