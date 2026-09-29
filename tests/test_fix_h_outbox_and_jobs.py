"""Fix round H — the owed-sends outbox and the billing jobs.

#12: a billing email is owed before it is sent, marked from the real
delivery result, retried with backoff and given up on loudly — and the
post-signing welcome carries a set-password link, never a password.
#25: run_dunning is the safety net for a dunning email the webhook could
not owe. #26: run_contract_chase re-sends the pay link on days 2, 5 and 9
after signing; nothing expires. #115/#15: reconcile_stripe refreshes the
mirror and records every local-vs-Stripe mismatch, bounded and resumable.

Senders are stubbed at the emails module; Stripe is a stand-in; nothing
leaves the process.
"""
import json
import sys
import types
from datetime import datetime, timedelta, timezone

import pytest

import auth
import billing_jobs
import emails
import models
import ops
import webhook_routes
from auth import create_user, init_auth
from models import Restaurant, create_restaurant, get_restaurant, update_restaurant


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
    for mod in (models, auth, webhook_routes, ops):
        monkeypatch.setattr(mod, "get_conn", redirect, raising=False)
        monkeypatch.setattr(mod, "DB_PATH", db_path, raising=False)
    init_auth(db_path=db_path)
    monkeypatch.setattr(billing_jobs, "_sending_allowed", lambda: True)


def _rid(db_path, name="Owed Co", owner_email="o@x.test", **kw):
    rid = create_restaurant(Restaurant(name=name, owner_email=owner_email), db_path=db_path)
    if kw:
        update_restaurant(rid, kw, db_path=db_path)
    return rid


def _rows(db_path, sql, *args):
    conn = models.get_conn(db_path)
    try:
        return [dict(r) for r in conn.execute(sql, args)]
    finally:
        conn.close()


def _owed(db_path, oid):
    return _rows(db_path, "SELECT * FROM owed_sends WHERE id=?", oid)[0]


@pytest.fixture
def mail(monkeypatch):
    """Record every billing send; `mail.next` is what the next one returns."""
    box = types.SimpleNamespace(sent=[], next=None)

    def rec(kind):
        def _f(**k):
            box.sent.append((kind, k))
            res = box.next if box.next is not None else emails.SendResult(True, message_id="msg_1", status_code=200)
            return res
        return _f
    for name, kind in (("send_payment_email", "payment"), ("send_signed_welcome_email", "welcome"),
                       ("send_payment_receipt_email", "receipt"), ("send_dunning_email", "dunning"),
                       ("send_pay_reminder_email", "reminder"), ("send_card_update_email", "card")):
        monkeypatch.setattr(emails, name, rec(kind))
    return box


# ── the drain ────────────────────────────────────────────────────────────────

def test_a_delivered_send_is_marked_sent_with_its_message_id(db_path, mail):
    rid = _rid(db_path, module_reviews=1)
    oid, created = billing_jobs.enqueue("payment_link", rid, "payment_link:t1", to_email="o@x.test")
    assert created and billing_jobs.enqueue("payment_link", rid, "payment_link:t1")[1] is False
    out = billing_jobs.drain_owed_sends()
    row = _owed(db_path, oid)
    assert out["ok"] == 1 and row["status"] == "sent" and row["message_id"] == "msg_1" and row["sent_at"]
    assert mail.sent[0][1]["restaurant_id"] == rid


def test_a_transient_failure_is_retried_with_backoff_then_given_up_loudly(db_path, mail):
    rid = _rid(db_path, module_reviews=1)
    oid, _ = billing_jobs.enqueue("payment_link", rid, "payment_link:t2", to_email="o@x.test")
    mail.next = emails.SendResult(False, error="Resend 503", status_code=503, attempts=3)
    billing_jobs.drain_owed_sends()
    row = _owed(db_path, oid)
    assert row["status"] == "pending" and row["attempts"] == 1 and row["last_status_code"] == 503
    assert row["next_attempt_at"] > billing_jobs._stamp()          # backed off, not hot-looped
    for _ in range(billing_jobs.MAX_ATTEMPTS):
        conn = models.get_conn(db_path)
        conn.execute("UPDATE owed_sends SET next_attempt_at=datetime('now','-1 minute') WHERE id=?", (oid,))
        conn.commit(); conn.close()
        billing_jobs.drain_owed_sends()
    row = _owed(db_path, oid)
    assert row["status"] == "failed" and row["attempts"] == billing_jobs.MAX_ATTEMPTS
    assert _rows(db_path, "SELECT * FROM job_failures WHERE job='owed_send:payment_link'")


def test_a_suppressed_address_fails_at_once_and_is_recorded(db_path, mail):
    rid = _rid(db_path, module_reviews=1)
    oid, _ = billing_jobs.enqueue("payment_link", rid, "payment_link:t3", to_email="o@x.test")
    mail.next = emails.SendResult(False, error="recipient suppressed (bounced/complained)", attempts=0)
    billing_jobs.drain_owed_sends()
    assert _owed(db_path, oid)["status"] == "failed"
    ctx = _rows(db_path, "SELECT context FROM job_failures WHERE job='owed_send:payment_link'")[0]["context"]
    assert f"restaurant_id={rid}" in ctx


@pytest.mark.parametrize("returned,state", [
    (None, "sent_unverified"),      # a sender that predates E's SendResult contract
    (True, "sent"),
    (False, "pending"),
])
def test_old_return_shapes_are_read_defensively(db_path, mail, returned, state):
    rid = _rid(db_path, module_reviews=1)
    oid, _ = billing_jobs.enqueue("payment_link", rid, f"payment_link:shape:{state}", to_email="o@x.test")
    mail.next = returned
    if returned is None:
        # The stub returns mail.next unless it is None; a None sender:
        emails_send = emails.send_payment_email
        emails.send_payment_email = lambda **k: None
        try:
            billing_jobs.drain_owed_sends()
        finally:
            emails.send_payment_email = emails_send
    else:
        billing_jobs.drain_owed_sends()
    assert _owed(db_path, oid)["status"] == state


def test_nothing_is_sent_where_the_scheduler_may_not_run(db_path, mail, monkeypatch):
    monkeypatch.setattr(billing_jobs, "_sending_allowed", lambda: False)
    rid = _rid(db_path, module_reviews=1)
    oid, _ = billing_jobs.enqueue("payment_link", rid, "payment_link:local", to_email="o@x.test")
    out = billing_jobs.drain_owed_sends()
    assert mail.sent == [] and out["results"][0]["state"] == "held"
    assert _owed(db_path, oid)["status"] == "pending"


def test_a_row_in_flight_is_not_sent_twice(db_path, mail):
    rid = _rid(db_path, module_reviews=1)
    oid, _ = billing_jobs.enqueue("payment_link", rid, "payment_link:race", to_email="o@x.test")
    conn = models.get_conn(db_path)
    assert billing_jobs._claim_row(conn, oid)          # another drain holds it
    conn.close()
    billing_jobs.drain_owed_sends()
    assert mail.sent == []


def test_a_payment_link_owed_to_a_client_who_has_since_paid_is_skipped(db_path, mail):
    rid = _rid(db_path, module_reviews=1, billing_status="active")
    oid, _ = billing_jobs.enqueue("payment_link", rid, "payment_link:paid", to_email="o@x.test")
    billing_jobs.drain_owed_sends()
    assert mail.sent == [] and _owed(db_path, oid)["status"] == "skipped"


# ── the welcome: a set-password link, and no password is touched ────────────

def _password_hash(db_path, uid):
    return _rows(db_path, "SELECT password_hash FROM users WHERE id=?", uid)[0]["password_hash"]


def test_the_welcome_sends_a_set_password_link_and_never_changes_the_password(db_path, mail):
    rid = _rid(db_path, module_reviews=1, google_place_id="ChIJ-1", owner_name="Erik J")
    uid = create_user(rid, "erik", "erik@x.test", "admin-typed-1", db_path=db_path)
    before = _password_hash(db_path, uid)
    oid, _ = billing_jobs.enqueue("welcome", rid, f"welcome:{uid}", to_email="erik@x.test",
                                  payload={"user_id": uid})
    billing_jobs.drain_owed_sends()
    kind, k = mail.sent[0]
    assert kind == "welcome" and "password" not in k
    token = k["set_password_url"].rsplit("/reset-password/", 1)[1]
    assert models.validate_reset_token(token, db_path=db_path)["id"] == uid
    assert k["google_place_id"] == "ChIJ-1" and k["owner_name"] == "Erik J" and k["restaurant_id"] == rid
    assert _password_hash(db_path, uid) == before
    assert _owed(db_path, oid)["status"] == "sent"
    # Nothing secret sits in the outbox.
    assert token not in (_owed(db_path, oid)["payload_json"] or "")


def test_a_failed_welcome_leaves_the_password_alone_and_retries(db_path, mail):
    rid = _rid(db_path, module_reviews=1)
    uid = create_user(rid, "erik", "erik@x.test", "admin-typed-1", db_path=db_path)
    before = _password_hash(db_path, uid)
    oid, _ = billing_jobs.enqueue("welcome", rid, f"welcome:{uid}", payload={"user_id": uid})
    mail.next = emails.SendResult(False, error="timeout", attempts=3)
    billing_jobs.drain_owed_sends()
    assert _password_hash(db_path, uid) == before
    assert _owed(db_path, oid)["status"] == "pending"


def test_no_welcome_for_an_owner_who_has_already_signed_in(db_path, mail):
    rid = _rid(db_path, module_reviews=1)
    uid = create_user(rid, "erik", "erik@x.test", "admin-typed-1", db_path=db_path)
    auth.update_last_login(uid, db_path=db_path)
    oid, _ = billing_jobs.enqueue("welcome", rid, f"welcome:{uid}", payload={"user_id": uid})
    billing_jobs.drain_owed_sends()
    assert mail.sent == [] and _owed(db_path, oid)["status"] == "skipped"


# ── #25 run_dunning ─────────────────────────────────────────────────────────

def test_run_dunning_owes_what_the_webhook_missed_and_stands_down_for_paid_invoices(db_path, mail):
    rid = _rid(db_path, owner_email="own@x.test", billing_status="past_due")
    billing_jobs.record_invoice({"id": "in_open", "status": "open", "attempt_count": 2, "amount_due": 34900,
                                 "amount_remaining": 34900}, restaurant_id=rid)
    paid = _rid(db_path, name="Paid Up", owner_email="p@x.test", billing_status="active")
    billing_jobs.record_invoice({"id": "in_paid", "status": "paid", "attempt_count": 1}, restaurant_id=paid)
    billing_jobs.enqueue("dunning", paid, "dunning:in_paid:1:p@x.test", to_email="p@x.test",
                         payload={"invoice_id": "in_paid", "attempt": 1}, due_at="2099-01-01 00:00:00")
    out = billing_jobs.run_dunning()
    assert out["enqueued"] == 1 and [k for k, _ in mail.sent] == ["dunning"]
    sent = mail.sent[0][1]
    assert sent["attempt"] == 2 and "/pay/" in sent["pay_url"] and sent["pay_url"].endswith("/invoice")
    assert sent["card_url"].endswith("/card") and sent["restaurant_id"] == rid
    assert _rows(db_path, "SELECT status FROM owed_sends WHERE dedupe_key='dunning:in_paid:1:p@x.test'")[0]["status"] \
        == "cancelled"


def test_a_dunning_email_for_an_invoice_paid_meanwhile_is_skipped(db_path, mail):
    rid = _rid(db_path, billing_status="past_due")
    billing_jobs.record_invoice({"id": "in_x", "status": "paid"}, restaurant_id=rid)
    oid, _ = billing_jobs.enqueue("dunning", rid, "dunning:in_x:1:o@x.test", to_email="o@x.test",
                                  payload={"invoice_id": "in_x", "attempt": 1})
    billing_jobs.drain_owed_sends()
    assert mail.sent == [] and _owed(db_path, oid)["status"] == "skipped"


# ── #26 the contract chase ──────────────────────────────────────────────────

def _signed(db_path, days_ago, **kw):
    at = (datetime.now(timezone.utc) - timedelta(days=days_ago, hours=1)).strftime("%Y-%m-%d %H:%M:%S")
    kw.setdefault("module_reviews", 1)
    return _rid(db_path, contract_status="signed", contract_signed_at=at, **kw)


def test_the_pay_link_is_re_sent_on_days_2_5_and_9_only(db_path, mail):
    d1 = _signed(db_path, 1, name="Day 1")
    d2 = _signed(db_path, 2, name="Day 2")
    d6 = _signed(db_path, 6, name="Day 6")
    d9 = _signed(db_path, 9, name="Day 9")
    old = _signed(db_path, 40, name="Signed long ago")
    out = billing_jobs.run_contract_chase()
    keys = {r["dedupe_key"] for r in _rows(db_path, "SELECT dedupe_key FROM owed_sends WHERE kind='pay_reminder'")}
    assert keys == {f"pay_reminder:{d2}:2", f"pay_reminder:{d6}:5", f"pay_reminder:{d9}:9"}
    assert d1 and old and out["enqueued"] == 3
    assert {k["restaurant_id"] for _, k in mail.sent} == {d2, d6, d9}
    # A second pass the same day owes nothing new.
    assert billing_jobs.run_contract_chase()["enqueued"] == 0


def test_no_chase_for_a_client_who_paid_or_is_covered(db_path, mail):
    _signed(db_path, 2, name="Paid", billing_status="active", converted_at="2026-09-01 00:00:00")
    a = _signed(db_path, 2, name="Covered", owner_email="g@x.test", location_group="G")
    payer = _rid(db_path, name="Payer", owner_email="g@x.test", location_group="G", billing_status="active")
    billing_jobs.upsert_subscription(payer, subscription_id="sub_g", facts={"status": "active"})
    billing_jobs.run_contract_chase()
    assert mail.sent == []
    assert billing_jobs.billed_by(a) == payer


def test_the_pipeline_marks_day_10_for_the_console_and_nothing_expires(db_path):
    rid = _signed(db_path, 12, name="Unpaid")
    (p,) = [x for x in billing_jobs.contract_pipeline() if x["restaurant_id"] == rid]
    assert p["overdue"] is True and p["days_since_signed"] >= 12
    assert get_restaurant(rid, db_path).billing_status == "trial"          # decision 6: no auto-expiry


# ── #115 reconcile ──────────────────────────────────────────────────────────

class _Obj(dict):
    __getattr__ = dict.get


def _fake_stripe(subs_by_customer, deleted=()):
    def sub_list(customer=None, status=None, limit=None):
        return _Obj(data=list(subs_by_customer.get(customer, [])))

    def cust_retrieve(cid, **k):
        if cid not in subs_by_customer and cid not in deleted:
            raise RuntimeError(f"No such customer: '{cid}'")
        return _Obj(id=cid, deleted=cid in deleted)
    return types.SimpleNamespace(Subscription=types.SimpleNamespace(list=sub_list),
                                 Customer=types.SimpleNamespace(retrieve=cust_retrieve))


def _stripe_sub(sid, rid, status, keys="reviews", amount=34900, interval="month", **kw):
    return _Obj(id=sid, status=status, customer="cus", metadata={"restaurant_id": str(rid), "module_keys": keys},
                items=_Obj(data=[_Obj(id="si", quantity=1, price=_Obj(id="p", unit_amount=amount, currency="usd",
                                                                      recurring={"interval": interval}))]), **kw)


def test_reconcile_records_a_local_status_stripe_disagrees_with(db_path):
    rid = _rid(db_path, billing_status="trial", stripe_customer_id="cus_a", module_reviews=1,
               module_labor=0, module_inventory=0, module_marketing=0)
    fake = _fake_stripe({"cus_a": [_stripe_sub("sub_a", rid, "active")]})
    res = billing_jobs.reconcile_one(rid, fake)
    assert [m["kind"] for m in res["mismatches"]] == ["status"]
    m = billing_jobs.mirror_row(rid)
    assert m["subscription_id"] == "sub_a" and m["status"] == "active" and m["amount_cents"] == 34900
    (f,) = billing_jobs.reconcile_findings()
    assert f["restaurant_id"] == rid and f["first_seen_at"]


def test_reconcile_finds_modules_customer_and_missing_subscription_mismatches(db_path):
    mods = _rid(db_path, name="Mods", billing_status="active", stripe_customer_id="cus_m", module_reviews=1,
                module_labor=1, module_inventory=0, module_marketing=0)
    gone = _rid(db_path, name="Gone", billing_status="active", stripe_customer_id="cus_gone")
    nosub = _rid(db_path, name="NoSub", billing_status="active", stripe_customer_id="cus_empty")
    manual = _rid(db_path, name="Manual", billing_status="active")
    fake = _fake_stripe({"cus_m": [_stripe_sub("sub_m", mods, "active", keys="reviews")], "cus_empty": []})
    kinds = {rid: [m["kind"] for m in billing_jobs.reconcile_one(rid, fake)["mismatches"]]
             for rid in (mods, gone, nosub, manual)}
    assert kinds[mods] == ["modules"]
    assert "customer_missing" in kinds[gone]
    assert kinds[nosub] == ["no_subscription"]
    assert kinds[manual] == ["no_stripe_customer"]


def test_reconcile_leaves_a_held_account_alone_and_never_writes_billing_status(db_path):
    rid = _rid(db_path, billing_status="paused", pause_reason="dispute", stripe_customer_id="cus_h",
               module_reviews=1, module_labor=0, module_inventory=0, module_marketing=0)
    fake = _fake_stripe({"cus_h": [_stripe_sub("sub_h", rid, "active")]})
    # Stripe says active; a dispute hold locally is the admin's call, not a mismatch.
    assert billing_jobs.reconcile_one(rid, fake)["mismatches"] == []
    assert get_restaurant(rid, db_path).billing_status == "paused"


def test_reconcile_stripe_is_a_bounded_resumable_sweep(db_path, monkeypatch):
    import config
    a = _rid(db_path, name="A", billing_status="active", stripe_customer_id="cus_a")
    b = _rid(db_path, name="B", billing_status="active", stripe_customer_id="cus_b")
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_fixture")
    fake = _fake_stripe({"cus_a": [_stripe_sub("sub_a", a, "active")], "cus_b": [_stripe_sub("sub_b", b, "active")]})
    monkeypatch.setattr(config, "stripe_api", lambda key=None: fake)
    out = billing_jobs.reconcile_stripe()
    assert out["attempted"] == 2 and out["ok"] == 2 and out["failed"] == 0 and out["hit_bound"] is False
    assert ops.run_outcome(out)[0] == ops.RUN_OK
    cursor = _rows(db_path, "SELECT value FROM job_cursors WHERE key=?", billing_jobs.RECONCILE_CURSOR_KEY)
    assert cursor and int(cursor[0]["value"]) == max(a, b)


def test_billing_coverage_counts_each_subscription_once(db_path):
    a = _rid(db_path, name="A", owner_email="g@x.test", location_group="G")
    b = _rid(db_path, name="B", owner_email="g@x.test", location_group="G")
    solo = _rid(db_path, name="Solo")
    billing_jobs.upsert_subscription(a, subscription_id="sub_g", facts={"status": "active"})
    cov = billing_jobs.billing_coverage()
    assert cov[a] == a and cov[b] == a and cov[solo] is None
