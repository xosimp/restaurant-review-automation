"""Memory audit 9/29/26, drafter_fixes (workstream M6): the reply drafter may
acknowledge a change the owner confirmed on the complaint's category — the
account holder's Done (or an implemented change) on a review diagnosis or top
issue — as optional owner-supplied text, and a draft that uses it is always
held for the owner: never bulk- or auto-published, never cleared by the boot
re-check."""
import sqlite3
import types

import pytest

import drafter
import models
import rec_ledger
from models import Restaurant, create_restaurant


@pytest.fixture(autouse=True)
def _db(db_path, monkeypatch):
    real = models.get_conn
    fake = lambda *a, **k: real(db_path)  # noqa: E731
    monkeypatch.setattr(models, "get_conn", fake)
    monkeypatch.setattr(models, "DB_PATH", db_path)
    monkeypatch.setattr(rec_ledger, "get_conn", fake, raising=False)
    monkeypatch.setattr(rec_ledger, "DB_PATH", db_path, raising=False)
    yield


FIX = "Add a second host on Friday nights"


def _rid(db_path):
    return create_restaurant(Restaurant(name="Gia Mia", owner_email="o@x.test"), db_path=db_path)


def _conn(db_path):
    c = sqlite3.connect(db_path)
    c.row_factory = sqlite3.Row
    return c


def _confirm(db_path, rid, key="diag_review:wait_time", role="client", meta=None, event="completed"):
    rec_ledger.present(rid, key, "reviews", "reviews", title=FIX, db_path=db_path)
    assert rec_ledger.record(rid, key, event, surface="reviews", user_id=11, role=role, meta=meta,
                             db_path=db_path)


def _pending(db_path, rid, rating=2, categories='["wait_time"]'):
    c = _conn(db_path)
    cur = c.execute("INSERT INTO reviews (restaurant_id, platform, external_id, author, rating, text, processed, "
                    "sentiment, categories, response_status, fetched_at) "
                    "VALUES (?,?,?,?,?,?,1,'negative',?,'pending',datetime('now'))",
                    (rid, "google", f"p{rating}{categories}", "Ann", rating, "We waited 40 minutes at the door on "
                     "Friday.", categories))
    c.commit()
    rv = cur.lastrowid
    c.close()
    return rv


def _stub(monkeypatch, text):
    seen = []
    msg = types.SimpleNamespace(content=[types.SimpleNamespace(type="text", text=text)], stop_reason="end_turn")
    monkeypatch.setattr(drafter, "get_client", lambda *a, **k: object())
    monkeypatch.setattr(drafter, "create_with_retry", lambda client, **kw: seen.append(kw["messages"][0]["content"])
                        or msg)
    return seen


def test_only_the_account_holders_confirmation_on_the_complaints_category_is_offered(db_path):
    rid = _rid(db_path)
    _confirm(db_path, rid)
    got = drafter.confirmed_fixes(rid, ["wait_time"])
    assert [f["text"] for f in got] == [FIX] and got[0]["category"] == "wait_time"
    assert drafter.confirmed_fixes(rid, ["food_quality"]) == []


def test_a_managers_or_an_admins_done_is_not_the_owners_confirmation(db_path):
    rid = _rid(db_path)
    _confirm(db_path, rid, role="manager")
    _confirm(db_path, rid, key="top_issue:wait_time", role="client", meta={"via": "view_as"})
    assert drafter.confirmed_fixes(rid, ["wait_time"]) == []


def test_the_answers_recorded_authority_decides_whose_done_it_was(db_path):
    """rec_ledger records whose answer it was on rec_events.authority (M1):
    a view-as session is minted for the owner's own login, so its Done
    carries the owner's role — only the authority (and the `via` M1 puts on
    the meta) says an admin gave it."""
    rid = _rid(db_path)
    rec_ledger.present(rid, "diag_review:wait_time", "reviews", "reviews", title=FIX, db_path=db_path)
    assert rec_ledger.record(rid, "diag_review:wait_time", "completed", surface="reviews", user_id=11, role="client",
                             authority="admin", via={"admin_id": 99, "admin": "support", "role": "admin"},
                             db_path=db_path)
    rec_ledger.present(rid, "top_issue:wait_time", "reviews", "reviews", title=FIX, db_path=db_path)
    assert rec_ledger.record(rid, "top_issue:wait_time", "completed", surface="reviews", user_id=12, role="client",
                             authority="delegate", db_path=db_path)
    assert drafter.confirmed_fixes(rid, ["wait_time"]) == []
    rec_ledger.present(rid, "diag_review:food_quality", "reviews", "reviews", title="New fryer oil schedule",
                       db_path=db_path)
    assert rec_ledger.record(rid, "diag_review:food_quality", "completed", surface="reviews", user_id=11,
                             role="manager", authority="principal", db_path=db_path)
    assert [f["text"] for f in drafter.confirmed_fixes(rid, ["food_quality"])] == ["New fryer oil schedule"]


def test_the_prompt_offers_the_change_and_lifts_the_no_fix_rule_only_for_it(db_path, monkeypatch):
    rid = _rid(db_path)
    _confirm(db_path, rid)
    seen = _stub(monkeypatch, "We're sorry about the wait at the door. Please reach out so we can make it right.")
    drafter.draft_response(_pending(db_path, rid), 2, "We waited 40 minutes", "negative", "Gia Mia",
                           restaurant_id=rid)
    p = seen[-1]
    assert "OWNER-CONFIRMED CHANGES" in p and FIX in p
    assert "other than an OWNER-CONFIRMED CHANGE" in p


def test_a_draft_that_uses_the_change_is_held_for_the_owner(db_path, monkeypatch):
    rid = _rid(db_path)
    _confirm(db_path, rid)
    _stub(monkeypatch, "We're sorry about the wait. We've added a second host on Friday nights so the door "
                       "moves faster. Please reach out to us directly.")
    rv = _pending(db_path, rid)
    drafter.draft_response(rv, 2, "We waited 40 minutes", "negative", "Gia Mia", restaurant_id=rid)
    c = _conn(db_path)
    row = c.execute("SELECT response_status, draft_needs_review, draft_review_reason FROM reviews WHERE id=?",
                    (rv,)).fetchone()
    c.close()
    assert (row["response_status"], row["draft_needs_review"], row["draft_review_reason"]) == \
        ("drafted", 1, drafter.FIX_REVIEW_REASON)
    # Never a bulk or automatic publish, and the boot re-check leaves the hold alone.
    assert rv not in [r["id"] for r in models.auto_approve_candidates(rid, ratings=(1, 2, 3, 4, 5))]
    assert drafter.recheck_draft_flags(db_path) == 0
    c = _conn(db_path)
    assert c.execute("SELECT draft_needs_review FROM reviews WHERE id=?", (rv,)).fetchone()[0] == 1
    c.close()


def test_without_a_confirmed_change_the_no_fix_rule_stands(db_path, monkeypatch):
    rid = _rid(db_path)
    seen = _stub(monkeypatch, "We're sorry about the wait. Please reach out to us directly.")
    rv = _pending(db_path, rid)
    drafter.draft_response(rv, 2, "We waited 40 minutes", "negative", "Gia Mia", restaurant_id=rid)
    assert "OWNER-CONFIRMED CHANGES" not in seen[-1]
    c = _conn(db_path)
    assert c.execute("SELECT draft_needs_review FROM reviews WHERE id=?", (rv,)).fetchone()[0] == 0
    c.close()


def test_using_a_change_is_read_from_its_words_or_any_stated_action():
    fixes = [{"text": FIX}]
    assert drafter.uses_confirmed_fix("We now have a second host at the door on Fridays.", fixes)
    assert drafter.uses_confirmed_fix("We have retrained our staff.", fixes)
    assert not drafter.uses_confirmed_fix("We're sorry about the wait. Please reach out to us.", fixes)
