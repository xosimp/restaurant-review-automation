"""format_insight_html() parses free-form consultant prose from the model into
the dashboard's intro/recommendations/forecast structure. These lock in the
FORECAST extraction added with the proactive-insights feature, and the
UNVERIFIED-caveat extraction added after it rendered as a bogus fourth
recommendation (ai_guard.verify_figures() appends "\\n\\nUNVERIFIED: $170"
to flag a figure the model stated that its own input didn't back up; the
recommendations splitter had no way to tell that block apart from a real
numbered suggestion)."""
from client_api import format_insight_html, parse_insight_sections

WITH_UNVERIFIED = """Erik, overall labor sits at 19.0% against your 26% target.

Recommendations:
1. Cut one shift on Tuesdays.
2. Check OT costs for Marcus and Vince.
3. Thursday and Friday ran lean on strong sales.

UNVERIFIED: $170"""

WITH_HEADING = """Hi Will, labor came in at 31% this week.

Recommendations:
1. Cut one closer shift on Tuesday.
2. Add a busser Saturday lunch.
3. Watch overtime for Jordan. Keep it up!

FORECAST: If this trend holds, labor % should drop back to 29% next week."""

NUMBERED_ONLY = """Waste hit $412 this week, mostly romaine.

1. Cut romaine par by 10%.
2. Reduce salmon order by 5 lbs.

Solid week overall.

FORECAST: Waste should trend down next week if the new par levels hold."""

NO_FORECAST = """Hi Will, quiet week.

Recommendations:
1. Keep doing what you are doing."""


def test_forecast_extracted_from_recommendations_format():
    out = format_insight_html(WITH_HEADING)
    assert "Forecast" in out
    assert "FORECAST:" not in out          # raw marker never leaks to the UI
    assert "drop back to 29%" in out


def test_forecast_extracted_from_bare_numbered_format():
    out = format_insight_html(NUMBERED_ONLY)
    assert "Forecast" in out
    assert "new par levels hold" in out


def test_no_forecast_line_means_no_forecast_block():
    out = format_insight_html(NO_FORECAST)
    assert "Forecast" not in out


def test_empty_input_is_graceful():
    assert format_insight_html("") == "Analysis unavailable."
    assert format_insight_html(None) == "Analysis unavailable."


def test_unverified_caveat_is_not_rendered_as_a_fourth_recommendation():
    out = format_insight_html(WITH_UNVERIFIED)
    # exactly the 3 numbered circles the prompt requires — not 4
    assert out.count('justify-content:center">1<') == 1
    assert out.count('justify-content:center">2<') == 1
    assert out.count('justify-content:center">3<') == 1
    assert 'justify-content:center">4<' not in out
    assert "UNVERIFIED:" not in out            # raw marker never leaks to the UI
    assert "$170" in out                       # but the actual caveat is shown
    assert "Unverified" in out


def test_unverified_caveat_reaches_mobiles_json_shape_too():
    intro, recs, forecast, unverified = parse_insight_sections(WITH_UNVERIFIED)
    assert len(recs) == 3
    assert unverified == "$170"


def test_no_unverified_line_means_no_caveat():
    intro, recs, forecast, unverified = parse_insight_sections(WITH_HEADING)
    assert unverified is None
    assert "Unverified" not in format_insight_html(WITH_HEADING)
