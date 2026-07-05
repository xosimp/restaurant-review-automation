"""format_insight_html() parses free-form consultant prose from the model into
the dashboard's intro/recommendations/forecast structure. These lock in the
FORECAST extraction added with the proactive-insights feature."""
from client_api import format_insight_html

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
