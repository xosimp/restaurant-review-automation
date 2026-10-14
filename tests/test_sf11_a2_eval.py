"""Schedule fix round 10/11/26, workstream A2 — the golden-set eval made
decision-grade (blind re-audit AI-2, AI-3, AI-4, AI-5, AI-6): arms scored
BEFORE the repair, an outcome metric for routing with agreement with the
published week only on weeks a third arm drafted, verdicts only from 20
weeks of 5 learning-eligible restaurants, demo accounts never frozen, the
critic arm's real cost. No model is called here."""
import json
import os
import sys
import types

import pytest

import models
import schedule_critic as scr
import schedule_eval as sev
import schedule_rules as sr
from models import Restaurant, create_restaurant
from test_sf10_e_eval import WEEK, _c, _frozen_item, _row, db  # noqa: F401  (db is a fixture)

sys.path.append(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))
import schedule_model_eval as sme  # noqa: E402


def _pair_runs(n_rest, weeks_each, a, b, metric="pre_repair_defects", drafted=""):
    runs = []
    for rid in range(1, n_rest + 1):
        for w in range(weeks_each):
            key = f"r{rid}w{w}"
            for i in range(3):
                jitter = ((rid * 7 + w * 3) % 5) * 0.01
                runs.append({"arm": "T4", "item_key": key, "restaurant_id": rid, "drafted_by": drafted,
                             "metrics": {metric: a + jitter}})
                runs.append({"arm": "T3", "item_key": key, "restaurant_id": rid, "drafted_by": drafted,
                             "metrics": {metric: b + jitter}})
    return runs


# ── AI-5: the floors ───────────────────────────────────────────────────────

def test_two_restaurants_never_name_a_winner_however_clear_the_interval(db):
    paired = sev.summarize_pair(_pair_runs(4, 6, 1.0, 3.0), "T4", "T3", sev.ROUTE_METRIC)
    assert paired["ci90"][1] < 0                     # T4 clearly fewer defects ...
    row = sev.record_verdict(sev.ROUTE_METRIC, "T4", "T3", (0, 39), paired, db_path=db)
    assert row["winner"] is None and "4/5 restaurants" in row["note"]  # ... but from 4 restaurants: none
    assert sev.route_evidence(20, db_path=db) is None


def test_twenty_weeks_of_five_restaurants_name_the_winner_in_the_metrics_direction(db):
    paired = sev.summarize_pair(_pair_runs(5, 4, 1.0, 3.0), "T4", "T3", sev.ROUTE_METRIC)
    assert paired["weeks"] == 20 and paired["restaurants"] == 5
    row = sev.record_verdict(sev.ROUTE_METRIC, "T4", "T3", (0, 39), paired, db_path=db)
    # Fewer pre-repair defects is better: a NEGATIVE T4-minus-T3 names T4.
    assert row["winner"] == "T4" and row["note"] is None
    assert "the eval showed T4 better for rosters of 0-39" in sev.route_evidence(20, db_path=db)
    # A higher-is-better metric reads the other way.
    assert sev.verdict_winner("quality", "T4", "T3", paired)[0] == "T3"


def test_a_stored_row_under_the_floor_is_never_evidence(db):
    conn = models.get_conn(db)
    conn.execute("INSERT INTO schedule_eval_verdicts (metric, arm_a, arm_b, band_lo, band_hi, weeks, restaurants, "
                 "diff, ci_lo, ci_hi, winner, method) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                 (sev.ROUTE_METRIC, "T4", "T3", 0, 10000, 6, 2, -1.0, -1.5, -0.5, "T4", sev.VERDICT_METHOD))
    conn.commit()
    conn.close()
    assert sev.route_evidence(250, db_path=db) is None


def test_a_kind_of_week_is_routed_only_on_its_own_verdict(db):
    assert sev.tag_evidence("holiday", db_path=db) is None
    paired = sev.summarize_pair(_pair_runs(5, 4, 1.0, 3.0), "T4", "T3", sev.ROUTE_METRIC)
    sev.record_verdict(sev.ROUTE_METRIC, "T4", "T3", None, paired, tag="holiday", db_path=db)
    assert "holiday weeks" in sev.tag_evidence("holiday", db_path=db)
    assert sev.tag_evidence("first_week", db_path=db) is None
    # A tag verdict is not a band verdict.
    assert sev.route_evidence(20, db_path=db) is None


def test_set_readiness_counts_weeks_and_restaurants_per_band_and_tag():
    items = [{"restaurant_id": r, "roster_size": 30, "tags": ["holiday"] if r == 1 else []}
             for r in range(1, 6) for _w in range(4)]
    got = sev.set_readiness(items)
    assert got["bands"]["0-39"] == {"weeks": 20, "restaurants": 5, "ready": True} and got["ready"]
    assert got["tags"]["holiday"]["ready"] is False


def test_a_demo_account_is_never_frozen(db):
    item = _frozen_item(db)                      # a live account freezes
    assert item["drafted_by"] == ""             # its stored call names no tier
    conn = models.get_conn(db)
    conn.execute("UPDATE restaurants SET is_demo=1 WHERE id=?", (item["restaurant_id"],))
    conn.commit()
    conn.close()
    got = sev.freeze("v-demo", [item["history_id"]], db_path=db)
    assert not got["frozen"] and "may not teach" in got["skipped"][0]["why"]


# ── AI-3: agreement with the published week is anchored ───────────────────

def test_agreement_with_the_published_week_is_read_only_on_weeks_a_third_arm_drafted():
    t3_drafted = _pair_runs(5, 4, 0.6, 0.9, metric="published_f1", drafted="T3")
    assert sev.summarize_pair(t3_drafted, "T4", "T3", "published_f1")["weeks"] == 0
    pinned = _pair_runs(5, 4, 0.6, 0.9, metric="published_f1", drafted="default")
    assert sev.summarize_pair(pinned, "T4", "T3", "published_f1")["weeks"] == 20
    assert sev.anchored("production", "") and sev.anchored("carry_forward", "T4")
    assert sev.anchored("T3:claude-sonnet-5-5/medium/stored", "T3") and not sev.anchored("T4", "T3")
    # The routing metric is an outcome, never the anchored one.
    assert sev.ROUTE_METRIC not in sev.ANCHORED_METRICS


# ── AI-4: the arm's own rows before the repair ────────────────────────────

def test_an_arm_is_scored_on_what_it_wrote_before_the_repair_touched_it(db):
    item = _frozen_item(db)
    c = item["context"]["constraints"]
    c.blocked_dates["ann"] = {WEEK[0]: "on approved time off"}
    rows = [_row(WEEK[0], "Ann"), _row(WEEK[1], "Bob")]          # Ann on her approved day off
    got = sev.score_arm(rows, item)
    assert got["hard_before"] >= 1
    for k in ("pre_repair_defects", "unmet_before", "manager_minutes_before", "rows_repaired", "repair_seconds",
              "hard", "published_f1"):
        assert k in got
    assert got["pre_repair_defects"] == got["hard_before"] + got["unmet_before"]
    assert sev.pre_repair_metrics([_row(WEEK[1], "Bob")], item)["hard_before"] == 0


def test_the_set_report_leads_with_the_pre_repair_metrics(db):
    item = _frozen_item(db)
    report = sme.run_set([item], [{"name": "production"}], runs=1)
    assert report["arms"]["production"]["pre_repair_defects"]["n"] == 1
    assert sme.SET_METRICS[0] == sev.ROUTE_METRIC
    assert all("drafted_by" in r for r in report["runs"])


# ── AI-6: the critic arm's cost is metered ─────────────────────────────────

def test_the_critic_arm_reports_what_its_calls_cost(db, monkeypatch):
    item = _frozen_item(db)
    monkeypatch.setattr(sev, "code_arm_rows", lambda arm, it: [_row(WEEK[0], "Ann")])

    def fake(req):
        return types.SimpleNamespace(stop_reason="end_turn", model="claude-sonnet-5-5",
                                     usage=types.SimpleNamespace(input_tokens=100000, output_tokens=20000,
                                                                 cache_read_input_tokens=0,
                                                                 cache_creation_input_tokens=0),
                                     content=[types.SimpleNamespace(type="text", text=json.dumps({"edits": []}))])
    rows, cost, _secs, err = sme.arm_rows(item, {"name": sev.CRITIC_ARM}, call_model=fake)
    # 100k in at $2 + 20k out at $10 a million = $0.40, never 0.
    assert err is None and cost == pytest.approx(0.40, abs=0.01)


def test_the_critic_reviews_a_big_week_a_few_whole_days_a_call_never_truncated():
    names = [f"P{i:03d}" for i in range(160)]
    c = _c(names)
    rows = [_row(d, n) for d in WEEK[:5] for n in names]          # 800 rows
    runs = scr.chunks(rows)
    assert len(runs) >= 3 and sorted(d for r in runs for d in r) == sorted(WEEK[:5])
    seen = []

    def fake(req):
        seen.append(req)
        return types.SimpleNamespace(stop_reason="end_turn", content=[types.SimpleNamespace(type="text",
                                                                                            text='{"edits": []}')])
    got = scr.review(1, rows, c, call=fake)
    assert got["calls"] == len(runs) == len(seen)
    shown = "\n".join(r["messages"][0]["content"] for r in seen)
    assert all(f"{d} Server" in shown for d in WEEK[:5])           # every day reviewed
    req = seen[0]
    assert req["thinking"] == {"type": "adaptive"} and req["output_config"]["effort"] == "medium"
    assert req["max_tokens"] >= 16000


def test_the_critic_is_shown_the_people_facts_its_judgement_needs():
    c = _c(("Ann", "Bob"))
    c.preferred = {"Ann": {"preferred_dayparts": ["night"], "desired_hours": 30}}
    signals = {"prior_pattern": {"Ann": {"days": ["Friday", "Saturday"], "dayparts": ["night"]}},
               "pairs": {"prefer": {frozenset(("ann", "bob"))}, "avoid": set(),
                         "private": set()},
               "reliability": {"Bob": {"shifts": 12, "no_show_rate": 0.25, "late_rate": 0.1}}}
    facts = scr.people_facts(["Ann", "Bob"], signals, c)
    assert "usually Fri/Sat night" in facts["Ann"] and "works well with Bob" in facts["Ann"]
    assert "asked for night" in facts["Ann"] and "wants about 30h a week" in facts["Ann"]
    assert "missed 25%, late 10% of 12 watched shifts" in facts["Bob"]
    # An owner-only pairing is never shown.
    private = dict(signals, pairs={"prefer": {frozenset(("ann", "bob"))}, "private": {frozenset(("ann", "bob"))}})
    assert not any("Bob" in f for f in scr.people_facts(["Ann"], private, c).get("Ann", []))
    req = scr.build_request(1, [_row(WEEK[4], "Ann")], people=facts, owner_words=["Keep Ann on Fridays"])
    body = req["messages"][0]["content"]
    assert "THE PEOPLE" in body and "usually Fri/Sat night" in body and "Keep Ann on Fridays" in body


def test_production_passes_the_critic_its_score_and_the_owners_words():
    import inspect
    import schedule_engine as se
    src = inspect.getsource(se._code_draft)
    assert "score=_critic_scorer(" in src and "owner_words=fallback.get(\"owner_words\")" in src
    assert se._critic_scorer({}, None, None) is None
    score = se._critic_scorer({}, {"roster": ["Ann"]}, None)
    assert callable(score)
