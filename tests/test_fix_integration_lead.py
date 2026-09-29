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
