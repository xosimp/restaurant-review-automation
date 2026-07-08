"""guest_marketing.py — SMS lifecycle marketing to guests. The consent
model is the whole point: an owner manually adding a number must never be
able to make it marketable, only the guest's own public opt-in submission
can. TWILIO_* env vars are unset in this test environment, so send_sms()
safely no-ops (prints + returns False) rather than making a real network
call — confirmed by reading notify.send_sms's own guard."""
import types
from datetime import timedelta

import pytest

import guest_marketing
import models
from guest_marketing import (
    init_guest_marketing, get_guest_contacts, add_guest_contact_manual,
    add_guest_contact_public_optin, delete_guest_contact, unsubscribe_guest,
    draft_campaign_message, send_campaign, mark_guest_visit,
    run_review_request_followups,
)
from models import create_restaurant, get_restaurant, get_conn, Restaurant
from time_utils import restaurant_now_by_id


@pytest.fixture(autouse=True)
def _init_tables(db_path):
    init_guest_marketing(db_path=db_path)


@pytest.fixture(autouse=True)
def _redirect_db(monkeypatch, db_path):
    """marketing.get_profile_for_restaurant() (used by draft_campaign_message)
    doesn't take a db_path argument — it always resolves models.get_conn(),
    same gap as get_review_stats() hit in test_ask_cavnar.py. Patching that
    one name redirects the whole chain to the test fixture DB."""
    real_get_conn = models.get_conn
    monkeypatch.setattr(models, "get_conn", lambda *a, **k: real_get_conn(db_path))


def _restaurant(db_path, **kw):
    rid = create_restaurant(Restaurant(name=kw.pop("name", "Guest Marketing Co"), owner_email="g@x.com", **kw), db_path=db_path)
    return get_restaurant(rid, db_path=db_path)


# ── consent model — the actual compliance boundary ──────────────────────────

def test_manually_added_contact_has_no_consent(db_path):
    r = _restaurant(db_path)
    add_guest_contact_manual(r.id, "555-123-4567", name="Walk-in Guest", db_path=db_path)
    contacts = get_guest_contacts(r.id, db_path=db_path)
    assert len(contacts) == 1
    assert contacts[0]["consent"] is False
    assert contacts[0]["consent_at"] is None


def test_manually_added_contact_is_never_sms_eligible(db_path):
    r = _restaurant(db_path)
    add_guest_contact_manual(r.id, "555-123-4567", db_path=db_path)
    eligible = get_guest_contacts(r.id, consent_only=True, db_path=db_path)
    assert eligible == []


def test_public_optin_grants_consent_with_timestamp(db_path):
    r = _restaurant(db_path)
    add_guest_contact_public_optin(r.id, "555-987-6543", name="Jane", db_path=db_path)
    contacts = get_guest_contacts(r.id, db_path=db_path)
    assert contacts[0]["consent"] is True
    assert contacts[0]["consent_at"] is not None


def test_public_optin_contact_is_sms_eligible(db_path):
    r = _restaurant(db_path)
    add_guest_contact_public_optin(r.id, "555-987-6543", db_path=db_path)
    eligible = get_guest_contacts(r.id, consent_only=True, db_path=db_path)
    assert len(eligible) == 1


def test_management_view_shows_both_consented_and_not(db_path):
    r = _restaurant(db_path)
    add_guest_contact_manual(r.id, "555-111-1111", db_path=db_path)
    add_guest_contact_public_optin(r.id, "555-222-2222", db_path=db_path)
    all_contacts = get_guest_contacts(r.id, db_path=db_path)
    eligible = get_guest_contacts(r.id, consent_only=True, db_path=db_path)
    assert len(all_contacts) == 2
    assert len(eligible) == 1


# ── phone-number upsert behavior ─────────────────────────────────────────────

def test_same_phone_normalizes_and_dedupes(db_path):
    """The same real number entered in different formats must not create
    two rows — the UNIQUE(restaurant_id, phone) constraint plus phone
    normalization is what makes that hold."""
    r = _restaurant(db_path)
    add_guest_contact_manual(r.id, "(555) 123-4567", db_path=db_path)
    add_guest_contact_manual(r.id, "555-123-4567", db_path=db_path)
    contacts = get_guest_contacts(r.id, db_path=db_path)
    assert len(contacts) == 1


def test_public_optin_upgrades_an_existing_manual_contact(db_path):
    """A number the owner already added manually (no consent) later opting
    in themselves via the public page must become SMS-eligible — same
    person, same row, not a duplicate."""
    r = _restaurant(db_path)
    add_guest_contact_manual(r.id, "555-123-4567", name="From receipt", db_path=db_path)
    add_guest_contact_public_optin(r.id, "555-123-4567", db_path=db_path)
    contacts = get_guest_contacts(r.id, db_path=db_path)
    assert len(contacts) == 1
    assert contacts[0]["consent"] is True


def test_manually_readding_an_already_consented_contact_does_not_revoke_it(db_path):
    r = _restaurant(db_path)
    add_guest_contact_public_optin(r.id, "555-123-4567", db_path=db_path)
    add_guest_contact_manual(r.id, "555-123-4567", db_path=db_path)  # owner re-adds later, e.g. from a new list
    contacts = get_guest_contacts(r.id, db_path=db_path)
    assert contacts[0]["consent"] is True  # unsubscribe() is the only way to revoke, not a manual re-add


def test_two_restaurants_can_have_the_same_guest_phone(db_path):
    r1 = _restaurant(db_path, name="Restaurant One")
    r2 = _restaurant(db_path, name="Restaurant Two")
    add_guest_contact_public_optin(r1.id, "555-123-4567", db_path=db_path)
    add_guest_contact_public_optin(r2.id, "555-123-4567", db_path=db_path)
    assert len(get_guest_contacts(r1.id, db_path=db_path)) == 1
    assert len(get_guest_contacts(r2.id, db_path=db_path)) == 1


# ── delete / unsubscribe ─────────────────────────────────────────────────────

def test_delete_contact_is_scoped_to_restaurant(db_path):
    """A client must never be able to delete another restaurant's contact
    by guessing an id — the exact IDOR shape this codebase has been
    careful about elsewhere (approve_response, alert_contacts, etc.)."""
    r1 = _restaurant(db_path, name="Restaurant One")
    r2 = _restaurant(db_path, name="Restaurant Two")
    cid = add_guest_contact_manual(r1.id, "555-123-4567", db_path=db_path)
    delete_guest_contact(cid, r2.id, db_path=db_path)  # wrong restaurant_id
    assert len(get_guest_contacts(r1.id, db_path=db_path)) == 1  # untouched
    delete_guest_contact(cid, r1.id, db_path=db_path)  # correct restaurant_id
    assert len(get_guest_contacts(r1.id, db_path=db_path)) == 0


def test_unsubscribe_makes_contact_ineligible_but_keeps_the_record(db_path):
    r = _restaurant(db_path)
    add_guest_contact_public_optin(r.id, "555-123-4567", db_path=db_path)
    unsubscribe_guest(r.id, "555-123-4567", db_path=db_path)
    all_contacts = get_guest_contacts(r.id, db_path=db_path)
    eligible = get_guest_contacts(r.id, consent_only=True, db_path=db_path)
    assert len(all_contacts) == 1  # record kept for audit/history
    assert eligible == []          # but no longer sendable


# ── AI drafting ──────────────────────────────────────────────────────────────

def test_draft_campaign_message_uses_restaurant_profile(db_path, monkeypatch):
    r = _restaurant(db_path, vibe="cozy Italian bistro", neighborhood="Test City")

    captured = {}

    def fake_create_with_retry(client, **kwargs):
        captured.update(kwargs)
        return types.SimpleNamespace(content=[types.SimpleNamespace(text="Come back and see us this week!")])

    monkeypatch.setattr(guest_marketing, "create_with_retry", fake_create_with_retry)
    message = draft_campaign_message(r, campaign_type="win_back")

    assert message == "Come back and see us this week!"
    prompt = captured["messages"][0]["content"]
    assert "cozy Italian bistro" in prompt
    assert "Test City" in prompt
    assert captured["restaurant_id"] == r.id
    assert captured["action"] == "guest_campaign_draft"
    assert "temperature" not in captured  # claude-sonnet-5 rejects this param outright


def test_draft_campaign_message_includes_topic_when_given(db_path, monkeypatch):
    r = _restaurant(db_path)
    captured = {}

    def fake_create_with_retry(client, **kwargs):
        captured.update(kwargs)
        return types.SimpleNamespace(content=[types.SimpleNamespace(text="ok")])

    monkeypatch.setattr(guest_marketing, "create_with_retry", fake_create_with_retry)
    draft_campaign_message(r, campaign_type="event", topic="Half-price wine Wednesdays")

    prompt = captured["messages"][0]["content"]
    assert "Half-price wine Wednesdays" in prompt


# ── sending a campaign ───────────────────────────────────────────────────────

def test_send_campaign_only_reaches_consented_contacts(db_path):
    r = _restaurant(db_path)
    add_guest_contact_manual(r.id, "555-111-1111", db_path=db_path)       # not consented
    add_guest_contact_public_optin(r.id, "555-222-2222", db_path=db_path)  # consented
    result = send_campaign(r.id, "We miss you!", db_path=db_path)
    assert result["total"] == 1  # only the consented one counted


def test_send_campaign_logs_to_guest_campaigns_table(db_path):
    r = _restaurant(db_path)
    add_guest_contact_public_optin(r.id, "555-222-2222", db_path=db_path)
    send_campaign(r.id, "We miss you!", db_path=db_path)
    conn = get_conn(db_path)
    row = conn.execute("SELECT message, sent_count, failed_count FROM guest_campaigns WHERE restaurant_id=?", (r.id,)).fetchone()
    conn.close()
    assert row["message"] == "We miss you!"
    # TWILIO_* unset in this test env, so send_sms() always returns False —
    # this is exercising the counting logic, not a real Twilio send.
    assert row["failed_count"] == 1
    assert row["sent_count"] == 0


def test_send_campaign_with_no_consented_contacts_sends_nothing(db_path):
    r = _restaurant(db_path)
    result = send_campaign(r.id, "Hello", db_path=db_path)
    assert result == {"sent": 0, "failed": 0, "total": 0}


def test_send_campaign_appends_stop_instructions(db_path, monkeypatch):
    r = _restaurant(db_path)
    add_guest_contact_public_optin(r.id, "555-222-2222", db_path=db_path)
    captured = {}

    def fake_send_sms(phone, message):
        captured["message"] = message
        return True

    monkeypatch.setattr(guest_marketing, "send_sms", fake_send_sms)
    send_campaign(r.id, "We miss you!", db_path=db_path)
    assert "STOP" in captured["message"]
    assert "We miss you!" in captured["message"]


# ── last_visit tracking ──────────────────────────────────────────────────────

def _backdate_visit(db_path, restaurant_id, contact_id, hours_ago):
    ts = (restaurant_now_by_id(restaurant_id, naive=True) - timedelta(hours=hours_ago)).isoformat()
    conn = get_conn(db_path)
    conn.execute("UPDATE guest_contacts SET last_visit=? WHERE id=?", (ts, contact_id))
    conn.commit()
    conn.close()
    return ts


def test_public_optin_sets_last_visit(db_path):
    """The QR-code scan itself is the visit signal — no separate step needed."""
    r = _restaurant(db_path)
    add_guest_contact_public_optin(r.id, "555-123-4567", db_path=db_path)
    contacts = get_guest_contacts(r.id, db_path=db_path)
    assert contacts[0]["last_visit"] is not None


def test_manual_add_does_not_set_last_visit(db_path):
    r = _restaurant(db_path)
    add_guest_contact_manual(r.id, "555-123-4567", db_path=db_path)
    contacts = get_guest_contacts(r.id, db_path=db_path)
    assert contacts[0]["last_visit"] is None


def test_repeat_optin_bumps_last_visit_but_not_consent_at(db_path):
    """A returning guest re-scanning the QR code is a fresh visit signal
    (re-arms the review-request follow-up), but their original consent
    timestamp shouldn't be rewritten."""
    r = _restaurant(db_path)
    cid = add_guest_contact_public_optin(r.id, "555-123-4567", db_path=db_path)
    first = get_guest_contacts(r.id, db_path=db_path)[0]
    _backdate_visit(db_path, r.id, cid, hours_ago=10)  # simulate time passing
    add_guest_contact_public_optin(r.id, "555-123-4567", db_path=db_path)  # scans again
    second = get_guest_contacts(r.id, db_path=db_path)[0]
    assert second["consent_at"] == first["consent_at"]
    assert second["last_visit"] != first["last_visit"]


def test_mark_guest_visit_sets_last_visit(db_path):
    r = _restaurant(db_path)
    cid = add_guest_contact_manual(r.id, "555-123-4567", db_path=db_path)
    mark_guest_visit(cid, r.id, db_path=db_path)
    contacts = get_guest_contacts(r.id, db_path=db_path)
    assert contacts[0]["last_visit"] is not None


def test_mark_guest_visit_is_scoped_to_restaurant(db_path):
    """Same IDOR shape as delete_guest_contact — a client must not be able to
    touch another restaurant's contact by guessing an id."""
    r1 = _restaurant(db_path, name="Restaurant One")
    r2 = _restaurant(db_path, name="Restaurant Two")
    cid = add_guest_contact_manual(r1.id, "555-123-4567", db_path=db_path)
    mark_guest_visit(cid, r2.id, db_path=db_path)  # wrong restaurant_id
    assert get_guest_contacts(r1.id, db_path=db_path)[0]["last_visit"] is None
    mark_guest_visit(cid, r1.id, db_path=db_path)  # correct restaurant_id
    assert get_guest_contacts(r1.id, db_path=db_path)[0]["last_visit"] is not None


# ── automated post-visit review request ──────────────────────────────────────

def test_review_request_followup_sends_after_delay(db_path):
    r = _restaurant(db_path, google_place_id="ChIJtestplace")
    cid = add_guest_contact_public_optin(r.id, "555-123-4567", name="Jane", db_path=db_path)
    _backdate_visit(db_path, r.id, cid, hours_ago=4)

    result = run_review_request_followups(delay_hours=3, db_path=db_path)

    # TWILIO_* unset in this test env, so send_sms() always returns False —
    # this exercises the eligibility/logging logic, not a real Twilio send.
    assert result == {"sent": 0, "failed": 1, "skipped_no_place_id": 0}
    contacts = get_guest_contacts(r.id, db_path=db_path)
    assert contacts[0]["last_review_requested_at"] is not None
    conn = get_conn(db_path)
    row = conn.execute("SELECT * FROM review_requests WHERE restaurant_id=?", (r.id,)).fetchone()
    conn.close()
    assert row["method"] == "sms_auto"
    assert row["status"] == "failed"
    assert row["customer_phone"] == "+15551234567"


def test_review_request_followup_skips_contact_within_delay_window(db_path):
    r = _restaurant(db_path, google_place_id="ChIJtestplace")
    cid = add_guest_contact_public_optin(r.id, "555-123-4567", db_path=db_path)
    _backdate_visit(db_path, r.id, cid, hours_ago=1)  # too recent for a 3hr delay

    result = run_review_request_followups(delay_hours=3, db_path=db_path)

    assert result == {"sent": 0, "failed": 0, "skipped_no_place_id": 0}
    assert get_guest_contacts(r.id, db_path=db_path)[0]["last_review_requested_at"] is None


def test_review_request_followup_skips_restaurant_without_place_id(db_path):
    r = _restaurant(db_path)  # no google_place_id
    cid = add_guest_contact_public_optin(r.id, "555-123-4567", db_path=db_path)
    _backdate_visit(db_path, r.id, cid, hours_ago=4)

    result = run_review_request_followups(delay_hours=3, db_path=db_path)

    assert result == {"sent": 0, "failed": 0, "skipped_no_place_id": 1}


def test_review_request_followup_skips_unconsented_contact(db_path):
    r = _restaurant(db_path, google_place_id="ChIJtestplace")
    cid = add_guest_contact_manual(r.id, "555-123-4567", db_path=db_path)  # no consent
    mark_guest_visit(cid, r.id, db_path=db_path)
    _backdate_visit(db_path, r.id, cid, hours_ago=4)

    result = run_review_request_followups(delay_hours=3, db_path=db_path)

    assert result == {"sent": 0, "failed": 0, "skipped_no_place_id": 0}


def test_review_request_followup_skips_unsubscribed_contact(db_path):
    r = _restaurant(db_path, google_place_id="ChIJtestplace")
    cid = add_guest_contact_public_optin(r.id, "555-123-4567", db_path=db_path)
    _backdate_visit(db_path, r.id, cid, hours_ago=4)
    unsubscribe_guest(r.id, "555-123-4567", db_path=db_path)

    result = run_review_request_followups(delay_hours=3, db_path=db_path)

    assert result == {"sent": 0, "failed": 0, "skipped_no_place_id": 0}


def test_review_request_followup_is_idempotent(db_path):
    """Running the hourly job twice must not double-text the same visit."""
    r = _restaurant(db_path, google_place_id="ChIJtestplace")
    cid = add_guest_contact_public_optin(r.id, "555-123-4567", db_path=db_path)
    _backdate_visit(db_path, r.id, cid, hours_ago=4)

    run_review_request_followups(delay_hours=3, db_path=db_path)
    second = run_review_request_followups(delay_hours=3, db_path=db_path)

    assert second == {"sent": 0, "failed": 0, "skipped_no_place_id": 0}
    conn = get_conn(db_path)
    count = conn.execute("SELECT COUNT(*) AS c FROM review_requests WHERE restaurant_id=?", (r.id,)).fetchone()["c"]
    conn.close()
    assert count == 1


def test_review_request_followup_rearms_on_new_visit(db_path):
    """A genuinely new visit (guest scans the join link again) must be
    eligible for its own follow-up, even though one was already sent for
    the earlier visit. Simulated as: the prior request sits far in the past,
    and a fresh visit landed more recently but still past the delay — the
    realistic timeline for "time has passed since the first text went out"."""
    r = _restaurant(db_path, google_place_id="ChIJtestplace")
    cid = add_guest_contact_public_optin(r.id, "555-123-4567", db_path=db_path)
    _backdate_visit(db_path, r.id, cid, hours_ago=4)
    run_review_request_followups(delay_hours=3, db_path=db_path)

    conn = get_conn(db_path)
    old_ts = (restaurant_now_by_id(r.id, naive=True) - timedelta(hours=48)).isoformat()
    conn.execute("UPDATE guest_contacts SET last_review_requested_at=? WHERE id=?", (old_ts, cid))
    conn.commit()
    conn.close()
    _backdate_visit(db_path, r.id, cid, hours_ago=4)  # new visit, itself now past the delay
    run_review_request_followups(delay_hours=3, db_path=db_path)

    conn = get_conn(db_path)
    count = conn.execute("SELECT COUNT(*) AS c FROM review_requests WHERE restaurant_id=?", (r.id,)).fetchone()["c"]
    conn.close()
    assert count == 2


def test_review_request_followup_respects_delay_hours_override(db_path):
    r = _restaurant(db_path, google_place_id="ChIJtestplace")
    cid = add_guest_contact_public_optin(r.id, "555-123-4567", db_path=db_path)
    _backdate_visit(db_path, r.id, cid, hours_ago=2)

    result = run_review_request_followups(delay_hours=1, db_path=db_path)

    assert result["failed"] == 1  # would have been skipped at the default 3hr delay


def test_review_request_followup_message_includes_review_link(db_path, monkeypatch):
    r = _restaurant(db_path, google_place_id="ChIJtestplace")
    cid = add_guest_contact_public_optin(r.id, "555-123-4567", name="Jane", db_path=db_path)
    _backdate_visit(db_path, r.id, cid, hours_ago=4)
    captured = {}

    def fake_send_sms(phone, message):
        captured["phone"] = phone
        captured["message"] = message
        return True

    monkeypatch.setattr(guest_marketing, "send_sms", fake_send_sms)
    run_review_request_followups(delay_hours=3, db_path=db_path)

    assert "ChIJtestplace" in captured["message"]
    assert "Jane" in captured["message"]
    assert "STOP" in captured["message"]
