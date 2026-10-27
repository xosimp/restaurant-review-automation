"""Audit #25 A06-09 (before Tock goes live): a late event never undoes a
newer one, and a guest's contact details come from the guest.

Tock retries deliveries, so a "booked" can arrive after the "cancelled" it
preceded. Each booking keeps the newest applied event's own time
(tock_bookings.event_ms, stripe_billing_clock's rule): an older event is
kept in tock_events and never applied, so the party stays cancelled and its
covers stay out of the forecast. reprocess_tock re-applies in event time,
not arrival order. A phone or email is read from the guest sub-object only
(or the body's own top-level fields when there is none) — never the venue's
or a server's number found deeper in the body.

Tock's payload format is still unconfirmed (docs/integrations/TOCK_WEBHOOKS.md);
these bodies are synthetic and use the field names the readers accept.
"""
import json
from datetime import timedelta

import pytest

import guest_hub as gh
import guest_marketing as gm
import models
from models import Restaurant, create_restaurant

# The deploy gate's contract slice (TESTING.md, Tiers; blind re-audit 10/27/26).
pytestmark = pytest.mark.contract


@pytest.fixture(autouse=True)
def _db(monkeypatch, db_path):
    real = models.get_conn
    redirect = lambda *a, **k: real(db_path)
    for mod in (models, gm):
        monkeypatch.setattr(mod, "get_conn", redirect, raising=False)
    monkeypatch.setattr(models, "DB_PATH", db_path)
    monkeypatch.setattr(gm, "DB_PATH", db_path)
    gm.init_guest_marketing(db_path)
    yield


@pytest.fixture
def rid(db_path, monkeypatch):
    r = create_restaurant(Restaurant(name="Fixture Tavern", owner_email="owner@fixture.test",
                                     timezone="America/Chicago"), db_path=db_path)
    monkeypatch.setenv(gh.TOCK_TOKENS_ENV, f"{r}:tock-fixture-secret")
    return r


def _night(rid):
    """Two nights from the restaurant's own today, 7pm local."""
    from time_utils import restaurant_now_by_id
    today = restaurant_now_by_id(rid).date()
    return (today + timedelta(days=2)).isoformat(), f"{(today + timedelta(days=2)).isoformat()}T19:00:00"


def _send(hook, event):
    body, status = gh.receive_tock(hook, "tock-fixture-secret", json.dumps(event).encode())
    assert status == 200, body
    return body


def _event(eid, etype, updated_at, when, party=4, **guest):
    guest = guest or {"firstName": "Robin", "lastName": "Vale", "phone": "(312) 555-0142"}
    return {"eventId": eid, "type": etype,
            "reservation": {"id": "RES-9001", "partySize": party, "dateTime": when, "updatedAt": updated_at,
                            "guest": guest}}


def _one(sql, args=()):
    conn = models.get_conn()
    try:
        r = conn.execute(sql, args).fetchone()
        return dict(r) if r else None
    finally:
        conn.close()


def _covers(rid, day):
    row = _one("SELECT covers FROM demand_signals WHERE restaurant_id=? AND date=?", (rid, day))
    return row["covers"] if row else None


def test_a_late_booked_event_never_revives_a_cancelled_party(rid):
    day, when = _night(rid)
    _send("reservations", _event("e-create", "reservation.created", "2026-10-01T15:00:00Z", when))
    assert _covers(rid, day) == 4
    _send("reservations", _event("e-cancel", "reservation.cancelled", "2026-10-01T17:00:00Z", when))
    assert _covers(rid, day) == 0
    # Tock's retry of an update made between the two (t1 < t2), delivered last.
    _send("reservations", _event("e-modify", "reservation.modified", "2026-10-01T16:00:00Z", when, party=6))
    booking = _one("SELECT status, party_size, event_ms FROM tock_bookings WHERE restaurant_id=?", (rid,))
    assert booking["status"] == "cancelled" and booking["party_size"] == 4
    assert _covers(rid, day) == 0, "a cancelled party's covers came back into the forecast"
    stored = _one("SELECT processed_at, error FROM tock_events WHERE event_key='e-modify'")
    assert stored["processed_at"] and stored["error"] is None, "kept and marked processed, just not applied"


def test_a_newer_event_still_applies_and_times_with_offsets_compare_as_instants(rid):
    day, when = _night(rid)
    _send("reservations", _event("e1", "reservation.created", "2026-10-01T10:00:00-05:00", when))
    # 15:30Z is 10:30 in Chicago: newer, though its clock reads earlier.
    _send("reservations", _event("e2", "reservation.modified", "2026-10-01T15:30:00Z", when, party=2))
    assert _one("SELECT party_size FROM tock_bookings")["party_size"] == 2
    assert _covers(rid, day) == 2


def test_events_without_a_time_behave_as_before(rid):
    day, when = _night(rid)
    base = {"reservation": {"id": "RES-77", "partySize": 3, "dateTime": when,
                            "guest": {"firstName": "Ari", "phone": "312-555-0177"}}}
    _send("reservations", dict(base, eventId="n1", type="reservation.created"))
    _send("reservations", dict(base, eventId="n2", type="reservation.cancelled"))
    assert _one("SELECT status FROM tock_bookings")["status"] == "cancelled" and _covers(rid, day) == 0


def test_reprocess_applies_events_in_the_order_they_happened(rid, monkeypatch):
    day, when = _night(rid)
    # Arrival order: cancelled (t3), created (t1), modified (t2).
    _send("reservations", _event("r3", "reservation.cancelled", "2026-10-02T12:00:00Z", when))
    _send("reservations", _event("r1", "reservation.created", "2026-10-02T10:00:00Z", when))
    _send("reservations", _event("r2", "reservation.modified", "2026-10-02T11:00:00Z", when, party=8))
    assert _one("SELECT status FROM tock_bookings")["status"] == "cancelled"
    conn = models.get_conn()
    try:
        conn.execute("DELETE FROM tock_bookings WHERE restaurant_id=?", (rid,))
        conn.commit()
    finally:
        conn.close()
    applied = []
    real_apply = gh.apply_tock_event

    def spy(r, hook, event, db_path=gh.DB_PATH):
        applied.append(event["eventId"])
        return real_apply(r, hook, event, db_path=db_path)
    monkeypatch.setattr(gh, "apply_tock_event", spy)
    out = gh.reprocess_tock(rid)
    assert applied == ["r1", "r2", "r3"] and out == {"events": 3, "ok": 3, "failed": 0}
    assert _one("SELECT status FROM tock_bookings")["status"] == "cancelled" and _covers(rid, day) == 0


def test_a_restaurant_phone_is_never_read_as_the_guests(rid):
    _day, when = _night(rid)
    event = {"eventId": "p1", "type": "reservation.created",
             "reservation": {"id": "RES-55", "partySize": 2, "dateTime": when,
                             "venue": {"name": "Fixture Tavern", "phone": "(312) 555-0100"},
                             "server": {"name": "Sam", "mobile": "312-555-0199"},
                             "guest": {"firstName": "Lee", "lastName": "Park", "email": "lee@fixture.test"}}}
    _send("reservations", event)
    booking = _one("SELECT phone, contact_id, email FROM tock_bookings")
    assert booking["phone"] is None and booking["contact_id"] is None, "the venue's number became a guest"
    assert booking["email"] == "lee@fixture.test"
    assert _one("SELECT COUNT(*) AS n FROM guest_contacts WHERE restaurant_id=?", (rid,))["n"] == 0


def test_with_no_guest_object_only_the_bodys_own_fields_are_read(rid):
    _day, when = _night(rid)
    event = {"eventId": "p2", "type": "reservation.created",
             "reservation": {"id": "RES-56", "partySize": 2, "dateTime": when, "phone": "312-555-0161",
                             "venue": {"phone": "(312) 555-0100", "email": "hello@fixture.test"}}}
    _send("reservations", event)
    booking = _one("SELECT phone, email FROM tock_bookings")
    assert booking["phone"] == "+13125550161" and booking["email"] is None


def test_the_event_time_is_read_from_the_change_stamp_never_the_booking_time():
    wrapped = {"eventId": "x", "timestamp": "2026-10-01T12:00:00Z",
               "reservation": {"id": "R", "dateTime": "2026-10-05T19:00:00", "updatedAt": "2026-10-01T11:00:00Z"}}
    assert gh._event_ms(wrapped, gh._body(wrapped, "reservations")) == 1790852400000   # the record's updatedAt
    occurred = {"eventId": "o", "occurredAt": 1790856000, "reservation": {"id": "R", "dateTime": "2026-10-05T19:00:00"}}
    assert gh._event_ms(occurred, gh._body(occurred, "reservations")) == 1790856000000
    # A bare record's "timestamp" may be its seating: not an event time.
    bare = {"id": "R", "timestamp": "2026-10-05T19:00:00Z", "partySize": 2}
    assert gh._event_ms(bare, gh._body(bare, "reservations")) is None
    assert gh._event_ms({"eventId": "z", "reservation": {"id": "R"}}, {"id": "R"}) is None


def test_a_delivery_time_is_never_read_as_the_change_time():
    """An envelope's timestamp, createdAt, sentAt, publishedAt or deliveredAt
    may be stamped by the delivery attempt (blind re-audit 10/27/26): never
    an ordering key. An event with only those is applied in arrival order."""
    for key in ("timestamp", "createdAt", "created_at", "created", "sentAt", "publishedAt", "deliveredAt"):
        event = {"eventId": "d", key: "2026-10-01T12:00:00Z", "reservation": {"id": "R", "partySize": 2}}
        assert gh._event_ms(event, gh._body(event, "reservations")) is None, key
    # A retried envelope carries a newer createdAt; the record's own updatedAt decides.
    retried = {"eventId": "r", "createdAt": "2026-10-09T12:00:00Z",
               "reservation": {"id": "R", "updatedAt": "2026-10-01T11:00:00Z"}}
    assert gh._event_ms(retried, gh._body(retried, "reservations")) == 1790852400000


def test_a_retried_envelope_with_a_newer_delivery_time_is_still_stale(rid):
    day, when = _night(rid)
    _send("reservations", _event("c1", "reservation.created", "2026-10-01T15:00:00Z", when))
    _send("reservations", _event("c2", "reservation.cancelled", "2026-10-01T17:00:00Z", when))
    late = _event("c3", "reservation.modified", "2026-10-01T16:00:00Z", when, party=6)
    late.update(createdAt="2026-10-02T09:00:00Z", sentAt="2026-10-02T09:00:01Z", timestamp=1790950000)
    _send("reservations", late)
    assert _one("SELECT status, party_size FROM tock_bookings") == {"status": "cancelled", "party_size": 4}
    assert _covers(rid, day) == 0


def test_a_stale_event_that_loses_the_race_never_touches_the_contact(rid, monkeypatch):
    """Two deliveries racing both pass the read check; the guarded write
    takes only the newer. The loser used to have updated the guest's
    contact (party, opt-in, Tock id) already."""
    _day, when = _night(rid)
    _send("reservations", _event("n1", "reservation.created", "2026-10-01T17:00:00Z", when, party=4,
                                 firstName="Robin", phone="(312) 555-0142", marketingOptIn=True, guestId="G-1"))
    contact = _one("SELECT id, last_party_size, tock_marketing_optin, tock_guest_id FROM guest_contacts "
                   "WHERE restaurant_id=?", (rid,))
    assert contact["last_party_size"] == 4
    # The older event, as if its read ran before the newer write landed.
    monkeypatch.setattr(gh, "_older", lambda event_ms, prev: False)
    touched = []
    real_touch = gh._touch_contact
    monkeypatch.setattr(gh, "_touch_contact", lambda *a, **k: touched.append(a) or real_touch(*a, **k))
    out = gh.apply_tock_event(rid, "reservations",
                              _event("o1", "reservation.modified", "2026-10-01T16:00:00Z", when, party=9,
                                     firstName="Robin", phone="(312) 555-0142", marketingOptIn=False,
                                     guestId="G-OLD"))
    assert out["applied"] is False and out["stale"] is True
    assert touched == [], "the losing event touched the contact"
    after = _one("SELECT last_party_size, tock_marketing_optin, tock_guest_id FROM guest_contacts WHERE id=?",
                 (contact["id"],))
    assert after == {"last_party_size": 4, "tock_marketing_optin": 1, "tock_guest_id": "G-1"}
    assert _one("SELECT party_size, contact_id FROM tock_bookings") == {"party_size": 4, "contact_id": contact["id"]}


def test_a_booking_level_phone_is_kept_when_the_guest_object_has_none(rid):
    """`{reservation: {phone, guest: {name}}}`: the booking's own top-level
    phone is the guest's when the guest object carries none — never a
    deeper one (the venue's)."""
    _day, when = _night(rid)
    event = {"eventId": "bp1", "type": "reservation.created",
             "reservation": {"id": "RES-60", "partySize": 3, "dateTime": when, "phone": "312-555-0163",
                             "email": "booker@fixture.test",
                             "venue": {"phone": "(312) 555-0100"},
                             "guest": {"firstName": "Dana", "lastName": "Reyes"}}}
    _send("reservations", event)
    booking = _one("SELECT phone, email, contact_id FROM tock_bookings")
    assert booking["phone"] == "+13125550163" and booking["email"] == "booker@fixture.test"
    assert booking["contact_id"] is not None
    contact = _one("SELECT name, phone FROM guest_contacts WHERE id=?", (booking["contact_id"],))
    assert contact == {"name": "Dana Reyes", "phone": "+13125550163"}
    # The guest object's own phone still wins over the booking's.
    event2 = {"eventId": "bp2", "type": "reservation.created",
              "reservation": {"id": "RES-61", "partySize": 2, "dateTime": when, "phone": "312-555-0164",
                              "guest": {"firstName": "Kim", "phone": "312-555-0165"}}}
    _send("reservations", event2)
    assert _one("SELECT phone FROM tock_bookings WHERE tock_id='RES-61'")["phone"] == "+13125550165"


def test_a_guest_profile_event_reads_its_own_nested_contact_details(rid):
    event = {"eventId": "g1", "type": "guest.updated",
             "guest": {"id": "G-7", "firstName": "Noor", "lastName": "Haddad",
                       "contactInfo": {"mobilePhone": "312-555-0188", "emailAddress": "noor@fixture.test"}}}
    _send("guests", event)
    contact = _one("SELECT name, email FROM guest_contacts WHERE restaurant_id=? AND phone=?", (rid, "+13125550188"))
    assert contact and contact["name"] == "Noor Haddad" and contact["email"] == "noor@fixture.test"
