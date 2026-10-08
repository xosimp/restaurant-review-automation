"""
push.py — APNs (Apple Push Notification service) delivery for the Cavnar AI
iOS app.

Modeled directly on webhooks.py's proven delivery pattern (retry-with-
backoff, per-delivery logging, auto-disable after repeated failures) — the
two real differences are: APNs requires HTTP/2 (requests has no HTTP/2
support, hence httpx here), and APNs' own "this token will never work again"
signal (410 / BadDeviceToken / Unregistered) gets an immediate delete rather
than waiting out the failure counter, since retrying a dead token is pure
waste.
"""
import json
import os
import threading
import time
import uuid
from datetime import datetime, timezone

from models import get_conn, DB_PATH

# Same meaning as webhooks.py's _AUTO_DISABLE_AFTER: a token that's failed
# this many consecutive deliveries for reasons OTHER than the fast-path
# "definitely dead" APNs responses below gets parked too — no visibility
# into a broken token otherwise, and nothing to gain from retrying forever.
_AUTO_DISABLE_AFTER = 10

# APNs responses that mean "this token will never be valid again" — no point
# waiting for _AUTO_DISABLE_AFTER consecutive failures, delete on the first.
_PERMANENT_FAILURE_REASONS = {"BadDeviceToken", "Unregistered", "DeviceTokenNotForTopic"}

# Failures that are OURS, not the token's: a bad or expired signing key, a
# missing/incorrect apns-topic, a rejected environment, an APNs outage, a
# network drop. None of these say anything about the device, so none of them
# may advance the failure counter.
#
# This is the P0 from the notifications audit. _AUTO_DISABLE_AFTER used to
# count every non-permanent failure, and one fire_push is one consecutive
# failure — so ten alerts sent while the .p8 was expired, or while
# APNS_BUNDLE_ID was unset (it defaulted to ""), deleted EVERY registered
# device at EVERY restaurant. Push then stayed dead until each owner happened
# to relaunch the app, with nothing anywhere saying why.
_PROVIDER_FAILURE_REASONS = {
    "ExpiredProviderToken", "InvalidProviderToken", "MissingProviderToken",
    "TooManyProviderTokenUpdates", "MissingTopic", "BadTopic", "TopicDisallowed",
    "BadCertificateEnvironment", "BadCertificate", "Forbidden",
    "InternalServerError", "ServiceUnavailable", "TooManyRequests", "Shutdown",
}

# Reasons that mean the cached provider JWT must be thrown away before the
# next attempt. Retrying a 403 with the same dead token just burns the
# remaining attempts.
_JWT_REMINT_REASONS = {"ExpiredProviderToken", "InvalidProviderToken", "TooManyProviderTokenUpdates"}

# Alert types where several genuinely distinct events happen in one day, so
# the date-keyed collapse id would silently overwrite all but the last.
_UNCOLLAPSIBLE_TYPES = {"login", "staff_signin", "issue", "issue_escalated", "coverage",
                        # each is its own staff request or held order (A-6, A-22)
                        "shift_request", "order_send_held",
                        # Every notice to an employee is its own event: "Drop
                        # approved" and "You picked up a shift" on one day used
                        # to share a date-keyed collapse id, so the second
                        # replaced the first on the lock screen (COM-13).
                        "staff_schedule", "staff_request", "staff_notice", "staff_reminder",
                        "staff_announcement", "staff_urgent", "staff_message",
                        # each message an employee sends the managers
                        "employee_message"}

# ── The staff tier (employee audit C4, 10/1/26) ─────────────────────────────
#
# A device registered from the staff app (POST /staff/api/device-tokens) is
# filed with tier='staff'. The fan-out was owner-first: a restaurant-wide
# push (user_ids=None) — the "New sign-in" alert with its IP, "Dana opened
# the staff portal", every brief and alert — selected every active login's
# phone at the restaurant, so the day staff phones registered, owner alerts
# would have reached employees' lock screens (COM-03). Now a staff device is
# reached only when the caller NAMES its login (user_ids), and only by one of
# these types, whatever user_ids says.
TIER_OWNER, TIER_STAFF = "owner", "staff"
STAFF_ALERT_TYPES = frozenset({
    "staff_schedule",      # a week posted or changed
    "staff_request",       # a request decided, a swap asked of them, an open shift offered
    "staff_notice",        # anything else addressed to one employee
    "staff_reminder",      # their shift starts soon, a critical task is due (staff_reminders)
    "staff_announcement",  # a manager's note to the team
    "staff_urgent",        # an urgent announcement: breaks Focus (P1)
    "staff_message",       # a reply in their thread with the manager on duty
})
# The staff app's tabs a notice may open (`tab` in the payload).
STAFF_TABS = ("today", "tasks", "requests", "me", "inbox")
STAFF_DEFAULT_TAB = {
    "staff_schedule": "today", "staff_request": "requests", "staff_notice": "inbox",
    "staff_reminder": "today", "staff_announcement": "inbox", "staff_urgent": "inbox",
    "staff_message": "inbox",
}
# The id a staff nav path names after its tab ("staff/requests/12").
_STAFF_NAV_IDS = ("request_id", "announcement_id", "thread_id", "assignment_id")


class PushNotConfigured(RuntimeError):
    """APNS_KEY_ID / APNS_TEAM_ID / APNS_PRIVATE_KEY missing. Raised rather
    than signing with an empty key, so the failure names itself instead of
    arriving as a wall of 403s that look like dead devices."""

_SCHEMA = """
CREATE TABLE IF NOT EXISTS device_tokens (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id              INTEGER NOT NULL REFERENCES users(id),
    restaurant_id        INTEGER NOT NULL REFERENCES restaurants(id),
    apns_token           TEXT NOT NULL UNIQUE,
    environment          TEXT NOT NULL DEFAULT 'production',
    created_at           TEXT NOT NULL DEFAULT (datetime('now')),
    last_success_at      TEXT,
    consecutive_failures INTEGER NOT NULL DEFAULT 0,
    disabled_reason      TEXT,
    -- 'owner' (the console app) or 'staff' (the staff app, a PIN session).
    tier                 TEXT NOT NULL DEFAULT 'owner'
);
CREATE TABLE IF NOT EXISTS push_deliveries (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    device_token_id INTEGER NOT NULL,
    restaurant_id   INTEGER NOT NULL,
    alert_type      TEXT NOT NULL,
    status          INTEGER,
    ok              INTEGER NOT NULL,
    attempts        INTEGER NOT NULL,
    error           TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now'))
);
-- ops.prune_ledgers deletes by created_at (DATA-40).
CREATE INDEX IF NOT EXISTS idx_push_deliveries_created ON push_deliveries(created_at);
-- Both are read on every alert: fire_push looks up the restaurant's devices,
-- brief_pushed_today asks whether this morning's brief already went out.
-- Neither had an index, so both were full scans of tables that only grow
-- (MOD-NOT-13).
CREATE INDEX IF NOT EXISTS idx_device_tokens_restaurant ON device_tokens(restaurant_id);
CREATE INDEX IF NOT EXISTS idx_push_deliveries_restaurant
    ON push_deliveries(restaurant_id, alert_type, created_at);
-- One row per device per push, written BEFORE it is handed to the pool
-- (#75): the pool and its overflow queue live in process memory, so a deploy
-- during the 7am brief burst lost every queued push with no row anywhere.
-- state: queued -> delivering -> sent | failed; 'expired' when a restart
-- left it longer than it is worth delivering. push_deliveries stays the
-- ledger of attempts made; this is the queue.
CREATE TABLE IF NOT EXISTS push_outbox (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    restaurant_id   INTEGER NOT NULL,
    device_token_id INTEGER NOT NULL,
    alert_type      TEXT NOT NULL,
    title           TEXT,
    body            TEXT,
    data_json       TEXT,
    state           TEXT NOT NULL DEFAULT 'queued',
    attempts        INTEGER NOT NULL DEFAULT 0,
    last_error      TEXT,
    created_at      TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at      TEXT,
    done_at         TEXT
);
CREATE INDEX IF NOT EXISTS idx_push_outbox_state ON push_outbox(state, created_at);
CREATE INDEX IF NOT EXISTS idx_push_outbox_created ON push_outbox(created_at);
"""


def init_push(db_path=DB_PATH):
    """Boot DDL (hosted_dashboard) — never on a request path."""
    conn = get_conn(db_path)
    conn.executescript(_SCHEMA)
    cols = {r[1] for r in conn.execute("PRAGMA table_info(device_tokens)").fetchall()}
    if "tier" not in cols:
        conn.execute("ALTER TABLE device_tokens ADD COLUMN tier TEXT NOT NULL DEFAULT 'owner'")
    conn.execute("CREATE INDEX IF NOT EXISTS idx_device_tokens_user ON device_tokens(user_id, tier)")
    conn.commit()
    conn.close()
    # The staff side of push — the reminder claims and the texts held through
    # the night (staff_reminders) — made with push's own tables, at boot.
    import staff_reminders
    staff_reminders.init_staff_reminders(db_path)


def register_device_token(user_id, restaurant_id, apns_token, environment="production", db_path=DB_PATH,
                          tier=TIER_OWNER):
    """Upsert by apns_token — a reinstall or token rotation just re-points the
    existing row (and clears any prior failure count), same idea as
    webhooks.save_webhook()'s upsert-by-restaurant.

    `tier` is the app that registered it: the console (TIER_OWNER, the
    mobile route) or the staff app (TIER_STAFF, staff_device_routes). One
    phone is one app in one mode, so a phone signed into the staff portal
    moves its row to the staff tier, and back when the owner signs in."""
    tier = TIER_STAFF if tier == TIER_STAFF else TIER_OWNER
    conn = get_conn(db_path)
    existing = conn.execute("SELECT id FROM device_tokens WHERE apns_token=?", (apns_token,)).fetchone()
    if existing:
        conn.execute(
            """UPDATE device_tokens SET user_id=?, restaurant_id=?, environment=?, tier=?,
               consecutive_failures=0, disabled_reason=NULL WHERE apns_token=?""",
            (user_id, restaurant_id, environment, tier, apns_token)
        )
    else:
        conn.execute(
            "INSERT INTO device_tokens (user_id, restaurant_id, apns_token, environment, tier) VALUES (?,?,?,?,?)",
            (user_id, restaurant_id, apns_token, environment, tier)
        )
    conn.commit()
    conn.close()


def unregister_staff_devices(user_id, restaurant_id=None, apns_token=None, db_path=DB_PATH) -> int:
    """Remove a staff login's staff-app devices — the one call for staff
    sign-out, a PIN change or reset, and a deactivation or unlink (employee
    audit C4). Narrowed to one location with `restaurant_id` and to one
    phone with `apns_token`. Never touches an owner-tier row. Returns how
    many rows went. Never raises."""
    try:
        sql = "DELETE FROM device_tokens WHERE user_id=? AND tier=?"
        args = [int(user_id), TIER_STAFF]
        if restaurant_id is not None:
            sql += " AND restaurant_id=?"
            args.append(int(restaurant_id))
        if apns_token:
            sql += " AND apns_token=?"
            args.append(str(apns_token))
        conn = get_conn(db_path)
        try:
            n = conn.execute(sql, args).rowcount or 0
            conn.commit()
        finally:
            conn.close()
        return n
    except Exception as e:
        try:
            import ops
            ops.capture(e, job="staff_device_unregister", context=f"user_id={user_id}", db_path=db_path)
        except Exception:
            pass
        return 0


def staff_device_users(restaurant_id, user_ids=None, db_path=DB_PATH) -> set:
    """The logins with a live device here that a staff notice can reach:
    the staff app's (an active membership here), or the console app's for a
    manager who is also on the schedule. people.reach reads this, so "has
    the app" means what fire_push will actually deliver to."""
    try:
        rows = get_device_tokens(restaurant_id, db_path, for_delivery=True, include_staff=True)
    except Exception:
        return set()
    out = {int(r.get("user_id") or 0) for r in rows if int(r.get("restaurant_id") or 0) == int(restaurant_id)}
    if user_ids is not None:
        out &= {int(u) for u in user_ids if u is not None}
    return out


def console_user_ids(restaurant_id, db_path=DB_PATH) -> list:
    """The logins a restaurant-wide CONSOLE alert is for — a sign-in with
    its IP address, "Dana opened the staff portal": every console login
    with a phone that hears about this restaurant, never an employee's,
    whatever tier its phone registered under (COM-03). Pass it as
    fire_push(user_ids=…)."""
    try:
        ids = {int(t.get("user_id") or 0) for t in get_device_tokens(restaurant_id, db_path, for_delivery=True)}
        if not ids:
            return []
        conn = get_conn(db_path)
        try:
            marks = ",".join("?" * len(ids))
            employees = {r[0] for r in conn.execute(
                f"SELECT u.id FROM users u LEFT JOIN memberships m ON m.user_id=u.id AND m.restaurant_id=? "
                f"WHERE u.id IN ({marks}) AND LOWER(COALESCE(m.role, u.role, '')) = 'employee'",
                (int(restaurant_id), *sorted(ids))).fetchall()}
        finally:
            conn.close()
        return sorted(ids - employees)
    except Exception as e:
        print(f"[push] console audience unreadable rid={restaurant_id}: {e}")
        return []


def token_may_receive(token_row, alert_type) -> bool:
    """A staff-app device receives staff notices only (and a test push) —
    the last gate before delivery, whoever asked: a caller that names an
    employee's login for an owner type still never reaches their phone."""
    try:
        tier = token_row.get("tier")
    except AttributeError:
        tier = None
    if tier == TIER_STAFF:
        return alert_type in STAFF_ALERT_TYPES or alert_type == "test_push"
    return True


def remove_device_token(apns_token, db_path=DB_PATH):
    conn = get_conn(db_path)
    conn.execute("DELETE FROM device_tokens WHERE apns_token=?", (apns_token,))
    conn.commit()
    conn.close()


def get_device_tokens(restaurant_id, db_path=DB_PATH, for_delivery=False, include_staff=False):
    """Every registered device for this restaurant.

    `for_delivery=True` drops the ones parked by repeated failures — sending
    to them is what filled push_deliveries with noise and, before the fix
    above, is what eventually deleted them. A parked token comes back on its
    own the next time the app launches and re-registers (register_device_token
    clears both the counter and the reason).

    Staff-app devices (tier 'staff') are left out unless `include_staff` —
    fire_push passes it only when its caller named the logins (user_ids).
    So every "who has a phone here" reader (the brief's audience, _reach,
    the DSR, the alert channels) means the console's phones, and a
    restaurant-wide alert can never reach an employee (COM-03). A staff
    device is delivered to only while its membership here is active."""
    conn = get_conn(db_path)
    try:
        if not for_delivery:
            sql = "SELECT * FROM device_tokens WHERE restaurant_id=?"
            if not include_staff:
                sql += " AND COALESCE(tier, 'owner') <> 'staff'"
            rows = conn.execute(sql, (restaurant_id,)).fetchall()
            return [dict(r) for r in rows]
        # Delivery is narrower and wider than "registered here":
        # - only a login that is still active. A removed teammate's or a
        #   deactivated login's phone kept receiving the restaurant's alerts
        #   (MOD-NOT-6, DATA-53).
        # - an account holder's phone registered at another location of the
        #   same group (same group name and owner) hears about this one too.
        #   A device belonged to whichever location was open when it
        #   registered, so a multi-location owner heard about one (MOD-NOT-7).
        # One row per phone, even when it registered at several locations.
        who = ("(COALESCE(d.tier, 'owner') <> 'staff' AND ("
               "  d.restaurant_id = ? OR ("
               "    COALESCE(u.role, 'client') IN ('client', 'owner') AND d.restaurant_id IN ("
               "      SELECT o.id FROM restaurants o JOIN restaurants me ON me.id = ? "
               "      WHERE TRIM(COALESCE(me.location_group, '')) <> '' "
               "        AND TRIM(o.location_group) = TRIM(me.location_group) "
               "        AND LOWER(TRIM(o.owner_email)) = LOWER(TRIM(me.owner_email))))))")
        args = [restaurant_id, restaurant_id]
        if include_staff:
            # Only this location's staff devices, and only while the
            # membership they signed in under is active: a deactivated or
            # unlinked employee's phone stops hearing about the restaurant.
            who = (f"({who} OR (d.tier = 'staff' AND d.restaurant_id = ? AND EXISTS ("
                   "SELECT 1 FROM memberships m WHERE m.user_id = d.user_id "
                   "AND m.restaurant_id = d.restaurant_id AND m.is_active = 1)))")
            args.append(restaurant_id)
        rows = conn.execute(
            "SELECT d.* FROM device_tokens d JOIN users u ON u.id = d.user_id "
            f"WHERE d.disabled_reason IS NULL AND u.is_active = 1 AND {who} "
            "ORDER BY (d.restaurant_id = ?) DESC, d.id",
            (*args, restaurant_id)).fetchall()
    finally:
        conn.close()
    out, seen = [], set()
    for r in rows:
        tok = r["apns_token"]
        if tok in seen:
            continue
        seen.add(tok)
        out.append(dict(r))
    return out


# ── Executive priority ──────────────────────────────────────────────────────
#
# Lives here rather than in notify.py because push.py is the lower layer —
# notify, morning_brief, strategy_jobs and both clients all read it, and
# push.py imports nothing of theirs. One number decides three things: whether
# the phone may break a Focus mode, how long APNs keeps trying, and how the
# notification centers rank and badge a row.
P0_CRITICAL   = 0   # someone could get hurt, or the business is at risk now
P1_ACT_NOW    = 1   # fixable while it still matters, and only while it does
P2_OPPORTUNITY= 2   # money on the table, worth today
P3_INFO       = 3   # worth knowing, not worth interrupting for
P4_SUMMARY    = 4   # the brief, the digest, the month
P5_LOW        = 5   # background, never buzzes

PRIORITY = {
    "health": P0_CRITICAL,
    "coverage": P1_ACT_NOW, "issue": P1_ACT_NOW, "issue_escalated": P1_ACT_NOW,
    "critical_low": P1_ACT_NOW,
    "1star": P2_OPPORTUNITY, "2star": P2_OPPORTUNITY, "neg_spike": P2_OPPORTUNITY,
    "edit_downgrade": P2_OPPORTUNITY, "price_spike": P2_OPPORTUNITY,
    "intraday_pulse": P2_OPPORTUNITY, "labor_over": P2_OPPORTUNITY,
    "food_waste": P2_OPPORTUNITY, "rating_threshold": P2_OPPORTUNITY,
    "no_response": P2_OPPORTUNITY, "unresponded": P2_OPPORTUNITY,
    "daily_briefing": P2_OPPORTUNITY,
    "3star": P3_INFO, "5star": P3_INFO, "resp_approved": P3_INFO,
    "schedule_drafted": P3_INFO, "login": P3_INFO, "staff_signin": P3_INFO,
    "negative_trend": P3_INFO, "outcome_achieved": P3_INFO,
    "morning_brief": P4_SUMMARY, "closing_summary": P4_SUMMARY,
    # The nightly Daily Sales Report (dsr.deliver): a summary, like the
    # closing summary it replaces; held through quiet hours, never sounds.
    "dsr": P4_SUMMARY,
    "weekly_review": P4_SUMMARY, "monthly_review": P4_SUMMARY,
    "any_review": P5_LOW, "ai_visibility_drop": P5_LOW, "demand_opportunity": P5_LOW,
    # The afternoon before a game measured big here (event_intel.gameday,
    # Event Intelligence phase 3): worth knowing today, never a Focus break.
    "event_ahead": P3_INFO,
    "competitor_move": P3_INFO, "review_request_nudge": P5_LOW,
    # A staff request waiting on a decision is worth today, not worth
    # breaking a Focus mode for; it used to ride "coverage" at P1 (A-6).
    "shift_request": P2_OPPORTUNITY, "labor_reminder": P3_INFO,
    # Held: the week did not go out and a person has to send it (A-18).
    "schedule_publish_held": P2_OPPORTUNITY, "schedule_publish_pending": P3_INFO,
    "milestone": P3_INFO, "order_send_held": P2_OPPORTUNITY,
    "order_send_pending": P3_INFO, "order_send_voided": P2_OPPORTUNITY,
    # A data source that stopped updating is worth today; its return is news
    # (notify.check_data_source_alerts, Data Freshness #18).
    "data_source_down": P2_OPPORTUNITY, "data_source_restored": P3_INFO,
    # To Will only (ops.alert_will): the scheduler stopped or a job is
    # overdue — every restaurant's data is going stale (DH2-2).
    "platform_alert": P1_ACT_NOW,
    # Notices to one employee (the staff app). Worth today, never a Focus
    # break — except an announcement the manager marked urgent ("storm:
    # don't come in"). They had no entry, so P3, and opened Reviews (COM-13).
    "staff_schedule": P2_OPPORTUNITY, "staff_request": P2_OPPORTUNITY, "staff_notice": P2_OPPORTUNITY,
    "staff_reminder": P2_OPPORTUNITY, "staff_announcement": P2_OPPORTUNITY, "staff_message": P2_OPPORTUNITY,
    "staff_urgent": P1_ACT_NOW,
    # The other direction, to the console: an employee wrote to the manager
    # on duty (staff_comms). A task like a staff request, never a Focus break.
    "employee_message": P2_OPPORTUNITY,
}
# Which module a notification opens — the web tab ids (?tab=). The ONE map:
# client_api._NOTIFICATION_MODULE is this dict (the bell's rows carry it),
# and every push payload carries `module` from it, so iOS routes on what the
# server says instead of a two-type copy that sent every food-cost, intel,
# coverage and order push to Reviews (re-audit A-7). iOS keeps a mirror
# only as the fallback for an old payload.
NOTIFICATION_MODULE = {
    "1star": "reviews", "2star": "reviews", "3star": "reviews", "5star": "reviews",
    "any_review": "reviews", "health": "reviews", "edit_downgrade": "reviews",
    "resp_approved": "reviews", "neg_spike": "reviews", "no_response": "reviews",
    "unresponded": "reviews", "negative_trend": "reviews", "rating_threshold": "reviews",
    "labor_over": "labor", "schedule_drafted": "labor", "coverage": "labor",
    "schedule_publish_pending": "labor", "schedule_publish_held": "labor",
    "shift_request": "labor", "labor_reminder": "labor", "employee_message": "labor",
    "food_waste": "inventory", "critical_low": "inventory", "price_spike": "inventory",
    "order_send_pending": "inventory", "order_send_held": "inventory", "order_send_voided": "inventory",
    "ai_visibility_drop": "competitor", "competitor_move": "competitor",
    "review_request_nudge": "reviews",
    "demand_opportunity": "marketing",
    # A big game tomorrow opens Ask on getting ready for it (its payload's nav).
    "event_ahead": "ask",
    # Cross-module reads that arrive with their own question, so they open
    # the assistant rather than guessing a module (iOS does the same).
    "morning_brief": "ask", "daily_briefing": "ask", "intraday_pulse": "ask",
    "closing_summary": "ask", "weekly_review": "ask", "monthly_review": "ask",
    "outcome_achieved": "ask", "milestone": "ask", "while_away": "reviews",
    # The report itself is the destination: the web opens it at #dsr/<date>
    # from Home, iOS from the payload's type + business_date.
    "dsr": "home",
    "issue": "account", "issue_escalated": "account",
    # Not a product module — the web dashboard's bell reads this field
    # directly; iOS's DeepLinkRouter has its own "login" special-case.
    "login": "account", "staff_signin": "account", "connection_lost": "account",
    "data_source_down": "account", "data_source_restored": "account",
    "platform_alert": "home",
    # The staff app, not a console module: the payload's `tab` and `nav`
    # ("staff/requests/12") say where in it. A console phone that gets one
    # (a manager on the schedule) degrades an unknown module to Home.
    **{t: "staff" for t in STAFF_ALERT_TYPES},
}


def module_of(alert_type) -> str:
    return NOTIFICATION_MODULE.get(alert_type or "", "reviews")


# The module whose readers a notification is FOR, where that is not the one
# it opens (re-audit 2 R3-07): the big-game heads-up opens Ask on the game,
# but it is pushed only to the logins who read Labor (event_intel.gameday) —
# so the bell, its badge and Ask's alerts show it to those logins only.
NOTIFICATION_AUDIENCE = {"event_ahead": "labor"}


def audience_of(alert_type) -> str:
    """The module a login must be able to view to see this notification —
    one rule for who is pushed it and whose bell lists it."""
    return NOTIFICATION_AUDIENCE.get(alert_type or "") or module_of(alert_type)


# Notifications that ask someone to DO something. Everything else — the
# briefs, summaries, wins, milestones, sign-ins, a reply that went out — is
# news. Ask counted every non-review row as "still needing action", so an
# owner heard they had six alerts outstanding when those were briefs and
# sign-ins (re-audit A-21). The while-away nudge counts only these too (A-23).
ACTIONABLE_TYPES = frozenset({
    "health", "1star", "2star", "3star", "neg_spike", "edit_downgrade", "no_response",
    "unresponded", "negative_trend", "rating_threshold",
    "labor_over", "coverage", "shift_request", "labor_reminder", "schedule_publish_held", "employee_message",
    "schedule_drafted",
    "food_waste", "critical_low", "price_spike", "order_send_held", "order_send_voided",
    "ai_visibility_drop", "issue", "issue_escalated", "connection_lost", "data_source_down",
    "platform_alert",
})


# An unmapped type is informational, not urgent. The old code had no priority
# at all and deliver_alert's unknown-type fallback was the HEALTH channel
# triplet — the most permissive default in the system.
DEFAULT_PRIORITY = P3_INFO


def priority_of(alert_type) -> int:
    return PRIORITY.get(alert_type, DEFAULT_PRIORITY)


# Only P0/P1 may pierce a Focus mode (needs the time-sensitive entitlement,
# set in project.yml). P4/P5 are delivered quietly to Notification Center.
_INTERRUPTION = {P0_CRITICAL: "time-sensitive", P1_ACT_NOW: "time-sensitive",
                 P4_SUMMARY: "passive", P5_LOW: "passive"}

# How long APNs should keep trying for a phone that is off or out of signal.
# Unset, this was Apple's default rather than a decision, and a pre-dinner
# pulse delivered at 11pm is worse than one not delivered at all.
_EXPIRY_SECONDS = {
    P0_CRITICAL: 24 * 3600,   # still worth knowing tomorrow
    P1_ACT_NOW: 4 * 3600,     # worthless once the shift it was about is over
    P2_OPPORTUNITY: 12 * 3600,
    P3_INFO: 12 * 3600,
    P4_SUMMARY: 6 * 3600,     # a morning brief arriving at 6pm is noise
    P5_LOW: 6 * 3600,
}

# UNNotificationCategory identifiers the app registers (PushManager.swift).
# A category is what lets an owner act from the lock screen instead of
# unlocking, finding the module and starting again.
CATEGORY_REVIEW = "CAVNAR_REVIEW"     # Reply (opens the review)
CATEGORY_BRIEF  = "CAVNAR_BRIEF"      # Ask about this
CATEGORY_ISSUE  = "CAVNAR_ISSUE"      # no extra button: the tap opens it
# The actionable kinds (friction audit #22, 9/25/26): the button does the
# work in the background, behind the phone's own unlock, and nothing opens.
CATEGORY_REVIEW_DRAFTED = "CAVNAR_REVIEW_DRAFTED"   # Approve & post · Edit
CATEGORY_UNDOABLE = "CAVNAR_UNDOABLE"               # Undo · Review
CATEGORY_REQUEST = "CAVNAR_REQUEST"                 # Approve · Deny
# Parity audit 10/7/26 (#35, #55): a recommendation answered from the lock
# screen, and the types that used to arrive with no button at all.
CATEGORY_REC = "CAVNAR_REC"                   # Done · Not for us (background)
CATEGORY_REC_ASK = "CAVNAR_REC_ASK"           # Done · Not for us · Ask about this
CATEGORY_PUBLISH_HELD = "CAVNAR_PUBLISH_HELD" # Send now (background) · Review
CATEGORY_STOCK = "CAVNAR_STOCK"               # Draft order (opens the order)
CATEGORY_LOGIN = "CAVNAR_LOGIN"               # This wasn't me (background)
CATEGORY_CONNECTION = "CAVNAR_CONNECTION"     # Reconnect (opens integrations)
CATEGORY_DSR = "CAVNAR_DSR"                   # Ask about last night
_BRIEF_TYPES = {"morning_brief", "intraday_pulse", "closing_summary",
                "weekly_review", "monthly_review", "daily_briefing"}
_ISSUE_TYPES = {"issue", "issue_escalated", "coverage", "critical_low"}
# A queued automatic send (delayed.py) the owner can still stop.
_UNDOABLE_TYPES = {"schedule_publish_pending", "order_send_pending"}


def _category(alert_type, data) -> str:
    data = data or {}
    if alert_type in _UNDOABLE_TYPES and data.get("delayed_action_id"):
        return CATEGORY_UNDOABLE
    if alert_type == "shift_request" and data.get("request_id") and data.get("request_kind"):
        return CATEGORY_REQUEST
    # A reply that already went out is news: no Reply button on it.
    if alert_type == "resp_approved":
        return ""
    # Send now acknowledges exactly the blockers the push named
    # (delayed._tell_owner_schedule_held), so it needs both.
    if alert_type == "schedule_publish_held" and data.get("schedule_id") \
            and isinstance(data.get("blocker_keys"), list):
        return CATEGORY_PUBLISH_HELD
    if alert_type == "critical_low":
        return CATEGORY_STOCK
    # "This wasn't me" only on the signed-in login's own notice.
    if alert_type == "login" and data.get("login_user_id"):
        return CATEGORY_LOGIN
    if alert_type in ("connection_lost", "data_source_down"):
        return CATEGORY_CONNECTION
    if alert_type == "dsr":
        return CATEGORY_DSR
    # A recommendation the app may answer (rec_delivery.answerable): Done and
    # Not for us from the lock screen. A review alert keeps its own buttons —
    # the reply is the thing to do about it — and an issue or a coverage gap
    # keeps the issue's (CATEGORY_ISSUE).
    if data.get("answerable") and data.get("rec_key") and not data.get("review_id") \
            and alert_type not in _ISSUE_TYPES:
        return CATEGORY_REC_ASK if (alert_type in _BRIEF_TYPES or data.get("ask_prompt")) else CATEGORY_REC
    if alert_type in _BRIEF_TYPES or data.get("ask_prompt"):
        return CATEGORY_BRIEF
    if alert_type in _ISSUE_TYPES:
        return CATEGORY_ISSUE
    if data.get("review_id"):
        # Approve from the lock screen only for a draft that could be
        # published without reading it one by one — the bulk-publish bar
        # (models.BULK_PUBLISHABLE_SQL): not flagged, not urgent, recent.
        return CATEGORY_REVIEW_DRAFTED if data.get("draft_ready") else CATEGORY_REVIEW
    return ""


def _review_draft_ready(restaurant_id, review_id, db_path=DB_PATH) -> bool:
    """Whether this review has a reply that one tap may publish — the same
    rule the Home "Publish N replies" button and approve-all apply. Never
    raises (False)."""
    return _publishable_draft(restaurant_id, review_id, db_path) is not None


def _publishable_draft(restaurant_id, review_id, db_path=DB_PATH):
    """The reply text one tap may publish (_review_draft_ready's rule), or
    None when there is none. Never raises (None)."""
    try:
        from models import BULK_PUBLISHABLE_SQL, bulk_publish_window
        conn = get_conn(db_path)
        try:
            row = conn.execute(
                f"SELECT draft_response FROM reviews WHERE id=? AND restaurant_id=? AND {BULK_PUBLISHABLE_SQL}",
                (int(review_id), int(restaurant_id), bulk_publish_window())).fetchone()
        finally:
            conn.close()
        return None if row is None else (row["draft_response"] or "")
    except Exception:
        return None


# The drafted reply rides an Approve & post push, so the notification's own
# view (the app's content extension) shows what one tap publishes (parity
# audit #36). APNs refuses a payload over 4096 bytes and the draft is the one
# field that can be long: it is clipped here and, if the whole payload is
# still too big, cut further at delivery (_fit_payload). `draft_complete`
# says whether the text is the whole reply — only then does the phone send
# it back as `expected_draft` with the approve.
PUSH_DRAFT_MAX_CHARS = 900
APNS_MAX_PAYLOAD_BYTES = 4096
_ELLIPSIS = "\u2026"


def _draft_fields(draft) -> dict:
    exact = str(draft or "").strip()
    if not exact:
        return {}
    if len(exact) <= PUSH_DRAFT_MAX_CHARS:
        return {"draft": exact, "draft_complete": True}
    return {"draft": exact[:PUSH_DRAFT_MAX_CHARS - 1].rstrip() + _ELLIPSIS, "draft_complete": False}


def _fit_payload(payload) -> bytes:
    """The payload's bytes, never over APNS_MAX_PAYLOAD_BYTES: the draft is
    shortened (nothing else is touched), and dropped when even a short one
    does not fit. Mutates `payload` to match what is returned."""
    def enc():
        return json.dumps(payload, separators=(",", ":")).encode()
    out = enc()
    cav = payload.get("cavnar") or {}
    if len(out) <= APNS_MAX_PAYLOAD_BYTES or not cav.get("draft"):
        return out
    text = str(cav["draft"])
    if text.endswith(_ELLIPSIS):
        text = text[:-1]
    lo, hi, best = 0, len(text), None
    while lo <= hi:
        mid = (lo + hi) // 2
        cav["draft"], cav["draft_complete"] = text[:mid].rstrip() + _ELLIPSIS, False
        if len(enc()) <= APNS_MAX_PAYLOAD_BYTES:
            best, lo = mid, mid + 1
        else:
            hi = mid - 1
    if best is None or best < 40:
        cav.pop("draft", None)
        cav.pop("draft_complete", None)
    else:
        cav["draft"], cav["draft_complete"] = text[:best].rstrip() + _ELLIPSIS, False
    return enc()


def staff_tab(alert_type, data=None) -> str:
    """The staff app tab a staff notice opens: the payload's own `tab` when
    it names one of STAFF_TABS, else the type's default."""
    tab = str((data or {}).get("tab") or "").strip().lower()
    return tab if tab in STAFF_TABS else STAFF_DEFAULT_TAB.get(alert_type, "today")


def staff_nav(alert_type, data=None) -> str:
    """"staff/<tab>[/<id>]" — the staff app's address for a notice: the tab,
    then the request, announcement, thread or task sheet it is about."""
    data = data or {}
    parts = ["staff", staff_tab(alert_type, data)]
    for k in _STAFF_NAV_IDS:
        if data.get(k) not in (None, ""):
            parts.append(str(data[k]))
            break
    return "/".join(parts)


def nav_for(alert_type, data=None) -> str:
    """The nav path (nav.py) a notification opens: the item when the payload
    names one, else the section, else the module. The same string rides the
    push and the notification list, so the app opens the review, the pending
    send or the request instead of a module's top (friction audit #3)."""
    import nav
    data = data or {}
    if data.get("nav"):
        return str(data["nav"])
    if alert_type in STAFF_ALERT_TYPES:
        return staff_nav(alert_type, data)
    if alert_type == "employee_message":
        # The console's Team inbox, on the thread when the payload names it.
        return nav.path("labor", "inbox", data.get("thread_id"))
    if alert_type in _UNDOABLE_TYPES and data.get("delayed_action_id"):
        return nav.path("action", data["delayed_action_id"])
    if data.get("review_id"):
        return nav.path("review", data["review_id"])
    if alert_type == "dsr":
        date = str(data.get("business_date") or "")
        return nav.path("dsr", "night", date) if len(date) == 10 else nav.path("dsr")
    if data.get("ask_prompt"):
        # The question itself rides as ask_prompt; repeating it URL-encoded
        # here would spend the 4KB APNs budget twice.
        return nav.path("ask")
    if alert_type == "demand_opportunity":
        # The opportunity feed's Fill card for the night (?card=, the web's
        # own card focus) and the post drafted for it (parity audit #22):
        # "marketing" opened Marketing's top and the draft id went unread.
        return nav.path("marketing", "opportunities", card=data.get("card"),
                        post_draft_id=data.get("post_draft_id"))
    if alert_type == "shift_request":
        if data.get("request_id") and data.get("request_kind"):
            return nav.path("request", f"{data['request_kind']}-{data['request_id']}")
        return nav.path("labor", "requests")
    if alert_type == "schedule_publish_held" and data.get("schedule_id"):
        return nav.path("schedule", data["schedule_id"])
    if alert_type in ("schedule_drafted", "schedule_publish_held", "coverage"):
        return nav.path("labor", "schedule")
    if alert_type in ("critical_low", "order_send_held", "order_send_voided", "order_send_pending"):
        return nav.path("inventory", "order")
    if alert_type in ("issue", "issue_escalated") and data.get("issue_id"):
        return nav.path("issue", data["issue_id"])
    # Everything else opens where its bell row does (nav.for_notification's
    # map): a staff sign-in on People, a price spike on Invoices, overtime on
    # Labor's overtime, a lost connection on Integrations — the push and the
    # bell disagreed on all of these (parity audit #49).
    if alert_type in nav._ALERT_NAV:
        return nav._ALERT_NAV[alert_type]
    module = module_of(alert_type)
    return nav.path({"competitor": "intel", "food": "inventory"}.get(module, module))


_jwt_cache = {"token": None, "minted_at": 0}
_JWT_MAX_AGE = 50 * 60  # Apple allows up to 60 min; regenerate a bit early
_jwt_lock = threading.Lock()


def invalidate_provider_jwt():
    """Drop the cached signing token so the next attempt mints a fresh one.

    APNs answers a stale JWT with 403 ExpiredProviderToken. Without this the
    retry loop re-sent the same dead token twice more and burned all three
    attempts, and the 50-minute cache meant every push for the rest of that
    window failed the same way."""
    with _jwt_lock:
        _jwt_cache["token"] = None
        _jwt_cache["minted_at"] = 0


def _provider_jwt():
    """ES256 JWT signed with Apple's .p8 auth key — Apple's own APNs auth
    scheme, unrelated to this app's user sessions. Cached for ~50 minutes
    since Apple explicitly discourages minting a new one on every request."""
    import os
    now = time.time()
    if _jwt_cache["token"] and (now - _jwt_cache["minted_at"]) < _JWT_MAX_AGE:
        return _jwt_cache["token"]
    key_id = os.getenv("APNS_KEY_ID")
    team_id = os.getenv("APNS_TEAM_ID")
    private_key = os.getenv("APNS_PRIVATE_KEY", "").replace("\\n", "\n")
    # Named, not signed-with-nothing. An empty key produced a token APNs
    # rejects, which read downstream as ten dead devices rather than as one
    # missing environment variable.
    missing = [n for n, v in (("APNS_KEY_ID", key_id), ("APNS_TEAM_ID", team_id),
                              ("APNS_PRIVATE_KEY", private_key)) if not v]
    if missing:
        raise PushNotConfigured("APNs is not configured: " + ", ".join(missing) + " unset")
    import jwt as _pyjwt
    try:
        token = _pyjwt.encode(
            {"iss": team_id, "iat": int(now)},
            private_key,
            algorithm="ES256",
            headers={"kid": key_id},
        )
    except Exception as e:
        # A set-but-unparseable key surfaced as cryptography's raw "Unable to
        # load PEM file ... InvalidData(Invalid symbol 226, offset 0)", which
        # names neither the variable nor the cause. Byte 226 is the first of
        # a UTF-8 bullet: production had a MASKED rendering of the .p8 stored
        # as the key itself — 200 bullet characters where the base64 body
        # should be, PEM headers intact. Push had therefore never worked, and
        # under the old failure handling each attempt counted toward deleting
        # the device, so there was nothing left to notice it with.
        body = "".join(private_key.splitlines()[1:-1])
        if body and not any(c.isalnum() for c in body):
            raise PushNotConfigured(
                "APNS_PRIVATE_KEY is a masked placeholder, not a key: the PEM headers are "
                "there but the body is all bullet characters. Re-paste the .p8 from the "
                "file itself, never from a screen that renders it as dots."
            ) from e
        raise PushNotConfigured(f"APNS_PRIVATE_KEY is not a usable .p8 private key: {e}") from e
    with _jwt_lock:
        _jwt_cache["token"] = token
        _jwt_cache["minted_at"] = now
    return token


def _bundle_id():
    """The apns-topic. Defaults to the real bundle id, matching
    mobile_api.py's Sign-in-with-Apple check — this used to default to "",
    which APNs rejects as MissingTopic on every single send."""
    return os.getenv("APNS_BUNDLE_ID", "ai.cavnar.CavnarAI")


def _apns_host(environment):
    return "api.push.apple.com" if environment == "production" else "api.sandbox.push.apple.com"


def _collapse_id(restaurant_id, alert_type, data) -> str:
    """A stable identifier for one logical alert, for APNs' apns-collapse-id.

    The retry loop below treats a client-side timeout as a failure and sends
    again, twice more — but a timeout often means Apple took the push and the
    response was what got lost. Without this header those become two or three
    separate banners on the same phone for the same event.

    It has to be stable across the retries of ONE alert and different between
    two alerts, so the review id is in the key: two 1-star reviews arriving in
    the same fetch are two things the owner needs to see, not one. Alerts with
    no review behind them (labor over target, food waste) get the local date
    instead, which also means the daily job cannot stack duplicates if it runs
    twice. Apple caps this at 64 bytes.
    """
    parts = [str(restaurant_id), str(alert_type or "alert")]
    review_id = (data or {}).get("review_id")
    explicit = (data or {}).get("collapse_key")
    if explicit:
        parts.append(str(explicit))
    elif review_id:
        parts.append(f"r{review_id}")
    elif alert_type in _UNCOLLAPSIBLE_TYPES:
        # Several of these happen in one day and each one is its own event.
        # Keyed on the date, a second sign-in REPLACED the first on the lock
        # screen: an owner with three staff sign-ins and two dashboard logins
        # saw one notification, and the security value of the feature went
        # with the ones it overwrote.
        parts.append(uuid.uuid4().hex[:12])
    else:
        parts.append(datetime.now(timezone.utc).strftime("%Y%m%d"))
    key = "-".join(parts)
    if len(key.encode()) > 64:
        import hashlib
        key = hashlib.sha256(key.encode()).hexdigest()[:32]
    return key


def _classify(status, reason):
    """('dead' | 'provider' | 'token') for one failed attempt.

    'dead'     — Apple says this token will never work: delete it.
    'provider' — our key, our topic, our network, or Apple's own trouble.
                 Says nothing about the device, so it must NOT advance the
                 failure counter (this is the audit's P0).
    'token'    — an unclassified 4xx. Counted, and at the threshold the
                 token is PARKED rather than deleted.
    """
    if reason in _PERMANENT_FAILURE_REASONS:
        return "dead"
    if reason in _PROVIDER_FAILURE_REASONS:
        return "provider"
    if status in (0, 403, 429) or status >= 500:
        return "provider"
    return "token"


# One capture per reason per window, so an expired key across 50 restaurants
# writes one job_failures row an hour rather than hundreds.
_PROVIDER_ALARM_WINDOW = 3600
_provider_alarms = {}


def _alarm_provider_failure(error, db_path):
    key = str(error)[:80]
    now = time.time()
    with _executor_lock:
        if now - _provider_alarms.get(key, 0) < _PROVIDER_ALARM_WINDOW:
            return
        _provider_alarms[key] = now
    try:
        import ops
        ops.capture(RuntimeError(f"APNs provider failure: {error}"),
                    job="push_provider", context="every device is affected, not one",
                    db_path=db_path)
    except Exception:
        pass


_http_client = None


def _client():
    """One shared HTTP/2 client for every delivery.

    Each _deliver used to open its own, so every notification paid a fresh
    TLS handshake to Apple — the thing that becomes the bottleneck long
    before SQLite does. httpx.Client is thread-safe, and the pool below is
    bounded at four workers anyway."""
    global _http_client
    if _http_client is None:
        with _executor_lock:
            if _http_client is None:
                import httpx
                _http_client = httpx.Client(http2=True, timeout=5)
    return _http_client


def _badge_for(device_token_row, db_path):
    """This login's unread count, for the app icon. The app asked for badge
    authorization from the first launch and nothing ever set one.

    None (no badge key at all) for a staff-app device: the count is the
    console's unread notifications — owner alerts — and must never land on
    an employee's icon (COM-03)."""
    try:
        if device_token_row.get("tier") == TIER_STAFF:
            return None
    except AttributeError:
        pass
    try:
        from models import unread_notification_count
        uid = int(device_token_row.get("user_id") or 0)
        rid = int(device_token_row["restaurant_id"])
        visible = None
        try:
            # The same role filter the list uses, from this login's role at
            # this location, so the icon agrees with the bell (MOD-NOT-10).
            from auth import get_membership, get_user_by_id
            import client_api
            member = get_membership(uid, rid, db_path) or {}
            user = get_user_by_id(uid, db_path) or {}
            viewer = {"id": uid, "role": member.get("role") or user.get("role"),
                      "is_admin": user.get("is_admin")}
            visible = client_api.notification_visibility(viewer)
        except Exception:
            visible = None
        return unread_notification_count(uid, rid, db_path, visible=visible)
    except Exception:
        return None


def _deliver(device_token_row, alert_type, title, body, data, db_path=DB_PATH):
    priority = priority_of(alert_type)
    aps = {"alert": {"title": title, "body": body}, "sound": "default"}
    level = _INTERRUPTION.get(priority)
    if level:
        aps["interruption-level"] = level
    category = _category(alert_type, data)
    if category:
        aps["category"] = category
    # Ranks Cavnar's own notifications against each other in a summary.
    aps["relevance-score"] = round(max(0.0, 1.0 - priority / 5.0), 2)
    # One thread per restaurant: five alerts group under one header instead
    # of stacking as five unrelated banners.
    aps["thread-id"] = f"cavnar-{device_token_row.get('restaurant_id')}"
    badge = _badge_for(device_token_row, db_path)
    if badge is not None:
        aps["badge"] = badge
    try:
        # The sound setting of the location the alert is ABOUT, not of the
        # location this phone happened to register at (A-14).
        rid = (data or {}).get("restaurant_id") or \
            (device_token_row["restaurant_id"] if "restaurant_id" in device_token_row.keys() else None)
        if rid:
            _c = get_conn(db_path)
            _r = _c.execute("SELECT push_sound, alert_health_bypass_quiet FROM restaurants WHERE id=?", (rid,)).fetchone()
            _c.close()
            if _r and _r["push_sound"] == 0:
                aps.pop("sound", None)
            # A P0 that has NOT opted out of quiet hours stays a normal
            # banner — the entitlement exists for the owner who asked to be
            # woken, not for every health mention.
            if alert_type == "health" and not (_r and _r["alert_health_bypass_quiet"]):
                aps.pop("interruption-level", None)
    except Exception:
        pass
    if (data or {}).get("quiet"):
        # Sent inside the owner's quiet hours (strategy_jobs._reach): into
        # Notification Center with no sound and no Focus break (A-10).
        aps.pop("sound", None)
        aps["interruption-level"] = "passive"
    payload = {
        "aps": aps,
        "cavnar": {"alert_type": alert_type, "priority": priority, **(data or {})},
    }
    payload_bytes = _fit_payload(payload)
    environment = device_token_row["environment"]
    url = f"https://{_apns_host(environment)}/3/device/{device_token_row['apns_token']}"
    expiry = int(time.time()) + _EXPIRY_SECONDS.get(priority, 12 * 3600)
    # Computed ONCE, outside the retry loop. Its whole job is to be stable
    # across the retries of one alert — a client-side timeout often means
    # Apple took the push and the response was lost, and a key that changed
    # per attempt would turn that into two or three banners for one event.
    collapse_id = _collapse_id(device_token_row.get("restaurant_id"), alert_type, data)
    status = 0
    ok = False
    error = None
    verdict = None
    backoffs = [0, 2, 6]
    attempts = 0
    for delay in backoffs:
        if delay:
            time.sleep(delay)
        attempts += 1
        try:
            headers = {
                "authorization": f"bearer {_provider_jwt()}",
                "apns-topic": _bundle_id(),
                "apns-push-type": "alert",
                "apns-collapse-id": collapse_id,
                # 5 lets Apple batch a summary with the phone's power state;
                # 10 is immediate. Nothing below P4 should cost battery.
                "apns-priority": "5" if priority >= P4_SUMMARY else "10",
                "apns-expiration": str(expiry),
                "content-type": "application/json",
            }
            resp = _client().post(url, content=payload_bytes, headers=headers)
            status = resp.status_code
            if status == 200:
                ok = True
                verdict = None
                break
            try:
                reason = resp.json().get("reason", "")
            except Exception:
                reason = ""
            error = reason or f"HTTP {status}"
            verdict = _classify(status, reason)
            if reason in _JWT_REMINT_REASONS:
                invalidate_provider_jwt()
            if verdict == "dead":
                break
        except PushNotConfigured as e:
            # No key, no point retrying 2 more times against the same env.
            error, status, verdict = str(e)[:300], 0, "provider"
            break
        except Exception as e:
            error = str(e)[:300]
            print(f"[push] delivery error (token={device_token_row['apns_token'][:12]}...): {e}")
            status = 0
            verdict = "provider"

    # BadDeviceToken does not only mean "this token is dead" — Apple returns
    # it just as readily for a LIVE token sent to the wrong host. The client
    # decides sandbox vs production from its own build, and that guess is
    # wrong whenever a Release build is signed with a development profile
    # (Xcode's default when you Run to a device), which yields a sandbox
    # token from a build that reports itself as production.
    #
    # Deleting on the first BadDeviceToken made that unrecoverable in a loop:
    # register, fail, delete, re-register on next launch, fail again. So try
    # the other host once before believing Apple, and correct the stored
    # environment when it works.
    if not ok and error == "BadDeviceToken":
        other = "sandbox" if environment == "production" else "production"
        alt_url = f"https://{_apns_host(other)}/3/device/{device_token_row['apns_token']}"
        try:
            resp = _client().post(alt_url, content=payload_bytes, headers={
                "authorization": f"bearer {_provider_jwt()}",
                "apns-topic": _bundle_id(),
                "apns-push-type": "alert",
                "apns-collapse-id": collapse_id,
                "apns-priority": "5" if priority >= P4_SUMMARY else "10",
                "apns-expiration": str(expiry),
                "content-type": "application/json",
            })
            attempts += 1
            if resp.status_code == 200:
                ok, status, error, verdict = True, 200, None, None
                environment = other
                try:
                    conn = get_conn(db_path)
                    conn.execute("UPDATE device_tokens SET environment=? WHERE id=?",
                                 (other, device_token_row["id"]))
                    conn.commit()
                    conn.close()
                except Exception as e:
                    # Not optional: losing this write means every later push
                    # to this device keeps guessing the wrong host first and
                    # paying two round-trips to reach Apple.
                    try:
                        import ops
                        ops.capture(e, job="push_environment_fix",
                                    context=f"device_token_id={device_token_row['id']}",
                                    db_path=db_path)
                    except Exception:
                        pass
                print(f"[push] token {device_token_row['id']} was registered as "
                      f"{device_token_row['environment']}, actually {other} — corrected")
        except Exception as e:
            print(f"[push] alternate-host retry failed: {e}")

    if not ok and verdict == "provider":
        _alarm_provider_failure(error, db_path)

    try:
        conn = get_conn(db_path)
        conn.execute(
            """INSERT INTO push_deliveries
               (device_token_id, restaurant_id, alert_type, status, ok, attempts, error)
               VALUES (?,?,?,?,?,?,?)""",
            (device_token_row["id"], device_token_row["restaurant_id"], alert_type, status, int(ok), attempts, error)
        )
        if ok:
            conn.execute(
                "UPDATE device_tokens SET last_success_at=datetime('now'), consecutive_failures=0, "
                "disabled_reason=NULL WHERE id=?",
                (device_token_row["id"],)
            )
        elif verdict == "dead":
            # Apple has told us this token will never work again — delete it
            # immediately rather than waiting out _AUTO_DISABLE_AFTER.
            conn.execute("DELETE FROM device_tokens WHERE id=?", (device_token_row["id"],))
        elif verdict == "provider":
            # Nothing about this device is wrong. Leave the counter alone.
            pass
        else:
            row = conn.execute(
                "SELECT consecutive_failures FROM device_tokens WHERE id=?", (device_token_row["id"],)
            ).fetchone()
            failures = (row["consecutive_failures"] or 0) + 1 if row else 1
            if failures >= _AUTO_DISABLE_AFTER:
                # PARKED, not deleted. get_device_tokens(for_delivery=True)
                # skips it, and the next app launch re-registers and clears
                # it — so a bad patch costs an owner one relaunch, not a
                # permanent silent unsubscribe they never asked for.
                conn.execute(
                    "UPDATE device_tokens SET consecutive_failures=?, disabled_reason=? WHERE id=?",
                    (failures, f"{_AUTO_DISABLE_AFTER} consecutive failures ({error})"[:200],
                     device_token_row["id"])
                )
            else:
                conn.execute(
                    "UPDATE device_tokens SET consecutive_failures=? WHERE id=?",
                    (failures, device_token_row["id"])
                )
        conn.commit()
        conn.close()
    except Exception as e:
        # This bookkeeping is what advances consecutive_failures, and that
        # counter is the only thing that ever deletes a dead token. Losing it
        # silently means retrying a token that will never work, forever.
        try:
            import ops
            ops.capture(e, job="push_delivery_log", context=f"alert_type={alert_type}", db_path=db_path)
        except Exception:
            pass
    return {"ok": ok, "status": status, "attempts": attempts, "error": error}


# Deliveries run on a small shared pool rather than a thread per device.
# _deliver retries with backoff, so a thread lives for seconds, and the
# scheduler fires push for every restaurant in one pass — 50 restaurants
# times a couple of devices each used to mean a hundred-plus concurrent
# threads on a small Railway container, every one of them holding an HTTP/2
# connection and writing to the same SQLite file.
_MAX_PUSH_WORKERS = int(os.getenv("PUSH_MAX_WORKERS", "4"))
# A ceiling on how far behind the pool may fall before deliveries are
# dropped. Dropping an alert is bad; exhausting the container's memory takes
# the whole app down with it, so there has to be a number — generous enough
# that a normal digest sweep never reaches it.
_MAX_PUSH_QUEUED = int(os.getenv("PUSH_MAX_QUEUED", "500"))

# Deliveries past _MAX_PUSH_QUEUED wait here instead of being dropped: each
# finished delivery hands its pool slot to the next one waiting. A queue
# ceiling used to drop every device after the 500th while the alert history
# said sent (MOD-NOT-12). This holds a few hundred bytes per device, so the
# memory ceiling moves here, far higher; only past it is a push dropped.
_MAX_PUSH_OVERFLOW = int(os.getenv("PUSH_MAX_OVERFLOW", "20000"))

_executor = None
_executor_lock = threading.Lock()
_queued = 0
_overflow = None


def _overflow_queue():
    global _overflow
    if _overflow is None:
        from collections import deque
        _overflow = deque()
    return _overflow


def _push_executor():
    global _executor
    if _executor is None:
        with _executor_lock:
            if _executor is None:
                from concurrent.futures import ThreadPoolExecutor
                _executor = ThreadPoolExecutor(
                    max_workers=_MAX_PUSH_WORKERS, thread_name_prefix="push"
                )
    return _executor


class OnDelivered:
    """A caller's "it was delivered" hook, run at most once per push however
    many of the recipient's devices Apple accepts it on. A recommendation a
    push carries is shown when the notification reaches a phone — not when
    it is queued: a push nobody's device took (no device, a dead token, APNs
    down) showed nobody anything, and was recorded as shown all the same
    (re-audit C2/C4). Thread-safe; a hook that raises is logged and dropped,
    never retried."""

    def __init__(self, fn):
        self._fn = fn
        self._lock = threading.Lock()
        self.fired = False

    def __call__(self):
        with self._lock:
            if self.fired or self._fn is None:
                return
            self.fired = True
        try:
            self._fn()
        except Exception as e:
            print(f"[push] delivered hook failed: {e}")


class _PushGroup:
    """One fire_push across a recipient's devices. Runs the caller's
    `on_failed` once, after every queued device has been tried, when Apple
    took it on none of them — what the morning brief falls back to email on
    (#82). Thread-safe; a hook that raises is logged and dropped."""

    def __init__(self, n, on_failed):
        self._left, self._ok, self._fn = int(n), False, on_failed
        self._lock = threading.Lock()

    def done(self, ok):
        fn = None
        with self._lock:
            self._ok = self._ok or bool(ok)
            self._left -= 1
            if self._left <= 0 and not self._ok and self._fn is not None:
                fn, self._fn = self._fn, None
        if fn is not None:
            try:
                fn()
            except Exception as e:
                print(f"[push] failed hook failed: {e}")


def _outbox_claim(outbox_id, db_path=DB_PATH) -> bool:
    """queued -> delivering, by exactly one runner (the pool or the reaper)."""
    try:
        conn = get_conn(db_path)
        try:
            won = conn.execute("UPDATE push_outbox SET state='delivering', attempts=attempts+1, "
                               "updated_at=datetime('now') WHERE id=? AND state='queued'",
                               (outbox_id,)).rowcount == 1
            conn.commit()
            return won
        finally:
            conn.close()
    except Exception:
        return True                     # deliver rather than lose it


def _outbox_finish(outbox_id, state, error=None, db_path=DB_PATH):
    if outbox_id is None:
        return
    try:
        conn = get_conn(db_path)
        try:
            conn.execute("UPDATE push_outbox SET state=?, last_error=?, updated_at=datetime('now'), "
                         "done_at=datetime('now') WHERE id=?",
                         (state, (str(error)[:300] if error else None), outbox_id))
            conn.commit()
        finally:
            conn.close()
    except Exception as e:
        print(f"[push] outbox update failed ({outbox_id}): {e}")


def _run_delivery(token_row, alert_type, title, body, data, db_path, on_delivered=None,
                  outbox_id=None, group=None):
    global _queued
    try:
        # Claimed first: a row the reaper also picked up is delivered once.
        if outbox_id is None or _outbox_claim(outbox_id, db_path):
            ok, error = False, None
            try:
                res = _deliver(token_row, alert_type, title, body, data, db_path)
                ok, error = bool((res or {}).get("ok")), (res or {}).get("error")
            except Exception as e:
                error = str(e)
                print(f"[push] delivery raised ({alert_type}): {e}")
            # Marked from the delivery's own result (#75): the ledgers used to
            # say "sent" the moment a push was queued.
            _outbox_finish(outbox_id, "sent" if ok else "failed", None if ok else error, db_path)
            if on_delivered is not None and ok:
                on_delivered()
            if group is not None:
                group.done(ok)
    finally:
        with _executor_lock:
            waiting = _overflow_queue()
            nxt = waiting.popleft() if waiting else None
            if nxt is None:
                _queued -= 1          # the slot is free; a waiting one keeps it
        if nxt is not None:
            try:
                _push_executor().submit(_run_delivery, *nxt)
            except Exception as e:
                with _executor_lock:
                    _queued -= 1
                print(f"[push] could not resubmit a waiting delivery: {e}")


def send_test_push(restaurant_id, user_id, db_path=DB_PATH):
    """Send one test notification to THIS login's own devices, synchronously,
    and report what APNs actually said.

    Everything else here is fire-and-forget on a background pool, which is
    right for an alert and useless for "is push working for this client?" —
    that question had no answer short of reaching into the database. Runs
    inline so the caller can be told the truth, and uses a real alert type so
    the phone renders it exactly as it would render the real thing: same
    category, same thread, same badge, same sound setting.

    Returns {"ok", "devices", "sent", "failures": [...]}.
    """
    tokens = [t for t in get_device_tokens(restaurant_id, db_path, for_delivery=True)
              if int(t.get("user_id") or 0) == int(user_id)]
    if not tokens:
        return {"ok": False, "devices": 0, "sent": 0, "failures": [],
                "error": "No device registered for your login. Open the Cavnar AI app on "
                         "your phone, allow notifications, and try again."}
    sent, failures = 0, []
    for token_row in tokens:
        result = _deliver(
            token_row, "test_push", "Test notification",
            "If you can read this, push is working. Nothing was sent to anyone else.",
            # A distinct collapse key per test. Keyed on the date like every
            # other review-less alert, a second test the same day would
            # silently replace the first on the lock screen — which reads
            # exactly like "it didn't work".
            {"collapse_key": uuid.uuid4().hex[:12]}, db_path)
        if result.get("ok"):
            sent += 1
        else:
            failures.append(result.get("error") or f"HTTP {result.get('status')}")
    return {"ok": sent > 0, "devices": len(tokens), "sent": sent, "failures": failures,
            "error": None if sent else (failures[0] if failures else "Apple did not accept it.")}


def _record_dropped(token_rows, alert_type, db_path=DB_PATH):
    """A push dropped at the queue ceiling leaves a push_deliveries row per
    device, like any other failed delivery (AI-29). Only the failure digest
    knew before, so the alert's own delivery history showed nothing tried.
    Written without touching the device's failure counter: nothing is wrong
    with the device."""
    try:
        conn = get_conn(db_path)
        try:
            conn.executemany(
                "INSERT INTO push_deliveries (device_token_id, restaurant_id, alert_type, status, ok, "
                "attempts, error) VALUES (?,?,?,NULL,0,0,?)",
                [(t["id"], t["restaurant_id"], alert_type,
                  "not sent: the push queue was full") for t in token_rows])
            conn.commit()
        finally:
            conn.close()
    except Exception as e:
        print(f"[push] could not record dropped pushes ({alert_type}): {e}")


def _outbox_write(token_rows, restaurant_id, alert_type, title, body, data, db_path=DB_PATH) -> list:
    """One push_outbox row per device, written before anything is handed to
    the pool (#75). Returns the row ids in token order; None for each when
    the write failed (the push then goes from memory, as before)."""
    try:
        conn = get_conn(db_path)
        try:
            payload = json.dumps(data or {}, default=str)[:8000]
            ids = [conn.execute(
                "INSERT INTO push_outbox (restaurant_id, device_token_id, alert_type, title, body, data_json, "
                "updated_at) VALUES (?,?,?,?,?,?, datetime('now'))",
                (restaurant_id, t["id"], alert_type, (title or "")[:300], (body or "")[:1000], payload)).lastrowid
                for t in token_rows]
            conn.commit()
            return ids
        finally:
            conn.close()
    except Exception as e:
        print(f"[push] outbox write failed ({alert_type}, rid={restaurant_id}): {e}")
        return [None] * len(token_rows)


def fire_push(restaurant_id, alert_type, title, body, data=None, db_path=DB_PATH, user_ids=None,
              on_delivered=None, on_failed=None):
    """Fire push to every device registered for this restaurant, on a bounded
    background pool — never blocks the caller. Mirrors webhooks.fire_webhook()'s
    fire-and-forget shape.

    Each device's push is written to push_outbox before it is handed to the
    pool and marked sent or failed from Apple's answer (#75); what a restart
    leaves behind, reap_push_outbox delivers.

    `user_ids` narrows delivery to those logins' devices. Every device at a
    restaurant includes managers' and teammates' phones, so anything carrying
    owner-only content (the morning brief's prime cost and loss signals) must
    pass it — None keeps the everyone-at-the-restaurant behaviour.

    `on_delivered` (a no-argument callable) runs once, on the push pool,
    after the first device APNs accepted this push on — where a caller
    records what the notification showed (rec_delivery). It never runs for a
    push no device took.

    `on_failed` (a no-argument callable) runs once, on the push pool, when
    every device it was queued for has been tried and Apple accepted it on
    none — where a caller falls back to email (morning_brief, #82). Not run
    when nothing was queued: the return value 0 says that, synchronously.

    Returns how many devices it was queued for (0: nobody could receive
    it)."""
    global _queued
    hook = OnDelivered(on_delivered) if on_delivered is not None else None
    queued = 0
    # Every payload names the location it is about and the module it opens.
    # A group owner's phone registered at location B receives location A's
    # alerts (get_device_tokens), and with no restaurant_id a tap opened A's
    # review inside B — not found, and the open recorded against B (A-14).
    data = dict(data or {})
    data.setdefault("restaurant_id", restaurant_id)
    data.setdefault("module", module_of(alert_type))
    if alert_type in STAFF_ALERT_TYPES:
        # The staff app routes on `tab` (+ the id) — every staff push says it.
        data["tab"] = staff_tab(alert_type, data)
    # A review push carries whether its reply may be published from the
    # lock screen (read once here, not per device), and every push carries
    # where it opens (nav.py) — friction audit #3/#22.
    if data.get("review_id") and "draft_ready" not in data:
        draft = _publishable_draft(restaurant_id, data["review_id"], db_path)
        data["draft_ready"] = draft is not None
        # What Approve & post would publish, for the notification's own view
        # (parity audit #36).
        if draft is not None:
            data.update(_draft_fields(draft))
    try:
        data.setdefault("nav", nav_for(alert_type, data))
    except Exception as e:
        print(f"[push] nav for {alert_type} failed: {e}")
    try:
        # A staff-app device only when the caller named its login, and only
        # for a staff type (COM-03): a restaurant-wide push never reaches one.
        tokens = get_device_tokens(restaurant_id, db_path, for_delivery=True,
                                   include_staff=user_ids is not None)
        if user_ids is not None:
            allowed = {int(u) for u in user_ids}
            tokens = [t for t in tokens if int(t.get("user_id") or 0) in allowed]
        tokens = [t for t in tokens if token_may_receive(t, alert_type)]
        # Each login's OWN choices at this location (preferences, memory
        # audit 9/29/26 owner_layers): push off, a type they muted, their own
        # quiet hours. They only ever take a push away from that login's own
        # phones — the location's matrix, quiet hours and cap were applied
        # when the alert was raised — and health, safety and an assigned
        # issue always reach them.
        try:
            import preferences as _prefs
            _pc = {}
            tokens = [t for t in tokens if _prefs.push_allowed(t.get("user_id"), restaurant_id, alert_type,
                                                               db_path=db_path, _cache=_pc)]
        except Exception as e:
            print(f"[push] login preferences not applied ({alert_type}, rid={restaurant_id}): {e}")
        outbox_ids = _outbox_write(tokens, restaurant_id, alert_type, title, body, data, db_path) if tokens else []
        group = _PushGroup(len(tokens), on_failed) if (on_failed is not None and tokens) else None
        dropped = []
        for i, token_row in enumerate(tokens):
            outbox_id = outbox_ids[i] if i < len(outbox_ids) else None
            with _executor_lock:
                if _queued >= _MAX_PUSH_QUEUED:
                    waiting = _overflow_queue()
                    if len(waiting) < _MAX_PUSH_OVERFLOW:
                        # The pool is busy: wait for a slot rather than drop.
                        waiting.append((token_row, alert_type, title, body, data, db_path, hook,
                                        outbox_id, group))
                        queued += 1
                        continue
                    full_at = _queued + len(waiting)
                    dropped = list(zip(tokens[i:], outbox_ids[i:]))
                else:
                    full_at = None
                    _queued += 1
            if dropped:
                print(f"[push] queue full ({full_at}) — dropping {alert_type} for rid={restaurant_id}")
                try:
                    import ops
                    ops.capture(RuntimeError(f"push queue full at {full_at}"),
                                job="fire_push", context=f"rid={restaurant_id} {alert_type}",
                                db_path=db_path)
                except Exception:
                    pass
                break
            # Positional: the delivery's arguments, then its outbox row and group.
            _push_executor().submit(
                _run_delivery, token_row, alert_type, title, body, data, db_path, hook, outbox_id, group
            )
            queued += 1
        if dropped:
            _record_dropped([t for t, _o in dropped], alert_type, db_path)
            for _t, oid in dropped:
                _outbox_finish(oid, "failed", "not sent: the push queue was full", db_path)
                if group is not None:
                    group.done(False)
    except Exception as e:
        print(f"[push] fire_push error ({alert_type}, rid={restaurant_id}): {e}")
    return queued


# A push a restart left 'delivering' (or 'queued' past the in-memory
# overflow) is re-driven after this long; one older than
# PUSH_OUTBOX_MAX_AGE_MINUTES is no longer worth a banner and expires, with a
# push_deliveries row saying so (#75).
PUSH_OUTBOX_STALE_MINUTES = 10
PUSH_OUTBOX_MAX_AGE_MINUTES = int(os.getenv("PUSH_OUTBOX_MAX_AGE_MINUTES", "120"))


def reap_push_outbox(db_path=DB_PATH, limit=200) -> dict:
    """Deliver what a restart left behind (#75). Stale 'delivering' rows go
    back to 'queued'; rows past PUSH_OUTBOX_MAX_AGE_MINUTES expire (and are
    recorded in push_deliveries as not sent); up to `limit` stale queued rows
    are handed to the pool. A re-driven push carries no caller hooks — a
    process that died took them with it. Returns {"requeued", "expired",
    "submitted"}. Scheduled by the integration wave (a minute duty)."""
    global _queued
    out = {"requeued": 0, "expired": 0, "submitted": 0}
    conn = get_conn(db_path)
    try:
        old = conn.execute(
            "SELECT id, restaurant_id, device_token_id, alert_type FROM push_outbox WHERE state IN "
            "('queued','delivering') AND created_at < datetime('now', ?) LIMIT ?",
            (f"-{PUSH_OUTBOX_MAX_AGE_MINUTES} minutes", int(limit) * 5)).fetchall()
        for r in old:
            conn.execute("UPDATE push_outbox SET state='expired', done_at=datetime('now'), updated_at=datetime('now'), "
                         "last_error=COALESCE(last_error, 'not delivered in time (a restart)') WHERE id=?", (r["id"],))
            conn.execute("INSERT INTO push_deliveries (device_token_id, restaurant_id, alert_type, status, ok, "
                         "attempts, error) VALUES (?,?,?,NULL,0,0,?)",
                         (r["device_token_id"], r["restaurant_id"], r["alert_type"],
                          "not sent: lost in a restart and too old to deliver"))
        out["expired"] = len(old)
        out["requeued"] = conn.execute(
            "UPDATE push_outbox SET state='queued', updated_at=datetime('now') WHERE state='delivering' "
            "AND COALESCE(updated_at, created_at) < datetime('now', ?)",
            (f"-{PUSH_OUTBOX_STALE_MINUTES} minutes",)).rowcount
        conn.commit()
        rows = conn.execute(
            "SELECT o.*, d.user_id, d.apns_token, d.environment, d.disabled_reason, d.tier FROM push_outbox o "
            "LEFT JOIN device_tokens d ON d.id=o.device_token_id WHERE o.state='queued' "
            "AND COALESCE(o.updated_at, o.created_at) < datetime('now', ?) ORDER BY o.id LIMIT ?",
            (f"-{PUSH_OUTBOX_STALE_MINUTES} minutes", int(limit))).fetchall()
    finally:
        conn.close()
    for r in rows:
        if not r["apns_token"] or r["disabled_reason"]:
            _outbox_finish(r["id"], "failed", "the device is gone or parked", db_path)
            continue
        try:
            data = json.loads(r["data_json"] or "null") or {}
        except (TypeError, ValueError):
            data = {}
        token_row = {"id": r["device_token_id"], "user_id": r["user_id"], "restaurant_id": r["restaurant_id"],
                     "apns_token": r["apns_token"], "environment": r["environment"] or "production",
                     "tier": r["tier"] or TIER_OWNER}
        if not token_may_receive(token_row, r["alert_type"]):
            # The row moved to the staff app since this was queued.
            _outbox_finish(r["id"], "failed", "the device is now a staff-app device", db_path)
            continue
        with _executor_lock:
            if _queued >= _MAX_PUSH_QUEUED:
                break
            _queued += 1
        _push_executor().submit(_run_delivery, token_row, r["alert_type"], r["title"], r["body"], data,
                                db_path, None, r["id"], None)
        out["submitted"] += 1
    return out


def outbox_counts(db_path=DB_PATH) -> dict:
    """{state: n, "oldest_pending_at", "failed_24h"} — the console's Queues
    panel (#75)."""
    conn = get_conn(db_path)
    try:
        by_state = {r["state"]: r["n"] for r in conn.execute(
            "SELECT state, COUNT(*) AS n FROM push_outbox GROUP BY state").fetchall()}
        oldest = conn.execute("SELECT MIN(created_at) FROM push_outbox WHERE state IN ('queued','delivering')"
                              ).fetchone()[0]
        failed_24h = conn.execute("SELECT COUNT(*) FROM push_outbox WHERE state IN ('failed','expired') "
                                  "AND created_at >= datetime('now','-1 day')").fetchone()[0]
    finally:
        conn.close()
    return {"by_state": by_state, "oldest_pending_at": oldest, "failed_24h": failed_24h}
