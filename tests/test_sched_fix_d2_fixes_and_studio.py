"""Apply fixes and the Studio's re-score after the 10/3/26 schedule fix
round (workstream D2): a fix is kept only when the week, compared by breach
identity, is better and nothing else new or worse (E-1), its person is one
code may choose and the row is legal for them (P-2), overtime only when
nobody under their line can take it, never a pinned row, and a day-level
breach is the day's; the Studio's re-score as the manager drags keeps its
inputs, its live check, its sweep and its exact scorer per week being
edited and runs the what-if only when asked (P-25).
"""
import sys

import pytest
from flask import Flask

import client_api  # noqa: F401  (imported before the fixture redirects every bound get_conn)
import models
import schedule_rules as sr
import shift_quality as sq
from models import Restaurant, create_restaurant
from sched_d2_week import WEEK, DAYS, row

MON, TUE, WED, THU, FRI, SAT, SUN = WEEK


def cons(names, **kw):
    c = sr.Constraints(restaurant_id=1, week_dates=list(WEEK), week_days=list(DAYS), roster_names=list(names),
                       active={n.lower() for n in names})
    c.compliance = dict(sr.DEFAULTS)
    for k, v in kw.items():
        setattr(c, k, v)
    return c


def _sig(names, role="Server", **kw):
    out = {"roster": list(names), "roster_roles": {n: role for n in names}}
    out.update(kw)
    return out


def _fix(rows, c, **sig):
    return sq.apply_fixes(rows, sr.violations(rows, c), rule_constraints=c, **sig)


# ── E-1: a fix is judged by breach identity, the manager's minutes included ──

def test_a_fix_that_leaves_the_floor_without_a_manager_longer_is_refused(monkeypatch):
    """Max manages Tuesday dinner from 4pm though his window opens at 5pm;
    Tuesday lunch already has no manager. Handing his shift to Kim clears
    the window breach and leaves dinner unmanaged too — compared by (row,
    kind), nothing was new: the day's no-manager breach was already pinned
    to a row. By its minutes it is worse, and refused even where the score
    would take it (here it is rigged to); Mo, who manages, takes it."""
    names = ["Max", "Mo", "Kim", "Ann"]
    c = cons(names, managers={"max": "Manager", "mo": "Manager"},
             time_windows={"max": {"Tuesday": (sr.parse_minutes("5:00pm"), sr.parse_minutes("11:00pm"))}})
    rows = [row(TUE, "Ann", "11:00am", "3:00pm"), row(TUE, "Max", "4:00pm", "10:00pm"),
            row(MON, "Mo", "4:00pm", "10:00pm"), row(MON, "Kim", "4:00pm", "10:00pm")]
    viols = sr.violations(rows, c)
    assert {"outside_window", "no_manager"} <= {v["kind"] for v in viols}
    monkeypatch.setattr(sq.LocalScorer, "score",
                        lambda self, rs, flagged=None, hard_breaches=None: 100.0 if rs[1]["employee"] == "Kim" else 50.0)
    out = sq.apply_fixes(rows, viols, rule_constraints=c, **_sig(names))
    assert out["rows"][1]["employee"] == "Mo"
    gaps = lambda rs: sum(e - s for s, e, _i in (sr.manager_gaps(rs, c).get(TUE) or []))  # noqa: E731
    assert gaps(out["rows"]) <= gaps(rows)


def test_a_fix_is_never_a_trade_of_one_breach_for_another():
    """Ann is on Tuesday on time off. Bob closed Monday at 1am: in her
    Tuesday 8am shift he would break the rest rule — the same row, another
    breach."""
    names = ["Ann", "Bob"]
    c = cons(names, blocked_dates={"ann": {TUE: "on approved time off"}})
    rows = [row(TUE, "Ann", "8:00am", "2:00pm"), row(MON, "Bob", "6:00pm", "1:00am")]
    out = _fix(rows, c, **_sig(names, typical_headcount={("Tuesday", "morning"): {"Server": 1}}))
    assert out["rows"][0]["employee"] == "Ann" and not out["fixes"]
    assert out["unfixed"] and "Nobody on the roster can legally take" in out["unfixed"][0]["reason"]


# ── P-2: whoever is chosen is choosable and legal ───────────────────────────

def test_a_fix_never_chooses_somebody_dormant_or_a_minor_past_their_band():
    names = ["Ann", "Old", "Teen", "Kim"]
    c = cons(names, blocked_dates={"ann": {TUE: "on approved time off"}}, dormant={"old": "2026-08-01"},
             minors={"teen"}, minor_bands={"teen": "14-15"})
    rows = [row(TUE, "Ann", "5:00pm", "11:00pm"), row(MON, "Old", "5:00pm", "11:00pm"),
            row(MON, "Teen", "3:00pm", "6:00pm"), row(MON, "Kim", "5:00pm", "11:00pm")]
    out = _fix(rows, c, **_sig(names, scores={"Old": 5, "Teen": 5, "Kim": 1},
                               typical_headcount={("Tuesday", "night"): {"Server": 1}}))
    assert out["rows"][0]["employee"] == "Kim"


def test_overtime_only_when_nobody_under_their_line_can_take_the_shift():
    """The weekly ceiling is 48h, the overtime line 40h. Bob is at 36h, Kim
    at 12h: Kim takes Ann's shift though Bob scores higher. With Kim gone,
    Bob takes it past his line — a person's legality outranks overtime —
    but never past his maximum."""
    def c_(names, **kw):
        c = cons(names, blocked_dates={"ann": {TUE: "on approved time off"}}, **kw)
        c.compliance = dict(c.compliance, weekly_hours_ceiling=48)
        return c
    base = [row(TUE, "Ann", "4:00pm", "10:00pm")] + \
        [row(d, "Bob", "10:00am", "7:00pm") for d in (MON, WED, THU, FRI)]
    head = {("Tuesday", "night"): {"Server": 1}}
    rows = base + [row(SAT, "Kim", "10:00am", "4:00pm"), row(SUN, "Kim", "10:00am", "4:00pm")]
    names = ["Ann", "Bob", "Kim"]
    assert _fix(rows, c_(names), **_sig(names, scores={"Bob": 5, "Kim": 1}, typical_headcount=head)
                )["rows"][0]["employee"] == "Kim"
    two = ["Ann", "Bob"]
    assert _fix(base, c_(two), **_sig(two, scores={"Bob": 5}, typical_headcount=head))["rows"][0]["employee"] == "Bob"
    held = _fix(base, c_(two, hours_limits={"bob": (None, 40.0)}), **_sig(two, scores={"Bob": 5}, typical_headcount=head))
    assert held["rows"][0]["employee"] == "Ann" and not held["fixes"]


# ── pinned rows, day-level breaches, roles not held ─────────────────────────

def test_a_pinned_row_is_named_never_handed_to_somebody_else():
    names = ["Max", "Kim"]
    c = cons(names, managers={"max": "Manager"}, blocked_dates={"max": {TUE: "on approved time off"}})
    rows = [dict(row(TUE, "Max", "4:00pm", "10:00pm"), _pinned="manager_plan"), row(MON, "Kim", "4:00pm", "10:00pm")]
    out = _fix(rows, c, **_sig(names))
    assert out["rows"][0]["employee"] == "Max" and not out["fixes"]
    assert "fixed" in out["unfixed"][0]["reason"] and "by hand" in out["unfixed"][0]["reason"]


def test_a_breach_about_the_day_is_never_an_unfixed_line_on_a_person():
    names = ["Ann", "Bob"]
    c = cons(names, managers={"boss": "Owner"}, role_floors={"Server": {"night": 3}})
    rows = [row(TUE, "Ann", "4:00pm", "10:00pm"), row(TUE, "Bob", "4:00pm", "10:00pm")]
    viols = sr.violations(rows, c)
    assert any(v.get("day_level") for v in viols)
    out = sq.apply_fixes(rows, viols, rule_constraints=c, **_sig(names))
    assert not out["unfixed"] and not out["fixes"]


def test_somebody_in_a_role_they_do_not_hold_is_replaced_by_somebody_who_holds_it():
    names = ["Ann", "Cook", "Kim"]
    c = cons(names, known_roles={"ann": {"server"}, "cook": {"line cook"}, "kim": {"line cook"}})
    rows = [row(TUE, "Ann", "4:00pm", "10:00pm", role="Line Cook"), row(MON, "Kim", "4:00pm", "10:00pm", role="Line Cook")]
    viols = sr.violations(rows, c)
    assert any(v["kind"] == "role_not_held" and v.get("hard") for v in viols)
    out = sq.apply_fixes(rows, viols, rule_constraints=c,
                         **_sig(names, role="Line Cook", roster_roles={"Ann": "Server", "Cook": "Line Cook",
                                                                        "Kim": "Line Cook"}))
    assert out["rows"][0]["employee"] in ("Cook", "Kim") and out["fixes"][0]["kind"] == "role_not_held"


# ── P-25: the Studio's re-score as the manager drags ─────────────────────────

@pytest.fixture(autouse=True)
def _db(db_path, monkeypatch):
    real = models.get_conn

    def conn(*a, **k):
        return real(db_path)
    for mod in list(sys.modules.values()):
        if mod is not None and getattr(mod, "get_conn", None) is real:
            monkeypatch.setattr(mod, "get_conn", conn)
    monkeypatch.setattr(models, "get_conn", conn)
    monkeypatch.setattr(models, "DB_PATH", db_path)
    import auth
    monkeypatch.setattr(auth, "DB_PATH", db_path, raising=False)
    auth.init_auth(db_path=db_path)
    yield


@pytest.fixture
def studio(monkeypatch):
    """A restaurant with live shifts, a logged-in owner and a counter of the
    heavy reads: every shift on file (the live check), the generation's
    inputs, and the what-if."""
    import labor
    import mobile_api
    import schedule_engine
    from auth import create_session, create_user, upsert_membership
    rid = create_restaurant(Restaurant(name="Studio Co", owner_email="s@x.test", module_labor=1))
    from models import set_capability, update_restaurant
    for name, score in (("Ann", 5), ("Bob", 2), ("Cat", 3), ("Dee", 4)):
        set_capability(rid, name, score=score)
    update_restaurant(rid, {"role_strength_json": '{"Server": 8}'})
    uid = create_user(rid, "erik", "erik@x.test", "pw-d2-test-1")
    upsert_membership(uid, rid, "client")
    calls = {"live": 0, "inputs": 0, "what_if": 0}

    def live(*a, **k):
        calls["live"] += 1
        return {"is_live": True}
    monkeypatch.setattr(labor, "analyse_shifts_for_restaurant", live)
    monkeypatch.setattr(labor, "load_shifts_for_restaurant", lambda *a, **k: [])
    real_inputs = schedule_engine.quality_inputs_from_db

    def inputs(*a, **k):
        calls["inputs"] += 1
        return real_inputs(*a, **k)
    monkeypatch.setattr(schedule_engine, "quality_inputs_from_db", inputs)
    real_cc = sq.compare_candidates

    def what_if(*a, **k):
        calls["what_if"] += 1
        return real_cc(*a, **k)
    monkeypatch.setattr(sq, "compare_candidates", what_if)
    app = Flask(__name__)
    app.register_blueprint(mobile_api.mobile_bp)
    client = app.test_client()
    headers = {"Authorization": "Bearer " + create_session(uid, device_type="ios")}

    def post(body):
        resp = client.post("/mobile/api/labor/schedule/score", json=body, headers=headers)
        assert resp.status_code == 200, resp.get_json()
        return resp.get_json()
    return {"rid": rid, "calls": calls, "post": post}


def _week_rows(names=("Ann", "Bob", "Cat", "Dee")):
    return [row(d, n, "4:00pm", "10:00pm") for d in (MON, TUE, WED) for n in names[:2]] + \
        [row(THU, n, "11:00am", "3:00pm") for n in names[2:]]


def test_a_drag_re_scores_on_kept_inputs_and_asks_for_the_what_if_only_on_demand(studio):
    rows = _week_rows()
    first = studio["post"]({"rows": rows, "history_id": 7})
    assert first["ok"] and first["quality"]["checked"]
    assert first["what_if"]["on_demand"] is True and first["what_if"]["ran"] is False
    dragged = [dict(r) for r in rows]
    dragged[0]["employee"], dragged[6]["employee"] = "Cat", "Ann"
    second = studio["post"]({"rows": dragged, "history_id": 7})
    assert second["ok"]
    # the inputs, the live check and no what-if: once for the week, not per drag
    assert studio["calls"] == {"live": 1, "inputs": 1, "what_if": 0}
    asked = studio["post"]({"rows": dragged, "history_id": 7, "what_if": True})
    assert studio["calls"]["what_if"] == 1 and asked["what_if"].get("ran") is True
    assert studio["calls"]["inputs"] == 1


def test_the_kept_re_score_is_the_whole_score_exactly(studio):
    """Whatever it reuses, a re-score's number, shifts and violations are
    those of scoring the edited week from scratch."""
    import schedule_engine
    rows = _week_rows()
    studio["post"]({"rows": rows, "history_id": 9})
    dragged = [dict(r) for r in rows]
    dragged[2]["shift_end"] = "11:30pm"
    dragged[2]["scheduled_hours"] = "7.5"
    kept = studio["post"]({"rows": dragged, "history_id": 9})
    schedule_engine.studio_invalidate()
    fresh = studio["post"]({"rows": dragged, "history_id": 9})
    for k in ("score", "raw_score", "checked", "band"):
        assert kept["quality"].get(k) == fresh["quality"].get(k), k
    assert [s["score"] for s in kept["quality"]["shifts"]] == [s["score"] for s in fresh["quality"]["shifts"]]
    assert kept["violations"] == fresh["violations"]


def test_a_save_rebuilds_the_kept_inputs_from_the_database(studio):
    from models import save_schedule_history
    rows = _week_rows()
    csv = "date,day,employee,role,shift_start,shift_end,scheduled_hours,notes\n" + "".join(
        f"{r['date']},{r['day']},{r['employee']},{r['role']},{r['shift_start']},{r['shift_end']},{r['scheduled_hours']},\n"
        for r in rows)
    hid = save_schedule_history(studio["rid"], MON, SUN, 36, 60, 30, csv, [])
    studio["post"]({"rows": rows, "history_id": hid})
    saved = studio["post"]({"rows": rows, "history_id": hid, "save": True})
    assert saved["saved"] is True
    assert studio["calls"]["inputs"] == 2
