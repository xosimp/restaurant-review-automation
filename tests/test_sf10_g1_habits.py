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
def _two_edit_candidates(monkeypatch):
    """These tests are about their own subject, built on two-edit fixtures
    from before the candidacy floor (schedule re-audit 10/11/26
    LEARNING-10: three opportunities and a Wilson lower bound of 0.3 —
    tests/test_sf11_g2_habits.py holds it). The old floor keeps each test
    about its subject; enforcement is still earned by hand
    (schedule_versions.habit_enforceable)."""
    monkeypatch.setattr(schedule_versions, "CANDIDATE_MIN_OPPORTUNITIES", 2)
    monkeypatch.setattr(schedule_versions, "CANDIDATE_MIN_LOWER", 0.0)


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
    # The manager takes Bob off Tuesday dinner three times by hand, about
    # twelve weeks ago (what binds since re-audit 10/11/26 LEARNING-9/11:
    # schedule_versions.ENFORCE_MIN_HAND); from then on every draft keeps him
    # off and every week goes out (and is worked) that way.
    _week(rid, _tuesday(13), EVERYONE, WITHOUT_BOB, age_days=91)
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
    # The kept weeks renew its decay (LEARNING-1) but are not evidence
    # (re-audit 10/11/26 LEARNING-9): the hand's 3 of 3 alone.
    assert (row["hits"], row["opportunities"]) == (3, 3)
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
    # Two hand edits (under this module's two-edit floor): a candidate, not
    # bound — binding takes ENFORCE_MIN_HAND (LEARNING-9/11).
    assert not row["enforced_since"]
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


# ── LEARNING-2: a new habit is read from its onset ──────────────────────────

def test_a_new_habit_in_a_mature_restaurant_is_a_pattern_after_two_weeks_and_binds_after_three():
    rid = _rid()
    # 19 weeks the draft had Bob on Tuesday dinner and nobody touched it.
    for w in range(23, 4, -1):
        _week(rid, _tuesday(w), EVERYONE, EVERYONE, age_days=7 * w)
    # A new manager starts taking him off.
    _week(rid, _tuesday(4), EVERYONE, WITHOUT_BOB, age_days=28)
    _week(rid, _tuesday(3), EVERYONE, WITHOUT_BOB, age_days=21)
    pats = [p for p in schedule_versions.learned_patterns(rid) if p["kind"] == "moved_off" and p["employee"] == "Bob"]
    # Before: 2 hits over 21 opportunities, a rate of ~0.1 — not a pattern
    # for another ten weeks.
    assert pats and pats[0]["times"] == 2 and pats[0]["opportunities"] == 2 and pats[0]["onset"]
    assert not schedule_versions.habit_enforceable({"hits": 2, "opportunities": 2})
    _week(rid, _tuesday(2), EVERYONE, WITHOUT_BOB, age_days=14)
    pats = [p for p in schedule_versions.learned_patterns(rid) if p["kind"] == "moved_off" and p["employee"] == "Bob"]
    # The third hand edit in a row binds (re-audit 10/11/26 LEARNING-11: it
    # took five).
    assert pats[0]["times"] == 3
    assert schedule_versions.habit_enforceable({"hits": pats[0]["times"], "opportunities": pats[0]["opportunities"]})


def _edit_save(rid, hid, rows_csv, published=False):
    conn = models.get_conn()
    try:
        conn.execute("BEGIN IMMEDIATE")
        before = schedule_versions.latest_rows(conn, hid)
        after = schedule_versions.rows_from_csv(rows_csv)
        step = schedule_versions.step_origins(before, after)
        version = schedule_versions.write_on(conn, rid, hid, "edited", rows_csv, saved_by="erik",
                                             saved_authority="principal", row_origins=step["stored"])
        conn.commit()
    finally:
        conn.close()
    import schedule_learning
    return schedule_learning.capture_save(rid, hid, version, step,
                                          user={"id": 1, "username": "erik", "role": "owner"}, published=published)


def _draft(rid, tuesday):
    conn = models.get_conn()
    ws = tuesday - timedelta(days=1)
    hid = conn.execute("INSERT INTO schedule_history (restaurant_id, week_start, week_end, schedule_csv) "
                       "VALUES (?,?,?,?)", (rid, ws.isoformat(), (ws + timedelta(days=6)).isoformat(),
                                            _csv(tuesday, EVERYONE))).lastrowid
    conn.commit()
    conn.close()
    schedule_versions.append(rid, hid, "generated", _csv(tuesday, EVERYONE), saved_by="Cavnar AI",
                             saved_authority="system")
    return hid


def test_the_second_time_a_change_repeats_it_is_asked_about_at_any_age():
    rid = _rid()
    for w in range(9, 4, -1):                      # well past the first four weeks
        _week(rid, _tuesday(w), EVERYONE, EVERYONE, age_days=7 * w)
    # The first time: asked too, at any age (re-audit 10/11/26 LEARNING-10 —
    # a one-off is named before it can count toward a habit), not a repeat.
    hid = _draft(rid, _tuesday(1))
    first = _edit_save(rid, hid, _csv(_tuesday(1), WITHOUT_BOB))["why_questions"]
    assert len(first) == 1 and first[0]["repeat"] == 0
    schedule_versions.append(rid, hid, "published", _csv(_tuesday(1), WITHOUT_BOB), saved_by="erik",
                             saved_authority="principal")
    schedule_versions.observe_publish(rid, hid, authority="principal", editor="erik")
    # The second time, a week later: the one-tap "Always / just this week".
    hid2 = _draft(rid, _tuesday(0))
    q = _edit_save(rid, hid2, _csv(_tuesday(0), WITHOUT_BOB))["why_questions"]
    assert len(q) == 1 and q[0]["kind"] == "moved_off" and q[0]["employee"] == "Bob" and q[0]["repeat"] == 1
    assert [o["answer"] for o in q[0]["options"]] == ["always", "this_week"]
