"""Live A/B of schedule generation (schedule_experiments, audit #50).

The arm is a pure function of (experiment, restaurant, week), so a
regeneration keeps it; the arm is stored with the week; an arm never costs
a model call; the readout's maths are the stated ones; and below the minimum
sample it refuses to call a winner. Owners never see an arm. No model is
called: the generator is stubbed the way tests/test_edge_sched_generator.py
stubs it.
"""
import datetime as dt
import json
import math
import sys

import pytest

import activity, covers, decisions, delayed, demand_signals, goals, issues, metrics, outcomes  # noqa: E401,F401
import push, schedule_economics, schedule_intel, schedule_rules, schedule_versions  # noqa: E401,F401
import shift_requests, shift_quality, staff_schedule, staff_settings, strategy_jobs, time_off  # noqa: E401,F401
import labor_replacements  # noqa: F401

import models
import schedule_engine as se
import schedule_experiments as sx
from models import create_restaurant, Restaurant

WEEK = ["2026-10-05", "2026-10-06", "2026-10-07", "2026-10-08", "2026-10-09", "2026-10-10", "2026-10-11"]
HEADER = "date,day,employee,role,shift_start,shift_end,scheduled_hours,notes"
EXP = sx.EXPERIMENTS[0]


@pytest.fixture
def db(db_path, monkeypatch):
    real = models.get_conn

    def conn(*a, **k):
        return real(db_path)
    for mod in list(sys.modules.values()):
        bound = getattr(mod, "get_conn", None) if mod is not None else None
        if bound is real or str(getattr(bound, "__module__", "")).startswith(("test_", "tests.", "conftest")):
            monkeypatch.setattr(mod, "get_conn", conn)
    monkeypatch.setattr(models, "get_conn", conn)
    monkeypatch.setattr(models, "DB_PATH", db_path)
    monkeypatch.setattr(models, "_cached_shifts", lambda r: [])
    monkeypatch.delenv(sx.PIN_ENV, raising=False)
    return db_path


def _restaurant(db_path, name="Arm Grill"):
    return create_restaurant(Restaurant(name=name, owner_email=f"{name.split()[0].lower()}@x.com"), db_path=db_path)


# ── assignment ─────────────────────────────────────────────────────────────

def test_the_arm_is_a_pure_function_of_restaurant_and_week(db):
    rid = _restaurant(db)
    first = sx.arms_for(rid, "2026-10-05")
    assert first == sx.arms_for(rid, "2026-10-05")          # a regeneration keeps its arm
    assert [a["experiment"] for a in first] == [EXP["key"]] and not first[0]["pinned"]
    assert first[0]["arm"] == sx.hashed_arm(EXP, rid, "2026-10-05")
    weeks = [(dt.date(2026, 1, 5) + dt.timedelta(weeks=k)).isoformat() for k in range(80)]
    arms = [sx.hashed_arm(EXP, rid, w) for w in weeks]
    assert set(arms) == {"model", "solver"}
    assert 25 <= arms.count("solver") <= 55                 # the weights are 1:1


def test_a_new_experiment_is_one_registry_entry(monkeypatch):
    extra = {"key": "stagger_v1", "active": True, "control": "off", "question": "", "started": "2026-10-01",
             "arms": ({"key": "off", "label": "Off", "weight": 1, "flags": {"stagger": False}},
                      {"key": "on", "label": "On", "weight": 3, "flags": {"stagger": True}})}
    monkeypatch.setattr(sx, "EXPERIMENTS", sx.EXPERIMENTS + (extra,))
    monkeypatch.setattr(sx, "_restaurant_pins", lambda rid, db_path: {})
    arms = sx.arms_for(7, "2026-10-05")
    assert [a["experiment"] for a in arms] == [EXP["key"], "stagger_v1"]
    assert isinstance(sx.flag(arms, "stagger"), bool) and isinstance(sx.flag(arms, "solver"), bool)
    assert sx.flag([], "solver") is sx.DEFAULT_FLAGS["solver"]


def test_the_kill_switches_pin_every_restaurant_or_one(db, monkeypatch):
    rid = _restaurant(db)
    monkeypatch.setenv(sx.PIN_ENV, "off")
    [a] = sx.arms_for(rid, "2026-10-05")
    assert a["arm"] == EXP["control"] and a["pinned"] and a["pin_source"] == "env" and not sx.flag([a], "solver")
    monkeypatch.setenv(sx.PIN_ENV, f"{EXP['key']}:solver")
    assert sx.arms_for(rid, "2026-10-05")[0]["arm"] == "solver"
    monkeypatch.delenv(sx.PIN_ENV)
    assert sx.set_pin(rid, EXP["key"], "nonsense")["ok"] is False
    assert sx.set_pin(rid, EXP["key"], "off")["ok"]
    [a] = sx.arms_for(rid, "2026-10-05")
    assert a["arm"] == "model" and a["pin_source"] == "restaurant"
    sx.set_pin(rid, EXP["key"], None)
    assert not sx.arms_for(rid, "2026-10-05")[0]["pinned"]


# ── the job: arm recorded, no extra model call, owners never see it ────────

def _run_job(monkeypatch, rid, rows, calls, **extra):
    csv_text = HEADER + "\n" + "\n".join(",".join(str(x) for x in r) for r in rows)
    base = {"ok": True, "schedule_csv": csv_text, "week_dates": list(WEEK),
            "week_days": [dt.date.fromisoformat(d).strftime("%A") for d in WEEK], "summary": [],
            "hours_budget": 0, "daily_target_hours": {}, "labor_target": 30, "blended_rate": 20.0}
    base.update(extra)

    def build(r, week_start=None, **k):
        calls.append(week_start)
        return dict(base)
    monkeypatch.setattr(se, "_build_schedule_result", build)
    finished = {}
    monkeypatch.setattr(se._ops, "finish_async_job", lambda job_id, status, result: finished.update(status=status, result=result))
    se._run_schedule_job("arm-job", rid)
    return finished


def _week_rows():
    return [(WEEK[5], "Saturday", "Ann", "Server", "5:00pm", "10:00pm", 5, ""),
            (WEEK[5], "Saturday", "Bob", "Server", "5:00pm", "10:00pm", 5, ""),
            (WEEK[1], "Tuesday", "Cat", "Server", "5:00pm", "10:00pm", 5, ""),
            (WEEK[1], "Tuesday", "Dee", "Server", "5:00pm", "10:00pm", 5, "")]


@pytest.mark.parametrize("arm", ["model", "solver"])
def test_each_arm_is_recorded_and_costs_no_extra_model_call(db, monkeypatch, arm):
    import ai_utils
    rid = _restaurant(db)
    monkeypatch.setenv(sx.PIN_ENV, arm)
    monkeypatch.setattr(ai_utils, "get_client", lambda *a, **k: (_ for _ in ()).throw(AssertionError("model called")))
    calls = []
    out = _run_job(monkeypatch, rid, _week_rows(), calls,
                   roster=["Ann", "Bob", "Cat", "Dee"], roster_roles={n: "Server" for n in ("Ann", "Bob", "Cat", "Dee")},
                   operational_scores={"Ann": 2, "Bob": 2, "Cat": 5, "Dee": 4},
                   typical_headcount={("Saturday", "night"): {"Server": 2}, ("Tuesday", "night"): {"Server": 2}})
    assert out["status"] == "done", out
    assert len(calls) == 1                                   # one generation, whichever the arm
    res = out["result"]
    conn = models.get_conn(db)
    got = [dict(r) for r in conn.execute("SELECT * FROM schedule_experiment_weeks WHERE restaurant_id=?", (rid,)).fetchall()]
    conn.close()
    assert len(got) == 1 and got[0]["history_id"] == res["history_id"] and got[0]["arm"] == arm
    assert got[0]["experiment"] == EXP["key"] and got[0]["pinned"] == 1 and got[0]["week_start"] == WEEK[0]
    opt = res.get("optimizer") or {}
    if arm == "solver":
        assert (opt.get("solver") or {}).get("applied") and opt["changes"][0]["kind"] == "solve"
        assert got[0]["solver_applied"] == 1
    else:
        assert "solver" not in opt and got[0]["solver_applied"] is None
    # nothing an owner receives names the experiment or the arm
    blob = json.dumps(res, default=str)
    assert EXP["key"] not in blob and "experiment" not in blob


# ── the readout ────────────────────────────────────────────────────────────

def _csv(rows):
    return HEADER + "\n" + "\n".join(",".join(r) for r in rows)


def _published_week(db, rid, week_start, arm, unchanged, total=10, issues=0, labor=30.0, score=80):
    gen = [(week_start, "Monday", f"P{k}", "Server", "5:00pm", "10:00pm", "5", "") for k in range(total)]
    pub = [r if k < unchanged else (r[0], r[1], f"Q{k}", *r[3:]) for k, r in enumerate(gen)]
    hid = models.save_schedule_history(rid, week_start, week_start, 50, 50, 30, _csv(gen), [], db_path=db)
    conn = models.get_conn(db)
    conn.execute("UPDATE schedule_history SET published_at=datetime('now') WHERE id=?", (hid,))
    conn.execute("INSERT INTO schedule_versions (restaurant_id, history_id, version, reason, schedule_csv) VALUES (?,?,?,?,?)",
                 (rid, hid, 1, "generated", _csv(gen)))
    conn.execute("INSERT INTO schedule_versions (restaurant_id, history_id, version, reason, schedule_csv) VALUES (?,?,?,?,?)",
                 (rid, hid, 2, "published", _csv(pub)))
    conn.execute("INSERT INTO schedule_outcomes (restaurant_id, history_id, date, daypart, hours, issues, labor_pct) "
                 "VALUES (?,?,?,?,?,?,?)", (rid, hid, week_start, "night", 50, issues, labor))
    conn.commit()
    conn.close()
    sx.record(rid, hid, [{"experiment": EXP["key"], "arm": arm, "pinned": False}], score, db_path=db)
    return hid


def _world(db, restaurants, weeks, shares, issues=None):
    """`restaurants` restaurants, each with `weeks` published weeks per arm;
    shares[arm](r, w) gives the unchanged rows out of 10."""
    for r in range(restaurants):
        rid = _restaurant(db, f"R{r} Grill")
        k = 0
        for arm in ("model", "solver"):
            for w in range(weeks):
                ws = (dt.date(2026, 1, 5) + dt.timedelta(weeks=k)).isoformat()
                k += 1
                _published_week(db, rid, ws, arm, shares[arm](r, w),
                                issues=(issues or {}).get(arm, lambda r, w: 0)(r, w))


def _arm(read, key):
    return next(a for a in read["experiments"][0]["arms"] if a["arm"] == key)


def test_the_readout_means_and_intervals_are_the_stated_maths(db):
    _world(db, 2, 3, {"model": lambda r, w: 4 + w, "solver": lambda r, w: 8})
    read = sx.readout(db_path=db)
    m, s = _arm(read, "model"), _arm(read, "solver")
    vals = [0.4, 0.5, 0.6, 0.4, 0.5, 0.6]
    mean = sum(vals) / 6
    sd = math.sqrt(sum((v - mean) ** 2 for v in vals) / 5)
    assert m["acceptance"]["n"] == 6 and m["restaurants"] == 2 and m["generated"] == 6
    assert m["acceptance"]["mean"] == pytest.approx(mean, abs=1e-4)
    lo, hi = m["acceptance"]["ci90"]
    assert lo == pytest.approx(mean - 1.645 * sd / math.sqrt(6), abs=1e-4)
    assert hi == pytest.approx(mean + 1.645 * sd / math.sqrt(6), abs=1e-4)
    assert s["acceptance"]["mean"] == pytest.approx(0.8) and s["acceptance"]["sd"] == 0
    d = s["vs_control"]["acceptance"]
    assert d["diff"] == pytest.approx(0.8 - mean, abs=1e-4)
    se_ = math.sqrt(sd ** 2 / 6)
    assert d["ci90"][0] == pytest.approx(0.8 - mean - 1.645 * se_, abs=1e-4)
    assert m["quality"]["mean"] == 80 and m["issues"]["n"] == 6 and m["labor_pct"]["mean"] == pytest.approx(30.0)


def test_below_the_minimum_sample_no_winner_is_called(db):
    _world(db, 2, 3, {"model": lambda r, w: 2, "solver": lambda r, w: 10})
    read = sx.readout(db_path=db)
    v = read["experiments"][0]["verdict"]
    assert v["call"] is None and v["state"] == "insufficient" and v["text"].startswith("No call")
    assert str(sx.MIN_WEEKS_PER_ARM) in read["rule"] and str(sx.MIN_RESTAURANTS_PER_ARM) in read["rule"]


def test_enough_weeks_from_enough_restaurants_calls_the_leader(db):
    per = -(-sx.MIN_WEEKS_PER_ARM // sx.MIN_RESTAURANTS_PER_ARM)
    _world(db, sx.MIN_RESTAURANTS_PER_ARM, per, {"model": lambda r, w: 4 + (w + r) % 3, "solver": lambda r, w: 8 + (w + r) % 2})
    v = sx.readout(db_path=db)["experiments"][0]["verdict"]
    assert v["call"] == "solver" and v["state"] == "winner"


def test_a_leader_with_worse_outcomes_is_not_called(db):
    per = -(-sx.MIN_WEEKS_PER_ARM // sx.MIN_RESTAURANTS_PER_ARM)
    _world(db, sx.MIN_RESTAURANTS_PER_ARM, per, {"model": lambda r, w: 4 + (w + r) % 3, "solver": lambda r, w: 8 + (w + r) % 2},
           issues={"model": lambda r, w: (w + r) % 2, "solver": lambda r, w: 5 + (w + r) % 3})
    v = sx.readout(db_path=db)["experiments"][0]["verdict"]
    assert v["call"] is None and v["state"] == "conflicting" and "issues" in v["text"]


def test_pinned_weeks_stay_out_of_the_comparison(db):
    rid = _restaurant(db)
    hid = _published_week(db, rid, "2026-03-02", "solver", 10)
    sx.record(rid, hid, [{"experiment": EXP["key"], "arm": "solver", "pinned": True, "pin_source": "env"}], 90, db_path=db)
    s = _arm(sx.readout(db_path=db), "solver")
    assert s["generated"] == 0 and s["pinned"] == 1 and s["acceptance"]["n"] == 0


def test_the_admin_readout_and_pin_route(db, monkeypatch):
    import admin_ops
    import auth
    from flask import Flask
    import admin_routes
    rid = _restaurant(db)
    monkeypatch.setattr(auth, "get_current_user", lambda: {"id": 1, "is_admin": 1, "role": "admin", "username": "will"})
    app = Flask(__name__, template_folder="../templates")
    app.register_blueprint(admin_routes.admin_bp)
    with app.test_request_context(json={"restaurant_id": rid, "experiment": EXP["key"], "arm": "off"}):
        resp = admin_routes.admin_api_schedule_experiment_pin()
    body = resp.get_json() if hasattr(resp, "get_json") else resp[0].get_json()
    assert body["ok"] and body["arm"] == "off"
    read = admin_ops.schedule_experiments()
    assert read["ok"] and read["pins"][0]["restaurant_id"] == rid and read["pins"][0]["pinned_by"] == "will"
