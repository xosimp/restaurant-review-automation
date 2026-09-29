"""
webhook_routes.py — Stripe and DocuSign webhook handlers
Registered as a Flask Blueprint in hosted_dashboard.py
"""
from flask import Blueprint, request, jsonify, redirect
import os
import config
import emails as _emails
from datetime import datetime

from models import get_conn, get_restaurant, update_restaurant, log_email

# Exception text handed to a client, with credentials stripped — a requests
# error carries the failing URL, and a Places URL carries key= in its query
# string. See ai_guard.safe_error.
from ai_guard import safe_error as _safe_err


import sqlite3
from emails import html_document as _html_doc  # one definition; emails reads its env lazily

def _claim_stripe_event(event_id: str) -> bool:
    """True if this is the first time we've seen this Stripe event.

    Stripe delivers at-least-once and retries any non-2xx, so without this a
    retry re-runs the whole handler: a second receipt email to the customer,
    a second "new paying client" alert. The state changes were already
    idempotent by accident (guarded on billing_status != active), the emails
    were not.

    INSERT on a PRIMARY KEY is the claim — two concurrent deliveries of the
    same event cannot both succeed.
    """
    if not event_id:
        return True
    try:
        conn = get_conn()
        try:
            conn.execute("INSERT INTO stripe_events_seen (event_id) VALUES (?)", (event_id,))
            conn.commit()
            claimed = True
        except sqlite3.IntegrityError:
            claimed = False   # duplicate PK — already handled
        finally:
            conn.close()
        return claimed
    except Exception as e:
        # Fail open: a bookkeeping failure must not drop a real payment event.
        print(f"_claim_stripe_event failed ({event_id}): {e}")
        return True


def _claim_docusign_event(envelope_id: str, status: str) -> bool:
    """True the first time this envelope reaches this status.

    DocuSign Connect retries any non-2xx and can deliver the same
    notification more than once. Without this, a repeat of a single
    "completed" callback re-sends the payment link AND the welcome email —
    to a client who already got both, at the least confusing moment
    possible.

    Deliberately the same shape as _claim_stripe_event above: the primary-key
    insert is the claim, and it fails open, because a bookkeeping outage must
    not swallow the one callback that starts a paying client's account.
    """
    if not envelope_id:
        return True
    key = f"{envelope_id}:{status or ''}"
    try:
        conn = get_conn()
        try:
            conn.execute("INSERT INTO docusign_events_seen (event_key, envelope_id, status) VALUES (?,?,?)",
                         (key, envelope_id, status))
            conn.commit()
            claimed = True
        except sqlite3.IntegrityError:
            claimed = False   # duplicate PK — already handled
        finally:
            conn.close()
        return claimed
    except Exception as e:
        print(f"_claim_docusign_event failed ({key}): {e}")
        return True


class _AlreadyOnboarded(Exception):
    """The owner has signed in before; no welcome email or new password."""


def _release_claim(table, column, key):
    """Undo a claim whose processing failed, so the provider's retry is
    processed rather than skipped as a duplicate. Only "database is locked"
    style errors were ever meant to be survivable; a claim that stuck after
    its handler died dropped the retry of a real payment (AI-2, SEC-27)."""
    if not key:
        return
    try:
        # A fresh connection through models, not this module's bound name:
        # the release runs right after a connection failed, and is the one
        # write that has to land for the provider's retry to be processed.
        import models as _models_rel
        conn = _models_rel.get_conn()
        try:
            conn.execute(f"DELETE FROM {table} WHERE {column}=?", (key,))
            conn.commit()
        finally:
            conn.close()
    except Exception as e:
        print(f"release of {table} claim {key} failed: {e}")


# ── inbound webhook health (#74) ─────────────────────────────────────────────
# A rotated signing secret used to fail every delivery with a print and a
# 4xx: billing sync or contract signing stopped and nothing anywhere said so.
# Every verdict is now counted per provider (webhook_verifications), and a
# failure reaches ops.capture — the failure digest and the console — at most
# once an hour per provider.
_WEBHOOK_CAPTURE_MINUTES = 60


def _webhook_seen(provider, ok, event=None, error=None):
    """Record one inbound request's signature verdict. Never raises."""
    capture = False
    row = None
    try:
        conn = get_conn()
        try:
            if ok:
                conn.execute(
                    "INSERT INTO webhook_verifications (provider, last_verified_at, last_verified_event, "
                    "failures_since_verified, updated_at) VALUES (?, datetime('now'), ?, 0, datetime('now')) "
                    "ON CONFLICT(provider) DO UPDATE SET last_verified_at=excluded.last_verified_at, "
                    "last_verified_event=excluded.last_verified_event, failures_since_verified=0, "
                    "updated_at=excluded.updated_at", (provider, (event or "")[:120]))
                conn.commit()
                return
            conn.execute(
                "INSERT INTO webhook_verifications (provider, last_failure_at, last_failure_error, failures_total, "
                "failures_since_verified, updated_at) VALUES (?, datetime('now'), ?, 1, 1, datetime('now')) "
                "ON CONFLICT(provider) DO UPDATE SET last_failure_at=excluded.last_failure_at, "
                "last_failure_error=excluded.last_failure_error, "
                "failures_total=webhook_verifications.failures_total+1, "
                "failures_since_verified=webhook_verifications.failures_since_verified+1, "
                "updated_at=excluded.updated_at", (provider, (error or "")[:300]))
            cur = conn.execute(
                "UPDATE webhook_verifications SET last_captured_at=datetime('now') WHERE provider=? "
                "AND (last_captured_at IS NULL OR last_captured_at < datetime('now', ?))",
                (provider, f"-{_WEBHOOK_CAPTURE_MINUTES} minutes"))
            conn.commit()
            capture = cur.rowcount == 1
            row = conn.execute("SELECT failures_since_verified, last_verified_at FROM webhook_verifications "
                               "WHERE provider=?", (provider,)).fetchone()
        finally:
            conn.close()
    except Exception as e:
        print(f"_webhook_seen({provider}) not recorded: {e}")
        capture = not ok
    if capture:
        try:
            import ops
            n = row["failures_since_verified"] if row else "?"
            last = (row["last_verified_at"] if row else None) or "never"
            ops.capture(RuntimeError(f"{provider} webhook refused a request ({error}); {n} refused since the "
                                     f"last verified event ({last})"),
                        job=f"{provider}_webhook_signature", context="inbound webhook verification")
        except Exception:
            pass


def _restaurant_for_stripe(customer_id: str = "", email: str = ""):
    """Resolve a Stripe CHECKOUT to a restaurant id.

    Matching used to be `WHERE u.email = customer_email LIMIT 1`, which broke
    in two ways the audit caught: an owner who changed their email in the app
    stopped matching entirely (payments silently stopped reconciling and
    billing_status never activated), and a multi-location owner only ever
    resolved to their base restaurant.

    stripe_customer_id is the stable key and is tried first; email is the
    fallback for the very first payment, before we have a customer id stored.
    Only checkout.session.completed still uses the email: every lifecycle
    event after it resolves by the ids we hold (_resolve_stripe_event, #115).
    """
    if not customer_id and not email:
        return None
    try:
        conn = get_conn()
        if customer_id:
            row = conn.execute(
                "SELECT id FROM restaurants WHERE stripe_customer_id=? LIMIT 1",
                (customer_id,)
            ).fetchone()
            if row:
                conn.close()
                return row["id"]
        if email:
            row = conn.execute(
                """SELECT r.id FROM restaurants r
                   JOIN users u ON u.restaurant_id = r.id
                   WHERE lower(u.email)=lower(?) ORDER BY u.is_admin ASC, r.id ASC LIMIT 1""",
                (email,)
            ).fetchone()
            if row:
                conn.close()
                return row["id"]
            # Some accounts carry the billing address on the restaurant only.
            row = conn.execute(
                "SELECT id FROM restaurants WHERE lower(owner_email)=lower(?) ORDER BY id ASC LIMIT 1",
                (email,)
            ).fetchone()
            if row:
                conn.close()
                return row["id"]
        conn.close()
    except Exception as e:
        print(f"_restaurant_for_stripe lookup failed: {e}")
    return None


def _restaurant_for_customer(customer_id):
    """The restaurant a Stripe customer pays for: the one holding a
    subscription when several locations share the customer id."""
    if not customer_id or not isinstance(customer_id, str):
        return None
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT id FROM restaurants WHERE stripe_customer_id=? "
            "ORDER BY (id IN (SELECT restaurant_id FROM stripe_subscriptions)) DESC, id ASC LIMIT 1",
            (customer_id,)).fetchone()
        return row["id"] if row else None
    finally:
        conn.close()


def _restaurant_from_metadata(meta) -> int:
    """restaurant_id out of Stripe metadata, if it is there and it is real.

    Preferred over customer id and email both: it is set at checkout, it is
    stable, and it survives an owner changing their email. Validated against
    the table rather than trusted, because metadata is only as good as the
    session that set it."""
    try:
        raw = (meta or {}).get("restaurant_id")
        if not raw:
            return None
        rid = int(str(raw).strip())
        conn = get_conn()
        row = conn.execute("SELECT id FROM restaurants WHERE id=?", (rid,)).fetchone()
        conn.close()
        return row["id"] if row else None
    except Exception:
        return None


# The module keys checkout is allowed to grant. "intel" is derived from full
# tier and has no column, so it is not settable here.
_GRANTABLE_MODULES = ("reviews", "labor", "inventory", "marketing")


def _apply_module_entitlement(restaurant_id: int, module_keys: str) -> dict:
    """Set this restaurant's module flags to exactly what was paid for.

    Audit #5: checkout put a module COUNT in metadata and nothing read it, so
    what a client paid for and what they could open were two unconnected
    facts — every flag was a manual admin step, and Stripe would never correct
    a mismatch in either direction.

    Only called with keys that came from OUR checkout metadata, and only for
    the four real columns. Returns the updates applied, or {} if the metadata
    carried nothing usable — an empty list must never be read as "revoke
    everything", because that is also what a missing key looks like.
    """
    keys = {k.strip().lower() for k in (module_keys or "").split(",") if k.strip()}
    keys &= set(_GRANTABLE_MODULES)
    if not keys:
        return {}
    updates = {f"module_{k}": (1 if k in keys else 0) for k in _GRANTABLE_MODULES}
    # A failed write raises: the webhook then answers 5xx and Stripe retries,
    # instead of the client paying for modules they never receive.
    update_restaurant(restaurant_id, updates)
    print(f"Entitlement set from Stripe for restaurant {restaurant_id}: {sorted(keys)}")
    return updates


# ── billing state, applied (fix round H) ─────────────────────────────────────
# One writer for every billing-state move a Stripe event makes. It raises on
# a failed write (#7): _set_billing_status used to catch the error and
# return False, every caller ignored the False, the event was acknowledged
# and Stripe never retried — a client whose cancellation hit a locked
# database kept access for good. It also respects the locks only an admin
# lifts (#114): a later "active" from Stripe no longer undoes a chargeback
# pause, and a failed card no longer lifts one.

_KEEP = object()
_BLOCKED = ("paused", "churned", "canceled", "cancelled")


def _apply_state(rids, status, pause_reason=_KEEP, paused_until=_KEEP, override_locks=False,
                 skip_from=()):
    """Move each restaurant in `rids` to `status`. Returns (moved, held):
    the ids written and the ids a lock (or `skip_from`) kept where they are."""
    import models as _m
    moved, held = [], []
    for rid in rids:
        r = get_restaurant(rid)
        if not r:
            continue
        cur = (r.billing_status or "").lower()
        lock = _m.pause_lock(r)
        if (lock and not override_locks) or cur in skip_from:
            held.append(rid)
            continue
        updates = {"billing_status": status}
        if pause_reason is not _KEEP:
            updates["pause_reason"] = pause_reason
        if paused_until is not _KEEP:
            updates["paused_until"] = paused_until
        if status == "churned" and (r.pause_reason or "") == "self":
            # A self-serve pause means nothing once the subscription is over;
            # a dispute or refund lock is kept for the day they come back.
            updates["pause_reason"] = None
            updates["paused_until"] = None
        update_restaurant(rid, updates)
        moved.append(rid)
    return moved, held


def _set_billing_status(restaurant_id: int, status: str, reason: str = ""):
    """Move a restaurant and every location its subscription covers to one
    status. Raises when a write fails, so the webhook answers 5xx and Stripe
    redelivers (#7); a lock only an admin lifts is left in place."""
    import billing_jobs as _bj
    moved, held = _apply_state(_bj.subscription_scope(restaurant_id), status)
    print(f"billing_status={status} for {moved} ({reason})" + (f"; held by a lock: {held}" if held else ""))
    return True


_LOCK_RANK = {"admin": 1, "refund": 2, "dispute": 3}


def _lock_accounts(rids, reason):
    """Hold each restaurant paused with `reason` until an admin lifts it
    (lead default 5). A stronger lock is never downgraded (a dispute outranks
    a refund), and a churned account keeps its status but records the lock,
    so a returning client's new checkout meets it."""
    import models as _m
    locked = []
    for rid in rids:
        r = get_restaurant(rid)
        if not r:
            continue
        cur = _m.pause_lock(r)
        if cur and _LOCK_RANK.get(cur, 0) >= _LOCK_RANK.get(reason, 0):
            locked.append(rid)
            continue
        if (r.billing_status or "").lower() in ("churned", "canceled", "cancelled"):
            update_restaurant(rid, {"pause_reason": reason})
        else:
            update_restaurant(rid, {"billing_status": "paused", "pause_reason": reason, "paused_until": None})
        locked.append(rid)
    return locked


def _paused_until(sub) -> str:
    """The ISO date Stripe will resume collecting, or "" when the
    subscription is not paused. pause_collection is a dict while a pause
    is in force and None otherwise; resumes_at is a unix timestamp, or
    absent for an open-ended pause, which we hold as paused with no date."""
    pc = (sub or {}).get("pause_collection")
    if not pc:
        return ""
    ts = pc.get("resumes_at") if isinstance(pc, dict) else None
    if ts:
        from datetime import datetime, timezone
        try:
            return datetime.fromtimestamp(int(ts), tz=timezone.utc).date().isoformat()
        except (TypeError, ValueError, OSError):
            pass
    return "open"


def _sibling_restaurant_ids(restaurant_id: int):
    """Every location that shares this restaurant's location_group.

    A multi-location owner pays once for the group (owner decision 1: one
    subscription per group), so this is the GROUP. What a subscription event
    moves is narrower — the locations that subscription covers,
    billing_jobs.subscription_scope — so a location billed on its own
    subscription is never moved by another location's card or cancellation.
    A dispute is the one event that locks the whole group.
    """
    try:
        conn = get_conn()
        row = conn.execute(
            "SELECT location_group, owner_email, stripe_customer_id FROM restaurants WHERE id=?",
            (restaurant_id,)
        ).fetchone()
        if not row:
            conn.close()
            return [restaurant_id]
        group = (row["location_group"] or "").strip()
        owner = (row["owner_email"] or "").strip().lower()
        customer = (row["stripe_customer_id"] or "").strip()
        if group:
            # Scoped to the owner as well as the group name: two unrelated
            # clients typed into the same group must not have one's
            # cancellation churn the other's locations. See
            # models.get_location_group.
            rows = conn.execute(
                "SELECT id FROM restaurants WHERE location_group=? "
                "AND LOWER(TRIM(COALESCE(owner_email,'')))=?",
                (group, owner)
            ).fetchall()
        elif customer:
            # location_group is free text an admin types, and it is unset on
            # every production row today. Without this branch a multi-location
            # owner's single payment activated only the location Stripe
            # resolved to and left the rest on their previous status — which,
            # now that billing is actually enforced, means the entitlement
            # gate 402s a customer who has paid. One Stripe customer is one
            # subscription, so every restaurant billed to it moves together.
            rows = conn.execute(
                "SELECT id FROM restaurants WHERE stripe_customer_id=?", (customer,)
            ).fetchall()
        else:
            conn.close()
            return [restaurant_id]
        conn.close()
        ids = [r["id"] for r in rows]
        return ids or [restaurant_id]
    except Exception:
        return [restaurant_id]


webhook_bp = Blueprint('webhook', __name__)

from emails import _resend_key

FROM_EMAIL            = config.from_email()
WILL_EMAIL            = config.will_email()
# An override for tests only. The live secret is read from the environment on
# every request (#145): it was read once at import, so a rotated secret did
# nothing until a restart, and an unset one was handed to construct_event.
STRIPE_WEBHOOK_SECRET = ""


def _stripe_webhook_secret() -> str:
    return STRIPE_WEBHOOK_SECRET or os.getenv("STRIPE_WEBHOOK_SECRET", "")


_PAY_PAGE = ("<!doctype html><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>"
             "<title>Cavnar AI</title><div style=\"font-family:-apple-system,BlinkMacSystemFont,'Helvetica Neue',Arial,"
             "sans-serif;max-width:480px;margin:12vh auto;padding:24px;color:#0e0c0a\"><h2>%s</h2><p>%s</p></div>")


@webhook_bp.route("/pay/<token>/<period>")
def pay_link_route(token, period):
    """Where every billing email's buttons go. The signed token names one
    restaurant; the link never expires, because what it opens is decided at
    click time (MOD-BIL-5):

      monthly / annual  a fresh Checkout Session for a client who has not paid;
      card              a Stripe Billing Portal session (update the card);
      invoice           the open invoice's hosted page (pay what failed).

    A past-due client is sent to fix the card, never told "You're all set"
    (#6); a location covered by its group's subscription is told who pays
    for it instead of being offered a second checkout (owner decision 1); a
    paused one is never sent to a new checkout (COMMS-27)."""
    from emails import read_pay_token, create_stripe_checkout
    from markupsafe import escape as _esc_pay
    import billing_jobs as _bj
    import models as _m
    page = _PAY_PAGE
    rid = read_pay_token(token)
    if not rid or period not in ("monthly", "annual", "card", "invoice"):
        return page % ("That link isn't right", "Reply to your payment email or write to will@cavnar.ai."), 404
    r = get_restaurant(rid)
    if not r:
        return page % ("That link isn't right", "Reply to your payment email or write to will@cavnar.ai."), 404
    name = _esc_pay(r.name)
    status = (r.billing_status or "").lower()
    if _m.pause_lock(r):
        return page % ("Your account is on hold",
                       f"{name}'s account is paused while a billing question is sorted out. "
                       "Reply to Will at will@cavnar.ai and he'll help."), 200
    payer = _bj.billed_by(rid)
    if payer and payer != rid:
        p = get_restaurant(payer)
        if status == "past_due" or (p and (p.billing_status or "").lower() == "past_due"):
            url = _bj.fix_payment_url(p, prefer_invoice=(period != "card"))
            if url:
                return redirect(url)
        return page % ("Already covered",
                       f"{name} is billed together with {_esc_pay(p.name if p else 'your group')} — "
                       "one subscription covers the group, so there is nothing to pay here."), 200
    if status == "past_due" or period in ("card", "invoice"):
        url = _bj.fix_payment_url(r, prefer_invoice=(period != "card"))
        if url:
            return redirect(url)
        if not r.stripe_customer_id:
            return page % ("No card on file yet",
                           "There is no payment method to update. Reply to Will at will@cavnar.ai."), 404
        return page % ("We couldn't open billing", "Try again in a minute, or write to will@cavnar.ai."), 503
    if status == "paused":
        return page % ("Paused", f"{name} is paused. Resume any time from Account → Billing in Cavnar AI, "
                                 "or reply to Will at will@cavnar.ai."), 200
    if status in ("active", "internal") or _bj.live_subscription(rid):
        return page % ("You're all set", f"{name} is already paid for. Nothing more to do."), 200
    modules = [k for k in ("reviews", "labor", "inventory", "marketing") if getattr(r, f"module_{k}", 0)]
    url = create_stripe_checkout(max(1, len(modules)), r.owner_email, r.name, period,
                                 restaurant_id=rid, modules=modules)
    if not url:
        return page % ("We couldn't open checkout", "Try again in a minute, or write to will@cavnar.ai."), 503
    return redirect(url)


@webhook_bp.route("/stripe-webhook", methods=["POST"])
def stripe_webhook():
    secret = _stripe_webhook_secret()
    if not secret:
        # Fail closed (#145), as the DocuSign webhook does: without a secret
        # there is nothing to verify against, and a forged invoice.paid could
        # activate or re-entitle an account.
        _webhook_seen("stripe", False, error="STRIPE_WEBHOOK_SECRET is not set")
        return jsonify(error="Unauthorized"), 401
    import stripe
    payload = request.get_data()
    sig_header = request.headers.get("Stripe-Signature","")

    try:
        event = stripe.Webhook.construct_event(
            payload, sig_header, secret
        )
    except Exception as e:
        _webhook_seen("stripe", False, error=_safe_err(e)[:200])
        return jsonify(error=_safe_err(e)), 400
    _webhook_seen("stripe", True, event=f"{event.get('type')} {event.get('id')}")

    # Stripe retries on any non-2xx and can deliver the same event twice.
    # Everything below this line sends email or changes billing state, so it
    # runs at most once per event id.
    if not _claim_stripe_event(event.get("id", "")):
        print(f"Stripe event {event.get('id')} already handled — skipping duplicate")
        return jsonify(received=True, duplicate=True)
    try:
        import models as _m
        # Every billing-status, pause and module change this event makes is
        # recorded against it (billing_status_history, #11).
        with _m.billing_context(source="stripe", actor="stripe", stripe_event_id=event.get("id"),
                                reason=event.get("type")):
            return _stripe_dispatch(event)
    except Exception as e:
        # The claim is released and Stripe is told to retry. Answering 200
        # here marked a payment handled that never activated anything.
        _release_claim("stripe_events_seen", "event_id", event.get("id", ""))
        try:
            import ops
            ops.capture(e, job="stripe_webhook", context=f"{event.get('type')} {event.get('id')}")
        except Exception:
            pass
        return jsonify(error="processing failed; will retry"), 500


_BILLING_STATE_EVENTS = {
    "customer.subscription.created", "customer.subscription.updated", "customer.subscription.deleted",
    "invoice.paid", "invoice.payment_failed", "charge.refunded", "charge.dispute.created",
}


def _second_subscription(rid, sub_id, session_id, customer_id=None) -> bool:
    """Record the restaurant's subscription; True when it already has a
    DIFFERENT subscription that is still live.

    Any stored row used to count as live, and rows were cleared only by a
    matched cancellation — so a client whose last subscription ended "unpaid"
    had their new one auto-cancelled as a second checkout (#115). A row whose
    status is over is replaced; one from before the mirror had a status is
    checked with Stripe, then against the local state."""
    import billing_jobs as _bj
    row = _bj.mirror_row(rid)
    if row and row.get("subscription_id") != sub_id and _subscription_still_live(rid, row):
        return True
    if not row or row.get("subscription_id") != sub_id:
        _bj.upsert_subscription(rid, subscription_id=sub_id, session_id=session_id,
                                facts={"customer_id": customer_id, "status": None})
    return False


def _subscription_still_live(rid, row) -> bool:
    import billing_jobs as _bj
    status = row.get("status")
    if status:
        return _bj.is_live_status(status)
    key = os.getenv("STRIPE_SECRET_KEY", "")
    if key:
        try:
            s = config.stripe_api(key).Subscription.retrieve(row["subscription_id"])
            st = _bj._g(s, "status")
            if st:
                _bj.upsert_subscription(rid, sub=s)
                return _bj.is_live_status(st)
        except Exception as e:
            print(f"subscription {row.get('subscription_id')} could not be checked with Stripe: {_safe_err(e)}")
    r = get_restaurant(rid)
    return (getattr(r, "billing_status", "") or "").lower() not in ("churned", "canceled", "cancelled")


def _retrieve_charge(charge_id):
    """A dispute carries only its charge id. Raises on a Stripe error, so the
    webhook answers 5xx and Stripe redelivers the dispute."""
    key = os.getenv("STRIPE_SECRET_KEY", "")
    if not key or not charge_id:
        return None
    return config.stripe_api(key).Charge.retrieve(charge_id)


def _resolve_stripe_event(event):
    """(restaurant_id, how) for a Stripe event, by the ids we hold — never by
    email (#115). The subscription id first (the stripe_subscriptions
    mirror), then the restaurant_id Stripe carries back in metadata, then the
    invoice or charge we recorded, then the stored customer id. `how` is
    subscription | metadata | invoice | customer | duplicate (the event is
    about a second checkout's subscription, cancelled against the first)."""
    import billing_jobs as _bj
    etype = event.get("type") or ""
    obj = (event.get("data") or {}).get("object") or {}
    sub_id = None
    if etype.startswith("customer.subscription."):
        sub_id = obj.get("id")
    elif etype.startswith("invoice."):
        sub_id = _bj.invoice_facts(obj).get("subscription_id")
    elif etype.startswith("checkout.session."):
        sub_id = obj.get("subscription")
    if sub_id:
        rid = _bj.restaurant_for_subscription(sub_id)
        if rid:
            return rid, "subscription"
        dup = _bj.duplicate_subscription(sub_id)
        if dup:
            return dup.get("restaurant_id"), "duplicate"
    meta = obj.get("metadata") or {}
    if etype.startswith("invoice."):
        meta = _bj.invoice_metadata(obj) or meta
    rid = _restaurant_from_metadata(meta)
    if rid:
        return rid, "metadata"
    customer = obj.get("customer") if isinstance(obj.get("customer"), str) else None
    if etype.startswith("charge."):
        dispute = etype.startswith("charge.dispute.")
        charge_id = obj.get("charge") if dispute else obj.get("id")
        if not isinstance(charge_id, str):
            charge_id = (charge_id or {}).get("id") if isinstance(charge_id, dict) else None
        inv_id = obj.get("invoice") if isinstance(obj.get("invoice"), str) else None
        pi = obj.get("payment_intent") if isinstance(obj.get("payment_intent"), str) else None
        rid, _sub = _bj.restaurant_for_charge(charge_id, inv_id, pi)
        if rid:
            if _sub and _bj.duplicate_subscription(_sub):
                return rid, "duplicate"
            return rid, "invoice"
        if dispute and not customer and charge_id:
            ch = _retrieve_charge(charge_id)
            if ch is not None:
                customer = _bj._g(ch, "customer")
                customer = customer if isinstance(customer, str) else _bj._g(customer, "id")
                ch_inv = _bj._g(ch, "invoice")
                ch_inv = ch_inv if isinstance(ch_inv, str) else _bj._g(ch_inv, "id")
                rid, _sub = _bj.restaurant_for_charge(None, ch_inv, None)
                if rid:
                    return rid, ("duplicate" if _sub and _bj.duplicate_subscription(_sub) else "invoice")
    if customer:
        rid = _restaurant_for_customer(customer)
        if rid:
            return rid, "customer"
        dup = _bj.duplicate_subscription(customer_id=customer)
        if dup:
            return dup.get("restaurant_id"), "duplicate"
    return None, None


def _event_restaurant(event):
    return _resolve_stripe_event(event)[0]


def _stale_billing_event(event, rid=None) -> bool:
    """True when a newer event has already been applied to this restaurant's
    billing state; otherwise records this one as the newest. An event with
    no timestamp or no restaurant is never called stale."""
    created = event.get("created")
    if event.get("type") not in _BILLING_STATE_EVENTS or not created:
        return False
    rid = rid or _event_restaurant(event)
    if not rid:
        return False
    conn = get_conn()
    try:
        row = conn.execute("SELECT last_event_created FROM stripe_billing_clock WHERE restaurant_id=?",
                           (rid,)).fetchone()
        if row and int(created) < int(row["last_event_created"]):
            return True
        conn.execute("INSERT INTO stripe_billing_clock (restaurant_id, last_event_created, last_event_id) "
                     "VALUES (?,?,?) ON CONFLICT(restaurant_id) DO UPDATE SET "
                     "last_event_created=excluded.last_event_created, last_event_id=excluded.last_event_id, "
                     "updated_at=datetime('now')", (rid, int(created), event.get("id")))
        conn.commit()
        return False
    finally:
        conn.close()


def _send_alert(subject, body):
    """Operator alert to Will. Never raises: the billing work it reports on
    has already been written, and an alert failure must not undo it."""
    if not _resend_key():
        print(f"ALERT: {subject}\n{body}")
        return
    try:
        _emails.deliver_or_raise(email_type="ops_payment_alert", payload={
            "from": _emails.sender("ops"),
            "to": [WILL_EMAIL],
            "subject": subject,
            "html": _html_doc(f"""<div style="font-family:sans-serif;max-width:500px;margin:0 auto">
                <div style="border-top:3px solid #c84b2f;padding-top:20px;margin-bottom:20px">
                    <h3 style="color:#0e0c0a;margin:0">Cavnar AI — Payment Alert</h3>
                </div>
                <p style="font-size:15px;line-height:1.6">{body}</p>
                <hr style="border:none;border-top:1px solid #e0dbd0;margin:20px 0"/>
                <p style="font-size:11px;color:#7a736a">
                    Manage clients at
                    <a href="https://dashboard.cavnar.ai/admin" style="color:#c84b2f">
                        dashboard.cavnar.ai/admin
                    </a>
                </p>
            </div>"""),
        })
    except Exception as e:
        print(f"Alert email failed: {e}")


def _names(rids):
    out = []
    for rid in rids:
        r = get_restaurant(rid)
        out.append(f"{_emails.esc(r.name)} (#{rid})" if r else f"#{rid}")
    return ", ".join(out) or "none"


def _mdy_ts(ts):
    from time_utils import mdy
    try:
        return mdy(datetime.fromtimestamp(int(ts)))
    except (TypeError, ValueError, OSError):
        return ""


def _stripe_dispatch(event):
    """Everything a verified, first-time Stripe event does. Raises on any
    state write that fails, so stripe_webhook can release the claim and
    answer 5xx; email failures are the only ones swallowed — and billing
    email to a client is owed (billing_jobs.owed_sends) before it is sent,
    so a send that fails is retried, not lost."""
    etype = event.get("type") or ""
    obj = (event.get("data") or {}).get("object") or {}
    rid, how = _resolve_stripe_event(event) if etype != "checkout.session.completed" else (None, None)
    # The ledger, after the duplicate check and once per event id (#50).
    try:
        import admin_events as _ae
        _ae.record_stripe(event, restaurant_id=rid)
    except Exception:
        pass
    if _stale_billing_event(event, rid):
        # Older than what this restaurant's billing state already reflects:
        # recorded, not applied (MOD-BIL-1).
        print(f"Stripe {etype} {event.get('id')} is older than the last applied event — ignored")
        return jsonify(ok=True, stale=True)
    if etype == "checkout.session.completed":
        return _on_checkout_completed(event, obj)
    if etype in ("customer.subscription.created", "customer.subscription.updated"):
        return _on_subscription_changed(event, obj, rid, how)
    if etype == "customer.subscription.deleted":
        return _on_subscription_deleted(event, obj, rid, how)
    if etype in ("charge.refunded", "charge.dispute.created"):
        return _on_money_returned(event, obj, rid, how)
    if etype == "charge.dispute.closed":
        return _on_dispute_closed(event, obj, rid, how)
    if etype == "invoice.payment_action_required":
        # 3-D Secure. The client has to authenticate or the payment never
        # lands; silence here looked exactly like a successful renewal.
        email = obj.get("customer_email", "unknown")
        _send_alert(
            f"🔐 Payment needs authentication — {email}",
            f"""Stripe needs the client to confirm this payment (3-D Secure).<br><br>
            <strong>Restaurant:</strong> {_names([rid]) if rid else '(no restaurant matched)'}<br>
            <strong>Customer:</strong> {_emails.esc(email)}<br>
            <strong>Amount:</strong> ${(obj.get('amount_due', 0) / 100):.2f}<br><br>
            They should have an email from Stripe. Access is unchanged for now."""
        )
        return jsonify(ok=True)
    if etype == "invoice.payment_failed":
        return _on_payment_failed(event, obj, rid, how)
    if etype == "invoice.paid":
        return _on_invoice_paid(event, obj, rid, how)
    return jsonify(ok=True)


def _fill_mirror_from_stripe(rid, sub_id, event_id=None):
    """Checkout carries only the subscription id; its amount, interval, trial
    end and metadata come from Stripe. Best effort — the next subscription
    event or the nightly reconcile fills anything this misses."""
    import billing_jobs as _bj
    key = os.getenv("STRIPE_SECRET_KEY", "")
    if not key or not sub_id:
        return
    try:
        s = config.stripe_api(key).Subscription.retrieve(sub_id)
        if _bj._g(s, "id") == sub_id:
            _bj.upsert_subscription(rid, sub=s, event_id=event_id)
            _bj.record_module_mismatch(rid)
    except Exception as e:
        print(f"mirror of {sub_id} not filled at checkout: {_safe_err(e)}")


def _on_checkout_completed(event, sess):
    # The event that actually knows who paid and what for. Before this
    # existed, nothing ran until the first invoice.paid — so the Stripe
    # customer id was unknown in between, and the module count sitting in
    # metadata was never read by anything at all.
    import billing_jobs as _bj
    import models as _m
    meta        = sess.get("metadata") or {}
    customer_id = sess.get("customer", "") or ""
    sub_id      = sess.get("subscription", "") or ""
    email       = (sess.get("customer_details") or {}).get("email") or sess.get("customer_email") or ""
    rid = _restaurant_from_metadata(meta)
    how = "metadata" if rid else None
    if not rid:
        rid = _restaurant_for_customer(customer_id) if customer_id else None
        how = "customer" if rid else None
    if not rid:
        rid = _restaurant_for_stripe("", email)
        how = "email" if rid else None
    if rid and sub_id and _second_subscription(rid, sub_id, sess.get("id"), customer_id):
        # Paid in both the monthly and the annual tab: the first
        # subscription stands, this one is cancelled and Will is told to
        # refund its setup fee (MOD-BIL-5). It is remembered as a duplicate,
        # so that refund, its cancellation and its invoices never pause,
        # churn or re-key the paying account (COMMS-19).
        cancelled = False
        try:
            key = os.getenv("STRIPE_SECRET_KEY", "")
            config.stripe_api(key).Subscription.cancel(sub_id)
            cancelled = True
            _dup_note = "The second subscription was cancelled."
        except Exception as _de:
            _dup_note = f"Cancelling it failed ({_safe_err(_de)}); cancel {sub_id} in Stripe."
        _bj.record_duplicate_subscription(sub_id, rid, customer_id, sess.get("id"), cancelled=cancelled)
        _send_alert(f"⚠ Second checkout for the same restaurant — {_emails.esc(meta.get('restaurant') or email)}",
                    f"Restaurant {rid} completed a second checkout (subscription {sub_id}). {_dup_note} "
                    "Refund the second setup fee in Stripe — that refund will not pause the account.")
        return jsonify(ok=True, duplicate_subscription=True)
    if rid:
        r = get_restaurant(rid)
        lock = _m.pause_lock(r)
        updates = {}
        notes = []
        if lock:
            notes.append(f"Access NOT turned on: the account is held for a {lock} until an admin lifts it.")
        else:
            updates.update({"billing_status": "active", "pause_reason": None, "paused_until": None})
        stored = (getattr(r, "stripe_customer_id", "") or "").strip()
        if customer_id and (not stored or (how == "metadata" and stored != customer_id)):
            # Metadata names this restaurant, so a new customer (a returning
            # client's new checkout) replaces the old one — recorded in the
            # billing history. An email match never overwrites (#115).
            updates["stripe_customer_id"] = customer_id
        elif customer_id and stored and stored != customer_id:
            notes.append(f"This checkout's customer {customer_id} differs from the stored {stored}; "
                         "the stored one was kept.")
        if not getattr(r, "converted_at", None) and (sess.get("payment_status") or "paid") != "unpaid":
            updates["converted_at"] = _bj._stamp()
        try:
            if updates:
                update_restaurant(rid, updates)
            moved = [rid] if not lock else []
            if not lock:
                _m2, held = _apply_state([s for s in _bj.subscription_scope(rid, sub_id) if s != rid], "active",
                                         pause_reason=None, paused_until=None)
                moved += _m2
        except Exception as e:
            print(f"checkout.session.completed: failed to activate {rid}: {e}")
            raise
        if sub_id:
            _bj.upsert_subscription(rid, subscription_id=sub_id, session_id=sess.get("id"),
                                    facts={"customer_id": customer_id or None}, event_id=event.get("id"))
            _fill_mirror_from_stripe(rid, sub_id, event.get("id"))
        granted = _apply_module_entitlement(rid, meta.get("module_keys", ""))
        if sub_id:
            _bj.record_module_mismatch(rid, meta.get("module_keys"))
        _send_alert(
            f"✅ Checkout completed — {_emails.esc(meta.get('restaurant') or email)}",
            f"""Checkout completed{'' if lock else ' and access provisioned'}.<br><br>
            <strong>Restaurant:</strong> {_emails.esc(meta.get('restaurant') or '(unknown)')} (id {rid})<br>
            <strong>Locations on this subscription:</strong> {_names(moved) if not lock else '—'}<br>
            <strong>Stripe customer:</strong> {customer_id or '(none)'}<br>
            <strong>Subscription:</strong> {sub_id or '(none)'}<br>
            <strong>Modules granted:</strong> """ + (
                ", ".join(sorted(k.replace("module_", "") for k, v in granted.items() if v))
                if granted else "none in metadata — entitlement left as it was, set it in admin")
            + ("<br><br>" + "<br>".join(notes) if notes else "")
        )
    else:
        # Nothing matched: do what the admin form would do, from the
        # fields the session carries, and tell Will what happened rather
        # than what to do (provisioning.py). Falls through to the old
        # alert when it would have to guess.
        # A provisioning failure is raised, not printed: the webhook then
        # releases its claim and answers 500, so Stripe's retry provisions
        # the paid customer instead of being dropped as a duplicate while
        # they have no login (DATA-54).
        import provisioning
        _new = provisioning.provision_from_checkout(sess)
        if _new:
            _granted = _apply_module_entitlement(_new, meta.get("module_keys", ""))
            if sub_id:
                _bj.upsert_subscription(_new, subscription_id=sub_id, session_id=sess.get("id"),
                                        facts={"customer_id": customer_id or None}, event_id=event.get("id"))
                _fill_mirror_from_stripe(_new, sub_id, event.get("id"))
            _send_alert(
                f"✅ Provisioned from checkout — {_emails.esc(meta.get('restaurant') or email)}",
                f"""No restaurant matched this checkout, so one was created from it.<br><br>
                <strong>Restaurant id:</strong> {_new}<br>
                <strong>Owner:</strong> {_emails.esc(email)}<br>
                <strong>Stripe customer:</strong> {customer_id or '(none)'}<br>
                <strong>Modules:</strong> {', '.join(sorted(k.replace('module_', '') for k, v in (_granted or {}).items() if v)) or 'reviews'}<br><br>
                The welcome email with a set-password link is owed to the owner (it retries until it
                is delivered). Add the Google Place ID and the contract at
                <a href="https://dashboard.cavnar.ai/admin">dashboard.cavnar.ai/admin</a>."""
            )
            return jsonify(ok=True, provisioned=_new), 200
        _send_alert(
            "⚠ Checkout completed but no restaurant matched",
            f"""A checkout completed and could not be reconciled.<br><br>
            <strong>Customer:</strong> {customer_id}<br>
            <strong>Email:</strong> {_emails.esc(email)}<br>
            <strong>Metadata:</strong> {_emails.esc(meta)}<br><br>
            Nothing was provisioned. Set this up by hand at
            <a href="https://dashboard.cavnar.ai/admin">dashboard.cavnar.ai/admin</a>."""
        )
    return jsonify(ok=True)


def _on_subscription_changed(event, sub, rid, how):
    """customer.subscription.created / .updated — the mirror first, then state.

    Module flags follow Stripe only when Stripe's plan actually changed
    (previous_attributes names metadata or items, or the subscription is
    new). Every update used to re-apply checkout's module_keys, so a module
    the console added was silently revoked at the next renewal (#106); a
    disagreement is now recorded on the mirror for the console to raise."""
    import billing_jobs as _bj
    from client_api import log_account_event
    etype = event.get("type") or ""
    sub_id = sub.get("id")
    meta = sub.get("metadata") or {}
    status = (sub.get("status") or "").lower()
    if not rid:
        print(f"{etype} for unmatched customer {sub.get('customer','')}")
        return jsonify(ok=True, unmatched=True)
    if how == "duplicate" or _bj.duplicate_subscription(sub_id):
        return jsonify(ok=True, duplicate_subscription=True)
    mirror = _bj.mirror_row(rid)
    if mirror and mirror.get("subscription_id") != sub_id and status in _bj.LIVE_SUB_STATES \
            and _subscription_still_live(rid, mirror):
        # The restaurant is billed on another live subscription; this one
        # must not move its state. Two live subscriptions is Will's call.
        _send_alert(f"⚠ Second live subscription — {_names([rid])}",
                    f"Stripe reports subscription {sub_id} ({status}) for restaurant {rid}, which is billed "
                    f"on {mirror.get('subscription_id')}. Nothing was changed; cancel whichever is wrong in Stripe.")
        return jsonify(ok=True, untracked_subscription=True)
    if mirror and mirror.get("subscription_id") != sub_id and status not in _bj.LIVE_SUB_STATES:
        return jsonify(ok=True, ignored="an old subscription")
    _bj.upsert_subscription(rid, sub=sub, event_id=event.get("id"))
    prev_attrs = (event.get("data") or {}).get("previous_attributes") or {}
    plan_changed = etype == "customer.subscription.created" or "metadata" in prev_attrs or "items" in prev_attrs
    granted = _apply_module_entitlement(rid, meta.get("module_keys", "")) if plan_changed else {}
    mm = _bj.record_module_mismatch(rid, meta.get("module_keys"))
    r = get_restaurant(rid)
    _prev = (getattr(r, "billing_status", "") or "").lower()
    scope = _bj.subscription_scope(rid, sub_id)
    paused_until = _paused_until(sub)
    held = []
    if paused_until and status in _bj.LIVE_SUB_STATES:
        # A pause (self-serve, or set in the Stripe dashboard) leaves
        # status "active" with pause_collection set. This event fires
        # for the pause itself, so reading status alone would undo a
        # pause seconds after it was made. When Stripe reaches
        # resumes_at it clears pause_collection and fires again, and
        # the branch below brings the account back on its own.
        _moved, held = _apply_state(scope, "paused", pause_reason="self", paused_until=paused_until)
        print(f"billing_status=paused until {paused_until} for {_moved} ({etype})")
        # One account history whoever pressed the button: a pause
        # set in the Stripe dashboard reads exactly like a self-serve
        # one in Account → Security → Activity.
        if _prev != "paused" and _moved:
            log_account_event(rid, "subscription_paused", detail=f"set in Stripe, resumes {paused_until}")
    elif status in ("active", "trialing"):
        _moved, held = _apply_state(scope, "active", pause_reason=None, paused_until=None)
        if _prev == "paused" and rid in _moved:
            log_account_event(rid, "subscription_resumed",
                              detail="Stripe reached the resume date, or the pause was cleared in Stripe")
    elif status in ("past_due", "paused"):
        _moved, held = _apply_state(scope, "past_due", pause_reason=None, paused_until=None,
                                    skip_from=("churned", "canceled", "cancelled"))
    elif status in _bj.ENDED_SUB_STATES:
        _moved, held = _apply_state(scope, "churned", override_locks=True)
    if granted or mm or held or status not in ("active", "trialing"):
        _send_alert(
            f"🔁 Subscription changed — {_emails.esc(meta.get('restaurant') or rid)}",
            f"""Subscription status is now <strong>{_emails.esc(status)}</strong>.<br><br>
            <strong>Restaurant id:</strong> {rid}<br>
            <strong>Modules:</strong> """ + (
                ", ".join(sorted(k.replace("module_", "") for k, v in granted.items() if v))
                if granted else "unchanged")
            + (f"<br><strong>Stripe bills</strong> {', '.join(mm['stripe'])} but the account has "
               f"{', '.join(mm['local']) or 'none'} — use Change plan in the console to line them up." if mm else "")
            + (f"<br><strong>Held by a lock (not changed):</strong> {_names(held)}" if held else "")
        )
    return jsonify(ok=True)


def _on_subscription_deleted(event, sub, rid, how):
    """The subscription is over: every location it covered is churned —
    and ONLY the locations it covered, and only when it is the restaurant's
    current subscription (#115). An old or duplicate subscription ending
    changes nothing."""
    import billing_jobs as _bj
    sub_id = sub.get("id")
    customer_id = sub.get("customer", "")
    details = sub.get("cancellation_details") or {}
    reason = details.get("reason") or "unknown"
    if how == "duplicate" or _bj.duplicate_subscription(sub_id):
        _bj.record_duplicate_subscription(sub_id, rid, customer_id if isinstance(customer_id, str) else None,
                                          cancelled=True)
        print(f"Duplicate subscription {sub_id} ended — nothing to revoke")
        return jsonify(ok=True, duplicate_subscription=True)
    if not rid:
        print(f"Subscription cancelled but no restaurant matched customer {customer_id}")
        _send_alert(
            f"📋 Subscription cancelled — {customer_id}",
            f"""A client subscription has been cancelled.<br><br>
            <strong>Customer ID:</strong> {customer_id}<br>
            <strong>Subscription:</strong> {sub_id}<br>
            <strong>Reason:</strong> {_emails.esc(reason)}<br>
            <strong>Access:</strong> NOT revoked — no restaurant is billed on this subscription or customer.
            Check it at <a href="https://dashboard.cavnar.ai/admin">dashboard.cavnar.ai/admin</a>."""
        )
        return jsonify(ok=True, unmatched=True)
    mirror = _bj.mirror_row(rid)
    if mirror and mirror.get("subscription_id") != sub_id and _bj.is_live_status(mirror.get("status")):
        print(f"Subscription {sub_id} ended; restaurant {rid} is billed on {mirror.get('subscription_id')} — no change")
        return jsonify(ok=True, ignored="not the current subscription")
    facts = _bj.subscription_facts(sub)
    facts["status"] = facts.get("status") or "canceled"
    _bj.upsert_subscription(rid, facts=facts, subscription_id=sub_id, event_id=event.get("id"))
    scope = _bj.subscription_scope(rid, sub_id)
    # Raises on a failed write: the alert below then never claims a
    # revocation that did not happen, and Stripe redelivers (#7).
    moved, _held = _apply_state(scope, "churned", override_locks=True)
    print(f"Subscription cancelled — billing_status=churned for {moved}")
    _send_alert(
        f"📋 Subscription cancelled — {customer_id}",
        f"""A client subscription has been cancelled.<br><br>
        <strong>Customer ID:</strong> {customer_id}<br>
        <strong>Reason:</strong> {_emails.esc(reason)}{(' · ' + _emails.esc(details.get('feedback'))) if details.get('feedback') else ''}<br>
        <strong>Access:</strong> revoked — set to churned: {_names(moved)}.<br>
        <strong>Action needed:</strong> If this was unintentional, restore their
        billing status at <a href="https://dashboard.cavnar.ai/admin">dashboard.cavnar.ai/admin</a>."""
    )
    return jsonify(ok=True)


def _on_money_returned(event, obj, rid, how):
    """charge.refunded / charge.dispute.created (lead default 5).

    A dispute locks the whole group, reason 'dispute'. A FULL refund locks
    only the restaurant that paid, reason 'refund'. A partial refund — a $5
    goodwill credit on a $750 charge — changes nothing (MOD-BIL-2). Only an
    admin lifts either lock; the owner's Resume button and later Stripe
    events no longer can (#114). The duplicate checkout's refund, which the
    duplicate alert tells Will to issue, locks nobody (COMMS-19)."""
    import billing_jobs as _bj
    etype = event.get("type") or ""
    disputed = etype == "charge.dispute.created"
    customer_id = obj.get("customer", "") or ""
    email = (obj.get("billing_details") or {}).get("email") or obj.get("receipt_email") or ""
    if disputed:
        amount = (obj.get("amount") or 0) / 100
        full_refund = False
    else:
        amount = (obj.get("amount_refunded") or 0) / 100
        full_refund = bool(obj.get("refunded")) or (
            (obj.get("amount_refunded") or 0) >= (obj.get("amount") or 0) > 0)
    locked = []
    if how == "duplicate":
        access = "unchanged — this charge was the duplicate checkout's, cancelled against the paying one."
    elif rid and disputed:
        locked = _lock_accounts(_sibling_restaurant_ids(rid), "dispute")
        access = (f"paused for a dispute: {_names(locked)}. Only an admin can lift it — "
                  "Billing → Lift hold in the console.")
    elif rid and full_refund:
        locked = _lock_accounts([rid], "refund")
        access = (f"paused for a full refund: {_names(locked)} (only the location that paid). "
                  "Lift it in the console if the refund was a goodwill gesture.")
    elif rid:
        access = "left on — a partial refund does not pause the account."
    else:
        access = ("NOT changed — no restaurant matched this charge. Handle it by hand at "
                  "<a href=\"https://dashboard.cavnar.ai/admin\">dashboard.cavnar.ai/admin</a>.")
    _send_alert(
        ("⛔ Chargeback opened — " if disputed else "↩ Refund issued — ") + _emails.esc(email or customer_id),
        f"""{'A customer has disputed a charge.' if disputed else 'A charge was refunded.'}<br><br>
        <strong>Amount:</strong> ${amount:,.2f}{'' if disputed or full_refund else ' (partial)'}<br>
        <strong>Customer:</strong> {customer_id or '(none)'}<br>
        <strong>Email:</strong> {_emails.esc(email) or '(none)'}<br>
        <strong>Access:</strong> {access}"""
    )
    return jsonify(ok=True)


def _on_dispute_closed(event, obj, rid, how):
    """A dispute's outcome. Nothing is lifted on its own: whether a client
    who disputed a charge keeps the product is Will's decision."""
    status = obj.get("status") or "closed"
    _send_alert(
        f"⚖ Dispute closed ({_emails.esc(status)}) — {_names([rid]) if rid else obj.get('charge')}",
        f"""A dispute has closed with status <strong>{_emails.esc(status)}</strong>.<br><br>
        The account stays on hold until an admin lifts it (console → Billing → Lift hold)."""
    )
    return jsonify(ok=True)


def _on_payment_failed(event, inv, rid, how):
    """A charge failed: past_due (still in service) for the locations the
    subscription covers, and a dunning email owed to the owner and every
    principal login on attempts 1, 2 and 3, with a link that fixes the card
    (#25). The dunning stops when the invoice is paid."""
    import billing_jobs as _bj
    email   = inv.get("customer_email", "unknown")
    amount  = inv.get("amount_due", 0) / 100
    attempt = int(inv.get("attempt_count") or 1)
    next_attempt = inv.get("next_payment_attempt")
    next_str = f" Stripe will retry on {_mdy_ts(next_attempt)}." if next_attempt else " Stripe will not retry again."
    facts = _bj.record_invoice(inv, rid if how != "duplicate" else None, event.get("id"), failed=True)
    owed = []
    if rid and how != "duplicate":
        # past_due is in ACTIVE_BILLING_STATES, so this is a warning state
        # and not a lockout — access continues while Stripe retries. A
        # paused (locked or self-paused) or churned account is never moved
        # back into service by its card failing (MOD-BIL-2).
        _apply_state(_bj.subscription_scope(rid, facts.get("subscription_id")), "past_due",
                     skip_from=("paused", "churned", "canceled", "cancelled"))
        # Dunning says "your dashboard keeps running": owed only to an
        # account that is past due now — never to one on hold or churned.
        if (getattr(get_restaurant(rid), "billing_status", "") or "").lower() == "past_due":
            owed, _made = _bj.enqueue_dunning(rid, facts.get("invoice_id"), attempt,
                                              facts.get("amount_remaining_cents") or facts.get("amount_due_cents"),
                                              facts.get("next_payment_attempt"))
    client_note = (f"the client is owed a dunning email ({len(owed)} recipient(s)); it goes out within minutes."
                   if owed else ("no client email for this attempt (only attempts 1–3 are emailed)."
                                 if rid and attempt > 3 else
                                 ("no client email — the account is on hold or not in service."
                                  if rid and how != "duplicate" else "no client email — no restaurant matched.")))
    _send_alert(
        f"⚠ Payment failed — {_emails.esc(email)}",
        f"""A client payment has failed.<br><br>
        <strong>Restaurant:</strong> {_names([rid]) if rid and how != 'duplicate' else '(no restaurant matched)'}<br>
        <strong>Customer:</strong> {_emails.esc(email)}<br>
        <strong>Amount:</strong> ${amount:.2f}<br>
        <strong>Attempt:</strong> #{attempt}.{next_str}<br>
        <strong>Client:</strong> {client_note}<br><br>
        The console's "Send card-update link" re-sends the fix-your-card link on demand."""
    )
    return jsonify(ok=True)


def _on_invoice_paid(event, inv, rid, how):
    """A paid invoice: one receipt per invoice id (#155), dunning for it
    stood down (#25), past-due cleared, and — on the first paid invoice
    with a non-zero retainer charge — the "New paying client" alert. None of
    it depends on the order Stripe delivers checkout and invoice events in
    any more: the receipt used to go only when the account was not already
    active, which checkout had almost always made it."""
    import billing_jobs as _bj
    import ops
    customer_id = inv.get("customer", "") if isinstance(inv.get("customer"), str) else ""
    email = inv.get("customer_email", "unknown")
    facts = _bj.record_invoice(inv, rid if how != "duplicate" else None, event.get("id"), paid=True)
    inv_id = facts.get("invoice_id")
    amount_paid = int(facts.get("amount_paid_cents") or 0)
    billing_reason = inv.get("billing_reason", "")
    sub_id = facts.get("subscription_id")
    print(f"Payment received: {inv_id} — ${amount_paid / 100:.2f} ({billing_reason})")
    if inv_id:
        _bj.cancel_owed(kind="dunning", key_prefix=f"dunning:{inv_id}:", reason="invoice paid")
    if how == "duplicate":
        return jsonify(ok=True, duplicate_subscription=True)
    if not rid:
        _send_alert(f"💳 Payment received — no restaurant matched ({_emails.esc(email)})",
                    f"Invoice {inv_id} (${amount_paid / 100:,.2f}) was paid by customer {customer_id or '(none)'}, "
                    "which no restaurant is billed to. Nothing was changed.")
        return jsonify(ok=True, unmatched=True)
    r = get_restaurant(rid)
    updates = {}
    stored = (getattr(r, "stripe_customer_id", "") or "").strip()
    if customer_id and not stored and how in ("subscription", "metadata"):
        updates["stripe_customer_id"] = customer_id
    if amount_paid > 0 and not getattr(r, "converted_at", None):
        updates["converted_at"] = _bj._stamp()
    if updates:
        update_restaurant(rid, updates)
    mirror = _bj.mirror_row(rid)
    if sub_id and (not mirror or not _bj.is_live_status(mirror.get("status"))):
        # A paid invoice for a subscription the mirror did not hold (its
        # created event missed, or a returning client): it is the current one.
        _bj.upsert_subscription(rid, subscription_id=sub_id, facts={"customer_id": customer_id or None,
                                                                    "status": None}, event_id=event.get("id"))
        mirror = _bj.mirror_row(rid)
    current = bool(sub_id and mirror and mirror.get("subscription_id") == sub_id)
    cur = (getattr(r, "billing_status", "") or "").lower()
    if current and (cur == "past_due" or (cur in ("", "trial", "pending", "churned", "canceled", "cancelled")
                                          and billing_reason in ("subscription_create", "subscription_cycle",
                                                                 "subscription_update"))):
        # A retry succeeded, or the retainer (or the first invoice) was paid:
        # every location the subscription covers is in service. A lock only
        # an admin lifts stays where it is.
        _apply_state(_bj.subscription_scope(rid, sub_id), "active", pause_reason=None, paused_until=None)
    if amount_paid > 0 and inv_id:
        _bj.enqueue("receipt", rid, f"receipt:{inv_id}", to_email=(r.owner_email or None) or
                    (email if email and email != "unknown" else None),
                    payload={"invoice_id": inv_id, "amount_cents": amount_paid, "kind": facts.get("kind")},
                    source="stripe")
    if int(facts.get("recurring_cents") or 0) > 0 and amount_paid > 0 and _bj.first_paid_retainer(rid, inv_id) \
            and ops.claim_period("conversion_alert", str(rid)):
        _send_alert(
            f"💳 New paying client — {_emails.esc(r.name)}",
            f"""<strong>{_emails.esc(r.name)}</strong> just paid its first retainer invoice.<br><br>
            <strong>Amount:</strong> ${amount_paid / 100:,.2f}<br>
            <strong>Invoice:</strong> {inv_id} ({_emails.esc(billing_reason.replace('_', ' '))})"""
        )
    return jsonify(ok=True)

@webhook_bp.route("/docusign/callback")

@webhook_bp.route("/docusign/callback2")
def docusign_callback():
    """Handle DocuSign OAuth callback — just confirms consent was granted."""
    code = request.args.get("code")
    error = request.args.get("error")
    if error:
        # Escaped: this public route used to reflect ?error= into the page
        # verbatim, under a CSP that allows inline script (SEC-10).
        from markupsafe import escape as _esc
        return f"""<div style="font-family:sans-serif;max-width:500px;margin:60px auto;padding:24px">
            <h2 style="color:#c84b2f">DocuSign Error</h2>
            <p>Error: {_esc(error[:200])}</p>
            <p><a href="/admin">Back to admin</a></p>
        </div>"""
    if code:
        return """<div style="font-family:sans-serif;max-width:500px;margin:60px auto;padding:24px;text-align:center">
            <h2 style="color:#2d6a4f">&#10003; DocuSign Connected</h2>
            <p style="color:#3a3530;margin:12px 0">Production consent granted successfully.<br>
            Contracts will now send automatically when you create a client.</p>
            <a href="/admin" style="display:inline-block;margin-top:16px;background:#c84b2f;color:white;padding:10px 24px;border-radius:6px;text-decoration:none;font-weight:600">Back to admin</a>
        </div>"""
    return redirect("/admin")


# DocuSign Connect names an envelope's state two ways (legacy "status" and
# the JSON SIM "event"); both map here. A recipient-level decline is the
# client refusing the agreement.
_DOCUSIGN_STATES = {
    "completed": "completed", "envelope-completed": "completed",
    "declined": "declined", "envelope-declined": "declined", "recipient-declined": "declined",
    "voided": "voided", "envelope-voided": "voided",
    "delivered": "delivered", "envelope-delivered": "delivered", "recipient-delivered": "delivered",
}


def _docusign_reason(data, state):
    summ = (data.get("data") or {}).get("envelopeSummary") or {}
    if state == "voided":
        return (summ.get("voidedReason") or data.get("voidedReason") or "")[:300]
    if state == "declined":
        for s in ((summ.get("recipients") or {}).get("signers") or []):
            if s.get("declinedReason"):
                return str(s["declinedReason"])[:300]
    return ""


@webhook_bp.route("/docusign/webhook", methods=["POST"])
def docusign_webhook():
    """Receive DocuSign connect notifications when envelope status changes."""
    # HMAC-SHA256 check — DocuSign Connect signs the raw body with the configured
    # HMAC key and sends it base64-encoded in X-DocuSign-Signature-1. Previously
    # this only checked the header was *present*, which any caller can fake —
    # now it actually verifies the signature matches the body.
    ds_secret = os.getenv("DOCUSIGN_WEBHOOK_SECRET", "")
    if not ds_secret:
        # Fail closed. Without the secret the check used to be skipped, so an
        # unsigned "completed" marked a contract signed and could replace the
        # owner's password (SEC-11, MOD-BIL-7). A missing variable now stops
        # every delivery, loudly, instead of trusting every caller.
        _webhook_seen("docusign", False, error="DOCUSIGN_WEBHOOK_SECRET is not set")
        return jsonify(error="Unauthorized"), 401
    import hmac as _hmac_ds, hashlib as _hashlib_ds, base64 as _b64_ds
    auth_header = request.headers.get("X-DocuSign-Signature-1", "")
    raw_bytes = request.get_data()
    expected = _b64_ds.b64encode(
        _hmac_ds.new(ds_secret.encode(), raw_bytes, _hashlib_ds.sha256).digest()
    ).decode()
    if not auth_header or not _hmac_ds.compare_digest(auth_header, expected):
        _webhook_seen("docusign", False, error="signature mismatch" if auth_header else "no signature header")
        return jsonify(error="Unauthorized"), 401
    claimed_key = None
    try:
        data = request.get_json(force=True) or {}
        # Try multiple envelope ID locations
        envelope_id = (
            data.get("envelopeId") or
            data.get("data",{}).get("envelopeId","") or
            data.get("data",{}).get("envelopeSummary",{}).get("envelopeId","")
        )
        # Try multiple status locations
        raw_status = (
            data.get("status") or
            data.get("event") or
            data.get("data",{}).get("envelopeSummary",{}).get("status","") or
            data.get("data",{}).get("status","")
        )
        state = _DOCUSIGN_STATES.get(str(raw_status or "").strip().lower())
        # The envelope id and its status, never the payload: it carries the
        # signers' names and addresses (COMMS-24).
        print(f"DocuSign webhook envelope_id={envelope_id} status={raw_status}")
        _webhook_seen("docusign", True, event=f"{envelope_id}:{raw_status}")
        if not envelope_id or not state:
            return jsonify(ok=True)
        if not _claim_docusign_event(envelope_id, state):
            print(f"DocuSign envelope {envelope_id} already processed ({state}) — ignoring repeat delivery")
            return jsonify(ok=True, duplicate=True), 200
        claimed_key = f"{envelope_id}:{state}"
        if state == "completed":
            owed = _docusign_completed(envelope_id)
        else:
            _docusign_state(envelope_id, state, _docusign_reason(data, state))
            owed = []
        # From here on only emails are sent; a failure in them must not
        # release the claim (that would re-send on DocuSign's retry). They
        # are owed first (billing_jobs.owed_sends), so a send that fails
        # here is retried by the drain rather than lost (#12).
        claimed_key = None
        if owed:
            try:
                import billing_jobs as _bj
                _bj.drain_owed_sends(ids=owed)
            except Exception as e:
                print(f"post-signing send attempt failed; the outbox retries it: {e}")
        return jsonify(ok=True)
    except Exception as e:
        print(f"DocuSign webhook error: {e}")
        if claimed_key:
            # The contract write failed after the claim: release it and ask
            # DocuSign to retry, instead of a 200 that dropped the signing.
            _release_claim("docusign_events_seen", "event_key", claimed_key)
            try:
                import ops
                ops.capture(e, job="docusign_webhook", context=claimed_key)
            except Exception:
                pass
            return jsonify(error="processing failed; will retry"), 500
        return jsonify(ok=True)


def _envelope_restaurants(envelope_id):
    """Every restaurant this envelope belongs to: the one it is current for,
    and the one it was sent to if it has since been superseded (MOD-BIL-6)."""
    conn = get_conn()
    try:
        rows = conn.execute(
            "SELECT id, docusign_envelope_id FROM restaurants WHERE docusign_envelope_id=? "
            "OR id = (SELECT restaurant_id FROM docusign_envelopes WHERE envelope_id=?) "
            "ORDER BY (docusign_envelope_id = ?) DESC, id",
            (envelope_id, envelope_id, envelope_id)).fetchall()
        return [(r["id"], r["docusign_envelope_id"] == envelope_id) for r in rows]
    finally:
        conn.close()


def _record_envelope_state(envelope_id, state, reason=""):
    conn = get_conn()
    try:
        conn.execute("UPDATE docusign_envelopes SET status=?, status_at=datetime('now'), status_reason=? "
                     "WHERE envelope_id=?", (state, reason or None, envelope_id))
        conn.commit()
    finally:
        conn.close()


def _docusign_state(envelope_id, state, reason=""):
    """declined / voided / delivered (#144): recorded on the envelope and the
    ledger, and — when it is the restaurant's current envelope and the
    contract is not already signed — on contract_status, which the console
    raises. A superseded envelope being voided changes nothing current."""
    import admin_events as _ae
    import models as _m
    _record_envelope_state(envelope_id, state, reason)
    rids = _envelope_restaurants(envelope_id)
    for rid, current in rids:
        r = get_restaurant(rid)
        if not r:
            continue
        cs = (r.contract_status or "pending").lower()
        if current and cs != "signed" and (state != "delivered" or cs in ("sent", "pending")):
            with _m.billing_context(source="docusign", actor="docusign", reason=f"envelope {state}"):
                update_restaurant(rid, {"contract_status": state})
        try:
            _ae.record("docusign", f"contract.{state}", restaurant_id=rid, envelope_id=envelope_id,
                       summary=f"Contract {state}" + (f": {reason}" if reason else "")
                       + ("" if current else " (a superseded envelope)"))
        except Exception:
            pass


def _docusign_completed(envelope_id):
    """A signed contract. The restaurant is marked signed (with when), and —
    for a client who is not already paying — the payment link and the
    welcome are OWED before anything is sent (#12): written here, in the
    step the claim protects, drained right after and retried with backoff
    until delivered, marked from the real delivery result. No password is
    reset: the welcome carries a set-password link, only to an owner who
    has never signed in (SEC-11). Returns the owed_sends ids to send now."""
    import admin_events as _ae
    import billing_jobs as _bj
    import models as _m
    rids = _envelope_restaurants(envelope_id)
    now = _bj._stamp()
    with _m.billing_context(source="docusign", actor="docusign", reason="contract signed"):
        for rid, _current in rids:
            r = get_restaurant(rid)
            updates = {"contract_status": "signed"}
            if r is not None and not getattr(r, "contract_signed_at", None):
                updates["contract_signed_at"] = now
            update_restaurant(rid, updates)
    _record_envelope_state(envelope_id, "completed")
    _m._invalidate_request_cache()
    print(f"Contract signed: {envelope_id}")
    primary = rids[0][0] if rids else None
    try:
        r0 = get_restaurant(primary) if primary else None
        _ae.record("docusign", "contract.signed", restaurant_id=primary,
                   email=(r0.owner_email if r0 else None), summary="Contract signed", envelope_id=envelope_id)
    except Exception:
        pass
    if not primary:
        print(f"WARNING: No restaurant found for envelope {envelope_id} - emails not owed")
        return []
    r = get_restaurant(primary)
    # A re-sent contract signed by a client who is already paying (or whose
    # group's subscription covers it) is a contract update, not onboarding:
    # no second setup-fee link and no welcome email (MOD-BIL-6).
    if (r.billing_status or "").lower() in ("active", "past_due") or _bj.billed_by(primary):
        print(f"Envelope {envelope_id} signed by an already-paying client — no payment or welcome email")
        return []
    owed = []
    pid, _made = _bj.enqueue("payment_link", primary, f"payment_link:{primary}:{envelope_id}",
                             to_email=r.owner_email, source="docusign")
    owed.append(pid)
    login = _bj.principal_login(primary)
    if login and not login.get("last_login"):
        wid, _made = _bj.enqueue("welcome", primary, f"welcome:{login['id']}",
                                 to_email=login.get("email") or r.owner_email,
                                 payload={"user_id": login["id"]}, source="docusign")
        owed.append(wid)
    elif login:
        print(f"Owner of {r.name} has already signed in — no welcome and no new password")
    return [i for i in owed if i]


# ── Inbound SMS (Twilio) ────────────────────────────────────────────────────
# Public and unauthenticated by necessity — Twilio is the caller. The
# signature check stands in for auth: without it anyone could forge a STOP
# (silencing a guest) or a YES (granting marketing consent on someone's
# behalf, the exact thing guest_marketing's consent model exists to prevent).

@webhook_bp.route("/webhooks/twilio/sms", methods=["POST"])
def twilio_inbound_sms():
    from flask import Response
    from notify import validate_twilio_signature
    from guest_marketing import handle_inbound_sms

    params = request.form.to_dict()
    # Twilio signs the URL it was configured with. Behind Railway's proxy
    # request.url arrives as http://, so rebuild it as https to match.
    url = request.url
    if request.headers.get("X-Forwarded-Proto") == "https" and url.startswith("http://"):
        url = "https://" + url[len("http://"):]

    if not validate_twilio_signature(url, params, request.headers.get("X-Twilio-Signature", "")):
        return Response("", status=403, mimetype="text/xml")

    from_phone = (params.get("From") or "").strip()
    body = params.get("Body") or ""
    if not from_phone:
        return Response("<Response></Response>", mimetype="text/xml")

    try:
        # The MessageSid rides on the consent evidence the reply produces
        # (guest_consent_events), so a confirmation can be tied to Twilio's
        # own record of the inbound text.
        reply = handle_inbound_sms(from_phone, body,
                                   message_sid=(params.get("MessageSid") or params.get("SmsSid") or None))
    except Exception as e:
        try:
            import ops
            ops.capture(e, job="twilio_inbound_sms", context=f"from={from_phone[-4:]}")
        except Exception:
            pass
        reply = None

    if reply:
        safe = reply.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        return Response(f"<Response><Message>{safe}</Message></Response>", mimetype="text/xml")
    return Response("<Response></Response>", mimetype="text/xml")


# ── Resend delivery events (bounces / complaints) ───────────────────────────
# Resend signs with Svix headers. Without consuming these, a hard-bounced
# address is retried forever and a spam complaint is invisible — both of
# which degrade the sending reputation of the domain that also carries 2FA
# and password-reset mail.

# An override for tests only. The live secret is read from the environment
# when each request arrives: it was frozen at import, so setting or rotating
# it on Railway did nothing until the next restart (MOD-EML-9) — the pattern
# already fixed for RESEND_API_KEY.
RESEND_WEBHOOK_SECRET = ""

# How far a Svix timestamp may be from now. Nothing checked it, so a
# captured signed event (a complaint that suppresses an address) could be
# replayed forever (MOD-EML-9). Five minutes is Svix's own tolerance.
SVIX_TOLERANCE_SECONDS = 300


def _webhook_secret() -> str:
    return RESEND_WEBHOOK_SECRET or os.getenv("RESEND_WEBHOOK_SECRET", "")

# Statuses worth suppressing on. A soft bounce (full mailbox, temporary
# defer) is deliberately not here — that address may well work tomorrow.
_SUPPRESS_EVENTS = {
    "email.bounced":   "bounced",
    "email.complained": "complained",
}


def _verify_svix(payload_body: bytes, headers) -> bool:
    """Svix signature: HMAC-SHA256 over "{id}.{timestamp}.{body}", keyed by
    the base64 secret after the 'whsec_' prefix. Multiple space-separated
    signatures may be present; any valid one passes."""
    import base64, hashlib, hmac as _hmac, time as _time
    secret = _webhook_secret()
    svix_id = headers.get("svix-id", "")
    svix_ts = headers.get("svix-timestamp", "")
    svix_sig = headers.get("svix-signature", "")
    if not (secret and svix_id and svix_ts and svix_sig):
        return False
    try:
        if abs(_time.time() - int(svix_ts)) > SVIX_TOLERANCE_SECONDS:
            return False
    except (TypeError, ValueError):
        return False
    try:
        key = base64.b64decode(secret.split("_", 1)[1] if secret.startswith("whsec_") else secret)
    except Exception:
        return False
    signed = f"{svix_id}.{svix_ts}.".encode() + payload_body
    expected = base64.b64encode(_hmac.new(key, signed, hashlib.sha256).digest()).decode()
    for part in svix_sig.split():
        candidate = part.split(",", 1)[1] if "," in part else part
        if _hmac.compare_digest(candidate, expected):
            return True
    return False


def _guest_email_types():
    from models import GUEST_EMAIL_TYPES
    return GUEST_EMAIL_TYPES


def _logged_send(message_id):
    """The email_log row a Resend event is about, or None."""
    if not message_id:
        return None
    try:
        from models import get_conn as _gc
        conn = _gc()
        try:
            row = conn.execute("SELECT email_type, restaurant_id FROM email_log WHERE message_id=? "
                               "ORDER BY id DESC LIMIT 1", (message_id,)).fetchone()
        finally:
            conn.close()
        return dict(row) if row else None
    except Exception:
        return None


def _unsubscribe_guest_email(address, restaurant_id):
    """A complaint about a restaurant's guest mail unsubscribes the address
    from that restaurant the way its own link does — by address, every row
    with it now or later (guest_email.unsubscribe_address, CS-2) — beside
    the cross-restaurant guest suppression the caller writes."""
    if not (address and restaurant_id):
        return
    try:
        import guest_email
        guest_email.unsubscribe_address(restaurant_id, address, source="complaint")
    except Exception as e:
        print(f"[resend-webhook] guest unsubscribe failed: {e}")


# The unsubscribe links Cavnar AI puts in its own mail: /e/ (a newsletter),
# /ue/ (a guest email with no list), /u/ (owner product mail). Resend
# rewrites every link for click tracking, the footer's too, so a guest
# unsubscribing was counted as a click on the newsletter (CS-7 / EML-5).
_UNSUBSCRIBE_PATH = r"^/(?:e|ue|u)/[^/]+/?$"


def _is_unsubscribe_click(data) -> bool:
    """Whether a Resend click event is on one of our unsubscribe links: the
    path, on this platform's own host (config.base_url, which built it)."""
    import re
    from urllib.parse import urlparse
    click = data.get("click") if isinstance(data, dict) else None
    link = (click or {}).get("link") if isinstance(click, dict) else None
    if not link:
        return False
    try:
        u = urlparse(str(link).strip())
        ours = urlparse(config.base_url()).netloc.lower()
    except Exception:
        return False
    return bool(re.match(_UNSUBSCRIBE_PATH, u.path or "")) and (not ours or u.netloc.lower() == ours)


@webhook_bp.route("/webhooks/resend", methods=["POST"])
def resend_webhook():
    from models import suppress_email, mark_email_delivery_event

    if not _verify_svix(request.get_data(), request.headers):
        return jsonify(ok=False, error="bad signature"), 403

    event = request.get_json(silent=True) or {}
    etype = event.get("type") or ""
    data = event.get("data") or {}
    message_id = data.get("email_id") or data.get("id") or ""
    to = data.get("to") or []
    recipients = to if isinstance(to, list) else [to]
    detail = ""
    if isinstance(data.get("bounce"), dict):
        detail = data["bounce"].get("message") or data["bounce"].get("type") or ""

    status = {
        "email.bounced": "bounced",
        "email.complained": "complained",
        "email.delivered": "delivered",
        "email.delivery_delayed": "delayed",
    }.get(etype)

    if status:
        mark_email_delivery_event(message_id, status, detail)

    # Engagement is recorded separately from delivery state — an open must
    # not erase the fact that the mail was delivered. See
    # models.mark_email_engagement for why these are a floor, not a rate.
    engagement = {"email.opened": "opened", "email.clicked": "clicked"}.get(etype)
    if engagement == "clicked" and _is_unsubscribe_click(data):
        engagement = None       # leaving the list is not a click on the email
    if engagement:
        from models import mark_email_engagement
        mark_email_engagement(message_id, engagement)

    if etype in _SUPPRESS_EVENTS:
        # A complaint about a restaurant's GUEST mail (a newsletter, a
        # review request) is about that list: it stops guest mail to the
        # address and unsubscribes them from that restaurant, and leaves the
        # same person's staff schedules and account mail alone (MOD-EML-7).
        # A bounce is about the mailbox itself, so it stops everything.
        sent = _logged_send(message_id)
        guest_mail = bool(sent and sent.get("email_type") in _guest_email_types())
        for addr in recipients:
            if etype == "email.complained" and guest_mail:
                suppress_email(addr, _SUPPRESS_EVENTS[etype], detail, scope="guest")
                _unsubscribe_guest_email(addr, sent.get("restaurant_id"))
            else:
                suppress_email(addr, _SUPPRESS_EVENTS[etype], detail)

    # Always 200 on a verified event — a non-2xx makes Resend retry, and
    # nothing here is worth replaying.
    return jsonify(ok=True)
