"""The admin's Invoice intake card (bar fix round 10/15/26, F10 / V12):
Customers → restaurant → Data & integrations. Create or rotate a
restaurant's intake address, change its senders, release a held email,
re-read and purge a file — each through one typed audit row
(admin_events.record_admin_action), rotate and purge behind the step-up.
Nothing here emails anyone; Resend is never reached.
"""
import json
import sys

import pytest
from flask import Flask

import admin_routes
import auth
import email_intake
import fintech_invoices as fi
import job_queue
import models
import ops
from auth import create_session, create_user, init_auth
from models import Restaurant, create_restaurant

CSRF = "intake-admin-csrf"
EXPORT = (
    "Vendor Name,Invoice Number,Invoice Date,Invoice Total,Product Number,Product Description,U.P.C,"
    "Unit Of Measure,Item Quantity,Item Cost,Item Adjustment\n"
    "Breakthru,0128850598,09/15/2026,$252.00,9637400,HIGH NOON VODKA SELTZER,085000000000,CA,6,$42.00,$0.00\n"
)


@pytest.fixture(autouse=True)
def _world(monkeypatch, db_path):
    real = models.get_conn
    redirect = lambda *a, **k: real(db_path)
    for mod in list(sys.modules.values()):
        try:
            if getattr(mod, "get_conn", None) is real:
                monkeypatch.setattr(mod, "get_conn", redirect)
        except Exception:
            pass
    for mod in (models, auth, admin_routes, ops):
        monkeypatch.setattr(mod, "get_conn", redirect, raising=False)
        monkeypatch.setattr(mod, "DB_PATH", db_path, raising=False)
    monkeypatch.setattr(job_queue, "kick", lambda *a, **k: None)
    monkeypatch.setenv("INTAKE_EMAIL_DOMAIN", "intake.cavnar.test")
    init_auth(db_path=db_path)
    # Admin jobs run inline, so a test reads their result.
    monkeypatch.setattr(admin_routes, "_submit_admin_job", lambda job_id, fn, *a: fn(*a))


@pytest.fixture
def app():
    flask_app = Flask(__name__, template_folder="../templates")
    flask_app.secret_key = "intake-admin"
    flask_app.register_blueprint(admin_routes.admin_bp)
    return flask_app


def _admin(app, db_path, stepped_up=True):
    hq = create_restaurant(Restaurant(name="Cavnar AI Admin", owner_email="will@cavnar.test"), db_path=db_path)
    uid = create_user(hq, "will", "will@cavnar.test", "Admin-pass-2026", is_admin=True, db_path=db_path)
    c = app.test_client()
    c.set_cookie("session_token", create_session(uid, password_verified_at=True if stepped_up else None,
                                                 db_path=db_path))
    c.set_cookie("csrf_js", CSRF)
    return c


def _post(c, url, body=None):
    return c.post(url, json=body if body is not None else {}, headers={"X-CSRF": CSRF})


def _rid(db_path):
    return create_restaurant(Restaurant(name="Simple EJ's", owner_email="erik@ejs.test"), db_path=db_path)


def _rows(db_path, sql, args=()):
    conn = models.get_conn(db_path)
    try:
        return [dict(r) for r in conn.execute(sql, args).fetchall()]
    finally:
        conn.close()


def _events(db_path, action):
    return _rows(db_path, "SELECT * FROM admin_events WHERE event_type=? ORDER BY id", (action,))


def test_rotate_route_records_admin_action_and_deactivates_old(app, db_path):
    c = _admin(app, db_path)
    rid = _rid(db_path)
    r = _post(c, f"/admin/api/client/{rid}/intake/create",
              {"allowed_senders": "processor@fintech.net\n@cavnar.ai"})
    assert r.status_code == 200, r.get_json()
    first = r.get_json()["address"]
    assert first.startswith("fintech-") and first.endswith("@intake.cavnar.test")
    assert _post(c, f"/admin/api/client/{rid}/intake/create", {"allowed_senders": "a@b.test"}).status_code == 409
    r = _post(c, f"/admin/api/client/{rid}/intake/rotate")
    assert r.status_code == 200, r.get_json()
    new = r.get_json()["address"]
    assert new != first and r.get_json()["retired"] == [first]
    routes = {x["address"]: x for x in _rows(db_path, "SELECT * FROM email_intake_routes WHERE restaurant_id=?",
                                             (rid,))}
    assert routes[first]["active"] == 0 and routes[first]["retired_at"]
    assert routes[new]["active"] == 1
    assert routes[new]["allowed_senders"].split("\n") == ["processor@fintech.net", "@cavnar.ai"]
    ev = _events(db_path, "intake.route_rotated")
    assert len(ev) == 1 and ev[0]["actor"] == "will" and ev[0]["restaurant_id"] == rid and ev[0]["result"] == "ok"
    assert first in ev[0]["before_json"] and new in ev[0]["after_json"]
    assert len(_events(db_path, "intake.route_created")) == 2          # one made, one refused
    # Mail to the retired address is never fetched again.
    assert email_intake.receive({"email_id": "old1", "to": [first], "from": "processor@fintech.net"},
                                db_path=db_path) == "no_route"


def test_rotate_and_purge_need_the_step_up(app, db_path):
    c = _admin(app, db_path, stepped_up=False)
    rid = _rid(db_path)
    for url, body in ((f"/admin/api/client/{rid}/intake/rotate", {}),
                      (f"/admin/api/client/{rid}/intake/purge", {"file_id": 1})):
        r = _post(c, url, body)
        assert r.status_code == 403 and r.get_json()["reauth_required"] is True, url
    # Making the first address and changing senders do not.
    assert _post(c, f"/admin/api/client/{rid}/intake/create",
                 {"allowed_senders": "processor@fintech.net"}).status_code == 200


def test_senders_are_checked_and_audited(app, db_path):
    c = _admin(app, db_path)
    rid = _rid(db_path)
    addr = _post(c, f"/admin/api/client/{rid}/intake/create",
                 {"allowed_senders": "processor@fintech.net"}).get_json()["address"]
    r = _post(c, f"/admin/api/client/{rid}/intake/senders", {"address": addr, "allowed_senders": "nobody"})
    assert r.status_code == 400 and "isn't an email address" in r.get_json()["error"]
    r = _post(c, f"/admin/api/client/{rid}/intake/senders",
              {"address": addr, "allowed_senders": "processor@fintech.net, will@cavnar.ai"})
    assert r.status_code == 200 and r.get_json()["allowed_senders"] == ["processor@fintech.net", "will@cavnar.ai"]
    other = _rid(db_path)
    assert _post(c, f"/admin/api/client/{other}/intake/senders",
                 {"address": addr, "allowed_senders": "x@y.test"}).status_code == 400
    ev = _events(db_path, "intake.senders_set")
    assert len(ev) == 1 and "will@cavnar.ai" in ev[0]["after_json"]


def test_a_held_email_is_released_through_the_console(app, db_path):
    c = _admin(app, db_path)
    rid = _rid(db_path)
    addr = _post(c, f"/admin/api/client/{rid}/intake/create",
                 {"allowed_senders": "processor@fintech.net"}).get_json()["address"]
    assert email_intake.receive({"email_id": "h1", "to": [addr], "from": "erik@ejs.test"},
                                db_path=db_path) == "held_sender"
    view = c.get(f"/admin/api/client/{rid}/intake").get_json()
    assert view["ok"] and view["routes"][0]["address"] == addr and view["routes"][0]["allowed_senders"]
    assert view["emails"][0]["status"] == "held_sender" and view["emails"][0]["releasable"] is True
    assert "feed" in view and "files" in view
    r = _post(c, f"/admin/api/client/{rid}/intake/release", {"email_id": "h1"})
    assert r.status_code == 200, r.get_json()
    jobs = _rows(db_path, "SELECT payload_json FROM jobs WHERE kind='email_intake'")
    assert [json.loads(j["payload_json"]) for j in jobs] == [{"email_id": "h1", "released": True}]
    ev = _events(db_path, "intake.email_released")
    assert len(ev) == 1 and ev[0]["target"] == "email_intake:h1"
    assert _post(c, f"/admin/api/client/{rid}/intake/release", {"email_id": "h1"}).status_code == 400


def test_reread_and_purge_a_file(app, db_path, monkeypatch):
    c = _admin(app, db_path)
    rid = _rid(db_path)
    res = fi.ingest_file(rid, "x.csv", EXPORT.encode(), db_path=db_path)
    r = _post(c, f"/admin/api/client/{rid}/intake/reread", {"file_id": res["file_id"]})
    assert r.status_code == 200 and r.get_json()["job_id"]
    job = ops.read_async_job(r.get_json()["job_id"])
    assert job["status"] == "done" and job["result"]["invoices"] == 1
    assert _events(db_path, "intake.file_reread")[0]["result"] == "ok"
    # Purge: the ingest workstream's fintech_invoices.purge_file does the work.
    called = []
    monkeypatch.setattr(fi, "purge_file", lambda rid_, fid: called.append((rid_, fid)) or {"ok": True, "invoices": 1},
                        raising=False)
    r = _post(c, f"/admin/api/client/{rid}/intake/purge", {"file_id": res["file_id"]})
    assert r.status_code == 200, r.get_json()
    assert called == [(rid, res["file_id"])]
    ev = _events(db_path, "intake.file_purged")
    assert len(ev) == 1 and ev[0]["result"] == "ok" and "x.csv" in ev[0]["before_json"]
    # Another restaurant's file is not this one's to purge.
    other = _rid(db_path)
    assert _post(c, f"/admin/api/client/{other}/intake/purge", {"file_id": res["file_id"]}).status_code == 404
