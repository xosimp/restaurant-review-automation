"""The background scheduler under load, long passes, cancellation and hung
calls (DATA audit: DATA-3, DATA-7, DATA-8, DATA-16, DATA-31, DATA-49,
DATA-51, DATA-52, DATA-61).

What this protects: one serial scheduler thread runs every fetch, digest,
alert, auto-draft, delayed action and scheduled post. These tests drive real
`scheduler_loop` ticks (every job body stubbed, sleep replaced by a stop) and
the job functions themselves, and pin what the owner is owed: minute-level
duties are not starved by a three-hour pass, every opted-in restaurant gets
its Thursday draft or is told, a cancelled account stops being fetched and
its guests stop being texted, a due post is not stuck behind posts that are
not due, and no outbound call can hang the thread forever.

Tests without a marker pin behaviour that works today; xfail(strict=True)
tests assert the correct behaviour for a defect the audit confirmed.
"""
import ast
import importlib.util
import os
import sqlite3
import sys
import threading
import time
from datetime import datetime, timedelta, timezone

import pytest

import models
import ops
from models import Restaurant, create_restaurant

# Imported here, at collection, so each binds the real get_conn. A module
# first imported inside a test binds that test's redirect and keeps it for
# the rest of the session (CLAUDE.md "Bound imports").
import admin_ops, analyser, delayed, drafter, fetcher, guest_marketing, issues, marketing_publish  # noqa: E401,F401
import morning_brief, notify, push, schedule_engine, scheduler, status_manager, strategy_jobs, time_utils  # noqa: E401,F401

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_REAL_CLAIM = ops.claim_period


@pytest.fixture(autouse=True)
def _redirect(db_path, monkeypatch):
    real = models.get_conn
    redirect = lambda *a, **k: real(db_path)
    redirect._edge_redirect = True
    for mod in list(sys.modules.values()):
        g = getattr(mod, "get_conn", None) if mod is not None else None
        if g is real or getattr(g, "_edge_redirect", False):
            monkeypatch.setattr(mod, "get_conn", redirect)
    monkeypatch.setattr(models, "DB_PATH", db_path)
    # status_manager opens sqlite3.connect(DB_PATH) with its own bound copy of
    # the default path, which is the developer's ./reviews.db.
    monkeypatch.setattr(status_manager, "DB_PATH", db_path)
    ops._claim_fallback.clear()
    guest_marketing.init_guest_marketing(db_path)


def _rid(db_path, **kw):
    kw.setdefault("name", "Sched Co")
    kw.setdefault("owner_email", "o@x.test")
    kw.setdefault("timezone", "America/Chicago")
    return create_restaurant(Restaurant(**kw), db_path=db_path)


def _sql(db_path, sql, args=()):
    conn = models.get_conn(db_path)
    cur = conn.execute(sql, args)
    conn.commit()
    rows = [dict(r) for r in cur.fetchall()] if cur.description else []
    conn.close()
    return rows


# ── driving one real tick ──────────────────────────────────────────────────

class _StopLoop(Exception):
    pass


def _prepare_tick(monkeypatch, now, claims, **duties):
    """Patch scheduler_loop so one call runs exactly one tick at `now` with
    `claims(job, period)` deciding which gated jobs run. Every-tick duties
    default to no-ops; pass a replacement to observe one."""
    import scheduler, marketing_publish, notify, issues, morning_brief, delayed
    monkeypatch.setattr(scheduler, "_chi_now", lambda: now)
    monkeypatch.setattr(scheduler._ops, "claim_period", claims)
    monkeypatch.setattr(notify, "release_due_alerts", duties.get("release_due_alerts", lambda *a, **k: {}))
    monkeypatch.setattr(issues, "tick", duties.get("issues_tick", lambda *a, **k: None))
    monkeypatch.setattr(morning_brief, "run_due", duties.get("morning_brief", lambda *a, **k: None))
    monkeypatch.setattr(delayed, "run_due", duties.get("delayed", lambda *a, **k: {}))
    monkeypatch.setattr(marketing_publish, "run_due_posts", duties.get("posts", lambda **k: {}))
    if "heartbeat" not in duties:
        monkeypatch.setattr(scheduler, "record_scheduler_heartbeat", lambda *a, **k: None)
    monkeypatch.setattr(scheduler, "run_health_checks", lambda *a, **k: None)

    def _stop(_s):
        raise _StopLoop()
    monkeypatch.setattr(scheduler.time, "sleep", _stop)


def _tick():
    import scheduler
    try:
        scheduler.scheduler_loop()
    except _StopLoop:
        pass


def _only(*prefixes):
    """Real, durable claims for the named jobs; every other job not due."""
    def claims(job, period):
        if any(job.startswith(p) for p in prefixes):
            return _REAL_CLAIM(job, period)
        return False
    return claims


TUESDAY_9AM = datetime(2026, 9, 22, 9, 0)


# ── minute duties behind a long pass (DATA-3) ──────────────────────────────

def test_minute_duties_run_after_a_short_pass(db_path, monkeypatch):
    import scheduler
    ran = []
    monkeypatch.setattr(scheduler, "run_daily_fetch", lambda: None)
    _prepare_tick(monkeypatch, TUESDAY_9AM, lambda job, period: job == "review_fetch",
                  delayed=lambda *a, **k: ran.append("delayed") or {})
    _tick()
    assert ran == ["delayed"]


@pytest.mark.xfail(strict=True, reason="DATA-3: the scheduler is one serial thread and delayed.run_due only runs "
                                       "after the review fetch returns, so undo-window sends wait out a 3-hour pass")
def test_minute_duties_run_while_a_long_pass_is_in_progress(db_path, monkeypatch):
    import scheduler
    fetch_started, release, delayed_ran = threading.Event(), threading.Event(), threading.Event()

    def long_fetch():
        fetch_started.set()
        release.wait(3)
    monkeypatch.setattr(scheduler, "run_daily_fetch", long_fetch)
    _prepare_tick(monkeypatch, TUESDAY_9AM, lambda job, period: job == "review_fetch",
                  delayed=lambda *a, **k: delayed_ran.set() or {})
    loop = threading.Thread(target=_tick, daemon=True)
    loop.start()
    try:
        assert fetch_started.wait(2), "the fetch never started"
        during = delayed_ran.wait(1.0)          # stands in for "within one tick interval"
    finally:
        release.set()
        loop.join(5)
    assert during, "delayed actions did not run while the fetch pass was in progress"


@pytest.mark.xfail(strict=True, reason="DATA-3: the heartbeat is stamped only at the end of a tick, so every long "
                                       "pass marks the scheduler as an outage on the public status page")
def test_the_status_page_does_not_report_an_outage_during_a_long_pass(db_path, monkeypatch):
    import scheduler, status_manager
    status_manager.seed_default_services()
    status_manager.update_service_status("scheduler", "operational", None)
    _sql(db_path, "UPDATE service_status SET updated_at=datetime('now','-25 minutes') WHERE service_key='scheduler'")
    seen = {}

    def long_fetch():                               # 25 minutes into a pass that started on time
        seen["age"] = status_manager.check_scheduler_liveness()
        seen["status"] = _sql(db_path, "SELECT status FROM service_status WHERE service_key='scheduler'")[0]["status"]
    monkeypatch.setattr(scheduler, "run_daily_fetch", long_fetch)
    _prepare_tick(monkeypatch, TUESDAY_9AM, lambda job, period: job == "review_fetch", heartbeat=True)
    _tick()
    assert seen["status"] != "outage", f"liveness during a normal pass: {seen}"


# ── the Thursday auto-draft (DATA-8) ───────────────────────────────────────

def test_auto_draft_reaches_every_opted_in_restaurant_by_thursday_evening(db_path, monkeypatch):
    import schedule_engine, push, notify
    rids = [_rid(db_path, name=f"Opted {i}") for i in range(3)]
    for rid in rids:
        models.update_restaurant(rid, {"auto_draft_schedule": 1, "module_labor": 1}, db_path=db_path)
    clock = [1000.0]
    monkeypatch.setattr(time, "monotonic", lambda: clock[0])

    def draft(job_id, rid, *a, **k):                # a 70-person roster takes 25 minutes
        clock[0] += 25 * 60
        models.save_schedule_history(rid, "2026-09-28", "2026-10-04", 1.0, 1.0, 1.0,
                                     "date,day,employee,role,shift_start,shift_end,scheduled_hours,notes\n", [],
                                     db_path=db_path)
        ops.finish_async_job(job_id, "done", {"ok": True})
    monkeypatch.setattr(schedule_engine, "_run_schedule_job", draft)
    pushes = []
    monkeypatch.setattr(push, "fire_push", lambda rid, kind, *a, **k: pushes.append((rid, kind)))
    monkeypatch.setattr(notify, "briefing_allowed", lambda *a, **k: True)
    for hour in range(6, 21):                        # every tick from 6am to 8pm, Thursday 9/24/26
        _prepare_tick(monkeypatch, datetime(2026, 9, 24, hour, 0), _only("auto_draft"))
        _tick()

    drafted = {r["restaurant_id"] for r in _sql(db_path, "SELECT DISTINCT restaurant_id FROM schedule_history")}
    told = {rid for rid, kind in pushes if kind != "schedule_drafted"}
    missed = set(rids) - drafted - told
    assert not missed, f"restaurants with no draft and no word by Thursday evening: {sorted(missed)}"


# ── cancelled and paused accounts (DATA-51, DATA-61) ───────────────────────

def _live(db_path, **kw):
    kw.setdefault("reviews_live", 1)
    kw.setdefault("google_place_id", "ChIJ-edge-place")
    return _rid(db_path, **kw)


def _mark(db_path, rid, state):
    if state == "deletion_requested":
        models.request_account_deletion(rid, db_path=db_path)
    else:
        models.update_restaurant(rid, {"billing_status": state}, db_path=db_path)


def test_strategy_jobs_already_skip_churned_and_paused_accounts(db_path):
    import strategy_jobs
    active = _rid(db_path, name="Active")
    churned, paused = _rid(db_path, name="Churned"), _rid(db_path, name="Paused")
    _mark(db_path, churned, "churned")
    _mark(db_path, paused, "paused")
    assert [r.id for r in strategy_jobs._restaurants(db_path)] == [active]


def test_an_active_restaurant_is_fetched(db_path, monkeypatch):
    import fetcher, scheduler
    rid = _live(db_path, billing_status="active")
    fetched = []
    monkeypatch.setattr(fetcher, "fetch_google", lambda place_id, r: fetched.append(r) or [])
    scheduler.run_daily_fetch()
    assert fetched == [rid]


@pytest.mark.parametrize("state", ["churned", "paused", "deletion_requested"])
def test_a_cancelled_restaurant_is_not_fetched(db_path, monkeypatch, state):
    import fetcher, scheduler
    rid = _live(db_path)
    _mark(db_path, rid, state)
    fetched = []
    monkeypatch.setattr(fetcher, "fetch_google", lambda place_id, r: fetched.append(r) or [])
    scheduler.run_daily_fetch()
    assert fetched == []


@pytest.mark.xfail(strict=True, reason="DATA-51: get_restaurants_for_digest filters only digest_day/enabled/"
                                       "module_reviews, so a cancelled owner keeps getting weekly digests")
@pytest.mark.parametrize("state", ["churned", "paused"])
def test_a_cancelled_restaurant_gets_no_weekly_digest(db_path, state):
    from auth import create_user, init_auth
    init_auth(db_path=db_path)
    rid = _rid(db_path, digest_day="monday", digest_enabled=1, module_reviews=1)
    create_user(rid, "own", "own@x.test", "a-Long-unique-pass-9481!", db_path=db_path)
    assert [r["id"] for r in models.get_restaurants_for_digest("monday", db_path=db_path)] == [rid]
    _mark(db_path, rid, state)
    assert models.get_restaurants_for_digest("monday", db_path=db_path) == []


@pytest.mark.xfail(strict=True, reason="DATA-51: delayed.run_due executes pending auto-publish / supplier-order "
                                       "actions for an account cancelled inside the undo window")
def test_a_cancelled_restaurant_s_queued_action_does_not_run(db_path, monkeypatch):
    import delayed
    rid = _rid(db_path)
    ran = []
    monkeypatch.setitem(delayed.HANDLERS, "order_send", lambda r, p, d: ran.append(r) or {"ok": True})
    delayed.schedule(rid, "order_send", {"supplier_email": "s@x.test"}, 10, db_path=db_path)
    _mark(db_path, rid, "churned")
    delayed.run_due(db_path=db_path, now=datetime.now(timezone.utc) + timedelta(hours=1))
    assert ran == [], "a supplier order went out for an account that had cancelled"


def _guest(db_path, rid, phone, visited_days_ago=2):
    import time_utils
    local = time_utils.restaurant_now_by_id(rid, naive=True) - timedelta(days=visited_days_ago)
    _sql(db_path, "INSERT INTO guest_contacts (restaurant_id, name, phone, consent, last_visit) VALUES (?,?,?,1,?)",
         (rid, "Guest " + phone[-2:], phone, local.isoformat()))


def _followup_world(db_path, monkeypatch, n_guests=1, **kw):
    import guest_marketing
    rid = _rid(db_path, module_marketing=1, google_place_id="ChIJ-edge-place", **kw)
    for i in range(n_guests):
        _guest(db_path, rid, f"+1512555010{i}")
    monkeypatch.setattr(guest_marketing, "guest_sms_allowed_now", lambda r: True)
    return rid


def test_an_active_restaurant_s_guests_get_their_follow_up(db_path, monkeypatch):
    import guest_marketing
    _followup_world(db_path, monkeypatch)
    texted = []
    monkeypatch.setattr(guest_marketing, "send_sms", lambda phone, msg, **kw: texted.append(phone) or True)
    guest_marketing.run_review_request_followups(db_path=db_path)
    assert texted == ["+15125550100"]


def test_a_cancelled_restaurant_s_guests_are_not_texted(db_path, monkeypatch):
    import guest_marketing
    rid = _followup_world(db_path, monkeypatch)
    _mark(db_path, rid, "churned")
    texted = []
    monkeypatch.setattr(guest_marketing, "send_sms", lambda phone, msg, **kw: texted.append(phone) or True)
    guest_marketing.run_review_request_followups(db_path=db_path)
    assert texted == []


# ── the follow-up job's transaction (DATA-52) ──────────────────────────────

def test_followups_commit_each_claim_before_sending(db_path, monkeypatch):
    import guest_marketing
    _followup_world(db_path, monkeypatch, n_guests=3)
    blocked = []

    def send(phone, msg, **kw):
        other = sqlite3.connect(db_path, timeout=0.05)
        try:
            other.execute("BEGIN IMMEDIATE")      # what a request's last_active write needs
            other.rollback()
        except sqlite3.OperationalError:
            blocked.append(phone)
        finally:
            other.close()
        return True
    monkeypatch.setattr(guest_marketing, "send_sms", send)
    guest_marketing.run_review_request_followups(db_path=db_path)
    assert blocked == [], f"the write lock was held across the SMS send for {blocked}"


class _Killed(BaseException):
    """A deploy's SIGTERM: not an Exception, so nothing in the loop catches it."""


def test_a_follow_up_run_killed_mid_loop_does_not_re_text_guests_already_reached(db_path, monkeypatch):
    import guest_marketing
    _followup_world(db_path, monkeypatch, n_guests=2)
    texted = []

    def dies_on_second(phone, msg, **kw):
        if texted:
            raise _Killed()
        texted.append(phone)
        return True
    monkeypatch.setattr(guest_marketing, "send_sms", dies_on_second)
    with pytest.raises(_Killed):
        guest_marketing.run_review_request_followups(db_path=db_path)
    models.close_thread_connections()               # the dead process's connection goes with it
    first = list(texted)

    again = []
    monkeypatch.setattr(guest_marketing, "send_sms", lambda phone, msg, **kw: again.append(phone) or True)
    guest_marketing.run_review_request_followups(db_path=db_path)
    assert not (set(again) & set(first)), f"{first} were texted again after the restart"


# ── fetch coverage visible to the operator (DATA-7) ────────────────────────

def _admin_issue_keys(rid):
    import admin_ops
    return {i["key"] for i in admin_ops.issues()["issues"] if i["restaurant_id"] == rid}


@pytest.mark.xfail(strict=True, reason="DATA-7: nothing reports a restaurant whose last review fetch is older than "
                                       "the 4-hour cadence; the admin view keys staleness on 3 days of review data")
def test_fetch_coverage_age_is_reported_per_restaurant(db_path):
    from models import Review, save_reviews
    rid = _live(db_path, contract_status="signed", billing_status="active", module_reviews=1)
    save_reviews([Review(restaurant_id=rid, platform="google", external_id="cov-1", author="a", rating=5,
                         text="Great")], db_path=db_path)
    _sql(db_path, "UPDATE reviews SET review_date=date('now') WHERE restaurant_id=?", (rid,))
    _sql(db_path, "UPDATE restaurants SET last_fetched_at=datetime('now','-1 hours') WHERE id=?", (rid,))
    fresh = _admin_issue_keys(rid)
    _sql(db_path, "UPDATE restaurants SET last_fetched_at=datetime('now','-10 hours') WHERE id=?", (rid,))
    behind = _admin_issue_keys(rid)
    assert behind - fresh, "a restaurant two fetch slots behind raises nothing in the operator view"


# ── scheduled posts across time zones (DATA-31) ────────────────────────────

@pytest.mark.xfail(strict=True, reason="DATA-31: run_due_posts takes the 200 earliest rows by local-time string, so "
                                       "a due Eastern post sits behind 200 not-yet-due Pacific posts")
def test_a_due_post_is_picked_even_behind_200_undue_ones(db_path, monkeypatch):
    import marketing_publish
    pacific = _rid(db_path, name="Pacific", timezone="America/Los_Angeles")
    eastern = _rid(db_path, name="Eastern", timezone="America/New_York")
    conn = models.get_conn(db_path)
    conn.executemany("INSERT INTO marketing_scheduled_posts (restaurant_id, platform, body, scheduled_for) "
                     "VALUES (?, 'facebook', ?, '2026-09-22T08:00:00')", [(pacific, f"p{i}") for i in range(200)])
    cur = conn.execute("INSERT INTO marketing_scheduled_posts (restaurant_id, platform, body, scheduled_for) "
                       "VALUES (?, 'facebook', 'due now', '2026-09-22T08:30:00')", (eastern,))
    due_id = cur.lastrowid
    conn.commit()
    conn.close()
    # 13:00 UTC: 06:00 in Los Angeles (its posts are 2 h away), 09:00 in New York (its post is 30 min late).
    local = {pacific: datetime(2026, 9, 22, 6, 0), eastern: datetime(2026, 9, 22, 9, 0)}
    monkeypatch.setattr(marketing_publish, "_local_now", lambda rid, *a, **k: local[rid])
    published = []
    monkeypatch.setattr(marketing_publish, "publish_now",
                        lambda rid, platform, body, **k: published.append(k.get("scheduled_post_id")) or {"ok": True})
    marketing_publish.run_due_posts(db_path=db_path)
    assert published == [due_id]


# ── outbound calls that cannot hang (DATA-16, DATA-49) ─────────────────────

def _alias_scan():
    """Every requests/httpx call, whatever name the module was imported as,
    that passes no timeout= — probe p12 as a test."""
    spec = importlib.util.spec_from_file_location("check_timeouts", os.path.join(ROOT, "scripts", "check_timeouts.py"))
    lint = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(lint)
    hits = []
    for root, dirs, files in os.walk(ROOT):
        dirs[:] = [d for d in dirs if d not in lint.SKIP_DIRS]
        for name in files:
            if not name.endswith(".py"):
                continue
            path = os.path.join(root, name)
            try:
                tree = ast.parse(open(path, encoding="utf-8").read())
            except (SyntaxError, UnicodeDecodeError):
                continue
            aliases = set()
            for n in ast.walk(tree):
                if isinstance(n, ast.Import):
                    for a in n.names:
                        if a.name in ("requests", "httpx"):
                            aliases.add(a.asname or a.name)
            for n in ast.walk(tree):
                if (isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                        and isinstance(n.func.value, ast.Name) and n.func.value.id in aliases
                        and n.func.attr in lint.VERBS
                        and not any(k.arg == "timeout" for k in n.keywords)):
                    hits.append(f"{os.path.relpath(path, ROOT)}:{n.lineno} {n.func.value.id}.{n.func.attr}")
    return hits


def test_every_outbound_http_call_names_a_timeout_whatever_the_import_alias():
    hits = _alias_scan()
    assert not hits, "outbound calls with no timeout:\n" + "\n".join(hits)


def _lint_on(tmp_path, monkeypatch, source):
    spec = importlib.util.spec_from_file_location("check_timeouts_t", os.path.join(ROOT, "scripts", "check_timeouts.py"))
    lint = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(lint)
    (tmp_path / "sample.py").write_text(source)
    monkeypatch.setattr(lint, "ROOT", str(tmp_path))
    return lint.offenders()


def test_the_timeout_lint_flags_a_plain_requests_call(tmp_path, monkeypatch):
    assert len(_lint_on(tmp_path, monkeypatch, "import requests\nrequests.get('https://x.test')\n")) == 1


def test_the_timeout_lint_flags_an_aliased_requests_call(tmp_path, monkeypatch):
    assert len(_lint_on(tmp_path, monkeypatch, "import requests as _req\n_req.get('https://x.test')\n")) == 1


class _Resp:
    def __init__(self, code, token="new-token"):
        self.status_code, self._token, self.text = code, token, "x"

    def json(self):
        return {"access_token": self._token}


def _expiring(db_path, name, token):
    rid = _rid(db_path, name=name)
    models.update_restaurant(rid, {"ig_token": token, "ig_token_expires": "2026-09-25"}, db_path=db_path)
    return rid


def test_token_refresh_continues_after_one_restaurant_fails(db_path, monkeypatch):
    import requests, scheduler
    monkeypatch.setenv("META_APP_ID", "app")
    monkeypatch.setenv("META_APP_SECRET", "secret")
    monkeypatch.setattr(scheduler, "_chi_now", lambda: datetime(2026, 9, 22, 7, 0))
    bad, good = _expiring(db_path, "Bad", "tok-bad"), _expiring(db_path, "Good", "tok-good")

    def graph(url, params=None, **k):
        if params["fb_exchange_token"] == "tok-bad":
            raise requests.ConnectionError("reset by peer")
        return _Resp(200, "tok-good-2")
    monkeypatch.setattr(requests, "get", graph)
    scheduler.refresh_expiring_tokens()
    assert models.get_restaurant(good, db_path=db_path).ig_token == "tok-good-2"
    assert models.get_restaurant(bad, db_path=db_path).ig_token == "tok-bad"


def test_every_token_refresh_call_names_a_timeout(db_path, monkeypatch):
    import requests, scheduler
    monkeypatch.setenv("META_APP_ID", "app")
    monkeypatch.setenv("META_APP_SECRET", "secret")
    monkeypatch.setattr(scheduler, "_chi_now", lambda: datetime(2026, 9, 22, 7, 0))
    _expiring(db_path, "One", "tok-1")
    calls = []
    monkeypatch.setattr(requests, "get", lambda url, **k: calls.append(k) or _Resp(200))
    scheduler.refresh_expiring_tokens()
    assert calls and all(k.get("timeout") for k in calls), calls


@pytest.mark.xfail(strict=True, reason="DATA-49: the refresh is claimed once per day at the job level, so a "
                                       "restaurant whose refresh failed waits a full day for another attempt")
def test_a_failed_token_refresh_is_retried_the_same_day(db_path, monkeypatch):
    import requests, scheduler
    monkeypatch.setenv("META_APP_ID", "app")
    monkeypatch.setenv("META_APP_SECRET", "secret")
    rid = _expiring(db_path, "Flaky", "tok-flaky")
    attempts = []

    def graph(url, params=None, **k):
        attempts.append(params["fb_exchange_token"])
        if len(attempts) == 1:
            raise requests.Timeout("Graph did not answer")
        return _Resp(200, "tok-fresh")
    monkeypatch.setattr(requests, "get", graph)
    for hour in (7, 8, 9, 10):
        _prepare_tick(monkeypatch, datetime(2026, 9, 22, hour, 0), _only("refresh_tokens"))
        monkeypatch.setattr(scheduler, "_chi_now", lambda h=hour: datetime(2026, 9, 22, h, 0))
        _tick()
    assert models.get_restaurant(rid, db_path=db_path).ig_token == "tok-fresh", f"attempts: {attempts}"
