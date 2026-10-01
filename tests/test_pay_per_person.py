"""Hourly pay is per person (owner, 9/30/26: "shouldn't all employees have
this data since we pull a month of data? If this is per employee now, why do
we have the role box?").

RPOWER puts a person's rate on some punches and $0 on others: at Simple EJ's
every Host PM and Barback PM job row is $0 while the same people's AM punches
carry $15-17, and those hours were costed at the $26 blended default. A $0
punch now costs that person's rate from their other punches, then the owner's
rate for the role, then what the role's people make, then the blended rate.
"""
import json
import os

import pytest

import labor
import models
import schedule_economics as econ

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = open(os.path.join(ROOT, "templates", "dashboard.html"), encoding="utf-8").read()


def _s(date, emp, role, hours=5, pay=""):
    return {"date": date, "day": "Monday", "employee": emp, "role": role, "shift_start": "16:00",
            "shift_end": "21:00", "scheduled_hours": hours, "actual_hours": hours, "sales": "4000",
            "pay_rate": pay}


SHIFTS = [
    _s("2026-09-01", "Emily Meredith", "Host AM", pay="17"),
    _s("2026-09-02", "Emily Meredith", "Host PM"),                       # $0 on the punch
    _s("2026-09-01", "Mia Martin", "Host AM", pay="15"),
    _s("2026-09-03", "Mia Martin", "Host PM"),
    _s("2026-09-03", "Lilli Margewich", "Host PM"),                     # no rate anywhere
    _s("2026-08-01", "Caden Heflin", "Utility AM", pay="9"),
    _s("2026-09-01", "Caden Heflin", "Utility AM", pay="15"),           # latest wins
]


def test_a_zero_punch_costs_the_persons_own_rate_from_their_other_punches():
    people, roles = labor.rate_book(SHIFTS)
    assert people == {"emily meredith": 17.0, "mia martin": 15.0, "caden heflin": 15.0}
    assert roles["host pm"] == 16.0 and roles["host am"] == 16.0          # median of 15 and 17
    assert labor._shift_rate(SHIFTS[1], {"_default": 26.0}, 26.0, people, roles) == 17.0
    # nobody's rate: the role's people, never the $26 default
    assert labor._shift_rate(SHIFTS[4], {"_default": 26.0}, 26.0, people, roles) == 16.0
    # the owner's role rate still beats what the role's people make
    assert labor._shift_rate(SHIFTS[4], {"Host PM": 14.0, "_default": 26.0}, 26.0, people, roles) == 14.0
    # and the owner's own rate for a person beats their POS rate on a $0 punch
    own, _ = labor.rate_book(SHIFTS, {"emily meredith": 19.0})
    assert labor._shift_rate(SHIFTS[1], {"_default": 26.0}, 26.0, own, roles) == 19.0
    # a paid punch is what the hour cost, whatever else is known
    assert labor._shift_rate(SHIFTS[0], {"_default": 26.0}, 26.0, own, roles) == 17.0


def test_the_analysis_costs_zero_punches_at_the_persons_rate():
    out = labor.analyse_shifts(SHIFTS, hourly_rate=26.0, labor_target=30.0, role_rates={"_default": 26.0})
    # 5h each: 17+17 (Emily) + 15+15 (Mia) + 16 (Lilli, the role's median) + 9+15 (Caden)
    assert out["total_labor_cost"] == pytest.approx(5 * (17 + 17 + 15 + 15 + 16 + 9 + 15))


def test_a_drafted_week_is_priced_at_each_persons_rate():
    rows = [{"date": "2026-10-05", "employee": "Oscar Avelar", "role": "Kitchen", "shift_start": "9:00am",
             "shift_end": "5:00pm", "scheduled_hours": "8"},
            {"date": "2026-10-05", "employee": "New Cook", "role": "Kitchen", "shift_start": "9:00am",
             "shift_end": "5:00pm", "scheduled_hours": "8"}]
    cost = econ.priced_cost(rows, {}, 26.0, person_rates={"oscar avelar": 24.0}, role_typical={"kitchen": 21.0})
    assert cost["total"] == pytest.approx(8 * 24 + 8 * 21)
    assert econ.priced_cost(rows, {}, 26.0)["total"] == pytest.approx(16 * 26)


@pytest.fixture
def _db(db_path, monkeypatch):
    real = models.get_conn
    monkeypatch.setattr(models, "get_conn", lambda *a, **k: real(db_path))
    monkeypatch.setattr(models, "DB_PATH", db_path)
    return db_path


def test_each_role_says_what_its_people_make_and_who_has_no_rate(_db, monkeypatch):
    import staff_settings
    import strategy_routes as sr
    rid = models.create_restaurant(models.Restaurant(name="EJ", owner_email="e@x.test", hourly_rate=26.0), db_path=_db)
    monkeypatch.setattr(staff_settings, "roster", lambda *a, **k: [
        {"name": "Emily Meredith", "role": "Host PM"}, {"name": "Mia Martin", "role": "Host PM"},
        {"name": "Lilli Margewich", "role": "Host PM"}, {"name": "Brand New", "role": "Barback"}])
    monkeypatch.setattr(labor, "load_shifts_for_restaurant", lambda *a, **k: SHIFTS)
    t = sr._targets_payload(rid)
    host = t["role_pay"]["Host PM"]
    assert (host["low"], host["high"], host["people"], host["unrated"]) == (15.0, 17.0, 3, 1)
    assert host["fallback"] == 16.0 and host["fallback_from"] == "typical"
    bb = t["role_pay"]["Barback"]
    assert bb["low"] is None and bb["unrated"] == 1 and bb["fallback"] == 26.0 and bb["fallback_from"] == "blended"
    lilli = [p for p in t["people_by_role"]["Host PM"] if p["name"] == "Lilli Margewich"][0]
    assert lilli["pos_rate"] is None


def test_the_screen_is_one_closed_section_and_a_role_box_only_where_nobody_has_a_rate():
    assert 'details class="co-more tg-pay" id="as-tg-pay"' in SRC
    assert "Pay by role<small>" not in SRC
    body = SRC[SRC.index("function tgRender("):SRC.index("function tgWords(")]
    # the role's box is the fallback branch of its range, never beside it
    assert "(range ? '<span class=\"tg-pos hb-num\">'" in body
    assert "without a rate" in body and "costed at" in body
    # saving one role's rate keeps the rates of roles whose box isn't shown
    save = SRC[SRC.index("Only this role changes"):SRC.index("body.role_rates = rates;")]
    assert "_tg.targets" in save and "querySelectorAll('#as-tg-roles [data-tg-role]')" not in save


def test_the_labor_headline_no_longer_repeats_the_salaries_total():
    assert "salaries &middot;" not in SRC
    assert "labor_salaried.cost" not in SRC
