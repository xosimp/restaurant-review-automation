"""Write routes that must happen once: a double-click, a second device, a
retry after a timeout, a restart, or an automation firing after the owner
already acted must not send, post or record the same thing twice.

What this protects (DATA audit, write-route idempotency):
  - schedule publish emails each member of staff once, and the delayed
    auto-publish re-checks published / edited state when it runs (DATA-12,
    Async Jobs appendix #13)
  - a guest SMS campaign texts each guest once, however many requests are
    in flight (DATA-13)
  - concurrent newsletters never leave a guest with a dead unsubscribe link
    (DATA-14)
  - a supplier purchase order is not re-sent after a restart or after the
    60-second cooldown (DATA-15)
  - a double-submitted Instagram post publishes once (DATA-25)
  - a review approved twice fires its side effects once, and a draft save
    never un-posts a live reply (DATA-26)
  - a double-submitted food-cost quick count keeps last week's baseline
    (DATA-27)
  - identical time-off / drop requests become one row (DATA-35)
  - creating the same staff member twice is refused (DATA-37)
  - a password reset token works once under concurrency (DATA-50)
  - the owner's send delay (the undo window) is honoured on web AND phone
    (DATA-41, Settings appendix #14)

Tests without a marker pin behaviour that works today. Tests marked
xfail(strict=True) assert the CORRECT behaviour for a defect confirmed in the
DATA audit; they flip to a failure the day the defect is fixed, so the marker
is removed with the fix.

Races use real threads against the test's own SQLite file. Where the race
window is a statement, a gated connection holds each thread just before
that statement until both threads are there, so both are inside the window
on every run; where it is an outbound call, the stubbed sender waits on a
barrier instead. Every sender (email, SMS, Graph API, webhooks) is stubbed
and counted; nothing reaches the network.
"""
import datetime as dt
import json
import sys
import threading
from datetime import datetime, timedelta, timezone

import pytest
from flask import Flask

import auth
import client_api
import delayed
import emails
import guest_email
import guest_marketing
import inventory
import labor
import mobile_api
import models
import notify
import push
import schedule_versions
import shift_requests
import social_routes
import time_off
import webhooks
from auth import create_session, create_user, init_auth, upsert_membership
from models import Restaurant, create_restaurant

CSRF = "edge-data-idem-csrf"
HEADER = "date,day,employee,role,shift_start,shift_end,scheduled_hours,notes\n"
WEEK = ["2026-10-05", "2026-10-06", "2026-10-07", "2026-10-08", "2026-10-09", "2026-10-10", "2026-10-11"]
CLEAN = (HEADER + f"{WEEK[0]},Monday,Ana,Server,4:00pm,10:00pm,6.0,\n"
         f"{WEEK[1]},Tuesday,Bob,Server,4:00pm,10:00pm,6.0,\n")
TODAY = dt.date(2026, 9, 28)


# ── race gate ───────────────────────────────────────────────────────────────

_ACTIVE_GATE = {}


class _Gate:
    """Holds each thread that reaches a statement containing `needle` until
    `parties` threads are there, once per thread. A barrier timeout (the
    fixed code may never let the loser reach the statement) just lets the
    waiting thread carry on."""

    def __init__(self, needle, parties=2, timeout=1.0):
        self.needle = " ".join(needle.split()).lower()
        self.barrier = threading.Barrier(parties, timeout=timeout)
        self.local = threading.local()

    def reached(self, sql):
        if getattr(self.local, "done", False):
            return
        if self.needle in " ".join(str(sql).split()).lower():
            self.local.done = True
            try:
                self.barrier.wait()
            except threading.BrokenBarrierError:
                pass


class _GatedConn:
    """A sqlite3 connection that consults the active gate before execute."""

    def __init__(self, conn, gate):
        object.__setattr__(self, "_conn", conn)
        object.__setattr__(self, "_gate", gate)

    def execute(self, sql, *a, **k):
        self._gate.reached(sql)
        return self._conn.execute(sql, *a, **k)

    def executemany(self, sql, *a, **k):
        self._gate.reached(sql)
        return self._conn.executemany(sql, *a, **k)

    def __getattr__(self, name):
        return getattr(self._conn, name)

    def __setattr__(self, name, value):
        setattr(self._conn, name, value)

    def __enter__(self):
        self._conn.__enter__()
        return self

    def __exit__(self, *exc):
        return self._conn.__exit__(*exc)


def _run_together(*fns):
    """Run each callable on its own thread; return their results (or the
    exception each raised) in order."""
    out = [None] * len(fns)

    def runner(i, fn):
        try:
            out[i] = fn()
        except BaseException as e:           # the loser of a race may raise
            out[i] = e

    threads = [threading.Thread(target=runner, args=(i, fn)) for i, fn in enumerate(fns)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)
    assert not any(t.is_alive() for t in threads), "a racing thread never finished"
    return out


# ── fixtures ────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _db(db_path, monkeypatch):
    """Every module that bound get_conn at import (CLAUDE.md "Bound
    imports") is pointed at this test's database — the named ones, and any
    other loaded module still holding the real function."""
    real = models.get_conn

    def redirect(*a, **k):
        conn = real(db_path)
        gate = _ACTIVE_GATE.get("gate")
        return _GatedConn(conn, gate) if gate else conn

    named = (models, auth, client_api, mobile_api, delayed, guest_marketing, guest_email,
             shift_requests, time_off, schedule_versions, labor, inventory, notify, push,
             webhooks, social_routes)
    for mod in named:
        monkeypatch.setattr(mod, "get_conn", redirect, raising=False)
    for mod in list(sys.modules.values()):
        try:
            if mod is not None and getattr(mod, "get_conn", None) is real:
                monkeypatch.setattr(mod, "get_conn", redirect)
        except Exception:
            pass
    for mod in (models, auth, delayed, guest_marketing, guest_email, shift_requests, time_off,
                schedule_versions):
        monkeypatch.setattr(mod, "DB_PATH", db_path, raising=False)
    monkeypatch.setattr(models, "_cached_shifts", lambda r: [], raising=False)
    init_auth(db_path=db_path)
    push.init_push(db_path=db_path)
    webhooks.init_webhooks(db_path=db_path)
    guest_marketing.init_guest_marketing(db_path=db_path)
    _ACTIVE_GATE.clear()
    yield
    _ACTIVE_GATE.clear()


def _arm(needle, parties=2):
    _ACTIVE_GATE["gate"] = _Gate(needle, parties=parties)


@pytest.fixture
def app():
    flask_app = Flask(__name__, template_folder="../templates")
    flask_app.register_blueprint(client_api.client_bp)
    flask_app.register_blueprint(mobile_api.mobile_bp)
    return flask_app


def _restaurant(db_path, **kw):
    kw.setdefault("name", "Edge Data Co")
    kw.setdefault("owner_email", "owner@edge.test")
    kw.setdefault("timezone", "America/Chicago")
    kw.setdefault("module_labor", 1)
    kw.setdefault("module_inventory", 1)
    return create_restaurant(Restaurant(**kw), db_path=db_path)


def _owner(db_path, rid, username="owner"):
    uid = create_user(rid, username, f"{username}@edge.test", "pw-edge-123", db_path=db_path)
    upsert_membership(uid, rid, "client", db_path=db_path)
    return uid


def _web(app, db_path, uid):
    c = app.test_client()
    c.set_cookie("session_token", create_session(uid, db_path=db_path))
    c.set_cookie("csrf_js", CSRF)
    return c


def _bearer(db_path, uid):
    return {"Authorization": "Bearer " + create_session(uid, device_type="ios", db_path=db_path)}


def _week(db_path, rid, csv_text=CLEAN, published=False):
    conn = models.get_conn(db_path)
    models._ensure_history_columns(conn)
    quality = json.dumps({"checked": True, "band": "solid", "score": 80, "confidence": {"level": "high"}})
    cur = conn.execute(
        "INSERT INTO schedule_history (restaurant_id, week_start, week_end, hours_scheduled, hours_budget, "
        "labor_target, schedule_csv, summary_json, quality_json, published_at) VALUES (?,?,?,?,?,?,?,'[]',?,?)",
        (rid, WEEK[0], WEEK[6], 12, 40, 30, csv_text, quality,
         "2026-10-01 10:00:00" if published else None))
    conn.commit()
    hid = cur.lastrowid
    conn.close()
    return hid


def _staff_contacts(db_path, rid, people=(("Ana", "ana@staff.test"), ("Bob", "bob@staff.test"))):
    conn = models.get_conn(db_path)
    for name, email in people:
        conn.execute("INSERT OR REPLACE INTO staff_contacts (restaurant_id, employee_name, email) VALUES (?,?,?)",
                     (rid, name, email))
    conn.commit()
    conn.close()


@pytest.fixture
def staff_mail(monkeypatch):
    sent = []
    monkeypatch.setattr(emails, "send_staff_schedule_email",
                        lambda **kw: sent.append(kw["employee_name"]) or {"id": "e"})
    monkeypatch.setattr(client_api, "log_account_event", lambda *a, **k: None)
    return sent


def _ingredient(db_path, rid, name="Romaine", supplier_name="Fresh Co", supplier_email="orders@fresh.test"):
    conn = models.get_conn(db_path)
    conn.execute("""
        INSERT INTO ingredients (restaurant_id, name, category, unit, par_level, unit_cost,
                                 case_size, current_stock, avg_daily_usage, last_order_qty,
                                 waste_last_week, is_active, supplier_name, supplier_email)
        VALUES (?,?,?,?,10,4.0,1,0,3,0,0,1,?,?)
    """, (rid, name, "Produce", "lb", supplier_name, supplier_email))
    conn.commit()
    conn.close()


@pytest.fixture
def supplier_mail(monkeypatch):
    sent = []
    monkeypatch.setattr(emails, "send_supplier_order_email",
                        lambda **kw: sent.append(kw["to_email"]) or {"id": "e"})
    return sent


# ── DATA-12 · schedule publish ──────────────────────────────────────────────

def test_publishing_the_same_week_twice_emails_each_member_of_staff_once(db_path, staff_mail):
    rid = _restaurant(db_path)
    _staff_contacts(db_path, rid)
    hid = _week(db_path, rid)
    owner = {"id": 1, "username": "owner"}

    first, s1 = client_api._publish_schedule(rid, hid, owner)
    assert s1 == 200 and first["ok"] and sorted(staff_mail) == ["Ana", "Bob"]

    client_api._publish_schedule(rid, hid, owner)      # the double-click / the second device
    assert sorted(staff_mail) == ["Ana", "Bob"]


def test_the_auto_publish_after_a_manual_publish_sends_nothing(db_path, staff_mail):
    rid = _restaurant(db_path, auto_publish_schedule=1)
    _staff_contacts(db_path, rid)
    hid = _week(db_path, rid)
    # Friday 9am: the automation queues the untouched draft for 11am.
    delayed.schedule(rid, "schedule_publish", {"schedule_id": hid}, 120, db_path=db_path)
    # 10am: the owner publishes it by hand.
    out, status = client_api._publish_schedule(rid, hid, {"id": 1, "username": "owner"})
    assert status == 200 and out["ok"]
    assert sorted(staff_mail) == ["Ana", "Bob"]

    later = datetime.now(timezone.utc) + timedelta(hours=3)
    delayed.run_due(db_path=db_path, now=later)
    assert sorted(staff_mail) == ["Ana", "Bob"], "the 11am action emailed staff a second time"


def test_an_auto_publish_queued_before_an_edit_sends_nothing_once_the_week_is_edited(db_path, staff_mail):
    """Async Jobs appendix #13: the queue-time checks (unedited, unpublished)
    are two hours old when the action runs."""
    rid = _restaurant(db_path, auto_publish_schedule=1)
    _staff_contacts(db_path, rid)
    hid = _week(db_path, rid)
    delayed.schedule(rid, "schedule_publish", {"schedule_id": hid}, 120, db_path=db_path)
    edited = CLEAN.replace("Bob,Server,4:00pm", "Bob,Server,5:00pm")
    models.update_schedule_history_rows(rid, edited, history_id=hid, edited_by="manager", db_path=db_path)

    delayed.run_due(db_path=db_path, now=datetime.now(timezone.utc) + timedelta(hours=3))
    assert staff_mail == [], "a week the manager changed after it was queued went out under the 'unchanged' promise"


# ── DATA-41 · the send delay on both surfaces ───────────────────────────────

def test_the_web_publish_honours_the_send_delay(app, db_path, staff_mail):
    rid = _restaurant(db_path)
    models.update_restaurant(rid, {"send_delay_minutes": 10}, db_path=db_path)
    _staff_contacts(db_path, rid)
    _week(db_path, rid)
    c = _web(app, db_path, _owner(db_path, rid))
    body = c.post("/api/labor/publish-schedule", json={}, headers={"X-CSRF": CSRF}).get_json()
    assert body["ok"] and body["queued"] is True and body["undo_minutes"] == 10
    assert staff_mail == []
    assert [a["kind"] for a in delayed.pending(rid, db_path=db_path)] == ["schedule_publish"]


@pytest.mark.xfail(strict=True, reason="DATA-41: mobile publish-schedule never reads send_delay_minutes, so the phone skips the undo window")
def test_the_phone_publish_honours_the_send_delay_too(app, db_path, staff_mail):
    rid = _restaurant(db_path)
    models.update_restaurant(rid, {"send_delay_minutes": 10}, db_path=db_path)
    _staff_contacts(db_path, rid)
    _week(db_path, rid)
    h = _bearer(db_path, _owner(db_path, rid))
    app.test_client().post("/mobile/api/labor/publish-schedule", json={}, headers=h)
    assert staff_mail == [], "the phone sent to staff inside the owner's undo window"
    assert [a["kind"] for a in delayed.pending(rid, db_path=db_path)] == ["schedule_publish"]


@pytest.mark.xfail(strict=True, reason="DATA-41: mobile send-order never reads send_delay_minutes, so a supplier order from the phone goes out instantly")
def test_the_phone_supplier_order_honours_the_send_delay_too(app, db_path, supplier_mail):
    rid = _restaurant(db_path)
    models.update_restaurant(rid, {"send_delay_minutes": 10}, db_path=db_path)
    _ingredient(db_path, rid)
    h = _bearer(db_path, _owner(db_path, rid))
    app.test_client().post("/mobile/api/food-cost/send-order", json={}, headers=h)
    assert supplier_mail == [], "the phone emailed the supplier inside the owner's undo window"
    assert [a["kind"] for a in delayed.pending(rid, db_path=db_path)] == ["order_send"]


# ── DATA-15 · supplier orders ───────────────────────────────────────────────

def test_a_second_send_inside_the_cooldown_is_refused(app, db_path, supplier_mail):
    rid = _restaurant(db_path)
    _ingredient(db_path, rid)
    c = _web(app, db_path, _owner(db_path, rid))
    assert c.post("/api/food-cost/send-order", json={}, headers={"X-CSRF": CSRF}).get_json()["ok"] is True
    again = c.post("/api/food-cost/send-order", json={}, headers={"X-CSRF": CSRF})
    assert again.status_code == 429
    assert supplier_mail == ["orders@fresh.test"]


@pytest.mark.xfail(strict=True, reason="DATA-15: the only guard is the process-local _order_send_last, so a restart forgets the order was sent and the same draft is emailed again")
def test_the_same_order_is_not_sent_twice_across_a_restart(app, db_path, supplier_mail):
    rid = _restaurant(db_path)
    _ingredient(db_path, rid)
    c = _web(app, db_path, _owner(db_path, rid))
    draft_hash = c.get("/api/food-cost/order-draft").get_json()["draft_hash"]
    assert c.post("/api/food-cost/send-order", json={"draft_hash": draft_hash},
                  headers={"X-CSRF": CSRF}).get_json()["ok"] is True
    client_api._order_send_last.clear()             # a deploy / restart
    c.post("/api/food-cost/send-order", json={"draft_hash": draft_hash}, headers={"X-CSRF": CSRF})
    assert supplier_mail == ["orders@fresh.test"], "the supplier received the same purchase order twice"
    assert len(models.get_purchase_orders(rid, db_path=db_path)) == 1


@pytest.mark.xfail(strict=True, reason="DATA-15: the draft does not net out open purchase orders, so the same order re-sends once the 60-second cooldown passes")
def test_the_same_order_is_not_sent_again_once_the_cooldown_passes(app, db_path, supplier_mail, monkeypatch):
    rid = _restaurant(db_path)
    _ingredient(db_path, rid)
    c = _web(app, db_path, _owner(db_path, rid))
    draft_hash = c.get("/api/food-cost/order-draft").get_json()["draft_hash"]
    c.post("/api/food-cost/send-order", json={"draft_hash": draft_hash}, headers={"X-CSRF": CSRF})
    import time as _time
    later = _time.monotonic() + client_api._ORDER_SEND_COOLDOWN + 5
    monkeypatch.setattr(_time, "monotonic", lambda: later)
    c.post("/api/food-cost/send-order", json={"draft_hash": draft_hash}, headers={"X-CSRF": CSRF})
    assert supplier_mail == ["orders@fresh.test"]


# ── DATA-13 · guest SMS campaign ────────────────────────────────────────────

def test_two_concurrent_campaign_sends_text_each_guest_once(db_path, monkeypatch):
    rid = _restaurant(db_path)
    phones = ["+15550001001", "+15550001002"]
    for p in phones:
        guest_marketing.add_guest_contact_public_optin(rid, p, db_path=db_path)
    monkeypatch.setattr(guest_marketing, "_sms_local_now",
                        lambda r: datetime.now().replace(hour=12, minute=0))
    both_sending = threading.Barrier(2, timeout=1.0)
    local = threading.local()
    texts, lock = [], threading.Lock()

    def fake_sms(phone, message, *a, **k):
        if not getattr(local, "waited", False):          # both requests are inside the send loop
            local.waited = True
            try:
                both_sending.wait()
            except threading.BrokenBarrierError:
                pass
        with lock:
            texts.append(notify._normalize_phone(phone))
        return True

    monkeypatch.setattr(guest_marketing, "send_sms", fake_sms)
    send = lambda: guest_marketing.send_campaign(rid, "Half-price wine tonight", db_path=db_path)
    _run_together(send, send)
    assert sorted(texts) == sorted(notify._normalize_phone(p) for p in phones), \
        f"guests were texted {len(texts)} times for {len(phones)} guests"


# ── DATA-14 · guest newsletter ──────────────────────────────────────────────

@pytest.mark.xfail(strict=True, reason="DATA-14: concurrent newsletters each mint and write their own unsubscribe tokens; the last writer wins and the other batch's links are dead")
def test_concurrent_newsletters_keep_every_unsubscribe_link_valid(db_path, monkeypatch):
    rid = _restaurant(db_path)
    for i, addr in enumerate(("ann@guest.test", "ben@guest.test")):
        cid = guest_marketing.add_guest_contact_public_optin(rid, f"+1555000200{i}", db_path=db_path)
        guest_email.set_guest_email(cid, rid, addr, consent=True, db_path=db_path)
    # Contacts from before tokens were minted at consent: the send mints them.
    conn = models.get_conn(db_path)
    conn.execute("UPDATE guest_contacts SET email_token=NULL WHERE restaurant_id=?", (rid,))
    conn.commit()
    conn.close()

    real_subscribers = guest_email.subscribers
    both_read = threading.Barrier(2, timeout=1.0)

    def subscribers(*a, **k):
        people = real_subscribers(*a, **k)
        try:
            both_read.wait()                    # both sends have read the list before either writes
        except threading.BrokenBarrierError:
            pass
        return people

    monkeypatch.setattr(guest_email, "subscribers", subscribers)
    links, lock = [], threading.Lock()

    def deliver(payload, **k):
        with lock:
            links.append(payload["headers"]["List-Unsubscribe"].strip("<>").rsplit("/e/", 1)[1])
        return True

    monkeypatch.setattr(emails, "deliver", deliver)
    send = lambda: guest_email.send_newsletter(rid, "BODY:\nTruffle season.", subject="News", db_path=db_path)
    _run_together(send, send)
    assert links, "nothing was sent"
    conn = models.get_conn(db_path)
    dead = [t for t in links
            if not conn.execute("SELECT 1 FROM guest_contacts WHERE email_token=?", (t,)).fetchone()]
    conn.close()
    assert dead == [], f"{len(dead)} of {len(links)} unsubscribe links point at no guest"


# ── DATA-25 · public posts ──────────────────────────────────────────────────

class _Resp:
    def __init__(self, body, status=200):
        self._body, self.status_code, self.text = body, status, json.dumps(body)

    def json(self):
        return self._body


@pytest.mark.xfail(strict=True, reason="DATA-25: immediate Instagram publishes carry no idempotency claim, so a re-click during the 20-second poll makes a second public post")
def test_a_double_submitted_instagram_post_publishes_once(db_path, monkeypatch):
    rid = _restaurant(db_path)
    conn = models.get_conn(db_path)
    conn.execute("UPDATE restaurants SET ig_token='tok', ig_user_id='ig1' WHERE id=?", (rid,))
    conn.commit()
    conn.close()
    import requests
    import time as _time
    monkeypatch.setattr(_time, "sleep", lambda s: None)
    both_in_flight = threading.Barrier(2, timeout=1.0)
    local = threading.local()
    published, lock = [], threading.Lock()

    def fake_post(url, *a, **k):
        if url.endswith("/media_publish"):
            with lock:
                published.append(k.get("data", {}).get("creation_id"))
            return _Resp({"id": f"post{len(published)}"})
        if url.endswith("/media"):
            if not getattr(local, "waited", False):
                local.waited = True
                try:
                    both_in_flight.wait()        # the owner re-clicks while the first is processing
                except threading.BrokenBarrierError:
                    pass
            return _Resp({"id": "container1"})
        raise AssertionError(f"unexpected POST {url}")

    monkeypatch.setattr(requests, "post", fake_post)
    monkeypatch.setattr(requests, "get", lambda url, *a, **k: _Resp({"status_code": "FINISHED"}))
    post = lambda: social_routes._do_post_to_instagram(rid, "Truffle season", "https://img.test/a.jpg", "")
    _run_together(post, post)
    assert len(published) == 1, f"{len(published)} public posts from one intended post"


# ── DATA-26 · review approve ────────────────────────────────────────────────

def _drafted_review(db_path, rid, status="drafted"):
    models.save_reviews([models.Review(restaurant_id=rid, platform="google", external_id="rv1",
                                       author="Ann", rating=2, text="Slow service.")], db_path=db_path)
    conn = models.get_conn(db_path)
    conn.execute("UPDATE reviews SET draft_response='Thanks, Ann.', response_status=?, processed=1, "
                 "review_name='accounts/1/locations/2/reviews/rv1' WHERE restaurant_id=?", (status, rid))
    conn.commit()
    review_id = conn.execute("SELECT id FROM reviews WHERE restaurant_id=?", (rid,)).fetchone()["id"]
    conn.close()
    return review_id


def test_approving_twice_fires_the_webhook_once(db_path, monkeypatch):
    rid = _restaurant(db_path)
    review_id = _drafted_review(db_path, rid)
    fired = {"webhook": 0, "google": 0, "alert": 0}
    monkeypatch.setattr(webhooks, "fire_webhook",
                        lambda r, ev, data, *a, **k: fired.__setitem__("webhook", fired["webhook"] + 1))
    monkeypatch.setattr(client_api, "_attempt_google_post",
                        lambda *a, **k: (fired.__setitem__("google", fired["google"] + 1), (False, None))[1])
    monkeypatch.setattr(notify, "fire_response_approved_alert",
                        lambda *a, **k: fired.__setitem__("alert", fired["alert"] + 1))

    assert client_api._do_approve(review_id, rid)[1] == 200
    client_api._do_approve(review_id, rid)             # the second tab / the retry
    assert fired == {"webhook": 1, "google": 1, "alert": 1}


@pytest.mark.xfail(strict=True, reason="DATA-26: _do_save_draft sets response_status='drafted' unconditionally, stranding a live Google reply that retract then refuses")
def test_saving_a_draft_on_a_posted_reply_leaves_it_posted(db_path):
    rid = _restaurant(db_path)
    review_id = _drafted_review(db_path, rid, status="posted")
    client_api._do_save_draft(review_id, rid, "A different reply")
    conn = models.get_conn(db_path)
    status = conn.execute("SELECT response_status FROM reviews WHERE id=?", (review_id,)).fetchone()[0]
    conn.close()
    assert status == "posted", "the live reply is now marked a draft and can no longer be retracted"


# ── DATA-27 · food-cost quick count ─────────────────────────────────────────

@pytest.mark.xfail(strict=True, reason="DATA-27: every quick count rotates current into previous, so a double-submit replaces last week's baseline with this week's count")
def test_submitting_the_same_count_twice_keeps_the_previous_week(db_path):
    rid = _restaurant(db_path)
    last_week = {"current": {"submitted_at": "2026-09-14",
                             "items": [{"name": "Romaine", "unit": "case", "price": 4.0, "usage": 10}]}}
    conn = models.get_conn(db_path)
    conn.execute("INSERT INTO client_data (restaurant_id, food_cost_json) VALUES (?,?)",
                 (rid, json.dumps(last_week)))
    conn.commit()
    conn.close()
    count = [{"name": "Romaine", "unit": "case", "price": 5.0, "usage": 10}]

    first, _ = client_api._do_food_cost_quickcount(rid, count)
    assert first["ok"] and first["drift"][0]["prev_price"] == 4.0
    second, _ = client_api._do_food_cost_quickcount(rid, count)       # the double-submit

    stored = json.loads(models.get_client_data(rid, db_path=db_path)["food_cost_json"])
    assert stored["previous"]["items"][0]["price"] == 4.0, "last week's prices are gone"
    assert second["drift"] and second["drift"][0]["prev_price"] == 4.0


# ── DATA-35 · staff request inserts ─────────────────────────────────────────

@pytest.mark.xfail(strict=True, reason="DATA-35: request_time_off is SELECT-then-INSERT with no UNIQUE, so two concurrent taps make two requests")
def test_two_identical_time_off_requests_become_one(db_path):
    rid = _restaurant(db_path)
    _arm("INSERT INTO staff_time_off")
    ask = lambda: time_off.request_time_off(rid, "Ana", "2026-10-12", "2026-10-15", reason="wedding",
                                            db_path=db_path, today=TODAY)
    _run_together(ask, ask)
    _ACTIVE_GATE.clear()
    conn = models.get_conn(db_path)
    n = conn.execute("SELECT COUNT(*) FROM staff_time_off WHERE restaurant_id=?", (rid,)).fetchone()[0]
    conn.close()
    assert n == 1, f"{n} pending requests for one ask"


@pytest.mark.xfail(strict=True, reason="DATA-35: request_drop is SELECT-then-INSERT with no UNIQUE, so two concurrent taps make two drop requests")
def test_two_identical_drop_requests_become_one(db_path):
    rid = _restaurant(db_path)
    _week(db_path, rid, published=True)
    _arm("INSERT INTO shift_change_requests")
    ask = lambda: shift_requests.request_drop(rid, "Ana", WEEK[0], "4:00pm", db_path=db_path, today=TODAY)
    _run_together(ask, ask)
    _ACTIVE_GATE.clear()
    assert len(shift_requests.for_manager(rid, db_path=db_path)) == 1


# ── DATA-37 · staff account creation ────────────────────────────────────────

@pytest.mark.xfail(strict=True, reason="DATA-37: a username collision invents a new suffix, so a double-submit creates a second identity with the same name and PIN")
def test_creating_the_same_staff_member_twice_is_refused(app, db_path):
    rid = _restaurant(db_path)
    c = _web(app, db_path, _owner(db_path, rid))
    body = {"employee_name": "Jordan P.", "job_role": "Bartender", "pin": "8317"}
    first = c.post("/api/account/staff", json=body, headers={"X-CSRF": CSRF})
    assert first.get_json()["ok"] is True
    second = c.post("/api/account/staff", json=body, headers={"X-CSRF": CSRF})
    conn = models.get_conn(db_path)
    n = conn.execute("SELECT COUNT(*) FROM memberships WHERE restaurant_id=? AND employee_name='Jordan P.'",
                     (rid,)).fetchone()[0]
    conn.close()
    assert n == 1, "two staff identities named Jordan P."
    assert second.status_code in (400, 409)


# ── DATA-50 · password reset tokens ─────────────────────────────────────────

@pytest.mark.xfail(strict=True, reason="DATA-50: consume_reset_token validates with a SELECT and updates WHERE id=?, so two concurrent submissions both succeed")
def test_a_reset_token_works_once_under_concurrency(db_path):
    rid = _restaurant(db_path)
    create_user(rid, "resetme", "reset@edge.test", "old-password-1", db_path=db_path)
    token = models.create_reset_token("reset@edge.test", db_path=db_path)
    assert token
    _arm("UPDATE users SET password_hash")
    results = _run_together(lambda: models.consume_reset_token(token, "first-new-pass-1", db_path=db_path),
                            lambda: models.consume_reset_token(token, "second-new-pass-2", db_path=db_path))
    _ACTIVE_GATE.clear()
    assert sorted(r is True for r in results) == [False, True], results


def test_a_reset_token_is_refused_the_second_time_in_sequence(db_path):
    rid = _restaurant(db_path)
    create_user(rid, "resetme", "reset@edge.test", "old-password-1", db_path=db_path)
    token = models.create_reset_token("reset@edge.test", db_path=db_path)
    assert models.consume_reset_token(token, "first-new-pass-1", db_path=db_path) is True
    assert models.consume_reset_token(token, "second-new-pass-2", db_path=db_path) is False
