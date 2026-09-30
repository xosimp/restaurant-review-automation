"""The standing schedule patterns' lifecycle (memory re-audit 9/29/26, R6).

LOOPS-2 / QUALITY-2: a retired pattern kept steering the draft through the
live window's copy, and the next nightly refresh set it active again from
the same old edits. FORGET-5: two reversals a year apart retired a pattern
kept 40 weeks; a pattern about someone who left was never retired.
INVENTORY-2: a rename stranded a pattern under a name no schedule carried,
where it read as kept every week. QUALITY-14: the weeks that taught a
pattern, and every week the draft merely carried it, counted as the manager
confirming it. QUALITY-3: headcount learning was forgotten as soon as it
worked.
"""
from datetime import date, timedelta

import pytest

import models
import people
import schedule_intel
import schedule_learning
import schedule_versions
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


def _rid():
    return create_restaurant(Restaurant(name="Patterns Co", owner_email="p@x.test", module_labor=1))


HEAD = "date,day,employee,role,shift_start,shift_end,scheduled_hours,notes\n"


def _csv(tuesday, people_, wednesday=("Bob",), role="Server"):
    wed = (tuesday + timedelta(days=1)).isoformat()
    return HEAD + "".join(f"{tuesday.isoformat()},Tuesday,{p},{role},5:00pm,10:00pm,5,\n" for p in people_) \
        + "".join(f"{wed},Wednesday,{p},Server,11:00am,3:00pm,4,\n" for p in wednesday)


def _week(rid, tuesday, generated, final, editor="dana", age_days=0, wednesday=("Bob",)):
    conn = models.get_conn()
    ws = tuesday - timedelta(days=1)
    hid = conn.execute("INSERT INTO schedule_history (restaurant_id, week_start, week_end, schedule_csv, "
                       "published_at, generated_at) VALUES (?,?,?,?,datetime('now', ?),datetime('now', ?))",
                       (rid, ws.isoformat(), (ws + timedelta(days=6)).isoformat(),
                        _csv(tuesday, final, wednesday), f"-{age_days} days", f"-{age_days} days")).lastrowid
    conn.commit()
    conn.close()
    schedule_versions.append(rid, hid, "generated", _csv(tuesday, generated, wednesday), saved_by="cavnar",
                             saved_authority="system")
    if final != generated:
        schedule_versions.append(rid, hid, "edited", _csv(tuesday, final, wednesday), saved_by=editor,
                                 saved_authority="principal")
    schedule_versions.append(rid, hid, "published", _csv(tuesday, final, wednesday), saved_by=editor,
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


EVERYONE, WITHOUT_BOB = ["Ana", "Bob", "Cy"], ["Ana", "Cy"]


def _learn_then_reverse_twice(rid):
    _week(rid, _tuesday(6), EVERYONE, WITHOUT_BOB, age_days=40)
    _week(rid, _tuesday(5), EVERYONE, WITHOUT_BOB, age_days=33)
    schedule_versions.refresh_standing_patterns(rid)
    _week(rid, _tuesday(2), WITHOUT_BOB, EVERYONE, age_days=12)
    _week(rid, _tuesday(1), WITHOUT_BOB, EVERYONE, age_days=5)
    schedule_versions.refresh_standing_patterns(rid)


# ── LOOPS-2 / QUALITY-2 ─────────────────────────────────────────────────────

def test_a_retired_pattern_is_not_drafted_and_not_reactivated_the_next_night():
    rid = _rid()
    _learn_then_reverse_twice(rid)
    row = _bob_off(rid)
    assert row["status"] == "retired" and row["times_overridden"] == 2 and row["retired_at"]
    drafted, _c = schedule_versions.patterns_for_draft(rid)
    assert not [p for p in drafted if p.get("employee") == "Bob" and p["kind"] == "moved_off"], \
        "the draft was still told to keep Bob off (the live copy)"
    schedule_versions.refresh_standing_patterns(rid)            # the next night
    row = _bob_off(rid)
    assert row["status"] == "retired" and row["times_overridden"] == 2


def test_the_managers_add_back_clears_the_live_evidence():
    rid = _rid()
    _week(rid, _tuesday(6), EVERYONE, WITHOUT_BOB, age_days=40)
    _week(rid, _tuesday(5), EVERYONE, WITHOUT_BOB, age_days=33)
    assert [p for p in schedule_versions.learned_patterns(rid) if p["kind"] == "moved_off" and p["employee"] == "Bob"]
    # The same manager puts Bob back on Tuesday dinner: an `added` row.
    _week(rid, _tuesday(4), WITHOUT_BOB, EVERYONE, age_days=26)
    assert not [p for p in schedule_versions.learned_patterns(rid)
                if p["kind"] == "moved_off" and p["employee"] == "Bob"]


def test_newer_evidence_brings_a_retired_pattern_back():
    rid = _rid()
    _learn_then_reverse_twice(rid)
    assert _bob_off(rid)["status"] == "retired"
    # The manager changes their mind again — two NEW weeks taking Bob off.
    _week(rid, _tuesday(0), EVERYONE, WITHOUT_BOB, age_days=2)
    _week(rid, _tuesday(-1), EVERYONE, WITHOUT_BOB, age_days=1)
    schedule_versions.refresh_standing_patterns(rid)
    row = _bob_off(rid)
    assert row["status"] == "active" and row["times_overridden"] == 0


# ── FORGET-5: overrides in a rolling window ────────────────────────────────

def test_two_reversals_far_apart_do_not_retire_a_long_kept_pattern():
    rid = _rid()
    _week(rid, _tuesday(30), EVERYONE, WITHOUT_BOB, age_days=5)
    _week(rid, _tuesday(29), EVERYONE, WITHOUT_BOB, age_days=5)
    schedule_versions.refresh_standing_patterns(rid)
    # One reversal, then months of weeks keeping it, then another reversal.
    _week(rid, _tuesday(20), WITHOUT_BOB, EVERYONE, age_days=4)
    for w in range(19, 3, -1):
        _week(rid, _tuesday(w), WITHOUT_BOB, WITHOUT_BOB, age_days=4)
    _week(rid, _tuesday(3), WITHOUT_BOB, EVERYONE, age_days=3)
    conn = models.get_conn()
    # Publish dates as the weeks were really published.
    for hid, wk in conn.execute("SELECT id, week_start FROM schedule_history WHERE restaurant_id=?", (rid,)).fetchall():
        conn.execute("UPDATE schedule_history SET published_at=? WHERE id=?", (wk + " 12:00:00", hid))
    conn.commit()
    conn.close()
    schedule_versions.refresh_standing_patterns(rid)
    row = _bob_off(rid)
    assert row["status"] == "active", "two reversals 17 weeks apart retired it"
    assert row["times_overridden"] == 1


# ── QUALITY-14: what confirms a pattern ─────────────────────────────────────

def test_the_teaching_weeks_and_draft_carried_weeks_are_not_the_managers_confirmation():
    rid = _rid()
    _week(rid, _tuesday(6), EVERYONE, WITHOUT_BOB, age_days=40)
    _week(rid, _tuesday(5), EVERYONE, WITHOUT_BOB, age_days=33)
    stats = schedule_versions.refresh_standing_patterns(rid)
    assert stats["learned"] >= 1 and stats["confirmed"] == 0, "the two teaching weeks confirmed it"
    # Two weeks the draft carried it and nobody touched Tuesday dinner.
    _week(rid, _tuesday(3), WITHOUT_BOB, WITHOUT_BOB, age_days=20)
    _week(rid, _tuesday(2), WITHOUT_BOB, WITHOUT_BOB, age_days=13)
    schedule_versions.refresh_standing_patterns(rid)
    row = _bob_off(rid)
    assert row["times_applied"] == 2 and row["times_confirmed"] == 0
    # A week the draft forgot and the manager took him off again themself.
    _week(rid, _tuesday(1), EVERYONE, WITHOUT_BOB, age_days=6)
    schedule_versions.refresh_standing_patterns(rid)
    row = _bob_off(rid)
    assert row["times_applied"] == 3 and row["times_confirmed"] == 1


def test_a_person_who_left_goes_dormant_and_wakes_when_they_return():
    rid = _rid()
    _week(rid, _tuesday(8), EVERYONE, WITHOUT_BOB, age_days=40)
    _week(rid, _tuesday(7), EVERYONE, WITHOUT_BOB, age_days=33)
    schedule_versions.refresh_standing_patterns(rid)
    assert _bob_off(rid)["status"] == "active"
    # Bob leaves: five weeks with no shift for him at all.
    for w in range(5, 0, -1):
        _week(rid, _tuesday(w), WITHOUT_BOB, WITHOUT_BOB, age_days=7 * w, wednesday=("Ana",))
    before = _bob_off(rid)["times_applied"]
    schedule_versions.refresh_standing_patterns(rid)
    row = _bob_off(rid)
    assert row["status"] == "dormant" and row["dormant_since"]
    assert row["times_applied"] == before, "a week Bob was not on the schedule counted as keeping it"
    drafted, _c = schedule_versions.patterns_for_draft(rid)
    assert not [p for p in drafted if p.get("employee") == "Bob"]
    lines = people._mem_standing(rid, date.today(), None)
    assert not [l for l in lines if "Bob" in l["text"] and "Standing preference" in l["text"]]
    # He is back.
    _week(rid, _tuesday(0), WITHOUT_BOB, WITHOUT_BOB, age_days=0)
    stats = schedule_versions.refresh_standing_patterns(rid)
    assert stats["woke"] == 1 and _bob_off(rid)["status"] == "active"


# ── INVENTORY-2: a rename carries the pattern and its dismissal ─────────────

def test_a_rename_rekeys_the_standing_pattern_and_the_dismissal():
    rid = _rid()
    _week(rid, _tuesday(6), EVERYONE, WITHOUT_BOB, age_days=40)
    _week(rid, _tuesday(5), EVERYONE, WITHOUT_BOB, age_days=33)
    people.stamp_person_ids(rid)
    schedule_versions.refresh_standing_patterns(rid)
    on_key = "moved_on|bob|Friday|night"
    schedule_intel.dismiss_pattern(rid, on_key, actor="owner")
    pid = people.person_id_for(rid, "Bob")
    people.rename_person(rid, pid, "Bob Smith")
    rows = [s for s in schedule_versions.standing_patterns(rid) if s["kind"] == "moved_off"]
    assert [r["employee"] for r in rows] == ["Bob Smith"]
    assert rows[0]["key"] == "moved_off|bob smith|Tuesday|night" and "Bob Smith" in rows[0]["text"]
    assert schedule_intel.dismissed_patterns(rid) == {"moved_on|bob smith|Friday|night"}
    # The old weeks, still spelled "Bob", read as Bob Smith: one pattern, not two.
    live = [p for p in schedule_versions.learned_patterns(rid) if p["kind"] == "moved_off"]
    assert [p["employee"] for p in live] == ["Bob Smith"]
    schedule_versions.refresh_standing_patterns(rid)
    assert len([s for s in schedule_versions.standing_patterns(rid) if s["kind"] == "moved_off"]) == 1


# ── QUALITY-3: headcount stays learned once it works ────────────────────────

def test_headcount_learning_outlives_the_window_and_two_cuts_retire_it():
    rid = _rid()
    three, four = ["Ana", "Bob", "Cy"], ["Ana", "Bob", "Cy", "Dee"]
    _week(rid, _tuesday(6), three, four, age_days=40)
    _week(rid, _tuesday(5), three, four, age_days=33)
    assert schedule_learning.learned_headcount_adjustments(rid) == {("Tuesday", "night"): {"Server": 1}}
    schedule_versions.refresh_standing_patterns(rid)
    for w in range(4, 2, -1):
        _week(rid, _tuesday(w), four, four, age_days=7 * w)
    conn = models.get_conn()
    conn.execute("UPDATE schedule_versions SET created_at=datetime('now','-70 days') WHERE reason='edited' "
                 "AND restaurant_id=?", (rid,))
    conn.commit()
    conn.close()
    schedule_versions.refresh_standing_patterns(rid)
    assert schedule_learning.learned_headcount_adjustments(rid) == {("Tuesday", "night"): {"Server": 1}}, \
        "the adjustment was forgotten once it stopped needing correction"
    row = next(s for s in schedule_versions.standing_patterns(rid) if s["kind"] == "headcount_add")
    assert row["delta"] == 1 and row["times_applied"] >= 2
    # Twice the manager cuts the 4th server the draft now adds.
    _week(rid, _tuesday(2), four, three, age_days=12)
    _week(rid, _tuesday(1), four, three, age_days=5)
    schedule_versions.refresh_standing_patterns(rid)
    row = next(s for s in schedule_versions.standing_patterns(rid) if s["kind"] == "headcount_add")
    assert row["status"] == "retired"
    assert ("Tuesday", "night") not in schedule_learning.learned_headcount_adjustments(rid)
