"""Schedule fix round 10/10/26, workstream D1: a schedule that is data, and
a publish that cannot lie.

ARCH-1, ARCH-6, ARCH-11, ARCH-15, ARCH-19, ARCH-20, ARCH-21, ARCH-29,
ARCH-31, ARCH-32, ARCH-33, BUGSLIFE-2, -3, -6, -7, -8, -9, -10, -11, -17,
-25 and SCALE-31. Every sender is replaced with a recorder; nothing leaves
the process.
"""
import datetime as dt
import inspect
import json
import sys

import pytest
from flask import Flask

import auth
import client_api
import delayed
import emails
import intraday
import mobile_api
import models
import people
import schedule_publish
import schedule_rows
import schedule_versions as sv
import staff_schedule
import strategy_jobs
from models import Restaurant, create_restaurant

HEADER = "date,day,employee,role,shift_start,shift_end,scheduled_hours,notes"
W1 = ["2026-10-05", "2026-10-06", "2026-10-07", "2026-10-08", "2026-10-09"]
MON = dt.date(2026, 10, 5)


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
    import labor
    monkeypatch.setattr(labor, "analyse_shifts_for_restaurant", lambda r, **k: {"is_live": True})
    # The gate is not what most of these are about; each test that is sets it.
    monkeypatch.setattr(schedule_publish, "publish_review",
                        lambda *a, **k: {"blockers": [], "soft": [], "notes": [], "hours": None,
                                         "schedule_id": a[1] if len(a) > 1 else None})
    return db_path


@pytest.fixture
def mail(monkeypatch):
    """Each staff schedule email: who, which week label, how many shifts;
    `fail` names addresses whose send comes back not ok."""
    box = {"sent": [], "fail": set(), "kw": []}

    def send(**kw):
        box["kw"].append(kw)
        if kw["to_email"] in box["fail"]:
            return emails.SendResult(False, error="Resend 503", attempts=3)
        box["sent"].append(kw["employee_name"])
        return emails.SendResult(True)
    monkeypatch.setattr(emails, "send_staff_schedule_email", send)
    return box


def _restaurant(db_path, names=("Ana", "Bob", "Cy"), **cols):
    rid = create_restaurant(Restaurant(name="Data Co", owner_email="o@x.com"), db_path=db_path)
    cols.setdefault("module_labor", 1)
    conn = models.get_conn(db_path)
    conn.execute("UPDATE restaurants SET " + ", ".join(f"{k}=?" for k in cols) + " WHERE id=?", (*cols.values(), rid))
    conn.commit()
    conn.close()
    for n in names:
        models.add_manual_team_member(rid, n, role="Server", db_path=db_path)
    return rid


def _day(d):
    return dt.date.fromisoformat(d).strftime("%A")


def _csv(people_=(("Ana", "5:00pm", "10:00pm", 5), ("Bob", "11:00am", "3:00pm", 4)), dates=W1, note=""):
    return HEADER + "\n" + "\n".join(f"{d},{_day(d)},{n},Server,{s},{e},{h},{note}"
                                     for d in dates for n, s, e, h in people_)


def _save(db_path, rid, text, published=False):
    hid = models.save_schedule_history(rid, W1[0], W1[-1], 0, 0, 30, text, [], db_path=db_path)
    sv.append(rid, hid, "generated", models.get_conn(db_path).execute(
        "SELECT schedule_csv FROM schedule_history WHERE id=?", (hid,)).fetchone()[0], saved_by="Cavnar AI")
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


def _publish(rid, hid, **kw):
    return client_api._publish_schedule(rid, hid, actor={"username": "owner"}, acknowledge=True, **kw)


def _save_rows(app, headers, hid, rows, version):
    return app.test_client().post("/mobile/api/labor/schedule/score", headers=headers,
                                  json={"rows": rows, "save": True, "history_id": hid, "version": version})


def _latest(rid, hid):
    return sv.list_versions(rid, hid)[-1]["version"]


def _ana_monday(rid, today=MON):
    week = staff_schedule.shifts_for_employee(rid, "Ana", today=today)
    return [(s.get("start"), s.get("end")) for s in week["week"][0]["shifts"]]


# ── ARCH-31 · the one row format ───────────────────────────────────────────

def test_a_note_with_commas_quotes_and_a_line_break_survives_whole():
    rows = schedule_rows.parse(_csv())
    rows[0]["notes"] = 'bring keys\nthen lock patio, back door — "the blue one"'
    back = schedule_rows.parse(schedule_rows.to_csv(rows))
    assert back[0]["notes"] == 'bring keys then lock patio, back door — "the blue one"'
    assert len(back) == len(rows)
    # Every stored text is one line per row, whatever the note held.
    assert len(schedule_rows.to_csv(rows).split("\n")) == len(rows) + 1
    # The model pipeline's internal text stays its own eight columns.
    assert schedule_rows.answer_line(rows[0]).count(",") == 7


def test_a_shift_keeps_its_id_by_key_slot_or_person_and_a_new_one_gets_a_new_id():
    prior = schedule_rows.carry_ids(schedule_rows.parse(_csv()), [])
    ids = [r["shift_id"] for r in prior]
    assert len(set(ids)) == len(ids) and all(schedule_rows.valid_id(i) for i in ids)
    edited = [dict(r) for r in prior]
    for r in edited:
        r.pop("shift_id")                                    # an older app sends none
    edited[0]["shift_end"] = "11:00pm"                       # Ana retimed: same person, same day
    edited[1]["employee"] = "Cy"                             # Bob's Monday shift given to Cy: same slot
    edited.append(dict(edited[2], shift_id="", date="2026-10-10", day="Saturday"))   # a new shift
    got = schedule_rows.carry_ids(edited, prior)
    assert [r["shift_id"] for r in got[:len(prior)]] == ids
    assert got[-1]["shift_id"] not in ids
    # A copied row never takes another shift's id; an id the week never had is replaced.
    dup = schedule_rows.carry_ids([dict(prior[0]), dict(prior[0]), dict(prior[1], shift_id="f" * 32)], prior)
    assert dup[0]["shift_id"] == ids[0] and dup[1]["shift_id"] != ids[0] and dup[2]["shift_id"] == ids[1]


def test_a_swap_is_carried_into_the_draft_three_way():
    live = schedule_rows.carry_ids(schedule_rows.parse(_csv(dates=W1[:1])), [])
    draft = [dict(r) for r in live]
    draft[1]["shift_start"] = "12:00pm"                      # the manager's unsent retime of Bob's shift
    new_live = [dict(r) for r in live]
    new_live[0]["employee"] = "Cy"                           # Ana's shift covered by Cy
    new_live[1]["shift_start"] = "10:00am"                   # and Bob's moved by the swap too
    merged = schedule_rows.merge_live_change(draft, live, new_live)
    assert merged[0]["employee"] == "Cy"                     # the cover reaches the draft
    assert merged[1]["shift_start"] == "12:00pm"             # the manager's unsent edit keeps its value


# ── ARCH-1, BUGSLIFE-3 · a save of a sent week reaches nobody before Send ──

def test_an_unsent_edit_never_reaches_the_portal_the_link_or_the_ops_checks(db, mail):
    rid = _restaurant(db)
    hid = _save(db, rid, _csv())
    _contacts(db, rid, "Ana", "Bob")
    assert _publish(rid, hid)[0]["ok"]
    assert _ana_monday(rid) == [("5:00pm", "10:00pm")]
    app, owner = _app(db), _bearer(db, rid)
    rows = sv.draft_rows(hid)
    for r in rows:
        if r["employee"] == "Ana":
            r["shift_start"], r["shift_end"] = "11:00am", "4:00pm"
    r = _save_rows(app, owner, hid, rows, _latest(rid, hid))
    assert r.status_code == 200 and r.get_json()["saved"] and r.get_json()["unsent_changes"] == ["Ana"]
    # Staff, the no-show / coverage reader and the share link still have what was sent.
    assert _ana_monday(rid) == [("5:00pm", "10:00pm")]
    ana_rows = [x for x in intraday.published_rows(rid, MON, db) if x["employee"] == "Ana"]
    assert [(x["shift_start"], x["shift_end"]) for x in ana_rows] == [("5:00pm", "10:00pm")]
    live = models.live_week_rows(rid, MON, db_path=db)
    assert {(x["employee"], x["shift_start"]) for x in live if x["date"] == W1[0]} == \
        {("Ana", "5:00pm"), ("Bob", "11:00am")}
    # The Studio has the edit; Send tells Ana and only then does the portal move.
    assert {(x["employee"], x["shift_start"]) for x in sv.draft_rows(hid) if x["date"] == W1[0]} == \
        {("Ana", "11:00am"), ("Bob", "11:00am")}
    del mail["sent"][:]
    out = app.test_client().post("/mobile/api/labor/publish-schedule", headers=owner,
                                 json={"schedule_id": hid, "acknowledge": True}).get_json()
    assert out["ok"] and out["changes_sent"] and mail["sent"] == ["Ana"]
    assert _ana_monday(rid) == [("11:00am", "4:00pm")]
    assert sv.unsent_changes(rid, hid)["people"] == []


def test_a_week_published_before_the_copies_were_split_is_frozen_on_its_first_edit(db, mail):
    """A legacy row (published_csv never set, not yet backfilled): the first
    save freezes what each person was told, so it still cannot reach them."""
    rid = _restaurant(db)
    hid = _save(db, rid, _csv())
    conn = models.get_conn(db)
    conn.execute("UPDATE schedule_history SET published_at=datetime('now'), published_csv=NULL WHERE id=?", (hid,))
    conn.commit()
    conn.close()
    sv.append(rid, hid, "published", _one(db, "SELECT schedule_csv FROM schedule_history WHERE id=?", hid)[0])
    rows = sv.draft_rows(hid)
    rows[0]["shift_start"] = "6:00pm"
    conn = models.get_conn(db)
    conn.execute("BEGIN IMMEDIATE")
    sv.write_on(conn, rid, hid, "edited", schedule_rows.to_csv(rows), saved_by="gm")
    conn.commit()
    conn.close()
    assert _ana_monday(rid) == [("5:00pm", "10:00pm")]
    assert sv.unsent_changes(rid, hid)["people"] == ["Ana"]


def test_the_boot_backfill_rebuilds_what_each_person_was_told(db):
    rid = _restaurant(db)
    hid = _save(db, rid, _csv())
    told = _one(db, "SELECT schedule_csv FROM schedule_history WHERE id=?", hid)[0]
    sv.append(rid, hid, "published", told)
    edited = told.replace("5:00pm,10:00pm", "6:00pm,10:00pm")     # saved before the split, never sent
    conn = models.get_conn(db)
    conn.execute("UPDATE schedule_history SET published_at=datetime('now'), published_csv=NULL, schedule_csv=? "
                 "WHERE id=?", (edited, hid))
    conn.commit()
    conn.close()
    sv.append(rid, hid, "edited", edited)
    assert sv.backfill_published_copies(db_path=db) == 1
    assert _ana_monday(rid) == [("5:00pm", "10:00pm")]
    assert sv.unsent_changes(rid, hid)["people"] == ["Ana"]
    pub = schedule_rows.parse(_one(db, "SELECT published_csv FROM schedule_history WHERE id=?", hid)[0])
    draft = sv.draft_rows(hid)
    assert {r["shift_id"] for r in pub} == {r["shift_id"] for r in draft}     # one set of ids
    assert sv.backfill_published_copies(db_path=db) == 0                     # idempotent


# ── ARCH-6, SCALE-31, contract #4 · ids, people, locks ──────────────────────

def test_a_shift_keeps_its_id_through_a_save_a_send_and_a_cover(db, mail):
    rid = _restaurant(db)
    hid = _save(db, rid, _csv())
    ids = {(r["date"], r["employee"]): r["shift_id"] for r in sv.draft_rows(hid)}
    assert len(ids) == 10 and all(schedule_rows.valid_id(i) for i in ids.values())
    _publish(rid, hid)
    app, owner = _app(db), _bearer(db, rid)
    rows = sv.draft_rows(hid)
    rows[0]["shift_end"] = "11:00pm"
    for r in rows:
        r.pop("shift_id")                                       # an older client sends no ids
    assert _save_rows(app, owner, hid, rows, _latest(rid, hid)).status_code == 200
    assert {(r["date"], r["employee"]): r["shift_id"] for r in sv.draft_rows(hid)} == ids
    # A cover on the published copy (the requests flow's write): Bob's Tuesday to Cy.
    live = models.live_week_rows(rid, MON, db_path=db)
    for r in live:
        if r["date"] == W1[1] and r["employee"] == "Bob":
            r["employee"] = "Cy"
    conn = models.get_conn(db)
    conn.execute("BEGIN IMMEDIATE")
    sv.write_on(conn, rid, hid, sv.LIVE_REASON, schedule_rows.to_csv(live), saved_by="swap")
    conn.commit()
    conn.close()
    cy = [r for r in models.live_week_rows(rid, MON, db_path=db) if r["employee"] == "Cy"]
    assert [r["shift_id"] for r in cy] == [ids[(W1[1], "Bob")]]
    # The draft took the cover and kept the manager's unsent retime.
    draft = {(r["date"], r["employee"]): r for r in sv.draft_rows(hid)}
    assert draft[(W1[1], "Cy")]["shift_id"] == ids[(W1[1], "Bob")]
    assert draft[(W1[0], "Ana")]["shift_end"] == "11:00pm"
    assert sv.unsent_changes(rid, hid)["people"] == ["Ana"]


def test_rows_carry_the_person_they_are(db):
    rid = _restaurant(db)
    pid = people.person_id_for(rid, "Ana")
    assert pid
    hid = _save(db, rid, _csv())
    conn = models.get_conn(db)
    conn.execute("BEGIN IMMEDIATE")
    sv.write_on(conn, rid, hid, "edited", _one(db, "SELECT schedule_csv FROM schedule_history WHERE id=?", hid)[0])
    conn.commit()
    conn.close()
    assert {r["person_id"] for r in sv.draft_rows(hid) if r["employee"] == "Ana"} == {str(pid)}
    assert people.person_ids(rid, ["Ana", "Nobody"]) == {"Ana": pid, "Nobody": None}


def test_a_row_the_manager_changes_locks_and_the_owner_can_unlock_it(db):
    rid = _restaurant(db)
    hid = _save(db, rid, _csv())
    app, owner = _app(db), _bearer(db, rid)
    rows = sv.draft_rows(hid)
    rows[0]["shift_start"] = "6:00pm"
    assert _save_rows(app, owner, hid, rows, _latest(rid, hid)).status_code == 200
    locked = {(r["date"], r["employee"]) for r in sv.draft_rows(hid) if schedule_rows.is_locked(r)}
    assert locked == {(W1[0], "Ana")}
    # The echoed `locked` is no instruction; the owner's `lock: false` is.
    rows = sv.draft_rows(hid)
    for r in rows:
        r["locked"] = False
    rows[1]["shift_end"] = "4:00pm"
    rows[0]["lock"] = False
    assert _save_rows(app, owner, hid, rows, _latest(rid, hid)).status_code == 200
    locked = {(r["date"], r["employee"]) for r in sv.draft_rows(hid) if schedule_rows.is_locked(r)}
    assert locked == {(W1[0], "Bob")}


def test_the_same_rows_saved_twice_write_one_version_and_teach_once(db, monkeypatch):
    import schedule_learning
    rid = _restaurant(db)
    hid = _save(db, rid, _csv())
    captured = []
    monkeypatch.setattr(schedule_learning, "capture_save", lambda *a, **k: captured.append(a) or {})
    app, owner = _app(db), _bearer(db, rid)
    rows = sv.draft_rows(hid)
    rows[0]["shift_start"] = "6:00pm"
    base = _latest(rid, hid)
    first = _save_rows(app, owner, hid, rows, base).get_json()
    again = _save_rows(app, owner, hid, rows, base).get_json()          # the phone's retry, same base
    assert first["saved"] and again["saved"] and again["unchanged"] is True
    assert again["version"] == first["version"] == base + 1 and _latest(rid, hid) == base + 1
    assert len(captured) == 1


# ── ARCH-11 · one definition of the week in force ──────────────────────────

def test_a_published_copy_is_replaced_only_by_a_newer_published_one(db, mail):
    """The BUGSLIFE-2 state (a published copy superseded by an unpublished
    draft) no longer hides the week staff have."""
    rid = _restaurant(db)
    x = _save(db, rid, _csv())
    _publish(rid, x)
    y = _save(db, rid, _csv(people_=(("Ana", "4:00pm", "10:00pm", 6),)))
    conn = models.get_conn(db)
    conn.execute("UPDATE schedule_history SET superseded_by=? WHERE id=?", (y, x))      # the old race's damage
    conn.commit()
    try:
        assert models.live_week_row(conn, rid, W1[0])["id"] == x
        assert models.week_draft(conn, rid, W1[0])["id"] == y
        assert models.newest_draft(conn, rid)["id"] == y
        live_ids = [r[0] for r in conn.execute("SELECT id FROM schedule_history h WHERE h.restaurant_id=? AND "
                                               + models.live_where("h"), (rid,)).fetchall()]
        assert live_ids == [x]
    finally:
        conn.close()
    assert _ana_monday(rid) == [("5:00pm", "10:00pm")]
    assert models.live_week_for(rid, MON, db_path=db)["id"] == x


def test_no_reader_asks_for_the_newest_row_of_schedule_history():
    import re
    import glob
    import os
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    bad = []
    for path in glob.glob(os.path.join(root, "*.py")):
        src = open(path).read()
        for m in re.finditer(r"FROM schedule_history\s+(?:h\s+)?WHERE\s+(?:h\.)?restaurant_id=\?\s*\"?\s*\"?\s*ORDER BY "
                             r"(?:h\.)?id DESC LIMIT 1", src):
            bad.append((os.path.basename(path), src[:m.start()].count("\n") + 1))
    assert bad == []


# ── BUGSLIFE-2 · a generation landing during a Send ────────────────────────

def test_a_generation_never_supersedes_the_copy_a_send_holds(db, mail):
    rid = _restaurant(db)
    _contacts(db, rid, "Ana", "Bob")
    x = _save(db, rid, _csv())
    landed = {}

    def gate(r, sid, **k):
        # The generation lands while the Send holds the claim... (it claims
        # after the gate; take the claim here as the Send would and land).
        conn = models.get_conn(db)
        conn.execute("UPDATE schedule_history SET publishing_at=datetime('now') WHERE id=?", (x,))
        conn.commit()
        conn.close()
        landed["y"] = models.save_schedule_history(rid, W1[0], W1[-1], 0, 0, 30,
                                                   _csv(people_=(("Ana", "4:00pm", "10:00pm", 6),)), [], db_path=db)
        conn = models.get_conn(db)
        conn.execute("UPDATE schedule_history SET publishing_at=NULL WHERE id=?", (x,))
        conn.commit()
        conn.close()
        return {"blockers": [], "soft": [], "notes": [], "hours": None, "schedule_id": sid}
    import schedule_publish as sp
    orig = sp.publish_review
    sp.publish_review = gate
    try:
        out, status = _publish(rid, x)
    finally:
        sp.publish_review = orig
    assert status == 200 and out["ok"]
    row = _one(db, "SELECT published_at, superseded_by FROM schedule_history WHERE id=?", x)
    assert row["published_at"] and row["superseded_by"] is None
    assert _ana_monday(rid) == [("5:00pm", "10:00pm")]                 # staff have the week they were told
    assert _one(db, "SELECT superseded_by FROM schedule_history WHERE id=?", landed["y"])["superseded_by"] is None


def test_generate_waits_while_a_send_holds_the_week(db):
    rid = _restaurant(db)
    x = _save(db, rid, _csv())
    conn = models.get_conn(db)
    conn.execute("UPDATE schedule_history SET publishing_at=datetime('now') WHERE id=?", (x,))
    conn.commit()
    conn.close()
    assert schedule_publish.send_in_progress(rid, week_start=W1[0]) is True
    assert schedule_publish.send_in_progress(rid, history_id=x) is True
    schedule_publish._release(rid, x)
    assert schedule_publish.send_in_progress(rid, week_start=W1[0]) is False


def test_a_send_that_ends_unpublished_lets_the_newer_draft_supersede_it(db):
    rid = _restaurant(db)
    x = _save(db, rid, _csv())
    conn = models.get_conn(db)
    conn.execute("UPDATE schedule_history SET publishing_at=datetime('now') WHERE id=?", (x,))
    conn.commit()
    conn.close()
    y = models.save_schedule_history(rid, W1[0], W1[-1], 0, 0, 30, _csv(), [], db_path=db)
    assert _one(db, "SELECT superseded_by FROM schedule_history WHERE id=?", x)["superseded_by"] is None
    schedule_publish._release(rid, x)
    assert _one(db, "SELECT superseded_by FROM schedule_history WHERE id=?", x)["superseded_by"] == y


# ── BUGSLIFE-6, ARCH-21 · a save during the Send ───────────────────────────

def _save_mid_gate(db, rid, hid, monkeypatch, times=1):
    """A gate during which manager B saves Ana -> Cy (once)."""
    done = []

    def gate(r, sid, **k):
        if len(done) < times:
            done.append(1)
            rows = sv.draft_rows(hid)
            for x in rows:
                if x["employee"] == "Ana":
                    x["employee"] = "Cy"
            conn = models.get_conn(db)
            conn.execute("BEGIN IMMEDIATE")
            sv.write_on(conn, rid, hid, "edited", schedule_rows.to_csv(rows), saved_by="b")
            conn.commit()
            conn.close()
        return {"blockers": [], "soft": [], "notes": [], "hours": None, "schedule_id": sid}
    monkeypatch.setattr(schedule_publish, "publish_review", gate)
    return done


def test_a_save_landing_while_the_send_is_checked_is_sent_as_saved_and_checked_again(db, mail, monkeypatch):
    rid = _restaurant(db)
    _contacts(db, rid, "Ana", "Bob", "Cy")
    hid = _save(db, rid, _csv())
    v0 = _latest(rid, hid)
    gated = _save_mid_gate(db, rid, hid, monkeypatch)
    out, status = _publish(rid, hid)
    # Never the rows read before the save: the saved week, gated again.
    assert status == 200 and out["ok"] and sorted(mail["sent"]) == ["Bob", "Cy"] and len(gated) == 1
    pub = schedule_rows.parse(_one(db, "SELECT published_csv FROM schedule_history WHERE id=?", hid)[0])
    assert {r["employee"] for r in pub} == {"Bob", "Cy"}
    assert sv.list_versions(rid, hid)[-1]["reason"] == "published"
    # The published version holds the saved rows: B's next save on their own
    # version is no conflict ("a revert nobody made").
    conn = models.get_conn(db)
    try:
        assert sv.rows_changed_since(conn, hid, v0 + 1) is None
    finally:
        conn.close()


def test_a_send_naming_its_version_is_refused_when_a_save_lands_mid_check(db, mail, monkeypatch):
    rid = _restaurant(db)
    _contacts(db, rid, "Ana", "Bob", "Cy")
    hid = _save(db, rid, _csv())
    _save_mid_gate(db, rid, hid, monkeypatch)
    out, status = _publish(rid, hid, version=_latest(rid, hid))
    assert status == 409 and out["stale"] and mail["sent"] == []
    row = _one(db, "SELECT published_at, publishing_at FROM schedule_history WHERE id=?", hid)
    assert row["published_at"] is None and row["publishing_at"] is None


# ── BUGSLIFE-11 · Send publishes the version the sender reviewed ───────────

def test_a_send_of_rows_somebody_changed_since_is_refused_with_their_change(db, mail):
    rid = _restaurant(db)
    _contacts(db, rid, "Ana", "Bob")
    hid = _save(db, rid, _csv())
    seen = _latest(rid, hid)
    app = _app(db)
    other = _bearer(db, rid, name="b")
    rows = sv.draft_rows(hid)
    rows = [r for r in rows if not (r["employee"] == "Bob" and r["date"] == W1[4])]   # B takes out the closer
    assert _save_rows(app, other, hid, rows, seen).status_code == 200
    owner = _bearer(db, rid)
    r = app.test_client().post("/mobile/api/labor/publish-schedule", headers=owner,
                               json={"schedule_id": hid, "acknowledge": True, "version": seen})
    body = r.get_json()
    assert r.status_code == 409 and body["stale"] and body["saved_by"] == "b" and body["lines"]
    assert mail["sent"] == []
    r = app.test_client().post("/mobile/api/labor/publish-schedule", headers=owner,
                               json={"schedule_id": hid, "acknowledge": True, "version": body["latest_version"]})
    assert r.status_code == 200 and sorted(mail["sent"]) == ["Ana", "Bob"]


# ── BUGSLIFE-7 · a replacement tells the changed and the removed, nobody else

def test_republishing_tells_the_people_taken_off_and_changed_and_not_the_rest(db, mail):
    rid = _restaurant(db, names=("Ana", "Bob", "Cy", "Dee"))
    _contacts(db, rid, "Ana", "Bob", "Cy", "Dee")
    x = _save(db, rid, _csv(people_=(("Ana", "5:00pm", "10:00pm", 5), ("Bob", "11:00am", "3:00pm", 4),
                                      ("Cy", "4:00pm", "9:00pm", 5))))
    _publish(rid, x)
    del mail["sent"][:], mail["kw"][:]
    y = _save(db, rid, _csv(people_=(("Ana", "5:00pm", "10:00pm", 5), ("Bob", "12:00pm", "4:00pm", 4),
                                      ("Dee", "4:00pm", "9:00pm", 5))))
    out, status = _publish(rid, y)
    assert status == 200 and out["ok"]
    assert sorted(mail["sent"]) == ["Bob", "Cy", "Dee"]                  # Ana is unchanged: nothing
    by = {k["employee_name"]: k for k in mail["kw"]}
    assert by["Cy"]["shifts"] == [] and "(updated)" in by["Cy"]["week_label"]
    assert "(updated)" in by["Bob"]["week_label"] and "(updated)" not in by["Dee"]["week_label"]
    kinds = {r["employee_name"]: r["kind"] for r in models.get_conn(db).execute(
        "SELECT employee_name, kind FROM schedule_notices WHERE history_id=?", (y,)).fetchall()}
    assert kinds == {"Bob": "changed", "Cy": "removed", "Dee": "posted"}
    assert _one(db, "SELECT superseded_by FROM schedule_history WHERE id=?", x)["superseded_by"] == y


# ── ARCH-19, ARCH-20, BUGSLIFE-9, BUGSLIFE-10 · the outbox ─────────────────

def test_the_stamp_comes_first_and_a_stamp_that_fails_tells_nobody(db, mail, monkeypatch):
    rid = _restaurant(db)
    _contacts(db, rid, "Ana", "Bob")
    hid = _save(db, rid, _csv())
    monkeypatch.setattr(sv, "_insert_version", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("database is locked")))
    with pytest.raises(RuntimeError):
        _publish(rid, hid)
    assert mail["sent"] == []
    row = _one(db, "SELECT published_at, published_csv, publishing_at FROM schedule_history WHERE id=?", hid)
    assert row["published_at"] is None and row["published_csv"] is None and row["publishing_at"] is None
    assert _one(db, "SELECT COUNT(*) FROM schedule_notices WHERE history_id=?", hid)[0] == 0


def test_a_big_send_returns_within_its_window_and_the_outbox_finishes_it(db, mail, monkeypatch):
    rid = _restaurant(db)
    _contacts(db, rid, "Ana", "Bob")
    hid = _save(db, rid, _csv())
    monkeypatch.setattr(schedule_publish, "INLINE_SECONDS", 0.0)
    out, status = _publish(rid, hid)
    assert status == 200 and out["ok"] and out["sent"] == [] and sorted(out["sending"]) == ["Ana", "Bob"]
    assert _one(db, "SELECT published_at FROM schedule_history WHERE id=?", hid)["published_at"]
    assert mail["sent"] == []
    got = schedule_publish.run_schedule_notices(max_seconds=30)
    assert got["ok"] == 2 and got["failed"] == 0 and sorted(mail["sent"]) == ["Ana", "Bob"]
    st = schedule_publish.notice_status(rid, hid)
    assert st["sending"] == 0 and {p["status"] for p in st["people"]} == {"sent"}


def test_a_failed_person_is_retried_never_counted_told_and_send_again_reaches_them(db, mail, monkeypatch):
    rid = _restaurant(db)
    _contacts(db, rid, "Ana", "Bob")
    hid = _save(db, rid, _csv())
    mail["fail"] = {"bob@x.com"}
    out, _ = _publish(rid, hid)
    assert [s["employee_name"] for s in out["sent"]] == ["Ana"] and [f["employee_name"] for f in out["failed"]] == ["Bob"]
    st = {p["employee_name"]: p for p in schedule_publish.notice_status(rid, hid)["people"]}
    assert st["Bob"]["status"] == "failed" and st["Bob"]["next_attempt_at"]
    # Not due yet: nothing goes; once due, it does.
    assert schedule_publish.run_schedule_notices()["attempted"] == 0
    conn = models.get_conn(db)
    conn.execute("UPDATE schedule_notices SET next_attempt_at=datetime('now','-1 minute') WHERE employee_name='Bob'")
    conn.commit()
    conn.close()
    mail["fail"] = set()
    assert schedule_publish.run_schedule_notices()["ok"] == 1 and mail["sent"] == ["Ana", "Bob"]
    # Send again to whoever has not got it: nobody now.
    app, owner = _app(db), _bearer(db, rid)
    r = app.test_client().post("/mobile/api/labor/publish-schedule", headers=owner,
                               json={"schedule_id": hid, "resend": "missing"}).get_json()
    assert r["ok"] and r["nothing_to_send"] and mail["sent"] == ["Ana", "Bob"]
    # "Send again to everyone" sends (the phone's button used to be a no-op).
    r = app.test_client().post("/mobile/api/labor/publish-schedule", headers=owner,
                               json={"schedule_id": hid, "resend": "all"}).get_json()
    assert r["ok"] and r["resent"] and sorted(mail["sent"]) == ["Ana", "Ana", "Bob", "Bob"]


def test_someone_unreachable_then_with_an_address_now_gets_it_from_send_again(db, mail):
    rid = _restaurant(db)
    _contacts(db, rid, "Ana")
    hid = _save(db, rid, _csv())
    out, _ = _publish(rid, hid)
    assert [u["employee_name"] for u in out["unreachable"]] == ["Bob"]
    _contacts(db, rid, "Bob")
    app, owner = _app(db), _bearer(db, rid)
    r = app.test_client().post("/mobile/api/labor/publish-schedule", headers=owner,
                               json={"schedule_id": hid, "resend": "missing"}).get_json()
    assert r["ok"] and [s["employee_name"] for s in r["sent"]] == ["Bob"]
    assert mail["sent"] == ["Ana", "Bob"]


# ── BUGSLIFE-8 · a queued Send voided by an edit tells the owner ───────────

def test_an_edit_in_the_undo_window_voids_the_send_and_the_owner_is_told(db, mail, monkeypatch):
    rid = _restaurant(db, send_delay_minutes=10)
    _contacts(db, rid, "Ana", "Bob")
    hid = _save(db, rid, _csv())
    app, owner = _app(db), _bearer(db, rid)
    r = app.test_client().post("/mobile/api/labor/publish-schedule", headers=owner,
                               json={"schedule_id": hid, "acknowledge": True, "version": _latest(rid, hid)})
    assert r.get_json()["queued"] is True
    rows = sv.draft_rows(hid)
    rows[0]["notes"] = "typo fixed"
    rows[0]["shift_start"] = "5:30pm"
    assert _save_rows(app, owner, hid, rows, _latest(rid, hid)).status_code == 200
    told = []
    monkeypatch.setattr(strategy_jobs, "_reach", lambda rid_, kind, title, body, data, *a, **k:
                        told.append((kind, title, data)) or 1)
    delayed.run_due(now=dt.datetime.now(dt.timezone.utc) + dt.timedelta(hours=1))
    assert mail["sent"] == []
    assert told and told[0][0] == "schedule_publish_held" and told[0][2]["voided"] is True
    assert told[0][2]["blocker_keys"] == []                          # a Send now of the week as it stands


def test_a_staff_cover_in_the_window_does_not_void_a_changes_send(db, mail):
    rid = _restaurant(db, names=("Ana", "Bob", "Cy"))
    _contacts(db, rid, "Ana", "Bob", "Cy")
    hid = _save(db, rid, _csv())
    _publish(rid, hid)
    v = _latest(rid, hid)
    rows = sv.draft_rows(hid)
    rows[0]["shift_start"] = "5:30pm"                                # Ana, unsent
    conn = models.get_conn(db)
    conn.execute("BEGIN IMMEDIATE")
    edited_v = sv.write_on(conn, rid, hid, "edited", schedule_rows.to_csv(rows), saved_by="gm")
    conn.commit()
    live = models.live_week_rows(rid, MON, db_path=db)
    for r in live:
        if r["date"] == W1[2] and r["employee"] == "Bob":
            r["employee"] = "Cy"
    conn.execute("BEGIN IMMEDIATE")
    sv.write_on(conn, rid, hid, sv.LIVE_REASON, schedule_rows.to_csv(live), saved_by="swap")
    conn.commit()
    conn.close()
    assert edited_v == v + 1
    assert schedule_publish.edited_since(rid, hid, version=edited_v) is False
    assert schedule_publish.edited_since(rid, hid, version=v) is True
    out = delayed._run_schedule_changes_send(rid, {"schedule_id": hid, "version": edited_v, "manual": True,
                                                   "acknowledge": []}, db)
    assert out["ok"] and mail["sent"][-1:] == ["Ana"]


# ── ARCH-29, BUGSLIFE-25 · the draft and its baseline are one write ────────

def test_the_week_and_its_generated_version_are_one_write(db):
    rid = _restaurant(db)

    def fails(conn, new_id):
        raise RuntimeError("version not written")
    with pytest.raises(RuntimeError):
        models.save_schedule_history(rid, W1[0], W1[-1], 0, 0, 30, _csv(), [], db_path=db, in_transaction=fails)
    assert _one(db, "SELECT COUNT(*) FROM schedule_history WHERE restaurant_id=?", rid)[0] == 0
    box = {}

    def writes(conn, new_id):
        text = conn.execute("SELECT schedule_csv FROM schedule_history WHERE id=?", (new_id,)).fetchone()[0]
        box["v"] = sv._insert_version(conn, rid, new_id, "generated", text, None, "Cavnar AI", "system")[1]
    hid = models.save_schedule_history(rid, W1[0], W1[-1], 0, 0, 30, _csv(), [], db_path=db, in_transaction=writes)
    assert box["v"] == 1 and sv.list_versions(rid, hid)[0]["reason"] == "generated"
    src = inspect.getsource(sys.modules["schedule_engine"]._run_schedule_job)
    assert "in_transaction=_with_the_week" in src and '_versions.append(restaurant_id, _history_id, "generated"' not in src


# ── ARCH-15, ARCH-32, ARCH-33 · where the code lives ───────────────────────

def test_the_publish_service_has_no_flask_and_jobs_never_import_the_route_module_for_it():
    src = open(schedule_publish.__file__).read()
    assert "flask" not in src.lower().replace("no flask", "").replace("without flask", "")
    jobs = ("delayed", "scheduler", "schedule_engine")
    for mod in jobs + ("strategy_routes", "ask_cavnar_tools"):
        text = open(f"{mod}.py").read()
        names = ("_publish_schedule", "publish_review", "send_schedule_changes", "publish_blockers")
        for name in names + (("log_account_event",) if mod in jobs else ()):
            assert f"from client_api import {name}" not in text, (mod, name)
    # The old names still answer from client_api.
    assert client_api._publish_schedule is schedule_publish.publish
    assert client_api.send_schedule_changes is schedule_publish.send_changes
    assert client_api.publish_review is schedule_publish.publish_review


def test_the_web_status_poll_is_the_phones_body():
    assert '_m("mobile_schedule_status")' in inspect.getsource(client_api.schedule_status)
    assert "error=_safe_err(e)" in inspect.getsource(mobile_api.mobile_schedule_status)


def test_update_schedule_history_rows_stays_gone():
    """It wrote schedule_csv with no version, no conflict check and, with no
    id, onto the restaurant's newest row of any week (ARCH-33); tests now
    write through schedule_versions.write_on."""
    assert not hasattr(models, "update_schedule_history_rows")


# ── BUGSLIFE-17 · the web save never marks an in-flight edit saved ────────

def test_the_web_save_keeps_an_edit_made_while_saving_unsaved():
    html = open("templates/dashboard.html").read()
    fn = html[html.index("function rescoreSchedule(btn, after)"):html.index("function _setQualityState(")]
    assert "var postedSig = _schedSaveSig(_schedRows);" in fn
    assert "var movedSince = !!d.saved && _schedSaveSig(_schedRows) !== postedSig;" in fn
    assert "if (d.saved && !movedSince && typeof after === 'function') after();" in fn
    assert "_schedSetDirty(true);" in fn
    sig = html[html.index("function _schedSaveSig(rows)"):html.index("function _schedSnapshot()")]
    assert "r.shift_id" in sig and "r.notes" in sig and "=>" not in sig and "let " not in sig
    pub = html[html.index("function publishScheduleNow(ack)"):html.index("function cavBlockers(")]
    assert "body.version = _schedVersion" in pub
