"""Alerts wait out service instead of interrupting it (workflow audit #1)."""
from datetime import datetime, timedelta, timezone

import pytest

import models
import notify
from models import create_restaurant, get_conn, Restaurant


@pytest.fixture(autouse=True)
def _redirect(monkeypatch, db_path):
    # push is imported HERE, before models.get_conn is patched: imported
    # first inside a test, its bound get_conn would capture that test's
    # database and every later test would write to the wrong file.
    import auth, push
    real = models.get_conn
    redirect = lambda *a, **k: real(db_path)
    for mod in (models, notify, auth, push):
        monkeypatch.setattr(mod, "get_conn", redirect, raising=False)
    for mod in (models, notify, auth, push):
        monkeypatch.setattr(mod, "DB_PATH", db_path, raising=False)


def _rid(db_path, **kw):
    kw.setdefault("name", "Rush Co")
    kw.setdefault("owner_email", "o@x.test")
    kw.setdefault("timezone", "America/Chicago")
    return create_restaurant(Restaurant(**kw), db_path=db_path)


def _at(hh, mm):
    return datetime(2026, 9, 21, hh, mm)          # a Monday


def test_a_review_alert_at_lunch_waits_until_the_rush_ends(db_path):
    rid = _rid(db_path)
    release = notify.rush_release_at(rid, "2star", db_path, now_local=_at(12, 15))
    # 13:30 America/Chicago on 21 Sep 2026 is 18:30 UTC.
    assert release is not None
    assert (release.hour, release.minute) == (18, 30)


def test_dinner_is_held_too_and_quiet_afternoons_are_not(db_path):
    rid = _rid(db_path)
    assert notify.rush_release_at(rid, "1star", db_path, now_local=_at(19, 0)) is not None
    assert notify.rush_release_at(rid, "1star", db_path, now_local=_at(15, 0)) is None
    assert notify.rush_release_at(rid, "1star", db_path, now_local=_at(9, 30)) is None


def test_a_health_alert_is_never_held(db_path):
    rid = _rid(db_path)
    assert notify.rush_release_at(rid, "health", db_path, now_local=_at(12, 15)) is None


def test_a_restaurant_closed_at_lunch_is_not_in_a_rush(db_path):
    """Dinner-only: noon is not its rush."""
    rid = _rid(db_path)
    models.update_restaurant(rid, {"open_times_json": '{"Monday": "4:00pm"}',
                                   "close_times_json": '{"Monday": "10:00pm"}'}, db_path=db_path)
    assert notify.rush_release_at(rid, "2star", db_path, now_local=_at(12, 15)) is None
    assert notify.rush_release_at(rid, "2star", db_path, now_local=_at(18, 0)) is not None


def test_an_owner_can_turn_holding_off(db_path):
    rid = _rid(db_path)
    models.update_restaurant(rid, {"alert_hold_during_service": 0}, db_path=db_path)
    assert notify.rush_release_at(rid, "2star", db_path, now_local=_at(12, 15)) is None


def test_a_held_alert_is_delivered_once_the_rush_ends(db_path, monkeypatch):
    rid = _rid(db_path)
    sent = []
    monkeypatch.setattr(notify, "deliver_alert", lambda *a, **k: sent.append(a[1]))
    release = datetime.now(timezone.utc) + timedelta(minutes=30)
    notify.hold_alert(rid, "2star", "sms", "subject", "<p>html</p>", release, db_path=db_path)

    assert notify.release_due_alerts(db_path=db_path)["released"] == 0, "not yet"
    later = release + timedelta(minutes=1)
    assert notify.release_due_alerts(db_path=db_path, now_utc=later)["released"] == 1
    assert sent == ["2star"]
    assert notify.release_due_alerts(db_path=db_path, now_utc=later)["released"] == 0, "once only"


def test_a_delivery_failure_does_not_replay_every_tick(db_path, monkeypatch):
    rid = _rid(db_path)
    def _boom(*a, **k):
        raise RuntimeError("resend down")
    monkeypatch.setattr(notify, "deliver_alert", _boom)
    notify.hold_alert(rid, "2star", "sms", "s", "<p>h</p>",
                      datetime.now(timezone.utc) - timedelta(minutes=1), db_path=db_path)
    notify.release_due_alerts(db_path=db_path)
    conn = get_conn(db_path)
    assert conn.execute("SELECT COUNT(*) FROM alert_holds WHERE sent_at IS NULL").fetchone()[0] == 0
    conn.close()


# ── owner-facing jobs run on the restaurant's clock (workflow audit #4) ────

def test_daily_alerts_are_gated_on_the_restaurants_own_10am(db_path, monkeypatch):
    import time_utils
    rid = _rid(db_path)

    def _at_local(hour):
        monkeypatch.setattr(time_utils, "restaurant_now_by_id",
                            lambda r, naive=False: datetime(2026, 9, 21, hour, 5))
        monkeypatch.setattr(notify, "_gated_out", notify._gated_out)

    _at_local(8)
    assert notify._gated_out(rid, 10, "t_early", db_path) is True, "8am local: too early"
    _at_local(10)
    assert notify._gated_out(rid, 10, "t_ok", db_path) is False, "10am local: send"
    assert notify._gated_out(rid, 10, "t_ok", db_path) is True, "and only once that day"
    _at_local(16)
    assert notify._gated_out(rid, 10, "t_late", db_path) is True, "4pm local: missed the window"


def test_no_gate_means_run_for_everyone(db_path):
    """A direct call — a test, an admin re-run — is never time-gated."""
    assert notify._gated_out(_rid(db_path), None, "t_none", db_path) is False


# ── one morning email, not three (workflow audit #16) ─────────────────────

def _push_delivery(db_path, rid, alert_type="morning_brief", ok=1):
    """A delivered push, as push.py records one. The device_tokens FK is
    irrelevant here — brief_pushed_today reads push_deliveries only."""
    import auth, push
    auth.init_auth(db_path=db_path)
    push.init_push(db_path=db_path)
    conn = get_conn(db_path)
    conn.execute("INSERT INTO push_deliveries (device_token_id, restaurant_id, alert_type, ok, attempts) "
                 "VALUES (1,?,?,?,1)", (rid, alert_type, ok))
    conn.commit(); conn.close()


def test_an_alert_the_brief_already_carried_does_not_email_again(db_path, monkeypatch):
    rid = _rid(db_path)
    sent = []
    monkeypatch.setattr(notify, "_send_alert_email", lambda *a, **k: sent.append(a[1]) or True)
    _push_delivery(db_path, rid)
    assert notify._email_alert(rid, "o@x.test", "Labor over", "<p>x</p>", "labor_over", db_path) is False
    assert sent == []


def test_an_urgent_alert_still_emails_after_the_brief(db_path, monkeypatch):
    """Only the types the brief covers are folded — a one-star review is not."""
    rid = _rid(db_path)
    sent = []
    monkeypatch.setattr(notify, "_send_alert_email", lambda *a, **k: sent.append(a[1]) or True)
    _push_delivery(db_path, rid)
    notify._email_alert(rid, "o@x.test", "1 star", "<p>x</p>", "1star", db_path)
    assert sent == ["1 star"]


def test_without_the_app_the_alert_email_still_arrives(db_path, monkeypatch):
    """No push brief means email is the owner's only channel — never fold it."""
    rid = _rid(db_path)
    sent = []
    monkeypatch.setattr(notify, "_send_alert_email", lambda *a, **k: sent.append(a[1]) or True)
    notify._email_alert(rid, "o@x.test", "Labor over", "<p>x</p>", "labor_over", db_path)
    assert sent == ["Labor over"]
