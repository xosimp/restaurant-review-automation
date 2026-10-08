"""Marketing fix round, slice B (9/28/26): guest EMAIL consent, sending and
reporting.

  MB-12 / CS-2 / CS-11 / #4 / #5  consent and opt-out belong to the ADDRESS,
        per restaurant: an unsubscribe (the /e/ link, the one-click POST, a
        complaint) writes guest_email_optouts and every row with the address
        honours it; the public form never clears it and never moves a
        consented newsletter to a new address without the box ticked; one
        address gets one copy.
  CS-3 / #12 / MB-16  sent and failed are reported apart, never the total as
        sent; failures a retry can reach are retried (idempotently); with no
        Resend key the send is refused before anything is recorded.
  CS-10  the drafted email is validated as guest copy: no link or phone
        number the owner didn't write.
  CS-15  the same email pressed again after it finished says "Already sent
        on M/D/YY - N new subscribers since" and can go to just those.
  CS-18  three at most inline, bounded in time; the tick sends the rest.
  CS-21  a photo a queued or sending newsletter carries is in use.
  CS-7   opens and clicks are "recorded", never "at least"; an unsubscribe
        click is not a click.
  CS-19  the preview greets a stand-in, never a real guest, and reads no list.

Resend is never reached: emails.deliver is a recorder here.
"""
import base64
import hashlib
import hmac
import json
import sys
import time
import types

import pytest
from flask import Flask

import auth
import auth_routes
import client_api
import emails
import guest_email as ge
import guest_marketing as gm
import marketing_media
import mobile_api
import models
import webhook_routes
from auth import create_user, init_auth
from models import Restaurant, create_restaurant, get_conn, get_restaurant, update_restaurant

SRC = open("templates/dashboard.html", encoding="utf-8").read()


@pytest.fixture
def db(db_path, monkeypatch):
    real = models.get_conn
    redirect = lambda *a, **k: real(db_path)
    for mod in list(sys.modules.values()):
        try:
            if getattr(mod, "get_conn", None) is real:
                monkeypatch.setattr(mod, "get_conn", redirect)
        except Exception:
            pass
    for mod in (models, ge, gm, marketing_media, auth, auth_routes, client_api, mobile_api, webhook_routes):
        monkeypatch.setattr(mod, "get_conn", redirect, raising=False)
    init_auth(db_path=db_path)
    gm.init_guest_marketing(db_path)
    return db_path


@pytest.fixture
def outbox(monkeypatch):
    """Every newsletter emails.deliver was asked to send."""
    box = []

    def rec(payload=None, restaurant_id=None, email_type=None, log_send=True):
        box.append(dict(payload or {}))
        return emails.SendResult(True, message_id=f"msg_{len(box)}", status_code=200, attempts=1)
    monkeypatch.setattr(emails, "deliver", rec)
    monkeypatch.setattr(emails, "_resend_key", lambda: "k")
    return box


def _rid(db, name="Fix B Co", address="9 Oak Ave, Aurora, IL 60505"):
    rid = create_restaurant(Restaurant(name=name, owner_email="o@x.test", module_marketing=1), db_path=db)
    if address:
        update_restaurant(rid, {"mailing_address": address}, db_path=db)
    return rid


def _contact(db, rid, phone, name="Ann Guest", visits=1):
    cid = gm.add_guest_contact_public_optin(rid, phone, name=name, db_path=db)
    c = get_conn(db)
    c.execute("UPDATE guest_contacts SET visit_count=? WHERE id=?", (visits, cid))
    c.commit()
    c.close()
    return cid


def _row(db, cid):
    c = get_conn(db)
    try:
        return dict(c.execute("SELECT * FROM guest_contacts WHERE id=?", (cid,)).fetchone())
    finally:
        c.close()


def _q(db, sql, args=()):
    c = get_conn(db)
    try:
        return [dict(r) for r in c.execute(sql, args).fetchall()]
    finally:
        c.close()


def _subs(db, rid, segment=None):
    return sorted(s["email"].lower() for s in ge.subscribers(rid, segment=segment, db_path=db))


@pytest.fixture
def web(db):
    app = Flask(__name__, template_folder="../templates")
    app.register_blueprint(client_api.client_bp)
    app.register_blueprint(mobile_api.mobile_bp)
    app.register_blueprint(webhook_routes.webhook_bp)
    auth_routes._login_attempts.clear()
    return app


# ── MB-12: the three probed scenarios ───────────────────────────────────────

def test_mb12_a_resubmitted_join_form_never_re_subscribes_an_unsubscribed_address(db, web):
    rid = _rid(db)
    cid = _contact(db, rid, "5550100001")
    ge.set_guest_email(cid, rid, "ann@x.test", consent=True, db_path=db)
    token = _row(db, cid)["email_token"]
    assert web.test_client().post(f"/e/{token}").status_code == 200
    assert _subs(db, rid) == []
    # The form again, box ticked: the guest (or anyone typing their address)
    # cannot undo the opt-out from a public page.
    ge.set_guest_email(cid, rid, "ann@x.test", consent=True, db_path=db)
    ge.set_guest_email(cid, rid, "ANN@x.test", consent=True, db_path=db)
    assert _subs(db, rid) == [] and _row(db, cid)["email_unsubscribed"] == 1
    # Nor from a NEW contact row carrying the same address.
    other = _contact(db, rid, "5550100002", name="Ann Again")
    ge.set_guest_email(other, rid, "ann@x.test", consent=True, db_path=db)
    assert _subs(db, rid) == [] and _row(db, other)["email_unsubscribed"] == 1


def test_mb12_an_unticked_box_never_moves_a_consented_newsletter_to_a_new_address(db, outbox):
    rid = _rid(db)
    cid = _contact(db, rid, "5550100003")
    ge.set_guest_email(cid, rid, "ann@x.test", consent=True, db_path=db)
    ge.set_guest_email(cid, rid, "stranger@x.test", consent=False, db_path=db)
    assert _row(db, cid)["email"] == "ann@x.test" and _subs(db, rid) == ["ann@x.test"]
    ge.send_newsletter(rid, "BODY:\nHello.", subject="News", db_path=db)
    assert [m["to"] for m in outbox] == [["ann@x.test"]]


def test_mb12_two_rows_with_one_address_get_one_copy_and_one_unsubscribe_covers_both(db, web, outbox):
    rid = _rid(db)
    a, b = _contact(db, rid, "5550100004", name="Ann"), _contact(db, rid, "5550100005", name="Ben")
    ge.set_guest_email(a, rid, "home@x.test", consent=True, db_path=db)
    ge.set_guest_email(b, rid, "Home@X.test", consent=True, db_path=db)
    c = get_conn(db)
    c.execute("UPDATE guest_contacts SET email='Home@X.test' WHERE id=?", (b,))   # a mixed-case import
    c.commit()
    c.close()
    assert _subs(db, rid) == ["home@x.test"] and ge.subscriber_count(rid, db_path=db) == 1
    assert ge.segment_counts(rid, db_path=db)["all"] == 1
    ge.send_newsletter(rid, "BODY:\nHello.", subject="News", db_path=db)
    assert len(outbox) == 1
    # Ben's row never mailed, so his own token is the one on file: either
    # row's link unsubscribes the address from this restaurant.
    assert web.test_client().post(f"/e/{_row(db, b)['email_token']}").status_code == 200
    assert _subs(db, rid) == []
    assert _row(db, a)["email_unsubscribed"] == 1 and _row(db, b)["email_unsubscribed"] == 1
    assert _q(db, "SELECT email, source FROM guest_email_optouts WHERE restaurant_id=?", (rid,)) == [
        {"email": "home@x.test", "source": "link"}]


# ── consent by address: the rest of item 1 ──────────────────────────────────

def test_a_ticked_new_address_consents_that_address_only_with_a_fresh_stamp_and_token(db, web, outbox):
    rid = _rid(db)
    cid = _contact(db, rid, "5550100006")
    ge.set_guest_email(cid, rid, "old@x.test", consent=True, db_path=db)
    c = get_conn(db)
    c.execute("UPDATE guest_contacts SET email_consent_at='2025-01-01T12:00:00' WHERE id=?", (cid,))
    c.commit()
    c.close()
    ge.send_newsletter(rid, "BODY:\nFirst.", subject="One", db_path=db)
    old_token = _row(db, cid)["email_token"]
    ge.set_guest_email(cid, rid, "new@x.test", consent=True, db_path=db)
    row = _row(db, cid)
    assert row["email"] == "new@x.test" and row["email_consent"] == 1
    assert row["email_consent_at"] != "2025-01-01T12:00:00" and row["email_token"] != old_token
    # The OLD email's link still works, for the old address only.
    assert web.test_client().get(f"/e/{old_token}").status_code == 200
    assert web.test_client().post(f"/e/{old_token}").status_code == 200
    assert _q(db, "SELECT email FROM guest_email_optouts WHERE restaurant_id=?", (rid,)) == [{"email": "old@x.test"}]
    assert _subs(db, rid) == ["new@x.test"]


def test_the_same_address_ticked_again_keeps_its_first_consent(db):
    rid = _rid(db)
    cid = _contact(db, rid, "5550100007")
    ge.set_guest_email(cid, rid, "ann@x.test", consent=False, db_path=db)
    assert _subs(db, rid) == []
    ge.set_guest_email(cid, rid, "ann@x.test", consent=True, db_path=db)
    first = _row(db, cid)["email_consent_at"]
    ge.set_guest_email(cid, rid, "ann@x.test", consent=True, db_path=db)
    assert _row(db, cid)["email_consent_at"] == first and _subs(db, rid) == ["ann@x.test"]


def test_the_one_click_post_unsubscribes_the_address_and_is_recorded_as_one_click(db, web):
    rid = _rid(db)
    cid = _contact(db, rid, "5550100008")
    ge.set_guest_email(cid, rid, "ann@x.test", consent=True, db_path=db)
    r = web.test_client().post(f"/e/{_row(db, cid)['email_token']}", data={"List-Unsubscribe": "One-Click"})
    assert r.status_code == 200
    assert _q(db, "SELECT source FROM guest_email_optouts") == [{"source": "one_click"}]


def test_an_opt_out_outlives_the_contact_row(db, web):
    rid = _rid(db)
    cid = _contact(db, rid, "5550100009")
    ge.set_guest_email(cid, rid, "ann@x.test", consent=True, db_path=db)
    web.test_client().post(f"/e/{_row(db, cid)['email_token']}")
    gm.delete_guest_contact(cid, rid, db_path=db)
    again = _contact(db, rid, "5550100009")
    ge.set_guest_email(again, rid, "ann@x.test", consent=True, db_path=db)
    assert _subs(db, rid) == []


def test_an_opt_out_is_per_restaurant(db, web):
    a, b = _rid(db, name="A Co"), _rid(db, name="B Co")
    ca, cb = _contact(db, a, "5550100010"), _contact(db, b, "5550100010")
    ge.set_guest_email(ca, a, "ann@x.test", consent=True, db_path=db)
    ge.set_guest_email(cb, b, "ann@x.test", consent=True, db_path=db)
    web.test_client().post(f"/e/{_row(db, ca)['email_token']}")
    assert _subs(db, a) == [] and _subs(db, b) == ["ann@x.test"]


def test_one_per_address_is_taken_after_the_audience_so_the_row_in_it_counts(db):
    rid = _rid(db)
    regular, first_timer = _contact(db, rid, "5550100011", visits=1), _contact(db, rid, "5550100012", visits=5)
    ge.set_guest_email(regular, rid, "home@x.test", consent=True, db_path=db)
    ge.set_guest_email(first_timer, rid, "home@x.test", consent=True, db_path=db)
    assert _subs(db, rid, "regulars") == ["home@x.test"]
    assert ge.segment_counts(rid, db_path=db)["regulars"] == 1


def test_an_unsubscribe_after_a_newsletter_was_recorded_skips_every_row_with_the_address(db, monkeypatch):
    rid = _rid(db)
    cid = _contact(db, rid, "5550100013")
    ge.set_guest_email(cid, rid, "ann@x.test", consent=True, db_path=db)
    for i in range(4):
        other = _contact(db, rid, f"555010002{i}")
        ge.set_guest_email(other, rid, f"g{i}@x.test", consent=True, db_path=db)
    monkeypatch.setattr(emails, "_resend_key", lambda: "k")
    sent = []
    monkeypatch.setattr(emails, "deliver", lambda payload=None, **k: sent.append(payload["to"][0])
                        or emails.SendResult(True, message_id=f"m{len(sent)}", status_code=200))
    monkeypatch.setattr(ge, "NEWSLETTER_INLINE_BATCH", 0)
    ge.send_newsletter(rid, "BODY:\nHello.", subject="News", db_path=db)
    ge.unsubscribe_address(rid, "ANN@x.test", db_path=db)
    ge.run_newsletter_sends(db_path=db)
    assert "ann@x.test" not in sent and len(sent) == 4


def test_the_boot_backfill_turns_old_row_unsubscribes_into_address_opt_outs(db):
    rid = _rid(db)
    a, b = _contact(db, rid, "5550100014"), _contact(db, rid, "5550100015")
    c = get_conn(db)
    c.execute("UPDATE guest_contacts SET email='home@x.test', email_consent=1, email_unsubscribed=1 WHERE id=?", (a,))
    c.execute("UPDATE guest_contacts SET email='home@x.test', email_consent=1, email_unsubscribed=0 WHERE id=?", (b,))
    c.commit()
    c.close()
    assert _subs(db, rid) == ["home@x.test"]     # a row-only unsubscribe, as it was stored before the fix
    gm.init_guest_marketing(db)                  # boot
    assert _q(db, "SELECT email, source FROM guest_email_optouts") == [{"email": "home@x.test", "source": "backfill"}]
    assert _subs(db, rid) == []


# ── the complaint webhook ──────────────────────────────────────────────────

_SECRET = "whsec_" + base64.b64encode(b"fixbsecret").decode()


def _svix(body: bytes):
    ts = int(time.time())
    key = base64.b64decode(_SECRET.split("_", 1)[1])
    sig = base64.b64encode(hmac.new(key, f"msg_1.{ts}.".encode() + body, hashlib.sha256).digest()).decode()
    return {"svix-id": "msg_1", "svix-timestamp": str(ts), "svix-signature": f"v1,{sig}",
            "Content-Type": "application/json"}


def _event(web, monkeypatch, payload):
    monkeypatch.setattr(webhook_routes, "RESEND_WEBHOOK_SECRET", _SECRET)
    body = json.dumps(payload).encode()
    return web.test_client().post("/webhooks/resend", data=body, headers=_svix(body))


def _logged(db, rid, message_id, to="ann@x.test"):
    c = get_conn(db)
    c.execute("INSERT INTO email_log (restaurant_id, email_type, to_email, subject, message_id) VALUES (?,?,?,?,?)",
              (rid, "guest_newsletter", to, "News", message_id))
    c.commit()
    c.close()


def test_a_complaint_opts_the_address_out_of_that_restaurant_and_keeps_the_guest_suppression(db, web, monkeypatch):
    rid = _rid(db)
    a, b = _contact(db, rid, "5550100016"), _contact(db, rid, "5550100017")
    ge.set_guest_email(a, rid, "ann@x.test", consent=True, db_path=db)
    ge.set_guest_email(b, rid, "ann@x.test", consent=True, db_path=db)
    _logged(db, rid, "m_c1")
    r = _event(web, monkeypatch, {"type": "email.complained", "data": {"email_id": "m_c1", "to": ["ann@x.test"]}})
    assert r.status_code == 200
    assert _q(db, "SELECT email, source FROM guest_email_optouts WHERE restaurant_id=?", (rid,)) == [
        {"email": "ann@x.test", "source": "complaint"}]
    assert _q(db, "SELECT email, scope FROM email_suppressions") == [{"email": "ann@x.test", "scope": "guest"}]
    assert _row(db, a)["email_unsubscribed"] == 1 and _row(db, b)["email_unsubscribed"] == 1


# ── CS-3: honest results, retries, no key ───────────────────────────────────

def _list(db, rid, n):
    for i in range(n):
        cid = _contact(db, rid, f"55502000{i:02d}", name=f"G{i} Last")
        ge.set_guest_email(cid, rid, f"g{i}@x.test", consent=True, db_path=db)


def test_with_no_resend_key_the_send_is_refused_before_anything_is_recorded(db, monkeypatch):
    rid = _rid(db)
    _list(db, rid, 2)
    called = []
    monkeypatch.setattr(emails, "deliver", lambda *a, **k: called.append(1))
    out = ge.send_newsletter(rid, "BODY:\nHello.", subject="News", db_path=db)
    assert out["ok"] is False and out["not_configured"] is True and "nothing was sent" in out["error"]
    assert called == [] and _q(db, "SELECT id FROM guest_newsletters") == []


def test_a_key_that_goes_missing_mid_send_leaves_the_rest_pending_not_failed(db, outbox, monkeypatch):
    rid = _rid(db)
    _list(db, rid, 6)
    out = ge.send_newsletter(rid, "BODY:\nHello.", subject="News", db_path=db)
    assert out["sent"] == 3 and out["queued"] == 3
    monkeypatch.setattr(emails, "_resend_key", lambda: "")
    ge.run_newsletter_sends(db_path=db)
    st = ge.newsletter_status(out["newsletter_id"], db_path=db)
    assert (st["sent"], st["failed"], st["pending"]) == (3, 0, 3)
    monkeypatch.setattr(emails, "_resend_key", lambda: "k")
    ge.run_newsletter_sends(db_path=db)
    assert ge.newsletter_status(out["newsletter_id"], db_path=db)["sent"] == 6


def _mixed_deliver(monkeypatch, outcomes):
    """deliver() answering by address: 'ok', a status code, or 'suppressed'."""
    sent = []

    def deliver(payload=None, restaurant_id=None, email_type=None, log_send=True):
        to = payload["to"][0]
        sent.append(to)
        how = outcomes.get(to, "ok")
        if how == "ok":
            return emails.SendResult(True, message_id=f"m_{to}_{len(sent)}", status_code=200)
        if how == "suppressed":
            return emails.SendResult(False, error="recipient suppressed (bounced/complained)", attempts=0)
        return emails.SendResult(False, error=json.dumps({"statusCode": how, "message": "x"}), status_code=how,
                                 attempts=3 if how >= 500 or how == 429 else 1)
    monkeypatch.setattr(emails, "deliver", deliver)
    monkeypatch.setattr(emails, "_resend_key", lambda: "k")
    return sent


def test_sent_failed_and_skipped_are_reported_apart_and_retry_reaches_only_what_it_can(db, monkeypatch):
    rid = _rid(db)
    _list(db, rid, 3)
    sent = _mixed_deliver(monkeypatch, {"g0@x.test": 503, "g1@x.test": 422, "g2@x.test": "suppressed"})
    out = ge.send_newsletter(rid, "BODY:\nHello.", subject="News", db_path=db)
    assert out["ok"] is True
    assert (out["sent"], out["failed"], out["retryable"], out["skipped"], out["queued"]) == (0, 2, 1, 1, 0)
    hist = ge.newsletter_history(rid, db_path=db)[0]
    assert (hist["sent"], hist["failed"], hist["retryable"], hist["skipped"]) == (0, 2, 1, 1)
    # Resend is back: the retry reaches the 503 only - never the rejected or
    # suppressed address - and a second press mails nobody.
    sent.clear()
    _mixed_deliver(monkeypatch, {}).clear()
    again = []
    monkeypatch.setattr(emails, "deliver", lambda payload=None, **k: again.append(payload["to"][0])
                        or emails.SendResult(True, message_id="m_retry", status_code=200))
    r = ge.retry_failed(rid, out["newsletter_id"], db_path=db)
    assert r["ok"] and r["retried"] == 1 and again == ["g0@x.test"]
    assert (r["sent"], r["failed"], r["retryable"]) == (1, 1, 0)
    r2 = ge.retry_failed(rid, out["newsletter_id"], db_path=db)
    assert r2["retried"] == 0 and again == ["g0@x.test"]


def test_retry_is_refused_for_another_restaurants_newsletter_and_without_a_key(db, outbox, monkeypatch):
    rid, other = _rid(db), _rid(db, name="Other Co")
    _list(db, rid, 1)
    out = ge.send_newsletter(rid, "BODY:\nHello.", subject="News", db_path=db)
    assert ge.retry_failed(other, out["newsletter_id"], db_path=db)["status"] == 404
    monkeypatch.setattr(emails, "_resend_key", lambda: "")
    assert ge.retry_failed(rid, out["newsletter_id"], db_path=db)["not_configured"] is True


def test_an_interrupted_send_is_failed_but_never_retried(db, outbox):
    rid = _rid(db)
    _list(db, rid, 1)
    out = ge.send_newsletter(rid, "BODY:\nHello.", subject="News", db_path=db)
    c = get_conn(db)
    c.execute("UPDATE guest_newsletter_recipients SET status='sending', claimed_at=datetime('now','-2 hours')")
    c.execute("UPDATE guest_newsletters SET completed_at=NULL")
    c.commit()
    c.close()
    ge.run_newsletter_sends(db_path=db)
    st = ge.newsletter_status(out["newsletter_id"], db_path=db)
    assert st["failed"] == 1 and st["retryable"] == 0       # it may have gone out: never mailed twice


def test_the_last_campaign_figure_is_what_was_sent_never_the_total(db, monkeypatch):
    rid = _rid(db)
    _list(db, rid, 2)
    _mixed_deliver(monkeypatch, {"g0@x.test": 503, "g1@x.test": 503})
    ge.send_newsletter(rid, "BODY:\nHello.", subject="News", db_path=db)
    lc = gm.campaign_overview(rid, db_path=db)["last_campaign"]
    assert lc["channel"] == "email" and lc["sent"] == 0


def test_the_retry_and_send_new_routes_and_their_web_twins(db, web, outbox):
    rid = _rid(db)
    _list(db, rid, 1)
    create_user(rid, "owner", "owner@x.test", "correct-horse", db_path=db)
    c = web.test_client()
    tok = c.post("/mobile/api/login", json={"username": "owner", "password": "correct-horse"}).get_json()["token"]
    h = {"Authorization": f"Bearer {tok}"}
    first = c.post("/mobile/api/guest-newsletter", json={"body": "Hello.", "subject": "News"}, headers=h)
    assert first.status_code == 200 and first.get_json()["sent"] == 1
    nid = first.get_json()["newsletter_id"]
    again = c.post("/mobile/api/guest-newsletter", json={"body": "Hello.", "subject": "News"}, headers=h)
    assert again.status_code == 409 and again.get_json()["already_sent"] is True
    r = c.post(f"/mobile/api/guest-newsletter/{nid}/retry", headers=h)
    assert r.status_code == 200 and r.get_json()["retried"] == 0
    assert c.post(f"/mobile/api/guest-newsletter/{nid + 99}/retry", headers=h).status_code == 404
    n = c.post(f"/mobile/api/guest-newsletter/{nid}/send-new", headers=h)
    assert n.status_code == 200 and n.get_json()["added"] == 0 and len(outbox) == 1
    src = open("client_api.py", encoding="utf-8").read()
    for path, body in (("/api/guest-newsletter/<int:newsletter_id>/retry", "mobile_guest_newsletter_retry"),
                       ("/api/guest-newsletter/<int:newsletter_id>/send-new", "mobile_guest_newsletter_send_new")):
        i = src.index(f'@client_bp.route("{path}"')
        assert f'_m("{body}")(current_user, newsletter_id)' in src[i:i + 400], path


def test_the_studio_reports_sent_failed_and_queued_by_name_never_the_total():
    send = SRC[SRC.index("window.sendGuestCampaign = function(btn) {"):SRC.index("// A changed draft or audience is a new send")]
    email = send[send.index("if (snap.email)"):send.index("if (snap.social)")]
    assert "cpMailResult(d)" in email and "d.total" not in email
    res = SRC[SRC.index("function cpMailResult(d, noActs) {"):SRC.index("function cpMailAct(")]
    assert "d.total" not in res and "+d.sent" in res and "d.failed" in res and "d.queued" in res
    assert "'data-cp-mail-retry'" in res and "'Retry ' + d.retryable + ' failed'" in res
    assert "'data-cp-mail-new'" in res and "d.already_sent" in res
    assert "'/retry'" in SRC and "'/send-new'" in SRC
    hist = SRC[SRC.index("function cpPaintHistory() {"):]
    hist = hist[:hist.index("\n}\n")]
    assert "bar(c.total || ef, 'Failed', ef, 'var(--red)', '0')" in hist and "data-cp-mail-retry" in hist


# ── CS-15: the same email again, after it finished ──────────────────────────

def test_the_same_email_again_says_when_it_went_and_can_go_to_just_the_new_subscribers(db, outbox):
    rid = _rid(db)
    _list(db, rid, 2)
    first = ge.send_newsletter(rid, "BODY:\nHello.", subject="News", db_path=db)
    assert first["sent"] == 2
    cid = _contact(db, rid, "5550300001", name="New Guest")
    ge.set_guest_email(cid, rid, "new@x.test", consent=True, db_path=db)
    again = ge.send_newsletter(rid, "BODY:\nHello.", subject="News", db_path=db)
    from time_utils import mdy, local_iso
    on = mdy(local_iso(_q(db, "SELECT created_at FROM guest_newsletters")[0]["created_at"],
                       getattr(get_restaurant(rid, db), "timezone", None)))
    assert again["ok"] is False and again["already_sent"] is True and again["new_subscribers"] == 1
    assert again["error"] == f"Already sent on {on} — 1 new subscriber since." and len(outbox) == 2
    out = ge.send_to_new_subscribers(rid, first["newsletter_id"], db_path=db)
    assert out["ok"] and out["added"] == 1 and [m["to"] for m in outbox[2:]] == [["new@x.test"]]
    assert ge.send_to_new_subscribers(rid, first["newsletter_id"], db_path=db)["added"] == 0
    assert len(outbox) == 3
    item = ge.newsletter_history(rid, db_path=db)
    assert len(item) == 1 and item[0]["sent"] == 3 and item[0]["total"] == 3
    none_new = ge.send_newsletter(rid, "BODY:\nHello.", subject="News", db_path=db)
    assert none_new["error"].endswith("no new subscribers since.")


# ── CS-18: small inline batch, bounded in time ──────────────────────────────

def test_the_request_sends_at_most_three_and_starts_none_past_its_time_bound(db, outbox, monkeypatch):
    rid = _rid(db)
    _list(db, rid, 8)
    assert ge.NEWSLETTER_INLINE_BATCH <= 3
    out = ge.send_newsletter(rid, "BODY:\nHello.", subject="News", db_path=db)
    assert out["sent"] == 3 and out["queued"] == 5 and len(outbox) == 3
    # A slow Resend: each send takes 3s of the clock; with a 5s bound the
    # request starts a second and stops.
    clock = [1000.0]
    monkeypatch.setattr(time, "monotonic", lambda: clock[0])
    slow = []

    def deliver(payload=None, **k):
        clock[0] += 3.0
        slow.append(payload["to"][0])
        return emails.SendResult(True, message_id=f"s{len(slow)}", status_code=200)
    monkeypatch.setattr(emails, "deliver", deliver)
    out2 = ge.send_newsletter(rid, "BODY:\nSomething else.", subject="Two", db_path=db)
    assert out2["sent"] == 2 and out2["queued"] == 6


# ── CS-21: a photo a queued newsletter carries ──────────────────────────────

def test_a_photo_a_newsletter_is_still_sending_cannot_be_deleted(db, outbox):
    rid = _rid(db)
    _list(db, rid, 5)
    c = get_conn(db)
    mid = c.execute("INSERT INTO marketing_media (restaurant_id, token, mime, data) VALUES (?,?,?,?)",
                    (rid, "tokfixb", "image/jpeg", b"x")).lastrowid
    c.commit()
    c.close()
    ge.send_newsletter(rid, "BODY:\nHello.", subject="News", design={"image_media_id": mid}, db_path=db)
    assert marketing_media.media_in_use(mid, rid, db_path=db) is True
    refused = marketing_media.remove_media(mid, rid, db_path=db)
    assert refused["status"] == 409 and "email still sending" in refused["error"]
    ge.run_newsletter_sends(db_path=db)
    assert marketing_media.media_in_use(mid, rid, db_path=db) is False
    # Sent is not done with it: guests' copies load the photo when opened,
    # so it is kept for good (re-audit 10/8/26).
    kept = marketing_media.remove_media(mid, rid, db_path=db)
    assert kept["status"] == 409 and kept["in_sent_email"] is True
    assert marketing_media.get_media_token(mid, rid, db_path=db) == "tokfixb"


# ── CS-7: recorded, not "at least"; unsubscribe clicks are not clicks ───────

def test_an_unsubscribe_click_is_not_counted_as_a_click(db, web, monkeypatch):
    import config
    rid = _rid(db)
    base = config.base_url().rstrip("/")
    _logged(db, rid, "m_k1")
    _logged(db, rid, "m_k2")
    _event(web, monkeypatch, {"type": "email.clicked",
                              "data": {"email_id": "m_k1", "click": {"link": f"{base}/e/abc123"}}})
    _event(web, monkeypatch, {"type": "email.clicked",
                              "data": {"email_id": "m_k2", "click": {"link": "https://ej.test/menu"}}})
    got = {r["message_id"]: r["clicked_at"] for r in _q(db, "SELECT message_id, clicked_at FROM email_log")}
    assert got["m_k1"] is None and got["m_k2"] is not None
    assert webhook_routes._is_unsubscribe_click({"click": {"link": f"{base}/u/tok"}})
    assert not webhook_routes._is_unsubscribe_click({"click": {"link": "https://elsewhere.test/e/tok"}})


def test_opens_are_worded_as_recorded_everywhere_an_owner_reads_them(db, outbox):
    rid = _rid(db)
    _list(db, rid, 3)
    for i in range(10):
        cid = _contact(db, rid, f"55504000{i:02d}")
        ge.set_guest_email(cid, rid, f"o{i}@x.test", consent=True, db_path=db)
    ge.send_newsletter(rid, "BODY:\nHello.", subject="News", db_path=db)
    ge.run_newsletter_sends(db_path=db)
    c = get_conn(db)
    c.execute("INSERT INTO email_log (restaurant_id, email_type, to_email, message_id, opened_at) "
              "VALUES (?,?,?,?,datetime('now'))", (rid, "guest_newsletter", "g0@x.test", "msg_1"))
    c.commit()
    c.close()
    ins = [i for i in gm.campaign_insights(rid, hist=[], db_path=db) if i["kind"] == "opened"]
    assert ins and "≥" not in ins[0]["figure"] and "at least" not in ins[0]["text"]
    assert ins[0]["text"] == "opens recorded on your last email" and "Apple Mail" in ins[0]["basis"]
    paint = SRC[SRC.index("window.cpPaint = function"):SRC.index("function cpPaintAi")]
    email = paint[paint.index("var eft = _cpEl('cp-email-ft');"):paint.index("// Social")]
    assert "at least" not in email and "opens recorded" in email and "Apple Mail auto-opens" in email


# ── CS-10: guest copy rules on the drafted email ────────────────────────────

def _msg(text):
    return types.SimpleNamespace(content=[types.SimpleNamespace(type="text", text=text)], stop_reason="end_turn")


GOOD = {"subject": "Pull up a chair", "preheader": "Your table is ready", "headline": "Come see us",
        "body": "The kitchen is on.\n\nCome sit with us this week.", "button": "Book a table"}


def _draft(monkeypatch, db, reply, goal="Fill Tuesday lunch"):
    rid = _rid(db)
    monkeypatch.setattr(ge, "get_client", lambda *a, **k: object())
    monkeypatch.setattr(ge, "create_with_retry", lambda client, **kw: _msg(json.dumps(reply)))
    return ge.draft_newsletter(get_restaurant(rid, db_path=db), goal=goal)


@pytest.mark.parametrize("body,kind", [
    ("Book now at https://evil.test/win.", "a link"),
    ("Call us at (630) 555-0142 to book.", "a phone number"),
    ("Write to deals@evil.test for a table.", "an email address"),
])
def test_a_drafted_link_phone_or_address_nobody_wrote_is_refused(db, monkeypatch, body, kind):
    with pytest.raises(ValueError, match=f"newsletter copy rejected: the copy contains {kind}"):
        _draft(monkeypatch, db, {**GOOD, "body": body})


def test_a_phone_number_the_owner_typed_may_appear(db, monkeypatch):
    out = _draft(monkeypatch, db, {**GOOD, "body": "Call (630) 555-0142 to book Tuesday."},
                 goal="Fill Tuesday lunch, call 630-555-0142")
    assert "555-0142" in out["body"]


def test_newsletter_fields_are_checked_on_the_guest_text_surface(db, monkeypatch):
    import response_validation as rv
    seen = []
    real = rv.enforce
    monkeypatch.setattr(rv, "enforce", lambda text, ctx, **kw: seen.append(ctx.surface) or real(text, ctx, **kw))
    _draft(monkeypatch, db, GOOD)
    assert seen and set(seen) == {"guest_sms"}


# ── CS-19: the preview ──────────────────────────────────────────────────────

def test_the_preview_greets_a_stand_in_and_never_reads_the_list(db, monkeypatch):
    rid = _rid(db)
    _list(db, rid, 2)

    def boom(*a, **k):
        raise AssertionError("the preview read the subscriber list")
    monkeypatch.setattr(ge, "subscribers", boom)
    monkeypatch.setattr(ge, "_subscriber_rows", boom)
    out = ge.preview(rid, subject="S", body="Hello.", base="https://dash.test", db_path=db)
    assert out["ok"] and "Hi Alex —" in out["html"] and "G0" not in out["html"]
