"""The Home pulse chip's "bad" tone (density audit item 31).

"bad" comes from a named threshold, never a feeling: labor at least
thresholds.LABOR_OVER_TARGET_PTS over its target, or an urgent review still
owed a reply. The iOS chip and the Modules grid map it to cavnarRed and
sort by it, so a module in trouble no longer reads like a neutral one.
"""
import inspect

import mobile_api
from models import Restaurant
from thresholds import LABOR_OVER_TARGET_PTS


def _r():
    return Restaurant(name="x", owner_email="x@x.test", labor_target_pct=30.0)


def _labor(pct):
    return mobile_api._home_pulse("labor", {"value": f"{pct}%"}, {},
                                  {"is_live": True, "overall_labor_pct": pct}, _r(), {})


def test_labor_on_target_is_good():
    assert _labor(29.0)["tone"] == "good"
    assert _labor(30.0)["tone"] == "good"


def test_labor_just_over_target_is_warn():
    assert _labor(30.0 + LABOR_OVER_TARGET_PTS - 0.1)["tone"] == "warn"


def test_labor_over_by_the_named_margin_is_bad():
    assert _labor(30.0 + LABOR_OVER_TARGET_PTS)["tone"] == "bad"
    assert _labor(40.0)["tone"] == "bad"


def test_sample_labor_is_never_bad():
    p = mobile_api._home_pulse("labor", {"value": "—"}, {},
                               {"is_live": False, "overall_labor_pct": 45.0}, _r(), {})
    assert p["tone"] is None


def test_an_urgent_review_owed_a_reply_is_bad_and_says_so():
    p = mobile_api._home_pulse("reviews", {"value": "3/10"},
                               {"response_rate": 90, "total": 10, "urgent": 2}, None, _r(), {})
    assert p["tone"] == "bad"
    assert p["label"] == "2 urgent unanswered"


def test_reviews_without_urgent_keep_the_rate_tones():
    good = mobile_api._home_pulse("reviews", {"value": "9/10"},
                                  {"response_rate": 90, "total": 10, "urgent": 0}, None, _r(), {})
    warn = mobile_api._home_pulse("reviews", {"value": "1/10"},
                                  {"response_rate": 10, "total": 10}, None, _r(), {})
    assert good["tone"] == "good" and warn["tone"] == "warn"


def test_bad_reads_the_shared_threshold_not_a_literal():
    src = inspect.getsource(mobile_api._home_pulse)
    assert "LABOR_OVER_TARGET_PTS" in src
