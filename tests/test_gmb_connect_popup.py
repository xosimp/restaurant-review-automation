"""The Google Business connect popup always ends the dashboard's
"Connecting · Google Business" card (owner, 9/28/26: it stayed on screen
after the Google window was gone).

The card only cleared on a postMessage from the callback page. A window the
owner closed, a flow Google stopped on its own page, or a refusal from
/auth/google/connect itself (a bare JSON 403) never sent one."""
import inspect
import os
import re

import auth_routes

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _gmb_connect_js():
    src = open(os.path.join(ROOT, "templates", "dashboard.html"), encoding="utf-8").read()
    start = src.index("function gmbConnect(){")
    return src[start:src.index("\nfunction ", start + 10)]


def test_the_card_follows_the_window_not_only_the_message():
    js = _gmb_connect_js()
    assert "popup.closed" in js, "a closed Google window must end the Connecting card"
    assert "cmFloatHide()" in js
    assert re.search(r"if\(!popup\)\{[^}]*toast\(", js), "a blocked popup says so instead of showing the card"
    assert "e.origin !== window.location.origin" in js, "only our own callback page may settle it"


def test_a_refused_connect_is_a_page_that_reports_back_not_bare_json(monkeypatch):
    from flask import Flask
    monkeypatch.setenv("GOOGLE_CLIENT_ID", "cid")
    manager = {"id": 9, "restaurant_id": 1, "role": "manager", "is_admin": 0}
    monkeypatch.setattr("permissions.is_principal", lambda u: False)
    app = Flask(__name__)
    with app.test_request_context("/auth/google/connect"):
        body, status = inspect.unwrap(auth_routes.gmb_connect)(current_user=manager)
    assert status == 403
    assert "postMessage({gmb:'error'" in body
    assert "Only the account owner can connect Google Business." in body


def test_the_refusal_message_cannot_break_out_of_its_script():
    body = auth_routes._gmb_popup_error("</script><img src=x onerror=alert(1)>")
    assert body.count("</script>") == 1, "the message must not close the script early"
    assert "<img" not in body.split("</script>", 1)[1], "the visible copy is escaped"
