"""Provision a restaurant from a paid checkout when nothing matched.

The automation audit's one piece of manual work on Will's desk: a Stripe
checkout that arrived without restaurant_id metadata ended in an email
saying "Nothing was provisioned. Set this up by hand". The checkout
session knows the payer's email and name, the restaurant name and the
module keys — the same fields the admin console's create-client form asks
for. This does what that form does, sends the welcome email with the
temporary password, and tells Will what it did instead of what to do.

Refuses (returns None) rather than guessing when there is no email, or the
email already belongs to a login — that is a reconciliation, not a
provisioning, and it stays with Will.
"""
import secrets

from models import get_conn, DB_PATH

MODULE_KEYS = ("reviews", "labor", "inventory", "marketing")


def _username_for(email, db_path):
    base = (email.split("@", 1)[0] or "owner").lower()
    base = "".join(ch for ch in base if ch.isalnum() or ch in "._-")[:24] or "owner"
    conn = get_conn(db_path)
    try:
        name, n = base, 1
        while conn.execute("SELECT 1 FROM users WHERE username=?", (name,)).fetchone():
            n += 1
            name = f"{base}{n}"
        return name
    finally:
        conn.close()


def _claim(key, db_path):
    """One provisioning per checkout: two deliveries of the same session
    (Stripe retries, or two arriving together) both passed the email check
    and each created a restaurant (MOD-BIL-9)."""
    import ops
    return ops.claim_period("provision_checkout", key)


def _release(key, db_path):
    import ops
    ops.release_period("provision_checkout", key)


def _drop_restaurant(rid, db_path):
    """Undo a half-made provisioning: the restaurant row goes if its login
    could not be created, so no orphan 'trial' restaurant with no login is
    left behind (MOD-BIL-9)."""
    conn = get_conn(db_path)
    try:
        conn.execute("PRAGMA foreign_keys=OFF")
        conn.execute("DELETE FROM restaurants WHERE id=?", (rid,))
        conn.commit()
    finally:
        conn.close()


def provision_from_checkout(session, db_path=DB_PATH):
    """`session` is Stripe's checkout.session object (a dict). Returns the new
    restaurant id, or None when it would have to guess.

    An email that already belongs to an OWNER login is a second location for
    that owner: a new restaurant in their group, no new login. Any other
    existing login stays a reconciliation for Will."""
    meta = session.get("metadata") or {}
    details = session.get("customer_details") or {}
    email = ((details.get("email") or session.get("customer_email") or "").strip().lower())
    if not email:
        return None
    claim_key = session.get("id") or f"{session.get('customer') or ''}:{email}:{(meta.get('restaurant') or '').strip().lower()}"
    if not _claim(claim_key, db_path):
        return None
    try:
        return _provision(session, meta, details, email, db_path)
    except BaseException:
        _release(claim_key, db_path)
        raise


def _provision(session, meta, details, email, db_path):
    from models import Restaurant, create_restaurant, update_restaurant
    from auth import create_user
    import auth as _auth
    conn = get_conn(db_path)
    try:
        existing = conn.execute(
            "SELECT u.id, u.restaurant_id, COALESCE(u.role,'client') AS role, r.owner_email, r.location_group, r.name "
            "FROM users u JOIN restaurants r ON r.id = u.restaurant_id "
            "WHERE LOWER(u.email)=? AND u.is_active=1 ORDER BY u.id LIMIT 1", (email,)).fetchone()
    finally:
        conn.close()
    if existing and not (existing["role"] in ("client", "owner")
                         and (existing["owner_email"] or "").strip().lower() == email):
        return None                      # someone else's login: reconcile by hand
    name = (meta.get("restaurant") or "").strip() or (details.get("name") or "").strip() or email.split("@")[0]
    keys = {k.strip().lower() for k in (meta.get("module_keys") or "").split(",") if k.strip()}
    flags = {f"module_{k}": (1 if k in keys else 0) for k in MODULE_KEYS}
    if not any(flags.values()):
        flags["module_reviews"] = 1     # the one every plan has
    rid = create_restaurant(Restaurant(name=name[:120], owner_email=email,
                                       owner_name=(details.get("name") or "").strip()[:120] or None),
                            db_path=db_path)
    updates = dict(flags)
    updates.update({"billing_status": "active"})
    if session.get("customer"):
        updates["stripe_customer_id"] = session["customer"]
    if existing:
        # A second location for an owner who already signs in (MOD-BIL-9):
        # same group, same login, switchable from the location picker.
        group = (existing["location_group"] or "").strip() or (existing["name"] or "").strip() or email
        if not (existing["location_group"] or "").strip():
            update_restaurant(existing["restaurant_id"], {"location_group": group}, db_path=db_path)
        updates["location_group"] = group
        update_restaurant(rid, updates, db_path=db_path)
        _auth.set_user_role(existing["id"], "owner", db_path=db_path)
        try:
            _auth.upsert_membership(existing["id"], rid, "owner", db_path=db_path)
        except Exception:
            pass
        _record(rid, email, f"Added {name} as another location for {email}", keys)
        return rid
    password = secrets.token_urlsafe(9)
    username = _username_for(email, db_path)
    try:
        create_user(restaurant_id=rid, username=username, email=email, password=password, db_path=db_path)
    except BaseException:
        _drop_restaurant(rid, db_path)
        raise
    update_restaurant(rid, updates, db_path=db_path)   # the password is emailed once, never stored
    try:
        from emails import send_welcome_email
        send_welcome_email(to_email=email, restaurant_name=name, username=username, password=password,
                           **{k: v for k, v in flags.items()})
    except Exception as e:
        import ops
        ops.capture(e, job="provision_welcome", context=f"restaurant_id={rid}")
    _record(rid, email, f"Provisioned {name} from checkout ({', '.join(sorted(keys)) or 'reviews'})", keys)
    return rid


def _record(rid, email, summary, keys):
    try:
        import admin_events as _ae
        _ae.record("stripe", "checkout.provisioned", restaurant_id=rid, email=email, summary=summary)
    except Exception:
        pass
