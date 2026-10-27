"""A ranking said as an aim, an opinion or someone's words is the
restaurant's voice; one said as a fact, or one someone could check, is held
(owner, 10/27/26: "Simple EJ's aims for the best wings in St. Charles" was
held - the search phrase the drafter had been handed). A reason given for a
feeling ("this is why we think ...") is not an excuse for a failure."""
import pytest

import drafter
import reply_keywords


def _reason(text):
    reason, _ = drafter.check_reply(text, None, restaurant_id=0, review_id=0, review_text="Great wings",
                                    author="Sam", action="test")
    return reason or ""


@pytest.mark.parametrize("text", [
    "Sam, Simple EJ's aims for the best wings in St. Charles, so that means a lot. Come back soon!",
    "Honestly this is why we think Simple EJ's is the best sports bar in St. Charles. Come back soon!",
    "That vibe is exactly what we're going for, some say it makes us the best sports restaurant in St. Charles.",
    "Thanks for calling our wings the best in town, Sam!",
    "We're always working toward the best burger in Chicago - glad it showed.",
    "That's why we love what we do. See you soon!",
])
def test_an_aim_an_opinion_or_their_words_passes(text):
    assert _reason(text) == ""


@pytest.mark.parametrize("text, held", [
    ("Thanks Sam! We have the best wings in St. Charles. Come back soon!", "best wings in St. Charles"),
    ("We love St. Charles. We have the best wings in St. Charles.", "best wings in St. Charles"),
    ("We aim to please. We have the best wings in St. Charles.", "best wings in St. Charles"),   # the aim is another sentence
    ("We think you'll love the wings that were voted best in town.", "voted"),
    ("Some say we're the #1 sports bar in town.", "#1"),
    ("Come back for our famous wings!", "famous"),
])
def test_a_fact_or_a_checkable_ranking_is_held(text, held):
    reason = _reason(text)
    assert "award or ranking claim" in reason and held in reason, reason


def test_an_excuse_is_still_held():
    assert "short-staffed" in _reason("Sorry for the wait - we were short-staffed, which is why it took so long.")
    assert "that's why" in _reason("The kitchen was slammed, that's why your food was late.").lower()


def test_the_flag_quotes_the_whole_place_name():
    assert "('best wings in St. Charles')" in _reason("We have the best wings in St. Charles.")


def test_a_ranking_search_phrase_is_asked_for_as_an_aim():
    note = reply_keywords.prompt_note("best wings in St. Charles")
    assert "never as a fact" in note and "we aim for the best wings in St. Charles" in note
    assert "never as a fact" not in reply_keywords.prompt_note("wings in St. Charles")
