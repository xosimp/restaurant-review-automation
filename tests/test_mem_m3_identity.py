"""An employee is a person, not a display name (memory audit 9/29/26,
identity).

The POS's stable ids were dropped at import, so a rename stranded every
rating, setting, note and tenure record: Gia Mia's owner rated 17 people the
POS then spelled differently and the engine scheduled everyone as unrated; a
15-year-old flagged 14-15 under "Jake S." who re-imported as "Jacob Smith"
lost the minor rules. Now every POS row carries the employee's own id,
ingest resolves by id then exact name, a similar name is a question for the
owner (never an automatic merge), and merge_people / rename_person re-point
every name-keyed store.
"""
from datetime import date, timedelta

import pytest
from flask import Flask

import auth
import intraday
import models
import people
import pos
import shift_facts
import staff_settings
import strategy_routes
from models import Restaurant, create_restaurant


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
    return create_restaurant(Restaurant(name="Identity Co", owner_email="i@x.test", module_labor=1))


HEADER = ("date,day,employee,role,shift_start,shift_end,scheduled_hours,actual_hours,sales,notes,pay_rate,"
          "employee_ext_id,employee_payroll_id,employee_named,schedule_known\n")


def _sync(rid, people_rows, days=(1, 2, 3), month=9, source="rpower"):
    """A POS pull: [(name, ext_id, payroll, named)] each working `days`."""
    lines = [HEADER]
    for d in days:
        day = date(2026, month, d)
        for name, ext, payroll, named in people_rows:
            lines.append(f"{day.isoformat()},{day.strftime('%A')},{name},Server,16:00,22:00,6,6,1000,,,"
                         f"{ext},{payroll},{'1' if named else '0'},0\n")
    return pos.save_synced_shifts(rid, "".join(lines), source)


def _names_in(table, col="employee_name", rid=None):
    conn = models.get_conn()
    try:
        return sorted({r[0] for r in conn.execute(f"SELECT {col} FROM {table} WHERE restaurant_id=?", (rid,))})
    finally:
        conn.close()


def _csv_names(rid):
    from labor import load_shifts
    return sorted({r["employee"] for r in load_shifts(csv_string=models.get_client_data(rid)["shifts_csv"])})


# ── a POS rename carries every record with it ───────────────────────────────

def test_a_pos_rename_keeps_the_minor_flag_rating_note_and_history():
    rid = _rid()
    _sync(rid, [("Jake S.", "4444", "20CY2G", True), ("Ana B.", "4445", "1C4PM4", True)])
    staff_settings.upsert(rid, "Jake S.", minor_age_band="14-15")
    models.set_capability(rid, "Jake S.", "overall", score=4)
    models.save_staff_note(rid, "Jake S.", "no school nights")
    # The POS now spells him in full; his id has not changed.
    _sync(rid, [("Jacob Smith", "4444", "20CY2G", True), ("Ana B.", "4445", "1C4PM4", True)], days=(8, 9))
    st = staff_settings.for_name(rid, "Jacob Smith")
    assert st["minor_age_band"] == "14-15" and st["is_minor"] is True
    assert _names_in("staff_settings", rid=rid) == ["Jacob Smith"]
    assert models.get_operational_scores(rid) == {"Jacob Smith": 4}
    assert [n["employee_name"] for n in models.get_staff_notes(rid)] == ["Jacob Smith"]
    # One person in the history, not a departure and a new hire.
    assert _csv_names(rid) == ["Ana B.", "Jacob Smith"]
    assert [e["name"] for e in staff_settings.roster(rid)] == ["Ana B.", "Jacob Smith"]
    assert set(_names_in("shift_facts", rid=rid)) == {"Ana B.", "Jacob Smith"}
    # The rename is on the record, and the old spelling is his alias.
    conn = models.get_conn()
    try:
        kinds = [r["kind"] for r in conn.execute("SELECT kind FROM person_merges WHERE restaurant_id=?", (rid,))]
    finally:
        conn.close()
    assert kinds == ["rename"]
    assert people.canonical_names(rid, ["Jake S."])["Jake S."] == "Jacob Smith"


def test_an_upload_under_an_old_spelling_lands_on_the_person():
    rid = _rid()
    _sync(rid, [("Jake S.", "4444", "", True)])
    _sync(rid, [("Jacob Smith", "4444", "", True)], days=(8,))
    rows = [{"date": "2026-09-15", "employee": "Jake S.", "role": "Server", "shift_start": "16:00",
             "shift_end": "22:00", "scheduled_hours": "6", "actual_hours": "6", "sales": "1000"}]
    shift_facts.ingest(rid, rows, "upload")
    assert _csv_names(rid) == ["Jacob Smith"]


def test_a_payroll_code_never_renames_a_person():
    rid = _rid()
    _sync(rid, [("Amy Baylis", "4444", "20CY2G", True)])
    # RPOWER's employee list failed for this punch: the row carries the code.
    _sync(rid, [("20CY2G", "4444", "20CY2G", False)], days=(8,))
    assert people.canonical_names(rid, ["Amy Baylis"])["Amy Baylis"] == "Amy Baylis"
    conn = models.get_conn()
    try:
        assert conn.execute("SELECT COUNT(*) FROM person_merges WHERE restaurant_id=?", (rid,)).fetchone()[0] == 0
        assert [r[0] for r in conn.execute("SELECT display_name FROM people WHERE restaurant_id=?", (rid,))] == ["Amy Baylis"]
    finally:
        conn.close()
    # The code row is her punch: resolved by the id, stored under her name.
    assert _csv_names(rid) == ["Amy Baylis"]


def test_a_new_spelling_that_is_someone_elses_name_is_a_question_not_a_rename():
    rid = _rid()
    _sync(rid, [("Chris P.", "4444", "", True), ("Chris Park", "5555", "", True)])
    _sync(rid, [("Chris Park", "4444", "", True), ("Chris Park", "5555", "", True)], days=(8,))
    names = {r["name"] for r in people.list_people(rid)}
    assert "Chris P." in names                       # not renamed onto somebody else's name
    qs = people.open_questions(rid)
    assert any({q["a"]["name"], q["b"]["name"]} == {"Chris P.", "Chris Park"} for q in qs)


def test_two_pos_people_with_one_name_stay_two_people():
    rid = _rid()
    _sync(rid, [("Maria G.", "1111", "", True), ("Maria G.", "2222", "", True)])
    names = sorted(r["name"] for r in people.list_people(rid))
    assert names[0] == "Maria G." and names[1].startswith("Maria G. #")
    assert any(q["kind"] == "same_name" for q in people.open_questions(rid))


# ── similar names: a question, never an automatic merge ─────────────────────

def test_ratings_under_another_spelling_raise_a_question_and_merge_only_on_the_answer():
    rid = _rid()
    _sync(rid, [("Kim T.", "4444", "", True)])
    models.set_capability(rid, "Kim Tran", "overall", score=5)        # typed by the owner
    people.stamp_person_ids(rid)
    assert models.get_operational_scores(rid) == {"Kim Tran": 5}       # nothing moved on a guess
    q = next(q for q in people.open_questions(rid) if {q["a"]["name"], q["b"]["name"]} == {"Kim T.", "Kim Tran"})
    assert "last initial" in q["reason"]
    out = people.answer_question(rid, q["id"], True)
    # The one the POS knows keeps its spelling; the rating follows.
    assert out["into"] == "Kim T." and models.get_operational_scores(rid) == {"Kim T.": 5}
    assert people.open_questions(rid) == []


def test_different_people_is_never_asked_again():
    rid = _rid()
    _sync(rid, [("Sam Lee", "1", "", True), ("Sam L.", "2", "", True)])
    q = next(q for q in people.open_questions(rid))
    people.answer_question(rid, q["id"], False)
    people.stamp_person_ids(rid)
    _sync(rid, [("Sam Lee", "1", "", True), ("Sam L.", "2", "", True)], days=(8,))
    assert people.open_questions(rid) == []


def test_each_answer_records_whose_word_it_is():
    """permissions.answer_authority on every stored answer (SHARED_MEM): the
    owner's is the principal's, a support login acting through view-as is
    the admin's."""
    rid = _rid()
    _sync(rid, [("Sam Lee", "1", "", True), ("Sam L.", "2", "", True)])
    q = next(q for q in people.open_questions(rid))
    people.answer_question(rid, q["id"], False, user={"id": 3, "role": "client"})
    _sync(rid, [("Kim T.", "4444", "", True)], days=(9,))
    models.set_capability(rid, "Kim Tran", "overall", score=5)
    people.stamp_person_ids(rid)
    q2 = next(q for q in people.open_questions(rid) if {q["a"]["name"], q["b"]["name"]} == {"Kim T.", "Kim Tran"})
    people.answer_question(rid, q2["id"], True, user={"id": 3, "role": "client", "acting_admin_id": 1})
    conn = models.get_conn()
    got = {r["id"]: (r["status"], r["answered_by"], r["answered_authority"])
           for r in conn.execute("SELECT * FROM person_questions WHERE restaurant_id=?", (rid,)).fetchall()}
    conn.close()
    assert got[q["id"]] == ("different", 3, "principal")
    assert got[q2["id"]] == ("merged", 3, "admin")


def test_similar_is_a_reason_or_nothing():
    assert people.similar("Kim T.", "Kim Tran") == "same first name and last initial"
    assert people.similar("Jake Smith", "Jacob Smith")
    assert people.similar("Maria Garcia", "Maria Lopez") is None
    assert people.similar("Ana B.", "Ana B.") is None


# ── merging folds two rows in one slot ──────────────────────────────────────

def test_a_merge_folds_settings_ratings_and_tenure():
    rid = _rid()
    _sync(rid, [("Kim T.", "4444", "", True)], days=(1, 2))
    models.set_capability(rid, "Kim T.", "overall", score=3)
    staff_settings.upsert(rid, "Kim T.", max_hours=30)
    models.set_capability(rid, "Kim Tran", "overall", score=5)
    conn = models.get_conn()
    conn.execute("UPDATE staff_capabilities SET updated_at=datetime('now', '+1 minute') WHERE employee_name='Kim Tran'")
    conn.commit()
    conn.close()
    staff_settings.upsert(rid, "Kim Tran", employment_type="full")
    import schedule_intel
    schedule_intel.remember_tenure(rid, [{"employee": "Kim Tran", "date": "2026-03-01"}])
    a = people.person_id_for(rid, "Kim Tran")
    z = people.person_id_for(rid, "Kim T.")
    out = people.merge_people(rid, a, z)
    assert out["into"] == "Kim T."
    st = staff_settings.for_name(rid, "Kim T.")
    assert st["max_hours"] == 30 and st["employment_type"] == "full"          # blanks filled from both
    assert models.get_operational_scores(rid) == {"Kim T.": 5}                 # the newer judgment stays
    assert any(f["table"] == "staff_capabilities" for f in out["folded"])      # the other is on the record
    conn = models.get_conn()
    try:
        row = conn.execute("SELECT first_seen FROM staff_first_seen WHERE restaurant_id=?", (rid,)).fetchall()
    finally:
        conn.close()
    assert [r[0] for r in row] == ["2026-03-01"]                              # the earliest first day


# ── a clock-in is its person, whatever either side is spelled ──────────────

def test_a_pos_rename_mid_week_is_not_a_no_show(monkeypatch):
    rid = _rid()
    _sync(rid, [("Jake S.", "4444", "", True)])
    _sync(rid, [("Jacob Smith", "4444", "", True)], days=(8,))
    now = pytest.importorskip("datetime").datetime(2026, 9, 29, 18, 30)
    monkeypatch.setattr(intraday, "_todays_scheduled", lambda r, d, db_path=None: [
        {"employee": "Jake S.", "role": "Server", "shift_start": "4:00pm"}])
    monkeypatch.setattr(pos, "fetch_clock_ins_today", lambda r, d: (
        [{"employee": "J. Smith (bar)", "role": "Server", "clocked_in_at": "x", "external_id": "4444"}], "toast"))
    # The clock-in's id is the RPOWER id here for the test; Toast would give its GUID.
    conn = models.get_conn()
    conn.execute("UPDATE person_aliases SET source='toast' WHERE restaurant_id=? AND external_id='4444'", (rid,))
    conn.commit()
    conn.close()
    gaps = intraday.coverage_gaps(rid, now_local=now, restaurant=models.get_restaurant(rid))
    assert gaps["available"] and gaps["missing"] == []


# ── the write-back finds the payroll id ─────────────────────────────────────

def test_the_rpower_push_finds_a_persons_payroll_id(monkeypatch):
    import rpower
    rid = _rid()
    _sync(rid, [("Dana Reyes", "4444", "20CY2G", True)])
    monkeypatch.setattr(rpower, "_ctx", lambda r: ("tok", {"storemid": "9", "cg": "1"}))
    sent = {}

    class _Resp:
        status_code, ok, text = 200, True, ""

    def fake_post(url, headers=None, json=None, timeout=None):
        sent["json"] = json
        return _Resp()
    import requests
    monkeypatch.setattr(requests, "post", fake_post)
    out = rpower.push_labor_schedule(rid, [{"employee": "Dana Reyes", "job": "Server",
                                            "start": "2026-10-05T16:00:00", "end": "2026-10-05T22:00:00"}])
    assert out["ok"] is True and sent["json"]["schedules"][0]["payrollid"] == "20CY2G"


# ── the owner's routes ──────────────────────────────────────────────────────

def _client(monkeypatch, rid, role="owner"):
    user = {"id": 7, "restaurant_id": rid, "base_restaurant_id": rid, "username": role, "role": role,
            "is_admin": 0, "email": "o@x.test"}
    monkeypatch.setattr(auth, "get_current_user", lambda: user)
    monkeypatch.setattr(auth, "get_session_user", lambda *a, **k: user, raising=False)
    app = Flask(__name__)
    app.register_blueprint(strategy_routes.strategy_bp)
    app.register_blueprint(strategy_routes.strategy_mobile_bp)
    return app.test_client()


def test_the_owner_answers_and_a_manager_cannot(monkeypatch):
    rid = _rid()
    _sync(rid, [("Kim T.", "4444", "", True)])
    models.set_capability(rid, "Kim Tran", "overall", score=5)
    people.stamp_person_ids(rid)
    mgr = _client(monkeypatch, rid, role="manager")
    got = mgr.get("/api/people/identity").get_json()
    assert got["ok"] and got["questions"] and got["can_answer"] is False
    qid = got["questions"][0]["id"]
    assert mgr.post(f"/api/people/identity/{qid}", json={"same": True}).status_code == 403
    owner = _client(monkeypatch, rid)
    r = owner.post(f"/mobile/api/people/identity/{qid}", json={"same": True}, headers={"Authorization": "Bearer t"})
    assert r.status_code == 200 and r.get_json()["status"] == "merged"
    assert models.get_operational_scores(rid) == {"Kim T.": 5}
