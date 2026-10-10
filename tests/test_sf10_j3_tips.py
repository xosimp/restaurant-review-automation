"""Schedule fix round 10/10/26, workstream J3 — MARKET-17: the tip
opportunity of each shift slot, rotated like weekends and closes."""
from datetime import date, timedelta

import pytest

import models
import schedule_intel
import schedule_solver as ss
import schedule_rules as sr
import shift_quality as sq
import tip_opportunity as tip
from models import Restaurant, create_restaurant


@pytest.fixture(autouse=True)
def _db(db_path, monkeypatch):
    real = models.get_conn
    monkeypatch.setattr(models, "get_conn", lambda *a, **k: real(db_path))
    monkeypatch.setattr(models, "DB_PATH", db_path)
    yield


TODAY = date(2026, 10, 10)


def _punch(conn, rid, n, d, name, start_h, end_h, tips, role="Server"):
    cin = f"{d.isoformat()} {start_h:02d}:00:00"
    cout = f"{d.isoformat()} {end_h:02d}:00:00"
    conn.execute("INSERT INTO pos_punches (restaurant_id, provider, punch_id, business_date, employee_name, role, "
                 "clock_in, clock_out, tips, grats) VALUES (?,?,?,?,?,?,?,?,?,0)",
                 (rid, "rpower", f"p{n}", d.isoformat(), name, role, cin, cout, tips))


def _restaurant():
    rid = create_restaurant(Restaurant(name="Tips Co", owner_email="t@x.test", module_labor=1))
    for n in ("Ana", "Bob", "Cy"):
        models.add_manual_team_member(rid, n, role="Server")
    conn = models.get_conn()
    n = 0
    # Six weeks: Ana works every Friday dinner ($180), Bob and Cy split the
    # Tuesday lunches ($40) and Wednesday dinners ($60).
    for w in range(1, 7):
        fri = TODAY - timedelta(days=(TODAY.weekday() - 4) % 7) - timedelta(weeks=w)
        tue = fri - timedelta(days=3)
        wed = fri - timedelta(days=2)
        n += 1; _punch(conn, rid, n, fri, "Ana", 17, 22, 180)
        n += 1; _punch(conn, rid, n, tue, "Bob" if w % 2 else "Cy", 11, 15, 40)
        n += 1; _punch(conn, rid, n, wed, "Cy" if w % 2 else "Bob", 17, 22, 60)
        n += 1; _punch(conn, rid, n, tue, "Ana", 11, 15, 42)
    conn.commit()
    conn.close()
    return rid


def test_a_slot_is_worth_its_mean_tips_and_nothing_below_the_floor():
    rid = _restaurant()
    v = tip.slot_values(rid, today=TODAY)
    assert v["available"]
    srv = v["families"]["server"]
    assert srv["slots"]["Friday|night"]["mean"] == 180.0
    assert srv["slots"]["Friday|night"]["per_hour"] == 36.0
    assert srv["slots"]["Tuesday|morning"]["shifts"] == 12
    # Bob and Cy have 3 Wednesday dinners each, 6 together: stated.
    assert "Wednesday|night" in srv["slots"]


def test_no_punch_archive_says_so_instead_of_zero():
    rid = create_restaurant(Restaurant(name="No POS", owner_email="n@x.test", module_labor=1))
    v = tip.slot_values(rid, today=TODAY)
    assert v["available"] is False and "RPOWER" in v["reason"]
    assert tip.rotation(rid, today=TODAY) == {}


def test_the_opportunity_is_the_slot_mean_not_the_persons_own_tips_and_rotates():
    rid = _restaurant()
    book = tip.ledger(rid, today=TODAY)["server"]
    # Ana's Tuesday lunches ($42 each) count at the slot's mean, not her own.
    tue_mean = tip.slot_values(rid, today=TODAY)["families"]["server"]["slots"]["Tuesday|morning"]["mean"]
    assert book["Ana"]["opportunity"] == pytest.approx(6 * 180 + 6 * tue_mean, abs=0.05)
    assert book["Ana"]["rich"] == 6 and book["Ana"]["share"] > 1.2
    rot = tip.rotation(rid, today=TODAY)["server"]
    assert rot["rich_slots"][0]["day"] == "Friday" and rot["rich_slots"][0]["part"] == "night"
    assert rot["rest_from_rich"] == ["Ana"]
    assert rot["next_rich"][0] in ("Bob", "Cy") and "Ana" not in rot["next_rich"]
    assert "Ana has averaged $" in rot["lines"][0]


def test_the_rotation_plan_carries_the_tip_queue_to_the_prompt(monkeypatch):
    rid = _restaurant()
    # Two weeks of record for the rotation (published, else punches).
    weeks = []
    for w in (1, 2):
        mon = TODAY - timedelta(days=TODAY.weekday()) - timedelta(weeks=w)
        rs = [{"date": (mon + timedelta(days=i)).isoformat(), "employee": n, "role": "Server",
               "shift_start": "5:00pm", "shift_end": "10:00pm"} for i, n in ((4, "Ana"), (1, "Bob"), (2, "Cy"),
                                                                             (1, "Ana"), (2, "Bob"), (3, "Cy"))]
        weeks.append((mon.isoformat(), rs, "published"))
    monkeypatch.setattr(schedule_intel, "record_weeks", lambda *a, **k: weeks)
    plan = schedule_intel.rotation_plan(rid, today=TODAY, roster_roles={"Ana": "Server", "Bob": "Server", "Cy": "Server"})
    tips = plan["roles"]["Server"]["tips"]
    assert tips["rest_from_rich"] == ["Ana"] and tips["role_mean"] > 0
    block = schedule_intel.rotation_block(plan)
    assert "best-tipped shifts are Friday dinner" in block and "less to Ana" in block
    assert any("rest from the best-tipped shifts: Ana" in ln for ln in plan["lines"])


WEEK = ["2026-10-05", "2026-10-06", "2026-10-07", "2026-10-08", "2026-10-09", "2026-10-10", "2026-10-11"]


def _row(d, name, start="5:00pm", end="10:00pm"):
    return {"date": d, "day": sq._day_name(d), "employee": name, "role": "Server", "shift_start": start,
            "shift_end": end, "scheduled_hours": "5", "notes": ""}


def test_the_solver_moves_a_rich_slot_off_the_person_resting_from_them():
    names = ["Ann", "Bob", "Cat"]
    rows = [_row(WEEK[4], "Ann"), _row(WEEK[1], "Bob", "11:00am", "3:00pm"), _row(WEEK[2], "Cat", "11:00am", "3:00pm")]
    rotation = {"roles": {"Server": {"people": names, "weekend_due": [], "weekend_streak": {}, "rest_from_close": [],
                                     "next_close": [],
                                     "tips": {"rich_slots": [{"day": "Friday", "part": "night", "mean": 180}],
                                              "rest_from_rich": ["Ann"], "next_rich": ["Bob", "Cat"], "role_mean": 60,
                                              "per_shift": {"Ann": 120, "Bob": 45, "Cat": 45}}}}}
    c = sr.Constraints(restaurant_id=1, week_dates=list(WEEK), week_days=[sq._day_name(d) for d in WEEK],
                       roster_names=list(names), active={n.lower() for n in names})
    sig = {"roster": names, "roster_roles": {n: "Server" for n in names}, "rotation": rotation, "typical_headcount": {("Friday", "night"): {"Server": 1}, ("Tuesday", "morning"): {"Server": 1}, ("Wednesday", "morning"): {"Server": 1}}}
    out = ss.improve(rows, {}, signals=sig, constraints=c)
    assert out["applied"]
    assert [r["employee"] for r in out["rows"] if r["date"] == WEEK[4]] != ["Ann"]
    assert any("best-tipped" in ch["reason"] for ch in out["changes"] if ch["kind"] == "reassign")


def test_the_scorer_names_the_rich_slot_given_to_the_person_resting_from_them():
    ctx = sq.ShiftContext(date=WEEK[4], day="Friday", daypart="night", rows=[_row(WEEK[4], "Ann")],
                          week_assignments={"Ann": [{"date": WEEK[4]}], "Bob": [{"date": WEEK[1]}]},
                          rotation={"roles": {"Server": {"tips": {"rich_slots": [{"day": "Friday", "part": "night"}],
                                                                  "rest_from_rich": ["Ann"], "next_rich": ["Bob"],
                                                                  "role_mean": 60, "per_shift": {"Ann": 120}}}}})
    weak, good, facts = sq._rotation_findings(ctx, set())
    assert facts.get("tips_rest_working") == ["Ann"]
    assert "Ann has averaged $120 a shift in tip opportunity" in weak[0] and "Bob is next" in weak[0]
    assert sq._rotation_covers(ctx, set())
