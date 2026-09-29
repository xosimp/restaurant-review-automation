"""The lead's integration fixes in the 9/29/26 fix round — the small gaps
the workstream reports handed back that no single workstream owned."""
import admin_events


def test_the_audit_trail_keeps_only_a_phones_last_four_digits():
    body = {"phone": "(314) 555-0199", "owner_phone": 3145550177,
            "contacts": [{"name": "Dana", "phone": "+1 314 555 0188"}],
            "password": "hunter22", "note": "call me"}
    out = admin_events._redact(body)
    assert out["phone"] == "…0199" and out["owner_phone"] == "…0177"
    assert out["contacts"][0]["phone"] == "…0188" and out["contacts"][0]["name"] == "Dana"
    assert out["password"] == "[redacted]" and out["note"] == "call me"
    assert admin_events._redact({"phone_last4": "0123"})["phone_last4"] == "0123"   # already a last four
    assert admin_events._redact({"phone": ""})["phone"] == ""
