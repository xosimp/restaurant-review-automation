"""labor.py's generate_optimized_schedule — weather_forecast parameter only.
generate_optimized_schedule has no other test coverage yet (pre-existing
gap, out of scope here); these tests are scoped narrowly to confirming the
weather forecast block this task added actually reaches the AI prompt,
mirroring the create_with_retry-capture style already used in
test_guest_marketing.py."""
import types

import pytest

import labor
from labor import generate_optimized_schedule


def _minimal_analysis():
    return {"overall_labor_pct": 28.0, "overstaffed_days": [], "understaffed_days": [], "dow_summary": {}}


def _minimal_shifts():
    return [{"employee": "Alex", "role": "Server", "date": "2026-06-01",
              "scheduled_hours": 8, "actual_hours": 8}]


def _capture_create_with_retry(monkeypatch):
    captured = {}

    def fake(client, **kwargs):
        captured.update(kwargs)
        return types.SimpleNamespace(
            content=[types.SimpleNamespace(text="date,day,employee,role,shift_start,shift_end,scheduled_hours,notes\n---SUMMARY---\n- ok")],
            stop_reason="end_turn",
        )
    monkeypatch.setattr(labor, "create_with_retry", fake)
    return captured


def test_weather_forecast_reaches_the_prompt(monkeypatch):
    captured = _capture_create_with_retry(monkeypatch)
    weather_forecast = [
        {"date": "2026-07-11", "day_name": "Saturday", "high_f": 91, "short_forecast": "Sunny", "precip_pct": 5},
        {"date": "2026-07-12", "day_name": "Sunday", "high_f": 68, "short_forecast": "Rain Showers", "precip_pct": 85},
    ]

    generate_optimized_schedule(_minimal_analysis(), _minimal_shifts(),
                                 restaurant_name="Test Bistro", weather_forecast=weather_forecast)

    prompt = captured["messages"][0]["content"]
    assert "Weather forecast for next week" in prompt
    assert "2026-07-11 (Saturday): 91°F, Sunny, 5% chance of rain" in prompt
    assert "2026-07-12 (Sunday): 68°F, Rain Showers, 85% chance of rain" in prompt


def test_no_weather_forecast_omits_the_block(monkeypatch):
    captured = _capture_create_with_retry(monkeypatch)

    generate_optimized_schedule(_minimal_analysis(), _minimal_shifts(), restaurant_name="Test Bistro")

    prompt = captured["messages"][0]["content"]
    assert "Weather forecast" not in prompt


def test_zero_percent_precip_omits_the_rain_chance_clause(monkeypatch):
    """precip_pct=0 is falsy, so the ', N% chance of rain' clause should be
    skipped entirely rather than rendering ', 0% chance of rain' — this
    exercises the `if w.get('precip_pct')` truthiness check in labor.py."""
    captured = _capture_create_with_retry(monkeypatch)
    weather_forecast = [
        {"date": "2026-07-11", "day_name": "Saturday", "high_f": 75, "short_forecast": "Clear", "precip_pct": 0},
    ]

    generate_optimized_schedule(_minimal_analysis(), _minimal_shifts(),
                                 restaurant_name="Test Bistro", weather_forecast=weather_forecast)

    prompt = captured["messages"][0]["content"]
    assert "2026-07-11 (Saturday): 75°F, Clear" in prompt
    assert "chance of rain" not in prompt
