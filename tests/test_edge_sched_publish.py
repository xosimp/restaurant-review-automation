"""Edge cases of publishing, the staff portal and schedule versions (SCHED audit).

What staff are shown after next week is published, a double shift in the
portal, publishing with no email or an email outage, two publishes at once,
a week published twice, deleting a draft that has versions, stale and
concurrent saves, blockers that should clear once a manager fixes the week,
and the Friday auto-publish picking the wrong week or an edited one.

xfail(strict=True) marks a confirmed defect, asserting the correct
behaviour; the marker comes off with the fix. Nothing is emailed: the send
function is replaced with a recorder, and the conftest guard blocks Resend.
"""
import datetime as dt
import json
import sqlite3
import sys
import threading

import pytest

# Imported here, before any fixture patches models.get_conn, so no module is
# first imported mid-test and left holding a redirect to a deleted database.
import activity, covers, decisions, delayed, demand_signals, goals, issues, metrics, outcomes  # noqa: E401,F401
import push, schedule_economics, schedule_intel, schedule_rules, schedule_versions  # noqa: E401,F401
import shift_requests, shift_quality, staff_schedule, staff_settings, strategy_jobs, time_off  # noqa: E401,F401
import labor_replacements  # noqa: F401
from flask import Flask

import auth
import client_api
import delayed
import emails
import labor
import mobile_api
import models
import schedule_engine as se
import schedule_rules as sr
import schedule_versions as sv
import scheduler
import shift_requests as srq
import staff_schedule
import strategy_jobs
from models import create_restaurant, Restaurant

HEADER = "date,day,employee,role,shift_start,shift_end,scheduled_hours,notes"
W1 = ["2026-10-05", "2026-10-06", "2026-10-07", "2026-10-08", "2026-10-09", "2026-10-10", "2026-10-11"]
W2 = ["2026-10-12", "2026-10-13", "2026-10-14", "2026-10-15", "2026-10-16", "2026-10-17", "2026-10-18"]
THU_W1 = dt.date(2026, 10, 8)


@pytest.fixture
def db(db_path, monkeypatch):
    real = models.get_conn

    def conn(*a, **k):
        return real(db_path)
    for mod in list(sys.modules.values()):
        bound = getattr(mod, "get_conn", None) if mod is not None else None
        # The real one, or a redirect some earlier test left bound in a
        # module it imported for the first time mid-test.
        if bound is real or str(getattr(bound, "__module__", "")).startswith(("test_", "tests.", "conftest")):
            monkeypatch.setattr(mod, "get_conn", conn)
    monkeypatch.setattr(models, "get_conn", conn)
    monkeypatch.setattr(models, "DB_PATH", db_path)
    monkeypatch.setattr(models, "_cached_shifts", lambda r: [])
    monkeypatch.setattr(client_api, "log_account_event", lambda *a, **k: None)
    return db_path


def _restaurant(db_path, people=("Ana", "Bob", "Cy"), **cols):
    rid = create_restaurant(Restaurant(name="Publish Co", owner_email="p@x.com"), db_path=db_path)
    if cols:
        conn = models.get_conn(db_path)
        conn.execute("UPDATE restaurants SET " + ", ".join(f"{k}=?" for k in cols) + " WHERE id=?", (*cols.values(), rid))
        conn.commit()
        conn.close()
    for n in people:
        models.add_manual_team_member(rid, n, role="Server", db_path=db_path)
    return rid


def _day(d):
    return dt.date.fromisoformat(d).strftime("%A")


def _week_csv(dates, people=(("Ana", "11:00am", "3:00pm", 4), ("Bob", "5:00pm", "9:00pm", 4))):
    return HEADER + "\n" + "\n".join(f"{d},{_day(d)},{n},Server,{s},{e},{h}," for d in dates for n, s, e, h in people)


def _save(db_path, rid, dates, text, published=False, hours=None, budget=0):
    hid = models.save_schedule_history(rid, dates[0], dates[-1], hours if hours is not None else 0, budget, 30,
                                       text, [], db_path=db_path)
    if published:
        conn = models.get_conn(db_path)
        conn.execute("UPDATE schedule_history SET published_at=datetime('now') WHERE id=?", (hid,))
        conn.commit()
        conn.close()
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


# ── SCHED-2: the portal after next week is published ─────────────────────

def test_the_portal_still_shows_the_rest_of_this_week_after_next_week_is_published(db):
    rid = _restaurant(db)
    _save(db, rid, W1, _week_csv(W1), published=True)
    _save(db, rid, W2, _week_csv(W2), published=True)
    out = staff_schedule.shifts_for_employee(rid, "Ana", today=THU_W1)
    assert out["today"] and out["today"]["start"] == "11:00am"
    by_date = {d["date"]: d for d in out["week"]}
    for d in W1[3:]:                                        # Thursday to Sunday of this week
        assert by_date[d]["off"] is False, d
    assert by_date[W2[0]]["off"] is False                    # and next Monday too


def test_the_portal_shows_this_week_when_it_is_the_only_published_one(db):
    rid = _restaurant(db)
    _save(db, rid, W1, _week_csv(W1), published=True)
    out = staff_schedule.shifts_for_employee(rid, "Ana", today=THU_W1)
    assert [d["off"] for d in out["week"][:4]] == [False, False, False, False]


def test_a_shift_this_week_can_still_be_dropped_after_next_week_is_published(db):
    rid = _restaurant(db)
    _save(db, rid, W1, _week_csv(W1), published=True)
    _save(db, rid, W2, _week_csv(W2), published=True)
    req = srq.request_drop(rid, "Ana", W1[4], "11:00am", today=THU_W1)
    assert req["status"] == "pending" and req["date"] == W1[4]


# ── SCHED-4: a double shift in the portal ─────────────────────────────────

def test_both_legs_of_a_double_appear_in_today_and_the_week(db):
    rid = _restaurant(db)
    text = (HEADER + f"\n{W1[0]},Monday,Ana,Server,10:30am,2:30pm,4,lunch\n{W1[0]},Monday,Ana,Server,5:00pm,10:00pm,5,dinner")
    _save(db, rid, W1, text, published=True)
    out = staff_schedule.shifts_for_employee(rid, "Ana", today=dt.date(2026, 10, 5))
    today_blob, week_blob = json.dumps(out["today"]), json.dumps(out["week"][0])
    assert "10:30am" in today_blob and "5:00pm" in today_blob
    assert "10:30am" in week_blob and "5:00pm" in week_blob


def test_a_double_already_lists_both_legs_in_upcoming(db):
    rid = _restaurant(db)
    text = (HEADER + f"\n{W1[0]},Monday,Ana,Server,10:30am,2:30pm,4,lunch\n{W1[0]},Monday,Ana,Server,5:00pm,10:00pm,5,dinner")
    _save(db, rid, W1, text, published=True)
    out = staff_schedule.shifts_for_employee(rid, "Ana", today=dt.date(2026, 10, 5))
    assert len(out["upcoming"]) == 2


# ── SCHED-9: publishing without email ─────────────────────────────────────

def test_publishing_with_no_email_contacts_still_publishes_to_the_portal(db, monkeypatch):
    rid = _restaurant(db)
    hid = _save(db, rid, W1, _week_csv(W1))
    monkeypatch.setattr(emails, "send_staff_schedule_email", lambda **kw: None)
    payload, status = client_api._publish_schedule(rid, hid, actor={"username": "owner"}, acknowledge=True)
    assert status == 200
    assert _one(db, "SELECT published_at FROM schedule_history WHERE id=?", hid)["published_at"]
    assert staff_schedule.shifts_for_employee(rid, "Ana", today=dt.date(2026, 10, 5))["published"] is True


def test_every_send_failing_leaves_no_share_row_and_says_the_send_failed(db, monkeypatch):
    rid = _restaurant(db)
    hid = _save(db, rid, W1, _week_csv(W1))
    _contacts(db, rid, "Ana", "Bob")

    def resend_down(**kw):
        raise RuntimeError("Resend 503")
    monkeypatch.setattr(emails, "send_staff_schedule_email", resend_down)
    payload, status = client_api._publish_schedule(rid, hid, actor={"username": "owner"}, acknowledge=True)
    shares = _one(db, "SELECT COUNT(*) AS n FROM schedule_shares WHERE schedule_id=?", hid)["n"]
    assert shares == 0
    assert "email address" not in (payload.get("error") or "")
    assert "fail" in (payload.get("error") or "").lower() or "could not" in (payload.get("error") or "").lower()


def test_a_successful_send_stamps_published_at_and_versions_it(db, monkeypatch):
    rid = _restaurant(db)
    hid = _save(db, rid, W1, _week_csv(W1))
    _contacts(db, rid, "Ana", "Bob")
    sent = []
    monkeypatch.setattr(emails, "send_staff_schedule_email", lambda **kw: sent.append(kw["employee_name"]))
    payload, status = client_api._publish_schedule(rid, hid, actor={"username": "owner"}, acknowledge=True)
    assert status == 200 and payload["ok"] and sorted(sent) == ["Ana", "Bob"]
    assert _one(db, "SELECT published_at FROM schedule_history WHERE id=?", hid)["published_at"]
    assert sv.list_versions(rid, hid)[-1]["reason"] == "published"


# ── SCHED-29: two publishes at once ───────────────────────────────────────

def test_two_concurrent_publishes_email_each_employee_once(db, monkeypatch):
    rid = _restaurant(db)
    hid = _save(db, rid, W1, _week_csv(W1))
    _contacts(db, rid, "Ana", "Bob")
    sent, lock = [], threading.Lock()

    def record(**kw):
        with lock:
            sent.append(kw["employee_name"])
    monkeypatch.setattr(emails, "send_staff_schedule_email", record)
    barrier = threading.Barrier(2)
    real_blockers = client_api.publish_blockers

    def racing_blockers(*a, **k):
        out = real_blockers(*a, **k)
        try:
            barrier.wait(timeout=30)
        except threading.BrokenBarrierError:
            pass
        return out
    monkeypatch.setattr(client_api, "publish_blockers", racing_blockers)
    threads = [threading.Thread(target=client_api._publish_schedule, args=(rid, hid),
                                kwargs={"actor": {"username": f"m{i}"}, "acknowledge": True}) for i in range(2)]
    [t.start() for t in threads]
    [t.join(60) for t in threads]
    if barrier.broken:
        pytest.skip("the race window was not staged (a thread missed the barrier under load)")
    assert sorted(sent) == ["Ana", "Bob"], sent


# ── SCHED-10: a week published twice ──────────────────────────────────────

def test_publishing_a_second_version_of_a_week_retires_the_first(db, monkeypatch):
    rid = _restaurant(db)
    _contacts(db, rid, "Ana", "Bob")
    monkeypatch.setattr(emails, "send_staff_schedule_email", lambda **kw: None)
    v1 = _save(db, rid, W1, _week_csv(W1))
    client_api._publish_schedule(rid, v1, actor={"username": "owner"}, acknowledge=True)
    v2 = _save(db, rid, W1, _week_csv(W1, people=(("Bob", "11:00am", "3:00pm", 4), ("Ana", "5:00pm", "9:00pm", 4))))
    client_api._publish_schedule(rid, v2, actor={"username": "owner"}, acknowledge=True)
    live = [r["id"] for r in models.get_conn(db).execute(
        "SELECT id FROM schedule_history WHERE restaurant_id=? AND week_start=? AND published_at IS NOT NULL "
        "AND superseded_by IS NULL", (rid, W1[0])).fetchall()]
    assert live == [v2]


# ── SCHED-16: deleting a draft that has versions ──────────────────────────

def _mobile_app(db):
    auth.init_auth(db_path=db)
    app = Flask(__name__, template_folder="../templates")
    app.register_blueprint(mobile_api.mobile_bp)
    return app


def _bearer(db, rid, name="owner", role="client"):
    uid = auth.create_user(rid, name, f"{name}@x.com", "pw", db_path=db)
    conn = models.get_conn(db)
    conn.execute("UPDATE users SET role=? WHERE id=?", (role, uid))
    conn.commit()
    conn.close()
    return {"Authorization": f"Bearer {auth.create_session(uid, db_path=db)}"}


def test_a_generated_draft_with_a_version_row_can_be_deleted(db):
    rid = _restaurant(db)
    hid = _save(db, rid, W1, _week_csv(W1))
    sv.append(rid, hid, "generated", _week_csv(W1), saved_by="Cavnar AI")
    assert models.delete_schedule_history(hid, rid) is True
    assert _one(db, "SELECT COUNT(*) AS n FROM schedule_history WHERE id=?", hid)["n"] == 0


def test_the_delete_route_removes_a_generated_draft(db):
    rid = _restaurant(db, module_labor=1)
    hid = _save(db, rid, W1, _week_csv(W1))
    sv.append(rid, hid, "generated", _week_csv(W1), saved_by="Cavnar AI")
    app = _mobile_app(db)
    r = app.test_client().delete(f"/mobile/api/labor/schedule-history/{hid}", headers=_bearer(db, rid))
    assert r.status_code == 200 and r.get_json()["ok"]


def test_a_draft_with_no_version_row_deletes_and_a_published_week_is_refused(db):
    rid = _restaurant(db, module_labor=1)
    draft = _save(db, rid, W1, _week_csv(W1))
    sent = _save(db, rid, W2, _week_csv(W2), published=True)
    app = _mobile_app(db)
    h = _bearer(db, rid)
    assert app.test_client().delete(f"/mobile/api/labor/schedule-history/{draft}", headers=h).status_code == 200
    assert app.test_client().delete(f"/mobile/api/labor/schedule-history/{sent}", headers=h).status_code == 400


# ── SCHED-19, 17, 18: saving an edited week ───────────────────────────────

def _rows(text):
    return sv.rows_from_csv(text)


@pytest.fixture
def save_app(db, monkeypatch):
    monkeypatch.setattr(labor, "analyse_shifts_for_restaurant", lambda r: {"is_live": True})
    return _mobile_app(db)


def _post_save(app, headers, rows, **body):
    return app.test_client().post("/mobile/api/labor/schedule/score", headers=headers,
                                  json={"rows": rows, "save": True, **body})


def test_two_saves_on_the_same_base_version_give_one_200_and_one_409(db, save_app, monkeypatch):
    rid = _restaurant(db, module_labor=1)
    hid = _save(db, rid, W1, _week_csv(W1))
    sv.append(rid, hid, "generated", _week_csv(W1), saved_by="Cavnar AI")
    headers = _bearer(db, rid)
    barrier = threading.Barrier(2)
    real_list = sv.list_versions

    def racing_list(*a, **k):
        out = real_list(*a, **k)
        try:
            barrier.wait(timeout=30)
        except threading.BrokenBarrierError:
            pass
        return out
    monkeypatch.setattr(sv, "list_versions", racing_list)
    edits = {"Cy": _rows(_week_csv(W1, people=(("Cy", "11:00am", "3:00pm", 4), ("Bob", "5:00pm", "9:00pm", 4)))),
             "Ana": _rows(_week_csv(W1, people=(("Ana", "11:00am", "2:00pm", 3), ("Bob", "5:00pm", "9:00pm", 4))))}
    statuses = {}

    def save(key):
        statuses[key] = _post_save(save_app, headers, edits[key], history_id=hid, version=1).status_code
    threads = [threading.Thread(target=save, args=(k,)) for k in edits]
    [t.start() for t in threads]
    [t.join(60) for t in threads]
    if barrier.broken:
        pytest.skip("the race window was not staged (a thread missed the barrier under load)")
    assert sorted(statuses.values()) == [200, 409], statuses
    winner = next(k for k, s in statuses.items() if s == 200)
    stored = _one(db, "SELECT schedule_csv FROM schedule_history WHERE id=?", hid)["schedule_csv"]
    assert _rows(stored) == edits[winner]


def test_a_save_behind_a_newer_version_is_refused_with_the_diff(db, save_app):
    rid = _restaurant(db, module_labor=1)
    hid = _save(db, rid, W1, _week_csv(W1))
    sv.append(rid, hid, "generated", _week_csv(W1), saved_by="Cavnar AI")
    sv.append(rid, hid, "edited", _week_csv(W1, people=(("Cy", "11:00am", "3:00pm", 4),)), saved_by="gm")
    r = _post_save(save_app, _bearer(db, rid), _rows(_week_csv(W1)), history_id=hid, version=1)
    assert r.status_code == 409 and r.get_json()["conflict"] and r.get_json()["latest_version"] == 2


def test_a_save_without_the_version_it_was_loaded_at_is_refused(db, save_app):
    rid = _restaurant(db, module_labor=1)
    hid = _save(db, rid, W1, _week_csv(W1))
    sv.append(rid, hid, "generated", _week_csv(W1), saved_by="Cavnar AI")
    sv.append(rid, hid, "edited", _week_csv(W1, people=(("Cy", "11:00am", "3:00pm", 4),)), saved_by="gm")
    r = _post_save(save_app, _bearer(db, rid), _rows(_week_csv(W1)), history_id=hid)
    assert r.status_code in (400, 409)


def test_a_save_without_a_history_id_never_writes_into_another_week(db, save_app):
    rid = _restaurant(db, module_labor=1)
    w1 = _save(db, rid, W1, _week_csv(W1))
    w2 = _save(db, rid, W2, _week_csv(W2))
    edited = _rows(_week_csv(W1, people=(("Cy", "11:00am", "3:00pm", 4),)))
    r = _post_save(save_app, _bearer(db, rid), edited)
    stored_w2 = _one(db, "SELECT schedule_csv FROM schedule_history WHERE id=?", w2)["schedule_csv"]
    assert r.status_code == 400 or stored_w2 == _week_csv(W2)
    assert stored_w2 == _week_csv(W2)


def test_a_save_whose_version_cannot_be_written_does_not_report_saved(db, save_app, monkeypatch):
    rid = _restaurant(db, module_labor=1)
    hid = _save(db, rid, W1, _week_csv(W1))
    sv.append(rid, hid, "generated", _week_csv(W1), saved_by="Cavnar AI")

    def lost_race(*a, **k):
        raise sqlite3.IntegrityError("UNIQUE constraint failed: schedule_versions.history_id, schedule_versions.version")
    monkeypatch.setattr(sv, "append", lost_race)
    edited = _rows(_week_csv(W1, people=(("Cy", "11:00am", "3:00pm", 4),)))
    r = _post_save(save_app, _bearer(db, rid), edited, history_id=hid, version=1)
    stored = _one(db, "SELECT schedule_csv FROM schedule_history WHERE id=?", hid)["schedule_csv"]
    latest = _one(db, "SELECT schedule_csv FROM schedule_versions WHERE history_id=? ORDER BY version DESC LIMIT 1", hid)
    assert r.status_code != 200 or stored == latest["schedule_csv"]


def test_an_edit_that_brings_the_week_under_budget_clears_the_budget_blocker(db, save_app):
    rid = _restaurant(db, module_labor=1)
    hid = _save(db, rid, W1, _week_csv(W1), hours=100, budget=50)
    assert any("over the ceiling" in b for b in client_api.publish_blockers(rid, hid))
    trimmed = _rows(_week_csv(W1[:3]))                           # 24h, well under the 50h budget
    assert _post_save(save_app, _bearer(db, rid), trimmed, history_id=hid, version=1).status_code == 200
    assert not any("over the ceiling" in b for b in client_api.publish_blockers(rid, hid))


def test_an_edit_that_takes_the_week_over_budget_raises_the_budget_blocker(db, save_app):
    rid = _restaurant(db, module_labor=1)
    hid = _save(db, rid, W1, _week_csv(W1[:1]), hours=8, budget=20)
    assert client_api.publish_blockers(rid, hid) == []
    assert _post_save(save_app, _bearer(db, rid), _rows(_week_csv(W1)), history_id=hid).status_code == 200
    assert any("over the ceiling" in b for b in client_api.publish_blockers(rid, hid))


def test_a_flagged_row_reassigned_legally_no_longer_blocks_publishing(db, save_app):
    rid = _restaurant(db, module_labor=1)
    flagged = HEADER + (f"\n{W1[0]},Monday,Ana,Server,11:00am,3:00pm,4,"
                        f"\n{W1[1]},Tuesday,Bob,Server,5:00pm,9:00pm,4,— NEEDS REVIEW: on approved time off")
    hid = _save(db, rid, W1, flagged)
    assert "1 shift marked NEEDS REVIEW" in client_api.publish_blockers(rid, hid)
    rows = _rows(flagged)
    rows[1]["employee"] = "Cy"                                   # the web fix: new name, notes untouched
    assert _post_save(save_app, _bearer(db, rid), rows, history_id=hid).status_code == 200
    assert not any("NEEDS REVIEW" in b for b in client_api.publish_blockers(rid, hid))


# ── SCHED-28: the Friday auto-publish ─────────────────────────────────────

@pytest.fixture
def friday(db, monkeypatch):
    import time_utils
    monkeypatch.setattr(time_utils, "restaurant_now", lambda *a, **k: dt.datetime(2026, 10, 2, 9, 30))
    monkeypatch.setattr(scheduler, "local_due", lambda *a, **k: True)
    monkeypatch.setattr(models, "schedule_publish_trust", lambda *a, **k: 99)
    told = []
    monkeypatch.setattr(strategy_jobs, "_reach", lambda *a, **k: told.append(a[1]))
    return told


def _auto_restaurant(db):
    return _restaurant(db, auto_publish_schedule=1, module_labor=1, billing_status="active")


def _queued(db, rid):
    conn = models.get_conn(db)
    try:
        return [json.loads(r["payload_json"]) for r in conn.execute(
            "SELECT payload_json FROM delayed_actions WHERE restaurant_id=? AND kind='schedule_publish'", (rid,)).fetchall()]
    finally:
        conn.close()


def test_auto_publish_queues_next_weeks_clean_draft(db, friday):
    rid = _auto_restaurant(db)
    hid = _save(db, rid, W1, _week_csv(W1))
    scheduler.run_auto_publish_schedules()
    assert _queued(db, rid) == [{"schedule_id": hid}] and "schedule_publish_pending" in friday


def test_auto_publish_queues_nothing_for_a_week_already_published(db, friday):
    rid = _auto_restaurant(db)
    sent = _save(db, rid, W1, _week_csv(W1), published=True)
    models.create_schedule_share(rid, sent, "Ana", sent_to="ana@x.com")
    _save(db, rid, W1, _week_csv(W1, people=(("Cy", "11:00am", "3:00pm", 4),)))    # regenerated to compare
    scheduler.run_auto_publish_schedules()
    assert _queued(db, rid) == []


def test_auto_publish_picks_next_weeks_draft_not_a_later_one(db, friday):
    rid = _auto_restaurant(db)
    nxt = _save(db, rid, W1, _week_csv(W1))
    _save(db, rid, W2, _week_csv(W2))                            # generated afterwards, a week further out
    scheduler.run_auto_publish_schedules()
    assert _queued(db, rid) == [{"schedule_id": nxt}]


def test_the_delayed_publish_is_voided_when_the_week_was_edited_in_the_window(db, friday, monkeypatch):
    rid = _auto_restaurant(db)
    _contacts(db, rid, "Ana", "Bob")
    hid = _save(db, rid, W1, _week_csv(W1))
    scheduler.run_auto_publish_schedules()
    assert _queued(db, rid)
    models.update_schedule_history_rows(rid, _week_csv(W1, people=(("Ana", "9:00am", "1:00pm", 4),)),
                                        history_id=hid, edited_by="half-finished edit")
    sent = []
    monkeypatch.setattr(emails, "send_staff_schedule_email", lambda **kw: sent.append(kw["employee_name"]))
    delayed.run_due(now=dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=3))
    assert sent == []
    assert not _one(db, "SELECT published_at FROM schedule_history WHERE id=?", hid)["published_at"]
