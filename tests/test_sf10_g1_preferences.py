"""Schedule fix round 10/10/26, workstream G1 — drop and claim preferences
read against their exposure (LEARNING-15), and the usual week said as what
was assigned, opened upward for somebody who wants more hours (LEARNING-28)."""
from datetime import date, timedelta

import pytest

import models
import schedule_intel as si
import shift_quality as sq
import staff_settings
from models import Restaurant, create_restaurant

HEAD = "date,day,employee,role,shift_start,shift_end,scheduled_hours,notes\n"


@pytest.fixture(autouse=True)
def _db(db_path, monkeypatch):
    real = models.get_conn
    fake = lambda *a, **k: real(db_path)  # noqa: E731
    monkeypatch.setattr(models, "get_conn", fake)
    monkeypatch.setattr(models, "DB_PATH", db_path)
    monkeypatch.setattr(staff_settings, "get_conn", fake)
    yield


def _rid():
    return create_restaurant(Restaurant(name="Prefs Co", owner_email="pf@x.test", module_labor=1))


def _day(weekday, weeks_ago):
    t = date.today()
    return t - timedelta(days=(t.weekday() - weekday) % 7) - timedelta(weeks=weeks_ago)


def _publish(rid, rows):
    by = {}
    for d, name, start in rows:
        ws = d - timedelta(days=d.weekday())
        by.setdefault(ws, []).append(f"{d.isoformat()},{d.strftime('%A')},{name},Server,{start},10:00pm,5,")
    conn = models.get_conn()
    for ws, lines in by.items():
        conn.execute("INSERT INTO schedule_history (restaurant_id, week_start, week_end, schedule_csv, published_at) "
                     "VALUES (?,?,?,?,datetime('now'))", (rid, ws.isoformat(), (ws + timedelta(days=6)).isoformat(),
                                                          HEAD + "\n".join(lines) + "\n"))
    conn.commit()
    conn.close()


def _req(rid, name, d, start, status="covered", replacement=None):
    conn = models.get_conn()
    hid = conn.execute("SELECT id FROM schedule_history WHERE restaurant_id=? ORDER BY id LIMIT 1",
                       (rid,)).fetchone()[0]
    conn.execute("INSERT INTO shift_change_requests (restaurant_id, history_id, employee_name, date, shift_start, "
                 "status, replacement_name, kind) VALUES (?,?,?,?,?,?,?, 'drop')",
                 (rid, hid, name, d.isoformat(), start, status, replacement))
    conn.commit()
    conn.close()


def test_two_drops_of_twelve_sunday_nights_is_not_avoiding_sunday_night():
    rid = _rid()
    suns = [_day(6, w) for w in range(1, 12)]
    _publish(rid, [(d, "Ana", "5:00pm") for d in suns] + [(_day(6, 12), "Bo", "5:00pm")])
    _req(rid, "Ana", suns[0], "5:00pm", status="open")
    _req(rid, "Ana", suns[1], "5:00pm", status="open")
    prefs = si.behaviour_preferences(rid)
    assert "Ana" not in prefs or prefs["Ana"]["avoids"] == [], "2 of 11 Sunday nights is not an avoid"
    ev = prefs.get("Ana", {}).get("evidence", {}).get("Sunday night") if "Ana" in prefs else None
    assert ev is None or ev["scheduled"] == 11


def test_dropping_every_sunday_night_one_was_on_is_avoiding_it():
    rid = _rid()
    suns = [_day(6, w) for w in (1, 2, 3)]
    _publish(rid, [(d, "Ana", "5:00pm") for d in suns])
    for d in suns:
        _req(rid, "Ana", d, "5:00pm", status="open")
    prefs = si.behaviour_preferences(rid)
    assert prefs["Ana"]["avoids"] == ["Sunday night"]
    ev = prefs["Ana"]["evidence"]["Sunday night"]
    assert (ev["dropped"], ev["scheduled"]) == (3, 3) and ev["drop_confidence"] >= si.AVOID_MIN_CONFIDENCE


def test_a_slot_preference_needs_open_shifts_elsewhere_left_alone():
    rid = _rid()
    tues = [_day(1, w) for w in (1, 2, 3)]
    suns = [_day(6, w) for w in (1, 2)]
    _publish(rid, [(d, "Cy", "11:00am") for d in tues] + [(d, "Dee", "5:00pm") for d in suns])
    for d in tues:
        _req(rid, "Cy", d, "11:00am", status="open")                     # offered, not taken by Ben
    for d in suns:
        _req(rid, "Dee", d, "5:00pm", status="covered", replacement="Ben")
    prefs = si.behaviour_preferences(rid)
    assert prefs["Ben"]["prefers"] == ["Sunday night"] and prefs["Ben"]["wants_hours"] is False
    assert prefs["Ben"]["evidence"]["Sunday night"]["other_claim_rate"] == 0.0


def test_the_usual_week_is_what_was_assigned_and_opens_up_for_who_wants_more():
    assert sq.usual_band({}) == (sq.USUAL_HOURS_BAND, sq.USUAL_HOURS_BAND)
    assert sq.usual_band({"wants_hours": True}) == (sq.USUAL_HOURS_BAND, sq.USUAL_HOURS_BAND_WANTS_MORE)
    # 30h against an assigned 20h week: over the band — unless they keep
    # picking up open shifts.
    lo, hi = sq.usual_band({"wants_hours": True})
    assert 20 * (1 + hi) >= 30 > 20 * (1 + sq.USUAL_HOURS_BAND)
