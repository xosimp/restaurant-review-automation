"""The brief's "Yesterday's sheets left unfinished" line (Simple EJ's,
10/5/26): with no published schedule every sheet is unassigned and nobody
can own it, so "Server PM sheet: 0 of 9 done (no one scheduled)" came every
morning. The line keeps evaluate()'s rule: an unassigned sheet nobody
touched is not work left undone; one someone started, or one with a name
on it, still is."""
import json
from datetime import date

import models
import task_sheets


def _row(conn, rid, sheet_id, assignees, unassigned, lines=3):
    sheet_id = conn.execute("INSERT INTO task_sheets (restaurant_id, job_code, shift_kind, title) "
                            "VALUES (?, 'Server PM', 'any', ?)", (rid, "Sheet %d" % sheet_id)).lastrowid or sheet_id
    ls = [{"line_id": i, "label": "Line %d" % i} for i in range(1, lines + 1)]
    conn.execute("INSERT INTO task_assignments (restaurant_id, sheet_id, task_date, shift_kind, job_code, title, "
                 "assignees_json, unassigned, lines_json, line_count, status, sheet_version) "
                 "VALUES (?, ?, '2026-10-05', 'any', 'Server PM', ?, ?, ?, ?, ?, 'missed', 1)",
                 (rid, sheet_id, "Sheet %d" % sheet_id, json.dumps(assignees), unassigned, json.dumps(ls), lines))


def test_an_untouched_unassigned_sheet_is_not_reported(db_path, monkeypatch):
    real = models.get_conn
    monkeypatch.setattr(task_sheets, "get_conn", lambda *a, **k: real(db_path))
    rid = models.create_restaurant(models.Restaurant(name="EJ", owner_email="e@x.test"), db_path=db_path)
    conn = real(db_path)
    _row(conn, rid, 1, [], 1)
    conn.commit()
    conn.close()
    assert task_sheets.yesterday_line(rid, today=date(2026, 10, 6), db_path=db_path) is None
    conn = real(db_path)
    _row(conn, rid, 2, ["Kerri Trejo"], 0)
    conn.commit()
    conn.close()
    line = task_sheets.yesterday_line(rid, today=date(2026, 10, 6), db_path=db_path)
    assert line and "Sheet 2: 0 of 3 done (Kerri Trejo)" in line["text"] and "Sheet 1" not in line["text"]
