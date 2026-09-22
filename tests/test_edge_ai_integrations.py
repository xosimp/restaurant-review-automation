"""Edge cases at the external-integration boundary: Stripe, Google Places,
Google Business (OAuth refresh) and Toast.

Each of these is a third party that fails in a way the happy path never
sees — a locked database under the Stripe claim, an HTTP 200 carrying
REQUEST_DENIED from Places, a timeout on Google's token endpoint, a Toast
restaurant large enough to page past the safety cap. What these tests
protect is the same thing in every case: a failure at the boundary must
never be recorded as a success. A payment event that was never processed
must not be marked handled; a Places key that stopped working must not read
as "synced"; a network blip must not tell the owner their Google connection
is gone; 2,000 rows of a 2,500-row window must not be archived as complete;
two staff called "Maria G." must not be one person with double hours.

Audit: edge_audit/AI.md, findings AI-2, AI-3, AI-6, AI-22, AI-23 and the
"External Integrations" appendix items 15-19. A test that reproduces a
confirmed defect asserts the correct behaviour and is marked
xfail(strict=True), so it flips to a failure the day the fix lands and the
marker comes off with it. Nothing here reaches the network: every HTTP call
is stubbed on the module that makes it.
"""
import sqlite3
import sys
import types
from datetime import date, datetime, timedelta

import pytest
import requests
from flask import Flask

import auth
import client_api
import models
import webhook_routes
from models import Restaurant, create_restaurant


# ── shared plumbing ─────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _redirect_db(db_path, monkeypatch):
    """Every module that bound get_conn at import, plus models itself, points
    at this test's database — and models.DB_PATH too, for the call sites that
    read it lazily (ai_utils.log_api_call, admin_events, ops)."""
    real = models.get_conn
    redirect = lambda *a, **k: real(db_path)
    for mod in (models, webhook_routes, client_api, auth):
        monkeypatch.setattr(mod, "get_conn", redirect, raising=False)
    import scheduler
    monkeypatch.setattr(scheduler, "get_conn", redirect, raising=False)
    monkeypatch.setattr(models, "DB_PATH", db_path)
    auth.init_auth(db_path=db_path)


def _restaurant(db_path, **cols):
    rid = create_restaurant(Restaurant(name=cols.pop("name", "Edge Bistro"),
                                       owner_email=cols.pop("owner_email", "owner@edge.test")),
                            db_path=db_path)
    if cols:
        conn = models.get_conn(db_path)
        conn.execute(f"UPDATE restaurants SET {', '.join(k + '=?' for k in cols)} WHERE id=?",
                     (*cols.values(), rid))
        conn.commit()
        conn.close()
    return rid


def _row(db_path, rid, *cols):
    conn = models.get_conn(db_path)
    try:
        return dict(conn.execute(f"SELECT {', '.join(cols)} FROM restaurants WHERE id=?",
                                 (rid,)).fetchone())
    finally:
        conn.close()


class _Resp:
    """A requests.Response stand-in: status, JSON body, raise_for_status."""

    def __init__(self, status=200, body=None, text=""):
        self.status_code = status
        self._body = body
        self.text = text

    def json(self):
        return self._body

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code} error", response=self)


# ═══ AI-2 · Stripe: claim before handle ═════════════════════════════════════

def _stripe_client(monkeypatch, event_holder, alerts=None):
    """The real /stripe-webhook route with the signature check stood in for.
    `event_holder["event"]` is what construct_event returns, so one client can
    replay a delivery the way Stripe's retry does. Payment alerts to Will go
    through the Resend SDK inside the route; they are recorded, never sent."""
    fake = types.ModuleType("stripe")
    fake.Webhook = type("W", (), {"construct_event": staticmethod(
        lambda *a, **k: event_holder["event"])})
    monkeypatch.setitem(sys.modules, "stripe", fake)
    sent = alerts if alerts is not None else []
    monkeypatch.setattr(webhook_routes, "_resend_key", lambda: "test-key")
    import resend
    monkeypatch.setattr(resend.Emails, "send",
                        staticmethod(lambda payload: sent.append(payload.get("subject"))),
                        raising=False)
    app = Flask(__name__)
    app.register_blueprint(webhook_routes.webhook_bp)
    return app.test_client()


def _post(client):
    return client.post("/stripe-webhook", data=b"{}", headers={"Stripe-Signature": "t=1,v1=x"})


def _checkout_event(rid, event_id="evt_checkout_1"):
    return {"id": event_id, "type": "checkout.session.completed",
            "data": {"object": {"customer": "cus_edge", "subscription": "sub_edge",
                                "customer_details": {"email": "owner@edge.test"},
                                "metadata": {"restaurant_id": str(rid),
                                             "restaurant": "Edge Bistro"}}}}


class _LockedClaimConn:
    """A connection whose INSERT into stripe_events_seen fails the way SQLite
    does under writer contention. Everything else passes straight through."""

    def __init__(self, conn):
        self._c = conn

    def execute(self, sql, *a):
        if sql.lstrip().upper().startswith("INSERT INTO STRIPE_EVENTS_SEEN"):
            raise sqlite3.OperationalError("database is locked")
        return self._c.execute(sql, *a)

    def commit(self):
        self._c.commit()

    def close(self):
        self._c.close()


def _lock_the_claim(monkeypatch, db_path):
    real = models.get_conn
    monkeypatch.setattr(webhook_routes, "get_conn",
                        lambda *a, **k: _LockedClaimConn(real(db_path)))


def test_a_stripe_claim_that_hits_a_locked_database_is_not_treated_as_a_duplicate(db_path, monkeypatch):
    _lock_the_claim(monkeypatch, db_path)
    assert webhook_routes._claim_stripe_event("evt_locked_1") is True, \
        "a bookkeeping failure must fail open, not drop a payment event as already seen"


def test_a_checkout_whose_claim_hits_a_locked_database_still_activates_the_account(db_path, monkeypatch):
    rid = _restaurant(db_path, billing_status="pending")
    holder = {"event": _checkout_event(rid)}
    client = _stripe_client(monkeypatch, holder)
    _lock_the_claim(monkeypatch, db_path)
    resp = _post(client)
    body = resp.get_json() or {}
    assert not body.get("duplicate"), "a lock on the dedup table reported a real payment as a duplicate"
    assert _row(db_path, rid, "billing_status")["billing_status"] == "active", \
        "the client paid and the account was never switched on"


def test_an_event_that_crashed_after_its_claim_is_processed_on_stripes_retry(db_path, monkeypatch):
    # A paused account, and Stripe reporting that the pause is over.
    rid = _restaurant(db_path, billing_status="paused", paused_until="2026-10-20",
                      stripe_customer_id="cus_resume")
    event = {"id": "evt_resume_crash", "type": "customer.subscription.updated",
             "data": {"object": {"id": "sub_r", "customer": "cus_resume", "status": "active",
                                 "metadata": {"restaurant_id": str(rid)},
                                 "pause_collection": None}}}
    holder = {"event": event}
    client = _stripe_client(monkeypatch, holder)

    # The first delivery dies on the unguarded paused_until write.
    real_update = webhook_routes.update_restaurant
    state = {"fail": True}

    def flaky_update(restaurant_id, fields, *a, **k):
        if state["fail"] and "paused_until" in fields:
            raise sqlite3.OperationalError("database is locked")
        return real_update(restaurant_id, fields, *a, **k)

    monkeypatch.setattr(webhook_routes, "update_restaurant", flaky_update)
    first = _post(client)
    assert first.status_code >= 500, "a crash inside the handler must be a 5xx so Stripe retries"

    # Stripe retries the same event id; the database is healthy again.
    state["fail"] = False
    retry = _post(client)
    assert not (retry.get_json() or {}).get("duplicate"), \
        "the retry of an event that never finished was skipped as already handled"
    assert _row(db_path, rid, "paused_until")["paused_until"] is None, \
        "the resume never landed: the account still carries its old pause date"


def test_a_checkout_whose_activation_write_fails_asks_stripe_to_retry_and_the_retry_activates(db_path, monkeypatch):
    rid = _restaurant(db_path, billing_status="pending")
    holder = {"event": _checkout_event(rid, "evt_checkout_locked_write")}
    client = _stripe_client(monkeypatch, holder)
    real_update = webhook_routes.update_restaurant
    state = {"fail": True}

    def flaky_update(restaurant_id, fields, *a, **k):
        if state["fail"] and "billing_status" in fields:
            raise sqlite3.OperationalError("database is locked")
        return real_update(restaurant_id, fields, *a, **k)

    monkeypatch.setattr(webhook_routes, "update_restaurant", flaky_update)
    first = _post(client)
    assert first.status_code >= 500, \
        "activation failed and Stripe was told 200 — it will never redeliver"
    state["fail"] = False
    _post(client)
    assert _row(db_path, rid, "billing_status")["billing_status"] == "active"


def test_a_genuine_redelivery_of_a_processed_stripe_event_is_skipped_and_alerts_once(db_path, monkeypatch):
    """The dedup must survive the fix: a real duplicate (the PRIMARY KEY
    refusing a second insert) is still a duplicate, and Will is told once."""
    rid = _restaurant(db_path, billing_status="pending")
    alerts = []
    holder = {"event": _checkout_event(rid, "evt_checkout_dupe")}
    client = _stripe_client(monkeypatch, holder, alerts)
    first = _post(client)
    assert first.status_code == 200 and not (first.get_json() or {}).get("duplicate")
    assert _row(db_path, rid, "billing_status")["billing_status"] == "active"
    second = _post(client)
    assert second.status_code == 200 and (second.get_json() or {}).get("duplicate") is True
    assert len(alerts) == 1, f"a redelivery re-sent the payment alert: {alerts}"


# ═══ AI-6 · Places: a non-OK status is not a successful fetch ═══════════════

def _stub_places(monkeypatch, body, status=200):
    import fetcher
    calls = []

    def get(url, params=None, timeout=None, **kw):
        calls.append(url)
        return _Resp(status, body)

    monkeypatch.setattr(fetcher.requests, "get", get)
    return calls


def _ledger(db_path):
    conn = models.get_conn(db_path)
    try:
        return [dict(r) for r in conn.execute(
            "SELECT model, status, cost_usd FROM ai_usage WHERE model LIKE 'google-places%'")]
    finally:
        conn.close()


_NON_OK = ["REQUEST_DENIED", "OVER_QUERY_LIMIT", "NOT_FOUND", "INVALID_REQUEST"]


@pytest.mark.parametrize("places_status", _NON_OK)
def test_a_places_error_status_inside_an_http_200_makes_fetch_google_raise(db_path, monkeypatch, places_status):
    import fetcher
    rid = _restaurant(db_path)
    _stub_places(monkeypatch, {"status": places_status,
                               "error_message": "The provided API key is invalid."})
    with pytest.raises(Exception):
        fetcher.fetch_google("ChIJbroken", rid)


def test_a_places_error_status_is_metered_as_an_error_at_no_cost(db_path, monkeypatch):
    import fetcher
    rid = _restaurant(db_path)
    _stub_places(monkeypatch, {"status": "REQUEST_DENIED", "error_message": "billing disabled"})
    try:
        fetcher.fetch_google("ChIJbroken", rid)
    except Exception:
        pass  # raising is the correct half of the fix; the ledger is this test's subject
    rows = _ledger(db_path)
    assert rows, "the Places call was not metered at all"
    assert all(r["status"] == "error" for r in rows), f"a refused call was booked as ok: {rows}"
    assert all((r["cost_usd"] or 0) == 0 for r in rows)


def test_an_ok_places_response_returns_its_reviews_and_is_metered_ok(db_path, monkeypatch):
    import fetcher
    rid = _restaurant(db_path)
    _stub_places(monkeypatch, {"status": "OK", "result": {"reviews": [
        {"author_name": "Ann", "author_url": "https://maps.google.com/contrib/1",
         "rating": 2, "text": "Cold soup.", "time": 1790000000},
    ]}})
    out = fetcher.fetch_google("ChIJgood", rid)
    assert [(r.author, r.rating) for r in out] == [("Ann", 2)]
    rows = _ledger(db_path)
    assert [r["status"] for r in rows] == ["ok"]


@pytest.mark.parametrize("body", [
    {"status": "OK", "result": {}},            # a listing with no reviews yet
    {"status": "ZERO_RESULTS"},                # Places' own "nothing here"
])
def test_a_places_answer_that_legitimately_has_no_reviews_returns_empty_without_raising(db_path, monkeypatch, body):
    """The other side of AI-6: a new restaurant with no reviews is not an
    error, and the fix must not start treating it as one."""
    import fetcher
    rid = _restaurant(db_path)
    _stub_places(monkeypatch, body)
    assert fetcher.fetch_google("ChIJnew", rid) == []


def test_an_http_error_from_places_raises_and_is_metered_as_an_error(db_path, monkeypatch):
    import fetcher
    rid = _restaurant(db_path)
    _stub_places(monkeypatch, {"error": "x"}, status=503)
    with pytest.raises(requests.HTTPError):
        fetcher.fetch_google("ChIJdown", rid)
    rows = _ledger(db_path)
    assert [r["status"] for r in rows] == ["error"] and rows[0]["cost_usd"] == 0


def _run_daily_fetch_with_places(db_path, monkeypatch, body):
    """Drive the real run_daily_fetch against the real fetch_google with only
    the HTTP call stubbed — the path a Places-only restaurant takes."""
    import scheduler
    _stub_places(monkeypatch, body)
    scheduler.run_daily_fetch()


def test_a_places_request_denied_leaves_last_fetched_at_unstamped(db_path, monkeypatch):
    rid = _restaurant(db_path, google_place_id="ChIJbroken", reviews_live=1)
    _run_daily_fetch_with_places(db_path, monkeypatch,
                                 {"status": "REQUEST_DENIED", "error_message": "key revoked"})
    assert _row(db_path, rid, "last_fetched_at")["last_fetched_at"] is None, \
        "a refused Places call stamped the sync, so the 25-hour staleness check can never fire"


def test_a_healthy_places_fetch_with_no_new_reviews_still_stamps_last_fetched_at(db_path, monkeypatch):
    """The fix must not swing the other way: a quiet day is still a sync."""
    rid = _restaurant(db_path, google_place_id="ChIJquiet", reviews_live=1)
    _run_daily_fetch_with_places(db_path, monkeypatch, {"status": "OK", "result": {"reviews": []}})
    assert _row(db_path, rid, "last_fetched_at")["last_fetched_at"] is not None


# ═══ AI-22 · GMB: a transient refresh failure is not "reconnect Google" ══════

def _run_gmb_fetch(db_path, monkeypatch, token_endpoint):
    """run_daily_fetch for a GBP-connected restaurant whose access token has
    expired, with Google's token endpoint replaced by `token_endpoint`.
    Returns the owner alerts raised, by type."""
    import gmb
    import notify
    import scheduler
    raised = []
    monkeypatch.setattr(notify, "raise_alert",
                        lambda rid, alert_type, *a, **k: raised.append(alert_type))
    monkeypatch.setattr(gmb.requests, "post", token_endpoint)
    scheduler.run_daily_fetch()
    return raised


def _gmb_restaurant(db_path):
    return _restaurant(db_path, gmb_refresh_token="refresh-token-1", reviews_live=1,
                       gmb_access_token="expired", gmb_token_expires="2020-01-01T00:00:00")


def _raises(exc):
    def endpoint(*a, **k):
        raise exc
    return endpoint


def _answers(status, body):
    return lambda *a, **k: _Resp(status, body)


_TRANSIENT = [
    pytest.param(_raises(requests.Timeout("read timed out (10s)")), id="timeout"),
    pytest.param(_raises(requests.ConnectionError("connection reset")), id="connection-reset"),
    pytest.param(_answers(500, {"error": "internal_failure"}), id="http-500"),
    pytest.param(_answers(503, {"error": "backendError"}), id="http-503"),
]


@pytest.mark.xfail(strict=True, reason="AI-22: get_valid_token returns None on any refresh error, read as a revoked connection")
@pytest.mark.parametrize("endpoint", _TRANSIENT)
def test_a_transient_google_token_refresh_failure_does_not_tell_the_owner_to_reconnect(db_path, monkeypatch, endpoint):
    _gmb_restaurant(db_path)
    raised = _run_gmb_fetch(db_path, monkeypatch, endpoint)
    assert "connection_lost" not in raised, \
        "a network blip told the owner their Google connection needs reconnecting"


def test_a_revoked_google_refresh_token_does_tell_the_owner_to_reconnect(db_path, monkeypatch):
    """invalid_grant is Google saying the refresh token is dead — that one
    is real and must still reach the owner."""
    _gmb_restaurant(db_path)
    raised = _run_gmb_fetch(db_path, monkeypatch,
                            _answers(400, {"error": "invalid_grant",
                                           "error_description": "Token has been expired or revoked."}))
    assert raised.count("connection_lost") == 1


def test_a_google_refresh_failure_never_stamps_the_sync(db_path, monkeypatch):
    """Whatever the fix does about the alert, a refresh that failed is not a
    fetch that happened (the restaurant has no Place ID to fall back on)."""
    rid = _gmb_restaurant(db_path)
    _run_gmb_fetch(db_path, monkeypatch, _raises(requests.Timeout("slow")))
    assert _row(db_path, rid, "last_fetched_at")["last_fetched_at"] is None


# ═══ AI-3 · Toast: the 20-page cap ═══════════════════════════════════════════

def _toast_restaurant(db_path):
    return _restaurant(db_path, toast_client_id="cid", toast_client_secret="secret",
                       toast_restaurant_guid="guid-large-restaurant")


def _time_entry(i, first="Staff", last="Member", guid=None, day=None, hours=6):
    start = datetime(2026, 8, 1, 6, 0) + timedelta(days=(day if day is not None else i // 40),
                                                   minutes=(i % 40) * 7)
    return {
        "guid": f"te-{i}",
        "employee": {"guid": guid or f"emp-{i % 300}", "firstName": first, "lastName": last},
        "employeeReference": {"guid": guid or f"emp-{i % 300}"},
        "jobReference": {"name": "Server"},
        "inDate": start.strftime("%Y-%m-%dT%H:%M:%S.000+0000"),
        "outDate": (start + timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%S.000+0000"),
    }


def _stub_toast(monkeypatch, pages_of_time_entries, pages_of_orders=0, orders_per_page=100):
    """Toast's labor, business-day and ordersBulk endpoints, paged the way
    Toast pages them. Records every request so a test can see how far the
    client read."""
    import toast
    monkeypatch.setattr(toast, "get_toast_token", lambda rid: "tok")
    seen = {"timeEntries": 0, "ordersBulk": 0}

    def get(url, headers=None, params=None, timeout=None, **kw):
        params = params or {}
        if "timeEntries" in url:
            seen["timeEntries"] += 1
            n = int((params.get("pageToken") or "p0")[1:])
            entries = [_time_entry(n * 100 + j) for j in range(100)]
            nxt = f"p{n + 1}" if n + 1 < pages_of_time_entries else None
            return _Resp(200, {"timeEntries": entries, "nextPageToken": nxt})
        if "businessDay" in url:
            return _Resp(200, [])
        if "ordersBulk" in url:
            seen["ordersBulk"] += 1
            page = int(params.get("page", 1))
            if page > pages_of_orders:
                return _Resp(200, [])
            return _Resp(200, [{"checks": [{"selections": [
                {"item": {"guid": f"item-{page}-{j}"}, "displayName": "Dish", "quantity": 1,
                 "voided": False}]}]} for j in range(orders_per_page)])
        raise AssertionError(f"unexpected Toast URL {url}")

    monkeypatch.setattr(toast.requests, "get", get)
    # Side jobs of a sync that are not under test here.
    import inventory_ledger
    import labor
    monkeypatch.setattr(inventory_ledger, "discover_menu_items", lambda *a, **k: None)
    monkeypatch.setattr(labor, "analyse_shifts_for_restaurant",
                        lambda rid: {"by_day": {}, "date_range": {}})
    return seen


def test_toast_time_entries_spanning_25_pages_are_fetched_in_full_or_the_sync_is_marked_partial(db_path, monkeypatch):
    import toast
    rid = _toast_restaurant(db_path)
    seen = _stub_toast(monkeypatch, pages_of_time_entries=25)
    result = toast.sync_to_db(rid)
    r = _row(db_path, rid, "toast_sync_error")
    complete = bool(result.get("ok")) and result.get("rows") == 2500
    flagged = bool(result.get("partial")) or bool(r["toast_sync_error"])
    assert complete or flagged, (
        f"sync read {seen['timeEntries']} of 25 pages and reported {result} with no partial flag — "
        "labor figures for the whole window rest on part of it")


def test_toast_order_selections_spanning_25_pages_are_all_returned_or_the_fetch_refuses(db_path, monkeypatch):
    import toast
    rid = _toast_restaurant(db_path)
    _stub_toast(monkeypatch, pages_of_time_entries=1, pages_of_orders=25)
    try:
        selections = toast.fetch_order_selections(rid, date(2026, 8, 1))
    except Exception:
        return  # refusing to hand back a partial day is an acceptable fix
    assert len(selections) == 2500, \
        f"a 2,500-order day came back as {len(selections)} with nothing saying it was cut short"


def test_a_toast_window_under_the_page_cap_is_fetched_completely(db_path, monkeypatch):
    """The ordinary restaurant: pagination follows nextPageToken to the end
    and the sync reports every row."""
    import toast
    rid = _toast_restaurant(db_path)
    seen = _stub_toast(monkeypatch, pages_of_time_entries=3)
    result = toast.sync_to_db(rid)
    assert result == {"ok": True, "rows": 300}
    assert seen["timeEntries"] == 3
    assert _row(db_path, rid, "toast_sync_error")["toast_sync_error"] is None


def test_toast_order_pagination_stops_at_the_first_short_page(db_path, monkeypatch):
    import toast
    rid = _toast_restaurant(db_path)
    seen = _stub_toast(monkeypatch, pages_of_time_entries=1, pages_of_orders=2, orders_per_page=100)
    # Page 3 is empty; a full page 2 means the client must ask for page 3.
    assert len(toast.fetch_order_selections(rid, date(2026, 8, 1))) == 200
    assert seen["ordersBulk"] == 3


# ═══ AI-23 · Toast: "First L." is not an identity ═══════════════════════════

def _week_of_shifts(first, last, guid, start_minute):
    """Five 6-hour shifts, Mon 8/3/26 to Fri 8/7/26 — 30 hours, no overtime."""
    out = []
    for d in range(5):
        start = datetime(2026, 8, 3, 9, 0) + timedelta(days=d, minutes=start_minute)
        out.append({
            "employee": {"guid": guid, "firstName": first, "lastName": last},
            "employeeReference": {"guid": guid},
            "jobReference": {"name": "Server"},
            "inDate": start.strftime("%Y-%m-%dT%H:%M:%S.000+0000"),
            "outDate": (start + timedelta(hours=6)).strftime("%Y-%m-%dT%H:%M:%S.000+0000"),
        })
    return out


def test_two_toast_staff_with_the_same_first_name_and_last_initial_stay_two_people(db_path):
    import labor
    import toast
    entries = (_week_of_shifts("Maria", "Garcia", "emp-guid-garcia", 0)
               + _week_of_shifts("Maria", "Gomez", "emp-guid-gomez", 30))
    rows = toast.normalise_entries(entries, {})
    analysis = labor.analyse_shifts(rows, hourly_rate=15.0)
    assert len(analysis["employee_hours"]) == 2, \
        f"two people became one: {list(analysis['employee_hours'])}"
    assert not [f for f in analysis["overtime_risk"] if f["status"] == "overtime"], \
        "two 30-hour weeks were flagged as one 60-hour overtime week"


def test_one_toast_employee_across_many_shifts_is_one_person(db_path):
    """The other direction: identity must not fork a single person."""
    import labor
    import toast
    rows = toast.normalise_entries(_week_of_shifts("Jake", "Thomas", "emp-guid-jake", 0), {})
    analysis = labor.analyse_shifts(rows, hourly_rate=15.0)
    assert len(analysis["employee_hours"]) == 1
    (hours,) = analysis["employee_hours"].values()
    assert hours["actual"] == pytest.approx(30.0)
