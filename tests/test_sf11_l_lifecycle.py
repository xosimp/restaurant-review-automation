"""Schedule fix round 10/11/26, workstream L: the week's life after the draft.

BUGSLIFE-1 (a regenerated draft no longer undoes approved covers), -3 (a push
Apple rejects is not "sent"), -4/-5 (a live cut sends only itself, through the
undo window, past its own late-change line), -8 (who has it, across the
week's copies), -9 (one queued send per week; Undo stops all of it), -11 (the
POS push after every live write), -14 (a queued send a newer draft replaced
tells the owner), -15 (a rename never voids a queued send), -19 (the pay
ledger keeps the version), -20 (cover asks text on the staff campaign only)
and STRESS-16 (the only manager's drop needs who runs the floor; the gate
reads the week as it will run). Nothing leaves the process: every sender is a
recorder.
"""
import datetime as dt
import json

import pytest

import delayed
import emails
import intraday
import labor_live
import models
import people
import push
import schedule_publish as sp
import schedule_versions as sv
import shift_requests as srq
from tests.publish_overrides import overrides_for
from tests.test_empfix_b3 import db, told, clock, _restaurant, _member, _coverage_issue  # noqa: F401 — fixtures

HEADER = "date,day,employee,role,shift_start,shift_end,scheduled_hours,notes"
W = ["2026-11-02", "2026-11-03", "2026-11-04", "2026-11-05", "2026-11-06", "2026-11-07", "2026-11-08"]
NOW = dt.datetime(2026, 10, 20, 9, 0)
OWNER = {"id": 1, "username": "owner", "role": "owner"}
BODY = [(W[0], "Ana B", "Server", "16:00", "22:00", 6), (W[1], "Bo C", "Cook", "08:00", "16:00", 8),
        (W[2], "Cy D", "Server", "16:00", "22:00", 6)]


@pytest.fixture
def mail(monkeypatch):
    """Everyone is reachable by email; each schedule email is recorded."""
    box = {"sent": []}
    monkeypatch.setattr(people, "reach", lambda rid, names, purpose=None, db_path=None: {
        n: {"push_user_id": None, "sms": None, "email": n.replace(" ", "").lower() + "@x.com"} for n in names})

    def send(**kw):
        box["sent"].append(kw["employee_name"])
        return emails.SendResult(True)
    monkeypatch.setattr(emails, "send_staff_schedule_email", send)
    return box


def _day(d):
    return dt.date.fromisoformat(d).strftime("%A")


def _text(rows):
    return HEADER + "\n" + "\n".join(f"{d},{_day(d)},{n},{r},{s},{e},{h}," for d, n, r, s, e, h in rows)


def _draft(db_path, rid, rows=BODY):
    hid = models.save_schedule_history(rid, W[0], W[-1], 20, 0, 30, _text(rows), [], db_path=db_path)
    conn = models.get_conn(db_path)
    try:
        text = conn.execute("SELECT schedule_csv FROM schedule_history WHERE id=?", (hid,)).fetchone()[0]
    finally:
        conn.close()
    sv.append(rid, hid, "generated", text, saved_by="Cavnar AI")
    return hid


def _publish(rid, hid, **kw):
    return sp.publish(rid, hid, OWNER, acknowledge=True, override_reasons=overrides_for(rid, hid), **kw)


def _one(db_path, sql, *args):
    conn = models.get_conn(db_path)
    try:
        r = conn.execute(sql, args).fetchone()
        return dict(r) if r else None
    finally:
        conn.close()


def _live(db_path, hid):
    return sv.rows_from_csv(_one(db_path, "SELECT published_csv FROM schedule_history WHERE id=?", hid)["published_csv"])


def _who(rows, day):
    return [r["employee"] for r in rows if r["date"] == day]


def _covered_week(db_path, rid):
    """Week A sent; Ana's Monday drop approved with Cy named on the copy
    staff have. Returns (A, request id)."""
    a = _draft(db_path, rid)
    assert _publish(rid, a)[1] == 200
    req = srq.request_drop(rid, "Ana B", W[0], "16:00", reason="sick", now=NOW)
    out = srq.decide(rid, req["id"], True, decided_by="owner", replacement="Cy D", now=NOW)
    assert out["status"] == "covered"
    return a, req["id"]


# ── BUGSLIFE-1 ──────────────────────────────────────────────────────────────

def test_a_cover_approved_after_the_draft_was_made_is_carried_into_it_and_sent(db, told, mail):
    rid = _restaurant(db)
    a = _draft(db, rid)
    _publish(rid, a)
    b = _draft(db, rid)                                    # regenerated before the cover
    req = srq.request_drop(rid, "Ana B", W[0], "16:00", reason="sick", now=NOW)
    srq.decide(rid, req["id"], True, decided_by="owner", replacement="Cy D", now=NOW)
    assert _who(sv.draft_rows(b, rid), W[0]) == ["Cy D"]   # the draft carries it
    assert any(v["reason"] == "swap" for v in sv.list_versions(rid, b))
    rv = sp.publish_review(rid, b)
    assert not [x for x in rv["blockers"] if x["key"].startswith("reverts:")]
    assert any(n["key"] == f"carried:{req['id']}" for n in rv["notes"])
    out, st = _publish(rid, b)
    assert st == 200 and _who(_live(db, b), W[0]) == ["Cy D"] and not out["staff_changes_undone"]
    assert _one(db, "SELECT status FROM shift_change_requests WHERE id=?", req["id"])["status"] == "covered"


def test_a_send_that_puts_the_dropper_back_is_a_must_fix_line_and_closes_the_request(db, told, mail):
    rid = _restaurant(db)
    a, req_id = _covered_week(db, rid)
    b = _draft(db, rid)                                    # made after the cover, Ana back on Monday
    rv = sp.publish_review(rid, b)
    line = next(x for x in rv["blockers"] if x["key"] == f"reverts:{req_id}")
    assert line["tier"] == sp.MUST_FIX and "Ana B is back on Monday" in line["text"]
    out, st = sp.publish(rid, b, OWNER, acknowledge=True,
                         override_reasons={k: v for k, v in overrides_for(rid, b).items() if k != line["key"]})
    assert st == 409 and out["needs_ack"]                  # a bare yes never covers it
    out, st = _publish(rid, b)                            # with its own reason
    assert st == 200 and out["staff_changes_undone"]
    r = _one(db, "SELECT status, decision_note, notice_pending FROM shift_change_requests WHERE id=?", req_id)
    assert r["status"] == "cancelled" and "changes this back" in r["decision_note"] and r["notice_pending"] is None
    assert any("Ana B" in x["to"] for x in told["staff"]) and any("Cy D" in x["to"] for x in told["staff"])
    assert not [e for e in srq.shift_release(rid, W[0]) if e["outcome"] == "covered"]


def test_a_changes_send_of_a_working_copy_that_undoes_a_cover_is_held_too():
    told_rows = [{"date": W[0], "employee": "Cy D", "shift_start": "16:00", "shift_end": "22:00"}]
    # Pure reading: covered request rows are read from the database, so this
    # checks only the span logic.
    assert srq._overlaps(told_rows, "Cy D", W[0], srq.shift_span(W[0], "17:00", "21:00"))
    assert not srq._overlaps(told_rows, "Cy D", W[0], srq.shift_span(W[0], "10:00", "15:00"))


# ── BUGSLIFE-3 ──────────────────────────────────────────────────────────────

def _push_reach(monkeypatch):
    monkeypatch.setattr(people, "reach", lambda rid, names, purpose=None, db_path=None: {
        n: {"push_user_id": 99, "sms": None, "email": None} for n in names})


def test_a_push_apple_rejects_with_nothing_else_on_file_is_failed_and_send_again_reaches_them(db, told, monkeypatch):
    _push_reach(monkeypatch)
    monkeypatch.setattr(push, "fire_push", lambda *a, on_failed=None, **k: (on_failed and on_failed()) or 1)
    rid = _restaurant(db)
    a = _draft(db, rid, BODY[:1])
    out, st = _publish(rid, a)
    n = _one(db, "SELECT status, delivery, last_error FROM schedule_notices")
    assert n["status"] == "failed" and n["delivery"] is None and "didn't reach their phone" in n["last_error"]
    out2, _ = sp.resend(rid, a, OWNER, scope="missing")
    assert not out2.get("nothing_to_send")


def test_a_late_apple_answer_lands_on_the_notice(db, told, monkeypatch):
    _push_reach(monkeypatch)
    hooks = []
    monkeypatch.setattr(push, "fire_push", lambda *a, on_delivered=None, on_failed=None, **k:
                        hooks.append((on_delivered, on_failed)) or 1)
    rid = _restaurant(db)
    a = _draft(db, rid, BODY[:1])
    _publish(rid, a)
    assert _one(db, "SELECT status, delivery FROM schedule_notices") == {"status": "sent", "delivery": "pending"}
    # A push still waiting on Apple is not "has it".
    assert not sp.resend(rid, a, OWNER, scope="missing")[0].get("nothing_to_send")
    hooks[0][1]()                                          # Apple: BadDeviceToken
    assert _one(db, "SELECT status FROM schedule_notices WHERE id=1")["status"] == "failed"
    # Delivered: the final word, and the app's share row.
    hooks[-1][0]()
    rows = [dict(r) for r in models.get_conn(db).execute("SELECT status, delivery FROM schedule_notices "
                                                         "ORDER BY id").fetchall()]
    assert rows[-1] == {"status": "sent", "delivery": "delivered"}


# ── BUGSLIFE-8 ──────────────────────────────────────────────────────────────

def test_a_replacement_reads_who_has_it_across_the_weeks_copies(db, told, mail):
    rid = _restaurant(db)
    a = _draft(db, rid)
    _publish(rid, a)
    b = _draft(db, rid, [BODY[0], (W[1], "Bo C", "Cook", "09:00", "17:00", 8), BODY[2]])
    mail["sent"].clear()
    _publish(rid, b)
    assert mail["sent"] == ["Bo C"]
    st = {p["employee_name"]: p for p in sp.notice_status(rid, b)["people"]}
    assert set(st) == {"Ana B", "Bo C", "Cy D"} and st["Ana B"]["carried"] and not st["Bo C"]["carried"]
    mail["sent"].clear()
    out, _ = sp.resend(rid, b, OWNER, scope="missing")
    assert out["nothing_to_send"] and mail["sent"] == []


# ── BUGSLIFE-4 / -5 ─────────────────────────────────────────────────────────

def _cut_setup(db, notice_days=None):
    rid = _restaurant(db)
    if notice_days:
        models.update_restaurant(rid, {"compliance_json": json.dumps({"notice_days": notice_days})})
    a = _draft(db, rid)
    _publish(rid, a)
    import shift_edits
    # The manager's unsent Studio edit of Bo's Tuesday.
    pl = shift_edits.plan(rid, "retime", {"date": W[1], "employee": "Bo C", "shift_start": "08:00",
                                          "new_start": "10:00", "new_end": "18:00"})
    assert shift_edits.save_rows(rid, a, pl["_rows"], pl["version"], user=OWNER)["ok"]
    # The live cut of Ana, saved the way labor_live.act saves it.
    pl = shift_edits.plan(rid, "retime", {"date": W[0], "employee": "Ana B", "shift_start": "16:00", "new_end": "20:00"})
    saved = shift_edits.save_rows(rid, a, pl["_rows"], pl["version"], user=OWNER, origin="cavnar:live_move")
    assert saved["ok"]
    return rid, a, pl, saved


def test_a_live_cut_sends_only_itself_under_a_notice_rule_and_keeps_the_premium_line(db, told, mail):
    rid, a, pl, saved = _cut_setup(db, notice_days=30)      # the week is inside the notice window
    mail["sent"].clear()
    p = {"date": W[0], "employee": "Ana B", "shift_start": "16:00"}
    out = labor_live._send_cut(rid, pl, p, saved, OWNER)
    assert out["ok"] and out["live_change_sent"] and mail["sent"] == ["Ana B"]
    assert out["late_change"] and "notice" in out["late_change"]["text"]
    live = _live(db, a)
    assert [r["shift_end"] for r in live if r["employee"] == "Ana B"] == ["8:00pm"]
    assert [r["shift_start"] for r in live if r["employee"] == "Bo C"] == ["08:00"]   # Bo's edit stays unsent
    assert sv.unsent_changes(rid, a)["people"] == ["Bo C"]


def test_a_live_cut_waits_out_the_undo_window_as_one_send(db, told, mail):
    rid, a, pl, saved = _cut_setup(db)
    models.update_restaurant(rid, {"send_delay_minutes": 60})
    p = {"date": W[0], "employee": "Ana B", "shift_start": "16:00"}
    mail["sent"].clear()
    out = labor_live._send_cut(rid, pl, p, saved, OWNER)
    assert out["queued"] and mail["sent"] == []
    row = _one(db, "SELECT kind, payload_json FROM delayed_actions WHERE id=?", out["action_id"])
    pay = json.loads(row["payload_json"])
    assert row["kind"] == "schedule_changes_send" and len(pay["only_shift_ids"]) == 1
    res = delayed._run_schedule_changes_send(rid, pay, db)
    assert res["ok"] and mail["sent"] == ["Ana B"]


# ── BUGSLIFE-9 ──────────────────────────────────────────────────────────────

def test_one_queued_send_per_week_and_undo_stops_all_of_it(db):
    rid = _restaurant(db)
    a = _draft(db, rid)
    first = delayed.queue_schedule_send(rid, "schedule_publish", {"schedule_id": a, "manual": True}, 60)
    again = delayed.queue_schedule_send(rid, "schedule_publish", {"schedule_id": a, "manual": True, "version": 2}, 60)
    assert again["id"] == first["id"] and again["replaced"] and again["payload"]["version"] == 2
    auto = delayed.queue_schedule_send(rid, "schedule_publish", {"schedule_id": a, "automatic": True}, 120,
                                       replace=False)
    assert auto["existing"] and auto["id"] == first["id"] and not auto["payload"].get("automatic")
    # An older row queued before the rule (two rows) — one Undo stops both.
    delayed.schedule(rid, "schedule_publish", {"schedule_id": a, "automatic": True}, 120)
    assert len(delayed.pending(rid)) == 2
    assert delayed.cancel(rid, first["id"], actor=OWNER)
    assert delayed.pending(rid) == []


# ── BUGSLIFE-11 ─────────────────────────────────────────────────────────────

def test_a_cover_queues_the_pos_push(db, told, mail, monkeypatch):
    import pos_schedule_push
    pushed = []
    monkeypatch.setattr(pos_schedule_push, "enqueue", lambda rid, hid, actor=None, reason="publish", db_path=None:
                        pushed.append((hid, reason)))
    rid = _restaurant(db)
    a, _req = _covered_week(db, rid)
    assert (a, "live_change") in pushed


# ── BUGSLIFE-14 ─────────────────────────────────────────────────────────────

def test_a_queued_send_a_newer_draft_replaced_tells_the_owner(db, monkeypatch):
    rid = _restaurant(db)
    a = _draft(db, rid)
    b = _draft(db, rid)
    said = []
    monkeypatch.setattr(delayed, "_tell_owner_send_voided", lambda r, p, d, changes=False, replaced_by=None:
                        said.append(replaced_by))
    out = delayed._run_schedule_publish(rid, {"schedule_id": a, "manual": True, "acknowledge": []}, db)
    assert out.get("replaced") and said == [b]


# ── BUGSLIFE-15 ─────────────────────────────────────────────────────────────

def test_a_rename_never_voids_a_queued_send(db):
    rid = _restaurant(db)
    a = _draft(db, rid)
    v = sv.list_versions(rid, a)[-1]["version"]
    conn = models.get_conn(db)
    sv._insert_version(conn, rid, a, "rename", _text([(W[0], "Ana Bell", "Server", "16:00", "22:00", 6)] + BODY[1:]),
                       None, "rename", "system")
    conn.commit()
    conn.close()
    assert sp.edited_since(rid, a, version=v) is False


# ── BUGSLIFE-19 ─────────────────────────────────────────────────────────────

def test_the_pay_ledger_rows_of_a_send_carry_its_version(db, told, mail, monkeypatch):
    import predictability_pay
    got = []
    monkeypatch.setattr(predictability_pay, "record_send", lambda *a, version=None, **k: got.append(("send", version)))
    monkeypatch.setattr(predictability_pay, "record_rest_premiums",
                        lambda *a, version=None, **k: got.append(("rest", version)))
    rid = _restaurant(db)
    a = _draft(db, rid)
    out, _ = _publish(rid, a)
    b = _draft(db, rid, BODY[:2])
    out2, _ = _publish(rid, b)
    assert ("rest", out["version"]) in got and ("send", out2["version"]) in got


# ── BUGSLIFE-20 ─────────────────────────────────────────────────────────────

def test_a_cover_ask_by_text_goes_on_the_staff_campaign_never_the_owner_alert(db, told, clock, monkeypatch):
    import notify
    alert = []
    monkeypatch.setattr(notify, "send_sms", lambda *a, **k: alert.append(a) or True)
    monkeypatch.setattr(notify, "textable", lambda *a, **k: True)
    asked = []
    monkeypatch.setattr(people, "reach", lambda rid, names, purpose=None, db_path=None: {
        n: {"push_user_id": None, "sms": "+15550001111" if purpose == "request" else None,
            "sms_scope": ["schedule", "request"], "email": None} for n in names})
    monkeypatch.setattr(people, "deliver", lambda rid, name, ch, **k: asked.append((name, k.get("purpose"))) or "sms")
    rid = _restaurant(db, [("Ana", "Server"), ("Cara", "Server")])
    conn = models.get_conn(db)
    conn.execute("INSERT INTO staff_contacts (restaurant_id, employee_name, phone) VALUES (?,?,?)",
                 (rid, "Cara", "+15550001111"))
    conn.commit()
    conn.close()
    hid = models.save_schedule_history(rid, "2026-10-05", "2026-10-11", 0, 0, 30,
                                       _text([("2026-10-06", "Ana", "Server", "5:00pm", "10:00pm", 5)]), [], db_path=db)
    conn = models.get_conn(db)
    conn.execute("UPDATE schedule_history SET published_at=datetime('now') WHERE id=?", (hid,))
    conn.commit()
    conn.close()
    clock["now"] = dt.datetime(2026, 10, 6, 17, 20)
    issue = _coverage_issue(db, rid, "2026-10-06")
    out = intraday.ask_to_cover(rid, issue["id"], "Cara", db_path=db)
    assert out["ok"] and out["via"] == "sms" and asked == [("Cara", "request")] and alert == []


# ── STRESS-16 ───────────────────────────────────────────────────────────────

def test_the_only_managers_drop_needs_who_runs_the_floor_and_the_gate_reads_the_gap(db, told, mail):
    import staff_settings
    rid = _restaurant(db, [("Max", "Manager"), ("Ana B", "Server")],
                      open_times_json=json.dumps({_day(W[1]): "11:00am"}),
                      close_times_json=json.dumps({_day(W[1]): "10:00pm"}))
    staff_settings.upsert(rid, "Max", floor_manager=True)
    rows = [(W[1], "Max", "Manager", "10:00", "22:30", 12.5), (W[1], "Ana B", "Server", "11:00", "22:00", 11)]
    a = _draft(db, rid, rows)
    _publish(rid, a)
    req = srq.request_drop(rid, "Max", W[1], "10:00", reason="sick", now=NOW)
    with pytest.raises(srq.ManagerGapRefused) as e:
        srq.decide(rid, req["id"], True, decided_by="owner", now=NOW)
    assert "no manager on" in e.value.gap
    assert _one(db, "SELECT status FROM shift_change_requests WHERE id=?", req["id"])["status"] == "pending"
    out = srq.decide(rid, req["id"], True, decided_by="owner", now=NOW, covered_by="Erik")
    assert out["status"] == "open" and out["floor_covered_by"] == "Erik" and "Erik runs the floor" in out["manager_gap"]
    rv = sp.publish_review(rid, a)
    assert any(b["key"].startswith("rule:no_manager") for b in rv["blockers"])
