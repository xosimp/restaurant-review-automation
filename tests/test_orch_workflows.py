"""The drafting, staff, intel and document workflows on the orchestrator (AI
cost audit 10/7/26, orchestration Phases 3 and 5): each call's model comes
from its policy's route, a refusal climbs one rung with its reasons as
notes, the Haiku reviewer gates text no human reads first (and an
auto-approved reply it flags is never posted), and what the owner does
with a draft is filed on the run that wrote it (ai_runs.outcome).

No model is reached: every create_with_retry and every reviewer call is
stubbed."""
import inspect
import json
import sqlite3
import types

import pytest

import ai_orchestrator as orch
import ai_workflows as wf
import models
from models import Restaurant, Review, create_restaurant, get_restaurant, save_reviews, update_restaurant


def _tier(workflow, step=0, start=None):
    return wf.route_for(wf.POLICIES[workflow], step, start).model


T1 = _tier("draft_response", 0)
T2 = _tier("draft_response", 0, "T2")
T3 = _tier("marketing_content", 1)


def _msg(text, stop="end_turn"):
    return types.SimpleNamespace(content=[types.SimpleNamespace(type="text", text=text)], stop_reason=stop,
                                 _cavnar_call_id=None)


def _conn(db_path):
    c = sqlite3.connect(db_path)
    c.row_factory = sqlite3.Row
    return c


@pytest.fixture(autouse=True)
def _every_restaurant_on_the_canary(monkeypatch):
    """These tests hold each ladder as written; a canaried workflow starts on
    its cheap rung only for AI_CANARY_RESTAURANTS (context re-audit 10/7/26
    #3, held in tests/test_context_reaudit_1007.py), so every restaurant here
    is a canary restaurant."""
    monkeypatch.setenv("AI_CANARY_RESTAURANTS", "*")


@pytest.fixture
def db(db_path, monkeypatch):
    real = models.get_conn
    import auth
    import client_api
    import mobile_api
    import scheduler
    for mod in (models, scheduler, client_api, mobile_api, auth):
        monkeypatch.setattr(mod, "get_conn", lambda *a, **k: real(db_path), raising=False)
    monkeypatch.setattr(models, "DB_PATH", db_path)
    monkeypatch.setattr(auth, "DB_PATH", db_path)
    import guest_marketing
    guest_marketing.init_guest_marketing(db_path=db_path)
    orch._OVERRIDES.clear()
    monkeypatch.setattr(orch, "REPLAY_SAMPLE_RATE", 0.0)
    monkeypatch.setenv("AI_SHADOW_REVIEW", "0")
    return db_path


@pytest.fixture
def reviewer(monkeypatch):
    """The Haiku rubric's stand-in: each call pops the next verdict (a pass
    when none is left) and records what it read."""
    import ai_reviewer

    class _R:
        def __init__(self):
            self.verdicts, self.calls = [], []

        def __call__(self, kind, draft, restaurant_id=None, context="", mode="haiku_gate"):
            self.calls.append({"kind": kind, "draft": draft, "context": context, "mode": mode})
            return self.verdicts.pop(0) if self.verdicts else orch.Verdict(ok=True, score=0.9)
    r = _R()
    monkeypatch.setattr(ai_reviewer, "review_text", r)
    return r


def _flag(*reasons):
    return orch.Verdict(ok=False, trigger="reviewer_flag", reasons=list(reasons), score=0.3, label="flag")


def _restaurant(db, **cols):
    rid = create_restaurant(Restaurant(name=cols.pop("name", "Gia Mia"), owner_email="o@x.test"), db_path=db)
    cols.setdefault("billing_status", "active")
    update_restaurant(rid, cols, db_path=db)
    return rid


def _review(db, rid, ext, rating, text, draft=None, author="Sam Lee", **cols):
    save_reviews([Review(restaurant_id=rid, platform="google", external_id=ext, author=author, rating=rating,
                         text=text)], db_path=db)
    c = _conn(db)
    rev = c.execute("SELECT id FROM reviews WHERE external_id=? AND restaurant_id=?", (ext, rid)).fetchone()[0]
    sets = {"draft_response": draft, "response_status": "drafted" if draft else "pending", "processed": 1, **cols}
    c.execute("UPDATE reviews SET " + ", ".join(f"{k}=?" for k in sets) + " WHERE id=?", (*sets.values(), rev))
    c.commit()
    c.close()
    return rev


def _runs(db, workflow=None):
    c = _conn(db)
    try:
        q = "SELECT * FROM ai_runs" + (" WHERE workflow=?" if workflow else "") + " ORDER BY created_at, rowid"
        return [dict(r) for r in c.execute(q, (workflow,) if workflow else ())]
    finally:
        c.close()


# ── review replies: the pre-router, escalation, the unattended gate ─────────

@pytest.mark.parametrize("kw,tier", [
    (dict(rating=5, text="Loved the carbonara!", sentiment="positive"), "T1"),
    (dict(rating=4, text="Great night out.", sentiment="positive", severity="minor"), "T1"),
    (dict(rating=3, text="It was fine."), "T2"),
    (dict(rating=5, text="Lovely, but my son had an allergic reaction to the sauce."), "T2"),
    (dict(rating=5, text="Great", urgency="high"), "T2"),
    (dict(rating=5, text="Great", severity="safety"), "T2"),
    (dict(rating=4, text="Great", complaint="cold fries"), "T2"),
    (dict(rating=5, text="Great", entities=json.dumps({"staff_roles": ["server"]})), "T2"),
    (dict(rating=5, text="Great", sentiment="negative"), "T2"),
    (dict(rating=None, text="Great"), "T2"),
])
def test_the_reply_pre_router_starts_only_a_clean_4_or_5_star_review_on_t1(kw, tier):
    import drafter
    assert drafter.draft_start_tier(kw.pop("rating"), kw.pop("text"), **kw) == tier


def _drafts(monkeypatch, *texts):
    import drafter
    seen = []
    monkeypatch.setattr(drafter, "get_client", lambda *a, **k: object())

    def create(*a, **k):
        seen.append(k)
        return _msg(texts[min(len(seen), len(texts)) - 1])
    monkeypatch.setattr(drafter, "create_with_retry", create)
    return seen


def test_a_glowing_review_is_drafted_on_t1_and_a_low_one_on_t2(db, monkeypatch):
    import drafter
    rid = _restaurant(db)
    good = _review(db, rid, "g1", 5, "Loved the carbonara!")
    low = _review(db, rid, "g2", 2, "Cold pasta and a long wait.")
    seen = _drafts(monkeypatch, "Thank you so much, Sam! We're thrilled you loved the carbonara.")
    drafter.draft_response(good, 5, "Loved the carbonara!", "positive", "Gia Mia", restaurant_id=rid)
    drafter.draft_response(low, 2, "Cold pasta and a long wait.", "negative", "Gia Mia", restaurant_id=rid)
    assert [k["model"] for k in seen] == [T1, T2]
    assert all(k["action"] == "draft_response" for k in seen)
    subjects = [r["subject"] for r in _runs(db, "draft_response")]
    assert subjects == [f"review:{good}", f"review:{low}"]


def test_a_refused_t1_reply_is_written_again_on_t2_told_why(db, monkeypatch):
    import drafter
    rid = _restaurant(db)
    rev = _review(db, rid, "e1", 5, "Loved the carbonara!")
    seen = _drafts(monkeypatch, "Thanks Sam! We were voted the best pasta in Chicago, so we're thrilled.",
                   "Thank you so much, Sam! We're thrilled you loved the carbonara.")
    out = drafter.draft_response(rev, 5, "Loved the carbonara!", "positive", "Gia Mia", restaurant_id=rid)
    assert [k["model"] for k in seen] == [T1, T2]
    assert "held before anyone saw it" in seen[1]["messages"][0]["content"]
    assert "voted" in seen[1]["messages"][0]["content"]
    assert out.startswith("Thank you so much") and _conn(db).execute(
        "SELECT draft_needs_review FROM reviews WHERE id=?", (rev,)).fetchone()[0] == 0
    (run,) = _runs(db, "draft_response")
    assert run["escalations"] == 1 and run["final_tier"] == "T2" and run["status"] == "ok"


def test_a_reply_refused_on_both_rungs_is_kept_flagged_for_the_owner(db, monkeypatch):
    import drafter
    rid = _restaurant(db)
    rev = _review(db, rid, "e2", 5, "Loved the carbonara!")
    _drafts(monkeypatch, "Thanks Sam! We were voted the best pasta in Chicago.")
    drafter.draft_response(rev, 5, "Loved the carbonara!", "positive", "Gia Mia", restaurant_id=rid)
    row = _conn(db).execute("SELECT draft_needs_review, draft_review_reason FROM reviews WHERE id=?", (rev,)).fetchone()
    assert row["draft_needs_review"] == 1 and "voted" in row["draft_review_reason"]


def _auto(db, rid):
    import client_api
    import scheduler
    calls = []
    real = client_api._do_approve
    # **kw: the rule now passes the text its checks read (expected_draft,
    # re-audit #2).
    client_api._do_approve = lambda review_id, r, auto=False, **kw: (calls.append(review_id) or ({"ok": True}, 200))
    try:
        scheduler.auto_approve_five_stars(rid, get_restaurant(rid, db))
    finally:
        client_api._do_approve = real
    return calls


def test_an_auto_approved_reply_the_reviewer_flags_is_never_posted(db, reviewer):
    rid = _restaurant(db, auto_approve_5star=1, auto_approve_daily_cap=5)
    rev = _review(db, rid, "a1", 5, "Great night", "Thanks so much, Sam! See you soon.")
    orch.generate("draft_response", rid, lambda r, n: "x", subject=f"review:{rev}", db_path=db)
    reviewer.verdicts = [_flag("reads like a template")]
    assert _auto(db, rid) == []
    row = _conn(db).execute("SELECT draft_needs_review, draft_review_reason FROM reviews WHERE id=?", (rev,)).fetchone()
    assert row["draft_needs_review"] == 1
    assert row["draft_review_reason"].startswith("needs a read before it posts") and "template" in row["draft_review_reason"]
    assert reviewer.calls[0]["kind"] == "review_reply" and "Great night" in reviewer.calls[0]["context"]
    run = orch.subject_run("draft_response", rid, f"review:{rev}", db_path=db)
    assert run["reviewer"] == "haiku_gate" and run["reviewer_score"] == 0.3
    # Held means out of the candidates: the next pass neither reads nor posts it.
    assert _auto(db, rid) == [] and len(reviewer.calls) == 1


def test_an_auto_approved_reply_the_reviewer_passes_is_posted(db, reviewer):
    rid = _restaurant(db, auto_approve_5star=1, auto_approve_daily_cap=5)
    rev = _review(db, rid, "a2", 5, "Great night", "Thanks so much, Sam! See you soon.")
    assert _auto(db, rid) == [rev] and len(reviewer.calls) == 1


def test_the_auto_approve_rule_runs_the_gate_in_its_source():
    import scheduler
    src = inspect.getsource(scheduler.auto_approve_five_stars)
    assert src.index("gate_unattended_reply") < src.index("_do_approve(review_id")


# ── outcomes on review replies ─────────────────────────────────────────────

def _drafted_run(db, rid, rev):
    orch.generate("draft_response", rid, lambda r, n: "x", subject=f"review:{rev}", db_path=db)


def test_an_unedited_approval_is_accepted_and_an_edited_one_scored(db):
    import client_api
    rid = _restaurant(db)
    a = _review(db, rid, "o1", 5, "Great", "Thanks so much, Sam! See you soon.")
    b = _review(db, rid, "o2", 5, "Great", "Thanks so much, Sam! See you soon, and bring friends.",
                original_draft="Thanks so much, Sam! See you soon.", draft_edited=1)
    for rev in (a, b):
        _drafted_run(db, rid, rev)
        payload, status = client_api._do_approve(rev, rid, auto=False)
        assert status == 200 and payload["ok"], payload
    ra = orch.subject_run("draft_response", rid, f"review:{a}", db_path=db)
    rb = orch.subject_run("draft_response", rid, f"review:{b}", db_path=db)
    assert ra["outcome"] == "accepted" and ra["outcome_quality"] == 1.0
    assert rb["outcome"] == "edited" and 0.5 < rb["outcome_quality"] < 1.0


def test_a_skipped_draft_is_rejected_and_a_bulk_publish_says_nothing_about_quality(db):
    import client_api
    rid = _restaurant(db)
    s = _review(db, rid, "o3", 5, "Great", "Thanks so much, Sam!")
    b = _review(db, rid, "o4", 5, "Great", "Thanks so much, Sam! See you soon.")
    for rev in (s, b):
        _drafted_run(db, rid, rev)
    assert client_api._do_skip(s, rid)[1] == 200
    client_api._do_approve(b, rid, auto=False, bulk=True)
    assert orch.subject_run("draft_response", rid, f"review:{s}", db_path=db)["outcome"] == "rejected"
    rb = orch.subject_run("draft_response", rid, f"review:{b}", db_path=db)
    assert rb["outcome"] == "ignored" and rb["outcome_quality"] is None


def test_a_regenerated_draft_rejects_the_run_it_replaces(db, monkeypatch):
    import client_api
    rid = _restaurant(db)
    rev = _review(db, rid, "o5", 5, "Loved the carbonara!", "Thanks so much, Sam!")
    _drafted_run(db, rid, rev)
    old = orch.subject_run("draft_response", rid, f"review:{rev}", db_path=db)["run_id"]
    _drafts(monkeypatch, "Thank you so much, Sam! We're thrilled you loved the carbonara.")
    payload, status = client_api._do_regenerate_draft(rev, rid)
    assert payload["ok"], payload
    runs = {r["run_id"]: r for r in _runs(db, "draft_response")}
    assert runs[old]["outcome"] == "rejected" and runs[old]["outcome_detail"] == "regenerated"
    assert [r for r in runs.values() if r["run_id"] != old][0]["outcome"] is None


# ── marketing: the post, the calendar, guest texts and email ────────────────

def test_a_refused_post_is_written_again_one_tier_up_told_why(db, monkeypatch):
    import marketing
    rid = _restaurant(db)
    drafts = ["Our famous meatballs, all week long! #GiaMia", "Meatballs, all week long! #GiaMia"]
    seen = []
    monkeypatch.setattr(marketing, "get_client", lambda *a, **k: object())
    monkeypatch.setattr(marketing, "create_with_retry", lambda *a, **k: seen.append(k) or _msg(drafts[len(seen) - 1]))
    monkeypatch.setattr(marketing, "extract_text", lambda m: m.content[0].text)
    out = marketing.generate_content("instagram_post", "fall menu", restaurant_id=rid)
    assert out.startswith("Meatballs")
    assert [k["model"] for k in seen] == [_tier("marketing_content"), T3]
    assert seen[1]["thinking"] == {"type": "adaptive"} and seen[1]["output_config"]["effort"] == "medium"
    assert seen[1]["max_tokens"] >= wf.THINKING_MIN_MAX_TOKENS        # room to think, then write
    assert "rejected before anyone saw it" in seen[1]["messages"][0]["content"]
    run = orch.subject_run("marketing_content", rid, f"mkt_draft:{out.draft_ref}", db_path=db)
    assert run and run["escalations"] == 1


def test_a_post_published_as_drafted_is_accepted_and_a_regenerated_one_rejected(db):
    import marketing_voice as mv
    rid = _restaurant(db)
    r1 = orch.generate("marketing_content", rid, lambda r, n: "a", db_path=db).run_id
    d1 = mv.record_draft(rid, "social", "Meatballs all week! #GiaMia", "post", user_id=7, db_path=db, run_id=r1)
    r2 = orch.generate("marketing_content", rid, lambda r, n: "b", db_path=db).run_id
    d2 = mv.record_draft(rid, "social", "Fresh meatballs, every night. #GiaMia", "post", user_id=7, db_path=db,
                         run_id=r2)
    first = orch.subject_run("marketing_content", rid, mv.draft_subject(d1), db_path=db)
    assert first["run_id"] == r1 and first["outcome"] == "rejected" and first["outcome_detail"] == "regenerated"
    mv.record_final(rid, "social", "Fresh meatballs, every night. #GiaMia", "post_publish", ref_id=1,
                    user={"id": 7, "role": "owner"}, draft_id=d2, db_path=db)
    second = orch.subject_run("marketing_content", rid, mv.draft_subject(d2), db_path=db)
    assert second["outcome"] == "accepted" and second["outcome_quality"] == 1.0


def test_a_rewritten_guest_text_is_scored_by_what_survived(db):
    import marketing_voice as mv
    rid = _restaurant(db)
    run_id = orch.generate("guest_campaign_draft", rid, lambda r, n: "a", db_path=db).run_id
    d = mv.record_draft(rid, "text", "Pasta night is on Thursday. Come by!", "campaign_draft", user_id=7,
                        db_path=db, run_id=run_id)
    mv.record_final(rid, "text", "Trivia at 8 tonight, see you there", "campaign", ref_id=3,
                    user={"id": 7, "role": "owner"}, draft_id=d, db_path=db)
    run = orch.subject_run("guest_campaign_draft", rid, mv.draft_subject(d), db_path=db)
    assert run["outcome"] == "edited" and run["outcome_quality"] < 0.5


WEEK = ("Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday")


def test_the_calendar_asks_for_all_seven_days_and_refills_a_refused_one_on_t3(db, monkeypatch):
    import marketing
    rid = _restaurant(db)
    week = {d: {"platform": "Google", "angle": f"{d} pasta special close-up", "type": "google_promo"} for d in WEEK}
    week["Monday"]["angle"] = "Voted the #1 pizza in town three years running"
    fill = {"Monday": {"platform": "Google", "angle": "Monday meatball close-up", "type": "google_promo"}}
    seen = []
    monkeypatch.setattr(marketing, "get_client", lambda *a, **k: object())
    monkeypatch.setattr(marketing, "create_with_retry",
                        lambda *a, **k: seen.append(k) or _msg(json.dumps(week if len(seen) == 1 else fill)))
    monkeypatch.setattr(marketing, "extract_text", lambda m: m.content[0].text)
    ideas = marketing.get_content_calendar_ideas(restaurant_id=rid, force=True)
    schema = seen[0]["output_config"]["format"]["schema"]
    assert sorted(schema["required"]) == sorted(WEEK) and schema["additionalProperties"] is False
    assert seen[0]["model"] == _tier("content_calendar")
    # The fill: only the refused day, one tier up, told why.
    assert len(seen) == 2 and seen[1]["model"] == _tier("content_calendar", 1)
    assert seen[1]["output_config"]["format"]["schema"]["required"] == ["Monday"]
    assert "refused before anyone saw them" in seen[1]["messages"][0]["content"]
    assert [i["day"] for i in ideas] == list(WEEK)
    assert next(i for i in ideas if i["day"] == "Monday")["angle"] == "Monday meatball close-up"


def test_a_whole_clean_week_makes_one_call(db, monkeypatch):
    import marketing
    rid = _restaurant(db)
    week = {d: {"platform": "Google", "angle": f"{d} pasta special close-up", "type": "google_promo"} for d in WEEK}
    seen = []
    monkeypatch.setattr(marketing, "get_client", lambda *a, **k: object())
    monkeypatch.setattr(marketing, "create_with_retry", lambda *a, **k: seen.append(k) or _msg(json.dumps(week)))
    monkeypatch.setattr(marketing, "extract_text", lambda m: m.content[0].text)
    assert len(marketing.get_content_calendar_ideas(restaurant_id=rid, force=True)) == 7 and len(seen) == 1


def _guest_text(monkeypatch, texts):
    import guest_marketing as gm
    import marketing
    seen = []

    def once(client, prompt, restaurant, topic, goal, p, budget, data_health, route=None):
        seen.append((route.tier, prompt))
        t = texts[min(len(seen), len(texts)) - 1]
        if t.startswith("campaign copy rejected"):
            raise ValueError(t)
        return t
    monkeypatch.setattr(gm, "_draft_campaign_text_once", once)
    monkeypatch.setattr(gm, "get_client", lambda *a, **k: None)
    monkeypatch.setattr(marketing, "get_profile_for_restaurant",
                        lambda *a, **k: dict(marketing.DEFAULT_PROFILE, name="Simple EJ's"))
    return seen


def test_a_guest_text_the_reviewer_flags_is_written_again_then_refused_with_the_flags(db, monkeypatch, reviewer):
    import guest_marketing as gm
    seen = _guest_text(monkeypatch, ["Simple EJ's: BIG DEALS!!! Hurry!", "Simple EJ's: HUGE DEALS!!! Hurry!"])
    reviewer.verdicts = [_flag("reads like an ad"), _flag("fake urgency")]
    r = types.SimpleNamespace(id=1, name="Simple EJ's", sign_off_name=None)
    with pytest.raises(ValueError, match="campaign copy rejected: fake urgency"):
        gm.draft_campaign_message(r, goal="Fill Thursday dinner")
    assert [t for t, _p in seen] == ["T2", "T3"]
    assert "Your previous draft was not used: reads like an ad" in seen[1][1]
    assert reviewer.calls[0]["kind"] == "guest_text" and "Fill Thursday dinner" in reviewer.calls[0]["context"]


def test_a_guest_text_flagged_once_and_passed_after_is_the_owners(db, monkeypatch, reviewer):
    import guest_marketing as gm
    _guest_text(monkeypatch, ["Simple EJ's: BIG DEALS!!!", "Simple EJ's: carving night is Thursday."])
    reviewer.verdicts = [_flag("reads like an ad")]
    r = types.SimpleNamespace(id=1, name="Simple EJ's", sign_off_name=None)
    assert gm.draft_campaign_message(r, goal="pumpkin carving") == "Simple EJ's: carving night is Thursday."


def test_a_refused_newsletter_is_written_again_before_it_is_a_422(db, monkeypatch, reviewer):
    import guest_email as ge
    rid = _restaurant(db)
    good = {"subject": "Pull up a chair", "preheader": "Your table is ready", "headline": "Come see us",
            "body": "The kitchen is on.\n\nCome sit with us this week.", "button": "Book a table"}
    replies = [dict(good, body="Enjoy 20% off all week."), good]
    seen = []
    monkeypatch.setattr(ge, "get_client", lambda *a, **k: object())
    monkeypatch.setattr(ge, "create_with_retry", lambda *a, **k: seen.append(k) or _msg(json.dumps(replies[len(seen) - 1])))
    out = ge.draft_newsletter(get_restaurant(rid, db_path=db), goal="Fill Tuesday lunch")
    assert out["body"].startswith("The kitchen is on") and out.run_id
    assert [k["model"] for k in seen] == [_tier("guest_newsletter_draft"), _tier("guest_newsletter_draft", 1)]
    assert "Your previous draft was not used: it offers" in seen[1]["messages"][0]["content"]
    assert reviewer.calls and reviewer.calls[-1]["kind"] == "guest_email"


# ── staff answers: escalation, the gate, the cache ──────────────────────────

@pytest.fixture
def staff_rules(db, monkeypatch):
    import staff_brief
    import staff_knowledge as sk
    sk.clear_answer_cache()
    monkeypatch.setattr(staff_brief, "roster_names", lambda *a, **k: [])
    rid = _restaurant(db)
    sk.save_doc(rid, {"id": None}, "house_rules", "House rules",
                "Phones stay in the locker during service.\nShift meal is after close, from the staff menu.",
                db_path=db)
    yield rid
    sk.clear_answer_cache()


def _model(monkeypatch, *replies):
    import ai_utils
    seen = []

    def create(client, **k):
        seen.append(k)
        if k.get("action") != "staff_answer":
            raise AssertionError("only the answer is a model call here")
        return _msg(replies[min(len(seen), len(replies)) - 1])
    monkeypatch.setattr(ai_utils, "create_with_retry", create)
    monkeypatch.setattr(ai_utils, "get_client", lambda *a, **k: object())
    return seen


FOUND = json.dumps({"found": True, "answer": "Phones stay in the locker during service. [S1]", "sources": ["S1"]})
NOT_FOUND = json.dumps({"found": False, "answer": "", "sources": []})


def test_a_staff_answer_starts_on_t1_with_the_sources_cached_and_the_question_last(db, monkeypatch, staff_rules,
                                                                                    reviewer):
    import staff_knowledge as sk
    seen = _model(monkeypatch, FOUND)
    d = sk.answer(staff_rules, None, "Priya Shah", ["Server"], "Can I keep my phone on me?", db_path=db)
    assert d["answered"] is True and d["answer"] == "Phones stay in the locker during service."
    assert seen[0]["model"] == _tier("staff_answer")
    first, last = seen[0]["messages"][0]["content"]
    assert first["cache_control"] == {"type": "ephemeral"} and "[S1]" in first["text"]
    assert "<question>Can I keep my phone on me?</question>" in last["text"] and "cache_control" not in last
    assert reviewer.calls[0]["kind"] == "staff_answer" and "Phones stay" in reviewer.calls[0]["context"]


def test_the_same_question_about_the_same_rules_is_answered_from_the_cache(db, monkeypatch, staff_rules, reviewer):
    import staff_knowledge as sk
    seen = _model(monkeypatch, FOUND)
    a = sk.answer(staff_rules, None, "Priya Shah", ["Server"], "Can I keep my phone on me?", db_path=db)
    b = sk.answer(staff_rules, None, "Jake Moss", ["Server"], "  can i keep my PHONE on me ", db_path=db)
    assert a == b and len(seen) == 1
    # An edited rule is a new key: asked again, answered again.
    sk.save_doc(staff_rules, {"id": None}, "house_rules", "More rules", "Aprons go in the bin by the door.",
                db_path=db)
    sk.answer(staff_rules, None, "Priya Shah", ["Server"], "Can I keep my phone on me?", db_path=db)
    assert len(seen) == 2


def test_a_staff_answer_not_covered_on_t1_is_final_and_kept_for_an_hour(db, monkeypatch, staff_rules, reviewer):
    """AI cost audit 10/7/26 re-audit #3 — deliberately changed: it used to
    be read again on T2. "The lines don't cover it" is missing data, not a
    weak model: Sonnet reading the same lines cannot add the rule that is
    not there, so every uncovered question cost Haiku and Sonnet."""
    import staff_knowledge as sk
    seen = _model(monkeypatch, NOT_FOUND, FOUND)
    d = sk.answer(staff_rules, None, "Priya Shah", ["Server"], "Where do I park?", db_path=db)
    assert d["answered"] is False and d["reason"] == "not_found" and d["answer"] == sk.ASK_REFUSAL
    assert [k["model"] for k in seen] == [_tier("staff_answer")]
    # The same question about the same lines is answered from the cache...
    again = sk.answer(staff_rules, None, "Jake Moss", ["Server"], "where do i park", db_path=db)
    assert again == d and len(seen) == 1
    # ...for REFUSAL_CACHE_SECONDS, not the day a shown answer is kept.
    key = next(iter(sk._ANSWER_CACHE))
    at, payload, ttl = sk._ANSWER_CACHE[key]
    assert ttl == sk.REFUSAL_CACHE_SECONDS == 3600
    sk._ANSWER_CACHE[key] = (at - ttl - 1, payload, ttl)
    assert sk.answer(staff_rules, None, "Priya Shah", ["Server"], "Where do I park?", db_path=db)["answered"] is True
    assert len(seen) == 2


def test_a_staff_answer_that_cites_no_line_is_read_again_on_t2(db, monkeypatch, staff_rules, reviewer):
    import staff_knowledge as sk
    uncited = json.dumps({"found": True, "answer": "Phones stay in the locker.", "sources": []})
    seen = _model(monkeypatch, uncited, FOUND)
    d = sk.answer(staff_rules, None, "Priya Shah", ["Server"], "Phone rules?", db_path=db)
    assert d["answered"] is True
    assert [k["model"] for k in seen] == [_tier("staff_answer"), _tier("staff_answer", 1)]
    assert "was not used: it cited no line" in seen[1]["messages"][0]["content"][1]["text"]


def test_the_staff_answer_policy_never_escalates_on_missing_coverage():
    import ai_workflows as wf
    pol = wf.POLICIES["staff_answer"]
    assert "uncited" in pol.escalate_on and "not_covered" not in pol.escalate_on
    assert "not_covered" in wf.NEVER_TRIGGERS and "not_found" not in wf.TRIGGERS
    # A console override stored under the trigger's old name means the citation check.
    assert wf.apply_override(pol, {"escalate_on": ["not_found", "reviewer_flag"]}).escalate_on == \
        ("uncited", "reviewer_flag")


def test_a_staff_answer_the_reviewer_flags_twice_says_ask_your_manager(db, monkeypatch, staff_rules, reviewer):
    import staff_knowledge as sk
    seen = _model(monkeypatch, FOUND, FOUND)
    reviewer.verdicts = [_flag("doesn't answer the question"), _flag("doesn't answer the question")]
    d = sk.answer(staff_rules, None, "Priya Shah", ["Server"], "Phone rules?", db_path=db)
    assert d["answered"] is False and d["reason"] == "unchecked" and d["answer"] == sk.ASK_REFUSAL
    assert len(seen) == 2
    # A refused answer is never cached.
    _model(monkeypatch, FOUND)
    reviewer.verdicts = []
    assert sk.answer(staff_rules, None, "Priya Shah", ["Server"], "Phone rules?", db_path=db)["answered"] is True


# ── task sheets: the starter's ladder, its outcome, its rate limit ──────────

def test_the_starter_escalates_an_unreadable_draft_and_scores_the_lines_kept(db, monkeypatch):
    import ai_utils
    import task_sheets as ts
    rid = _restaurant(db)
    s = ts.create_sheet(rid, "Bartender", "opening", db_path=db)
    replies = ["Sorry, here are some ideas.",
               json.dumps([{"label": "Stock the ice well", "proof": "none"},
                           {"label": "Cut fruit for garnish", "proof": "none"}])]
    seen = []
    monkeypatch.setattr(ai_utils, "get_client", lambda *a, **k: None)
    monkeypatch.setattr(ai_utils, "create_with_retry", lambda *a, **k: seen.append(k) or _msg(replies[len(seen) - 1]))
    lines = ts.starter_lines(rid, "Bartender", "opening")
    assert [l["label"] for l in lines] == ["Stock the ice well", "Cut fruit for garnish"]
    assert [k["model"] for k in seen] == [_tier("task_sheet_starter"), _tier("task_sheet_starter", 1)]
    ts.add_line(rid, s["id"], {"label": "Stock the ice well"}, db_path=db)
    run = orch.subject_run("task_sheet_starter", rid, ts.starter_subject("Bartender", "opening"), db_path=db)
    assert run["outcome"] == "edited" and run["outcome_quality"] == 0.5
    ts.add_line(rid, s["id"], {"label": "Cut fruit for garnish"}, db_path=db)
    run = orch.subject_run("task_sheet_starter", rid, ts.starter_subject("Bartender", "opening"), db_path=db)
    assert run["outcome"] == "accepted" and run["outcome_quality"] == 1.0


def test_the_starter_route_is_rate_limited(db, monkeypatch):
    """#88: the one drafting route without an AI rate limit. The web twin
    runs the mobile body (client_api._m), so one guard covers both."""
    import ai_utils
    import client_api
    import mobile_api
    from flask import Flask
    assert "ai_rate_limited" in inspect.getsource(mobile_api.mobile_task_sheet_starter)
    assert "mobile_task_sheet_starter" in inspect.getsource(client_api.task_sheets_starter)
    monkeypatch.setattr(mobile_api, "_may_manage_team", lambda u: True)
    monkeypatch.setattr(ai_utils, "ai_rate_limited", lambda *a, **k: True)
    fn = getattr(mobile_api.mobile_task_sheet_starter, "__wrapped__", mobile_api.mobile_task_sheet_starter)
    with Flask(__name__).test_request_context(method="POST"):
        resp, status = fn(1, current_user={"restaurant_id": 1, "id": 1, "role": "owner"})
    assert status == 429 and resp.get_json()["ok"] is False


# ── every migrated site takes its model from its route ──────────────────────

MIGRATED = {
    "marketing.py": ("marketing_content", "content_calendar"),
    "guest_marketing.py": ("guest_campaign_draft",),
    "guest_email.py": ("guest_newsletter_draft",),
    "drafter.py": ("draft_response",),
    "staff_knowledge.py": ("staff_answer", "staff_translation"),
    "staff_brief.py": ("staff_brief",),
    "task_sheets.py": ("task_sheet_starter",),
    "competitor.py": ("competitor_insight", "menu_extract_url", "menu_extract_pdf"),
    "reporter.py": ("weekly_digest",),
    "emails.py": ("email_personalization",),
    "recipes.py": ("recipe_draft", "recipe_photo"),
    "invoices.py": ("invoice_extract",),
    "analyser.py": ("review_analysis",),
}


@pytest.mark.parametrize("module", sorted(MIGRATED))
def test_every_migrated_call_takes_its_model_from_the_route(module):
    """Read from the source: each create_with_retry call in these modules
    passes its model through route.apply (never a bare model=), and each
    workflow is generated by the orchestrator in that module."""
    import os
    import re
    src = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), module),
               encoding="utf-8").read()
    for m in re.finditer(r"create_with_retry\(", src):
        if src[m.start() - 5:m.start()] in ("from ", "port "):
            continue
        window = src[m.end():m.end() + 1400]
        call = window[:window.find("\n\n")] if "\n\n" in window else window
        assert "route.apply(" in call, f"{module}: a create_with_retry call does not take its route"
    for workflow in MIGRATED[module]:
        assert re.search(r"generate\(\s*\"" + workflow + "\"", src), f"{module}: {workflow} is not orchestrated"


# ── AI cost audit 10/7/26, the blind re-audit of these workflows ────────────

def _rubric_answers(monkeypatch, answer=None, raises=None):
    """The real ai_reviewer.review_text on a stubbed model: `answer` is its
    JSON reply, `raises` an exception the call raises instead."""
    import ai_utils
    calls = []

    def create(client, **k):
        calls.append(k)
        if raises is not None:
            raise raises
        return _msg(json.dumps(answer))
    monkeypatch.setattr(ai_utils, "create_with_retry", create)
    monkeypatch.setattr(ai_utils, "get_client", lambda *a, **k: object())
    return calls


def test_the_gate_fails_a_low_score_with_no_flags_and_shadow_only_scores(db, monkeypatch):
    """#1: a 0.15 with no flags passed every gate — the reviewer saying the
    text is bad without saying why. In gate mode it now fails with a
    generic reason; in shadow mode it is only a score."""
    import ai_reviewer
    _rubric_answers(monkeypatch, {"score": 0.15, "flags": []})
    gate = ai_reviewer.review_text("review_reply", "Thanks!", restaurant_id=1, mode="haiku_gate")
    assert gate.ok is False and gate.trigger == "reviewer_flag" and gate.reasons == [ai_reviewer.LOW_SCORE_REASON]
    shadow = ai_reviewer.review_text("review_reply", "Thanks!", restaurant_id=1, mode="haiku_shadow")
    assert shadow.ok is True and shadow.score == 0.15
    # A good score passes in both; a flag with a low score fails in both, as before.
    _rubric_answers(monkeypatch, {"score": 0.9, "flags": []})
    assert ai_reviewer.review_text("guest_text", "x", mode="haiku_gate").ok is True
    _rubric_answers(monkeypatch, {"score": 0.3, "flags": ["reads like an ad"]})
    assert ai_reviewer.review_text("guest_text", "x", mode="haiku_shadow").ok is False


def test_an_unattended_reply_scored_low_with_no_flags_is_held(db, monkeypatch):
    rid = _restaurant(db, auto_approve_5star=1, auto_approve_daily_cap=5)
    rev = _review(db, rid, "q1", 5, "Great night", "Thanks so much, Sam! See you soon.")
    _rubric_answers(monkeypatch, {"score": 0.2, "flags": []})
    assert _auto(db, rid) == []
    row = _conn(db).execute("SELECT draft_needs_review, draft_review_reason FROM reviews WHERE id=?", (rev,)).fetchone()
    assert row["draft_needs_review"] == 1 and row["draft_review_reason"].startswith("needs a read before it posts")


def test_an_unattended_reply_the_gate_could_not_read_is_held_not_posted(db, monkeypatch):
    """#1: a reviewer that cannot run (budget spent) passed, so the AI budget
    running out meant public replies went out with no gate. Nobody waits on
    this reply: it is held for the owner instead."""
    import ai_utils
    rid = _restaurant(db, auto_approve_5star=1, auto_approve_daily_cap=5)
    rev = _review(db, rid, "q2", 5, "Great night", "Thanks so much, Sam! See you soon.")
    calls = _rubric_answers(monkeypatch, raises=ai_utils.AIBudgetExceeded("paused"))
    assert _auto(db, rid) == [] and len(calls) == 1
    row = _conn(db).execute("SELECT draft_needs_review, draft_review_reason FROM reviews WHERE id=?", (rev,)).fetchone()
    assert row["draft_needs_review"] == 1
    assert row["draft_review_reason"] == "needs a read before it posts: the automatic check couldn't read it"
    import drafter
    assert drafter.owner_reason(row["draft_review_reason"]).startswith("needs a read before it posts")


def test_the_rule_posts_only_the_text_its_checks_read(db, monkeypatch):
    """#2: the gate read one text and the approve posted whatever was stored.
    A draft rewritten after the checks read it is a 409, never posted."""
    import ai_reviewer
    import scheduler
    rid = _restaurant(db, auto_approve_5star=1, auto_approve_daily_cap=5)
    rev = _review(db, rid, "q3", 5, "Great night", "Thanks so much, Sam! See you soon.")

    def review_text(kind, draft, restaurant_id=None, context="", mode="haiku_gate"):
        # A regenerate lands between the gate reading the draft and the approve.
        c = _conn(db)
        c.execute("UPDATE reviews SET draft_response='Thanks Sam! Next round is on us.' WHERE id=?", (rev,))
        c.commit()
        c.close()
        return orch.Verdict(ok=True, score=0.9)
    monkeypatch.setattr(ai_reviewer, "review_text", review_text)
    assert scheduler.auto_approve_five_stars(rid, get_restaurant(rid, db)) == 0
    row = _conn(db).execute("SELECT response_status, draft_response FROM reviews WHERE id=?", (rev,)).fetchone()
    assert row["response_status"] == "drafted" and "Next round" in row["draft_response"]
    assert "expected_draft=draft_text" in inspect.getsource(scheduler.auto_approve_five_stars)


VIEW_AS = {"id": 3, "restaurant_id": None, "role": "client", "acting_admin_id": 99, "acting_admin": "will"}


def test_supports_skip_and_ask_rewrite_through_view_as_file_nothing_off_the_request(db):
    """#7a/#7b: Ask's tools run on the stream's worker thread, where there is
    no request to read view-as from — the login says who is acting."""
    import ask_cavnar_tools
    import client_api
    rid = _restaurant(db)
    who = dict(VIEW_AS, restaurant_id=rid)
    a = _review(db, rid, "v1", 5, "Great", "Thanks so much, Sam! See you soon.")
    b = _review(db, rid, "v2", 5, "Great", "Thanks so much, Sam! See you soon.")
    c = _review(db, rid, "v3", 5, "Great", "Thanks so much, Sam! See you soon.")
    for rev in (a, b, c):
        _drafted_run(db, rid, rev)
    viewer = types.SimpleNamespace(_ask_dsr_user=who)
    assert ask_cavnar_tools._skip_review(rid, review_id=a, _viewer=viewer)["ok"] is True
    assert ask_cavnar_tools._BY_NAME["skip_review"].get("wants_viewer") is True
    row = _conn(db).execute("SELECT response_action FROM reviews WHERE id=?", (a,)).fetchone()
    assert row["response_action"] == "support_skipped"
    assert orch.subject_run("draft_response", rid, f"review:{a}", db_path=db)["outcome"] is None
    payload, status = client_api._do_save_draft(b, rid, "Thank you, Sam! We loved having you.", by_model=True,
                                                user=who)
    assert status == 200 and payload["ok"], payload
    assert orch.subject_run("draft_response", rid, f"review:{b}", db_path=db)["outcome"] is None
    # The owner's own rewrite through Ask is still the owner turning it down.
    owner = {"id": 1, "restaurant_id": rid, "role": "client"}
    client_api._do_save_draft(c, rid, "Thank you, Sam! We loved having you.", by_model=True, user=owner)
    assert orch.subject_run("draft_response", rid, f"review:{c}", db_path=db)["outcome"] == "rejected"


def test_an_approval_of_a_reply_support_edited_is_ignored(db):
    """#7d: support's words through view-as approved later are not the
    owner's verdict on the model's draft."""
    import client_api
    rid = _restaurant(db)
    rev = _review(db, rid, "v4", 5, "Great", "Thanks so much, Sam! See you soon, and bring friends.",
                  original_draft="Thanks so much, Sam! See you soon.", draft_edited=1, draft_edited_via="view_as")
    _drafted_run(db, rid, rev)
    payload, status = client_api._do_approve(rev, rid, auto=False)
    assert status == 200 and payload["ok"], payload
    run = orch.subject_run("draft_response", rid, f"review:{rev}", db_path=db)
    assert run["outcome"] == "ignored" and run["outcome_quality"] is None


def test_a_starter_line_support_adds_through_view_as_files_nothing(db):
    """#7c: starter_outcome ran on every add_line, whoever added it."""
    import task_sheets as ts
    rid = _restaurant(db)
    s = ts.create_sheet(rid, "Bartender", "opening", db_path=db)
    subject = ts.starter_subject("Bartender", "opening")
    run_id = orch.generate("task_sheet_starter", rid, lambda r, n: {"lines": []}, subject=subject, db_path=db).run_id
    orch.attach(run_id, context={"offered": [ts._key("Stock the ice well")[:40], ts._key("Cut fruit")[:40]]},
                db_path=db)
    ts.add_line(rid, s["id"], {"label": "Stock the ice well"}, db_path=db, user=dict(VIEW_AS, restaurant_id=rid))
    assert orch.subject_run("task_sheet_starter", rid, subject, db_path=db)["outcome"] is None
    ts.add_line(rid, s["id"], {"label": "Cut fruit"}, db_path=db,
                user={"id": 1, "restaurant_id": rid, "role": "client"})
    assert orch.subject_run("task_sheet_starter", rid, subject, db_path=db)["outcome"] == "accepted"
    import mobile_api
    assert "user=current_user" in inspect.getsource(mobile_api.mobile_task_sheet_add_line)


def test_a_view_as_draft_on_the_job_pool_carries_no_person(db):
    """#4: the Studio drafts on the owner AI job pool, where flask.g says
    nothing — the login captured on the request thread does."""
    import marketing_voice as mv
    rid = _restaurant(db)
    d = mv.record_draft(rid, "text", "Pasta night is Thursday.", "campaign_draft", user_id=3,
                        user=dict(VIEW_AS, restaurant_id=rid), db_path=db)
    mine = mv.record_draft(rid, "text", "Trivia is Tuesday.", "campaign_draft", user_id=1,
                           user={"id": 1, "restaurant_id": rid, "role": "client"}, db_path=db)
    rows = {r["id"]: r["user_id"] for r in _conn(db).execute("SELECT id, user_id FROM marketing_model_drafts")}
    assert rows[d] is None and rows[mine] == 1
    # Every async caller hands the login down (read from the source: the
    # body runs on a pool thread).
    import client_api
    import mobile_api
    assert "user=dict(current_user)" in inspect.getsource(client_api.generate_content_answer)
    assert "user=current_user" in inspect.getsource(mobile_api.guest_campaign_draft_result)
    assert "user=current_user" in inspect.getsource(mobile_api.guest_newsletter_draft_result)


def test_an_approved_saved_draft_files_its_outcome(db):
    """#8: approve always passed the original, which skipped finding the
    model draft — no outcome was ever filed for a saved draft."""
    import marketing_drafts
    import marketing_voice as mv
    rid = _restaurant(db)
    run_id = orch.generate("marketing_content", rid, lambda r, n: "a", db_path=db).run_id
    ref = mv.record_draft(rid, "social", "Meatballs all week! #GiaMia", "job", db_path=db, run_id=run_id,
                          workflow="marketing_content")
    saved = marketing_drafts.save_draft(rid, "Meatballs all week, every night! #GiaMia",
                                        content_type="instagram_post", topic="Tuesday", draft_ref=ref, db_path=db)
    assert saved["ok"]
    out = marketing_drafts.approve_draft(saved["id"], rid, user={"id": 1, "role": "client", "restaurant_id": rid},
                                         db_path=db)
    assert out["ok"], out
    run = orch.subject_run("marketing_content", rid, mv.draft_subject(ref), db_path=db)
    assert run["outcome"] == "edited" and 0 < run["outcome_quality"] < 1
    import strategy_jobs
    assert inspect.getsource(strategy_jobs._quiet_night_post_now).count("draft_ref=getattr(body") == 1
    assert inspect.getsource(strategy_jobs.on_quiet_night_post).count("draft_ref=getattr(body") == 1


def test_a_content_tab_email_files_under_the_run_that_wrote_it(db):
    """#9: a weekly email from the Content tab is a marketing_content run on
    the email channel; its outcome went to guest_newsletter_draft."""
    import marketing_voice as mv
    rid = _restaurant(db)
    run_id = orch.generate("marketing_content", rid, lambda r, n: "a", db_path=db).run_id
    d = mv.record_draft(rid, "email", "Our fall menu is here. Come taste it.", "post", user_id=1, db_path=db,
                        run_id=run_id, workflow="marketing_content")
    mv.record_final(rid, "email", "Our fall menu is here. Come taste it.", "newsletter", ref_id=5,
                    user={"id": 1, "role": "client"}, draft_id=d, db_path=db)
    assert orch.subject_run("marketing_content", rid, mv.draft_subject(d), db_path=db)["outcome"] == "accepted"
    assert orch.subject_run("guest_newsletter_draft", rid, mv.draft_subject(d), db_path=db) is None
    import marketing
    assert 'workflow="marketing_content"' in inspect.getsource(marketing.generate_content)
