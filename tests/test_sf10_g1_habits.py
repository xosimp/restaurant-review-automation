"""Schedule fix round 10/10/26, workstream G1 — learned habits that stick
(LEARNING-1, RM-5): a week the draft kept a habit and that was worked as
published renews its decay, and a habit that has bound the passes stays
bound until its lifecycle (a failed re-test, two reversals, dormancy, the
owner) ends it — never because a confidence decayed while it was working."""
from datetime import date, timedelta

import pytest

import models
import schedule_memory
import schedule_versions
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
    import client_api
    monkeypatch.setattr(client_api, "invalidate_insight_cache", lambda *a, **k: None)
    yield


HEAD = "date,day,employee,role,shift_start,shift_end,scheduled_hours,notes\n"
EVERYONE, WITHOUT_BOB = ["Ana", "Bob", "Cy"], ["Ana", "Cy"]


def _rid():
    return create_restaurant(Restaurant(name="Habits Co", owner_email="h@x.test", module_labor=1))


def _csv(tuesday, people_):
    wed = (tuesday + timedelta(days=1)).isoformat()
    return HEAD + "".join(f"{tuesday.isoformat()},Tuesday,{p},Server,5:00pm,10:00pm,5,\n" for p in people_) \
        + f"{wed},Wednesday,Bob,Server,11:00am,3:00pm,4,\n"


def _week(rid, tuesday, generated, final, age_days=0):
    conn = models.get_conn()
    ws = tuesday - timedelta(days=1)
    hid = conn.execute("INSERT INTO schedule_history (restaurant_id, week_start, week_end, schedule_csv, "
                       "published_at, generated_at) VALUES (?,?,?,?,datetime('now', ?),datetime('now', ?))",
                       (rid, ws.isoformat(), (ws + timedelta(days=6)).isoformat(), _csv(tuesday, final),
                        f"-{age_days} days", f"-{age_days} days")).lastrowid
    conn.commit()
    conn.close()
    schedule_versions.append(rid, hid, "generated", _csv(tuesday, generated), saved_by="cavnar",
                             saved_authority="system")
    if final != generated:
        schedule_versions.append(rid, hid, "edited", _csv(tuesday, final), saved_by="dana",
                                 saved_authority="principal")
    schedule_versions.append(rid, hid, "published", _csv(tuesday, final), saved_by="dana",
                             saved_authority="principal")
    conn = models.get_conn()
    conn.execute("UPDATE schedule_versions SET created_at=datetime('now', ?) WHERE history_id=?",
                 (f"-{age_days} days", hid))
    conn.commit()
    conn.close()
    return hid


def _tuesday(weeks_ago):
    today = date.today()
    return today - timedelta(days=(today.weekday() - 1) % 7) - timedelta(weeks=weeks_ago)


def _bob_off(rid):
    return next(s for s in schedule_versions.standing_patterns(rid)
                if s["employee"] == "Bob" and s["kind"] == "moved_off")


def _memory_status(rid):
    schedule_memory.consolidate(rid)
    conn = models.get_conn()
    try:
        row = conn.execute("SELECT status FROM schedule_memory WHERE restaurant_id=? AND kind='moved_off' "
                           "AND person='Bob'", (rid,)).fetchone()
    finally:
        conn.close()
    return row["status"] if row else None


def _taught_then_kept(rid, kept_weeks=10):
    # The manager takes Bob off Tuesday dinner twice by hand, twelve weeks
    # ago; from then on every draft keeps him off and every week goes out
    # (and is worked) that way.
    _week(rid, _tuesday(12), EVERYONE, WITHOUT_BOB, age_days=84)
    _week(rid, _tuesday(11), EVERYONE, WITHOUT_BOB, age_days=77)
    schedule_versions.refresh_standing_patterns(rid)
    for w in range(kept_weeks, 0, -1):
        _week(rid, _tuesday(w), WITHOUT_BOB, WITHOUT_BOB, age_days=7 * w)


def test_a_habit_the_draft_keeps_every_week_does_not_fade_out_of_force():
    rid = _rid()
    _taught_then_kept(rid)
    stats = schedule_versions.refresh_standing_patterns(rid)
    row = _bob_off(rid)
    assert stats["renewed"] >= 1
    assert row["last_kept_iso"] == _tuesday(1).isoformat()
    # Before: the decay ran from the last hand edit only, 0.62 x Wilson(12/12)
    # = 0.50 — under ACTIVE_CONFIDENCE, so the memory dropped it to a prompt
    # line while the draft was still carrying it every week.
    assert row["confidence"] >= schedule_memory.ACTIVE_CONFIDENCE
    assert row["enforced_since"]
    assert _memory_status(rid) == "active"


def test_a_habit_that_bound_stays_bound_while_its_confidence_decays():
    rid = _rid()
    _taught_then_kept(rid)
    schedule_versions.refresh_standing_patterns(rid)
    assert _bob_off(rid)["enforced_since"]
    # The confidence decays with no new evidence (a long quiet spell) — the
    # lifecycle, not the decay, decides: still applied, not a candidate.
    conn = models.get_conn()
    conn.execute("UPDATE schedule_standing_patterns SET confidence=0.41 WHERE restaurant_id=? AND kind='moved_off'",
                 (rid,))
    conn.commit()
    conn.close()
    assert _memory_status(rid) == "active"


def test_a_born_pattern_is_not_bound_until_it_clears_the_line():
    rid = _rid()
    _week(rid, _tuesday(2), EVERYONE, WITHOUT_BOB, age_days=14)
    _week(rid, _tuesday(1), EVERYONE, WITHOUT_BOB, age_days=7)
    schedule_versions.refresh_standing_patterns(rid)
    row = _bob_off(rid)
    assert row["confidence"] < schedule_memory.ACTIVE_CONFIDENCE and not row["enforced_since"]
    assert _memory_status(rid) == "candidate"


def test_a_kept_week_the_punches_undid_does_not_renew_it():
    rid = _rid()
    _taught_then_kept(rid, kept_weeks=1)
    # The published week kept Bob off Tuesday dinner — but he worked it.
    tue = _tuesday(1)
    shift_facts.ingest(rid, [{"date": tue.isoformat(), "day": "Tuesday", "employee": "Bob", "role": "Server",
                              "shift_start": "5:00pm", "shift_end": "10:00pm", "hours": 5.0},
                             {"date": tue.isoformat(), "day": "Tuesday", "employee": "Ana", "role": "Server",
                              "shift_start": "5:00pm", "shift_end": "10:00pm", "hours": 5.0}], "upload")
    schedule_versions.refresh_standing_patterns(rid)
    row = _bob_off(rid)
    assert row["last_kept_iso"] is None, "a week Bob worked the slot renewed 'Bob off Tuesday dinner'"


def test_a_kept_week_worked_as_published_renews_it_with_a_pos():
    rid = _rid()
    _taught_then_kept(rid, kept_weeks=1)
    tue = _tuesday(1)
    shift_facts.ingest(rid, [{"date": tue.isoformat(), "day": "Tuesday", "employee": "Ana", "role": "Server",
                              "shift_start": "5:00pm", "shift_end": "10:00pm", "hours": 5.0}], "upload")
    schedule_versions.refresh_standing_patterns(rid)
    assert _bob_off(rid)["last_kept_iso"] == tue.isoformat()


def test_two_reversals_still_retire_a_bound_habit_and_unbind_it():
    rid = _rid()
    _taught_then_kept(rid, kept_weeks=4)
    schedule_versions.refresh_standing_patterns(rid)
    assert _bob_off(rid)["enforced_since"]
    _week(rid, _tuesday(0), WITHOUT_BOB, EVERYONE, age_days=3)
    _week(rid, _tuesday(-1), WITHOUT_BOB, EVERYONE, age_days=1)
    schedule_versions.refresh_standing_patterns(rid)
    row = _bob_off(rid)
    assert row["status"] == "retired" and not row["enforced_since"]
