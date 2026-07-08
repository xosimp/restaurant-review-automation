"""admin_routes.py — the largest, highest-blast-radius file in the codebase
(91KB, 59 routes) with zero prior test coverage. Covers the pieces most
likely to cause real damage if broken: the admin gate itself, deactivating/
reactivating a client's access, MRR accounting, and the AI usage view added
in this session."""
import pytest
from flask import Flask

import admin_routes
import auth
import auth_routes
import models
from admin_routes import admin_bp, admin, deactivate_client, reactivate_client, ai_usage
from auth_routes import auth_bp
from auth import create_user, init_auth, verify_password
from ai_utils import log_ai_usage
from models import create_restaurant, Restaurant, get_conn


@pytest.fixture(autouse=True)
def _init_auth_tables(db_path):
    init_auth(db_path=db_path)
    # email_log lives in models.py but, like webhooks/job_failures, is its
    # own separate init call at boot — not part of init_db()/ensure_columns().
    from models import init_email_log
    init_email_log(db_path=db_path)


@pytest.fixture(autouse=True)
def _redirect_db(monkeypatch, db_path):
    """models.py, auth.py, and admin_routes.py each did `from models import
    get_conn` (or bound functions closing over their own copy) at module
    level — the same independent-reference gotcha documented in
    test_sms_consent.py. ai_utils.py's log_ai_usage/usage_summary do a lazy
    `from models import get_conn` *inside* the function body, so patching
    models.get_conn here is sufficient for those too — no separate patch
    needed. Autouse so every test in this file gets it, since forgetting it
    on even one test means that test silently writes to the real reviews.db."""
    real_get_conn = models.get_conn
    redirect = lambda *a, **k: real_get_conn(db_path)
    for mod in (models, auth, admin_routes):
        monkeypatch.setattr(mod, "get_conn", redirect)


def _restaurant(db_path, **kw):
    return create_restaurant(Restaurant(name=kw.pop("name", "Admin Test Co"), owner_email="a@x.com", **kw), db_path=db_path)


@pytest.fixture
def app():
    """Registers both blueprints for real so url_for("auth.login") resolves —
    admin_required's redirect branch (hit by any plain GET, since ai-usage
    isn't under /api/) calls it, and a bare Flask() with no auth_bp raises
    a BuildError instead of exercising the actual rejection path."""
    flask_app = Flask(__name__, template_folder="../templates")
    flask_app.register_blueprint(admin_bp)
    flask_app.register_blueprint(auth_bp)
    # format_num is registered on the real app in hosted_dashboard.py
    # (@app.template_filter) — admin.html needs it and importing
    # hosted_dashboard.py itself is unsafe here (real DB init, background
    # threads at module level), so the filter is duplicated rather than
    # imported.
    flask_app.jinja_env.filters["format_num"] = lambda v: f"{float(v):,.0f}" if v not in (None, "") else v
    return flask_app


# ── admin_required enforcement on a real route ──────────────────────────────

def test_admin_route_rejects_non_admin_get_with_redirect(monkeypatch, app):
    """ai-usage is a GET route outside /api/, so _wants_json_response() is
    False here — the real behavior for a non-admin hitting it in a browser
    is a redirect to /login, not a JSON 401 (that branch is for the fetch()
    calls under /api/, covered by the POST-based tests below and in
    test_auth.py's decorator-level tests)."""
    monkeypatch.setattr(auth, "get_current_user", lambda: {"id": 1, "is_admin": 0})
    with app.test_request_context("/admin/ai-usage/1"):
        resp = ai_usage(1)
        assert resp.status_code == 302
        assert "/login" in resp.headers["Location"]


def test_admin_route_rejects_non_admin_post_with_json_401(monkeypatch, app):
    monkeypatch.setattr(auth, "get_current_user", lambda: {"id": 1, "is_admin": 0})
    with app.test_request_context("/admin/deactivate-client/1", method="POST"):
        resp, status = deactivate_client(1)
        assert status == 401
        assert resp.get_json()["session_expired"] is True


def test_admin_route_allows_real_admin(monkeypatch, app, db_path):
    monkeypatch.setattr(auth, "get_current_user", lambda: {"id": 999, "is_admin": 1})
    rid = _restaurant(db_path)
    with app.test_request_context(f"/admin/ai-usage/{rid}"):
        resp = ai_usage(rid)
    assert resp.get_json()["ok"] is True


# ── deactivate / reactivate client ──────────────────────────────────────────

def test_deactivate_client_flips_is_active(monkeypatch, app, db_path):
    monkeypatch.setattr(auth, "get_current_user", lambda: {"id": 999, "is_admin": 1})
    rid = _restaurant(db_path)
    uid = create_user(rid, "alice", "alice@x.com", "pw", db_path=db_path)
    with app.test_request_context(f"/admin/deactivate-client/{uid}", method="POST"):
        deactivate_client(uid)
    conn = get_conn(db_path)
    row = conn.execute("SELECT is_active FROM users WHERE id=?", (uid,)).fetchone()
    conn.close()
    assert row["is_active"] == 0


def test_deactivated_user_cannot_log_in(monkeypatch, app, db_path):
    """The actual point of deactivation — verify_password must reject it."""
    monkeypatch.setattr(auth, "get_current_user", lambda: {"id": 999, "is_admin": 1})
    rid = _restaurant(db_path)
    uid = create_user(rid, "alice", "alice@x.com", "pw", db_path=db_path)
    with app.test_request_context(f"/admin/deactivate-client/{uid}", method="POST"):
        deactivate_client(uid)
    assert verify_password("alice", "pw", db_path=db_path) is None


def test_deactivate_client_cannot_deactivate_an_admin(monkeypatch, app, db_path):
    """The query is scoped `AND is_admin=0` — deactivating a user_id that
    happens to belong to an admin must be a no-op, not lock out the operator."""
    monkeypatch.setattr(auth, "get_current_user", lambda: {"id": 999, "is_admin": 1})
    rid = _restaurant(db_path)
    admin_uid = create_user(rid, "willadmin", "will@x.com", "pw", is_admin=True, db_path=db_path)
    with app.test_request_context(f"/admin/deactivate-client/{admin_uid}", method="POST"):
        deactivate_client(admin_uid)
    conn = get_conn(db_path)
    row = conn.execute("SELECT is_active FROM users WHERE id=?", (admin_uid,)).fetchone()
    conn.close()
    assert row["is_active"] == 1  # unchanged


def test_reactivate_client_flips_is_active_back(monkeypatch, app, db_path):
    monkeypatch.setattr(auth, "get_current_user", lambda: {"id": 999, "is_admin": 1})
    rid = _restaurant(db_path)
    uid = create_user(rid, "alice", "alice@x.com", "pw", db_path=db_path)
    with app.test_request_context(f"/admin/deactivate-client/{uid}", method="POST"):
        deactivate_client(uid)
    with app.test_request_context(f"/admin/reactivate-client/{uid}", method="POST"):
        reactivate_client(uid)
    assert verify_password("alice", "pw", db_path=db_path) is not None


# ── main admin dashboard view ────────────────────────────────────────────────

def test_admin_dashboard_renders_with_real_data(monkeypatch, app, db_path):
    monkeypatch.setattr(auth, "get_current_user", lambda: {"id": 999, "is_admin": 1})
    rid = _restaurant(db_path, name="Rendered Restaurant")
    create_user(rid, "alice", "alice@x.com", "pw", db_path=db_path)
    with app.test_request_context("/admin"):
        resp = admin()
    assert "Rendered Restaurant" in resp


def test_admin_dashboard_mrr_counts_only_active_billing(monkeypatch, app, db_path):
    """MRR = $300 per active module, but only for non-admin users with
    billing_status == 'active' — a trial client must not inflate MRR."""
    monkeypatch.setattr(auth, "get_current_user", lambda: {"id": 999, "is_admin": 1})
    rid_active = _restaurant(db_path, name="Active Co", billing_status="active",
                              module_reviews=1, module_labor=1, module_inventory=0, module_marketing=0)
    rid_trial = _restaurant(db_path, name="Trial Co", billing_status="trial",
                             module_reviews=1, module_labor=1, module_inventory=1, module_marketing=1)
    create_user(rid_active, "activeowner", "active@x.com", "pw", db_path=db_path)
    create_user(rid_trial, "trialowner", "trial@x.com", "pw", db_path=db_path)
    with app.test_request_context("/admin"):
        resp = admin()
    # 2 active modules × $300 = $600 from the active client; the trial
    # client (4 modules) contributes nothing to MRR.
    assert "$600" in resp or ">600<" in resp


def test_admin_dashboard_never_counts_admin_toward_mrr(monkeypatch, app, db_path):
    monkeypatch.setattr(auth, "get_current_user", lambda: {"id": 999, "is_admin": 1})
    rid = _restaurant(db_path, name="Admin Home", billing_status="active",
                       module_reviews=1, module_labor=1, module_inventory=1, module_marketing=1)
    create_user(rid, "willadmin", "will@x.com", "pw", is_admin=True, db_path=db_path)
    with app.test_request_context("/admin"):
        resp = admin()
    assert "$1,200" not in resp and ">1200<" not in resp


# ── AI usage view (added alongside the ai_usage table in this session) ─────

def test_ai_usage_route_aggregates_by_action(monkeypatch, app, db_path):
    monkeypatch.setattr(auth, "get_current_user", lambda: {"id": 999, "is_admin": 1})
    rid = _restaurant(db_path)
    log_ai_usage(rid, "draft_response", "claude-sonnet-5", 1000, 200, db_path=db_path)
    log_ai_usage(rid, "draft_response", "claude-sonnet-5", 500, 100, db_path=db_path)
    with app.test_request_context(f"/admin/ai-usage/{rid}"):
        resp = ai_usage(rid)
    data = resp.get_json()
    assert data["ok"] is True
    assert data["total_calls"] == 2
    assert data["total_cost"] > 0


def test_ai_usage_route_scoped_to_requested_restaurant_only(monkeypatch, app, db_path):
    monkeypatch.setattr(auth, "get_current_user", lambda: {"id": 999, "is_admin": 1})
    rid_a = _restaurant(db_path, name="Co A")
    rid_b = _restaurant(db_path, name="Co B")
    log_ai_usage(rid_a, "draft_response", "claude-sonnet-5", 1000, 200, db_path=db_path)
    log_ai_usage(rid_b, "draft_response", "claude-sonnet-5", 5000, 1000, db_path=db_path)
    with app.test_request_context(f"/admin/ai-usage/{rid_a}"):
        resp = ai_usage(rid_a)
    assert resp.get_json()["total_calls"] == 1
