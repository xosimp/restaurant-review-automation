"""Memberships worked into review replies (reply_programs; Danny at Simple
EJ's, 10/9/26): one program per reply, the one the review names most, only
on a happy guest's reply and never on Yelp, by name and perk with no price,
the link only on Google and only its own; the perk the owner wrote is theirs
to repeat in public, an invented comp still is not."""
import json

import pytest

import reply_programs as rp
from models import Restaurant, create_restaurant, get_conn, get_restaurant

CREW = {"name": "Charlie's Crew", "perk": "kids under 12 eat free Monday to Friday, 4 to 6pm",
        "triggers": "kids, family, son, daughter, high chair", "link": "https://simpleejs.com/loyalty/",
        "followup": "Charlie's Crew costs about what one kids meal costs. Sign up: https://simpleejs.com/loyalty/"}
CLUB = {"name": "Founders Club", "perk": "your second drink is free, every visit",
        "triggers": ["beer", "cocktail", "bartender", "the bar"], "adults_only": True}


def _progs():
    return [rp.clean_program(CREW), rp.clean_program(CLUB)]


def test_a_program_is_tidied_and_a_price_in_the_perk_is_refused():
    p = rp.clean_program(CREW)
    assert p["triggers"] == ["kids", "family", "son", "daughter", "high chair"]
    assert p["link"] == "https://simpleejs.com/loyalty/" and rp.display_link(p["link"]) == "simpleejs.com/loyalty"
    assert rp.clean_program(dict(CLUB, link="simpleejs.com/club"))["link"] == "https://simpleejs.com/club"
    for bad in ({"name": "", "perk": "x" * 10, "triggers": "a b"}, dict(CREW, perk="kids eat free for $24.99"),
                dict(CREW, triggers=""), dict(CREW, link="not a link")):
        with pytest.raises(ValueError):
            rp.clean_program(bad)


def test_only_a_happy_guests_reply_off_yelp_gets_one():
    progs = _progs()
    text = "Brought the kids, great family night"
    assert rp.pick(progs, 5, text)["name"] == "Charlie's Crew"
    for kw in ({"rating": 3}, {"rating": 2}, {"rating": 5, "urgency": "high"}, {"rating": 4, "sentiment": "negative"},
               {"rating": 5, "platform": "yelp"}, {"rating": 5, "rating_only": True}):
        r = kw.pop("rating")
        assert rp.pick(progs, r, text, **kw) is None
    assert rp.pick(progs, 5, "Lovely evening") is None                      # nothing it is for


def test_the_program_the_review_names_most_leads():
    progs = _progs()
    assert rp.pick(progs, 5, "The kids loved it and the bartender made a great cocktail, best beer")["name"] \
        == "Founders Club"


def test_a_draft_that_breaks_the_rules_is_written_again():
    progs = _progs()
    crew, club = progs
    ok = "Thanks Sam! If the kids are regulars now, ask about Charlie's Crew: simpleejs.com/loyalty"
    assert rp.check(ok, progs, crew, 5) == ""
    assert rp.check("Charlie's Crew and Founders Club both await", progs, crew, 5).startswith("named more than one")
    assert "doesn't belong" in rp.check("Ask about Founders Club!", progs, crew, 5)
    assert "doesn't belong" in rp.check(ok, progs, crew, 3)
    assert "doesn't belong" in rp.check(ok, progs, None, 5)
    assert rp.check("Charlie's Crew is only $24.99", progs, crew, 5).startswith("put a price")
    assert rp.check("Join Charlie's Crew at otherplace.com/deal", progs, crew, 5).startswith("added a link")
    assert rp.check(ok, progs, crew, 5, platform="facebook").startswith("added a link")   # the link is Google's only
    assert rp.check("No program here", progs, crew, 5) == ""


def test_the_prompt_names_the_program_and_its_perk_and_never_a_price():
    note = rp.prompt_note(rp.clean_program(CREW), "google")
    assert "Charlie's Crew" in note and "eat free" in note and "simpleejs.com/loyalty" in note
    assert "simpleejs.com" not in rp.prompt_note(rp.clean_program(CREW), "facebook")
    assert "21 and over" in rp.prompt_note(rp.clean_program(CLUB))
    assert rp.prompt_note(None) == ""


def test_the_owners_perk_passes_the_public_reply_check_and_an_invented_comp_does_not():
    import drafter
    with_programs = Restaurant(name="Simple EJ's", owner_email="e@x.test",
                               reply_programs=json.dumps([CREW, CLUB]))
    without = Restaurant(name="Simple EJ's", owner_email="e@x.test")
    reply = ("Thanks Sam! Ask the bar about Founders Club next time: your second drink is free, every visit. "
             "-Simple EJ's team")
    reason, _ = drafter.check_reply(reply, restaurant=with_programs, review_text="Great cocktails at the bar")
    assert reason is None
    reason, _ = drafter.check_reply(reply, restaurant=without, review_text="Great cocktails at the bar")
    assert reason                                                           # nobody wrote that offer down
    invented = "Thanks Sam! Next time dessert is free on us. -Simple EJ's team"
    reason, _ = drafter.check_reply(invented, restaurant=with_programs, review_text="Great cocktails")
    assert reason


def test_the_api_adds_edits_and_removes_one_at_a_time_and_counts_posted_uses(db_path):
    rid = create_restaurant(Restaurant(name="EJ", owner_email="e@x.test"), db_path=db_path)
    out, status = rp.api(rid, "POST", {"add": CREW}, db_path=db_path)
    assert status == 200
    out, status = rp.api(rid, "POST", {"add": dict(CREW, perk="something else entirely")}, db_path=db_path)
    assert status == 400                                                    # the name is taken
    out, status = rp.api(rid, "POST", {"add": CLUB}, db_path=db_path)
    out, status = rp.api(rid, "POST", {"update": dict(CLUB, perk="a free second drink every visit"),
                                       "name": "founders club"}, db_path=db_path)
    assert [p["perk"] for p in out["programs"]][1] == "a free second drink every visit"
    out, status = rp.api(rid, "POST", {"add": dict(CREW, perk="nope nope nope", name="Xtra")}, db_path=db_path)
    out, status = rp.api(rid, "POST", {"add": dict(CREW, name="Yard")}, db_path=db_path)
    out, status = rp.api(rid, "POST", {"add": dict(CREW, name="Zest")}, db_path=db_path)
    assert status == 400 and "Up to" in out["error"]                        # four at most
    conn = get_conn(db_path)
    conn.execute("INSERT INTO reviews (restaurant_id, platform, external_id, author, rating, text, draft_response, "
                 "response_status, posted_at, fetched_at) VALUES (?,?,?,?,?,?,?,?,datetime('now'),datetime('now'))",
                 (rid, "google", "p1", "A", 5, "kids", "Ask about Charlie's Crew!", "posted"))
    conn.commit()
    conn.close()
    out, _ = rp.api(rid, "POST", {"remove": "Xtra"}, db_path=db_path)
    used = {p["name"]: p["used"] for p in out["programs"]}
    assert used["Charlie's Crew"] == 1 and "Xtra" not in used
    stored = json.loads(get_restaurant(rid, db_path=db_path).reply_programs)
    assert [p["name"] for p in stored] == ["Charlie's Crew", "Founders Club", "Yard"]


def test_the_card_reads_the_program_and_its_private_follow_up():
    r = Restaurant(name="EJ", owner_email="e@x.test", reply_programs=json.dumps([CREW, CLUB]))
    hit = r.reply_program_in("Ask about Charlie's Crew next time!")
    assert hit["name"] == "Charlie's Crew" and hit["followup"].startswith("Charlie's Crew costs")
    assert r.reply_program_in("Thanks for coming in") == {}


def test_both_routes_exist_and_the_drafter_uses_it():
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    assert '@client_bp.route("/api/reviews/programs"' in (root / "client_api.py").read_text()
    assert '@mobile_bp.route("/reviews/programs"' in (root / "mobile_api.py").read_text()
    d = (root / "drafter.py").read_text()
    assert "_rp.prompt_note(_prog, platform)" in d and "_rp.check(draft, _rp_all, _prog, rating" in d
    assert "programs_said" in d                                              # every path's offer source
