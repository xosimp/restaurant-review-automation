"""Scheduler audit 10/10/26, workstream H1 — SCALE-5: the shift history's
record is shift_facts (each row whole, row_json), readers read windows by
date, a POS sync replaces only its window and never rewrites the three-year
file, and the file is an export rebuilt on demand."""
import sys
from datetime import date, timedelta

import pytest

import labor
import models
import shift_facts
from models import Restaurant, create_restaurant


@pytest.fixture(autouse=True)
def _db(db_path, monkeypatch):
    real = models.get_conn

    def conn(*a, **k):
        return real(db_path)
    for mod in list(sys.modules.values()):
        if mod is not None and getattr(mod, "get_conn", None) is real:
            monkeypatch.setattr(mod, "get_conn", conn)
    monkeypatch.setattr(models, "get_conn", conn)
    monkeypatch.setattr(models, "DB_PATH", db_path)
    yield


def _rows(start, days, names=("Ana Bee", "Cy Dee"), extra=True):
    out = []
    for i in range(days):
        d = start + timedelta(days=i)
        for n in names:
            r = {"date": d.isoformat(), "day": d.strftime("%A"), "employee": n, "role": "Server",
                 "shift_start": "16:00", "shift_end": "22:00", "scheduled_hours": "6", "actual_hours": "5.75",
                 "sales": "4200", "notes": "patio, busy"}
            if extra:
                r.update({"employee_ext_id": "E" + n[:1], "pay_rate": "12.5"})
            out.append(r)
    return out


def _key(r):
    return (r["date"], r["employee"], r.get("shift_start"))


def _file(rid):
    c = models.get_conn()
    try:
        d = dict(c.execute("SELECT shifts_csv, shifts_csv_z, shifts_store, shifts_stale FROM client_data "
                           "WHERE restaurant_id=?", (rid,)).fetchone())
        # On shift_facts the file is the facts' export, stored compressed
        # (blind re-audit SCALE-14): read back as text.
        d["plain_stored"] = bool(d.pop("shifts_csv"))
        d.pop("shifts_csv_z")
        d["shifts_csv"] = models.shift_file_text(c, rid)
        return d
    finally:
        c.close()


def test_a_sync_after_adoption_replaces_its_window_and_never_rewrites_the_file():
    rid = create_restaurant(Restaurant(name="Facts Co", owner_email="f@x.test"))
    today = date.today()
    shift_facts.ingest(rid, _rows(today - timedelta(days=120), 100), "rpower")
    first = _file(rid)
    assert first["shifts_store"] == "facts" and not first["shifts_stale"]
    # The next night's 60-day window: the file is not touched, only marked.
    window = _rows(today - timedelta(days=60), 61, names=("Ana Bee", "Cy Dee", "Eve Eff"))
    shift_facts.ingest(rid, window, "rpower")
    after = _file(rid)
    assert after["shifts_csv"] == first["shifts_csv"] and after["shifts_stale"] == 1
    # Every reader sees the merged history: older rows kept, the window replaced.
    rows = labor.load_shifts_for_restaurant(rid)
    assert len(rows) == 2 * 60 + 3 * 61                  # days -120..-61 kept, -60..0 the window
    assert {_key(r) for r in rows} >= {_key(r) for r in window}
    # A window by date reads only its dates.
    lo = (today - timedelta(days=7)).isoformat()
    assert {r["date"] for r in labor.shift_history(rid, since=lo)} == \
        {(today - timedelta(days=i)).isoformat() for i in range(8) if today - timedelta(days=i) <= today}
    # The file a reader of client_data gets is the export, rebuilt once.
    exported = models.get_client_data(rid)["shifts_csv"]
    assert _file(rid)["shifts_stale"] == 0 and _file(rid)["shifts_csv"] == exported
    parsed = labor.load_shifts(csv_string=exported)
    assert sorted(map(_key, parsed)) == sorted(map(_key, rows))


def test_a_stored_shift_reads_back_as_its_file_row_did():
    rid = create_restaurant(Restaurant(name="Whole Co", owner_email="w@x.test"))
    sent = _rows(date.today() - timedelta(days=10), 5)
    models.save_client_data(rid, "shifts", _csv(sent), source="upload")
    from_file = labor.load_shifts_for_restaurant(rid)
    assert shift_facts.adopt(rid)["adopted"] is True
    from_facts = labor.load_shifts_for_restaurant(rid)
    by = {_key(r): r for r in from_facts}
    for r in from_file:
        got = by[_key(r)]
        for k, v in r.items():
            assert got.get(k) == v, (k, v, got.get(k))


def _csv(rows):
    import csv
    import io
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=list(rows[0].keys()), lineterminator="\n")
    w.writeheader()
    w.writerows(rows)
    return buf.getvalue()


def test_rows_a_fact_cannot_hold_keep_the_restaurant_on_its_file():
    rid = create_restaurant(Restaurant(name="Dup Co", owner_email="d@x.test"))
    start = date.today() - timedelta(days=20)
    shift_facts.ingest(rid, _rows(start, 3), "rpower")
    assert _file(rid)["shifts_store"] == "facts"
    twice = _rows(start + timedelta(days=5), 1, names=("Ana Bee",)) * 2          # one shift sent twice
    shift_facts.ingest(rid, twice, "rpower")
    f = _file(rid)
    assert f["shifts_store"] is None
    assert f["shifts_csv"].count((start + timedelta(days=5)).isoformat()) == 2   # the file keeps what was sent
    assert len(labor.load_shifts_for_restaurant(rid)) == 6 + 2


def test_the_nightly_adoption_reaches_a_restaurant_that_only_uploaded():
    rid = create_restaurant(Restaurant(name="Up Co", owner_email="u@x.test"))
    models.save_client_data(rid, "shifts", _csv(_rows(date.today() - timedelta(days=30), 10)), source="upload")
    assert shift_facts.store_of(rid) is None
    out = shift_facts.adopt_pending(max_seconds=30)
    assert out["adopted"] >= 1 and shift_facts.store_of(rid) == "facts"
    assert len(labor.shift_history(rid)) == 20
