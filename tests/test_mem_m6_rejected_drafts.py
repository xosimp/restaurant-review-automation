"""Memory audit 9/29/26, rejected_drafts (workstream M6): a reply draft the
owner turns down by regenerating it (or by asking Ask to rewrite it) is kept
for 90 days as its hash and closed-vocabulary signals — never its words — and
the drafter's edit note says what those drafts had in common."""
import hashlib
import json
import sqlite3
import types

import pytest

import client_api
import drafter
import models
import ops
import reply_edits
from models import Restaurant, create_restaurant


@pytest.fixture(autouse=True)
def _db(db_path, monkeypatch):
    real = models.get_conn
    monkeypatch.setattr(models, "get_conn", lambda *a, **k: real(db_path))
    monkeypatch.setattr(models, "DB_PATH", db_path)
    yield


def _rid(db_path):
    return create_restaurant(Restaurant(name="Gia Mia", owner_email="o@x.test"), db_path=db_path)


def _conn(db_path):
    c = sqlite3.connect(db_path)
    c.row_factory = sqlite3.Row
    return c


LONG = ("Thank you so much for coming in and for taking the time to write such a thoughtful review of your "
        "evening with us! We are thrilled that you enjoyed the carbonara and the tiramisu, and our whole team "
        "loved hearing it. We hope to see you again very soon!")
SHORT = "Thanks for coming in, Ann. See you soon."
OWNER = {"id": 11, "role": "client"}
ADMIN = {"id": 11, "role": "client", "acting_admin_id": 99, "acting_admin_role": "admin",
         "device_type": "admin-view-as"}


def _review(db_path, rid, rating=5, draft=LONG, status="drafted", edited=0):
    c = _conn(db_path)
    cur = c.execute("INSERT INTO reviews (restaurant_id, platform, external_id, author, rating, text, processed, "
                    "response_status, draft_response, draft_edited, fetched_at, approved_at) "
                    "VALUES (?,?,?,?,?,?,1,?,?,?,datetime('now'),datetime('now'))",
                    (rid, "google", f"x{id(draft)}{rating}{status}{edited}{c.execute('SELECT COUNT(*) FROM reviews').fetchone()[0]}",
                     "Ann", rating, "Loved it", status, draft, edited))
    c.commit()
    rv = cur.lastrowid
    c.close()
    return rv


def _stub_drafter(monkeypatch, text=SHORT):
    msg = types.SimpleNamespace(content=[types.SimpleNamespace(type="text", text=text)], stop_reason="end_turn")
    monkeypatch.setattr(drafter, "get_client", lambda *a, **k: object())
    monkeypatch.setattr(drafter, "create_with_retry", lambda client, **kw: msg)


def test_a_drafts_signals_are_a_closed_vocabulary_and_length_is_judged_by_band():
    assert reply_edits.draft_signals(LONG, 5) == ["long", "exclamations", "invitation"]
    assert "long" not in reply_edits.draft_signals(LONG, 1)          # 1-2 stars are asked for 60-80 words
    assert reply_edits.draft_signals("We are so sorry 😞", 1) == ["short", "apology", "emoji"]
    assert reply_edits.draft_signals(LONG) == ["exclamations", "invitation"]   # no rating, no length


def test_the_rejection_note_needs_a_pattern_the_approved_replies_do_not_share():
    long_ex = ["long", "exclamations"]
    assert reply_edits.rejection_note([long_ex, long_ex]) == ""                     # two is not a pattern
    note = reply_edits.rejection_note([long_ex] * 3, [["invitation"]] * 3, scope="for 4-5★ reviews")
    assert "run longer than they want" in note and "use exclamation marks" in note and "for 4-5★ reviews" in note
    # A signal the approved replies share as often is not why a draft was turned down.
    assert "exclamation" not in reply_edits.rejection_note([long_ex] * 3, [["exclamations"]] * 3)


def test_a_regenerate_keeps_the_rejected_drafts_hash_and_signals_never_its_words(db_path, monkeypatch):
    rid = _rid(db_path)
    rv = _review(db_path, rid)
    _stub_drafter(monkeypatch)
    payload, status = client_api._do_regenerate_draft(rv, rid, user=OWNER)
    assert status == 200 and payload["ok"], payload
    c = _conn(db_path)
    row = dict(c.execute("SELECT * FROM reply_draft_rejections").fetchone())
    c.close()
    assert row["draft_hash"] == hashlib.sha256(LONG.encode()).hexdigest()
    assert json.loads(row["signals"]) == ["long", "exclamations", "invitation"]
    assert (row["how"], row["authority"], row["user_id"], row["rating"]) == ("regenerate", "principal", 11, 5)
    assert "carbonara" not in json.dumps(row)


def test_a_draft_the_owner_had_edited_is_not_a_rejected_model_draft(db_path, monkeypatch):
    rid = _rid(db_path)
    rv = _review(db_path, rid, draft="My own words for this one.", edited=1)
    _stub_drafter(monkeypatch)
    assert client_api._do_regenerate_draft(rv, rid, user=OWNER)[1] == 200
    c = _conn(db_path)
    assert c.execute("SELECT COUNT(*) FROM reply_draft_rejections").fetchone()[0] == 0
    c.close()


def test_asks_rewrite_turns_down_the_model_draft_it_replaces(db_path):
    rid = _rid(db_path)
    rv = _review(db_path, rid)
    out, status = client_api._do_save_draft(rv, rid, SHORT, by_model=True, user=OWNER)
    assert status == 200 and out["ok"]
    c = _conn(db_path)
    assert [r["how"] for r in c.execute("SELECT how FROM reply_draft_rejections")] == ["rewrite"]
    c.close()


def test_the_edit_note_says_what_the_regenerated_drafts_had_in_common(db_path, monkeypatch):
    rid = _rid(db_path)
    _stub_drafter(monkeypatch)
    for _ in range(3):
        client_api._do_regenerate_draft(_review(db_path, rid), rid, user=OWNER)
    for _ in range(3):
        _review(db_path, rid, draft=SHORT, status="approved")
    note = drafter.get_owner_edit_note(rid, rating=5)
    assert "regenerated 3 drafts (for 4-5★ reviews)" in note and "run longer than they want" in note
    # Another band's drafts are not this band's pattern.
    assert "regenerated" not in drafter.get_owner_edit_note(rid, rating=1)


def test_an_admins_regenerate_through_view_as_teaches_nothing(db_path, monkeypatch):
    rid = _rid(db_path)
    _stub_drafter(monkeypatch)
    for _ in range(3):
        client_api._do_regenerate_draft(_review(db_path, rid), rid, user=ADMIN)
    assert models.get_reply_rejection_signals(rid, rating=5)["rejected"] == []


def test_rejections_are_kept_ninety_days_by_the_one_registry():
    assert ops._RETENTION_DAYS["reply_draft_rejections"] == 90 == models.REJECTIONS_KEEP_DAYS
    assert ops._RETENTION_COLUMN["reply_draft_rejections"] == "created_at"


def test_both_regenerate_routes_pass_their_login():
    import inspect
    import mobile_api
    assert "user=current_user" in inspect.getsource(client_api.regenerate_draft)
    assert "user=current_user" in inspect.getsource(mobile_api.mobile_regenerate_draft)
