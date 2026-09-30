"""Memory audit 9/29/26, workstream M1 — decision memory as a model reads it.

  unfenced   the owner's reasons, managers' notes and model-written titles
             reach Ask's snapshot fenced (ai_guard.wrap_untrusted): they can
             neither verify a figure nor anchor a cause.
  proposed   Ask's "already proposed" list follows the action queue's rule
             (a ⌘K / Home preview is never "still open"; 7-day open window)
             and the viewer's (a manager reads only their own), in M/D/YY.
  relevance  memory is chosen by relevance, a "not for us" with a reason is
             never capped out, lines sharing a signature collapse, ALREADY
             ANSWERED is newest-first, and Ask's prose is checked against
             the declines.
"""
import os
import re

import pytest

import models
import rec_ledger as rl
from ai_guard import wrap_untrusted, _strip_untrusted
from models import Restaurant, create_restaurant

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@pytest.fixture(autouse=True)
def _db(db_path, monkeypatch):
    real = models.get_conn
    monkeypatch.setattr(models, "get_conn", lambda *a, **k: real(db_path))
    monkeypatch.setattr(models, "DB_PATH", db_path)
    yield


def _rid(db_path, name="Prompt Co"):
    return create_restaurant(Restaurant(name=name, owner_email=f"{name.split()[0].lower()}@x.test"), db_path=db_path)


def _sql(db_path, sql, *args):
    c = models.get_conn(db_path)
    c.execute(sql, args)
    c.commit()
    c.close()


# ── unfenced ─────────────────────────────────────────────────────────────────

def test_owner_words_in_the_decision_memory_verify_no_figure_and_anchor_no_cause(db_path):
    import decisions
    import ask_cavnar
    rid = _rid(db_path)
    rl.present(rid, "trim_day:Saturday", "labor", "home", title="Trim Saturday — worth roughly $689 on 9/14 alone",
               model_written=True, db_path=db_path)
    rl.record(rid, "trim_day:Saturday", "dismissed",
              meta={"kind": "not_for_us", "reason": "Saturdays are slow because of the Main St construction; "
                                                    "we already cut labor to 24%"}, db_path=db_path)
    ctx = decisions.context(rid, db_path=db_path)
    assert "construction" in ctx and "24%" in ctx and "$689" in ctx
    bare = _strip_untrusted(ctx)
    # Outside the fences: only the structure (the answer, the date).
    assert "construction" not in bare and "24%" not in bare and "689" not in bare
    assert "not for us" in bare
    anchors = ask_cavnar._cause_anchors([ctx])
    assert not [a for a in anchors if "construction" in a["text"]]
    from ai_guard import figure_claims
    assert not [c for c in figure_claims(ctx) if c["raw"].startswith(("24", "$689"))]


def test_a_resolution_note_is_fenced_too(db_path):
    import decisions
    import issues
    rid = _rid(db_path)
    issue, _t = issues.create_issue(rid, "plan", "Retrain expo", source_key="plan:2026-W39:0", notify=False,
                                    db_path=db_path)
    _sql(db_path, "UPDATE ops_issues SET status='resolved', resolved_at=datetime('now'), "
                  "resolution_note='cut prep to 3 people, saved $400' WHERE id=?", issue["id"])
    ctx = decisions.context(rid, db_path=db_path)
    assert "saved $400" in ctx and "saved $400" not in _strip_untrusted(ctx)


def test_no_owner_or_manager_free_text_reaches_asks_corpus_outside_a_fence():
    """Source test: every free-text field decisions renders passes _fence,
    and the Ask commitments section fences the owner's decline reason."""
    src = open(os.path.join(ROOT, "decisions.py")).read()
    body = src[src.index("def _line(r):"):src.index("def _collapse(rows):")]
    assert '_fence(r["reason"])' in body and '_fence(r["issue"]["note"][:80])' in body and "_fence(title)" in body
    ask = open(os.path.join(ROOT, "ask_cavnar.py")).read()
    com = ask[ask.index("def _commitments_context("):ask.index("def _cross_module_context(")]
    assert "wrap_untrusted(r['reason'])" in com


# ── proposed ─────────────────────────────────────────────────────────────────

def _action(db_path, rid, action, summary, outcome, surface="ask", user_id=None, days_ago=0, proposal_id=None,
            reason=None):
    c = models.get_conn(db_path)
    cur = c.execute("INSERT INTO ask_cavnar_actions (restaurant_id, user_id, action, summary, outcome, surface, "
                    "proposal_id, reason, created_at) VALUES (?,?,?,?,?,?,?,?, datetime('now', ?))",
                    (rid, user_id, action, summary, outcome, surface, proposal_id, reason, f"-{int(days_ago)} days"))
    c.commit()
    pid = cur.lastrowid
    c.close()
    return pid


class _Viewer:
    def __init__(self, user):
        self._ask_dsr_user = user


def test_a_palette_preview_and_an_old_proposal_are_not_still_open(db_path):
    import ask_cavnar
    rid = _rid(db_path)
    _action(db_path, rid, "send_supplier_order", "Send the order to Fresh Co", "proposed", surface="command")
    _action(db_path, rid, "draft_campaign", "Text regulars about the patio", "proposed", days_ago=12)
    _action(db_path, rid, "publish_schedule", "Publish next week's schedule", "proposed")
    text = ask_cavnar._commitments_context(rid)
    assert "Fresh Co" not in text and "patio" not in text
    assert "Publish next week's schedule — proposed" in text
    # Dated M/D/YY, never ISO.
    assert not re.search(r"\d{4}-\d{2}-\d{2}", text)


def test_a_manager_reads_only_their_own_proposals(db_path):
    import ask_cavnar
    rid = _rid(db_path)
    _action(db_path, rid, "draft_campaign", "Owner's patio text", "dismissed", user_id=1,
            reason="patio closes in October")
    _action(db_path, rid, "publish_schedule", "Manager's schedule", "confirmed", user_id=7)
    owner_text = ask_cavnar._commitments_context(rid)
    assert "Owner's patio text" in owner_text and wrap_untrusted("patio closes in October") in owner_text
    mgr = _Viewer({"id": 7, "role": "manager", "restaurant_id": rid})
    mgr_text = ask_cavnar._commitments_context(rid, viewer=mgr)
    assert "Manager's schedule" in mgr_text and "Owner's patio text" not in mgr_text


# ── relevance ────────────────────────────────────────────────────────────────

def test_a_reasoned_decline_is_never_capped_out_by_newer_answers(db_path):
    import decisions
    rid = _rid(db_path)
    rl.present(rid, "trim_day:Friday", "labor", "home", title="Trim a Friday closer", db_path=db_path)
    rl.record(rid, "trim_day:Friday", "dismissed", meta={"kind": "not_for_us",
                                                          "reason": "we never cut the Friday closer"},
              db_path=db_path)
    _sql(db_path, "UPDATE rec_events SET at=datetime('now','-50 days') WHERE key='trim_day:Friday'")
    for i in range(20):
        rl.present(rid, f"reprice:Dish {i}", "food", "home", db_path=db_path)
        rl.record(rid, f"reprice:Dish {i}", "completed", db_path=db_path)
    ctx = decisions.context(rid, db_path=db_path)
    assert "we never cut the Friday closer" in ctx


def test_the_provider_ranks_by_subject_and_collapses_by_signature(db_path):
    import decisions
    from memory_context import MemoryRequest
    rid = _rid(db_path)
    for k, t in (("insight_labor:aaaaaaaaaa", "Cut a server on Tuesday nights"),
                 ("insight_labor:bbbbbbbbbb", "Trim one server from Tuesday dinner")):
        rl.present(rid, k, "labor", "labor", title=t, model_written=True, db_path=db_path)
        rl.record(rid, k, "dismissed", meta={"kind": "not_for_us", "reason_code": "doesnt_fit"}, db_path=db_path)
    rl.present(rid, "reprice:Soup", "food", "home", db_path=db_path)
    rl.record(rid, "reprice:Soup", "completed", db_path=db_path)
    lines = decisions.memory_lines(MemoryRequest(restaurant_id=rid, surface="labor_read",
                                                 subjects=("labor:day:tuesday",)))
    assert lines and lines[0]["subject"] == "labor:day:tuesday"
    tuesday = [l for l in lines if l["subject"] == "labor:day:tuesday"]
    assert len(tuesday) == 1 and "other wording" in tuesday[0]["text"]      # two wordings, one line
    assert lines[0]["weight"] > max(l["weight"] for l in lines if l["subject"] != "labor:day:tuesday")
    # Fenced where people or models wrote it, dated by memory_context.
    assert "<<<UNTRUSTED" in tuesday[0]["text"] and tuesday[0]["date"]


def test_memory_context_serves_the_decisions_section_on_every_surface_that_needs_it(db_path):
    import memory_context as mc
    for surface in ("competitor_read", "digest", "review_diagnosis", "food_diagnosis", "labor_read", "food_read",
                    "brief", "marketing", "schedule"):
        assert "decisions" in mc.SURFACE_SECTIONS[surface], surface
    # The Monday plan and the nightly report carry decisions.context in their
    # own prompt, read as the same team viewer — a second copy through
    # memory_context paid twice under two viewers (memory re-audit PROMPTS-14).
    for surface in ("weekly_plan", "dsr_narrative"):
        assert "decisions" not in mc.SURFACE_SECTIONS[surface], surface
    rid = _rid(db_path)
    rl.present(rid, "trim_day:Friday", "labor", "home", title="Trim Friday", db_path=db_path)
    rl.record(rid, "trim_day:Friday", "dismissed", meta={"kind": "not_for_us", "reason": "no"}, db_path=db_path)
    block = mc.memory_context(rid, "competitor_read")
    assert "decisions" in block.sections and "not for us" in block.text
    assert not re.search(r"\d{4}-\d{2}-\d{2}", block.text)


def test_already_answered_is_newest_first_keeps_reasons_and_collapses(db_path):
    import insight_store
    rid = _rid(db_path)
    for i in range(15):
        k = f"insight_food:{i:010d}"
        rl.present(rid, k, "food", "food", title=f"Cut the order of item {i}", model_written=True, db_path=db_path)
        rl.record(rid, k, "completed", db_path=db_path)
        _sql(db_path, "UPDATE rec_instances SET closed_at=datetime('now', ?) WHERE key=?", f"-{15 - i} hours", k)
    rl.present(rid, "insight_food:zzzzzzzzzz", "food", "food", title="Reprice the Salmon special", db_path=db_path)
    rl.record(rid, "insight_food:zzzzzzzzzz", "dismissed",
              meta={"kind": "not_for_us", "reason": "regulars would revolt"}, db_path=db_path)
    _sql(db_path, "UPDATE rec_instances SET closed_at=datetime('now','-30 days') WHERE key='insight_food:zzzzzzzzzz'")
    lines = insight_store.answered_lines(rid, ("insight_food",), limit=12, db_path=db_path)
    assert "Reprice the Salmon special" in lines                 # reasoned: never capped out
    assert lines[1] == "Cut the order of item 14"               # then newest first
    # The same advice from Home reaches the food read's block.
    rl.present(rid, "cut_waste:Salmon", "food", "home", title="Cut the Salmon order", db_path=db_path)
    rl.record(rid, "cut_waste:Salmon", "completed", db_path=db_path)
    assert "Cut the Salmon order" in insight_store.answered_lines(rid, ("insight_food",), limit=40, db_path=db_path)


def test_asks_prose_is_caveated_when_it_repeats_a_decline(db_path):
    import decisions
    rid = _rid(db_path)
    rl.present(rid, "trim_day:Friday", "labor", "home", title="Trim Friday staffing", db_path=db_path)
    rl.record(rid, "trim_day:Friday", "dismissed", meta={"kind": "not_for_us"}, db_path=db_path)
    answer = ("Friday labor ran high.\n\n- Cut one server from Friday dinner next week\n"
              "- Post the brunch special on Instagram")
    text, repeats = decisions.annotate_declined(rid, answer, db_path=db_path)
    assert "Cut one server from Friday dinner next week (you passed on this on " in text
    assert "Post the brunch special on Instagram\n" not in text or "(you passed" not in text.split("\n")[-1]
    assert repeats and repeats[0]["signature"] == "labor:day:friday"
    assert not re.search(r"\d{4}-\d{2}-\d{2}", text)
