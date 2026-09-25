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
    assert body["blocker_keys"] == ["hours_over", "rule:x"]
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
    assert out["blocker_keys"] == ["hours_over"] and out["blockers"] == ["10h over"]


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


# ── helpers for the handler-level tests ─────────────────────────────────────

def _call(fn, user, *args, body=None, query=""):
    app = Flask(__name__)
    with app.test_request_context("/" + query, method="POST" if body is not None else "GET", json=body):
        return fn(user, *args)


def _user(rid, role="client", uid=1, **kw):
    return dict({"id": uid, "restaurant_id": rid, "role": role, "is_admin": False, "username": role}, **kw)


@pytest.fixture
def staff_out(monkeypatch):
    """Every channel people.tell uses, recorded; nothing leaves."""
    out = {"push": [], "sms": [], "email": []}
    monkeypatch.setattr(push, "fire_push", lambda rid, t, title, body, data=None, user_ids=None, **k:
                        out["push"].append((t, list(user_ids or []), body)) or 1)
    monkeypatch.setattr(notify, "send_sms", lambda to, msg, use_case="alert": out["sms"].append((to, msg)) or True)
    monkeypatch.setattr(emails, "deliver", lambda payload=None, restaurant_id=None, email_type=None, **k:
                        out["email"].append((payload["to"][0], payload["subject"], email_type)) or emails.SendResult(True))
    return out


# ── F2-5: a time-off decision reaches the person who asked ──────────────────

def test_a_time_off_decision_is_told_to_the_requester(db, staff_out):
    rid = _restaurant(db)
    _contacts(db, rid, "Ana")
    row, err = time_off.request_time_off(rid, "Ana", "2099-01-05", "2099-01-06", today=dt.date(2098, 12, 1))
    assert not err
    staff_out["email"].clear()
    time_off.decide(rid, row["id"], True, decided_by=1)
    assert [(to, et) for to, _s, et in staff_out["email"]] == [("ana@x.com", "time_off")]
    assert "approved" in staff_out["email"][0][1].lower()


# ── F2-6: a request reaches whoever can decide it, brief or not ─────────────

def test_a_request_reaches_a_decider_who_turned_the_brief_off_with_its_id(db, monkeypatch):
    rid = _restaurant(db)
    auth.init_auth(db_path=db)
    push.init_push(db_path=db)
    uid = auth.create_user(rid, "gm", "gm@x.com", "pw", db_path=db)
    conn = models.get_conn(db)
    conn.execute("UPDATE users SET role='manager' WHERE id=?", (uid,))
    conn.execute("INSERT INTO login_prefs (user_id, restaurant_id, morning_brief) VALUES (?,?,0)", (uid, rid))
    conn.commit()
    conn.close()
    assert not [u for u in __import__("morning_brief").recipients(rid, db) if u["id"] == uid]
    sent = []
    monkeypatch.setattr(emails, "deliver", lambda payload=None, **k: sent.append(payload["to"][0]) or emails.SendResult(True))
    pushed = []
    monkeypatch.setattr(push, "fire_push", lambda rid_, t, title, body, data=None, **k: pushed.append(data) or 1)
    row, err = time_off.request_time_off(rid, "Ana", "2099-01-05", "2099-01-05", today=dt.date(2098, 12, 1))
    assert not err
    assert "gm@x.com" in sent


def test_a_time_off_request_push_carries_its_id_for_approve_and_deny(db, monkeypatch):
    rid = _restaurant(db)
    got = []
    monkeypatch.setattr(strategy_jobs, "_reach", lambda r, t, title, body, data, dbp, **k: got.append((t, data, k)))
    row, _ = time_off.request_time_off(rid, "Ana", "2099-01-05", "2099-01-05", today=dt.date(2098, 12, 1))
    t, data, kw = got[0]
    assert t == "shift_request" and data["request_id"] == row["id"] and data["request_kind"] == "time_off"
    assert kw.get("deciders") is True
    assert push._category("shift_request", data) == push.CATEGORY_REQUEST
    assert push.nav_for("shift_request", data) == f"request/time_off-{row['id']}"


# ── F2-11: "schedule drafted" never goes to every phone ─────────────────────

def test_the_drafted_push_goes_to_nobody_when_no_publisher_gets_the_brief(db, monkeypatch):
    import ops
    rid = _restaurant(db)
    r = models.get_restaurant(rid)
    monkeypatch.setattr(ops, "start_async_job", lambda *a, **k: None)
    monkeypatch.setattr(ops, "read_async_job", lambda *a, **k: {"status": "done"})
    fired = []
    monkeypatch.setattr(push, "fire_push", lambda *a, **k: fired.append(k.get("user_ids")) or 1)
    import morning_brief
    monkeypatch.setattr(morning_brief, "recipients", lambda *a, **k: [{"id": 9, "role": "member", "grants": []}])

    class _SE:
        @staticmethod
        def _run_schedule_job(job_id, rid_):
            return None
    strategy_jobs._draft_one(r, db, _SE, lambda k: None)
    assert fired == []


# ── F2-12: shift-request outcomes use the person's own channel ──────────────

def test_a_shift_request_outcome_reaches_someone_with_no_email_by_text(db, staff_out, monkeypatch):
    rid = _restaurant(db)
    monkeypatch.setattr(people, "reach", lambda r, names, db_path=None: {
        n: {"push_user_id": None, "sms": "+15550001111", "email": None} for n in names})
    n = shift_requests._email_staff(rid, ["Ana"], "Your shift is off your schedule", ["Approved."], db)
    assert n == 1 and staff_out["sms"] and not staff_out["email"]


# ── F2-7: a count dated before a delivery does not erase it ─────────────────

def _ingredient(db_path, rid, name, cost=4.0):
    conn = models.get_conn(db_path)
    i = conn.execute("INSERT INTO ingredients (restaurant_id, name, unit, unit_cost, par_level, current_stock, "
                     "avg_daily_usage, is_active) VALUES (?,?,?,?,10,0,3,1)", (rid, name, "lb", cost)).lastrowid
    conn.commit()
    conn.close()
    return i


def test_a_count_entered_after_a_delivery_but_dated_before_it_keeps_the_delivery(db):
    import inventory_ledger as il
    rid = _restaurant(db)
    i = _ingredient(db, rid, "Beef")
    il.record_recount(rid, i, 5, event_date="2026-09-20")
    il.record_receiving(rid, i, 40, event_date="2026-09-23")
    out = il.record_recount(rid, i, 6, event_date="2026-09-22")
    conn = models.get_conn(db)
    try:
        assert il._compute_current_stock(conn, i, rid) == 46
    finally:
        conn.close()
    # ... and the late count was compared with the stock ON its date (5), not today's 45.
    assert out["inferred_waste_qty"] == 0


def test_the_count_sheet_asks_about_a_delivery_posted_after_it_opened(db):
    import inventory_ledger as il
    import strategy_routes
    rid = _restaurant(db)
    i = _ingredient(db, rid, "Beef")
    il.record_recount(rid, i, 5, event_date="2026-09-20")
    owner = _user(rid)
    sheet, _ = _call(strategy_routes._do_count_sheet_get, owner)
    il.record_receiving(rid, i, 40)                                    # the truck, after the sheet opened
    body = {"items": [{"ingredient_id": i, "counted": 6}], "ledger_mark": sheet["ledger_mark"]}
    out, status = _call(strategy_routes._do_count_sheet_save, owner, body=body)
    assert status == 409 and out["needs_confirm"] and out["deliveries"][0]["qty"] == 40
    out, status = _call(strategy_routes._do_count_sheet_save, owner, body=dict(body, deliveries="after"))
    assert status == 200
    conn = models.get_conn(db)
    try:
        assert il._compute_current_stock(conn, i, rid) == 46
    finally:
        conn.close()


# ── F2-8: an 86 matches whole words and acts once ───────────────────────────

def test_an_86_of_egg_does_not_zero_eggplant():
    import closeout
    ings = [{"id": 1, "name": "Eggplant"}, {"id": 2, "name": "Boiled Peanuts"}]
    assert closeout._match_ingredient("egg", ings) is None
    assert closeout._match_ingredient("86'd oil", ings) is None
    assert closeout._match_ingredient("eggs", ings + [{"id": 3, "name": "Eggs"}])["id"] == 3
    assert closeout._match_ingredient("chicken breast", [{"id": 4, "name": "Chicken"},
                                                         {"id": 5, "name": "Chicken Breast"}])["id"] == 5
    assert closeout._match_ingredient("chicken", [{"id": 6, "name": "Chicken Thighs"},
                                                  {"id": 7, "name": "Chicken Stock"}]) is None


def test_re_filing_a_close_out_does_not_zero_the_86_again(db, monkeypatch):
    import closeout
    import inventory_ledger as il
    rid = _restaurant(db, module_inventory=1)
    i = _ingredient(db, rid, "Eggs")
    counts = []
    monkeypatch.setattr(il, "record_recount", lambda *a, **k: counts.append(a[1]) or {"recount_id": 1})
    r = models.get_restaurant(rid)
    fields = {k: "" for k in closeout.FIELDS}
    closeout.save(rid, dict(fields, eighty_sixed="eggs"), business_date="2026-09-24", restaurant=r)
    closeout.save(rid, dict(fields, eighty_sixed="eggs", shift_notes="fixed a typo"),
                  business_date="2026-09-24", restaurant=r)
    assert counts == [i]


# ── F2-13: a refused PIN writes nothing; two alike names are two people ─────

def test_a_refused_pin_leaves_the_other_fields_unwritten(db):
    rid = _restaurant(db)
    with pytest.raises(people.PersonError):
        people.update_person(rid, "ana", {"phone": "+15551234567", "pin": "1234"}, may_manage_logins=False)
    assert not [c for c in models.get_staff_contacts(rid) if c["employee_name"] == "Ana" and c.get("phone")]


def test_two_names_that_slug_alike_each_open_their_own_record(db):
    rid = _restaurant(db, people_=("Jo-Ann", "Jo Ann"))
    rows = people.list_people(rid)
    keys = [p["key"] for p in rows]
    assert len(set(keys)) == 2
    for p in rows:
        assert people.find(rid, p["key"])["name"] == p["name"]
    # The bare slug, which names neither exactly, is refused — never the first.
    with pytest.raises(people.AmbiguousPerson):
        people.find(rid, "jo_ann")


# ── F3-5: role and pay rate are refused in words, pay rate read as a number ─

def test_a_shift_request_decision_is_logged_in_m_d_yy(db, monkeypatch):
    import strategy_routes
    rid = _restaurant(db)
    monkeypatch.setattr(shift_requests, "decide", lambda *a, **k: {
        "employee_name": "Ana", "date": "2026-09-26", "shift_start": "5:00pm", "status": "open"})
    logged = []
    monkeypatch.setattr(client_api, "log_account_event", lambda r, t, current_user=None, detail=None, **k:
                        logged.append(detail))
    out, status = _call(strategy_routes._do_shift_request_decide, _user(rid), 7, body={"decision": "approve"})
    assert status == 200 and logged == ["Ana 9/26/26 5:00pm: open"]


def test_a_changed_role_or_pay_rate_is_refused_not_saved_silently(db):
    import strategy_routes
    rid = _restaurant(db)
    owner = _user(rid)
    got, status = _call(strategy_routes._do_person_get, owner, "ana")
    p = got["person"]
    assert status == 200 and p["editable"]["role"] is False and p["editable"]["pay_rate"] is False
    assert "pay_rate_amount" in p
    out, status = _call(strategy_routes._do_person_set, owner, "ana", body={"role": "Bartender"})
    assert status == 400 and "role" in out["error"].lower()
    out, status = _call(strategy_routes._do_person_set, owner, "ana", body={"pay_rate": 99})
    assert status == 400 and "rate" in out["error"].lower()
    # The same values echoed back with a real change beside them save.
    out, status = _call(strategy_routes._do_person_set, owner, "ana",
                        body={"role": p["role"], "pay_rate": p["pay_rate"], "email": "ana@new.com"})
    assert status == 200 and out["changed"] == ["email"]


# ── F2-14: switches read "false" as off; ordering is the owner's ────────────

def test_the_automation_switches_read_false_as_off_and_auto_order_is_the_owners(db):
    import strategy_routes
    rid = _restaurant(db, auto_draft_schedule=1)
    out, status = _call(strategy_routes._do_auto_draft_set, _user(rid), body={"enabled": "false"})
    assert status == 200 and out["enabled"] is False
    out, status = _call(strategy_routes._do_auto_order_set, _user(rid, role="manager", grants=["food_cost.view"]),
                        body={"enabled": True})
    assert status == 403
    out, status = _call(strategy_routes._do_auto_order_set, _user(rid), body={"enabled": "false"})
    assert status == 200 and out["enabled"] is False


# ── F2-18: a receiving that fails partway can be retried ────────────────────

def test_a_po_receive_that_fails_partway_reopens_and_posts_only_whats_missing(db, monkeypatch):
    import inventory_ledger as il
    rid = _restaurant(db)
    a, b = _ingredient(db, rid, "Beef"), _ingredient(db, rid, "Kale")
    models.record_purchase_order(rid, "Sysco", "s@x.com", [{"item": "Beef", "qty": 4, "ingredient_id": a},
                                                           {"item": "Kale", "qty": 2, "ingredient_id": b}], 40,
                                 db_path=db)
    po = models.get_conn(db).execute("SELECT id FROM purchase_orders WHERE restaurant_id=?", (rid,)).fetchone()["id"]
    real = il.record_receiving
    calls = {"n": 0}

    def flaky(*a_, **k):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("database is locked")
        return real(*a_, **k)
    monkeypatch.setattr(il, "record_receiving", flaky)
    out, status = client_api._do_receive_po(_user(rid), po, {})
    assert status == 503 and out["retry"] is True
    assert _one(db, "SELECT status FROM purchase_orders WHERE id=?", po)["status"] == "sent"
    out, status = client_api._do_receive_po(_user(rid), po, {})
    assert status == 200
    n = _one(db, "SELECT COUNT(*) AS n FROM ingredient_stock_events WHERE restaurant_id=? AND event_type='receiving'", rid)["n"]
    assert n == 2, "each line went into stock once"


# ── F2-19: a prefilled night takes one source ───────────────────────────────

def test_a_forecast_prefill_names_gross_to_clear(db, monkeypatch):
    from dsr import store
    import demand
    rid = _restaurant(db)
    mon = dt.date(2026, 9, 21)
    monkeypatch.setattr(demand, "week_projection", lambda r, dates, db_path=None: {"by_day": {mon.isoformat(): 3000}})
    out = store.budget_prefill(rid, [mon], "forecast", db_path=db)
    assert out["days"][0]["clear"] == ["gross"] and "Gross left blank" in out["basis"]


# ── F2-20: a submitted target is the owner's; pay is not every login's ─────

def test_confirming_a_seeded_target_marks_it_set(db):
    import strategy_routes
    rid = _restaurant(db)
    models.update_restaurant(rid, {"labor_target_pct": 30, "labor_target_source": "seeded"})
    out, status = _call(strategy_routes._do_targets_set, _user(rid), body={"labor_target_pct": 30})
    assert status == 200 and out["targets"]["sources"]["labor_target_pct"] == "set"


def test_a_teammate_login_does_not_see_pay_rates(db):
    import strategy_routes
    rid = _restaurant(db)
    models.update_restaurant(rid, {"role_rates_json": json.dumps({"Server": 14.5}), "hourly_rate": 15})
    member = _user(rid, role="member")
    out, _ = _call(strategy_routes._do_targets_get, member)
    assert out["targets"]["role_rates"] == {} and out["targets"]["hourly_rate"] is None
    p, _ = _call(strategy_routes._do_person_get, member, "ana")
    assert "pay_rate" not in p["person"]
    out, _ = _call(strategy_routes._do_targets_get, _user(rid, role="manager"))
    assert out["targets"]["role_rates"] == {"Server": 14.5}


# ── F3-3: the approve answer says whether the reply is live ─────────────────

def test_approve_says_when_google_is_not_connected(db, monkeypatch):
    import gmb
    rid = _restaurant(db)
    conn = models.get_conn(db)
    rv = conn.execute("INSERT INTO reviews (restaurant_id, platform, external_id, author, rating, text, fetched_at, "
                      "draft_response, response_status) VALUES (?,?,?,?,?,?,datetime('now'),?,?)",
                      (rid, "google", "g-1", "Pat", 5, "Great", "Thanks Pat!", "drafted")).lastrowid
    conn.commit()
    conn.close()
    monkeypatch.setattr(gmb, "is_connected", lambda r: False)
    out, status = client_api._do_approve(rv, rid, auto=False)
    assert status == 200 and out["ok"] and out["auto_posted"] is False
    assert out["post_status"] == "not_connected" and "isn't on Google" in out["post_note"]


# ── F3-13: undoing a queued send takes the power to send it ─────────────────

def test_a_view_only_login_cannot_undo_the_owners_auto_publish(db):
    import strategy_routes
    rid = _restaurant(db)
    act = delayed.schedule(rid, "schedule_publish", {"schedule_id": 1, "automatic": True}, 60)
    out, status = _call(strategy_routes._do_delayed_cancel, _user(rid, role="member"), act["id"], body={})
    assert status == 403
    assert [a["id"] for a in delayed.pending(rid)] == [act["id"]]
    out, status = _call(strategy_routes._do_delayed_cancel, _user(rid, role="manager"), act["id"], body={})
    assert status == 200 and delayed.pending(rid) == []
