"""The ROI audit's invariants (Sep 2026).

The product could always say what was WRONG and what it was WORTH fixing.
It could not say what fixing it had been worth, because almost nothing ever
started a tracker: outcomes.record had three call sites, one of which was a
route no client called and another a conversation with Ask Cavnar.

These tests pin the pieces that close that loop, and the honesty rules that
keep the resulting number defensible.
"""
from datetime import date, timedelta

import pytest

import metrics
import models
import outcomes
import promise
# Imported at module scope DELIBERATELY. sales_audits does `from models import
# get_conn` at ITS module scope, so whichever test first triggers the import
# binds whatever models.get_conn is at that moment — and the autouse fixture
# below has already replaced it with a lambda hard-wired to that one test's
# db_path. Importing it inside a helper made every later test create its
# sales_audits table in the FIRST test's database. This is the exact bound-
# import hazard CLAUDE.md documents, and it bit this file.
import sales_audits
from models import create_restaurant, Restaurant, get_conn


@pytest.fixture(autouse=True)
def _redirect_db(monkeypatch, db_path):
    real = models.get_conn
    monkeypatch.setattr(models, "get_conn", lambda *a, **k: real(db_path))


def _restaurant(db_path, **kw):
    return create_restaurant(Restaurant(name=kw.pop("name", "ROI Co"),
                                        owner_email="r@x.com", **kw), db_path=db_path)


def _sales(db_path, rid, days=60, per_day=2000.0, start=None):
    start = start or (date.today() - timedelta(days=days))
    conn = get_conn(db_path)
    for i in range(days):
        d = (start + timedelta(days=i)).isoformat()
        conn.execute("INSERT OR REPLACE INTO labor_daily_history "
                     "(restaurant_id, date, day_of_week, sales, labor_cost) VALUES (?,?,?,?,?)",
                     (rid, d, (start + timedelta(days=i)).strftime("%A"), per_day, per_day * 0.30))
    conn.commit()
    conn.close()


# ── the roll-up ─────────────────────────────────────────────────────────────

def _evaluated(db_path, rid, title, metric, dollars, verdict="improved", evaluate_on=None):
    conn = get_conn(db_path)
    conn.execute(
        "INSERT INTO recommendation_outcomes (restaurant_id, source, source_key, title, metric, "
        "baseline_value, started_on, evaluate_on, after_value, verdict, dollars_monthly, status) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?, 'evaluated')",
        (rid, "test", f"k:{title}", title, metric, 30.0, "2026-01-01",
         evaluate_on or date.today().isoformat(), 28.0, verdict, dollars))
    conn.commit()
    conn.close()


def test_total_value_sums_only_measured_wins(db_path):
    rid = _restaurant(db_path)
    _evaluated(db_path, rid, "Trimmed Mondays", "labor_pct", 410.0)
    _evaluated(db_path, rid, "Cut prep waste", "weekly_waste", 120.0)
    _evaluated(db_path, rid, "Tried a special", "sales", 900.0, verdict="no_clear_change")
    _evaluated(db_path, rid, "Reprice", "food_cost_pct", None, verdict="unknown")

    v = outcomes.total_value(rid, db_path=db_path)
    assert v["monthly"] == 530.0
    assert v["annual"] == 530.0 * 12
    assert v["wins"] == 2


def test_total_value_reports_its_own_denominator(db_path):
    """A total without the count of what could NOT be measured is the number
    an owner stops believing the second time they see it."""
    rid = _restaurant(db_path)
    _evaluated(db_path, rid, "Worked", "labor_pct", 410.0)
    _evaluated(db_path, rid, "Flat", "sales", 900.0, verdict="no_clear_change")
    _evaluated(db_path, rid, "Unreadable", "food_cost_pct", None, verdict="unknown")

    v = outcomes.total_value(rid, db_path=db_path)
    assert v["evaluated"] == 3
    assert v["no_clear_change"] == 1
    assert v["unmeasurable"] == 1


def test_value_is_attributed_to_the_module_that_earned_it(db_path):
    """Owners buy modules one at a time. "Which of the four I pay for is
    paying for itself" had no answer before this."""
    rid = _restaurant(db_path)
    _evaluated(db_path, rid, "Labor", "labor_pct", 400.0)
    _evaluated(db_path, rid, "Waste", "weekly_waste", 100.0)
    v = outcomes.total_value(rid, db_path=db_path)
    assert v["by_module"] == {"labor": 400.0, "inventory": 100.0}


def test_best_ever_has_no_window_and_no_dollar_floor(db_path):
    """strategy_jobs picks the biggest win of ONE daily pass, above $100, to
    notify on. "Which recommendation created the biggest impact" is a
    different question: all time, any size."""
    rid = _restaurant(db_path)
    _evaluated(db_path, rid, "Small but old", "labor_pct", 40.0,
               evaluate_on=(date.today() - timedelta(days=300)).isoformat())
    _evaluated(db_path, rid, "Bigger", "weekly_waste", 90.0,
               evaluate_on=(date.today() - timedelta(days=200)).isoformat())
    best = outcomes.best_ever(rid, db_path=db_path)
    assert best["title"] == "Bigger"


def test_a_worsened_result_is_never_counted_as_value(db_path):
    rid = _restaurant(db_path)
    _evaluated(db_path, rid, "Backfired", "labor_pct", 500.0, verdict="worsened")
    assert outcomes.total_value(rid, db_path=db_path)["monthly"] == 0
    assert outcomes.best_ever(rid, db_path=db_path) is None


# ── the new metrics ─────────────────────────────────────────────────────────

def _loss(db_path, rid, kind, day, amount, events=3):
    conn = get_conn(db_path)
    conn.execute("INSERT OR REPLACE INTO pos_loss_daily "
                 "(restaurant_id, business_date, kind, amount, events) VALUES (?,?,?,?,?)",
                 (rid, day, kind, amount, events))
    conn.commit()
    conn.close()


def test_comp_rate_is_a_share_of_sales_not_a_total(db_path):
    """Comps fall on a quiet week without anything having changed, so a
    total would read a slow month as an improvement."""
    rid = _restaurant(db_path)
    start = date.today() - timedelta(days=10)
    _sales(db_path, rid, days=10, per_day=1000.0, start=start)
    # loss_detection writes a row for every day it asked about, zero
    # included — a day with no row was never asked (re-audit A3).
    for i in range(10):
        _loss(db_path, rid, "comp", (start + timedelta(days=i)).isoformat(), 200.0 if i == 1 else 0.0,
              events=3 if i == 1 else 0)

    value, detail = metrics.measure(rid, "comp_rate", start.isoformat(),
                                    (start + timedelta(days=9)).isoformat(), db_path)
    assert value == 2.0          # $200 of comps on $10,000 of sales
    assert "comp" in detail


def test_no_synced_loss_data_is_unknown_not_zero(db_path):
    """A POS that does not report comps looks identical to a restaurant with
    none. metrics.py's rule is that None is never 0."""
    rid = _restaurant(db_path)
    start = date.today() - timedelta(days=10)
    _sales(db_path, rid, days=10, start=start)
    value, detail = metrics.measure(rid, "comp_rate", start.isoformat(),
                                    (start + timedelta(days=9)).isoformat(), db_path)
    assert value is None
    assert "no comp data synced" in detail


def test_comps_without_a_sales_base_are_unmeasurable(db_path):
    rid = _restaurant(db_path)
    start = date.today() - timedelta(days=10)
    _loss(db_path, rid, "comp", (start + timedelta(days=1)).isoformat(), 200.0)
    value, detail = metrics.measure(rid, "comp_rate", start.isoformat(),
                                    (start + timedelta(days=9)).isoformat(), db_path)
    assert value is None
    assert "no sales" in detail


# ── one calendar ────────────────────────────────────────────────────────────

def test_a_daily_and_a_weekly_saving_annualise_on_the_same_month(db_path):
    """A per-day figure was annualised at 30 days a month while a per-week
    figure used 52/12 weeks — which is 30.33 days. Two constants for one
    month meant $10/day and $70/week came out different."""
    rid = _restaurant(db_path)
    # A restaurant that trades every day (re-audit A1 prices a sales day on
    # the restaurant's own trading days).
    _sales(db_path, rid, days=28, start=date.today() - timedelta(days=28))
    daily = metrics.monthly_dollars(rid, "sales", 10.0, db_path)
    weekly = metrics.monthly_dollars(rid, "weekly_waste", -70.0, db_path)
    assert abs(daily - weekly) < 0.01


# ── the audit comparison ────────────────────────────────────────────────────

def _audit(db_path, rid, audit_date, categories, totals=None):
    import json
    sales_audits.init_sales_audits(db_path)
    conn = get_conn(db_path)
    cur = conn.execute(
        "INSERT INTO sales_audits (restaurant_name, audit_date, results_json, linked_restaurant_id) "
        "VALUES (?,?,?,?)",
        ("ROI Co", audit_date,
         json.dumps({"categories": categories,
                     "totals": totals or {"annual": {"low": 18000, "high": 34000}}}), rid))
    conn.commit()
    aid = cur.lastrowid
    conn.close()
    return aid


def test_no_linked_audit_says_so_rather_than_inventing_one(db_path):
    rid = _restaurant(db_path)
    out = promise.compare(rid, db_path=db_path)
    assert out["available"] is False
    assert "no sales audit" in out["reason"]


def test_the_audit_estimate_sits_beside_the_measurements(db_path):
    """The comparison the product could make for a PROSPECT and never for a
    CLIENT. sales_audits.linked_restaurant_id was a declared column that
    nothing read or wrote."""
    rid = _restaurant(db_path, module_labor=1)
    audit_day = date.today() - timedelta(days=120)
    _sales(db_path, rid, days=200, per_day=2000.0,
           start=date.today() - timedelta(days=200))
    _audit(db_path, rid, audit_day.isoformat(), {
        "labor": {"label": "Labor", "status": "ok", "low": 18000, "high": 34000,
                  "confidence": "medium"},
    })
    out = promise.compare(rid, db_path=db_path)
    assert out["available"] is True
    row = next(c for c in out["categories"] if c["key"] == "labor")
    assert row["promised_low"] == 18000 and row["promised_high"] == 34000
    assert row["then"] is not None and row["now"] is not None
    assert out["caveat"]


def test_a_category_the_product_cannot_measure_says_so(db_path):
    """The audit sizes bar and waitlist; Cavnar AI has no pour-cost or
    turn-time data. Dropping them would quietly overstate coverage, and
    scoring them zero would be worse."""
    rid = _restaurant(db_path)
    _audit(db_path, rid, (date.today() - timedelta(days=60)).isoformat(), {
        "bar": {"label": "Bar", "status": "ok", "low": 5000, "high": 9000},
        "waitlist": {"label": "Waitlist", "status": "ok", "low": 1000, "high": 3000},
    })
    out = promise.compare(rid, db_path=db_path)
    states = {c["key"]: c["state"] for c in out["categories"]}
    assert states["bar"] == "not_measurable"
    assert states["waitlist"] == "not_measurable"
    for c in out["categories"]:
        assert c["note"]


def test_a_category_the_audit_could_not_size_is_carried_not_dropped(db_path):
    rid = _restaurant(db_path, module_labor=1)
    _audit(db_path, rid, (date.today() - timedelta(days=60)).isoformat(), {
        "labor": {"label": "Labor", "status": "insufficient", "low": 0, "high": 0},
    })
    out = promise.compare(rid, db_path=db_path)
    row = next(c for c in out["categories"] if c["key"] == "labor")
    assert row["state"] == "not_sized"


def test_link_is_idempotent_and_reversible(db_path):
    rid = _restaurant(db_path)
    aid = _audit(db_path, rid, date.today().isoformat(), {})
    promise.link(aid, rid, db_path=db_path)
    promise.link(aid, rid, db_path=db_path)
    assert promise.linked_audit(rid, db_path=db_path)["id"] == aid
    promise.link(aid, None, db_path=db_path)
    assert promise.linked_audit(rid, db_path=db_path) is None

# ── permission: filter before the sum, never after ──────────────────────────

def test_denied_modules_are_removed_before_the_total_is_summed(db_path):
    """A manager without FOOD_COST_VIEW must not be able to subtract their
    way to the margin dollars.

    The first version of /api/value stripped by_module and the opportunity
    items but left the headline total whole — so visible_total minus the
    visible module rows WAS the food-cost figure. Filtering has to happen
    before the sum, not on the way out.
    """
    rid = _restaurant(db_path)
    _evaluated(db_path, rid, "Trimmed Mondays", "labor_pct", 400.0)
    _evaluated(db_path, rid, "Cut food cost", "food_cost_pct", 250.0)

    full = outcomes.total_value(rid, db_path=db_path)
    assert full["monthly"] == 650.0

    limited = outcomes.total_value(rid, db_path=db_path, denied_modules={"inventory"})
    assert limited["monthly"] == 400.0
    assert "inventory" not in limited["by_module"]
    # and the denominator moves with it, or the missing row is inferable.
    assert limited["evaluated"] == 1
    assert sum(limited["by_module"].values()) == limited["monthly"]


def test_best_ever_respects_denied_modules(db_path):
    rid = _restaurant(db_path)
    _evaluated(db_path, rid, "Labor win", "labor_pct", 100.0)
    _evaluated(db_path, rid, "Huge margin win", "food_cost_pct", 900.0)
    assert outcomes.best_ever(rid, db_path=db_path)["title"] == "Huge margin win"
    limited = outcomes.best_ever(rid, db_path=db_path, denied_modules={"inventory"})
    assert limited["title"] == "Labor win"
