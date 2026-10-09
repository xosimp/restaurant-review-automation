"""Search phrases worked into review replies (reply_keywords; Danny at
Simple EJ's, 10/9/26): one per reply at most, only where it fits, never on an
unhappy guest's reply, the phrase matching the review first, then the one
used least; a draft that breaks it is written again."""
import json

import reply_keywords as rk
from models import Restaurant, create_restaurant, get_conn, get_restaurant, update_restaurant

KW = ["brunch in St. Charles", "smash burger", "patio"]


def test_clean_dedupes_trims_and_caps():
    assert rk.clean("Brunch in St. Charles, brunch in st. charles\n patio ;; x") == ["Brunch in St. Charles", "patio"]
    assert len(rk.clean([f"phrase {i}" for i in range(40)])) == rk.MAX_KEYWORDS


def test_never_on_an_unhappy_guests_reply():
    for args in ((2, "normal", "negative"), (1, None, None), (5, "high", "positive"), (4, None, "negative")):
        assert rk.pick(1, KW, args[0], "x", urgency=args[1], sentiment=args[2]) is None
    assert rk.check("Our smash burger is the best", KW, 1) .startswith("worked a search phrase into a reply to an unhappy")


def test_the_phrase_the_review_names_leads_then_the_least_used(db_path):
    rid = create_restaurant(Restaurant(name="EJ", owner_email="e@x.test"), db_path=db_path)
    assert rk.pick(rid, KW, 5, "The brunch was amazing", db_path=db_path) == "brunch in St. Charles"
    conn = get_conn(db_path)
    for i in range(3):
        conn.execute("INSERT INTO reviews (restaurant_id, platform, external_id, author, rating, text, draft_response, fetched_at) "
                     "VALUES (?,?,?,?,?,?,?,datetime('now'))", (rid, "google", f"g{i}", "A", 5, "great", "Come try our smash burger!"))
    conn.commit()
    conn.close()
    assert rk.pick(rid, KW, 5, "Great service", db_path=db_path) == "brunch in St. Charles"   # least used of the rest


def test_more_than_one_is_refused():
    assert rk.check("Our smash burger on the patio", KW, 5).startswith("worked in more than one")
    assert rk.check("Our smash burger is waiting", KW, 5) == ""
    assert "SEARCH PHRASE" in rk.prompt_note("patio") and rk.prompt_note(None) == ""


def test_the_api_adds_and_removes_one_at_a_time_and_counts_posted_uses(db_path):
    rid = create_restaurant(Restaurant(name="EJ", owner_email="e@x.test"), db_path=db_path)
    out = rk.api(rid, "POST", {"add": "patio"}, db_path=db_path)
    out = rk.api(rid, "POST", {"add": "smash burger"}, db_path=db_path)
    assert [k["phrase"] for k in out["keywords"]] == ["patio", "smash burger"]
    conn = get_conn(db_path)
    conn.execute("INSERT INTO reviews (restaurant_id, platform, external_id, author, rating, text, draft_response, "
                 "response_status, posted_at, fetched_at) VALUES (?,?,?,?,?,?,?,?,datetime('now'),datetime('now'))",
                 (rid, "google", "p1", "A", 5, "x", "See you on the patio!", "posted"))
    conn.commit()
    conn.close()
    out = rk.api(rid, "POST", {"remove": "SMASH BURGER"}, db_path=db_path)
    assert out["keywords"] == [{"phrase": "patio", "used": 1}]
    assert json.loads(get_restaurant(rid, db_path=db_path).reply_keywords) == ["patio"]


def test_both_routes_exist_and_the_drafter_uses_it():
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    assert '@client_bp.route("/api/reviews/keywords"' in (root / "client_api.py").read_text()
    assert '@mobile_bp.route("/reviews/keywords"' in (root / "mobile_api.py").read_text()
    d = (root / "drafter.py").read_text()
    assert "{keyword_note}" in d and "_rk.check(draft, _kw_all, rating" in d


def test_the_reviews_header_links_down_to_the_search_phrases():
    from pathlib import Path
    src = (Path(__file__).resolve().parents[1] / "templates" / "dashboard.html").read_text()
    assert 'onclick="rvKwJump()">Search phrases</button>' in src and "window.rvKwJump=function()" in src
