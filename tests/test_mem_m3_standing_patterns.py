"""What the draft learns from the manager stays learned until a manager
reverses it (memory audit 9/29/26, standing_patterns).

The manager takes Bob off Tuesday dinner in weeks 1 and 3; from week 4 the
draft leaves him off, so no edit repeats the move. Once week 1 is more than
56 days old the count falls below 2, the pattern drops and Bob comes back.
Two GMs with opposite habits on alternate weeks blended into one "manager".
"""
from datetime import date, timedelta

import pytest

import models
import schedule_intel
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
    yield


def _rid():
    return create_restaurant(Restaurant(name="Patterns Co", owner_email="p@x.test", module_labor=1))


HEAD = "date,day,employee,role,shift_start,shift_end,scheduled_hours,notes\n"


def _csv(tuesday, people):
    return HEAD + "".join(f"{tuesday.isoformat()},Tuesday,{p},Server,5:00pm,10:00pm,5,\n" for p in people)


def _week(rid, tuesday, generated, final, editor="dana", age_days=0):
    """One published week: the generated draft, the manager's edit (when the
    final differs) and the publish — versions dated `age_days` ago."""
    conn = models.get_conn()
    ws = tuesday - timedelta(days=1)
    hid = conn.execute("INSERT INTO schedule_history (restaurant_id, week_start, week_end, schedule_csv, "
                       "published_at, generated_at) VALUES (?,?,?,?,datetime('now', ?),datetime('now', ?))",
                       (rid, ws.isoformat(), (ws + timedelta(days=6)).isoformat(), _csv(tuesday, final),
                        f"-{age_days} days", f"-{age_days} days")).lastrowid
    conn.commit()
    conn.close()
    schedule_versions.append(rid, hid, "generated", _csv(tuesday, generated), saved_by="cavnar")
    if final != generated:
        schedule_versions.append(rid, hid, "edited", _csv(tuesday, final), saved_by=editor)
    schedule_versions.append(rid, hid, "published", _csv(tuesday, final), saved_by=editor)
    conn = models.get_conn()
    conn.execute("UPDATE schedule_versions SET created_at=datetime('now', ?) WHERE history_id=?",
                 (f"-{age_days} days", hid))
    conn.commit()
    conn.close()
    return hid


def _tuesday(weeks_ago):
    today = date.today()
    return today - timedelta(days=(today.weekday() - 1) % 7) - timedelta(weeks=weeks_ago)


def test_a_learned_move_outlives_the_eight_week_window_and_counts_what_kept_it():
    rid = _rid()
    everyone, without_bob = ["Ana", "Bob", "Cy"], ["Ana", "Cy"]
    # Weeks 1 and 3: the manager takes Bob off Tuesday dinner.
    _week(rid, _tuesday(11), everyone, without_bob, age_days=77)
    _week(rid, _tuesday(9), everyone, without_bob, age_days=63)
    # The window still sees both edits only if they are recent: refresh while it does.
    conn = models.get_conn()
    conn.execute("UPDATE schedule_versions SET created_at=datetime('now', '-20 days') WHERE restaurant_id=?", (rid,))
    conn.commit()
    conn.close()
    stats = schedule_versions.refresh_standing_patterns(rid)
    assert stats["learned"] >= 1
    # Then eight weeks of drafts that already leave him off, published as drafted.
    for w in range(8, 0, -1):
        _week(rid, _tuesday(w), without_bob, without_bob, age_days=7 * w)
    conn = models.get_conn()
    conn.execute("UPDATE schedule_versions SET created_at=datetime('now', '-70 days') WHERE reason='edited' "
                 "AND restaurant_id=?", (rid,))
    conn.commit()
    conn.close()
    schedule_versions.refresh_standing_patterns(rid)
    assert not [p for p in schedule_versions.learned_patterns(rid) if p["kind"] == "moved_off"], \
        "the live window has forgotten it — the bug"
    drafted, conflicts = schedule_versions.patterns_for_draft(rid)
    bob = [p for p in drafted if p["kind"] == "moved_off" and p["employee"] == "Bob"]
    assert bob and bob[0]["standing"] and "Standing preference" in bob[0]["text"]
    row = next(s for s in schedule_versions.standing_patterns(rid) if s["employee"] == "Bob")
    assert row["times_applied"] >= 8 and row["status"] == "active" and "/" in row["last_confirmed"]


def test_two_manager_reversals_retire_it():
    rid = _rid()
    everyone, without_bob = ["Ana", "Bob", "Cy"], ["Ana", "Cy"]
    _week(rid, _tuesday(6), everyone, without_bob, age_days=40)
    _week(rid, _tuesday(5), everyone, without_bob, age_days=33)
    schedule_versions.refresh_standing_patterns(rid)
    # The draft leaves him off; twice a manager puts him back.
    _week(rid, _tuesday(2), without_bob, everyone, age_days=12)
    _week(rid, _tuesday(1), without_bob, everyone, age_days=5)
    schedule_versions.refresh_standing_patterns(rid)
    row = next(s for s in schedule_versions.standing_patterns(rid) if s["employee"] == "Bob" and s["kind"] == "moved_off")
    assert row["times_overridden"] == 2 and row["status"] == "retired"
    drafted, _c = schedule_versions.patterns_for_draft(rid)
    assert not [p for p in drafted if p.get("standing") and p["employee"] == "Bob" and p["kind"] == "moved_off"]


def test_two_editors_pulling_opposite_ways_go_to_the_owner_not_the_model():
    rid = _rid()
    # Dana takes Bob off Tuesday dinner; Chris swaps Cy out for Bob there.
    _week(rid, _tuesday(4), ["Ana", "Bob"], ["Ana"], editor="dana", age_days=28)
    _week(rid, _tuesday(3), ["Ana", "Cy"], ["Ana", "Bob"], editor="chris", age_days=21)
    _week(rid, _tuesday(2), ["Ana", "Bob"], ["Ana"], editor="dana", age_days=14)
    _week(rid, _tuesday(1), ["Ana", "Cy"], ["Ana", "Bob"], editor="chris", age_days=7)
    live = {p["kind"]: p for p in schedule_versions.learned_patterns(rid) if p.get("employee") == "Bob"}
    assert live["moved_off"]["editors"] == {"dana": 2} and live["moved_on"]["editors"] == {"chris": 2}
    drafted, conflicts = schedule_versions.patterns_for_draft(rid)
    assert not [p for p in drafted if p.get("employee") == "Bob"]
    assert conflicts and conflicts[0]["off_by"] == ["dana"] and conflicts[0]["on_by"] == ["chris"]


def test_make_it_a_rule_writes_the_persons_availability_with_its_author():
    rid = _rid()
    everyone, without_bob = ["Ana", "Bob", "Cy"], ["Ana", "Cy"]
    _week(rid, _tuesday(3), everyone, without_bob, age_days=20)
    _week(rid, _tuesday(2), everyone, without_bob, age_days=13)
    key = next(schedule_intel.pattern_key(p) for p in schedule_versions.learned_patterns(rid)
               if p["kind"] == "moved_off" and p["employee"] == "Bob")
    out = schedule_versions.make_rule(rid, key, user={"id": 7, "username": "owner"})
    assert out["rule"] == "Tuesday: morning only"
    st = staff_settings.for_name(rid, "Bob")
    assert st["daypart_availability"]["Tuesday"] == "morning" and st["updated_by"] == "owner"
    row = next(s for s in schedule_versions.standing_patterns(rid) if s["key"] == key)
    assert row["status"] == "ruled" and row["rule"]["by"] == "owner"
