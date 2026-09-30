"""Memory re-audit fix round 9/29/26 (R5, voice_learning):

  LOOPS-4     the reply style note (and marketing's) reads the owner's EDITED
              approvals over a year, so it no longer erases itself once the
              drafter follows it.
  PROMPTS-11  "edited only" is applied per star band, and each band is read
              in SQL before its limit.
  QUALITY-18  marketing examples are only pieces the owner edited or wrote;
              drafts sent as drafted are approvals, never "their own words".
  LOOPS-15    a login with the marketing-voice grant teaches marketing voice.
  PROMPTS-12  accepted recipe drafts are examples only when nothing the owner
              wrote exists, and say so; a manager's re-tag is labelled a
              manager's and the analysed review's band comes first; a fact
              support writes through view-as is for the account holders.
"""
import json
import sys

import pytest

import models
from models import Restaurant, create_restaurant


@pytest.fixture(autouse=True)
def _db(monkeypatch, db_path):
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
    from auth import init_auth
    init_auth(db_path=db_path)
    yield


def _rid(name="Voice Co"):
    return create_restaurant(Restaurant(name=name, owner_email=f"{name.split()[0].lower()}@x.test"))


def _exec(sql, args=()):
    conn = models.get_conn()
    try:
        conn.execute(sql, args)
        conn.commit()
    finally:
        conn.close()


_n = [0]


def _approved(rid, rating, category, signals=(), days_ago=10, draft="Thanks for coming in.", original=None):
    _n[0] += 1
    _exec("INSERT INTO reviews (restaurant_id, platform, external_id, author, rating, text, fetched_at, "
          "draft_response, original_draft, response_status, edit_category, edit_signals, approved_at) "
          "VALUES (?,'manual',?,?,?,?,?,?,?,?,?,?,datetime('now', ?))",
          (rid, f"v{_n[0]}", "g", rating, f"review {_n[0]}", "2026-09-01", draft, original, "approved", category,
           json.dumps(list(signals)), f"-{days_ago} days"))


# ── LOOPS-4 ──────────────────────────────────────────────────────────────────

def test_the_reply_style_note_survives_the_drafter_following_it():
    import drafter
    rid = _rid()
    for i in range(3):
        _approved(rid, 5, "light", ["removed_exclamations"], days_ago=30 - i, original="Thanks for coming in!")
    assert "exclamation" in drafter.get_owner_edit_note(rid, rating=5)
    for i in range(10):                                 # the drafter complies; ten approvals as drafted
        _approved(rid, 5, "unchanged", days_ago=10 - i)
    assert "exclamation" in drafter.get_owner_edit_note(rid, rating=5)     # the proof asserted ""


def test_newer_edits_that_reverse_the_lesson_outvote_it():
    import drafter
    rid = _rid()
    for i in range(3):
        _approved(rid, 5, "light", ["removed_exclamations"], days_ago=60 - i)
    for i in range(6):
        _approved(rid, 5, "light", ["added_exclamations"], days_ago=20 - i)
    note = drafter.get_owner_edit_note(rid, rating=5)
    assert "they add exclamation marks" in note and "take out exclamation" not in note


def test_marketing_style_note_reads_edited_pieces_only():
    import marketing_voice
    rid = _rid()
    for i in range(3):
        _exec("INSERT INTO marketing_edits (restaurant_id, channel, source, ref_id, original_body, final_body, "
              "edit_category, edit_signals, words_before, words_after, authority, created_at) "
              "VALUES (?,?,?,?,?,?,?,?,?,?,?,datetime('now','-40 days'))",
              (rid, "social", "post", f"e{i}", "Come in!!", "Come in.", "light",
               json.dumps(["removed_exclamations"]), 2, 2, "principal"))
    for i in range(40):
        _exec("INSERT INTO marketing_edits (restaurant_id, channel, source, ref_id, original_body, final_body, "
              "edit_category, edit_signals, words_before, words_after, authority) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
              (rid, "social", "post", f"u{i}", "Come in.", "Come in.", "unchanged", "[]", 2, 2, "principal"))
    block = marketing_voice.voice_block(rid, "social")
    assert "OWNER'S EDITS" in block


# ── PROMPTS-11 ───────────────────────────────────────────────────────────────

def test_an_edit_in_one_band_keeps_the_other_bands_approvals():
    rid = _rid()
    for _ in range(10):
        _approved(rid, 1, "unchanged", draft="We're sorry — please call us.")
    _approved(rid, 5, "light", ["removed_exclamations"], draft="Thanks!")
    got = models.get_approved_examples(rid, rating=1)
    assert got and {g["rating"] for g in got} == {1}                  # was [] (the proof)
    assert models.get_approved_examples(rid, rating=5)[0]["edited"] is True


def test_a_busy_band_no_longer_crowds_out_another_bands_approvals(monkeypatch):
    rid = _rid()
    _approved(rid, 1, "light", ["removed_apology"], days_ago=200, draft="Please call us.")
    monkeypatch.setattr(models, "EXAMPLES_POOL", 20)
    for _ in range(30):
        _approved(rid, 5, "unchanged", days_ago=1)
    got = models.get_approved_examples(rid, rating=1)
    assert got and got[0]["rating"] == 1


# ── QUALITY-18 and LOOPS-15 ──────────────────────────────────────────────────

def test_drafts_sent_as_drafted_are_never_the_owners_own_words():
    import marketing_voice
    rid = _rid()
    for i in range(3):
        _exec("INSERT INTO marketing_edits (restaurant_id, channel, source, ref_id, original_body, final_body, "
              "edit_category, edit_signals, words_before, words_after, authority) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
              (rid, "social", "post", f"u{i}", f"Model draft {i}", f"Model draft {i}", "unchanged", "[]", 3, 3,
               "principal"))
    block = marketing_voice.voice_block(rid, "social")
    assert "in their own words" not in block and "Model draft" not in block
    assert "approved as drafted" in block
    assert marketing_voice.examples([{"edit_category": "unchanged", "final_body": "x"}]) == []


def test_a_login_with_the_marketing_voice_grant_teaches_the_voice():
    import marketing_voice
    import permissions
    import auth
    rid = _rid()
    assert permissions.MARKETING_VOICE in permissions.GRANTABLE
    gm = auth.create_user(rid, "gm", "gm@voice.test", "Gm-pass-2026-long", role="manager", db_path=models.DB_PATH)
    for i in range(3):
        _exec("INSERT INTO marketing_edits (restaurant_id, channel, source, ref_id, original_body, final_body, "
              "edit_category, edit_signals, words_before, words_after, authority, user_id) "
              "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
              (rid, "social", "post", f"g{i}", "Come in!!", "Come in, friends.", "light",
               json.dumps(["removed_exclamations"]), 2, 3, "delegate", gm))
    assert "OWNER'S EDITS" not in marketing_voice.voice_block(rid, "social")
    _exec("INSERT INTO permission_grants (user_id, restaurant_id, permission) VALUES (?,?,?)",
          (gm, rid, "marketing.voice"))
    block = marketing_voice.voice_block(rid, "social")
    assert "OWNER'S EDITS" in block and "Come in, friends." in block


# ── PROMPTS-12 ───────────────────────────────────────────────────────────────

def test_accepted_recipe_drafts_are_labelled_and_only_used_alone():
    import recipes
    owner = [{"dish": "Margherita", "lines": [("mozzarella", 6.0, "oz")], "accepted_draft": False}]
    drafts = [{"dish": "Pepperoni", "lines": [("mozzarella", 5.0, "oz")], "accepted_draft": True}]
    assert "The owner's confirmed recipes" in recipes._examples_block(owner)
    b = recipes._examples_block(drafts)
    assert "Accepted drafts" in b and "The owner's confirmed" not in b


def test_a_managers_retag_is_labelled_a_managers_and_the_band_comes_first():
    import review_signals
    rid = _rid()
    ids = []
    for rating, who in ((5, "principal"), (5, "principal"), (5, "principal"), (5, "principal"), (1, "delegate")):
        _n[0] += 1
        conn = models.get_conn()
        cur = conn.execute("INSERT INTO reviews (restaurant_id, platform, external_id, rating, text, fetched_at) "
                           "VALUES (?,?,?,?,?,?)", (rid, "google", f"t{_n[0]}", rating, f"text {_n[0]}", "2026-09-01"))
        ids.append(cur.lastrowid)
        conn.execute("INSERT INTO review_retags (restaurant_id, review_id, field, before_json, after_json, "
                     "user_id, authority) VALUES (?,?,?,?,?,?,?)",
                     (rid, cur.lastrowid, "sentiment", '"positive"', '"negative"', 2, who))
        conn.commit()
        conn.close()
    ex = review_signals.retag_examples(rid, rating=1)
    assert ex[0]["rating"] == 1 and ex[0]["by"] == "manager"
    block = review_signals.analyser_block(rid, rating=1)
    assert "A manager's tags" in block


def test_a_fact_support_writes_through_view_as_is_for_the_account_holders():
    import owner_memory
    rid = _rid()
    admin = {"id": 99, "restaurant_id": rid, "is_admin": 1, "role": "client", "username": "will"}
    out = owner_memory.remember(rid, "Closed on the Fourth of July", kind="context", user=admin)
    assert out["audience"] == "principals"
