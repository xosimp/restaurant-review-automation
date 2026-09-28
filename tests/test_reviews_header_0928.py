"""Reviews header, 9/28/26 (owner): the rating pill keeps Google's figure,
the data line reads as one line, the Today line is gone, and "so we can make
this right" is an invitation, not a promise."""
import os
import re

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC = open(os.path.join(ROOT, "templates", "dashboard.html"), encoding="utf-8").read()


def test_the_rating_refresh_keeps_googles_figure():
    block = SRC[SRC.index("// ── Avg rating ──"):SRC.index("// ── Responded this month")]
    assert "d.official_rating != null) ? d.official_rating : d.avg_rating" in block
    assert "ratingNEl.textContent = d.avg_rating" not in block


def test_the_today_line_is_gone_from_reviews():
    assert 'id="rv-today"' not in SRC


def test_a_data_line_is_one_inline_run_inside_its_pill():
    assert "<b>Data</b> <span class=\"bs\">'+num(worst.line||'')+'</span></span>" in SRC
    assert re.search(r"\.hb-fresh-it \.bs\{display:inline\}", SRC)


def _promise_rx():
    import response_validation as rv
    return next(rx for label, rx, _ in rv._P1 if label == "promise the restaurant never made")


@pytest.mark.parametrize("fine", [
    "Please reach out to us directly so we can make this right.",
    "Email us and we'll make it right.",
])
def test_an_invitation_to_make_it_right_is_not_a_promise(fine):
    import ai_guard
    assert not [label for label, rx in ai_guard._REPLY_CLAIMS if rx.search(fine)]
    assert not _promise_rx().search(fine)


@pytest.mark.parametrize("promise", [
    "We will make it right on your next visit.",
    "We'd love to make it up to you.",
])
def test_a_promise_tied_to_a_next_visit_or_a_make_up_is_still_flagged(promise):
    import ai_guard
    assert [label for label, rx in ai_guard._REPLY_CLAIMS if rx.search(promise)]
    assert _promise_rx().search(promise)


def test_a_flag_todays_rules_no_longer_raise_is_cleared(db_path):
    import drafter
    import models
    rid = models.create_restaurant(models.Restaurant(name="Simple EJ's", owner_email="e@x.test"), db_path=db_path)
    old = "makes a claim a public reply must not: promise the restaurant never made ('make this right')"
    c = models.get_conn(db_path)
    for ext, draft in (("a", "Joan, please reach out to us directly so we can make this right."),
                       ("b", "Joan, we will make it right on your next visit.")):
        c.execute("INSERT INTO reviews (restaurant_id, platform, external_id, author, rating, text, processed, "
                  "response_status, draft_response, draft_needs_review, draft_review_reason, fetched_at) "
                  "VALUES (?,?,?,?,?,?,1,'drafted',?,1,?,datetime('now'))",
                  (rid, "google", ext, "Joan Robin C", 2, "Service was terrible.", draft, old))
    c.commit()
    c.close()
    assert drafter.recheck_draft_flags(db_path=db_path) == 1
    c = models.get_conn(db_path)
    rows = {r["external_id"]: (r["draft_needs_review"], r["draft_review_reason"]) for r in c.execute(
        "SELECT external_id, draft_needs_review, draft_review_reason FROM reviews WHERE restaurant_id=?", (rid,))}
    c.close()
    assert rows["a"] == (0, None)
    assert rows["b"][0] == 1 and rows["b"][1]                   # still a promise: still flagged
