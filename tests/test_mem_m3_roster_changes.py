"""Every roster change reaches the change history with the person as its
subject (memory audit 9/29/26, change_log — owed by M3 to M7's log): taking
someone off the roster, putting them back, their settings, a hand-added
person, a role, a merge. outcomes.find_concurrent and rec_trust's "changed
since your data" read these as changes on the labor number.
"""
import pytest

import change_log
import models
import people
import shift_facts
import staff_settings
from datetime import date, timedelta
from models import Restaurant, create_restaurant


@pytest.fixture(autouse=True)
def _db(db_path, monkeypatch):
    real = models.get_conn
    fake = lambda *a, **k: real(db_path)  # noqa: E731
    monkeypatch.setattr(models, "get_conn", fake)
    monkeypatch.setattr(models, "DB_PATH", db_path)
    monkeypatch.setattr(staff_settings, "get_conn", fake)
    yield


def _rid():
    return create_restaurant(Restaurant(name="Roster Co", owner_email="r@x.test", module_labor=1))


def _log(rid):
    return [(r["kind"], r["field"], r["subject"]) for r in reversed(change_log.history(rid))]


def test_leaving_returning_and_settings_are_roster_changes_about_the_person():
    rid = _rid()
    staff_settings.upsert(rid, "Maria G.", max_hours=30, updated_by="owner")
    staff_settings.upsert(rid, "Maria G.", active=False, updated_by="owner")
    staff_settings.upsert(rid, "Maria G.", active=True, updated_by="owner")
    staff_settings.upsert(rid, "Maria G.", max_hours=30, updated_by="owner")          # no change: nothing logged
    assert _log(rid) == [("roster_change", "maximum hours", "Maria G."), ("roster_leave", "left", "Maria G."),
                         ("roster_add", "added", "Maria G.")]


def test_a_hand_added_person_and_their_removal():
    rid = _rid()
    models.add_manual_team_member(rid, "Tom B.", role="Cook", added_by="owner")
    models.add_manual_team_member(rid, "Tom B.", role="Line Cook", added_by="owner")
    models.remove_manual_team_member(rid, "Tom B.")
    assert _log(rid) == [("roster_add", "added", "Tom B."), ("roster_change", "role", "Tom B."),
                         ("roster_leave", "removed", "Tom B.")]


def test_a_role_and_a_merge_say_whose_change_it_was():
    rid = _rid()
    rows = [{"date": (date(2026, 9, 1) + timedelta(days=i)).isoformat(), "day": "x", "employee": n, "role": "Server",
             "shift_start": "16:00", "shift_end": "22:00", "scheduled_hours": "6", "actual_hours": "6", "sales": "1"}
            for i, n in enumerate(("Kim T.", "Kim Tran"))]
    shift_facts.ingest(rid, rows, "upload")
    people.add_role(rid, "Kim T.", "Bartender", user={"id": 5, "role": "manager"})
    a, z = people.person_id_for(rid, "Kim Tran"), people.person_id_for(rid, "Kim T.")
    people.merge_people(rid, a, z, user={"id": 3, "role": "client", "acting_admin_id": 1})
    got = {r["field"]: r for r in change_log.history(rid)}
    assert (got["role"]["kind"], got["role"]["subject"], got["role"]["source"]) == ("roster_change", "Kim T.", "manager")
    m = got["merge"]
    assert (m["subject"], m["old_value"], m["new_value"]) == ("Kim T.", "Kim Tran", "Kim T.")
    assert (m["source"], m["actor_role"], m["actor_user_id"]) == ("admin", "view-as", 1)   # the admin behind view-as
