"""NS5 H2 — an edit to a week staff already have, inside the notice window,
answers with a predictability-pay warning worded as a possibility to check
with counsel (never a sum). Replays the audit's finding that the edit path
told staff and said nothing to the owner about pay."""
import datetime as dt

import test_edge_sched_publish as T
from test_edge_sched_publish import db, save_app  # noqa: F401  (fixtures)

import schedule_rules as sr
import schedule_versions as sv


def test_editing_a_published_week_inside_the_notice_window_warns(db, save_app, monkeypatch):  # noqa: F811
    import time_utils
    rid = T._restaurant(db, module_labor=1)
    sr.save_compliance(rid, {"notice_days": 14}, db_path=db)
    csv_text = T._week_csv(T.W1[:5])
    hid = T._save(db, rid, T.W1, csv_text, published=True)
    sv.append(rid, hid, "published", csv_text, saved_by="Owner")
    monkeypatch.setattr(time_utils, "restaurant_now_by_id", lambda *a, **k: dt.datetime(2026, 10, 1, 12, 0))
    rows = T._rows(csv_text)
    rows[0]["shift_start"] = "12:00pm"
    resp = T._post_save(save_app, T._bearer(db, rid), rows, history_id=hid, version=1)
    body = resp.get_json()
    assert resp.status_code == 200, body
    w = body.get("late_change_warning") or ""
    assert "inside your 14-day notice window" in w and "may owe" in w and "check with counsel" in w


def test_no_warning_without_a_notice_rule(db, save_app, monkeypatch):  # noqa: F811
    rid = T._restaurant(db, module_labor=1)
    csv_text = T._week_csv(T.W1[:5])
    hid = T._save(db, rid, T.W1, csv_text, published=True)
    sv.append(rid, hid, "published", csv_text, saved_by="Owner")
    rows = T._rows(csv_text)
    rows[0]["shift_start"] = "12:00pm"
    body = T._post_save(save_app, T._bearer(db, rid), rows, history_id=hid, version=1).get_json()
    assert body.get("late_change_warning") is None
