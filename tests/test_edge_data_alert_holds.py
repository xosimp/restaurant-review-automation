"""Held alerts under the failures the release loop has to survive (DATA audit:
DATA-4, DATA-5).

What this protects: an alert held through lunch or dinner service is released
by `notify.release_due_alerts` on the next scheduler tick. That loop delivers
first and marks the hold sent afterwards, so two runners (a lease handed over
mid-tick) or a "mark sent" write that fails (a full or locked volume) turn one
alert into many texts. The owner must get each held alert once.

Tests without a marker pin behaviour that works today; xfail(strict=True)
tests assert the correct behaviour for a defect the audit confirmed.
"""
import threading
from datetime import datetime, timedelta, timezone

import pytest

import models
import notify
from models import Restaurant, create_restaurant


@pytest.fixture(autouse=True)
def _redirect(monkeypatch, db_path):
    import auth, push
    real = models.get_conn
    redirect = lambda *a, **k: real(db_path)
    for mod in (models, notify, auth, push):
        monkeypatch.setattr(mod, "get_conn", redirect, raising=False)
        monkeypatch.setattr(mod, "DB_PATH", db_path, raising=False)


def _held(db_path):
    rid = create_restaurant(Restaurant(name="Held Co", owner_email="o@x.test", timezone="America/Chicago"),
                            db_path=db_path)
    release = datetime.now(timezone.utc) - timedelta(minutes=1)
    notify.hold_alert(rid, "1star", "A 1-star review needs you", "1-star review", "<p>h</p>", release,
                      db_path=db_path)
    return rid


def _refuse_writes(monkeypatch, db_path):
    real = models.get_conn

    def refuses(*a, **k):
        conn = real(db_path)
        conn.execute("PRAGMA query_only=ON")
        return conn
    monkeypatch.setattr(models, "get_conn", refuses)


def test_a_released_hold_is_delivered_once_across_ticks(db_path, monkeypatch):
    _held(db_path)
    sent = []
    monkeypatch.setattr(notify, "deliver_alert", lambda *a, **k: sent.append(a[1]))
    for _ in range(3):
        notify.release_due_alerts(db_path=db_path)
    assert sent == ["1star"]


@pytest.mark.xfail(strict=True, reason="DATA-5: _mark_sent runs after deliver_alert and is unprotected; when the "
                                       "write fails the same hold is re-delivered on every tick")
def test_a_failed_mark_sent_does_not_redeliver(db_path, monkeypatch):
    _held(db_path)
    sent = []
    monkeypatch.setattr(notify, "deliver_alert", lambda *a, **k: sent.append(a[1]))
    _refuse_writes(monkeypatch, db_path)
    for _ in range(3):                       # three 5-minute ticks on a full volume
        try:
            notify.release_due_alerts(db_path=db_path)
        except Exception:
            pass
    assert len(sent) <= 1, f"one held alert was delivered {len(sent)} times"


@pytest.mark.xfail(strict=True, reason="DATA-4: release_due_alerts selects unsent holds and marks them only after "
                                       "delivery, so two concurrent runners both deliver the same hold")
def test_two_runners_deliver_a_hold_once(db_path, monkeypatch):
    _held(db_path)
    sent = []
    both_in = threading.Barrier(2)

    def slow_deliver(*a, **k):
        sent.append(a[1])
        try:
            both_in.wait(timeout=1)          # hold delivery open until the other runner is here too
        except threading.BrokenBarrierError:
            pass
    monkeypatch.setattr(notify, "deliver_alert", slow_deliver)
    runners = [threading.Thread(target=notify.release_due_alerts, kwargs={"db_path": db_path}) for _ in range(2)]
    for t in runners:
        t.start()
    for t in runners:
        t.join(5)
    assert len(sent) == 1, f"two runners delivered one held alert {len(sent)} times"
