"""The content calendar's cache, and the mobile Marketing payload it feeds.

get_content_calendar_ideas() used to run a Sonnet generation on EVERY read.
The web tab only read it on a button press, but /mobile/api/marketing called
it on every load — so opening the Marketing tab on the phone paid for a fresh
generation, blocked the tab on it, and handed back a different "this week"
each time, while the generator's own comment claimed the week never changes
until Sunday.
"""
import json

import pytest

import marketing
import models
from models import create_restaurant, get_conn, Restaurant


@pytest.fixture(autouse=True)
def _redirect_db(monkeypatch, db_path):
    """marketing.py resolves models.get_conn() with no db_path argument."""
    real = models.get_conn
    monkeypatch.setattr(models, "get_conn", lambda *a, **k: real(db_path))


@pytest.fixture
def rid(db_path):
    return create_restaurant(Restaurant(name="Calendar Co", owner_email="c@x.com"), db_path=db_path)


def _fake_generation(monkeypatch, ideas, calls):
    """Stand in for the Claude round trip, counting how often it happens."""
    def _create(*a, **kw):
        calls.append(kw.get("action"))
        return ideas
    monkeypatch.setattr(marketing, "create_with_retry", _create)
    monkeypatch.setattr(marketing, "extract_text", lambda m: json.dumps(m))


IDEAS = [{"day": "Monday", "platform": "Instagram & FB",
          "angle": "Truffle pasta close-up", "type": "instagram_post"}]


def test_a_calendar_is_generated_once_and_read_after_that(rid, monkeypatch):
    calls = []
    _fake_generation(monkeypatch, IDEAS, calls)

    first = marketing.get_content_calendar_ideas(restaurant_id=rid)
    second = marketing.get_content_calendar_ideas(restaurant_id=rid)
    third = marketing.get_content_calendar_ideas(restaurant_id=rid)

    assert len(calls) == 1, "reading the calendar must not pay for a generation"
    assert first == second == third


def test_the_same_week_shows_the_same_calendar(rid, monkeypatch):
    """The visible symptom of the missing cache: Tuesday's calendar was not
    Monday's, so nobody could plan against it."""
    calls = []
    _fake_generation(monkeypatch, IDEAS, calls)
    first = marketing.get_content_calendar_ideas(restaurant_id=rid)

    _fake_generation(monkeypatch, [{"day": "Friday", "platform": "Email",
                                    "angle": "Something else entirely", "type": "weekly_email"}], calls)
    assert marketing.get_content_calendar_ideas(restaurant_id=rid) == first


def test_force_is_the_owner_asking_for_a_different_week(rid, monkeypatch):
    calls = []
    _fake_generation(monkeypatch, IDEAS, calls)
    marketing.get_content_calendar_ideas(restaurant_id=rid)
    # Past the window where a force yields to work that just finished (see
    # test_a_forced_redraw_moments_later_returns_what_was_just_built).
    _age_calendar(rid, marketing.RECENT_CALENDAR_SECONDS + 60)

    other = [{"day": "Friday", "platform": "Email", "angle": "Wine dinner", "type": "weekly_email"}]
    _fake_generation(monkeypatch, other, calls)
    fresh = marketing.get_content_calendar_ideas(restaurant_id=rid, force=True)

    assert len(calls) == 2
    assert fresh[0]["angle"] == "Wine dinner"
    # and the new one replaces the old for subsequent plain reads
    assert marketing.get_content_calendar_ideas(restaurant_id=rid)[0]["angle"] == "Wine dinner"


def test_one_restaurants_calendar_is_never_served_to_another(db_path, rid, monkeypatch):
    other_rid = create_restaurant(Restaurant(name="Other Co", owner_email="o@x.com"), db_path=db_path)
    calls = []
    _fake_generation(monkeypatch, IDEAS, calls)
    marketing.get_content_calendar_ideas(restaurant_id=rid)

    assert marketing.get_cached_calendar(other_rid) is None


def test_nothing_is_cached_when_the_model_returns_junk(rid, monkeypatch):
    calls = []
    monkeypatch.setattr(marketing, "create_with_retry", lambda *a, **kw: calls.append(1))
    monkeypatch.setattr(marketing, "extract_text", lambda m: "not json at all")

    assert marketing.get_content_calendar_ideas(restaurant_id=rid) == []
    assert marketing.get_cached_calendar(rid) is None


def test_reading_a_calendar_that_was_never_generated_returns_nothing(rid):
    assert marketing.get_cached_calendar(rid) is None


# ── A retry must not throw away work that already finished ─────────────────
# Generating takes several seconds. If the client gives up waiting and the
# person presses the button again, the first call has usually completed and
# cached a perfectly good week — regenerating discards it, pays for a second
# model call, and answers the same question differently.

def test_a_forced_redraw_moments_later_returns_what_was_just_built(rid, monkeypatch):
    calls = []
    _fake_generation(monkeypatch, IDEAS, calls)
    first = marketing.get_content_calendar_ideas(restaurant_id=rid, force=True)

    other = [{"day": "Friday", "platform": "Email", "angle": "Something else", "type": "weekly_email"}]
    _fake_generation(monkeypatch, other, calls)
    retry = marketing.get_content_calendar_ideas(restaurant_id=rid, force=True)

    assert len(calls) == 1, "the retry paid for a second generation"
    assert retry == first


def test_a_forced_redraw_later_on_really_does_redraw(rid, monkeypatch):
    """"Generate a new week" a minute later still has to mean it."""
    calls = []
    _fake_generation(monkeypatch, IDEAS, calls)
    marketing.get_content_calendar_ideas(restaurant_id=rid, force=True)
    _age_calendar(rid, marketing.RECENT_CALENDAR_SECONDS + 60)

    other = [{"day": "Friday", "platform": "Email", "angle": "Wine dinner", "type": "weekly_email"}]
    _fake_generation(monkeypatch, other, calls)
    fresh = marketing.get_content_calendar_ideas(restaurant_id=rid, force=True)

    assert len(calls) == 2
    assert fresh[0]["angle"] == "Wine dinner"


def test_an_aged_calendar_is_not_mistaken_for_a_fresh_one(rid, monkeypatch):
    calls = []
    _fake_generation(monkeypatch, IDEAS, calls)
    marketing.get_content_calendar_ideas(restaurant_id=rid, force=True)

    assert marketing.get_cached_calendar(rid, max_age_seconds=60) is not None
    _age_calendar(rid, 300)
    assert marketing.get_cached_calendar(rid, max_age_seconds=60) is None
    # …but it is still this week's calendar for an ordinary read.
    assert marketing.get_cached_calendar(rid) is not None


def _age_calendar(rid, seconds):
    conn = models.get_conn()
    conn.execute(
        "UPDATE content_calendar_cache SET generated_at=datetime('now', ?) WHERE restaurant_id=?",
        (f"-{int(seconds)} seconds", rid))
    conn.commit()
    conn.close()


def test_each_idea_carries_an_unambiguous_iso_date(rid, monkeypatch):
    """"9/6" is what the web tab prints; the phone needs the year to know
    which day is today and to print the week's range."""
    _fake_generation(monkeypatch, IDEAS, [])

    idea = marketing.get_content_calendar_ideas(restaurant_id=rid)[0]

    assert idea["date"].count("/") == 1
    assert len(idea["iso_date"]) == 10 and idea["iso_date"].count("-") == 2
    y, m, d = idea["iso_date"].split("-")
    assert idea["date"] == f"{int(m)}/{int(d)}"
