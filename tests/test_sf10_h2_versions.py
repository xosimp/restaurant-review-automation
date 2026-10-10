"""Scheduler audit 10/10/26, workstream H2 — SCALE-34 / RM-14: a version is
kept as its difference from the week's snapshot, so a save writes the
week's whole text once (the working copy), not twice; every reader gets the
version's exact text back (schedule_versions.materialize); the prune never
removes a snapshot a kept version is stored against."""
import json

import pytest

import models
import schedule_rows as srows
import schedule_versions as sv
from models import Restaurant, create_restaurant


def _week(n=40, change=None):
    rows = []
    for i in range(n):
        d = f"2026-10-{19 + (i % 7):02d}"
        rows.append({"date": d, "day": "Mon", "employee": f"Person {i}", "role": "Server",
                     "shift_start": "10:00am", "shift_end": "4:00pm", "scheduled_hours": "6", "notes": "",
                     "shift_id": f"{i:032x}", "person_id": str(i + 1), "locked": ""})
    if change:
        change(rows)
    return srows.to_csv(rows)


@pytest.fixture
def week(db_path):
    rid = create_restaurant(Restaurant(name="Version Grill", owner_email="v@x.com"), db_path=db_path)
    conn = models.get_conn(db_path)
    try:
        hid = conn.execute("INSERT INTO schedule_history (restaurant_id, week_start, week_end, schedule_csv) "
                           "VALUES (?,?,?,?)", (rid, "2026-10-19", "2026-10-25", _week())).lastrowid
        conn.commit()
    finally:
        conn.close()
    return db_path, rid, hid


def _all(field, value, row=None, note=None):
    def change(rows):
        for r in rows:
            r[field] = value
        if row is not None:
            rows[row]["notes"] = note
    return change


def _rows(db_path, hid):
    conn = models.get_conn(db_path)
    try:
        return [dict(r) for r in conn.execute("SELECT version, reason, schedule_csv, delta_json, base_version "
                                              "FROM schedule_versions WHERE history_id=? ORDER BY version", (hid,))]
    finally:
        conn.close()


def test_the_delta_rebuilds_the_text_exactly():
    a = _week()
    b = _week(change=lambda rows: rows[3].update(shift_start="11:00am") or rows.pop(10))
    d = sv.encode_delta(a, b)
    assert sv.apply_delta(a, d) == b
    assert len(json.dumps(d)) < 0.2 * len(b)
    # Text the snapshot never had, and an empty week, still round-trip.
    assert sv.apply_delta(a, sv.encode_delta(a, "date,day\nx,y")) == "date,day\nx,y"
    assert sv.apply_delta(a, sv.encode_delta(a, "")) == ""


def test_a_save_stores_its_difference_not_a_second_copy(week):
    db, rid, hid = week
    texts = [_week(), _week(change=lambda r: r[0].update(shift_end="5:00pm")),
             _week(change=lambda r: r[1].update(employee="Person 99")),
             _week(change=lambda r: r.pop(5))]
    reasons = ["generated", "edited", "edited", "published"]
    for t, why in zip(texts, reasons):
        sv.append(rid, hid, why, t, db_path=db)
    stored = _rows(db, hid)
    assert stored[0]["delta_json"] is None and stored[0]["schedule_csv"] == texts[0]
    for r in stored[1:]:
        assert r["schedule_csv"] == "" and r["base_version"] == 1 and r["delta_json"]
    whole = sum(len(t) for t in texts)
    kept = sum(len(r["schedule_csv"]) + len(r["delta_json"] or "") for r in stored)
    assert kept < 0.4 * whole                    # one text and three small differences
    conn = models.get_conn(db)
    try:
        got = sv.materialize(conn, conn.execute(
            "SELECT version, " + sv.VERSION_TEXT + " FROM schedule_versions WHERE history_id=? ORDER BY version",
            (hid,)).fetchall(), hid)
        assert [g["schedule_csv"] for g in got] == texts
        assert sv.rows_from_csv(texts[-1]) == sv.latest_rows(conn, hid)
    finally:
        conn.close()
    # The readers built on the versions see the same weeks.
    d = sv.draft_vs_published(rid, hid, db_path=db)
    assert d["available"] and d["removed"]
    conn = models.get_conn(db)
    try:
        assert sv.rows_changed_since(conn, hid, 1) == 4
    finally:
        conn.close()


def test_a_week_rewritten_whole_is_a_new_snapshot(week):
    db, rid, hid = week
    sv.append(rid, hid, "generated", _week(), db_path=db)
    other = _week(n=40, change=lambda rows: [r.update(shift_start="5:00pm", shift_end="11:00pm") for r in rows])
    sv.append(rid, hid, "generated", other, db_path=db)
    stored = _rows(db, hid)
    assert stored[1]["delta_json"] is None and stored[1]["schedule_csv"] == other
    shifted = _week(n=40, change=lambda rows: [r.update(shift_start="5:00pm", shift_end="11:00pm") for r in rows]
                    and rows[0].update(notes="Opener"))
    assert "Opener" in shifted
    sv.append(rid, hid, "edited", shifted, db_path=db)
    assert _rows(db, hid)[2]["base_version"] == 2          # against the newest snapshot


def test_the_prune_never_removes_a_snapshot_a_kept_version_reads(week, monkeypatch):
    import ops
    db, rid, hid = week
    sv.append(rid, hid, "generated", _week(), db_path=db)
    sv.append(rid, hid, "edited", _week(change=_all("role", "Host")), db_path=db)     # an 'edited' snapshot
    sv.append(rid, hid, "edited", _week(change=_all("role", "Host", 0, "x")), db_path=db)
    sv.append(rid, hid, "edited", _week(change=_all("role", "Host", 1, "y")), db_path=db)
    stored = _rows(db, hid)
    assert stored[1]["delta_json"] is None and stored[2]["base_version"] == 2
    conn = models.get_conn(db)
    try:
        conn.execute("UPDATE schedule_versions SET created_at=datetime('now', '-400 days') WHERE history_id=?",
                     (hid,))
        conn.commit()
        import time
        ops._prune_schedules(conn, time.monotonic() + 30)
        left = [r["version"] for r in conn.execute("SELECT version FROM schedule_versions WHERE history_id=? "
                                                   "ORDER BY version", (hid,))]
        # Version 3 (an intermediate difference) may go; the snapshot (2)
        # the newest (4) is stored against never does.
        assert 2 in left and 4 in left and 1 in left
        newest = sv.latest_rows(conn, hid)
    finally:
        conn.close()
    assert newest[1]["notes"] == "y"
