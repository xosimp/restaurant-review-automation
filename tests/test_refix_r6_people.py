"""People memory: merges that can be undone and keep everything, erasing one
person, the bounded shift file, tenure across spellings, the cover answer
(memory re-audit 9/29/26, R6).

INVENTORY-11: person_merges was written and never read — a wrong "same
person" could not be taken back. FORGET-13: a merge dropped the gone
person's covers, mentions, roles and first day from past quarters.
FORGET-12: the shift file kept every shift forever and nobody could be
erased. FORGET-17: tenure rows were deleted on an exact-spelling mismatch.
INVENTORY-10: a later tap overwrote a cover answer given elsewhere.
QUALITY-14: a model's restated claim was shown back as a count.
"""
import json
from datetime import date, timedelta
from pathlib import Path

import pytest
from flask import Flask

import auth
import intraday
import models
import people
import pos
import schedule_intel
import shift_facts
import staff_settings
import strategy_routes
from models import Restaurant, create_restaurant

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def _db(db_path, monkeypatch):
    real = models.get_conn
    fake = lambda *a, **k: real(db_path)  # noqa: E731
    monkeypatch.setattr(models, "get_conn", fake)
    monkeypatch.setattr(models, "DB_PATH", db_path)
    monkeypatch.setattr(staff_settings, "get_conn", fake)
    monkeypatch.setattr(intraday, "get_conn", fake)
    monkeypatch.setattr(pos, "_complete_through_for", lambda r: date.today())
    import client_api
    monkeypatch.setattr(client_api, "invalidate_insight_cache", lambda *a, **k: None)
    yield


def _rid():
    return create_restaurant(Restaurant(name="People Co", owner_email="i@x.test", module_labor=1))


HEADER = ("date,day,employee,role,shift_start,shift_end,scheduled_hours,actual_hours,sales,notes,pay_rate,"
          "employee_ext_id,employee_payroll_id,employee_named,schedule_known\n")


def _sync(rid, people_rows, days=(1, 2, 3), month=9, year=2026, source="rpower"):
    lines = [HEADER]
    for d in days:
        day = date(year, month, d)
        for name, ext in people_rows:
            lines.append(f"{day.isoformat()},{day.strftime('%A')},{name},Server,16:00,22:00,6,6,1000,,,"
                         f"{ext},,1,0\n")
    return pos.save_synced_shifts(rid, "".join(lines), source)


def _csv_names(rid):
    from labor import load_shifts
    return sorted({r["employee"] for r in load_shifts(csv_string=models.get_client_data(rid)["shifts_csv"])})


def _client(monkeypatch, rid, role="owner"):
    user = {"id": 7, "restaurant_id": rid, "base_restaurant_id": rid, "username": role, "role": role,
            "is_admin": 0, "email": "o@x.test"}
    monkeypatch.setattr(auth, "get_current_user", lambda: user)
    monkeypatch.setattr(auth, "get_session_user", lambda *a, **k: user, raising=False)
    app = Flask(__name__)
    app.register_blueprint(strategy_routes.strategy_bp)
    app.register_blueprint(strategy_routes.strategy_mobile_bp)
    return app.test_client()


def _quarter(rid, name, quarter, **vals):
    conn = models.get_conn()
    cols = ["restaurant_id", "employee_name", "employee_key", "quarter", *vals]
    conn.execute(f"INSERT INTO person_quarters ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                 (rid, name, staff_settings.name_key(name), quarter, *vals.values()))
    conn.commit()
    conn.close()


# ── FORGET-13: a merge keeps every count of a closed quarter ────────────────

def test_a_merge_sums_covers_mentions_maps_and_keeps_the_earliest_day():
    rid = _rid()
    _sync(rid, [("Dana Kim", "9001")], days=(1,))
    _sync(rid, [("Dana K.", "")], days=(2,), source="upload")
    _quarter(rid, "Dana K.", "2024-Q1", shifts=10, covers_taken=3, covers_declined=1, mentions_positive=2,
             mentions_negative=1, roles_json='{"Server": 10}', dayparts_json='{"night": 10}',
             weekdays_json='{"Friday": 4}', first_date="2024-01-03", last_date="2024-03-20")
    _quarter(rid, "Dana Kim", "2024-Q1", shifts=5, covers_taken=1, roles_json='{"Server": 3, "Host": 2}',
             dayparts_json='{"night": 5}', weekdays_json='{"Friday": 1, "Monday": 2}',
             first_date="2024-02-01", last_date="2024-03-28")
    a, z = people.person_id_for(rid, "Dana K."), people.person_id_for(rid, "Dana Kim")
    people.merge_people(rid, a, z)
    conn = models.get_conn()
    try:
        q = dict(conn.execute("SELECT * FROM person_quarters WHERE restaurant_id=? AND quarter='2024-Q1'",
                              (rid,)).fetchone())
        n = conn.execute("SELECT COUNT(*) FROM person_quarters WHERE restaurant_id=? AND quarter='2024-Q1'",
                         (rid,)).fetchone()[0]
    finally:
        conn.close()
    assert n == 1 and q["shifts"] == 15
    assert (q["covers_taken"], q["covers_declined"], q["mentions_positive"], q["mentions_negative"]) == (4, 1, 2, 1)
    assert json.loads(q["roles_json"]) == {"Server": 13, "Host": 2}
    assert json.loads(q["weekdays_json"]) == {"Friday": 5, "Monday": 2}
    assert q["first_date"] == "2024-01-03" and q["last_date"] == "2024-03-28"


# ── INVENTORY-11: undoing a merge ───────────────────────────────────────────

def test_a_wrong_merge_is_undone_with_every_record_back(monkeypatch):
    rid = _rid()
    _sync(rid, [("Kim T.", "4444"), ("Kim Tran", "5555")], days=(1, 2))
    models.set_capability(rid, "Kim T.", "overall", score=3)
    models.set_capability(rid, "Kim Tran", "overall", score=5)
    conn = models.get_conn()
    conn.execute("UPDATE staff_capabilities SET updated_at=datetime('now', '+1 minute') WHERE employee_name='Kim Tran'")
    conn.commit()
    conn.close()
    staff_settings.upsert(rid, "Kim T.", max_hours=30)
    staff_settings.upsert(rid, "Kim Tran", employment_type="full")
    before_scores = models.get_operational_scores(rid)
    a, z = people.person_id_for(rid, "Kim Tran"), people.person_id_for(rid, "Kim T.")
    out = people.merge_people(rid, a, z)
    assert out["merge_id"] and _csv_names(rid) == ["Kim T."]
    assert models.get_operational_scores(rid) == {"Kim T.": 5}
    # A manager may not undo it; the owner may, from either client.
    mgr = _client(monkeypatch, rid, role="manager")
    listed = mgr.get("/api/people/merges").get_json()
    assert listed["ok"] and listed["can_undo"] is False and listed["merges"][0]["undoable"]
    assert "/" in listed["merges"][0]["undo_until"]
    assert mgr.post(f"/api/people/merges/{out['merge_id']}/undo").status_code == 403
    owner = _client(monkeypatch, rid)
    r = owner.post(f"/mobile/api/people/merges/{out['merge_id']}/undo", headers={"Authorization": "Bearer t"})
    assert r.status_code == 200, r.get_json()
    assert _csv_names(rid) == ["Kim T.", "Kim Tran"]
    assert models.get_operational_scores(rid) == before_scores
    assert staff_settings.for_name(rid, "Kim T.")["max_hours"] == 30
    assert staff_settings.for_name(rid, "Kim T.").get("employment_type") != "full"
    assert staff_settings.for_name(rid, "Kim Tran")["employment_type"] == "full"
    assert people.person_id_for(rid, "Kim Tran", create=False) == a
    conn = models.get_conn()
    try:
        ext = conn.execute("SELECT person_id FROM person_aliases WHERE restaurant_id=? AND external_id='5555'",
                           (rid,)).fetchone()[0]
        facts = {r[0] for r in conn.execute("SELECT employee_name FROM shift_facts WHERE restaurant_id=?", (rid,))}
    finally:
        conn.close()
    assert ext == a and facts == {"Kim T.", "Kim Tran"}
    assert people.recent_merges(rid)[0]["undone"] is True
    assert owner.post(f"/api/people/merges/{out['merge_id']}/undo").status_code == 409     # once


def test_a_later_rename_must_be_undone_first():
    rid = _rid()
    _sync(rid, [("Kim T.", "4444"), ("Kim Tran", "5555")], days=(1,))
    a, z = people.person_id_for(rid, "Kim Tran"), people.person_id_for(rid, "Kim T.")
    out = people.merge_people(rid, a, z)
    people.rename_person(rid, z, "Kimberly Tran")
    with pytest.raises(people.PeopleError, match="changed again"):
        people.unmerge_people(rid, out["merge_id"])
    assert people.recent_merges(rid)[0]["undoable"] is False


# ── FORGET-12: erasing one person; the shift file is bounded ────────────────

def test_erasing_a_departed_person_removes_every_record_about_them(monkeypatch):
    rid = _rid()
    _sync(rid, [("Zed Q.", "7777"), ("Ana B.", "7778")], days=(1, 2))
    models.set_capability(rid, "Zed Q.", "overall", score=4)
    staff_settings.upsert(rid, "Zed Q.", max_hours=20)
    _quarter(rid, "Zed Q.", "2023-Q4", shifts=30)
    people.record_signal(rid, "Zed Q.", "cover_accepted", "2026-09-01", ref="issue:1")
    conn = models.get_conn()
    conn.execute("INSERT INTO schedule_standing_patterns (restaurant_id, pattern_key, kind, employee, day, daypart, "
                 "text, first_learned, last_confirmed) VALUES (?,?,?,?,?,?,?,?,?)",
                 (rid, "moved_off|zed q.|Tuesday|night", "moved_off", "Zed Q.", "Tuesday", "night",
                  "The manager has taken Zed Q. off Tuesday dinner", "2026-09-01", "2026-09-01"))
    conn.commit()
    conn.close()
    pid = people.person_id_for(rid, "Zed Q.", create=False)
    key = people.person_key("Zed Q.")
    assert _client(monkeypatch, rid, role="manager").post(f"/api/people/{key}/erase",
                                                          json={"confirm": "Zed Q."}).status_code == 403
    owner = _client(monkeypatch, rid)
    # On the roster: refused. A wrong name typed back: refused.
    assert owner.post(f"/api/people/{key}/erase", json={"confirm": "Zed Q."}).status_code == 409
    staff_settings.upsert(rid, "Zed Q.", active=False)
    assert owner.post(f"/api/people/{key}/erase", json={"confirm": "Zed"}).status_code == 400
    r = owner.post(f"/api/people/{key}/erase", json={"confirm": "zed q."})
    assert r.status_code == 200, r.get_json()
    assert _csv_names(rid) == ["Ana B."]
    conn = models.get_conn()
    try:
        for table in ("staff_capabilities", "staff_settings", "shift_facts", "person_quarters", "person_signals",
                      "schedule_standing_patterns", "person_aliases"):
            col = "employee" if table == "schedule_standing_patterns" else "employee_name"
            if table == "person_aliases":
                n = conn.execute("SELECT COUNT(*) FROM person_aliases WHERE restaurant_id=? AND person_id=?",
                                 (rid, pid)).fetchone()[0]
            else:
                n = conn.execute(f"SELECT COUNT(*) FROM {table} WHERE restaurant_id=? AND {col} LIKE 'Zed%'",
                                 (rid,)).fetchone()[0]
            assert n == 0, table
        tomb = conn.execute("SELECT display_name, active FROM people WHERE id=?", (pid,)).fetchone()
        log = [r[0] for r in conn.execute("SELECT COALESCE(subject,'') || COALESCE(old_value,'') || "
                                          "COALESCE(new_value,'') FROM change_log WHERE restaurant_id=? AND "
                                          "field='erase'", (rid,))]
    finally:
        conn.close()
    assert tomb["display_name"] == f"Erased #{pid}" and tomb["active"] == 0
    assert log and not any("Zed" in (x or "") for x in log)


def test_the_shift_file_keeps_no_more_than_shift_facts():
    rid = _rid()
    old = date.today() - timedelta(days=shift_facts.RETAIN_DAYS + 40)
    recent = date.today() - timedelta(days=200)
    # A file stored before the trim: a shift past the three-year window and one inside it.
    models.save_client_data(rid, "shifts", "date,day,employee,role,shift_start,shift_end,scheduled_hours\n"
                            f"{old.isoformat()},{old.strftime('%A')},Ana B.,Server,16:00,22:00,6\n"
                            f"{recent.isoformat()},{recent.strftime('%A')},Cy D.,Server,16:00,22:00,6\n")
    assert _csv_names(rid) == ["Ana B.", "Cy D."]
    _sync(rid, [("Bo C.", "2")], days=(1,), source="upload")
    assert _csv_names(rid) == ["Bo C.", "Cy D."], "a shift older than the three-year window stayed in the file"


# ── FORGET-17: tenure survives a new spelling ───────────────────────────────

def test_a_new_spelling_keeps_the_earliest_first_day():
    rid = _rid()
    conn = models.get_conn()
    conn.execute("INSERT INTO staff_first_seen (restaurant_id, employee_name, first_seen, shifts_seen, last_seen) "
                 "VALUES (?,?,?,?,?)", (rid, "Dana K.", "2025-01-05", 90, "2026-09-02"))
    conn.commit()
    conn.close()
    rows = [{"employee": "Dana k.", "date": "2026-09-01"}, {"employee": "Dana k.", "date": "2026-09-03"}]
    schedule_intel.remember_tenure(rid, rows)
    schedule_intel.forget_stale_names(rid, rows)
    conn = models.get_conn()
    try:
        got = {r["employee_name"]: (r["first_seen"], r["shifts_seen"]) for r in conn.execute(
            "SELECT employee_name, first_seen, shifts_seen FROM staff_first_seen WHERE restaurant_id=?", (rid,))}
    finally:
        conn.close()
    assert got == {"Dana k.": ("2025-01-05", 90)}


# ── INVENTORY-10: the cover answer lives on the issue ───────────────────────

def test_a_cover_answered_elsewhere_is_not_overwritten_by_a_stale_tap(monkeypatch):
    import issues
    rid = _rid()
    issue, _tok = issues.create_issue(rid, "coverage", "Tuesday dinner is short a server",
                                      source_key="coverage:2026-09-29:x", notify=False,
                                      meta={"asked": [{"name": "Zed Q."}]})
    iid = issue["id"] if isinstance(issue, dict) else issue
    owner = _client(monkeypatch, rid)
    assert owner.post(f"/api/issues/{iid}/cover-answer", json={"name": "Zed Q.", "accepted": True}).status_code == 200
    r = owner.post(f"/mobile/api/issues/{iid}/cover-answer", json={"name": "Zed Q.", "accepted": False},
                   headers={"Authorization": "Bearer t"})
    assert r.status_code == 409 and r.get_json()["answer"] == "took"
    assert owner.post(f"/api/issues/{iid}/cover-answer", json={"name": "Zed Q.", "accepted": True}).status_code == 200


def test_the_ios_row_reads_the_servers_answer_and_keeps_nothing_on_the_device():
    row = (ROOT / "ios/CavnarAI/CavnarAI/Features/Labor/TeamMemorySection.swift").read_text()
    day = (ROOT / "ios/CavnarAI/CavnarAI/Features/Home/HomeDay.swift").read_text()
    body = row[row.index("struct CoverAnswerRow"):]
    body = body[:body.index("\n}\n")]
    assert "UserDefaults.standard.set" not in body and "UserDefaults.standard.bool" not in body
    assert 'case answeredAt = "answered_at"' in day and "$0.answer" in day


# ── QUALITY-14: a restated claim is not a count ─────────────────────────────

def test_a_restated_read_is_not_shown_to_the_model_as_a_count():
    src = (ROOT / "ai_reads.py").read_text()
    assert "restated {r['restated_n']}" not in src
    assert "the model's own read, repeated since" in src


def test_a_merge_folds_two_peoples_patterns_and_the_undo_splits_them_again():
    rid = _rid()
    _sync(rid, [("Bob S.", "1"), ("Bob Smith", "2")], days=(1,))
    conn = models.get_conn()
    for name, applied, status in (("Bob S.", 4, "active"), ("Bob Smith", 2, "ruled")):
        conn.execute("INSERT INTO schedule_standing_patterns (restaurant_id, pattern_key, kind, employee, day, daypart, "
                     "text, first_learned, last_confirmed, times_applied, status) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                     (rid, f"moved_off|{name.lower()}|Tuesday|night", "moved_off", name, "Tuesday", "night",
                      f"The manager has taken {name} off Tuesday dinner", "2026-08-0" + str(applied),
                      "2026-09-0" + str(applied), applied, status))
    conn.commit()
    conn.close()
    schedule_intel.dismiss_pattern(rid, "moved_on|bob s.|Friday|night")
    a, z = people.person_id_for(rid, "Bob S."), people.person_id_for(rid, "Bob Smith")
    out = people.merge_people(rid, a, z)
    import schedule_versions
    rows = [r for r in schedule_versions.standing_patterns(rid) if r["kind"] == "moved_off"]
    assert len(rows) == 1 and rows[0]["employee"] == "Bob Smith"
    assert rows[0]["times_applied"] == 6 and rows[0]["status"] == "ruled" and rows[0]["first_learned"] == "8/2/26"
    assert schedule_intel.dismissed_patterns(rid) == {"moved_on|bob smith|Friday|night"}
    people.unmerge_people(rid, out["merge_id"])
    rows = {r["employee"]: r for r in schedule_versions.standing_patterns(rid) if r["kind"] == "moved_off"}
    assert set(rows) == {"Bob S.", "Bob Smith"}
    assert rows["Bob S."]["times_applied"] == 4 and rows["Bob Smith"]["times_applied"] == 2
    assert rows["Bob Smith"]["status"] == "ruled"
    assert schedule_intel.dismissed_patterns(rid) == {"moved_on|bob s.|Friday|night"}
