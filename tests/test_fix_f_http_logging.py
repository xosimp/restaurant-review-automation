"""Workstream F — the response layer and the process's logs (#30, #38, #77, #94).

Everything here runs against a throwaway Flask app with http_layer attached
(importing hosted_dashboard would boot the database and the scheduler), and
against logging_setup's formatter and handler directly.
"""
import io
import json
import logging
import threading
import time

import pytest
from flask import Flask, jsonify

import http_layer
import logging_setup


@pytest.fixture(autouse=True)
def _clean_metrics():
    http_layer.reset_metrics()
    logging_setup.clear()
    yield
    http_layer.reset_metrics()
    logging_setup.clear()


def _app():
    app = Flask(__name__)
    http_layer.register(app)

    @app.route("/api/thing")
    def api_thing():
        return jsonify(ok=True)

    @app.route("/admin/api/things")
    def admin_things():
        return jsonify(ok=True)

    @app.route("/admin")
    def admin_page():
        return "<html>admin</html>"

    @app.route("/health")
    def health():
        return jsonify(status="ok")

    @app.route("/audit/r/<token>")
    def shared(token):
        return "<html>report</html>"

    @app.route("/page")
    def page():
        return "<html>page</html>"

    @app.route("/api/handled/<int:rid>")
    def handled(rid):
        return jsonify(ok=False, error="Toast said no: https://x.test/?key=AIzaSySECRETSECRETSECRETSECRET12"), 500

    @app.route("/api/boom")
    def boom():
        raise RuntimeError("kaboom")

    @app.route("/api/inflight")
    def inflight_view():
        return jsonify(http_layer.inflight())

    @app.route("/api/log")
    def log_view():
        logging.getLogger("unit").warning("inside a request")
        return jsonify(ok=True)
    return app


# ── #94: what may be cached ──────────────────────────────────────────────────

def test_admin_json_admin_pages_health_and_share_links_are_no_store():
    c = _app().test_client()
    for path in ("/admin/api/things", "/admin", "/health", "/audit/r/abc", "/api/thing"):
        assert c.get(path).headers.get("Cache-Control") == "no-store", path


def test_ordinary_pages_still_get_no_cache_policy():
    c = _app().test_client()
    assert c.get("/page").headers.get("Cache-Control") is None


# ── #38: request ids ─────────────────────────────────────────────────────────

def test_every_response_carries_a_request_id():
    c = _app().test_client()
    a = c.get("/api/thing").headers.get("X-Request-ID")
    b = c.get("/api/thing").headers.get("X-Request-ID")
    assert a and b and a != b


def test_an_inbound_request_id_is_kept_and_a_malformed_one_replaced():
    c = _app().test_client()
    assert c.get("/api/thing", headers={"X-Request-ID": "edge-abc-12345"}).headers["X-Request-ID"] == "edge-abc-12345"
    assert c.get("/api/thing", headers={"X-Railway-Request-Id": "rw_0123456789"}).headers["X-Request-ID"] == "rw_0123456789"
    bad = c.get("/api/thing", headers={"X-Request-ID": "x\" injected=yes <b>"}).headers["X-Request-ID"]
    assert bad != "x\" injected=yes <b>" and '"' not in bad and " " not in bad
    short = c.get("/api/thing", headers={"X-Request-ID": "abc"}).headers["X-Request-ID"]
    assert short != "abc"


def test_a_log_line_written_inside_a_request_names_the_request():
    records = []

    class Grab(logging.Handler):
        def emit(self, record):
            records.append(record)
    h = Grab()
    h.addFilter(logging_setup.ContextFilter())
    logger = logging.getLogger("unit")
    logger.addHandler(h)
    logger.setLevel(logging.INFO)
    try:
        resp = _app().test_client().get("/api/log")
    finally:
        logger.removeHandler(h)
    assert records, "the view logged nothing"
    rec = records[-1]
    assert rec.request_id == resp.headers["X-Request-ID"]
    assert rec.route == "/api/log" and rec.method == "GET"
    assert logging_setup.current() == {}, "the context must not leak past the request"


# ── #77: traffic classes, in-flight, rollups ────────────────────────────────

def test_traffic_classes():
    tc = http_layer.traffic_class
    assert tc("/admin/api/overview") == "admin"
    assert tc("/mobile/api/home") == "mobile"
    assert tc("/api/reviews/1") == "web_api"
    assert tc("/health") == "probe" and tc("/status") == "probe" and tc("/api/status") == "probe"
    assert tc("/static/x.js") == "static" and tc("/robots.txt") == "static"
    assert tc("/stripe-webhook") == "webhook" and tc("/webhooks/twilio/sms") == "webhook"
    assert tc("/") == "web" and tc("/login") == "web"


def test_admin_traffic_is_kept_out_of_the_customer_latency_figures():
    c = _app().test_client()
    for _ in range(3):
        c.get("/api/thing")
    for _ in range(5):
        c.get("/admin/api/things")
    c.get("/health")
    m = http_layer.request_metrics()
    assert m["requests"] == 3, "the headline covers customer traffic only"
    assert m["by_class"]["admin"]["requests"] == 5
    assert m["by_class"]["probe"]["requests"] == 1
    assert http_layer.request_metrics(classes=None)["requests"] == 9


def test_in_flight_counts_what_is_running_now():
    c = _app().test_client()
    during = c.get("/api/inflight").get_json()
    assert during["now"] == 1 and during["oldest_route"] == "/api/inflight"
    assert http_layer.inflight()["now"] == 0


def test_worker_threads_is_read_from_gunicorns_command_line(monkeypatch):
    import sys
    monkeypatch.setattr(sys, "argv", ["gunicorn", "hosted_dashboard:app", "--workers", "1", "--threads", "4"])
    assert http_layer.worker_threads() == 4
    monkeypatch.setattr(sys, "argv", ["gunicorn", "--threads=8"])
    assert http_layer.worker_threads() == 8
    monkeypatch.setattr(sys, "argv", ["pytest"])
    assert http_layer.worker_threads() is None


def test_completed_minutes_drain_as_rollups_by_route_with_latency_buckets():
    c = _app().test_client()
    for _ in range(4):
        c.get("/api/thing")
    c.get("/api/handled/7")
    rows = http_layer.drain_rollups(now=time.time() + 120)
    by_route = {r["route"]: r for r in rows}
    thing = by_route["/api/thing"]
    assert thing["requests"] == 4 and thing["klass"] == "web_api" and thing["method"] == "GET"
    assert sum(thing["buckets"]) == 4 and len(thing["buckets"]) == len(http_layer.LATENCY_BUCKETS_MS) + 1
    assert by_route["/api/handled/<int:rid>"]["errors_5xx"] == 1, "keyed by the rule, never the raw path"
    star = [r for r in rows if r["route"] == "*"]
    assert star and star[0]["max_inflight"] >= 1 and star[0]["requests"] == 5
    assert http_layer.drain_rollups(now=time.time() + 120) == [], "drained once"


def test_the_current_minute_stays_buffered_until_it_is_complete():
    _app().test_client().get("/api/thing")
    assert http_layer.drain_rollups() == []
    assert http_layer.drain_rollups(include_current=True)


# ── #30: every 5xx leaves a trace ────────────────────────────────────────────

def test_a_handled_500_is_sampled_with_its_redacted_error_and_request_id(monkeypatch):
    sent = []
    monkeypatch.setenv("SENTRY_DSN", "https://public@example.invalid/1")
    import sentry_sdk
    monkeypatch.setattr(sentry_sdk, "capture_message", lambda msg, **kw: sent.append((msg, kw)))
    resp = _app().test_client().get("/api/handled/42")
    assert resp.status_code == 500
    samples = http_layer.drain_server_errors()
    assert len(samples) == 1
    s = samples[0]
    assert s["route"] == "/api/handled/<int:rid>" and s["status"] == 500
    assert s["request_id"] == resp.headers["X-Request-ID"]
    assert s["restaurant_id"] == 42 and s["klass"] == "web_api" and s["unhandled"] is False
    assert "Toast said no" in s["error"] and "SECRETSECRET" not in s["error"]
    assert len(sent) == 1 and sent[0][1]["tags"]["request_id"] == s["request_id"]
    assert sent[0][1]["fingerprint"] == ["http-5xx", "/api/handled/<int:rid>", "500"]


def test_sentry_hears_a_handled_500_at_most_once_a_minute_per_route(monkeypatch):
    sent = []
    monkeypatch.setenv("SENTRY_DSN", "https://public@example.invalid/1")
    import sentry_sdk
    monkeypatch.setattr(sentry_sdk, "capture_message", lambda msg, **kw: sent.append(msg))
    c = _app().test_client()
    for _ in range(5):
        c.get("/api/handled/1")
    assert len(sent) == 1
    assert len(http_layer.drain_server_errors()) == 5, "every one is still recorded"


def test_an_unhandled_exception_is_sampled_as_unhandled_and_not_sent_twice(monkeypatch):
    sent = []
    monkeypatch.setenv("SENTRY_DSN", "https://public@example.invalid/1")
    import sentry_sdk
    monkeypatch.setattr(sentry_sdk, "capture_message", lambda msg, **kw: sent.append(msg))
    app = _app()
    app.config["PROPAGATE_EXCEPTIONS"] = False
    resp = app.test_client().get("/api/boom")
    assert resp.status_code == 500
    s = http_layer.drain_server_errors()[0]
    assert s["unhandled"] is True and s["exc_type"] == "RuntimeError" and "kaboom" in s["error"]
    assert sent == [], "the Flask integration already captures unhandled exceptions"


# ── #38: the log format ──────────────────────────────────────────────────────

def _record(msg="hello", level=logging.INFO, exc_info=None, **extra):
    rec = logging.LogRecord("ops", level, __file__, 1, msg, (), exc_info)
    for k, v in extra.items():
        setattr(rec, k, v)
    return rec


def test_json_lines_carry_level_logger_message_context_and_traceback():
    fmt = logging_setup.JsonFormatter()
    logging_setup.bind(request_id="rid-123", restaurant_id=5)
    try:
        rec = _record("job crashed", logging.WARNING)
        logging_setup.ContextFilter().filter(rec)
        out = json.loads(fmt.format(rec))
    finally:
        logging_setup.clear()
    assert out["level"] == "warn" and out["logger"] == "ops" and out["message"] == "job crashed"
    assert out["request_id"] == "rid-123" and out["restaurant_id"] == 5
    try:
        raise ValueError("bad value")
    except ValueError:
        import sys
        out = json.loads(fmt.format(_record("failed", logging.ERROR, exc_info=sys.exc_info())))
    assert out["level"] == "error" and out["exc_type"] == "ValueError"
    assert "Traceback" in out["traceback"] and "bad value" in out["traceback"]


def test_the_format_is_json_on_railway_and_text_elsewhere():
    assert logging_setup.use_json({"RAILWAY_ENVIRONMENT": "production"}) is True
    assert logging_setup.use_json({}) is False
    assert logging_setup.use_json({"RAILWAY_ENVIRONMENT": "production", "LOG_FORMAT": "text"}) is False
    assert logging_setup.use_json({"LOG_FORMAT": "json"}) is True


def test_configure_installs_one_handler_and_keeps_everyone_elses(monkeypatch):
    root = logging.getLogger()
    before_handlers, before_level = list(root.handlers), root.level
    before_hook = threading.excepthook
    other = logging.NullHandler()
    root.addHandler(other)
    try:
        stream = io.StringIO()
        logging_setup.configure(fmt="json", stream=stream, force=True)
        logging_setup.configure(fmt="json", stream=stream, force=True)
        mine = [h for h in root.handlers if getattr(h, "_cavnar_handler", False)]
        assert len(mine) == 1 and other in root.handlers
        logging.getLogger("unit.cfg").info("configured")
        assert json.loads(stream.getvalue().strip().splitlines()[-1])["message"] == "configured"

        def die():
            raise RuntimeError("thread died")
        t = threading.Thread(target=die, name="doomed")
        t.start()
        t.join()
        lines = [json.loads(l) for l in stream.getvalue().strip().splitlines()]
        dead = [l for l in lines if l["logger"] == "thread"]
        assert dead and "doomed" in dead[-1]["message"] and "thread died" in dead[-1]["traceback"]
    finally:
        root.handlers = before_handlers
        root.setLevel(before_level)
        threading.excepthook = before_hook
        logging_setup._configured = False
