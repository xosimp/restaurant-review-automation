"""Schedule fix round 10/11/26, workstream D: shift identity and the week's
data model (blind re-audit 10/11/26).

ARCH-7      one row per shift of each copy of a week (schedule_shifts),
            written at the one commit point; readers use it, the text while
            a week has no current projection.
ARCH-24     every write of a week's text goes through schedule_versions.
            store_week; the Studio save is the week service's.
ARCH-6      requests, sections, consents and commitments keep their shift by
            id: a retime or a rename never orphans them.
STRESS-7/8  the history's own evidence of who is who is asked and said.
BUGSLIFE-2  a covered release holds only while the week staff have shows it.
BUGSLIFE-6  a rename rewrites both copies; recipients and the pay ledger read
            people, not spellings.
BUGSLIFE-7  a rest waiver is recorded only by the claim that writes the
            week, bound to that shift.
BUGSLIFE-10 the cross-site yes runs the one cover body (gate, notices).
BUGSLIFE-13 contact edits need the publishing permission.
BUGSLIFE-16 a one-shift edit lands on the live copy.
BUGSLIFE-17 good-faith estimates read the published copies.
BUGSLIFE-18 the duplicate check reads the row's spelling and the shift id."""
import datetime as dt
import json
import pathlib
import re

import pytest

import cross_site
import models
import people
import predictability_pay
import schedule_publish
import schedule_rows
import schedule_shifts
import schedule_versions as sv
import shift_requests as srq
import staff_consent
import staff_settings as ss
from tests.test_sf10_j2_floor import db, _restaurant, _rows, W1, HEADER, _day  # noqa: F401 — db is the fixture

ROOT = pathlib.Path(__file__).resolve().parent.parent
NOW = dt.datetime(2026, 10, 1, 9, 0)


def _week(db, rid, rows, publish=True):
    """A week saved and published through the real stamp (the one commit
    point), so its shift projection is current."""
    text = HEADER + "\n" + "\n".join(f"{d},{_day(d)},{n},{role},{s},{e},{h}," for d, n, role, s, e, h in rows)
    hid = models.save_schedule_history(rid, W1[0], W1[-1], 0, 0, 30, text, [], db_path=db)
    if publish:
        schedule_publish._stamp(rid, hid, "publish", "owner", "owner", None, db_path=db)
    return hid


def _req(db, request_id):
    conn = models.get_conn(db)
    try:
        return dict(conn.execute("SELECT * FROM shift_change_requests WHERE id=?", (request_id,)).fetchone())
    finally:
        conn.close()


def _live(db, hid):
    conn = models.get_conn(db)
    try:
        return schedule_rows.parse(conn.execute("SELECT published_csv FROM schedule_history WHERE id=?",
                                                (hid,)).fetchone()[0])
    finally:
        conn.close()


SERVERS = [("Ana", "Server"), ("Ben", "Server"), ("Cara", "Server"), ("Max", "Manager")]


# ── ARCH-7: the shift projection ─────────────────────────────────────────────

def test_every_copy_of_a_week_is_projected_at_the_commit_point(db):
    rid = _restaurant(db, SERVERS)
    hid = _week(db, rid, [(W1[1], "Ana", "Server", "5:00pm", "11:00pm", 6),
                          (W1[2], "Max", "Manager", "4:00pm", "1:00am", 9)])
    conn = models.get_conn(db)
    try:
        got = {(r["copy"], r["employee_name"]): dict(r) for r in conn.execute(
            "SELECT * FROM schedule_shifts WHERE history_id=?", (hid,)).fetchall()}
        assert schedule_shifts.current(conn, hid)
        assert schedule_shifts.double_bookings(conn, hid) == []
    finally:
        conn.close()
    assert set(got) == {("working", "Ana"), ("working", "Max"), ("published", "Ana"), ("published", "Max")}
    # The overnight close ends past midnight on real time; the id is the row's.
    assert got[("published", "Max")]["ends_at"] == f"{W1[3]}T01:00" and got[("published", "Max")]["hours"] == 9.0
    live_ids = {r["shift_id"] for r in _live(db, hid)}
    assert got[("published", "Ana")]["shift_id"] in live_ids
    assert got[("published", "Ana")]["revision"] >= 1


def test_a_reader_never_serves_a_stale_projection(db):
    rid = _restaurant(db, SERVERS)
    hid = _week(db, rid, [(W1[1], "Ana", "Server", "5:00pm", "11:00pm", 6)])
    rows, _h = schedule_shifts.live_shifts_on(rid, W1[1], db_path=db)
    assert [r["employee"] for r in rows] == ["Ana"]
    # A write that went around store_week (an old script): the reader sees
    # the text, never the projection it no longer matches.
    conn = models.get_conn(db)
    conn.execute("UPDATE schedule_history SET published_csv=replace(published_csv, 'Ana', 'Ben') WHERE id=?", (hid,))
    conn.commit()
    conn.close()
    rows, _h = schedule_shifts.live_shifts_on(rid, W1[1], db_path=db)
    assert [r["employee"] for r in rows] == ["Ben"]


def test_the_boot_backfill_projects_weeks_it_has_not_met(db):
    rid = _restaurant(db, SERVERS)
    hid = _week(db, rid, [(W1[1], "Ana", "Server", "5:00pm", "11:00pm", 6)])
    conn = models.get_conn(db)
    conn.execute("DELETE FROM schedule_shifts")
    conn.execute("DELETE FROM schedule_shift_weeks")
    conn.commit()
    conn.close()
    assert schedule_shifts.backfill(db_path=db)["projected"] == 1
    assert schedule_shifts.find(rid, _live(db, hid)[0]["shift_id"], db_path=db)["employee"] == "Ana"
    assert schedule_shifts.backfill(db_path=db)["projected"] == 0


def test_only_the_week_service_writes_a_weeks_text():
    """ARCH-24 / ARCH-7: schedule_csv and published_csv are written by
    schedule_versions.store_week (and the generation's INSERT, which projects
    in its transaction) — never by an UPDATE anywhere else, or the shift
    projection and the week's invariants drift."""
    offenders = []
    for path in ROOT.glob("*.py"):
        text = path.read_text()
        for m in re.finditer(r"UPDATE schedule_history SET[^\"']*(schedule_csv|published_csv)\s*=", text):
            offenders.append(f"{path.name}:{text[:m.start()].count(chr(10)) + 1}")
    assert offenders == []
    src = (ROOT / "models.py").read_text()
    insert = src.index("INSERT INTO schedule_history")
    assert "_ss_save.project(conn, restaurant_id, new_id)" in src[insert:insert + 2500]


def test_the_studio_save_is_the_week_services(db):
    rid = _restaurant(db, SERVERS)
    hid = _week(db, rid, [(W1[1], "Ana", "Server", "5:00pm", "11:00pm", 6)], publish=False)
    rows = sv.draft_rows(hid, db_path=db)
    rows[0] = dict(rows[0], shift_start="4:00pm", scheduled_hours="7")
    base = (sv.newest_version(rid, hid, db) or [{"version": 0}])[0]["version"]
    got = sv.save_studio_week(rid, hid, schedule_rows.to_csv(rows), expected_version=base, saved_by="gm", db_path=db)
    assert not got["unchanged"] and got["version"] == base + 1
    again = sv.save_studio_week(rid, hid, got["csv_text"], expected_version=base + 1, saved_by="gm", db_path=db)
    assert again["unchanged"] and again["version"] == base + 1
    conn = models.get_conn(db)
    try:
        assert conn.execute("SELECT shift_start FROM schedule_shifts WHERE history_id=?", (hid,)).fetchone()[0] \
            == "4:00pm"
    finally:
        conn.close()
    route = (ROOT / "mobile_api.py").read_text()
    body = route[route.index("def mobile_score_schedule"):route.index("def mobile_score_schedule") + 30000]
    assert "save_studio_week(" in body and "conn2.execute(\"UPDATE schedule_history" not in body


# ── ARCH-6: requests follow their shift by id ───────────────────────────────

def test_a_request_keeps_its_shifts_id_and_follows_a_retime_and_a_rename(db):
    rid = _restaurant(db, SERVERS)
    hid = _week(db, rid, [(W1[2], "Ana", "Server", "5:00pm", "11:00pm", 6)])
    req = srq.request_drop(rid, "Ana", W1[2], "5:00pm", now=NOW)
    sid = _live(db, hid)[0]["shift_id"]
    assert req["shift_id"] == sid
    # The manager retimes it 15 minutes and sends: the request follows.
    rows = sv.draft_rows(hid, db_path=db)
    rows[0] = dict(rows[0], shift_start="5:15pm")
    sv.save_studio_week(rid, hid, schedule_rows.to_csv(rows), expected_version=sv.newest_version(rid, hid, db)[0]
                        ["version"], saved_by="gm", db_path=db)
    schedule_publish._stamp(rid, hid, "changes", "gm", "owner", None, db_path=db)
    got = _req(db, req["id"])
    assert got["status"] == "pending" and got["shift_start"] == "5:15pm" and got["shift_id"] == sid
    # A by-id lookup of the request finds it whatever the spelling.
    live = _live(db, hid)
    assert srq._leg_index(live, dict(got, employee_name="ana")) == 0


def test_the_duplicate_check_reads_the_rows_spelling_and_the_swaps_target_leg(db):
    """BUGSLIFE-18: a renamed employee could drop a shift that was the target
    of a pending swap (stored under the row's spelling)."""
    rid = _restaurant(db, SERVERS)
    hid = _week(db, rid, [(W1[2], "Ana", "Server", "5:00pm", "11:00pm", 6),
                          (W1[3], "Ben", "Server", "5:00pm", "11:00pm", 6)])
    srq.request_swap(rid, "Ben", W1[3], "5:00pm", "Ana", W1[2], "5:00pm", now=NOW)
    with pytest.raises(srq.ShiftRequestError):
        srq.request_drop(rid, "ana", W1[2], "5:00pm", now=NOW)
    conn = models.get_conn(db)
    try:
        sid = _live(db, hid)[0]["shift_id"]
        assert srq._live_on_shift(conn, rid, "Somebody Else", "x", "y", shift_id=sid)
    finally:
        conn.close()


def test_a_section_follows_its_shift_through_a_retime(db):
    rid = _restaurant(db, SERVERS, foh_sections_json=json.dumps(["Patio"]))
    hid = _week(db, rid, [(W1[2], "Ana", "Server", "5:00pm", "11:00pm", 6)])
    models.set_shift_section(rid, W1[2], "Ana", "5:00pm", "Patio", db_path=db)
    rows = sv.draft_rows(hid, db_path=db)
    rows[0] = dict(rows[0], shift_start="4:00pm")
    sv.save_studio_week(rid, hid, schedule_rows.to_csv(rows), expected_version=sv.newest_version(rid, hid, db)[0]
                        ["version"], saved_by="gm", db_path=db)
    schedule_publish._stamp(rid, hid, "changes", "gm", "owner", None, db_path=db)
    assert models.sections_for_employee(rid, "Ana", db_path=db) == {(W1[2], "16:00"): "Patio"}


# ── BUGSLIFE-2: a cover holds only while the week shows it ──────────────────

def test_a_covered_release_does_not_outlive_the_cover(db):
    rid = _restaurant(db, SERVERS)
    hid = _week(db, rid, [(W1[2], "Ana", "Server", "5:00pm", "11:00pm", 6)])
    req = srq.request_drop(rid, "Ana", W1[2], "5:00pm", now=NOW)
    srq.decide(rid, req["id"], True, now=NOW)
    srq.claim(rid, req["id"], "Cara", now=NOW)
    rel = srq.shift_release(rid, W1[2])
    assert rel and rel[0]["outcome"] == "covered" and rel[0]["replacement"] == "Cara"
    # Ana is put back on the shift on the copy staff have (a write that
    # went around the closing path): the release no longer reads covered.
    conn = models.get_conn(db)
    live = _live(db, hid)
    live[0] = dict(live[0], employee="Ana")
    conn.execute("BEGIN IMMEDIATE")
    sv.store_week(conn, rid, hid, published=schedule_rows.to_csv(live))
    conn.commit()
    conn.close()
    assert not [e for e in srq.shift_release(rid, W1[2]) if e["outcome"] == "covered"]


# ── BUGSLIFE-7: consent only with the claim, bound to the shift ─────────────

def test_a_rest_waiver_binds_to_its_shift_and_is_recorded_only_with_the_cover(db):
    w = {"standing": False, "dates": set(), "shifts": {W1[2]: {7 * 60}}}
    assert staff_consent.waiver_allows(w, W1[2], "7:00am")
    assert not staff_consent.waiver_allows(w, W1[2], "10:00am")     # another shift that day
    assert not staff_consent.waiver_allows(w, W1[3], "7:00am")
    assert staff_consent.waiver_allows({"standing": True}, W1[3], "9:00am")
    src = (ROOT / "shift_requests.py").read_text()
    judge = src[src.index("def _judge("):src.index("def teammates_with_room(")]
    assert "_consent.record(" not in judge and "consent_out[\"consent\"] = consent" in judge
    cover = src[src.index("def _cover("):src.index("def _taken_shift_id(")]
    assert cover.index("_record_consents(conn") < cover.index("conn.commit()")


def test_a_waiver_left_by_a_claim_that_never_went_through_is_void(db):
    rid = _restaurant(db, SERVERS)
    _week(db, rid, [(W1[2], "Ana", "Server", "7:00am", "1:00pm", 6)])
    req = srq.post_open_shift(rid, W1[2], "7:00am", shift_end="1:00pm", role="Server", actor="gm", broadcast=False,
                              now=NOW)["request"]
    conn = models.get_conn(db)
    staff_consent.record_on(conn, rid, "Ben", "rest_waiver", "I agree", subject_date=W1[2],
                            source_ref=f"request:{req['id']}")
    conn.commit()
    conn.close()
    assert staff_consent.void_unused_rest_waivers(db) == 1
    assert staff_consent.rest_waivers(rid, db_path=db).get("ben", {}).get("dates", set()) == set()


# ── BUGSLIFE-10: the cross-site yes runs the one cover body ─────────────────

def test_a_cross_site_yes_is_gated_on_the_shifts_end_and_tells_the_holder(db, monkeypatch):
    src = (ROOT / "cross_site.py").read_text()
    body = src[src.index("def respond("):src.index("def _set(")]
    assert "_srq._cover(" in body and "visitor_home=home_restaurant_id" in body
    assert "write_on(" not in body
    cover = (ROOT / "shift_requests.py").read_text()
    cover = cover[cover.index("def _cover("):cover.index("def _taken_shift_id(")]
    for step in ("_gate(", "_notify(restaurant_id, \"covered\"", "_cancel_dependents(", "_move_section(",
                 "_refresh_task_sheets(", "_record_compliance_change("):
        assert step in cover


# ── BUGSLIFE-6: a rename and the next Send ──────────────────────────────────

def test_after_a_rename_the_next_send_tells_one_person_once(db):
    rid = _restaurant(db, [("Ana B", "Server"), ("Bo", "Server"), ("Max", "Manager")])
    hid = _week(db, rid, [(W1[2], "Ana B", "Server", "5:00pm", "11:00pm", 6),
                          (W1[3], "Bo", "Server", "5:00pm", "11:00pm", 6)])
    pid = people.person_id_for(rid, "Ana B", db_path=db)
    people.rename_person(rid, pid, "Ana Bell", db_path=db)
    assert {r["employee"] for r in _live(db, hid)} == {"Ana Bell", "Bo"}      # the published copy too
    rows = sv.draft_rows(hid, db_path=db)
    rows = [dict(r, shift_end="10:00pm") if r["employee"] == "Bo" else r for r in rows]
    sv.save_studio_week(rid, hid, schedule_rows.to_csv(rows), expected_version=sv.newest_version(rid, hid, db)[0]
                        ["version"], saved_by="gm", db_path=db)
    told = schedule_publish._recipients(
        [dict(r, employee="Ana B") if r["employee"] == "Ana Bell" else r for r in _live(db, hid)],
        sv.draft_rows(hid, db_path=db), restaurant_id=rid, db_path=db)
    assert told == [("Bo", "changed")]


def test_the_pay_ledger_never_prices_a_rename(db):
    rid = _restaurant(db, [("Ana B", "Server")])
    pid = people.person_id_for(rid, "Ana B", db_path=db)
    people.rename_person(rid, pid, "Ana Bell", db_path=db)
    before = [{"date": W1[2], "employee": "Ana B", "shift_start": "5:00pm", "shift_end": "11:00pm"}]
    after = [dict(before[0], employee="Ana Bell")]
    assert predictability_pay.classify(before, after, rid, db) == []
    assert predictability_pay.classify(before, after)          # by spelling alone it was a cut + an add


# ── STRESS-7 / STRESS-8: who is who ─────────────────────────────────────────

def _hist(rows):
    return [{"date": d, "employee": n, "role": r, "shift_start": s, "shift_end": e} for d, n, r, s, e in rows]


def test_two_people_under_one_name_are_said_not_merged():
    hist = _hist([(f"2026-09-{d:02d}", "Maria Garcia", "Server", "11:00am", "5:00pm") for d in (1, 2, 3)]
                 + [(f"2026-09-{d:02d}", "Maria Garcia", "Line Cook", "10:00am", "4:00pm") for d in (1, 2, 3)])
    got = people.identity_concerns(hist)
    assert got and got[0]["kind"] == "two_people" and got[0]["roles"] == ["Line Cook", "Server"]
    assert "Two people may share the name Maria Garcia" in got[0]["text"]
    # One person working two roles on different days is no concern.
    assert people.identity_concerns(_hist([("2026-09-01", "Jo", "Server", "11:00am", "5:00pm"),
                                           ("2026-09-02", "Jo", "Line Cook", "11:00am", "5:00pm"),
                                           ("2026-09-03", "Jo", "Server", "5:00pm", "9:00pm")])) == []


def test_a_mid_week_rename_is_read_as_one_person_until_answered():
    days = [dt.date(2026, 9, 1) + dt.timedelta(days=i) for i in range(42)]
    hist = _hist([(d.isoformat(), "Jon Smith" if d < dt.date(2026, 10, 7) else "Jonathan Smith", "Server",
                   "5:00pm", "11:00pm") for d in days if d.weekday() in (0, 3, 4, 5)])
    got = [f for f in people.identity_concerns(hist) if f["kind"] == "handoff"]
    assert got and got[0]["old"] == "Jon Smith" and got[0]["new"] == "Jonathan Smith"
    assert "counts them as one person" in got[0]["text"]


def _cook_replaced_by_two():
    """Cesar's kitchen shifts stop 9/5; Alan starts 9/9 and Jhoan 9/13, the
    same role and days (EJ's, 10/15/26)."""
    days = [dt.date(2026, 8, 10) + dt.timedelta(days=i) for i in range(60)]
    rows = []
    for d in days:
        if d.weekday() not in (1, 3, 4, 5):
            continue
        if d <= dt.date(2026, 9, 5):
            rows.append((d.isoformat(), "Cesar Lopez", "Kitchen", "3:00pm", "10:00pm"))
        if d >= dt.date(2026, 9, 9):
            rows.append((d.isoformat(), "Alan Gutierrez", "Kitchen", "3:00pm", "10:00pm"))
        if d >= dt.date(2026, 9, 13):
            rows.append((d.isoformat(), "Jhoan Martinez", "Kitchen", "3:00pm", "10:00pm"))
    return _hist(rows)


def test_two_hires_after_one_departure_are_never_one_person():
    # Each new cook alone looks like Cesar under a new name; together they are
    # two hires, and were joined into one through him - a double booking.
    assert [f for f in people.identity_concerns(_cook_replaced_by_two()) if f["kind"] == "handoff"] == []


def test_names_the_pos_gave_their_own_ids_are_never_joined_by_a_guess(db):
    rid = _restaurant(db, [("Cesar Lopez", "Kitchen"), ("Alan Gutierrez", "Kitchen")])
    people.resolve_rows(rid, [{"employee": "Cesar Lopez", "employee_ext_id": "111", "date": "2026-09-05"},
                              {"employee": "Alan Gutierrez", "employee_ext_id": "222", "date": "2026-09-09"}],
                        "rpower", db_path=db)
    hist = [r for r in _cook_replaced_by_two() if r["employee"] != "Jhoan Martinez"]
    assert [f["new"] for f in people.identity_concerns(hist) if f["kind"] == "handoff"] == ["Alan Gutierrez"]
    assert people.told_apart(rid, [("Cesar Lopez", "Alan Gutierrez")], db_path=db) == {("Cesar Lopez", "Alan Gutierrez")}
    assert people.raise_identity_questions(rid, shifts=hist, db_path=db) == 0
    # A question filed before the ids were known is closed as the POS's
    # answer, and never binds the two meanwhile.
    pa, pb = (people.person_id_for(rid, n, db_path=db) for n in ("Alan Gutierrez", "Cesar Lopez"))
    conn = models.get_conn(db)
    conn.execute("INSERT INTO person_questions (restaurant_id, person_a, person_b, kind, reason, evidence_json) "
                 "VALUES (?,?,?,?,?,?)", (rid, pa, pb, "same_person", "took over", json.dumps({"new_first": "2026-09-09"})))
    conn.commit()
    conn.close()
    assert people.identity_index(rid, ["Cesar Lopez", "Alan Gutierrez"], db_path=db)["linked"] == {}
    people.raise_identity_questions(rid, shifts=hist, db_path=db)
    assert not [q for q in people.open_questions(rid, db_path=db) if q["kind"] == "same_person"]


def test_a_two_people_answer_splits_the_history_by_role(db):
    rid = _restaurant(db, [("Maria Garcia", "Server")])
    rows = [f"2026-09-{d:02d},Tuesday,Maria Garcia,Server,11:00am,5:00pm,6,6,0," for d in (1, 2)] + \
           [f"2026-09-{d:02d},Tuesday,Maria Garcia,Line Cook,10:00am,4:00pm,6,6,0," for d in (1, 2)]
    models.save_client_data(rid, "shifts", "date,day,employee,role,shift_start,shift_end,scheduled_hours,"
                            "actual_hours,sales,notes\n" + "\n".join(rows) + "\n", db_path=db)
    pid = people.person_id_for(rid, "Maria Garcia", db_path=db)
    from labor import load_shifts
    assert people.raise_identity_questions(rid, shifts=load_shifts(csv_string="date,day,employee,role,shift_start,"
                                           "shift_end,scheduled_hours,actual_hours,sales,notes\n" + "\n".join(rows)),
                                           db_path=db) == 1
    q = next(x for x in people.open_questions(rid, db_path=db) if x["kind"] == "two_people")
    got = people.answer_question(rid, q["id"], False, keep="Line Cook", db_path=db)
    assert got["status"] == "split" and got["name"] == "Maria Garcia (Line Cook)" and got["moved"] == 2
    assert got["person_id"] != pid


# ── BUGSLIFE-13 / -16 / -17 ─────────────────────────────────────────────────

def test_contact_edits_need_the_publishing_permission():
    src = (ROOT / "mobile_api.py").read_text()
    body = src[src.index("def mobile_set_staff_contact"):src.index("def mobile_publish_schedule")]
    assert "SCHEDULE_PUBLISH" in body and "TEAM_INVITE" in body and "403" in body
    assert "change_log.record(" in body and "_send_staff_email(" in body


def test_a_one_shift_edit_lands_on_the_live_copy_not_a_newer_draft(db):
    import shift_edits
    rid = _restaurant(db, SERVERS)
    live = _week(db, rid, [(W1[2], "Ana", "Server", "5:00pm", "11:00pm", 6)])
    _week(db, rid, [(W1[2], "Ben", "Server", "5:00pm", "11:00pm", 6)], publish=False)     # a regenerated draft
    assert shift_edits.week_for(rid, W1[2], db_path=db)["id"] == live


def test_good_faith_estimates_read_what_staff_were_sent(db):
    import fair_workweek
    rid = _restaurant(db, SERVERS)
    hid = _week(db, rid, [(W1[2], "Ana", "Server", "5:00pm", "11:00pm", 6)])
    rows = sv.draft_rows(hid, db_path=db)
    rows[0] = dict(rows[0], employee="Ben")                    # an unsent edit
    sv.save_studio_week(rid, hid, schedule_rows.to_csv(rows), expected_version=sv.newest_version(rid, hid, db)[0]
                        ["version"], saved_by="gm", db_path=db)
    monkey_today = dt.date.fromisoformat(W1[0])
    got = fair_workweek._published_rows(rid, weeks=520, db_path=db)
    assert got and [r["employee"] for r in got[0][1]] == ["Ana"]
    assert monkey_today
