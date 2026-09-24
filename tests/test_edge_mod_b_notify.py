"""Edge cases for Notifications & Alerts: the daily ceiling, rush holds,
quiet hours, the morning batch, push fan-out and the unread badge.

Every test comes from the MOD edge-case audit (appendix A8, findings
MOD-NOT-1..16 and the alert half of MOD-REV-6). They protect three promises
the alert system makes: an owner never gets more than the ceiling on a bad
day, an owner who asked for an alert gets it on the channel they kept on,
and nobody gets an alert they should not see.

Nothing is sent: SMS, email, push and outbound webhooks are recorded by
stubs. xfail(strict=True) tests assert the CORRECT behaviour for a defect the
audit confirmed and flip to a failure the day it is fixed.
"""
import datetime as _dtmod
import sys
from datetime import date, datetime, timedelta, timezone

import pytest
from flask import Flask

# Imported before any patch of models.get_conn (see tests/test_alert_holds.py).
import auth
import client_api
import mobile_api
import models
import notify
import ops
import push
import webhooks
from auth import create_user, init_auth, revoke_team_member, set_user_role
from models import (Restaurant, Review, create_restaurant, get_conn, save_labor_snapshot,
                    save_reviews, update_restaurant)


@pytest.fixture(autouse=True)
def _world(monkeypatch, db_path):
    real = models.get_conn
    redirect = lambda *a, **k: real(db_path)
    for mod in list(sys.modules.values()):
        try:
            if getattr(mod, "get_conn", None) is real:
                monkeypatch.setattr(mod, "get_conn", redirect)
        except Exception:
            pass
    for mod in (models, notify, auth, push, client_api, mobile_api, webhooks, ops):
        monkeypatch.setattr(mod, "get_conn", redirect, raising=False)
        monkeypatch.setattr(mod, "DB_PATH", db_path, raising=False)
    init_auth(db_path=db_path)
    push.init_push(db_path=db_path)
    webhooks.init_webhooks(db_path)


@pytest.fixture
def sent(monkeypatch):
    """Every channel, recorded instead of sent."""
    out = {"sms": [], "email": [], "push": []}
    monkeypatch.setattr(notify, "send_sms", lambda to, msg, use_case="alert": out["sms"].append((to, msg)) or True)
    monkeypatch.setattr(notify, "_email_alert",
                        lambda rid, to, subject, html, alert_type, db_path=None, **k: out["email"].append((rid, alert_type)))
    monkeypatch.setattr(push, "fire_push",
                        lambda rid, at, title, body, data=None, db_path=None, user_ids=None, on_delivered=None:
                        out["push"].append((rid, at)))
    monkeypatch.setattr(webhooks, "fire_webhook", lambda *a, **k: None)
    return out


def _rid(db_path, **kw):
    kw.setdefault("name", "Alert Co")
    kw.setdefault("owner_email", "o@x.test")
    kw.setdefault("timezone", "America/Chicago")
    return create_restaurant(Restaurant(**kw), db_path=db_path)


def _alert_log(db_path, rid, alert_type=None):
    conn = get_conn(db_path)
    sql, args = "SELECT alert_type FROM alert_log WHERE restaurant_id=?", [rid]
    if alert_type:
        sql += " AND alert_type=?"
        args.append(alert_type)
    rows = [r["alert_type"] for r in conn.execute(sql, args)]
    conn.close()
    return rows


def _one_star_reviews(rid, n, prefix="e", review_date=None):
    return [Review(restaurant_id=rid, platform="google", external_id=f"{prefix}{i}", author="A B",
                   rating=1, text="bad", review_date=review_date) for i in range(n)]


# ── The ceiling and holds ───────────────────────────────────────────────────

def test_alerts_held_through_a_rush_still_respect_the_daily_ceiling(db_path, sent, monkeypatch):
    """A8 #8 / MOD-NOT-1 — 60 one-star reviews land mid-lunch; draining the
    holds must deliver at most ALERT_HARD_CEILING_PER_DAY."""
    rid = _rid(db_path)
    update_restaurant(rid, {"urgent_via_email": 1, "alert_1star": 1}, db_path=db_path)
    _n, saved = save_reviews(_one_star_reviews(rid, 60), db_path=db_path)
    release = datetime.now(timezone.utc) + timedelta(minutes=1)
    monkeypatch.setattr(notify, "rush_release_at", lambda *a, **k: release)
    notify.fire_review_alerts(rid, "Alert Co", saved, db_path=db_path)
    t = release + timedelta(minutes=1)
    for _ in range(40):
        r = notify.release_due_alerts(db_path=db_path, now_utc=t)
        if not r["released"] and not r["dropped_stale"]:
            break
        t += timedelta(minutes=5)
    assert len(_alert_log(db_path, rid, "1star")) <= notify.ALERT_HARD_CEILING_PER_DAY


def test_one_restaurants_hold_backlog_does_not_starve_another(db_path, sent, monkeypatch):
    """A8 #18 / MOD-NOT-2 — 300 due holds for A and one for B: the first
    release pass delivers B's."""
    delivered = []
    monkeypatch.setattr(notify, "deliver_alert", lambda rid, *a, **k: delivered.append(rid))
    a, b = _rid(db_path, name="A"), _rid(db_path, name="B")
    now = datetime.now(timezone.utc)
    for i in range(300):
        notify.hold_alert(a, "1star", "s", "subj", "<p>x</p>", now, review_id=10000 + i, db_path=db_path)
    notify.hold_alert(b, "health", "s", "B", "<p>x</p>", now, review_id=1, db_path=db_path)
    notify.release_due_alerts(db_path=db_path, now_utc=now + timedelta(minutes=1))
    assert b in delivered


# ── Quiet hours ─────────────────────────────────────────────────────────────

def _freeze_clock(monkeypatch, utc_dt):
    """datetime.now() everywhere reads `utc_dt` (converted to the tz asked)."""
    real = _dtmod.datetime

    class _Frozen(real):
        @classmethod
        def now(cls, tz=None):
            return utc_dt.astimezone(tz) if tz else utc_dt.replace(tzinfo=None)
    monkeypatch.setattr(_dtmod, "datetime", _Frozen)


def _chicago(y, mo, d, h, mi):
    from zoneinfo import ZoneInfo
    return datetime(y, mo, d, h, mi, tzinfo=ZoneInfo("America/Chicago")).astimezone(timezone.utc)


def _la(y, mo, d, h, mi):
    from zoneinfo import ZoneInfo
    return datetime(y, mo, d, h, mi, tzinfo=ZoneInfo("America/Los_Angeles")).astimezone(timezone.utc)


@pytest.mark.parametrize("hh,mm,quiet", [(23, 30, True), (2, 0, True), (6, 59, True),
                                          (7, 0, False), (7, 30, False), (21, 59, False), (22, 0, True)])
def test_a_quiet_window_that_crosses_midnight(db_path, monkeypatch, hh, mm, quiet):
    """A8 #19 — 22:00-07:00 for a Chicago restaurant (the timezone the code
    uses today, so this pins only the wrap-around arithmetic)."""
    rid = _rid(db_path)
    update_restaurant(rid, {"alert_quiet_start": "22:00", "alert_quiet_end": "07:00"}, db_path=db_path)
    _freeze_clock(monkeypatch, _chicago(2026, 9, 21, hh, mm))
    assert models.is_in_quiet_hours(rid, db_path) is quiet


def test_quiet_hours_are_read_on_the_restaurants_own_clock(db_path, monkeypatch):
    """A8 #20 / MOD-NOT-4 — 06:00 in Los Angeles (08:00 Chicago) is inside an
    LA owner's 22:00-07:00 window; 07:30 LA is not."""
    rid = _rid(db_path, name="LA Co", timezone="America/Los_Angeles")
    update_restaurant(rid, {"alert_quiet_start": "22:00", "alert_quiet_end": "07:00"}, db_path=db_path)
    _freeze_clock(monkeypatch, _la(2026, 9, 21, 6, 0))
    assert models.is_in_quiet_hours(rid, db_path) is True
    _freeze_clock(monkeypatch, _la(2026, 9, 21, 7, 30))
    assert models.is_in_quiet_hours(rid, db_path) is False


@pytest.fixture
def app():
    flask_app = Flask(__name__, template_folder="../templates")
    flask_app.register_blueprint(client_api.client_bp)
    flask_app.register_blueprint(mobile_api.mobile_bp)
    return flask_app


def _owner_token(rid, username="owner"):
    uid = create_user(rid, username, f"{username}@x.test", "correct-horse-1")
    return uid, auth.create_session(uid)


@pytest.mark.parametrize("surface", ["web", "mobile"])
def test_a_quiet_hours_value_that_is_not_hh_mm_is_refused_not_stored(db_path, app, surface):
    """A8 #21 / MOD-NOT-5 — refused (or normalised to 21:00); never stored as
    '9pm' where is_in_quiet_hours reads it as 'no quiet hours'."""
    rid = _rid(db_path)
    _uid, token = _owner_token(rid)
    body = {"alert_quiet_start": "9pm", "alert_quiet_end": "7am", "urgent_via_email": 1}
    if surface == "web":
        c = app.test_client()
        c.set_cookie("session_token", token)
        resp = c.post("/api/alert-settings", json=body)
    else:
        resp = app.test_client().post("/mobile/api/account/alert-settings", json=body,
                                      headers={"Authorization": f"Bearer {token}"})
    stored = models.get_restaurant(rid, db_path).alert_quiet_start
    assert resp.status_code == 400 or stored == "21:00", (resp.status_code, stored)


def test_a_non_numeric_daily_cap_is_a_field_error_not_a_server_error(db_path, app):
    """A8 #21 / MOD-NOT-5."""
    rid = _rid(db_path)
    _uid, token = _owner_token(rid)
    c = app.test_client()
    c.set_cookie("session_token", token)
    resp = c.post("/api/alert-settings", json={"alert_max_per_day": "abc", "urgent_via_email": 1})
    assert resp.status_code == 400


def test_an_unknown_digest_day_is_refused_by_the_alert_settings_save(db_path, app):
    """A8 #21 / MOD-NOT-5 — an invalid day means the digest never matches
    and silently stops."""
    rid = _rid(db_path)
    _uid, token = _owner_token(rid)
    c = app.test_client()
    c.set_cookie("session_token", token)
    c.post("/api/alert-settings", json={"digest_day": "funday", "urgent_via_email": 1})
    assert models.get_restaurant(rid, db_path).digest_day in ("monday", "tuesday", "wednesday", "thursday",
                                                              "friday", "saturday", "sunday")


def _health_review(rid, ext="h1"):
    return Review(restaurant_id=rid, platform="google", external_id=ext, author="Sam K",
                  rating=1, text="I got food poisoning", urgency="high", processed=True)


def test_a_health_alert_goes_through_quiet_hours_when_the_owner_opted_in(db_path, sent, monkeypatch):
    """A8 #22 — the delivery half, not just the settings round-trip."""
    rid = _rid(db_path)
    update_restaurant(rid, {"urgent_via_email": 1, "alert_health": 1, "alert_health_bypass_quiet": 1,
                            "alert_quiet_start": "00:00", "alert_quiet_end": "23:59"}, db_path=db_path)
    monkeypatch.setattr(models, "is_in_quiet_hours", lambda *a, **k: True)
    monkeypatch.setattr(notify, "rush_release_at", lambda *a, **k: None)
    _n, saved = save_reviews([_health_review(rid)], db_path=db_path)
    notify.fire_review_alerts(rid, "Alert Co", saved, db_path=db_path)
    assert _alert_log(db_path, rid) == ["health"]
    assert sent["email"] == [(rid, "health")]


def test_a_health_alert_waits_out_quiet_hours_when_the_owner_did_not_opt_in(db_path, sent, monkeypatch):
    """A8 #22 — and the default stays quiet."""
    rid = _rid(db_path)
    update_restaurant(rid, {"urgent_via_email": 1, "alert_health": 1, "alert_health_bypass_quiet": 0},
                      db_path=db_path)
    monkeypatch.setattr(models, "is_in_quiet_hours", lambda *a, **k: True)
    monkeypatch.setattr(notify, "rush_release_at", lambda *a, **k: None)
    _n, saved = save_reviews([_health_review(rid)], db_path=db_path)
    notify.fire_review_alerts(rid, "Alert Co", saved, db_path=db_path)
    assert _alert_log(db_path, rid) == [] and sent["email"] == []


# ── The daily checks ────────────────────────────────────────────────────────

def _labor_over(db_path, rid):
    end = date.today() - timedelta(days=2)
    start = end - timedelta(days=6)
    save_labor_snapshot(rid, start.isoformat(), end.isoformat(), 38.0, 3800, 10000, db_path=db_path)


def test_a_combined_morning_batch_is_delivered_with_the_unresponded_toggles_off(db_path, sent, monkeypatch):
    """A8 #24 / MOD-NOT-3 — labor over AND rating under the floor, with the
    'no reply after 48h' reminders switched off: one push still goes out.
    No deliver_alert monkeypatch — that is what hid this."""
    monkeypatch.setattr(notify, "rush_release_at", lambda *a, **k: None)
    rid = _rid(db_path)
    update_restaurant(rid, {"urgent_via_sms": 0, "urgent_via_email": 1, "alert_labor_over": 1,
                            "labor_target_pct": 30.0, "alert_rating_threshold": 1, "alert_rating_floor": 4.5,
                            "gbp_rating": 3.9, "al_unres_push": 0, "al_unres_email": 0}, db_path=db_path)
    _labor_over(db_path, rid)
    notify.begin_daily_batch()
    notify.check_daily_alerts(db_path=db_path)
    out = notify.flush_daily_batch(db_path=db_path)
    assert out["combined"] == 1
    assert [p for p in sent["push"] if p[0] == rid], "the owner heard about none of it"


def test_a_single_labor_alert_is_delivered_with_the_unresponded_toggles_off(db_path, sent, monkeypatch):
    """A8 #24 — the single-item half, which works today."""
    monkeypatch.setattr(notify, "rush_release_at", lambda *a, **k: None)
    rid = _rid(db_path)
    update_restaurant(rid, {"urgent_via_sms": 0, "urgent_via_email": 1, "alert_labor_over": 1,
                            "labor_target_pct": 30.0, "al_unres_push": 0, "al_unres_email": 0}, db_path=db_path)
    _labor_over(db_path, rid)
    notify.begin_daily_batch()
    notify.check_daily_alerts(db_path=db_path)
    notify.flush_daily_batch(db_path=db_path)
    assert (rid, "labor_over") in sent["push"]


def test_a_push_only_owner_still_gets_the_labor_alert(db_path, sent, monkeypatch):
    """A8 #27 / MOD-NOT-8."""
    monkeypatch.setattr(notify, "rush_release_at", lambda *a, **k: None)
    rid = _rid(db_path)
    update_restaurant(rid, {"urgent_via_sms": 0, "urgent_via_email": 0, "alert_labor_over": 1,
                            "labor_target_pct": 30.0}, db_path=db_path)
    _labor_over(db_path, rid)
    notify.check_daily_alerts(db_path=db_path)
    assert (rid, "labor_over") in sent["push"]


def _negative_review(db_path, rid, status, days_old=5, deleted=False):
    save_reviews([Review(restaurant_id=rid, platform="google", external_id=f"nr-{status}-{deleted}",
                         author="Z", rating=1, text="awful")], db_path=db_path)
    conn = get_conn(db_path)
    conn.execute("UPDATE reviews SET sentiment='negative', processed=1, response_status=?, "
                 "draft_response='Sorry', fetched_at=datetime('now', ?), deleted_at=? WHERE restaurant_id=?",
                 (status, f"-{days_old} days", "2026-09-01 00:00:00" if deleted else None, rid))
    conn.commit()
    conn.close()


def test_a_drafted_but_unposted_negative_review_triggers_the_no_response_alert(db_path, sent, monkeypatch):
    """A8 #28 / MOD-NOT-9 — auto-drafting makes 'drafted' the normal state of
    a review waiting on the owner."""
    monkeypatch.setattr(notify, "rush_release_at", lambda *a, **k: None)
    rid = _rid(db_path)
    update_restaurant(rid, {"urgent_via_email": 1, "alert_no_response": 1}, db_path=db_path)
    _negative_review(db_path, rid, "drafted")
    notify.check_no_response_alerts(db_path=db_path)
    assert "no_response" in _alert_log(db_path, rid)


def test_a_soft_deleted_review_never_triggers_the_no_response_alert(db_path, sent, monkeypatch):
    """A8 #29 / MOD-NOT-9 — retention-purged rows are not actionable."""
    monkeypatch.setattr(notify, "rush_release_at", lambda *a, **k: None)
    rid = _rid(db_path)
    update_restaurant(rid, {"urgent_via_email": 1, "alert_no_response": 1}, db_path=db_path)
    _negative_review(db_path, rid, "pending", deleted=True)
    notify.check_no_response_alerts(db_path=db_path)
    assert _alert_log(db_path, rid) == []


# ── A history import is not news ────────────────────────────────────────────

def test_a_history_import_of_old_reviews_sends_no_alerts(db_path, sent, monkeypatch):
    """A8 #31 / MOD-REV-6 — 30 one-star reviews written in 2019 arrive on the
    first GBP connect."""
    monkeypatch.setattr(notify, "rush_release_at", lambda *a, **k: None)
    rid = _rid(db_path)
    update_restaurant(rid, {"urgent_via_email": 1, "alert_1star": 1}, db_path=db_path)
    _n, saved = save_reviews(_one_star_reviews(rid, 30, review_date="2019-05-01T12:00:00Z"), db_path=db_path)
    notify.fire_review_alerts(rid, "Alert Co", saved, db_path=db_path)
    assert _alert_log(db_path, rid) == []


def test_a_review_written_today_in_the_same_import_still_alerts(db_path, sent, monkeypatch):
    """A8 #31 — the guard must not swallow a genuinely new review."""
    monkeypatch.setattr(notify, "rush_release_at", lambda *a, **k: None)
    rid = _rid(db_path)
    update_restaurant(rid, {"urgent_via_email": 1, "alert_1star": 1}, db_path=db_path)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    _n, saved = save_reviews(_one_star_reviews(rid, 1, prefix="new", review_date=today), db_path=db_path)
    notify.fire_review_alerts(rid, "Alert Co", saved, db_path=db_path)
    assert _alert_log(db_path, rid) == ["1star"]


# ── Scale and restarts ──────────────────────────────────────────────────────

class _CountingConn:
    def __init__(self, conn, counter):
        self._c, self._n = conn, counter

    def execute(self, *a, **k):
        self._n[0] += 1
        return self._c.execute(*a, **k)

    def __getattr__(self, name):
        return getattr(self._c, name)


def test_restaurants_outside_their_10am_window_cost_nothing_on_the_hourly_pass(db_path, sent, monkeypatch):
    """A8 #33 / MOD-NOT-11 — 200 restaurants, all at 3am local: the pass
    must not issue a query per restaurant (it runs every hour, forever)."""
    import time_utils
    for i in range(200):
        rid = _rid(db_path, name=f"R{i}")
        update_restaurant(rid, {"urgent_via_email": 1, "alert_labor_over": 1, "alert_no_response": 1,
                                "alert_food_waste": 1}, db_path=db_path)
    _freeze_clock(monkeypatch, _chicago(2026, 9, 21, 3, 0))     # 3am where every one of them is
    monkeypatch.setattr(time_utils, "datetime", _dtmod.datetime)
    real = models.get_conn
    n = [0]
    counting = lambda *a, **k: _CountingConn(real(db_path), n)
    for mod in (models, notify, ops):
        monkeypatch.setattr(mod, "get_conn", counting, raising=False)
    notify.begin_daily_batch()
    notify.check_daily_alerts(db_path=db_path, local_hour=10)
    notify.check_extra_daily_alerts(db_path=db_path, local_hour=10)
    notify.flush_daily_batch(db_path=db_path)
    assert n[0] < 200, f"{n[0]} queries for 200 restaurants none of which were due"


def test_a_restart_mid_batch_does_not_lose_the_days_alerts(db_path, sent, monkeypatch):
    """A8 #34 / MOD-NOT-16 — the process dies after the checks collected a
    labor alert but before flush; the next hourly pass must re-collect it."""
    import time_utils
    monkeypatch.setattr(notify, "rush_release_at", lambda *a, **k: None)
    monkeypatch.setattr(time_utils, "restaurant_now_by_id", lambda r, naive=False: datetime(2026, 9, 21, 10, 5))
    rid = _rid(db_path)
    update_restaurant(rid, {"urgent_via_email": 1, "alert_labor_over": 1, "labor_target_pct": 30.0},
                      db_path=db_path)
    _labor_over(db_path, rid)
    notify.begin_daily_batch()
    notify.check_daily_alerts(db_path=db_path, local_hour=10)
    notify._batch = None                                  # the deploy
    notify.begin_daily_batch()
    notify.check_daily_alerts(db_path=db_path, local_hour=10)
    notify.flush_daily_batch(db_path=db_path)
    assert "labor_over" in _alert_log(db_path, rid)


# ── Push fan-out ────────────────────────────────────────────────────────────

class _NowExecutor:
    def submit(self, fn, *a, **k):
        fn(*a, **k)


@pytest.fixture
def delivered(monkeypatch):
    got = []
    monkeypatch.setattr(push, "_deliver", lambda row, *a, **k: got.append(row["apns_token"]))
    monkeypatch.setattr(push, "_push_executor", lambda: _NowExecutor())
    return got


def test_a_revoked_teammates_phone_stops_receiving_alerts(db_path, delivered):
    """A8 #35 / MOD-NOT-6."""
    rid = _rid(db_path)
    owner = create_user(rid, "owner", "owner@x.test", "pw-owner-1", db_path=db_path)
    mgr = create_user(rid, "mgr", "mgr@x.test", "pw-mgr-1", db_path=db_path)
    set_user_role(mgr, "manager", db_path=db_path)
    push.register_device_token(owner, rid, "tok-owner", db_path=db_path)
    push.register_device_token(mgr, rid, "tok-mgr", db_path=db_path)
    assert revoke_team_member(rid, mgr, owner, db_path=db_path)["ok"] is True
    push.fire_push(rid, "1star", "t", "b", db_path=db_path)
    assert delivered == ["tok-owner"]


def test_a_multi_location_owner_hears_about_every_location(db_path, delivered):
    """A8 #36 / MOD-NOT-7 — registered once while viewing A; a health alert
    at B must still reach the phone."""
    a = _rid(db_path, name="Syrup A", owner_email="own@x.test", location_group="Syrup")
    b = _rid(db_path, name="Syrup B", owner_email="own@x.test", location_group="Syrup")
    owner = create_user(a, "own", "own@x.test", "pw-owner-1", db_path=db_path)
    push.register_device_token(owner, a, "tok-own", db_path=db_path)
    push.fire_push(a, "health", "t", "b", db_path=db_path)
    push.fire_push(b, "health", "t", "b", db_path=db_path)
    assert delivered == ["tok-own", "tok-own"]


def test_a_full_push_queue_does_not_drop_devices(db_path, monkeypatch):
    """A8 #41 / MOD-NOT-12 — with room for 2 in flight and 5 devices, every
    device is delivered to once the pool drains."""
    rid = _rid(db_path)
    uid = create_user(rid, "owner", "owner@x.test", "pw-owner-1", db_path=db_path)
    for i in range(5):
        push.register_device_token(uid, rid, f"tok-{i}", db_path=db_path)
    queued, got = [], []

    class _Later:
        def submit(self, fn, *a, **k):
            queued.append((fn, a, k))
    monkeypatch.setattr(push, "_push_executor", lambda: _Later())
    monkeypatch.setattr(push, "_deliver", lambda row, *a, **k: got.append(row["apns_token"]))
    monkeypatch.setattr(push, "_MAX_PUSH_QUEUED", 2)
    monkeypatch.setattr(push, "_queued", 0)
    monkeypatch.setattr(ops, "capture", lambda *a, **k: None)
    push.fire_push(rid, "morning_brief", "t", "b", db_path=db_path)
    while queued:
        fn, a, k = queued.pop(0)
        fn(*a, **k)
    assert sorted(got) == [f"tok-{i}" for i in range(5)]


def test_the_per_alert_push_lookups_use_an_index(db_path):
    """MOD-NOT-13 — brief_pushed_today and get_device_tokens run on every
    alert; neither may be a full-table scan."""
    conn = get_conn(db_path)
    try:
        plans = [
            " ".join(r[3] for r in conn.execute(
                "EXPLAIN QUERY PLAN SELECT 1 FROM push_deliveries WHERE restaurant_id=? AND "
                "alert_type='morning_brief' AND ok=1 AND date(created_at) >= ?", (1, "2026-09-21"))),
            " ".join(r[3] for r in conn.execute(
                "EXPLAIN QUERY PLAN SELECT * FROM device_tokens WHERE restaurant_id=? AND disabled_reason IS NULL",
                (1,))),
        ]
    finally:
        conn.close()
    assert all("USING INDEX" in p or "USING COVERING INDEX" in p for p in plans), plans


# ── The unread badge ────────────────────────────────────────────────────────

def _log(db_path, rid, alert_type, fired_at=None):
    conn = get_conn(db_path)
    if fired_at:
        conn.execute("INSERT INTO alert_log (restaurant_id, alert_type, fired_at) VALUES (?,?,?)",
                     (rid, alert_type, fired_at))
    else:
        conn.execute("INSERT INTO alert_log (restaurant_id, alert_type) VALUES (?,?)", (rid, alert_type))
    conn.commit()
    conn.close()


def _bearer(app, username, password):
    token = app.test_client().post("/mobile/api/login", json={"username": username, "password": password}).get_json()["token"]
    return {"Authorization": f"Bearer {token}"}


def test_the_badge_counts_only_what_the_list_shows_this_login(db_path, app):
    """A8 #43 / MOD-NOT-10 — a manager cannot see food cost; a food_waste row
    must not badge them."""
    rid = _rid(db_path, module_reviews=1, module_labor=1, module_inventory=1)
    create_user(rid, "owner", "owner@x.test", "pw-owner-1", db_path=db_path)
    mgr = create_user(rid, "mgr", "mgr@x.test", "pw-mgr-12", db_path=db_path)
    set_user_role(mgr, "manager", db_path=db_path)
    _log(db_path, rid, "labor_over")
    _log(db_path, rid, "food_waste")
    h = _bearer(app, "mgr", "pw-mgr-12")
    c = app.test_client()
    count = c.get("/mobile/api/notifications/unread-count", headers=h).get_json()["count"]
    visible = c.get("/mobile/api/notifications", headers=h).get_json()["notifications"]
    assert count == len(visible) == 1


def test_a_new_login_is_not_badged_with_the_restaurants_whole_history(db_path, app):
    """A8 #43 / MOD-NOT-10 — a co-owner invited today has read nothing, but
    nothing from before they existed is news to them either."""
    rid = _rid(db_path)
    create_user(rid, "owner", "owner@x.test", "pw-owner-1", db_path=db_path)
    for i in range(40):
        _log(db_path, rid, "1star", fired_at=f"2026-08-{(i % 28) + 1:02d} 12:00:00")
    create_user(rid, "coowner", "co@x.test", "pw-co-1234", db_path=db_path)
    h = _bearer(app, "coowner", "pw-co-1234")
    assert app.test_client().get("/mobile/api/notifications/unread-count", headers=h).get_json()["count"] == 0


# ── Retention ───────────────────────────────────────────────────────────────

def test_old_sent_holds_and_notification_opens_are_pruned(db_path):
    """A8 'also noted' / MOD-NOT-14 — the retention registry covers both."""
    rid = _rid(db_path)
    conn = get_conn(db_path)
    conn.execute("INSERT INTO alert_holds (restaurant_id, alert_type, subject, html, sms_text, release_at, "
                 "sent_at, created_at) VALUES (?,?,?,?,?,?,?,?)",
                 (rid, "1star", "s", "<p>guest excerpt</p>", "s", "2025-01-01 12:00:00",
                  "2025-01-01 12:30:00", "2025-01-01 12:00:00"))
    conn.execute("INSERT INTO notification_opens (restaurant_id, alert_type, opened_at) VALUES (?,?,?)",
                 (rid, "1star", "2024-01-01 12:00:00"))
    conn.commit()
    conn.close()
    ops.prune_ledgers(db_path)
    conn = get_conn(db_path)
    holds = conn.execute("SELECT COUNT(*) FROM alert_holds").fetchone()[0]
    opens = conn.execute("SELECT COUNT(*) FROM notification_opens").fetchone()[0]
    conn.close()
    assert holds == 0 and opens == 0
