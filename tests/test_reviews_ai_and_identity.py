"""Audit #10 remediation: what the AI publishes, and which listing it lands on.

A drafted reply goes onto a public Google listing under the owner's name,
and the location it lands on was picked by taking the first item Google
happened to return. These tests hold both of those.
"""
import types

import pytest

import analyser
import drafter
import fetcher
import gmb
import models
import notify


# ── Which Google listing this restaurant is bound to ───────────────────────

def _stub_google(monkeypatch, accounts, locations_by_account):
    monkeypatch.setattr(gmb, "list_gmb_accounts", lambda tok: accounts)
    monkeypatch.setattr(gmb, "list_gmb_locations",
                        lambda tok, acct: locations_by_account.get(acct, []))


def _loc(name, place_id, title):
    return {"name": name, "title": title, "metadata": {"placeId": place_id}}


def test_the_location_is_matched_on_this_restaurants_place_id(monkeypatch):
    """get_gmb_location_id took place_id as a parameter and never read it —
    it returned locations[0] of accounts[0]. An owner with three restaurants
    under one Business Profile connected all three to the same listing, so
    each fetched the same reviews and posted replies to the same listing."""
    _stub_google(monkeypatch, [{"name": "accounts/1"}], {"accounts/1": [
        _loc("locations/10", "ChIJ_downtown", "Downtown"),
        _loc("locations/11", "ChIJ_uptown", "Uptown"),
        _loc("locations/12", "ChIJ_airport", "Airport"),
    ]})
    got = gmb.find_gmb_location("tok", "ChIJ_uptown")
    assert got["ok"] is True
    assert got["location"] == "locations/11"
    assert got["title"] == "Uptown"


def test_each_sibling_resolves_to_its_own_listing(monkeypatch):
    _stub_google(monkeypatch, [{"name": "accounts/1"}], {"accounts/1": [
        _loc("locations/10", "ChIJ_downtown", "Downtown"),
        _loc("locations/11", "ChIJ_uptown", "Uptown"),
    ]})
    a = gmb.find_gmb_location("tok", "ChIJ_downtown")["location"]
    b = gmb.find_gmb_location("tok", "ChIJ_uptown")["location"]
    assert a != b


def test_a_location_in_a_second_account_is_still_found(monkeypatch):
    _stub_google(monkeypatch, [{"name": "accounts/1"}, {"name": "accounts/2"}], {
        "accounts/1": [_loc("locations/10", "ChIJ_other", "Other")],
        "accounts/2": [_loc("locations/20", "ChIJ_mine", "Mine")],
    })
    got = gmb.find_gmb_location("tok", "ChIJ_mine")
    assert got["ok"] and got["account"] == "accounts/2" and got["location"] == "locations/20"


def test_no_match_is_an_error_not_a_first_item_guess(monkeypatch):
    """Binding to the wrong listing is worse than not connecting."""
    _stub_google(monkeypatch, [{"name": "accounts/1"}], {"accounts/1": [
        _loc("locations/10", "ChIJ_somebody_else", "Somebody Else"),
    ]})
    got = gmb.find_gmb_location("tok", "ChIJ_mine")
    assert got["ok"] is False
    assert "match" in got["error"].lower()
    assert got["choices"] and got["choices"][0]["title"] == "Somebody Else"


def test_a_restaurant_with_no_place_id_cannot_connect(monkeypatch):
    _stub_google(monkeypatch, [{"name": "accounts/1"}], {"accounts/1": [
        _loc("locations/10", "ChIJ_anything", "Anything")]})
    got = gmb.find_gmb_location("tok", "")
    assert got["ok"] is False and "Place ID" in got["error"]


# ── Ratings are never invented ─────────────────────────────────────────────

def test_an_unspecified_star_rating_is_skipped_not_called_three(monkeypatch):
    """star_map.get(..., 3) turned Google's STAR_RATING_UNSPECIFIED into a
    three-star review nobody left, which then entered the average."""
    class _Resp:
        def raise_for_status(self): pass
        def json(self):
            return {"reviews": [
                {"name": "n/1", "starRating": "FIVE", "comment": "Great",
                 "createTime": "2026-09-01T00:00:00Z", "reviewer": {"displayName": "A"}},
                {"name": "n/2", "starRating": "STAR_RATING_UNSPECIFIED", "comment": "?",
                 "createTime": "2026-09-02T00:00:00Z", "reviewer": {"displayName": "B"}},
            ]}
    monkeypatch.setattr(gmb.requests, "get", lambda *a, **kw: _Resp())
    out = gmb.fetch_reviews_via_gmb("tok", "locations/1", 1)
    assert [r.rating for r in out] == [5]


def test_the_review_date_is_when_it_was_written_not_last_touched(monkeypatch):
    """updateTime moves when anybody REPLIES to a review, so importing a
    backlog a previous agency had answered dated every one of those reviews
    to the day of the reply — and the fetch was insert-only, so that wrong
    date was then frozen."""
    class _Resp:
        def raise_for_status(self): pass
        def json(self):
            return {"reviews": [{"name": "n/1", "starRating": "FOUR", "comment": "Fine",
                                 "createTime": "2023-04-05T10:00:00Z",
                                 "updateTime": "2026-09-09T10:00:00Z",
                                 "reviewer": {"displayName": "A"}}]}
    monkeypatch.setattr(gmb.requests, "get", lambda *a, **kw: _Resp())
    out = gmb.fetch_reviews_via_gmb("tok", "locations/1", 1)
    assert out[0].review_date.startswith("2023-04-05")
    assert out[0].source_updated_at.startswith("2026-09-09")


def test_a_places_review_with_no_usable_rating_is_skipped(monkeypatch):
    """rating=0 would fail the table's own CHECK and be swallowed as an
    IntegrityError, dropping the review with no trace."""
    class _Resp:
        def raise_for_status(self): pass
        def json(self):
            return {"result": {"reviews": [
                {"time": 1757000000, "rating": 5, "text": "Good", "author_name": "A"},
                {"time": 1757000001, "text": "No rating", "author_name": "B"},
            ]}}
    monkeypatch.setattr(fetcher.requests, "get", lambda *a, **kw: _Resp())
    monkeypatch.setattr(fetcher, "_meter_places", lambda *a, **kw: None)
    out = fetcher.fetch_google("place", 1)
    assert [r.rating for r in out] == [5]


def test_two_anonymous_reviewers_in_one_second_do_not_collide(monkeypatch):
    """external_id was time + author_name, so two "Anonymous" reviewers at
    the same second produced one id and the second review was dropped as a
    duplicate."""
    class _Resp:
        def raise_for_status(self): pass
        def json(self):
            return {"result": {"reviews": [
                {"time": 1757000000, "rating": 5, "text": "a", "author_name": "Anonymous",
                 "author_url": "https://maps.google.com/u/1"},
                {"time": 1757000000, "rating": 1, "text": "b", "author_name": "Anonymous",
                 "author_url": "https://maps.google.com/u/2"},
            ]}}
    monkeypatch.setattr(fetcher.requests, "get", lambda *a, **kw: _Resp())
    monkeypatch.setattr(fetcher, "_meter_places", lambda *a, **kw: None)
    out = fetcher.fetch_google("place", 1)
    assert len({r.external_id for r in out}) == 2


# ── Sentiment cannot contradict the stars the guest chose ──────────────────

@pytest.mark.parametrize("rating,model_says,expected", [
    (1, "positive", "negative"),
    (2, "positive", "negative"),
    (5, "negative", "neutral"),
    (3, "positive", "positive"),
    (4, "neutral", "neutral"),
])
def test_sentiment_is_floored_by_the_star_rating(rating, model_says, expected):
    """"The staff were lovely but I was ill afterwards" on a one-star could
    classify positive, which dropped it out of the negative spike count and
    drew it as a positive on the sentiment chart."""
    out = analyser._validate_analysis(
        {"sentiment": model_says, "categories": ["service"], "summary": "s", "urgency": "normal"},
        rating=rating)
    assert out["sentiment"] == expected


# ── The health alert reads the analysis, not a substring ───────────────────

def test_a_negated_mention_is_not_a_health_emergency():
    """"roach" is in HEALTH_KEYWORDS and negation is invisible to a
    substring match. drafter.py moved off keywords for exactly this reason
    and documented this exact false positive; the alert path, which is the
    one that texts the owner at 11pm, had not."""
    text = "No roach problem here at all, unlike the place down the road. Five stars."
    assert notify._is_health_alert(text, urgency="normal") is False


def test_a_real_health_concern_still_alerts():
    assert notify._is_health_alert("I was violently ill after eating here", urgency="high") is True


def test_the_keyword_list_still_covers_an_unanalysed_review():
    """Fallback only — a review that could not be analysed has no urgency."""
    assert notify._is_health_alert("I got food poisoning", urgency=None) is True


# ── A public reply may not state an action nobody took ─────────────────────

@pytest.mark.parametrize("draft", [
    "So sorry about the wait — we have retrained our floor team.",
    "We're sorry. A new supplier is now in place for our produce.",
    "This has been escalated to our general manager.",
    "We are now hiring more kitchen staff to fix this.",
])
def test_an_invented_commitment_is_flagged(draft):
    from ai_guard import unsupported_commitments
    assert unsupported_commitments(draft)


@pytest.mark.parametrize("draft", [
    "We're truly sorry you had this experience. Please email us and we'll make it right.",
    "Thank you for the kind words, Marcus — see you next Friday!",
    "Sorry the wings were cold. We'd love the chance to get it right next time.",
])
def test_an_ordinary_reply_is_not_flagged(draft):
    from ai_guard import unsupported_commitments
    assert unsupported_commitments(draft) == []


def test_a_flagged_draft_is_stored_with_its_reason(db_path, monkeypatch):
    real = models.get_conn
    monkeypatch.setattr(models, "get_conn", lambda *a, **k: real(db_path))
    monkeypatch.setattr(drafter, "get_conn", lambda *a, **k: real(db_path))
    conn = real(db_path)
    conn.execute("INSERT INTO restaurants (id,name,owner_email) VALUES (1,'R','o@x.test')")
    conn.execute("""INSERT INTO reviews (id,restaurant_id,platform,external_id,author,rating,
                    text,fetched_at) VALUES (7,1,'google','x','Ann',1,'Cold food','2026-09-01')""")
    conn.commit()
    conn.close()
    monkeypatch.setattr(drafter, "create_with_retry", lambda *a, **kw: types.SimpleNamespace(
        content=[types.SimpleNamespace(text="Sorry — we have retrained our kitchen team.")],
        stop_reason="end_turn"))
    drafter.draft_response(7, 1, "Cold food", "negative", "R", restaurant_id=1)
    conn = real(db_path)
    row = conn.execute("SELECT draft_needs_review, draft_review_reason FROM reviews "
                       "WHERE id=7").fetchone()
    conn.close()
    assert row["draft_needs_review"] == 1
    assert "may not have taken" in row["draft_review_reason"]


def test_a_flagged_draft_is_never_auto_published(db_path):
    """auto_approve_candidates is the only query the auto-approve rule reads.
    A reply claiming an action nobody took is exactly what must not go out
    with nobody reading it."""
    conn = models.get_conn(db_path)
    conn.execute("INSERT INTO restaurants (id,name,owner_email) VALUES (1,'R','o@x.test')")
    conn.execute("""INSERT INTO reviews (restaurant_id,platform,external_id,author,rating,text,
                    fetched_at,response_status,draft_response,draft_needs_review)
                    VALUES (1,'google','x','Ann',5,'Great','2026-09-01','drafted','Thanks!',1)""")
    conn.execute("""INSERT INTO reviews (restaurant_id,platform,external_id,author,rating,text,
                    fetched_at,response_status,draft_response,draft_needs_review)
                    VALUES (1,'google','y','Bob',5,'Great','2026-09-01','drafted','Thanks!',0)""")
    conn.commit()
    conn.close()
    ids = [c["id"] for c in models.auto_approve_candidates(1, db_path=db_path)]
    assert len(ids) == 1


# ── The reply drafter's "recurring theme" note ─────────────────────────────

def test_recurring_themes_needs_a_real_pattern_in_a_real_window(db_path, monkeypatch):
    """Was "{n} negative reviews recently" with NO date filter and a count
    capped by its own LIMIT, then told the model to say the pattern "is
    being actively addressed" — a claim about the restaurant's operations
    that this system has no way to make, published publicly."""
    real = models.get_conn
    monkeypatch.setattr(drafter, "get_conn", lambda *a, **k: real(db_path))
    conn = real(db_path)
    conn.execute("INSERT INTO restaurants (id,name,owner_email) VALUES (1,'R','o@x.test')")
    for i in range(4):
        conn.execute("""INSERT INTO reviews (restaurant_id,platform,external_id,author,rating,
                        text,review_date,fetched_at,sentiment,categories,response_status)
                        VALUES (1,'google',?,?,1,'slow',datetime('now','-900 days'),
                                datetime('now'),'negative','["wait_time"]','pending')""",
                     (f"old{i}", f"A{i}"))
    conn.commit()
    conn.close()
    assert drafter.get_recurring_themes(1) == "", "3-year-old complaints are not a pattern"


def test_recurring_themes_names_the_actual_categories(db_path, monkeypatch):
    real = models.get_conn
    monkeypatch.setattr(drafter, "get_conn", lambda *a, **k: real(db_path))
    conn = real(db_path)
    conn.execute("INSERT INTO restaurants (id,name,owner_email) VALUES (1,'R','o@x.test')")
    for i in range(3):
        conn.execute("""INSERT INTO reviews (restaurant_id,platform,external_id,author,rating,
                        text,review_date,fetched_at,sentiment,categories,response_status)
                        VALUES (1,'google',?,?,1,'slow',datetime('now','-10 days'),
                                datetime('now'),'negative','["wait_time"]','pending')""",
                     (f"new{i}", f"A{i}"))
    conn.commit()
    conn.close()
    note = drafter.get_recurring_themes(1)
    assert "wait time" in note
    assert "actively addressed" not in note
    assert "Do NOT claim any specific fix" in note
