"""The blind re-audit of the AI orchestration core (10/7/26), finding by
finding (#3 and the invoice half of #11 belong to another fix round):

  #1  a kept request is redacted like the ai_calls trace — the guest names
      collected from all of its text first
  #2  a shadow pair is judged fairly: the same form, the same deterministic
      check, the same rubric and context, production's first call's cost
  #4  the schedule, recipe drafts and the content calendar file outcomes
  #5  an outcome or a later verdict lands on the run that served the text
  #6  each rung keeps its own request (seq = step + 1, tagged with its tier);
      a batch item that is a run's first rung keeps its request at submit;
      the diagnoses are not replayed and are console-read-only
  #7  a ladder the router cannot run is refused, and a bad rung is recorded
  #8  every guest text and email is gated where it is SENT
  #9  (tests/test_ai_orchestration.py) the learner reads the ladder in force
  #10 Policy.batch gates batching; AI_WORKFLOW_OFF switches a workflow off
  #11 a run whose caller served fallback copy is re-filed as a fallback
  #12 review replies get the shadow rubric
"""
import inspect
import json
import os
import sqlite3
import sys
import zlib
from dataclasses import replace

import pytest

import admin_ops
import ai_batches
import ai_learning
import ai_orchestrator as orch
import ai_reviewer
import ai_utils
import ai_workflows as wf
import data_health
import models
from models import Restaurant, create_restaurant

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@pytest.fixture(autouse=True)
def _every_restaurant_a_canary(monkeypatch):
    """These tests read the ladders as written; a canaried workflow starts on
    its cheap rung only for AI_CANARY_RESTAURANTS (context re-audit 10/7/26
    #3), so every restaurant here is a canary restaurant."""
    monkeypatch.setenv("AI_CANARY_RESTAURANTS", "*")


@pytest.fixture
def db(db_path, monkeypatch):
    real = models.get_conn
    redirect = lambda *a, **k: real(db_path)  # noqa: E731
    for mod in list(sys.modules.values()):
        try:
            if getattr(mod, "get_conn", None) is real:
                monkeypatch.setattr(mod, "get_conn", redirect)
        except Exception:
            pass
    monkeypatch.setattr(models, "get_conn", redirect)
    monkeypatch.setattr(models, "DB_PATH", db_path)
    orch._OVERRIDES.clear()
    monkeypatch.setattr(orch, "REPLAY_SAMPLE_RATE", 0.0)
    monkeypatch.setattr(ai_utils, "ai_budget_exceeded", lambda *a, **k: None)
    monkeypatch.setenv("AI_SHADOW_REVIEW", "0")
    monkeypatch.delenv("AI_WORKFLOW_OFF", raising=False)
    ai_utils._LAST_CALL.set(None)
    yield db_path
    orch._OVERRIDES.clear()


def _q(db, sql, args=()):
    c = sqlite3.connect(db)
    c.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in c.execute(sql, args)]
    finally:
        c.close()


def _x(db, sql, args=()):
    c = sqlite3.connect(db)
    try:
        cur = c.execute(sql, args)
        c.commit()
        return cur.lastrowid
    finally:
        c.close()


class _Usage:
    def __init__(self):
        self.input_tokens, self.output_tokens = 1000, 200
        self.cache_creation_input_tokens = self.cache_read_input_tokens = 0


class _Block:
    def __init__(self, text):
        self.type, self.text = "text", text


class _Msg:
    def __init__(self, text):
        self.content = [_Block(text)]
        self.usage = _Usage()
        self.stop_reason = "end_turn"
        self.id = "msg_test"


class _Client:
    def __init__(self, text):
        self.messages = self
        self.text = text

    def create(self, **kwargs):
        return _Msg(self.text)


def _run_through_ledger(workflow, text, *, prompt="Ratings fell 0.2 this month.", check=None, rid=7, subject=None):
    """A real run whose one call goes through create_with_retry (ledger row,
    trace, correlation id = the run id)."""
    client = _Client(text)

    def attempt(route, notes):
        return ai_utils.create_with_retry(client, restaurant_id=rid, action=workflow, max_tokens=300,
                                          **route.apply({"model": "m", "messages": [
                                              {"role": "user", "content": prompt}]}))
    return orch.generate(workflow, rid, attempt=attempt, check=check, subject=subject or workflow)


# ── #1 ─────────────────────────────────────────────────────────────────────

def test_a_kept_request_redacts_a_guest_name_wherever_the_request_carries_it(db, monkeypatch):
    monkeypatch.setattr(orch, "REPLAY_SAMPLE_RATE", 1.0)

    def attempt(route, notes):
        orch.keep_request({"model": route.model, "max_tokens": 50, "system": "Thank Ann Smith by name.",
                           "messages": [{"role": "user", "content": "Reviewer: Ann Smith\nReview: Ann Smith said "
                                                                    "the soup was cold. Write to ann@example.com"}]})
        return "x"
    orch.generate("draft_response", 7, attempt=attempt)
    ((_run, _rid, req),) = orch.kept_requests("draft_response")
    blob = json.dumps(req)
    assert "Ann" not in blob and "ann@example.com" not in blob, "kept in the clear, unlike the trace"
    assert "[name] said the soup was cold" in blob and "Thank [name] by name" in blob


# ── #6 ─────────────────────────────────────────────────────────────────────

def test_each_rung_keeps_its_first_call_under_its_own_seq_and_tier(db, monkeypatch):
    monkeypatch.setattr(orch, "REPLAY_SAMPLE_RATE", 1.0)

    def attempt(route, notes):
        for n in range(2):          # a second call inside one rung is not kept
            orch.keep_request({"model": route.model, "messages": [
                {"role": "user", "content": f"call {n}; notes: {'; '.join(notes) or 'none'}"}]})
        return route.tier

    def check(r):
        return orch.Verdict.passed() if r == "T2" else orch.Verdict.failed("validation_refuse", "an invented figure")
    rr = orch.generate("labor_insight", 7, attempt=attempt, check=check)
    rows = _q(db, "SELECT seq, tier FROM ai_run_requests WHERE run_id=? ORDER BY seq", (rr.run_id,))
    assert rows == [{"seq": 1, "tier": "T1"}, {"seq": 2, "tier": "T2"}]
    ((run_id, _rid, req),) = orch.kept_requests("labor_insight")
    text = json.dumps(req)
    assert run_id == rr.run_id and "call 0" in text and "invented" not in text, "seq 1 is the first rung"
    assert orch.kept_requests("labor_insight", tier="T2") == []
    # A row kept before requests carried their tier is never read as a first rung.
    _x(db, "INSERT INTO ai_run_requests (run_id, seq, workflow, restaurant_id, request_z) "
           "VALUES ('run:old', 1, 'labor_insight', 7, ?)", (zlib.compress(b"{}"),))
    assert len(orch.kept_requests("labor_insight")) == 1


class _Batches:
    def __init__(self):
        self.sent = []

    def create(self, requests):
        self.sent.extend(requests)
        return type("Batch", (), {"id": "msgbatch_test"})()


class _BatchClient:
    def __init__(self):
        self.messages = type("Messages", (), {})()
        self.messages.batches = _Batches()


def test_a_batch_item_that_is_a_runs_first_rung_keeps_its_request_at_submit(db, monkeypatch):
    monkeypatch.setattr(orch, "REPLAY_SAMPLE_RATE", 1.0)
    monkeypatch.setattr(ai_batches, "_scheduling_allowed", lambda: True)
    run_id = orch.new_run_id("dsr_narrative")
    item = {"custom_id": "dsr-7-test", "restaurant_id": 7, "action": "dsr_narrative",
            "request": {"model": "m", "max_tokens": 50, "messages": [{"role": "user", "content": "the night"}]},
            "readiness": data_health.NOT_APPLICABLE, "callback": "ai_learning:shadow_landed",
            "correlation_id": run_id}
    assert ai_batches.submit("dsr_narrative", [item], client=_BatchClient()) == {"dsr-7-test": ai_batches.SUBMITTED}
    assert _q(db, "SELECT seq, tier, workflow FROM ai_run_requests WHERE run_id=?", (run_id,)) == \
        [{"seq": 1, "tier": wf.POLICIES["dsr_narrative"].ladder[0], "workflow": "dsr_narrative"}]
    # The learner's own replays (a "shadow:" correlation) keep nothing.
    shadow = dict(item, custom_id="sa-test", correlation_id="shadow:x")
    ai_batches.submit("shadow_arms", [shadow], client=_BatchClient())
    assert len(_q(db, "SELECT 1 FROM ai_run_requests")) == 1


def test_the_diagnoses_are_not_replayed_and_the_console_cannot_override_them(db):
    for name in ("review_diagnosis", "food_cost_diagnosis"):
        assert name not in ai_learning.SHADOW_RUBRICS and name not in dict(ai_learning.shadow_workflows())
    for name in wf.CONSOLE_READ_ONLY:
        assert not wf.overridable(name)
        with pytest.raises(ValueError):
            orch.set_override(name, {"caps": {"calls": 2}}, db_path=db)
    out = admin_ops.ai_route_set_override("weekly_plan", {"caps": {"calls": 3}})
    assert out["ok"] is False and out["status"] == 409
    view = {w["workflow"]: w for w in admin_ops.ai_routes_view()["workflows"]}
    assert view["weekly_plan"]["overridable"] is False and view["labor_insight"]["overridable"] is True
    assert view["labor_insight"]["effective"]["context_informational"] is True
    # Every workflow a rubric replays runs through generate() and has a check surface.
    for name in ai_learning.SHADOW_RUBRICS:
        assert wf.overridable(name) and name in ai_learning.SHADOW_SURFACES


# ── #2 ─────────────────────────────────────────────────────────────────────

def _shadow_row(db, prod_run_id, workflow="review_insight", tier="T1"):
    return ai_learning._write_shadow_run({"workflow": workflow, "run_id": prod_run_id, "tier": tier},
                                         status="ok", model="m", cost=0.001,
                                         detail={"production_first_cost": 0.01})


def test_a_candidate_the_check_refuses_scores_zero_and_its_rubric_is_never_asked(db, monkeypatch):
    monkeypatch.setattr(orch, "REPLAY_SAMPLE_RATE", 1.0)
    rr = _run_through_ledger("review_insight", "Ratings fell 0.2 this month; answer the low ones.")
    call_id, _pv = ai_learning._production_call(models.get_conn(), rr.run_id, "review_insight")
    asked = []

    def review_text(kind, draft, restaurant_id=None, context="", mode="haiku_gate"):
        asked.append(draft)
        return orch.Verdict(ok=True, score=0.9, label="pass")
    monkeypatch.setattr(ai_reviewer, "review_text", review_text)
    sid = _shadow_row(db, rr.run_id)
    ps, cs = ai_learning.score_pair("review_insight", sid, "Ratings fell 40% — comp every table tonight.",
                                    rr.run_id, call_id)
    assert (ps, cs) == (0.9, 0.0)
    assert asked == ["Ratings fell 0.2 this month; answer the low ones."], "only production was scored"
    (row,) = _q(db, "SELECT reviewer_score, reviewer_notes, context_json FROM ai_runs WHERE run_id=?", (sid,))
    assert row["reviewer_score"] == 0.0 and "refused by the check" in row["reviewer_notes"]
    assert json.loads(row["context_json"])["production_score"] == 0.9
    assert _q(db, "SELECT reviewer_score FROM ai_runs WHERE run_id=?", (rr.run_id,))[0]["reviewer_score"] is None


def test_a_production_text_the_check_refuses_leaves_the_pair_uncounted(db, monkeypatch):
    monkeypatch.setattr(orch, "REPLAY_SAMPLE_RATE", 1.0)
    rr = _run_through_ledger("review_insight", "Labor ran 31% on Tuesday, two points over target.")
    call_id, _pv = ai_learning._production_call(models.get_conn(), rr.run_id, "review_insight")
    monkeypatch.setattr(ai_reviewer, "review_text", lambda *a, **k: pytest.fail("scored an uncounted pair"))
    sid = _shadow_row(db, rr.run_id)
    assert ai_learning.score_pair("review_insight", sid, "Ratings fell this month.", rr.run_id, call_id) == \
        (None, None)
    assert ai_learning._shadow_pairs(28, None) == {}


def test_the_candidate_is_read_in_the_form_the_trace_keeps_production():
    cap = ai_utils._trace_caps("review_insight")[1]
    out = ai_learning._as_traced("review_insight", "Call 312-555-0199. " + "x" * (cap + 500),
                                 {"messages": [{"role": "user", "content": "Reviewer: Bea Lin\nfine"}]})
    assert len(out) <= cap and "312-555-0199" not in out
    assert ai_learning._as_traced("review_insight", "Thanks, Bea Lin!", {"messages": [
        {"role": "user", "content": "Reviewer: Bea Lin\nfine"}]}) == "Thanks, [name]!"


def test_an_escalated_production_run_is_never_replayed(db, monkeypatch):
    monkeypatch.setattr(orch, "REPLAY_SAMPLE_RATE", 1.0)
    rr = _run_through_ledger("marketing_content", "Taco night is back.")
    _x(db, "UPDATE ai_runs SET escalations=1 WHERE run_id=?", (rr.run_id,))
    monkeypatch.setattr(ai_batches, "enabled", lambda w: True)
    monkeypatch.setattr(ai_batches, "submit", lambda w, items: pytest.fail("replayed an escalated run"))
    out = ai_learning.shadow_arms("marketing_content", "T1", sample=5, budget_usd=1.0)
    assert out["submitted"] == 0 and out["skipped"] == 1


# ── #5 ─────────────────────────────────────────────────────────────────────

def test_an_outcome_never_lands_on_a_held_run_or_a_shadow(db):
    ok = orch.generate("labor_insight", 7, attempt=lambda r, n: "read", subject="labor:10/7")

    def held(route, notes):
        raise ai_utils.DataNotReady({"decision": "refuse", "reason": "stale"})
    with pytest.raises(ai_utils.DataNotReady):
        orch.generate("labor_insight", 7, attempt=held, subject="labor:10/7")
    _x(db, "INSERT INTO ai_runs (run_id, workflow, restaurant_id, subject, status, shadow_of, created_at) "
           "VALUES ('run:shadow1', 'labor_insight', 7, 'labor:10/7', 'ok', ?, datetime('now', '+1 minute'))",
       (ok.run_id,))
    assert orch.subject_run("labor_insight", 7, "labor:10/7")["run_id"] == ok.run_id
    assert orch.record_outcome("labor_insight", 7, "labor:10/7", "accepted")
    assert _q(db, "SELECT outcome FROM ai_runs WHERE run_id=?", (ok.run_id,))[0]["outcome"] == "accepted"
    assert _q(db, "SELECT COUNT(*) n FROM ai_runs WHERE outcome IS NOT NULL")[0]["n"] == 1


def test_a_replys_outcome_and_gate_verdict_land_on_the_run_that_wrote_it(db):
    import drafter
    rid = create_restaurant(Restaurant(name="Run Grill", owner_email="o@rungrill.test"), db_path=db)
    review_id = _x(db, "INSERT INTO reviews (restaurant_id, platform, external_id, rating, text, fetched_at) "
                       "VALUES (?, 'google', 'r-run', 5, 'Lovely', datetime('now'))", (rid,))
    wrote = orch.generate("draft_response", rid, attempt=lambda r, n: "Thanks!", subject=f"review:{review_id}")
    assert models.update_draft(review_id, "Thanks!", run_id=wrote.run_id)
    # A later redraft that lost (an approval won the race): its run is newer.
    _x(db, "UPDATE ai_runs SET created_at=datetime('now','-1 hour') WHERE run_id=?", (wrote.run_id,))
    lost = orch.generate("draft_response", rid, attempt=lambda r, n: "Thank you!", subject=f"review:{review_id}")
    assert drafter.draft_run_id(rid, review_id) == wrote.run_id
    assert drafter.record_reply_outcome(rid, review_id, "accepted")
    outcomes = {r["run_id"]: r["outcome"] for r in _q(db, "SELECT run_id, outcome FROM ai_runs")}
    assert outcomes == {wrote.run_id: "accepted", lost.run_id: None}
    assert orch.record_review("draft_response", rid, f"review:{review_id}", orch.Verdict(ok=True, score=0.7),
                              run_id=drafter.draft_run_id(rid, review_id))
    assert _q(db, "SELECT reviewer_score FROM ai_runs WHERE run_id=?", (wrote.run_id,))[0]["reviewer_score"] == 0.7


# ── #4 ─────────────────────────────────────────────────────────────────────

_CSV_HEAD = "date,day,employee,role,shift_start,shift_end,scheduled_hours,notes\n"
_WEEK = (_CSV_HEAD + "2026-10-12,Monday,Ann,Server,10:00,16:00,6,\n"
         "2026-10-12,Monday,Bob,Cook,08:00,16:00,8,\n")


def _schedule_week(db, rid):
    import schedule_versions as sv
    hid = _x(db, "INSERT INTO schedule_history (restaurant_id, week_start, week_end, schedule_csv) "
                 "VALUES (?, '2026-10-12', '2026-10-18', ?)", (rid, _WEEK))
    sv.append(rid, hid, "generated", _WEEK, db_path=db)
    _x(db, "INSERT INTO ai_runs (run_id, workflow, restaurant_id, subject, status, policy_version) "
           "VALUES (?, 'labor_schedule', ?, 'week:2026-10-12', 'ok', ?)", (f"run:sched:{hid}", rid, wf.POLICY_VERSION))
    return hid


def test_a_saved_and_published_week_files_the_share_of_its_shifts_kept(db):
    import schedule_versions as sv
    rid = create_restaurant(Restaurant(name="Week Grill", owner_email="o@weekgrill.test"), db_path=db)
    hid = _schedule_week(db, rid)
    edited = _WEEK.replace("Bob,Cook,08:00", "Cal,Cook,08:00")
    sv.append(rid, hid, "edited", edited, saved_by="owner", saved_authority="principal", db_path=db)
    assert sv.record_week_outcome(rid, hid, actor=None, db_path=db)
    (row,) = _q(db, "SELECT outcome, outcome_quality FROM ai_runs WHERE run_id=?", (f"run:sched:{hid}",))
    assert row == {"outcome": "edited", "outcome_quality": 0.5}
    # The automatic publish of a week a person edited is still that person's answer.
    assert sv.record_week_outcome(rid, hid, actor={"role": "automation"}, published=True, db_path=db)
    assert _q(db, "SELECT outcome FROM ai_runs WHERE run_id=?", (f"run:sched:{hid}",))[0]["outcome"] == "edited"


def test_an_unlooked_at_automatic_publish_is_nobodys_answer(db):
    import schedule_versions as sv
    rid = create_restaurant(Restaurant(name="Auto Grill", owner_email="o@autogrill.test"), db_path=db)
    hid = _schedule_week(db, rid)
    assert sv.record_week_outcome(rid, hid, actor={"role": "automation"}, published=True, db_path=db)
    (row,) = _q(db, "SELECT outcome, outcome_quality FROM ai_runs WHERE run_id=?", (f"run:sched:{hid}",))
    assert row == {"outcome": "ignored", "outcome_quality": None}


def test_the_publish_and_the_save_file_the_weeks_outcome():
    import client_api
    import mobile_api
    assert "record_week_outcome(rid, schedule_id, actor=actor, published=True)" in \
        inspect.getsource(client_api._publish_schedule)
    assert "record_week_outcome(rid, saved, actor=current_user)" in inspect.getsource(mobile_api.mobile_score_schedule)


def test_a_rejected_recipe_draft_is_filed_on_its_run_and_a_photo_card_is_not(db):
    import recipes
    rid = create_restaurant(Restaurant(name="Dish Grill", owner_email="o@dishgrill.test"), db_path=db)
    did = _x(db, "INSERT INTO recipe_drafts (restaurant_id, menu_item_id, menu_item_name, lines_json) "
                 "VALUES (?, 42, 'Tacos', '[]')", (rid,))
    run = orch.generate("recipe_draft", rid, attempt=lambda r, n: "{}", subject="recipe:42")
    assert recipes.reject(rid, did)["ok"]
    assert _q(db, "SELECT outcome FROM ai_runs WHERE run_id=?", (run.run_id,))[0]["outcome"] == "rejected"
    photo = {"menu_item_id": 42, "image_sha": "abc"}
    assert recipes._file_draft_outcome(rid, photo, "accepted") is False


def test_a_calendar_idea_written_from_is_accepted_and_survives_a_regenerate(db):
    import marketing
    rid = create_restaurant(Restaurant(name="Cal Grill", owner_email="o@calgrill.test"), db_path=db)
    run = orch.generate("content_calendar", rid, attempt=lambda r, n: [{"day": "Sunday"}],
                        subject="calendar:2026-10-11")
    marketing.mark_calendar_idea_used(rid, "instagram_post", "Taco Tuesday")
    assert orch.record_latest_outcome("content_calendar", rid, "rejected", subject_prefix="calendar:",
                                      only_unfiled=True) is False
    assert _q(db, "SELECT outcome FROM ai_runs WHERE run_id=?", (run.run_id,))[0]["outcome"] == "accepted"
    src = inspect.getsource(marketing.get_content_calendar_ideas)
    assert 'record_latest_outcome("content_calendar", restaurant_id, "rejected"' in src and "only_unfiled=True" in src


def test_staff_answers_and_invoices_have_no_outcome_hook_by_decision():
    # staff_answer: no owner or staff feedback on an answer exists to read;
    # invoice_extract belongs to the invoices fix round. Neither files one.
    src = open(os.path.join(ROOT, "staff_knowledge.py"), encoding="utf-8").read()
    assert 'record_outcome("staff_answer"' not in src


# ── #7 ─────────────────────────────────────────────────────────────────────

def test_a_ladder_the_router_cannot_run_is_refused(db):
    base = wf.POLICIES["labor_insight"]
    for bad in (["T0", "T1"], ["T1", "T1"], ["T5"]):
        with pytest.raises(ValueError):
            wf.apply_override(base, {"ladder": bad})
    with pytest.raises(ValueError):
        wf.apply_override(wf.POLICIES["ai_review"], {"ladder": ["default"]})
    assert wf.apply_override(wf.POLICIES["invoice_extract"], {"ladder": ["default", "T4"]}).ladder == ("default", "T4")
    # One stored before the validator tightened falls back to the defaults.
    _x(db, "INSERT INTO ai_route_overrides (workflow, override_json) VALUES ('labor_insight', ?)",
       (json.dumps({"ladder": ["T0", "T1"]}),))
    orch._OVERRIDES.clear()
    assert wf.policy("labor_insight", db).ladder == ("T1", "T2")
    assert wf.route_for(wf.policy("ai_review"), 0).model


def test_a_rung_with_no_model_is_recorded_as_an_error_run(db, monkeypatch):
    real = wf.policy
    monkeypatch.setattr(wf, "policy", lambda w, d=None: replace(real(w, d), ladder=("T0",)))
    with pytest.raises(ValueError):
        orch.generate("labor_insight", 7, attempt=lambda r, n: pytest.fail("called with no model"))
    (row,) = _q(db, "SELECT status, steps_json FROM ai_runs")
    assert row["status"] == "error" and "route_error" in row["steps_json"]


# ── #8 ─────────────────────────────────────────────────────────────────────

def test_a_flagged_guest_text_is_refused_at_the_send_and_the_verdict_is_kept(db, monkeypatch):
    import guest_marketing as gm
    rid = create_restaurant(Restaurant(name="Gate Grill", owner_email="o@gategrill.test"), db_path=db)
    asked = []

    def flag(kind, text, restaurant_id, context):
        asked.append((kind, text))
        return orch.Verdict(ok=False, trigger="reviewer_flag", reasons=["fake urgency"], score=0.3, label="flag")
    monkeypatch.setattr(ai_reviewer, "_gate_review", flag)
    seg, refused = gm.prepare_campaign(rid, "LAST CHANCE!!! Come in NOW or miss out forever", "all", db_path=db)
    assert seg == "all" and refused["blocked"] == "gate_flagged" and "fake urgency" in refused["error"]
    assert asked == [("guest_text", "LAST CHANCE!!! Come in NOW or miss out forever")]
    # The phone's pre-check, then its send: the same text is not read twice.
    gm.prepare_campaign(rid, "LAST CHANCE!!!  Come in NOW or miss out forever", "all", db_path=db)
    assert len(asked) == 1
    assert gm.start_campaign(rid, "LAST CHANCE!!! Come in NOW or miss out forever", segment="all",
                             db_path=db)["blocked"] == "gate_flagged"


def test_a_gated_draft_sent_as_it_was_is_not_read_again(db, monkeypatch):
    monkeypatch.setattr(ai_reviewer, "_gate_review", lambda *a: pytest.fail("read a gated draft again"))
    parts = {"subject": "Fall menu", "preheader": "New dishes", "headline": "It's here", "body": "Come try it.",
             "button_label": "See the menu"}
    ai_reviewer.remember_verdict("guest_email", 9, parts, orch.Verdict(ok=True, score=0.9))
    assert ai_reviewer.gate_send("guest_email", 9, dict(parts, body="Come  try it.\n"), db_path=db).ok
    # An edited headline is a new text: read.
    monkeypatch.setattr(ai_reviewer, "_gate_review",
                        lambda kind, text, rid, ctx: orch.Verdict(ok=True, score=0.8, label="pass"))
    assert ai_reviewer.gate_send("guest_email", 9, dict(parts, headline="Fall is here"), db_path=db).score == 0.8
    # A reviewer that could not run passes and leaves no verdict to stand on.
    monkeypatch.setattr(ai_reviewer, "_gate_review",
                        lambda kind, text, rid, ctx: orch.Verdict.passed(label="reviewer_unavailable"))
    assert ai_reviewer.gate_send("guest_text", 9, "Soup's on", db_path=db).ok
    assert not _q(db, "SELECT 1 FROM ai_gate_verdicts WHERE kind='guest_text'")


def test_the_gate_reads_every_field_a_guest_reads_and_follows_the_policy(db, monkeypatch):
    seen = []
    monkeypatch.setattr(ai_reviewer, "review_text",
                        lambda kind, draft, **k: seen.append(draft) or orch.Verdict(ok=True, score=0.9))
    ai_reviewer.reviewer_for("guest_email", 9)({"subject": "S", "preheader": "P", "headline": "H", "body": "B",
                                                "button_label": "Go"}, "haiku_gate")
    assert seen == ["S\n\nP\n\nH\n\nB\n\nGo"]
    assert ai_reviewer._send_review("guest_text", "Hi", 9, "").score == 0.9
    orch.set_override("guest_campaign_draft", {"reviewer": "owner"}, db_path=db)
    monkeypatch.setattr(ai_reviewer, "_gate_review", lambda *a: pytest.fail("gated with the gate off"))
    assert ai_reviewer.gate_send("guest_text", 9, "Soup's on", db_path=db).label == "not_gated"


def test_the_newsletter_send_gates_its_final_fields_before_anything_is_recorded():
    import guest_email
    src = inspect.getsource(guest_email.send_newsletter)
    assert src.index("ai_reviewer.gate_send(\"guest_email\"") < src.index("INSERT INTO guest_newsletters")
    assert guest_email.send_parts("S", "B", {"preheader": "P", "headline": "H", "button_label": "Go"}) == \
        {"subject": "S", "preheader": "P", "headline": "H", "body": "B", "button_label": "Go"}
    assert "remember_verdict" in inspect.getsource(guest_email.draft_newsletter)


# ── #10 ────────────────────────────────────────────────────────────────────

def test_a_workflow_switched_off_makes_no_call_and_says_so(db, monkeypatch):
    monkeypatch.setenv("AI_WORKFLOW_OFF", "labor_insight, staff_brief")
    with pytest.raises(ai_utils.AIRefused) as e:
        orch.generate("labor_insight", 7, attempt=lambda r, n: pytest.fail("called a switched-off workflow"))
    assert isinstance(e.value, orch.WorkflowOff)
    assert _q(db, "SELECT status FROM ai_runs")[0]["status"] == "off"
    assert orch.generate("review_insight", 7, attempt=lambda r, n: "ok").ok


def test_the_policys_batch_flag_gates_batching(db, monkeypatch):
    monkeypatch.setattr(ai_batches, "_scheduling_allowed", lambda: True)
    assert ai_batches.enabled("competitor_insight") and ai_batches.enabled("quiet_night_post")
    orch.set_override("competitor_insight", {"batch": False}, db_path=db)
    orch.set_override("marketing_content", {"batch": False}, db_path=db)
    assert not ai_batches.enabled("competitor_insight")
    assert not ai_batches.enabled("quiet_night_post"), "the quiet-night post reads marketing_content's flag"
    assert ai_batches.enabled("shadow_arms") and ai_batches.enabled("dsr_narrative")


# ── #11 ────────────────────────────────────────────────────────────────────

def test_a_run_whose_caller_served_its_fallback_is_refiled(db):
    rr = _run_through_ledger("email_personalization", "Your first week went well.")
    (call,) = _q(db, "SELECT call_id FROM ai_usage WHERE correlation_id=?", (rr.run_id,))
    assert _q(db, "SELECT status FROM ai_runs WHERE run_id=?", (rr.run_id,))[0]["status"] == "ok"
    ai_utils.record_quality_event("email_personalization", "fallback", restaurant_id=7,
                                  action="email_personalization", call_id=call["call_id"],
                                  detail="refused by validation")
    (row,) = _q(db, "SELECT status, verdict, reasons FROM ai_runs WHERE run_id=?", (rr.run_id,))
    assert row["status"] == "failed" and row["verdict"] == "fallback" and "refused by validation" in row["reasons"]


def test_an_unusable_answer_refiles_its_run_and_never_another(db):
    rr = _run_through_ledger("recipe_draft", "not json")
    (call,) = _q(db, "SELECT call_id FROM ai_usage WHERE correlation_id=?", (rr.run_id,))
    other = _run_through_ledger("staff_brief", "Busy night ahead.")
    # A call of another kind never re-files a run.
    _x(db, "UPDATE ai_usage SET correlation_id=? WHERE call_id=?", (other.run_id, call["call_id"]))
    assert not orch.mark_fallback(call["call_id"])
    _x(db, "UPDATE ai_usage SET correlation_id=? WHERE call_id=?", (rr.run_id, call["call_id"]))
    ai_utils.mark_outcome(call["call_id"], "unparseable", reason="recipe draft was not JSON")
    assert _q(db, "SELECT verdict FROM ai_runs WHERE run_id=?", (rr.run_id,))[0]["verdict"] == "fallback"
    assert _q(db, "SELECT status FROM ai_runs WHERE run_id=?", (other.run_id,))[0]["status"] == "ok"


# ── #12 ────────────────────────────────────────────────────────────────────

def test_a_review_reply_run_carries_the_shadow_rubric():
    import drafter
    src = inspect.getsource(drafter.draft_response)
    assert 'review=_reviewer_for("review_reply", restaurant_id' in src
    assert wf.POLICIES["draft_response"].shadow_rate > 0
