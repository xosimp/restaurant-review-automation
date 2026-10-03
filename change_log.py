"""change_log — a lasting, attributed history of settings, targets, prices,
menu, roster and hours changes (memory audit 9/29/26: "change_log").

"Which labor target applied last March?", "when did we switch auto-publish
on?", "who cleared the never-say list?" had no answer after six months: the
only record was an activity_log event with no actor column, pruned at 180
days, and a price typed on Food Cost or synced from Back Office left no
trace at all. So every advice built on data from before a change carried no
"changed since" caution.

One append-only table, kept FOREVER — it is deliberately in no retention
registry (ops._RETENTION_DAYS): the rows are tiny and rare, and the history
is the point. A row is never edited or deleted, except with its restaurant
(models.delete_restaurant: the restaurant's own data always goes).

    restaurant_id, kind, entity, field, subject,
    old_value, new_value      JSON text (what json.dumps made of the value)
    source                    owner | manager | admin | seeded | sync |
                              import | system | legacy
    actor_user_id, actor_role, via (the request endpoint or the job)
    changed_at                UTC, SQLite's form

Who: inside a request, the login the auth decorators resolved
(auth._bind_log_context puts it on flask.g), read through
permissions.answer_authority — a principal's change is the owner's, a
delegate's the manager's, an admin's (view-as included) Cavnar AI's.
Off a request, whatever `attributed(...)` says (a POS sync, the demo seed,
a target seeded from a published median), else "system".

Writers: models.update_restaurant writes its rows inside its own
transaction (record(..., conn=conn)); the roster (M3), menu and price (M6)
and goal (M2) writers call record() with `subject=` naming the thing.
Readers: rec_trust.owner_changes and outcomes.find_concurrent (the
"changed since" caution and the concurrent-change check), history() for
the Account change list. value_as_of (the target in force on a date) is
read by Ask's read_target_history tool (targets_as_of / target_changes —
"what was my food cost target in August?", Will, 9/29/26; memory re-audit
INVENTORY-12 had found it with no reader).
"""
import contextlib
import contextvars
import json
import logging

import models as _models      # constants only at import; get_conn is looked up at call time

log = logging.getLogger(__name__)

# ── the vocabulary ────────────────────────────────────────────────────────
SOURCES = ("owner", "manager", "admin", "seeded", "sync", "import", "system", "legacy")
# The authority permissions.answer_authority gives a login, as a source.
_AUTHORITY_SOURCE = {"principal": "owner", "delegate": "manager", "admin": "admin"}

KINDS = ("target", "never_say", "voice", "hours", "profile", "automation", "notifications", "rules", "pay",
         "setting", "price", "menu_add", "menu_remove", "menu_change", "roster_add", "roster_leave",
         "roster_change", "supplier", "goal")

# Every field models.update_restaurant may write, classified. A field here
# is recorded when its value changes; a field in RESTAURANT_UNTRACKED never
# is, each for the reason beside it. tests/test_mem_m7_change_log.py fails
# when update_restaurant's `allowed` gains a field in neither — a new
# setting is recorded from the day it ships, or somebody decided it is not.
_K = {
    # The owner's targets: models.OWNER_TARGET_FIELDS, named once there.
    "target": tuple(_models.OWNER_TARGET_FIELDS),
    "never_say": ("never_say",),
    "voice": ("voice_notes", "sign_off_name", "tone_preset", "response_language", "known_for", "vibe",
              "neighborhood", "brand_name", "brand_color", "brand_logo_url", "email_theme"),
    "hours": ("open_times_json", "close_times_json", "hours_notes", "skip_holidays"),
    "profile": ("service_model", "concept", "bar_led", "ownership", "opened_year", "category"),
    "automation": ("auto_approve_4star", "auto_approve_5star", "auto_approve_daily_cap", "auto_approve_earned",
                   "auto_approve_paused", "auto_draft_schedule", "auto_draft_weekday", "auto_order_trusted",
                   "auto_order_weekday", "auto_publish_schedule", "weekly_plan_enabled", "send_delay_minutes",
                   "trim_to_budget", "optin_invites_enabled", "reviews_live", "dsr_enabled",
                   "monthly_review_enabled", "digest_enabled"),
    "notifications": ("al_1star_email", "al_1star_push", "al_1star_sms", "al_2star_email", "al_2star_push",
                      "al_2star_sms", "al_3star_email", "al_3star_push", "al_3star_sms", "al_5star_email",
                      "al_5star_push", "al_5star_sms", "al_health_email", "al_health_push", "al_health_sms",
                      "al_spike_email", "al_spike_push", "al_spike_sms", "al_unres_email", "al_unres_push",
                      "al_unres_sms", "alert_1star", "alert_2star", "alert_3star", "alert_5star",
                      "alert_ai_visibility_drop", "alert_any_review", "alert_competitor_move",
                      "alert_extra_emails", "alert_food_waste", "alert_health", "alert_health_bypass_quiet",
                      "alert_hold_during_service", "alert_labor_over", "alert_max_per_day", "alert_neg_spike",
                      "alert_negative_trend", "alert_no_response", "alert_quiet_end", "alert_quiet_start",
                      "alert_rating_floor", "alert_rating_threshold", "alert_resp_approved", "urgent_via_email",
                      "urgent_via_sms", "morning_brief_enabled", "morning_brief_hour", "briefing_level",
                      "digest_day", "dsr_notify", "dsr_deadline_hour", "preshift_nudge_hour", "push_sound",
                      "login_notify", "staff_signin_notify", "marketing_emails_opt_out"),
    "rules": ("compliance_json", "cut_floor_default", "daypart_split", "delivery_pct", "foh_roles_json",
              "patio_roles_json", "jurisdiction", "quality_weights_json", "quality_tuning_json", "role_arrival_json",
              "role_close_buffer_json", "role_close_min_json", "role_cross_training_json", "role_floors_json",
              "role_minimums_json", "role_requirements_json", "kitchen_stations_json", "role_strength_json", "shift_leader_rules_json",
              "section_count", "foh_sections_json", "sched_notes",
              # Role families, the roles with chosen closers, the salaried
              # weekly cap (schedule audit 10/3/26 F1).
              "role_families_json", "closer_roles_json", "salaried_cap"),
    "pay": ("hourly_rate", "role_rates_json", "salaried_staff_json", "person_rates_json"),
    "setting": ("name", "owner_email", "owner_name", "owner_phone", "mailing_address", "location_group",
                "location_name", "timezone", "week_start_day", "fiscal_period_scheme", "fiscal_week_start_dow",
                "fiscal_year_start", "fiscal_years_json", "dsr_gross_basis", "dsr_late_night_hour", "data_retention_months",
                "inventory_frequency", "delivery_days", "inventory_notes", "menu_notes", "menu_url",
                "pos_system", "external_scheduling_tool", "reservation_provider", "custom_competitors",
                "google_place_id", "yelp_business_id", "exclude_from_learning", "learning_override", "is_demo"),
}
RESTAURANT_FIELD_KINDS = {f: k for k, fields in _K.items() for f in fields}

RESTAURANT_UNTRACKED = frozenset({
    # Credentials and connection secrets: never written anywhere a person
    # reads (credentials.FIELDS), and the connect routes record the event.
    "backoffice_api_key", "clover_api_token", "fb_page_token", "gmb_access_token", "gmb_refresh_token",
    "ig_token", "reservation_api_key", "rpower_token", "square_access_token", "toast_access_token",
    "toast_client_secret", "temp_password",
    # Integration plumbing: identifiers, expiries and sync state a connect
    # or a sync sets, not a choice anyone made.
    "backoffice_account_id", "clover_merchant_id", "square_location_id", "toast_client_id",
    "toast_restaurant_guid", "rpower_cg", "rpower_store_mid", "rpower_store_name", "rpower_verified_at",
    "gmb_account_id", "gmb_location_id", "gmb_token_expires", "gmb_revoked_at", "ig_user_id", "fb_page_id",
    "fb_page_name", "ig_username", "ga4_property_id", "gsc_site_url", "web_analytics_synced_at",
    "web_analytics_error",
    "ig_token_expires", "fb_token_expires", "toast_token_expires", "clover_last_synced", "clover_sync_error",
    "rpower_last_synced", "rpower_sync_error", "square_last_synced", "square_sync_error", "toast_last_synced",
    "toast_sync_error", "inventory_updated_at",
    # The account's own security: the security trail is activity_log's
    # (password, 2FA, device events), not a setting history.
    "two_fa_code", "two_fa_device_token", "two_fa_enabled", "two_fa_expires", "two_fa_method", "two_fa_pending",
    # The owner's grant of full control through view-as: set only by the
    # admin console's one route, whose admin_events row (record_admin_action,
    # before/after) is its history.
    "admin_control_until", "admin_control_note",
    # Billing and the contract: billing_status_history records every move
    # with its source and actor (models.BILLING_HISTORY_FIELDS).
    "billing_status", "pause_reason", "paused_until", "contract_status", "stripe_customer_id", "service_tier",
    "module_inventory", "module_labor", "module_marketing", "module_reviews", "docusign_envelope_id",
    "contract_signed_at", "converted_at",
    # Caches and fetched public facts (the own-rating history is its own
    # store): refreshed by jobs, never chosen.
    "competitor_intel", "competitor_updated_at", "weather_cache_json", "weather_cached_at", "geocode_failed_at",
    "latitude", "longitude", "gbp_rating", "gbp_rating_updated_at", "gbp_review_count", "google_types",
    "google_price_level",
    # Screen state.
    "last_active_tab", "last_activity", "changelog_seen_at", "notifications_seen_at", "onboarding_dismissed",
    # Stamps derived from a tracked change (the change's own row carries
    # them): who set a target and how, when a profile was confirmed, when a
    # demo was converted, from when a restaurant teaches learners.
    "labor_target_source", "food_cost_target_source", "hourly_rate_source", "target_setters_json",
    "profile_source", "profile_confirmed_at", "demo_cleared_at", "learning_since",
    # A consent acknowledgement is its own record (who and when).
    "optin_invites_ack_at", "optin_invites_ack_by",
    # The operator's private notes about the account (support_notes is the
    # console's append-only record).
    "internal_notes",
})

# ── who is changing it ────────────────────────────────────────────────────
_CTX = contextvars.ContextVar("cavnar_change_ctx", default=None)


@contextlib.contextmanager
def attributed(source=None, actor_user_id=None, role=None, via=None):
    """Attribute every change recorded inside the block — for work with no
    request behind it, or a request acting for someone else:

        with change_log.attributed(source="sync", via="pos:toast"):
            ...

    Nested blocks inherit what they do not set."""
    outer = _CTX.get() or {}
    merged = dict(outer)
    for k, v in (("source", source), ("actor_user_id", actor_user_id), ("role", role), ("via", via)):
        if v is not None:
            merged[k] = v
    token = _CTX.set(merged)
    try:
        yield merged
    finally:
        _CTX.reset(token)


def _request_user():
    """The login this request's auth decorator resolved, or None."""
    try:
        from flask import g, has_request_context
        if not has_request_context():
            return None
        return getattr(g, "cavnar_current_user", None)
    except Exception:
        return None


def _via():
    try:
        from flask import has_request_context, request
        if has_request_context():
            return ("request:" + (request.endpoint or request.path or "?"))[:120]
    except Exception:
        pass
    return None


def actor_context(user=None) -> dict:
    """{source, actor_user_id, actor_role, authority, via} for a change made
    now. `user` (a current_user dict) wins; then an attributed() block; then
    the request's signed-in login; else the system. An admin acting through
    view-as is the admin (the person behind it), never the owner."""
    ctx = dict(_CTX.get() or {})
    u = user if user is not None else (None if ctx.get("source") else _request_user())
    if u:
        import permissions
        authority = permissions.answer_authority(u)
        actor = u.get("acting_admin_id") or u.get("id")
        role = "view-as" if u.get("acting_admin_id") else (
            "admin" if u.get("is_admin") else str(u.get("role") or "").strip().lower() or None)
        out = {"source": _AUTHORITY_SOURCE.get(authority, "manager"), "actor_user_id": actor,
               "actor_role": role, "authority": authority, "via": ctx.get("via") or _via()}
        if ctx.get("source"):
            out["source"] = ctx["source"]
        return out
    source = ctx.get("source") or "system"
    authority = {"owner": "principal", "manager": "delegate", "admin": "admin"}.get(source)
    return {"source": source, "actor_user_id": ctx.get("actor_user_id"), "actor_role": ctx.get("role"),
            "authority": authority, "via": ctx.get("via") or _via()}


# ── writing ───────────────────────────────────────────────────────────────
_TABLE_SQL = """CREATE TABLE IF NOT EXISTS change_log (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    restaurant_id  INTEGER NOT NULL REFERENCES restaurants(id),
    kind           TEXT    NOT NULL,
    entity         TEXT    NOT NULL,
    field          TEXT,
    subject        TEXT,
    old_value      TEXT,
    new_value      TEXT,
    source         TEXT    NOT NULL,
    actor_user_id  INTEGER,
    actor_role     TEXT,
    via            TEXT,
    changed_at     TEXT    NOT NULL DEFAULT (datetime('now'))
)"""
_INDEXES = (
    "CREATE INDEX IF NOT EXISTS idx_change_log_rid ON change_log(restaurant_id, changed_at)",
    "CREATE INDEX IF NOT EXISTS idx_change_log_kind ON change_log(restaurant_id, kind, changed_at)",
    "CREATE INDEX IF NOT EXISTS idx_change_log_field ON change_log(restaurant_id, field, changed_at)",
)
BACKFILL_MIGRATION = "change_log_from_activity_log_v1"


def init_change_log(db_path=None):
    """The table and its indexes, at boot (models.init_db), then the one-time
    carry-over of the target and profile changes activity_log holds — the
    only record of them, due to be pruned 180 days after each was made."""
    import models
    conn = models.get_conn(db_path) if db_path else models.get_conn()
    try:
        conn.execute(_TABLE_SQL)
        for sql in _INDEXES:
            conn.execute(sql)
        conn.commit()
        _backfill_from_activity_log(conn)
    finally:
        conn.close()


def _backfill_from_activity_log(conn) -> int:
    """Once (data_migrations): every activity_log `target_change` and
    `profile_changed` row, as change_log rows with source 'legacy' (nobody
    was recorded). Idempotent; a failure is retried at the next boot."""
    try:
        have = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not {"activity_log", "data_migrations"} <= have:
            return 0
        conn.execute("BEGIN IMMEDIATE")
        if conn.execute("SELECT 1 FROM data_migrations WHERE name=?", (BACKFILL_MIGRATION,)).fetchone():
            conn.rollback()
            return 0
        n = 0
        for r in conn.execute("SELECT restaurant_id, event_type, event_data, created_at FROM activity_log "
                              "WHERE event_type IN ('target_change','profile_changed') "
                              "AND restaurant_id IN (SELECT id FROM restaurants) ORDER BY id").fetchall():
            try:
                data = json.loads(r[2] or "{}") or {}
            except (TypeError, ValueError):
                continue
            at = _utc_form(r[3])
            if r[1] == "target_change":
                changes = {data.get("field"): {"from": data.get("from"), "to": data.get("to")}}
            else:
                changes = data
            for field, ch in changes.items():
                if not field or not isinstance(ch, dict):
                    continue
                conn.execute(
                    "INSERT INTO change_log (restaurant_id, kind, entity, field, old_value, new_value, source, "
                    "via, changed_at) VALUES (?,?,?,?,?,?,?,?,?)",
                    (r[0], RESTAURANT_FIELD_KINDS.get(field, "setting"), "restaurant", field,
                     _dump(ch.get("from")), _dump(ch.get("to")), "legacy", "activity_log", at))
                n += 1
        conn.execute("INSERT INTO data_migrations (name, detail) VALUES (?, ?)",
                     (BACKFILL_MIGRATION, f"{n} target and profile changes carried over from activity_log"))
        conn.commit()
        return n
    except Exception as e:
        try:
            conn.rollback()
        except Exception:
            pass
        print(f"change_log backfill failed (will retry at next boot): {e}")
        return 0


def _utc_form(stamp):
    """activity_log stamps come in two shapes (ISO with a T, SQLite's space
    form); change_log keeps SQLite's."""
    s = str(stamp or "").strip().replace("T", " ")[:19]
    return s or None


def _dump(value):
    if value is None:
        return None
    try:
        return json.dumps(value, default=str, sort_keys=True)
    except (TypeError, ValueError):
        return json.dumps(str(value))


def _load(text):
    if text is None:
        return None
    try:
        return json.loads(text)
    except (TypeError, ValueError):
        return text


def _same(a, b) -> bool:
    """None and '' are both absent; numbers compare as numbers ("30" == 30.0)."""
    a = None if a == "" else a
    b = None if b == "" else b
    if a is None or b is None:
        return a is None and b is None
    try:
        return abs(float(a) - float(b)) < 1e-9
    except (TypeError, ValueError):
        pass
    return str(a) == str(b)


def kind_for(entity, field) -> str:
    """The kind of one change, from what changed."""
    e, f = str(entity or "").lower(), str(field or "").lower()
    if e == "restaurant":
        return RESTAURANT_FIELD_KINDS.get(field, "setting")
    if e in ("price", "sell_price") or f in ("sell_price", "price", "unit_price"):
        return "price"
    if e in ("menu", "menu_item"):
        return {"added": "menu_add", "add": "menu_add", "removed": "menu_remove", "remove": "menu_remove",
                "deactivated": "menu_remove", "active": "menu_change"}.get(f, "menu_change")
    if e in ("roster", "staff", "employee"):
        return {"added": "roster_add", "add": "roster_add", "joined": "roster_add", "hired": "roster_add",
                "left": "roster_leave", "removed": "roster_leave", "deactivated": "roster_leave",
                "leave": "roster_leave"}.get(f, "roster_change")
    if e in ("hours", "closure"):
        return "hours"
    if e == "supplier":
        return "supplier"
    if e == "goal":
        return "goal"
    if e in ("target",):
        return "target"
    return "setting"


def _split_entity(entity, subject):
    """record(rid, "menu_item:Salmon", ...) and record(rid, "menu_item",
    ..., subject="Salmon") mean the same thing."""
    e = str(entity or "").strip()
    if subject is None and ":" in e:
        e, subject = e.split(":", 1)
    return (e or "setting")[:40], (str(subject)[:200] if subject not in (None, "") else None)


def record(restaurant_id, entity, field, before, after, actor_user_id=None, source=None, db_path=None,
           subject=None, kind=None, conn=None, role=None, via=None, user=None, errors=None):
    """One change: entity ("restaurant" | "menu_item" | "price" | "roster" |
    "hours" | "supplier" | "goal" | ...), field, before/after (JSON-able),
    who and through what (source: owner | manager | admin | seeded | sync |
    import | system; default: whoever this request or attributed() block
    is). `subject` names the thing changed (a dish, an employee, a
    supplier). A value that did not change records nothing. Returns the row
    id, or None. Never raises into the caller's write.

    With `conn` the row is written on the caller's connection, inside the
    caller's transaction, uncommitted — and a failure is NOT reported from
    here (a report is a write on a second connection, which would wait on
    the lock the caller holds): it is appended to `errors` for the caller
    to report after its commit."""
    err = None
    try:
        if _same(before, after):
            return None
        ent, subj = _split_entity(entity, subject)
        ctx = actor_context(user)
        src = source or ctx["source"]
        if src not in SOURCES:
            src = "system"
        row = (int(restaurant_id), kind or kind_for(ent, field), ent, (str(field)[:80] if field else None), subj,
               _dump(before), _dump(after), src,
               actor_user_id if actor_user_id is not None else ctx.get("actor_user_id"),
               role or ctx.get("actor_role"), (via or ctx.get("via") or None))
        sql = ("INSERT INTO change_log (restaurant_id, kind, entity, field, subject, old_value, new_value, source, "
               "actor_user_id, actor_role, via) VALUES (?,?,?,?,?,?,?,?,?,?,?)")
        if conn is not None:
            return conn.execute(sql, row).lastrowid
        import models
        c = models.get_conn(db_path) if db_path else models.get_conn()
        try:
            new_id = c.execute(sql, row).lastrowid
            c.commit()
            return new_id
        finally:
            c.close()
    except Exception as e:
        err = e
    log.warning("change_log: not recorded for restaurant %s (%s.%s): %s", restaurant_id, entity, field, err)
    if conn is not None:
        if errors is not None:
            errors.append(err)
        return None
    report(err, restaurant_id, entity, field, db_path=db_path)
    return None


def report(err, restaurant_id, entity=None, field=None, db_path=None):
    """A change that could not be recorded, where the console sees it."""
    try:
        import ops
        ops.capture(err, job="change_log", context=f"restaurant_id={restaurant_id} {entity}.{field}",
                    db_path=db_path)
    except Exception:
        pass


def record_restaurant_changes(conn, restaurant_id, old: dict, new: dict, sources: dict = None,
                              user=None, errors=None) -> int:
    """models.update_restaurant's rows, on its connection inside its
    transaction: every tracked field whose value changed. `sources` names a
    field's source where it is not the actor's (a target seeded from a
    published median is 'seeded'). Failures go to `errors` (see record).
    Returns rows written."""
    n = 0
    sources = sources or {}
    ctx = None
    for field, after in (new or {}).items():
        if field not in RESTAURANT_FIELD_KINDS:
            continue
        before = (old or {}).get(field)
        if _same(before, after):
            continue
        if ctx is None:
            ctx = actor_context(user)
        if record(restaurant_id, "restaurant", field, before, after, conn=conn,
                  source=sources.get(field) or ctx["source"], actor_user_id=ctx.get("actor_user_id"),
                  role=ctx.get("actor_role"), via=ctx.get("via"), errors=errors) is not None:
            n += 1
    return n


# ── reading ───────────────────────────────────────────────────────────────

def _row(r) -> dict:
    d = dict(r)
    d["old_value"], d["new_value"] = _load(d.get("old_value")), _load(d.get("new_value"))
    return d


def history(restaurant_id, kinds=None, field=None, since=None, until=None, limit=200, db_path=None) -> list:
    """The restaurant's changes, newest first: [{id, kind, entity, field,
    subject, old_value, new_value, source, actor_user_id, actor_role, via,
    changed_at}]. `since`/`until` are UTC stamps or dates (inclusive)."""
    import models
    where, args = ["restaurant_id=?"], [int(restaurant_id)]
    if kinds:
        kinds = [kinds] if isinstance(kinds, str) else list(kinds)
        where.append(f"kind IN ({','.join('?' for _ in kinds)})")
        args += kinds
    if field:
        where.append("field=?")
        args.append(field)
    if since:
        where.append("changed_at >= ?")
        args.append(str(since).replace("T", " ")[:19])
    if until:
        u = str(until).replace("T", " ")[:19]
        where.append("changed_at <= ?" if len(u) > 10 else "changed_at < date(?, '+1 day')")
        args.append(u)
    conn = models.get_conn(db_path) if db_path else models.get_conn()
    try:
        rows = conn.execute(f"SELECT * FROM change_log WHERE {' AND '.join(where)} "
                            f"ORDER BY changed_at DESC, id DESC LIMIT ?", (*args, int(limit))).fetchall()
    except Exception as e:
        log.warning("change_log history unreadable for %s: %s", restaurant_id, e)
        return []
    finally:
        conn.close()
    return [_row(r) for r in rows]


def changes_since(restaurant_id, since, kinds=None, until=None, db_path=None, limit=200) -> list:
    """history() from `since` (a date or stamp), oldest first."""
    return list(reversed(history(restaurant_id, kinds=kinds, since=since, until=until, limit=limit,
                                 db_path=db_path)))


def value_as_of(restaurant_id, field, when, db_path=None, current=None):
    """The value `field` held on `when` (a date: the end of that day; or a
    stamp) — "which labor target applied last March?". The newest change
    at or before it gives its new value; with none, the oldest change after
    it gives its OLD value (what held before anyone changed it); with no
    change at all, `current` (the column as it is now). A bare date is the
    RESTAURANT's day: changed_at is UTC, so the bound is the end of that
    local day in UTC (a 9pm Central change is already tomorrow in UTC).
    Returns {"value", "since" (the change's stamp or None), "source"}."""
    import models
    w = str(when).replace("T", " ")[:19]
    bound = w if len(w) > 10 else _local_day_end_utc(restaurant_id, w)
    conn = models.get_conn(db_path) if db_path else models.get_conn()
    try:
        before = conn.execute("SELECT new_value, changed_at, source FROM change_log WHERE restaurant_id=? AND field=? "
                              "AND changed_at <= ? ORDER BY changed_at DESC, id DESC LIMIT 1",
                              (int(restaurant_id), field, bound)).fetchone()
        if before:
            return {"value": _load(before["new_value"]), "since": before["changed_at"], "source": before["source"]}
        after = conn.execute("SELECT old_value FROM change_log WHERE restaurant_id=? AND field=? "
                             "AND changed_at > ? ORDER BY changed_at ASC, id ASC LIMIT 1",
                             (int(restaurant_id), field, bound)).fetchone()
    except Exception as e:
        log.warning("change_log value_as_of unreadable for %s: %s", restaurant_id, e)
        return {"value": current, "since": None, "source": None}
    finally:
        conn.close()
    if after:
        return {"value": _load(after["old_value"]), "since": None, "source": None}
    return {"value": current, "since": None, "source": None}


SOURCE_LABELS = {"owner": "the owner", "manager": "a manager", "admin": "Cavnar AI", "seeded": "Cavnar AI (seeded)",
                 "sync": "a sync", "import": "an import", "system": "Cavnar AI", "legacy": None}

# The words an owner reads for the fields most often changed; any other
# field is its column name in plain words.
FIELD_LABELS = {
    # models.OWNER_TARGET_FIELDS, in its order (labor, food cost, waste, revenue).
    **dict(zip(_models.OWNER_TARGET_FIELDS, ("Labor target", "Food cost target", "Waste target",
                                            "Monthly revenue target"))),
    "never_say": "Never-say list", "voice_notes": "Brand voice",
    "hourly_rate": "Blended hourly rate", "role_rates_json": "Pay rates by role", "salaried_staff_json": "Salaried staff",
    "open_times_json": "Opening times", "close_times_json": "Closing times", "hours_notes": "Hours notes",
    "auto_publish_schedule": "Auto-publish schedules", "auto_approve_5star": "Auto-approve 5-star replies",
    "auto_approve_4star": "Auto-approve 4-star replies", "auto_draft_schedule": "Auto-draft schedules",
    "compliance_json": "Scheduling rules", "role_floors_json": "Staffing floors", "sched_notes": "Scheduling notes",
    "data_retention_months": "Review retention (months)", "timezone": "Time zone",
    "sell_price": "Price", "service_model": "Service model", "concept": "Restaurant type",
}
# Kinds whose values are switches: 1/0 read as on/off.
_SWITCH_KINDS = frozenset({"automation", "notifications"})


def _show(v, switch=False) -> str:
    if v is None or v == "":
        return "—"
    if isinstance(v, (dict, list)):
        return "updated"
    if switch and str(v) in ("0", "1", "True", "False", "true", "false"):
        return "on" if str(v).lower() in ("1", "true") else "off"
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    s = str(v)
    return s if len(s) <= 40 else s[:37] + "…"


def describe(row) -> str:
    """One owner-facing line for a change: "Labor target: 30 → 28, by the
    owner on 9/12/26". Dates M/D/YY."""
    from time_utils import mdy
    raw = str(row.get("field") or row.get("entity") or "setting")
    field = FIELD_LABELS.get(raw) or raw.replace("_json", "").replace("_", " ")
    what = f"{row['subject']} — {field}" if row.get("subject") else field
    switch = row.get("kind") in _SWITCH_KINDS
    who = SOURCE_LABELS.get(row.get("source"))
    tail = (f", by {who}" if who else "") + (f" on {mdy(row.get('changed_at'))}" if row.get("changed_at") else "")
    return (f"{what[:1].upper()}{what[1:]}: {_show(row.get('old_value'), switch)} → "
            f"{_show(row.get('new_value'), switch)}{tail}")


def _restaurant_tz(restaurant_id):
    try:
        from time_utils import restaurant_now_by_id
        return restaurant_now_by_id(restaurant_id).tzinfo
    except Exception:
        return None


def _local_day_end_utc(restaurant_id, day) -> str:
    """The last second of the restaurant's local `day` (YYYY-MM-DD) as the
    UTC stamp change_log stores; the plain end of day when the restaurant's
    zone is unknown."""
    from datetime import date as _date, datetime as _dt, time as _time, timezone as _tz
    tz = _restaurant_tz(restaurant_id)
    try:
        d = _date.fromisoformat(str(day)[:10])
    except ValueError:
        return f"{str(day)[:10]} 23:59:59"
    if tz is None:
        return f"{d.isoformat()} 23:59:59"
    local = _dt.combine(d, _time(23, 59, 59)).replace(tzinfo=tz)
    return local.astimezone(_tz.utc).strftime("%Y-%m-%d %H:%M:%S")


def _local_mdy(restaurant_id, stamp) -> str:
    """A UTC changed_at as M/D/YY on the restaurant's own day."""
    from datetime import datetime as _dt, timezone as _tz
    from time_utils import mdy
    try:
        at = _dt.fromisoformat(str(stamp).replace("T", " ")[:19]).replace(tzinfo=_tz.utc)
    except ValueError:
        return mdy(stamp)
    tz = _restaurant_tz(restaurant_id)
    return mdy((at.astimezone(tz) if tz else at).date().isoformat())


def _show_target(field, value) -> str:
    """A target as an owner reads it: "28%" or "$368,333 a month ($85,000 a
    week)". Zero or empty is "not set"."""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return "not set" if value in (None, "") else str(value)
    if not v:
        return "not set"
    if field == "monthly_revenue_target":
        from metrics import WEEKS_PER_MONTH
        return f"${v:,.0f} a month (${v / WEEKS_PER_MONTH:,.0f} a week)"
    return f"{v:g}%"


def targets_as_of(restaurant_id, when, fields=None, current=None, db_path=None) -> list:
    """Which owner targets applied on `when` (a date on the restaurant's
    day, or a UTC stamp): value_as_of for each field (default every one of
    models.OWNER_TARGET_FIELDS). `current` maps field → the column now, the
    answer when it was never changed. [{field, label, value, shown, set_on
    (M/D/YY on the restaurant's day, or None when it predates the record),
    set_by}]."""
    out = []
    for f in (fields or _models.OWNER_TARGET_FIELDS):
        got = value_as_of(restaurant_id, f, when, db_path=db_path, current=(current or {}).get(f))
        out.append({"field": f, "label": FIELD_LABELS.get(f, f), "value": got["value"],
                    "shown": _show_target(f, got["value"]),
                    "set_on": _local_mdy(restaurant_id, got["since"]) if got["since"] else None,
                    "set_by": SOURCE_LABELS.get(got["source"]) if got["since"] else None})
    return out


# Back-to-back changes to one target this close together are one edit — a
# stepper clicked five times (Simple EJ's waste target, 9/28/26: 2.5 → 3 →
# 3.5 → 4 → 4.5 → 5 in one sitting) reads as "2.5% → 5%".
EDIT_BURST_MINUTES = 10


def target_changes(restaurant_id, fields=None, limit=20, db_path=None) -> list:
    """The owner targets' changes, newest first: [{field, label, from, to,
    on (M/D/YY, the restaurant's day), by}]. Consecutive changes to the same
    target by the same source within EDIT_BURST_MINUTES are one change, from
    the first value to the last."""
    from datetime import datetime as _dt
    fields = set(fields or _models.OWNER_TARGET_FIELDS)
    rows = [r for r in history(restaurant_id, kinds="target", limit=500, db_path=db_path)
            if r.get("field") in fields]

    def _at(r):
        try:
            return _dt.fromisoformat(str(r["changed_at"]).replace("T", " ")[:19])
        except ValueError:
            return None
    merged = []                                   # oldest first while merging
    for r in reversed(rows):
        last = merged[-1] if merged else None
        a, b = (_at(last["_end"]) if last else None), _at(r)
        if (last and last["field"] == r["field"] and last["_end"].get("source") == r.get("source")
                and a and b and (b - a).total_seconds() <= EDIT_BURST_MINUTES * 60):
            last["_end"] = r
            continue
        merged.append({"field": r["field"], "_start": r, "_end": r})
    out = []
    for m in reversed(merged):
        f, first, end = m["field"], m["_start"], m["_end"]
        if _show_target(f, first.get("old_value")) == _show_target(f, end.get("new_value")):
            continue                              # changed and changed back: no change
        out.append({"field": f, "label": FIELD_LABELS.get(f, f), "from": _show_target(f, first.get("old_value")),
                    "to": _show_target(f, end.get("new_value")), "on": _local_mdy(restaurant_id, end["changed_at"]),
                    "by": SOURCE_LABELS.get(end.get("source"))})
        if len(out) >= limit:
            break
    return out


# ── what a change means to the learning readers ───────────────────────────
# The metric families (metrics.FAMILIES) a change of each kind moves, for
# outcomes.find_concurrent: a price typed on Food Cost, a dish taken off,
# someone leaving, new hours or new pay rates in a tracker's window is
# another change on the same number, and caps its attribution at
# "associated". Targets, voice, notifications and rules move no number.
_PRICE_MOVED = frozenset({"labor_cost", "food_cost", "sales"})
CONFOUNDING_FAMILIES = {
    "price": _PRICE_MOVED, "menu_add": _PRICE_MOVED, "menu_remove": _PRICE_MOVED, "menu_change": _PRICE_MOVED,
    "roster_add": frozenset({"labor_cost"}), "roster_leave": frozenset({"labor_cost"}),
    "roster_change": frozenset({"labor_cost"}), "pay": frozenset({"labor_cost"}),
    "hours": frozenset({"labor_cost", "food_cost", "sales", "comps", "voids"}),
    "supplier": frozenset({"food_cost"}),
}
# The data sources (data_freshness keys) each kind touches, for
# rec_trust.owner_changes' "changed since your data" caution.
def _target_field_sources() -> dict:
    """{target field: (data_freshness key, "your labor target")} — rec_trust's
    own map, read from there so the two cannot disagree."""
    import rec_trust
    return rec_trust._TARGET_SOURCES
CHANGE_SOURCES = {"price": ("inventory", "sales"), "menu_add": ("inventory", "sales"),
                  "menu_remove": ("inventory", "sales"), "menu_change": ("inventory", "sales"),
                  "supplier": ("inventory", "purchases"), "roster_add": ("labor",), "roster_leave": ("labor",),
                  "roster_change": ("labor",), "pay": ("labor",), "hours": ("labor", "sales")}
# Who made it, as the subject of an owner-facing sentence. A change carried
# over with nobody recorded reads as it always did ("You").
WHO = {"owner": "You", "manager": "A manager", "admin": "Cavnar AI", "seeded": "Cavnar AI",
       "sync": "A sync", "import": "An import", "system": "Cavnar AI", "legacy": "You"}


def _what(kind, field, subject, n=1) -> str:
    """The verb phrase of one change (or `n` of a kind)."""
    s = (subject or "").strip()
    if kind == "target":
        return f"changed {_target_field_sources().get(field, ('', 'a target'))[1]}"
    if n > 1:
        return {"price": f"changed {n} prices", "menu_add": f"added {n} dishes to the menu",
                "menu_remove": f"took {n} dishes off the menu", "menu_change": f"changed {n} menu items",
                "roster_add": f"added {n} people to the team", "roster_leave": f"took {n} people off the team",
                "roster_change": f"changed {n} team members' details", "supplier": f"changed {n} suppliers",
                "pay": "changed pay rates", "hours": "changed the hours"}.get(kind, f"made {n} changes")
    return {"price": f"changed the {s} price" if s else "changed a price",
            "menu_add": f"added {s} to the menu" if s else "added a dish to the menu",
            "menu_remove": f"took {s} off the menu" if s else "took a dish off the menu",
            "menu_change": f"changed {s} on the menu" if s else "changed a menu item",
            "roster_add": f"added {s} to the team" if s else "added someone to the team",
            "roster_leave": f"took {s} off the team" if s else "took someone off the team",
            "roster_change": f"changed {s}'s details" if s else "changed a team member's details",
            "supplier": f"changed supplier {s}" if s else "changed a supplier",
            "pay": "changed pay rates", "hours": "changed the hours"}.get(kind, "changed a setting")


def owner_change_entries(restaurant_id, since, conn=None, db_path=None) -> list:
    """rec_trust.owner_changes' rows from the log since `since`: a changed
    target (newest per target), and the newest of each kind of price, menu,
    roster, pay, hours and supplier change, with how many there were —
    [{what, who, at (ISO date), sources, kind: None, change_kind}]."""
    import models
    own = conn is None
    c = (models.get_conn(db_path) if db_path else models.get_conn()) if own else conn
    try:
        rows = c.execute("SELECT kind, field, subject, source, changed_at FROM change_log WHERE restaurant_id=? "
                         "AND changed_at >= ? AND kind IN (" + ",".join("?" for _ in ("target", *CHANGE_SOURCES))
                         + ") ORDER BY changed_at DESC, id DESC",
                         (int(restaurant_id), str(since)[:19], "target", *CHANGE_SOURCES)).fetchall()
    finally:
        if own:
            c.close()
    out, seen, counts = [], set(), {}
    for r in rows:
        counts[r["kind"]] = counts.get(r["kind"], 0) + 1
    for r in rows:
        k = r["kind"]
        group = (k, r["field"]) if k == "target" else (k,)
        if group in seen:
            continue
        seen.add(group)
        if k == "target":
            targets = _target_field_sources()
            if r["field"] not in targets:
                continue
            sources = (targets[r["field"]][0],)
            what = _what(k, r["field"], None)
        else:
            sources = CHANGE_SOURCES[k]
            what = _what(k, r["field"], r["subject"], counts.get(k, 1))
        out.append({"what": what, "who": WHO.get(r["source"], "You"), "at": str(r["changed_at"])[:10],
                    "sources": sources, "kind": None, "change_kind": k})
    return out


def concurrent_changes(restaurant_id, family, start, end, conn=None, db_path=None, skip_prices=None) -> list:
    """outcomes.find_concurrent's rows from the log: every change in
    [start, end] (ISO dates, inclusive) of a kind that moves `family` —
    [{kind: "<change kind>_change", label, date}]. `skip_prices` is a set of
    (subject lower-cased, date) already listed from reprice_decisions."""
    kinds = [k for k, fams in CONFOUNDING_FAMILIES.items() if family in fams]
    if not kinds:
        return []
    import models
    own = conn is None
    c = (models.get_conn(db_path) if db_path else models.get_conn()) if own else conn
    try:
        rows = c.execute("SELECT kind, field, subject, source, changed_at FROM change_log WHERE restaurant_id=? "
                         "AND changed_at >= ? AND changed_at < date(?, '+1 day') AND kind IN ("
                         + ",".join("?" for _ in kinds) + ") ORDER BY changed_at",
                         (int(restaurant_id), str(start)[:10], str(end)[:10], *kinds)).fetchall()
    finally:
        if own:
            c.close()
    out = []
    for r in rows:
        day = str(r["changed_at"])[:10]
        subj = (r["subject"] or "").strip()
        if r["kind"] == "price" and skip_prices and (subj.lower(), day) in skip_prices:
            continue
        out.append({"kind": f"{r['kind']}_change" if r["kind"] != "price" else "price_change",
                    "label": _noun(r["kind"], subj), "date": day})
    return out


def _noun(kind, subject) -> str:
    """A change as a short noun phrase for "Also changed these weeks: …" —
    the same form as outcomes' own "<dish> repriced"."""
    s = (subject or "").strip()
    return {"price": f"{s or 'A dish'} repriced",
            "menu_add": f"{s} added to the menu" if s else "A dish added to the menu",
            "menu_remove": f"{s} taken off the menu" if s else "A dish taken off the menu",
            "menu_change": f"{s} changed on the menu" if s else "A menu item changed",
            "roster_add": f"{s} joined the team" if s else "Someone joined the team",
            "roster_leave": f"{s} left the team" if s else "Someone left the team",
            "roster_change": f"{s}'s details changed" if s else "A team member's details changed",
            "pay": "Pay rates changed", "hours": "Hours changed",
            "supplier": f"Supplier {s} changed" if s else "A supplier changed"}.get(kind, "A setting changed")


# ── what a login may read of it ───────────────────────────────────────────
# Pay (a salary, a pay rate) is the account holder's alone — the salaried
# staff figure already is; the learning switches are the operator's.
_PRINCIPAL_ONLY_KINDS = frozenset({"pay"})
_OPERATOR_ONLY_FIELDS = frozenset({"exclude_from_learning", "learning_override", "is_demo"})


def for_viewer(restaurant_id, user, limit=100, db_path=None) -> list:
    """The change history as `user` may see it, newest first — the Account
    activity payload's `changes`: [{kind, field, subject, old_value,
    new_value, source, who, changed_at, line}], `line` the owner-facing
    sentence (dates M/D/YY). A delegate never sees pay changes; nobody but
    an internal login sees the operator's learning switches."""
    import permissions
    authority = permissions.answer_authority(user) if user else "delegate"
    principal = authority in ("principal", "admin")
    out = []
    for row in history(restaurant_id, limit=limit * 2, db_path=db_path):
        if row.get("kind") in _PRINCIPAL_ONLY_KINDS and not principal:
            continue
        if row.get("field") in _OPERATOR_ONLY_FIELDS and authority != "admin":
            continue
        out.append({"kind": row.get("kind"), "field": row.get("field"), "subject": row.get("subject"),
                    "old_value": row.get("old_value"), "new_value": row.get("new_value"),
                    "source": row.get("source"), "who": SOURCE_LABELS.get(row.get("source")),
                    "changed_at": row.get("changed_at"), "line": describe(row)})
        if len(out) >= limit:
            break
    return out
