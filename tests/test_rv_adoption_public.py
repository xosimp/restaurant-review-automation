"""Workstream A — the Response Validation Layer at the public call sites.

Every text below reaches a guest (a Google reply, a guest text, a social
post, a content-calendar idea). Each site now runs response_validation on
its public surface (reply_public, guest_sms, social_post, calendar_idea),
which runs check_public_reply / check_marketing_copy WITH the never-say
list, the public-reply claims and commitments, the engine's own P1 list
(awards, comps, sourcing, allergen, fault, inspections, promises, private
details, a staff member named in public), other tenants' names (T1) and
injection residue (I1). Anything above a rewrite refuses.

Each case here passed the hand-rolled check it replaced. No model, SMS,
email, post or scheduler run is reached: model calls are stubbed and the
auto-approve decision is called directly.
"""
import json
import sqlite3
import types

import pytest

import models
from models import Restaurant, Review, create_restaurant, get_restaurant, save_reviews, update_restaurant


def _msg(text):
    return types.SimpleNamespace(content=[types.SimpleNamespace(type="text", text=text)], stop_reason="end_turn")


def _conn(db_path):
    c = sqlite3.connect(db_path)
    c.row_factory = sqlite3.Row
    return c


@pytest.fixture
def db(db_path, monkeypatch):
    real = models.get_conn
    import scheduler, client_api, drafter
    for mod in (models, scheduler, client_api):
        monkeypatch.setattr(mod, "get_conn", lambda *a, **k: real(db_path), raising=False)
    monkeypatch.setattr(models, "DB_PATH", db_path)
    import guest_marketing
    guest_marketing.init_guest_marketing(db_path=db_path)
    return db_path


def _restaurant(db, **cols):
    rid = create_restaurant(Restaurant(name=cols.pop("name", "Gia Mia"), owner_email="o@x.test"), db_path=db)
    cols.setdefault("billing_status", "active")
    update_restaurant(rid, cols, db_path=db)
    return rid


def _review(db, rid, ext, rating, text, draft, author="Sam Lee", **cols):
    save_reviews([Review(restaurant_id=rid, platform="google", external_id=ext, author=author, rating=rating,
                         text=text)], db_path=db)
    c = _conn(db)
    rev = c.execute("SELECT id FROM reviews WHERE external_id=? AND restaurant_id=?", (ext, rid)).fetchone()[0]
    sets = {"draft_response": draft, "response_status": "drafted", "processed": 1, **cols}
    c.execute("UPDATE reviews SET " + ", ".join(f"{k}=?" for k in sets) + " WHERE id=?", (*sets.values(), rev))
    c.commit()
    c.close()
    return rev


def _flag(db, rev):
    c = _conn(db)
    row = c.execute("SELECT draft_needs_review, draft_review_reason, draft_response FROM reviews WHERE id=?",
                    (rev,)).fetchone()
    c.close()
    return row


# ── 1. reply drafts (drafter.draft_response) ───────────────────────────

def _draft(db, monkeypatch, rid, rev, model_text, **kw):
    import drafter
    monkeypatch.setattr(drafter, "get_client", lambda *a, **k: object())
    monkeypatch.setattr(drafter, "create_with_retry", lambda *a, **k: _msg(model_text))
    r = get_restaurant(rid, db)
    return drafter.draft_response(rev, 5, "Loved the carbonara!", "positive", r.name, restaurant_id=rid,
                                  voice_notes=kw.get("voice_notes", ""), never_say=kw.get("never_say", ""))


def test_a_draft_claiming_an_award_is_held_with_the_reason(db, monkeypatch):
    rid = _restaurant(db)
    rev = _review(db, rid, "d1", 5, "Loved the carbonara!", None, response_status="pending")
    text = "Thanks Sam! We were voted the best pasta in Chicago, so we're thrilled you agree."
    out = _draft(db, monkeypatch, rid, rev, text)
    row = _flag(db, rev)
    assert row["draft_needs_review"] == 1
    assert "award" in row["draft_review_reason"] and "voted" in row["draft_review_reason"]
    # The draft is kept for the owner to edit, not thrown away.
    assert row["draft_response"] == text and out == text


def test_a_draft_using_a_never_say_term_is_held(db, monkeypatch):
    rid = _restaurant(db)
    rev = _review(db, rid, "d2", 5, "Loved the carbonara!", None, response_status="pending")
    _draft(db, monkeypatch, rid, rev, "Thanks Sam! So glad you loved our cheap eats.", never_say="cheap")
    row = _flag(db, rev)
    assert row["draft_needs_review"] == 1 and "cheap" in row["draft_review_reason"]


def test_a_draft_naming_another_cavnar_restaurant_is_held(db, monkeypatch):
    _restaurant(db, name="Luigi's Trattoria")
    rid = _restaurant(db)
    rev = _review(db, rid, "d3", 5, "Loved the carbonara!", None, response_status="pending")
    _draft(db, monkeypatch, rid, rev, "Thanks Sam! Better than Luigi's Trattoria, we hope.")
    assert _flag(db, rev)["draft_needs_review"] == 1


def test_a_clean_draft_is_stored_unflagged_and_carries_its_verdict(db, monkeypatch):
    import response_validation as rv
    rid = _restaurant(db)
    rev = _review(db, rid, "d4", 5, "Loved the carbonara!", None, response_status="pending")
    out = _draft(db, monkeypatch, rid, rev, "Thank you so much, Sam! We're thrilled you loved the carbonara.")
    row = _flag(db, rev)
    assert row["draft_needs_review"] == 0 and row["draft_review_reason"] is None
    assert rv.validation_of(out)["verdict"] == "pass" and rv.validation_of(out)["version"] == rv.VERSION


def test_a_sourcing_claim_the_owner_wrote_in_menu_notes_is_theirs(db, monkeypatch):
    rid = _restaurant(db, menu_notes="All pasta is made fresh daily in-house.")
    rev = _review(db, rid, "d5", 5, "Loved the carbonara!", None, response_status="pending")
    _draft(db, monkeypatch, rid, rev, "Thanks Sam! Our pasta is made fresh daily, so come back soon.")
    assert _flag(db, rev)["draft_needs_review"] == 0


# ── 2. auto-approve (scheduler.auto_approve_five_stars) ────────────────

def _auto(db, monkeypatch, rid):
    import client_api, scheduler
    calls = []
    monkeypatch.setattr(client_api, "_do_approve",
                        lambda review_id, r, auto=False: (calls.append(review_id) or ({"ok": True}, 200)))
    scheduler.auto_approve_five_stars(rid, get_restaurant(rid, db))
    return calls


def test_auto_approve_holds_an_award_claim(db, monkeypatch):
    rid = _restaurant(db, auto_approve_5star=1, auto_approve_daily_cap=5)
    bad = _review(db, rid, "a1", 5, "Great night", "Thanks Sam! Voted #1 pizza in town three years running.")
    good = _review(db, rid, "a2", 5, "Great night", "Thanks so much, Sam! See you soon.")
    calls = _auto(db, monkeypatch, rid)
    assert good in calls and bad not in calls
    row = _flag(db, bad)
    # The plain reason, read after "This reply …" on every surface — not
    # "held from auto-approve: …" (drafter.owner_reason; the log names the path).
    assert row["draft_needs_review"] == 1 and row["draft_review_reason"]
    assert not row["draft_review_reason"].startswith("held from")


def test_auto_approve_holds_a_reply_naming_another_tenant(db, monkeypatch):
    _restaurant(db, name="Luigi's Trattoria")
    rid = _restaurant(db, auto_approve_5star=1, auto_approve_daily_cap=5)
    bad = _review(db, rid, "a3", 5, "Great night", "Thanks Sam! Say hi to the folks at Luigi's Trattoria.")
    assert bad not in _auto(db, monkeypatch, rid)


def test_auto_approve_holds_a_staff_member_named_in_public(db, monkeypatch):
    rid = _restaurant(db, auto_approve_5star=1, auto_approve_daily_cap=5)
    bad = _review(db, rid, "a4", 5, "Great night", "Thanks Sam! Sorry about Kevin, he is still learning.")
    assert bad not in _auto(db, monkeypatch, rid)


# ── 3. bulk approve via Ask (the approve_all_reviews card) ─────────────

def test_the_ask_approve_all_card_holds_an_award_claim(db, monkeypatch):
    import ask_cavnar_tools as t
    rid = _restaurant(db)
    _review(db, rid, "b1", 5, "x", "Thanks Ann, see you soon!", author="Ann", fetched_at="2099-01-01 00:00:00")
    _review(db, rid, "b2", 5, "x", "Thanks Bea! Proud to be award-winning.", author="Bea",
            fetched_at="2099-01-01 00:00:00")
    c = _conn(db)
    c.execute("UPDATE reviews SET fetched_at=datetime('now') WHERE restaurant_id=?", (rid,))
    c.commit()
    c.close()
    p = t.build_proposal("approve_all_reviews", {}, restaurant_id=rid)
    d = {x["label"]: x["value"] for x in p["details"]}
    assert d["Replies that would post"] == "1"
    assert "Held for you to read" in d
    assert "Thanks Ann, see you soon!" in d.values()
    assert not any("award-winning" in v for v in d.values())


# ── 4. guest SMS (guest_marketing.draft_campaign_message / send_winback) ──

def _sms(db, monkeypatch, rid, text, topic=""):
    import guest_marketing as gm
    monkeypatch.setattr(gm, "get_client", lambda *a, **k: object())
    monkeypatch.setattr(gm, "create_with_retry", lambda client, **kw: _msg(text))
    return gm.draft_campaign_message(get_restaurant(rid, db), campaign_type="general", topic=topic)


def test_guest_text_with_a_never_say_term_is_rejected(db, monkeypatch):
    """NS6 A3 #6: the prompt carried the never-say list, the check did not."""
    rid = _restaurant(db, never_say="cheap")
    with pytest.raises(ValueError, match="campaign copy rejected"):
        _sms(db, monkeypatch, rid, "Cheap eats all week at Gia Mia, come see us!")


def test_guest_text_with_an_award_claim_is_rejected(db, monkeypatch):
    rid = _restaurant(db)
    with pytest.raises(ValueError, match="campaign copy rejected"):
        _sms(db, monkeypatch, rid, "Chicago's award-winning lasagna is back Friday. See you there!")


def test_guest_text_with_an_award_the_owner_named_passes(db, monkeypatch):
    import response_validation as rv
    rid = _restaurant(db)
    out = _sms(db, monkeypatch, rid, "Our award-winning lasagna is back Friday. See you there!",
               topic="award-winning lasagna back Friday")
    assert out.startswith("Our award-winning lasagna") and rv.validation_of(out)["verdict"] == "pass"


def test_a_winback_send_with_a_never_say_term_is_not_sent(db, monkeypatch):
    import guest_marketing as gm
    rid = _restaurant(db, never_say="cheap")
    c = _conn(db)
    c.execute("INSERT INTO guest_campaign_drafts (restaurant_id, kind, segment, segment_size, message, rec_key) "
              "VALUES (?,?,?,?,?,?)", (rid, "winback", "lapsed_30", 6, "We miss you!", "winback:lapsed_30"))
    c.commit()
    draft_id = c.execute("SELECT id FROM guest_campaign_drafts WHERE restaurant_id=?", (rid,)).fetchone()[0]
    c.close()
    started = []
    monkeypatch.setattr(gm, "start_campaign", lambda *a, **k: started.append(a) or {"ok": True, "total": 6})
    out = gm.send_winback(rid, draft_id, message="Cheap eats are back, come see us!", db_path=db)
    assert out["ok"] is False and "cheap" in out["error"] and started == []


# ── 5. social post (marketing.generate_content) ────────────────────────

def _post(db, monkeypatch, rid, text, topic="fall menu"):
    import marketing
    monkeypatch.setattr(marketing, "get_client", lambda *a, **k: object())
    monkeypatch.setattr(marketing, "create_with_retry", lambda *a, **k: _msg(text))
    monkeypatch.setattr(marketing, "extract_text", lambda m: m.content[0].text)
    return marketing.generate_content("instagram_post", topic, restaurant_id=rid)


def test_a_post_calling_the_restaurant_famous_is_refused(db, monkeypatch):
    rid = _restaurant(db)
    with pytest.raises(ValueError, match="marketing copy rejected"):
        _post(db, monkeypatch, rid, "Our famous meatballs are back this week! #GiaMia")


def test_a_post_offering_a_discount_nobody_set_is_refused(db, monkeypatch):
    rid = _restaurant(db)
    with pytest.raises(ValueError, match="marketing copy rejected"):
        _post(db, monkeypatch, rid, "20% off every pizza this Friday! #GiaMia")


def test_a_post_repeating_what_the_owner_is_known_for_passes(db, monkeypatch):
    import response_validation as rv
    rid = _restaurant(db, known_for="famous meatballs and Sunday gravy")
    out = _post(db, monkeypatch, rid, "Our famous meatballs are back this week! #GiaMia")
    assert out.startswith("Our famous meatballs") and rv.validation_of(out)["verdict"] == "pass"


# ── 6. content calendar ideas (marketing.get_content_calendar_ideas) ───

def _week(monkeypatch, ideas):
    import marketing
    monkeypatch.setattr(marketing, "get_client", lambda *a, **k: object())
    monkeypatch.setattr(marketing, "create_with_retry", lambda *a, **k: ideas)
    monkeypatch.setattr(marketing, "extract_text", lambda m: json.dumps(m))


IDEAS = [
    {"day": "Monday", "platform": "Instagram & FB", "angle": "Truffle pasta close-up", "type": "instagram_post"},
    {"day": "Tuesday", "platform": "SMS", "angle": "Text regulars: 50% off apps Tuesday", "type": "loyalty_nudge"},
    {"day": "Wednesday", "platform": "Google", "angle": "Announce we were voted #1 pizza in Chicago",
     "type": "google_promo"},
    {"day": "Thursday", "platform": "Email", "angle": "Weekly email: the new fall menu", "type": "weekly_email"},
]


def test_calendar_ideas_the_engine_refuses_are_dropped(db, monkeypatch):
    import marketing
    import response_validation as rv
    rid = _restaurant(db)
    _week(monkeypatch, IDEAS)
    out = marketing.get_content_calendar_ideas(restaurant_id=rid)
    angles = [i["angle"] for i in out]
    assert angles == ["Truffle pasta close-up", "Weekly email: the new fall menu"]
    assert all(i["validation"]["version"] == rv.VERSION for i in out)
    # What was cached is what was shown.
    assert marketing.get_cached_calendar(rid) == out


def test_a_calendar_cached_before_the_engine_is_revalidated_on_read(db, monkeypatch):
    import marketing
    rid = _restaurant(db)
    marketing._cache_calendar(rid, [dict(i) for i in IDEAS])       # an old row: no verdict on any idea
    out = marketing.get_cached_calendar(rid)
    assert [i["angle"] for i in out] == ["Truffle pasta close-up", "Weekly email: the new fall menu"]
