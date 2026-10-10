"""Schedule fix round 10/10/26, workstream G1 — the learners read kept week
records instead of replaying 24 weeks of CSV versions on every call
(SCALE-33, ARCH-28)."""
from datetime import date, timedelta

import pytest

import models
import people
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
EVERYONE, WITHOUT_BOB = ["Ana", "Bob", "Cy"], ["Ana", "Cy"]


def _rid():
    return create_restaurant(Restaurant(name="Records Co", owner_email="r@x.test", module_labor=1))


def _csv(tuesday, people_):
    return HEAD + "".join(f"{tuesday.isoformat()},Tuesday,{p},Server,5:00pm,10:00pm,5,\n" for p in people_)


def _tuesday(weeks_ago):
    today = date.today()
    return today - timedelta(days=(today.weekday() - 1) % 7) - timedelta(weeks=weeks_ago)


def _week(rid, tuesday, generated, final, age_days=0):
    ws = tuesday - timedelta(days=1)
    conn = models.get_conn()
    hid = conn.execute("INSERT INTO schedule_history (restaurant_id, week_start, week_end, schedule_csv, "
                       "published_at) VALUES (?,?,?,?,datetime('now', ?))",
                       (rid, ws.isoformat(), (ws + timedelta(days=6)).isoformat(), _csv(tuesday, final),
                        f"-{age_days} days")).lastrowid
    conn.commit()
    conn.close()
    schedule_versions.append(rid, hid, "generated", _csv(tuesday, generated), saved_by="cavnar",
                             saved_authority="system")
    if final != generated:
        schedule_versions.append(rid, hid, "edited", _csv(tuesday, final), saved_by="dana",
                                 saved_authority="principal")
    schedule_versions.append(rid, hid, "published", _csv(tuesday, final), saved_by="dana",
                             saved_authority="principal")
    return hid


@pytest.fixture
def replays(monkeypatch):
    seen = []
    real = schedule_versions._WeekData

    class Counting(real):
        def __init__(self, *a, **k):
            seen.append(sorted(k.get("history_ids") or []))
            super().__init__(*a, **k)
    monkeypatch.setattr(schedule_versions, "_WeekData", Counting)
    return seen


def _strip(recs):
    return [{k: v for k, v in r.items() if k != "age_days"} for r in recs]


def test_a_second_read_replays_nothing_and_reads_the_same_weeks(replays):
    rid = _rid()
    for w in (3, 2, 1):
        _week(rid, _tuesday(w), EVERYONE, WITHOUT_BOB, age_days=7 * w)
    first = schedule_versions.learning_weeks(rid)
    assert len(replays) == 1 and len(replays[0]) == 3
    second = schedule_versions.learning_weeks(rid)
    assert len(replays) == 1, "a week nothing touched was replayed from its CSV versions again"
    assert _strip(first) == _strip(second)
    assert [w["age_days"] for w in first] == [w["age_days"] for w in second]
    # The learners read the same habit either way.
    pats = [p for p in schedule_versions.learned_patterns(rid) if p["kind"] == "moved_off"]
    assert pats and pats[0]["employee"] == "Bob" and pats[0]["times"] == 3


def test_only_the_week_that_changed_is_read_anew(replays):
    rid = _rid()
    hids = [_week(rid, _tuesday(w), EVERYONE, WITHOUT_BOB, age_days=7 * w) for w in (3, 2)]
    schedule_versions.learning_weeks(rid)
    hid = _week(rid, _tuesday(1), EVERYONE, EVERYONE, age_days=7)
    schedule_versions.learning_weeks(rid)
    assert replays[-1] == [hid]
    # A later save to an older week reads that week (and only it) again.
    schedule_versions.append(rid, hids[0], "edited", _csv(_tuesday(3), ["Ana"]), saved_by="dana",
                             saved_authority="principal")
    schedule_versions.learning_weeks(rid)
    assert replays[-1] == [hids[0]]


def test_a_rename_reads_every_week_anew(replays):
    rid = _rid()
    for w in (2, 1):
        _week(rid, _tuesday(w), EVERYONE, WITHOUT_BOB, age_days=7 * w)
    schedule_versions.learning_weeks(rid)
    people.stamp_person_ids(rid)
    people.rename_person(rid, people.person_id_for(rid, "Bob"), "Bob Smith")
    recs = schedule_versions.learning_weeks(rid)
    assert len(replays[-1]) == 2
    assert all("Bob Smith" in {r["employee"] for r in w["base"]} for w in recs)


def test_a_one_tap_answer_reads_its_week_anew(replays):
    rid = _rid()
    hid = _week(rid, _tuesday(1), EVERYONE, WITHOUT_BOB, age_days=7)
    schedule_versions.learning_weeks(rid)
    conn = models.get_conn()
    conn.execute("INSERT INTO schedule_edit_answers (restaurant_id, history_id, version, question_key, kind, "
                 "subject_json, keys_json, phase, answer, authority) VALUES (?,?,?,?,?,?,?,?,?,?)",
                 (rid, hid, 2, "k", "moved_off", "{}", '["%s|bob|5:00pm"]' % _tuesday(1).isoformat(),
                  "pre_publish", "this_week", "principal"))
    conn.commit()
    conn.close()
    rec = schedule_versions.learning_weeks(rid)[0]
    assert replays[-1] == [hid] and rec["excluded"]["answered"] == 1
