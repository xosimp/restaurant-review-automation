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


def provision_from_checkout(session, db_path=DB_PATH):
    """`session` is Stripe's checkout.session object (a dict). Returns the new
    restaurant id, or None when it would have to guess."""
    meta = session.get("metadata") or {}
    details = session.get("customer_details") or {}
    email = ((details.get("email") or session.get("customer_email") or "").strip().lower())
    if not email:
        return None
    conn = get_conn(db_path)
    try:
        if conn.execute("SELECT 1 FROM users WHERE LOWER(email)=?", (email,)).fetchone():
            return None
    finally:
        conn.close()
    from models import Restaurant, create_restaurant, update_restaurant
    from auth import create_user
    name = (meta.get("restaurant") or "").strip() or (details.get("name") or "").strip() or email.split("@")[0]
    keys = {k.strip().lower() for k in (meta.get("module_keys") or "").split(",") if k.strip()}
    flags = {f"module_{k}": (1 if k in keys else 0) for k in MODULE_KEYS}
    if not any(flags.values()):
        flags["module_reviews"] = 1     # the one every plan has
    rid = create_restaurant(Restaurant(name=name[:120], owner_email=email,
                                       owner_name=(details.get("name") or "").strip()[:120] or None),
                            db_path=db_path)
    password = secrets.token_urlsafe(9)
    username = _username_for(email, db_path)
    create_user(restaurant_id=rid, username=username, email=email, password=password, db_path=db_path)
    updates = dict(flags)
    updates.update({"temp_password": password, "billing_status": "active"})
    if session.get("customer"):
        updates["stripe_customer_id"] = session["customer"]
    update_restaurant(rid, updates, db_path=db_path)
    try:
        from emails import send_welcome_email
        send_welcome_email(to_email=email, restaurant_name=name, username=username, password=password,
                           **{k: v for k, v in flags.items()})
    except Exception as e:
        import ops
        ops.capture(e, job="provision_welcome", context=f"restaurant_id={rid}")
    try:
        import admin_events as _ae
        _ae.record("stripe", "checkout.provisioned", restaurant_id=rid, email=email,
                   summary=f"Provisioned {name} from checkout ({', '.join(sorted(keys)) or 'reviews'})")
    except Exception:
        pass
    return rid
