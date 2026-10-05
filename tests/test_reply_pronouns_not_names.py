"""A pronoun is not a person's name (Simple EJ's, 10/5/26): "She's a gem"
matched the possessive pattern ("Amanda's review") as the name "She", and
the public reply was held for "names a person in public"."""
import ai_guard


def test_pronouns_and_contractions_are_not_names():
    for text in ("Kerri taking great care of you is what we love to hear! She's a gem.",
                 "They're the best crew in town and Their energy shows.",
                 "Who's ready for the next game? Here's to Sundays. There's a seat for you.",
                 "Let's do it again. You're always welcome and Your table is ready."):
        assert ai_guard.unsupported_names(text, "Kerri") == [], text


def test_a_real_name_is_still_caught():
    assert ai_guard.unsupported_names("Thanks for coming, Amanda's crew loved having you.", "") == ["Amanda"]
    assert ai_guard.unsupported_names("Server Tina will take care of you next time.", "") == ["Tina"]
