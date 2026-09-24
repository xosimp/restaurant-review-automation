"""NS5 H5 / M10 (+ NS1 H6, NS2 H8) — what a public review reply may say
when nobody reads it first. Every draft below passed check_public_reply and
unsupported_commitments before (scratchpad/ns5/probe_text.py,
probe_autoapprove.py); each is now refused on the unattended paths and
flagged on the draft.
"""
import json

import pytest

import models
from ai_guard import public_reply_claims, check_review_reply, check_public_reply, reply_review_reason
from models import create_restaurant, Restaurant, update_restaurant, get_conn, get_restaurant, Review, save_reviews

REFUSED = {
    "liability": "Maria, we're so sorry our kitchen made you ill - that's entirely our fault and we take full responsibility.",
    "allergen": "Thanks Sam! Good news for next time: our whole kitchen is nut-free and every dish is safe for celiacs.",
    "allergen_safe": "Rest easy next time: our kitchen is completely nut-free and every dish is allergen-safe.",
    "inspection": "We passed our latest health inspection with a perfect score, so you can dine with total confidence.",
    "private": "Great to see you and Tom again at table 12 Tuesday - see you at your usual 7pm Friday reservation!",
    "comp_on_me": "Next round's on me when you're back!",
    "buy_drink": "We'd love to buy you a drink on your next visit.",
    "pct_word": "Mention this review for 20 percent off your next dinner.",
    "free_glass": "Your next glass of wine is free.",
    "legal": "We are fully compliant with the ADA and all Illinois accessibility law.",
    "fixed": "Thanks for telling us. This has been fixed.",
    "made_sure": "We made sure this won't happen again.",
    "make_right": "We will make it right on your next visit.",
    "guarantee": "We guarantee your next meal will be perfect.",
    "sourcing": "Our pasta is made fresh daily from local, organic ingredients.",
    "short_staffed": "That night our kitchen was short-staffed, which caused the delay.",
    "new_server": "Your server was new that night, which is why the order was wrong.",
    "normal_night": "Sorry about the wait. It never happens on a normal night.",
}

FINE = [
    "Thank you so much for the kind words, Sam! We're thrilled you loved the carbonara and hope to see you again soon.",
    "We're sorry the wait was longer than it should have been. We'd love the chance to welcome you back.",
    "Thanks for coming in! Our team loved having you, and the patio is a lovely spot on a warm night.",
    "Sorry to hear the steak wasn't cooked the way you asked. Please reach out so we can talk it through.",
    "Thanks Priya! So glad the birthday dinner was a hit. See you next time.",
]


@pytest.mark.parametrize("key", sorted(REFUSED))
def test_each_probe_draft_is_refused_for_unattended_publish(key):
    draft = REFUSED[key]
    assert check_public_reply(draft) is None or key == "private"     # the old check let (almost) all through
    assert public_reply_claims(draft), key
    assert check_review_reply(draft), key
    assert reply_review_reason(draft), key


@pytest.mark.parametrize("draft", FINE)
def test_an_ordinary_warm_reply_is_not_flagged(draft):
    assert public_reply_claims(draft) == []
    assert check_review_reply(draft) is None


def test_a_cause_or_sourcing_claim_the_owner_wrote_down_is_theirs():
    notes = "Our pasta is made fresh daily. We were short-staffed on Friday because of the flu going around."
    assert public_reply_claims("Our pasta is made fresh daily, and we hope you try it again.", notes) == []
    assert public_reply_claims("We were short-staffed on Friday because of the flu going around, sorry.", notes) == []
    # An allergen promise is never allowed, whoever wrote it first.
    assert public_reply_claims("Our kitchen is nut-free.", "we are a nut-free kitchen")


# ── the health keywords: allergies and undercooking ────────────────────

@pytest.mark.parametrize("text", [
    "the chicken was pink in the middle and I was up all night",
    "my son had a peanut reaction after the pad thai",
    "I told the server about my shellfish allergy and they ignored it",
    "My son reacted to something in the pad thai, not sure what.",
    "burger was undercooked",
])
def test_allergy_and_undercooking_are_health_keywords(text):
    from notify import health_keyword_hits
    assert health_keyword_hits(text), text


def test_a_plain_review_is_still_not_a_health_hit():
    from notify import health_keyword_hits
    assert health_keyword_hits("Great pasta, lovely staff, my first reaction was wow") == []


# ── auto-approve ───────────────────────────────────────────────────────

@pytest.fixture
def db(db_path, monkeypatch):
    real = models.get_conn
    import scheduler, client_api
    for mod in (models, scheduler, client_api):
        monkeypatch.setattr(mod, "get_conn", lambda *a, **k: real(db_path), raising=False)
    monkeypatch.setattr(models, "DB_PATH", db_path)
    return db_path


def _earned_restaurant(db):
    rid = create_restaurant(Restaurant(name="AA", owner_email="a@x.com"), db_path=db)
    update_restaurant(rid, {"auto_approve_5star": 1, "auto_approve_earned": 1, "auto_approve_daily_cap": 5,
                            "billing_status": "active"}, db_path=db)
    revs = [Review(restaurant_id=rid, platform="google", external_id=f"h{i}", author="g", rating=3, text="ok")
            for i in range(10)]
    save_reviews(revs, db_path=db)
    conn = get_conn(db)
    conn.execute("UPDATE reviews SET response_status='approved', approved_at=datetime('now','-2 days'), "
                 "draft_response='Thanks!', draft_edited=0, response_action='approved' "
                 "WHERE restaurant_id=? AND external_id LIKE 'h%'", (rid,))
    conn.commit()
    conn.close()
    return rid


def _new_review(db, rid, ext, rating, text, draft, **cols):
    save_reviews([Review(restaurant_id=rid, platform="google", external_id=ext, author="Sam", rating=rating,
                         text=text)], db_path=db)
    conn = get_conn(db)
    rev_id = conn.execute("SELECT id FROM reviews WHERE external_id=? AND restaurant_id=?", (ext, rid)).fetchone()[0]
    sets = {"draft_response": draft, "response_status": "drafted", **cols}
    conn.execute("UPDATE reviews SET " + ", ".join(f"{k}=?" for k in sets) + " WHERE id=?", (*sets.values(), rev_id))
    conn.commit()
    conn.close()
    return rev_id


def test_the_probe_3_star_allergen_reply_is_not_auto_approved(db, monkeypatch):
    """probe_autoapprove.py: a 3-star review mentioning a reaction, a reply
    promising a nut-free kitchen, and the earned band published it."""
    import client_api, scheduler
    rid = _earned_restaurant(db)
    rev = _new_review(db, rid, "new3", 3, "Food was good, service slow. My son reacted to something in the pad thai.",
                      "Thanks Sam! Sorry the service dragged. Rest easy next time: our kitchen is completely "
                      "nut-free and every dish is allergen-safe.", processed=1)
    calls = []
    monkeypatch.setattr(client_api, "_do_approve", lambda review_id, r, auto=False: (calls.append(review_id) or ({"ok": True}, 200)))
    scheduler.auto_approve_five_stars(rid, get_restaurant(rid, db))
    assert rev not in calls


def test_a_clean_draft_is_still_held_on_a_3_star_review_with_a_complaint(db, monkeypatch):
    import client_api, scheduler
    rid = _earned_restaurant(db)
    held = _new_review(db, rid, "c3", 3, "Food fine but the wait was 40 minutes.", "Thanks for coming in, Sam!",
                       processed=1, specific_complaint="40 minute wait", categories=json.dumps(["wait_time"]))
    clean = _new_review(db, rid, "k3", 3, "Pretty good overall.", "Thanks for coming in, Sam!",
                        processed=1, sentiment="positive", categories=json.dumps(["food_quality"]))
    calls = []
    monkeypatch.setattr(client_api, "_do_approve", lambda review_id, r, auto=False: (calls.append(review_id) or ({"ok": True}, 200)))
    scheduler.auto_approve_five_stars(rid, get_restaurant(rid, db))
    assert held not in calls and clean in calls


def test_a_five_star_reply_with_a_comp_is_held_and_marked(db, monkeypatch):
    import client_api, scheduler
    rid = create_restaurant(Restaurant(name="F", owner_email="f@x.com"), db_path=db)
    update_restaurant(rid, {"auto_approve_5star": 1, "auto_approve_daily_cap": 5, "billing_status": "active"}, db_path=db)
    rev = _new_review(db, rid, "f5", 5, "Loved it", "Thanks! Next round's on me when you're back.", processed=1)
    calls = []
    monkeypatch.setattr(client_api, "_do_approve", lambda review_id, r, auto=False: (calls.append(review_id) or ({"ok": True}, 200)))
    scheduler.auto_approve_five_stars(rid, get_restaurant(rid, db))
    assert calls == []
    conn = get_conn(db)
    row = conn.execute("SELECT draft_needs_review, draft_review_reason FROM reviews WHERE id=?", (rev,)).fetchone()
    conn.close()
    assert row["draft_needs_review"] == 1 and "comp" in row["draft_review_reason"]


# ── M10: bulk approve checks every reply ───────────────────────────────

def test_bulk_approve_holds_a_reply_that_makes_a_claim(db, monkeypatch):
    import client_api
    rid = create_restaurant(Restaurant(name="B", owner_email="b@x.com"), db_path=db)
    bad = _new_review(db, rid, "b1", 2, "Got sick", "We take full responsibility and the next dinner is free.",
                      processed=1)
    good = _new_review(db, rid, "b2", 5, "Great", "Thanks so much, see you soon!", processed=1)
    approved = []
    monkeypatch.setattr(client_api, "_do_approve",
                        lambda review_id, r, google=None, bulk=False: (approved.append(review_id) or ({"ok": True}, 200)))
    payload, status = client_api._do_approve_all(rid)
    assert status == 200 and approved == [good]
    assert payload.get("held_for_review") == 1
    conn = get_conn(db)
    row = conn.execute("SELECT draft_needs_review FROM reviews WHERE id=?", (bad,)).fetchone()
    conn.close()
    assert row["draft_needs_review"] == 1
