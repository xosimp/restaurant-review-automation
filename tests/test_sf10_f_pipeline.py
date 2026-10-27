"""Workstream F (scheduler audit 10/10/26): the solver-first draft in the
pipeline, the repair loop's CP-SAT stage, and the re-solve in seconds.

  LOGIC-24 / RM-21 / ARCH-27  the "solver" draft source (contract #7) makes
                   no model call and drafts the week from shapes + CP-SAT
  LOGIC-28         the solver stage runs again when a later cycle changed the
                   week, never twice over the same week
  MARKET-13        "Maria is out, re-solve Friday": /labor/schedule/resolve
                   (web and phone) and Ask's propose_resolve, nothing saved
"""
import datetime as dt
import sys

import pytest
from flask import Flask

import auth
import client_api
import labor
import mobile_api
import models
import schedule_cpsat as cs
import schedule_engine as se
import schedule_rules as sr
import shift_quality as sq
from models import create_restaurant, Restaurant

pytestmark = pytest.mark.skipif(not cs.available(), reason="OR-Tools not installed")

WEEK = ["2026-10-14", "2026-10-15", "2026-10-16", "2026-10-17", "2026-10-18", "2026-10-19", "2026-10-20"]
MON_WEEK = ["2026-10-12", "2026-10-13", "2026-10-14", "2026-10-15", "2026-10-16", "2026-10-17", "2026-10-18"]
HEAD = "date,day,employee,role,shift_start,shift_end,scheduled_hours,notes\n"


def row(d, name, role="Server", start="5:00pm", end="10:00pm"):
    r = {"date": d, "day": sq._day_name(d), "employee": name, "role": role, "shift_start": start,
         "shift_end": end, "scheduled_hours": "", "notes": ""}
    r["scheduled_hours"] = sr.hours_text(sr.span_hours(r))
    return r


# ── the solver-first draft through the generation (contract #7) ───────────

def test_the_solver_source_drafts_the_week_with_no_model_call(monkeypatch):
    WEEK = MON_WEEK                                   # the generation's own Monday week
    names = ["Ana", "Ben", "Cy", "Dee", "Max"]
    c = sr.Constraints(restaurant_id=None, week_dates=list(WEEK), week_days=[sq._day_name(d) for d in WEEK],
                       roster_names=names, active={n.lower() for n in names})
    c.known_roles = {n.lower(): {"server"} for n in names[:4]}
    c.known_roles["max"] = {"manager"}
    c.managers = {"max": "Manager"}
    reqs = [{"date": d, "daypart": "night", "factor": 1.0, "roles": [{"role": "Server", "required": 2}]}
            for d in WEEK]
    concurrent = {(sq._day_name(d), "night"): {"Server": {"slots": {t: 2 for t in range(1020, 1320, 30)}}}
                  for d in WEEK}
    sent = []

    def gen(analysis, shifts, prepare_only=False, **kw):
        if not prepare_only:
            sent.append(1)
        return {"week_dates": WEEK, "week_days": [sq._day_name(d) for d in WEEK], "rows": [], "schedule_csv": HEAD,
                "narrative": [], "requirements": reqs, "requirements_by_date": {}, "concurrent": concurrent,
                "hours_budget": 0}
    monkeypatch.setattr(labor, "generate_optimized_schedule", gen)
    plan = [dict(row(d, "Max", "Manager", "4:00pm", "11:00pm"), _pinned=True) for d in WEEK]
    fallback = {"source": "solver", "restaurant_id": None, "constraints": c, "roster_names": names,
                "roster_roles": {**{n: "Server" for n in names[:4]}, "Max": "Manager"}, "week_dates": WEEK,
                "plan_rows": plan, "plan_dates": list(WEEK), "open_times": {}, "close_times": {}}
    out = se._generate_in_parts({}, [], [(n, "Server") for n in names[:4]] + [("Max", "Manager")],
                                {"week_start": WEEK[0], "restaurant_id": None}, fallback=fallback)
    assert not sent                                                      # no model call at all
    ds = out["draft_source"]
    assert ds["chosen"] == "solver" and ds["code_source"] == "solver" and sorted(ds["code_dates"]) == WEEK
    assert ds["solver"]["engine"] == "shapes+cp-sat" and ds["solver"]["reasons"]
    servers = [r for r in out["rows"] if r.get("role") == "Server"]
    assert len(servers) >= 14 and all(r["employee"] in names for r in servers)


def test_a_solver_draft_the_clock_cut_before_it_found_a_week_is_still_counted_as_cut(monkeypatch):
    """Stage 1 cut by the wall clock before it shaped anything: the v0 code
    draft stands in, and the draft's solver record still says the solve was
    cut — the review's line and ops.schedule_solve_capped count it
    (release-safety audit 10/26/26 A02-01: it vanished with the empty try)."""
    WEEK = MON_WEEK
    names = ["Ana", "Ben", "Cy", "Dee", "Max"]
    c = sr.Constraints(restaurant_id=None, week_dates=list(WEEK), week_days=[sq._day_name(d) for d in WEEK],
                       roster_names=names, active={n.lower() for n in names})
    c.known_roles = {n.lower(): {"server"} for n in names[:4]}
    c.known_roles["max"] = {"manager"}
    c.managers = {"max": "Manager"}
    reqs = [{"date": d, "daypart": "night", "factor": 1.0, "roles": [{"role": "Server", "required": 2}]}
            for d in WEEK]
    by_date = {f"{d}|night": {"Server": 2} for d in WEEK}
    monkeypatch.setattr(labor, "generate_optimized_schedule", lambda analysis, shifts, prepare_only=False, **kw: {
        "week_dates": WEEK, "week_days": [sq._day_name(d) for d in WEEK], "rows": [], "schedule_csv": HEAD,
        "narrative": [], "requirements": reqs, "requirements_by_date": by_date, "concurrent": {},
        "hours_budget": 0})
    monkeypatch.setattr(cs, "solver_draft", lambda *a, **k: {
        "rows": [], "unassigned": [], "short": [], "assumed": [], "conflicts": [], "reasons": [], "terms": {},
        "stats": {"engine": "shapes+cp-sat", "status": "nothing_to_solve", "wall_capped": True,
                  "memory_capped": False, "shapes": {"status": "unknown", "wall_capped": True}}})
    plan = [dict(row(d, "Max", "Manager", "4:00pm", "11:00pm"), _pinned=True) for d in WEEK]
    fallback = {"source": "solver", "restaurant_id": None, "constraints": c, "roster_names": names,
                "roster_roles": {**{n: "Server" for n in names[:4]}, "Max": "Manager"}, "week_dates": WEEK,
                "plan_rows": plan, "plan_dates": list(WEEK),
                "open_times": {sq._day_name(d): "4:00pm" for d in WEEK},
                "close_times": {sq._day_name(d): "10:00pm" for d in WEEK}}
    out = se._generate_in_parts({}, [], [(n, "Server") for n in names[:4]] + [("Max", "Manager")],
                                {"week_start": WEEK[0], "restaurant_id": None}, fallback=fallback)
    ds = out["draft_source"]
    assert [r for r in out["rows"] if r.get("role") == "Server"]             # the v0 code draft stood in
    assert ds["solver"]["wall_capped"] is True and ds["solver"]["stood_in"] == "v0 code draft"
    assert se.solve_capped({"draft_source": ds}) == ["wall:draft"]


# ── the repair loop runs the solver again only on a changed week ──────────

def test_the_solver_stage_runs_again_when_a_later_cycle_changed_the_week(monkeypatch):
    seen = []
    order = [k for k, *_r in se.REPAIR_STAGES]

    def stage(key):
        def fn(rows, x):
            seen.append(key)
            # the role-times pass (after the solver) changes the week once
            if key == "role_times" and seen.count("role_times") == 1:
                return {"rows": rows + [row(WEEK[1], "Bo")]}
            return {"rows": rows}
        return fn
    for k in order:
        monkeypatch.setitem(se._STAGE_FNS, k, stage(k))
    c = sr.Constraints(restaurant_id=1, week_dates=list(WEEK), week_days=[sq._day_name(d) for d in WEEK],
                       roster_names=["Ana", "Bo"], active={"ana", "bo"})
    x = se.RepairContext(None, {"week_dates": WEEK}, c, searches=False)
    out = se.repair_week([row(WEEK[0], "Ana")], x)
    assert out["converged"]
    assert seen.count("solver") == 2          # cycle 1, then again over the week role_times changed
    assert seen.count("top_up") == 1


# ── the re-solve in seconds (MARKET-13) ───────────────────────────────────

def _week_rows():
    rows = []
    for d in WEEK:
        rows += [row(d, "Ana"), row(d, "Ben", start="11:00am", end="4:00pm")]
    rows.append(row(WEEK[2], "Maria"))           # Friday dinner
    return rows


def test_maria_out_friday_moves_only_what_has_to_move():
    names = ["Ana", "Ben", "Maria", "Cy"]
    c = sr.Constraints(restaurant_id=1, week_dates=list(WEEK), week_days=[sq._day_name(d) for d in WEEK],
                       roster_names=names, active={n.lower() for n in names}, week_start_day=2)
    c.known_roles = {n.lower(): {"server"} for n in names}
    rows = _week_rows()
    rows[0] = dict(rows[0], locked=True)
    got = cs.resolve(rows, c, [WEEK[2]], out=[{"employee": "Maria"}],
                     signals={"roster": names, "roster_roles": {n: "Server" for n in names}}, max_seconds=3)
    assert got["ok"] and got["seconds"] < 10
    fri = [r for r in got["rows"] if r["date"] == WEEK[2]]
    assert "Maria" not in {r["employee"] for r in fri}
    assert len(got["changes"]) == 1 and got["changes"][0]["from"] == "Maria" and got["changes"][0]["to"] == "Cy"
    others = [r for r in got["rows"] if r["date"] != WEEK[2]]
    assert sorted((r["date"], r["employee"]) for r in others) == \
        sorted((r["date"], r["employee"]) for r in rows if r["date"] != WEEK[2])
    assert got["rows"][0].get("locked") is True


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
    monkeypatch.setattr(client_api, "log_account_event", lambda *a, **k: None)
    return db_path


def _restaurant(db_path, roster):
    rid = create_restaurant(Restaurant(name="Resolve Co", owner_email="r@x.com"), db_path=db_path)
    conn = models.get_conn(db_path)
    conn.execute("UPDATE restaurants SET module_labor=1 WHERE id=?", (rid,))
    conn.commit()
    conn.close()
    for name, role in roster:
        models.add_manual_team_member(rid, name, role=role, db_path=db_path)
    return rid


def test_the_resolve_route_answers_on_both_surfaces_and_saves_nothing(db):
    auth.init_auth(db_path=db)
    app = Flask(__name__, template_folder="../templates")
    app.register_blueprint(mobile_api.mobile_bp)
    app.register_blueprint(client_api.client_bp)
    client = app.test_client()
    rid = _restaurant(db, [("Ana", "Server"), ("Ben", "Server"), ("Maria", "Server"), ("Cy", "Server")])
    uid = auth.create_user(rid, "owner", "owner@x.com", "pw", db_path=db)
    headers = {"Authorization": f"Bearer {auth.create_session(uid, db_path=db)}"}
    body = {"rows": _week_rows(), "dates": [WEEK[2]], "out": [{"employee": "Maria"}]}
    r = client.post("/mobile/api/labor/schedule/resolve", headers=headers, json=body)
    assert r.status_code == 200, r.get_json()
    d = r.get_json()
    assert d["ok"] and [c["from"] for c in d["changes"]] == ["Maria"]
    assert "Maria" not in {x["employee"] for x in d["rows"] if x["date"] == WEEK[2]}
    conn = models.get_conn(db)
    try:
        assert conn.execute("SELECT COUNT(*) FROM schedule_history WHERE restaurant_id=?", (rid,)).fetchone()[0] == 0
    finally:
        conn.close()
    bad = client.post("/mobile/api/labor/schedule/resolve", headers=headers, json={"rows": []})
    assert bad.status_code == 400
    # the web route is the same body
    import inspect
    assert "mobile_schedule_resolve" in inspect.getsource(client_api.labor_schedule_resolve)


def test_ask_proposes_a_resolve_and_saves_nothing(db):
    import ask_cavnar_tools as act
    rid = _restaurant(db, [("Ana", "Server"), ("Ben", "Server"), ("Maria", "Server"), ("Cy", "Server")])
    text = HEAD + "\n".join(",".join(str(r[k]) for k in ("date", "day", "employee", "role", "shift_start",
                                                           "shift_end", "scheduled_hours", "notes"))
                            for r in _week_rows())
    hid = models.save_schedule_history(rid, WEEK[0], WEEK[-1], 0, 0, 30, text, [], db_path=db)
    got = act._propose_resolve(rid, dates=[WEEK[2]], out=["Maria"])
    assert got["schedule_id"] == hid and got["ok"] and not got["saved"]
    assert [c["from"] for c in got["changes"]] == ["Maria"]
    assert any(t["spec"]["name"] == "propose_resolve" and t["kind"] == "read" for t in act.TOOLS)
