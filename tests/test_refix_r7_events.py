"""Re-audit fix round R7 (9/29/26), event memory: QUALITY-4 (one night's lift
was given to every label on it and then multiplied), QUALITY-5 (holidays
keyed by words: New Year's Day read New Year's Eve), QUALITY-21 (effects
never aged)."""
import json
from datetime import date, timedelta

import pytest

import event_memory as em
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


def _rid(name="Event Tap"):
    return create_restaurant(Restaurant(name=name, owner_email=f"{name.split()[0].lower()}@x.test"))


def _sales(rid, d, net):
    conn = models.get_conn()
    conn.execute("INSERT OR REPLACE INTO labor_daily_history (restaurant_id, date, day_of_week, sales, labor_cost, "
                 "labor_pct, final, provider) VALUES (?,?,?,?,?,?,?,?)",
                 (rid, d.isoformat(), d.strftime("%A"), net, net * 0.3, 30.0, 1, "rpower"))
    conn.commit()
    conn.close()


def _row(rid, d, label, lift, kind="event", confounded=0):
    conn = models.get_conn()
    conn.execute("INSERT INTO event_outcomes (restaurant_id, business_date, weekday, kind, label, raw_label, source, "
                 "net, baseline, baseline_n, lift_pct, confounded) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                 (rid, d.isoformat(), d.strftime("%A"), kind, label, label, "x", 100 + lift, 100, 8, lift, confounded))
    conn.commit()
    conn.close()


# ── QUALITY-5: holidays by calendar identity ───────────────────────────────

def test_holidays_are_keyed_by_their_calendar_identity():
    assert em.holiday_key("New Year's Day") == "holiday:new_years_day"
    assert em.holiday_key("New Year's Eve") == "holiday:new_years_eve"
    assert em._holiday(date(2026, 1, 1)) == ("holiday:new_years_day", "New Year's Day")
    assert em._holiday(date(2025, 12, 24))[0] == "holiday:christmas_eve"
    assert em._holiday(date(2025, 12, 25))[0] == "holiday:christmas_day"


def test_new_years_day_never_reads_new_years_eve():
    # The reviewer's case (quality/t2_labels.py): NYE +80%, NYD -30%. New
    # Year's Day read the median of both, +25%.
    rid = _rid()
    _row(rid, date(2025, 12, 31), "holiday:new_years_eve", 80.0, kind="holiday")
    _row(rid, date(2026, 1, 1), "holiday:new_years_day", -30.0, kind="holiday")
    nyd = em.measured_effect(rid, "holiday:new_years_day")
    assert nyd["n"] == 1 and nyd["median_lift_pct"] == -30.0 and nyd["applies"] is True
    assert em.measured_effect(rid, "holiday:new_years_eve")["median_lift_pct"] == 80.0
    # An event's words never match a holiday's record.
    assert em.measured_effect(rid, "New Year's party") is None


def test_the_old_word_keyed_record_is_rekeyed_at_boot():
    rid = _rid()
    for d, lab, lift in ((date(2025, 12, 31), "new year's eve", 80.0), (date(2026, 1, 1), "new year's", -30.0),
                         (date(2025, 12, 24), "christmas eve", -40.0), (date(2025, 12, 25), "christmas", -90.0)):
        _row(rid, d, lab, lift, kind="holiday")
    em.refresh_effects(rid, {"new year's eve", "new year's", "christmas eve", "christmas"})
    got = em.rekey_record()
    assert got["rekeyed"] == 4
    conn = models.get_conn()
    labels = {r[0] for r in conn.execute("SELECT label FROM event_outcomes WHERE restaurant_id=?", (rid,))}
    effects = {r[0]: r[1] for r in conn.execute("SELECT label, median_lift_pct FROM event_effects WHERE restaurant_id=?",
                                                (rid,))}
    conn.close()
    assert labels == {"holiday:new_years_eve", "holiday:new_years_day", "holiday:christmas_eve",
                      "holiday:christmas_day"}
    assert effects == {"holiday:new_years_eve": 80.0, "holiday:new_years_day": -30.0,
                       "holiday:christmas_eve": -40.0, "holiday:christmas_day": -90.0}
    assert em.rekey_record() == {"rekeyed": 0, "marked": 0}          # idempotent


# ── QUALITY-4: one night's lift, one time ──────────────────────────────────

MOTHERS_DAYS = (date(2023, 5, 14), date(2024, 5, 12), date(2025, 5, 11))


def _mothers_day_history(rid):
    """Plain $4,000 Sundays for the eight weeks before each Mother's Day and
    before 5/10/26, and $5,600 (+40%) on each Mother's Day, when the owner
    also listed "Mother's Day brunch"."""
    import demand_signals
    for md in MOTHERS_DAYS + (date(2026, 5, 10),):
        for k in range(1, 9):
            _sales(rid, md - timedelta(weeks=k), 4000.0)
    for md in MOTHERS_DAYS:
        _sales(rid, md, 5600.0)
        demand_signals.save(rid, [{"date": md.isoformat(), "kind": "event", "label": "Mother's Day brunch"}])
        em.record_night(rid, md)


def test_a_night_with_two_things_is_confounded_and_never_counted_twice():
    import demand
    import demand_signals
    rid = _rid()
    _mothers_day_history(rid)
    conn = models.get_conn()
    rows = [dict(r) for r in conn.execute("SELECT business_date, kind, label, lift_pct, confounded, co_labels "
                                          "FROM event_outcomes WHERE restaurant_id=? ORDER BY business_date, kind",
                                          (rid,))]
    conn.close()
    assert len(rows) == 6 and all(r["lift_pct"] == 40.0 and r["confounded"] == 1 for r in rows)
    ev = next(r for r in rows if r["kind"] == "event")
    assert json.loads(ev["co_labels"]) == ["holiday:mothers_day"]
    # The reviewer's case (quality/t7_double.py): +40% x +40% forecast +96%.
    demand_signals.save(rid, [{"date": "2026-05-10", "kind": "event", "label": "Mother's Day brunch"}])
    eff = em.effects_for_day(rid, date(2026, 5, 10))
    assert eff["pct"] == 40.0
    assert len(eff["applied"]) == 1 and len(eff["subsumed"]) == 1
    fc = demand.forecast_day(rid, date(2026, 5, 10), calibrate=False)
    assert fc["effect_pct"] == 40.0 and fc["typical_sales"] == 5600.0
    # The summary says its nights were shared.
    assert em.measured_effect(rid, "holiday:mothers_day")["confounded"] is True
    assert "something else going on too" in em.measured_effect(rid, "Mother's Day brunch")["basis"]


def test_a_label_is_measured_on_its_own_nights_when_it_has_enough():
    rid = _rid()
    for k, lift in ((3, 25.0), (6, 25.0), (9, 25.0)):
        _row(rid, date(2026, 9, 29) - timedelta(weeks=k), "cubs", lift, kind="influence")
    _row(rid, date(2026, 9, 29) - timedelta(weeks=12), "cubs", 60.0, kind="influence", confounded=1)
    e = em.measured_effect(rid, "Cubs")
    assert e["n"] == 3 and e["median_lift_pct"] == 25.0 and e["confounded"] is False


def test_the_same_thing_twice_on_a_night_is_one_thing():
    marks = em._confounding([{"label": "cubs"}, {"label": "cubs cards"}])
    assert marks == {"cubs": (0, None), "cubs cards": (0, None)}
    marks = em._confounding([{"label": "cubs"}, {"label": "rain"}])
    assert marks["cubs"][0] == 1 and json.loads(marks["cubs"][1]) == ["rain"]


# ── QUALITY-21: effects age ────────────────────────────────────────────────

def test_the_weighted_median_is_the_plain_median_for_equal_weights():
    assert em._weighted_median([(10, 1), (30, 1)]) == 20
    assert em._weighted_median([(10, 1), (25, 1), (40, 1)]) == 25
    # A night three years older than the newest counts 0.35 of it.
    old = em._age_weight("2023-06-01", "2026-06-10")
    assert round(old, 3) == round(0.5 ** 1.5, 3)
    assert em._weighted_median([(10, 1.0), (40, old), (40, old)]) < 30


def test_a_record_that_moved_says_so_and_uses_its_recent_nights():
    rid = _rid()
    for d in (date(2023, 4, 4), date(2023, 5, 2), date(2023, 6, 6)):
        _row(rid, d, "cubs", 25.0, kind="influence")
    for d, lift in ((date(2026, 8, 4), 8.0), (date(2026, 8, 18), 7.0), (date(2026, 9, 1), 9.0)):
        _row(rid, d, "cubs", lift, kind="influence")
    e = em.measured_effect(rid, "cubs")
    assert e["median_lift_pct"] == 8.0 and e["n"] == 6
    assert e["drift"] == {"was": 25.0, "recent": 8.0, "n_recent": 3, "since": "2026-08-04"}
    assert "was +25%, the last 3 nights +8%" in e["basis"]
    em.refresh_effects(rid, {"cubs"})
    s = next(x for x in em.summaries(rid) if x["label"] == "cubs")
    assert s["drift"]["recent"] == 8.0 and "the last 3 nights +8%" in s["text"]
