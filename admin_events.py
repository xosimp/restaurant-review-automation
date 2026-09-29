"""
admin_events.py — the events the business runs on, kept.

Stripe and DocuSign webhooks used to act (flip billing_status, email Will)
and forget. Every event now lands here too, so the admin console's Billing
page and a client's Subscription tab can show payment and contract history
instead of only the current flag.

It is also the admin audit trail (record_admin_action and the request hook,
below), and holds the support workspace's own ledgers: support notes and
in-app bug reports.
"""
import json

def _ensure(conn):
    """admin_events is created by models.init_db now; kept so callers read the same."""
    return None


def _resolve_restaurant(conn, customer_id=None, email=None, envelope_id=None):
    try:
        if customer_id:
            r = conn.execute("SELECT id FROM restaurants WHERE stripe_customer_id=? LIMIT 1", (customer_id,)).fetchone()
            if r:
                return r[0]
        if envelope_id:
            r = conn.execute("SELECT id FROM restaurants WHERE docusign_envelope_id=? LIMIT 1", (envelope_id,)).fetchone()
            if r:
                return r[0]
        if email:
            r = conn.execute("SELECT r.id FROM restaurants r LEFT JOIN users u ON u.restaurant_id=r.id "
                             "WHERE lower(r.owner_email)=lower(?) OR lower(u.email)=lower(?) LIMIT 1", (email, email)).fetchone()
            if r:
                return r[0]
    except Exception:
        pass
    return None


def record(source, event_type, restaurant_id=None, customer_id=None, email=None, amount=None,
           summary=None, payload=None, envelope_id=None, db_path=None):
    """Never raises — a history table must not break the webhook it records."""
    try:
        from models import get_conn, DB_PATH
        conn = get_conn(db_path or DB_PATH)
        _ensure(conn)
        if restaurant_id is None:
            restaurant_id = _resolve_restaurant(conn, customer_id, email, envelope_id)
        body = None
        if payload is not None:
            try:
                body = json.dumps(payload, default=str)[:4000]
            except Exception:
                body = str(payload)[:4000]
        conn.execute(
            "INSERT INTO admin_events (source, event_type, restaurant_id, customer_id, email, amount, summary, payload) VALUES (?,?,?,?,?,?,?,?)",
            (source, event_type, restaurant_id, customer_id, email, amount, (summary or "")[:300], body),
        )
        conn.commit()
        conn.close()
        return True
    except Exception:
        return False


def _stripe_amount_cents(t, obj):
    """The money an event is about: what an invoice collected (paid) or
    asks for, what a refund returned — amount_refunded, never the charge's
    full amount (#50: a $5 goodwill refund on a $750 charge read "$750.00
    refunded") — and what a dispute holds."""
    if t.startswith("invoice."):
        return (obj.get("amount_paid") if t == "invoice.paid" else obj.get("amount_due")) or 0
    if t == "charge.refunded":
        return obj.get("amount_refunded") or 0
    if t.startswith("charge."):
        return obj.get("amount") or 0
    return None


def _stripe_summary(event):
    t = event.get("type", "")
    obj = (event.get("data") or {}).get("object") or {}
    if t.startswith("invoice."):
        amt = _stripe_amount_cents(t, obj)
        return f"{t.split('.', 1)[1].replace('_', ' ')} · ${amt / 100:,.2f}" + (f" · attempt {obj.get('attempt_count')}" if obj.get("attempt_count") else "")
    if t.startswith("customer.subscription."):
        status = obj.get("status") or ""
        return f"subscription {t.rsplit('.', 1)[1]}" + (f" · {status}" if status else "")
    if t.startswith("checkout.session."):
        return f"checkout {t.rsplit('.', 1)[1]}"
    if t == "charge.refunded":
        full = obj.get("amount") or 0
        back = obj.get("amount_refunded") or 0
        return (f"charge refunded · ${back / 100:,.2f}" + ("" if back >= full > 0 else f" of ${full / 100:,.2f}"))
    if t.startswith("charge."):
        return f"charge {t.split('.', 1)[1].replace('.', ' ')} · ${(obj.get('amount') or 0) / 100:,.2f}"
    return t


def _stripe_restaurant(conn, event):
    """Which restaurant a Stripe event is about (#50) — the same order the
    webhook acts in: the restaurant_id Stripe carries back in metadata (set
    at checkout; survives an owner changing their email), then the
    subscription the event belongs to (the stripe_subscriptions mirror),
    then the invoice or charge we already hold, then the stored customer id.
    An email is the last resort, and only for this ledger's attribution."""
    import billing_jobs as _bj
    obj = (event.get("data") or {}).get("object") or {}
    t = event.get("type", "")

    def _valid(rid):
        try:
            rid = int(str(rid).strip())
        except (TypeError, ValueError):
            return None
        r = conn.execute("SELECT id FROM restaurants WHERE id=?", (rid,)).fetchone()
        return r[0] if r else None

    meta = obj.get("metadata") or {}
    if t.startswith("invoice."):
        meta = _bj.invoice_metadata(obj) or meta
    rid = _valid(meta.get("restaurant_id")) if meta.get("restaurant_id") else None
    if rid:
        return rid
    sub_id = obj.get("id") if t.startswith("customer.subscription.") else (
        _bj.invoice_facts(obj).get("subscription_id") if t.startswith("invoice.") else obj.get("subscription"))
    if isinstance(sub_id, str) and sub_id:
        r = conn.execute("SELECT restaurant_id FROM stripe_subscriptions WHERE subscription_id=?",
                         (sub_id,)).fetchone()
        if r:
            return r[0]
    if t.startswith("charge."):
        charge_id = obj.get("charge") if t.startswith("charge.dispute.") else obj.get("id")
        for col, val in (("charge_id", charge_id), ("invoice_id", obj.get("invoice"))):
            if isinstance(val, str) and val:
                r = conn.execute(f"SELECT restaurant_id FROM stripe_invoices WHERE {col}=? "
                                 "AND restaurant_id IS NOT NULL LIMIT 1", (val,)).fetchone()
                if r:
                    return r[0]
    customer = obj.get("customer") if isinstance(obj.get("customer"), str) else None
    if customer:
        r = conn.execute("SELECT id FROM restaurants WHERE stripe_customer_id=? "
                         "ORDER BY (id IN (SELECT restaurant_id FROM stripe_subscriptions)) DESC, id LIMIT 1",
                         (customer,)).fetchone()
        if r:
            return r[0]
    email = (obj.get("customer_email") or (obj.get("customer_details") or {}).get("email")
             or (obj.get("billing_details") or {}).get("email"))
    return _resolve_restaurant(conn, None, email, None) if email else None


def record_stripe(event, restaurant_id=None, db_path=None):
    """One line per Stripe event, however often Stripe delivers it (#50).

    The webhook calls this after its duplicate check, and the row carries the
    event id as external_id under a UNIQUE (source, external_id) index — so a
    redelivery, or the retry after a failed dispatch, never adds a second
    line. Invoice rows carry the invoice's facts (subtotal, discount, tax,
    currency, setup vs recurring) for the Billing history. Never raises."""
    try:
        from models import get_conn, DB_PATH
        import billing_jobs as _bj
        obj = (event.get("data") or {}).get("object") or {}
        t = event.get("type", "")
        conn = get_conn(db_path or DB_PATH)
        try:
            rid = restaurant_id or _stripe_restaurant(conn, event)
            cents = _stripe_amount_cents(t, obj)
            amount = (cents / 100) if cents is not None else None
            email = obj.get("customer_email") or (obj.get("customer_details") or {}).get("email")
            customer = obj.get("customer") if isinstance(obj.get("customer"), str) else None
            payload = {"id": event.get("id"), "type": t, "status": obj.get("status"),
                       "billing_reason": obj.get("billing_reason"), "subscription": obj.get("subscription"),
                       "hosted_invoice_url": obj.get("hosted_invoice_url"),
                       "next_payment_attempt": obj.get("next_payment_attempt"),
                       "cancel_at_period_end": obj.get("cancel_at_period_end")}
            if t.startswith("invoice."):
                f = _bj.invoice_facts(obj)
                payload.update({k: f.get(k) for k in ("subscription_id", "kind", "currency", "subtotal_cents",
                                                      "discount_cents", "tax_cents", "total_cents",
                                                      "setup_cents", "recurring_cents", "attempt_count")})
            elif t.startswith("customer.subscription."):
                details = obj.get("cancellation_details") or {}
                payload.update({"cancellation_reason": details.get("reason"),
                                "canceled_at": obj.get("canceled_at"), "ended_at": obj.get("ended_at"),
                                "changed": sorted(((event.get("data") or {}).get("previous_attributes") or {}).keys())})
            elif t == "charge.refunded":
                payload.update({"charge_amount": obj.get("amount"), "amount_refunded": obj.get("amount_refunded"),
                                "full_refund": bool(obj.get("refunded"))})
            elif t.startswith("charge.dispute."):
                payload.update({"charge": obj.get("charge"), "reason": obj.get("reason")})
            try:
                body = json.dumps(payload, default=str)[:4000]
            except Exception:
                body = str(payload)[:4000]
            conn.execute(
                "INSERT OR IGNORE INTO admin_events (source, event_type, restaurant_id, customer_id, email, amount, "
                "summary, payload, external_id) VALUES (?,?,?,?,?,?,?,?,?)",
                ("stripe", t, rid, customer, email, amount, _stripe_summary(event)[:300], body,
                 event.get("id") or None))
            conn.commit()
        finally:
            conn.close()
        return True
    except Exception:
        return False


def recent(limit=100, restaurant_id=None, db_path=None, sources=None, exclude_sources=None):
    """The newest events, optionally for one restaurant and only (or never)
    from the given sources. The billing history reads ("stripe",
    "docusign") only: every admin write is a row here too, and unfiltered
    they pushed the payment and contract events off the page (#53)."""
    from models import get_conn, DB_PATH
    conn = get_conn(db_path or DB_PATH)
    _ensure(conn)
    where, args = [], []
    if restaurant_id:
        where.append("e.restaurant_id=?")
        args.append(restaurant_id)
    if sources:
        where.append("e.source IN (%s)" % ",".join("?" * len(sources)))
        args.extend(sources)
    if exclude_sources:
        where.append("e.source NOT IN (%s)" % ",".join("?" * len(exclude_sources)))
        args.extend(exclude_sources)
    sql = ("SELECT e.*, r.name AS restaurant FROM admin_events e LEFT JOIN restaurants r ON r.id=e.restaurant_id"
           + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY e.id DESC LIMIT ?")
    rows = conn.execute(sql, tuple(args) + (limit,)).fetchall()
    out = [dict(r) for r in rows]
    conn.close()
    for o in out:
        o.pop("payload", None)
    return out


# ── The admin audit trail (fix round B2: #53, #72, #126) ─────────────────────
#
# Two kinds of row, both in admin_events:
#
#   * source 'admin' — a TYPED action a route records itself through
#     record_admin_action(): who, what, which client, which object, the state
#     before and after (secrets redacted), whether it worked, and the IP.
#   * source 'audit' — one row per admin write request, written by the
#     request hook below AFTER the response exists: the endpoint, the actor,
#     the status it ended with, the target ids and the (redacted) body. It is
#     what covers a route that records nothing of its own, and a refusal
#     (a support login's 403, a failed step-up) that never reached the route.
#
# Both carry the request's id, so the audit view can fold a request's
# generic row under the typed action it produced.
#
# Rows are written only once an actor is resolved: a console login (admin or
# support) that passed CSRF. Anonymous, non-admin and CSRF-refused attempts
# go to admin_audit_refused instead — capped, pruned and rate-limited per IP —
# because the old hook wrote an admin_events row BEFORE authentication, so
# any anonymous POST (with path text the caller chose) became a permanent
# audit row that pushed real events out of every view (SECURITY-4).
#
# delete_restaurant keeps a deleted restaurant's admin_events rows (models.py
# _KEEP_ON_RESTAURANT_DELETE): the record of what was done to an account must
# outlive the account.

import logging as _logging

_log = _logging.getLogger("admin_events")

_SAFE_METHODS = ("GET", "HEAD", "OPTIONS")

# Keys whose values never reach the audit trail, matched as substrings of the
# lowercased key ("toast_client_secret", "rpower_token", "new_password").
_SENSITIVE_PARTS = ("password", "passwd", "secret", "token", "api_key", "apikey", "access_key",
                    "private_key", "credential", "authorization", "cookie", "otp", "backup_code",
                    "two_fa_code", "csrf")
# …and whole keys that are sensitive but too short to match safely as a part
# ("pin" would match "shipping"). Not "key": the issue-resolve body's `key`
# is the issue's name, and a credential in a URL is caught by value below.
_SENSITIVE_KEYS = ("pin", "code", "sig", "signature")

REFUSED_CAP_ROWS = 5000          # admin_audit_refused never grows past this
REFUSED_KEEP_DAYS = 30           # …and nothing in it is older than this
REFUSED_PER_IP = 20              # rows one IP may add per window
REFUSED_WINDOW_SECONDS = 600
_refused_window = {}             # ip -> [window_start, rows_written, dropped]; process-local, best effort

# Writes that repeat every few seconds while someone types — the sales-audit
# autosave — get one successful audit row per actor and object per window,
# with the body's keys but not its values (the answers blob is the prospect's
# business, and hundreds of copies of it say nothing more). A refused or
# failed one is always recorded.
_COALESCE_SECONDS = {"sales_audit.api_save": 600}
_coalesce_last = {}              # (actor_id, endpoint, target) -> monotonic time of the last row

# The ALTERs and tables below are applied at boot by init_admin_events(),
# which models.init_db() calls — never on a request path.
_AUDIT_COLUMNS = (("actor", "TEXT"), ("actor_id", "INTEGER"), ("target", "TEXT"),
                  ("before_json", "TEXT"), ("after_json", "TEXT"), ("result", "TEXT"),
                  ("ip", "TEXT"), ("request_id", "TEXT"))

_SCHEMA = (
    # One definition of each index (integration wave): these two are the ones
    # ops._ensure_retention_indexes also names, with the same columns, so
    # whichever boot step runs first creates them and the other is a no-op.
    # This file briefly created them as idx_admin_events_rest / _created as
    # well — a second copy of each to maintain on every write — so those are
    # dropped where a database got them.
    "CREATE INDEX IF NOT EXISTS idx_admin_events_rid ON admin_events(restaurant_id, id)",
    "CREATE INDEX IF NOT EXISTS idx_admin_events_created_at ON admin_events(created_at)",
    "DROP INDEX IF EXISTS idx_admin_events_rest",
    "DROP INDEX IF EXISTS idx_admin_events_created",
    "CREATE INDEX IF NOT EXISTS idx_admin_events_source ON admin_events(source, id)",
    "CREATE INDEX IF NOT EXISTS idx_admin_events_actor ON admin_events(actor, id)",
    "CREATE INDEX IF NOT EXISTS idx_admin_events_request ON admin_events(request_id)",
    # Refused and anonymous /admin writes: kept apart from the audit trail,
    # never more than REFUSED_CAP_ROWS rows or REFUSED_KEEP_DAYS old.
    """CREATE TABLE IF NOT EXISTS admin_audit_refused (
        id         INTEGER PRIMARY KEY AUTOINCREMENT,
        created_at TEXT NOT NULL DEFAULT (datetime('now')),
        method     TEXT,
        path       TEXT,
        endpoint   TEXT,
        ip         TEXT,
        user_id    INTEGER,
        reason     TEXT NOT NULL,
        status     INTEGER,
        dropped    INTEGER NOT NULL DEFAULT 0
    )""",
    "CREATE INDEX IF NOT EXISTS idx_admin_audit_refused_created ON admin_audit_refused(created_at)",
    # Support notes (#55): authored, dated, append-only. There is no edit or
    # delete path — a correction is another note. restaurants.internal_notes
    # stays as it was (read-only here) beside them.
    """CREATE TABLE IF NOT EXISTS support_notes (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        restaurant_id INTEGER NOT NULL,
        author        TEXT NOT NULL,
        author_id     INTEGER,
        body          TEXT NOT NULL,
        created_at    TEXT NOT NULL DEFAULT (datetime('now'))
    )""",
    "CREATE INDEX IF NOT EXISTS idx_support_notes_rest ON support_notes(restaurant_id, id)",
    # In-app bug reports (#34): stored first, then emailed, so a failed email
    # no longer loses the report.
    """CREATE TABLE IF NOT EXISTS bug_reports (
        id            INTEGER PRIMARY KEY AUTOINCREMENT,
        restaurant_id INTEGER,
        user_id       INTEGER,
        username      TEXT,
        email         TEXT,
        message       TEXT NOT NULL,
        meta_json     TEXT,
        source        TEXT NOT NULL DEFAULT 'ios',
        status        TEXT NOT NULL DEFAULT 'open',
        notified      INTEGER NOT NULL DEFAULT 0,
        notify_error  TEXT,
        created_at    TEXT NOT NULL DEFAULT (datetime('now')),
        closed_at     TEXT,
        closed_by     TEXT,
        close_note    TEXT
    )""",
    "CREATE INDEX IF NOT EXISTS idx_bug_reports_status ON bug_reports(status, id)",
    "CREATE INDEX IF NOT EXISTS idx_bug_reports_rest ON bug_reports(restaurant_id, id)",
)


def init_admin_events(db_path=None):
    """Boot-time schema for the audit trail, support notes and bug reports.
    Called by models.init_db() (admin_events itself is created there)."""
    from models import get_conn, DB_PATH
    conn = get_conn(db_path or DB_PATH)
    try:
        have = {r[1] for r in conn.execute("PRAGMA table_info(admin_events)").fetchall()}
        for col, kind in _AUDIT_COLUMNS:
            if have and col not in have:
                conn.execute(f"ALTER TABLE admin_events ADD COLUMN {col} {kind}")
        for stmt in _SCHEMA:
            conn.execute(stmt)
        conn.commit()
    finally:
        conn.close()


def _is_sensitive(key) -> bool:
    k = str(key or "").strip().lower()
    return k in _SENSITIVE_KEYS or any(part in k for part in _SENSITIVE_PARTS)


def _redact(obj, max_str=1000, _depth=0):
    """`obj` with every secret-bearing value replaced by "[redacted]": by
    key name at any depth, and inside strings by ai_guard.redact_secrets
    (key= query strings, bearer tokens). Long strings are shortened."""
    if _depth > 6:
        return "…"
    if isinstance(obj, dict):
        # A true/false can't be a secret ("password_changed": false stays
        # readable); a number can (a PIN), so only booleans pass.
        return {str(k): ("[redacted]" if _is_sensitive(k) and v not in (None, "") and not isinstance(v, bool) else
                         _redact(v, max_str, _depth + 1))
                for k, v in list(obj.items())[:120]}
    if isinstance(obj, (list, tuple, set)):
        return [_redact(v, max_str, _depth + 1) for v in list(obj)[:60]]
    if isinstance(obj, str):
        try:
            from ai_guard import redact_secrets
            obj = redact_secrets(obj)
        except Exception:
            pass
        return obj if len(obj) <= max_str else obj[:max_str] + "…"
    if obj is None or isinstance(obj, (bool, int, float)):
        return obj
    return _redact(str(obj), max_str, _depth + 1)


def _dump(obj, cap=16000):
    """JSON for a redacted value, bounded. Over the cap it keeps the keys
    (what changed) and drops the values rather than cutting JSON in half."""
    try:
        text = json.dumps(obj, default=str, sort_keys=True)
    except Exception:
        text = json.dumps(str(obj))
    if len(text) <= cap:
        return text
    if isinstance(obj, dict):
        return json.dumps({"_truncated": True, "keys": sorted(str(k) for k in obj)[:200]})
    return json.dumps({"_truncated": True})


def _loads(text):
    if not text:
        return None
    try:
        return json.loads(text)
    except Exception:
        return None


def _actor_parts(actor):
    """(name, user id) for whoever acted: a current_user dict, a username,
    or — with neither — the actor this request's hook resolved."""
    if isinstance(actor, dict):
        name = actor.get("username") or actor.get("email") or (f"user:{actor.get('id')}" if actor.get("id") else None)
        return (name or "unknown"), actor.get("id")
    if actor:
        return str(actor)[:120], None
    state = _request_state()
    user = (state or {}).get("actor") or {}
    if user:
        return (user.get("username") or f"user:{user.get('id')}"), user.get("id")
    return "system", None


def _request_state():
    """This request's audit state (set by the hook), or None outside one."""
    try:
        from flask import has_request_context, g
        if not has_request_context():
            return None
        return getattr(g, "_admin_audit", None)
    except Exception:
        return None


def _request_meta():
    """(ip, request_id) for the current request, or (None, None)."""
    try:
        from flask import has_request_context, request
        if not has_request_context():
            return None, None
        state = _request_state() or {}
        return (request.remote_addr or None), state.get("request_id")
    except Exception:
        return None, None


def _target_str(target):
    if target in (None, "", {}):
        return None
    if isinstance(target, (tuple, list)) and len(target) == 2:
        return f"{target[0]}:{target[1]}"[:160]
    if isinstance(target, dict):
        return ",".join(f"{k}:{v}" for k, v in target.items())[:160]
    return str(target)[:160]


def record_admin_action(actor, action, restaurant_id=None, target=None, before=None, after=None,
                        result="ok", summary=None, db_path=None):
    """One typed admin action in the audit trail (source 'admin').

    `actor` is the acting login (the current_user dict, or a username);
    `action` a dotted name ("welcome.resent", "demo_flag.set");
    `restaurant_id` the client it concerns; `target` the object inside it
    ("user:12", ("review", 9)); `before`/`after` the state either side of
    the change — secrets are redacted by key name before anything is
    stored; `result` ok | failed | refused | error | partial. The request's
    IP and id are read here. Never raises: the audit must never break the
    action it records. Returns the row id, or None."""
    try:
        name, uid = _actor_parts(actor)
        ip, request_id = _request_meta()
        tgt = _target_str(target)
        text = (summary or " ".join(p for p in (name, str(action), tgt or "") if p))[:300]
        from models import get_conn, DB_PATH
        conn = get_conn(db_path or DB_PATH)
        try:
            cur = conn.execute(
                "INSERT INTO admin_events (source, event_type, restaurant_id, summary, actor, actor_id, target, "
                "before_json, after_json, result, ip, request_id) VALUES ('admin',?,?,?,?,?,?,?,?,?,?,?)",
                (str(action)[:120], restaurant_id, text, name, uid, tgt,
                 _dump(_redact(before)) if before is not None else None,
                 _dump(_redact(after)) if after is not None else None,
                 str(result or "ok")[:20], ip, request_id))
            conn.commit()
            return cur.lastrowid
        finally:
            conn.close()
    except Exception as e:
        _log.warning("record_admin_action(%s) not recorded: %s", action, e)
        return None


# ── the request hook ─────────────────────────────────────────────────────────

def audit_admin_write():
    """before_request for every blueprint that carries /admin writes
    (admin_bp, status_bp, the POS blueprints, the sales-audit tool — see
    register_audit). It writes nothing: it resolves who is asking and asks
    Flask to call _finish_request_audit once the response exists, so the row
    says how the request ENDED, and is written only for a resolved actor."""
    from flask import request
    if request.method in _SAFE_METHODS or not (request.path or "").startswith("/admin"):
        return None
    _begin_request_audit(admin_only=False)
    return None


def _begin_request_audit(admin_only=False):
    try:
        import uuid
        from flask import g, after_this_request
        if getattr(g, "_admin_audit", None) is not None:
            return
        user = None
        try:
            import auth
            user = auth.get_current_user()
        except Exception:
            user = None
        g._admin_audit = {"request_id": uuid.uuid4().hex[:16], "actor": user or None,
                          "admin_only": bool(admin_only)}
        after_this_request(_finish_request_audit)
    except Exception as e:
        _log.warning("admin audit hook could not start: %s", e)


def register_audit(blueprint, endpoints=None, admin_only=False):
    """Audit this blueprint's admin writes. With `endpoints`, only those
    (for admin writes that land on a client blueprint — the review imports
    an admin runs for a restaurant id, #126), recorded only when the actor is
    a console login: an owner using the same route is not an admin action."""
    if getattr(blueprint, "_admin_audit_wired", False):
        return blueprint
    wanted = frozenset(endpoints or ())

    def _audit_hook():
        from flask import request
        if request.method in _SAFE_METHODS:
            return None
        if wanted:
            if request.endpoint not in wanted:
                return None
            _begin_request_audit(admin_only=admin_only)
            return None
        return audit_admin_write()

    blueprint.before_request(_audit_hook)
    blueprint._admin_audit_wired = True
    return blueprint


def _csrf_passed():
    """Whether this request carried the double-submit pair (or its view is
    exempt). A write that failed CSRF did not prove its actor sent it."""
    try:
        import hmac
        from flask import request, current_app
        import csrf as _csrf
        view = current_app.view_functions.get(request.endpoint)
        if getattr(view, "_csrf_exempt", False):
            return True
        cookie_tok = request.cookies.get(_csrf.CSRF_COOKIE, "")
        sent_tok = _csrf._token_from_request()
        return bool(cookie_tok and sent_tok and hmac.compare_digest(cookie_tok, sent_tok))
    except Exception:
        return False


def _is_console_actor(user) -> bool:
    return bool(user) and bool(user.get("is_admin") or user.get("role") == "support")


def _finish_request_audit(response):
    """after_this_request: the audit row for an admin write, now that the
    response (and so the outcome) is known. Never raises; always returns
    the response untouched."""
    try:
        from flask import g
        state = getattr(g, "_admin_audit", None) or {}
        user = state.get("actor")
        csrf_ok = _csrf_passed()
        if _is_console_actor(user) and csrf_ok:
            _write_request_row(user, response, state)
        elif not state.get("admin_only"):
            reason = "anonymous" if not user else ("csrf" if not csrf_ok else "not_admin")
            _record_refused(reason, response.status_code, user)
    except Exception as e:
        _log.warning("admin audit row not written: %s", e)
    return response


_BODY_TARGET_KEYS = ("user_id", "review_id", "key", "experiment", "job", "audit_id", "note_id", "step", "organization_id")


def _request_body():
    """The write's body, redacted and shortened, or None."""
    from flask import request
    try:
        if request.is_json:
            data = request.get_json(silent=True)
        elif request.form:
            data = {k: v for k, v in request.form.items()}
        else:
            data = None
        if request.files:
            data = dict(data or {}) if isinstance(data, dict) else {"_body": data}
            data["_files"] = [getattr(f, "filename", "") or k for k, f in request.files.items()]
    except Exception:
        return None
    if data is None:
        return None
    return _redact(data, max_str=160)


def _int_or_none(v):
    try:
        return int(v) if v not in (None, "", True, False) else None
    except (TypeError, ValueError):
        return None


def _write_request_row(user, response, state):
    import json as _json
    from flask import request
    status = int(response.status_code or 0)
    body_ok = None
    try:
        if response.is_json and not response.direct_passthrough:
            j = response.get_json(silent=True)
            if isinstance(j, dict) and "ok" in j:
                body_ok = bool(j.get("ok"))
    except Exception:
        body_ok = None
    if status >= 500:
        result = "error"
    elif status >= 400:
        result = "refused"
    elif body_ok is False:
        result = "failed"
    else:
        result = "ok"
    view_args = dict(request.view_args or {})
    body = _request_body()
    rid = _int_or_none(view_args.pop("restaurant_id", None))
    if rid is None and isinstance(body, dict):
        rid = _int_or_none(body.get("restaurant_id"))
    target = None
    parts = [f"{k}:{v}" for k, v in view_args.items()]
    if not parts and isinstance(body, dict):
        parts = [f"{k}:{body[k]}" for k in _BODY_TARGET_KEYS if body.get(k) not in (None, "")]
    if parts:
        target = ",".join(str(p) for p in parts)[:160]
    name = user.get("username") or user.get("email") or f"user:{user.get('id')}"
    window = _COALESCE_SECONDS.get(request.endpoint or "")
    if window:
        if isinstance(body, dict):
            body = {"_keys": sorted(body)[:60]}
        if result == "ok":
            import time as _time
            key = (user.get("id"), request.endpoint, target)
            now = _time.monotonic()
            if len(_coalesce_last) > 5000:
                _coalesce_last.clear()
            if now - _coalesce_last.get(key, -1e9) < window:
                return
            _coalesce_last[key] = now
    payload = {"status": status, "method": request.method, "path": (request.path or "")[:300],
               "role": user.get("role"), "is_admin": bool(user.get("is_admin")), "body": body}
    text = _json.dumps(payload, default=str)
    if len(text) > 6000:
        payload["body"] = {"_truncated": True,
                           "keys": sorted(body)[:120] if isinstance(body, dict) else None}
        text = _json.dumps(payload, default=str)
    from models import get_conn, DB_PATH
    conn = get_conn(DB_PATH)
    try:
        conn.execute(
            "INSERT INTO admin_events (source, event_type, restaurant_id, summary, payload, actor, actor_id, "
            "target, result, ip, request_id) VALUES ('audit',?,?,?,?,?,?,?,?,?,?)",
            (f"admin_write:{request.endpoint or request.path}"[:200], rid,
             f"{name} {request.method} {(request.path or '')[:200]} → {status}"[:300], text,
             name, user.get("id"), target, result, request.remote_addr or None, state.get("request_id")))
        conn.commit()
    finally:
        conn.close()


def _refused_allowance(ip):
    """(may_write, dropped_since_last_row) for this IP's refused attempts:
    REFUSED_PER_IP rows a window, the rest only counted."""
    import time as _time
    now = _time.monotonic()
    if len(_refused_window) > 2000:
        _refused_window.clear()
    w = _refused_window.get(ip)
    if not w or now - w[0] > REFUSED_WINDOW_SECONDS:
        dropped = w[2] if w else 0
        _refused_window[ip] = [now, 1, 0]
        return True, dropped
    if w[1] < REFUSED_PER_IP:
        w[1] += 1
        dropped, w[2] = w[2], 0
        return True, dropped
    w[2] += 1
    return False, 0


def _record_refused(reason, status, user):
    """A refused or anonymous /admin write, into the capped side table."""
    from flask import request
    ip = request.remote_addr or ""
    ok, dropped = _refused_allowance(ip)
    if not ok:
        return
    from models import get_conn, DB_PATH
    conn = get_conn(DB_PATH)
    try:
        cur = conn.execute(
            "INSERT INTO admin_audit_refused (method, path, endpoint, ip, user_id, reason, status, dropped) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (request.method, (request.path or "")[:200], (request.endpoint or "")[:120], ip or None,
             (user or {}).get("id"), reason, int(status or 0), int(dropped or 0)))
        last = cur.lastrowid or 0
        if last % 50 == 0:
            conn.execute("DELETE FROM admin_audit_refused WHERE id <= ? OR created_at < datetime('now', ?)",
                         (last - REFUSED_CAP_ROWS, f"-{REFUSED_KEEP_DAYS} days"))
        conn.commit()
    finally:
        conn.close()


# ── reading the trail ────────────────────────────────────────────────────────

def _page_args(limit, before_id):
    try:
        limit = max(1, min(int(limit or 50), 200))
    except (TypeError, ValueError):
        limit = 50
    try:
        before_id = int(before_id) if before_id not in (None, "") else None
    except (TypeError, ValueError):
        before_id = None
    return limit, before_id


def audit_list(restaurant_id=None, actor=None, action=None, result=None, before_id=None,
               limit=50, include_requests=False, db_path=None):
    """Admin actions, newest first, one page at a time.

    Paginated by id (`before_id` = the `next_before_id` of the page before):
    stable while new rows arrive, and an index seek at any depth. By default
    a request's generic 'audit' row is folded away when the same request
    recorded a typed action; `include_requests` shows both. Returns
    {"events": [...], "next_before_id": id or None}."""
    limit, before_id = _page_args(limit, before_id)
    where, args = ["e.source IN ('admin', 'audit')"], []
    if restaurant_id:
        where.append("e.restaurant_id=?")
        args.append(int(restaurant_id))
    if actor:
        where.append("lower(e.actor)=lower(?)")
        args.append(str(actor)[:120])
    if action:
        where.append("e.event_type LIKE ? ESCAPE '\\'")
        args.append(str(action)[:120].replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%")
    if result:
        where.append("e.result=?")
        args.append(str(result)[:20])
    if before_id:
        where.append("e.id < ?")
        args.append(before_id)
    if not include_requests:
        where.append("NOT (e.source='audit' AND e.request_id IS NOT NULL AND EXISTS "
                     "(SELECT 1 FROM admin_events t WHERE t.request_id=e.request_id AND t.source='admin'))")
    from models import get_conn, DB_PATH
    conn = get_conn(db_path or DB_PATH)
    try:
        rows = conn.execute(
            "SELECT e.id, e.created_at, e.source, e.event_type, e.restaurant_id, r.name AS restaurant, "
            "e.actor, e.actor_id, e.target, e.result, e.ip, e.summary, e.before_json, e.after_json, "
            "e.payload, e.request_id FROM admin_events e LEFT JOIN restaurants r ON r.id=e.restaurant_id "
            "WHERE " + " AND ".join(where) + " ORDER BY e.id DESC LIMIT ?",
            tuple(args) + (limit + 1,)).fetchall()
    finally:
        conn.close()
    events = []
    for r in rows[:limit]:
        d = dict(r)
        payload = _loads(d.pop("payload", None)) or {}
        d["before"] = _loads(d.pop("before_json", None))
        d["after"] = _loads(d.pop("after_json", None))
        d["kind"] = "action" if d["source"] == "admin" else "request"
        d["request"] = ({k: payload.get(k) for k in ("status", "method", "path", "role", "body")}
                        if d["source"] == "audit" else None)
        d["action"] = d.pop("event_type")
        events.append(d)
    next_id = events[-1]["id"] if len(rows) > limit and events else None
    return {"events": events, "next_before_id": next_id}


def refused_list(before_id=None, limit=50, db_path=None):
    """The refused/anonymous /admin writes, newest first, paginated like
    audit_list."""
    limit, before_id = _page_args(limit, before_id)
    from models import get_conn, DB_PATH
    conn = get_conn(db_path or DB_PATH)
    try:
        rows = conn.execute(
            "SELECT id, created_at, method, path, endpoint, ip, user_id, reason, status, dropped "
            "FROM admin_audit_refused" + (" WHERE id < ?" if before_id else "") + " ORDER BY id DESC LIMIT ?",
            ((before_id,) if before_id else ()) + (limit + 1,)).fetchall()
    finally:
        conn.close()
    items = [dict(r) for r in rows[:limit]]
    return {"attempts": items, "next_before_id": items[-1]["id"] if len(rows) > limit and items else None}


# ── support notes (#55) ──────────────────────────────────────────────────────

SUPPORT_NOTE_MAX = 4000


def add_support_note(restaurant_id, author, body, db_path=None):
    """Append one note. Returns the stored note, or None for an empty body.
    Append-only on purpose: nothing edits or deletes a note."""
    text = (body or "").strip()
    if not text:
        return None
    name, uid = _actor_parts(author)
    from models import get_conn, DB_PATH
    conn = get_conn(db_path or DB_PATH)
    try:
        cur = conn.execute("INSERT INTO support_notes (restaurant_id, author, author_id, body) VALUES (?,?,?,?)",
                           (int(restaurant_id), name, uid, text[:SUPPORT_NOTE_MAX]))
        conn.commit()
        row = conn.execute("SELECT * FROM support_notes WHERE id=?", (cur.lastrowid,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def list_support_notes(restaurant_id, before_id=None, limit=50, db_path=None):
    limit, before_id = _page_args(limit, before_id)
    from models import get_conn, DB_PATH
    conn = get_conn(db_path or DB_PATH)
    try:
        rows = conn.execute(
            "SELECT id, restaurant_id, author, author_id, body, created_at FROM support_notes "
            "WHERE restaurant_id=?" + (" AND id < ?" if before_id else "") + " ORDER BY id DESC LIMIT ?",
            (int(restaurant_id),) + ((before_id,) if before_id else ()) + (limit + 1,)).fetchall()
    finally:
        conn.close()
    notes = [dict(r) for r in rows[:limit]]
    return {"notes": notes, "next_before_id": notes[-1]["id"] if len(rows) > limit and notes else None}


# ── bug reports (#34) ────────────────────────────────────────────────────────

def store_bug_report(restaurant_id, user, message, meta=None, source="ios", db_path=None):
    """The report, stored before anything is emailed. Returns its id."""
    user = user or {}
    from models import get_conn, DB_PATH
    conn = get_conn(db_path or DB_PATH)
    try:
        cur = conn.execute(
            "INSERT INTO bug_reports (restaurant_id, user_id, username, email, message, meta_json, source) "
            "VALUES (?,?,?,?,?,?,?)",
            (restaurant_id, user.get("id"), user.get("username"), user.get("email"),
             (message or "")[:4000], json.dumps(meta or {}, default=str)[:4000], (source or "ios")[:20]))
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def mark_bug_report_notified(report_id, ok, error=None, db_path=None):
    from models import get_conn, DB_PATH
    conn = get_conn(db_path or DB_PATH)
    try:
        conn.execute("UPDATE bug_reports SET notified=?, notify_error=? WHERE id=?",
                     (1 if ok else 0, (str(error)[:300] if error else None), report_id))
        conn.commit()
    finally:
        conn.close()


def list_bug_reports(status=None, restaurant_id=None, before_id=None, limit=50, db_path=None):
    limit, before_id = _page_args(limit, before_id)
    where, args = [], []
    if status in ("open", "closed"):
        where.append("b.status=?")
        args.append(status)
    if restaurant_id:
        where.append("b.restaurant_id=?")
        args.append(int(restaurant_id))
    if before_id:
        where.append("b.id < ?")
        args.append(before_id)
    from models import get_conn, DB_PATH
    conn = get_conn(db_path or DB_PATH)
    try:
        rows = conn.execute(
            "SELECT b.*, r.name AS restaurant FROM bug_reports b LEFT JOIN restaurants r ON r.id=b.restaurant_id"
            + (" WHERE " + " AND ".join(where) if where else "") + " ORDER BY b.id DESC LIMIT ?",
            tuple(args) + (limit + 1,)).fetchall()
        open_n = conn.execute("SELECT COUNT(*) FROM bug_reports WHERE status='open'").fetchone()[0]
    finally:
        conn.close()
    reports = []
    for r in rows[:limit]:
        d = dict(r)
        d["meta"] = _loads(d.pop("meta_json", None)) or {}
        reports.append(d)
    return {"reports": reports, "open": open_n,
            "next_before_id": reports[-1]["id"] if len(rows) > limit and reports else None}


def set_bug_report_status(report_id, status, actor, note=None, db_path=None):
    """Close (or reopen) a report. Returns the updated row, or None."""
    if status not in ("open", "closed"):
        raise ValueError("status must be open or closed")
    name, _uid = _actor_parts(actor)
    from models import get_conn, DB_PATH
    conn = get_conn(db_path or DB_PATH)
    try:
        if status == "closed":
            cur = conn.execute("UPDATE bug_reports SET status='closed', closed_at=datetime('now'), closed_by=?, "
                               "close_note=? WHERE id=?", (name, (note or "")[:300] or None, report_id))
        else:
            cur = conn.execute("UPDATE bug_reports SET status='open', closed_at=NULL, closed_by=NULL, "
                               "close_note=NULL WHERE id=?", (report_id,))
        conn.commit()
        if not cur.rowcount:
            return None
        row = conn.execute("SELECT * FROM bug_reports WHERE id=?", (report_id,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()
