"""Schedule fix round 10/10/26, workstream J3 — MARKET-29: retention-aware
scheduling. Quits labelled from the roster, risk from schedule-side
signals (a fitted model only when it predicts out of sample, stated rules
otherwise), a soft protect term in the solver, owner-only."""
import datetime as dt

import pytest

import models
import retention_risk as rr
import schedule_rules as sr
import schedule_solver as ss
import shift_quality as sq
from models import Restaurant, create_restaurant


@pytest.fixture(autouse=True)
def _db(db_path, monkeypatch):
    real = models.get_conn
    monkeypatch.setattr(models, "get_conn", lambda *a, **k: real(db_path))
    monkeypatch.setattr(models, "DB_PATH", db_path)
    rr._MODEL_CACHE.clear()
    yield


TODAY = dt.date(2026, 10, 12)


def _weeks(name, start, end, hours_fn):
    out = []
    d = start
    while d < end:
        out.append({"date": d.isoformat(), "employee": name, "role": "Server", "shift_start": "5:00pm",
                    "shift_end": "10:00pm", "scheduled_hours": hours_fn(d)})
        d += dt.timedelta(weeks=1)
    return out


def _data(n_quit=14, n_stay=26):
    """People who left had their hours halved in the six weeks before —
    from well before any notice (the model reads only the weeks NOTICE_DAYS
    and more before a departure: blind re-audit 10/11/26 LEARNING-25); the
    rest held steady. One current regular is being cut now."""
    start = TODAY - dt.timedelta(weeks=70)
    data = {"shifts": [], "drops": {}, "attendance": {}, "left": {}, "broken": {}, "avoids": {}, "stated": {}}
    for i in range(n_quit):
        name = f"Quit{i}"
        left = TODAY - dt.timedelta(weeks=8 + i * 3)
        data["left"][name.casefold()] = [left.isoformat()]
        data["shifts"] += _weeks(name, start, left, lambda d, left=left: 12 if (left - d).days <= 42 else 30)
    for i in range(n_stay):
        name = f"Stay{i}"
        data["shifts"] += _weeks(name, start, TODAY, lambda d, i=i: 28 + (i % 4))
    data["shifts"] += _weeks("Cut", start, TODAY, lambda d: 12 if (TODAY - d).days <= 28 else 30)
    by = {}
    for r in data["shifts"]:
        by.setdefault(r["employee"].casefold(), []).append(r)
    data["by_person"] = by
    return data


def test_the_signals_read_an_hours_cut_against_the_persons_own_usual():
    data = _data()
    s = rr.signals(data, "cut", TODAY)
    assert s["hours_usual"] == 30 and s["hours_recent"] == 12 and s["hours_cut"] == pytest.approx(0.6)
    assert rr.signals(data, "stay0", TODAY)["hours_cut"] == 0.0


def test_a_model_is_used_only_when_it_predicts_departures_out_of_sample():
    data = _data()
    m = rr.model(1, today=TODAY, data=data)
    assert m["available"] and m["auc"] >= rr.MIN_AUC and m["quits"] == 14 and m["auc_lo"] > 0.5
    cut = rr._prob(m["model"], [float(rr.signals(data, "cut", TODAY)[f]) for f in rr.FEATURES])
    steady = rr._prob(m["model"], [float(rr.signals(data, "stay0", TODAY)[f]) for f in rr.FEATURES])
    assert cut > steady and cut >= rr.HIGH_PROB
    few = rr.model(2, today=TODAY, data=_data(n_quit=2))
    assert few["available"] is False and "warning signs are shown by stated rules" in few["reason"]


def test_assess_protects_a_valued_person_at_risk_and_the_owner_can_switch_it_off(monkeypatch):
    rid = create_restaurant(Restaurant(name="Keep Co", owner_email="k@x.test", module_labor=1))
    data = _data(n_quit=2, n_stay=4)               # too few departures: rules, not a model
    data["drops"]["cut"] = [(TODAY - dt.timedelta(days=d)).isoformat() for d in (3, 10, 17)]
    monkeypatch.setattr(rr, "_load", lambda *a, **k: data)
    import staff_settings
    monkeypatch.setattr(staff_settings, "roster", lambda *a, **k: [{"name": "Cut", "active": True},
                                                                   {"name": "Stay0", "active": True}])
    got = rr.assess(rid, today=TODAY)
    assert "stated rules" in got["basis"] and "Only you see this" in got["note"]
    cut = next(p for p in got["people"] if p["name"] == "Cut")
    assert cut["level"] == "high" and cut["risk"] is None and cut["valued"] and cut["protect"]
    assert any("hours down from about 30 to 12" in r for r in cut["reasons"])
    assert any("3 shifts given up" in r for r in cut["reasons"])
    stay = next(p for p in got["people"] if p["name"] == "Stay0")
    assert stay["level"] == "none" and not stay["protect"]
    assert rr.protect_signal(rid, today=TODAY) == {"Cut": {"protect": True, "avoid": []}}
    rr.set_protect(rid, "Cut", False, by="erik")
    assert rr.protect_signal(rid, today=TODAY) == {}


WEEK = ["2026-10-12", "2026-10-13", "2026-10-14", "2026-10-15", "2026-10-16", "2026-10-17", "2026-10-18"]


def _row(d, name, start="5:00pm", end="10:00pm"):
    return {"date": d, "day": sq._day_name(d), "employee": name, "role": "Server", "shift_start": start,
            "shift_end": end, "scheduled_hours": "5", "notes": ""}


def test_the_solver_keeps_a_protected_persons_shift_against_a_soft_pull():
    names = ["Ann", "Bob", "Cat"]
    rows = [_row(WEEK[4], "Ann"), _row(WEEK[1], "Bob", "11:00am", "3:00pm"), _row(WEEK[2], "Cat", "11:00am", "3:00pm")]
    rotation = {"roles": {"Server": {"people": names, "weekend_due": [], "weekend_streak": {}, "rest_from_close": [],
                                     "next_close": [],
                                     "tips": {"rich_slots": [{"day": "Friday", "part": "night", "mean": 180}],
                                              "rest_from_rich": ["Ann"], "next_rich": ["Bob", "Cat"], "role_mean": 60,
                                              "per_shift": {"Ann": 120}}}}}
    c = sr.Constraints(restaurant_id=1, week_dates=list(WEEK), week_days=[sq._day_name(d) for d in WEEK],
                       roster_names=list(names), active={n.lower() for n in names})
    sig = {"roster": names, "roster_roles": {n: "Server" for n in names}, "rotation": rotation,
           "typical_headcount": {("Friday", "night"): {"Server": 1}, ("Tuesday", "morning"): {"Server": 1},
                                 ("Wednesday", "morning"): {"Server": 1}}}
    moved = ss.improve(rows, {}, signals=sig, constraints=c)
    assert [r["employee"] for r in moved["rows"] if r["date"] == WEEK[4]] != ["Ann"]
    kept = ss.improve(rows, {}, signals=dict(sig, retention={"Ann": {"protect": True, "avoid": []}}), constraints=c)
    assert [r["employee"] for r in kept["rows"] if r["date"] == WEEK[4]] == ["Ann"]


def test_the_weeks_a_notice_may_already_shape_are_never_examples():
    # Hours cut only in the two weeks before leaving (the notice period):
    # nothing before the notice predicts it, so no model is kept.
    start = TODAY - dt.timedelta(weeks=70)
    data = {"shifts": [], "drops": {}, "attendance": {}, "left": {}, "broken": {}, "avoids": {}, "stated": {}}
    for i in range(14):
        name = f"Quit{i}"
        left = TODAY - dt.timedelta(weeks=8 + i * 3)
        data["left"][name.casefold()] = [left.isoformat()]
        data["shifts"] += _weeks(name, start, left, lambda d, left=left: 12 if (left - d).days <= 13 else 30)
    for i in range(26):
        data["shifts"] += _weeks(f"Stay{i}", start, TODAY, lambda d, i=i: 28 + (i % 4))
    by = {}
    for r in data["shifts"]:
        by.setdefault(r["employee"].casefold(), []).append(r)
    data["by_person"] = by
    ex = rr._examples(data, TODAY)
    for key, mon, _x, _y in ex:
        for d in data["left"].get(key) or []:
            assert not (mon.isoformat() <= d < (mon + dt.timedelta(days=rr.NOTICE_DAYS)).isoformat())
    assert rr.model(3, today=TODAY, data=data)["available"] is False


def test_noise_is_not_kept_as_a_model():
    import random as _r
    rng = _r.Random(5)
    kept = 0
    for _ in range(25):
        ex = []
        leavers = set(rng.sample(range(40), rr.MIN_QUITS))
        for p in range(40):
            base = [rng.random() for _ in rr.FEATURES]
            lw = rng.randint(8, 47) if p in leavers else None
            for w in range(48):
                if lw is not None and w > lw:
                    break
                ex.append((f"p{p}", w, [max(0.0, b + rng.gauss(0, 0.2)) for b in base],
                           1 if (lw is not None and lw - 4 < w <= lw) else 0))
        kept += rr.gate(ex)["ok"]
    assert kept <= 3, kept          # about 4% expected; 6 quits / 0.65 alone kept 7%
