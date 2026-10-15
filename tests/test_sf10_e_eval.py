"""Scheduler audit 10/10/26, workstream E — the generation's metrics and the
frozen golden eval set (AI-2 cost per accepted week, AI-23 role violations,
stage attribution; RM-1 / AI-5 / AI-6 / AI-7 / AI-8 / AI-22 / AI-30: a
retention-exempt set scored through the production post-model path against
the published week, >=3 runs per arm, paired means with 90% intervals, code
arms with no model). No model is called here."""
import json
import os
import sys

import pytest

import models
import ops
import schedule_engine as se
import schedule_eval as sev
import schedule_metrics as sm
import schedule_output as so
import schedule_rules as sr
from models import Restaurant, create_restaurant

sys.path.append(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))
import schedule_model_eval as sme  # noqa: E402

WEEK = ["2026-10-12", "2026-10-13", "2026-10-14", "2026-10-15", "2026-10-16", "2026-10-17", "2026-10-18"]
DAYS = list(sr.DAYS)
HEAD = "date,day,employee,role,shift_start,shift_end,scheduled_hours,notes"


def _row(d, who, role="Server", start="4:00pm", end="10:00pm", notes=""):
    return {"date": d, "day": DAYS[WEEK.index(d)], "employee": who, "role": role, "shift_start": start,
            "shift_end": end, "scheduled_hours": "6", "notes": notes}


def _csv(rows):
    return HEAD + "\n" + "\n".join(",".join(str(r[k]) for k in HEAD.split(",")) for r in rows)


def _c(names=("Ann", "Bob", "Cat")):
    c = sr.Constraints(restaurant_id=1, week_dates=list(WEEK), week_days=DAYS, roster_names=list(names),
                       active={n.lower() for n in names})
    c.compliance = dict(sr.DEFAULTS)
    c.known_roles = {n.lower(): {"Server"} for n in names}
    return c


# ── metrics ────────────────────────────────────────────────────────────────

def test_model_rows_leave_out_the_plan_kept_days_and_code_days():
    rows = [_row(WEEK[0], "Ann"), _row(WEEK[1], "Bob", notes="Cavnar AI: manager plan — x"),
            _row(WEEK[2], "Cat"), _row(WEEK[3], "Ann")]
    got = sm.model_rows_of(rows, kept_dates={WEEK[2]}, code_dates={WEEK[3]})
    assert [r["date"] for r in got] == [WEEK[0]]
    assert sm.model_rows_of(rows, source="carry_forward") == []


def test_survival_is_the_share_of_model_rows_unchanged_in_the_final_week():
    model = [_row(WEEK[0], "Ann"), _row(WEEK[0], "Ann"), _row(WEEK[1], "Bob")]
    final = [_row(WEEK[0], "Ann"), _row(WEEK[1], "Bob", start="5:00pm"), _row(WEEK[2], "Cat")]
    assert sm.survival(model, final) == (1, 3)                 # one Ann copy and Bob's moved start do not count


def test_stage_attribution_reads_the_loops_records():
    loop = {"records": [{"stage": "overtime", "fixes": [{}, {}], "trimmed": [{}], "staggered": []},
                        {"stage": "stagger", "fixes": [], "trimmed": [], "staggered": [{}, {}, {}]}],
            "rows_added": {"top_up": {WEEK[0]: 2, WEEK[1]: 1}}}
    got = sm.by_stage(loop)
    assert got["overtime"] == {"changed": 2, "trimmed": 1, "staggered": 0, "added": 0}
    assert got["stagger"]["staggered"] == 3 and got["top_up"]["added"] == 3


def test_role_violations_count_rows_in_a_role_the_person_does_not_hold():
    assert sm.role_violations([_row(WEEK[4], "Ann", role="Cook"), _row(WEEK[4], "Bob")], _c()) == 1


def test_the_context_round_trips_with_its_constraints():
    c = _c()
    c.blocked_dates = {"cat": {WEEK[4]: "on approved time off"}}
    blob = sm.snapshot({"week_dates": WEEK, "roster": ["Ann", "Bob", "Cat"], "hours_budget": 40.0,
                        "requirements_by_date": {f"{WEEK[4]}|night": {"Server": 2}}}, c,
                       signals={"roster": ["Ann"]}, weights={"coverage": 1.0}, model_rows=[_row(WEEK[4], "Ann")])
    back = sm.load_context(blob)
    assert isinstance(back["constraints"], sr.Constraints)
    assert back["constraints"].active == {"ann", "bob", "cat"}
    assert back["constraints"].can_work("Cat", WEEK[4], "night")[0] is False
    assert back["result"]["requirements_by_date"] == {f"{WEEK[4]}|night": {"Server": 2}}


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
    return db_path


def _call(rid, gid, model="claude-sonnet-5-5", out=40000, answer=None, rows=None):
    return so.record_call(rid, {"model": model, "messages": [{"role": "user", "content": "x"}]},
                          inputs={"roster": [["Ann", "Server"], ["Bob", "Server"], ["Cat", "Server"]]},
                          answer=answer, generation_id=gid, week_start=WEEK[0], dates=WEEK, model=model,
                          effort="medium", contract="csv", outcome="ok", seconds=300.0,
                          usage={"input_tokens": 10000, "output_tokens": out}, rows=rows or 0, call_kind="week")


def test_cost_per_accepted_week_counts_every_generation_over_the_published_weeks(db):
    rid = create_restaurant(Restaurant(name="Metric Grill", owner_email="m@x.com"), db_path=db)
    h1 = models.save_schedule_history(rid, WEEK[0], WEEK[-1], 40.0, 0, 30, _csv([_row(WEEK[0], "Ann")]), [],
                                      db_path=db)
    for gid in ("g1", "g2"):
        _call(rid, gid)
        sm.record(rid, h1, gid, WEEK[0], "week", model_rows=[_row(WEEK[0], "Ann")],
                  final_rows=[_row(WEEK[0], "Ann")], seconds=400.0, db_path=db)
    s = sm.summary(rid, db_path=db)
    assert s["generations"] == 2 and s["accepted_weeks"] == 0 and s["cost_per_accepted_week"] is None
    conn = models.get_conn(db)
    conn.execute("UPDATE schedule_history SET published_at=datetime('now') WHERE id=?", (h1,))
    conn.commit()
    conn.close()
    s = sm.summary(rid, db_path=db)
    from ai_utils import _estimate_cost
    one = _estimate_cost("claude-sonnet-5-5", 10000, 40000)
    assert s["accepted_weeks"] == 1
    assert s["cost_per_accepted_week"] == pytest.approx(2 * one, rel=0.05)
    assert s["survival_mean"] == 1.0 and s["seconds_p50"] == 400.0


def test_the_metrics_table_is_retained_a_year_and_the_eval_set_never_expires():
    assert ops._RETENTION_DAYS["schedule_generation_metrics"] == 365
    assert ops._RETENTION_COLUMN["schedule_generation_metrics"] == "created_at"
    for t in ("schedule_eval_items", "schedule_eval_runs", "schedule_eval_verdicts"):
        assert t not in ops._RETENTION_DAYS, t


# ── the post-model path, offline ───────────────────────────────────────────

def test_post_model_rows_runs_the_repair_loop_then_the_gates_fill_with_the_stored_context(monkeypatch):
    c = _c()
    ctx = {"constraints": c, "result": {"week_dates": WEEK, "week_days": DAYS, "roster": ["Ann", "Bob", "Cat"],
                                        "roster_roles": {"Ann": "Server", "Bob": "Server", "Cat": "Server"},
                                        "hours_budget": 0},
           "signals": None, "weights": None, "close_times": {"Friday": "10:00pm"}}
    seen = []
    real = se.repair_week

    def spy(rows, x):
        seen.append((x.editable, x.c is c, x.close_times, x.roster_roles))
        return real(rows, x)
    monkeypatch.setattr(se, "repair_week", spy)
    monkeypatch.setattr(se, "_quality_gate", lambda result: {"dates": [WEEK[4]], "reason": "1 day still had x",
                                                             "focus": [], "triggers": {}, "kinds": []})
    out = se.post_model_rows([_row(WEEK[4], "Ann")], ctx)
    assert [s[0] for s in seen] == [None, {WEEK[4]}]          # the week, then the gate's dates in code
    assert all(s[1] and s[2] == {"Friday": "10:00pm"} for s in seen)
    assert seen[0][3] == {"Ann": "Server", "Bob": "Server", "Cat": "Server"}
    assert c.roster_roles == {"Ann": "Server", "Bob": "Server", "Cat": "Server"}   # not a dataclass field
    assert out["gate"]["dates"] == [WEEK[4]] and out["gate"]["kept"] is True
    assert se.post_model_rows([_row(WEEK[4], "Ann")], ctx, gate=False)["gate"] is None


# ── the score against the published week ───────────────────────────────────

def test_rows_are_matched_to_the_published_week_by_person_date_family_and_start():
    published = [_row(WEEK[0], "Ann"), _row(WEEK[1], "Bob"), _row(WEEK[2], "Cat")]
    rows = [_row(WEEK[0], "Ann", start="4:30pm"),            # within 30 minutes: a match
            _row(WEEK[1], "Bob", start="5:00pm"),            # an hour off: not
            _row(WEEK[3], "Cat")]
    got = sev.compare_with_published(rows, published)
    assert got["matched"] == 1
    assert got["published_precision"] == pytest.approx(1 / 3, abs=1e-3)
    assert got["published_recall"] == pytest.approx(1 / 3, abs=1e-3)
    assert got["published_f1"] == pytest.approx(1 / 3, abs=1e-3)
    assert sev.compare_with_published(published, published)["published_f1"] == 1.0
    assert sev.compare_with_published(published, published)["hours_l1"] == 0.0


def test_a_paired_comparison_needs_its_interval_to_exclude_zero():
    runs = []
    for rid, (a, b) in enumerate(((0.8, 0.6), (0.7, 0.55), (0.9, 0.7), (0.75, 0.6)), 1):
        for i in range(3):
            runs.append({"arm": "T4", "item_key": f"k{rid}", "restaurant_id": rid, "metrics": {"published_f1": a}})
            runs.append({"arm": "T3", "item_key": f"k{rid}", "restaurant_id": rid, "metrics": {"published_f1": b}})
    p = sev.summarize_pair(runs, "T4", "T3", "published_f1")
    assert p["weeks"] == 4 and p["restaurants"] == 4 and p["ci90"][0] > 0
    assert sev.runs_short(runs, ["T4", "T3"], ["k1"]) == []
    assert sev.runs_short(runs[:2], ["T4"], ["k1"]) == [("T4", "k1", 1)]


# ── freezing and running the set ───────────────────────────────────────────

def _frozen_item(db, version="v1"):
    rid = create_restaurant(Restaurant(name="Eval Grill", owner_email="e@x.com"), db_path=db)
    names = ("Ann", "Bob", "Cat")
    published = [_row(d, n) for d in WEEK[:5] for n in names[:2]]
    hid = models.save_schedule_history(rid, WEEK[0], WEEK[-1], 60.0, 0, 30, _csv(published), [], db_path=db)
    conn = models.get_conn(db)
    conn.execute("UPDATE schedule_history SET published_at=datetime('now') WHERE id=?", (hid,))
    conn.commit()
    conn.close()
    answer = "\n".join(",".join(str(r[k]) for k in HEAD.split(",")) for r in published[:6])
    _call(rid, "gen-eval", answer=answer, rows=6)
    so.link_calls(rid, hid, "gen-eval")
    c = _c(names)
    blob = sm.snapshot({"week_dates": WEEK, "week_days": DAYS, "roster": list(names),
                        "roster_roles": {n: "Server" for n in names}, "hours_budget": 0,
                        "requirements_by_date": {f"{d}|night": {"Server": 2} for d in WEEK[:5]}}, c)
    sm.record(rid, hid, "gen-eval", WEEK[0], "week", final_rows=published, context=blob, db_path=db)
    got = sev.freeze(version, [hid], tags={hid: ["instruction:none"]}, db_path=db)
    assert got["frozen"] and not got["skipped"]
    return sev.load_items(version, db_path=db)[0]


def test_freeze_keeps_the_calls_the_context_and_the_published_rows(db):
    item = _frozen_item(db)
    assert item["published"] and len(item["published"]) == 10
    assert isinstance(item["context"]["constraints"], sr.Constraints)
    assert "size:0-39" in item["tags"] and "first_week" in item["tags"] and "instruction:none" in item["tags"]
    assert item["calls"][0]["answer"]


def test_a_week_never_published_is_not_frozen(db):
    rid = create_restaurant(Restaurant(name="Draft Grill", owner_email="d@x.com"), db_path=db)
    hid = models.save_schedule_history(rid, WEEK[0], WEEK[-1], 6.0, 0, 30, _csv([_row(WEEK[0], "Ann")]), [],
                                       db_path=db)
    _call(rid, "gen-draft", answer="x")
    so.link_calls(rid, hid, "gen-draft")
    sm.record(rid, hid, "gen-draft", WEEK[0], "week", context=sm.snapshot({"week_dates": WEEK}, _c()), db_path=db)
    got = sev.freeze("v2", [hid], db_path=db)
    assert not got["frozen"] and got["skipped"][0]["why"] == "the week was never published"


def test_the_set_runs_production_and_the_code_arms_three_times_each_with_no_model(db, monkeypatch):
    item = _frozen_item(db)
    called = []
    monkeypatch.setattr(sme, "default_call_model", lambda request: called.append(1))
    report = sme.run_set([item], [{"name": "production"}, {"name": "solver"}], runs=None)
    assert not called
    assert report["short"] == []
    per = {}
    for r in report["runs"]:
        per[r["arm"]] = per.get(r["arm"], 0) + 1
    assert per == {"production": sev.RUNS_PER_ARM, "solver": sev.RUNS_PER_ARM}
    prod = report["arms"]["production"]["published_f1"]
    assert prod["mean"] == pytest.approx(0.75, abs=0.26)      # 6 of the 10 published rows answered
    assert "published_f1" in report["pairs"]["solver"]


def test_a_saved_generation_leaves_its_metrics_row_with_its_context(db, monkeypatch):
    import test_sf10_e_redo as tr
    rid = create_restaurant(Restaurant(name="Hook Grill", owner_email="h@x.com"), db_path=db)
    base_rows = [tr._row(d, "Ana") for d in tr.WEEK]
    base = models.save_schedule_history(rid, tr.WEEK[0], tr.WEEK[-1], 42.0, 0, 30,
                                        tr.HEAD + "\n" + "\n".join(",".join(r[c] for c in se._ANSWER_COLS)
                                                                   for r in base_rows), [], db_path=db)
    monkeypatch.setattr(se, "_build_schedule_result", lambda restaurant_id, **k: tr._result(
        [tr._row(tr.WEEK[4], "Cy", "4:30pm", "10:00pm")]))
    finished = {}
    monkeypatch.setattr(se._ops, "finish_async_job", lambda j, s, r: finished.update(status=s, result=r))
    se._run_schedule_job("metrics-hook", rid, dates=[tr.WEEK[4]], base_history_id=base,
                         instruction=se.with_redo_reason(None, {"chip": "times", "text": None}))
    assert finished["status"] == "done"
    conn = models.get_conn(db)
    row = conn.execute("SELECT * FROM schedule_generation_metrics WHERE restaurant_id=?", (rid,)).fetchone()
    conn.close()
    assert row is not None and row["kind"] == "redo" and row["history_id"] == finished["result"]["history_id"]
    assert row["final_rows"] == 7 and row["context_z"]
    ctx = sm.load_context(row["context_z"])
    assert ctx["result"]["week_dates"] == tr.WEEK


def test_a_stored_context_with_a_set_of_pairs_replays():
    # The learning ablation failed every week with "unhashable type: 'set'"
    # (10/15/26): a set of frozensets came back as a set of sets.
    v = {"pairs": {frozenset({"Ana", "Bo"}), frozenset({"Cy", "Di"})}, ("Mon", 1): {("x", frozenset({1}))}}
    back = so.decode(json.loads(json.dumps(so.encode(v))))
    assert back == v
