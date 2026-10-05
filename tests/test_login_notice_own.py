"""A sign-in notice goes to the login that signed in, once per burst (10/5/26:
Simple EJ's owner got ten in a week, most of them teammates signing in)."""
import notify


def test_the_notice_goes_to_the_person_who_signed_in(monkeypatch):
    sent, pushed = [], []
    import emails, push, ops
    monkeypatch.setattr(emails, "send_login_notification", lambda to, *a, **k: sent.append(to))
    monkeypatch.setattr(push, "fire_push", lambda *a, **k: pushed.append(k.get("user_ids")))
    monkeypatch.setattr(notify, "_log_alert", lambda *a, **k: None)
    seen = set()
    monkeypatch.setattr(ops, "claim_cooldown", lambda key, minutes: not (key in seen or seen.add(key)))
    notify.send_login_alert(5, "EJ's", "owner@x.test", "1.2.3.4", "Safari", user_id=7, to_email="jim@x.test")
    assert sent == ["jim@x.test"] and pushed == [[7]], "Jim hears about Jim's sign-in, not the owner"
    notify.send_login_alert(5, "EJ's", "owner@x.test", "1.2.3.4", "Safari", user_id=7, to_email="jim@x.test")
    assert len(sent) == 1, "two sign-ins seconds apart are one notice"
    notify.send_login_alert(5, "EJ's", "owner@x.test", "1.2.3.4", "Safari", user_id=9, to_email="")
    assert sent[-1] == "owner@x.test", "a login with no email of its own falls back to the owner"


def test_both_login_paths_name_the_login():
    import inspect, auth_routes, mobile_api
    for src in (inspect.getsource(auth_routes._send_restaurant_login_alert),
                inspect.getsource(mobile_api._send_login_notification)):
        assert 'user_id=user["id"]' in src and 'to_email=user.get("email")' in src
