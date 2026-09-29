"""Memory audit 9/29/26, mkt_edits (workstream M6): the owner's edits to posts,
texts and newsletters are kept and taught back. Every model draft is kept
(marketing_model_drafts), every piece that goes out is measured against it with
who sent it (marketing_edits), saved drafts keep the model's first text, the
win-back draft starts from the owner's own last text, and the post, calendar,
text and email prompts read the account holder's style line, three pieces they
sent and what their regenerated drafts had in common."""
import sqlite3
import types

import pytest

import guest_marketing as gm
import marketing
import marketing_drafts
import marketing_voice as mv
import models
import ops
from models import Restaurant, create_restaurant


@pytest.fixture(autouse=True)
def _db(db_path, monkeypatch):
    real = models.get_conn
    monkeypatch.setattr(models, "get_conn", lambda *a, **k: real(db_path))
    monkeypatch.setattr(models, "DB_PATH", db_path)
    gm.init_guest_marketing(db_path)
    yield


def _rid(db_path, **kw):
    return create_restaurant(Restaurant(name="Gia Mia", owner_email="o@x.test", sign_off_name="Gia", **kw),
                             db_path=db_path)


def _conn(db_path):
    c = sqlite3.connect(db_path)
    c.row_factory = sqlite3.Row
    return c


OWNER = {"id": 11, "role": "client"}
MANAGER = {"id": 22, "role": "manager"}
DRAFT = "Truffle season is here! Come taste our fall truffle pasta tonight 🍝✨ #GiaMia #TruffleSeason #Foodie"
FINAL = "Truffle season is here. Come taste our fall truffle pasta tonight.\n— Gia"


def test_the_marketing_compare_reads_hashtags_emoji_and_a_sign_off():
    got = mv.compare(DRAFT, FINAL)
    assert {"removed_hashtags", "removed_emoji", "added_signoff", "removed_exclamations"} <= set(got["signals"])
    same = mv.compare(DRAFT, DRAFT)
    assert same["category"] == "unchanged" and same["signals"] == []
    # Same words, hashtags gone: an edit to the voice, not "unchanged".
    assert mv.compare("Pasta night #GiaMia", "Pasta night")["category"] == "light"


def _published(db_path, rid, user, n=3, draft=DRAFT, final=FINAL):
    for i in range(n):
        did = mv.record_draft(rid, "social", f"Week {i}. {draft}", "post", user_id=user["id"])
        mv.record_final(rid, "social", f"Week {i}. {final}", "post_publish", ref_id=1000 + i, user=user, draft_id=did)


def test_a_published_piece_is_measured_against_its_model_draft_with_who_sent_it(db_path):
    rid = _rid(db_path)
    did = mv.record_draft(rid, "social", DRAFT, "post", user_id=11)
    mv.record_final(rid, "social", FINAL, "post_publish", ref_id=7, user=OWNER, draft_id=did)
    c = _conn(db_path)
    row = dict(c.execute("SELECT * FROM marketing_edits").fetchone())
    used = c.execute("SELECT used_at FROM marketing_model_drafts WHERE id=?", (did,)).fetchone()[0]
    c.close()
    assert row["original_body"] == DRAFT and row["final_body"] == FINAL and row["authority"] == "principal"
    assert row["edit_category"] in ("light", "heavy", "rewrite") and "removed_hashtags" in row["edit_signals"]
    assert used


def test_a_send_with_no_draft_reference_finds_this_persons_recent_draft_by_its_words(db_path):
    rid = _rid(db_path)
    mv.record_draft(rid, "text", "Gia Mia: come in Tuesday for our fall pasta, we'd love to see you!", "campaign_draft",
                    user_id=11)
    mv.record_final(rid, "text", "Gia Mia: come in Tuesday for fall pasta. -Gia", "campaign", ref_id=3, user=OWNER)
    mv.record_final(rid, "text", "Totally different words written from scratch here", "campaign", ref_id=4,
                    user=OWNER)
    c = _conn(db_path)
    rows = {r["ref_id"]: r for r in c.execute("SELECT ref_id, original_body, edit_category FROM marketing_edits")}
    c.close()
    assert rows[3]["original_body"] and rows[3]["edit_category"] != "written"
    assert rows[4]["original_body"] is None and rows[4]["edit_category"] == "written"


def test_the_owners_voice_block_names_what_they_change_and_shows_their_words(db_path):
    import ai_guard
    rid = _rid(db_path)
    _published(db_path, rid, OWNER)
    block = mv.voice_block(rid, "social")
    assert "they take the hashtags out" in block and "they take the emoji out" in block
    assert "they end it with their own sign-off line" in block
    assert ai_guard.UNTRUSTED_OPEN in block and "— Gia" in block
    assert "never copy an offer" in block
    assert mv.voice_block(rid, "text") == ""                  # nothing learned on texts yet


def test_a_managers_or_an_admins_pieces_are_not_the_owners_voice(db_path):
    rid = _rid(db_path)
    _published(db_path, rid, MANAGER)
    _published(db_path, rid, {"id": 11, "role": "client", "acting_admin_id": 99, "device_type": "admin-view-as"})
    assert mv.voice_block(rid, "social") == ""


def test_a_demo_account_teaches_no_voice(db_path):
    rid = _rid(db_path)
    c = _conn(db_path)
    c.execute("UPDATE restaurants SET is_demo=1 WHERE id=?", (rid,))
    c.commit()
    c.close()
    _published(db_path, rid, OWNER)
    assert mv.voice_block(rid, "social") == ""


def test_drafts_thrown_away_for_another_are_a_no(db_path):
    rid = _rid(db_path)
    _published(db_path, rid, OWNER)
    for i in range(3):
        mv.record_draft(rid, "social", f"Pasta night! #GiaMia #Pasta {i}", "post", user_id=11)
    mv.record_draft(rid, "social", "The one they kept", "post", user_id=11)
    block = mv.voice_block(rid, "social")
    assert "regenerated 3 captions drafts" in block and "carry hashtags" in block


# ── saved drafts ─────────────────────────────────────────────────────────────

def test_a_saved_draft_keeps_the_models_first_text_through_every_edit(db_path):
    rid = _rid(db_path)
    did = mv.record_draft(rid, "social", DRAFT, "post", user_id=11)
    out = marketing_drafts.save_draft(rid, "My own first version", content_type="instagram_post", draft_ref=did,
                                      user_id=22, db_path=db_path)
    marketing_drafts.save_draft(rid, FINAL, content_type="instagram_post", draft_id=out["id"], db_path=db_path)
    c = _conn(db_path)
    row = c.execute("SELECT body, original_body FROM marketing_drafts WHERE id=?", (out["id"],)).fetchone()
    c.close()
    assert row["original_body"] == DRAFT and row["body"] == FINAL


def test_an_approved_draft_is_a_piece_that_went_out(db_path):
    rid = _rid(db_path)
    out = marketing_drafts.save_draft(rid, DRAFT, content_type="instagram_post", user_id=22, db_path=db_path)
    marketing_drafts.save_draft(rid, FINAL, content_type="instagram_post", draft_id=out["id"], db_path=db_path)
    res = marketing_drafts.approve_draft(out["id"], rid, user_id=11, role="client", user=OWNER, db_path=db_path)
    assert res["ok"]
    c = _conn(db_path)
    row = c.execute("SELECT source, original_body, final_body, authority FROM marketing_edits").fetchone()
    c.close()
    assert (row["source"], row["original_body"], row["final_body"], row["authority"]) == \
        ("draft_approved", DRAFT, FINAL, "principal")


# ── win-back: sent_message is read ───────────────────────────────────────────

def _contacts(db_path, rid, n=6):
    c = _conn(db_path)
    for i in range(n):
        c.execute("INSERT INTO guest_contacts (restaurant_id, phone, consent, last_visit) "
                  "VALUES (?,?,1,datetime('now','-45 days'))", (rid, f"+1312555{1000 + i}"))
    c.commit()
    c.close()


def test_the_next_win_back_draft_starts_from_the_owners_own_last_text(db_path, monkeypatch):
    rid = _rid(db_path)
    _contacts(db_path, rid)
    monkeypatch.setattr(gm, "start_campaign", lambda *a, **k: {"ok": True, "total": 6, "campaign_id": 1})
    first = gm.winback_suggestion(rid, restaurant_name="Gia Mia", db_path=db_path)["draft"]
    assert "Your table's waiting" in first["message"]
    mine = "Gia Mia: miss you! Come see us this week. -Gia"
    out = gm.send_winback(rid, first["id"], message=mine, user_id=11, user=OWNER, db_path=db_path)
    assert out["ok"], out
    c = _conn(db_path)
    row = c.execute("SELECT source, original_body, final_body FROM marketing_edits").fetchone()
    c.execute("UPDATE rec_instances SET silenced_until=NULL")
    c.commit()
    c.close()
    assert row["source"] == "winback" and row["final_body"] == mine and "Your table's waiting" in row["original_body"]
    nxt = gm.winback_suggestion(rid, restaurant_name="Gia Mia", db_path=db_path)
    assert nxt["available"] and nxt["draft"]["message"] == mine


# ── the prompts ──────────────────────────────────────────────────────────────

def _msg(text):
    return types.SimpleNamespace(content=[types.SimpleNamespace(type="text", text=text)], stop_reason="end_turn")


def test_the_post_prompt_reads_the_owners_voice_and_the_draft_is_kept(db_path, monkeypatch):
    rid = _rid(db_path)
    _published(db_path, rid, OWNER)
    seen = []
    monkeypatch.setattr(marketing, "get_client", lambda *a, **k: object())
    monkeypatch.setattr(marketing, "create_with_retry",
                        lambda *a, **k: seen.append(k["messages"][0]["content"]) or _msg("Fall pasta is here."))
    out = marketing.generate_content("instagram_post", "fall pasta", restaurant_id=rid, user_id=11)
    assert "THE OWNER'S VOICE" in seen[-1] and "they take the hashtags out" in seen[-1]
    c = _conn(db_path)
    row = c.execute("SELECT id, channel, source, user_id, content_log_id FROM marketing_model_drafts "
                    "ORDER BY id DESC LIMIT 1").fetchone()
    c.close()
    assert (row["channel"], row["source"], row["user_id"]) == ("social", "post", 11)
    assert getattr(out, "draft_ref", None) == row["id"] and row["content_log_id"] == out.content_log_id


def test_a_publish_by_a_person_is_measured_and_a_scheduled_one_is_not(db_path):
    rid = _rid(db_path)
    did = mv.record_draft(rid, "social", DRAFT, "post", user_id=11)
    marketing.log_content(rid, "instagram_post", "fall", post_id="p1", post_platform="instagram", body=FINAL,
                          user=OWNER)
    marketing.log_content(rid, "instagram_post", "fall", post_id="p2", post_platform="instagram",
                          body="A scheduled caption nobody pressed publish on")
    c = _conn(db_path)
    rows = c.execute("SELECT final_body, draft_id FROM marketing_edits").fetchall()
    c.close()
    assert [(r["final_body"], r["draft_id"]) for r in rows] == [(FINAL, did)]


def test_the_text_and_email_prompts_read_their_channels_voice(db_path, monkeypatch):
    import guest_email
    rid = _rid(db_path)
    for i in range(3):
        did = mv.record_draft(rid, "text", f"Gia Mia: we'd love to see you this week for pasta night! 🍝 {i}",
                              "campaign_draft", user_id=11)
        mv.record_final(rid, "text", f"Gia Mia: pasta night this week. -Gia {i}", "campaign", ref_id=i,
                        user=OWNER, draft_id=did)
    seen = []
    monkeypatch.setattr(gm, "get_client", lambda *a, **k: object())
    monkeypatch.setattr(gm, "create_with_retry",
                        lambda *a, **k: seen.append(k["messages"][0]["content"]) or _msg("Pasta night this week."))
    gm.draft_campaign_message(models.get_restaurant(rid), "event", goal="fill tuesday")
    assert "THE OWNER'S VOICE" in seen[-1] and "guest texts" in seen[-1]
    monkeypatch.setattr(guest_email, "get_client", lambda *a, **k: object())
    monkeypatch.setattr(guest_email, "create_with_retry",
                        lambda *a, **k: seen.append(k["messages"][0]["content"]) or _msg(
                            '{"subject": "Pasta night", "preheader": "This week", "headline": "Pasta night", '
                            '"body": "Come in for pasta night.", "button": "See the menu"}'))
    guest_email.draft_newsletter(models.get_restaurant(rid), goal="pasta night")
    assert "THE OWNER'S VOICE" not in seen[-1]            # nothing learned on email yet


def test_model_drafts_are_kept_ninety_days_by_the_one_registry():
    assert ops._RETENTION_DAYS["marketing_model_drafts"] == 90 == mv.DRAFTS_KEEP_DAYS
