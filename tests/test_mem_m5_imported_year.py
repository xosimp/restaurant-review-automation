"""Memory fix round M5, imported_year (memory audit 9/29/26, QUALITY-14).

A client who imported a year of DSR workbooks but had 60 days of POS
history got no seasonality, no holiday lift, no year-over-year context and
no seasonal adjustment on trackers, although last year's nights were in the
database: dsr_history_import was read only by the DSR's own baselines. Every
one of them now reads the ONE last-year reader (canonical_facts:
the night's report, then the import, then the POS sync), naming its source.
"""
from datetime import date, timedelta

import pytest

import models
from models import Restaurant, create_restaurant


@pytest.fixture(autouse=True)
def _db(db_path, monkeypatch):
    real = models.get_conn
    monkeypatch.setattr(models, "get_conn", lambda *a, **k: real(db_path))
    monkeypatch.setattr(models, "DB_PATH", db_path)
    import pos
    monkeypatch.setattr(pos, "connected_provider", lambda rid: ("rpower", object()))
    yield


def _rid():
    return create_restaurant(Restaurant(name="Import Co", owner_email="import@x.test"))


def _import_year(rid, start, days, net_for):
    from dsr import store
    store.import_history(rid, [{"date": (start + timedelta(days=i)).isoformat(), "gross": net_for(start + timedelta(days=i)) + 500,
                                "net": net_for(start + timedelta(days=i))} for i in range(days)])


def test_year_over_year_context_reads_the_imported_workbook_and_says_so():
    rid = _rid()
    week = [date(2026, 10, 5) + timedelta(days=i) for i in range(7)]
    _import_year(rid, date(2025, 9, 1), 60, lambda d: 5000.0 + d.weekday() * 100)
    rows = models.get_yoy_schedule_context(rid, [d.isoformat() for d in week])
    assert all(r["yoy_sales"] for r in rows) and {r["yoy_source"] for r in rows} == {"import"}
    mon = rows[0]
    assert mon["yoy_date"] == (week[0] - timedelta(weeks=52)).isoformat() and mon["yoy_sales"] == 5000.0
    assert mon["yoy_labor_pct"] is None and mon["yoy_hours"] is None
    import labor
    import inspect
    assert "your imported DSR workbook" in inspect.getsource(labor)


def test_seasonality_counts_an_imported_year():
    from intelligence import memory
    rid = _rid()
    start = date(2025, 1, 1)
    _import_year(rid, start, 365, lambda d: 4000.0 + (1500.0 if d.month in (6, 7) else 0.0))
    se = memory.seasonality(rid)
    assert se["available"] and set(se["peak"]) == {"Jun", "Jul"}
    assert "imported DSR workbooks" in se["sources"]
    lines = memory.lines({"seasonality": se})
    assert any("imported DSR workbooks" in l for l in lines)


def test_a_holiday_lift_comes_from_the_imported_year():
    import schedule_economics as econ
    rid = _rid()
    # Halloween 2025 was a Friday: $9,000 against $6,000 Fridays around it.
    _import_year(rid, date(2025, 10, 1), 61, lambda d: 9000.0 if d == date(2025, 10, 31) else 6000.0)
    lift = econ.holiday_lift(rid, ["2026-10-31"])
    h = lift["2026-10-31"]
    assert h["lift_pct"] == 50 and h["source"] == "import" and "imported DSR workbooks" in h["based_on"]


def test_the_trackers_seasonal_shift_reads_the_imported_year():
    import outcomes
    rid = _rid()
    _import_year(rid, date(2025, 6, 1), 120, lambda d: 5000.0 if d < date(2025, 7, 15) else 5500.0)
    base = (date(2026, 6, 16).isoformat(), date(2026, 7, 13).isoformat())
    win = (date(2026, 7, 14).isoformat(), date(2026, 8, 10).isoformat())
    shift = outcomes._seasonal_shift(rid, "sales", base, win, None)
    assert shift is not None
    b, w = shift
    assert b == pytest.approx(5000.0, abs=1) and w > 5400
    # Labor has no imported figure: no shift, never a guess.
    assert outcomes._seasonal_shift(rid, "labor_pct", base, win, None) is None
