"""A star rating with no words (owner, 10/9/26: Simple EJ's had 35 - the
model filed most five-stars as "neutral", wrote "No review text provided to
analyse" as their summary five ways, and a 1-star draft opened "This one's
tough to respond to since the review didn't actually include any specific
details"). The stars decide it, with no model call; the card says so; the
draft answers the rating and never remarks on the missing words."""
from pathlib import Path

import pytest

import analyser
import drafter

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("rating,want", [(5, "positive"), (4, "positive"), (3, "neutral"), (2, "negative"), (1, "negative")])
def test_the_stars_decide_a_rating_only_review_with_no_model_call(monkeypatch, rating, want):
    saved = {}
    monkeypatch.setattr(analyser, "update_analysis", lambda rid, s, c, summ, u, **k: saved.update(s=s, c=c, summ=summ, u=u, **k))
    monkeypatch.setattr(analyser, "create_with_retry", lambda *a, **k: pytest.fail("no model call for a rating only"))
    out = analyser.analyse_review(1, rating, "  ", restaurant_id=None)
    assert out["sentiment"] == want and saved["s"] == want
    assert saved["c"] == [] and saved["summ"] is None and saved["u"] == "normal" and saved["severity"] == "minor"


def test_a_short_word_still_counts_as_a_review():
    assert analyser.is_rating_only("") and analyser.is_rating_only(" .") and analyser.is_rating_only(None)
    assert not analyser.is_rating_only("Fun")


def test_the_card_says_rating_only_instead_of_a_summary():
    card = (ROOT / "templates" / "_review_card.html").read_text(encoding="utf-8")
    assert "Rating only — the guest didn’t write a review." in card
    i = card.index("Rating only")
    assert card.rfind("(r.text or '')|trim|length < 3", 0, i) > 0


def test_the_draft_prompt_answers_the_rating_and_never_the_missing_words():
    src = (ROOT / "drafter.py").read_text(encoding="utf-8")
    assert "rating_only = analyser.is_rating_only(text)" in src
    assert "Never mention, hint at or apologise for the review having no words" in src
    assert '"(a star rating only — the guest wrote nothing)" if rating_only else wrap_untrusted(text)' in src
