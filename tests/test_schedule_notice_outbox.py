"""The staff schedule-notice outbox sends each notice to the provider at most
once (release-safety audit 10/26/26 A02-11).

A deliverer stamps `send_started_at` over its own claim before it calls the
provider. A process killed after the provider took the send (a deploy's
SIGTERM, a crash before the outcome was written) used to leave the row
'sending'; when its lease ran out the next pass sent it again. Now the pass
that takes it over leaves it failed, saying it may have reached them, and
sends nothing. A crash BEFORE the provider was called still delivers once,
and a send the provider refused is still retried.

Nothing is sent: the provider is a spy at emails.send_staff_schedule_email
(the conftest guard blocks Resend), and "advance past the lease" moves the
claim's stamp to a fixed old time — no wall clock is read here.
"""
import sqlite3
import sys

import pytest

import emails
import models
import people
import schedule_publish as sp
from models import Restaurant, create_restaurant

# The deploy gate's contract slice (TESTING.md, Tiers; blind re-audit 10/27/26).
pytestmark = pytest.mark.contract

LONG_AGO = "2000-01-01 00:00:00.00000000"           # older than any lease


class _Crash(BaseException):
    """The process dying mid-pass: nothing after it runs, nothing catches it."""


@pytest.fixture
def db(db_path, monkeypatch):
    real = models.get_conn

    def conn(*a, **k):
        return real(db_path)
    for mod in list(sys.modules.values()):
        bound = getattr(mod, "get_conn", None) if mod is not None else None
        if bound is real or str(getattr(bound, "__module__", "")).startswith(("test_", "tests.", "conftest")):
            monkeypatch.setattr(mod, "get_conn", conn)
    monkeypatch.setattr(models, "get_conn", conn)
    monkeypatch.setattr(models, "DB_PATH", db_path)
    return db_path


@pytest.fixture
def outbox(db, monkeypatch):
    """A published week with one notice queued for Ana, who is reachable by
    email only; the provider is a spy that accepts every send."""
    rid = create_restaurant(Restaurant(name="Outbox Grill", owner_email="o@x.test"), db_path=db)
    csv = ("date,day,employee,role,shift_start,shift_end,scheduled_hours,notes\n"
           "2026-10-19,Monday,Ana,Server,5:00pm,10:00pm,5,\n")
    hid = models.save_schedule_history(rid, "2026-10-19", "2026-10-25", 5, 0, 30, csv, [])
    conn = models.get_conn()
    try:
        sp.enqueue(conn, rid, hid, "2026-10-19", "b1", [("Ana", "posted")])
        conn.execute("UPDATE schedule_history SET published_at=datetime('now') WHERE id=?", (hid,))
        conn.commit()
    finally:
        conn.close()
    monkeypatch.setattr(people, "reach", lambda rid_, names, purpose=None, **k: {
        n: {"push_user_id": None, "sms": None, "email": "ana@x.test"} for n in names})
    sent = []

    def provider(**kw):
        sent.append(kw["to_email"])
        return emails.SendResult(True, message_id=f"re_{len(sent)}")
    monkeypatch.setattr(emails, "send_staff_schedule_email", provider)
    return {"rid": rid, "hid": hid, "sent": sent}


def _row(rid):
    conn = models.get_conn()
    try:
        return dict(conn.execute("SELECT * FROM schedule_notices WHERE restaurant_id=? AND batch='b1'",
                                 (rid,)).fetchone())
    finally:
        conn.close()


def _set(rid, **cols):
    conn = models.get_conn()
    try:
        conn.execute("UPDATE schedule_notices SET " + ", ".join(f"{k}=?" for k in cols)
                     + " WHERE restaurant_id=? AND batch='b1'", (*cols.values(), rid))
        conn.commit()
    finally:
        conn.close()


def test_a_crash_after_the_provider_took_the_send_never_sends_it_again(outbox, monkeypatch):
    rid = outbox["rid"]
    real_finish = sp._finish_notice

    def die(*a, **k):
        raise _Crash()
    monkeypatch.setattr(sp, "_finish_notice", die)
    with pytest.raises(_Crash):
        sp.deliver_notices(restaurant_id=rid, batch="b1", budget_seconds=60)
    assert outbox["sent"] == ["ana@x.test"]                      # the provider took it
    row = _row(rid)
    assert row["status"] == "sending" and row["send_started_at"]  # and the outcome was never written
    monkeypatch.setattr(sp, "_finish_notice", real_finish)
    _set(rid, claimed_at=LONG_AGO)                                # the dead claim's lease runs out
    got = sp.deliver_notices(restaurant_id=rid, batch="b1", budget_seconds=60)
    assert outbox["sent"] == ["ana@x.test"]                      # one send, ever
    assert got["failed"] == [{"employee_name": "Ana", "error": sp.INTERRUPTED_SEND_LINE}] and not got["sent"]
    row = _row(rid)
    assert row["status"] == "failed" and row["last_error"] == sp.INTERRUPTED_SEND_LINE
    assert row["next_attempt_at"] is None and row["claimed_at"] is None       # no retry on its own
    sp.deliver_notices(restaurant_id=rid, batch="b1", budget_seconds=60)
    assert outbox["sent"] == ["ana@x.test"]
    # the owner's view says what happened, and the share link the send carried still works
    status = sp.notice_status(rid, outbox["hid"])
    assert status["failed"] == 1 and status["people"][0]["error"] == sp.INTERRUPTED_SEND_LINE
    assert [s["employee_name"] for s in sp.week_share_status(rid, outbox["hid"])] == ["Ana"]


def test_a_crash_before_the_provider_was_called_still_delivers_once(outbox, monkeypatch):
    rid = outbox["rid"]
    real_reach = people.reach

    def die(*a, **k):
        raise _Crash()
    monkeypatch.setattr(people, "reach", die)
    with pytest.raises(_Crash):
        sp.deliver_notices(restaurant_id=rid, batch="b1", budget_seconds=60)
    assert outbox["sent"] == [] and _row(rid)["send_started_at"] is None
    monkeypatch.setattr(people, "reach", real_reach)
    _set(rid, claimed_at=LONG_AGO)
    got = sp.deliver_notices(restaurant_id=rid, batch="b1", budget_seconds=60)
    assert outbox["sent"] == ["ana@x.test"] and [x["employee_name"] for x in got["sent"]] == ["Ana"]
    row = _row(rid)
    assert row["status"] == "sent" and row["channel"] == "email" and row["provider_id"] == "re_1"
    assert "provider_id" not in got["sent"][0]                  # kept on the row, not in the response


def test_a_send_the_provider_refused_is_still_retried(outbox, monkeypatch):
    rid = outbox["rid"]
    calls = []

    def refused(**kw):
        calls.append(kw["to_email"])
        return emails.SendResult(False, error="Resend 503", reason="transient")
    monkeypatch.setattr(emails, "send_staff_schedule_email", refused)
    got = sp.deliver_notices(restaurant_id=rid, batch="b1", budget_seconds=60)
    assert calls == ["ana@x.test"] and [x["employee_name"] for x in got["failed"]] == ["Ana"]
    row = _row(rid)
    assert row["status"] == "failed" and row["next_attempt_at"] and row["send_started_at"] is None
    _set(rid, next_attempt_at=LONG_AGO)                          # its retry time comes
    monkeypatch.setattr(emails, "send_staff_schedule_email",
                        lambda **kw: calls.append(kw["to_email"]) or emails.SendResult(True, message_id="re_9"))
    got = sp.deliver_notices(restaurant_id=rid, batch="b1", budget_seconds=60)
    assert calls == ["ana@x.test", "ana@x.test"] and _row(rid)["status"] == "sent"


def test_a_deliverer_whose_claim_was_taken_over_sends_nothing(outbox):
    rid = outbox["rid"]
    nid = _row(rid)["id"]
    first = sp._claim_notices([{"id": nid}])
    assert list(first) == [nid]
    _set(rid, claimed_at=LONG_AGO)                               # its lease ran out; another pass takes it
    second = sp._claim_notices([{"id": nid}])
    assert list(second) == [nid] and second[nid] != first[nid]
    assert sp._stamp_send_started(rid, nid, first[nid]) is False     # the slow one may not send
    assert sp._stamp_send_started(rid, nid, second[nid]) is True
    assert sp._stamp_send_started(rid + 1, nid, second[nid]) is False   # never another restaurant's row


def test_a_database_made_before_the_send_stamp_gets_its_columns_at_boot(tmp_path):
    path = str(tmp_path / "old.db")
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE schedule_notices (id INTEGER PRIMARY KEY AUTOINCREMENT, restaurant_id INTEGER NOT "
                 "NULL, history_id INTEGER NOT NULL, week_start TEXT, batch TEXT NOT NULL, employee_name TEXT NOT "
                 "NULL, person_id INTEGER, kind TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending', channel TEXT, "
                 "sent_to TEXT, delivery TEXT, shifts INTEGER, attempts INTEGER NOT NULL DEFAULT 0, last_error TEXT, "
                 "next_attempt_at TEXT, claimed_at TEXT, requested_by TEXT, created_at TEXT NOT NULL DEFAULT "
                 "(datetime('now')), sent_at TEXT, UNIQUE(batch, employee_name))")
    conn.commit()
    conn.close()
    sp.init_schedule_notices(path)
    sp.init_schedule_notices(path)                               # and again: a second boot changes nothing
    conn = sqlite3.connect(path)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(schedule_notices)").fetchall()}
    conn.close()
    assert {"send_started_at", "provider_id"} <= cols
