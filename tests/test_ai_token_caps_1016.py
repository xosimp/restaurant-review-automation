"""10/16/26: two calls ran out of room — the Ask conversation notes (700)
and the review diagnosis (800) — so half their answers were cut off,
refused and still paid for. The caps leave room for a full answer."""
import ask_conversations
import review_intelligence


def test_the_ask_notes_and_the_review_diagnosis_have_room_to_finish():
    assert ask_conversations.SUMMARY_MAX_TOKENS >= 1500
    req = review_intelligence.diagnosis_request({"user": "x"})
    assert req["max_tokens"] == review_intelligence.DIAGNOSIS_MAX_TOKENS >= 1600
