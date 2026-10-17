"""10/16/26 (Simple EJ's): a manager changed their own password and the
"your password was changed" notice went to the owner, who thought someone
was in his account. The notice goes to the login whose password changed."""
import os

import emails
from auth import create_user
from models import Restaurant, create_restaurant

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def test_the_notice_goes_to_the_login_not_the_owner(monkeypatch):
    rid = create_restaurant(Restaurant(name="Pw Spot", owner_email="owner@x.test"))
    uid = create_user(rid, "mgr_pw_1016", "mgr@x.test", "x", generated=True)
    sent = []
    monkeypatch.setattr(emails, "send_password_changed_email",
                        lambda to, name, *a, **k: sent.append((to, name, k.get("login"))) or True)
    emails.send_password_changed_to_user(uid)
    assert sent == [("mgr@x.test", "Pw Spot", "mgr_pw_1016")]


def test_a_login_with_no_email_is_told_nothing(monkeypatch):
    rid = create_restaurant(Restaurant(name="Pw Spot 2", owner_email="owner2@x.test"))
    uid = create_user(rid, "nomail_pw_1016", "", "x", generated=True)
    monkeypatch.setattr(emails, "send_password_changed_email", lambda *a, **k: (_ for _ in ()).throw(AssertionError))
    assert not emails.send_password_changed_to_user(uid).ok


def test_no_route_sends_it_to_the_owner():
    for f in ("auth_routes.py", "mobile_api.py"):
        src = open(os.path.join(ROOT, f), encoding="utf-8").read()
        assert "send_password_changed_email(restaurant.owner_email" not in src, f
        assert "send_password_changed_to_user(" in src, f
