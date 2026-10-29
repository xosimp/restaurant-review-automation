"""The Weather Service's short forecast in plain words (owner, 10/29/26:
"'Chance showers' needs to be worded different and not capitalized — should
be 'chance of showers', plain English")."""
import weather


def test_the_forecast_reads_in_plain_words_lower_case():
    raw = "Slight Chance Showers And Thunderstorms then Chance Showers And Thunderstorms"
    assert weather.plain_forecast(raw) == ("slight chance of showers and thunderstorms, "
                                           "then a chance of showers and thunderstorms")
    assert weather.plain_forecast("Rain Showers Likely then Slight Chance Rain Showers") == \
        "rain showers likely, then a slight chance of rain showers"
    assert weather.plain_forecast("Areas Of Fog then Partly Sunny") == "areas of fog, then partly sunny"
    assert weather.plain_forecast("Chance Snow Showers") == "chance of snow showers"
    assert weather.plain_forecast("") == "" and weather.plain_forecast(None) == ""


def test_a_line_starts_with_a_capital_and_saying_it_twice_changes_nothing():
    line = weather.plain_line("Slight Chance Showers And Thunderstorms then Chance Showers And Thunderstorms, high 67°")
    assert line == "Slight chance of showers and thunderstorms, then a chance of showers and thunderstorms, high 67°"
    assert weather.plain_line(line) == line
    # Lines already in plain words keep them.
    assert weather.plain_line("Rain forecast (60% chance, high 50°)") == "Rain forecast (60% chance, high 50°)"
    assert weather.plain_line("chance of rain, then a chance of snow") == "Chance of rain, then a chance of snow"


def test_a_forecast_row_carries_plain_words():
    row = weather._row("2026-10-29", {"shortForecast": "Slight Chance Showers And Thunderstorms", "temperature": 67})
    assert row["short_forecast"] == "slight chance of showers and thunderstorms"
    night = weather._with_night(dict(row), {"shortForecast": "Chance Rain Showers", "temperature": 50})
    assert night["night_short_forecast"] == "chance of rain showers"
