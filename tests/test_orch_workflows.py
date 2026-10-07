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
    client_api._do_approve = lambda review_id, r, auto=False: (calls.append(review_id) or ({"ok": True}, 200))
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


def test_a_staff_answer_not_found_on_t1_is_read_again_on_t2(db, monkeypatch, staff_rules, reviewer):
    import staff_knowledge as sk
    seen = _model(monkeypatch, NOT_FOUND, FOUND)
    d = sk.answer(staff_rules, None, "Priya Shah", ["Server"], "Phone rules?", db_path=db)
    assert d["answered"] is True
    assert [k["model"] for k in seen] == [_tier("staff_answer"), _tier("staff_answer", 1)]
    assert "was not used: it found no line" in seen[1]["messages"][0]["content"][1]["text"]


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
