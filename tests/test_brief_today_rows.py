"""The Before service "Today" line as a lead and one row per item (owner,
10/5/26: "wordy and crowded"); the one-sentence text stays for a text or a
push."""
import morning_brief


def test_the_today_line_carries_a_head_and_rows(monkeypatch):
    monkeypatch.setattr(morning_brief, "_holiday_today", lambda today: None)
    carry = {"forecast": {"typical": 6089.0}, "items": [
        {"kind": "weather", "text": "Sunny, high 67°"},
        {"kind": "event", "text": "White Sox at Cleveland Guardians · 4pm · TBS", "plain": "White Sox game"},
        {"kind": "staffing", "text": "On 10/3/26 you ran 4 Bartender PM against a usual Saturday's 2."}]}
    line = morning_brief._carry_today_line(carry, None, show_forecast=True)
    assert line["head"] == "Today, from last night's report: about $6,089."
    assert line["rows"] == ["Sunny, high 67°.", "White Sox at Cleveland Guardians · 4pm · TBS.",
                            "On 10/3/26 you ran 4 Bartender PM against a usual Saturday's 2."]
    assert line["text"].startswith("Today, from last night's report: about $6,089. Sunny, high 67° · White Sox")
