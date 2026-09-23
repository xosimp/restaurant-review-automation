"""The DSR foundation (dsr/): the block contract, report versions and stages,
the searchable metric history, budgets, the category map and the fiscal
calendar Erik's sheet is labelled with ("Period 9 · Week 1", Wed–Tue)."""
import sys
from datetime import date

import pytest

import models
import dsr
from dsr import store, fiscal
from models import create_restaurant, Restaurant


@pytest.fixture
def db(db_path, monkeypatch):
    real = models.get_conn

    def conn(*a, **k):
        return real(db_path)
    for mod in list(sys.modules.values()):
        if mod is not None and getattr(mod, "get_conn", None) is real:
            monkeypatch.setattr(mod, "get_conn", conn)
    monkeypatch.setattr(models, "get_conn", conn)
    monkeypatch.setattr(models, "DB_PATH", db_path)
    return db_path


def _rid(db):
    return create_restaurant(Restaurant(name="DSR Co", owner_email="d@x.com"), db_path=db)


# ── the block contract ──────────────────────────────────────────────────────

def test_a_block_refuses_a_non_numeric_metric_and_keeps_unknown_as_none():
    with pytest.raises(ValueError):
        dsr.block(dsr.READY, metrics={"net": "$3,900"})
    b = dsr.block(dsr.READY, source="rpower", metrics={"net": 3900, "guests": None})
    assert b["metrics"] == {"net": 3900.0, "guests": None} and b["reason"] is None


def test_a_block_that_is_not_ready_always_says_why():
    b = dsr.block(dsr.AWAITING, block_name="sales")
    assert b["reason"] == "Awaiting POS synchronization"
    assert dsr.block(dsr.UNAVAILABLE, block_name="food")["reason"] == "Inventory data unavailable"
    with pytest.raises(ValueError):
        dsr.block("maybe")


# ── reports, versions, stages ───────────────────────────────────────────────

def test_versions_count_up_and_the_latest_is_the_report(db):
    rid = _rid(db)
    r1 = store.create_report(rid, "2026-09-22", trigger="closeday")
    r2 = store.create_report(rid, "2026-09-22", trigger="deadline")
    assert (r1["version"], r2["version"]) == (1, 2)
    assert store.get_report(rid, "2026-09-22")["id"] == r2["id"]
    assert store.get_report(rid, "2026-09-22", version=1)["id"] == r1["id"]


def test_stages_are_stamped_and_a_provisional_finish_is_marked(db):
    rid = _rid(db)
    r = store.create_report(rid, "2026-09-22")
    store.set_stage(r["id"], "collecting")
    store.set_stage(r["id"], "provisional")
    got = store.get_report_by_id(r["id"])
    assert got["status"] == "provisional" and got["provisional"] and got["finalized_at"]
    assert set(got["stages"]) >= {"scheduled", "collecting", "provisional"}
    with pytest.raises(ValueError):
        store.set_stage(r["id"], "done-ish")


def test_a_report_id_never_reaches_another_restaurants_report(db):
    a, b = _rid(db), _rid(db)
    r = store.create_report(a, "2026-09-22")
    assert store.get_report_by_id(r["id"], restaurant_id=b) is None
    assert store.get_report_by_id(r["id"], restaurant_id=a)["id"] == r["id"]


# ── facts and the searchable history ────────────────────────────────────────

def test_blocks_land_in_facts_with_missing_reasons_and_metrics_become_searchable(db):
    rid = _rid(db)
    for day, pct in (("2026-09-20", 22.0), ("2026-09-21", 27.5), ("2026-09-22", 31.0)):
        r = store.create_report(rid, day)
        store.save_block(r["id"], "labor", dsr.block(dsr.READY, source="rpower", metrics={"pct": pct}))
        store.save_block(r["id"], "food", dsr.block(dsr.UNAVAILABLE, block_name="food"))
    facts = store.get_report(rid, "2026-09-22")["facts"]
    assert facts["blocks"]["labor"]["metrics"]["pct"] == 31.0
    assert facts["missing"] == ["Inventory data unavailable"]
    assert store.find_days(rid, "labor.pct", ">", 25) == [("2026-09-22", 31.0), ("2026-09-21", 27.5)]
    assert [d for d, _ in store.metric_series(rid, "labor.pct", "2026-09-01", "2026-09-30")] == \
        ["2026-09-20", "2026-09-21", "2026-09-22"]


def test_an_unmeasured_metric_is_absent_from_the_history_not_zero(db):
    rid = _rid(db)
    r = store.create_report(rid, "2026-09-22")
    store.save_block(r["id"], "sales", dsr.block(dsr.READY, metrics={"net": 3900, "guests": None}))
    assert store.metric_series(rid, "sales.guests", "2026-09-01", "2026-09-30") == []
    assert store.find_days(rid, "sales.guests", "<", 1) == []


def test_a_later_version_replaces_the_nights_metrics(db):
    rid = _rid(db)
    r1 = store.create_report(rid, "2026-09-22")
    store.save_block(r1["id"], "sales", dsr.block(dsr.READY, metrics={"net": 3000}))
    r2 = store.create_report(rid, "2026-09-22")
    store.save_block(r2["id"], "sales", dsr.block(dsr.READY, metrics={"net": 3900}))
    assert store.metric_series(rid, "sales.net", "2026-09-22", "2026-09-22") == [("2026-09-22", 3900.0)]


# ── budgets and the category map ────────────────────────────────────────────

def test_budgets_are_per_night_and_a_night_without_one_is_absent(db):
    rid = _rid(db)
    store.set_budget(rid, "2026-08-26", gross=7500)
    store.set_budget(rid, "2026-08-29", gross=20000, net=18500)
    b = store.budgets_for(rid, "2026-08-26", "2026-09-01")
    assert b["2026-08-26"] == {"gross": 7500.0, "net": None} and "2026-08-27" not in b


def test_an_unmapped_pos_department_is_shown_as_unmapped_never_guessed(db):
    rid = _rid(db)
    store.set_category(rid, "Draft Beer", "Beer")
    m = store.category_map(rid)
    assert store.category_for("draft beer", m) == "Beer"
    assert store.category_for("Merch", m) == dsr.UNMAPPED


# ── the fiscal calendar ─────────────────────────────────────────────────────

def test_eriks_sheet_period_9_week_1_is_wednesday_8_26_26():
    r = Restaurant(name="EJ", owner_email="e@x.com")
    r.fiscal_week_start_dow = 2               # Wednesday
    r.fiscal_year_start = "2026-01-14"        # a Wednesday: 32 weeks before 8/26; 13 periods of 4 weeks
    for d in ("2026-08-26", "2026-08-29", "2026-09-01"):
        p = fiscal.position(r, d)
        assert (p["period"], p["week"], p["week_start"], p["week_end"]) == (9, 1, "2026-08-26", "2026-09-01"), d
    assert fiscal.position(r, "2026-09-02")["week"] == 2
    assert fiscal.label(r, "2026-08-26") == "Period 9 · Week 1"


def test_no_year_start_means_no_period_number_only_the_week():
    r = Restaurant(name="EJ", owner_email="e@x.com")
    r.fiscal_week_start_dow = 2
    p = fiscal.position(r, date(2026, 8, 28))
    assert p["period"] is None and p["week_start"] == "2026-08-26"
    assert fiscal.label(r, "2026-08-28") == "Week of 8/26/26"


def test_the_fiscal_columns_round_trip_through_update_restaurant(db):
    rid = _rid(db)
    models.update_restaurant(rid, {"fiscal_week_start_dow": 2, "fiscal_year_start": "2026-01-14",
                                   "fiscal_period_scheme": "4x13", "dsr_deadline_hour": 5}, db_path=db)
    r = models.get_restaurant(rid, db_path=db)
    assert (r.fiscal_week_start_dow, r.fiscal_year_start, r.fiscal_period_scheme, r.dsr_enabled,
            r.dsr_deadline_hour) == (2, "2026-01-14", "4x13", 1, 5)
