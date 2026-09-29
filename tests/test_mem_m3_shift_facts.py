"""Per-shift, per-person history is kept, and an upload no longer erases what
lies outside it (memory audit 9/29/26, shift_facts).

client_data held one shifts_csv per restaurant and an upload replaced it
whole: a restaurant with a year of history uploaded a two-week export and
from then on each person's no-show rate rested on two weeks, "usual days"
reflected two weeks, and the mentoring evidence was gone. Only the POS path
merged. Now one ingest serves a sync, the owner's upload and the admin's,
with the POS rule, and shift_facts keeps each shift with its person.
"""
from datetime import date, timedelta

import pytest

import models
import ops
import shift_facts
import staff_settings
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
    return create_restaurant(Restaurant(name="Facts Co", owner_email="f@x.test", module_labor=1))


def _rows(start, days, people=("Ana B.", "Ben C."), actual=6, sched=6):
    out = []
    for i in range(days):
        d = start + timedelta(days=i)
        for p in people:
            out.append({"date": d.isoformat(), "day": d.strftime("%A"), "employee": p, "role": "Server",
                        "shift_start": "16:00", "shift_end": "22:00", "scheduled_hours": str(sched),
                        "actual_hours": str(actual), "sales": "1000"})
    return out


def _facts(rid):
    conn = models.get_conn()
    try:
        return [dict(r) for r in conn.execute("SELECT * FROM shift_facts WHERE restaurant_id=? ORDER BY business_date",
                                              (rid,)).fetchall()]
    finally:
        conn.close()


def test_a_short_upload_keeps_the_year_before_it():
    rid = _rid()
    year = date(2026, 9, 28) - timedelta(days=300)
    shift_facts.ingest(rid, _rows(year, 280), "upload")
    shift_facts.ingest(rid, _rows(date(2026, 9, 14), 14), "upload")
    facts = _facts(rid)
    assert facts[0]["business_date"] == year.isoformat()
    assert len({f["business_date"] for f in facts}) == 280 + 14 - len(
        {d for d in (year + timedelta(days=i) for i in range(280)) if d >= date(2026, 9, 14)})
    # The stored file keeps them too — every older reader parses it.
    from labor import load_shifts
    stored = load_shifts(csv_string=models.get_client_data(rid)["shifts_csv"])
    assert min(r["date"] for r in stored) == year.isoformat()


def test_inside_the_new_files_dates_it_is_the_record():
    rid = _rid()
    shift_facts.ingest(rid, _rows(date(2026, 9, 1), 14, people=("Ana B.", "Ben C.")), "upload")
    shift_facts.ingest(rid, _rows(date(2026, 9, 7), 2, people=("Cy D.",)), "upload")
    inside = {f["employee_name"] for f in _facts(rid) if "2026-09-07" <= f["business_date"] <= "2026-09-08"}
    assert inside == {"Cy D."}
    outside = {f["employee_name"] for f in _facts(rid) if f["business_date"] == "2026-09-01"}
    assert outside == {"Ana B.", "Ben C."}


def test_a_pos_copied_schedule_is_stored_as_no_schedule():
    rid = _rid()
    rows = _rows(date(2026, 9, 1), 2)
    for r in rows:
        r["schedule_known"] = "0"
    shift_facts.ingest(rid, rows, "rpower")
    assert all(f["scheduled_hours"] is None and f["actual_hours"] == 6 for f in _facts(rid))
    shift_facts.ingest(rid, _rows(date(2026, 9, 10), 1), "upload")
    assert [f["scheduled_hours"] for f in _facts(rid) if f["business_date"] == "2026-09-10"] == [6, 6]


def test_tenure_counts_every_shift_not_the_rolling_window():
    rid = _rid()
    shift_facts.ingest(rid, _rows(date(2026, 1, 5), 100, people=("Ana B.",)), "upload")
    shift_facts.ingest(rid, _rows(date(2026, 9, 14), 14, people=("Ana B.",)), "upload")
    assert models.get_employee_tenure(rid)["Ana B."] == 114


def test_the_owners_upload_route_goes_through_the_one_ingest(db_path, monkeypatch):
    """client_api._do_upload_data: an upload merges and writes facts."""
    import io
    from flask import Flask
    import auth
    import client_api
    rid = _rid()
    shift_facts.ingest(rid, _rows(date(2026, 8, 1), 10), "upload")
    user = {"id": 1, "restaurant_id": rid, "base_restaurant_id": rid, "username": "o", "role": "owner",
            "is_admin": 0, "email": "o@x.test"}
    app = Flask(__name__)
    with app.test_request_context():
        csv_text = ("date,day,employee,role,shift_start,shift_end,scheduled_hours,actual_hours,sales,notes\n"
                    "2026-09-14,Monday,Ana B.,Server,16:00,22:00,6,6,1000,\n")
        monkeypatch.setattr(client_api, "_upload_notifies_outward", lambda op: False)
        resp = client_api._do_upload_data(rid, "shifts", io.BytesIO(csv_text.encode()), user)
        body = resp.get_json() if hasattr(resp, "get_json") else resp[0].get_json()
    assert body["ok"] is True
    dates = sorted({f["business_date"] for f in _facts(rid)})
    assert dates[0] == "2026-08-01" and dates[-1] == "2026-09-14"


def test_quarterly_summaries_are_written_while_the_quarter_is_whole():
    rid = _rid()
    shift_facts.ingest(rid, _rows(date(2026, 7, 1), 20, people=("Ana B.",)), "upload")
    n = shift_facts.rollup_quarters(rid, today=date(2026, 9, 29))
    assert n == 1
    conn = models.get_conn()
    try:
        q = dict(conn.execute("SELECT * FROM person_quarters WHERE restaurant_id=?", (rid,)).fetchone())
    finally:
        conn.close()
    assert q["quarter"] == "2026-Q3" and q["shifts"] == 20 and q["hours"] == 120
    # Three years on, the raw rows are gone and the summary is not rewritten.
    ops_days = ops._RETENTION_DAYS["shift_facts"]
    assert ops_days == 1095 and ops._RETENTION_COLUMN["shift_facts"] == "business_date"
    assert shift_facts.rollup_quarters(rid, today=date(2029, 12, 31)) == 0
    conn = models.get_conn()
    try:
        assert conn.execute("SELECT shifts FROM person_quarters WHERE restaurant_id=?", (rid,)).fetchone()[0] == 20
    finally:
        conn.close()


def test_a_restaurant_with_history_before_the_table_is_backfilled_once():
    rid = _rid()
    from labor import load_shifts
    import csv
    import io
    rows = _rows(date(2026, 9, 1), 3)
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=list(rows[0].keys()))
    w.writeheader()
    w.writerows(rows)
    models.save_client_data(rid, "shifts", buf.getvalue(), source="upload")
    assert _facts(rid) == []
    assert shift_facts.backfill_from_csv() >= 1
    assert len(_facts(rid)) == 6
    assert shift_facts.backfill_from_csv() == 0                   # once
