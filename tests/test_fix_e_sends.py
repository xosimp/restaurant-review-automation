"""Fix round E — success recorded only after it happens: supplier orders
(#103), the order cooldown in the database (#96), review-request consent and
method (#159), the webhook test owner-only and queued (#156), outbound
webhooks and pushes through a durable outbox with event ids (#75), and
net_safety pinning the vetted address (#156)."""
import json
import types

import pytest

import auth
import client_api
import models
import webhooks
from models import Restaurant, create_restaurant, get_conn


@pytest.fixture(autouse=True)
def _redirect(monkeypatch, db_path):
    import notify, push, guest_marketing, issues
    real = models.get_conn
    redirect = lambda *a, **k: real(db_path)
    for mod in (models, client_api, notify, push, webhooks, auth, issues, guest_marketing):
        monkeypatch.setattr(mod, "get_conn", redirect, raising=False)
    monkeypatch.setattr(models, "DB_PATH", db_path)
    monkeypatch.setattr(client_api, "get_restaurant", lambda r, *a, **k: models.get_restaurant(r, db_path))
    guest_marketing.init_guest_marketing(db_path)
    auth.init_auth(db_path)
    webhooks.init_webhooks(db_path)
    push.init_push(db_path)


def _rid(db_path, **kw):
    kw.setdefault("name", "Send Co")
    kw.setdefault("owner_email", "o@send.test")
    return create_restaurant(Restaurant(**kw), db_path=db_path)


def _rows(db_path, sql, args=()):
    conn = get_conn(db_path)
    try:
        return [dict(r) for r in conn.execute(sql, args).fetchall()]
    finally:
        conn.close()


# ── #103: a supplier email that did not go is not an order that went ───────

def _group(email="orders@fresh.test"):
    return {"supplier_email": email, "supplier_name": "Fresh Co", "total_cost": 12.0,
            "items": [{"item": "Romaine", "qty": 3, "unit": "case"}], "draft_hash": "h1"}


@pytest.mark.parametrize("result", [
    pytest.param(lambda e: e.SendResult(False, error="recipient suppressed (bounced/complained)", attempts=0,
                                        reason="suppressed"), id="suppressed"),
    pytest.param(lambda e: e.SendResult(False, error="503", status_code=503, attempts=3, reason="transient"),
                 id="resend-down"),
    pytest.param(lambda e: e.not_sent("not_configured"), id="no-key"),
])
def test_a_failed_supplier_email_voids_the_po_and_is_reported_failed(db_path, monkeypatch, result):
    import emails, rec_ledger
    rid = _rid(db_path)
    monkeypatch.setattr(emails, "send_supplier_order_email", lambda **kw: result(emails))
    events, implemented = [], []
    monkeypatch.setattr(client_api, "log_account_event", lambda *a, **k: events.append(a))
    monkeypatch.setattr(rec_ledger, "implemented", lambda *a, **k: implemented.append(a))
    sent, failed = client_api._send_supplier_orders(rid, models.get_restaurant(rid, db_path), [_group()], {"id": 1})
    assert sent == [] and len(failed) == 1 and failed[0]["voided_po"]
    assert events == [] and implemented == []
    pos = models.get_purchase_orders(rid, db_path=db_path)
    assert all(p["status"] != "sent" for p in pos), "the PO stayed open and blocks the retry"
    # The retry is allowed: nothing is open for this draft.
    assert models.open_purchase_order(rid, "orders@fresh.test", "h1", db_path=db_path) is None


def test_a_sent_supplier_email_is_logged_against_the_restaurant(db_path, monkeypatch):
    import emails
    rid = _rid(db_path)
    monkeypatch.setattr(emails, "_resend_key", lambda: "k")
    monkeypatch.setattr("requests.post", lambda url, **kw: types.SimpleNamespace(
        status_code=200, text='{"id":"m1"}', json=lambda: {"id": "m1"}))
    sent, failed = client_api._send_supplier_orders(rid, models.get_restaurant(rid, db_path), [_group()], {"id": 1})
    assert len(sent) == 1 and failed == []
    rows = _rows(db_path, "SELECT restaurant_id, email_type, status FROM email_log")
    assert rows == [{"restaurant_id": rid, "email_type": "send_supplier_order_email", "status": "sent"}]


def test_the_trusted_order_job_tells_the_owner_when_the_email_failed(db_path, monkeypatch):
    import delayed, emails, inventory
    rid = _rid(db_path)
    monkeypatch.setattr(inventory, "build_supplier_orders",
                        lambda r: {"draft_hash": "h1", "groups": [_group()]})
    monkeypatch.setattr(emails, "send_supplier_order_email",
                        lambda **kw: emails.SendResult(False, error="422", status_code=422, attempts=1,
                                                       reason="rejected"))
    told = []
    monkeypatch.setattr(delayed, "_tell_owner_order_not_sent", lambda r, p, why, db: told.append(why))
    out = delayed._run_order_send(rid, {"supplier_email": "orders@fresh.test", "draft_hash": "h1",
                                        "automatic": True}, db_path)
    assert out["ok"] is False and told and "refused" in told[0]


# ── #96: the cooldown is in the database ────────────────────────────────────

def test_the_supplier_cooldown_survives_the_process(db_path):
    assert client_api._order_send_allowed(7, ["a@x.test", "b@x.test"]) is True
    assert client_api._order_send_allowed(7, ["b@x.test"]) is False
    # All or nothing: a key still cooling gives back the ones just taken.
    assert client_api._order_send_allowed(7, ["c@x.test", "a@x.test"]) is False
    assert client_api._order_send_allowed(7, ["c@x.test"]) is True
    assert not hasattr(client_api, "_order_send_last")


# ── #159: review-request texts ──────────────────────────────────────────────

def test_a_review_request_text_writes_consent_evidence_first(db_path, monkeypatch):
    import notify, guest_marketing
    rid = _rid(db_path, google_place_id="ChIJx")
    monkeypatch.setattr(guest_marketing, "guest_sms_allowed_now", lambda r: True)
    texted = []
    monkeypatch.setattr(notify, "send_sms", lambda phone, msg, **kw: texted.append(phone) or True)
    out, code = client_api._do_send_review_request(
        rid, {"name": "Ana", "phone": "5551234567", "sms_consent": True},
        actor={"id": 9, "username": "gm-ana"})
    assert code == 200 and texted
    ev = _rows(db_path, "SELECT event, source, detail FROM guest_consent_events")
    assert ev and ev[0]["event"] == "review_only" and ev[0]["source"] == "owner_attested"
    assert "gm-ana" in ev[0]["detail"]
    assert _rows(db_path, "SELECT method FROM review_requests")[0]["method"] == "sms"


def test_a_failed_text_is_not_logged_as_both(db_path, monkeypatch):
    import notify, guest_marketing, emails
    rid = _rid(db_path, google_place_id="ChIJx")
    monkeypatch.setattr(guest_marketing, "guest_sms_allowed_now", lambda r: True)
    monkeypatch.setattr(notify, "send_sms", lambda phone, msg, **kw: False)
    monkeypatch.setattr(emails, "_resend_key", lambda: "k")
    monkeypatch.setattr(emails, "deliver", lambda **kw: emails.SendResult(True, message_id="m"))
    out, code = client_api._do_send_review_request(
        rid, {"name": "Ana", "phone": "5551234567", "email": "ana@x.test", "sms_consent": True})
    assert code == 200
    assert _rows(db_path, "SELECT method FROM review_requests")[0]["method"] == "email"


# ── #75 / #156: outbound webhooks ───────────────────────────────────────────

def _hook(db_path, rid, events=("review.received",)):
    conn = get_conn(db_path)
    conn.execute("INSERT INTO webhooks (restaurant_id, url, secret, events) VALUES (?,?,?,?)",
                 (rid, "https://93.184.216.34/hook", "whsec_x", json.dumps(list(events))))
    conn.commit()
    conn.close()


class _Inline:
    def submit(self, fn, *a, **k):
        fn(*a, **k)


def test_a_webhook_event_is_written_before_delivery_and_carries_an_id(db_path, monkeypatch):
    import net_safety
    rid = _rid(db_path)
    _hook(db_path, rid)
    posted = []
    monkeypatch.setattr(net_safety, "safe_post", lambda url, **kw: posted.append(kw) or
                        types.SimpleNamespace(status_code=200, ok=True))
    monkeypatch.setattr(webhooks, "_webhook_executor", lambda: _Inline())
    event_id = webhooks.fire_webhook(rid, "review.received", {"x": 1}, db_path=db_path)
    assert event_id and event_id.startswith("evt_")
    body = json.loads(posted[0]["data"])
    assert body["id"] == event_id and posted[0]["headers"]["X-Cavnar-Event-Id"] == event_id
    row = _rows(db_path, "SELECT state, event_id FROM webhook_outbox")[0]
    assert row == {"state": "delivered", "event_id": event_id}
    assert _rows(db_path, "SELECT event_id FROM webhook_deliveries")[0]["event_id"] == event_id
    assert _rows(db_path, "SELECT last_success_at FROM webhooks")[0]["last_success_at"]


def test_an_event_a_restart_left_behind_is_delivered_by_the_reaper(db_path, monkeypatch):
    import net_safety
    rid = _rid(db_path)
    _hook(db_path, rid)

    class _Dead:
        def submit(self, fn, *a, **k):
            pass                    # the process died before the pool ran it
    monkeypatch.setattr(webhooks, "_webhook_executor", lambda: _Dead())
    event_id = webhooks.fire_webhook(rid, "review.received", {"x": 1}, db_path=db_path)
    assert _rows(db_path, "SELECT state FROM webhook_outbox")[0]["state"] == "queued"
    conn = get_conn(db_path)
    conn.execute("UPDATE webhook_outbox SET created_at=datetime('now','-5 minutes'), "
                 "updated_at=datetime('now','-5 minutes')")
    conn.commit()
    conn.close()
    posted = []
    monkeypatch.setattr(net_safety, "safe_post", lambda url, **kw: posted.append(kw) or
                        types.SimpleNamespace(status_code=200, ok=True))
    monkeypatch.setattr(webhooks, "_webhook_executor", lambda: _Inline())
    monkeypatch.setattr(webhooks, "_in_pool", 0)
    assert webhooks.reap_webhook_outbox(db_path=db_path)["submitted"] == 1
    assert json.loads(posted[0]["data"])["id"] == event_id
    assert _rows(db_path, "SELECT state FROM webhook_outbox")[0]["state"] == "delivered"


def test_a_redirect_is_never_followed(db_path, monkeypatch):
    import net_safety
    rid = _rid(db_path)
    _hook(db_path, rid)
    calls = []
    monkeypatch.setattr(net_safety, "safe_post", lambda url, **kw: calls.append(url) or
                        types.SimpleNamespace(status_code=302, ok=False))
    monkeypatch.setattr(webhooks.time, "sleep", lambda s: None)
    wh = webhooks.get_webhook(rid, db_path=db_path)
    res = webhooks._deliver(wh, "review.received", {}, db_path=db_path)
    assert res["ok"] is False and "redirect" in res["error"] and len(calls) == 1


def test_the_webhook_test_is_owner_only_and_queued(db_path, monkeypatch):
    from flask import Flask
    rid = _rid(db_path)
    _hook(db_path, rid)
    queued = []
    monkeypatch.setattr(webhooks, "queue_test_delivery", lambda r, db_path=None: queued.append(r) or "evt_1")
    app = Flask(__name__)
    app.register_blueprint(client_api.client_bp)
    monkeypatch.setattr(auth, "get_current_user", lambda: {"id": 2, "restaurant_id": rid, "is_admin": 0,
                                                           "role": "manager", "username": "mgr"})
    c = app.test_client()
    c.set_cookie("csrf_token", "t")
    r = c.post("/api/webhook/test", headers={"X-CSRF-Token": "t"})
    assert r.status_code == 403 and queued == []
    monkeypatch.setattr(auth, "get_current_user", lambda: {"id": 1, "restaurant_id": rid, "is_admin": 0,
                                                           "role": "owner", "username": "own"})
    r = c.post("/api/webhook/test", headers={"X-CSRF-Token": "t"})
    body = r.get_json()
    assert body["ok"] is True and body["queued"] is True and queued == [rid]


# ── #156: net_safety ────────────────────────────────────────────────────────

@pytest.mark.parametrize("url", [
    "http://127.0.0.1/x", "http://10.1.2.3/x", "http://169.254.169.254/latest/meta-data/",
    "http://100.64.0.1/x", "http://[::1]/x", "http://[::ffff:10.0.0.1]/x", "http://localhost/x",
    "http://service.railway.internal/x", "ftp://8.8.8.8/x",
])
def test_net_safety_refuses_internal_addresses(url):
    import net_safety
    with pytest.raises(net_safety.UnsafeURL):
        net_safety.vet(url)


def test_net_safety_connects_to_the_address_it_vetted(monkeypatch):
    """The DNS answer is taken once; a second, different answer (a rebinding
    attack) never reaches the connection."""
    import net_safety, socket, requests
    answers = iter(["93.184.216.34", "10.0.0.7"])
    monkeypatch.setattr(socket, "getaddrinfo",
                        lambda host, port, **kw: [(2, 1, 6, "", (next(answers), port))])
    seen = {}

    def fake_request(self, method, url, **kw):
        seen.update(url=url, host=kw["headers"]["Host"], redirects=kw["allow_redirects"])
        return types.SimpleNamespace(status_code=200, ok=True)
    monkeypatch.setattr(requests.Session, "request", fake_request)
    net_safety.safe_get("https://menu.example.test/page", timeout=5)
    assert seen == {"url": "https://93.184.216.34/page", "host": "menu.example.test", "redirects": False}


def test_net_safety_requires_a_timeout():
    import net_safety
    with pytest.raises(ValueError):
        net_safety.safe_get("https://93.184.216.34/")


# ── #75: pushes go through a durable outbox ─────────────────────────────────

def _device(db_path, rid, uid=None):
    uid = uid or auth.create_user(rid, f"u{rid}", f"u{rid}@x.test", "pw-12345678", db_path=db_path)
    import push
    push.register_device_token(uid, rid, f"tok-{uid}", db_path=db_path)
    return uid


def test_a_push_is_written_before_it_is_queued_and_marked_from_apples_answer(db_path, monkeypatch):
    import push
    rid = _rid(db_path)
    _device(db_path, rid)
    monkeypatch.setattr(push, "_push_executor", lambda: _Inline())
    monkeypatch.setattr(push, "_deliver", lambda row, *a, **k: {"ok": False, "error": "BadTopic"})
    failed = []
    assert push.fire_push(rid, "1star", "t", "b", db_path=db_path, on_failed=lambda: failed.append(1)) == 1
    row = _rows(db_path, "SELECT state, last_error FROM push_outbox")[0]
    assert row == {"state": "failed", "last_error": "BadTopic"} and failed == [1]


def test_the_push_reaper_redelivers_what_a_restart_left(db_path, monkeypatch):
    import push

    class _Dead:
        def submit(self, fn, *a, **k):
            pass
    rid = _rid(db_path)
    _device(db_path, rid)
    monkeypatch.setattr(push, "_push_executor", lambda: _Dead())
    monkeypatch.setattr(push, "_queued", 0)
    push.fire_push(rid, "1star", "t", "b", db_path=db_path)
    conn = get_conn(db_path)
    conn.execute("UPDATE push_outbox SET updated_at=datetime('now','-30 minutes')")
    conn.commit()
    conn.close()
    got = []
    monkeypatch.setattr(push, "_push_executor", lambda: _Inline())
    monkeypatch.setattr(push, "_queued", 0)
    monkeypatch.setattr(push, "_deliver", lambda row, alert_type, *a, **k: got.append(alert_type) or {"ok": True})
    assert push.reap_push_outbox(db_path=db_path)["submitted"] == 1
    assert got == ["1star"] and _rows(db_path, "SELECT state FROM push_outbox")[0]["state"] == "sent"
