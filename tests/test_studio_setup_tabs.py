"""The Studio's Setup tabs (owner, 10/1/26).

The Forecast tab said "built when the draft runs" and showed nothing until a
draft existed; it now previews the picked week through the same arithmetic
the draft is given (labor.week_hours_plan). The AI tab's placeholder showed
a rule about one person, which belongs in that person's scheduling notes.
The Advanced tab held only "redo some days"; it now carries the settings
that decide how the draft fits the budget. The Studio docks under where the
tab bar actually ends, so a banner above the header no longer hides its
step bar.
"""
import inspect
import os

import labor
import schedule_engine

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATES = ["2026-10-05", "2026-10-06", "2026-10-07", "2026-10-08", "2026-10-09", "2026-10-10", "2026-10-11"]


def _page():
    with open(os.path.join(ROOT, "templates", "dashboard.html"), encoding="utf-8") as f:
        return f.read()


def test_the_draft_and_the_forecast_tab_share_one_hours_plan():
    src = inspect.getsource(labor.generate_optimized_schedule)
    assert "week_hours_plan(" in src and "_MIN_DAYS_COVERED_TO_SCALE = 5" not in src
    assert "week_hours_plan(" in inspect.getsource(schedule_engine.forecast_preview)


def test_the_hours_plan_scales_each_weekday_to_par():
    by_day = {"2026-09-%02d" % d: {"actual": 10.0 + (d % 7)} for d in range(14, 28)}
    plan = labor.week_hours_plan({"by_day": by_day, "period_days": 14, "total_sales": 0}, DATES,
                                 labor_target=25.0, hourly_rate=15.0, projected_revenue_override=12000)
    assert plan["projected_revenue"] == 12000
    assert plan["hours_budget"] == round(12000 * 0.25 / 15.0, 1)
    assert plan["labor_budget_dollars"] == 3000
    assert set(plan["daily_target_hours"]) == set(DATES)
    assert abs(sum(plan["daily_target_hours"].values()) - plan["hours_budget"]) < 1.0


def test_no_sales_and_no_target_projects_nothing():
    plan = labor.week_hours_plan({"by_day": {}, "period_days": 3, "total_sales": 900}, DATES,
                                 labor_target=30.0, hourly_rate=15.0)
    assert plan["projected_revenue"] == 0 and plan["hours_budget"] == 0 and plan["daily_target_hours"] == {}


def test_the_preview_refuses_without_live_shifts(monkeypatch):
    monkeypatch.setattr(labor, "analyse_shifts_for_restaurant", lambda rid, with_salaries=False: {"is_live": False})
    monkeypatch.setattr(schedule_engine, "_no_shift_data_message", lambda rid, r=None: "no shifts on file")
    monkeypatch.setattr(schedule_engine, "get_restaurant", lambda rid: object(), raising=False)
    import models
    monkeypatch.setattr(models, "get_restaurant", lambda rid: object())
    assert schedule_engine.forecast_preview(1) == {"ok": False, "reason": "no shifts on file"}


def test_the_ai_tab_example_is_about_the_week_not_one_person():
    page = _page()
    i = page.index('id="sw-notes"')
    tag = page[i:page.index(">", i)]
    assert "Marcus" not in tag and "Keep two cooks on Friday lunch" in tag
    assert "swOpenTeam('notes')" in page and "toggleNotesPanel(); }" in page


def test_advanced_carries_the_budget_settings_and_mirrors_labor():
    page = _page()
    for el in ('id="ssa-trim"', 'id="ssa-cut"', 'id="ssa-sections"'):
        assert el in page
    assert "mirror = 'rul-trim'" in page and "mirror = 'rul-sections'" in page
    # One save at a time per field, and the Labor card gets what the server stored.
    assert "if (_ssaBusy[id]) { _ssaPending[id] = true; return; }" in page and "var v = d[field]" in page
    assert "_ssAdvLoaded" not in page


def test_the_studio_tabs_never_show_stale_or_duplicate_state():
    """Blind audit, 10/1/26."""
    page = _page()
    # The forecast re-reads when the labor target changes, and names its week.
    assert "var key = ws + '|' + (tg ? tg.value : '');" in page and "function () { _ssFcKey = null; }" in page
    assert "The week of <span class=\"hb-num\">' + _escHtml(mdy(d.week_start))" in page
    # The AI tab: the newest request wins, the rule lands on the week shown,
    # and a button can't be clicked twice while it saves.
    assert "if (tok !== _snrTok) return;" in page and "week_start: _snr.week_of" in page
    assert "btns[b].disabled = true" in page


def test_the_studio_docks_where_the_tab_bar_ends():
    page = _page()
    assert "function ssDock()" in page and "getBoundingClientRect().bottom" in page
    open_src = page[page.index("function studioOpen("):page.index("function ssOpenLatest(")]
    assert "ssDock()" in open_src


def test_the_route_reads_the_week_and_refuses_a_past_one(monkeypatch):
    from flask import Flask
    import strategy_routes
    monkeypatch.setattr(strategy_routes, "_sees_labor", lambda u: True)
    seen = {}
    monkeypatch.setattr(schedule_engine, "check_week_start", lambda rid, raw: (raw or None, None))
    monkeypatch.setattr(schedule_engine, "forecast_preview",
                        lambda rid, week=None: seen.setdefault("week", week) and {"ok": True, "days": []})
    app = Flask(__name__)
    with app.test_request_context("/labor/schedule-forecast?week_start=2026-10-14"):
        body, status = strategy_routes._do_schedule_forecast({"restaurant_id": 1, "role": "owner", "id": 1})
    assert status == 200 and body["available"] is True and seen["week"] == "2026-10-14"
    monkeypatch.setattr(schedule_engine, "check_week_start", lambda rid, raw: (None, "That week has already happened"))
    with app.test_request_context("/labor/schedule-forecast?week_start=2020-01-01"):
        body, status = strategy_routes._do_schedule_forecast({"restaurant_id": 1, "role": "owner", "id": 1})
    assert status == 400 and "already happened" in body["error"]
    monkeypatch.setattr(strategy_routes, "_sees_labor", lambda u: False)
    with app.test_request_context("/labor/schedule-forecast"):
        assert strategy_routes._do_schedule_forecast({"restaurant_id": 1, "role": "host", "id": 2})[1] == 403
