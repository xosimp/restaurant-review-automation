"""A schedule week is the restaurant's own week (owner, 10/10/26: Simple
EJ's weeks run Wednesday to Tuesday - "have this ordered correctly in the
header"): restaurants.week_start_day, the week its payroll and overtime are
counted in, sets which seven days a draft covers, so the Studio's header,
the Crew grid, note rules, Ask and the auto-draft all read Wednesday first."""
import datetime as dt

import pytest

import models
import schedule_engine as se
from models import Restaurant, create_restaurant, get_restaurant


@pytest.fixture(autouse=True)
def _redirect(db_path, monkeypatch):
    real = models.get_conn
    monkeypatch.setattr(models, "get_conn", lambda *a, **k: real(db_path))
    monkeypatch.setattr(models, "DB_PATH", db_path)


SAT = dt.datetime(2026, 10, 10)


def test_the_week_starts_on_the_restaurants_own_day():
    assert se._week_monday(SAT, start_day=2) == dt.datetime(2026, 10, 14)       # next Wednesday
    assert se._week_monday(SAT) == dt.datetime(2026, 10, 12)                    # Monday stays the default
    # Any date in the wanted week: Monday 10/19 is in the week of Wed 10/14.
    assert se._week_monday(SAT, "2026-10-19", start_day=2) == dt.datetime(2026, 10, 14)
    assert se._week_monday(SAT, "2026-10-21", start_day=2) == dt.datetime(2026, 10, 21)
    # On the week's own first day, "next week" is the following one.
    assert se._week_monday(dt.datetime(2026, 10, 14), start_day=2) == dt.datetime(2026, 10, 21)
    dates = se.week_dates_from(dt.datetime(2026, 10, 14))
    assert dates[0] == "2026-10-14" and dates[-1] == "2026-10-20"
    assert se.week_day_names(dates) == ["Wednesday", "Thursday", "Friday", "Saturday", "Sunday", "Monday", "Tuesday"]
    assert se.week_day_order(2)[0] == "Wednesday" and se.week_day_order(0)[0] == "Monday"


def test_a_generate_press_and_its_checks_read_the_restaurants_week(monkeypatch):
    rid = create_restaurant(Restaurant(name="EJ", owner_email="e@x.test"))
    models.update_restaurant(rid, {"week_start_day": 2})
    req = se.generation_request(rid, today=SAT)
    assert req["week_start"] == "2026-10-14"
    assert se.generation_request(rid, week_start="2026-10-19", today=SAT)["week_start"] == "2026-10-14"
    # A redo's dates belong to the week their first date is in.
    assert se.generation_request(rid, dates=["2026-10-20"], history_id=3, today=SAT)["week_start"] == "2026-10-14"
    monkeypatch.setattr("time_utils.restaurant_now_by_id", lambda *_a, **_k: SAT)
    # This week (Wed 10/7 - Tue 10/13) has not ended yet, so it may be picked.
    assert se.check_week_start(rid, "2026-10-12") == ("2026-10-12", None)


def test_the_auto_draft_never_falls_on_the_eve_of_the_week():
    assert models.auto_draft_days(0) == (0, 1, 2, 3, 4, 5)                     # Monday week: Monday-Saturday
    assert 1 not in models.auto_draft_days(2) and 6 in models.auto_draft_days(2)  # Wednesday week: not Tuesday
    # Restaurant 5 spreads to Tuesday by id: on a Wednesday week it drafts Monday.
    assert models.default_auto_draft_weekday(5) == 1
    assert models.effective_auto_draft_weekday(5, None, 0, week_start_day=2) == 0
    assert models.effective_auto_draft_weekday(5, 1, 1, week_start_day=2) == 0  # a stored Tuesday is moved too
    assert models.effective_auto_draft_weekday(5, 6, 1, week_start_day=2) == 6  # Sunday is a draft day here
    r = Restaurant(name="x", owner_email="x@x.test", week_start_day=2)
    r.id, r.auto_draft_weekday, r.auto_draft_weekday_chosen = 9, 6, 1
    assert models.auto_publish_weekday(r) == 0                                   # Sunday's draft goes Monday


def test_a_week_only_note_rule_holds_for_the_restaurants_week():
    import schedule_note_rules as nr
    assert nr._monday("2026-10-19", 2).isoformat() == "2026-10-14"
    assert nr._monday("2026-10-19").isoformat() == "2026-10-19"


def test_crew_and_the_next_draft_read_the_restaurants_week(monkeypatch):
    import crew_matrix
    import staffing_signals
    rid = create_restaurant(Restaurant(name="EJ", owner_email="e@x.test"))
    models.update_restaurant(rid, {"week_start_day": 2})
    monkeypatch.setattr("labor.staffing_baseline", lambda *_a, **_k: {})
    assert crew_matrix.build(rid)["days"][0] == "Wednesday"
    # Advice "on the next schedule" about a Monday is the Monday inside the
    # next Wednesday-to-Tuesday week.
    assert staffing_signals.next_draft_date(rid, "Monday", today=SAT.date()) == dt.date(2026, 10, 19)
    assert staffing_signals.next_draft_date(rid, "Wednesday", today=SAT.date()) == dt.date(2026, 10, 14)
