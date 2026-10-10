"""What a test that sends a week past its must-fix blockers now has to say.

Since the publish gate was tiered (schedule audit 10/10/26 TRUST-3, ARCH-30)
a must-fix blocker — a hard rule, a manager gap, a roster with no floor
manager — is acknowledged only by an override of its own with a reason, and a
manager gap only by naming who runs the floor. A bare `acknowledge: true` no
longer covers them. Tests about delivery, versions or identity that publish a
deliberately bare week pass these overrides, exactly as the pre-publish screen
does."""


def overrides_for(restaurant_id, history_id, covered_by="Owner", reason="Test: sent knowingly") -> dict:
    """{key: {reason, covered_by}} for every blocker the gate finds on the
    week now — the `override_reasons` a direct publish/send_changes call
    takes."""
    import schedule_publish as sp
    review = sp.publish_review(restaurant_id, history_id)
    return {b["key"]: {"reason": reason, "covered_by": covered_by} for b in review["blockers"]}


def override_list(restaurant_id, history_id, covered_by="Owner", reason="Test: sent knowingly") -> list:
    """The same as the request body's `overrides: [{key, reason, covered_by}]`."""
    return [{"key": k, **v} for k, v in overrides_for(restaurant_id, history_id, covered_by, reason).items()]
