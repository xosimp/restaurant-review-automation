"""MOD audit, Reviews: a cancelled restaurant is inert in the background (MOD-REV-2).

Cancellation (`customer.subscription.deleted`) only sets
billing_status='churned'. What these tests protect: from that moment Cavnar
stops reading the former customer's reviews, stops publishing replies on
their live Google listing under their name, stops alerting them, and stops
spending Places, Claude and Perplexity on their weekly Intel — while an
active restaurant alongside it carries on exactly as before.

Every test that asserts the churned half is xfail(strict=True) against the
confirmed defect; the active half in the same test is the control that
stops a fix from simply switching the job off.
"""
import sqlite3
from datetime import timedelta

import pytest

import gmb
import models
import notify

ACTIVE, CHURNED = 1, 2
_FULL_TIER = dict(module_reviews=1, module_labor=1, module_inventory=1, module_marketing=1)


@pytest.fixture(autouse=True)
def _redirect(db_path, monkeypatch):
    real = models.get_conn
    fake = lambda *a, **k: real(db_path)  # noqa: E731
    monkeypatch.setattr(models, "get_conn", fake)
    monkeypatch.setattr(models, "DB_PATH", db_path)
    import webhooks
    monkeypatch.setattr(webhooks, "get_conn", fake, raising=False)
    monkeypatch.setattr(webhooks, "fire_webhook", lambda *a, **k: None)
    yield


def _restaurant(db_path, rid, billing_status, **kw):
    cols = {"id": rid, "name": f"R{rid}", "owner_email": f"o{rid}@x.test",
            "billing_status": billing_status, "google_place_id": f"ChIJ_{rid}", "reviews_live": 1}
    cols.update(kw)
    conn = sqlite3.connect(db_path)
    conn.execute(f"INSERT INTO restaurants ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                 tuple(cols.values()))
    conn.commit()
    conn.close()
    return rid


def _pair(db_path, **kw):
    _restaurant(db_path, ACTIVE, "active", **kw)
    _restaurant(db_path, CHURNED, "churned", **kw)


def _quiet_fetch(monkeypatch):
    """Record which restaurants run_daily_fetch reaches, with no AI and no
    network behind it."""
    import analyser
    import drafter
    import fetcher
    reached = []
    monkeypatch.setattr(fetcher, "fetch_google", lambda pid, rid: reached.append(rid) or [])
    monkeypatch.setattr(analyser, "analyse_review", lambda *a, **k: None)
    monkeypatch.setattr(drafter, "draft_response", lambda *a, **k: None)
    monkeypatch.setattr(notify, "deliver_alert", lambda *a, **k: None)
    return reached


# ── R1 #29: not fetched ─────────────────────────────────────────────────────

def test_a_churned_restaurant_is_not_fetched(db_path, monkeypatch):
    import scheduler
    _pair(db_path, module_reviews=1)
    reached = _quiet_fetch(monkeypatch)
    monkeypatch.setattr(scheduler, "auto_approve_five_stars", lambda *a, **k: 0)
    scheduler.run_daily_fetch()
    assert sorted(reached) == [ACTIVE]


# ── R3 #8: not auto-published ──────────────────────────────────────────────

def test_a_churned_restaurant_is_never_auto_published(db_path, monkeypatch):
    import scheduler
    _pair(db_path, module_reviews=1, auto_approve_5star=1, auto_approve_daily_cap=5,
          gmb_refresh_token=None)
    conn = sqlite3.connect(db_path)
    for rid in (ACTIVE, CHURNED):
        conn.execute(
            "INSERT INTO reviews (restaurant_id, platform, external_id, author, rating, text, review_date, "
            "fetched_at, processed, response_status, draft_response, review_name, urgency) VALUES "
            "(?, 'google', ?, 'G', 5, 'Great', datetime('now','-1 day'), datetime('now'), 1, 'drafted', "
            "'Thank you for the kind words!', ?, 'normal')",
            (rid, f"ext{rid}", f"accounts/1/locations/{rid}/reviews/ext{rid}"))
    conn.commit()
    conn.close()
    _quiet_fetch(monkeypatch)
    posted = []
    monkeypatch.setattr(gmb, "is_connected", lambda rid: True)
    monkeypatch.setattr(gmb, "post_reply", lambda rid, name, text: posted.append(rid) or {"ok": True})
    monkeypatch.setattr(notify, "fire_response_approved_alert", lambda *a, **k: None)
    scheduler.run_daily_fetch()
    assert posted == [ACTIVE], "a reply went out on a former customer's listing under their name"


# ── R4 #7: not alerted ─────────────────────────────────────────────────────

def _one_star(db_path, rid):
    from models import Review, save_reviews
    _n, new = save_reviews([Review(restaurant_id=rid, platform="google", external_id=f"neg{rid}",
                                   author="Pat", rating=1, text="Cold food and a long wait.")],
                           db_path=db_path)
    return new


def _record_delivery(monkeypatch):
    sent = []
    monkeypatch.setattr(notify, "deliver_alert",
                        lambda rid, kind, *a, **k: sent.append((rid, kind)))
    monkeypatch.setattr(notify, "rush_release_at", lambda *a, **k: None)
    return sent


def test_an_active_restaurant_is_alerted_about_a_new_one_star(db_path, monkeypatch):
    """Control: the alert path itself works in this fixture."""
    _restaurant(db_path, ACTIVE, "active", alert_1star=1, urgent_via_email=1)
    sent = _record_delivery(monkeypatch)
    notify.fire_review_alerts(ACTIVE, "R1", _one_star(db_path, ACTIVE), db_path=db_path)
    assert (ACTIVE, "1star") in sent


def test_a_churned_restaurant_is_not_alerted_about_a_new_one_star(db_path, monkeypatch):
    _restaurant(db_path, CHURNED, "churned", alert_1star=1, urgent_via_email=1)
    sent = _record_delivery(monkeypatch)
    notify.fire_review_alerts(CHURNED, "R2", _one_star(db_path, CHURNED), db_path=db_path)
    assert sent == []


# ── the weekly Intel loops ─────────────────────────────────────────────────

def test_the_weekly_competitor_analysis_skips_a_churned_restaurant(db_path, monkeypatch):
    import competitor
    import scheduler
    _pair(db_path, **_FULL_TIER)
    seen = []
    monkeypatch.setattr(competitor, "run_competitor_analysis", lambda rid: seen.append(rid) or {"ok": True})
    scheduler.run_weekly_competitor_analysis()
    assert seen == [ACTIVE]


def test_the_weekly_ai_visibility_run_skips_a_churned_restaurant(db_path, monkeypatch):
    import client_api
    import scheduler
    _pair(db_path, **_FULL_TIER)
    seen = []
    monkeypatch.setattr(client_api, "_do_ai_visibility_inner",
                        lambda rid, force=False: (seen.append(rid) or ({"ok": True}, 200)))
    scheduler.run_weekly_ai_visibility()
    assert seen == [ACTIVE]


# ── R6 #5: review-request follow-ups ───────────────────────────────────────

def test_review_request_follow_ups_skip_a_churned_restaurant(db_path, monkeypatch):
    import guest_marketing
    from time_utils import restaurant_now_by_id
    real = models.get_conn
    monkeypatch.setattr(guest_marketing, "get_conn", lambda *a, **k: real(db_path), raising=False)
    guest_marketing.init_guest_marketing(db_path=db_path)
    _pair(db_path, module_marketing=1)
    monkeypatch.setattr(guest_marketing, "guest_sms_allowed_now", lambda rid: True)
    texted = []
    monkeypatch.setattr(guest_marketing, "send_sms", lambda phone, msg, *a, **k: texted.append(phone) or True)
    for rid, phone in ((ACTIVE, "555-123-0001"), (CHURNED, "555-123-0002")):
        cid = guest_marketing.add_guest_contact_public_optin(rid, phone, name="Jane", db_path=db_path)
        visited = (restaurant_now_by_id(rid, naive=True) - timedelta(hours=6)).isoformat()
        conn = sqlite3.connect(db_path)
        conn.execute("UPDATE guest_contacts SET last_visit=? WHERE id=?", (visited, cid))
        conn.commit()
        conn.close()
    guest_marketing.run_review_request_followups(delay_hours=3, db_path=db_path)
    assert texted == ["+15551230001"], f"texted: {texted}"
