"""dsr_own: the nightly report never read its own last actions or how its
predictions held (memory audit 9/29/26, M4). The narrative prompt now carries
YESTERDAY'S PRIORITIES — the previous report's actions for tonight, each with
its ledger status and the fact keys tonight that measure it — and the
prediction review, both fenced as data."""
import pytest

import ai_guard
import dsr
import rec_ledger
from dsr import narrative, predictions, store
from tests.test_dsr_narrative import (_Client, _act, _ctx, _prompt, _quiet_ai, rest,  # noqa: F401
                                      strong_night, strong_reply)
import ai_utils

PREV = "2026-09-18"
DAY = "2026-09-19"


def _previous_report(rid, db_path, actions):
    rep = store.create_report(rid, PREV, db_path=db_path)
    store.save_narrative(rep["id"], {"executive_summary": {"text": "Friday was steady.", "cites": ["sales.net"]},
                                     "actions_tomorrow": actions}, db_path=db_path)
    return rep


def _actions():
    labor = dict(_act("Cut one server from Saturday lunch.", "Labor ran 29% against a 26% target.",
                      "control_hours", ["labor.pct", "labor.target_pct"]), key="dsr_action:control_hours:labor")
    food = dict(_act("Order extra brioche buns.", "Buns ran low.", "reorder", ["food.low_stock"],
                     subject="Brioche buns"), key="dsr_action:reorder:food/brioche-buns")
    reviews = dict(_act("Reply to the urgent review before service.", "1 urgent review.", "respond_reviews",
                        ["reviews.urgent"]), key="dsr_action:respond_reviews:reviews")
    return [labor, food, reviews]


def test_each_priority_carries_its_ledger_status_and_what_measures_it(rest, db_path):
    _previous_report(rest.id, db_path, _actions())
    rec_ledger.record(rest.id, "dsr_action:control_hours:labor", "completed", surface="dsr", db_path=db_path)
    rec_ledger.present(rest.id, "dsr_action:respond_reviews:reviews", "reviews", "dsr", db_path=db_path)
    own = narrative.own_record(_ctx(rest, db_path, DAY), strong_night())
    labor, reviews = own["priorities"]
    assert labor.startswith("- Cut one server from Saturday lunch. — answered Done (")
    assert "compare with labor.pct" in labor
    assert "labor.target_pct" not in labor, "a target is a plan, never a measured follow-up"
    assert "not answered yet" in reviews and "compare with reviews.urgent" in reviews
    assert not any("brioche" in line.lower() for line in own["priorities"]), \
        "the Food block is owner-only and this narrative renders into the manager's view"
    assert own["label"] == "9/18/26" and PREV in own["dates"]
    answered = labor.split("answered Done (")[1].split(")")[0]
    assert answered in narrative.Facts(strong_night(), extra_dates=own["dates"]).dates, \
        "the answer's date may be named in tonight's report"


def test_a_priority_never_shown_says_so_and_nothing_before_the_first_report(rest, db_path):
    assert narrative.own_record(_ctx(rest, db_path, DAY), strong_night())["priorities"] == []
    _previous_report(rest.id, db_path, _actions()[:1])
    own = narrative.own_record(_ctx(rest, db_path, DAY), strong_night())
    assert "not shown to anyone yet" in own["priorities"][0]


def test_the_prediction_review_leaves_out_the_budget_and_counts_what_held(rest, db_path):
    preds = [{"key": "sales_range", "metric": "sales.net", "op": "between", "low": 15000.0, "high": 21000.0,
              "text": "Sales between $15,000 and $21,000", "basis": "b"},
             {"key": "sales_budget", "metric": "sales.net", "op": "gt", "value": 18000.0,
              "text": "Sales expected above budget ($18,000)", "basis": "b"}]
    predictions.record(rest.id, PREV, DAY, preds, db_path=db_path)
    predictions.grade(rest.id, DAY, strong_night(), db_path=db_path)
    own = narrative.own_record(_ctx(rest, db_path, DAY), strong_night())
    assert own["predictions"][0] == "- Sales between $15,000 and $21,000 — came true"
    assert not any("budget" in line for line in own["predictions"]), "the budget is owner-only"
    assert own["predictions"][-1] == "- Over the last 30 days: 1 of 1 predictions came true (too few to call a rate)"


def test_both_sections_reach_the_model_fenced_with_the_rule_that_governs_them(monkeypatch, rest, db_path):
    _previous_report(rest.id, db_path, _actions())
    rec_ledger.record(rest.id, "dsr_action:control_hours:labor", "completed", surface="dsr", db_path=db_path)
    predictions.record(rest.id, PREV, DAY, [{"key": "sales_range", "metric": "sales.net", "op": "between",
                                             "low": 15000.0, "high": 21000.0,
                                             "text": "Sales between $15,000 and $21,000", "basis": "b"}],
                       db_path=db_path)
    predictions.grade(rest.id, DAY, strong_night(), db_path=db_path)
    client = _Client(strong_reply())
    monkeypatch.setattr(ai_utils, "get_client", lambda timeout=None: client)
    out = narrative.write(_ctx(rest, db_path, DAY), strong_night())
    assert out["ok"], out
    system, user = _prompt(client)
    assert "Never repeat one the owner already answered" in system
    assert "yesterday's priorities, the prediction review" in system, "named among the fenced data"
    head, _, rest_of = user.partition("YESTERDAY'S PRIORITIES (the 9/18/26 report's actions for tonight")
    assert rest_of, user
    fenced = rest_of.split("\n", 1)[1]
    assert fenced.startswith(ai_guard.UNTRUSTED_OPEN)
    assert "Cut one server from Saturday lunch. — answered Done" in fenced
    assert "HOW THE LAST REPORT'S PREDICTIONS ABOUT TONIGHT TURNED OUT" in user
    assert "Sales between $15,000 and $21,000 — came true" in user


def test_a_broken_ledger_never_takes_the_report_down(monkeypatch, rest, db_path):
    import ai_reads
    _previous_report(rest.id, db_path, _actions())
    monkeypatch.setattr(ai_reads, "answer_state", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("x")))
    own = narrative.own_record(_ctx(rest, db_path, DAY), strong_night())
    assert own["priorities"] and all("status unknown" in line for line in own["priorities"])
