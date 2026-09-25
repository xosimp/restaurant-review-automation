"""Server operations fixes from the 9/25/26 re-audit (F2 / F3 server sides).

Publishing and re-sending a week, time-off and shift-request notices, the
stock ledger's order, close-out 86s, people records, automation switches,
targets, PO receiving and the delayed-action undo. Nothing leaves the
process: every sender is replaced with a recorder, and the conftest guard
blocks Resend and Twilio.
"""
import datetime as dt
import json
import sys

import pytest

import activity, covers, decisions, delayed, demand_signals, goals, issues, metrics, outcomes  # noqa: E401,F401
import push, schedule_economics, schedule_intel, schedule_rules, schedule_versions  # noqa: E401,F401
import shift_requests, shift_quality, staff_schedule, staff_settings, strategy_jobs, time_off  # noqa: E401,F401
import labor_replacements  # noqa: F401
from flask import Flask

import auth
import client_api
import emails
import labor
import mobile_api
import models
import notify
import people
import schedule_versions as sv
from models import create_restaurant, Restaurant

HEADER = "date,day,employee,role,shift_start,shift_end,scheduled_hours,notes"
W1 = ["2026-10-05", "2026-10-06", "2026-10-07", "2026-10-08", "2026-10-09"]


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
    monkeypatch.setattr(models, "_cached_shifts", lambda r: [])
    monkeypatch.setattr(client_api, "log_account_event", lambda *a, **k: None)
    monkeypatch.setattr(labor, "analyse_shifts_for_restaurant", lambda r: {"is_live": True})
    return db_path


def _restaurant(db_path, people_=("Ana", "Bob", "Cy"), **cols):
    rid = create_restaurant(Restaurant(name="Ops Co", owner_email="o@x.com"), db_path=db_path)
    cols.setdefault("module_labor", 1)
    conn = models.get_conn(db_path)
    conn.execute("UPDATE restaurants SET " + ", ".join(f"{k}=?" for k in cols) + " WHERE id=?", (*cols.values(), rid))
    conn.commit()
    conn.close()
    for n in people_:
        models.add_manual_team_member(rid, n, role="Server", db_path=db_path)
    return rid


def _day(d):
    return dt.date.fromisoformat(d).strftime("%A")


def _csv(dates=W1, people_=(("Ana", "11:00am", "3:00pm", 4), ("Bob", "5:00pm", "9:00pm", 4))):
    return HEADER + "\n" + "\n".join(f"{d},{_day(d)},{n},Server,{s},{e},{h}," for d in dates for n, s, e, h in people_)


def _save(db_path, rid, text, published=False):
    hid = models.save_schedule_history(rid, W1[0], W1[-1], 0, 0, 30, text, [], db_path=db_path)
    sv.append(rid, hid, "generated", text, saved_by="Cavnar AI")
    if published:
        conn = models.get_conn(db_path)
        conn.execute("UPDATE schedule_history SET published_at=datetime('now') WHERE id=?", (hid,))
        conn.commit()
        conn.close()
        sv.append(rid, hid, "published", text, saved_by="Owner")
    return hid


def _contacts(db_path, rid, *names):
    conn = models.get_conn(db_path)
    for n in names:
        conn.execute("INSERT OR REPLACE INTO staff_contacts (restaurant_id, employee_name, email) VALUES (?,?,?)",
                     (rid, n, f"{n.lower()}@x.com"))
    conn.commit()
    conn.close()


def _one(db_path, sql, *args):
    conn = models.get_conn(db_path)
    try:
        return conn.execute(sql, args).fetchone()
    finally:
        conn.close()


def _app(db_path):
    auth.init_auth(db_path=db_path)
    app = Flask(__name__, template_folder="../templates")
    app.register_blueprint(mobile_api.mobile_bp)
    return app


def _bearer(db_path, rid, name="owner", role="client"):
    uid = auth.create_user(rid, name, f"{name}@x.com", "pw", db_path=db_path)
    conn = models.get_conn(db_path)
    conn.execute("UPDATE users SET role=? WHERE id=?", (role, uid))
    conn.commit()
    conn.close()
    return {"Authorization": f"Bearer {auth.create_session(uid, db_path=db_path)}"}


@pytest.fixture
def mail(monkeypatch):
    """Records each staff schedule email; `fail` names addresses whose send
    comes back SendResult(ok=False) the way deliver() reports a failure."""
    box = {"sent": [], "fail": set(), "kw": []}

    def send(**kw):
        box["kw"].append(kw)
        if kw["to_email"] in box["fail"]:
            return emails.SendResult(False, error="Resend 503", attempts=3)
        box["sent"].append(kw["employee_name"])
        return emails.SendResult(True)
    monkeypatch.setattr(emails, "send_staff_schedule_email", send)
    return box


# ── F2-1: a send that failed is not "sent" ──────────────────────────────────

def test_a_failed_send_result_is_not_reported_sent_and_nothing_publishes(db, mail):
    rid = _restaurant(db)
    hid = _save(db, rid, _csv())
    _contacts(db, rid, "Ana", "Bob")
    mail["fail"] = {"ana@x.com", "bob@x.com"}
    out, status = client_api._publish_schedule(rid, hid, actor={"username": "o"}, acknowledge=True)
    assert out["ok"] is False and out["sent"] == []
    assert sorted(f["employee_name"] for f in out["failed"]) == ["Ana", "Bob"]
    assert not _one(db, "SELECT published_at FROM schedule_history WHERE id=?", hid)["published_at"]
    assert _one(db, "SELECT COUNT(*) AS n FROM schedule_shares WHERE schedule_id=?", hid)["n"] == 0


def test_a_partial_send_drops_the_failed_persons_share_row(db, mail):
    rid = _restaurant(db)
    hid = _save(db, rid, _csv())
    _contacts(db, rid, "Ana", "Bob")
    mail["fail"] = {"bob@x.com"}
    out, _ = client_api._publish_schedule(rid, hid, actor={"username": "o"}, acknowledge=True)
    assert [s["employee_name"] for s in out["sent"]] == ["Ana"]
    assert [f["employee_name"] for f in out["failed"]] == ["Bob"]
    shares = [r["employee_name"] for r in models.get_conn(db).execute(
        "SELECT employee_name FROM schedule_shares WHERE schedule_id=?", (hid,)).fetchall()]
    assert shares == ["Ana"]


# ── F2-15: staff read M/D/YY ────────────────────────────────────────────────

def test_the_week_label_staff_read_is_m_d_yy(db, mail):
    rid = _restaurant(db)
    hid = _save(db, rid, _csv())
    _contacts(db, rid, "Ana")
    out, _ = client_api._publish_schedule(rid, hid, actor={"username": "o"}, acknowledge=True)
    assert out["week_label"] == "10/5/26 – 10/9/26"
    assert mail["kw"][0]["week_label"] == "10/5/26 – 10/9/26"


# ── F2-10: the week is always named ─────────────────────────────────────────

def test_publish_without_a_schedule_id_is_refused_not_sent_to_the_newest_row(db, mail):
    rid = _restaurant(db)
    _save(db, rid, _csv())
    _contacts(db, rid, "Ana", "Bob")
    r = _app(db).test_client().post("/mobile/api/labor/publish-schedule", json={}, headers=_bearer(db, rid))
    assert r.status_code == 400 and "reload" in r.get_json()["error"].lower()
    assert mail["sent"] == []


# ── F2-9: an acknowledgement covers the list that was shown ────────────────

def test_an_acknowledgement_by_keys_does_not_cover_a_blocker_that_appeared_since(db, mail, monkeypatch):
    rid = _restaurant(db)
    hid = _save(db, rid, _csv())
    _contacts(db, rid, "Ana")
    shown = [{"key": "hours_over", "text": "10h over"}]
    now = shown + [{"key": "rule:x", "text": "Ana — Monday: a new breach"}]
    monkeypatch.setattr(client_api, "publish_review", lambda *a, **k: {"blockers": now, "soft": [], "schedule_id": hid})
    r = _app(db).test_client().post("/mobile/api/labor/publish-schedule",
                                    json={"schedule_id": hid, "acknowledge": ["hours_over"]}, headers=_bearer(db, rid))
    body = r.get_json()
    assert r.status_code == 409 and body["new_blockers"] == ["Ana — Monday: a new breach"]
    assert [b["key"] for b in body["blocker_items"]] == ["hours_over", "rule:x"]
    assert mail["sent"] == []
    r = _app(db).test_client().post("/mobile/api/labor/publish-schedule",
                                    json={"schedule_id": hid, "acknowledge": ["hours_over", "rule:x"]},
                                    headers=_bearer(db, rid, name="owner2"))
    assert r.status_code == 200 and r.get_json()["ok"] and mail["sent"] == ["Ana"]


def test_publish_check_returns_each_blocker_with_its_key(db, monkeypatch):
    import strategy_routes
    rid = _restaurant(db)
    hid = _save(db, rid, _csv())
    monkeypatch.setattr(client_api, "publish_review",
                        lambda *a, **k: {"blockers": [{"key": "hours_over", "text": "10h over"}], "soft": [],
                                         "schedule_id": hid})
    app = Flask(__name__)
    with app.test_request_context(f"/?schedule_id={hid}"):
        out, status = strategy_routes._do_publish_check({"restaurant_id": rid, "is_admin": True})
    assert status == 200 and out["blocker_items"] == [{"key": "hours_over", "text": "10h over"}]


# ── F2-2: a save of a sent week tells nobody; Send tells the people it moved ─

def _post_save(app, headers, rows, **body):
    return app.test_client().post("/mobile/api/labor/schedule/score", headers=headers,
                                  json={"rows": rows, "save": True, **body})


def _edited_rows():
    rows = sv.rows_from_csv(_csv())
    rows[0]["shift_start"] = "12:00pm"          # Ana's Monday
    return rows


def test_a_members_save_of_a_sent_week_emails_nobody(db, mail):
    rid = _restaurant(db)
    hid = _save(db, rid, _csv(), published=True)
    _contacts(db, rid, "Ana", "Bob")
    app = _app(db)
    member = _bearer(db, rid, name="teammate", role="member")
    v = sv.list_versions(rid, hid)[-1]["version"]
    r = _post_save(app, member, _edited_rows(), history_id=hid, version=v)
    assert r.status_code == 200 and r.get_json()["saved"]
    assert r.get_json()["unsent_changes"] == ["Ana"]
    assert mail["sent"] == []
    # ... and the member cannot send them: SCHEDULE_PUBLISH is withheld.
    r = app.test_client().post("/mobile/api/labor/publish-schedule", json={"schedule_id": hid}, headers=member)
    assert r.status_code == 403 and mail["sent"] == []


def test_send_tells_only_the_changed_people_once_and_then_nothing_is_pending(db, mail):
    rid = _restaurant(db)
    hid = _save(db, rid, _csv(), published=True)
    _contacts(db, rid, "Ana", "Bob")
    app, owner = _app(db), _bearer(db, rid)
    v = sv.list_versions(rid, hid)[-1]["version"]
    assert _post_save(app, owner, _edited_rows(), history_id=hid, version=v).status_code == 200
    assert _post_save(app, owner, _edited_rows(), history_id=hid, version=v + 1).status_code == 200
    assert mail["sent"] == []
    r = app.test_client().post("/mobile/api/labor/publish-schedule", json={"schedule_id": hid, "acknowledge": True},
                               headers=owner)
    body = r.get_json()
    assert r.status_code == 200 and body["ok"] and body["changes_sent"] is True
    assert mail["sent"] == ["Ana"] and "(updated)" in mail["kw"][0]["week_label"]
    assert sv.unsent_changes(rid, hid)["people"] == []
    # A second press sends nothing again.
    r = app.test_client().post("/mobile/api/labor/publish-schedule", json={"schedule_id": hid, "acknowledge": True},
                               headers=owner)
    assert r.get_json()["already_published"] is True and mail["sent"] == ["Ana"]


def test_sending_changes_runs_the_publish_gate(db, mail, monkeypatch):
    rid = _restaurant(db)
    hid = _save(db, rid, _csv(), published=True)
    _contacts(db, rid, "Ana", "Bob")
    app, owner = _app(db), _bearer(db, rid)
    v = sv.list_versions(rid, hid)[-1]["version"]
    _post_save(app, owner, _edited_rows(), history_id=hid, version=v)
    monkeypatch.setattr(client_api, "publish_review",
                        lambda *a, **k: {"blockers": [{"key": "rule:x", "text": "Ana — Monday: under 18 past 11pm"}],
                                         "soft": [], "schedule_id": hid})
    r = app.test_client().post("/mobile/api/labor/publish-schedule", json={"schedule_id": hid}, headers=owner)
    assert r.status_code == 409 and r.get_json()["needs_ack"] and mail["sent"] == []


def test_sending_changes_honours_the_owners_undo_window(db, mail):
    rid = _restaurant(db, send_delay_minutes=10)
    hid = _save(db, rid, _csv(), published=True)
    _contacts(db, rid, "Ana", "Bob")
    app, owner = _app(db), _bearer(db, rid)
    v = sv.list_versions(rid, hid)[-1]["version"]
    _post_save(app, owner, _edited_rows(), history_id=hid, version=v)
    r = app.test_client().post("/mobile/api/labor/publish-schedule", json={"schedule_id": hid, "acknowledge": True},
                               headers=owner)
    assert r.get_json()["queued"] is True and mail["sent"] == []
    assert [a["kind"] for a in delayed.pending(rid)] == ["schedule_changes_send"]
    delayed.run_due(now=dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=1))
    assert mail["sent"] == ["Ana"]


def test_an_edit_inside_the_undo_window_voids_the_queued_changes(db, mail):
    rid = _restaurant(db, send_delay_minutes=10)
    hid = _save(db, rid, _csv(), published=True)
    _contacts(db, rid, "Ana", "Bob")
    app, owner = _app(db), _bearer(db, rid)
    v = sv.list_versions(rid, hid)[-1]["version"]
    _post_save(app, owner, _edited_rows(), history_id=hid, version=v)
    app.test_client().post("/mobile/api/labor/publish-schedule", json={"schedule_id": hid, "acknowledge": True},
                           headers=owner)
    rows = _edited_rows()
    rows[1]["shift_start"] = "6:00pm"            # Bob's Monday, after Send was pressed
    _post_save(app, owner, rows, history_id=hid, version=v + 1)
    delayed.run_due(now=dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=1))
    assert mail["sent"] == []


def test_a_cover_already_told_is_not_sent_again_on_the_next_edit(db):
    rid = _restaurant(db)
    text = _csv()
    hid = _save(db, rid, text, published=True)
    # A cover: Cy takes Bob's Monday; shift_requests told both of them.
    covered = text.replace(f"{W1[0]},{_day(W1[0])},Bob,", f"{W1[0]},{_day(W1[0])},Cy,")
    sv.append(rid, hid, "swap", covered, saved_by="Cy")
    # Then the manager moves Ana's Monday.
    edited = covered.replace(f"{W1[0]},{_day(W1[0])},Ana,Server,11:00am", f"{W1[0]},{_day(W1[0])},Ana,Server,12:00pm")
    assert sv.unsent_changes(rid, hid, edited)["people"] == ["Ana"]


# ── F2-3: account hours and closures are the owner's ────────────────────────

def test_a_manager_cannot_rewrite_hours_or_closures(db):
    rid = _restaurant(db)
    app = _app(db)
    r = app.test_client().post("/mobile/api/account/hours",
                               json={"closures": [{"date": "2026-12-25", "reason": "x"}]},
                               headers=_bearer(db, rid, name="mgr", role="manager"))
    assert r.status_code == 403
    assert "2026-12-25" not in (schedule_rules.closures(models.get_restaurant(rid))["closed_dates"] or [])
    ok = app.test_client().get("/mobile/api/account", headers=_bearer(db, rid, name="mgr2", role="manager"))
    assert ok.status_code == 200 and ok.get_json()["profile"]["hours_can_edit"] is False
    owner = app.test_client().post("/mobile/api/account/hours", json={"closures": ["2026-12-25"]},
                                   headers=_bearer(db, rid))
    assert owner.status_code == 200 and owner.get_json()["closures"] == ["2026-12-25"]
