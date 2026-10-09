"""A standing shift that holds only on the days an event series plays
(owner, 10/5/26): "if the Bears play on a Sunday, schedule Erik 100% of the
time, no exceptions — ONLY if the Bears play on a Sunday". Read against the
real 2026 Bears season file."""
from datetime import date, timedelta
from pathlib import Path

import pytest

import models
import schedule_rules as sr
import staff_settings
from event_intel import store as ev

SEASON = Path(__file__).resolve().parents[1] / "event_intel" / "seasons" / "nfl-chicago-bears-2026.json"
ERIK = {"day": "Sunday", "start": "9:00am", "end": "10:00pm", "when_event": "nfl-chicago-bears"}


@pytest.fixture(autouse=True)
def _db(db_path, monkeypatch):
    real = models.get_conn
    monkeypatch.setattr(models, "get_conn", lambda *a, **k: real(db_path))
    monkeypatch.setattr(models, "DB_PATH", db_path)
    ev.load_season(str(SEASON), db_path=db_path)
    yield


def _week(monday):
    d = date.fromisoformat(monday)
    dates = [(d + timedelta(days=i)).isoformat() for i in range(7)]
    return sr.Constraints(restaurant_id=1, week_dates=dates, week_days=[sr.DAYS[i] for i in range(7)])


def test_a_bears_sunday_is_a_standing_shift_and_a_bye_sunday_is_not():
    c = _week("2026-10-05")                                    # Sun 10/11 at Green Bay, noon
    out = sr._event_standing(c, ERIK, "Owner", "Erik Baylis")
    assert out == [{"day": "Sunday", "start": "9:00am", "end": "10:00pm", "role": "Owner",
                    "from": "2026-10-11", "until": "2026-10-11", "when_event": "nfl-chicago-bears"}]
    c = _week("2026-10-26")                                    # the Bears play Monday 11/2, no Sunday game
    assert sr._event_standing(c, ERIK, "Owner", "Erik Baylis") == []
    assert c.input_problems == []


def test_a_night_game_keeps_him_on_until_it_is_over():
    c = _week("2026-11-02")                                    # Sun 11/8 vs ?, 7:20pm kickoff
    out = sr._event_standing(c, ERIK, "Owner", "Erik Baylis")
    assert len(out) == 1 and out[0]["from"] == "2026-11-08"
    assert out[0]["start"] == "9:00am" and out[0]["end"] == "11:20pm", "7:20pm kickoff + 4h, past the 10pm close"


def test_an_unknown_calendar_is_a_problem_on_the_week_never_a_quiet_week():
    c = _week("2026-10-05")
    assert sr._event_standing(c, dict(ERIK, when_event="nfl-nobody"), "Owner", "Erik Baylis") == []
    assert c.input_problems and c.input_problems[0]["source"] == "event standing shift"


def test_the_setting_keeps_its_event_and_refuses_junk():
    out = staff_settings._clean_standing([ERIK, {"day": "Tuesday", "start": "10:00am", "end": "11:00pm"}])
    assert out[1]["when_event"] == "nfl-chicago-bears" and "when_event" not in out[0]
    with pytest.raises(staff_settings.StaffSettingsError):
        staff_settings._clean_standing([dict(ERIK, when_event="Bears; drop table")])


def test_build_constraints_turns_it_into_a_dated_standing_shift_the_manager_plan_honours(db_path):
    import schedule_skeleton
    rid = models.create_restaurant(models.Restaurant(name="EJ", owner_email="e@x.test"), db_path=db_path)
    staff_settings.upsert(rid, "Erik Baylis", standing_shifts=[ERIK], updated_by="test", db_path=db_path)
    c = _week("2026-10-05")
    c.restaurant_id = rid
    e = {"name": "Erik Baylis", "role": "Owner", "active": True,
         "settings": staff_settings.for_name(rid, "Erik Baylis", db_path=db_path)}
    sr._person_settings(c, e, {}, staff_settings)
    key = c.key("Erik Baylis")
    assert [s["from"] for s in c.standing_shifts[key]] == ["2026-10-11"]
    assert schedule_skeleton._standing_for(c, key, "2026-10-11")
    assert not schedule_skeleton._standing_for(c, key, "2026-10-04"), "dated to the game, not every Sunday"


def test_sunday_off_unless_the_bears_play_holds_both_ways(db_path):
    """Owner, 10/5/26: "Erik: Sunday off unless the Bears play" — the
    weekday Off plus a Bears standing shift. On a game Sunday he works (the
    standing shift is the more particular word); on a bye Sunday he is off,
    and the model is told Sunday is off only when no game falls that week."""
    rid = models.create_restaurant(models.Restaurant(name="EJ", owner_email="e@x.test"), db_path=db_path)
    staff_settings.upsert(rid, "Erik Baylis", standing_shifts=[ERIK],
                          daypart_availability={"Sunday": "off"}, updated_by="test", db_path=db_path)

    def week(monday):
        c = _week(monday)
        c.restaurant_id = rid
        sr._person_settings(c, {"name": "Erik Baylis", "role": "Owner", "active": True,
                                "settings": staff_settings.for_name(rid, "Erik Baylis", db_path=db_path)},
                            {}, staff_settings)
        return c
    game, bye = week("2026-10-05"), week("2026-10-26")
    assert game.can_work("Erik Baylis", "2026-10-11")[0], "a Bears Sunday"
    assert not bye.can_work("Erik Baylis", "2026-11-01")[0], "no Sunday game that week"
    assert game.can_work("Erik Baylis", "2026-10-06")[0], "the rest of the week untouched"
