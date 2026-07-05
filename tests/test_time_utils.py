"""Per-restaurant timezone resolution — the fix for 'America/Chicago' being
hardcoded into every client-facing date."""
from datetime import datetime
from zoneinfo import ZoneInfo

from time_utils import restaurant_tz, restaurant_now
from models import Restaurant


def test_defaults_to_operator_time():
    assert str(restaurant_tz(None)) == "America/Chicago"


def test_reads_restaurant_field():
    r = Restaurant(name="West Coast Spot", owner_email="w@x.com", timezone="America/Los_Angeles")
    assert str(restaurant_tz(r)) == "America/Los_Angeles"


def test_accepts_raw_string():
    assert str(restaurant_tz("America/New_York")) == "America/New_York"


def test_invalid_name_falls_back_instead_of_crashing():
    assert str(restaurant_tz("Not/AZone")) == "America/Chicago"
    r = Restaurant(name="Typo'd", owner_email="t@x.com", timezone="Amercia/Chicgo")
    assert str(restaurant_tz(r)) == "America/Chicago"


def test_restaurant_now_matches_zone():
    r = Restaurant(name="NYC Spot", owner_email="n@x.com", timezone="America/New_York")
    local = restaurant_now(r)
    expected = datetime.now(ZoneInfo("America/New_York"))
    assert local.utcoffset() == expected.utcoffset()
    assert abs((expected - local).total_seconds()) < 5


def test_naive_flag_strips_tzinfo():
    assert restaurant_now("America/Denver", naive=True).tzinfo is None


def test_new_dbs_get_timezone_column(db_path):
    from models import get_conn
    conn = get_conn(db_path)
    cols = [r["name"] for r in conn.execute("PRAGMA table_info(restaurants)").fetchall()]
    conn.close()
    assert "timezone" in cols
