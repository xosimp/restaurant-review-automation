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


def test_a_thread_started_from_a_request_keeps_its_ai_attribution(monkeypatch):
    import ai_utils
    import threading
    monkeypatch.setattr(ai_utils, "_attribution", lambda: ("owner", 42, "ask:abc"))
    seen = {}

    def work():
        seen.update(ai_utils._CTX.get() or {})
    t = threading.Thread(target=ai_utils.attributed(work))
    t.start(); t.join()
    assert seen.get("trigger") == "owner" and seen.get("actor_user_id") == 42 and seen.get("correlation_id") == "ask:abc"


def test_schedule_generation_and_the_ask_stream_start_attributed_threads():
    import inspect
    import client_api
    import mobile_api
    assert "_ai_attributed(_run_sched)" in inspect.getsource(mobile_api)
    assert "_ai_attributed(work)" in inspect.getsource(client_api)


def test_the_consoles_busy_refusal_is_not_a_server_error():
    import http_layer
    from flask import Flask, jsonify
    http_layer.reset_metrics()
    app = Flask(__name__)
    http_layer.register(app)

    @app.route("/admin/api/overview")
    def busy():
        r = jsonify(ok=False, busy=True, retry_after=3)
        r.status_code = 503
        r.headers["X-Admin-Busy"] = "1"
        return r

    @app.route("/api/real_outage")
    def outage():
        return jsonify(ok=False), 503
    c = app.test_client()
    assert c.get("/admin/api/overview").status_code == 503      # the client still sees the 503
    statuses = [s[2] for s in list(http_layer._samples)]
    assert statuses == [429] and not list(http_layer._pending_5xx)
    c.get("/api/real_outage")
    assert [s[2] for s in list(http_layer._samples)][-1] == 503 and len(list(http_layer._pending_5xx)) == 1
    http_layer.reset_metrics()


def test_a_stale_count_is_never_its_own_repeat_offender(db_path, monkeypatch):
    """The food read's "last week" is a week before the week it reads, not six
    days before today: a count more than six days old was compared with the
    snapshot its own first render wrote, and the same items came back as
    "REPEAT waste offenders (2+ weeks)" (and the calendar changed the prompt)."""
    import json
    import inventory
    import models
    from tests.test_rv_adoption_insights import _rid, _stub_food, _food_analysis
    rid = _rid(db_path)
    seen = {}
    _stub_food(monkeypatch, "Waste ran $160 this week.\n1. Trim the Salmon par — $96 a week, low effort", seen)
    inventory.get_claude_insights(_food_analysis(), restaurant_id=rid, is_live=True)
    inventory.get_claude_insights(_food_analysis(), restaurant_id=rid, is_live=True)
    assert all("REPEAT waste offenders" not in p for p in seen["prompts"])
    # A genuinely earlier week with the same top items does make it a repeat.
    top = [x["item"] for x in _food_analysis()["waste_items"][:4]]
    c = models.get_conn()          # the connection the food read itself uses
    c.execute("INSERT INTO inventory_history (restaurant_id, waste_json, week_end, source) VALUES (?,?,?,?)",
              (rid, json.dumps({"total_waste_cost": 90.0, "top_items": top}), "2026-09-13", "test"))
    c.commit(); c.close()
    seen.clear()
    _stub_food(monkeypatch, "Waste ran $160 this week.\n1. Trim the Salmon par — $96 a week, low effort", seen)
    inventory.get_claude_insights(_food_analysis(), restaurant_id=rid, is_live=True)
    assert any("REPEAT waste offenders" in p for p in seen.get("prompts", []))


from tests.test_fix_ui_ui3 import world  # noqa: E402,F401  (the UI-3 console world, reused)


def test_a_failed_messaging_read_on_the_client_page_is_a_query_error(world, monkeypatch):
    """The client page's suppressions / texts / issue-text reads ran after its
    error bucket closed, so a failed read rendered as an empty, healthy list
    and never reached query_errors."""
    import admin_ops
    import models

    def boom(*a, **k):
        raise RuntimeError("database is locked")
    monkeypatch.setattr(models, "suppressions_for_restaurant", boom)
    admin_ops.invalidate_fleet_cache()
    out = admin_ops.client_detail(world["rid"])
    assert out["ok"] and out["suppressions"] == []
    assert any(e["query"] == "suppressions_for_restaurant" for e in out["query_errors"])


def test_every_link_to_a_legacy_client_page_is_hidden_from_a_support_login():
    """/admin/client-settings/<id> and /admin/client-data/<id> refuse support
    logins (403 "Use the admin console"), so every link to them is .w —
    hidden under body.role-support — wherever the console draws one."""
    import os
    import re
    src = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "templates", "admin.html")).read()
    not_links = ("path.startsWith('/admin/client-settings/')", "d.save_url || ('/admin/client-settings/'")
    seen = 0
    for m in re.finditer(r"/admin/client-(?:settings|data)/", src):
        seen += 1
        before = src[max(0, m.start() - 200):m.start()]
        if before.endswith('href="'):
            tag = src[src.rfind("<", 0, m.start()):m.start()]
            assert re.search(r'class="[^"]*\bw\b', tag), f"line {src.count(chr(10), 0, m.start()) + 1}: {tag}"
        elif re.search(r"aw\('[^']*', '$", before):
            pass
        else:
            line = src[src.rfind("\n", 0, m.start()) + 1:src.find("\n", m.start())]
            assert any(s in line for s in not_links), f"unclassified legacy-page reference: {line.strip()[:160]}"
    assert seen >= 12


def test_the_client_list_route_passes_every_filter_the_list_takes(world):
    """/admin/api/clients/list dropped churn, has_issues, joined_days and
    inactive_days on the floor, though admin_ops.clients_page takes them."""
    c, rid = world["c"], world["rid"]

    def ids(qs):
        r = c.get("/admin/api/clients/list?segment=all&per_page=200" + qs)
        assert r.status_code == 200, (qs, r.status_code)
        return [x["id"] for x in r.get_json()["items"]]
    assert rid in ids("")
    assert rid not in ids("&churn=high")          # a new account carries no high churn risk
    assert rid in ids("&joined_days=1")           # created today
    assert rid in ids("&joined_days=abc")         # not a whole number: ignored, never a 500


def test_an_expired_session_answers_a_json_ask_with_401_wherever_the_read_lives(world):
    """The console reads /admin/status/services and /incidents, outside
    /admin/api/: an expired session there was a 302 to the login page, which
    fetch() followed and couldn't parse. A JSON ask is a 401 on any path now;
    a browser page GET still gets the redirect."""
    from flask import Flask
    import status_routes
    from auth_routes import auth_bp
    app = Flask(__name__)
    for bp in (status_routes.status_bp, auth_bp):
        app.register_blueprint(bp)
    c = app.test_client()
    c.set_cookie("session_token", "expired-or-forged")
    for path in ("/admin/status/services", "/admin/status/incidents"):
        r = c.get(path, headers={"Accept": "application/json"})
        assert r.status_code == 401 and r.get_json()["session_expired"] is True, path
        page = c.get(path, headers={"Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8"})
        assert page.status_code == 302 and "/login" in page.headers["Location"], path


def test_the_console_asks_for_json_on_every_call_and_a_login_redirect_signs_in():
    import os
    import re
    src = open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                            "templates", "admin.html")).read()
    body = re.search(r"async function api\(path, opts, _retry, _s0\)\{(.*?)\n\}", src, re.S).group(1)
    assert "{'Accept': 'application/json'}" in body and "fetch(path, o)" in body
    assert "r.redirected && r.url.indexOf(location.origin + '/login') === 0" in body


def test_the_suppressed_welcome_refusal_names_the_tab_the_console_has():
    import os
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    routes = open(os.path.join(root, "admin_routes.py")).read()
    page = open(os.path.join(root, "templates", "admin.html")).read()
    assert "Messaging → Suppressions" not in routes
    assert "under Operations → Email & SMS" in routes and "['email', 'Email &amp; SMS'" in page
