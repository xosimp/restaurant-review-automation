"""Tenure memory keeps ISO dates only (memory audit 9/29/26, tenure_date).

A legacy first_seen of "09/01/2026" sorted before every ISO date, so it won
first_seen=MIN(...) forever and passed Restaurant DNA's "180 days of
history" string comparison. Normalised once at boot; remember_tenure writes
ISO or nothing.
"""
from datetime import date

import pytest

import models
import schedule_intel
from models import Restaurant, create_restaurant


@pytest.fixture(autouse=True)
def _db(db_path, monkeypatch):
    real = models.get_conn
    monkeypatch.setattr(models, "get_conn", lambda *a, **k: real(db_path))
    monkeypatch.setattr(models, "DB_PATH", db_path)
    yield


def _rid():
    return create_restaurant(Restaurant(name="Tenure Co", owner_email="t@x.test"))


def _row(rid, name):
    conn = models.get_conn()
    try:
        r = conn.execute("SELECT first_seen, last_seen, shifts_seen FROM staff_first_seen "
                         "WHERE restaurant_id=? AND employee_name=?", (rid, name)).fetchone()
    finally:
        conn.close()
    return dict(r) if r else None


def test_a_legacy_us_date_is_normalised_to_iso_at_boot(db_path):
    rid = _rid()
    conn = models.get_conn()
    conn.execute("INSERT INTO staff_first_seen (restaurant_id, employee_name, first_seen, shifts_seen, last_seen) "
                 "VALUES (?,?,?,?,?)", (rid, "Maria G.", "09/01/2026", 4, "2026-09-20"))
    conn.execute("INSERT INTO staff_first_seen (restaurant_id, employee_name, first_seen, shifts_seen, last_seen) "
                 "VALUES (?,?,?,?,?)", (rid, "Tom B.", "2026-08-01", 4, "9/15/26"))
    conn.commit()
    conn.close()
    schedule_intel.init_schedule_intel(db_path)          # the boot pass
    assert _row(rid, "Maria G.")["first_seen"] == "2026-09-01"
    assert _row(rid, "Tom B.")["last_seen"] == "2026-09-15"
    # Idempotent: a second boot rewrites nothing.
    conn = models.get_conn()
    try:
        assert schedule_intel._normalise_tenure_dates(conn) == 0
    finally:
        conn.close()


def test_min_now_compares_real_dates_after_the_normalise(db_path):
    rid = _rid()
    conn = models.get_conn()
    conn.execute("INSERT INTO staff_first_seen (restaurant_id, employee_name, first_seen, shifts_seen, last_seen) "
                 "VALUES (?,?,?,?,?)", (rid, "Maria G.", "09/01/2026", 4, "2026-09-20"))
    conn.commit()
    conn.close()
    schedule_intel.init_schedule_intel(db_path)
    # An earlier real shift now wins MIN, as it always should have.
    schedule_intel.remember_tenure(rid, [{"employee": "Maria G.", "date": "2026-03-02"}], db_path=db_path)
    assert _row(rid, "Maria G.")["first_seen"] == "2026-03-02"


def test_remember_tenure_writes_iso_whatever_the_row_carried(db_path):
    rid = _rid()
    schedule_intel.remember_tenure(rid, [{"employee": "Ana P.", "date": "09/14/2026"},
                                         {"employee": "Ana P.", "date": "2026-09-16"},
                                         {"employee": "Ana P.", "date": "not a date"}], db_path=db_path)
    row = _row(rid, "Ana P.")
    assert row["first_seen"] == "2026-09-14" and row["last_seen"] == "2026-09-16"
    assert row["shifts_seen"] == 2          # the unreadable date is not a shift


def test_dna_retention_no_longer_passes_on_a_legacy_date():
    """dna._retention's "180 days of history" check read '09/01/2026' as the
    earliest date there is; with ISO it reads the real history length."""
    from intelligence import dna
    today = date(2026, 9, 29)
    legacy = [{"first_seen": "09/01/2026", "last_seen": "2026-09-20", "shifts_seen": 5}] * 12
    assert "180 days" not in (dna._retention(legacy, today).get("basis") or "")   # the old bug: it passed
    staff = [{"first_seen": "2026-09-01", "last_seen": "2026-09-20", "shifts_seen": 5}] * 12
    out = dna._retention(staff, today)
    assert out["raw"] is None and "180 days" in out["basis"]    # under 180 days of history: no reading
