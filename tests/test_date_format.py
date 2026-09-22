"""Dates read `9/21/26` (DESIGN_SYSTEM.md → Dates and times).

The activity feed printed "the week of 2026-09-14" — the one place an ISO
date reached an owner. The helper is the rule; this pins it and the feed.
"""
import os
import re
from datetime import date, datetime, timezone

import time_utils

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def test_mdy_is_month_day_two_digit_year_with_no_leading_zeros():
    assert time_utils.mdy("2026-09-21") == "9/21/26"
    assert time_utils.mdy("2026-01-05T04:30:00Z") == "1/5/26"
    assert time_utils.mdy(date(2026, 12, 31)) == "12/31/26"
    assert time_utils.mdy(datetime(2027, 3, 4, 9, tzinfo=timezone.utc)) == "3/4/27"


def test_mdy_never_raises_inside_a_sentence():
    assert time_utils.mdy(None) == ""
    assert time_utils.mdy("") == ""
    assert time_utils.mdy("next week") == "next week"


def test_mdy_range_collapses_a_single_day():
    assert time_utils.mdy_range("2026-09-14", "2026-09-20") == "9/14/26 – 9/20/26"
    assert time_utils.mdy_range("2026-09-14", "2026-09-14") == "9/14/26"


def test_the_activity_feed_formats_the_schedule_week_through_mdy():
    src = open(os.path.join(ROOT, "activity.py"), encoding="utf-8").read()
    assert "mdy(sched['week_start'])" in src
    # No text= line interpolates a raw week_start / *_date / *_on column.
    for line in src.splitlines():
        if '"text": f"' in line:
            assert not re.search(r"\{[a-z_]+\[['\"](week_start|\w+_date|\w+_on)['\"]\]\}", line), line
