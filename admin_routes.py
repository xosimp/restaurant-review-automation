"""
admin_routes.py — Cavnar AI admin, infrastructure and API routes
Registered as a Flask Blueprint in hosted_dashboard.py
"""
import config
from flask import Blueprint, request, jsonify, redirect, render_template, make_response, send_file, Response
import os, json, io
from datetime import datetime

# Import everything needed from the main app
from models import get_conn, get_restaurant, update_restaurant, create_restaurant, Restaurant, get_reviews_data, get_review_stats, log_email, get_changelog, save_changelog_entry, delete_changelog_entry, location_group_conflict, place_id_conflict
from auth import get_session_user, delete_session, create_user, update_password, admin_required, login_required, recent_auth_required
from emails import send_payment_email, send_welcome_email
import emails as _emails


from emails import html_document as _html_doc  # one definition; emails reads its env lazily

admin_bp = Blueprint('admin', __name__)
# A JSON body must be an object: "x" or [1] used to 500 (SEC-32).
from security import json_object_guard as _json_object_guard
_json_object_guard(admin_bp)

def sanitize(value, max_len=1000):
    """Strip HTML tags and limit length to prevent XSS."""
    if not value:
        return value
    import re
    # Remove HTML tags
    value = re.sub(r'<[^>]+>', '', str(value))
    # Remove javascript: protocol
    value = re.sub(r'(?i)javascript\s*:', '', value)
    # Truncate
    return value[:max_len].strip() or None

from emails import _resend_key, _from_email  # one definition each
ADMIN_USERNAME        = os.getenv("ADMIN_USERNAME", "will")
STRIPE_WEBHOOK_SECRET = os.getenv("STRIPE_WEBHOOK_SECRET", "")
STRIPE_SECRET_KEY     = os.getenv("STRIPE_SECRET_KEY", "")

@admin_bp.route("/admin")
@admin_required
def admin(current_user):
    """The admin console. Everything it shows is fetched from /admin/api/*
    (see admin_ops.py) — this only renders the shell."""
    return render_template("admin.html", current_user=current_user)


@admin_bp.route("/admin/api/system")
@admin_required
def admin_api_system(current_user):
    """The platform's configuration and physical state, for Engineering.

    `services` is the label -> configured map the console has always read,
    now over every variable docs/ops/SECURITY.md calls required, not one
    per provider: it said "Every key is configured" with CREDENTIAL_KEY and
    BACKUP_ENCRYPTION_KEY unset (#125). `keys` says which variables, what
    breaks without each, and whether a set key is valid. `system` is the
    rest (platform_monitor.system_report): disk, database and WAL, backups,
    the scheduler lease and heartbeat, the last restore drill, the AI
    breaker, the supervisor, request rollups, 5xx by route, boots, provider
    probes, the credential-encryption state, and `warnings` in plain
    sentences. Presence only — never a value."""
    import os as _os
    import platform_monitor
    system = platform_monitor.system_report()
    keys = system.pop("keys")
    services = {k["label"]: bool(k["present"]) for k in keys if k["required"]}
    build = (_os.getenv("RAILWAY_GIT_COMMIT_SHA") or "")[:8] or None
    if not build:
        try:
            import subprocess
            build = subprocess.check_output(["git", "rev-parse", "--short", "HEAD"], stderr=subprocess.DEVNULL, timeout=2).decode().strip()
        except Exception:
            build = None
    from models import DB_PATH
    return jsonify(ok=True, services=services, keys=keys, warnings=system.pop("warnings"), system=system,
                   env=("railway" if config.on_railway() else "local"),
                   tick=int(_os.getenv("SCHEDULER_TICK_SECONDS", "300")), db=_os.path.basename(str(DB_PATH)),
                   build=build)

@admin_bp.route("/admin/create-client", methods=["POST"])
@admin_required
def create_client(current_user):
    from models import create_restaurant, Restaurant
    data = request.get_json()
    try:
        # Check for duplicate email/username BEFORE creating anything
        conn_check = get_conn()
        existing = conn_check.execute(
            "SELECT id FROM users WHERE email=? OR username=?",
            (data["owner_email"], data["username"])
        ).fetchone()
        conn_check.close()
        if existing:
            return jsonify(ok=False, error="A user with that email or username already exists — try a different username or email")

        # location_group is free text and defines a tenancy boundary: everyone
        # in a group can switch into each other's locations and shares billing
        # state. Two unrelated clients typed into the same group would become
        # one tenant, so a name already used by a different owner is refused
        # here rather than discovered later as a data leak.
        place_clash = place_id_conflict((data.get("google_place_id") or "").strip())
        if place_clash:
            return jsonify(ok=False, error=(
                f"That Google listing is already connected to {place_clash}. Two live "
                f"restaurants on one listing both pull the same reviews and only one of "
                f"them can own any given review — use a different Place ID, or mark this "
                f"one as a demo."
            ))

        conflict = location_group_conflict(
            data.get("location_group", "").strip(), data["owner_email"]
        )
        if conflict:
            return jsonify(ok=False, error=(
                f"Location group \u201c{data.get('location_group','').strip()}\u201d already belongs to "
                f"{conflict}. Pick a different group name — locations in a group share data and billing."
            ))

        # Create restaurant
        rid = create_restaurant(Restaurant(
            name=data["restaurant_name"],
            owner_email=data["owner_email"],
            google_place_id=data.get("google_place_id") or None,
            yelp_business_id=data.get("yelp_business_id") or None,
            voice_notes=data.get("voice_notes") or None,
            owner_phone=data.get("owner_phone") or None,
            owner_name=data.get("owner_name") or None,
            location_group=data.get("location_group","").strip() or None,
            location_name=data.get("location_name","").strip() or None,
        ))
        try:
            create_user(
                restaurant_id=rid,
                username=data["username"],
                email=data["owner_email"],
                password=data["password"],
            )
        except Exception:
            # A double-clicked Create passed the duplicate check twice and
            # left an orphan restaurant with no login (DATA-62): take the
            # half-made one back out before reporting the failure.
            try:
                import models as _models_cc
                _models_cc.delete_restaurant(rid)
            except Exception as _del_e:
                _ops.capture(_del_e, job="create_client_rollback", context=f"restaurant_id={rid}")
            raise
        # Set module access directly from checkboxes
        def _flag(key, default=0):
            try: return int(data.get(key, default))
            except (TypeError, ValueError): return default

        from models import update_restaurant
        update_restaurant(rid, {
            "module_reviews":  _flag("module_reviews", 1),
            "module_labor":    _flag("module_labor"),
            "module_inventory":_flag("module_inventory"),
            "module_marketing":_flag("module_marketing"),
            # A Place ID IS the reviews connection until Google OAuth is
            # done — fetcher.fetch_google reads it directly. reviews_live
            # defaulted to 0 and was settable only from the settings page,
            # so scheduler.run_daily_fetch's
            # "WHERE reviews_live=1 OR gmb_refresh_token IS NOT NULL"
            # skipped every new client until someone remembered the
            # checkbox. The owner's first session was a working dashboard
            # with nothing in it, and nothing said why. The Place ID was
            # already validated against place_id_conflict above, so turning
            # this on is exactly as safe as the ID that was just accepted.
            "reviews_live":    1 if ((data.get("google_place_id") or "").strip()
                                     and _flag("module_reviews", 1)) else 0,
        })

        # Auto-fetch menu notes from Google Places if place ID provided
        google_place_id = data.get("google_place_id") or None
        if google_place_id and _flag("module_marketing"):
            try:
                from competitor import fetch_menu_notes_from_places
                auto_menu = fetch_menu_notes_from_places(google_place_id, restaurant_id=rid)
                if auto_menu:
                    update_restaurant(rid, {"menu_notes": auto_menu})
                    print(f"[create_client] Auto-fetched menu notes for {data['restaurant_name']}")
            except Exception as me:
                print(f"[create_client] Menu auto-fetch failed: {me}")
        module_names = []
        if _flag("module_reviews"): module_names.append("Review Intelligence")
        if _flag("module_labor"):   module_names.append("Labor Optimizer")
        if _flag("module_inventory"): module_names.append("Food Cost Control")
        if _flag("module_marketing"): module_names.append("Marketing Autopilot")
        mods = len(module_names)
        modules_list = ", ".join(module_names)

        # Step 1: Send contract via DocuSign
        envelope_id = None
        if mods > 0 and data.get("owner_email"):
            try:
                from docusign_helper import send_contract
                result = send_contract(
                    owner_email=data["owner_email"],
                    owner_name=data.get("owner_name","") or data["restaurant_name"],
                    restaurant_name=data["restaurant_name"],
                    module_count=mods,
                    modules_list=modules_list,
                )
                envelope_id = result.get("envelope_id")
                update_restaurant(rid, {
                    "contract_status": "sent",
                    "docusign_envelope_id": envelope_id,
                })
                print(f"Contract sent via DocuSign to {data['owner_email']}, envelope: {envelope_id}")
                try:
                    log_email(rid, "contract", data["owner_email"], f"Service Agreement — {data['restaurant_name']}")
                except Exception: pass
            except Exception as e:
                print(f"DocuSign contract failed: {e}")
                import traceback; traceback.print_exc()

        # Steps 2 & 3 (payment + welcome emails) fire automatically
        # when the client signs the contract via the DocuSign webhook

        docusign_skipped = envelope_id is None and mods > 0
        return jsonify(ok=True, restaurant_id=rid, envelope_id=envelope_id, docusign_skipped=docusign_skipped)
    except Exception as e:
        import traceback; traceback.print_exc()
        return jsonify(ok=False, error=_safe_err(e))

def _audit_admin_action(current_user, action, restaurant_id=None, target=None, before=None, after=None,
                        result="ok", summary=None):
    """One admin_events row for an admin action, with who, on what, and the
    before and after. admin_events.record never raises. (B2's richer
    record_admin_action takes these same fields; the integration wave can
    route this through it.)"""
    try:
        import admin_events
        admin_events.record("admin", action, restaurant_id=restaurant_id,
                            summary=summary or f"{current_user.get('username')} {action.replace('_', ' ')}",
                            payload={"actor_id": current_user.get("id"), "actor": current_user.get("username"),
                                     "target": target, "before": before, "after": after, "result": result,
                                     "ip": request.remote_addr})
    except Exception:
        pass


def _login_row(user_id):
    conn = get_conn()
    try:
        row = conn.execute("SELECT id, restaurant_id, username, email, role, is_admin, is_active FROM users WHERE id=?",
                           (user_id,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


@admin_bp.route("/admin/deactivate-client/<int:user_id>", methods=["POST"])
@admin_required
@recent_auth_required()
def deactivate_client(user_id, current_user):
    """Switch a login off and end every way it was signed in: its sessions
    (web and phone) and its remembered devices. It used to set is_active=0
    and nothing else, so reactivating brought every unexpired session back —
    an iOS token for up to 30 days (SECURITY-13) — while the console said
    "signed out everywhere". Never an admin row. Step-up: it revokes
    sessions (owner decision 4)."""
    import auth as _auth_d
    row = _login_row(user_id)
    if not row or row["is_admin"]:
        return jsonify(ok=False, error="That login can't be deactivated here."), 404
    ended = _auth_d.end_login_access(user_id)
    conn = get_conn()
    conn.execute("UPDATE users SET is_active=0 WHERE id=? AND is_admin=0", (user_id,))
    conn.commit(); conn.close()
    _audit_admin_action(current_user, "login_deactivated", restaurant_id=row["restaurant_id"],
                        target={"user_id": user_id, "username": row["username"]},
                        before={"is_active": bool(row["is_active"])}, after={"is_active": False},
                        summary=f"{current_user.get('username')} deactivated {row['username']} "
                                f"({ended['sessions']} session(s), {ended['devices']} remembered device(s) ended)")
    return jsonify(ok=True, sessions_ended=ended["sessions"], devices_forgotten=ended["devices"])

@admin_bp.route("/admin/reactivate-client/<int:user_id>", methods=["POST"])
@admin_required
def reactivate_client(user_id, current_user):
    """Switch a login back on. Never an admin row (the deactivate twin
    always refused one; this did not — SECURITY #93). Any session left from
    before is ended rather than revived, so the login signs in fresh. The
    welcome-back email goes to the reactivated login itself — it went to
    the restaurant's owner whoever was reactivated — and only to an account
    holder whose subscription allows access, since it says everything is
    running again."""
    import auth as _auth_r
    row = _login_row(user_id)
    if not row or row["is_admin"]:
        return jsonify(ok=False, error="That login can't be reactivated here."), 404
    _auth_r.end_login_access(user_id)
    conn = get_conn()
    conn.execute("UPDATE users SET is_active=1 WHERE id=? AND is_admin=0", (user_id,))
    conn.commit()
    conn.close()
    _audit_admin_action(current_user, "login_reactivated", restaurant_id=row["restaurant_id"],
                        target={"user_id": user_id, "username": row["username"]},
                        before={"is_active": bool(row["is_active"])}, after={"is_active": True},
                        summary=f"{current_user.get('username')} reactivated {row['username']}")
    emailed, note = False, None
    try:
        from permissions import is_principal
        from models import subscription_allows_access
        import scheduler as _sched_r
        to_email = (row.get("email") or "").strip()
        if not is_principal(row) or not to_email or to_email.endswith("@staff.invalid"):
            note = "no email: not an account holder's login"
        elif not subscription_allows_access(row["restaurant_id"]):
            note = "no email: the subscription does not allow access"
        elif not _sched_r.scheduling_allowed():
            note = "no email: this backend does not send (local)"
        else:
            restaurant = get_restaurant(row["restaurant_id"])
            if restaurant:
                from emails import send_reactivation_email
                send_reactivation_email(to_email=to_email, restaurant_name=restaurant.name,
                                        owner_name=restaurant.owner_name)
                emailed = True
    except Exception as e:
        note = "no email: the send failed"
        print(f"Reactivation email failed: {e}")
    return jsonify(ok=True, emailed=emailed, email_note=note)

@admin_bp.route("/admin/api/set-user-role", methods=["POST"])
@admin_required
@recent_auth_required()
def set_user_role_route(current_user):
    """Change a login's role from the console. It wrote users.role only,
    and the session reads the membership, so the change never took effect;
    it refused "member" (Teammate) although the console offers it; and it
    would rewrite an admin's row (SECURITY #60, #93). One body now —
    auth.admin_set_role — writes users.role and the membership together
    with the owner path's last-owner guard, refuses admin, support and staff
    PIN rows, and the change is audited with before and after. Step-up:
    a role change (owner decision 4)."""
    data = request.get_json(silent=True) or {}
    try:
        user_id = int(data.get("user_id") or 0)
    except (TypeError, ValueError):
        return jsonify(ok=False, error="Invalid user_id"), 400
    if not user_id:
        return jsonify(ok=False, error="Missing user_id"), 400
    import auth as _auth_sr
    role = (data.get("role") or "").strip().lower()
    try:
        out = _auth_sr.admin_set_role(user_id, role, acting_user_id=current_user.get("id"))
    except _auth_sr.TeamAccessError as e:
        return jsonify(ok=False, error=e.message), 400
    _audit_admin_action(current_user, "login_role_changed", restaurant_id=out["restaurant_id"],
                        target={"user_id": user_id, "username": out["username"]},
                        before={"role": out["before"]}, after={"role": out["after"]},
                        summary=f"{current_user.get('username')} changed {out['username']}'s role "
                                f"from {out['before']} to {out['after']}")
    return jsonify(ok=True, role=out["after"], previous_role=out["before"])


@admin_bp.route("/admin/client-data/<int:restaurant_id>")
@admin_required
def client_data_page(restaurant_id, current_user):
    from models import get_client_data, get_staff_notes
    restaurant = get_restaurant(restaurant_id)
    if not restaurant:
        return "Restaurant not found", 404
    data        = get_client_data(restaurant_id) or {}
    staff_notes = get_staff_notes(restaurant_id)
    return render_template('client_data.html',
        current_user=current_user,
        restaurant=restaurant,
        data=data,
        staff_notes=staff_notes)

@admin_bp.route("/admin/staff-notes/<int:restaurant_id>", methods=["POST"])
@admin_required
def save_staff_note_route(restaurant_id, current_user):
    from models import save_staff_note
    name  = request.form.get("employee_name","").strip()
    notes = request.form.get("notes","").strip()
    if not name or not notes:
        return jsonify(ok=False, error="Name and notes required")
    save_staff_note(restaurant_id, name, notes)
    return jsonify(ok=True)

@admin_bp.route("/admin/staff-notes/<int:note_id>/delete", methods=["POST"])
@admin_required
def delete_staff_note_route(note_id, current_user):
    from models import delete_staff_note
    delete_staff_note(note_id)
    return jsonify(ok=True)

@admin_bp.route("/admin/seed-review-account", methods=["POST"])
@admin_required
def seed_review_account_route(current_user):
    """Create (or refresh) the App Store review account, from inside production.

    scripts/seed_review_account.py does the same thing, but running it needs a
    shell on the container that holds the volume — the database path only
    exists there, so running it on a laptop either fails or quietly seeds a
    local file that Apple will never see. This is the same code path, one
    click, in the process that already has the right database.

    The reviewer's EXISTING password is kept by default (--keep-password):
    Apple signs in with the one already in App Store Connect, and rotating
    it ended the reviewer's sessions mid-review with nothing asking first
    (#127). Rotation is explicit: {"rotate_password": true, "confirm":
    "ROTATE"}. A brand-new account, or a rotation, has a new password —
    returned ONCE in the job's result (password_once) and scrubbed from the
    stored result on that first read.

    Runs on the admin job pool (up to 120 s of subprocess), not the request
    thread (#153): the answer is {job_id}; poll /admin/api/admin-jobs/<id>.
    """
    import os as _os
    if not current_user.get("is_admin"):
        return jsonify(ok=False, error="Support accounts are read-only."), 403
    script = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)),
                           "scripts", "seed_review_account.py")
    if not _os.path.exists(script):
        return jsonify(ok=False, error="seed_review_account.py is not in this deployment"), 500
    data = request.get_json(silent=True) or {}
    rotate = bool(data.get("rotate_password"))
    if rotate and str(data.get("confirm") or "").strip().upper() != "ROTATE":
        return jsonify(ok=False, confirm_required=True,
                       error="Rotating the App Store reviewer's password signs the reviewer out and stops the "
                             "password in App Store Connect working. Send confirm \"ROTATE\" to do it."), 400
    actor = dict(current_user)
    job_id, joined = _start_admin_job("admin_review_account", 0,
                                      lambda: _seed_review_account_job(script, rotate, actor))
    if not job_id:
        return jsonify(ok=False, error="Too many admin jobs are running — try again in a minute."), 429
    import admin_events
    admin_events.record_admin_action(current_user, "review_account.seed", target="account:app_review",
                                     after={"rotate_password": rotate, "job_id": job_id, "joined": joined})
    return jsonify(ok=True, job_id=job_id, joined=joined, rotate_password=rotate,
                   message=("Seeding the App Store review account (up to two minutes) — "
                            + ("the password will be rotated." if rotate else "its password is kept.")))


def _seed_review_account_job(script, rotate, actor):
    """The subprocess half of seed_review_account_route, on the admin pool.
    The password line is lifted out of the printed output into
    password_once, which the job-status read scrubs after showing it."""
    import subprocess
    import sys
    import os as _os
    import admin_events
    args = [sys.executable, script] + ([] if rotate else ["--keep-password"])
    try:
        # Inherit the environment so the script resolves the same DB_PATH
        # this process is using, volume mount included.
        out = subprocess.run(args, capture_output=True, text=True, timeout=120, env=dict(_os.environ))
    except subprocess.TimeoutExpired:
        admin_events.record_admin_action(actor, "review_account.seeded", target="account:app_review",
                                         after={"rotate_password": rotate}, result="error",
                                         summary="Review account seed timed out")
        return {"ok": False, "error": "Seeding timed out after two minutes."}
    if out.returncode != 0:
        admin_events.record_admin_action(actor, "review_account.seeded", target="account:app_review",
                                         after={"rotate_password": rotate}, result="error",
                                         summary="Review account seed failed")
        return {"ok": False, "error": (out.stderr or out.stdout or "Seeding failed")[-1500:]}
    password, lines = None, []
    for line in (out.stdout or "").splitlines():
        stripped = line.strip()
        if stripped.startswith("Password:"):
            value = stripped[len("Password:"):].strip()
            if value and not value.startswith("("):
                password = value
                lines.append(line.replace(value, "(shown once, below)"))
                continue
        lines.append(line)
    created = "Created restaurant" in (out.stdout or "")
    admin_events.record_admin_action(actor, "review_account.seeded", target="account:app_review",
                                     after={"rotate_password": rotate, "created": created,
                                            "password_changed": bool(password)})
    result = {"ok": True, "output": "\n".join(lines), "created": created, "rotated": bool(rotate and password)}
    if password:
        result["password_once"] = password
        result["_scrub"] = ["password_once"]
    return result


@admin_bp.route("/admin/inventory/import-csv/<int:restaurant_id>", methods=["POST"])
@admin_required
def import_csv_to_ingredients_route(restaurant_id, current_user):
    import inventory_ledger
    return jsonify(ok=True, **inventory_ledger.import_csv_to_ingredients(restaurant_id))


@admin_bp.route("/admin/inventory/ingredients/<int:restaurant_id>", methods=["GET"])
@admin_required
def list_ingredients_route(restaurant_id, current_user):
    import inventory_ledger
    return jsonify(ok=True, ingredients=inventory_ledger.list_ingredients(restaurant_id))


@admin_bp.route("/admin/inventory/ingredients/<int:restaurant_id>", methods=["POST"])
@admin_required
def create_ingredient_route(restaurant_id, current_user):
    import inventory_ledger
    data = request.get_json() or {}
    name = (data.get("name") or "").strip()
    if not name:
        return jsonify(ok=False, error="Ingredient name required")
    ingredient_id = inventory_ledger.create_ingredient(
        restaurant_id, name=name, category=data.get("category", ""), unit=data.get("unit", ""),
        par_level=float(data.get("par_level") or 0), unit_cost=float(data.get("unit_cost") or 0),
        case_size=float(data.get("case_size") or 1.0), current_stock=float(data.get("current_stock") or 0)
    )
    return jsonify(ok=True, id=ingredient_id)


@admin_bp.route("/admin/inventory/ingredients/<int:restaurant_id>/<int:ingredient_id>", methods=["POST"])
@admin_required
def update_ingredient_route(restaurant_id, ingredient_id, current_user):
    import inventory_ledger
    data = request.get_json() or {}
    fields = {}
    for key in ("name", "category", "unit"):
        if key in data:
            fields[key] = data[key]
    import math
    for key in ("par_level", "unit_cost", "case_size", "avg_daily_usage", "waste_last_week"):
        if key in data and data[key] not in (None, ""):
            try:
                fields[key] = float(data[key])
            except (TypeError, ValueError):
                fields[key] = float("nan")
            # NaN is stored as NULL and broke the restaurant's analysis (MOD-FC-6).
            if not math.isfinite(fields[key]) or fields[key] < 0:
                return jsonify(ok=False, error=f"{key.replace('_', ' ')} must be a number of 0 or more."), 400
    if not inventory_ledger.update_ingredient(restaurant_id, ingredient_id, **fields):
        return jsonify(ok=False, error="That ingredient isn't this restaurant's, or nothing changed."), 404
    return jsonify(ok=True)


@admin_bp.route("/admin/inventory/ingredients/<int:restaurant_id>/<int:ingredient_id>/delete", methods=["POST"])
@admin_required
def delete_ingredient_route(restaurant_id, ingredient_id, current_user):
    import inventory_ledger
    if not inventory_ledger.deactivate_ingredient(restaurant_id, ingredient_id):
        return jsonify(ok=False, error="That ingredient isn't this restaurant's."), 404
    return jsonify(ok=True)


@admin_bp.route("/admin/inventory/discover-menu-items/<int:restaurant_id>", methods=["POST"])
@admin_required
def discover_menu_items_route(restaurant_id, current_user):
    import inventory_ledger
    try:
        return jsonify(ok=True, **inventory_ledger.discover_menu_items(restaurant_id))
    except Exception as e:
        return jsonify(ok=False, error=_safe_err(e))


@admin_bp.route("/admin/inventory/menu-items/<int:restaurant_id>", methods=["POST"])
@admin_required
def create_menu_item_route(restaurant_id, current_user):
    """Manual add — the only path for a restaurant with no Toast connection,
    or a dish too new to have shown up in discover_menu_items yet."""
    import inventory_ledger
    data = request.get_json() or {}
    name = (data.get("name") or "").strip()
    if not name:
        return jsonify(ok=False, error="Menu item name required")
    menu_item_id = inventory_ledger.create_menu_item(restaurant_id, name)
    return jsonify(ok=True, id=menu_item_id)


@admin_bp.route("/admin/inventory/recipes/<int:restaurant_id>", methods=["GET"])
@admin_required
def list_recipes_route(restaurant_id, current_user):
    import inventory_ledger
    return jsonify(ok=True, menu_items=inventory_ledger.list_menu_items_with_recipes(restaurant_id),
                   ingredients=inventory_ledger.list_ingredients(restaurant_id),
                   priority_ingredients=inventory_ledger.priority_ingredients(restaurant_id))


def _restaurant_of_menu_item(menu_item_id):
    """The menu item's own restaurant. These two routes are addressed by
    menu_item_id alone, so the tenant has to be derived from the row rather
    than trusted from anywhere else."""
    conn = get_conn()
    try:
        row = conn.execute("SELECT restaurant_id FROM menu_items WHERE id=?", (menu_item_id,)).fetchone()
        return row["restaurant_id"] if row else None
    finally:
        conn.close()


@admin_bp.route("/admin/inventory/recipes/<int:menu_item_id>", methods=["POST"])
@admin_required
def add_recipe_ingredient_route(menu_item_id, current_user):
    import inventory_ledger
    data = request.get_json() or {}
    ingredient_id = data.get("ingredient_id")
    qty_per_unit = data.get("qty_per_unit")
    if not ingredient_id or not qty_per_unit:
        return jsonify(ok=False, error="ingredient_id and qty_per_unit required")
    rid = _restaurant_of_menu_item(menu_item_id)
    if rid is None:
        return jsonify(ok=False, error="No such menu item."), 404
    # ingredient_id is caller-supplied and must belong to the same restaurant
    # as the dish — a recipe that reaches across locations silently costs one
    # location's plate from another's prices.
    row_id = inventory_ledger.add_recipe_ingredient(rid, menu_item_id, int(ingredient_id), float(qty_per_unit))
    if not row_id:
        return jsonify(ok=False, error="That ingredient belongs to a different restaurant."), 400
    return jsonify(ok=True, id=row_id)


@admin_bp.route("/admin/inventory/recipes/<int:menu_item_id>/<int:recipe_ingredient_id>/delete", methods=["POST"])
@admin_required
def delete_recipe_ingredient_route(menu_item_id, recipe_ingredient_id, current_user):
    import inventory_ledger
    rid = _restaurant_of_menu_item(menu_item_id)
    if rid is None:
        return jsonify(ok=False, error="No such menu item."), 404
    if not inventory_ledger.delete_recipe_ingredient(rid, recipe_ingredient_id):
        return jsonify(ok=False, error="That recipe row isn't this restaurant's."), 404
    return jsonify(ok=True)


@admin_bp.route("/admin/inventory/recount/<int:restaurant_id>/<int:ingredient_id>", methods=["POST"])
@admin_required
def record_recount_route(restaurant_id, ingredient_id, current_user):
    import inventory_ledger
    data = request.get_json() or {}
    if "counted_qty" not in data:
        return jsonify(ok=False, error="counted_qty required")
    try:
        result = inventory_ledger.record_recount(
            restaurant_id, ingredient_id, float(data["counted_qty"]),
            source="admin", note=data.get("note")
        )
    except (TypeError, ValueError):
        return jsonify(ok=False, error="counted_qty must be a number of 0 or more"), 400
    if result.get("ok") is False:
        return jsonify(**result), 404
    return jsonify(ok=True, **result)


@admin_bp.route("/admin/inventory/receiving/<int:restaurant_id>/<int:ingredient_id>", methods=["POST"])
@admin_required
def record_receiving_route(restaurant_id, ingredient_id, current_user):
    import inventory_ledger
    data = request.get_json() or {}
    if "qty" not in data:
        return jsonify(ok=False, error="qty required")
    try:
        _qty = float(data["qty"])
    except (TypeError, ValueError):
        _qty = float("nan")
    if not (_qty > 0 and _qty != float("inf")):
        # A delivery is a positive quantity; a correction is a recount (MOD-FC-12).
        return jsonify(ok=False, error="qty must be more than 0 — use a recount to correct stock"), 400
    event_id = inventory_ledger.record_receiving(
        restaurant_id, ingredient_id, float(data["qty"]),
        source="admin", note=data.get("note")
    )
    if not event_id:
        return jsonify(ok=False, error="That ingredient isn't this restaurant's."), 404
    return jsonify(ok=True, id=event_id)


@admin_bp.route("/admin/inventory/resync-depletion/<int:restaurant_id>", methods=["POST"])
@admin_required
def resync_depletion_route(restaurant_id, current_user):
    import inventory_ledger
    from datetime import date as _date, timedelta as _td
    data = request.get_json() or {}
    business_date_str = data.get("business_date")
    business_date = _date.fromisoformat(business_date_str) if business_date_str else (_date.today() - _td(days=1))
    try:
        return jsonify(ok=True, **inventory_ledger.compute_daily_depletion(restaurant_id, business_date))
    except Exception as e:
        return jsonify(ok=False, error=_safe_err(e))


# Internal admin only. These were @login_required with the restaurant taken
# from the URL, so any client login could read, overwrite or delete another
# restaurant's staff availability. The owner's roster (web and phone) uses
# the session-scoped /api/labor/availability and /mobile/api/labor/availability.
@admin_bp.route("/admin/staff-availability/<int:restaurant_id>", methods=["GET"])
@admin_required
def get_staff_availability_route(restaurant_id, current_user):
    from models import get_staff_availability
    return jsonify(ok=True, availability=get_staff_availability(restaurant_id))

@admin_bp.route("/admin/staff-availability/<int:restaurant_id>", methods=["POST"])
@admin_required
def save_staff_availability_route(restaurant_id, current_user):
    from models import save_staff_availability
    data = request.get_json() or {}
    name = (data.get("employee_name") or "").strip()
    if not name:
        return jsonify(ok=False, error="employee_name required"), 400
    save_staff_availability(
        restaurant_id, name,
        available_days=data.get("available_days") or [],
        unavailable_days=data.get("unavailable_days") or [],
        notes=(data.get("notes") or "").strip() or None
    )
    return jsonify(ok=True)

@admin_bp.route("/admin/staff-availability/<int:restaurant_id>/delete", methods=["POST"])
@admin_required
def delete_staff_availability_route(restaurant_id, current_user):
    from models import delete_staff_availability
    data = request.get_json() or {}
    name = (data.get("employee_name") or "").strip()
    if name:
        delete_staff_availability(restaurant_id, name)
    return jsonify(ok=True)

@admin_bp.route("/admin/alert-contacts/<int:restaurant_id>", methods=["GET"])
@admin_required
def get_alert_contacts_route(restaurant_id, current_user):
    from notify import get_alert_contacts
    return jsonify(ok=True, contacts=get_alert_contacts(restaurant_id))


@admin_bp.route("/admin/alert-contacts/<int:restaurant_id>", methods=["POST"])
@admin_required
def add_alert_contact_route(restaurant_id, current_user):
    """Admin-added contacts never get sms_consent=True — an operator typing in
    someone else's number isn't that person consenting. They still receive
    email alerts; only the client's own self-service Alert Settings flow
    (client_api.save_alert_settings, gated by the consent checkbox) can
    enable SMS for a contact."""
    from notify import add_alert_contact
    data = request.get_json() or {}
    name  = (data.get("name") or "").strip()
    phone = (data.get("phone") or "").strip()
    if not phone:
        return jsonify(ok=False, error="Phone number required")
    contact_id = add_alert_contact(restaurant_id, name, phone, sms_consent=False)
    return jsonify(ok=True, id=contact_id, name=name, phone=phone)


@admin_bp.route("/admin/alert-contacts/delete/<int:contact_id>", methods=["POST"])
@admin_required
def delete_alert_contact_route(contact_id, current_user):
    from notify import delete_alert_contact
    delete_alert_contact(contact_id)
    return jsonify(ok=True)


@admin_bp.route("/admin/alert-contacts/test/<int:restaurant_id>", methods=["POST"])
@admin_required
def test_alert_sms_route(restaurant_id, current_user):
    from notify import send_test_sms
    result = send_test_sms(restaurant_id)
    return jsonify(**result)


@admin_bp.route("/admin/upload-data/<int:restaurant_id>", methods=["POST"])
@admin_required
def upload_data(restaurant_id, current_user):
    from models import save_client_data
    data_type = request.form.get("data_type")  # "shifts" or "inventory"
    source     = request.form.get("source", "upload")

    if source == "upload":
        f = request.files.get("csv_file")
        if not f:
            return jsonify(ok=False, error="No file uploaded")
        csv_content = f.read().decode("utf-8")
    else:
        csv_content = request.form.get("csv_content", "")

    if not csv_content.strip():
        return jsonify(ok=False, error="No data provided")

    # Validate it parses correctly
    import io, csv as _csv
    try:
        rows = list(_csv.DictReader(io.StringIO(csv_content)))
        if not rows:
            return jsonify(ok=False, error="CSV appears empty")
    except Exception as e:
        return jsonify(ok=False, error=f"Could not parse CSV: {e}")

    save_client_data(restaurant_id, data_type, csv_content, source)
    return jsonify(ok=True, rows=len(rows))

@admin_bp.route("/admin/client-settings/<int:restaurant_id>")
@admin_required
def client_settings_page(restaurant_id, current_user):
    restaurant = get_restaurant(restaurant_id)
    if not restaurant:
        return "Restaurant not found", 404
    from models import get_client_data
    client_data = get_client_data(restaurant_id) or {}
    from models import get_staff_notes
    staff_notes = get_staff_notes(restaurant_id)
    from notify import get_alert_contacts
    alert_contacts = get_alert_contacts(restaurant_id)
    # The last physical count (ingredients.last_recount_at, the column
    # ordering reads) — restaurants.inventory_updated_at is never written, so
    # this field read "Never" or a stale date forever (CA3 F9).
    try:
        import ordering
        count_freshness = ordering.count_freshness(restaurant_id)
    except Exception:
        count_freshness = {}
    # The benchmark hints by this restaurant's type, each with its source
    # and year, or none (benchmark_registry, NS4 L2): the page said
    # "Industry average $22–28/hr" and "typically 28–35%" with no source.
    # Through the Benchmark Engine's industry kind (Benchmarking re-audit
    # #5, R1-17): nothing for a type Cavnar only guessed, and a figure
    # measured differently from Cavnar's is marked context only.
    import intelligence as _intel_hints
    bench_hints = {}
    for key, metric in (("labor", "labor_pct_28d"), ("food", "food_cost_pct_28d")):
        got = _intel_hints.industry_read(restaurant, metric)
        bench_hints[key] = ((got["comparison"].get("line") or "")
                            + (f" {got['definition_note']}" if got.get("definition_note") else "")) if got else None
    return render_template('client_settings.html',
        current_user=current_user,
        restaurant=restaurant,
        client_data=client_data,
        staff_notes=staff_notes,
        alert_contacts=alert_contacts,
        count_freshness=count_freshness,
        bench_hints=bench_hints)

@admin_bp.route("/admin/client-settings/<int:restaurant_id>", methods=["POST"])
@admin_required
def save_client_settings(restaurant_id, current_user):
    from models import update_restaurant, get_restaurant as _gr_cs
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify(ok=False, error="Send the settings as a JSON object."), 400
    current = _gr_cs(restaurant_id)
    if not current:
        return jsonify(ok=False, error="Restaurant not found"), 404

    def _given_or_current(key):
        """The value this save will leave in place: the payload's, or the
        stored one when the payload does not mention the field."""
        if key in data:
            return (data.get(key) or "").strip()
        return (getattr(current, key, "") or "").strip()

    def _valid_tz(name):
        """Only store real IANA zone names — a typo here would silently skew
        every date this restaurant sees."""
        name = (name or "").strip()
        if not name:
            return "America/Chicago"
        try:
            from zoneinfo import ZoneInfo
            ZoneInfo(name)
            return name
        except Exception:
            return "America/Chicago"

    # The labor target drives every schedule budget. Any number used to be
    # stored — negative, 500% — and a 0 silently became 30% (SCHED-36).
    _lt_raw = data.get("labor_target_pct")
    if _lt_raw in (None, ""):
        _labor_target = 30.0
    else:
        try:
            _labor_target = float(_lt_raw)
        except (TypeError, ValueError):
            _labor_target = None
        if _labor_target is None or _labor_target != _labor_target or not 5.0 <= _labor_target <= 60.0:
            return jsonify(ok=False, error="Labor target must be a percentage between 5 and 60.")

    # "Never cut a role below N people" (schedule_rules.cut_floor): the cut
    # floor for a role with no floor of its own, 1..CUT_FLOOR_MAX. Refused,
    # not clamped, so a typo is seen rather than silently saved as 10.
    _cut_floor = None
    if "cut_floor_default" in data:
        import schedule_rules as _sr_cut
        _cf_raw = data.get("cut_floor_default")
        _cut_floor = _sr_cut.clean_cut_floor_default(_cf_raw)
        try:
            _cf_ok = _cut_floor is not None and 1 <= float(_cf_raw) <= _sr_cut.CUT_FLOOR_MAX
        except (TypeError, ValueError):
            _cf_ok = False
        if not _cf_ok:
            return jsonify(ok=False, error=f"Never cut below must be a whole number of people from 1 to "
                                           f"{_sr_cut.CUT_FLOOR_MAX}.")

    try:
        tier = data.get("service_tier","trial")
        # Same tenancy guard as create-client: a group name in use by another
        # owner would silently merge two clients into one tenant.
        place_clash = place_id_conflict((data.get("google_place_id") or "").strip(),
                                        exclude_id=restaurant_id) if "google_place_id" in data else None
        # A demo row deliberately mirrors a real listing (place_id_conflict
        # exempts demos): the stored flag counts when the form doesn't send
        # one - renaming the demo Simple EJ's was refused because Erik's live
        # account holds the same listing (owner, 9/28/26).
        _is_demo = data.get("is_demo") if "is_demo" in data else getattr(current, "is_demo", 0)
        if place_clash and not int(_is_demo or 0):
            return jsonify(ok=False, error=(
                f"That Google listing is already connected to {place_clash}. Two live "
                f"restaurants on one listing both pull the same reviews and only one of "
                f"them can own any given review."
            ))

        conflict = location_group_conflict(
            _given_or_current("location_group"),
            _given_or_current("owner_email"),
            exclude_id=restaurant_id,
        ) if ("location_group" in data or "owner_email" in data) else None
        if conflict:
            return jsonify(ok=False, error=(
                f"Location group \u201c{_given_or_current('location_group')}\u201d already belongs to "
                f"{conflict}. Pick a different group name — locations in a group share data and billing."
            ))
        # Every field the form can carry, parsed and validated as before —
        # then only the ones this payload actually sent are written. This used
        # to write every key as data.get(key, default), so any save that left
        # a field out reset it: the settings page never sends the alert_* or
        # urgent_* switches, so every save silently turned all of a client's
        # alerts off, and a one-field payload also reset billing_status to
        # 'trial', the modules and owner_email (SEC-30).
        fields = {
            "name":            data.get("name","").strip(),
            "owner_email":     data.get("owner_email","").strip(),
            "google_place_id": data.get("google_place_id","").strip() or None,
            "yelp_business_id":data.get("yelp_business_id","").strip() or None,
            "voice_notes":     sanitize(data.get("voice_notes","")),
            "neighborhood":    data.get("neighborhood","").strip() or None,
            "vibe":            sanitize(data.get("vibe","")),
            "known_for":       sanitize(data.get("known_for","")),
            "timezone":        _valid_tz(data.get("timezone","")),
            "sign_off_name":   data.get("sign_off_name","").strip() or None,
            "never_say":       sanitize(data.get("never_say","")),
            "menu_notes":      sanitize(data.get("menu_notes",""), max_len=2000),
            "menu_url":        sanitize(data.get("menu_url","")),
            "skip_holidays":   sanitize(data.get("skip_holidays","")),
            "custom_competitors": sanitize(data.get("custom_competitors","")),
            "hourly_rate":     float(data.get("hourly_rate") or 26.0),
            "labor_target_pct": _labor_target,
            "pos_system":      data.get("pos_system","").strip() or None,
            "module_reviews":  int(data.get("module_reviews", 1)),
            "module_labor":    int(data.get("module_labor", 0)),
            "module_inventory":int(data.get("module_inventory", 0)),
            "module_marketing":int(data.get("module_marketing", 0)),
            "owner_name":      sanitize(data.get("owner_name","")),
            "owner_phone":     data.get("owner_phone","").strip() or None,
            "location_group":        data.get("location_group","").strip() or None,
            "location_name":         data.get("location_name","").strip() or None,
            "inventory_frequency":   data.get("inventory_frequency","weekly"),
            "delivery_days":         data.get("delivery_days","").strip() or None,
            "inventory_notes":       sanitize(data.get("inventory_notes","")),
            "hours_notes":           sanitize(data.get("hours_notes",""), max_len=2000),
            "role_rates_json":       data.get("role_rates_json","") or None,
            "close_times_json":      data.get("close_times_json","") or None,
            "role_close_buffer_json": data.get("role_close_buffer_json","") or None,
            "section_count":         int(data["section_count"]) if data.get("section_count") else None,
            "daypart_split":         data.get("daypart_split","").strip() or None,
            "delivery_pct":          int(data["delivery_pct"]) if data.get("delivery_pct") is not None and str(data.get("delivery_pct","")) != "" else None,
            "role_minimums_json":    data.get("role_minimums_json","") or None,
            "cut_floor_default":     _cut_floor,
            "sched_notes":           sanitize(data.get("sched_notes",""), max_len=2000),
            "monthly_revenue_target": float(data.get("monthly_revenue_target") or 0),
            # Sent only when typed by the week: update_restaurant stores the
            # monthly it implies (models.monthly_from_weekly) over the above.
            "weekly_revenue_target":  float(data.get("weekly_revenue_target") or 0),
            "food_cost_target":      float(data.get("food_cost_target", 30) or 30),
            "waste_target_pct":      float(data["waste_target_pct"]) if data.get("waste_target_pct") not in (None, "") else None,
            "digest_day":      data.get("digest_day","monday"),
            "digest_enabled":  int(data.get("digest_enabled",1)),
            "reviews_live":    int(bool(data.get("reviews_live"))),
            "billing_status":  data.get("billing_status","trial"),
            "internal_notes":  sanitize(data.get("internal_notes","")),
            "alert_1star":        int(bool(data.get("alert_1star"))),
            "alert_2star":        int(bool(data.get("alert_2star"))),
            "alert_health":       int(bool(data.get("alert_health"))),
            "alert_neg_spike":    int(bool(data.get("alert_neg_spike"))),
            "alert_negative_trend": int(bool(data.get("alert_negative_trend"))),
            "alert_no_response":  int(bool(data.get("alert_no_response"))),
            "urgent_via_email":   int(bool(data.get("urgent_via_email"))),
            "urgent_via_sms":     int(bool(data.get("urgent_via_sms"))),
            "alert_5star":             int(bool(data.get("alert_5star"))),
            "alert_rating_threshold":  int(bool(data.get("alert_rating_threshold"))),
            "alert_rating_floor":      float(data.get("alert_rating_floor") or 4.0),
            "alert_labor_over":        int(bool(data.get("alert_labor_over"))),
        }
        _upd = {k: v for k, v in fields.items() if k in data}
        # A target or the blended rate the admin actually edited is the
        # owner's, even when it is typed back as the default 30% or $26 —
        # the form names the fields it saw touched (re-audit #45, R2-21).
        # An untouched field re-sent with the rest confirms nothing.
        from models import TARGET_SOURCE_FIELDS as _TSF
        _touched = data.get("touched") if isinstance(data.get("touched"), list) else []
        for _tf in _touched:
            if _tf in _TSF and _tf in _upd:
                _upd[_TSF[_tf]] = "set"
        update_restaurant(restaurant_id, _upd)
        from models import log_event
        log_event(restaurant_id, "admin_settings_update", {"by": current_user.get("username", "admin")})
        return jsonify(ok=True)
    except Exception as e:
        return jsonify(ok=False, error=_safe_err(e))

@admin_bp.before_request
def _audit_admin_write():
    """Every admin write is a row in admin_events with the actor, before it
    runs (security audit Z2). The body is admin_events.audit_admin_write,
    shared with status_bp's admin writes."""
    import admin_events
    return admin_events.audit_admin_write()


@admin_bp.route("/admin/freeze/<int:restaurant_id>", methods=["POST"])
@admin_required
def freeze_account(restaurant_id, current_user):
    """The takeover response: every session and trusted device for this
    restaurant's logins is revoked and the next sign-in is refused until
    the password is reset (security audit R1)."""
    if not current_user.get("is_admin"):
        return jsonify(ok=False, error="Support accounts are read-only."), 403
    import security
    data = request.get_json(silent=True) or {}
    n = security.freeze_restaurant(restaurant_id, actor=current_user, reason=data.get("reason"))
    try:
        import admin_events
        admin_events.record("admin", "account_frozen", restaurant_id=restaurant_id,
                            summary=f"{current_user.get('username')} froze {n} login(s): {(data.get('reason') or '')[:120]}")
    except Exception:
        pass
    return jsonify(ok=True, frozen=n)


@admin_bp.route("/admin/send-reset-link/<int:user_id>", methods=["POST"])
@admin_required
def send_reset_link(user_id, current_user):
    """Preferred over setting a password by hand: the owner chooses it, and
    nothing about it passes through Will or the database. Answers with what
    actually happened to the email — a 502 and a sentence when it did not
    go (#109); it used to say "sent" whatever deliver() returned."""
    if not current_user.get("is_admin"):
        return jsonify(ok=False, error="Support accounts are read-only."), 403
    payload, status = _send_reset_link_to(user_id, current_user)
    return jsonify(**payload), status


@admin_bp.route("/admin/reset-password/<int:user_id>", methods=["POST"])
@admin_required
def reset_password(user_id, current_user):
    """An admin reset is a reset LINK now (#86). This route set a password
    by hand, returned it in the JSON and could email it in plain text; the
    owner now picks their own password from a one-hour link, and nothing
    about it passes through the console. A `password` in the body is
    ignored — the answer says so."""
    if not current_user.get("is_admin"):
        return jsonify(ok=False, error="Support accounts are read-only."), 403
    data = request.get_json(silent=True) or {}
    payload, status = _send_reset_link_to(user_id, current_user)
    if payload.get("ok") and (data.get("password") or "").strip():
        payload["password_ignored"] = True
        payload["message"] = ("Admins no longer set passwords. " + payload.get("message", "")).strip()
    return jsonify(**payload), status


def _send_failure_reason(result):
    """A sentence-fragment for why deliver() did not send."""
    err = str(getattr(result, "error", "") or "")
    if err.startswith("recipient suppressed"):
        return "that address is on the suppression list after a bounce or complaint"
    if err.startswith("RESEND_API_KEY"):
        return "email isn't configured on this server"
    if err.startswith("flood guard"):
        return "too many of these went to that address in the last hour"
    code = getattr(result, "status_code", None)
    return f"the email provider refused it{f' ({code})' if code else ''}"


def _send_reset_link_to(user_id, current_user):
    """(payload, status): email one login a one-hour reset link. Shared by
    send-reset-link and both reset-password routes. Refused on a local
    backend (#9), for admin logins and for staff PIN identities (no real
    address), and reported truthfully either way."""
    blocked = _send_blocked()
    if blocked:
        return blocked
    from models import create_reset_token
    conn = get_conn()
    try:
        row = conn.execute("SELECT u.id, u.email, u.username, u.is_admin, u.is_active, u.restaurant_id "
                           "FROM users u WHERE u.id=?", (user_id,)).fetchone()
    finally:
        conn.close()
    if not row:
        return {"ok": False, "error": "No such login."}, 404
    if row["is_admin"]:
        return {"ok": False, "error": "Admin logins reset their own password from the sign-in page."}, 403
    email = (row["email"] or "").strip()
    if not email or email.lower().endswith("@staff.invalid"):
        return {"ok": False, "error": "That login has no email address (a staff PIN identity): reset its PIN "
                                      "from the owner's team screen instead."}, 409
    if not row["is_active"]:
        return {"ok": False, "error": "That login is inactive."}, 409
    token = create_reset_token(email)
    if not token:
        return {"ok": False, "error": "That login is inactive."}, 409
    import admin_events
    try:
        from emails import send_password_reset_email
        result = send_password_reset_email(email, f"{config.base_url()}/reset-password/{token}")
    except Exception as e:
        result = _emails.SendResult(False, error=_safe_err(e))
    ok = bool(getattr(result, "ok", result))
    admin_events.record_admin_action(current_user, "password.reset_link_sent", restaurant_id=row["restaurant_id"],
                                     target=f"user:{row['id']}",
                                     after={"to": email, "delivered": ok,
                                            "error": None if ok else str(getattr(result, "error", "") or "")[:200]},
                                     result="ok" if ok else "failed")
    if not ok:
        return {"ok": False, "error": f"The reset link didn't go out: {_send_failure_reason(result)}. "
                                      f"Their password is unchanged."}, 502
    return {"ok": True, "email": email, "sent_to": email, "username": row["username"],
            "message": f"Reset link sent to {email}. It works once, for an hour; their password is "
                       f"unchanged until they use it."}, 200

def _principal_login_id(restaurant_id, fallback_to_any=False):
    """The restaurant's own account-holder login: an active, non-admin
    'client' or 'owner' login whose home is this restaurant, oldest first.

    Both callers used to take `... WHERE restaurant_id=? AND is_admin=0
    LIMIT 1` with no ORDER BY, i.e. whichever row SQLite returned first —
    often a staff PIN identity (an @staff.invalid users row) or a manager
    created before the owner's login, so support impersonated, or reset the
    password of, the wrong person (SEC-28). fallback_to_any lets view-as
    still open a restaurant that has no principal login, on its oldest
    non-staff console login."""
    conn = get_conn()
    try:
        row = conn.execute(
            "SELECT id FROM users WHERE restaurant_id=? AND is_admin=0 AND is_active=1 "
            "AND COALESCE(NULLIF(role,''),'client') IN ('client','owner') "
            "AND email NOT LIKE '%@staff.invalid' ORDER BY id LIMIT 1",
            (restaurant_id,)).fetchone()
        if not row and fallback_to_any:
            row = conn.execute(
                "SELECT id FROM users WHERE restaurant_id=? AND is_admin=0 AND is_active=1 "
                "AND COALESCE(role,'') NOT IN ('employee','support') "
                "AND email NOT LIKE '%@staff.invalid' ORDER BY id LIMIT 1",
                (restaurant_id,)).fetchone()
        return row["id"] if row else None
    finally:
        conn.close()


@admin_bp.route("/admin/reset-password-by-restaurant/<int:restaurant_id>", methods=["POST"])
@admin_required
def reset_password_by_restaurant(restaurant_id, current_user):
    """The restaurant's principal login gets a reset link (reset_password)."""
    user_id = _principal_login_id(restaurant_id)
    if not user_id:
        return jsonify(ok=False, error="This restaurant has no owner login to reset."), 404
    # reset_password is itself admin_required, which resolves and passes
    # current_user; passing it here as well raised TypeError ("multiple
    # values for 'current_user'") on every call, so this route always 500'd.
    return reset_password(user_id)

@admin_bp.route("/api/review-count")
@login_required
def review_count_api(current_user):
    """Lightweight polling endpoint for new review detection."""
    from models import get_review_stats
    stats = get_review_stats(current_user["restaurant_id"])
    return jsonify(
        total=stats.get("total", 0),
        pending=stats.get("awaiting_approval", 0),
        urgent=stats.get("urgent", 0)
    )


@admin_bp.route("/api/log-activity", methods=["POST"])
@login_required
def log_activity_route(current_user):
    """The web tab ping behind restaurants.last_activity (the console's
    "last active"). An admin viewing as the client is not the client being
    active: an operator opening an inactive account to investigate used to
    clear its "No activity" issue (LIFECYCLE-9)."""
    if (current_user.get("device_type") or "") == "admin-view-as":
        return jsonify(ok=True, skipped="view_as")
    from models import log_activity
    data = request.get_json(silent=True) or {}
    log_activity(current_user["restaurant_id"], data.get("tab",""))
    return jsonify(ok=True)

@admin_bp.route("/admin/resend-contract/<int:restaurant_id>", methods=["POST"])
@admin_required
def resend_contract(restaurant_id, current_user):
    """Re-send the service agreement (#26, #78).

    A contract the client has not signed is re-sent as the SAME envelope,
    with DocuSign's reminders on — a resend used to mint a new envelope every
    time. A new envelope goes out only when there is none, when the last one
    was declined or voided, or when the terms changed (the module list).
    A SIGNED contract is never reset to 'sent': it is refused, unless the body
    says {"amendment": true} — then a new envelope goes out and the signed
    contract stays in force. Body: {amendment?, new_envelope?}."""
    if not current_user.get("is_admin"):
        return jsonify(ok=False, error="Support accounts are read-only."), 403
    restaurant = get_restaurant(restaurant_id)
    if not restaurant:
        return jsonify(ok=False, error="Restaurant not found"), 404
    data = request.get_json(silent=True) or {}
    module_names = []
    if restaurant.module_reviews:  module_names.append("Review Intelligence")
    if restaurant.module_labor:    module_names.append("Labor Optimizer")
    if restaurant.module_inventory: module_names.append("Food Cost Control")
    if restaurant.module_marketing: module_names.append("Marketing Autopilot")
    mods = len(module_names)
    modules_list = ", ".join(module_names)
    signed = (restaurant.contract_status or "").lower() == "signed"
    if signed and not data.get("amendment"):
        return jsonify(ok=False, signed=True, error=(
            "This contract is already signed, so it was not re-sent. If the terms changed, send an "
            "amendment — a new envelope; the signed contract stays in force.")), 409
    if mods == 0:
        return jsonify(ok=False, error="No modules are on for this client, so there is nothing to contract for."), 400
    import models as _mdl
    try:
        env = _latest_envelope(restaurant_id, restaurant.docusign_envelope_id)
        same_terms = env and (env.get("modules_list") is None or
                              (env.get("module_count") == mods and env.get("modules_list") == modules_list))
        if (env and not signed and not data.get("new_envelope") and same_terms
                and (env.get("status") or "sent") not in ("declined", "voided", "completed")):
            from docusign_helper import resend_envelope
            res = resend_envelope(env["envelope_id"], restaurant_id=restaurant_id)
            if res.get("ok"):
                conn = get_conn()
                try:
                    conn.execute("UPDATE docusign_envelopes SET resend_count=COALESCE(resend_count,0)+1, "
                                 "last_resent_at=datetime('now') WHERE envelope_id=?", (env["envelope_id"],))
                    conn.commit()
                finally:
                    conn.close()
                _billing_audit(current_user, "contract.resent", restaurant_id, target=env["envelope_id"],
                               summary="Contract re-sent (same envelope, reminders on)")
                return jsonify(ok=True, resent=True, envelope_id=env["envelope_id"])
            if res.get("status") == "completed":
                return jsonify(ok=False, error=(
                    "DocuSign says this envelope is already signed. If the console still shows it unsigned, "
                    "use Mark signed (offline) after checking it in DocuSign.")), 409
            # Declined or voided at DocuSign: a new envelope is the only way.
        from docusign_helper import send_contract
        result = send_contract(
            owner_email=restaurant.owner_email,
            owner_name=restaurant.owner_name or restaurant.name,
            restaurant_name=restaurant.name,
            module_count=mods,
            modules_list=modules_list,
            restaurant_id=restaurant_id,
        )
        envelope_id = result.get("envelope_id")
        updates = {"docusign_envelope_id": envelope_id}
        if not signed:
            updates["contract_status"] = "sent"
        with _mdl.billing_context(source="admin", actor=current_user.get("username"),
                                  reason="amendment sent" if signed else "contract re-sent (new envelope)"):
            update_restaurant(restaurant_id, updates)
        if envelope_id:
            conn = get_conn()
            try:
                conn.execute("UPDATE docusign_envelopes SET module_count=?, modules_list=?, status='sent', "
                             "status_at=datetime('now') WHERE envelope_id=?", (mods, modules_list, envelope_id))
                conn.commit()
            finally:
                conn.close()
        # No email_log row: DocuSign sends that email itself, and a row
        # marked 'sent' here recorded a send nobody here made (#109).
        _billing_audit(current_user, "contract.amendment_sent" if signed else "contract.new_envelope",
                       restaurant_id, target=envelope_id,
                       summary=("Amendment sent" if signed else "New contract envelope sent") + f": {modules_list}")
        return jsonify(ok=True, resent=False, new_envelope=True, envelope_id=envelope_id, amendment=signed)
    except Exception as e:
        print(f"Resend contract error: {e}")
        return jsonify(ok=False, error=_safe_err(e)), 502


def _latest_envelope(restaurant_id, current_envelope_id=None):
    """The envelope a resend should reuse: the restaurant's current one."""
    conn = get_conn()
    try:
        row = None
        if current_envelope_id:
            row = conn.execute("SELECT * FROM docusign_envelopes WHERE envelope_id=?",
                               (current_envelope_id,)).fetchone()
            if not row:
                return {"envelope_id": current_envelope_id}
        if not row:
            row = conn.execute("SELECT * FROM docusign_envelopes WHERE restaurant_id=? ORDER BY sent_at DESC, "
                               "rowid DESC LIMIT 1", (restaurant_id,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def _billing_audit(current_user, action, restaurant_id, target=None, before=None, after=None,
                   result="ok", summary=None):
    """One audited row per billing action. B2's record_admin_action (fix
    round contract) when it exists, else the ledger's plain record."""
    import admin_events as _ae
    actor = (current_user or {}).get("username")
    try:
        rec = getattr(_ae, "record_admin_action", None)
        if rec is not None:
            return rec(actor, f"billing.{action}", restaurant_id=restaurant_id, target=target,
                       before=before, after=after, result=result, summary=summary)
    except Exception:
        pass
    try:
        return _ae.record("admin", f"billing.{action}", restaurant_id=restaurant_id,
                          summary=f"{actor}: {summary or action}"[:300],
                          payload={"target": target, "before": before, "after": after, "result": result,
                                   "actor": actor})
    except Exception:
        return False


def _send_owed_now(owed_id):
    """Send one owed billing email now and report what really happened:
    (http_status, payload). A failure is a 502 with a sentence (#109)."""
    import billing_jobs
    if not owed_id:
        return 500, {"ok": False, "error": "Nothing was queued."}
    out = billing_jobs.drain_owed_sends(ids=[owed_id])
    res = (out.get("results") or [{}])[0]
    state = res.get("state")
    if state in ("sent", "sent_unverified"):
        return 200, {"ok": True, "sent": True, "owed_send": owed_id, "state": state}
    if state == "held":
        return 409, {"ok": False, "sent": False, "owed_send": owed_id, "error": (
            "Not sent: this server does not send client email (it is not the production server). "
            "It stays queued here and is never sent from this machine.")}
    if state == "skipped":
        return 409, {"ok": False, "sent": False, "owed_send": owed_id,
                     "error": f"Not sent: {(res.get('error') or 'it no longer applies')}."}
    if state == "retry":
        return 502, {"ok": False, "sent": False, "owed_send": owed_id, "retrying": True, "error": (
            f"Not delivered yet: {res.get('error') or 'the email service did not accept it'}. "
            "It will retry on its own; the console shows when it goes.")}
    if not state:
        return 409, {"ok": False, "sent": False, "owed_send": owed_id,
                     "error": "It is already being sent — check again in a minute."}
    return 502, {"ok": False, "sent": False, "owed_send": owed_id,
                 "error": f"Not delivered: {res.get('error') or 'the send failed'}."}


@admin_bp.route("/admin/resend-payment/<int:restaurant_id>", methods=["POST"])
@admin_required
def resend_payment(restaurant_id, current_user):
    """Send the client the link that moves them forward, and say what really
    happened (#6, #109). A past-due client gets the card-update link — the
    setup-fee email answered them "You're all set" — a paying or covered
    client gets nothing (and is told why), and anyone else gets the payment
    link. The send is owed first and marked from the real delivery result;
    no email_log row is written here (the send writes the real one)."""
    if not current_user.get("is_admin"):
        return jsonify(ok=False, error="Support accounts are read-only."), 403
    import billing_jobs
    import models as _mdl
    restaurant = get_restaurant(restaurant_id)
    if not restaurant:
        return jsonify(ok=False, error="Restaurant not found"), 404
    lock = _mdl.billing_hold(restaurant)
    if lock:
        return jsonify(ok=False, error=f"This account is on hold ({lock}). Lift the hold before sending "
                                       "billing links."), 409
    payer = billing_jobs.billed_by(restaurant_id)
    status = (restaurant.billing_status or "").lower()
    if payer and payer != restaurant_id and status != "past_due":
        p = get_restaurant(payer)
        return jsonify(ok=False, covered=True, billed_by=payer, error=(
            f"{restaurant.name} is covered by {getattr(p, 'name', 'another location')}'s subscription — "
            "there is nothing for it to pay.")), 409
    if status == "past_due":
        kind, key = "card_update", f"card_update:{restaurant_id}:admin:{datetime.now().strftime('%Y%m%d%H%M%S%f')}"
        target = get_restaurant(payer) if payer and payer != restaurant_id else restaurant
    elif status in ("active", "internal") or billing_jobs.live_subscription(restaurant_id):
        return jsonify(ok=False, paying=True, error=(
            f"{restaurant.name} is already paying — no payment link was sent. Use Send card-update link "
            "to have them change the card.")), 409
    else:
        if not any((restaurant.module_reviews, restaurant.module_labor, restaurant.module_inventory,
                    restaurant.module_marketing)):
            return jsonify(ok=False, error="No modules active for this client"), 400
        kind, key = "payment_link", f"payment_link:{restaurant_id}:admin:{datetime.now().strftime('%Y%m%d%H%M%S%f')}"
        target = restaurant
    owed_id, _made = billing_jobs.enqueue(kind, target.id, key, to_email=target.owner_email,
                                          source="admin", actor=current_user.get("username"))
    code, payload = _send_owed_now(owed_id)
    _billing_audit(current_user, f"{kind}.sent" if payload.get("ok") else f"{kind}.failed", restaurant_id,
                   target=str(owed_id), result="ok" if payload.get("ok") else "error",
                   summary=("Payment link" if kind == "payment_link" else "Card-update link")
                   + (" sent" if payload.get("ok") else f" not sent: {payload.get('error')}"))
    payload["kind"] = kind
    return jsonify(**payload), code

@admin_bp.route("/admin/seed-reviews/<int:restaurant_id>", methods=["POST"])
@admin_required
def seed_reviews(restaurant_id, current_user):
    """Seed 12 invented sample reviews into a DEMO account so the dashboard
    has something to show.

    Demo accounts only, checked here on the server (#21): it ran on any
    restaurant, and its invented 1-stars ("Health department should know")
    then drove real alerts to a real owner and fed their ratings, AI
    context and the cross-restaurant inputs. The analysis is written for
    the seeded rows only (it used to label whatever real reviews were
    pending), and the replies are drafted on the admin job pool rather than
    by up to twelve model calls on the request thread (#153)."""
    from models import save_reviews, update_analysis, Review
    from datetime import datetime, timedelta
    restaurant = get_restaurant(restaurant_id)
    if not restaurant:
        return jsonify(ok=False, error="Restaurant not found"), 404
    if int(getattr(restaurant, "is_demo", 0) or 0) != 1:
        return jsonify(ok=False, error="Sample reviews go into demo accounts only. This is a real client: "
                                       "its reviews, ratings and alerts are real. Nothing was added."), 400
    # Nothing here sends: the alerts seeded reviews can lead to are the
    # scheduler's, which never runs on a local backend (scheduling_allowed).

    # Generate 12 realistic sample reviews
    sample = [
        ("Jennifer M.","google","r_s001",5,"Absolutely love this place. The food was incredible and our server was attentive without being intrusive. Will be back every month.",4),
        ("Tom K.","yelp","r_s002",2,"Waited 45 minutes for a table even though we had a reservation. Food was fine when it arrived but the experience was frustrating.",1),
        ("Aisha R.","google","r_s003",5,"Best spot in the neighborhood. The seasonal menu is always exciting and the cocktails are outstanding. Came three weekends in a row.",4),
        ("Derek S.","google","r_s004",1,"Found a hair in my food. Server was unapologetic. Manager offered a 10% discount which felt insulting. Health department should know.",4),
        ("Priya N.","yelp","r_s005",4,"Really good neighborhood spot. Salmon was perfectly cooked. Docked one star because the cocktail menu feels dated.",3),
        ("Carlos B.","google","r_s006",5,"Took my parents here for their anniversary and the staff went completely above and beyond. My mom is still talking about it.",5),
        ("Rachel W.","yelp","r_s007",3,"Mixed experience. Appetizers were excellent but the main courses took over an hour. Would try again on a quieter evening.",2),
        ("Mike T.","google","r_s008",5,"The happy hour deal is unreal. Half price on all small plates and the bartender is hilarious. Told everyone at work.",6),
        ("Sandra L.","yelp","r_s009",2,"Gluten-free options listed on the menu but staff seemed unsure whether dishes were actually safe for celiac. Need better training.",7),
        ("James O.","google","r_s010",5,"Took a date here and it couldn't have gone better. Warm atmosphere, great wine pairing suggestions. Already booked for next month.",8),
        ("Beth C.","google","r_s011",1,"Ordered takeout and it arrived 35 minutes late and completely cold. Called to complain and was offered nothing. Lost a loyal customer.",9),
        ("Olivia T.","yelp","r_s012",5,"Been a regular for two years and the kitchen keeps getting better. New menu just launched and it's an instant classic.",10),
    ]

    sentiments = {5:"positive",4:"positive",3:"neutral",2:"negative",1:"negative"}
    categories_map = [
        ["food_quality","service"],["service","reservation"],["food_quality","ambiance"],
        ["cleanliness","service"],["food_quality","value"],["service","ambiance"],
        ["food_quality","service"],["value","ambiance"],["service","cleanliness"],
        ["ambiance","service"],["takeout_delivery","service"],["food_quality"],
    ]
    urgencies = ["normal","normal","normal","high","normal","normal","normal",
                 "normal","normal","normal","normal","normal"]

    reviews = []
    for i, (author, platform, ext_id, rating, text, days_ago) in enumerate(sample):
        review_date = (datetime.now() - timedelta(days=days_ago*3)).isoformat()
        reviews.append(Review(
            restaurant_id=restaurant_id,
            platform=platform,
            external_id=f"{restaurant_id}_{ext_id}",
            author=author,
            rating=rating,
            text=text,
            review_date=review_date,
        ))

    new_count, new_reviews = save_reviews(reviews)

    # The analysis, for the rows just seeded — by their position in `sample`,
    # never whatever other reviews happened to be pending.
    by_ext = {f"{restaurant_id}_{s[2]}": i for i, s in enumerate(sample)}
    seeded_ids = []
    for r in new_reviews:
        i = by_ext.get(r.external_id)
        if i is None:
            continue
        sent = sentiments.get(r.rating, "neutral")
        summary = f"Guest {'praised' if sent=='positive' else 'criticized'} the experience."
        update_analysis(r.id, sent, categories_map[i % len(categories_map)], summary, urgencies[i % len(urgencies)])
        seeded_ids.append(r.id)

    import admin_events
    job_id = None
    if seeded_ids:
        job_id, _joined = _start_admin_job("admin_seed_drafts", restaurant_id,
                                           lambda: _draft_reviews_job(restaurant_id, seeded_ids))
    admin_events.record_admin_action(current_user, "reviews.seeded", restaurant_id=restaurant_id,
                                     target=f"restaurant:{restaurant_id}",
                                     after={"seeded": new_count, "review_ids": seeded_ids, "draft_job": job_id})
    return jsonify(ok=True, seeded=new_count, job_id=job_id,
                   message=(f"Seeded {new_count} sample review{'s' if new_count != 1 else ''}"
                            + (" — drafting replies in the background." if job_id else
                               (" (they were already there)." if not new_count else "."))))


def _draft_reviews_job(restaurant_id, review_ids):
    """Draft replies for these reviews, on the admin pool. Returns counts."""
    from drafter import draft_response, DraftNotReplaced
    from models import get_approved_examples
    from ai_utils import is_platform_stop
    restaurant = get_restaurant(restaurant_id)
    if not restaurant:
        return {"ok": False, "error": "Restaurant not found"}
    conn = get_conn()
    try:
        marks = ",".join("?" * len(review_ids))
        rows = conn.execute(f"SELECT id, rating, text, sentiment, urgency FROM reviews WHERE restaurant_id=? "
                            f"AND id IN ({marks}) AND deleted_at IS NULL", (restaurant_id, *review_ids)).fetchall()
    finally:
        conn.close()
    examples = get_approved_examples(restaurant_id, limit=4)
    drafted, failed, stopped = 0, 0, None
    for r in rows:
        try:
            draft_response(r["id"], r["rating"], r["text"], r["sentiment"], restaurant.name,
                           voice_notes=restaurant.voice_notes or "", restaurant_id=restaurant_id,
                           approved_examples=examples, sign_off=restaurant.sign_off_name or restaurant.name,
                           never_say=restaurant.never_say or "", urgency=r["urgency"] or "normal",
                           language=getattr(restaurant, "response_language", None) or None)
            drafted += 1
        except DraftNotReplaced:
            continue
        except Exception as e:
            if is_platform_stop(e):
                stopped = _safe_err(e)
                break
            failed += 1
            print(f"[seed] draft error [{r['id']}]: {_safe_err(e)}")
    out = {"ok": not failed and not stopped, "drafted": drafted, "failed": failed}
    if stopped or failed:
        out["error"] = (f"Drafting stopped: {stopped}" if stopped else f"{failed} draft(s) failed.")
    return out

@admin_bp.route("/admin/ai-usage/<int:restaurant_id>")
@admin_required
def ai_usage(restaurant_id, current_user):
    """Per-restaurant Claude spend, most-expensive action first — the
    visibility gap flagged when nothing in the app tracked AI cost at all."""
    from ai_utils import usage_summary
    since_days = request.args.get("days", 30, type=int)
    rows = usage_summary(restaurant_id=restaurant_id, since_days=since_days)
    total_cost = sum(r["cost_usd"] or 0 for r in rows)
    total_calls = sum(r["calls"] or 0 for r in rows)
    return jsonify(ok=True, rows=rows, total_cost=round(total_cost, 4), total_calls=total_calls, since_days=since_days)

@admin_bp.route("/admin/fetch-reviews/<int:restaurant_id>", methods=["POST"])
@admin_required
def fetch_reviews_now(restaurant_id, current_user):
    """Manually trigger a review fetch for a specific restaurant."""
    restaurant = get_restaurant(restaurant_id)
    if not restaurant:
        return jsonify(ok=False, error="Restaurant not found")

    from fetcher import fetch_google, save_reviews
    reviews = []
    errors = []

    if restaurant.gmb_refresh_token or restaurant.reviews_live:
        # Use GMB API if connected (stores review_name for auto-posting)
        if restaurant.gmb_refresh_token:
            try:
                from gmb import get_valid_token, fetch_reviews_via_gmb, find_gmb_location
                from models import update_restaurant
                token = get_valid_token(restaurant_id)
                if token:
                    loc_id = restaurant.gmb_location_id
                    acct_id = restaurant.gmb_account_id
                    if not loc_id:
                        _m = find_gmb_location(token, restaurant.google_place_id or "")
                        acct_id = _m.get("account") if _m.get("ok") else None
                        if acct_id:
                            loc_id = _m.get("location")
                            if loc_id:
                                update_restaurant(restaurant_id, {
                                    "gmb_account_id": acct_id,
                                    "gmb_location_id": loc_id,
                                })
                        else:
                            errors.append("Google: API access pending — awaiting Google approval")
                    if loc_id:
                        gmb_reviews = fetch_reviews_via_gmb(token, loc_id, restaurant_id)
                        reviews += gmb_reviews
                        # Also capture official GBP overall rating, and
                        # backfill the hero-banner logo if not set yet
                        try:
                            from gmb import fetch_location_rating
                            fetch_location_rating(restaurant_id, token, loc_id)
                        except Exception:
                            pass
                        try:
                            from gmb import fetch_gmb_logo_url
                            fetch_gmb_logo_url(restaurant_id, token, acct_id, loc_id)
                        except Exception:
                            pass
                    else:
                        errors.append("Google: location not found — API access may still be pending")
                else:
                    errors.append("Google: token refresh failed — try reconnecting Google Business")
            except Exception as e:
                errors.append(f"Google GMB: {e}")
        elif restaurant.google_place_id and restaurant.reviews_live:
            # Fallback to Places API
            try:
                reviews += fetch_google(restaurant.google_place_id, restaurant_id)
            except Exception as e:
                errors.append(f"Google: {e}")

    if not reviews and not errors:
        return jsonify(ok=False, error="No platform IDs configured, reviews_live is off, and GMB not connected")

    _downgraded = []
    new_count, new_reviews = save_reviews(reviews, downgrades=_downgraded) if reviews else (0, [])

    # Fire alerts for newly saved reviews, and for reviews a guest edited
    # down to a lower rating on this fetch.
    if new_reviews or _downgraded:
        try:
            from notify import fire_review_alerts
            fire_review_alerts(restaurant_id, restaurant.name, new_reviews,
                               edited_reviews=_downgraded)
        except Exception as _ae:
            print(f"[alert] fire error: {_ae}")

    # Run analysis + drafting in background so route returns immediately
    import threading
    def _analyse_and_draft():
        try:
            from models import get_pending_analysis, get_pending_drafts, get_approved_examples
            from analyser import analyse_review
            from drafter import draft_response
            for r in get_pending_analysis(restaurant_id, limit=50):
                try: analyse_review(r.id, r.rating, r.text, restaurant_id=restaurant_id)
                except Exception: pass
            approved_examples = get_approved_examples(restaurant_id, limit=4)
            for r in get_pending_drafts(restaurant_id):
                try:
                    draft_response(
                        r.id, r.rating, r.text, r.sentiment,
                        restaurant.name,
                        voice_notes=restaurant.voice_notes or "",
                        restaurant_id=restaurant_id,
                        approved_examples=approved_examples,
                        sign_off=restaurant.sign_off_name or restaurant.name,
                        never_say=restaurant.never_say or "",
                        # Both were missing here: urgency meant the serious-issue
                        # escalation never applied, and language meant a redraft
                        # silently reverted a non-English restaurant's replies.
                        urgency=r.urgency or "normal",
                        language=getattr(restaurant, "response_language", None) or None,
                    )
                except Exception: pass
        except Exception as e:
            print(f"[fetch] background error: {e}")
    threading.Thread(target=_analyse_and_draft, daemon=True).start()

    return jsonify(ok=True, new_reviews=new_count, errors=errors)

# The most replies one "Re-draft every pending reply" writes: one model call
# each, and the job must finish well inside ops.JOB_MAX_MINUTES. A review
# past the cap keeps the draft it has — nothing is cleared up front any more.
REDRAFT_MAX = 150


@admin_bp.route("/admin/redraft-all/<int:restaurant_id>", methods=["POST"])
@admin_required
def redraft_all(restaurant_id, current_user):
    """Write a fresh AI draft over every pending or drafted reply — IN
    PLACE, on the admin job pool (#78, #153).

    It used to set every drafted/pending review back to pending with its
    draft NULLed, then redraft 50 on an unbounded thread: an owner's
    hand-edited replies were wiped, and everything past the 50th was left
    with no draft at all. Now nothing is cleared: a reply the owner edited
    (draft_edited=1) is never touched, each draft is replaced only when its
    new one is written (models.update_draft never overwrites an approved or
    posted reply), and the job reports what it did."""
    restaurant = get_restaurant(restaurant_id)
    if not restaurant:
        return jsonify(ok=False, error="Restaurant not found"), 404
    actor = dict(current_user)
    job_id, joined = _start_admin_job("admin_redraft", restaurant_id, lambda: _redraft_job(restaurant_id, actor))
    if not job_id:
        return jsonify(ok=False, error="Too many admin jobs are running — try again in a minute."), 429
    import admin_events
    admin_events.record_admin_action(current_user, "reviews.redraft_started", restaurant_id=restaurant_id,
                                     target=f"restaurant:{restaurant_id}", after={"job_id": job_id, "joined": joined})
    return jsonify(ok=True, job_id=job_id, joined=joined,
                   message=("Already re-drafting this restaurant's replies — following that run."
                            if joined else "Re-drafting pending replies in the background. Replies the owner "
                                           "edited are left as they are."))


def _redraft_job(restaurant_id, actor=None):
    """The redraft, on the admin pool. Returns counts: redrafted, skipped
    (owner-edited, or approved/posted while it ran), failed, and how many
    are left past REDRAFT_MAX."""
    from models import get_approved_examples
    from analyser import analyse_review
    from drafter import draft_response, DraftNotReplaced
    from ai_utils import is_platform_stop
    import admin_events
    restaurant = get_restaurant(restaurant_id)
    if not restaurant:
        return {"ok": False, "error": "Restaurant not found"}
    conn = get_conn()
    try:
        rows = [dict(r) for r in conn.execute(
            "SELECT id, rating, text, sentiment, urgency, processed, COALESCE(draft_edited, 0) AS edited "
            "FROM reviews WHERE restaurant_id=? AND response_status IN ('drafted', 'pending') "
            "AND deleted_at IS NULL ORDER BY CASE urgency WHEN 'high' THEN 0 ELSE 1 END, fetched_at DESC",
            (restaurant_id,)).fetchall()]
    finally:
        conn.close()
    edited = [r for r in rows if r["edited"]]
    todo = [r for r in rows if not r["edited"]]
    redrafted, failed, skipped, stopped = 0, 0, 0, None
    examples = get_approved_examples(restaurant_id, limit=4)
    for r in todo[:REDRAFT_MAX]:
        try:
            sentiment, urgency = r["sentiment"], r["urgency"]
            if not sentiment and not r["processed"]:
                analysed = analyse_review(r["id"], r["rating"], r["text"], restaurant_id=restaurant_id) or {}
                sentiment, urgency = analysed.get("sentiment"), analysed.get("urgency") or urgency
            if not sentiment:
                skipped += 1
                continue
            draft_response(r["id"], r["rating"], r["text"], sentiment, restaurant.name,
                           voice_notes=restaurant.voice_notes or "", restaurant_id=restaurant_id,
                           approved_examples=examples, sign_off=restaurant.sign_off_name or restaurant.name,
                           never_say=restaurant.never_say or "",
                           # Both were missing once: urgency is the serious-issue
                           # escalation, language a non-English restaurant's replies.
                           urgency=urgency or "normal",
                           language=getattr(restaurant, "response_language", None) or None)
            redrafted += 1
        except DraftNotReplaced:
            skipped += 1
        except Exception as e:
            if is_platform_stop(e):
                stopped = _safe_err(e)
                break
            failed += 1
            print(f"[redraft-all] error [{r['id']}]: {_safe_err(e)}")
    remaining = max(0, len(todo) - REDRAFT_MAX)
    out = {"ok": not failed and not stopped, "redrafted": redrafted, "kept_owner_edits": len(edited),
           "skipped": skipped, "failed": failed, "remaining": remaining,
           "message": (f"Re-drafted {redrafted} repl{'y' if redrafted == 1 else 'ies'}; "
                       f"left {len(edited)} the owner edited as they were"
                       + (f"; {remaining} more past this run's limit keep their current draft" if remaining else "")
                       + ".")}
    if stopped or failed:
        out["error"] = (f"Stopped after {redrafted}: {stopped}" if stopped else
                        f"{failed} repl{'y' if failed == 1 else 'ies'} could not be re-drafted; they keep "
                        f"their current draft.")
    admin_events.record_admin_action(actor or "system", "reviews.redrafted", restaurant_id=restaurant_id,
                                     target=f"restaurant:{restaurant_id}",
                                     after={k: out[k] for k in ("redrafted", "kept_owner_edits", "skipped",
                                                                 "failed", "remaining")},
                                     result="ok" if out["ok"] else ("partial" if redrafted else "failed"))
    return out

@admin_bp.route("/admin/view-as/<int:restaurant_id>", methods=["GET", "POST"])
@admin_required
def view_as_client(restaurant_id, current_user):
    """Log in as a client to see exactly what they see.

    A GET only asks. It used to mint the impersonation session and swap the
    admin's cookie for it, so any page could send an admin's browser into a
    client's account with a link (SEC-34). The button on the page POSTs,
    with the same double-submit CSRF token every admin write carries.

    Owner decision (9/29/26): an admin's view-as keeps full write access;
    a support login's is read-only. Either way it lasts auth.VIEW_AS_HOURS
    from now (never extended by use), names the admin behind it on the
    session row, shows the banner on every page, and every write through it
    is recorded as that admin's (auth.record_view_as_write). It opens only
    on the restaurant's own owner login — never a manager's or anyone
    else's as a stand-in (SECURITY #85)."""
    user_id = _principal_login_id(restaurant_id)
    if not user_id:
        return ("This restaurant has no owner login to view as. Open it from the location "
                "that has the owner's login, or add one first."), 404
    if request.method != "POST":
        return _view_as_confirm_page(restaurant_id)
    from auth import create_view_as_session, VIEW_AS_HOURS, current_session_token, sql_utc
    from datetime import datetime as _dt_va, timedelta as _td_va, timezone as _tz_va
    read_only = not current_user.get("is_admin")
    token = create_view_as_session(user_id, current_user, read_only=read_only,
                                   ip_address=request.remote_addr,
                                   user_agent=request.headers.get("User-Agent", ""))
    try:
        import admin_events
        # The end is on the record too: a view that simply runs out has no
        # stop row of its own.
        admin_events.record("admin", "view_as_started", restaurant_id=restaurant_id,
                            summary=(f"{current_user.get('username')} opened a "
                                     f"{'read-only ' if read_only else ''}view-as session "
                                     f"(signed in as login #{user_id}, {VIEW_AS_HOURS}h)"),
                            payload={"actor_id": current_user.get("id"), "actor": current_user.get("username"),
                                     "target_user_id": user_id, "read_only": read_only,
                                     "hours": VIEW_AS_HOURS, "ip": request.remote_addr,
                                     "ends_at": sql_utc(_dt_va.now(_tz_va.utc) + _td_va(hours=VIEW_AS_HOURS))})
    except Exception:
        pass
    resp = make_response(redirect("/"))
    # The cookie ends when the session does: VIEW_AS_HOURS, absolute.
    resp.set_cookie("session_token", token, max_age=VIEW_AS_HOURS * 3600,
                    httponly=True, secure=config.on_railway(), samesite="Strict")
    # The admin's own session waits in a cookie only /admin/stop-viewing
    # can read, and comes back when the view ends — a view-as used to cost
    # a full sign-in (and a code) every time (LIFECYCLE-17).
    own = current_session_token()
    if own:
        resp.set_cookie(_VIEW_AS_RETURN_COOKIE, own, max_age=VIEW_AS_HOURS * 3600, path="/admin/stop-viewing",
                        httponly=True, secure=config.on_railway(), samesite="Strict")
    return resp


# The admin's own session token while a view-as is open, readable only by
# /admin/stop-viewing (path-scoped, HttpOnly, SameSite=Strict).
_VIEW_AS_RETURN_COOKIE = "cavnar_admin_return"


def _view_as_confirm_page(restaurant_id):
    """The GET half of view-as: a button that POSTs, carrying the csrf_js
    double-submit token (minted here when this browser has none yet —
    ensure_csrf_cookie leaves a response that already sets one alone)."""
    import secrets as _sec_va
    from markupsafe import escape as _esc_va
    from models import get_restaurant as _gr_va
    from csrf import CSRF_COOKIE
    from auth import VIEW_AS_HOURS
    rest = _gr_va(restaurant_id)
    name = _esc_va(rest.name if rest else f"restaurant {restaurant_id}")
    csrf_tok = request.cookies.get(CSRF_COOKIE) or _sec_va.token_urlsafe(32)
    import auth_routes as _ar_va
    body = _ar_va._SIMPLE_PAGE % (
        f"<h1>View as {name}?</h1><p>This opens their dashboard in this browser for {VIEW_AS_HOURS} hours, "
        f"signed in as their owner login. A banner shows on every page while it lasts, and every change "
        f"you make is recorded under your name.</p>"
        f"<form method='post' action='/admin/view-as/{int(restaurant_id)}'>"
        f"<input type='hidden' name='csrf_token' value='{_esc_va(csrf_tok)}'>"
        f"<button type='submit' class='cbtn cbtn-primary'>Open their dashboard</button></form>"
        f"<p style='margin-top:16px'><a href='/admin'>Back to the admin console</a></p>")
    resp = make_response(body)
    if not request.cookies.get(CSRF_COOKIE):
        resp.set_cookie(CSRF_COOKIE, csrf_tok, max_age=30 * 24 * 3600, httponly=False,
                        secure=config.on_railway(), samesite="Lax")
    return resp


@admin_bp.route("/admin/stop-viewing", methods=["GET", "POST"])
def stop_viewing():
    """End a view-as session. A GET only asks: it deleted whatever session
    the cookie named, so any link anywhere signed its visitor out
    (SECURITY-15). The POST (CSRF-checked like every admin_bp write) ends
    only an admin-view-as session, records who ended it, and puts the
    admin's own session back when it is still alive; otherwise the admin
    signs in again. Deliberately not @admin_required: the session in the
    cookie is the client's login, worn by the admin."""
    token = request.cookies.get("session_token")
    viewing = get_session_user(token) if token else None
    is_view_as = bool(viewing) and (viewing.get("device_type") or "") == "admin-view-as"
    if request.method != "POST":
        if not is_view_as:
            return redirect("/admin")
        import secrets as _sec_sv
        from markupsafe import escape as _esc_sv
        from csrf import CSRF_COOKIE
        csrf_tok = request.cookies.get(CSRF_COOKIE) or _sec_sv.token_urlsafe(32)
        import auth_routes as _ar_sv
        body = _ar_sv._SIMPLE_PAGE % (
            "<h1>Stop viewing as this client?</h1><p>This ends the view-as session and takes you back "
            "to the admin console.</p><form method='post' action='/admin/stop-viewing'>"
            f"<input type='hidden' name='csrf_token' value='{_esc_sv(csrf_tok)}'>"
            "<button type='submit' class='cbtn cbtn-primary'>Back to admin</button></form>")
        resp = make_response(body)
        if not request.cookies.get(CSRF_COOKIE):
            resp.set_cookie(CSRF_COOKIE, csrf_tok, max_age=30 * 24 * 3600, httponly=False,
                            secure=config.on_railway(), samesite="Lax")
        return resp
    if not is_view_as:
        # Nothing to stop; never sign an ordinary session out from here.
        return redirect("/admin")
    delete_session(token)
    try:
        import admin_events
        admin_events.record("admin", "view_as_stopped", restaurant_id=viewing.get("restaurant_id"),
                            summary=f"{viewing.get('acting_admin') or 'an admin'} stopped viewing as "
                                    f"{viewing.get('username')}",
                            payload={"actor_id": viewing.get("acting_admin_id"), "actor": viewing.get("acting_admin"),
                                     "target_user_id": viewing.get("id"), "ip": request.remote_addr})
    except Exception:
        pass
    own = request.cookies.get(_VIEW_AS_RETURN_COOKIE, "")
    back = get_session_user(own) if own else None
    from auth import is_internal_login as _iil_sv, session_cookie_max_age as _scma_sv
    if back and _iil_sv(back) and back.get("id") == viewing.get("acting_admin_id"):
        resp = make_response(redirect("/admin"))
        resp.set_cookie("session_token", own, max_age=_scma_sv(back), httponly=True,
                        secure=config.on_railway(), samesite="Lax")
    else:
        resp = make_response(redirect("/login?next=/admin"))
        resp.delete_cookie("session_token")
    resp.delete_cookie(_VIEW_AS_RETURN_COOKIE, path="/admin/stop-viewing")
    return resp

@admin_bp.route("/admin/inventory-template")
@admin_required
def inventory_template(current_user):
    """Download a pre-filled CSV template for inventory data."""
    import io
    template = """item,category,unit,par_level,current_stock,unit_cost,avg_daily_usage,last_ordered,last_order_qty,waste_last_week
Salmon fillet,protein,lb,20,18,14.50,3.2,2026-05-12,30,5.0
Chicken breast,protein,lb,30,25,4.20,5.0,2026-05-12,40,3.0
Romaine lettuce,produce,case,8,6,18.00,1.5,2026-05-12,10,1.5
Roma tomatoes,produce,lb,15,12,2.10,2.8,2026-05-12,20,2.0
Heavy cream,dairy,qt,12,10,3.80,2.0,2026-05-12,15,1.0
Pasta dried,dry,lb,25,22,1.20,4.0,2026-05-12,30,2.0
Olive oil,dry,liter,6,5,12.00,0.8,2026-05-12,8,0.5
House red wine,beverage,bottle,24,20,8.50,3.5,2026-05-12,30,2.0
"""
    buf = io.BytesIO(template.strip().encode())
    buf.seek(0)
    from flask import send_file
    return send_file(
        buf,
        mimetype="text/csv",
        as_attachment=True,
        download_name="cavnar_ai_inventory_template.csv"
    )

@admin_bp.route("/privacy")
def privacy_page():
    """Serve the Cavnar AI privacy policy page."""
    from flask import Response
    import os as _os
    try:
        html_path = _os.path.join(_os.path.dirname(__file__), "public", "privacy.html")
        with open(html_path, "r") as f:
            html = f.read()
    except FileNotFoundError:
        html = "<h1>Privacy Policy</h1><p>Coming soon. Contact will@cavnar.ai</p>"
    return Response(html, mimetype="text/html")

@admin_bp.route("/terms")
def terms_page():
    from flask import Response
    import os as _os
    try:
        html_path = _os.path.join(_os.path.dirname(__file__), "public", "terms.html")
        with open(html_path, "r") as f:
            html = f.read()
    except FileNotFoundError:
        html = "<h1>Terms of Service</h1><p>Coming soon. Contact will@cavnar.ai</p>"
    return Response(html, mimetype="text/html")

@admin_bp.route("/sms-optin-preview", methods=["GET", "POST"])
def sms_optin_preview_page():
    """Public, unauthenticated, and — since the rejection that made the
    point explicitly ("the opt-in link provided lacks phone number field")
    — genuinely FUNCTIONAL SMS opt-in form for Twilio's A2P 10DLC campaign
    reviewers, who cannot log into the dashboard to use the real one and
    won't accept a screenshot/mockup of it in place of a live, testable
    URL. A real <input type=tel> phone field, a real unchecked-by-default
    consent <input type=checkbox> (not a styled <span>), and all four
    required disclosures (message type, frequency, "rates may apply",
    STOP-to-opt-out) together in that checkbox's own label — the previous
    version had the STOP/rates language there but frequency only in a
    separate "Message program details" paragraph further down the page,
    which is what the most recent rejection's "missing required
    disclosures (frequency,)" note was pointing at.

    Submitting does not enroll the number in real messaging — the
    messaging service this campaign registers is itself pending Twilio's
    approval, so there is nothing live to send a confirmation through yet,
    and a stranger's number entered by a reviewer must never receive a
    real text from us. It validates, then shows an honest confirmation
    state saying exactly that, rather than implying an SMS went out.
    """
    from flask import Response
    import os as _os
    import re as _re
    import secrets as _secrets_csrf
    from csrf import CSRF_COOKIE as _CSRF_COOKIE

    submitted = False
    error = None
    if request.method == "POST":
        cookie_tok = request.cookies.get(_CSRF_COOKIE, "")
        sent_tok = request.form.get("csrf_token", "")
        if not (cookie_tok and sent_tok and cookie_tok == sent_tok):
            error = "Your session expired — please try again."
        else:
            phone = (request.form.get("phone") or "").strip()
            consent = request.form.get("consent") == "on"
            digits = _re.sub(r"\D", "", phone)
            if not consent:
                error = "Check the consent box to subscribe."
            elif len(digits) < 10:
                error = "Enter a valid mobile phone number."
            else:
                submitted = True

    try:
        html_path = _os.path.join(_os.path.dirname(__file__), "sms_optin_preview.html")
        with open(html_path, "r") as f:
            html = f.read()
    except FileNotFoundError:
        return Response("<h1>SMS Opt-In Flow</h1><p>Contact will@cavnar.ai</p>", mimetype="text/html")

    csrf_token = request.cookies.get(_CSRF_COOKIE) or _secrets_csrf.token_urlsafe(32)
    html = html.replace("{{CSRF_TOKEN}}", csrf_token)
    html = html.replace("{{FORM_STATE}}", "submitted" if submitted else ("error" if error else "form"))
    html = html.replace("{{ERROR_TEXT}}", error or "")
    html = html.replace("{{ERROR_DISPLAY}}", "block" if error else "none")

    resp = Response(html, mimetype="text/html")
    if not request.cookies.get(_CSRF_COOKIE):
        resp.set_cookie(_CSRF_COOKIE, csrf_token, max_age=30 * 24 * 3600,
                        httponly=False, secure=config.on_railway(),
                        samesite="Lax")
    return resp

@admin_bp.route("/.well-known/security.txt")
def security_txt():
    from flask import Response
    content = (
        "Contact: mailto:will@cavnar.ai\n"
        "Preferred-Languages: en\n"
        "Policy: https://dashboard.cavnar.ai/privacy\n"
        "Expires: 2027-01-01T00:00:00.000Z\n"
    )
    return Response(content, mimetype="text/plain")

# /sitemap.xml and /robots.txt live on the app in hosted_dashboard.py. This
# blueprint used to register both as well; it is registered first, so its
# permissive robots.txt (no Disallow) was the one that served and the app's
# `Disallow: /admin, /login, /api/` never shipped.

@admin_bp.route("/og-image.png")
def og_image():
    from flask import send_file
    import os as _os
    path = _os.path.join(_os.path.dirname(__file__), "static", "og-image.png")
    return send_file(path, mimetype="image/png")

@admin_bp.route("/favicon.ico")
def favicon_ico():
    from flask import send_file
    import os as _os
    path = _os.path.join(_os.path.dirname(__file__), "static", "favicon.ico")
    return send_file(path, mimetype="image/x-icon")

@admin_bp.route("/favicon.png")
def favicon_png():
    from flask import send_file
    import os as _os
    path = _os.path.join(_os.path.dirname(__file__), "static", "favicon.png")
    return send_file(path, mimetype="image/png")

# ── Instagram / Meta routes ───────────────────────────────────────────────────

@admin_bp.route("/admin/api/client-usage/<int:restaurant_id>")
@admin_required
def client_usage(restaurant_id, current_user):
    """Return 30-day activity summary for a restaurant."""
    from models import get_activity_summary, get_conn as _gc
    summary = get_activity_summary(restaurant_id, days=30)
    # last settings change timestamp
    last_settings = None
    try:
        conn = _gc()
        row = conn.execute(
            """SELECT created_at FROM activity_log
               WHERE restaurant_id=? AND event_type='admin_settings_update'
               ORDER BY created_at DESC LIMIT 1""",
            (restaurant_id,)
        ).fetchone()
        conn.close()
        if row:
            last_settings = row["created_at"]
    except Exception:
        pass
    return jsonify(ok=True, last_settings_update=last_settings, **summary)


@admin_bp.route("/api/mark-posted/<int:review_id>", methods=["POST"])
@login_required
def mark_posted(review_id, current_user):
    conn = get_conn()
    row = conn.execute("SELECT platform, author, rating FROM reviews WHERE id=? AND restaurant_id=?",
                       (review_id, current_user["restaurant_id"])).fetchone()
    if not row:
        # Returned ok=True regardless, so a request for another tenant's
        # review (or a deleted one) reported success having changed nothing.
        conn.close()
        return jsonify(ok=False, error="Review not found"), 404
    # posted_at as well as the status — models.mark_posted sets both, and
    # leaving it null here made two rows that mean the same thing look
    # different to anything reading the timestamp.
    conn.execute("UPDATE reviews SET response_status='posted', posted_at=datetime('now') "
                 "WHERE id=? AND restaurant_id=?",
                 (review_id, current_user["restaurant_id"]))
    conn.commit(); conn.close()
    # The reply is live where the guest wrote (pasted onto Yelp or Facebook
    # by hand): the alert that asked for it (review:<id>) was implemented,
    # exactly as a reply Cavnar posted to Google is (client_api, ROI #27).
    # Only an episode someone was shown is recorded (rec_ledger.implemented)
    # — re-audit C15. Web and phone share this handler.
    try:
        import rec_ledger as _rl_mp
        _rl_mp.implemented(current_user["restaurant_id"], _rl_mp.rec_key("review", review_id), "reviews",
                           user_id=current_user.get("id"), role=current_user.get("role"),
                           source_ref=f"posted:{review_id}", meta={"module": "reviews", "via": "marked_posted"})
    except Exception as _rle:
        print(f"[mark-posted] implementation not recorded for review {review_id}: {_rle}")
    try:
        from webhooks import fire_webhook as _fw
        _fw(current_user["restaurant_id"], "response.posted", {
            "review_id": review_id,
            "platform": row["platform"] if row else None,
            "author": row["author"] if row else None,
            "rating": row["rating"] if row else None,
        })
    except Exception:
        pass
    return jsonify(ok=True)

@admin_bp.route("/api/export-reviews")
@login_required
def export_reviews(current_user):
    import io, csv as _csv
    restaurant = get_restaurant(current_user["restaurant_id"])
    reviews = get_reviews_data(current_user["restaurant_id"])
    buf = io.StringIO()
    w = _csv.writer(buf)
    w.writerow(["Date","Author","Platform","Rating","Sentiment","Urgency","Review","Draft Response","Status"])
    for r in reviews:
        w.writerow([
            r.get("review_date","")[:10] if r.get("review_date") else "",
            r.get("author",""),
            r.get("platform",""),
            r.get("rating",""),
            r.get("sentiment",""),
            r.get("urgency",""),
            r.get("text",""),
            r.get("draft_response",""),
            r.get("response_status",""),
        ])
    name = (restaurant.name if restaurant else "restaurant").replace(" ","_")
    from flask import Response
    return Response(
        buf.getvalue(),
        mimetype="text/csv",
        headers={"Content-Disposition": f"attachment;filename={name}_reviews.csv"}
    )

@admin_bp.route("/api/inv-trend")
@login_required
def inv_trend_api(current_user):
    """Legacy weekly waste series (8 weeks, oldest first). The dashboard's
    Waste Trend card now reads /api/food-cost/waste-trend; this stays for
    anything still on the old shape and is served from the same ISO-week
    series (waste_trend.load_waste_history) so both agree."""
    try:
        from waste_trend import load_waste_history
        weeks, _total = load_waste_history(current_user["restaurant_id"], limit=8)
        return jsonify(weeks=[{
            "label": w["label"], "waste": w["waste"], "week_end": w["week_end"],
            "week_start_label": w["start_label"],
            "waste_rate_pct": w["rate"] or 0, "inv_value": w["inv_value"] or 0,
        } for w in weeks])
    except Exception as e:
        return jsonify(weeks=[], error=_safe_err(e))

@admin_bp.route("/admin/upload-menu-pdf/<int:restaurant_id>", methods=["POST"])
@admin_required
def upload_menu_pdf(restaurant_id, current_user):
    """Accept a PDF upload and extract menu items using AI."""
    restaurant = get_restaurant(restaurant_id)
    if not restaurant:
        return jsonify(ok=False, error="Restaurant not found")
    pdf_file = request.files.get("pdf")
    if not pdf_file:
        return jsonify(ok=False, error="No PDF file uploaded")
    try:
        pdf_bytes = pdf_file.read()
        if len(pdf_bytes) > 10 * 1024 * 1024:  # 10MB limit
            return jsonify(ok=False, error="PDF too large — max 10MB")
        from competitor import fetch_menu_from_pdf_bytes
        from models import update_restaurant
        menu_notes = fetch_menu_from_pdf_bytes(pdf_bytes, restaurant.name, restaurant_id=restaurant_id)
        if not menu_notes:
            return jsonify(ok=False, error="Could not extract menu items from this PDF — try a text-based PDF rather than a scanned image")
        update_restaurant(restaurant_id, {"menu_notes": menu_notes})
        return jsonify(ok=True, menu_notes=menu_notes)
    except Exception as e:
        return jsonify(ok=False, error=_safe_err(e))


@admin_bp.route("/admin/fetch-menu-from-url/<int:restaurant_id>", methods=["POST"])
@admin_required
def fetch_menu_from_url_route(restaurant_id, current_user):
    """Fetch and parse menu items from a given URL using AI."""
    data = request.get_json()
    url = data.get("url", "").strip()
    if not url:
        return jsonify(ok=False, error="No URL provided")
    try:
        from competitor import fetch_menu_from_url
        from models import update_restaurant
        menu_items = fetch_menu_from_url(url, restaurant_id=restaurant_id)
        if not menu_items:
            return jsonify(ok=False, error="Could not extract menu from this URL. The site may block automated requests or use JavaScript to load content. Try the PDF upload option instead, or enter items manually.")
        # Save the URL and extracted notes
        update_restaurant(restaurant_id, {"menu_url": url, "menu_notes": menu_items})
        return jsonify(ok=True, menu_notes=menu_items)
    except Exception as e:
        return jsonify(ok=False, error=_safe_err(e))


@admin_bp.route("/admin/refresh-menu-notes/<int:restaurant_id>", methods=["POST"])
@admin_required
def refresh_menu_notes(restaurant_id, current_user):
    """Re-fetch menu notes from Google Places API and update the restaurant record."""
    restaurant = get_restaurant(restaurant_id)
    if not restaurant:
        return jsonify(ok=False, error="Restaurant not found")
    if not restaurant.google_place_id:
        return jsonify(ok=False, error="No Google Place ID set for this restaurant")
    try:
        from competitor import fetch_menu_notes_from_places
        from models import update_restaurant
        menu_notes = fetch_menu_notes_from_places(restaurant.google_place_id,
                                                  restaurant_id=restaurant_id)
        if not menu_notes or len(menu_notes) < 30:
            # Build helpful message with where to find menu data manually
            yelp_url = f"https://www.yelp.com/biz/{restaurant.yelp_business_id}" if restaurant.yelp_business_id else ""
            tips = "Google Places has no menu data for this restaurant. "
            if yelp_url:
                tips += f"Try: 1) Upload a menu PDF, 2) Paste their menu URL and click Fetch, or 3) Copy dishes from their Yelp page ({yelp_url}) into the notes field manually."
            else:
                tips += "Try: 1) Upload a menu PDF, 2) Paste their menu URL and click Fetch, or 3) Enter key dishes manually in the notes field."
            return jsonify(ok=False, error=tips)
        existing = restaurant.menu_notes or ""
        merged = menu_notes if not existing else menu_notes + ("\n\nAdditional notes:\n" + existing if existing not in menu_notes else "")
        update_restaurant(restaurant_id, {"menu_notes": merged})
        has_url = "Menu URL:" in merged
        return jsonify(ok=True, menu_notes=merged,
                       message="\u2713 Updated from Google Places" + (" — menu URL found, dishes extracted" if has_url else ""))
    except Exception as e:
        return jsonify(ok=False, error=_safe_err(e))


RESEND_WELCOME_COOLDOWN_MINUTES = 5


@admin_bp.route("/admin/resend-welcome/<int:restaurant_id>", methods=["POST"])
@admin_required
def resend_welcome_email(restaurant_id, current_user):
    """Email the restaurant's owner login a welcome with their username and
    a link to set their password. Their password is NOT changed (#22).

    It used to pick `... WHERE restaurant_id=? AND is_admin=0 LIMIT 1` — any
    login, a manager or a staff PIN identity included — reset that login's
    password (ending every session it had, web and phone), mail the new
    password to the owner's address, and answer ok whatever the send did,
    with the double-click cooldown claimed before the send so a retry after
    a failure was told the email "went out". Now: the principal login
    (_principal_login_id), no password change, the link goes to that login's
    own address, the cooldown holds only a send that worked, and the answer
    is what actually happened — a 502 and a sentence when it didn't."""
    from models import create_reset_token
    restaurant = get_restaurant(restaurant_id)
    if not restaurant:
        return jsonify(ok=False, error="Restaurant not found"), 404
    status = (getattr(restaurant, "billing_status", "") or "").lower()
    if status in ("churned", "canceled"):
        return jsonify(ok=False, error=f"{restaurant.name} is {status}: a welcome email would invite them back "
                                       f"into a closed account. Nothing was sent."), 409
    import models as _models_rw
    if _models_rw.get_deletion_requested_at(restaurant_id):
        return jsonify(ok=False, error=f"{restaurant.name} asked to close their account, so no welcome email "
                                       f"was sent."), 409
    uid = _principal_login_id(restaurant_id)
    if not uid:
        return jsonify(ok=False, error="This restaurant has no owner login to welcome."), 404
    conn = get_conn()
    try:
        user = conn.execute("SELECT id, username, email, last_login FROM users WHERE id=?", (uid,)).fetchone()
    finally:
        conn.close()
    email = ((user["email"] if user else "") or "").strip()
    if not email:
        return jsonify(ok=False, error="The owner login has no email address."), 409
    blocked = _send_blocked()
    if blocked:
        payload, code = blocked
        return jsonify(**payload), code

    # A double-click must not send two emails whose links cancel each other
    # (DATA-38). The claim is taken before the send, so a second press while
    # the first is sending changes nothing — and given back if the send
    # fails, so the retry the operator makes next actually sends (#22).
    cooldown_key = f"resend_welcome:{restaurant_id}"
    if not _ops.claim_cooldown(cooldown_key, RESEND_WELCOME_COOLDOWN_MINUTES):
        return jsonify(ok=True, email=email, sent_to=email, already_sent=True,
                       message=f"A welcome email went to {email} in the last few minutes, so nothing was "
                               f"sent again.")
    import admin_events
    token = create_reset_token(email)
    if not token:
        _ops.release_period("cooldown", cooldown_key)
        return jsonify(ok=False, error="The owner login is inactive. Nothing was sent."), 409
    try:
        result = _send_welcome_link_email(restaurant, user["username"], email,
                                          f"{config.base_url()}/reset-password/{token}")
    except Exception as e:
        result = _emails.SendResult(False, error=_safe_err(e))
    ok = bool(getattr(result, "ok", False))
    admin_events.record_admin_action(current_user, "welcome.resent", restaurant_id=restaurant_id,
                                     target=f"user:{uid}",
                                     after={"to": email, "delivered": ok, "password_changed": False,
                                            "error": None if ok else str(getattr(result, "error", "") or "")[:200]},
                                     result="ok" if ok else "failed")
    if not ok:
        _ops.release_period("cooldown", cooldown_key)
        return jsonify(ok=False, error=f"The welcome email didn't go out: {_send_failure_reason(result)}. "
                                       f"Nothing about their login changed."), 502
    return jsonify(ok=True, email=email, sent_to=email, username=user["username"],
                   signed_in_before=bool(user["last_login"]),
                   message=f"Welcome email sent to {email} with a link to set their password (it works once, "
                           f"for an hour). Their current password still works.")


def _send_welcome_link_email(restaurant, username, to_email, set_password_url):
    """The welcome, with a set-password link where the password used to be.
    Returns the SendResult. BRAND colours only (DESIGN_SYSTEM.md → Email)."""
    B, esc = _emails.BRAND, _emails.esc
    first = ((getattr(restaurant, "owner_name", None) or "").strip().split() or [""])[0]
    greeting = f"Hi {esc(first)} &mdash;" if first else "Hi &mdash;"
    base = config.base_url()
    inner = f"""
      <p style="color:{B['strong']};font-size:18px;font-weight:700;margin:0 0 12px">Your Cavnar AI dashboard is ready</p>
      <p style="color:{B['body']};font-size:14px;line-height:1.6;margin:0 0 12px">{greeting} the dashboard for <strong>{esc(restaurant.name)}</strong> is live. Your username is <strong>{esc(username)}</strong>.</p>
      <p style="color:{B['body']};font-size:14px;line-height:1.6;margin:0 0 22px">Choose your password with the button below. The link works once, for an hour.</p>
      <a href="{esc(set_password_url)}" style="display:inline-block;background:{B['ember']};color:{B['card']};padding:12px 24px;border-radius:8px;text-decoration:none;font-size:14px;font-weight:600">Set your password &rarr;</a>
      <p style="color:{B['muted']};font-size:12px;line-height:1.6;margin:22px 0 0">If the link has expired, choose &ldquo;Forgot password?&rdquo; at <a href="{esc(base)}" style="color:{B['ember']}">{esc(base.split('//', 1)[-1])}</a> and enter {esc(to_email)}. Questions? Just reply to this email.</p>
    """
    return _emails.deliver(email_type="send_welcome_email", restaurant_id=restaurant.id, payload={
        "from": _emails.sender("will"),
        "to": [to_email],
        "subject": f"Your Cavnar AI dashboard is live — {restaurant.name}",
        "preheader": "Your username and a link to choose your password are inside.",
        "html": _emails._branded_email(inner),
    })


def _test_recipient(restaurant, current_user):
    """(address, 'owner' | 'me') for a test send: the client's owner by
    default, or the acting admin when the body says {"to": "me"} (#127) —
    a test the operator only wants to see need not land in the owner's
    inbox."""
    data = request.get_json(silent=True) or {}
    if str(data.get("to") or "").strip().lower() == "me":
        return ((current_user.get("email") or "").strip(), "me")
    return ((restaurant.owner_email or "").strip(), "owner")


@admin_bp.route("/admin/test-digest/<int:restaurant_id>", methods=["POST"])
@admin_required
def test_digest(restaurant_id, current_user):
    """A [TEST] copy of this week's digest, to the owner or (to: "me") to
    the admin. The answer names the recipient and says whether it went: it
    used to answer ok for a send that was suppressed or never attempted
    (deliver_or_raise raises only on an attempted failure) (#109)."""
    restaurant = get_restaurant(restaurant_id)
    if not restaurant:
        return jsonify(ok=False, error="Restaurant not found"), 404
    blocked = _send_blocked()
    if blocked:
        payload, code = blocked
        return jsonify(**payload), code
    recipient, who = _test_recipient(restaurant, current_user)
    if not recipient:
        return jsonify(ok=False, error="There is no email address to send the test to."), 409
    try:
        from reporter import build_report_from_db, render_html
        report = build_report_from_db(restaurant_id, restaurant.name, days=7)
        html = render_html(report, restaurant.name, owner_name=restaurant.owner_name, restaurant_id=restaurant_id,
                           owner_view=True)
    except Exception as e:
        return jsonify(ok=False, error=f"The digest couldn't be built: {_safe_err(e)}"), 500
    result = _emails.deliver(email_type="digest_preview", restaurant_id=restaurant_id, payload={
        "from": _emails.sender("client"),
        "to": [recipient],
        "subject": f"[TEST] Your weekly review digest — {restaurant.name}",
        "html": _html_doc(html),
    })
    return _test_send_answer(current_user, restaurant, "digest.test_sent", "test digest", recipient, who, result)


@admin_bp.route("/admin/test-urgent/<int:restaurant_id>", methods=["POST"])
@admin_required
def test_urgent(restaurant_id, current_user):
    """A [TEST] urgent-review alert email, to the owner or (to: "me") the
    admin, built with the same template and sent through the same path
    (notify._alert_email_html, emails.deliver) as a real alert. It used a
    bespoke email no real alert uses, swallowed every failure and answered
    ok (#109, #127)."""
    restaurant = get_restaurant(restaurant_id)
    if not restaurant:
        return jsonify(ok=False, error="Restaurant not found"), 404
    blocked = _send_blocked()
    if blocked:
        payload, code = blocked
        return jsonify(**payload), code
    recipient, who = _test_recipient(restaurant, current_user)
    if not recipient:
        return jsonify(ok=False, error="There is no email address to send the test to."), 409
    try:
        from notify import _alert_email_html
        html = _alert_email_html(
            restaurant.name, "Test urgent review alert",
            ["This is a test of the urgent-review alert, sent by Cavnar AI support. Nothing needs doing.",
             "<em>&ldquo;Waited an hour and nobody checked on us.&rdquo; &mdash; Test Guest, Google, 1&#9733;</em>",
             "A real urgent alert looks like this, with a reply already drafted for you to read and post."],
            restaurant_id=restaurant_id, cta_url=f"{config.base_url()}/?tab=reviews")
    except Exception as e:
        return jsonify(ok=False, error=f"The alert couldn't be built: {_safe_err(e)}"), 500
    result = _emails.deliver(email_type="alert_test", restaurant_id=restaurant_id, payload={
        "from": _emails.sender("client"),
        "to": [recipient],
        "subject": f"[TEST] Urgent review alert — {restaurant.name}",
        "html": _html_doc(html),
    })
    return _test_send_answer(current_user, restaurant, "urgent_alert.test_sent", "test urgent alert",
                             recipient, who, result)


def _test_send_answer(current_user, restaurant, action, label, recipient, who, result):
    """The audit row and the truthful answer for a test send."""
    import admin_events
    ok = bool(getattr(result, "ok", False))
    admin_events.record_admin_action(current_user, action, restaurant_id=restaurant.id,
                                     target=f"restaurant:{restaurant.id}",
                                     after={"to": recipient, "to_whom": who, "delivered": ok,
                                            "error": None if ok else str(getattr(result, "error", "") or "")[:200]},
                                     result="ok" if ok else "failed")
    if not ok:
        return jsonify(ok=False, sent_to=recipient, to=who,
                       error=f"The {label} to {recipient} didn't go out: {_send_failure_reason(result)}."), 502
    return jsonify(ok=True, email=recipient, sent_to=recipient, to=who,
                   message=f"The {label} was sent to {recipient}"
                           + (" (the client's owner)." if who == "owner" else " (you)."))

@admin_bp.route("/admin/refresh-ig-token/<int:restaurant_id>", methods=["POST"])
@admin_required
def refresh_ig_token(restaurant_id, current_user):
    """Refresh a restaurant's Instagram and Facebook page tokens.

    Two Meta calls of up to 25 s each ran on the request thread; they run on
    the admin job pool now (#153) — poll /admin/api/admin-jobs/<job_id>.
    The result says which token refreshed and which didn't: a Facebook
    refresh that failed used to be ignored and reported "Tokens refreshed"
    (#109)."""
    restaurant = get_restaurant(restaurant_id)
    if not restaurant or not restaurant.ig_token:
        return jsonify(ok=False, error="No Instagram token found"), 404
    actor = dict(current_user)
    job_id, joined = _start_admin_job("admin_ig_refresh", restaurant_id,
                                      lambda: _refresh_meta_tokens(restaurant_id, actor))
    if not job_id:
        return jsonify(ok=False, error="Too many admin jobs are running — try again in a minute."), 429
    return jsonify(ok=True, job_id=job_id, joined=joined,
                   message="Refreshing the Instagram and Facebook tokens — this takes a few seconds.")


def _meta_exchange(token):
    """(new_token, expires_date, error) for one long-lived token exchange."""
    import requests as _req
    from datetime import datetime, timedelta
    from meta_api import graph_url
    from ai_guard import redact_secrets
    try:
        r = _req.get(graph_url("oauth/access_token"), params={
            "grant_type":        "fb_exchange_token",
            "client_id":         os.getenv("META_APP_ID", ""),
            "client_secret":     os.getenv("META_APP_SECRET", ""),
            "fb_exchange_token": token,
        }, timeout=(5, 20))
    except Exception as e:
        return None, None, f"Meta could not be reached ({_safe_err(e)})"
    if r.status_code != 200:
        return None, None, f"Meta refused it ({r.status_code}: {redact_secrets((r.text or '')[:200])})"
    try:
        body = r.json() or {}
    except Exception:
        body = {}
    new_token = body.get("access_token")
    if not new_token:
        return None, None, "Meta answered without a token"
    try:
        days = max(1, int(body.get("expires_in") or 0) // 86400) if body.get("expires_in") else 60
    except (TypeError, ValueError):
        days = 60
    return new_token, (datetime.now() + timedelta(days=days)).strftime("%Y-%m-%d"), None


def _refresh_meta_tokens(restaurant_id, actor):
    """The refresh, on the admin pool. Saves each token that refreshed,
    and says plainly which did not."""
    from models import update_restaurant
    from time_utils import mdy
    import admin_events
    restaurant = get_restaurant(restaurant_id)
    if not restaurant or not restaurant.ig_token:
        return {"ok": False, "error": "No Instagram token found"}
    refreshed, errors, update = {}, {}, {}
    token, expires, err = _meta_exchange(restaurant.ig_token)
    if token:
        update.update(ig_token=token, ig_token_expires=expires)
        refreshed["instagram"] = expires
    else:
        errors["instagram"] = err
    if restaurant.fb_page_token:
        token, fb_expires, err = _meta_exchange(restaurant.fb_page_token)
        if token:
            update.update(fb_page_token=token, fb_token_expires=fb_expires)
            refreshed["facebook"] = fb_expires
        else:
            errors["facebook"] = err
    if update:
        update_restaurant(restaurant_id, update)
    ok = not errors
    admin_events.record_admin_action(
        actor, "meta_tokens.refreshed", restaurant_id=restaurant_id, target="integration:meta",
        before={"ig_token_expires": restaurant.ig_token_expires, "fb_token_expires": restaurant.fb_token_expires},
        after={"refreshed": refreshed, "failed": errors},
        result="ok" if ok else ("partial" if refreshed else "failed"))
    out = {"ok": ok, "refreshed": {k: True for k in refreshed},
           "expires": refreshed.get("instagram"), "expires_on": mdy(refreshed.get("instagram") or "")}
    names = {"instagram": "Instagram", "facebook": "Facebook page"}
    if refreshed:
        out["message"] = "; ".join(f"{names[k]} token refreshed, good until {mdy(v)}" for k, v in refreshed.items()) + "."
    if errors:
        out["error"] = (" ".join(f"{names[k]} token NOT refreshed: {v}." for k, v in errors.items())
                        + (" " + out["message"] if refreshed else "")
                        + " Reconnect Instagram/Facebook from the client's account if it keeps failing.")
    return out



@admin_bp.route("/api/competitor-intel")
@login_required
def competitor_intel_api(current_user):
    """Get competitor intel for the current restaurant."""
    import json
    from models import get_restaurant
    restaurant = get_restaurant(current_user["restaurant_id"])
    if not restaurant or not restaurant.competitor_intel:
        return jsonify(ok=False, data=None)
    try:
        data = json.loads(restaurant.competitor_intel)
        return jsonify(ok=True, data=data,
                      updated_at=restaurant.competitor_updated_at,
                      **__import__("ai_guard").freshness(restaurant.competitor_updated_at))
    except Exception:
        return jsonify(ok=False, data=None)

# Tracked in ops.async_jobs (a table), not a module dict — see
# ops.start_async_job for why.
import ops as _ops

# Exception text handed to a client, with credentials stripped — a requests
# error carries the failing URL, and a Places URL carries key= in its query
# string. See ai_guard.safe_error.
from ai_guard import safe_error as _safe_err


def _run_competitor_job(job_id, restaurant_id):
    from competitor import run_competitor_analysis
    try:
        result = run_competitor_analysis(restaurant_id)
        _ops.finish_async_job(job_id, "done" if result.get("ok") else "error", result)
    except Exception as e:
        import traceback as _tb
        print(f"[competitor job] FAILED:\n{_tb.format_exc()}")
        _ops.finish_async_job(job_id, "error", {"ok": False, "error": str(e)})

@admin_bp.route("/api/refresh-competitor-intel", methods=["POST"])
@login_required
def refresh_competitor_intel(current_user):
    """Kick off async competitor analysis — returns immediately with a job_id to poll.
    The analysis itself (Google Places calls + Claude generation) can take 20-40s,
    which can exceed platform-level edge/proxy timeouts on a synchronous request."""
    # Require all 4 modules (Full System only)
    from models import get_restaurant as _gr
    _r = _gr(current_user["restaurant_id"])
    if not (_r and _r.module_reviews and _r.module_labor and _r.module_inventory and _r.module_marketing):
        return jsonify(ok=False, error="Competitor intelligence is available on the Full System plan only."), 403
    return jsonify(ok=True, job_id=start_competitor_job(current_user["restaurant_id"]))


def start_competitor_job(restaurant_id):
    """This restaurant's one running competitor refresh: start it, or join
    the one already pending. Every press used to start another background
    thread of paid Google Places + Claude calls (SEC-31 / DATA-29); the claim
    is checked and inserted in one write (ops.claim_async_job), the same way
    schedule generation joins a running job. Shared with the app's
    /mobile/api/intel/refresh-competitors."""
    import threading, uuid
    job_id, joined = _ops.claim_async_job(str(uuid.uuid4()), "competitor_intel", restaurant_id)
    if not joined:
        threading.Thread(target=_run_competitor_job, args=(job_id, restaurant_id), daemon=True).start()
    return job_id

@admin_bp.route("/api/competitor-intel-status/<job_id>", methods=["GET"])
@login_required
def competitor_intel_status(current_user, job_id):
    """Poll for competitor analysis result. Scoped to the caller's own
    restaurant — this used to be unauthenticated on the reasoning that the
    job id is an unguessable UUID, which is true but left a competitor
    report readable by anyone who saw the id."""
    job = _ops.read_async_job(job_id, restaurant_id=current_user["restaurant_id"])
    if not job:
        return jsonify({"ok": False, "status": "error", "error": "Job not found"}), 404
    if job["status"] == "pending":
        return jsonify({"ok": True, "status": "pending"})
    result = dict(job["result"])
    result["status"] = job["status"]
    return jsonify(result)

# Referrals one restaurant may send per rolling hour. Counted from email_log,
# so it holds across restarts and workers. The "10 per hour" limit here was
# only ever a comment (MOD-EML-1).
REFERRALS_PER_HOUR = 10


def _referrals_last_hour(restaurant_id) -> int:
    try:
        from datetime import datetime as _dt, timedelta as _td
        from zoneinfo import ZoneInfo as _ZI
        # email_log.sent_at is America/Chicago local (models.log_email).
        since = (_dt.now(_ZI("America/Chicago")).replace(tzinfo=None) - _td(hours=1)).strftime("%Y-%m-%d %H:%M:%S")
        conn = get_conn()
        try:
            return conn.execute("SELECT COUNT(*) FROM email_log WHERE restaurant_id=? AND email_type='referral' "
                                "AND sent_at >= ?", (restaurant_id, since)).fetchone()[0]
        finally:
            conn.close()
    except Exception:
        return REFERRALS_PER_HOUR          # fail closed: this mails strangers as Will


@admin_bp.route("/api/send-referral", methods=["POST"])
@login_required
def send_referral(current_user):
    """An owner introduces Cavnar AI to someone they know, from will@.

    It was an open relay: any login could mail any address, unlimited, as
    Will, with the note injected as raw HTML, through a direct SDK send that
    skipped the suppression list (MOD-EML-1). Now: account holders only, a
    real address, REFERRALS_PER_HOUR per restaurant, every field escaped,
    and through emails.deliver."""
    from permissions import is_principal
    from guest_email import valid_email
    esc = _emails.esc
    if not is_principal(current_user):
        return jsonify(ok=False, error="Only the account owner can send referrals."), 403
    data = request.get_json(silent=True) or {}
    ref_name  = (data.get("name") or "").strip()[:80]
    ref_email = valid_email(data.get("email"))
    note      = (data.get("note") or "").strip()[:500]
    if not ref_name or not ref_email:
        return jsonify(ok=False, error="Name and a valid email address are required")
    rid = current_user["restaurant_id"]
    if _referrals_last_hour(rid) >= REFERRALS_PER_HOUR:
        return jsonify(ok=False, error="That's a lot of referrals in an hour — thank you! Try again later."), 429
    try:
        restaurant = get_restaurant(rid)
        referrer   = restaurant.name if restaurant else "A Cavnar AI client"
        owner_name = (restaurant.owner_name if restaurant else None) or "Your colleague"
        subject_owner = owner_name
        note_block = (f"<p style=\"margin:0 0 16px 0;font-style:italic;color:#4a4540\">\"{esc(note)}\"</p>"
                      if note else "")
        html = f"""
<div style="font-family:-apple-system,BlinkMacSystemFont,'Helvetica Neue',Arial,sans-serif;max-width:540px;margin:0 auto;padding:32px 24px;background:#fdf8f4">
  <img src="https://dashboard.cavnar.ai/static/brand/wordmark-dark-email.png" width="170" height="30" alt="Cavnar AI" style="display:block;width:170px;height:30px;border:0;outline:none;margin-bottom:6px">
  <div style="font-size:10px;color:#7a736a;letter-spacing:.1em;text-transform:uppercase;margin-bottom:24px">Restaurant Intelligence</div>
  <p style="margin:0 0 16px 0;font-size:15px;color:#0e0c0a;line-height:1.7">Hi — {esc(owner_name)} from {esc(referrer)} thought you might find this useful.</p>
  {note_block}
  <p style="margin:0 0 16px 0;font-size:14px;color:#3a3530;line-height:1.7">Cavnar AI is a fully managed dashboard that handles the operational side of running a restaurant — review responses, labor cost analysis, inventory tracking, and marketing content. It runs quietly in the background and takes about 30 minutes a week of your time.</p>
  <p style="margin:0 0 24px 0;font-size:14px;color:#3a3530;line-height:1.7">If you want to see what it looks like for your restaurant, book a free 30-minute call below.</p>
  <a href="https://calendly.com/will-cavnar/30min" style="display:inline-block;background:#c84b2f;color:white;padding:12px 24px;border-radius:4px;text-decoration:none;font-size:13px;font-weight:600">Book a free call</a>
  <p style="margin:24px 0 0 0;font-size:12px;color:#7a736a">Will Cavnar · Cavnar AI · <a href="https://cavnar.ai" style="color:#c84b2f;text-decoration:none">cavnar.ai</a></p>
</div>"""
        result = _emails.deliver(email_type="referral", restaurant_id=rid, payload={
            "from": _emails.sender("will"),
            "to": [ref_email],
            "subject": f"{subject_owner} thinks you should check out Cavnar AI",
            "html": _html_doc(html),
        })
        if not result.ok:
            if str(result.error or "").startswith("recipient suppressed"):
                return jsonify(ok=False, error="That address has asked not to get email from us."), 409
            return jsonify(ok=False, error="The referral didn't go out. Try again in a minute."), 502
        # Tell Will.
        _emails.deliver(email_type="referral_notice", restaurant_id=rid, payload={
            "from": _emails.sender("client"),
            "to": [_from_email()],
            "subject": f"New referral from {referrer} — {ref_name}",
            "html": _html_doc(f"<p>{esc(referrer)} referred {esc(ref_name)} ({esc(ref_email)}).</p>"
                              f"<p>Note: {esc(note) or 'none'}</p>"),
        })
        return jsonify(ok=True)
    except Exception as e:
        return jsonify(ok=False, error=_safe_err(e))


# ── Changelog admin ──────────────────────────────────────────────────────────

@admin_bp.route("/admin/api/changelog", methods=["GET"])
@admin_required
def admin_get_changelog(current_user):
    return jsonify(ok=True, entries=get_changelog())

@admin_bp.route("/admin/api/changelog", methods=["POST"])
@admin_required
def admin_create_changelog(current_user):
    data = request.get_json(force=True) or {}
    title = (data.get("title") or "").strip()
    body  = (data.get("body") or "").strip()
    tag   = (data.get("tag") or "feature").strip()
    if not title:
        return jsonify(ok=False, error="title required"), 400
    entry_id = save_changelog_entry(title, body, tag)
    return jsonify(ok=True, id=entry_id)

@admin_bp.route("/admin/api/changelog/<int:entry_id>", methods=["DELETE"])
@admin_required
def admin_delete_changelog(entry_id, current_user):
    delete_changelog_entry(entry_id)
    return jsonify(ok=True)


# ── White-label branding admin ───────────────────────────────────────────────

@admin_bp.route("/admin/api/brand/<int:restaurant_id>", methods=["POST"])
@admin_required
def admin_set_brand(restaurant_id, current_user):
    data = request.get_json(force=True) or {}
    allowed = {"brand_name", "brand_color", "brand_logo_url", "category"}
    updates = {k: v for k, v in data.items() if k in allowed}
    if "category" in updates:
        from intelligence.categories import valid as _valid_category
        cat = (updates["category"] or "").strip().lower()
        if cat and not _valid_category(cat):
            return jsonify(ok=False, error="Unknown category"), 400
        updates["category"] = cat or None
    # The restaurant profile (Benchmarking audit #7) — the same closed
    # vocabularies and confirmation stamp as the owner's Account block.
    if data.get("service_model"):
        from intelligence.categories import clean_profile
        prof, err = clean_profile(data)
        if err:
            return jsonify(ok=False, error=err), 400
        updates.update(prof)
    if "exclude_from_learning" in data:
        updates["exclude_from_learning"] = 1 if data.get("exclude_from_learning") in (1, True, "1", "true", "on") else 0
    if not updates:
        return jsonify(ok=False, error="No valid fields"), 400
    update_restaurant(restaurant_id, updates)
    if "profile_source" in updates:
        import thresholds as _thr
        from models import get_restaurant as _gr
        seed = _thr.seeded_targets(_gr(restaurant_id))
        if seed:
            update_restaurant(restaurant_id, seed)
    return jsonify(ok=True)



# ── Admin console (templates/admin.html) — every read goes through admin_ops ─

@admin_bp.route("/admin/api/overview")
@admin_required
def admin_api_overview(current_user):
    import admin_ops
    return jsonify(**admin_ops.overview())


@admin_bp.route("/admin/api/intelligence")
@admin_required
def admin_api_intelligence(current_user):
    """The Intelligence page (INTELLIGENCE_ENGINE.md): platform learning,
    patterns, recommendation rates, Cavnar cohort bands, trends. Every figure
    is an aggregate; the payload is asserted anonymous before it leaves.
    is_admin only — admin_required also admits the support role, and a
    support login has no need for cohort statistics (Benchmarking audit #47,
    BM1-20)."""
    if not current_user.get("is_admin"):
        return jsonify(ok=False, error="Cavnar AI admins only."), 403
    from intelligence import dashboard as _dash
    return jsonify(**_dash.build())


@admin_bp.route("/admin/api/recommendations")
@admin_required
def admin_api_recommendations(current_user):
    """Recommendation Acceptance (internal only): the funnel from shown to
    improved, the score, and what moves it. ?days=30&restaurant_id=N."""
    import admin_ops
    try:
        days = int(request.args.get("days") or 30)
    except (TypeError, ValueError):
        days = 30
    try:
        rid = int(request.args.get("restaurant_id")) if request.args.get("restaurant_id") else None
    except (TypeError, ValueError):
        rid = None
    return jsonify(**admin_ops.recommendation_acceptance(days=days, restaurant_id=rid))


@admin_bp.route("/admin/api/schedule-experiments")
@admin_required
def admin_api_schedule_experiments(current_user):
    """Schedule generation experiments (internal only): per arm n, draft
    acceptance with a 90% interval, outcomes, and the verdict."""
    import admin_ops
    return jsonify(**admin_ops.schedule_experiments())


@admin_bp.route("/admin/api/schedule-experiments/pin", methods=["POST"])
@admin_required
def admin_api_schedule_experiment_pin(current_user):
    """Pin a restaurant to an arm ('off' stops the experiment there), or
    unpin it with arm null. {restaurant_id, experiment, arm}."""
    import admin_ops
    data = request.get_json(force=True, silent=True) or {}
    try:
        rid = int(data.get("restaurant_id") or 0)
    except (TypeError, ValueError):
        rid = 0
    if not rid:
        return jsonify(ok=False, error="Missing restaurant_id")
    arm = data.get("arm")
    arm = str(arm).strip() if arm not in (None, "") else None
    return jsonify(**admin_ops.set_schedule_experiment_pin(rid, str(data.get("experiment") or ""), arm,
                                                           current_user.get("username") or "admin"))


@admin_bp.route("/admin/api/schedule-experiments/promote", methods=["POST"])
@admin_required
def admin_api_schedule_experiment_promote(current_user):
    """Make the readout's winning arm every restaurant's default — the
    reviewed step (ROI #46), recorded with who did it. {experiment, arm,
    note?}. Refused unless the verdict calls that arm now."""
    import admin_ops
    data = request.get_json(force=True, silent=True) or {}
    return jsonify(**admin_ops.promote_schedule_experiment(
        str(data.get("experiment") or ""), str(data.get("arm") or ""),
        by=current_user.get("username") or "admin", note=data.get("note")))


@admin_bp.route("/admin/api/schedule-experiments/revert", methods=["POST"])
@admin_required
def admin_api_schedule_experiment_revert(current_user):
    """End a promotion: the experiment randomises again. {experiment}."""
    import admin_ops
    data = request.get_json(force=True, silent=True) or {}
    return jsonify(**admin_ops.revert_schedule_experiment(str(data.get("experiment") or ""),
                                                          by=current_user.get("username") or "admin"))


def _admin_days_rid(default_days):
    try:
        days = int(request.args.get("days") or default_days)
    except (TypeError, ValueError):
        days = default_days
    try:
        rid = int(request.args.get("restaurant_id")) if request.args.get("restaurant_id") else None
    except (TypeError, ValueError):
        rid = None
    return days, rid


@admin_bp.route("/admin/api/recommendations/calibration")
@admin_required
def admin_api_recommendation_calibration(current_user):
    """Predicted vs measured dollars by recommendation kind (ROI #43,
    internal only). ?days=365&restaurant_id=N."""
    import admin_ops
    days, rid = _admin_days_rid(365)
    return jsonify(**admin_ops.recommendation_calibration(days=days, restaurant_id=rid))


@admin_bp.route("/admin/api/calibration")
@admin_required
def admin_api_confidence_calibration(current_user):
    """Stated Recommendation Confidence against what happened (contract K7,
    internal only): reliability by decile, Brier, per kind and per
    dimension, each withheld below its floor. ?days=365&restaurant_id=N."""
    import admin_ops
    days, rid = _admin_days_rid(365)
    return jsonify(**admin_ops.confidence_calibration(days=days, restaurant_id=rid))


@admin_bp.route("/admin/api/recommendations/missed")
@admin_required
def admin_api_missed_detections(current_user):
    """Problems that surfaced with no recommendation before them (ROI #44,
    internal only). ?days=30&restaurant_id=N."""
    import admin_ops
    days, rid = _admin_days_rid(30)
    return jsonify(**admin_ops.missed_detections(days=days, restaurant_id=rid))


@admin_bp.route("/admin/api/clients")
@admin_required
def admin_api_clients(current_user):
    import admin_ops
    return jsonify(**admin_ops.clients())


@admin_bp.route("/admin/api/client/<int:restaurant_id>")
@admin_required
def admin_api_client(restaurant_id, current_user):
    import admin_ops
    payload = admin_ops.client_detail(restaurant_id)
    return jsonify(**payload), (200 if payload.get("ok") else 404)


@admin_bp.route("/admin/api/integrations")
@admin_required
def admin_api_integrations(current_user):
    import admin_ops
    return jsonify(**admin_ops.integrations())


@admin_bp.route("/admin/api/ai")
@admin_required
def admin_api_ai(current_user):
    import admin_ops
    days = request.args.get("days", 30, type=int)
    return jsonify(**admin_ops.ai_ops(days=days if days in (1, 7, 30, 90) else 30))


@admin_bp.route("/admin/api/validation")
@admin_required
def admin_api_validation(current_user):
    """Response Validation Layer catch rates by surface × rule (internal
    only). ?days=1|7|30|90, default 30."""
    import admin_ops
    days = request.args.get("days", 30, type=int)
    return jsonify(**admin_ops.validation_rates(days=days if days in (1, 7, 30, 90) else 30))


@admin_bp.route("/admin/api/emails")
@admin_required
def admin_api_emails(current_user):
    import admin_ops
    return jsonify(**admin_ops.emails())


@admin_bp.route("/admin/api/notifications")
@admin_required
def admin_api_notifications(current_user):
    import admin_ops
    return jsonify(**admin_ops.notifications())


@admin_bp.route("/admin/api/billing")
@admin_required
def admin_api_billing(current_user):
    import admin_ops
    return jsonify(**admin_ops.billing())


@admin_bp.route("/admin/api/jobs")
@admin_required
def admin_api_jobs(current_user):
    import admin_ops
    return jsonify(**admin_ops.jobs())


@admin_bp.route("/admin/api/jobs/<job>/run", methods=["POST"])
@admin_required
def admin_api_job_run(job, current_user):
    import admin_ops
    out = admin_ops.run_job_now(job, current_user.get("username") or "admin")
    return jsonify(**out), (200 if out.get("ok") else (404 if out.get("error") == "Unknown job" else 409))


@admin_bp.route("/admin/api/client/<int:restaurant_id>/alert-cap", methods=["POST"])
@admin_required
def admin_api_alert_cap(restaurant_id, current_user):
    import admin_ops
    data = request.get_json(silent=True) or {}
    out = admin_ops.set_alert_cap(restaurant_id, data.get("max_per_day", 0), current_user.get("username") or "admin")
    return jsonify(**out), (200 if out.get("ok") else (404 if out.get("error") == "Not found" else 400))


@admin_bp.route("/admin/api/client/<int:restaurant_id>/demo", methods=["POST"])
@admin_required
def admin_api_set_demo(restaurant_id, current_user):
    """The is_demo flag decides whether boot-time seeding may wipe this
    restaurant's reviews and labor history, and it makes the account
    deletable by delete-demo. Nothing else in the console can change it.

    Turning it ON is guarded here on the server (#118): refused for an
    account with a Stripe customer, a signed contract, a paying billing
    status or an admin login living on it, and it needs the restaurant's
    exact name typed as confirm_name. One confirm() used to be all that
    stood between a paying client and the demo reseed. The before-state is
    recorded with the change."""
    from models import get_restaurant, update_restaurant
    import offboarding
    data = request.get_json(silent=True) or {}
    on = 1 if data.get("is_demo") else 0
    current = get_restaurant(restaurant_id)
    if not current:
        return jsonify(ok=False, error="Not found"), 404
    was = int(getattr(current, "is_demo", 0) or 0)
    before = {"is_demo": was, "billing_status": current.billing_status,
              "contract_status": getattr(current, "contract_status", None),
              "stripe_customer": bool((current.stripe_customer_id or "").strip())}
    if on and not was:
        reasons = []
        if (current.stripe_customer_id or "").strip():
            reasons.append("it has a Stripe customer")
        if (getattr(current, "contract_status", "") or "") == "signed":
            reasons.append("its contract is signed")
        if (current.billing_status or "") in _PAYING_STATUSES:
            reasons.append(f"its billing status is {current.billing_status}")
        if offboarding.admin_homes(restaurant_id):
            reasons.append("an admin login lives on it")
        import admin_events
        if reasons:
            admin_events.record_admin_action(current_user, "demo_flag.set", restaurant_id=restaurant_id,
                                             target=f"restaurant:{restaurant_id}", before=before,
                                             after={"is_demo": on, "refused": reasons}, result="refused")
            return jsonify(ok=False, error=f"{current.name} can't be marked as a demo: " + "; ".join(reasons)
                                           + ". A demo can be wiped by the boot seed and deleted outright, so "
                                             "only an account with no billing history can be one."), 409
        if (data.get("confirm_name") or "").strip() != (current.name or "").strip():
            return jsonify(ok=False, confirm_required=True,
                           error="Type the restaurant's exact name to mark it as a demo."), 400
    fields = {"is_demo": on}
    if not on and int(getattr(current, "is_demo", 0) or 0) == 1:
        # Turning demo OFF leaves the seeded rows (rr_% reviews, seeded
        # shifts, ingredients, labor history) in place — never hard-deleted
        # here — and TAGS the restaurant: demo_cleared_at keeps it out of
        # cross-restaurant learning until every feature window has rolled
        # past the seeded history (intelligence.jobs.real_restaurant_ids,
        # CA3 F7). Synthetic history must not become a real baseline for
        # everyone else.
        from time_utils import utc_stamp
        fields["demo_cleared_at"] = utc_stamp()
    update_restaurant(restaurant_id, fields)
    import admin_events
    admin_events.record_admin_action(current_user, "demo_flag.set", restaurant_id=restaurant_id,
                                     target=f"restaurant:{restaurant_id}", before=before, after=fields,
                                     summary=f"Marked as {'demo' if on else 'real client'} by "
                                             f"{current_user.get('username')}")
    return jsonify(ok=True, restaurant_id=restaurant_id, is_demo=on)


# Billing states that mean a client is (or was just) paying: never a demo.
_PAYING_STATUSES = ("active", "past_due", "paused")


@admin_bp.route("/admin/api/client/<int:restaurant_id>/delete-demo", methods=["POST"])
@admin_required
def admin_api_delete_demo(restaurant_id, current_user):
    """Permanently delete a DEMO account and every row that belongs to it
    (models.delete_restaurant). Two gates, both server-side: the restaurant
    must be flagged is_demo — a real client has to be marked as a demo first,
    which is its own confirmed step — and the request must carry the
    restaurant's exact name. Added to retire the Gia Mia demo (9/25/26)."""
    from models import get_restaurant, delete_restaurant
    import offboarding
    data = request.get_json(silent=True) or {}
    current = get_restaurant(restaurant_id)
    if not current:
        return jsonify(ok=False, error="Not found"), 404
    if int(getattr(current, "is_demo", 0) or 0) != 1:
        return jsonify(ok=False, error="Only a restaurant flagged as a demo can be deleted here."), 400
    if (data.get("confirm_name") or "").strip() != (current.name or "").strip():
        return jsonify(ok=False, error="The name did not match. Nothing was deleted."), 400
    if offboarding.admin_homes(restaurant_id):
        return jsonify(ok=False, error="An admin login lives on this restaurant; deleting it would delete that "
                                       "login. Nothing was deleted."), 409
    try:
        deleted = delete_restaurant(restaurant_id)
    except Exception as e:
        return jsonify(ok=False, error=_safe_err(e)), 500
    import admin_events
    # The row survives the delete (models._KEEP_ON_RESTAURANT_DELETE), so
    # the record of what happened stays attached to the id (#126).
    admin_events.record_admin_action(current_user, "demo.deleted", restaurant_id=restaurant_id,
                                     target=f"restaurant:{restaurant_id}",
                                     before={"name": current.name, "is_demo": 1},
                                     after={"rows": sum(deleted.values()), "tables": deleted},
                                     summary=f"Demo account {current.name} (#{restaurant_id}) deleted by "
                                             f"{current_user.get('username')}")
    return jsonify(ok=True, restaurant_id=restaurant_id, rows=sum(deleted.values()), tables=deleted)


@admin_bp.route("/admin/api/events")
@admin_required
def admin_api_events(current_user):
    import admin_events
    rid = request.args.get("restaurant_id", type=int)
    return jsonify(ok=True, events=admin_events.recent(limit=min(request.args.get("limit", 100, type=int), 500), restaurant_id=rid))


@admin_bp.route("/admin/api/issues")
@admin_required
def admin_api_issues(current_user):
    import admin_ops
    out = admin_ops.issues()
    out["resolved"] = admin_ops.resolved_issues()["resolved"]
    return jsonify(**out)


@admin_bp.route("/admin/api/issues/resolve", methods=["POST"])
@admin_required
def admin_api_issue_resolve(current_user):
    import admin_ops
    data = request.get_json(silent=True) or {}
    key = (data.get("key") or "").strip()
    if not key:
        return jsonify(ok=False, error="Missing key"), 400
    if data.get("undo"):
        return jsonify(**admin_ops.unresolve_issue(key))
    return jsonify(**admin_ops.resolve_issue(key, (data.get("note") or "")[:300], current_user.get("username")))


@admin_bp.route("/admin/api/activity")
@admin_required
def admin_api_activity(current_user):
    import admin_ops
    return jsonify(**admin_ops.activity(limit=request.args.get("limit", 80, type=int)))


@admin_bp.route("/admin/api/search")
@admin_required
def admin_api_search(current_user):
    import admin_ops
    return jsonify(**admin_ops.search(request.args.get("q", "")))


# ── Fix round A ── auth & sessions: step-up, admin two-factor, login support
# actions, support logins (9/29/26). The sensitive ones carry
# @recent_auth_required (owner decision 4): the console answers a
# {reauth_required: true} 403 by asking for the password, POSTing
# /admin/api/reauth, and sending the action again.

REAUTH_MAX_MISSES = 5


@admin_bp.route("/admin/api/reauth", methods=["POST"])
@admin_required
def admin_api_reauth(current_user):
    """Step-up: the admin types their password again; this session may then
    make sensitive changes for auth.RECENT_AUTH_MINUTES. Five wrong
    passwords on one session in 15 minutes end the session — a stolen
    cookie cannot guess its way to the dangerous actions."""
    import auth as _auth_ra
    import security as _sec_ra
    data = request.get_json(silent=True) or {}
    token = _auth_ra.current_session_token()
    if not token:
        return jsonify(ok=False, error="Your session expired — please log in again.", session_expired=True), 401
    key = "reauth:" + _auth_ra.hash_session_token(token)[:24]
    if not _auth_ra.password_matches(current_user["id"], data.get("password") or ""):
        misses = _sec_ra.record_reauth_miss(key, request.remote_addr)
        _audit_admin_action(current_user, "admin_reauth_failed", result="denied",
                            summary=f"{current_user.get('username')} entered a wrong password at a step-up "
                                    f"({misses} in 15 minutes)")
        if misses >= REAUTH_MAX_MISSES:
            _auth_ra.delete_session(token)
            return jsonify(ok=False, session_expired=True,
                           error="Too many wrong passwords — sign in again."), 401
        return jsonify(ok=False, error="That password isn't right."), 403
    _sec_ra.clear_reauth_misses(key)
    _auth_ra.mark_reauthenticated(token)
    return jsonify(ok=True, valid_minutes=_auth_ra.RECENT_AUTH_MINUTES)


# ── an internal login's own two-factor (SECURITY-1) ─────────────────────────

# two_fa_challenges.purpose for an internal login's enrolment code, per
# channel ("admin-setup-email" / "admin-setup-sms").
_ADMIN_SETUP_PURPOSE = "admin-setup-%s"


def _masked_contacts(user):
    from auth import _mask_email, _mask_phone
    email = (user.get("email") or "").strip()
    phone = (user.get("phone") or "").strip()
    return (_mask_email(email) if "@" in email else None), (_mask_phone(phone) if phone else None)


@admin_bp.route("/admin/two-factor")
@admin_required
def admin_two_factor_page(current_user):
    """Where an admin or support login turns its own two-factor on (and sees
    it). Reachable while ADMIN_REQUIRE_2FA holds the rest of the console
    shut — the old page sent admins to "/", which redirects them straight
    back to /admin."""
    import auth as _auth_tf
    from auth_routes import safe_next_url
    email_m, phone_m = _masked_contacts(current_user)
    return render_template(
        "admin_two_factor.html",
        enrolled=_auth_tf.user_two_factor_enrolled(current_user),
        method=current_user.get("two_fa_method") or "email",
        required=_auth_tf.admin_two_factor_required(),
        email_masked=email_m, phone_masked=phone_m,
        backup_left=_auth_tf.count_unused_user_backup_codes(current_user["id"]),
        next_url=safe_next_url(request.args.get("next"), "/admin"),
        username=current_user.get("username"),
        message=None)


@admin_bp.route("/admin/two-factor/send", methods=["POST"])
@admin_required
def admin_two_factor_send(current_user):
    """Send the enrolment code to this login's own email (or phone). Only
    to the contact already on the login — never an address typed here."""
    import auth as _auth_tf
    if _auth_tf.user_two_factor_enrolled(current_user):
        return jsonify(ok=False, error="Two-factor is already on for your login."), 409
    data = request.get_json(silent=True) or {}
    method = "sms" if data.get("method") == "sms" else "email"
    dest = _auth_tf.two_fa_destination(dict(current_user, two_fa_method=method), None, method=method, strict=True)
    if not dest:
        return jsonify(ok=False, error=("There's no phone number on your login — use email." if method == "sms"
                                        else "There's no email address on your login.")), 400
    conn = get_conn()
    try:
        recent = conn.execute(
            "SELECT 1 FROM two_fa_challenges WHERE user_id=? AND purpose LIKE 'admin-setup-%' "
            "AND created_at > datetime('now','-60 seconds') LIMIT 1", (current_user["id"],)).fetchone()
    finally:
        conn.close()
    if recent:
        return jsonify(ok=False, error="A code was just sent. Use that one, or wait a minute to send another."), 429
    # The challenge is bound to the channel it went by, so the confirm below
    # can only switch on a channel this login proved it receives.
    purpose = _ADMIN_SETUP_PURPOSE % dest["kind"]
    _pending, code = _auth_tf.issue_two_fa_challenge(current_user["restaurant_id"], current_user["id"], purpose)
    try:
        sent = _auth_tf.send_two_fa_code(dest, _auth_tf._code_label(current_user, None), code)
    except Exception as e:
        print(f"[admin 2fa] enrolment code send failed for user {current_user['id']}: {e}")
        sent = False
    if not sent:
        conn = get_conn()
        try:
            conn.execute("DELETE FROM two_fa_challenges WHERE user_id=? AND purpose=?", (current_user["id"], purpose))
            conn.commit()
        finally:
            conn.close()
        where = "text" if dest["kind"] == "sms" else "email"
        return jsonify(ok=False, error=f"We couldn't {where} the code just now. Try again in a minute."), 502
    return jsonify(ok=True, masked=dest["masked"], method=dest["kind"])


@admin_bp.route("/admin/two-factor/verify", methods=["POST"])
@admin_required
def admin_two_factor_verify(current_user):
    """Confirm the enrolment code: the login's two-factor goes on, its backup
    codes are shown once, and this session counts as having passed the
    second factor (it just did)."""
    import auth as _auth_tf
    if _auth_tf.user_two_factor_enrolled(current_user):
        return jsonify(ok=False, error="Two-factor is already on for your login."), 409
    data = request.get_json(silent=True) or {}
    method = "sms" if data.get("method") == "sms" else "email"
    result = _auth_tf.check_two_fa_code(current_user["restaurant_id"], current_user["id"],
                                        (data.get("code") or "").strip(), purpose=_ADMIN_SETUP_PURPOSE % method)
    if result in ("wrong", "missing"):
        return jsonify(ok=False, error="That code isn't right. Try again."), 400
    if result == "expired":
        return jsonify(ok=False, error="That code expired. Send a new one."), 400
    conn = get_conn()
    try:
        conn.execute("UPDATE users SET two_fa_enabled=1, two_fa_method=? WHERE id=?", (method, current_user["id"]))
        conn.commit()
    finally:
        conn.close()
    codes = _auth_tf.generate_user_backup_codes(current_user["id"])
    _auth_tf.mark_second_factor(_auth_tf.current_session_token())
    _audit_admin_action(current_user, "admin_two_factor_enabled", target={"user_id": current_user["id"]},
                        before={"two_fa_enabled": False}, after={"two_fa_enabled": True, "method": method},
                        summary=f"{current_user.get('username')} turned on two-factor ({method})")
    from auth_routes import safe_next_url
    return jsonify(ok=True, backup_codes=codes, method=method,
                   next=safe_next_url(data.get("next"), "/admin"))


@admin_bp.route("/admin/two-factor/disable", methods=["POST"])
@admin_required
@recent_auth_required()
def admin_two_factor_disable(current_user):
    """Turn this login's own two-factor off. Refused while ADMIN_REQUIRE_2FA
    is set (the login would be sent straight back to enrolment)."""
    import auth as _auth_tf
    if _auth_tf.admin_two_factor_required():
        return jsonify(ok=False, error="Two-factor is required for admin logins here, so it can't be turned off."), 409
    _clear_user_two_factor(current_user["id"])
    _audit_admin_action(current_user, "admin_two_factor_disabled", target={"user_id": current_user["id"]},
                        before={"two_fa_enabled": True}, after={"two_fa_enabled": False},
                        summary=f"{current_user.get('username')} turned off their own two-factor")
    return jsonify(ok=True)


@admin_bp.route("/admin/two-factor/backup-codes", methods=["POST"])
@admin_required
@recent_auth_required()
def admin_two_factor_backup_codes(current_user):
    """A fresh set of this login's backup codes (the old ones stop working)."""
    import auth as _auth_tf
    if not _auth_tf.user_two_factor_enrolled(current_user):
        return jsonify(ok=False, error="Turn two-factor on first."), 409
    codes = _auth_tf.generate_user_backup_codes(current_user["id"])
    _audit_admin_action(current_user, "admin_backup_codes_regenerated", target={"user_id": current_user["id"]},
                        summary=f"{current_user.get('username')} made new two-factor backup codes")
    return jsonify(ok=True, backup_codes=codes)


@admin_bp.route("/admin/api/me/username", methods=["POST"])
@admin_required
@recent_auth_required()
def admin_api_change_own_username(current_user):
    """Move this admin login off a guessable sign-in name (SECURITY-12): the
    seed's default is "will", and the sign-in lock is keyed on the name
    typed. Safe now that the boot seed only runs when no admin exists at
    all, whatever ADMIN_USERNAME says. Step-up; audited."""
    import re as _re_un
    data = request.get_json(silent=True) or {}
    new = (data.get("username") or "").strip().lower()
    if not _re_un.fullmatch(r"[a-z0-9._-]{3,30}", new):
        return jsonify(ok=False, error="Username: 3–30 letters, numbers, dots, dashes or underscores."), 400
    old = (current_user.get("username") or "").lower()
    if new == old:
        return jsonify(ok=True, username=new)
    conn = get_conn()
    try:
        if conn.execute("SELECT 1 FROM users WHERE LOWER(username)=? AND id<>?", (new, current_user["id"])).fetchone():
            return jsonify(ok=False, error="That username is taken."), 409
        conn.execute("UPDATE users SET username=? WHERE id=?", (new, current_user["id"]))
        conn.commit()
    finally:
        conn.close()
    _audit_admin_action(current_user, "admin_username_changed", target={"user_id": current_user["id"]},
                        before={"username": old}, after={"username": new},
                        summary=f"{old} changed their sign-in name to {new}")
    return jsonify(ok=True, username=new)


def _clear_user_two_factor(user_id):
    """An internal login's own second factor off (auth.clear_user_two_factor:
    the flag, its backup codes and the devices it remembered)."""
    import auth as _auth_cl
    _auth_cl.clear_user_two_factor(user_id)


# ── one login's access, for Access & activity (#55) ─────────────────────────

@admin_bp.route("/admin/api/users/<int:user_id>/security")
@admin_required
def admin_api_user_security(user_id, current_user):
    """One login's sign-in security as the console shows it: its two-factor
    (the login's own for an internal login, else its restaurant's switch),
    any lockout, its live sessions (each with a session_id the revoke route
    takes) and its remembered devices. IPs and user agents are withheld
    from a support login."""
    import auth as _auth_us
    import security as _sec_us
    row = _login_row(user_id)
    if not row:
        return jsonify(ok=False, error="That login wasn't found."), 404
    conn = get_conn()
    try:
        full = dict(conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone())
        devices = conn.execute("SELECT COUNT(*) FROM trusted_devices WHERE user_id=? AND datetime(expires_at) > "
                               "datetime('now')", (user_id,)).fetchone()[0]
    finally:
        conn.close()
    internal = _auth_us.is_internal_login(full)
    if internal:
        two = {"scope": "login", "enabled": _auth_us.user_two_factor_enrolled(full),
               "method": full.get("two_fa_method") or None,
               "backup_codes_left": _auth_us.count_unused_user_backup_codes(user_id)}
    else:
        r = get_restaurant(row["restaurant_id"])
        from models import count_unused_backup_codes
        two = {"scope": "restaurant", "enabled": bool(r and r.two_fa_enabled),
               "method": (getattr(r, "two_fa_method", None) or "email") if r else None,
               "backup_codes_left": count_unused_backup_codes(row["restaurant_id"])}
    names = {n for n in ((full.get("username") or "").lower(), (full.get("email") or "").lower()) if n}
    lock = {"locked": False, "seconds_left": 0, "failures_15m": 0, "failures_24h": 0, "addresses": []}
    for n in names:
        st = _sec_us.lockout_state(n, internal=internal)
        if st["locked"] or st["failures_24h"] > lock["failures_24h"]:
            lock = st
    support = _auth_us.current_admin_role() == "support"
    sessions = []
    for s in _auth_us.get_sessions_for_user(user_id):
        if support:
            s.pop("ip_address", None)
            s.pop("user_agent", None)
        sessions.append(s)
    return jsonify(ok=True, user={"id": user_id, "username": row["username"], "role": row["role"],
                                  "is_active": bool(row["is_active"]), "internal": internal,
                                  "must_reset_password": bool(full.get("must_reset_password"))},
                   two_factor=two, lockout=lock, sessions=sessions, trusted_devices=devices)


@admin_bp.route("/admin/api/users/<int:user_id>/reset-2fa", methods=["POST"])
@admin_required
@recent_auth_required()
def admin_api_reset_two_factor(user_id, current_user):
    """Two-factor reset for a login that lost its phone (#55). An internal
    login: its own second factor, backup codes and remembered devices.
    A restaurant's login: two-factor is one switch for the whole restaurant,
    so it is turned off there (the owner can turn it back on), with that
    restaurant's backup codes and remembered devices — the response says
    scope "restaurant" so the console can say so. Audited; the owner's
    Account activity shows it."""
    import auth as _auth_rt
    row = _login_row(user_id)
    if not row:
        return jsonify(ok=False, error="That login wasn't found."), 404
    conn = get_conn()
    try:
        full = dict(conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone())
    finally:
        conn.close()
    if _auth_rt.is_internal_login(full):
        before = {"two_fa_enabled": _auth_rt.user_two_factor_enrolled(full), "method": full.get("two_fa_method")}
        _clear_user_two_factor(user_id)
        scope = "login"
    else:
        r = get_restaurant(row["restaurant_id"])
        before = {"two_fa_enabled": bool(r and r.two_fa_enabled), "method": getattr(r, "two_fa_method", None)}
        update_restaurant(row["restaurant_id"], {"two_fa_enabled": 0})
        conn = get_conn()
        try:
            conn.execute("DELETE FROM two_fa_backup_codes WHERE restaurant_id=?", (row["restaurant_id"],))
            conn.commit()
        except Exception:
            pass
        finally:
            conn.close()
        _auth_rt.revoke_all_trusted_devices(row["restaurant_id"])
        try:
            from models import log_event
            log_event(row["restaurant_id"], "two_fa_disabled",
                      {"actor": "Cavnar AI support", "detail": f"reset for {row['username']}"})
        except Exception:
            pass
        scope = "restaurant"
    _audit_admin_action(current_user, "two_factor_reset", restaurant_id=row["restaurant_id"],
                        target={"user_id": user_id, "username": row["username"], "scope": scope},
                        before=before, after={"two_fa_enabled": False},
                        summary=f"{current_user.get('username')} reset two-factor for {row['username']} ({scope})")
    return jsonify(ok=True, scope=scope)


@admin_bp.route("/admin/api/lockouts")
@admin_required
def admin_api_lockouts(current_user):
    """Every account the sign-in throttle is holding right now."""
    import security as _sec_lo
    import auth as _auth_lo
    rows = _sec_lo.active_lockouts()
    if _auth_lo.current_admin_role() == "support":
        for r in rows:
            for a in r.get("addresses") or []:
                a.pop("ip", None)
    return jsonify(ok=True, lockouts=rows)


@admin_bp.route("/admin/api/users/<int:user_id>/clear-lockout", methods=["POST"])
@admin_required
@recent_auth_required()
def admin_api_clear_lockout(user_id, current_user):
    """Lift a sign-in lockout on one login (#55): its account key and every
    per-address lock, under its username and its email."""
    import security as _sec_cl
    row = _login_row(user_id)
    if not row:
        return jsonify(ok=False, error="That login wasn't found."), 404
    before = _sec_cl.lockout_state(row["username"])
    removed = 0
    for n in {(row.get("username") or "").lower(), (row.get("email") or "").lower()}:
        if n:
            removed += _sec_cl.clear_account_lock(n)
    _audit_admin_action(current_user, "lockout_cleared", restaurant_id=row["restaurant_id"],
                        target={"user_id": user_id, "username": row["username"]},
                        before={"locked": before["locked"], "failures_24h": before["failures_24h"]},
                        after={"locked": False},
                        summary=f"{current_user.get('username')} cleared the sign-in lockout on {row['username']}")
    return jsonify(ok=True, cleared=removed)


@admin_bp.route("/admin/api/users/<int:user_id>/sessions/<session_id>/revoke", methods=["POST"])
@admin_required
@recent_auth_required()
def admin_api_revoke_session(user_id, session_id, current_user):
    """Sign one session of one login out (#55). session_id is the handle the
    security view lists (auth.session_handle)."""
    import re as _re_rs
    row = _login_row(user_id)
    if not row:
        return jsonify(ok=False, error="That login wasn't found."), 404
    if not _re_rs.fullmatch(r"[0-9a-f]{16}", session_id or ""):
        return jsonify(ok=False, error="That session wasn't found."), 404
    conn = get_conn()
    try:
        n = conn.execute("DELETE FROM sessions WHERE user_id=? AND substr(token, 1, 16)=?",
                         (user_id, session_id)).rowcount or 0
        conn.commit()
    finally:
        conn.close()
    if not n:
        return jsonify(ok=False, error="That session has already ended."), 404
    _audit_admin_action(current_user, "session_revoked", restaurant_id=row["restaurant_id"],
                        target={"user_id": user_id, "username": row["username"], "session_id": session_id},
                        summary=f"{current_user.get('username')} signed out one session of {row['username']}")
    return jsonify(ok=True)


@admin_bp.route("/admin/api/users/<int:user_id>/revoke-sessions", methods=["POST"])
@admin_required
@recent_auth_required()
def admin_api_revoke_all_sessions(user_id, current_user):
    """Sign one login out everywhere and forget its remembered devices,
    leaving it active (#55)."""
    import auth as _auth_ras
    row = _login_row(user_id)
    if not row:
        return jsonify(ok=False, error="That login wasn't found."), 404
    if row["is_admin"] and user_id == current_user.get("id"):
        return jsonify(ok=False, error="Sign yourself out from the menu instead."), 400
    ended = _auth_ras.end_login_access(user_id)
    _audit_admin_action(current_user, "sessions_revoked", restaurant_id=row["restaurant_id"],
                        target={"user_id": user_id, "username": row["username"]}, after=ended,
                        summary=f"{current_user.get('username')} signed {row['username']} out everywhere "
                                f"({ended['sessions']} session(s))")
    return jsonify(ok=True, sessions_ended=ended["sessions"], devices_forgotten=ended["devices"])


# ── support logins (#99) ──────────────────────────────────────────────────────

@admin_bp.route("/admin/api/support-logins")
@admin_required
def admin_api_support_logins(current_user):
    """Cavnar AI's read-only support logins."""
    conn = get_conn()
    try:
        rows = conn.execute("SELECT id, username, email, is_active, last_login, created_at, two_fa_enabled "
                            "FROM users WHERE LOWER(COALESCE(role,''))='support' AND is_admin=0 "
                            "ORDER BY id").fetchall()
    finally:
        conn.close()
    return jsonify(ok=True, logins=[dict(r, two_fa_enabled=bool(r["two_fa_enabled"]),
                                         is_active=bool(r["is_active"])) for r in rows])


@admin_bp.route("/admin/api/support-logins", methods=["POST"])
@admin_required
@recent_auth_required()
def admin_api_create_support_login(current_user):
    """Provision a read-only support login from the console (#99). Nothing
    could create one — the only way to give a colleague access was
    is_admin=1, which is everything. It is homed on the admin's own internal
    row, gets a random password nobody sees, and its owner sets their own
    from an emailed reset link. Deactivate it like any other login."""
    import re as _re_sl
    import secrets as _sec_sl
    import auth as _auth_sl
    if not current_user.get("is_admin"):
        return jsonify(ok=False, error="Only an admin can add support logins."), 403
    data = request.get_json(silent=True) or {}
    username = (data.get("username") or "").strip().lower()
    email = (data.get("email") or "").strip().lower()
    if not _re_sl.fullmatch(r"[a-z0-9._-]{3,30}", username):
        return jsonify(ok=False, error="Username: 3–30 letters, numbers, dots, dashes or underscores."), 400
    if not _re_sl.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", email):
        return jsonify(ok=False, error="Enter a valid email address."), 400
    conn = get_conn()
    try:
        taken = conn.execute("SELECT 1 FROM users WHERE LOWER(username)=? OR LOWER(email)=?",
                             (username, email)).fetchone()
    finally:
        conn.close()
    if taken:
        return jsonify(ok=False, error="That username or email is already in use."), 409
    home = current_user.get("base_restaurant_id") or current_user.get("restaurant_id")
    try:
        uid = _auth_sl.create_user(home, username, email, _sec_sl.token_urlsafe(24), role="support",
                                   generated=True)
    except Exception as e:
        return jsonify(ok=False, error=_safe_err(e)), 400
    try:
        _auth_sl.upsert_membership(uid, home, "support")
    except Exception:
        pass
    sent, note = False, None
    try:
        import scheduler as _sched_sl
        from models import create_reset_token
        if not _sched_sl.scheduling_allowed():
            note = "Not emailed: this backend does not send (local). Send a reset link from production."
        else:
            tok = create_reset_token(email)
            if tok:
                from emails import send_password_reset_email
                sent = bool(send_password_reset_email(email, f"{config.base_url()}/reset-password/{tok}"))
                if not sent:
                    note = "The reset-link email didn't go out — send one from the login's row."
    except Exception as e:
        note = "The reset-link email didn't go out — send one from the login's row."
        print(f"[support login] reset link failed for {username}: {e}")
    _audit_admin_action(current_user, "support_login_created", restaurant_id=home,
                        target={"user_id": uid, "username": username}, after={"role": "support"},
                        summary=f"{current_user.get('username')} added support login {username}")
    return jsonify(ok=True, user_id=uid, reset_link_sent=sent, note=note)


# ── Fix round H ──────────────────────────────────────────────────────────────
# The billing lifecycle's console endpoints (workstream H). Every write here
# is audited (_billing_audit) and records who moved what in
# billing_status_history (models.billing_context). Anything that emails a
# client, or changes a live Stripe subscription, only happens where the
# scheduler may run (billing_jobs._sending_allowed): a local backend holds
# production's keys and must never act on a real client.

_PRORATIONS = ("create_prorations", "none", "always_invoice")
_PLAN_MODULES = ("reviews", "labor", "inventory", "marketing")


def _live_actions_refused():
    import billing_jobs
    if billing_jobs._sending_allowed():
        return None
    return jsonify(ok=False, error=(
        "Refused: this server is not the production server, so it does not email clients or change "
        "live Stripe subscriptions.")), 409


def _billing_detail(restaurant_id):
    import billing_jobs
    import models as _mdl
    r = get_restaurant(restaurant_id)
    if not r:
        return None
    payer = billing_jobs.billed_by(restaurant_id)
    mirror = billing_jobs.mirror_row(restaurant_id)
    if mirror:
        mirror["billed_mrr"] = billing_jobs.billed_mrr(mirror)
        try:
            mirror["module_mismatch"] = json.loads(mirror["module_mismatch"]) if mirror.get("module_mismatch") else None
        except Exception:
            pass
    conn = get_conn()
    try:
        envs = [dict(e) for e in conn.execute(
            "SELECT * FROM docusign_envelopes WHERE restaurant_id=? ORDER BY sent_at DESC LIMIT 10",
            (restaurant_id,))]
        rec = conn.execute("SELECT * FROM billing_reconcile WHERE restaurant_id=?", (restaurant_id,)).fetchone()
    finally:
        conn.close()
    rec = dict(rec) if rec else None
    if rec and rec.get("mismatches"):
        try:
            rec["mismatches"] = json.loads(rec["mismatches"])
        except Exception:
            pass
    open_inv = billing_jobs.latest_open_invoice(restaurant_id)
    return {
        "restaurant_id": restaurant_id, "name": r.name,
        "billing_status": r.billing_status, "pause_reason": r.pause_reason,
        "pause_lock": _mdl.pause_lock(r), "hold": _mdl.billing_hold(r), "paused_until": r.paused_until,
        "converted_at": r.converted_at, "contract_status": r.contract_status,
        "contract_signed_at": r.contract_signed_at, "stripe_customer_id": r.stripe_customer_id,
        "modules": [k for k in _PLAN_MODULES if getattr(r, f"module_{k}", 0)],
        # Owner decision 1: which location's subscription pays for this one.
        "billed_by": ({"restaurant_id": payer, "name": getattr(get_restaurant(payer), "name", None)}
                      if payer else None),
        "covers": billing_jobs.subscription_scope(restaurant_id) if payer == restaurant_id else [],
        "subscription": mirror,
        "open_invoice": ({k: open_inv.get(k) for k in ("invoice_id", "hosted_invoice_url", "amount_due_cents",
                                                       "amount_remaining_cents", "attempt_count",
                                                       "next_payment_attempt", "last_failed_at")}
                         if open_inv else None),
        "invoices": billing_jobs.invoices_for(restaurant_id),
        "history": billing_jobs.billing_history(restaurant_id),
        "owed_sends": billing_jobs.owed_sends_for(restaurant_id),
        "envelopes": envs,
        "reconcile": rec,
    }


@admin_bp.route("/admin/api/billing/<int:restaurant_id>")
@admin_required
def admin_api_billing_detail(restaurant_id, current_user):
    """One client's billing record: the Stripe mirror (billed MRR, interval,
    discount, trial end), invoices with their hosted links, the status and
    module history, owed billing mail and its real outcome, envelopes, the
    last reconcile's findings, and who pays for the location."""
    detail = _billing_detail(restaurant_id)
    if detail is None:
        return jsonify(ok=False, error="Restaurant not found"), 404
    return jsonify(ok=True, **detail)


@admin_bp.route("/admin/api/billing/health")
@admin_required
def admin_api_billing_health(current_user):
    """The fleet's billing plumbing: each inbound webhook's last verified
    event and failures since (#74), the nightly reconcile's mismatches
    (#115), owed billing mail that failed for good (#12), and the
    signed-but-unpaid pipeline (#26)."""
    import billing_jobs
    conn = get_conn()
    try:
        hooks = [dict(r) for r in conn.execute("SELECT * FROM webhook_verifications ORDER BY provider")]
        failed = [dict(r) for r in conn.execute(
            "SELECT o.id, o.restaurant_id, r.name, o.kind, o.to_email, o.attempts, o.last_error, o.updated_at "
            "FROM owed_sends o LEFT JOIN restaurants r ON r.id=o.restaurant_id "
            "WHERE o.status='failed' AND o.updated_at >= datetime('now', '-30 days') ORDER BY o.id DESC LIMIT 100")]
        pending = conn.execute("SELECT COUNT(*) AS n, MIN(created_at) AS oldest FROM owed_sends "
                               "WHERE status IN ('pending','sending')").fetchone()
    finally:
        conn.close()
    return jsonify(ok=True, webhooks=hooks, reconcile=billing_jobs.reconcile_findings(),
                   failed_sends=failed, owed_pending={"count": pending["n"], "oldest": pending["oldest"]},
                   pipeline=billing_jobs.contract_pipeline())


@admin_bp.route("/admin/api/billing/<int:restaurant_id>/card-update-link", methods=["POST"])
@admin_required
def admin_api_billing_card_link(restaurant_id, current_user):
    """Send card-update link (#6): the Billing Portal, plus the open invoice
    when something is owed, to the owner. Reports the real delivery."""
    if not current_user.get("is_admin"):
        return jsonify(ok=False, error="Support accounts are read-only."), 403
    refused = _live_actions_refused()
    if refused:
        return refused
    import billing_jobs
    r = get_restaurant(restaurant_id)
    if not r:
        return jsonify(ok=False, error="Restaurant not found"), 404
    payer = billing_jobs.billed_by(restaurant_id)
    target = get_restaurant(payer) if payer and payer != restaurant_id else r
    if not (target.stripe_customer_id or "").strip():
        return jsonify(ok=False, error=f"{target.name} has no Stripe customer, so there is no card to update. "
                                       "Attach the Stripe customer first."), 409
    key = f"card_update:{target.id}:admin:{datetime.now().strftime('%Y%m%d%H%M%S%f')}"
    owed_id, _made = billing_jobs.enqueue("card_update", target.id, key, to_email=target.owner_email,
                                          source="admin", actor=current_user.get("username"))
    code, payload = _send_owed_now(owed_id)
    _billing_audit(current_user, "card_update.sent" if payload.get("ok") else "card_update.failed", restaurant_id,
                   target=str(owed_id), result="ok" if payload.get("ok") else "error",
                   summary="Card-update link " + ("sent" if payload.get("ok") else f"not sent: {payload.get('error')}"))
    return jsonify(**payload), code


@admin_bp.route("/admin/api/billing/<int:restaurant_id>/change-plan", methods=["POST"])
@admin_required
def admin_api_billing_change_plan(restaurant_id, current_user):
    """Change plan (#106): the Stripe subscription's price and module_keys
    first, then the local module flags — one audited action, so a console
    module change reaches what Stripe bills and the next subscription event
    no longer reverts it. Body: {modules: [...], proration:
    create_prorations | none | always_invoice}. With no live subscription
    (not paying yet) only the local flags change, and the next payment link
    quotes the new plan."""
    if not current_user.get("is_admin"):
        return jsonify(ok=False, error="Support accounts are read-only."), 403
    import billing_jobs
    import models as _mdl
    data = request.get_json(silent=True) or {}
    modules = sorted({str(m).strip().lower() for m in (data.get("modules") or [])} & set(_PLAN_MODULES))
    if not modules:
        return jsonify(ok=False, error="Pick at least one module."), 400
    proration = data.get("proration") or "create_prorations"
    if proration not in _PRORATIONS:
        return jsonify(ok=False, error=f"proration must be one of {', '.join(_PRORATIONS)}"), 400
    r = get_restaurant(restaurant_id)
    if not r:
        return jsonify(ok=False, error="Restaurant not found"), 404
    payer = billing_jobs.billed_by(restaurant_id)
    if payer and payer != restaurant_id:
        return jsonify(ok=False, error=(f"{r.name} is billed with {getattr(get_restaurant(payer), 'name', 'another location')} "
                                        "— change the plan on the paying location.")), 409
    before = [k for k in _PLAN_MODULES if getattr(r, f"module_{k}", 0)]
    flags = {f"module_{k}": (1 if k in modules else 0) for k in _PLAN_MODULES}
    mirror = billing_jobs.live_subscription(restaurant_id)
    stripe_after = None
    if mirror:
        refused = _live_actions_refused()
        if refused:
            return refused
        key = os.getenv("STRIPE_SECRET_KEY", "")
        if not key:
            return jsonify(ok=False, error="Stripe is not configured on this server — nothing changed."), 409
        try:
            import pricing
            stripe_mod = config.stripe_api(key)
            sub = stripe_mod.Subscription.retrieve(mirror["subscription_id"])
            items = billing_jobs._list(billing_jobs._g(sub, "items"))
            item = next((it for it in items if billing_jobs._recurring(billing_jobs._price(it))), None)
            if item is None:
                return jsonify(ok=False, error="That subscription has no recurring price to change."), 409
            interval = billing_jobs._g(billing_jobs._recurring(billing_jobs._price(item)), "interval") or "month"
            price_id = pricing.retainer_price_id(stripe_mod, len(modules), interval)
            meta = dict(billing_jobs._g(sub, "metadata") or {})
            meta.update({"module_keys": ",".join(modules), "modules": str(len(modules))})
            updated = stripe_mod.Subscription.modify(
                mirror["subscription_id"], items=[{"id": billing_jobs._g(item, "id"), "price": price_id}],
                proration_behavior=proration, metadata=meta)
            stripe_after = billing_jobs.subscription_facts(updated if billing_jobs._g(updated, "id") else sub)
        except Exception as e:
            _billing_audit(current_user, "change_plan.failed", restaurant_id, before={"modules": before},
                           after={"modules": modules}, result="error", summary=f"Stripe refused: {_safe_err(e)}")
            return jsonify(ok=False, error=f"Stripe did not accept the change — nothing changed. ({_safe_err(e)})"), 502
    with _mdl.billing_context(source="admin", actor=current_user.get("username"),
                              reason=f"change plan: {','.join(modules)} ({proration})"):
        update_restaurant(restaurant_id, flags)
    if stripe_after:
        billing_jobs.upsert_subscription(restaurant_id, facts=stripe_after,
                                         subscription_id=mirror["subscription_id"])
        billing_jobs.record_module_mismatch(restaurant_id, ",".join(modules))
    _billing_audit(current_user, "change_plan", restaurant_id, target=(mirror or {}).get("subscription_id"),
                   before={"modules": before}, after={"modules": modules, "proration": proration},
                   summary=f"Plan {','.join(before) or 'none'} → {','.join(modules)}"
                           + ("" if mirror else " (no live subscription: local only)"))
    return jsonify(ok=True, modules=modules, stripe_updated=bool(mirror),
                   subscription=billing_jobs.mirror_row(restaurant_id))


def _parse_day(value):
    """YYYY-MM-DD or M/D/YY → a UTC stamp at noon (a day, not a moment)."""
    from datetime import datetime as _dt
    v = (value or "").strip()
    for fmt in ("%Y-%m-%d", "%m/%d/%y", "%m/%d/%Y"):
        try:
            return _dt.strptime(v, fmt).strftime("%Y-%m-%d 12:00:00")
        except ValueError:
            continue
    return None


@admin_bp.route("/admin/api/billing/<int:restaurant_id>/mark-signed", methods=["POST"])
@admin_required
def admin_api_billing_mark_signed(restaurant_id, current_user):
    """Mark signed (offline) (#78): a contract signed on paper or outside
    DocuSign. Audited, with a required note; starts the pay-link reminders
    from the signing date. Body: {note, signed_on?, send_payment_link?,
    send_welcome?} — the two sends are owed and sent like a DocuSign
    completion's."""
    if not current_user.get("is_admin"):
        return jsonify(ok=False, error="Support accounts are read-only."), 403
    import billing_jobs
    import models as _mdl
    data = request.get_json(silent=True) or {}
    note = (data.get("note") or "").strip()[:300]
    if not note:
        return jsonify(ok=False, error="Say how it was signed (the note is the record)."), 400
    r = get_restaurant(restaurant_id)
    if not r:
        return jsonify(ok=False, error="Restaurant not found"), 404
    if (r.contract_status or "").lower() == "signed":
        return jsonify(ok=False, error="The contract is already marked signed."), 409
    signed_at = _parse_day(data.get("signed_on")) if data.get("signed_on") else billing_jobs._stamp()
    if not signed_at:
        return jsonify(ok=False, error="signed_on must be a date (M/D/YY or YYYY-MM-DD)."), 400
    before = r.contract_status
    with _mdl.billing_context(source="admin", actor=current_user.get("username"),
                              reason=f"marked signed offline: {note}"):
        update_restaurant(restaurant_id, {"contract_status": "signed", "contract_signed_at": signed_at})
    owed = []
    if data.get("send_payment_link") or data.get("send_welcome"):
        refused = _live_actions_refused()
        if refused:
            return refused
        if data.get("send_payment_link"):
            oid, _m = billing_jobs.enqueue("payment_link", restaurant_id, f"payment_link:{restaurant_id}:offline",
                                           to_email=r.owner_email, source="admin", actor=current_user.get("username"))
            owed.append(oid)
        login = billing_jobs.principal_login(restaurant_id)
        if data.get("send_welcome") and login and not login.get("last_login"):
            oid, _m = billing_jobs.enqueue("welcome", restaurant_id, f"welcome:{login['id']}",
                                           to_email=login.get("email") or r.owner_email,
                                           payload={"user_id": login["id"]}, source="admin",
                                           actor=current_user.get("username"))
            owed.append(oid)
    results = billing_jobs.drain_owed_sends(ids=[o for o in owed if o]).get("results", []) if owed else []
    _billing_audit(current_user, "contract.marked_signed", restaurant_id, before={"contract_status": before},
                   after={"contract_status": "signed", "signed_at": signed_at}, summary=f"Marked signed (offline): {note}")
    return jsonify(ok=True, contract_status="signed", contract_signed_at=signed_at, sends=results)


@admin_bp.route("/admin/api/billing/<int:restaurant_id>/attach-stripe-customer", methods=["POST"])
@admin_required
def admin_api_billing_attach_customer(restaurant_id, current_user):
    """Attach Stripe customer (#78): link a restaurant to a customer that
    exists in Stripe (a checkout that could not be matched, a client billed
    by hand). Verified with Stripe (read-only), refused when another
    restaurant outside this group holds the customer, and never silently
    replaces a stored customer — {replace: true} says so. The mirror is
    filled from the customer's subscriptions and the reconcile's checks run
    at once. Body: {customer_id, replace?}."""
    if not current_user.get("is_admin"):
        return jsonify(ok=False, error="Support accounts are read-only."), 403
    import billing_jobs
    import models as _mdl
    data = request.get_json(silent=True) or {}
    cid = (data.get("customer_id") or "").strip()
    if not cid.startswith("cus_") or len(cid) > 64 or not all(ch.isalnum() or ch == "_" for ch in cid):
        return jsonify(ok=False, error="That is not a Stripe customer id (cus_…)."), 400
    r = get_restaurant(restaurant_id)
    if not r:
        return jsonify(ok=False, error="Restaurant not found"), 404
    stored = (r.stripe_customer_id or "").strip()
    if stored == cid:
        return jsonify(ok=False, error="That customer is already attached."), 409
    if stored and not data.get("replace"):
        return jsonify(ok=False, stored=stored, error=(
            f"{r.name} is already attached to {stored}. Replacing it changes where its payments are "
            "matched — confirm with replace.")), 409
    group = set(billing_jobs.billing_group_ids(restaurant_id))
    conn = get_conn()
    try:
        other = conn.execute("SELECT id, name FROM restaurants WHERE stripe_customer_id=? AND id<>?",
                             (cid, restaurant_id)).fetchall()
    finally:
        conn.close()
    strangers = [o for o in other if o["id"] not in group]
    if strangers:
        return jsonify(ok=False, error=f"{cid} is attached to {strangers[0]['name']} (#{strangers[0]['id']}), "
                                       "which is not in this client's group."), 409
    key = os.getenv("STRIPE_SECRET_KEY", "")
    if not key:
        return jsonify(ok=False, error="Stripe is not configured on this server, so the customer can't be "
                                       "verified — nothing changed."), 409
    stripe_mod = config.stripe_api(key)
    try:
        cust = stripe_mod.Customer.retrieve(cid)
        if billing_jobs._g(cust, "deleted"):
            return jsonify(ok=False, error=f"{cid} is deleted in Stripe."), 400
    except Exception as e:
        return jsonify(ok=False, error=f"Stripe could not find {cid}: {_safe_err(e)}"), 400
    with _mdl.billing_context(source="admin", actor=current_user.get("username"),
                              reason="attach Stripe customer" + (f" (replaced {stored})" if stored else "")):
        update_restaurant(restaurant_id, {"stripe_customer_id": cid})
    try:
        found = billing_jobs.reconcile_one(restaurant_id, stripe_mod)
    except Exception as e:
        found = {"error": _safe_err(e)}
    _billing_audit(current_user, "stripe_customer.attached", restaurant_id, target=cid,
                   before={"stripe_customer_id": stored or None}, after={"stripe_customer_id": cid},
                   summary=f"Attached Stripe customer {cid}" + (f" (was {stored})" if stored else ""))
    return jsonify(ok=True, customer_id=cid, subscription=billing_jobs.mirror_row(restaurant_id),
                   mismatches=found.get("mismatches"), error=found.get("error"))


@admin_bp.route("/admin/api/billing/<int:restaurant_id>/lift-hold", methods=["POST"])
@admin_required
def admin_api_billing_lift_hold(restaurant_id, current_user):
    """Lift a dispute, refund or admin hold (lead default 5) — the only way
    one is lifted. Every location held for the same reason in the group is
    released together (a dispute paused the whole group). The status goes
    back to what Stripe's subscription says (active / past_due), or failing
    that what it was before the hold. Body: {note, status?}."""
    if not current_user.get("is_admin"):
        return jsonify(ok=False, error="Support accounts are read-only."), 403
    import billing_jobs
    import models as _mdl
    data = request.get_json(silent=True) or {}
    note = (data.get("note") or "").strip()[:300]
    if not note:
        return jsonify(ok=False, error="Say why the hold is lifted (the note is the record)."), 400
    r = get_restaurant(restaurant_id)
    if not r:
        return jsonify(ok=False, error="Restaurant not found"), 404
    reason = _mdl.billing_hold(r)
    if not reason:
        return jsonify(ok=False, error="There is no hold on this account."), 409
    wanted = (data.get("status") or "").strip().lower() or None
    if wanted and wanted not in ("active", "past_due", "trial"):
        return jsonify(ok=False, error="status must be active, past_due or trial."), 400
    lifted = []
    for rid in billing_jobs.billing_group_ids(restaurant_id):
        other = get_restaurant(rid)
        if other is None or _mdl.billing_hold(other) != reason:
            continue
        cur = (other.billing_status or "").lower()
        with _mdl.billing_context(source="admin", actor=current_user.get("username"),
                                  reason=f"{reason} hold lifted: {note}"):
            if cur in ("churned", "canceled", "cancelled") and not billing_jobs.billed_by(rid):
                # Churned with nothing paying for it: the hold goes, the
                # churn stays. A client who has paid again (a live
                # subscription covers it) is restored below.
                update_restaurant(rid, {"pause_reason": None})
            else:
                update_restaurant(rid, {"billing_status": wanted or _restored_status(rid),
                                        "pause_reason": None, "paused_until": None})
        lifted.append(rid)
    _billing_audit(current_user, "hold.lifted", restaurant_id, target=reason,
                   before={"pause_reason": reason}, after={"lifted": lifted}, summary=f"{reason} hold lifted: {note}")
    return jsonify(ok=True, lifted=lifted, reason=reason)


def _restored_status(restaurant_id):
    """What a location returns to when its hold is lifted: its subscription's
    state in Stripe (as the mirror holds it), else its status before the
    hold, else trial."""
    import billing_jobs
    payer = billing_jobs.billed_by(restaurant_id)
    row = billing_jobs.mirror_row(payer) if payer else None
    st = ((row or {}).get("status") or "").lower()
    if row and billing_jobs.is_live_status(st):
        return "past_due" if st in ("past_due", "paused") else "active"
    for h in billing_jobs.billing_history(restaurant_id, limit=50):
        if h.get("field") == "billing_status" and (h.get("new_value") or "") == "paused":
            prior = (h.get("old_value") or "").lower()
            if prior and prior != "paused":
                return prior
            break
    return "trial"


# ── Fix round B2 ──────────────────────────────────────────────────────────────
# The audit trail's views, support notes, bug reports, the deletion request's
# lifecycle and the offboarding checklist, Add location, sales-audit linking —
# and the two helpers the B2 routes above share: the local-backend send guard
# and the bounded admin job pool.

def _send_blocked():
    """None when this server may email or text real people; otherwise the
    ({ok: False, local_backend: True, error}, 409) an admin send route
    returns as-is.

    The same gate the scheduler obeys (scheduler.scheduling_allowed): a
    local backend has production's Resend and Twilio keys and its own copy
    of the database, so an admin send from a laptop reached real owners
    (#9). ALLOW_LOCAL_SCHEDULER=1 opens it deliberately; RESTORE_FROM (a
    restore waiting to be confirmed) closes it everywhere. Fails closed."""
    try:
        import scheduler
        if scheduler.scheduling_allowed():
            return None
    except Exception:
        pass
    if (os.getenv("RESTORE_FROM") or "").strip():
        why = "a database restore is waiting to be confirmed (RESTORE_FROM is set), so this server sends nothing"
    else:
        why = ("this is a local backend, and its copy of the database would email real people with "
               "production's keys")
    return {"ok": False, "local_backend": True,
            "error": f"Not sent: {why}. Set ALLOW_LOCAL_SCHEDULER=1 to send from here deliberately."}, 409


class _AdminJobs:
    """A small bounded pool for admin work that must not hold one of the
    four request threads (#153): AI drafting, Meta token exchanges, the
    review-account seed. WORKERS threads and a queue of QUEUE jobs; a full
    queue refuses the job rather than starting another thread (fetch-now
    and redraft-all used to start one unbounded thread per click).

    Daemon threads on purpose: a deploy kills a job mid-run, and
    ops.sweep_stale_jobs fails its async_jobs row at the next boot — the
    contract every async job here already has."""
    WORKERS = 2
    QUEUE = 20

    def __init__(self):
        import queue
        import threading
        self._q = queue.Queue(maxsize=self.QUEUE)
        self._lock = threading.Lock()
        self._threads = []

    def _ensure_workers(self):
        import threading
        with self._lock:
            self._threads = [t for t in self._threads if t.is_alive()]
            while len(self._threads) < self.WORKERS:
                t = threading.Thread(target=self._loop, name=f"admin-job-{len(self._threads)}", daemon=True)
                t.start()
                self._threads.append(t)

    def _loop(self):
        while True:
            fn = self._q.get()
            try:
                fn()
            except Exception as e:
                print(f"[admin-job] {e}")
            finally:
                self._q.task_done()

    def submit(self, fn):
        """Queue fn. Raises queue.Full when QUEUE jobs are already waiting."""
        self._ensure_workers()
        self._q.put_nowait(fn)


_ADMIN_JOBS = _AdminJobs()


def _start_admin_job(kind, restaurant_id, fn):
    """Run fn() on the admin pool, tracked as an ops.async_jobs row.
    Returns (job_id, joined): a second press while one of the same kind is
    pending for the same restaurant joins it instead of queueing another.
    (None, False) when the pool is full. fn returns the job's result dict;
    ok False marks the job an error. Poll /admin/api/admin-jobs/<job_id>."""
    import uuid
    job_id, joined = _ops.claim_async_job(str(uuid.uuid4()), kind, restaurant_id)
    if joined:
        return job_id, True

    def _run():
        try:
            result = fn() or {}
            _ops.finish_async_job(job_id, "done" if result.get("ok", True) else "error", result)
        except Exception as e:
            _ops.capture(e, job=f"admin_job:{kind}", context=f"restaurant_id={restaurant_id}")
            _ops.finish_async_job(job_id, "error", {"ok": False, "error": _safe_err(e)})
    try:
        _ADMIN_JOBS.submit(_run)
    except Exception:
        _ops.finish_async_job(job_id, "error", {"ok": False, "error": "Too many admin jobs are queued."})
        return None, False
    return job_id, False


@admin_bp.route("/admin/api/admin-jobs/<job_id>")
@admin_required
def admin_api_admin_job(job_id, current_user):
    """One admin job's state: {status: pending} (200), the job's result
    (200), or a failed job's result (502, with `error`). A value the job
    marked read-once (the review account's new password) is returned to the
    first admin who reads it and then removed from the stored result."""
    job = _ops.read_async_job(job_id)
    if not job:
        return jsonify(ok=False, status="missing", error="No such job (results are kept for six hours)."), 404
    if job["status"] == "pending":
        return jsonify(ok=True, status="pending", job_id=job_id)
    result = dict(job.get("result") or {})
    scrub = [k for k in (result.pop("_scrub", None) or []) if isinstance(k, str)]
    if scrub:
        if current_user.get("is_admin"):
            _ops.finish_async_job(job_id, job["status"], {k: v for k, v in result.items() if k not in scrub})
        else:
            for k in scrub:
                result.pop(k, None)
    failed = job["status"] != "done" or result.get("ok") is False
    result.update(ok=not failed, status=job["status"], job_id=job_id)
    return jsonify(**result), (502 if failed else 200)


# ── the audit trail (#53, #72, #126) ─────────────────────────────────────────

def _audit_query_args():
    a = request.args
    return {"actor": (a.get("actor") or "").strip() or None, "action": (a.get("action") or "").strip() or None,
            "result": (a.get("result") or "").strip() or None, "before_id": a.get("before_id"),
            "limit": a.get("limit", 50), "include_requests": a.get("all") == "1"}


@admin_bp.route("/admin/api/audit")
@admin_required
def admin_api_audit(current_user):
    """Admin actions across the fleet, newest first, 50 a page.
    ?restaurant_id=&actor=&action=<prefix>&result=&before_id=&limit=&all=1"""
    import admin_events
    try:
        rid = int(request.args["restaurant_id"]) if request.args.get("restaurant_id") else None
    except (TypeError, ValueError):
        rid = None
    return jsonify(ok=True, **admin_events.audit_list(restaurant_id=rid, **_audit_query_args()))


@admin_bp.route("/admin/api/client/<int:restaurant_id>/audit")
@admin_required
def admin_api_client_audit(restaurant_id, current_user):
    """One client's admin actions — kept after the client is deleted."""
    import admin_events
    return jsonify(ok=True, restaurant_id=restaurant_id,
                   **admin_events.audit_list(restaurant_id=restaurant_id, **_audit_query_args()))


@admin_bp.route("/admin/api/audit/refused")
@admin_required
def admin_api_audit_refused(current_user):
    """Refused and anonymous /admin writes (capped, 30 days)."""
    import admin_events
    return jsonify(ok=True, **admin_events.refused_list(before_id=request.args.get("before_id"),
                                                         limit=request.args.get("limit", 50)))


# ── support notes (#55) ──────────────────────────────────────────────────────

@admin_bp.route("/admin/api/client/<int:restaurant_id>/notes")
@admin_required
def admin_api_support_notes(restaurant_id, current_user):
    """The client's support notes, newest first, with the old one-field
    internal_notes alongside (read-only)."""
    import admin_events
    r = get_restaurant(restaurant_id)
    if not r:
        return jsonify(ok=False, error="Not found"), 404
    out = admin_events.list_support_notes(restaurant_id, before_id=request.args.get("before_id"),
                                          limit=request.args.get("limit", 50))
    return jsonify(ok=True, restaurant_id=restaurant_id, legacy_internal_notes=r.internal_notes or "", **out)


@admin_bp.route("/admin/api/client/<int:restaurant_id>/notes", methods=["POST"])
@admin_required
def admin_api_add_support_note(restaurant_id, current_user):
    """Append a note: {body}. Authored and dated; nothing edits or deletes one."""
    import admin_events
    if not get_restaurant(restaurant_id):
        return jsonify(ok=False, error="Not found"), 404
    data = request.get_json(silent=True) or {}
    body = sanitize(data.get("body") or "", max_len=admin_events.SUPPORT_NOTE_MAX)
    note = admin_events.add_support_note(restaurant_id, current_user, body or "")
    if not note:
        return jsonify(ok=False, error="Write something first."), 400
    admin_events.record_admin_action(current_user, "support_note.added", restaurant_id=restaurant_id,
                                     target=f"note:{note['id']}", after={"length": len(note["body"])})
    return jsonify(ok=True, note=note)


# ── in-app bug reports (#34) ─────────────────────────────────────────────────

@admin_bp.route("/admin/api/bug-reports")
@admin_required
def admin_api_bug_reports(current_user):
    """?status=open|closed&restaurant_id=&before_id=&limit="""
    import admin_events
    try:
        rid = int(request.args["restaurant_id"]) if request.args.get("restaurant_id") else None
    except (TypeError, ValueError):
        rid = None
    return jsonify(ok=True, **admin_events.list_bug_reports(status=request.args.get("status"), restaurant_id=rid,
                                                             before_id=request.args.get("before_id"),
                                                             limit=request.args.get("limit", 50)))


@admin_bp.route("/admin/api/bug-reports/<int:report_id>/status", methods=["POST"])
@admin_required
def admin_api_bug_report_status(report_id, current_user):
    """{status: closed|open, note?}"""
    import admin_events
    data = request.get_json(silent=True) or {}
    status = str(data.get("status") or "").strip().lower()
    if status not in ("open", "closed"):
        return jsonify(ok=False, error="Status must be open or closed."), 400
    row = admin_events.set_bug_report_status(report_id, status, current_user, note=data.get("note"))
    if not row:
        return jsonify(ok=False, error="No such report."), 404
    admin_events.record_admin_action(current_user, f"bug_report.{status}", restaurant_id=row.get("restaurant_id"),
                                     target=f"bug_report:{report_id}", after={"status": status,
                                                                              "note": row.get("close_note")})
    return jsonify(ok=True, report=row)


# ── account deletion and offboarding (#34, #138) ─────────────────────────────

@admin_bp.route("/admin/api/deletion-requests")
@admin_required
def admin_api_deletion_requests(current_user):
    """Every open "Close my account" request, soonest due first."""
    import offboarding
    conn = get_conn()
    try:
        rows = conn.execute("SELECT id FROM restaurants WHERE deletion_requested_at IS NOT NULL "
                            "AND deletion_requested_at <> ''").fetchall()
    finally:
        conn.close()
    out = []
    for row in rows:
        state = offboarding.checklist(row["id"])
        if state and state.get("request"):
            out.append({"restaurant_id": row["id"], "name": state["name"], "billing_status": state["billing_status"],
                        "outstanding": state["outstanding"], **state["request"]})
    out.sort(key=lambda x: (x.get("due_at") or ""))
    return jsonify(ok=True, requests=out)


@admin_bp.route("/admin/api/client/<int:restaurant_id>/offboarding")
@admin_required
def admin_api_offboarding(restaurant_id, current_user):
    """The offboarding checklist: the deletion request (if any) and its
    30-day due date, each step's state, and whether delete is unlocked."""
    import offboarding
    state = offboarding.checklist(restaurant_id)
    if not state:
        return jsonify(ok=False, error="Not found"), 404
    return jsonify(**state)


@admin_bp.route("/admin/api/client/<int:restaurant_id>/offboarding/<step>", methods=["POST"])
@admin_required
def admin_api_offboarding_step(restaurant_id, step, current_user):
    """{status: done|skipped|pending, note?}. 'integrations' done clears
    every stored credential (offboarding.revoke_integrations); a skip needs
    a note."""
    import offboarding
    if not current_user.get("is_admin"):
        return jsonify(ok=False, error="Support accounts are read-only."), 403
    data = request.get_json(silent=True) or {}
    payload, status = offboarding.set_step(restaurant_id, step, str(data.get("status") or "").strip().lower(),
                                           current_user, note=data.get("note"))
    return jsonify(**payload), status


@admin_bp.route("/admin/api/client/<int:restaurant_id>/deletion/withdraw", methods=["POST"])
@admin_required
def admin_api_withdraw_deletion(restaurant_id, current_user):
    """The owner changed their mind: clear the request. {note?}"""
    import offboarding
    if not current_user.get("is_admin"):
        return jsonify(ok=False, error="Support accounts are read-only."), 403
    data = request.get_json(silent=True) or {}
    payload, status = offboarding.withdraw_deletion_request(restaurant_id, current_user, note=data.get("note"))
    return jsonify(**payload), status


@admin_bp.route("/admin/api/client/<int:restaurant_id>/delete", methods=["POST"])
@admin_required
def admin_api_delete_restaurant(restaurant_id, current_user):
    """Permanently delete a restaurant — a real client included — once its
    offboarding checklist is finished. {confirm_name}: its exact name.
    Server-side gates: admin (not support), the name, no admin login on it,
    the checklist. The step-up (auth.recent_auth_required) is applied to
    this route in the integration wave. The audit rows and the checklist
    survive the delete."""
    import offboarding
    if not current_user.get("is_admin"):
        return jsonify(ok=False, error="Support accounts are read-only."), 403
    data = request.get_json(silent=True) or {}
    try:
        payload, status = offboarding.delete_restaurant_now(restaurant_id, current_user, data.get("confirm_name"))
    except Exception as e:
        _ops.capture(e, job="delete_restaurant", context=f"restaurant_id={restaurant_id}")
        return jsonify(ok=False, error=f"The delete failed and was rolled back: {_safe_err(e)}"), 500
    return jsonify(**payload), status


# ── Add location to an existing brand (#42) ──────────────────────────────────

def _valid_timezone(name, fallback="America/Chicago"):
    name = (name or "").strip()
    if not name:
        return fallback
    try:
        from zoneinfo import ZoneInfo
        ZoneInfo(name)
        return name
    except Exception:
        return None


@admin_bp.route("/admin/api/brand/add-location", methods=["POST"])
@admin_required
def admin_api_add_location(current_user):
    """A new location under an existing brand: same owner, same group, no
    new login (#42). New client refused both ways in — an existing owner
    email, and a new email under a group that already belonged to someone.

    Body: {from_restaurant_id (a location of the brand), restaurant_name,
    location_name?, google_place_id?, yelp_business_id?, owner_phone?,
    timezone? (defaults to the brand's), module_* ? (default: the brand's),
    audit_id? (a sales audit to link)}. The brand's owner login becomes an
    'owner' (the role that may switch locations) at both. Billing follows
    the brand (decision 1: one subscription per group — the new location is
    covered by it). No contract is sent."""
    from models import Restaurant, create_restaurant, update_restaurant
    import auth
    import admin_events
    if not current_user.get("is_admin"):
        return jsonify(ok=False, error="Support accounts are read-only."), 403
    data = request.get_json(silent=True) or {}
    try:
        src_id = int(data.get("from_restaurant_id") or 0)
    except (TypeError, ValueError):
        src_id = 0
    src = get_restaurant(src_id) if src_id else None
    if not src:
        return jsonify(ok=False, error="Pick an existing location of the brand."), 400
    name = sanitize((data.get("restaurant_name") or "").strip(), max_len=120)
    if not name:
        return jsonify(ok=False, error="The new location needs a name."), 400
    owner_email = (src.owner_email or "").strip()
    if not owner_email:
        return jsonify(ok=False, error=f"{src.name} has no owner email to share."), 409
    uid = _principal_login_id(src_id)
    home_id = src_id
    if not uid and (src.location_group or "").strip():
        # The brand's owner may live on another of its locations.
        from models import get_location_group
        for sib in get_location_group(src.location_group.strip(), owner_email=owner_email):
            uid = _principal_login_id(sib["id"])
            if uid:
                home_id = sib["id"]
                break
    if not uid:
        return jsonify(ok=False, error=f"{src.name} has no owner login to add the location to."), 409
    tz = _valid_timezone(data.get("timezone"), fallback=getattr(src, "timezone", None) or "America/Chicago")
    if not tz:
        return jsonify(ok=False, error="That timezone isn't a real IANA name (e.g. America/Chicago)."), 400
    group = (src.location_group or "").strip() or (src.name or "").strip()
    clash = location_group_conflict(group, owner_email, exclude_id=src_id)
    if clash:
        return jsonify(ok=False, error=f"The group name “{group}” already belongs to {clash}. "
                                       f"Rename this brand's group in its settings first."), 409
    place = (data.get("google_place_id") or "").strip() or None
    is_demo = int(getattr(src, "is_demo", 0) or 0)
    # A demo deliberately mirrors a real listing (place_id_conflict exempts
    # demos), so only a live location is held to one listing per restaurant.
    place_clash = place_id_conflict(place) if (place and not is_demo) else None
    if place_clash:
        return jsonify(ok=False, error=f"That Google listing is already connected to {place_clash}."), 409

    def _flag(key):
        if key in data:
            try:
                return 1 if int(data.get(key) or 0) else 0
            except (TypeError, ValueError):
                return 0
        return int(getattr(src, key, 0) or 0)
    modules = {k: _flag(k) for k in ("module_reviews", "module_labor", "module_inventory", "module_marketing")}
    src_status = (src.billing_status or "trial")
    billing = src_status if src_status in ("active", "past_due", "paused", "internal") else "trial"

    if not (src.location_group or "").strip():
        # The first location joins the group it now shares (and its
        # organization, via update_restaurant) — as provisioning does for a
        # checkout's second location.
        update_restaurant(src_id, {"location_group": group})
    rid = create_restaurant(Restaurant(
        name=name, owner_email=owner_email, owner_name=src.owner_name,
        owner_phone=(data.get("owner_phone") or "").strip() or None,
        google_place_id=place, yelp_business_id=(data.get("yelp_business_id") or "").strip() or None,
        location_group=group, location_name=sanitize((data.get("location_name") or "").strip(), max_len=120),
        timezone=tz, is_demo=is_demo, billing_status=billing,
        # A Place ID IS the reviews connection until Google OAuth is done —
        # create_client's rule (a new location with no fetch flag was never
        # fetched).
        reviews_live=1 if (place and modules["module_reviews"]) else 0, **modules))
    conn = get_conn()
    try:
        login = conn.execute("SELECT id, username, COALESCE(NULLIF(role,''),'client') AS role FROM users WHERE id=?",
                             (uid,)).fetchone()
    finally:
        conn.close()
    promoted = login["role"] != "owner"
    if promoted:
        # 'owner' is the role that may switch between locations; the
        # membership at the home location carries the role there too.
        auth.set_user_role(uid, "owner")
        auth.upsert_membership(uid, home_id, "owner")
    auth.upsert_membership(uid, rid, "owner")
    linked_audit = None
    if data.get("audit_id"):
        try:
            import promise
            import sales_audits
            aid = int(data["audit_id"])
            if sales_audits.get_audit(aid):
                promise.link(aid, rid)
                linked_audit = aid
        except (TypeError, ValueError):
            linked_audit = None
    admin_events.record_admin_action(
        current_user, "location.added", restaurant_id=rid, target=f"restaurant:{src_id}",
        before={"owner_role": login["role"], "brand_group": (src.location_group or None)},
        after={"name": name, "group": group, "owner_login": uid, "owner_role": "owner", "billing_status": billing,
               "timezone": tz, "modules": modules, "linked_audit": linked_audit})
    return jsonify(ok=True, restaurant_id=rid, group=group, owner_username=login["username"],
                   promoted_to_owner=promoted, billing_status=billing, linked_audit=linked_audit,
                   message=f"Added {name} to {group}. {login['username']} can switch to it from the location "
                           f"picker. No contract was sent.")


# ── sales audits linked to a client (#66) ────────────────────────────────────

@admin_bp.route("/admin/api/client/<int:restaurant_id>/audits")
@admin_required
def admin_api_client_audits(restaurant_id, current_user):
    """The sales audits linked to this client (newest first) — what the
    audit-versus-now comparison (promise.py) reads."""
    import sales_audits
    return jsonify(ok=True, restaurant_id=restaurant_id, audits=sales_audits.linked_audits(restaurant_id))
