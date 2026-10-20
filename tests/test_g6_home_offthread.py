"""Home's kept blocks off the request thread (blind re-audit 10/20/26, R6).

F4/F5  a missing block is never built on a request: it is queued for the
       queue worker (job_queue `home_snapshot`, group `home`) and the route
       answers `preparing`; a stale one is served and rebuilt there too; the
       hourly precompute builds every scope opened lately before the owner
       wakes — managers' as well as the owner's.
F6     a write the owner makes (a close-out, a Send, an inventory count, a
       reply) moves the stamp: the kept block is served `stale` and rebuilt
       at the interactive class.
F8     /api/home/follow re-reads what was missing by name; the web never
       falls back to the fourteen reads.
F9     the data stamp is the raw restaurant's, whatever view a route passes.
F10    the group view's location rows are never built on the request.
F11    the mobile Home ETag can match.
F12    a key's lock is never forgotten while it is held.
F13    a failed Home block says it failed (web).
F14    "/" no longer computes value_delivered.breakdown.
F7     Studio rechecks and pre-publish reviews are coalesced (web).
"""
import json
import os
from datetime import datetime, timedelta, timezone

import pytest
from flask import Flask

import auth
import home_snapshots
import job_queue
import models
from models import Restaurant, create_restaurant, get_conn

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@pytest.fixture(autouse=True)
def _redirect_db(monkeypatch, db_path):
    import metrics, outcomes, goals, menu_intelligence, demand, loss_detection, issues
    import invoices, strategy_jobs, review_intelligence, food_cost_intelligence
    import business_intelligence, morning_brief, milestones, good_news, home_brief
    real = models.get_conn
    redirect = lambda *a, **k: real(db_path)
    for m in (models, auth, metrics, outcomes, goals, menu_intelligence, demand, loss_detection,
              issues, invoices, strategy_jobs, review_intelligence, food_cost_intelligence,
              business_intelligence, morning_brief, milestones, good_news, home_brief):
        monkeypatch.setattr(m, "get_conn", redirect, raising=False)
    monkeypatch.setattr(models, "DB_PATH", db_path)
    monkeypatch.setattr(auth, "DB_PATH", db_path)
    monkeypatch.setenv("HOME_SNAPSHOTS", "1")


@pytest.fixture
def queued(monkeypatch):
    """The production path (HOME_SNAPSHOT_COLD unset → queue), with the queue
    held: what was enqueued is recorded, nothing runs in the background."""
    monkeypatch.setenv("HOME_SNAPSHOT_COLD", "queue")
    calls = []

    def enqueue(kind, payload=None, restaurant_id=None, priority_class=None, idempotency_key=None, **kw):
        calls.append({"kind": kind, "payload": payload, "restaurant_id": restaurant_id,
                      "class": priority_class, "key": idempotency_key})
        return len(calls), True
    monkeypatch.setattr(job_queue, "enqueue", enqueue)
    return calls


def _rid(db_path, **kw):
    return create_restaurant(Restaurant(name=kw.pop("name", "Off Co"), owner_email="o@x.com", module_reviews=1, **kw),
                             db_path=db_path)


@pytest.fixture
def client():
    from strategy_routes import strategy_bp, strategy_mobile_bp
    app = Flask(__name__, template_folder="../templates")
    app.register_blueprint(strategy_bp)
    app.register_blueprint(strategy_mobile_bp)
    return app.test_client()


def _as(monkeypatch, rid, role="client", uid=5):
    monkeypatch.setattr(auth, "get_current_user",
                        lambda: {"id": uid, "restaurant_id": rid, "is_admin": 0, "role": role,
                                 "username": "u", "email": "u@x.com", "password_hash": "secret-hash"})


def _no_builds(monkeypatch):
    import business_intelligence, good_news, monthly_review, morning_brief

    def _boom(*a, **k):
        raise AssertionError("built on a request thread")
    for mod, fn in ((morning_brief, "build"), (good_news, "all_good_news"),
                    (monthly_review, "build"), (business_intelligence, "executive_brief")):
        monkeypatch.setattr(mod, fn, _boom)


# ── F4: a cold read is queued and answers preparing ─────────────────────────

@pytest.mark.parametrize("path,block", [("/api/morning-brief?view=home", "morning_brief"),
                                        ("/api/good-news", "good_news"),
                                        ("/api/monthly-review", "monthly_review"),
                                        ("/api/cross-module", "cross_module")])
def test_a_cold_read_queues_the_build_and_answers_preparing(client, db_path, monkeypatch, queued, path, block):
    rid = _rid(db_path)
    _as(monkeypatch, rid)
    _no_builds(monkeypatch)
    body = client.get(path).get_json()
    assert body["ok"] and body["preparing"] is True and body["retry_after"] == home_snapshots.RETRY_AFTER_SECONDS
    assert len(queued) == 1
    job = queued[0]
    assert job["kind"] == "home_snapshot" and job["class"] == "interactive" and job["restaurant_id"] == rid
    assert job["payload"]["block"] == block
    # The login it is for rides along, never a credential or an email.
    assert job["payload"]["viewer"]["id"] == 5
    assert "password_hash" not in job["payload"]["viewer"] and "email" not in job["payload"]["viewer"]


def test_the_brief_settings_still_answer_while_the_brief_is_prepared(client, db_path, monkeypatch, queued):
    rid = _rid(db_path)
    _as(monkeypatch, rid)
    _no_builds(monkeypatch)
    body = client.get("/api/morning-brief").get_json()
    assert body["brief"] is None and body["preparing"] and "hour" in body["settings"]


def test_the_queued_build_is_kept_and_the_next_read_serves_it(client, db_path, monkeypatch, queued):
    import good_news
    rid = _rid(db_path)
    _as(monkeypatch, rid)
    _no_builds(monkeypatch)
    assert client.get("/api/good-news").get_json()["preparing"]
    # The queue worker runs the row: no request context, the login's view.
    monkeypatch.setattr(good_news, "all_good_news", lambda *a, **k: [{"key": "r", "headline": "A record"}])
    job = queued[0]
    assert home_snapshots.run_queued({"restaurant_id": rid, "payload": job["payload"],
                                      "enqueued_at": 0}) == {"result": "built"}
    _no_builds(monkeypatch)
    body = client.get("/api/good-news").get_json()
    assert "preparing" not in body and body["items"][0]["headline"] == "A record"


def test_the_same_cold_key_queues_one_job_however_many_reads_ask(db_path, monkeypatch):
    monkeypatch.setenv("HOME_SNAPSHOT_COLD", "queue")
    monkeypatch.setattr(job_queue, "kick", lambda *a, **k: None)
    rid = _rid(db_path)
    app = Flask(__name__)
    for _ in range(3):
        with app.test_request_context("/api/good-news"):
            with pytest.raises(home_snapshots.SnapshotPreparing):
                home_snapshots.serve("good_news", rid, "d:-:s0", "2026-10-20", lambda: {"x": 1},
                                     viewer={"id": 5})
    conn = get_conn(db_path)
    n = conn.execute("SELECT COUNT(*) FROM jobs WHERE kind='home_snapshot' AND restaurant_id=?", (rid,)).fetchone()[0]
    grp = conn.execute("SELECT grp, priority_class FROM jobs WHERE kind='home_snapshot'").fetchone()
    conn.close()
    assert n == 1 and grp["grp"] == "home" and grp["priority_class"] == "interactive"


def test_the_home_group_is_its_own_pool_and_the_handler_resolves():
    assert job_queue.KINDS["home_snapshot"][0] == "home" and "home" in job_queue.GROUPS
    assert job_queue.group_concurrency("home") == 2
    assert job_queue._resolve(job_queue.HANDLERS["home_snapshot"]) is home_snapshots.run_queued


def test_off_a_request_a_missing_block_is_still_built_inline(db_path, monkeypatch):
    monkeypatch.setenv("HOME_SNAPSHOT_COLD", "queue")
    rid = _rid(db_path)
    assert home_snapshots.serve("good_news", rid, "d:-:s1", "2026-10-20", lambda: {"x": 1}) == {"x": 1}


# ── F5 / F6: a stale row is served, flagged and rebuilt in the queue ─────────

def test_a_write_marks_the_kept_block_stale_and_queues_an_interactive_rebuild(client, db_path, monkeypatch,
                                                                              queued):
    import good_news
    rid = _rid(db_path)
    _as(monkeypatch, rid)
    monkeypatch.setattr(good_news, "all_good_news", lambda *a, **k: [])
    monkeypatch.setenv("HOME_SNAPSHOT_COLD", "request")
    client.get("/api/good-news")                       # kept
    monkeypatch.setenv("HOME_SNAPSHOT_COLD", "queue")
    assert "stale" not in client.get("/api/good-news").get_json() and not queued
    import closeout
    closeout.save(rid, {"went_well": "Busy patio"}, db_path=db_path)
    body = client.get("/api/good-news").get_json()
    assert body["stale"] is True and body["retry_after"] == home_snapshots.RETRY_AFTER_SECONDS
    assert len(queued) == 1 and queued[0]["class"] == "interactive"


def test_an_old_row_with_nothing_moved_is_refreshed_at_the_bulk_class_unflagged(db_path, monkeypatch, queued):
    rid = _rid(db_path)
    monkeypatch.setenv("HOME_SNAPSHOT_COLD", "request")
    app = Flask(__name__)
    today = "2026-10-20"
    with app.test_request_context("/"):
        home_snapshots.serve("monthly_review", rid, "all:s1", today, lambda: {"x": 1}, viewer={"id": 5})
    conn = get_conn(db_path)
    conn.execute("UPDATE home_snapshots SET built_at = built_at - ?", (home_snapshots.REFRESH_SECONDS + 5,))
    conn.commit()
    conn.close()
    with app.test_request_context("/"):
        home_snapshots.serve("monthly_review", rid, "all:s1", today, lambda: {"x": 2}, viewer={"id": 5})
        assert home_snapshots.state("monthly_review") == "kept" and home_snapshots.flags("monthly_review") == {}
    assert len(queued) == 1 and queued[0]["class"] == "bulk"


@pytest.mark.parametrize("write", ["closeout", "publish", "inventory", "reply"])
def test_the_owners_writes_move_the_stamp(db_path, write):
    rid = _rid(db_path)
    before = home_snapshots.data_stamp(rid)
    conn = get_conn(db_path)
    if write == "closeout":
        conn.execute("INSERT INTO close_outs (restaurant_id, business_date, went_well) VALUES (?,?,?)",
                     (rid, "2026-10-20", "x"))
    elif write == "publish":
        conn.execute("INSERT INTO schedule_history (restaurant_id, week_start) VALUES (?,?)", (rid, "2026-10-19"))
        conn.commit()
        mid = home_snapshots.data_stamp(rid)
        assert mid != before
        before = mid
        conn.execute("UPDATE schedule_history SET published_at=datetime('now') WHERE restaurant_id=?", (rid,))
    elif write == "inventory":
        conn.execute("INSERT INTO inventory_history (restaurant_id, week_end) VALUES (?,?)", (rid, "2026-10-18"))
    else:
        conn.execute("INSERT INTO reviews (restaurant_id, platform, external_id, author, rating, text, "
                     "response_status, fetched_at) VALUES (?,?,?,?,?,?,?,datetime('now'))",
                     (rid, "google", "e1", "A", 5, "Great", "drafted"))
        conn.commit()
        before = home_snapshots.data_stamp(rid)
        conn.execute("UPDATE reviews SET response_status='posted' WHERE restaurant_id=?", (rid,))
    conn.commit()
    conn.close()
    assert home_snapshots.data_stamp(rid) != before


# ── F9: one stamp, whatever object the route passes ─────────────────────────

def test_the_stamp_is_the_raw_rows_whatever_view_arrives_first(db_path):
    from ask_cavnar_tools import viewer_restaurant
    rid = _rid(db_path, module_labor=1, module_inventory=1)
    raw = models.get_restaurant(rid)
    view = viewer_restaurant(raw, {"id": 6, "role": "manager", "restaurant_id": rid})
    assert home_snapshots.data_stamp(rid, view) == home_snapshots.data_stamp(rid, raw)
    app = Flask(__name__)
    with app.test_request_context("/"):                 # the iPhone order: the view first
        a = home_snapshots.data_stamp(rid, view)
        b = home_snapshots.data_stamp(rid, raw)
    with app.test_request_context("/"):                 # the web order: the raw row first
        c = home_snapshots.data_stamp(rid, raw)
        d = home_snapshots.data_stamp(rid, view)
    assert a == b == c == d


# ── F12: a held lock is never forgotten ─────────────────────────────────────

def test_a_keys_lock_is_kept_while_held_whatever_else_is_locked():
    key = (1, "good_news", "d:-", None)
    held = home_snapshots._key_lock(key)
    with held:
        for i in range(6000):
            home_snapshots._key_lock((i + 10, "x", "y", None))
        assert home_snapshots._key_lock(key) is held
    del held
    import gc
    gc.collect()
    assert len(home_snapshots._key_locks) < 6000            # unheld locks are forgotten


# ── the precompute ──────────────────────────────────────────────────────────

def _user(db_path, uid, rid, role, active=1):
    auth.init_auth(db_path)
    conn = get_conn(db_path)
    conn.execute("INSERT INTO users (id, restaurant_id, username, email, password_hash, role, is_active) "
                 "VALUES (?,?,?,?,?,?,?)", (uid, rid, f"u{uid}", f"u{uid}@x.com", "x", role, active))
    conn.commit()
    conn.close()


def test_the_precompute_rebuilds_every_scope_opened_lately_in_its_own_view(db_path, monkeypatch):
    rid = _rid(db_path)
    _user(db_path, 5, rid, "owner")
    _user(db_path, 6, rid, "manager")
    _user(db_path, 7, rid, "manager", active=0)
    today = home_snapshots._business_today(rid)
    yesterday = (datetime.fromisoformat(today) - timedelta(days=1)).date().isoformat()
    long_ago = (datetime.fromisoformat(today) - timedelta(days=home_snapshots.ACTIVE_DAYS + 3)).date().isoformat()
    owner, mgr, gone = ({"id": 5, "role": "owner"}, {"id": 6, "role": "manager"}, {"id": 7, "role": "manager"})
    view_as = {"id": 5, "role": "owner", "acting_admin_id": 1, "device_type": "admin-view-as"}
    rows = [("morning_brief", home_snapshots.viewer_scope(owner), owner, yesterday),
            ("morning_brief", home_snapshots.viewer_scope(mgr), mgr, yesterday),
            ("morning_brief", home_snapshots.viewer_scope(gone), gone, yesterday),
            ("morning_brief", home_snapshots.viewer_scope(view_as), view_as, yesterday),
            ("good_news", "d:inventory:s0", mgr, yesterday),
            ("monthly_review", "all:s1", owner, yesterday),
            ("cross_module", home_snapshots.viewer_scope(owner), owner, long_ago)]
    for block, scope, viewer, opened in rows:
        home_snapshots._write(rid, block, scope, yesterday, "old", {"old": True}, 1, viewer=viewer, opened_on=opened)
    built = []
    import models as _m
    monkeypatch.setattr(home_snapshots, "build_block", lambda block, r, viewer, as_of, ctx=None: built.append(
        (block, viewer["id"], _m.viewer_sees_salaries())) or {"new": True})
    out = home_snapshots.precompute_restaurant(rid, today)
    assert sorted(built) == sorted([("morning_brief", 5, True), ("morning_brief", 6, False),
                                    ("good_news", 6, False), ("monthly_review", 5, True)])
    assert out["ok"] == 4 and out["failed"] == 0 and out["skipped"] == 2
    # The brief is built first: it is what the morning open leads with.
    assert built[0][0] == "morning_brief"


def test_run_precompute_queues_each_restaurant_once_in_its_morning_window(db_path, monkeypatch, queued):
    a = _rid(db_path, name="Chicago Co", timezone="America/Chicago")
    b = _rid(db_path, name="LA Co", timezone="America/Los_Angeles")
    c = _rid(db_path, name="Unused Co", timezone="America/Chicago")
    for rid in (a, b):
        home_snapshots._write(rid, "monthly_review", "all:s1", "2026-10-19", "s", {}, 1, opened_on="2026-10-19")
    seen = []
    monkeypatch.setattr(job_queue, "enqueue_many", lambda kind, items, **kw: seen.extend(items) or len(items))
    # 10:30 UTC = 5:30 Chicago (in the window), 3:30 Los Angeles (not yet).
    now = datetime(2026, 10, 20, 10, 30, tzinfo=timezone.utc)
    out = home_snapshots.run_precompute(now=now)
    assert [i["restaurant_id"] for i in seen] == [a] and c not in [i["restaurant_id"] for i in seen]
    assert seen[0]["idempotency_key"] == f"home_pre:{a}:2026-10-20" and seen[0]["payload"]["mode"] == "precompute"
    assert set(out) == {"attempted", "ok", "failed", "skipped", "hit_bound"} and out["ok"] == 1


def test_the_precompute_forgets_scopes_nobody_opened(db_path):
    rid = _rid(db_path)
    old = (datetime.now(timezone.utc) - timedelta(days=home_snapshots.KEEP_DAYS + 2)).date().isoformat()
    home_snapshots._write(rid, "good_news", "d:-:s0", old, "s", {}, 1, opened_on=old)
    home_snapshots._write(rid, "good_news", "d:-:s1", old, "s", {}, 1, opened_on=datetime.now().date().isoformat())
    assert home_snapshots.prune() == 1


def test_the_precompute_is_a_registered_scheduled_job():
    import jobs_registry
    entry = jobs_registry.JOBS["home_precompute"]
    assert entry["target"] == ("home_snapshots", "run_precompute") and entry["sends"] is False
    src = open(os.path.join(ROOT, "scheduler.py"), encoding="utf-8").read()
    assert '_ops.run_job("home_precompute", _home_snap.run_precompute)' in src


def test_a_read_notes_the_scope_was_opened_today(db_path, monkeypatch):
    monkeypatch.setenv("HOME_SNAPSHOT_COLD", "request")
    rid = _rid(db_path)
    app = Flask(__name__)
    with app.test_request_context("/"):
        home_snapshots.serve("good_news", rid, "d:-:s1", "2026-10-20", lambda: {"x": 1}, viewer={"id": 5})
    row = home_snapshots._read(rid, "good_news", "d:-:s1")
    assert row["opened_on"] == "2026-10-20" and json.loads(row["viewer_json"])["viewer"] == {"id": 5}


# ── F8: Home's combined read ────────────────────────────────────────────────

def test_home_follow_queues_what_is_missing_and_rereads_it_by_name(client, db_path, monkeypatch, queued):
    rid = _rid(db_path)
    _as(monkeypatch, rid)
    _no_builds(monkeypatch)
    body = client.get("/api/home/follow").get_json()
    assert sorted(body["missing"]) == ["brief", "cross", "month", "news"]
    assert body["retry_after"] == home_snapshots.RETRY_AFTER_SECONDS
    assert not set(body["failed"]) & {"brief", "cross", "month", "news"}
    assert sorted(j["payload"]["block"] for j in queued) == ["cross_module", "good_news", "monthly_review",
                                                            "morning_brief"]
    again = client.get("/api/home/follow?only=brief,news").get_json()
    assert set(again["blocks"]) | set(again["missing"]) == {"brief", "news"}


def test_home_follow_names_a_block_whose_read_failed(client, db_path, monkeypatch):
    """R6-F13: a block that raised is `failed` (drawn "Couldn't load"),
    never a null that reads as a quiet week; a refusal stays a plain null."""
    import strategy_routes
    rid = _rid(db_path)
    _as(monkeypatch, rid, role="manager")
    monkeypatch.setattr(strategy_routes, "_do_issues_list", lambda u: 1 / 0)
    body = client.get("/api/home/follow?only=issues,loss").get_json()
    assert body["failed"] == ["issues"] and body["blocks"]["issues"] is None
    assert body["blocks"]["loss"] is None and "loss" not in body["failed"]     # a 403, not a failure


def test_home_follow_in_the_rollback_mode_builds_what_the_client_names(client, db_path, monkeypatch):
    import good_news
    monkeypatch.setenv("HOME_SNAPSHOT_COLD", "request")
    rid = _rid(db_path)
    _as(monkeypatch, rid)
    monkeypatch.setattr(good_news, "all_good_news", lambda *a, **k: [])
    assert "news" in client.get("/api/home/follow").get_json()["missing"]
    body = client.get("/api/home/follow?only=news").get_json()
    assert body["missing"] == [] and body["blocks"]["news"]["ok"]


# ── F10: the group view ─────────────────────────────────────────────────────

def test_the_group_view_never_builds_a_location_on_the_request(db_path, monkeypatch, queued):
    import home_brief
    a = _rid(db_path, name="North", location_group="G", location_name="North")
    b = _rid(db_path, name="South", location_group="G", location_name="South")
    monkeypatch.setattr(home_brief, "_location_record", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("built on the request")))
    monkeypatch.setattr(home_brief, "_location_last_night", lambda *a, **k: None)
    monkeypatch.setattr(home_brief, "_CACHE", {})
    user = {"id": 5, "restaurant_id": a, "base_restaurant_id": a, "role": "owner", "is_admin": 0}
    app = Flask(__name__)
    with app.test_request_context("/api/home/brief/group"):
        payload, status = home_brief.build_group_brief(user)
    assert status == 200 and sorted(payload["preparing"]) == sorted([a, b])
    assert payload["retry_after"] == home_snapshots.RETRY_AFTER_SECONDS
    assert payload["headline"] != "All locations healthy" and payload["tone"] == "neutral"
    assert all(l["health"] == "preparing" for l in payload["locations"])
    assert {j["payload"]["block"] for j in queued} == {"location_record"}
    assert home_brief._CACHE == {}                     # a re-read reaches the rows once built


def test_a_location_record_builds_from_its_row_off_the_request(db_path):
    import home_brief
    rid = _rid(db_path, name="North", location_group="G", location_name="North")
    rec = home_brief.build_location_record(rid)
    assert rec["id"] == rid and rec["name"] == "North" and rec["health"] in ("healthy", "watch", "important",
                                                                           "critical")


# ── F11: the mobile Home ETag can match ─────────────────────────────────────

def test_the_mobile_home_tag_ignores_the_build_clock():
    import http_layer
    a = json.dumps({"ok": True, "generated_at": "2026-10-20T12:00:01Z", "x": [1]}).encode()
    b = json.dumps({"x": [1], "generated_at": "2026-10-20T12:01:07Z", "ok": True}).encode()
    u = {"id": 1, "restaurant_id": 2}
    assert http_layer.etag_for(u, http_layer._tagged_bytes("/mobile/api/home", a)) == \
        http_layer.etag_for(u, http_layer._tagged_bytes("/mobile/api/home", b))
    c = json.dumps({"ok": True, "generated_at": "x", "x": [2]}).encode()
    assert http_layer.etag_for(u, http_layer._tagged_bytes("/mobile/api/home", a)) != \
        http_layer.etag_for(u, http_layer._tagged_bytes("/mobile/api/home", c))
    # Every other tagged read is hashed as sent.
    assert http_layer._tagged_bytes("/mobile/api/labor", a) == a


def test_the_mobile_home_clock_is_to_the_hour():
    import mobile_api
    s = mobile_api._home_local_now(Restaurant(name="x", owner_email="x@x.com", timezone="America/Chicago"))
    assert s[13:19] == ":00:00" and "." not in s


# ── web: F7, F8, F13 ────────────────────────────────────────────────────────

def _dash():
    return open(os.path.join(ROOT, "templates", "dashboard.html"), encoding="utf-8").read()


def test_studio_rechecks_and_reviews_are_coalesced():
    src = _dash()
    recheck = src.split("function _schedRecheck() {", 1)[1].split("\n}\n", 1)[0]
    assert "if (_schedRecheckBusy" in recheck and "_schedRecheckAgain = true; return;" in recheck
    pr = src.split("window.prRefresh = function (cb) {", 1)[1].split("\n  };\n", 1)[0]
    assert "S.again = true;" in pr and "S.waiting.push(cb)" in pr and "settle(hid)" in pr


def test_a_failed_home_block_says_it_failed():
    src = _dash()
    assert "function hbFailedLine(what)" in src
    assert "if(l&&l.failed)return" in src.split("function renderLoss(l){", 1)[1][:400]
    assert "if(failed){box.innerHTML=hbFailedLine('open issues');" in src
    assert "else if(bst.failed)h+=hbFailedLine('today’s brief');" in src
    assert "Preparing today’s brief" in src


# ── F14: the dashboard page no longer computes the unused value figures ─────

def test_the_dashboard_page_does_not_compute_value_delivered():
    src = open(os.path.join(ROOT, "hosted_dashboard.py"), encoding="utf-8").read()
    assert "value_delivered import breakdown" not in src and "_user_row" not in src
    tpl = _dash()
    for key in ("total", "wins", "in_flight", "avoided_dollars", "avoided_hours", "reviews_avoided",
                "reply_rate", "opportunity"):
        assert f"savings_breakdown.{key}" not in tpl and f"savings_breakdown['{key}']" not in tpl, key
