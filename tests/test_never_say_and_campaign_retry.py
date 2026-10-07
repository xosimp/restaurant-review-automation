"""Simple EJ's never-say list was "-" (10/6/26): read as a raw substring it
refused every draft holding a hyphenated word or "8-10pm", and the Campaign
Studio showed "the copy contains a never-say term ('-')". A dash entry now
means the punctuation dash, words match whole, a punctuation dash is
rewritten rather than refused, the model is told what the entry means, and
a refused guest text is drafted once more, told why."""
import types

import ai_guard as g
import guest_marketing as gm
import response_validation as rv


def test_a_dash_entry_means_the_punctuation_dash_not_a_hyphen():
    assert g.never_say_hit("Grab a smash-burger from 8-10pm", "-") is None
    assert g.never_say_hit("Come in — we saved you a seat", "-") == "-"
    assert g.never_say_hit("Come in - we saved you a seat", "-") == "-"
    assert g.never_say_hit("Come in -- we saved you a seat", "em dash") == "em dash"


def test_words_match_whole_words_and_other_terms_as_written():
    assert g.never_say_hit("Start your night here", "art") is None
    assert g.never_say_hit("Fine art on the walls", "art") == "art"
    assert g.never_say_hit("Our Chef’s special", "chef's") == "chef's"
    assert g.never_say_hit("Only $$$ tonight", "$$$") == "$$$"
    assert g.check_marketing_copy("Grab a smash-burger from 8-10pm", "-") is None
    assert "never-say" in g.check_public_reply("Thanks — see you soon", "-")


def test_a_punctuation_dash_is_rewritten_and_the_model_is_told():
    out, n = g.strip_never_say_dashes("Every game — every night. Open 8–10pm.", "-")
    assert n == 2 and out == "Every game, every night. Open 8 to 10pm."
    assert g.strip_never_say_dashes("Every game — every night", "cheap") == ("Every game — every night", 0)
    assert "dashes as punctuation" in g.never_say_prompt("-, cheap") and "cheap" in g.never_say_prompt("-, cheap")
    res = rv.enforce("Missed you — come by for the game tonight.", rv.ValidationContext(
        restaurant_id=1, surface="guest_sms", never_say="-", policy={"action": "test"}), marker=False)
    assert res.verdict is None or res.verdict.verdict != "refuse"
    assert "—" not in str(res)


def test_a_refused_guest_text_is_drafted_once_more_told_why(monkeypatch):
    calls = []

    def once(client, prompt, *a):
        calls.append(prompt)
        if len(calls) == 1:
            raise ValueError("campaign copy rejected: it offers free pumpkins, which nobody told Cavnar AI")
        return "Simple EJ's: carving night is back."
    monkeypatch.setattr(gm, "_draft_campaign_text_once", once)
    monkeypatch.setattr(gm, "get_client", lambda *a, **k: None)
    monkeypatch.setattr(gm, "record_quality_event", None, raising=False)
    r = types.SimpleNamespace(id=1, name="Simple EJ's", sign_off_name=None)
    import marketing
    monkeypatch.setattr(marketing, "get_profile_for_restaurant",
                        lambda *a, **k: dict(marketing.DEFAULT_PROFILE, name="Simple EJ's", never_say="-"))
    assert gm.draft_campaign_message(r, topic="pumpkin carving") == "Simple EJ's: carving night is back."
    assert len(calls) == 2 and "Your previous draft was not used: it offers free pumpkins" in calls[1]


def test_a_second_refusal_is_the_owners_to_see(monkeypatch):
    def once(*a):
        raise ValueError("campaign copy rejected: it offers free pumpkins")
    monkeypatch.setattr(gm, "_draft_campaign_text_once", once)
    monkeypatch.setattr(gm, "get_client", lambda *a, **k: None)
    import marketing
    monkeypatch.setattr(marketing, "get_profile_for_restaurant",
                        lambda *a, **k: dict(marketing.DEFAULT_PROFILE, name="Simple EJ's", never_say="-"))
    r = types.SimpleNamespace(id=1, name="Simple EJ's", sign_off_name=None)
    import pytest
    with pytest.raises(ValueError, match="free pumpkins"):
        gm.draft_campaign_message(r, topic="pumpkin carving")
