"""Schedule fix round 10/10/26, workstream G1 — the learned headcount is
not counted twice (LEARNING-3, RM-5): the residual is learned against what
the draft was ASKED for, and a learned add applies only while the baseline
is below the level the manager settled on."""
import json
from datetime import date, timedelta

import pytest

import labor
import models
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


HEAD = "date,day,employee,role,shift_start,shift_end,scheduled_hours,notes\n"


def _rid():
    return create_restaurant(Restaurant(name="Heads Co", owner_email="hc@x.test", module_labor=1))


def _csv(tuesday, people_):
    return HEAD + "".join(f"{tuesday.isoformat()},Tuesday,{p},Server,5:00pm,10:00pm,5,\n" for p in people_)


def _tuesday(weeks_ago):
    today = date.today()
    return today - timedelta(days=(today.weekday() - 1) % 7) - timedelta(weeks=weeks_ago)


def _week(rid, tuesday, generated, final, asked=None, age_days=0):
    ws = tuesday - timedelta(days=1)
    econ = json.dumps({"requirements_by_date": {f"{tuesday.isoformat()}|night": {"Server": asked}}}) \
        if asked is not None else None
    conn = models.get_conn()
    hid = conn.execute("INSERT INTO schedule_history (restaurant_id, week_start, week_end, schedule_csv, "
                       "published_at, generated_at, economics_json) VALUES (?,?,?,?,datetime('now', ?),"
                       "datetime('now', ?),?)",
                       (rid, ws.isoformat(), (ws + timedelta(days=6)).isoformat(), _csv(tuesday, final),
                        f"-{age_days} days", f"-{age_days} days", econ)).lastrowid
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


FOUR = ["Ana", "Bob", "Cy", "Dee"]
FIVE = FOUR + ["Eve"]


def test_an_add_applies_only_while_the_baseline_is_short_of_the_settled_level():
    assert schedule_learning.headcount_to_apply(4, 1, 5) == 1
    assert schedule_learning.headcount_to_apply(5, 1, 5) == 0, "the punches already carry the manager's fifth"
    assert schedule_learning.headcount_to_apply(3, 2, 4) == 1, "never past the settled level"
    assert schedule_learning.headcount_to_apply(6, -1, 5) == -1
    assert schedule_learning.headcount_to_apply(5, -1, 5) == 0
    assert schedule_learning.headcount_to_apply(4, 1, None) == 1


def test_the_manager_s_fifth_server_is_not_asked_for_twice_once_the_punches_carry_it():
    rid = _rid()
    # The manager adds a fifth server to Tuesday dinner, twice, by hand.
    _week(rid, _tuesday(2), FOUR, FIVE, age_days=14)
    _week(rid, _tuesday(1), FOUR, FIVE, age_days=7)
    got = schedule_learning.learned_headcount(rid)
    assert got[("Tuesday", "night")]["Server"] == {"delta": 1, "settled": 5}
    # Before the punches catch up the baseline says 4: the requirement is 5.
    assert labor.apply_learned_headcount(rid, {("Tuesday", "night"): {"Server": 4}}) == \
        {("Tuesday", "night"): {"Server": 5}}
    # The punches have caught up (the trend snapped the baseline to 5): the
    # requirement stays 5. It used to read 6 — a sixth server asked for,
    # scored and protected from the trim until the manager cut it twice.
    assert labor.apply_learned_headcount(rid, {("Tuesday", "night"): {"Server": 5}}) == \
        {("Tuesday", "night"): {"Server": 5}}


def test_a_draft_filled_up_to_its_own_requirement_teaches_no_extra_person():
    rid = _rid()
    # The draft was asked for five servers but came in with four; the
    # manager filled the fifth. That is the requirement kept, not a habit.
    _week(rid, _tuesday(2), FOUR, FIVE, asked=5, age_days=14)
    _week(rid, _tuesday(1), FOUR, FIVE, asked=5, age_days=7)
    assert schedule_learning.learned_headcount_adjustments(rid) == {}, \
        "the residual was read against the short draft, not what it was asked for"
    # Against a requirement of four, the same edits are the manager's +1.
    rid2 = _rid()
    _week(rid2, _tuesday(2), FOUR, FIVE, asked=4, age_days=14)
    _week(rid2, _tuesday(1), FOUR, FIVE, asked=4, age_days=7)
    assert schedule_learning.learned_headcount_adjustments(rid2) == {("Tuesday", "night"): {"Server": 1}}


def test_the_week_record_carries_what_the_draft_was_asked_for():
    rid = _rid()
    hid = _week(rid, _tuesday(1), FOUR, FOUR, asked=5, age_days=7)
    rec = schedule_versions.learning_weeks(rid, history_ids=[hid], include_unlooked=True)[0]
    assert rec["asked"] == {(_tuesday(1).isoformat(), "night", "server"): 5}
