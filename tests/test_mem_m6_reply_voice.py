"""Memory audit 9/29/26, reply_voice (workstream M6): the reply drafter learns
the OWNER's voice — who approved is recorded inside claim_approval; examples
and the edit note come from the owner's approvals (or a login marked "writes
in our voice"), banded by the star rating of the review being answered, edited
replies only once any exist, the last 12 months first, never a removed review;
the owner's most-used templates are offered as their own words; auto-approve
trust counts the account holder's approvals only."""
import inspect
import sqlite3
import types

import pytest
from flask import Flask

import client_api
import drafter
import models
import reply_edits
from models import Restaurant, create_restaurant


@pytest.fixture(autouse=True)
def _db(db_path, monkeypatch):
    real = models.get_conn
    fake = lambda *a, **k: real(db_path)  # noqa: E731
    monkeypatch.setattr(models, "get_conn", fake)
    monkeypatch.setattr(models, "DB_PATH", db_path)
    import gmb
    import webhooks
    monkeypatch.setattr(gmb, "is_connected", lambda rid: False)
    monkeypatch.setattr(webhooks, "fire_webhook", lambda *a, **k: None)
    yield


def _rid(db_path, name="Gia Mia"):
    return create_restaurant(Restaurant(name=name, owner_email="o@x.test"), db_path=db_path)


def _conn(db_path):
    c = sqlite3.connect(db_path)
    c.row_factory = sqlite3.Row
    return c


_N = [0]


def _review(db_path, rid, rating=5, draft="Thanks so much!", status="drafted", original=None, edited=0,
            role=None, via=None, by=None, approved_days_ago=1, deleted=False, category=None):
    _N[0] += 1
    c = _conn(db_path)
    cur = c.execute(
        "INSERT INTO reviews (restaurant_id, platform, external_id, author, rating, text, processed, response_status, "
        "draft_response, original_draft, draft_edited, edit_category, approved_role, approved_via, approved_by, "
        "approved_at, review_date, fetched_at, deleted_at) VALUES (?,?,?,?,?,?,1,?,?,?,?,?,?,?,?,"
        "datetime('now', ?),datetime('now'),datetime('now'),?)",
        (rid, "google", f"ext{_N[0]}", "Ann", rating, f"Review {_N[0]}", status, draft, original, edited, category,
         role, via, by, f"-{int(approved_days_ago)} days", "2026-09-01" if deleted else None))
    c.commit()
    rv = cur.lastrowid
    c.close()
    return rv


def _grant(db_path, rid, user_id, permission="reviews.voice"):
    c = _conn(db_path)
    c.execute("CREATE TABLE IF NOT EXISTS permission_grants (user_id INTEGER NOT NULL, restaurant_id INTEGER NOT NULL, "
              "permission TEXT NOT NULL, granted_by INTEGER, granted_at TEXT, "
              "PRIMARY KEY (user_id, restaurant_id, permission))")
    c.execute("INSERT OR IGNORE INTO permission_grants (user_id, restaurant_id, permission) VALUES (?,?,?)",
              (user_id, rid, permission))
    c.commit()
    c.close()


OWNER = {"id": 11, "role": "client"}
MANAGER = {"id": 22, "role": "manager"}
VIEW_AS = {"id": 11, "role": "client", "acting_admin_id": 99, "acting_admin_role": "admin",
           "device_type": "admin-view-as"}


# ── capture: who approved, inside claim_approval ─────────────────────────────

def test_the_approver_is_the_login_its_authority_and_how():
    assert models.reply_approver(OWNER) == {"user_id": 11, "role": "principal", "via": "normal"}
    assert models.reply_approver(MANAGER) == {"user_id": 22, "role": "delegate", "via": "normal"}
    # A view-as session runs as the owner's own user row: the admin behind it is recorded.
    assert models.reply_approver(VIEW_AS) == {"user_id": 99, "role": "admin", "via": "view_as"}
    assert models.reply_approver(None, auto=True) == {"user_id": None, "role": "rule", "via": "rule"}
    assert models.reply_approver(None) == {"user_id": None, "role": None, "via": None}


def test_every_approve_path_records_who_approved(db_path):
    rid = _rid(db_path)
    mine, theirs, admins, rules = (_review(db_path, rid) for _ in range(4))
    with Flask(__name__).test_request_context():
        assert client_api._do_approve(mine, rid, user=OWNER)[1] == 200
        assert client_api._do_approve(theirs, rid, user=MANAGER)[1] == 200
        assert client_api._do_approve(admins, rid, user=VIEW_AS)[1] == 200
    assert client_api._do_approve(rules, rid, auto=True)[1] == 200
    c = _conn(db_path)
    got = {r["id"]: (r["approved_by"], r["approved_role"], r["approved_via"]) for r in c.execute(
        "SELECT id, approved_by, approved_role, approved_via FROM reviews")}
    c.close()
    assert got[mine] == (11, "principal", "normal")
    assert got[theirs] == (22, "delegate", "normal")
    assert got[admins] == (99, "admin", "view_as")
    assert got[rules] == (None, "rule", "rule")


def test_a_bulk_publish_records_the_person_who_pressed_it(db_path):
    rid = _rid(db_path)
    ids = [_review(db_path, rid, draft=f"Thank you, see you soon {i}.") for i in range(2)]
    with Flask(__name__).test_request_context():
        out, status = client_api._do_approve_all(rid, limit=25, user=OWNER)
    assert status == 200 and out["approved"] == 2
    c = _conn(db_path)
    rows = c.execute("SELECT approved_role, response_action FROM reviews WHERE id IN (?,?)", ids).fetchall()
    c.close()
    assert {(r["approved_role"], r["response_action"]) for r in rows} == {("principal", "bulk_approved")}


def test_every_route_passes_its_login_to_the_approve_body():
    import mobile_api
    for fn in (client_api.approve, client_api.approve_all_reviews_api, mobile_api.mobile_approve_review,
               mobile_api.mobile_approve_all_reviews):
        assert "user=current_user" in inspect.getsource(fn), fn.__name__


def test_undoing_an_approval_clears_who_approved(db_path):
    rid = _rid(db_path)
    rv = _review(db_path, rid, status="approved", role="principal", via="normal", by=11)
    models.revert_to_drafted(rv, rid)
    c = _conn(db_path)
    row = c.execute("SELECT approved_by, approved_role, approved_via FROM reviews WHERE id=?", (rv,)).fetchone()
    c.close()
    assert tuple(row) == (None, None, None)


# ── examples: the owner's voice, the review's own band ───────────────────────

def test_examples_are_the_owners_voice_never_a_managers_or_an_admins(db_path):
    rid = _rid(db_path)
    _review(db_path, rid, status="approved", draft="Owner words.", role="principal", via="normal", by=11)
    _review(db_path, rid, status="approved", draft="Manager words.", role="delegate", via="normal", by=22)
    _review(db_path, rid, status="approved", draft="Admin words.", role="admin", via="view_as", by=99)
    _review(db_path, rid, status="approved", draft="Rule words.", role="rule", via="rule")
    _review(db_path, rid, status="approved", draft="Removed review.", role="principal", via="normal", deleted=True)
    assert [e["response"] for e in models.get_approved_examples(rid)] == ["Owner words."]
    # The owner marks the manager as writing in their voice: their approvals now teach.
    _grant(db_path, rid, 22)
    assert sorted(e["response"] for e in models.get_approved_examples(rid)) == ["Manager words.", "Owner words."]


def test_a_reply_approved_before_the_approver_was_recorded_still_counts_behind_known_ones(db_path):
    rid = _rid(db_path)
    old = _review(db_path, rid, status="approved", draft="Legacy words.")          # approved_role NULL
    _review(db_path, rid, status="approved", draft="Owner words.", role="principal", via="normal")
    got = [e["response"] for e in models.get_approved_examples(rid)]
    assert got == ["Owner words.", "Legacy words."] and old


def test_a_one_star_reply_gets_one_and_two_star_examples_and_never_five_star_ones(db_path):
    rid = _rid(db_path)
    _review(db_path, rid, rating=5, status="approved", draft="Five-star thanks!", role="principal", via="normal")
    _review(db_path, rid, rating=2, status="approved", draft="We are sorry about the wait.", role="principal",
            via="normal")
    _review(db_path, rid, rating=3, status="approved", draft="Thanks for the honest note.", role="principal",
            via="normal")
    low = [e["response"] for e in models.get_approved_examples(rid, limit=4, rating=1)]
    assert low == ["We are sorry about the wait.", "Thanks for the honest note."]
    high = [e["response"] for e in models.get_approved_examples(rid, limit=4, rating=5)]
    assert high == ["Five-star thanks!", "Thanks for the honest note."]


def test_once_the_owner_has_edited_a_reply_unedited_approvals_are_no_longer_examples(db_path):
    rid = _rid(db_path)
    _review(db_path, rid, status="approved", draft="Model text approved as is.", role="principal", via="normal",
            category="unchanged")
    assert [e["response"] for e in models.get_approved_examples(rid, rating=5)] == ["Model text approved as is."]
    _review(db_path, rid, status="approved", draft="My own words.", original="The model's words!", edited=1,
            role="principal", via="normal", category="heavy")
    got = models.get_approved_examples(rid, rating=5)
    assert [e["response"] for e in got] == ["My own words."] and got[0]["edited"] is True


def test_the_last_twelve_months_come_first(db_path):
    rid = _rid(db_path)
    _review(db_path, rid, status="approved", draft="Two years ago.", role="principal", via="normal",
            approved_days_ago=700)
    _review(db_path, rid, status="approved", draft="Last month.", role="principal", via="normal",
            approved_days_ago=30)
    _review(db_path, rid, status="approved", draft="Also two years ago.", role="principal", via="normal",
            approved_days_ago=720)
    assert models.get_approved_examples(rid, limit=2, rating=5)[0]["response"] == "Last month."


# ── the edit note, banded ────────────────────────────────────────────────────

def _edited(db_path, rid, rating, before, after, n=3, role="principal"):
    for i in range(n):
        rv = _review(db_path, rid, rating=rating, status="drafted", draft=f"{after} {i}", original=f"{before} {i}",
                     edited=1)
        with Flask(__name__).test_request_context():
            assert client_api._do_approve(rv, rid, user=OWNER if role == "principal" else MANAGER)[1] == 200


def test_a_one_star_note_is_never_measured_on_five_star_edits(db_path):
    rid = _rid(db_path)
    long_thanks = "Thank you so much for coming in! We loved having you and hope to see you again very soon!"
    _edited(db_path, rid, 5, long_thanks, "Thanks for coming in, see you soon.")
    five = drafter.get_owner_edit_note(rid, rating=5)
    assert "about" in five and "words" in five and "replies to 4-5★ reviews" in five
    one = drafter.get_owner_edit_note(rid, rating=1)
    # Borrowed from other bands: only the rating-free signal, never a length.
    assert "words" not in one and "they cut the draft down" not in one
    assert "they take out exclamation marks" in one


def test_a_managers_edits_are_not_the_owners_note(db_path):
    rid = _rid(db_path)
    long_thanks = "Thank you so much for coming in! We loved having you and hope to see you again very soon!"
    _edited(db_path, rid, 5, long_thanks, "Thanks for coming in, see you soon.", role="delegate")
    assert drafter.get_owner_edit_note(rid, rating=5) == ""


def test_the_style_note_can_be_limited_to_rating_free_signals():
    edit = {"category": "heavy", "signals": ["shortened", "removed_exclamations"], "words_after": 20}
    note = reply_edits.style_note([edit] * 3, only=reply_edits.BAND_FREE_SIGNALS, with_length=False,
                                  scope="replies to reviews of any rating")
    assert "exclamation" in note and "cut the draft down" not in note and "words" not in note
    assert "(replies to reviews of any rating)" in note


# ── trust: the account holder's approvals only ───────────────────────────────

def test_a_managers_approvals_never_earn_auto_approve_trust(db_path):
    rid = _rid(db_path)
    for i in range(12):
        _review(db_path, rid, rating=5, status="approved", draft=f"Thanks {i}", role="delegate", via="normal", by=22)
    assert models.auto_approve_trust(rid)[5]["approved"] == 0
    # Not even one the owner marked as writing in their voice.
    _grant(db_path, rid, 22)
    assert models.auto_approve_trust(rid)[5]["approved"] == 0
    for i in range(12):
        _review(db_path, rid, rating=5, status="approved", draft=f"Thanks! {i}", role="admin", via="view_as", by=99)
    assert models.auto_approve_trust(rid)[5]["approved"] == 0
    for i in range(10):
        _review(db_path, rid, rating=5, status="approved", draft=f"Thank you {i}", role="principal", via="normal")
    t = models.auto_approve_trust(rid)[5]
    assert t["approved"] == 10 and t["trusted"] is True


# ── templates, and the drafter's prompt ──────────────────────────────────────

def test_the_owners_most_used_templates_for_the_band_are_offered_fenced(db_path):
    import ai_guard
    rid = _rid(db_path)
    models.create_response_template(rid, "Sorry", "We're so sorry — please call Maria at the host stand.", "negative")
    models.create_response_template(rid, "Thanks", "Thank you for the kind words!", "positive")
    models.create_response_template(rid, "Unused", "Never used.", "negative")
    c = _conn(db_path)
    c.execute("UPDATE response_templates SET use_count=40 WHERE title='Sorry'")
    c.execute("UPDATE response_templates SET use_count=5 WHERE title='Thanks'")
    c.commit()
    c.close()
    block = drafter.owner_templates_block(rid, 1)
    assert "please call Maria" in block and "used 40 times" in block and ai_guard.UNTRUSTED_OPEN in block
    assert "kind words" not in block and "Never used" not in block
    assert "kind words" in drafter.owner_templates_block(rid, 5)


def _msg(text):
    return types.SimpleNamespace(content=[types.SimpleNamespace(type="text", text=text)], stop_reason="end_turn")


def test_each_draft_reads_examples_for_its_own_review(db_path, monkeypatch):
    rid = _rid(db_path)
    _review(db_path, rid, rating=5, status="approved", draft="Five-star thanks, friend!", role="principal",
            via="normal")
    _review(db_path, rid, rating=1, status="approved", draft="We are truly sorry about the cold soup.",
            role="principal", via="normal")
    seen = []
    monkeypatch.setattr(drafter, "get_client", lambda *a, **k: object())
    monkeypatch.setattr(drafter, "create_with_retry",
                        lambda client, **kw: seen.append(kw["messages"][0]["content"])
                        or _msg("We are sorry the soup was cold. Please reach out so we can make it right."))
    low = _review(db_path, rid, rating=1, status="pending", draft=None)
    drafter.draft_response(low, 1, "Cold soup", "negative", "Gia Mia", restaurant_id=rid)
    assert "truly sorry about the cold soup" in seen[-1] and "Five-star thanks" not in seen[-1]


def test_no_caller_hands_every_draft_one_shared_pool_of_examples():
    """The scheduler, draft_pending, the regenerate route and both admin
    redraft jobs fetched four examples once and passed them to every draft.
    They pass none now: draft_response picks each review's own. An absence
    test on purpose — re-adding the pool re-creates the bug."""
    import admin_routes
    import scheduler
    for src in (inspect.getsource(scheduler.run_daily_fetch), inspect.getsource(drafter.draft_pending),
                inspect.getsource(client_api._do_regenerate_draft), inspect.getsource(admin_routes)):
        assert "approved_examples=" not in src
