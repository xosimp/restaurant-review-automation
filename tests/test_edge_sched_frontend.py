"""Edge cases of the web schedule generator's inline JS (SCHED audit, SCHED-41).

Read at the source, as tests/test_frontend_rules.py does: a full Generate
must not throw away a manager's unsaved edits, and one failed status poll
must not abandon a job that is still running server-side.

xfail(strict=True) marks the confirmed defect; the marker comes off with
the fix. Only the markup is read — nothing here edits the template.
"""
import os
import re

import pytest

_TEMPLATE = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "templates", "dashboard.html")


def _src():
    with open(_TEMPLATE, encoding="utf-8") as f:
        return f.read()


def _function(name):
    src = _src()
    start = src.index(f"function {name}(")
    nxt = re.compile(r"\nfunction \w+\(").search(src, start + 10)
    return src[start:nxt.start() if nxt else len(src)]


def test_regenerating_some_days_already_refuses_over_unsaved_edits():
    body = _function("regenerateScheduleDays")
    assert "_schedDirty" in body


@pytest.mark.xfail(strict=True, reason="SCHED-41: a full generateSchedule replaces _schedRows without checking _schedDirty")
def test_a_full_generate_checks_for_unsaved_edits_before_it_starts():
    body = _function("generateSchedule")
    first_fetch = body.index("fetch('/api/generate-schedule'")
    assert "_schedDirty" in body[:first_fetch]


@pytest.mark.xfail(strict=True, reason="SCHED-41: the first failed status poll clears the interval and abandons a job that is still running")
def test_one_failed_status_poll_does_not_abandon_the_wait():
    body = _function("generateSchedule")
    poll = body[body.index("fetch('/api/schedule-status/'"):]
    catch = poll[poll.index(".catch(function"):]
    catch = catch[:catch.index("});") + 3]
    give_up = catch.index("clearInterval(_schedPollInterval)")
    assert re.search(r"if\s*\(", catch[:give_up]), catch


def test_the_generate_poll_still_has_a_hard_timeout():
    body = _function("generateSchedule")
    assert "_hardTimeout" in body and "900000" in body
