"""Schedule fix round 10/11/26, workstream TI — the server halves of the iOS
parity work:

TRUST-29  the review says WHY there is no labor % and the fix that works
          (`numbers.labor_missing`): never "generate a new draft" to an owner
          with no sales connected — that cannot produce a forecast.
TRUST-25  the review carries the gate's hours split (`hours`), so the phone's
          Send sheet runs the gate once (the review), never the publish check
          beside it.
TRUST-24  the gate's per-blocker flag is `demotable` (whether the owner's
          record may quiet a kind), never `hard` — the phone read `hard` as
          the colour and painted decisions red; the colour is `tier`.
TRUST-20  the web's open action goes to the panel and the person it names.

Nothing is sent; no model is called.
"""
import json

import pytest

import models
import prepublish
import schedule_overrides as so
from tests.test_ios_parity_labor import _rid, db, pushed  # noqa: F401

HEADER = "date,day,employee,role,shift_start,shift_end,scheduled_hours,notes"
MON = ["2026-10-12", "2026-10-13", "2026-10-14", "2026-10-15", "2026-10-16", "2026-10-17", "2026-10-18"]
SRC = open("templates/dashboard.html", encoding="utf-8").read()


@pytest.fixture(autouse=True)
def _fresh_cache():
    prepublish.invalidate()
    yield
    prepublish.invalidate()


def _week(rid, lines, economics=None):
    csv = HEADER + "\n" + "\n".join(lines)
    hid = models.save_schedule_history(rid, MON[0], MON[-1], 0, 0, 30, csv, [])
    conn = models.get_conn()
    conn.execute("UPDATE schedule_history SET economics_json=? WHERE id=?",
                 (json.dumps(economics) if economics is not None else None, hid))
    conn.commit()
    conn.close()
    return hid


# ── TRUST-29 ───────────────────────────────────────────────────────────────

def test_no_sales_says_connect_or_estimate_never_generate_again(db, monkeypatch):
    rid = _rid(db)
    import schedule_economics as se
    monkeypatch.setattr(se, "projected_weekly_revenue", lambda *a, **k: {"value": None, "weeks": 0})
    monkeypatch.setattr(prepublish, "_has_sales", lambda rid: False)
    got = prepublish.labor_missing(rid, {"total": 4200.0}, None, MON)
    assert got["reason"] == "no_sales"
    assert "generate" not in got["text"].lower()
    assert "Connect your POS" in got["text"] and "weekly sales estimate" in got["text"]
    assert got["action"]["action"] == "open" and got["action"]["payload"]["panel"] == "targets"


def test_a_forecast_that_exists_now_says_generating_again_measures_it(db, monkeypatch):
    rid = _rid(db)
    import schedule_economics as se
    monkeypatch.setattr(se, "projected_weekly_revenue",
                        lambda *a, **k: {"value": 41000.0, "source": "recent sales scaled to a week", "weeks": 6})
    got = prepublish.labor_missing(rid, {"total": 4200.0}, None, MON)
    assert got["reason"] == "forecast_since"
    assert "$41,000" in got["text"] and "Generating the week again" in got["text"]
    assert "action" not in got


def test_too_little_history_offers_the_estimate(db, monkeypatch):
    rid = _rid(db)
    import schedule_economics as se
    monkeypatch.setattr(se, "projected_weekly_revenue", lambda *a, **k: {"value": None, "weeks": 2})
    got = prepublish.labor_missing(rid, {"total": 4200.0}, None, MON)
    assert got["reason"] == "too_little_history" and "Only 2 complete weeks" in got["text"]
    assert got["action"]["payload"]["panel"] == "targets"


def test_the_review_says_why_labor_is_missing_and_carries_the_hours(db, monkeypatch):
    rid = _rid(db)
    import schedule_economics as se
    monkeypatch.setattr(se, "projected_weekly_revenue", lambda *a, **k: {"value": None, "weeks": 0})
    monkeypatch.setattr(prepublish, "_has_sales", lambda rid: False)
    hid = _week(rid, ["2026-10-12,Monday,Ana,Server,4:00pm,10:00pm,6,"], economics={"blended_rate": 15})
    out = prepublish.review(rid, hid)
    assert out["ok"]
    n = out["numbers"]
    assert n["labor_pct"] is None
    if n.get("cost"):
        assert n["labor_missing"]["reason"] == "no_sales"
    assert "hours" in out
    # The web's Summary tile and Numbers read the same sentence.
    assert "pnum.labor_missing.text" in SRC and "n.labor_missing.text" in SRC
    assert "generate a new draft to see it" not in SRC


# ── TRUST-24 ───────────────────────────────────────────────────────────────

def test_annotate_marks_demotable_never_hard():
    out = so.annotate(1, {"blockers": [{"key": "hours_over", "text": "Over", "tier": "decide"},
                                       {"key": "rule:no_manager:2026-10-12", "text": "Gap", "tier": "must_fix"}]},
                      db_path=None)
    by = {b["key"]: b for b in out["blockers"]}
    assert by["hours_over"]["demotable"] is True and "hard" not in by["hours_over"]
    assert by["rule:no_manager:2026-10-12"]["demotable"] is False
    # Severity first: the must-fix tier leads whatever the flag.
    assert out["blockers"][0]["key"].startswith("rule:no_manager")


# ── TRUST-20 (web half) ────────────────────────────────────────────────────

def test_the_webs_open_goes_to_the_panel_and_the_person():
    i = SRC.index("function openShift(p)")
    body = SRC[i:i + 1400]
    assert "p.panel === 'targets'" in body and "cavNav('account/restaurant')" in body
    assert "cavNav('labor/rules')" in body
    assert "openPersonSheet(p.employee)" in body
    assert "ssTab('basic')" in body
