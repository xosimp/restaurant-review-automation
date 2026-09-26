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


# ── Ratchet: no locale date forms on the owner-facing clients ────────────────
# "Sep 21" / "9/21/2026" / "6:45 PM" each reached an owner through a locale
# formatter (web toLocaleDateString, iOS DateFormatter "MMM d" / "h:mm a").
# The helpers are the rule: mdy() + cavClock() on web, CavnarDate on iOS.

_OWNER_TEMPLATES = ["dashboard.html", "client_settings.html", "login.html",
                    "staff_portal.html", "staff_schedule.html", "issue.html"]


def test_web_templates_never_format_a_date_through_the_locale():
    for name in _OWNER_TEMPLATES:
        path = os.path.join(ROOT, "templates", name)
        if not os.path.exists(path):
            continue
        src = open(path, encoding="utf-8").read()
        assert "toLocaleDateString(" not in src, name
        assert "toLocaleTimeString(" not in src, name
        # toLocaleString is fine for money; never with a date part.
        assert not re.search(r"toLocaleString\([^)]*\b(month|day|hour)\s*:", src), name


def test_ios_never_formats_an_owner_date_as_month_name_or_locale_time():
    ios = os.path.join(ROOT, "ios", "CavnarAI")
    bad = []
    for base, _dirs, files in os.walk(ios):
        if "Tests" in base or "/.dd" in base or "/build" in base:
            continue
        for f in files:
            if not f.endswith(".swift"):
                continue
            path = os.path.join(base, f)
            for n, line in enumerate(open(path, encoding="utf-8"), 1):
                code = line.split("//", 1)[0]
                if re.search(r'dateFormat\s*=\s*"[^"]*(MMM|h:mm a|hh:mm a)', code):
                    bad.append(f"{os.path.relpath(path, ROOT)}:{n}: {line.strip()}")
    assert not bad, "use CavnarDate.mdy / mdyTime / time:\n" + "\n".join(bad)


def test_ios_has_the_house_time_helper():
    src = open(os.path.join(ROOT, "ios", "CavnarAI", "CavnarAI", "DesignSystem", "Formatting.swift"),
               encoding="utf-8").read()
    assert "static func time(_ date: Date" in src
    assert '"am" : "pm"' in src
