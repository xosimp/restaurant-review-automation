"""Publishing a generated schedule to the people working it.

Before this, the labor module generated a schedule and stopped at a CSV
download — the staff who actually work the shifts never saw it. These
cover the per-employee split, the tokenised public page (staff have no
dashboard logins, which is exactly why schedules end up as a photo of a
printout in a group chat), and the honesty of the reachability reporting.
"""
import pytest
from flask import Flask

import auth
import client_api
import labor
import mobile_api
import models
from client_api import client_bp
from models import create_restaurant, Restaurant, get_conn

SCHEDULE_CSV = """date,day,employee,role,shift_start,shift_end,scheduled_hours,notes
2026-09-07,Monday,Sofia R.,Server,16:00,22:00,6.0,
2026-09-08,Tuesday,Sofia R.,Server,17:00,23:00,6.0,close
2026-09-07,Monday,Marcus T.,Cook,08:00,16:00,8.0,
"""


@pytest.fixture(autouse=True)
def _redirect_db(monkeypatch, db_path):
    real_get_conn = models.get_conn
    redirect = lambda *a, **k: real_get_conn(db_path)
    for mod in (models, auth, client_api, mobile_api, labor):
        monkeypatch.setattr(mod, "get_conn", redirect, raising=False)


@pytest.fixture
def app():
    flask_app = Flask(__name__, template_folder="../templates")
    flask_app.register_blueprint(client_bp)
    return flask_app


def _submit_availability(client, token, days, note="", extra=None):
    """Mirrors what the page's own form sends, including the double-submit
    CSRF pair (cookie + hidden field) — see staff_schedule_page's comment
    on why the route isn't exempted from CSRF. A MultiDict, because
    `unavailable` is a repeated field."""
    from werkzeug.datastructures import MultiDict
    client.set_cookie("csrf_js", "tok-for-test", domain="localhost")
    items = [("unavailable", d) for d in days]
    items.append(("note", note))
    items.append(("csrf_token", "tok-for-test"))
    items.extend(extra or [])
    return client.post(f"/s/{token}/availability", data=MultiDict(items))


@pytest.fixture
def client(app):
    return app.test_client()


def _restaurant(db_path):
    return create_restaurant(Restaurant(name="Schedule Co", owner_email="s@x.com"), db_path=db_path)


def _schedule(db_path, rid, csv_text=SCHEDULE_CSV):
    conn = get_conn(db_path)
    cur = conn.execute("""
        INSERT INTO schedule_history (restaurant_id, week_start, week_end, hours_scheduled,
                                      hours_budget, labor_target, schedule_csv, summary_json)
        VALUES (?, '2026-09-07', '2026-09-13', 20, 24, 30, ?, '[]')
    """, (rid, csv_text))
    conn.commit()
    sid = cur.lastrowid
    conn.close()
    return sid


def test_each_employee_sees_only_their_own_shifts(client, db_path):
    rid = _restaurant(db_path)
    sid = _schedule(db_path, rid)
    token = models.create_schedule_share(rid, sid, "Sofia R.", db_path=db_path)

    resp = client.get(f"/s/{token}")
    body = resp.get_data(as_text=True)
    assert resp.status_code == 200
    assert "Sofia R." in body
    # Her two shifts are there...
    assert "16:00" in body and "17:00" in body
    # ...and her colleague's shift is not.
    assert "Marcus T." not in body
    assert "08:00" not in body


def test_the_page_totals_only_that_persons_hours(client, db_path):
    rid = _restaurant(db_path)
    sid = _schedule(db_path, rid)
    token = models.create_schedule_share(rid, sid, "Sofia R.", db_path=db_path)
    body = client.get(f"/s/{token}").get_data(as_text=True)
    assert "12.0 hrs" in body          # 6 + 6, not the 20 on the whole schedule


def test_opening_the_link_records_that_it_was_seen(client, db_path):
    rid = _restaurant(db_path)
    sid = _schedule(db_path, rid)
    token = models.create_schedule_share(rid, sid, "Sofia R.", db_path=db_path)

    before = models.get_schedule_share_status(rid, sid, db_path=db_path)[0]
    assert before["viewed_at"] is None and before["view_count"] == 0

    client.get(f"/s/{token}")
    client.get(f"/s/{token}")
    after = models.get_schedule_share_status(rid, sid, db_path=db_path)[0]
    assert after["viewed_at"] is not None
    assert after["view_count"] == 2


def test_an_unknown_token_404s_without_confirming_anything(client, db_path):
    resp = client.get("/s/definitely-not-a-real-token")
    assert resp.status_code == 404
    assert "isn't valid" in resp.get_data(as_text=True)


def test_someone_with_no_shifts_gets_a_clear_page_not_an_error(client, db_path):
    rid = _restaurant(db_path)
    sid = _schedule(db_path, rid)
    token = models.create_schedule_share(rid, sid, "Not Scheduled", db_path=db_path)
    body = client.get(f"/s/{token}").get_data(as_text=True)
    assert "not scheduled for any shifts" in body.lower()


def test_republishing_reuses_the_link_already_in_someones_inbox(db_path):
    rid = _restaurant(db_path)
    sid = _schedule(db_path, rid)
    first = models.create_schedule_share(rid, sid, "Sofia R.", sent_to="a@x.test", db_path=db_path)
    second = models.create_schedule_share(rid, sid, "Sofia R.", sent_to="a@x.test", db_path=db_path)
    assert first == second
    # ...and there's still only one share row for her.
    assert len(models.get_schedule_share_status(rid, sid, db_path=db_path)) == 1


def test_a_token_is_bound_to_one_schedule_so_last_weeks_link_shows_last_week(client, db_path):
    rid = _restaurant(db_path)
    old_sid = _schedule(db_path, rid, SCHEDULE_CSV)
    old_token = models.create_schedule_share(rid, old_sid, "Sofia R.", db_path=db_path)
    new_csv = SCHEDULE_CSV.replace("16:00,22:00", "11:00,15:00")
    new_sid = _schedule(db_path, rid, new_csv)
    models.create_schedule_share(rid, new_sid, "Sofia R.", db_path=db_path)

    old_body = client.get(f"/s/{old_token}").get_data(as_text=True)
    assert "16:00" in old_body and "11:00" not in old_body


def test_staff_contacts_upsert_by_name(db_path):
    rid = _restaurant(db_path)
    models.set_staff_contact(rid, "Sofia R.", email="sofia@x.test", db_path=db_path)
    models.set_staff_contact(rid, "Sofia R.", email="sofia.new@x.test", phone="555", db_path=db_path)
    contacts = models.get_staff_contacts(rid, db_path=db_path)
    assert len(contacts) == 1
    assert contacts[0]["email"] == "sofia.new@x.test"
    assert contacts[0]["phone"] == "555"


def test_employee_extraction_is_whitespace_and_case_insensitive():
    shifts = labor.employee_shifts_from_csv(SCHEDULE_CSV, "  sofia r.  ")
    assert len(shifts) == 2
    assert labor.employees_in_schedule(SCHEDULE_CSV) == ["Marcus T.", "Sofia R."]
    assert labor.employee_shifts_from_csv(SCHEDULE_CSV, "Nobody") == []
    assert labor.employee_shifts_from_csv("", "Sofia R.") == []



# ── Staff availability self-service ────────────────────────────────────────

def test_staff_can_submit_the_days_they_cannot_work(client, db_path):
    rid = _restaurant(db_path)
    sid = _schedule(db_path, rid)
    token = models.create_schedule_share(rid, sid, "Sofia R.", db_path=db_path)

    resp = _submit_availability(client, token, ["Tuesday", "Sunday"], "Class on Tuesdays")
    assert resp.status_code in (302, 303)

    rows = models.get_staff_availability(rid, db_path=db_path)
    row = next(r for r in rows if r["employee_name"] == "Sofia R.")
    import json
    assert json.loads(row["unavailable_days"]) == ["Tuesday", "Sunday"]
    assert row["notes"] == "Class on Tuesdays"


def test_the_form_comes_back_pre_ticked(client, db_path):
    rid = _restaurant(db_path)
    sid = _schedule(db_path, rid)
    token = models.create_schedule_share(rid, sid, "Sofia R.", db_path=db_path)
    _submit_availability(client, token, ["Tuesday"])
    body = client.get(f"/s/{token}").get_data(as_text=True)
    assert 'value="Tuesday" checked' in body
    assert 'value="Monday" checked' not in body


def test_the_employee_name_comes_from_the_token_not_the_form(client, db_path):
    """Otherwise anyone with one link could rewrite a colleague's
    availability."""
    rid = _restaurant(db_path)
    sid = _schedule(db_path, rid)
    token = models.create_schedule_share(rid, sid, "Sofia R.", db_path=db_path)

    _submit_availability(client, token, ["Monday"],
                         extra=[("employee_name", "Marcus T.")])   # forged — must be ignored
    names = [r["employee_name"] for r in models.get_staff_availability(rid, db_path=db_path)]
    assert names == ["Sofia R."]


def test_only_real_day_names_are_accepted(client, db_path):
    rid = _restaurant(db_path)
    sid = _schedule(db_path, rid)
    token = models.create_schedule_share(rid, sid, "Sofia R.", db_path=db_path)
    _submit_availability(client, token, ["Tuesday", "Funday", "'; DROP TABLE staff_availability;--"])
    import json
    row = models.get_staff_availability(rid, db_path=db_path)[0]
    assert json.loads(row["unavailable_days"]) == ["Tuesday"]


def test_availability_submission_needs_a_valid_token(client, db_path):
    resp = _submit_availability(client, "not-a-real-token", ["Monday"])
    assert resp.status_code == 404


def test_the_page_issues_a_csrf_token_on_the_very_first_visit(client, db_path):
    """The form is a plain POST, so it can't use the dashboard's fetch
    wrapper — without this the first submission a staff member ever makes
    would be rejected."""
    rid = _restaurant(db_path)
    sid = _schedule(db_path, rid)
    token = models.create_schedule_share(rid, sid, "Sofia R.", db_path=db_path)
    resp = client.get(f"/s/{token}")
    body = resp.get_data(as_text=True)
    assert 'name="csrf_token"' in body
    assert "csrf_js" in resp.headers.get("Set-Cookie", "")
