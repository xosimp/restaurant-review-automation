"""Schedule fix round 10/10/26, workstream G1 — the Schedule Learning
Scorecard (RM-4, LEARNING-7), row origins (AI-29) and the learning
ablation: one frozen row per published week, measured on what happened."""
import json
from datetime import date, timedelta

import pytest

import models
import schedule_scorecard as ssc
import schedule_versions as sv
import shift_facts
import staff_settings
from models import Restaurant, create_restaurant


@pytest.fixture(autouse=True)
def _db(db_path, monkeypatch):
    real = models.get_conn
    fake = lambda *a, **k: real(db_path)  # noqa: E731
    monkeypatch.setattr(models, "get_conn", fake)
    monkeypatch.setattr(models, "DB_PATH", db_path)
    monkeypatch.setattr(staff_settings, "get_conn", fake)
    import client_api
    monkeypatch.setattr(client_api, "invalidate_insight_cache", lambda *a, **k: None)
    yield


HEAD = "date,day,employee,role,shift_start,shift_end,scheduled_hours,notes\n"


def _rid():
    rid = create_restaurant(Restaurant(name="Scorecard Co", owner_email="s@x.test", module_labor=1))
    for n, role in (("Ana", "Server"), ("Bob", "Server"), ("Cy", "Server"), ("Mia", "Manager")):
        models.add_manual_team_member(rid, n, role=role)
    return rid


def _row(d, name, start="5:00pm", end="10:00pm", role="Server", hours=5):
    return {"date": d.isoformat(), "day": d.strftime("%A"), "employee": name, "role": role, "shift_start": start,
            "shift_end": end, "scheduled_hours": str(hours), "notes": ""}


def _csv(rows):
    return HEAD + "".join(",".join(str(r[k]) for k in ("date", "day", "employee", "role", "shift_start", "shift_end",
                                                       "scheduled_hours", "notes")) + "\n" for r in rows)


def _week(rid, monday, generated, final, asked=None):
    econ = json.dumps({"requirements_by_date": asked}) if asked else None
    conn = models.get_conn()
    hid = conn.execute("INSERT INTO schedule_history (restaurant_id, week_start, week_end, schedule_csv, published_at, "
                       "generated_at, economics_json) VALUES (?,?,?,?,datetime('now','-1 day'),datetime('now','-2 days'),?)",
                       (rid, monday.isoformat(), (monday + timedelta(days=6)).isoformat(), _csv(final), econ)).lastrowid
    conn.commit()
    conn.close()
    sv.append(rid, hid, "generated", _csv(generated), saved_by="Cavnar AI", saved_authority="system")
    if final != generated:
        sv.append(rid, hid, "edited", _csv(final), saved_by="erik", saved_authority="principal")
    sv.append(rid, hid, "published", _csv(final), saved_by="erik", saved_authority="principal")
    return hid


def _monday(weeks_ago):
    t = date.today()
    return t - timedelta(days=t.weekday()) - timedelta(weeks=weeks_ago)


# ── the measures, pure ─────────────────────────────────────────────────────

def test_the_rework_weights_a_retime_by_how_far_it_moved():
    assert ssc.edit_weight("removed", {}) == 1.0 and ssc.edit_weight("moved", {}) == 1.0
    assert ssc.edit_weight("role_changed", {}) == 0.5
    assert ssc.edit_weight("retimed", {"old_start": "5:00pm", "new_start": "5:15pm", "old_end": "10:00pm",
                                       "new_end": "10:00pm"}) == 0.25
    assert ssc.edit_weight("retimed", {"old_start": "5:00pm", "new_start": "6:00pm", "old_end": "10:00pm",
                                       "new_end": "11:00pm"}) == 1.0


def test_distance_is_weighted_edits_per_100_rows_with_an_interval():
    d0 = date(2026, 10, 5)
    draft = [_row(d0, n) for n in ("Ana", "Bob", "Cy", "Dee")]
    final = [_row(d0, "Ana"), _row(d0, "Bob"), _row(d0, "Cy", start="6:00pm")]
    got = ssc.distance(draft, final)
    # One removal (1.0) and a one-hour retime (0.5) over 4 drafted rows.
    assert got["rate"] == 37.5 and got["lo"] < 37.5 < got["hi"] and got["n"] == 4


def test_row_origins_say_who_wrote_each_row_and_what_survived():
    d0 = date(2026, 10, 5)
    plan = dict(_row(d0, "Mia", role="Manager"), notes="Manager plan")
    import schedule_skeleton
    if not schedule_skeleton.is_plan_note(plan["notes"]):
        plan["notes"] = schedule_skeleton.PLAN_NOTE if hasattr(schedule_skeleton, "PLAN_NOTE") else plan["notes"]
    pre = [_row(d0, "Ana"), _row(d0, "Bob"), plan]
    final = [_row(d0, "Ana"), _row(d0, "Cy"), _row(d0, "Bob", start="6:00pm"), plan]
    info = {sv.row_sig(final[1]): {"added": "top_up", "changed": []},
            sv.row_sig(final[2]): {"added": None, "changed": ["role_times"]}}
    origins = ssc.classify_rows(pre, final, info)
    assert origins[sv.row_sig(final[0])] == "model"
    assert origins[sv.row_sig(final[1])] == "added:top_up"
    assert origins[sv.row_sig(final[2])] == "model_edited:role_times"
    if schedule_skeleton.is_plan_note(plan["notes"]):
        assert origins[sv.row_sig(plan)] == "manager_plan"
    published = [final[0], final[2]] + ([plan] if schedule_skeleton.is_plan_note(plan["notes"]) else [])
    surv = ssc.survival(origins, final, published)
    assert surv["model"] == {"rows": 1, "kept": 1, "share": 1.0}
    assert surv["added"] == {"rows": 1, "kept": 0, "share": 0.0}


def test_loop_info_names_the_stage_that_added_or_changed_a_row():
    d0 = date(2026, 10, 5)
    pre = [dict(_row(d0, "Ana"), _rid="r1"), dict(_row(d0, "Bob"), _rid="r2")]
    out_rows = [dict(_row(d0, "Ana"), _rid="r1"), dict(_row(d0, "Bob", start="6:00pm"), _rid="r2"),
                dict(_row(d0, "Cy"), _rid="r3")]
    info = ssc.loop_info(pre, out_rows, {"fixes": [], "trimmed": [], "staggered": []}, {"r3": "top_up"})
    assert info[sv.row_sig(out_rows[2])] == {"added": "top_up", "changed": []}
    assert info[sv.row_sig(out_rows[1])]["changed"] == ["search"]
    assert info[sv.row_sig(out_rows[0])] == {"added": None, "changed": []}


def test_the_ablation_runs_only_when_something_learned_binds_and_the_switch_is_on(monkeypatch):
    assert not ssc.ablation_wanted([])
    assert ssc.ablation_wanted([{"kind": "moved_off"}])
    monkeypatch.setenv(ssc.ABLATION_ENV, "0")
    assert not ssc.ablation_wanted([{"kind": "moved_off"}])


# ── one published week, scored and frozen ──────────────────────────────────

def test_a_week_is_scored_on_what_happened_and_frozen_after_two_weeks():
    rid = _rid()
    mon = _monday(3)
    days = [mon + timedelta(days=i) for i in range(7)]
    gen = [_row(d, n) for d in days[:2] for n in ("Ana", "Bob", "Cy")] + \
        [_row(d, "Mia", start="4:00pm", end="11:00pm", role="Manager", hours=7) for d in days[:2]]
    final = [r for r in gen if not (r["employee"] == "Cy" and r["date"] == days[0].isoformat())]
    asked = {f"{d.isoformat()}|night": {"Server": 3} for d in days[:2]}
    hid = _week(rid, mon, gen, final, asked=asked)
    # The punches: Bob no-showed on the Monday, nobody covered; the manager
    # is salaried? no — hourly here, so his punches count.
    punches = []
    for d in days[:2]:
        for n in ("Ana", "Cy") if d == days[1] else ("Ana",):
            punches.append({"date": d.isoformat(), "day": d.strftime("%A"), "employee": n, "role": "Server",
                            "shift_start": "5:00pm", "shift_end": "10:00pm", "hours": 5.0})
        punches.append({"date": d.isoformat(), "day": d.strftime("%A"), "employee": "Mia", "role": "Manager",
                        "shift_start": "4:00pm", "shift_end": "11:00pm", "hours": 7.0})
    shift_facts.ingest(rid, punches, "upload")
    conn = models.get_conn()
    conn.execute("INSERT INTO attendance_events (restaurant_id, employee_name, employee_key, business_date, "
                 "shift_start, outcome, source) VALUES (?,?,?,?,?,?,?)",
                 (rid, "Bob", "bob", days[0].isoformat(), "5:00pm", "no_show", "punches"))
    for d, sales, pct in ((days[0], 2000.0, 30.0), (days[1], 3000.0, 25.0)):
        conn.execute("INSERT INTO labor_daily_history (restaurant_id, date, labor_pct, sales) VALUES (?,?,?,?)",
                     (rid, d.isoformat(), pct, sales))
    conn.commit()
    conn.close()
    m = ssc.compute_week(rid, hid)
    assert m["looked_at"] and m["rows_drafted"] == len(gen)
    assert m["drr"]["rate"] == round(100.0 / len(gen), 2)          # one removal
    assert m["req_error_asked"] == 0.5                              # 2 of 3 then 3 of 3 servers
    assert m["uncovered_no_shows"] == 1
    assert m["labor_pct"] == 27.0                                   # (30*2000 + 25*3000) / 5000
    assert m["shortfall_hours"] is not None and m["shortfall_hours"] > 0
    assert m["ot_hours"] == 0.0
    assert "forecast_error" not in m, "no frozen projection: absent, never 0"
    assert ssc.store_week(rid, m) == "frozen"                      # ended 3 weeks ago
    # A frozen row is never written again.
    assert ssc.store_week(rid, dict(m, uncovered_no_shows=9)) == "kept"
    view = ssc.owner_view(rid)
    w = view["weeks"][0]
    assert w["uncovered_no_shows"] == 1 and w["week"] == f"{mon.month}/{mon.day}/{mon.strftime('%y')}"
    assert any("nobody covered" in line for line in w["lines"])


def test_a_week_nobody_looked_at_has_no_rework_rate():
    rid = _rid()
    mon = _monday(3)
    rows = [_row(mon, n) for n in ("Ana", "Bob")]
    conn = models.get_conn()
    hid = conn.execute("INSERT INTO schedule_history (restaurant_id, week_start, week_end, schedule_csv, published_at) "
                       "VALUES (?,?,?,?,datetime('now','-1 day'))",
                       (rid, mon.isoformat(), (mon + timedelta(days=6)).isoformat(), _csv(rows))).lastrowid
    conn.commit()
    conn.close()
    sv.append(rid, hid, "generated", _csv(rows), saved_by="Cavnar AI", saved_authority="system")
    sv.append(rid, hid, "published", _csv(rows), saved_by="Cavnar AI", saved_authority="system")
    m = ssc.compute_week(rid, hid)
    assert m["looked_at"] is False and m["drr"] is None


def test_the_ablation_effect_is_the_rework_the_learned_memory_saved():
    rid = _rid()
    mon = _monday(3)
    gen = [_row(mon, n) for n in ("Ana", "Cy")]
    final = [_row(mon, "Ana"), _row(mon, "Cy")]
    hid = _week(rid, mon, gen, final)
    off = [_row(mon, n) for n in ("Ana", "Bob")]          # with the memory off Bob was back on Monday
    ssc.tag_generation(rid, hid, arms=[{"experiment": "assign_v1", "arm": "solver"}],
                       origins={sv.row_sig(r): "model" for r in gen},
                       ablation={"ran": True, "learned": 1, "rows_on": ssc.compact(gen), "rows_off": ssc.compact(off)})
    m = ssc.compute_week(rid, hid)
    assert m["ablation"]["drr_on"]["rate"] == 0.0 and m["ablation"]["drr_off"]["rate"] == 50.0
    assert m["ablation"]["effect"] == 50.0
    assert m["arms"][0]["arm"] == "solver" and m["origin_survival"]["model"]["kept"] == 2


def test_the_job_returns_the_standard_counts_and_the_admin_view_reads_the_trend(monkeypatch):
    rid = _rid()
    for w in (5, 4, 3, 2):
        mon = _monday(w)
        gen = [_row(mon, n) for n in ("Ana", "Bob", "Cy", "Dee")]
        final = gen[: 4 - (5 - w)]                          # fewer edits each week
        _week(rid, mon, gen, final)
    import intelligence.jobs as ij
    monkeypatch.setattr(ij, "excluded_learning_ids", lambda db_path=None: set())
    out = ssc.run_scorecard()
    assert {"attempted", "ok", "failed", "skipped", "hit_bound"} <= set(out) and out["weeks"] == 4
    view = ssc.admin_view()
    assert view["ok"] and view["restaurants"] == 1 and view["weeks"] == 4
    assert view["trend"]["drr"]["slope"] is not None
    owner = ssc.owner_view(rid)
    assert owner["trend"] is not None and owner["trend_sentence"]


def test_one_place_says_why_the_draft_did_a_row():
    import schedule_memory
    rid = _rid()
    mon = _monday(1)
    gen = [_row(mon, "Ana"), _row(mon, "Bob")]
    hid = _week(rid, mon, gen, gen, asked={f"{mon.isoformat()}|night": {"Server": 2}})
    ssc.tag_generation(rid, hid, origins={sv.row_sig(gen[0]): "model", sv.row_sig(gen[1]): "added:top_up"})
    conn = models.get_conn()
    conn.execute("INSERT INTO schedule_memory (restaurant_id, memory_key, kind, fact_class, person, day, daypart, "
                 "status, enforcement, confidence, text) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                 (rid, "pattern:moved_on|bob|Monday|night", "moved_on", "habit", "Bob", mon.strftime("%A"), "night",
                  "active", "soft", 0.7, "The manager has put Bob on Monday dinner."))
    conn.commit()
    conn.close()
    why = schedule_memory.why_row(rid, hid, gen[1])
    assert why["origin"] == "added:top_up" and why["asked"] == {"night": 2}
    assert [m["kind"] for m in why["learned"]] == ["moved_on"] and why["learned"][0]["enforcement"] == "soft"


def test_the_owner_route_and_the_labor_intel_carry_the_scorecard():
    import strategy_routes
    rid = _rid()
    out, status = strategy_routes._do_learning_scorecard({"restaurant_id": rid, "is_admin": True, "role": "owner"})
    assert status == 200 and out["ok"] and out["weeks"] == []
