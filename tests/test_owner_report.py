"""Recommendation ROI audit #28 — "What worked for you".

owner_report.what_worked builds the owner's paragraph deterministically from
the ledger summary, the stored before/after results and the measured days:
every clause only when its own minimum is met, every figure equal to its
source, no causal word, and redacted per viewer exactly like /recs/summary.
GET /recs/what-worked serves it on both twins.
"""
import re
import sys
from datetime import date, timedelta

import pytest
from flask import Flask

import auth
import models
import rec_ledger as rl
from models import Restaurant, create_restaurant

# Imported before the fixture patches get_conn (the bound-import hazard).
import outcomes  # noqa: E402
import owner_report  # noqa: E402
import rec_learning  # noqa: E402

CAUSAL = re.compile(r"\b(caused|causes|because of|thanks to|drove|driven by|led to|resulted in|due to|"
                    r"saved you|made you|generated)\b", re.I)


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
    monkeypatch.setattr(auth, "DB_PATH", db_path)
    auth.init_auth(db_path=db_path)
    return db_path


@pytest.fixture
def client(db):
    from strategy_routes import strategy_bp, strategy_mobile_bp
    app = Flask(__name__, template_folder="../templates")
    app.register_blueprint(strategy_bp)
    app.register_blueprint(strategy_mobile_bp)
    return app.test_client()


def _as(monkeypatch, rid, role="client", uid=7):
    user = {"id": uid, "restaurant_id": rid, "base_restaurant_id": rid, "is_admin": 0, "role": role,
            "username": "u", "email": "u@x.com"}
    monkeypatch.setattr(auth, "get_current_user", lambda: user)
    monkeypatch.setattr(auth, "get_session_user", lambda token, *a, **k: user if token == "t" else None)
    return user


PHONE = {"Authorization": "Bearer t"}


def _rid(db, name="Worked Co"):
    return create_restaurant(Restaurant(name=name, owner_email=f"{name.split()[0].lower()}@x.test",
                                        module_labor=1, module_inventory=1), db_path=db)


def _x(db, sql, args=()):
    c = models.get_conn(db)
    try:
        cur = c.execute(sql, args)
        c.commit()
        return cur.lastrowid
    finally:
        c.close()


def _ignored(db, rid, key, module):
    rec = rl.present(rid, key, module, "home", db_path=db)
    _x(db, "UPDATE rec_instances SET created_at=datetime('now','-20 days') WHERE rec_id=?", (rec,))
    rl.expire_stale(db_path=db)


def _taken(db, rid, key, module="labor"):
    rl.present(rid, key, module, "home", db_path=db)
    rl.record(rid, key, "accepted", surface="home", db_path=db)


def _result(db, rid, key, metric, verdict, baseline, after, start_days_ago, window=28, module="labor",
            dollars=None, link=True, source="recommendation"):
    """An evaluated tracker whose after-window starts `start_days_ago` days
    ago, linked to the recommendation it measured."""
    start = date.today() - timedelta(days=start_days_ago)
    end = start + timedelta(days=window - 1)
    delta = after - baseline
    tid = _x(db, "INSERT INTO recommendation_outcomes (restaurant_id, source, source_key, title, metric, "
                 "baseline_value, started_on, evaluate_on, after_value, after_start, after_end, verdict, delta, "
                 "delta_pct, dollars_monthly, status, module) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'evaluated',?)",
             (rid, source, key, key, metric, baseline, start.isoformat(), (end + timedelta(days=1)).isoformat(),
              after, start.isoformat(), end.isoformat(), verdict, delta, round(delta / baseline * 100, 1),
              dollars, module))
    if link:
        rl.present(rid, key, module if module != "inventory" else "food", "home", db_path=db)
        rl.record(rid, key, "accepted", surface="home", db_path=db)
        rl.link_tracker(rid, key, tid, db_path=db)
    return tid


def _day(db, rid, tid, day, dollars, module="labor", metric="overtime_hours", counted=1):
    _x(db, "INSERT INTO outcome_value_days (outcome_id, restaurant_id, day, module, metric, family, sign, dollars, "
           "held, counted, basis) VALUES (?,?,?,?,?,?,?,?,1,?,'daily')",
       (tid, rid, day.isoformat(), module, metric, "labor_cost" if module == "labor" else "food_cost",
        1 if dollars >= 0 else -1, dollars, counted))


# ── not enough: nothing is said ──────────────────────────────────────────────

def test_with_too_little_nothing_is_said_and_nothing_is_zeroed(db):
    rid = _rid(db)
    for d in ("Monday", "Tuesday", "Wednesday"):
        _taken(db, rid, f"trim_day:{d}")
    tid = _result(db, rid, "cut_ot:one", "overtime_hours", "improved", 20.0, 16.0, 60)
    _day(db, rid, tid, date.today() - timedelta(days=5), 30.0)
    out = owner_report.what_worked(rid, days=180)
    assert set(out) == {"ok", "days", "enough", "sentences", "facts"}
    assert out["ok"] is True and out["days"] == 180
    assert out["enough"] is False and out["sentences"] == []
    f = out["facts"]
    assert f["acceptance"][0]["module"] == "labor" and f["acceptance"][0]["enough"] is False
    assert f["changes"] == []                       # one result on a number is not an average
    assert f["measured"]["measured_days"] == 1       # below MIN_MEASURED_DAYS: kept as a fact, not said
    assert f["minimums"] == {"settled_for_rate": rec_learning.MIN_SETTLED_FOR_RATE,
                             "measured_for_tag": rec_learning.MIN_MEASURED_FOR_RATE,
                             "results_per_metric": owner_report.MIN_RESULTS_PER_METRIC,
                             "measured_days": owner_report.MIN_MEASURED_DAYS}


def test_an_empty_restaurant_has_no_sentence_and_no_measured_total(db):
    rid = _rid(db)
    out = owner_report.what_worked(rid, days=90)
    assert out["enough"] is False and out["sentences"] == [] and out["days"] == 90
    assert out["facts"]["measured"]["total"] is None                # nothing measured is not $0
    assert out["facts"]["acceptance"] == [] and out["facts"]["most_effective"] is None
    with pytest.raises(ValueError):
        owner_report.what_worked(rid, days=30)


# ── enough: every clause, every figure from its source ──────────────────────

def _full_world(db, rid):
    # Labor: 12 settled — 9 taken, 1 not for us, 2 left unanswered.
    for i in range(9):
        _taken(db, rid, f"trim_day:Tuesday lunch {i}")
    rl.present(rid, "trim_day:Friday", "labor", "home", db_path=db)
    rl.record(rid, "trim_day:Friday", "dismissed", meta={"kind": "not_for_us", "reason_code": "too_costly"},
              db_path=db)
    _ignored(db, rid, "trim_day:Monday", "labor")
    _ignored(db, rid, "trim_day:Thursday", "labor")
    # Overtime: three non-overlapping results and one reading of the same
    # weeks as the first (one change, counted once).
    a = _result(db, rid, "cut_overtime:Saturday", "overtime_hours", "improved", 20.0, 16.0, 150)
    _result(db, rid, "cut_overtime:Sunday", "overtime_hours", "improved", 10.0, 9.0, 100)
    _result(db, rid, "cut_overtime:Friday", "overtime_hours", "worsened", 12.0, 14.4, 40)
    _result(db, rid, "cut_overtime:Saturday again", "overtime_hours", "improved", 20.0, 10.0, 145)
    # Measured days inside the window, and one outside it.
    for n in range(20):
        _day(db, rid, a, date.today() - timedelta(days=30 + n), 25.0)
    _day(db, rid, a, date.today() - timedelta(days=300), 999.0)
    return a


def test_the_full_paragraph_matches_its_sources(db):
    rid = _rid(db)
    _full_world(db, rid)
    out = owner_report.what_worked(rid, days=180)
    assert out["enough"] is True
    s = out["sentences"]
    summary = rec_learning.summary(rid, days=180)
    lab = summary["by_module"]["labor"]
    acc = next(r for r in out["facts"]["acceptance"] if r["module"] == "labor")
    assert (acc["n"], acc["taken"], acc["ignored"]) == (lab["n"], lab["accepted"] + lab["completed"] + lab["implemented"],
                                                        lab["ignored"])
    assert acc["accept_rate"] == lab["accept_rate"] and acc["enough"] is True
    pct = round(lab["accept_rate"] * 100)
    assert s[0].startswith("Over the past 6 months, you accepted ")
    assert f"{pct}% of Cavnar's labor recommendations ({acc['taken']} of {acc['n']})" in s[0]
    assert "left unanswered" in s[0]
    # Overtime: the overlapping reading is dropped; three results, mean of the stored delta_pct.
    ot = next(c for c in out["facts"]["changes"] if c["metric"] == "overtime_hours")
    assert ot["results"] == 3 and (ot["improved"], ot["worsened"]) == (2, 1)
    assert ot["mean_delta_pct"] == round((-20.0 + -10.0 + 20.0) / 3, 1)
    line = next(x for x in s if "overtime" in x)
    # Two improved and one got worse: the results disagree, so no direction
    # is quoted from their average (re-audit A32).
    assert ot["consistent_direction"] is False
    assert "3 measured results that didn't overlap" in line and "no consistent direction" in line
    assert "reduction" not in line and "increase" not in line
    # Measured dollars: the window's days only, net.
    cum = outcomes.cumulative(rid, since=out["facts"]["since"])
    assert out["facts"]["measured"]["total"] == cum["total"] == 500.0
    money = next(x for x in s if "measured days" in x)
    assert "$500" in money and "20 measured days" in money and "$999" not in " ".join(s)
    for x in s:
        assert not CAUSAL.search(x), x


def test_the_most_effective_subject_is_named_from_the_summary(db):
    rid = _rid(db)
    for i, v in enumerate(["improved"] * 5 + ["worsened"]):
        day = ("Saturday", "Sunday")[i % 2]
        _result(db, rid, f"schedule_coverage:{day} dinner gap {i}", "labor_pct", v, 30.0, 29.0 if v == "improved"
                else 31.0, 170 - i * 29, module="labor")
    for i, v in enumerate(["worsened", "worsened", "no_clear_change", "improved", "worsened"]):
        _result(db, rid, f"trim_day:{('Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Monday')[i]}#{i}", "food_cost_pct",
                v, 30.0, 30.5, 170 - i * 29, module="labor")
    out = owner_report.what_worked(rid, days=180)
    best = out["facts"]["most_effective"]
    assert best == rec_learning.summary(rid, days=180)["most_effective"]
    # What the count says, not an effect a before-and-after cannot show
    # (re-audit B13).
    line = next(x for x in out["sentences"] if "most often been followed by an improvement" in x)
    assert "most consistently effective" not in " ".join(out["sentences"])
    assert "weekend staffing" in line
    row = next(t for t in rec_learning.summary(rid, days=180)["by_tag"] if t["tag"] == best["tag"])
    assert f"{row['improved']} of {row['measured']} measured results improved" in line
    # A percentage metric is said in points, never "% of %".
    lab = next(c for c in out["facts"]["changes"] if c["metric"] == "labor_pct")
    pts = next(x for x in out["sentences"] if "labor %" in x)
    assert f"{abs(lab['mean_delta']):.1f}-point" in pts


def test_a_disowned_result_and_the_owners_own_idea_are_not_what_worked(db):
    rid = _rid(db)
    t1 = _result(db, rid, "cut_overtime:A", "overtime_hours", "improved", 20.0, 10.0, 120)
    _result(db, rid, "cut_overtime:B", "overtime_hours", "improved", 20.0, 10.0, 60)
    _x(db, "UPDATE recommendation_outcomes SET owner_checkin='{\"did_it\": \"no\"}' WHERE id=?", (t1,))
    _result(db, rid, "manual:my idea", "overtime_hours", "improved", 20.0, 10.0, 20, link=False, source="manual")
    out = owner_report.what_worked(rid, days=180)
    assert out["facts"]["changes"] == [] and out["enough"] is False


def test_worse_than_better_is_said_as_measured(db):
    rid = _rid(db)
    tid = _result(db, rid, "cut_overtime:A", "overtime_hours", "worsened", 10.0, 14.0, 60)
    for n in range(15):
        _day(db, rid, tid, date.today() - timedelta(days=10 + n), -40.0)
    out = owner_report.what_worked(rid, days=90)
    line = next(x for x in out["sentences"] if "measured days" in x)
    # It is dollars that went the wrong way, and it says dollars (re-audit A33).
    assert "more dollars were lost than gained" in line and "$600 less over 15 measured days" in line


# ── redaction: a manager sees what /recs/summary shows them ────────────────

def test_a_manager_never_reads_food_cost_or_loss_in_what_worked(db):
    rid = _rid(db)
    for i in range(10):
        _taken(db, rid, f"cut_waste:Item {i}", module="food")
    f1 = _result(db, rid, "cut_waste:Salmon", "weekly_waste", "improved", 500.0, 400.0, 120, module="inventory")
    _result(db, rid, "cut_waste:Tuna", "weekly_waste", "improved", 500.0, 450.0, 60, module="inventory")
    _result(db, rid, "loss:2026-W30:comps:spike", "comp_rate", "improved", 3.0, 2.0, 120, module="labor")
    _result(db, rid, "loss:2026-W36:comps:spike", "comp_rate", "improved", 3.0, 2.5, 60, module="labor")
    for n in range(20):
        _day(db, rid, f1, date.today() - timedelta(days=10 + n), 10.0, module="inventory", metric="weekly_waste")
    owner = owner_report.what_worked(rid, days=180, viewer=None)
    assert {c["metric"] for c in owner["facts"]["changes"]} == {"weekly_waste", "comp_rate"}
    assert any("food cost" in x for x in owner["sentences"])
    manager = {"id": 9, "restaurant_id": rid, "role": "manager", "is_admin": 0}
    m = owner_report.what_worked(rid, days=180, viewer=manager)
    assert m["facts"]["changes"] == [] and m["facts"]["acceptance"] == []
    assert m["facts"]["measured"]["total"] is None
    assert not any("food cost" in x or "waste" in x or "comp" in x.lower() for x in m["sentences"])


# ── the route, both twins ───────────────────────────────────────────────────

def test_the_route_serves_the_contract_on_both_twins(client, db, monkeypatch):
    rid = _rid(db)
    _full_world(db, rid)
    _as(monkeypatch, rid)
    web = client.get("/api/recs/what-worked?days=180").get_json()
    assert set(web) == {"ok", "days", "enough", "sentences", "facts"} and web["enough"] is True
    assert web == owner_report.what_worked(rid, days=180, viewer=auth.get_current_user())
    phone = client.get("/mobile/api/recs/what-worked?days=90", headers=PHONE).get_json()
    assert phone["ok"] is True and phone["days"] == 90
    assert client.get("/api/recs/what-worked").get_json()["days"] == 180            # the default
    for bad in ("30", "365", "x"):
        assert client.get(f"/api/recs/what-worked?days={bad}").status_code == 400


def test_the_route_is_redacted_for_a_manager_and_scoped_to_the_restaurant(client, db, monkeypatch):
    a, b = _rid(db, "Alpha Co"), _rid(db, "Bravo Co")
    _full_world(db, b)
    _as(monkeypatch, a)
    body = client.get("/api/recs/what-worked?days=180").get_json()
    assert body["enough"] is False and body["facts"]["changes"] == []
    for i in range(10):
        _taken(db, a, f"cut_waste:Item {i}", module="food")
    _as(monkeypatch, a, role="manager")
    body = client.get("/api/recs/what-worked?days=180").get_json()
    assert body["facts"]["acceptance"] == [] and body["sentences"] == []


def test_the_monthly_email_carries_what_worked_for_the_owner(db, monkeypatch):
    import emails
    rid = _rid(db)
    _full_world(db, rid)
    sections = emails._monthly_review_sections(rid)
    block = next((b for b in sections if "What worked for you" in b), None)
    assert block is not None
    for line in owner_report.email_lines(rid):
        assert line.replace("'", "&#x27;") in block or line in block
