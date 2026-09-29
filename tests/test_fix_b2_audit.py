"""Fix round B2 — the admin audit trail (#53, #72, #126).

What these protect:
  * An admin write is recorded AFTER it runs, with the actor, the result it
    ended with, the restaurant (in the column), the target ids, the IP and
    the redacted body — and only once an actor is resolved: anonymous,
    non-admin and CSRF-refused attempts go to the capped admin_audit_refused
    table instead of the audit trail.
  * record_admin_action stores before/after with secrets redacted, and links
    to the request's generic row, which the audit view folds away.
  * Every blueprint that serves admin writes is covered: the POS blueprints,
    the sales-audit tool and the admin imports on client_bp (admin-only).
  * Deleting a restaurant keeps its audit rows and its offboarding record.
  * The billing history reads Stripe and DocuSign only; client history and
    "What changed" leave out the per-request rows.
"""
import json

import pytest
from flask import Flask

import admin_events
import admin_routes
import auth
import auth_routes
import client_api
import models
import status_manager
import status_routes
from auth import create_session, create_user, init_auth, upsert_membership
from models import Restaurant, create_restaurant

CSRF = "fix-b2-audit-csrf"


@pytest.fixture(autouse=True)
def _redirect_db(db_path, monkeypatch):
    real = models.get_conn
    redirect = lambda *a, **k: real(db_path)
    for mod in (models, auth, auth_routes, admin_routes, client_api):
        monkeypatch.setattr(mod, "get_conn", redirect, raising=False)
    monkeypatch.setattr(models, "DB_PATH", db_path)
    monkeypatch.setattr(auth, "DB_PATH", db_path)
    monkeypatch.setattr(status_manager, "DB_PATH", db_path)
    init_auth(db_path=db_path)
    models.init_email_log(db_path=db_path)
    auth_routes._login_attempts.clear()
    admin_events._refused_window.clear()
    admin_events._coalesce_last.clear()


@pytest.fixture
def app():
    import toast_routes
    import sales_audit_routes
    from csrf import csrf_protect
    # Wire CSRF onto the audit blueprint before any app registers it, as
    # tests/test_sales_audit.py does: a blueprint can't take a new hook once
    # registered, and that file wires it if nobody has.
    if not getattr(sales_audit_routes.audit_bp, "_csrf_wired", False):
        csrf_protect(sales_audit_routes.audit_bp)
        sales_audit_routes.audit_bp._csrf_wired = True
    flask_app = Flask(__name__, template_folder="../templates")
    flask_app.secret_key = "fix-b2-audit"
    for bp in (admin_routes.admin_bp, auth_routes.auth_bp, status_routes.status_bp, client_api.client_bp,
               toast_routes.toast_bp, sales_audit_routes.audit_bp):
        flask_app.register_blueprint(bp)
    return flask_app


def _client(app, token=None, csrf=True):
    c = app.test_client()
    if token:
        c.set_cookie("session_token", token)
    if csrf:
        c.set_cookie("csrf_js", CSRF)
    return c


def _post(c, url, csrf=True, **kw):
    headers = kw.pop("headers", {})
    if csrf:
        headers.setdefault("X-CSRF", CSRF)
    return c.post(url, headers=headers, **kw)


def _admin(db_path, username="will"):
    home = create_restaurant(Restaurant(name="Cavnar HQ", owner_email=f"{username}@cavnar.test"), db_path=db_path)
    uid = create_user(home, username, f"{username}@cavnar.test", "Admin-pass-2026", is_admin=True, db_path=db_path)
    return home, uid


def _client_restaurant(db_path, name="Client Grill", email="owner@client.test"):
    rid = create_restaurant(Restaurant(name=name, owner_email=email), db_path=db_path)
    uid = create_user(rid, name.split()[0].lower(), email, "Owner-pass-2026", db_path=db_path)
    upsert_membership(uid, rid, "client", db_path=db_path)
    return rid, uid


def _rows(db_path, sql, args=()):
    conn = models.get_conn(db_path)
    try:
        return [dict(r) for r in conn.execute(sql, args).fetchall()]
    finally:
        conn.close()


# ── who gets a row, and where ───────────────────────────────────────────────

def test_an_admin_write_is_recorded_after_it_runs_with_outcome_actor_restaurant_and_ip(app, db_path):
    _, admin_uid = _admin(db_path)
    rid, _ = _client_restaurant(db_path)
    c = _client(app, create_session(admin_uid, db_path=db_path))
    r = _post(c, f"/admin/client-settings/{rid}", json={"name": "Audited Grill"},
              environ_base={"REMOTE_ADDR": "203.0.113.9"})
    assert r.status_code == 200
    row = _rows(db_path, "SELECT * FROM admin_events WHERE source='audit' ORDER BY id DESC LIMIT 1")[0]
    assert row["event_type"] == "admin_write:admin.save_client_settings"
    assert row["actor"] == "will" and row["actor_id"] == admin_uid
    assert row["restaurant_id"] == rid, "the restaurant is on the column now, so the client's audit view finds it"
    assert row["result"] == "ok" and row["ip"] == "203.0.113.9"
    payload = json.loads(row["payload"])
    assert payload["status"] == 200 and payload["body"]["name"] == "Audited Grill"


def test_an_anonymous_admin_post_never_reaches_the_audit_trail(app, db_path):
    c = _client(app, csrf=False)
    r = c.post("/admin/api/jobs/%3Cscript%3Eanything/run", json={})
    assert r.status_code in (401, 403)
    assert _rows(db_path, "SELECT * FROM admin_events") == []
    refused = _rows(db_path, "SELECT * FROM admin_audit_refused")
    assert len(refused) == 1 and refused[0]["reason"] == "anonymous" and refused[0]["status"] in (401, 403)


def test_a_client_login_posting_to_admin_is_a_refused_attempt_not_an_audit_row(app, db_path):
    _, owner_uid = _client_restaurant(db_path)
    c = _client(app, create_session(owner_uid, db_path=db_path))
    r = _post(c, "/admin/freeze/1", json={"reason": "x"})
    assert r.status_code == 401
    assert _rows(db_path, "SELECT * FROM admin_events") == []
    refused = _rows(db_path, "SELECT reason, user_id FROM admin_audit_refused")
    assert refused == [{"reason": "not_admin", "user_id": owner_uid}]


def test_a_write_that_failed_csrf_is_refused_not_attributed(app, db_path):
    _, admin_uid = _admin(db_path)
    rid, _ = _client_restaurant(db_path)
    c = _client(app, create_session(admin_uid, db_path=db_path), csrf=False)
    c.post(f"/admin/client-settings/{rid}", json={"name": "Forged"})
    assert _rows(db_path, "SELECT * FROM admin_events WHERE source='audit'") == []
    assert [r["reason"] for r in _rows(db_path, "SELECT reason FROM admin_audit_refused")] == ["csrf"]


def test_a_support_logins_refused_write_is_recorded_with_its_actor(app, db_path):
    home = create_restaurant(Restaurant(name="Cavnar HQ", owner_email="s@cavnar.test"), db_path=db_path)
    sup = create_user(home, "sam", "sam@cavnar.test", "Support-pass-2026", db_path=db_path, role="support")
    conn = models.get_conn(db_path)
    conn.execute("UPDATE users SET role='support' WHERE id=?", (sup,))
    conn.execute("DELETE FROM memberships WHERE user_id=?", (sup,))
    conn.commit(); conn.close()
    rid, _ = _client_restaurant(db_path)
    c = _client(app, create_session(sup, db_path=db_path))
    r = _post(c, f"/admin/client-settings/{rid}", json={"name": "Nope"})
    assert r.status_code == 403
    row = _rows(db_path, "SELECT actor, result FROM admin_events WHERE source='audit'")
    assert row == [{"actor": "sam", "result": "refused"}]


def test_a_route_that_answers_ok_false_is_recorded_as_failed(app, db_path):
    _, admin_uid = _admin(db_path)
    c = _client(app, create_session(admin_uid, db_path=db_path))
    r = _post(c, "/admin/api/schedule-experiments/pin", json={})
    assert r.status_code == 200 and r.get_json()["ok"] is False
    row = _rows(db_path, "SELECT result FROM admin_events WHERE source='audit' ORDER BY id DESC LIMIT 1")[0]
    assert row["result"] == "failed"


def test_the_body_is_recorded_with_secrets_redacted(app, db_path):
    _, admin_uid = _admin(db_path)
    rid, _ = _client_restaurant(db_path)
    c = _client(app, create_session(admin_uid, db_path=db_path))
    _post(c, f"/admin/toast/save/{rid}", json={"client_id": "cid-123", "client_secret": "s3cr3t-value",
                                              "restaurant_guid": "guid-9", "test": False})
    row = _rows(db_path, "SELECT payload, restaurant_id FROM admin_events WHERE event_type LIKE 'admin_write:toast.%'")
    assert row, "the Toast credential save (toast_bp) is audited now (#126)"
    assert row[0]["restaurant_id"] == rid
    assert "s3cr3t-value" not in row[0]["payload"]
    body = json.loads(row[0]["payload"])["body"]
    assert body["client_secret"] == "[redacted]" and body["client_id"] == "cid-123"


def test_the_sales_audit_tool_writes_are_audited(app, db_path):
    import sales_audits
    sales_audits.init_sales_audits(db_path=db_path)
    _, admin_uid = _admin(db_path)
    c = _client(app, create_session(admin_uid, db_path=db_path))
    r = _post(c, "/admin/api/audits", json={"restaurant_name": "Prospect Grill"})
    assert r.status_code == 200
    rows = _rows(db_path, "SELECT event_type, actor FROM admin_events WHERE source='audit'")
    assert {"event_type": "admin_write:sales_audit.api_create", "actor": "will"} in rows


def test_the_sales_audit_autosave_is_one_row_per_window_without_the_answers(app, db_path):
    import sales_audits
    sales_audits.init_sales_audits(db_path=db_path)
    _, admin_uid = _admin(db_path)
    c = _client(app, create_session(admin_uid, db_path=db_path))
    aid = _post(c, "/admin/api/audits", json={"restaurant_name": "Prospect Grill"}).get_json()["id"]
    for i in range(4):
        _post(c, f"/admin/api/audits/{aid}", json={"answers": {"restaurant_name": "Prospect Grill",
                                                               "fin_annual_revenue": 1000000 + i}})
    rows = _rows(db_path, "SELECT payload FROM admin_events WHERE event_type='admin_write:sales_audit.api_save'")
    assert len(rows) == 1, "typing in the audit must not write an audit row per keystroke-save"
    assert "1000000" not in rows[0]["payload"] and json.loads(rows[0]["payload"])["body"] == {"_keys": ["answers"]}


def test_an_admin_import_on_the_client_blueprint_is_audited_but_an_owners_is_not(app, db_path):
    import io
    _, admin_uid = _admin(db_path)
    rid, owner_uid = _client_restaurant(db_path)
    csv_bytes = b"rating,text,author\n5,Lovely,Ann\n"
    admin = _client(app, create_session(admin_uid, db_path=db_path))
    _post(admin, "/api/import-tripadvisor", data={"restaurant_id": str(rid), "platform": "tripadvisor",
                                                  "file": (io.BytesIO(csv_bytes), "r.csv")},
          content_type="multipart/form-data")
    owner = _client(app, create_session(owner_uid, db_path=db_path))
    _post(owner, "/api/import-tripadvisor", data={"file": (io.BytesIO(csv_bytes), "r.csv")},
          content_type="multipart/form-data")
    rows = _rows(db_path, "SELECT actor, restaurant_id FROM admin_events WHERE event_type='admin_write:client.import_tripadvisor'")
    assert rows == [{"actor": "will", "restaurant_id": rid}]
    assert _rows(db_path, "SELECT * FROM admin_audit_refused") == []


# ── typed actions ────────────────────────────────────────────────────────────

def test_record_admin_action_redacts_secrets_in_before_and_after(db_path):
    rid = create_restaurant(Restaurant(name="Typed Co", owner_email="t@x.test"), db_path=db_path)
    row_id = admin_events.record_admin_action(
        {"id": 7, "username": "will"}, "pos.credentials_changed", restaurant_id=rid, target=("user", 12),
        before={"toast_client_secret": "old-secret", "toast_client_id": "a"},
        after={"toast_client_secret": "new-secret", "nested": {"api_key": "k", "note": "fine"},
               "url": "https://x.test/cb?token=abc123"},
        db_path=db_path)
    row = _rows(db_path, "SELECT * FROM admin_events WHERE id=?", (row_id,))[0]
    assert row["source"] == "admin" and row["actor"] == "will" and row["actor_id"] == 7
    assert row["target"] == "user:12" and row["result"] == "ok"
    assert "old-secret" not in row["before_json"] and "new-secret" not in row["after_json"]
    after = json.loads(row["after_json"])
    assert after["nested"] == {"api_key": "[redacted]", "note": "fine"}
    assert "abc123" not in after["url"]


def test_record_admin_action_never_raises(db_path, monkeypatch):
    monkeypatch.setattr(models, "get_conn", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("db down")))
    assert admin_events.record_admin_action("will", "anything") is None


def test_the_audit_view_folds_a_requests_generic_row_under_its_typed_action(app, db_path):
    _, admin_uid = _admin(db_path)
    rid, owner_uid = _client_restaurant(db_path)
    models.update_restaurant(rid, {"is_demo": 0}, db_path=db_path)
    # The demo flag needs the step-up (integration wave): a fresh password sign-in.
    c = _client(app, create_session(admin_uid, password_verified_at=True, db_path=db_path))
    _post(c, f"/admin/api/client/{rid}/demo", json={"is_demo": 1, "confirm_name": "Client Grill"})
    rows = _rows(db_path, "SELECT source, request_id FROM admin_events WHERE restaurant_id=? ORDER BY id", (rid,))
    assert {r["source"] for r in rows} == {"admin", "audit"}
    assert len({r["request_id"] for r in rows}) == 1, "the typed row and the request row share the request id"
    folded = c.get(f"/admin/api/client/{rid}/audit").get_json()
    assert [e["kind"] for e in folded["events"]] == ["action"]
    assert folded["events"][0]["action"] == "demo_flag.set"
    assert folded["events"][0]["before"]["is_demo"] == 0 and folded["events"][0]["after"]["is_demo"] == 1
    both = c.get(f"/admin/api/client/{rid}/audit?all=1").get_json()
    assert sorted(e["kind"] for e in both["events"]) == ["action", "request"]


def test_the_fleet_audit_list_pages_by_id_and_filters(db_path):
    rid = create_restaurant(Restaurant(name="Paged Co", owner_email="p@x.test"), db_path=db_path)
    for i in range(7):
        admin_events.record_admin_action("will" if i % 2 else "sam", f"thing.done{i}", restaurant_id=rid,
                                         db_path=db_path)
    page1 = admin_events.audit_list(limit=3, db_path=db_path)
    assert len(page1["events"]) == 3 and page1["next_before_id"]
    page2 = admin_events.audit_list(limit=3, before_id=page1["next_before_id"], db_path=db_path)
    assert not {e["id"] for e in page1["events"]} & {e["id"] for e in page2["events"]}
    only_sam = admin_events.audit_list(actor="SAM", db_path=db_path)["events"]
    assert only_sam and all(e["actor"] == "sam" for e in only_sam)
    assert [e["action"] for e in admin_events.audit_list(action="thing.done3", db_path=db_path)["events"]] == ["thing.done3"]


def test_the_audit_endpoints_answer_for_the_fleet_and_the_refused_table(app, db_path):
    _, admin_uid = _admin(db_path)
    c = _client(app, create_session(admin_uid, db_path=db_path))
    admin_events.record_admin_action("will", "x.y", db_path=db_path)
    body = c.get("/admin/api/audit?limit=10").get_json()
    assert body["ok"] and body["events"][0]["action"] == "x.y"
    _client(app, csrf=False).post("/admin/create-client", json={})
    ref = c.get("/admin/api/audit/refused").get_json()
    assert ref["ok"] and ref["attempts"][0]["reason"] == "anonymous"


# ── the refused table is bounded ─────────────────────────────────────────────

def test_refused_attempts_are_rate_limited_per_ip(app, db_path, monkeypatch):
    monkeypatch.setattr(admin_events, "REFUSED_PER_IP", 3)
    c = _client(app, csrf=False)
    for _ in range(8):
        c.post("/admin/create-client", json={}, environ_base={"REMOTE_ADDR": "198.51.100.7"})
    assert len(_rows(db_path, "SELECT * FROM admin_audit_refused")) == 3


def test_refused_attempts_are_capped_and_pruned(db_path, monkeypatch):
    monkeypatch.setattr(admin_events, "REFUSED_CAP_ROWS", 10)
    conn = models.get_conn(db_path)
    conn.execute("INSERT INTO admin_audit_refused (created_at, reason) VALUES (datetime('now','-40 days'), 'old')")
    for _ in range(48):
        conn.execute("INSERT INTO admin_audit_refused (reason) VALUES ('anonymous')")
    conn.commit(); conn.close()
    app = Flask(__name__)
    with app.test_request_context("/admin/x", method="POST", environ_base={"REMOTE_ADDR": "192.0.2.1"}):
        admin_events._record_refused("anonymous", 401, None)          # id 50: every 50th insert prunes
    rows = _rows(db_path, "SELECT id, reason FROM admin_audit_refused")
    assert len(rows) == 10 and "old" not in {r["reason"] for r in rows}


# ── deletion keeps the record ────────────────────────────────────────────────

def test_deleting_a_restaurant_keeps_its_audit_rows_and_offboarding_record(db_path):
    rid = create_restaurant(Restaurant(name="Gone Co", owner_email="g@x.test"), db_path=db_path)
    admin_events.record_admin_action("will", "something.done", restaurant_id=rid, db_path=db_path)
    admin_events.record("stripe", "invoice.paid", restaurant_id=rid, db_path=db_path)
    conn = models.get_conn(db_path)
    conn.execute("INSERT INTO offboarding_steps (restaurant_id, step, status) VALUES (?, 'export', 'done')", (rid,))
    conn.execute("INSERT INTO support_notes (restaurant_id, author, body) VALUES (?, 'will', 'a note')", (rid,))
    conn.commit(); conn.close()
    deleted = models.delete_restaurant(rid, db_path=db_path)
    assert "admin_events" not in deleted and "offboarding_steps" not in deleted
    assert deleted.get("support_notes") == 1, "notes about the customer go with the customer"
    assert len(_rows(db_path, "SELECT * FROM admin_events WHERE restaurant_id=?", (rid,))) == 2
    assert len(_rows(db_path, "SELECT * FROM offboarding_steps WHERE restaurant_id=?", (rid,))) == 1


# ── history views read the right rows ────────────────────────────────────────

def test_billing_history_is_stripe_and_docusign_only(db_path, monkeypatch):
    import admin_ops
    monkeypatch.setattr(admin_ops, "get_conn", lambda *a, **k: models.get_conn(db_path), raising=False)
    monkeypatch.delenv("STRIPE_SECRET_KEY", raising=False)
    rid = create_restaurant(Restaurant(name="Billing Co", owner_email="b@x.test"), db_path=db_path)
    admin_events.record("stripe", "invoice.paid", restaurant_id=rid, db_path=db_path)
    admin_events.record("docusign", "contract.signed", restaurant_id=rid, db_path=db_path)
    for i in range(5):
        admin_events.record("audit", f"admin_write:admin.x{i}", restaurant_id=rid, db_path=db_path)
    admin_events.record_admin_action("will", "freeze", restaurant_id=rid, db_path=db_path)
    events = admin_events.recent(limit=120, sources=("stripe", "docusign"), db_path=db_path)
    assert {e["source"] for e in events} == {"stripe", "docusign"}
    src = open(admin_ops.__file__, encoding="utf-8").read()
    assert 'recent(limit=120, sources=("stripe", "docusign"))' in src
    assert "WHERE restaurant_id=? AND source <> 'audit'" in src
    assert "e.created_at >= ? AND e.source <> 'audit'" in src
