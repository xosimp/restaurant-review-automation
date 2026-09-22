"""Server guards behind the web client's worst edge cases.

A client can be made careful (a busy state, a confirm, a "check history"
message), but the CLIENT audit found four places where the only real fix is
on the server, because a retry, a double click or a forged header reaches
it anyway:

- CLIENT-1 (P0): `guest_marketing.send_campaign` texts every guest in a
  synchronous loop and stamps the three-day frequency cap only after the
  loop. A second send that starts while the first is still texting — the
  web re-enables its button after a timeout, iOS gives up at 20s — texts
  every guest again. The probe turned 40 guests into 80 texts.
- CLIENT-16 / CLIENT-6: `_do_approve` is not idempotent. A double click (or
  a replayed approve) re-fires the `response.approved` webhook and posts
  the reply to Google a second time.
- CLIENT-14: a failed schedule job stores its Python traceback in the
  payload the owner's browser polls, and the dashboard shows it.
- CLIENT-40: the session IP is the first X-Forwarded-For value, stored
  verbatim; the Sessions list then renders it into innerHTML.

Twilio, Google and webhooks are stubbed; nothing leaves the process.
"""
import ipaddress
import threading

import pytest
from flask import Flask

import auth_routes
import client_api
import guest_marketing as gm
import models
from models import Restaurant, Review, create_restaurant, save_reviews


@pytest.fixture
def _redirect(db_path, monkeypatch):
    real = models.get_conn
    redirect = lambda *a, **k: real(db_path)
    for mod in (models, client_api, gm):
        monkeypatch.setattr(mod, "get_conn", redirect, raising=False)
    monkeypatch.setattr(models, "DB_PATH", db_path)


# ── CLIENT-1: an overlapping guest blast ────────────────────────────────────

def _guest_list(db_path, n):
    gm.init_guest_marketing(db_path=db_path)
    rid = create_restaurant(Restaurant(name="Maple & Rye", owner_email="o@x.test"), db_path=db_path)
    for i in range(n):
        gm.add_guest_contact_public_optin(rid, "+1555010%04d" % i, name="G%d" % i, db_path=db_path)
    return rid


def test_a_second_campaign_after_the_first_finished_skips_everyone(db_path, _redirect, monkeypatch):
    """The frequency cap already works once the first send has committed."""
    rid = _guest_list(db_path, 5)
    sent = []
    monkeypatch.setattr(gm, "send_sms", lambda phone, msg, *a, **k: sent.append(phone) or True)
    monkeypatch.setattr(gm, "guest_sms_allowed_now", lambda rid: True)
    first = gm.send_campaign(rid, "Wine dinner Friday", db_path=db_path)
    second = gm.send_campaign(rid, "Wine dinner Friday", db_path=db_path)
    assert first["sent"] == 5
    assert second["sent"] == 0 and second["skipped_recent"] == 5
    assert len(sent) == 5


def test_an_overlapping_second_campaign_texts_each_guest_once(db_path, _redirect, monkeypatch):
    rid = _guest_list(db_path, 6)
    sent, lock = [], threading.Lock()
    first_is_texting, retry_done = threading.Event(), threading.Event()
    first_thread = {}

    def slow_first_send(phone, msg, *a, **k):
        # The first request is mid-loop (Twilio is slow) when the owner,
        # whose phone gave up waiting, presses Send again.
        if threading.current_thread() is first_thread.get("t") and not first_is_texting.is_set():
            first_is_texting.set()
            retry_done.wait(5)
        with lock:
            sent.append(phone)
        return True

    monkeypatch.setattr(gm, "send_sms", slow_first_send)
    monkeypatch.setattr(gm, "guest_sms_allowed_now", lambda rid: True)
    t = threading.Thread(target=lambda: gm.send_campaign(rid, "Wine dinner Friday", db_path=db_path))
    first_thread["t"] = t
    t.start()
    assert first_is_texting.wait(5)
    try:
        gm.send_campaign(rid, "Wine dinner Friday", db_path=db_path)
    finally:
        retry_done.set()
        t.join(10)
    assert len(sent) == len(set(sent)) == 6, "%d texts to %d phones" % (len(sent), len(set(sent)))


# ── CLIENT-16 / CLIENT-6: approving twice ───────────────────────────────────

@pytest.fixture
def google_review(db_path, _redirect, monkeypatch):
    rid = create_restaurant(Restaurant(name="Maple & Rye", owner_email="o@x.test"), db_path=db_path)
    save_reviews([Review(restaurant_id=rid, platform="google", external_id="g-1", author="Ann",
                         rating=2, text="Cold food.")], db_path=db_path)
    conn = models.get_conn(db_path)
    review_id = conn.execute("SELECT id FROM reviews WHERE restaurant_id=?", (rid,)).fetchone()["id"]
    conn.execute("UPDATE reviews SET draft_response=?, review_name=? WHERE id=?",
                 ("Thank you, Ann — we're sorry.", "accounts/1/locations/2/reviews/g-1", review_id))
    conn.commit()
    conn.close()

    calls = {"posts": 0, "webhooks": [], "alerts": 0}
    import gmb
    import notify
    import webhooks
    monkeypatch.setattr(gmb, "is_connected", lambda rid: True)

    def post_reply(rid, name, text):
        calls["posts"] += 1
        return {"ok": True}
    monkeypatch.setattr(gmb, "post_reply", post_reply)
    monkeypatch.setattr(webhooks, "fire_webhook", lambda rid, event, payload: calls["webhooks"].append(event))
    monkeypatch.setattr(notify, "fire_response_approved_alert",
                        lambda *a, **k: calls.__setitem__("alerts", calls["alerts"] + 1))
    return rid, review_id, calls


def test_one_approve_posts_once_and_fires_once(google_review):
    rid, review_id, calls = google_review
    payload, status = client_api._do_approve(review_id, rid)
    assert status == 200 and payload["auto_posted"] is True
    assert calls["posts"] == 1
    assert calls["webhooks"].count("response.approved") == 1


@pytest.mark.xfail(strict=True, reason="CLIENT-16/CLIENT-6: _do_approve is not idempotent — a second approve re-posts to Google and re-fires the webhook and alert")
def test_a_second_approve_does_not_repost_or_refire(google_review):
    rid, review_id, calls = google_review
    client_api._do_approve(review_id, rid)
    client_api._do_approve(review_id, rid)
    assert calls["posts"] == 1, "posted to Google %d times" % calls["posts"]
    assert calls["webhooks"].count("response.approved") == 1
    assert calls["alerts"] == 1


# ── CLIENT-14: failed schedule jobs ─────────────────────────────────────────

@pytest.fixture
def failed_job_payload(monkeypatch):
    import schedule_engine
    stored = {}

    def explode(restaurant_id, week_start=None):
        raise RuntimeError("could not read /app/secret_config.py line 12")
    monkeypatch.setattr(schedule_engine, "_build_schedule_result", explode)
    monkeypatch.setattr(schedule_engine._ops, "finish_async_job",
                        lambda job_id, status, result: stored.update(status=status, result=result))
    schedule_engine._run_schedule_job("edge-job", 1)
    return stored


def test_a_failed_schedule_job_is_recorded_as_an_error(failed_job_payload):
    assert failed_job_payload["status"] == "error"
    assert failed_job_payload["result"]["ok"] is False
    assert failed_job_payload["result"]["error"]


@pytest.mark.xfail(strict=True, reason="CLIENT-14: the failed-job payload the browser polls carries the full Python traceback")
def test_a_failed_schedule_job_payload_has_no_traceback(failed_job_payload):
    result = failed_job_payload["result"]
    assert "traceback" not in result
    assert "Traceback (most recent call last)" not in str(result)


# ── CLIENT-40: the session IP ───────────────────────────────────────────────

def _client_ip(xff):
    app = Flask(__name__)
    with app.test_request_context("/", headers={"X-Forwarded-For": xff},
                                  environ_base={"REMOTE_ADDR": "10.0.0.9"}):
        return auth_routes._get_client_ip()


def test_the_proxied_client_ip_is_used():
    assert _client_ip("203.0.113.7, 10.0.0.1") == "203.0.113.7"


@pytest.mark.xfail(strict=True, reason="CLIENT-40: the first X-Forwarded-For value is trusted verbatim, so HTML planted there is stored on the session and rendered in the owner's Sessions list")
def test_a_forged_forwarded_for_value_is_never_stored_as_the_ip():
    ip = _client_ip('<img src=x onerror="alert(1)">, 10.0.0.1')
    ipaddress.ip_address(ip)          # raises on anything that is not an IP
