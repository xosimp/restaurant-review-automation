"""Schedule fix round 10/10/26, workstream Z2 (final integration fixer).

labor_live._record wrote a live cut's answer to the recommendation ledger
with no db_path, so an answer given against an explicit database (a script,
a test, anything not on the default file) landed in the default database -
or nowhere - while the move itself was decided in the right one."""
import labor_live
import models


def _moves(db_path, rid):
    conn = models.get_conn(db_path)
    try:
        cur = conn.execute(
            "INSERT INTO live_labor_moves (restaurant_id, business_date, kind, move_key, employee, role, "
            "start_label, end_label, hours, dollars, premium, text, payload_json) VALUES "
            "(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (rid, "2026-10-09", "cut", "cut:z2", "Ana", "Server", "6:30pm", "10:00pm", 3.5, 52.5, None,
             "Send Ana home at 6:30pm", '{"date": "2026-10-09", "employee": "Ana", "role": "Server", "kind": "cut"}'))
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def test_a_dismissed_live_cut_is_recorded_in_the_database_it_was_answered_in(db_path):
    rid = models.create_restaurant(models.Restaurant(name="Z2 Co", owner_email="z2@x.test"), db_path=db_path)
    mid = _moves(db_path, rid)
    out = labor_live.dismiss(rid, mid, user={"id": 3, "username": "gm"}, db_path=db_path)
    assert out == {"ok": True, "status": "dismissed"}
    conn = models.get_conn(db_path)
    try:
        ev = conn.execute("SELECT event, key FROM rec_events "
                          "WHERE restaurant_id=? AND key LIKE ?", (rid, "pulse_cut:2026-10-09:%")).fetchall()
    finally:
        conn.close()
    assert [r["event"] for r in ev] == ["dismissed"]


# ── measured effects: the floor, and a holiday's cohort prior ───────────────

def _ctx():
    import schedule_economics as econ
    ctx = econ._DemandContext.__new__(econ._DemandContext)
    ctx.rid, ctx.db, ctx.today, ctx._memo, ctx._weather_rows = 1, models.DB_PATH, None, {}, []
    return ctx


def _holiday(monkeypatch, own):
    import event_memory as em
    from intelligence import schedule_priors as sp
    monkeypatch.setattr(em, "flags_for", lambda *a, **k: {"2026-12-25": [{"kind": "holiday", "label": "christmas",
                                                                          "raw": "Christmas"}]})
    monkeypatch.setattr(em, "measured_effect", lambda *a, **k: own)
    monkeypatch.setattr(sp, "event_prior", lambda *a, **k: 18.0)


def test_a_holiday_keeps_the_cohort_prior_until_its_own_effect_applies(monkeypatch):
    # One Christmas measured here, below the floor: the forecast does not use
    # it, so the cohort's still stands in (it used to stop at the first one,
    # leaving the second and third Christmas planned as a typical night).
    _holiday(monkeypatch, {"n": 1, "applies": False, "median_lift_pct": 40})
    got = _ctx().holiday_prior("2026-12-25")
    assert got and got["pct"] == 18.0 and got["own_n"] == 1 and got["min_n"] == 3
    # Once its own effect applies, that is what the forecast carries.
    _holiday(monkeypatch, {"n": 3, "applies": True, "median_lift_pct": 40})
    assert _ctx().holiday_prior("2026-12-25") is None


def test_a_labels_role_lifts_need_its_effect_to_apply(monkeypatch):
    import demand_drivers as ddr
    import event_memory as em
    from datetime import date, timedelta
    nights = ["2026-09-04", "2026-09-11"]
    rows = []
    for d in nights:
        for k in range(1, 6):
            o = (date.fromisoformat(d) - timedelta(weeks=k)).isoformat()
            rows += [{"date": o, "employee": f"bar{i}", "role": "Bartender"} for i in range(2)]
        rows += [{"date": d, "employee": f"bar{i}", "role": "Bartender"} for i in range(4)]
    eff = {"dates": nights, "median_lift_pct": 22, "n": 2}
    monkeypatch.setattr(em, "measured_effect", lambda *a, **k: dict(eff, applies=True))
    assert ddr.role_event_lifts(1, "cubs", 22, rows=rows, family_prior={})["bartender"]["raw"] == 2.0
    # Two nights, below the floor: they move nobody.
    monkeypatch.setattr(em, "measured_effect", lambda *a, **k: dict(eff, applies=False))
    assert ddr.role_event_lifts(1, "cubs", 22, rows=rows, family_prior={}) == {}


def test_the_review_screens_labor_target_is_the_owners_goal_when_one_is_set(db_path, monkeypatch):
    # prepublish read the bare labor_target_pct, so an owner's active goal
    # ("labor 27% by 12/1") never reached the review screen's labor %.
    import owner_memory
    import prepublish
    monkeypatch.setattr(models, "DB_PATH", db_path)
    real = models.get_conn
    monkeypatch.setattr(models, "get_conn", lambda *a, **k: real(db_path))
    rid = models.create_restaurant(models.Restaurant(name="Goal Co", owner_email="g@x.test", labor_target_pct=30.0),
                                   db_path=db_path)
    monkeypatch.setattr(owner_memory, "target_for",
                        lambda r, metric: {"value": 27.0, "source": "goal", "goal_id": 1, "until": None,
                                           "label": "your goal of 27%"} if r == rid else None)
    assert prepublish._labor_target(rid) == 27.0
