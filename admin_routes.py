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
from auth import get_session_user, delete_session, create_user, update_password, admin_required, login_required
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
    """Which services this server has keys for — presence only, never the
    values."""
    import os as _os
    keys = {"Anthropic (Claude)": "ANTHROPIC_API_KEY", "Perplexity": "PERPLEXITY_API_KEY", "Resend (email)": "RESEND_API_KEY",
            "Stripe": "STRIPE_SECRET_KEY", "Stripe webhook": "STRIPE_WEBHOOK_SECRET", "DocuSign": "DOCUSIGN_INTEGRATION_KEY",
            "Twilio (SMS)": "TWILIO_ACCOUNT_SID", "Google OAuth": "GOOGLE_CLIENT_ID", "Google Places": "GOOGLE_PLACES_API_KEY",
            "Meta (Instagram)": "META_APP_ID", "APNs (push)": "APNS_KEY_ID", "Sentry": "SENTRY_DSN"}
    services = {label: bool(_os.getenv(var)) for label, var in keys.items()}
    build = _os.getenv("RAILWAY_GIT_COMMIT_SHA", "")[:8] or None
    if not build:
        try:
            import subprocess
            build = subprocess.check_output(["git", "rev-parse", "--short", "HEAD"], stderr=subprocess.DEVNULL, timeout=2).decode().strip()
        except Exception:
            build = None
    from models import DB_PATH
    return jsonify(ok=True, services=services, env=("railway" if config.on_railway() else "local"),
                   tick=int(_os.getenv("SCHEDULER_TICK_SECONDS", "300")), db=_os.path.basename(str(DB_PATH)), build=build)

def _new_client_problem(data):
    """The first thing wrong with a New client form, as the sentence to
    show, or None. Missing keys and non-text values used to surface as a
    raw KeyError or AttributeError in the console."""
    import re as _re
    import time_utils
    for key, label in (("restaurant_name", "Restaurant name"), ("owner_email", "Owner email"),
                       ("username", "Dashboard username"), ("password", "Temporary password")):
        if not isinstance(data.get(key), str) or not data[key].strip():
            return f"{label} is required."
    for key in ("google_place_id", "yelp_business_id", "voice_notes", "owner_phone", "owner_name",
                "location_group", "location_name", "timezone"):
        if data.get(key) is not None and not isinstance(data.get(key), str):
            return f"{key.replace('_', ' ').capitalize()} must be text."
    if len(data["restaurant_name"].strip()) > 200:
        return "Restaurant name is too long."
    email = data["owner_email"].strip()
    if len(email) > 254 or not _re.match(r"^[^@\s,;<>]+@[^@\s,;<>]+\.[^@\s,;<>]+$", email):
        return "Owner email must be one email address."
    if not _re.match(r"^[A-Za-z0-9._@+-]{3,64}$", data["username"].strip()):
        return "Dashboard username: 3 to 64 letters, numbers, dots, dashes or underscores, with no spaces."
    place = (data.get("google_place_id") or "").strip()
    if place and (len(place) > 300 or any(c.isspace() for c in place)):
        return "Google Place ID has spaces in it. Paste just the ID (it starts with ChIJ)."
    # The zones the product offers (time_utils.COMMON_TIMEZONES, the same list
    # as the settings page and the owner's own profile). Every restaurant
    # defaulted to Chicago, so a Pacific client got its 9am mail at 7am
    # (fix round #152).
    tz = (data.get("timezone") or "").strip()
    if tz and tz not in time_utils.COMMON_TIMEZONES:
        return "Pick the restaurant's timezone from the list."
    return None


@admin_bp.route("/admin/create-client", methods=["POST"])
@admin_required
def create_client(current_user):
    """Create the restaurant and its owner's login, then start the provider
    calls (the Places menu fetch, the DocuSign contract) as a background
    job: they held a request thread for as long as Google and DocuSign took
    (fix round #153). The response carries `setup_job_id`; poll
    /admin/api/create-client/<job_id> for {envelope_id, docusign_skipped,
    menu_notes_fetched}. Body adds an optional `timezone` (one of
    time_utils.COMMON_TIMEZONES; Chicago when not given, and the response
    says so with timezone_defaulted)."""
    from models import create_restaurant, Restaurant
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify(ok=False, error="Send the new client as a JSON object."), 400
    problem = _new_client_problem(data)
    if problem:
        return jsonify(ok=False, error=problem), 400
    timezone = (data.get("timezone") or "").strip() or "America/Chicago"
    try:
        # Check for duplicate email/username BEFORE creating anything. Both
        # are stored lower-cased (auth.create_user), so compared that way.
        conn_check = get_conn()
        existing = conn_check.execute(
            "SELECT id FROM users WHERE lower(email)=lower(?) OR lower(username)=lower(?)",
            (data["owner_email"].strip(), data["username"].strip())
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
            (data.get("location_group") or "").strip(), data["owner_email"]
        )
        if conflict:
            return jsonify(ok=False, error=(
                f"Location group “{(data.get('location_group') or '').strip()}” already belongs to "
                f"{conflict}. Pick a different group name — locations in a group share data and billing."
            ))

        # Create restaurant
        rid = create_restaurant(Restaurant(
            name=data["restaurant_name"].strip(),
            owner_email=data["owner_email"].strip(),
            google_place_id=(data.get("google_place_id") or "").strip() or None,
            yelp_business_id=(data.get("yelp_business_id") or "").strip() or None,
            voice_notes=data.get("voice_notes") or None,
            owner_phone=data.get("owner_phone") or None,
            owner_name=data.get("owner_name") or None,
            location_group=(data.get("location_group") or "").strip() or None,
            location_name=(data.get("location_name") or "").strip() or None,
            timezone=timezone,
        ))
        try:
            create_user(
                restaurant_id=rid,
                username=data["username"].strip(),
                email=data["owner_email"].strip(),
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
            # The settings save applies the same rule (reviews_live_decision).
            "reviews_live":    1 if ((data.get("google_place_id") or "").strip()
                                     and _flag("module_reviews", 1)) else 0,
        })

        module_names = []
        if _flag("module_reviews"): module_names.append("Review Intelligence")
        if _flag("module_labor"):   module_names.append("Labor Optimizer")
        if _flag("module_inventory"): module_names.append("Food Cost Control")
        if _flag("module_marketing"): module_names.append("Marketing Autopilot")

        # Steps 2 & 3 (payment + welcome emails) fire automatically
        # when the client signs the contract via the DocuSign webhook
        setup = {
            "restaurant_name": data["restaurant_name"].strip(),
            "owner_email": data["owner_email"].strip(),
            "owner_name": (data.get("owner_name") or "").strip(),
            "google_place_id": (data.get("google_place_id") or "").strip() or None,
            "fetch_menu": bool(_flag("module_marketing")),
            "module_names": module_names,
        }
        job_id, setup_error = _start_client_setup(rid, setup)
        return jsonify(ok=True, restaurant_id=rid, setup_job_id=job_id, setup_error=setup_error,
                       envelope_id=None, docusign_skipped=False, timezone=timezone,
                       timezone_defaulted=not (data.get("timezone") or "").strip())
    except Exception as e:
        import traceback; traceback.print_exc()
        return jsonify(ok=False, error=_safe_err(e))


def _start_client_setup(restaurant_id, setup):
    """(job_id, error): the new client's provider calls, on the bounded admin
    job pool (see _submit_admin_job)."""
    import uuid
    job_id = str(uuid.uuid4())
    _ops.start_async_job(job_id, "client_setup", restaurant_id)
    err = _submit_admin_job(job_id, _run_client_setup, job_id, restaurant_id, setup)
    if err:
        _ops.finish_async_job(job_id, "error", {"ok": False, "error": err})
        return job_id, err
    return job_id, None


def _run_client_setup(job_id, restaurant_id, setup):
    """The Places menu fetch and the DocuSign contract for a new client.
    Result: {ok, restaurant_id, menu_notes_fetched, envelope_id,
    docusign_skipped, docusign_error}."""
    from models import update_restaurant, get_restaurant as _gr_setup
    result = {"ok": True, "restaurant_id": restaurant_id, "menu_notes_fetched": False,
              "envelope_id": None, "docusign_skipped": False, "docusign_error": None}
    # Auto-fetch menu notes from Google Places if place ID provided
    if setup.get("google_place_id") and setup.get("fetch_menu"):
        try:
            from competitor import fetch_menu_notes_from_places
            auto_menu = fetch_menu_notes_from_places(setup["google_place_id"], restaurant_id=restaurant_id)
            current = _gr_setup(restaurant_id)
            # Notes somebody typed while this ran are theirs to keep.
            if auto_menu and current and not (current.menu_notes or "").strip():
                update_restaurant(restaurant_id, {"menu_notes": auto_menu})
                result["menu_notes_fetched"] = True
        except Exception as me:
            _ops.capture(me, job="create_client_menu_fetch", context=f"restaurant_id={restaurant_id}")
    # Step 1: Send contract via DocuSign
    names = setup.get("module_names") or []
    if names and setup.get("owner_email"):
        try:
            import scheduler as _sched_cc
            local = not _sched_cc.scheduling_allowed()
        except Exception:
            local = True
        if local:
            # A local backend holds production's DocuSign credentials: a
            # contract from here reaches the real owner. The scheduler's rule
            # (scheduling_allowed; ALLOW_LOCAL_SCHEDULER=1 overrides) applies
            # to every send an admin action makes.
            result.update(docusign_skipped=True, docusign_error="Not sent from a local backend.")
        else:
            try:
                from docusign_helper import send_contract
                sent = send_contract(
                    owner_email=setup["owner_email"],
                    owner_name=setup.get("owner_name") or setup["restaurant_name"],
                    restaurant_name=setup["restaurant_name"],
                    module_count=len(names),
                    modules_list=", ".join(names),
                )
                envelope_id = sent.get("envelope_id")
                update_restaurant(restaurant_id, {"contract_status": "sent", "docusign_envelope_id": envelope_id})
                result["envelope_id"] = envelope_id
                try:
                    log_email(restaurant_id, "contract", setup["owner_email"],
                              f"Service Agreement — {setup['restaurant_name']}")
                except Exception as le:
                    _ops.capture(le, job="create_client_contract_log", context=f"restaurant_id={restaurant_id}")
            except Exception as e:
                result.update(docusign_skipped=True, docusign_error=_safe_err(e))
                _ops.capture(e, job="create_client_contract", context=f"restaurant_id={restaurant_id}")
    _ops.finish_async_job(job_id, "done", result)


@admin_bp.route("/admin/deactivate-client/<int:user_id>", methods=["POST"])
@admin_required
def deactivate_client(user_id, current_user):
    conn = get_conn()
    conn.execute("UPDATE users SET is_active=0 WHERE id=? AND is_admin=0", (user_id,))
    conn.commit(); conn.close()
    return jsonify(ok=True)

@admin_bp.route("/admin/reactivate-client/<int:user_id>", methods=["POST"])
@admin_required
def reactivate_client(user_id, current_user):
    conn = get_conn()
    conn.execute("UPDATE users SET is_active=1 WHERE id=?", (user_id,))
    conn.commit()
    # Get user info to send reactivation email
    user_row = conn.execute("SELECT * FROM users WHERE id=?", (user_id,)).fetchone()
    conn.close()
    if user_row:
        try:
            restaurant = get_restaurant(dict(user_row)["restaurant_id"])
            if restaurant:
                from emails import send_reactivation_email
                send_reactivation_email(
                    to_email=restaurant.owner_email,
                    restaurant_name=restaurant.name,
                    owner_name=restaurant.owner_name,
                )
        except Exception as e:
            print(f"Reactivation email failed: {e}")
    return jsonify(ok=True)

@admin_bp.route("/admin/api/set-user-role", methods=["POST"])
@admin_required
def set_user_role_route(current_user):
    data = request.get_json(force=True, silent=True) or {}
    try:
        user_id = int(data.get("user_id") or 0)
    except (TypeError, ValueError):
        return jsonify(ok=False, error="Invalid user_id")
    role = data.get("role", "client")
    if role not in ("client", "owner"):
        return jsonify(ok=False, error="Invalid role")
    if not user_id:
        return jsonify(ok=False, error="Missing user_id")
    from auth import set_user_role
    try:
        set_user_role(user_id, role)
    except Exception as e:
        return jsonify(ok=False, error=_safe_err(e))
    return jsonify(ok=True)


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
    """Add a scheduling constraint. A second one for the same person is
    added to the first, never written over it (models.save_staff_note,
    fix round #142). {ok, id, notes (the person's full text), appended}."""
    from models import save_staff_note
    name  = " ".join((request.form.get("employee_name") or "").split())[:80]
    notes = (request.form.get("notes") or "").strip()[:500]
    if not name or not notes:
        return jsonify(ok=False, error="Name and notes required"), 400
    if not get_restaurant(restaurant_id):
        return jsonify(ok=False, error="Restaurant not found"), 404
    result = save_staff_note(restaurant_id, name, notes)
    _record_staff_note(restaurant_id, current_user, "staff_note.saved", name,
                       {"added": notes, "now": result["notes"], "appended": result["appended"]})
    return jsonify(ok=True, **result)

@admin_bp.route("/admin/staff-notes/<int:note_id>/delete", methods=["POST"])
@admin_required
def delete_staff_note_route(note_id, current_user):
    """Remove one person's constraints. Returns what was removed, so the
    page can offer an undo, and records it with its restaurant and text —
    the audit row said neither (fix round #142)."""
    from models import delete_staff_note
    deleted = delete_staff_note(note_id)
    if not deleted:
        return jsonify(ok=False, error="That constraint was already removed."), 404
    _record_staff_note(deleted["restaurant_id"], current_user, "staff_note.removed", deleted["employee_name"],
                       {"removed": deleted["notes"]})
    return jsonify(ok=True, deleted={"employee_name": deleted["employee_name"], "notes": deleted["notes"],
                                     "restaurant_id": deleted["restaurant_id"]})


def _record_staff_note(restaurant_id, current_user, event, employee_name, detail):
    actor = current_user.get("username") or current_user.get("email") or "admin"
    try:
        from models import log_event
        log_event(restaurant_id, event.replace(".", "_"), {"by": actor, "employee": employee_name, **detail})
    except Exception as e:
        _ops.capture(e, job="staff_note_audit", context=f"restaurant_id={restaurant_id}")
    import admin_events
    admin_events.record("admin", event, restaurant_id=restaurant_id,
                        summary=f"{actor}: {employee_name}"[:300],
                        payload={"actor": actor, "employee": employee_name, **detail})

@admin_bp.route("/admin/seed-review-account", methods=["POST"])
@admin_required
def seed_review_account_route(current_user):
    """Create (or refresh) the App Store review account, from inside production.

    scripts/seed_review_account.py does the same thing, but running it needs a
    shell on the container that holds the volume — the database path only
    exists there, so running it on a laptop either fails or quietly seeds a
    local file that Apple will never see. This is the same code path, one
    click, in the process that already has the right database.

    Returns the credentials once. They go in App Store Connect under App
    Review Information; see docs/app-store-submission.md.
    """
    import subprocess
    import sys
    import os as _os
    script = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)),
                           "scripts", "seed_review_account.py")
    if not _os.path.exists(script):
        return jsonify(ok=False, error="seed_review_account.py is not in this deployment"), 500
    try:
        out = subprocess.run(
            [sys.executable, script],
            capture_output=True, text=True, timeout=120,
            # Inherit the environment so the script resolves the same DB_PATH
            # this process is using, volume mount included.
            env=dict(_os.environ),
        )
    except subprocess.TimeoutExpired:
        return jsonify(ok=False, error="Seeding timed out"), 504
    if out.returncode != 0:
        return jsonify(ok=False, error=(out.stderr or out.stdout or "")[-1500:]), 500
    return jsonify(ok=True, output=out.stdout)


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


def _ingredient_label(value, max_len):
    """An ingredient name, category or unit as stored: whitespace collapsed,
    angle brackets dropped. The page escapes on output (fix round #10); this
    is the second line, so markup never reaches the table at all."""
    return " ".join(str(value or "").replace("<", "").replace(">", "").split())[:max_len]


def _ingredient_amount(data, key, default, positive=False, most=1000000.0):
    """(number, error). Non-numbers used to 500 the request, and NaN,
    infinity and negatives were stored — the MOD-FC-6 bug the update route
    had already fixed (fix round #142)."""
    import math
    raw = data.get(key)
    if raw in (None, ""):
        return default, None
    try:
        value = float(raw)
    except (TypeError, ValueError):
        value = float("nan")
    label = key.replace("_", " ")
    if isinstance(raw, bool) or not math.isfinite(value) or value < 0 or value > most or (positive and value <= 0):
        return None, (f"{label.capitalize()} must be a number more than 0." if positive
                      else f"{label.capitalize()} must be a number of 0 or more.")
    return value, None


@admin_bp.route("/admin/inventory/ingredients/<int:restaurant_id>", methods=["POST"])
@admin_required
def create_ingredient_route(restaurant_id, current_user):
    import inventory_ledger
    data = request.get_json(silent=True) or {}
    name = _ingredient_label(data.get("name"), 80)
    if not name:
        return jsonify(ok=False, error="Ingredient name required"), 400
    amounts = {}
    for key, default, positive, most in (("par_level", 0.0, False, 1000000.0), ("unit_cost", 0.0, False, 100000.0),
                                         ("case_size", 1.0, True, 100000.0), ("current_stock", 0.0, False, 1000000.0)):
        amounts[key], err = _ingredient_amount(data, key, default, positive=positive, most=most)
        if err:
            return jsonify(ok=False, error=err), 400
    ingredient_id = inventory_ledger.create_ingredient(
        restaurant_id, name=name, category=_ingredient_label(data.get("category"), 40),
        unit=_ingredient_label(data.get("unit"), 20), **amounts)
    return jsonify(ok=True, id=ingredient_id)


@admin_bp.route("/admin/inventory/ingredients/<int:restaurant_id>/<int:ingredient_id>", methods=["POST"])
@admin_required
def update_ingredient_route(restaurant_id, ingredient_id, current_user):
    import inventory_ledger
    data = request.get_json(silent=True) or {}
    fields = {}
    for key, most in (("name", 80), ("category", 40), ("unit", 20)):
        if key in data:
            fields[key] = _ingredient_label(data[key], most)
    if "name" in fields and not fields["name"]:
        return jsonify(ok=False, error="An ingredient needs a name."), 400
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
    data = request.get_json(silent=True) or {}
    name = _ingredient_label(data.get("name"), 120)
    if not name:
        return jsonify(ok=False, error="Menu item name required"), 400
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
    data = request.get_json(silent=True) or {}
    try:
        ingredient_id = int(str(data.get("ingredient_id") or "").strip())
    except (TypeError, ValueError):
        return jsonify(ok=False, error="Pick an ingredient."), 400
    # A bad quantity used to 500, or be reported as "belongs to a different
    # restaurant" (fix round #142).
    qty_per_unit, err = _ingredient_amount(data, "qty_per_unit", None, positive=True, most=10000.0)
    if err or qty_per_unit is None:
        return jsonify(ok=False, error="The quantity per sale must be a number more than 0."), 400
    rid = _restaurant_of_menu_item(menu_item_id)
    if rid is None:
        return jsonify(ok=False, error="No such menu item."), 404
    # ingredient_id is caller-supplied and must belong to the same restaurant
    # as the dish — a recipe that reaches across locations silently costs one
    # location's plate from another's prices.
    row_id = inventory_ledger.add_recipe_ingredient(rid, menu_item_id, ingredient_id, qty_per_unit)
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
    data = request.get_json(silent=True) or {}
    business_date_str = data.get("business_date")
    try:
        business_date = _date.fromisoformat(str(business_date_str)) if business_date_str else (_date.today() - _td(days=1))
    except ValueError:
        # A bad date used to 500 the request (fix round #142).
        return jsonify(ok=False, error="Pick a business date."), 400
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
    """A client's shifts or inventory CSV, loaded by an admin into THAT
    client's restaurant — the one the URL names. The console's data page
    posted to the owner's /client/upload-data instead, which wrote into the
    admin's own home restaurant; and this route, which nothing called, passed
    data_type unchecked into the SQL text and skipped every validation the
    owner route applies (fix round #19, #130). It now runs the owner route's
    own body (client_api._do_upload_data).

    Form: data_type (shifts | inventory), csv_file, source (upload | manual)."""
    import client_api
    if not get_restaurant(restaurant_id):
        return jsonify(ok=False, error="Restaurant not found"), 404
    data_type = request.form.get("data_type")
    if data_type not in ("shifts", "inventory"):
        return jsonify(ok=False, error="Choose shifts or inventory data."), 400
    f = request.files.get("csv_file")
    if f is None and (request.form.get("csv_content") or "").strip():
        # The pasted-text shape this route used to take.
        from werkzeug.datastructures import FileStorage
        f = FileStorage(io.BytesIO(request.form["csv_content"].encode("utf-8")), filename="data.csv")
    return client_api._do_upload_data(restaurant_id, data_type, f, current_user,
                                      source=(request.form.get("source") or "upload"), operator=True)

# ── The legacy client-settings page (templates/client_settings.html) ─────────
#
# Every save used to post ~60 fields and write each one, so an admin fixing a
# typo in the voice notes reverted whatever the owner had changed since the
# page loaded, reset past_due/internal billing to 'trial' (the select offered
# four states and the browser submitted the first), and cleared an RPOWER POS
# label the select didn't list — while week_start_day, which the form did
# send, was silently dropped because it wasn't in the route's field list
# (fix round #8, #113). Now the page sends only the fields the admin touched,
# with the row version and the values it loaded; the server refuses a key it
# doesn't know, and refuses a touched field somebody else changed in the
# meantime (409) instead of reverting it.

# Every billing state the system itself writes: the Stripe webhooks (active,
# past_due, paused, churned), provisioning (active), the boot seed and the
# review account (internal), and a new restaurant's default (trial). A stored
# value outside this list is still rendered as its own option, and a save
# that doesn't touch the select never sends it.
SETTINGS_BILLING_STATES = ("trial", "active", "past_due", "paused", "churned", "internal")
SETTINGS_BILLING_LABELS = {"trial": "Trial", "active": "Active", "past_due": "Past due", "paused": "Paused",
                           "churned": "Churned", "internal": "Internal (not a client)"}
# The POS label (restaurants.pos_system). RPOWER is what rpower_routes writes
# on connect; it was missing here, so saving an RPOWER client blanked it.
SETTINGS_POS_SYSTEMS = ("Toast", "Square", "RPOWER", "Clover", "Lightspeed", "Aloha / NCR", "Revel",
                        "TouchBistro", "Other / Manual")
SETTINGS_INVENTORY_FREQUENCIES = ("weekly", "biweekly", "monthly")
SETTINGS_DIGEST_DAYS = ("monday", "tuesday", "wednesday", "thursday", "friday", "saturday", "sunday")
_SETTINGS_WEEKDAYS = ("Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday")
_TZ_LABELS = {"America/New_York": "Eastern (New York)", "America/Chicago": "Central (Chicago)",
              "America/Denver": "Mountain (Denver)", "America/Phoenix": "Arizona (Phoenix)",
              "America/Los_Angeles": "Pacific (Los Angeles)", "America/Anchorage": "Alaska (Anchorage)",
              "Pacific/Honolulu": "Hawaii (Honolulu)"}

# Keys the save understands besides the fields themselves.
_SETTINGS_CONTROL_KEYS = frozenset({"touched", "expected_version", "base", "billing_status_reason"})

# The two targets this page sets: label, lowest, highest. The labor target
# keeps the admin's stricter 5–60 (SCHED-36); food cost takes the owner
# route's bounds (strategy_routes._TARGET_BOUNDS). Written, never judged here.
_SETTINGS_TARGETS = {"labor_target_pct": ("Labor target", 5.0, 60.0), "food_cost_target": ("Food cost target", 5.0, 80.0)}

# Every field the page loads and may send back, in the order a conflict
# message lists them. weekly_revenue_target is not a column: it is stored as
# the monthly it implies (models.monthly_from_weekly).
SETTINGS_FIELDS = (
    "name", "owner_email", "owner_name", "owner_phone", "location_group", "location_name", "sign_off_name",
    "timezone", "module_reviews", "module_labor", "module_inventory", "module_marketing",
    "google_place_id", "yelp_business_id", "reviews_live", "digest_day", "digest_enabled",
    "hourly_rate", *_SETTINGS_TARGETS, "week_start_day", "monthly_revenue_target", "weekly_revenue_target",
    "hours_notes", "sched_notes", "section_count", "delivery_pct", "daypart_split", "role_minimums_json",
    "cut_floor_default", "role_rates_json", "close_times_json", "role_close_buffer_json",
    "neighborhood", "vibe", "known_for", "voice_notes", "never_say", "skip_holidays", "custom_competitors",
    "menu_notes", "menu_url", "pos_system", "inventory_frequency", "delivery_days", "inventory_notes",
    "waste_target_pct", "billing_status", "internal_notes",
    # Not on the page (the owner's Alert Settings own them); still accepted
    # from a direct call so an older caller isn't refused.
    "alert_1star", "alert_2star", "alert_health", "alert_neg_spike", "alert_negative_trend",
    "alert_no_response", "urgent_via_email", "urgent_via_sms", "alert_5star", "alert_rating_threshold",
    "alert_rating_floor", "alert_labor_over",
)

_SETTINGS_NAMES = {
    "name": "Restaurant name", "owner_email": "Owner email", "owner_name": "Owner name",
    "owner_phone": "Owner phone", "location_group": "Location group", "location_name": "Location name",
    "sign_off_name": "Sign-off name", "timezone": "Timezone", "google_place_id": "Google Place ID",
    "yelp_business_id": "Yelp business ID", "reviews_live": "Review fetching", "digest_day": "Digest day",
    "digest_enabled": "Weekly digest", "hourly_rate": "Blended hourly rate",
    "week_start_day": "Payroll week start", "monthly_revenue_target": "Monthly revenue target",
    "weekly_revenue_target": "Weekly revenue target", "hours_notes": "Hours & shift rules",
    "sched_notes": "Scheduling notes", "section_count": "Dining sections", "delivery_pct": "Delivery %",
    "daypart_split": "Daypart split", "role_minimums_json": "Role minimums",
    "cut_floor_default": "Never cut below", "role_rates_json": "Per-role hourly rates",
    "close_times_json": "Close time per day", "role_close_buffer_json": "Role after-close allowance",
    "neighborhood": "Neighborhood", "vibe": "Vibe", "known_for": "Known for", "voice_notes": "Brand voice notes",
    "never_say": "Never say", "skip_holidays": "Skipped holidays", "custom_competitors": "Custom competitors",
    "menu_notes": "Menu notes", "menu_url": "Menu URL", "pos_system": "POS system",
    "inventory_frequency": "Inventory frequency", "delivery_days": "Delivery days",
    "inventory_notes": "Inventory notes",
    "waste_target_pct": "Waste target", "billing_status": "Billing status", "internal_notes": "Internal notes",
    "alert_rating_floor": "Rating alert floor", **{k: v[0] for k, v in _SETTINGS_TARGETS.items()},
    "module_reviews": "Review Intelligence", "module_labor": "Labor Optimizer",
    "module_inventory": "Food Cost Control", "module_marketing": "Marketing Autopilot",
}

_REFUSE_BLANK = object()


class _SettingsError(ValueError):
    """A field the save refuses, with a sentence the page shows as-is."""


def _label(key):
    return _SETTINGS_NAMES.get(key, key.replace("_", " "))


def _settings_str(data, key):
    raw = data.get(key)
    if raw is None:
        return ""
    if isinstance(raw, (dict, list)):
        raise _SettingsError(f"{_label(key)} must be text.")
    return str(raw).strip()


def _settings_required(data, key, max_len):
    text = _settings_str(data, key)[:max_len]
    if not text:
        raise _SettingsError(f"{_label(key)} is required.")
    return text


def _settings_number(data, key, lo, hi, blank=_REFUSE_BLANK, integer=False):
    """A finite number in [lo, hi] — refused, never clamped, so a typo is
    seen rather than silently saved. `blank` is what an empty value stores
    (refused when not given)."""
    raw = data.get(key)
    if raw is None or (isinstance(raw, str) and not raw.strip()):
        if blank is _REFUSE_BLANK:
            raise _SettingsError(f"{_label(key)} is required.")
        return blank
    if isinstance(raw, (bool, dict, list)):
        raise _SettingsError(f"{_label(key)} must be a number.")
    import math
    try:
        value = float(str(raw).strip())
    except (TypeError, ValueError):
        raise _SettingsError(f"{_label(key)} must be a number.")
    if not math.isfinite(value) or not lo <= value <= hi:
        raise _SettingsError(f"{_label(key)} must be between {lo:g} and {hi:g}.")
    if integer:
        if value != int(value):
            raise _SettingsError(f"{_label(key)} must be a whole number.")
        return int(value)
    return value


def _settings_flag(data, key):
    raw = data.get(key)
    if raw in (True, 1, "1", "true", "on", "yes"):
        return 1
    if raw in (False, 0, "0", "false", "off", "no", None, ""):
        return 0
    raise _SettingsError(f"{_label(key)} must be on or off.")


def _settings_json_object(data, key):
    """The parsed object, or None for a blank box. The readers
    (models.get_role_rates, get_close_times, get_role_close_buffers,
    labor._role_minimums_dict) fall back to defaults on anything they can't
    read, so a malformed override saved as "Saved" and then did nothing
    (ROUTES-25)."""
    import json as _json
    text = _settings_str(data, key)
    if not text:
        return None
    try:
        obj = _json.loads(text)
    except ValueError:
        raise _SettingsError(f"{_label(key)} isn't valid JSON. Check the quotes and commas, or clear the box.")
    if not isinstance(obj, dict):
        raise _SettingsError(f"{_label(key)} must be a JSON object, like the example in the box.")
    return obj


def _settings_role_numbers(data, key, lo, hi, integer):
    import json as _json
    import math
    obj = _settings_json_object(data, key)
    if obj is None:
        return None
    out = {}
    for role, value in obj.items():
        role = " ".join(str(role or "").split())[:60]
        if not role:
            raise _SettingsError(f"{_label(key)}: every entry needs a role name.")
        try:
            num = float(value)
        except (TypeError, ValueError):
            raise _SettingsError(f"{_label(key)}: the value for {role} must be a number.")
        if isinstance(value, bool) or not math.isfinite(num) or not lo <= num <= hi:
            raise _SettingsError(f"{_label(key)}: the value for {role} must be between {lo:g} and {hi:g}.")
        if integer and num != int(num):
            raise _SettingsError(f"{_label(key)}: the value for {role} must be a whole number.")
        out[role] = int(num) if integer else round(num, 2)
    return _json.dumps(out) if out else None


def _settings_close_times(data, key):
    import json as _json
    import re as _re
    obj = _settings_json_object(data, key)
    if obj is None:
        return None
    canon = {d.lower(): d for d in _SETTINGS_WEEKDAYS}
    out = {}
    for day, value in obj.items():
        name = canon.get(str(day or "").strip().lower())
        if not name:
            raise _SettingsError(f"{_label(key)}: “{day}” isn't a day name. Use Sunday to Saturday.")
        text = str(value or "").strip()
        if not _re.match(r"^\d{1,2}(:\d{2})?\s*([ap]\.?m\.?)?$", text, _re.I):
            raise _SettingsError(f"{_label(key)}: “{text}” for {name} isn't a time like 9:00pm or 21:00.")
        out[name] = text
    return _json.dumps(out) if out else None


def _settings_email(data, key):
    import re as _re
    text = _settings_str(data, key)
    if not text or len(text) > 254 or not _re.match(r"^[^@\s,;<>]+@[^@\s,;<>]+\.[^@\s,;<>]+$", text):
        raise _SettingsError(f"{_label(key)} must be one email address.")
    return text


def _settings_timezone(data, key):
    """A real IANA zone name, or refused. A typo used to be stored as
    Chicago, which silently moved every "today" the restaurant sees."""
    text = _settings_str(data, key)
    try:
        from zoneinfo import ZoneInfo
        ZoneInfo(text)
    except Exception:
        raise _SettingsError("Pick the restaurant's timezone from the list.")
    return text


def _settings_choice(data, key, choices, blank=_REFUSE_BLANK):
    text = _settings_str(data, key)
    if not text and blank is not _REFUSE_BLANK:
        return blank
    if text not in choices:
        raise _SettingsError(f"{_label(key)}: pick one of the listed options.")
    return text


def _settings_url(data, key):
    text = sanitize(_settings_str(data, key))
    if not text:
        return None
    if not text.lower().startswith(("http://", "https://")) or any(c.isspace() for c in text):
        raise _SettingsError(f"{_label(key)} must be a web address starting with https://.")
    return text


def _settings_place_id(data, key):
    text = _settings_str(data, key)
    if not text:
        return None
    if len(text) > 300 or any(c.isspace() for c in text):
        raise _SettingsError("Google Place ID has spaces in it. Paste just the ID (it starts with ChIJ).")
    return text


_SETTINGS_ALERT_FLAGS = ("alert_1star", "alert_2star", "alert_health", "alert_neg_spike", "alert_negative_trend",
                         "alert_no_response", "urgent_via_email", "urgent_via_sms", "alert_5star",
                         "alert_rating_threshold", "alert_labor_over")

# field -> parser(data, key). The owner's own targets route
# (strategy_routes._TARGET_BOUNDS) is the reference for the numeric bounds;
# the labor target keeps the admin's stricter 5–60 (SCHED-36).
_SETTINGS_PARSERS = {
    "name": lambda d, k: _settings_required(d, k, 200),
    "owner_email": _settings_email,
    "owner_name": lambda d, k: sanitize(_settings_str(d, k), max_len=120),
    "owner_phone": lambda d, k: _settings_str(d, k)[:40] or None,
    "location_group": lambda d, k: _settings_str(d, k)[:120] or None,
    "location_name": lambda d, k: _settings_str(d, k)[:120] or None,
    "sign_off_name": lambda d, k: _settings_str(d, k)[:120] or None,
    "timezone": _settings_timezone,
    "module_reviews": _settings_flag,
    "module_labor": _settings_flag,
    "module_inventory": _settings_flag,
    "module_marketing": _settings_flag,
    "google_place_id": _settings_place_id,
    "yelp_business_id": lambda d, k: _settings_str(d, k)[:200] or None,
    "reviews_live": _settings_flag,
    "digest_day": lambda d, k: _settings_choice(d, k, SETTINGS_DIGEST_DAYS),
    "digest_enabled": _settings_flag,
    "hourly_rate": lambda d, k: _settings_number(d, k, 2.0, 250.0, blank=26.0),
    "week_start_day": lambda d, k: _settings_number(d, k, 0, 6, integer=True),
    "monthly_revenue_target": lambda d, k: _settings_number(d, k, 0.0, 100000000.0, blank=0.0),
    "weekly_revenue_target": lambda d, k: _settings_number(d, k, 0.0, 20000000.0, blank=0.0),
    "hours_notes": lambda d, k: sanitize(_settings_str(d, k), max_len=2000),
    "sched_notes": lambda d, k: sanitize(_settings_str(d, k), max_len=2000),
    "section_count": lambda d, k: _settings_number(d, k, 1, 30, blank=None, integer=True),
    # 0% is a real answer ("no delivery"); the page used to send it as null.
    "delivery_pct": lambda d, k: _settings_number(d, k, 0, 100, blank=None, integer=True),
    "daypart_split": lambda d, k: _settings_str(d, k)[:200] or None,
    "role_minimums_json": lambda d, k: _settings_role_numbers(d, k, 0, 50, integer=True),
    "role_rates_json": lambda d, k: _settings_role_numbers(d, k, 2.0, 250.0, integer=False),
    "close_times_json": _settings_close_times,
    "role_close_buffer_json": lambda d, k: _settings_role_numbers(d, k, 0, 240, integer=True),
    "neighborhood": lambda d, k: _settings_str(d, k)[:200] or None,
    "vibe": lambda d, k: sanitize(_settings_str(d, k)),
    "known_for": lambda d, k: sanitize(_settings_str(d, k)),
    "voice_notes": lambda d, k: sanitize(_settings_str(d, k)),
    "never_say": lambda d, k: sanitize(_settings_str(d, k)),
    "skip_holidays": lambda d, k: sanitize(_settings_str(d, k)),
    "custom_competitors": lambda d, k: sanitize(_settings_str(d, k)),
    "menu_notes": lambda d, k: sanitize(_settings_str(d, k), max_len=2000),
    "menu_url": _settings_url,
    "pos_system": lambda d, k: _settings_choice(d, k, SETTINGS_POS_SYSTEMS, blank=None),
    "inventory_frequency": lambda d, k: _settings_choice(d, k, SETTINGS_INVENTORY_FREQUENCIES),
    "delivery_days": lambda d, k: _settings_str(d, k)[:200] or None,
    "inventory_notes": lambda d, k: sanitize(_settings_str(d, k)),
    "waste_target_pct": lambda d, k: _settings_number(d, k, 0.0, 50.0, blank=None),
    "billing_status": lambda d, k: _settings_choice(d, k, SETTINGS_BILLING_STATES),
    "internal_notes": lambda d, k: sanitize(_settings_str(d, k)),
    "alert_rating_floor": lambda d, k: _settings_number(d, k, 1.0, 5.0, blank=4.0),
    **{flag: _settings_flag for flag in _SETTINGS_ALERT_FLAGS},
    **{key: (lambda lo, hi: lambda d, k: _settings_number(d, k, lo, hi, blank=30.0))(lo, hi)
       for key, (_name, lo, hi) in _SETTINGS_TARGETS.items()},
}


def _parse_client_settings(data):
    """{field: value} for every settings field in `data`, validated.
    Raises _SettingsError with the sentence to show. cut_floor_default is
    parsed by the route (schedule_rules owns its rule)."""
    return {key: _SETTINGS_PARSERS[key](data, key) for key in SETTINGS_FIELDS
            if key in data and key != "cut_floor_default"}


def _settings_same(a, b):
    """Stored and loaded values compared the way a person would: blank and
    None are one value, "30" and 30.0 are one value."""
    def norm(v):
        if v is None:
            return None
        if isinstance(v, bool):
            return float(int(v))
        if isinstance(v, (int, float)):
            return round(float(v), 6)
        s = str(v).strip()
        if not s:
            return None
        try:
            return round(float(s), 6)
        except ValueError:
            return s
    return norm(a) == norm(b)


def settings_loaded_values(restaurant) -> dict:
    """The stored value of every field the settings page shows, as it was
    when the page rendered — sent back with a save so the server can tell
    "the admin changed this" from "somebody changed this since"."""
    return {k: getattr(restaurant, k, None) for k in SETTINGS_FIELDS if k != "weekly_revenue_target"}


def _listing_shared_with_live(restaurant_id, place_id):
    """The live restaurant that also holds this listing, if any. Only a demo
    can share one (place_id_conflict refuses it for a live row), and a demo
    that shares a live client's listing must not fetch it: every review
    would be stored, drafted and alerted on twice."""
    return place_id_conflict((place_id or "").strip(), exclude_id=restaurant_id) if place_id else None


def reviews_live_decision(restaurant_id, current, fields, explicit=None):
    """(value or None for "leave it", error or None): the review-fetching
    flag a save leaves. The admin's explicit switch wins; otherwise
    create-client's rule runs on every save that sets the Place ID or the
    Reviews module — a Place ID plus Reviews means fetching — and clearing
    the Place ID with no Business Profile connected turns it off (there is
    nothing left to fetch, and every surface reads the flag as "Google
    reviews connected"). Fix round #110: the flag was only ever set at
    creation, so a client created before its Place ID was known, or
    provisioned from a checkout, never fetched and nothing could turn it on."""
    place = fields["google_place_id"] if "google_place_id" in fields else getattr(current, "google_place_id", None)
    reviews_on = fields["module_reviews"] if "module_reviews" in fields else getattr(current, "module_reviews", 0)
    has_gbp = bool(getattr(current, "gmb_refresh_token", None))
    was_live = int(getattr(current, "reviews_live", 0) or 0)
    shared = _listing_shared_with_live(restaurant_id, place)
    if explicit is not None:
        if explicit and not (place or has_gbp):
            return None, ("Add the Google Place ID (or connect the owner's Google Business Profile) "
                          "before turning on review fetching.")
        if explicit and shared and not has_gbp:
            return None, (f"That Google listing belongs to {shared}. A demo that shares it doesn't fetch "
                          f"reviews, or every one would be stored, drafted and alerted on twice.")
        return int(bool(explicit)), None
    if "google_place_id" in fields or "module_reviews" in fields:
        if place and int(reviews_on or 0) and not shared and not was_live:
            return 1, None
        if not place and not has_gbp and was_live:
            return 0, None
    return None, None


@admin_bp.route("/admin/client-settings/<int:restaurant_id>")
@admin_required
def client_settings_page(restaurant_id, current_user):
    # The version first, then the row: a write landing between the two reads
    # leaves an older version beside newer values, which a later save checks
    # field by field rather than reverting.
    from models import restaurant_version
    row_version = restaurant_version(restaurant_id)
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
    # Every stored value gets an option, so a select can't submit its first
    # option over a value it doesn't list (fix round #8).
    import time_utils
    tz = restaurant.timezone or "America/Chicago"
    timezone_choices = [(z, _TZ_LABELS.get(z, z)) for z in time_utils.COMMON_TIMEZONES]
    if tz not in time_utils.COMMON_TIMEZONES:
        timezone_choices.append((tz, f"{tz} (as stored)"))
    stored_billing = restaurant.billing_status or ""
    billing_choices = [(s, SETTINGS_BILLING_LABELS[s]) for s in SETTINGS_BILLING_STATES]
    if stored_billing not in SETTINGS_BILLING_STATES:
        billing_choices.insert(0, (stored_billing, f"{stored_billing} (as stored)" if stored_billing
                                   else "Not set (as stored)"))
    pos_choices = list(SETTINGS_POS_SYSTEMS)
    if restaurant.pos_system and restaurant.pos_system not in pos_choices:
        pos_choices.append(restaurant.pos_system)
    # Prices from the one price list (pricing.py), not the launch prices the
    # page used to print ($500 setup, $300/mo per module) — fix round #73.
    import pricing
    price_ladder = [{"modules": n, "setup": pricing.money(pricing.TIERS[n]["setup"]),
                     "monthly": pricing.money(pricing.TIERS[n]["monthly"])} for n in sorted(pricing.TIERS)]
    from notify import MAX_ALERT_CONTACTS
    return render_template('client_settings.html',
        current_user=current_user,
        restaurant=restaurant,
        client_data=client_data,
        staff_notes=staff_notes,
        alert_contacts=alert_contacts,
        count_freshness=count_freshness,
        bench_hints=bench_hints,
        row_version=row_version,
        loaded_settings=settings_loaded_values(restaurant),
        timezone_choices=timezone_choices,
        billing_choices=billing_choices,
        pos_choices=pos_choices,
        price_ladder=price_ladder,
        settings_fields=list(SETTINGS_FIELDS),
        max_alert_contacts=MAX_ALERT_CONTACTS,
        shared_listing=_listing_shared_with_live(restaurant_id, restaurant.google_place_id))


@admin_bp.route("/admin/client-settings/<int:restaurant_id>", methods=["POST"])
@admin_required
def save_client_settings(restaurant_id, current_user):
    """Write the fields this save names — and only those.

    Body: the touched fields, plus `expected_version` (the row_version the
    page loaded), `base` ({field: value as loaded} for each field sent),
    `touched` (the target fields typed in, see TARGET_SOURCE_FIELDS) and,
    with a billing-status override, `billing_status_reason`.

    400: an unknown key or an invalid value (the sentence says which).
    409: a field this save changes was changed by somebody else since the
    page loaded — {conflict: true, fields, labels, current, current_version}.
    A row that moved on only in fields this save doesn't touch (a POS sync, a
    token refresh, the owner editing something else) saves normally: only
    the named fields are written, so nothing of theirs is reverted.
    200: {ok, version, saved: {field: stored value}, changed: [...]}.
    """
    from models import (update_restaurant, get_restaurant as _gr_cs, restaurant_version, StaleWrite,
                        expected_version_from)
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify(ok=False, error="Send the settings as a JSON object."), 400
    unknown = sorted(k for k in data if k not in SETTINGS_FIELDS and k not in _SETTINGS_CONTROL_KEYS)
    if unknown:
        # A key this route doesn't write used to be dropped while the page
        # said "Saved" — week_start_day for months (fix round #113).
        return jsonify(ok=False, unknown_fields=unknown,
                       error="These settings aren't saved from this page: " + ", ".join(unknown) + "."), 400
    # Version before row, as in client_settings_page.
    version_now = restaurant_version(restaurant_id)
    current = _gr_cs(restaurant_id)
    if not current:
        return jsonify(ok=False, error="Restaurant not found"), 404

    def _given_or_current(key):
        """The value this save will leave in place: the payload's, or the
        stored one when the payload does not mention the field."""
        if key in data:
            return str(data.get(key) or "").strip()
        return (getattr(current, key, "") or "").strip()

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
                                           f"{_sr_cut.CUT_FLOOR_MAX}."), 400

    try:
        fields = _parse_client_settings(data)
    except _SettingsError as e:
        return jsonify(ok=False, error=str(e)), 400
    if "cut_floor_default" in data:
        fields["cut_floor_default"] = _cut_floor
    sent = set(fields)          # what the admin sent, as opposed to what this save derives

    # The Place ID and the location group are tenancy boundaries, checked
    # whenever this save sets them. Only the STORED demo flag exempts a row:
    # the payload used to be able to claim is_demo past this check without
    # the flag ever being written.
    if "google_place_id" in fields:
        place_clash = place_id_conflict(fields["google_place_id"] or "", exclude_id=restaurant_id)
        if place_clash and not int(getattr(current, "is_demo", 0) or 0):
            return jsonify(ok=False, error=(
                f"That Google listing is already connected to {place_clash}. Two live "
                f"restaurants on one listing both pull the same reviews and only one of "
                f"them can own any given review."
            )), 400
    if "location_group" in fields or "owner_email" in fields:
        conflict = location_group_conflict(_given_or_current("location_group"), _given_or_current("owner_email"),
                                           exclude_id=restaurant_id)
        if conflict:
            return jsonify(ok=False, error=(
                f"Location group “{_given_or_current('location_group')}” already belongs to "
                f"{conflict}. Pick a different group name — locations in a group share data and billing."
            )), 400

    # Review fetching (fix round #110).
    explicit_live = fields.pop("reviews_live") if "reviews_live" in fields else None
    live, live_err = reviews_live_decision(restaurant_id, current, fields, explicit_live)
    if live_err:
        return jsonify(ok=False, error=live_err), 400
    if live is not None:
        fields["reviews_live"] = live

    # The billing status is Stripe's; changing it here is an explicit,
    # reasoned override, recorded with the reason (fix round #8). Re-sending
    # the stored state changes nothing.
    billing_reason = None
    if "billing_status" in fields and fields["billing_status"] != (current.billing_status or ""):
        billing_reason = sanitize(_settings_str(data, "billing_status_reason"), max_len=300)
        if not billing_reason:
            return jsonify(ok=False, error=(
                "Changing the billing status overrides what Stripe set. Give a reason for the "
                "override — it is kept with the change.")), 400
        if (current.billing_status or "") == "paused":
            # The owner's own resume clears it the same way.
            fields["paused_until"] = None

    # A value equal to what is stored writes nothing: re-sending a value is
    # not an edit, and must not count as a conflict either.
    write = {k: v for k, v in fields.items()
             if k == "weekly_revenue_target" or not _settings_same(getattr(current, k, None), v)}

    expected = expected_version_from(data)
    if expected is not None and expected != version_now:
        # The row moved on since the page loaded. Refuse only when a field
        # this save changes is one that moved: what is stored now against
        # what the page loaded (`base`). A field sent without its loaded
        # value can't be checked, so it is refused rather than guessed.
        base = data.get("base") if isinstance(data.get("base"), dict) else {}
        conflicts = []
        for key in write:
            if key not in sent:
                continue
            column = "monthly_revenue_target" if key == "weekly_revenue_target" else key
            if column not in base or not _settings_same(getattr(current, column, None), base.get(column)):
                conflicts.append(column)
        if conflicts:
            order = {k: i for i, k in enumerate(SETTINGS_FIELDS)}
            conflicts = sorted(set(conflicts), key=lambda k: order.get(k, len(order)))
            return jsonify(ok=False, conflict=True, fields=conflicts,
                           labels={k: _label(k) for k in conflicts},
                           current={k: getattr(current, k, None) for k in conflicts},
                           current_version=version_now,
                           error=("Changed by somebody else since this page loaded: "
                                  + ", ".join(_label(k) for k in conflicts)
                                  + ". Reload to see the change, then make yours again.")), 409

    # A target or the blended rate the admin actually edited is the
    # owner's, even when it is typed back as the default 30% or $26 —
    # the form names the fields it saw touched (re-audit #45, R2-21).
    # An untouched field re-sent with the rest confirms nothing.
    from models import TARGET_SOURCE_FIELDS as _TSF
    _touched = data.get("touched") if isinstance(data.get("touched"), list) else []
    for _tf in _touched:
        if _tf in _TSF and _tf in fields:
            write[_TSF[_tf]] = "set"

    before = {k: getattr(current, k, None) for k in write}
    if write:
        try:
            # Compare-and-swap on the version checked above, so a write that
            # lands between that check and this one is refused too.
            update_restaurant(restaurant_id, write,
                              expected_version=(version_now if expected is not None else None))
        except StaleWrite as e:
            return jsonify(ok=False, conflict=True, fields=[], current_version=e.current_version,
                           error=e.user_message), 409
        _record_settings_change(restaurant_id, current_user, before, write, billing_reason)
    saved_row = _gr_cs(restaurant_id)
    saved = {k: getattr(saved_row, k, None) for k in fields if k != "weekly_revenue_target"}
    if "weekly_revenue_target" in fields:
        saved["monthly_revenue_target"] = getattr(saved_row, "monthly_revenue_target", None)
    return jsonify(ok=True, version=restaurant_version(restaurant_id), saved=saved, changed=sorted(write))


def _record_settings_change(restaurant_id, current_user, before, after, billing_reason=None):
    """Who changed what, from what: an activity_log row for the client's
    history and an admin_events row for the operator's audit trail. The old
    record was {"by": username} with no diff (ROUTES-4)."""
    actor = current_user.get("username") or current_user.get("email") or "admin"

    def short(v):
        return (v[:200] + "…") if isinstance(v, str) and len(v) > 200 else v
    diff = {k: {"from": short(before.get(k)), "to": short(v)} for k, v in after.items()}
    extra = {"billing_override_reason": billing_reason} if billing_reason else {}
    try:
        from models import log_event
        log_event(restaurant_id, "admin_settings_update", {"by": actor, "changed": diff, **extra})
    except Exception as e:
        _ops.capture(e, job="admin_settings_audit", context=f"restaurant_id={restaurant_id}")
    import admin_events
    if "billing_status" in after:
        admin_events.record("admin", "billing_status.override", restaurant_id=restaurant_id,
                            summary=(f"{actor}: {before.get('billing_status') or 'unset'} → "
                                     f"{after['billing_status']} — {billing_reason}")[:300],
                            payload={"actor": actor, "before": before.get("billing_status"),
                                     "after": after["billing_status"], "reason": billing_reason})
    admin_events.record("admin", "client_settings.update", restaurant_id=restaurant_id,
                        summary=(f"{actor} changed " + ", ".join(sorted(after)))[:300],
                        payload={"actor": actor, "changed": diff})


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
    nothing about it passes through Will or the database."""
    if not current_user.get("is_admin"):
        return jsonify(ok=False, error="Support accounts are read-only."), 403
    from models import create_reset_token
    conn = get_conn()
    row = conn.execute("SELECT u.email, r.name, r.owner_name FROM users u LEFT JOIN restaurants r ON r.id=u.restaurant_id "
                       "WHERE u.id=?", (user_id,)).fetchone()
    conn.close()
    if not row or not row["email"]:
        return jsonify(ok=False, error="No email on that login."), 404
    token = create_reset_token(row["email"])
    if not token:
        return jsonify(ok=False, error="That login is inactive."), 409
    try:
        from emails import send_password_reset_email
        send_password_reset_email(row["email"], f"https://dashboard.cavnar.ai/reset-password/{token}")
    except Exception as e:
        return jsonify(ok=False, error=f"Couldn't send: {e}"), 502
    return jsonify(ok=True)


@admin_bp.route("/admin/reset-password/<int:user_id>", methods=["POST"])
@admin_required
def reset_password(user_id, current_user):
    from models import reset_user_password
    import secrets, string
    data = request.get_json()
    new_pw = data.get("password","").strip()
    if not new_pw:
        # Auto-generate if not provided
        new_pw = ''.join(secrets.choice(string.ascii_letters + string.digits) for _ in range(10))
    if len(new_pw) < 8:
        return jsonify(ok=False, error="Password must be at least 8 characters")
    import security as _sec
    if _sec.password_pwned(new_pw):
        return jsonify(ok=False, error=_sec.PWNED_MESSAGE)
    if not current_user.get("is_admin"):
        return jsonify(ok=False, error="Support accounts are read-only."), 403
    reset_user_password(user_id, new_pw)
    # Optionally email the new password
    if data.get("send_email"):
        try:
            conn = get_conn()
            row = conn.execute(
                "SELECT u.email, r.name FROM users u JOIN restaurants r ON u.restaurant_id=r.id WHERE u.id=?",
                (user_id,)
            ).fetchone()
            conn.close()
            if row:
                _emails.deliver_or_raise(email_type="admin_password_reset", payload={
                    "from": _emails.sender("client"),
                    "to": [row["email"]],
                    "subject": "Your Cavnar AI password has been reset",
                    "html": _html_doc(f"""<div style="background:#f7f4ef;width:100%;padding:40px 20px;box-sizing:border-box">
                    <div style="font-family:-apple-system,BlinkMacSystemFont,'Helvetica Neue',Arial,sans-serif;max-width:500px;margin:0 auto;background:white;border-radius:12px;padding:28px 24px;box-sizing:border-box">
                        <img src="https://dashboard.cavnar.ai/static/brand/wordmark-dark-email.png" width="150" height="26" alt="Cavnar AI" style="display:block;width:150px;height:26px;border:0;outline:none;margin:0 0 20px">
                        <h3 style="color:#0e0c0a">Password reset</h3>
                        <p>Hi — your Cavnar AI dashboard password has been reset.</p>
                        <div style="background:#f7f4ef;padding:14px;border-radius:8px;margin:16px 0">
                            <p><strong>URL:</strong> <a href="https://dashboard.cavnar.ai">dashboard.cavnar.ai</a></p>
                            <p><strong>New password:</strong> {_emails.esc(new_pw)}</p>
                        </div>
                        <p>Log in and update your password in the Account tab.</p>
                        <p style="color:#7a736a;font-size:12px">— Will Cavnar · will@cavnar.ai</p>
                    </div>
                    </div>"""),
                })
        except Exception as e:
            print(f"Reset email failed: {e}")
    return jsonify(ok=True, password=new_pw)

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
    user_id = _principal_login_id(restaurant_id)
    if not user_id:
        return jsonify(ok=False, error="This restaurant has no owner login to reset.")
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
    from models import log_activity
    data = request.get_json()
    log_activity(current_user["restaurant_id"], data.get("tab",""))
    return jsonify(ok=True)

@admin_bp.route("/admin/resend-contract/<int:restaurant_id>", methods=["POST"])
@admin_required
def resend_contract(restaurant_id, current_user):
    restaurant = get_restaurant(restaurant_id)
    if not restaurant:
        return jsonify(ok=False, error="Restaurant not found")
    try:
        mods = sum([
            1 if restaurant.module_reviews else 0,
            1 if restaurant.module_labor else 0,
            1 if restaurant.module_inventory else 0,
            1 if restaurant.module_marketing else 0,
        ])
        module_names = []
        if restaurant.module_reviews:  module_names.append("Review Intelligence")
        if restaurant.module_labor:    module_names.append("Labor Optimizer")
        if restaurant.module_inventory: module_names.append("Food Cost Control")
        if restaurant.module_marketing: module_names.append("Marketing Autopilot")
        from docusign_helper import send_contract
        result = send_contract(
            owner_email=restaurant.owner_email,
            owner_name=restaurant.owner_name or restaurant.name,
            restaurant_name=restaurant.name,
            module_count=mods,
            modules_list=", ".join(module_names),
        )
        envelope_id = result.get("envelope_id")
        from models import update_restaurant
        update_restaurant(restaurant_id, {
            "contract_status": "sent",
            "docusign_envelope_id": envelope_id,
        })
        # Log it
        try:
            from models import log_email
            log_email(restaurant_id, "contract", restaurant.owner_email, f"Service Agreement — {restaurant.name}")
        except Exception: pass
        return jsonify(ok=True)
    except Exception as e:
        print(f"Resend contract error: {e}")
        return jsonify(ok=False, error=_safe_err(e))

@admin_bp.route("/admin/resend-payment/<int:restaurant_id>", methods=["POST"])
@admin_required
def resend_payment(restaurant_id, current_user):
    restaurant = get_restaurant(restaurant_id)
    if not restaurant:
        return jsonify(ok=False, error="Restaurant not found")
    try:
        # The module KEYS, not just the count — they ride in the checkout
        # metadata so the paid subscription can grant exactly what was bought
        # instead of leaving entitlement to whatever an admin last typed.
        module_keys = [k for k, on in (
            ("reviews",   restaurant.module_reviews),
            ("labor",     restaurant.module_labor),
            ("inventory", restaurant.module_inventory),
            ("marketing", restaurant.module_marketing),
        ) if on]
        mods = len(module_keys)
        if mods == 0:
            return jsonify(ok=False, error="No modules active for this client")
        send_payment_email(
            to_email=restaurant.owner_email,
            restaurant_name=restaurant.name,
            module_count=mods,
            restaurant_id=restaurant_id,
            modules=module_keys,
        )
        try:
            log_email(restaurant_id, "payment", restaurant.owner_email, f"Payment link — {restaurant.name}")
        except Exception: pass
        return jsonify(ok=True)
    except Exception as e:
        print(f"Resend payment error: {e}")
        return jsonify(ok=False, error=_safe_err(e))

@admin_bp.route("/admin/seed-reviews/<int:restaurant_id>", methods=["POST"])
@admin_required
def seed_reviews(restaurant_id, current_user):
    """Seed sample reviews for a restaurant so client can see the dashboard working."""
    from models import save_reviews, get_pending_analysis, update_analysis, get_pending_drafts, Review
    from datetime import datetime, timedelta

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

    new_count, _ = save_reviews(reviews)

    # Analyse and draft all of them
    pending = get_pending_analysis(restaurant_id, limit=50)
    for i, r in enumerate(pending):
        sent = sentiments.get(r.rating, "neutral")
        cats = categories_map[i % len(categories_map)]
        urg  = urgencies[i % len(urgencies)]
        summary = f"Guest {'praised' if sent=='positive' else 'criticized'} the experience."
        update_analysis(r.id, sent, cats, summary, urg)

    pending_drafts = get_pending_drafts(restaurant_id, limit=50)
    restaurant = get_restaurant(restaurant_id)
    from drafter import draft_response as _draft_fn
    from models import get_approved_examples as _get_ex
    approved_examples = _get_ex(restaurant_id, limit=4)
    for r in pending_drafts:
        try:
            _draft_fn(
                r.id, r.rating, r.text, r.sentiment,
                restaurant.name,
                voice_notes=restaurant.voice_notes or "",
                restaurant_id=restaurant_id,
                approved_examples=approved_examples,
                sign_off=restaurant.sign_off_name or restaurant.name,
                never_say=restaurant.never_say or "",
            )
        except Exception as _e:
            print(f"[seed] draft error [{r.id}]: {_e}")

    return jsonify(ok=True, seeded=new_count)

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

@admin_bp.route("/admin/redraft-all/<int:restaurant_id>", methods=["POST"])
@admin_required
def redraft_all(restaurant_id, current_user):
    """Regenerate AI drafts for all existing reviews — resets non-posted to pending first."""
    restaurant = get_restaurant(restaurant_id)
    if not restaurant:
        return jsonify(ok=False, error="Restaurant not found")
    conn = get_conn()
    # Reset all drafted/pending reviews so they queue for re-drafting
    conn.execute(
        "UPDATE reviews SET response_status='pending', draft_response=NULL WHERE restaurant_id=? AND response_status IN ('drafted','pending')",
        (restaurant_id,)
    )
    conn.commit()
    conn.close()
    import threading
    def _redraft():
        try:
            from models import get_pending_drafts, get_approved_examples, get_conn as _gc
            from analyser import analyse_review
            from drafter import draft_response
            # Re-analyse anything missing sentiment
            _conn = _gc()
            unanalysed = _conn.execute(
                "SELECT id, rating, text FROM reviews WHERE restaurant_id=? AND (sentiment IS NULL OR sentiment='') AND processed=0",
                (restaurant_id,)
            ).fetchall()
            _conn.close()
            for r in unanalysed:
                try: analyse_review(r["id"], r["rating"], r["text"], restaurant_id=restaurant_id)
                except Exception: pass
            approved_examples = get_approved_examples(restaurant_id, limit=4)
            for r in get_pending_drafts(restaurant_id, limit=50):
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
                except Exception as _e:
                    print(f"[redraft-all] error [{r.id}]: {_e}")
        except Exception as e:
            print(f"[redraft-all] background error: {e}")
    threading.Thread(target=_redraft, daemon=True).start()
    return jsonify(ok=True)

@admin_bp.route("/admin/view-as/<int:restaurant_id>", methods=["GET", "POST"])
@admin_required
def view_as_client(restaurant_id, current_user):
    """Log in as a client to see exactly what they see.

    A GET only asks. It used to mint the impersonation session and swap the
    admin's cookie for it, so any page could send an admin's browser into a
    client's account with a link (SEC-34). The button on the page POSTs,
    with the same double-submit CSRF token every admin write carries."""
    user_id = _principal_login_id(restaurant_id, fallback_to_any=True)
    if not user_id:
        return "No client user found for this restaurant", 404
    if request.method != "POST":
        return _view_as_confirm_page(restaurant_id)
    # A view-as session for that user: auth.VIEW_AS_HOURS, sliding with use
    # (auth.get_session_user renews it on every request).
    from auth import VIEW_AS_HOURS
    from datetime import datetime, timezone, timedelta
    from models import get_conn as _gc
    _conn = _gc()
    token = __import__('secrets').token_urlsafe(32)
    expires = (datetime.now(timezone.utc) + timedelta(hours=VIEW_AS_HOURS)).isoformat()
    # Store the hash, not the token — same rule as auth.create_session, or
    # this impersonation session would be unreadable by get_session_user.
    from auth import hash_session_token as _hst
    # device_type marks this as an admin impersonation rather than a real
    # client sign-in. Without it the row is indistinguishable from the
    # client's own session: it shows up in their Account -> Devices list as an
    # unexplained login, and nothing in activity_log separates what Will did
    # while viewing-as from what the client did themselves.
    _conn.execute(
        "INSERT INTO sessions (token, user_id, expires_at, last_active, device_type) VALUES (?,?,?,?,?)",
        (_hst(token), user_id, expires,
         datetime.now(timezone.utc).isoformat(), "admin-view-as")
    )
    _conn.commit(); _conn.close()
    # Who opened it, and whether it may write. A support login is read-only
    # in the admin console, and a view-as it opens must be read-only too — it
    # used to be a full client session (SEC-12). See auth.get_session_user.
    from auth import record_view_as_session
    record_view_as_session(token, current_user.get("id"), read_only=not current_user.get("is_admin"))
    try:
        import admin_events
        admin_events.record("admin", "view_as_started", restaurant_id=restaurant_id,
                            summary=f"{current_user.get('username')} opened a view-as session")
    except Exception:
        pass
    resp = make_response(redirect("/"))
    # A browser-session cookie: the server's sliding deadline is the only
    # clock. A fixed max_age cut an active review off at its mark however
    # recently the admin had clicked.
    resp.set_cookie("session_token", token,
                    httponly=True, secure=config.on_railway(), samesite="Strict")
    return resp

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
        f"<h1>View as {name}?</h1><p>This opens their dashboard in this browser until {VIEW_AS_HOURS} hours after you stop using it, "
        f"signed in as their owner login. Everything you do is recorded.</p>"
        f"<form method='post' action='/admin/view-as/{int(restaurant_id)}'>"
        f"<input type='hidden' name='csrf_token' value='{_esc_va(csrf_tok)}'>"
        f"<button type='submit' class='cbtn cbtn-primary'>Open their dashboard</button></form>"
        f"<p style='margin-top:16px'><a href='/admin'>Back to the admin console</a></p>")
    resp = make_response(body)
    if not request.cookies.get(CSRF_COOKIE):
        resp.set_cookie(CSRF_COOKIE, csrf_tok, max_age=30 * 24 * 3600, httponly=False,
                        secure=config.on_railway(), samesite="Lax")
    return resp


@admin_bp.route("/admin/stop-viewing")
def stop_viewing():
    """Return to admin — delete current session and redirect to admin login."""
    token = request.cookies.get("session_token")
    if token:
        # Only delete session if it actually exists (prevents session fixation).
        # auth.get_session_user, imported at the top — models has none, and the
        # local `from models import` made this route a 500.
        if get_session_user(token):
            delete_session(token)
    resp = make_response(redirect("/login?next=/admin"))
    resp.delete_cookie("session_token")
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
    import io
    from models import safe_csv_writer
    restaurant = get_restaurant(current_user["restaurant_id"])
    reviews = get_reviews_data(current_user["restaurant_id"])
    buf = io.StringIO()
    # Reviewer names, review text and drafts are other people's words: a
    # cell starting "=" ran as a formula in the owner's spreadsheet (fix
    # round #156).
    w = safe_csv_writer(buf)
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
    """Extract the menu from a PDF, as a background job; poll
    /admin/api/menu-extract/<job_id>. The text comes back to the page to
    review — nothing is saved until the admin saves the settings. It used to
    overwrite the saved menu notes the moment the model answered, while the
    page said "review and save", and to hold a request thread for the whole
    model call (fix round #142, #153)."""
    restaurant = get_restaurant(restaurant_id)
    if not restaurant:
        return jsonify(ok=False, error="Restaurant not found"), 404
    pdf_file = request.files.get("pdf")
    if not pdf_file:
        return jsonify(ok=False, error="No PDF file uploaded"), 400
    pdf_bytes = pdf_file.read(10 * 1024 * 1024 + 1)
    if len(pdf_bytes) > 10 * 1024 * 1024:  # 10MB limit
        return jsonify(ok=False, error="PDF too large — max 10MB"), 400
    if not pdf_bytes.startswith(b"%PDF"):
        return jsonify(ok=False, error="That file isn't a PDF."), 400
    return _start_menu_extraction(restaurant_id, "pdf", _extract_menu_pdf, pdf_bytes, restaurant.name)


def _extract_menu_pdf(restaurant_id, pdf_bytes, restaurant_name):
    from competitor import fetch_menu_from_pdf_bytes
    notes = fetch_menu_from_pdf_bytes(pdf_bytes, restaurant_name, restaurant_id=restaurant_id)
    if not notes:
        return {"ok": False, "error": "Could not extract menu items from this PDF — try a text-based PDF "
                                      "rather than a scanned image"}
    return {"ok": True, "menu_notes": notes}


@admin_bp.route("/admin/fetch-menu-from-url/<int:restaurant_id>", methods=["POST"])
@admin_required
def fetch_menu_from_url_route(restaurant_id, current_user):
    """Extract the menu from a web page, as a background job; poll
    /admin/api/menu-extract/<job_id>. Returned to review, never saved here
    (the URL and the notes are saved with the settings) — fix round #142,
    #153."""
    data = request.get_json(silent=True) or {}
    url = str(data.get("url") or "").strip()
    if not url:
        return jsonify(ok=False, error="No URL provided"), 400
    if not url.lower().startswith(("http://", "https://")) or any(c.isspace() for c in url):
        return jsonify(ok=False, error="Enter the menu's web address, starting with https://."), 400
    if not get_restaurant(restaurant_id):
        return jsonify(ok=False, error="Restaurant not found"), 404
    return _start_menu_extraction(restaurant_id, "url", _extract_menu_url, url)


def _extract_menu_url(restaurant_id, url):
    from competitor import fetch_menu_from_url
    items = fetch_menu_from_url(url, restaurant_id=restaurant_id)
    if not items:
        return {"ok": False, "error": "Could not extract menu from this URL. The site may block automated "
                                      "requests or use JavaScript to load content. Try the PDF upload option "
                                      "instead, or enter items manually."}
    return {"ok": True, "menu_notes": items, "menu_url": url}


@admin_bp.route("/admin/refresh-menu-notes/<int:restaurant_id>", methods=["POST"])
@admin_required
def refresh_menu_notes(restaurant_id, current_user):
    """Menu notes from Google Places, merged with the saved ones, as a
    background job; poll /admin/api/menu-extract/<job_id>. Returned to
    review like the PDF and URL extractions — the settings save writes it
    (fix round #142, #153)."""
    restaurant = get_restaurant(restaurant_id)
    if not restaurant:
        return jsonify(ok=False, error="Restaurant not found"), 404
    if not restaurant.google_place_id:
        return jsonify(ok=False, error="No Google Place ID set for this restaurant"), 400
    return _start_menu_extraction(restaurant_id, "places", _extract_menu_places, restaurant.google_place_id,
                                  restaurant.yelp_business_id, restaurant.menu_notes or "")


def _extract_menu_places(restaurant_id, place_id, yelp_business_id, existing):
    from competitor import fetch_menu_notes_from_places
    menu_notes = fetch_menu_notes_from_places(place_id, restaurant_id=restaurant_id)
    if not menu_notes or len(menu_notes) < 30:
        # Build helpful message with where to find menu data manually
        yelp_url = f"https://www.yelp.com/biz/{yelp_business_id}" if yelp_business_id else ""
        tips = "Google Places has no menu data for this restaurant. "
        if yelp_url:
            tips += (f"Try: 1) Upload a menu PDF, 2) Paste their menu URL and click Fetch, or 3) Copy dishes "
                     f"from their Yelp page ({yelp_url}) into the notes field manually.")
        else:
            tips += ("Try: 1) Upload a menu PDF, 2) Paste their menu URL and click Fetch, or 3) Enter key "
                     "dishes manually in the notes field.")
        return {"ok": False, "error": tips}
    merged = menu_notes if not existing else menu_notes + ("\n\nAdditional notes:\n" + existing
                                                           if existing not in menu_notes else "")
    has_url = "Menu URL:" in merged
    return {"ok": True, "menu_notes": merged,
            "message": "✓ Updated from Google Places" + (" — menu URL found, dishes extracted" if has_url else "")}


RESEND_WELCOME_COOLDOWN_MINUTES = 5


@admin_bp.route("/admin/resend-welcome/<int:restaurant_id>", methods=["POST"])
@admin_required
def resend_welcome_email(restaurant_id, current_user):
    """Reset client password and resend welcome email with new credentials."""
    import secrets, string
    from models import get_restaurant, get_conn
    from auth import update_password

    restaurant = get_restaurant(restaurant_id)
    if not restaurant:
        return jsonify(ok=False, error="Restaurant not found")

    try:
        # Get client user
        conn = get_conn()
        user = conn.execute(
            "SELECT * FROM users WHERE restaurant_id=? AND is_admin=0 LIMIT 1",
            (restaurant_id,)
        ).fetchone()
        if not user:
            conn.close()
            return jsonify(ok=False, error="No client user found")
        conn.close()

        # A double-click reset the password twice, so the password in the
        # first email was dead before the client opened it (DATA-38). One
        # reset per restaurant per few minutes; a second press inside that
        # changes nothing.
        import ops as _ops_rw
        if not _ops_rw.claim_cooldown(f"resend_welcome:{restaurant_id}", RESEND_WELCOME_COOLDOWN_MINUTES):
            return jsonify(ok=True, email=restaurant.owner_email, already_sent=True,
                           message="A welcome email with a new password went out a few minutes ago, "
                                   "so nothing was changed and no second email was sent.")

        # Generate a new temporary password
        alphabet = string.ascii_letters + string.digits
        new_password = "".join(secrets.choice(alphabet) for _ in range(12))

        # update_password() (not a raw UPDATE) so this admin-issued reset
        # also stamps password_changed_at/password_strength, same as a
        # client's own change-password flow — the Security sheet's
        # "changed X ago" needs to stay accurate regardless of who reset it.
        update_password(user["id"], new_password)

        # Send welcome email with new credentials
        from emails import send_welcome_email
        send_welcome_email(
            to_email=restaurant.owner_email,
            restaurant_name=restaurant.name,
            username=user["username"],
            password=new_password,
            module_reviews=restaurant.module_reviews,
            module_labor=restaurant.module_labor,
            module_inventory=restaurant.module_inventory,
            module_marketing=restaurant.module_marketing,
            google_place_id=restaurant.google_place_id,
            owner_name=restaurant.owner_name,
        )

        return jsonify(ok=True, email=restaurant.owner_email)

    except Exception as e:
        print(f"[resend-welcome] error: {e}")
        return jsonify(ok=False, error=_safe_err(e))


@admin_bp.route("/admin/test-digest/<int:restaurant_id>", methods=["POST"])
@admin_required
def test_digest(restaurant_id, current_user):
    restaurant = get_restaurant(restaurant_id)
    if not restaurant:
        return jsonify(ok=False, error="Restaurant not found")
    try:
        from reporter import build_report_from_db, render_html
        owner_email = restaurant.owner_email
        report = build_report_from_db(restaurant_id, restaurant.name, days=7)
        html = render_html(report, restaurant.name, owner_name=restaurant.owner_name, restaurant_id=restaurant_id,
                           owner_view=True)
        _emails.deliver_or_raise(email_type="digest_preview", restaurant_id=restaurant_id, payload={
            "from": _emails.sender("client"),
            "to": [owner_email],
            "subject": f"[TEST] Your weekly review digest — {restaurant.name}",
            "html": _html_doc(html),
        })
        return jsonify(ok=True, email=owner_email)
    except Exception as e:
        return jsonify(ok=False, error=_safe_err(e))

@admin_bp.route("/admin/test-urgent/<int:restaurant_id>", methods=["POST"])
@admin_required
def test_urgent(restaurant_id, current_user):
    restaurant = get_restaurant(restaurant_id)
    if not restaurant:
        return jsonify(ok=False, error="Restaurant not found")
    try:
        from scheduler import send_urgent_alert
        owner_email = restaurant.owner_email
        send_urgent_alert(
            restaurant.name,
            owner_email,
            [{"author": "Test Customer", "platform": "google", "rating": 1,
              "text": "This is a test urgent review alert from Cavnar AI admin. Your urgent alert email is working correctly."}]
        )
        return jsonify(ok=True, email=owner_email)
    except Exception as e:
        return jsonify(ok=False, error=_safe_err(e))

@admin_bp.route("/admin/refresh-ig-token/<int:restaurant_id>", methods=["POST"])
@admin_required
def refresh_ig_token(restaurant_id, current_user):
    """Manually refresh Instagram + Facebook tokens for a restaurant."""
    restaurant = get_restaurant(restaurant_id)
    if not restaurant or not restaurant.ig_token:
        return jsonify(ok=False, error="No Instagram token found")
    try:
        import requests as _req
        from datetime import datetime, timedelta
        from models import update_restaurant
        from meta_api import graph_url
        app_secret = os.getenv("META_APP_SECRET","")

        # Refresh IG long-lived token
        r = _req.get(graph_url("oauth/access_token"), params={
            "grant_type":        "fb_exchange_token",
            "client_id":         os.getenv("META_APP_ID",""),
            "client_secret":     app_secret,
            "fb_exchange_token": restaurant.ig_token,
        }, timeout=(5, 20))
        if r.status_code != 200:
            return jsonify(ok=False, error=f"IG refresh failed: {r.text[:200]}")

        new_token   = r.json().get("access_token", restaurant.ig_token)
        new_expires = (datetime.now() + timedelta(days=60)).strftime("%Y-%m-%d")

        update_data = {"ig_token": new_token, "ig_token_expires": new_expires}

        # Refresh FB page token too if we have one
        if restaurant.fb_page_token:
            r2 = _req.get(graph_url("oauth/access_token"), params={
                "grant_type":        "fb_exchange_token",
                "client_id":         os.getenv("META_APP_ID",""),
                "client_secret":     app_secret,
                "fb_exchange_token": restaurant.fb_page_token,
            }, timeout=(5, 20))
            if r2.status_code == 200:
                update_data["fb_page_token"]    = r2.json().get("access_token", restaurant.fb_page_token)
                update_data["fb_token_expires"] = new_expires

        update_restaurant(restaurant_id, update_data)
        print(f"IG/FB tokens refreshed for restaurant {restaurant_id}, expires {new_expires}")
        return jsonify(ok=True, expires=new_expires)
    except Exception as e:
        return jsonify(ok=False, error=_safe_err(e))



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

_BRAND_FIELDS = ("brand_name", "brand_color", "brand_logo_url")


def brand_color_value(raw):
    """A brand colour as stored: "#rrggbb" in lower case, or None to clear.
    Raises ValueError for anything else. The colour is written into a
    <style> block on the owner's dashboard (dashboard.html) and read by the
    iOS app, and the dashboard appends an alpha ("…cc"), so only a six-digit
    hex is valid there; a value like "red;}body{…" went into the page as CSS
    (fix round #142). "#abc" is expanded; a missing "#" is added."""
    import re as _re
    text = str(raw or "").strip().lower()
    if not text:
        return None
    if not text.startswith("#"):
        text = "#" + text
    if _re.match(r"^#[0-9a-f]{3}$", text):
        text = "#" + "".join(c * 2 for c in text[1:])
    if not _re.match(r"^#[0-9a-f]{6}$", text):
        raise ValueError("The brand color must be a hex color like #c84b2f.")
    return text


def _brand_logo_value(raw):
    text = str(raw or "").strip()
    if not text:
        return None
    if not text.lower().startswith("https://") or any(c.isspace() or c in "\"'<>" for c in text) or len(text) > 1000:
        raise ValueError("The logo URL must be an https:// address of an image.")
    return text


def _brand_payload(restaurant_id):
    """What the Branding & peer-profile form shows for this client — every
    field it can set, as stored, so the console fills the form on open
    instead of carrying over the last client's values (fix round #23)."""
    from models import restaurant_version
    r = get_restaurant(restaurant_id)
    if not r:
        return None
    return {"restaurant_id": restaurant_id, "version": restaurant_version(restaurant_id),
            "brand_name": r.brand_name, "brand_color": r.brand_color, "brand_logo_url": r.brand_logo_url,
            "category": getattr(r, "category", None),
            "exclude_from_learning": int(getattr(r, "exclude_from_learning", 0) or 0),
            "profile": {k: getattr(r, k, None) for k in ("service_model", "concept", "bar_led", "ownership",
                                                        "opened_year", "profile_source", "profile_confirmed_at")}}


@admin_bp.route("/admin/api/brand/<int:restaurant_id>", methods=["GET"])
@admin_required
def admin_get_brand(restaurant_id, current_user):
    payload = _brand_payload(restaurant_id)
    if payload is None:
        return jsonify(ok=False, error="Restaurant not found"), 404
    return jsonify(ok=True, **payload)


@admin_bp.route("/admin/api/brand/<int:restaurant_id>", methods=["POST"])
@admin_required
def admin_set_brand(restaurant_id, current_user):
    """Write the branding and peer-profile fields this call sends.

    The console's form was never filled or reset, so a save wrote the
    previous client's values, or blanks, over this client's name, colour and
    logo (fix round #23). Now: a blank brand field is left alone unless it is
    named in `clear` (["brand_logo_url", ...]); the colour and logo are
    validated; `expected_version` (from the GET) refuses a stale form with a
    409; and the response carries the stored values."""
    from models import StaleWrite, expected_version_from
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        return jsonify(ok=False, error="Send the branding as a JSON object."), 400
    if not get_restaurant(restaurant_id):
        return jsonify(ok=False, error="Restaurant not found"), 404
    clear = data.get("clear") if isinstance(data.get("clear"), list) else []
    updates = {}
    try:
        for key in _BRAND_FIELDS:
            if key in clear:
                updates[key] = None
                continue
            raw = data.get(key)
            if raw is None or not str(raw).strip():
                continue
            if key == "brand_color":
                updates[key] = brand_color_value(raw)
            elif key == "brand_logo_url":
                updates[key] = _brand_logo_value(raw)
            else:
                updates[key] = sanitize(str(raw), max_len=80)
    except ValueError as e:
        return jsonify(ok=False, error=str(e)), 400
    if "category" in data:
        from intelligence.categories import valid as _valid_category
        cat = (str(data.get("category") or "")).strip().lower()
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
    if "exclude_from_learning" in data and data.get("exclude_from_learning") not in (None, ""):
        updates["exclude_from_learning"] = 1 if data.get("exclude_from_learning") in (1, True, "1", "true", "on") else 0
    if not updates:
        return jsonify(ok=False, error="Nothing to save: every field was blank, and a blank field leaves "
                                       "what is stored alone."), 400
    try:
        update_restaurant(restaurant_id, updates, expected_version=expected_version_from(data))
    except StaleWrite as e:
        return jsonify(ok=False, conflict=True, error=e.user_message, current_version=e.current_version,
                       current=_brand_payload(restaurant_id)), 409
    if "profile_source" in updates:
        import thresholds as _thr
        from models import get_restaurant as _gr
        seed = _thr.seeded_targets(_gr(restaurant_id))
        if seed:
            update_restaurant(restaurant_id, seed)
    import admin_events
    admin_events.record("admin", "brand.update", restaurant_id=restaurant_id,
                        summary=(f"{current_user.get('username') or 'admin'} set " + ", ".join(sorted(updates)))[:300],
                        payload={"actor": current_user.get("username"), "fields": sorted(updates)})
    return jsonify(ok=True, **_brand_payload(restaurant_id))



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
    restaurant's reviews and labor history. Nothing else in the console can
    change it, so a real client stays real."""
    from models import get_restaurant, update_restaurant
    data = request.get_json(silent=True) or {}
    on = 1 if data.get("is_demo") else 0
    current = get_restaurant(restaurant_id)
    if not current:
        return jsonify(ok=False, error="Not found"), 404
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
    try:
        import admin_events
        admin_events.record("admin", "demo_flag.set", restaurant_id=restaurant_id, amount=on,
                            summary=f"Marked as {'demo' if on else 'real client'} by {current_user.get('username')}")
    except Exception:
        pass
    return jsonify(ok=True, restaurant_id=restaurant_id, is_demo=on)


@admin_bp.route("/admin/api/client/<int:restaurant_id>/delete-demo", methods=["POST"])
@admin_required
def admin_api_delete_demo(restaurant_id, current_user):
    """Permanently delete a DEMO account and every row that belongs to it
    (models.delete_restaurant). Two gates, both server-side: the restaurant
    must be flagged is_demo — a real client has to be marked as a demo first,
    which is its own confirmed step — and the request must carry the
    restaurant's exact name. Added to retire the Gia Mia demo (9/25/26)."""
    from models import get_restaurant, delete_restaurant
    data = request.get_json(silent=True) or {}
    current = get_restaurant(restaurant_id)
    if not current:
        return jsonify(ok=False, error="Not found"), 404
    if int(getattr(current, "is_demo", 0) or 0) != 1:
        return jsonify(ok=False, error="Only a restaurant flagged as a demo can be deleted here."), 400
    if (data.get("confirm_name") or "").strip() != (current.name or "").strip():
        return jsonify(ok=False, error="The name did not match. Nothing was deleted."), 400
    try:
        deleted = delete_restaurant(restaurant_id)
    except Exception as e:
        return jsonify(ok=False, error=_safe_err(e)), 500
    try:
        import admin_events
        admin_events.record("admin", "demo.deleted", restaurant_id=restaurant_id,
                            amount=sum(deleted.values()),
                            summary=f"Demo account {current.name} (#{restaurant_id}) deleted by "
                                    f"{current_user.get('username')}")
    except Exception:
        pass
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


# ── Fix round B1 ─────────────────────────────────────────────────────────────
# Legacy pages and data integrity: the bounded admin job pool, the admin
# twins of the owner's session-scoped routes (response templates, review
# imports) that name the client they act on, alert contacts, review
# fetching, and the polls for menu extraction and a new client's setup.

import threading as _b1_threading

# Admin actions that wait on a model or a provider run here instead of on a
# request thread (fix round #153): the platform has four request threads,
# and a menu extraction or a DocuSign send held one for as long as the
# provider took. Bounded in workers AND in queue — an unbounded queue is the
# old thread-per-click problem one step removed.
ADMIN_JOB_WORKERS = max(1, min(int(os.getenv("ADMIN_JOB_WORKERS", "2") or 2), 4))
ADMIN_JOB_QUEUE_MAX = 8
_admin_job_pool = None
_admin_job_lock = _b1_threading.Lock()
_admin_jobs_waiting = 0


def _admin_job_executor():
    global _admin_job_pool
    if _admin_job_pool is None:
        with _admin_job_lock:
            if _admin_job_pool is None:
                from concurrent.futures import ThreadPoolExecutor
                _admin_job_pool = ThreadPoolExecutor(max_workers=ADMIN_JOB_WORKERS, thread_name_prefix="admin-job")
    return _admin_job_pool


def _submit_admin_job(job_id, fn, *args):
    """Run fn(*args) on the admin job pool. Returns None, or the sentence
    saying why it was not started. fn records its own result in
    ops.async_jobs; an exception it raises is recorded as the job's error."""
    global _admin_jobs_waiting
    with _admin_job_lock:
        if _admin_jobs_waiting >= ADMIN_JOB_QUEUE_MAX:
            return "The server is busy with other admin jobs. Try again in a minute."
        _admin_jobs_waiting += 1

    def _run():
        global _admin_jobs_waiting
        try:
            fn(*args)
        except Exception as e:
            _ops.capture(e, job="admin_job", context=f"job_id={job_id}")
            _ops.finish_async_job(job_id, "error", {"ok": False, "error": _safe_err(e)})
        finally:
            with _admin_job_lock:
                _admin_jobs_waiting -= 1
            try:
                from models import close_thread_connections
                close_thread_connections()
            except Exception:
                pass
    try:
        _admin_job_executor().submit(_run)
    except RuntimeError:            # the pool is shutting down: a deploy
        with _admin_job_lock:
            _admin_jobs_waiting -= 1
        return "The server is restarting. Try again in a minute."
    return None


def _start_menu_extraction(restaurant_id, source, fn, *args):
    """Start one menu extraction for this restaurant as a job; a second
    while one is running is refused rather than joined (its result would
    be the other source's)."""
    import uuid
    job_id, joined = _ops.claim_async_job(str(uuid.uuid4()), "menu_extract", restaurant_id)
    if joined:
        return jsonify(ok=False, job_id=job_id, error="A menu extraction is already running for this "
                                                      "restaurant. Wait for it to finish."), 409

    def _job():
        result = fn(restaurant_id, *args)
        result["source"] = source
        _ops.finish_async_job(job_id, "done" if result.get("ok") else "error", result)
    err = _submit_admin_job(job_id, _job)
    if err:
        _ops.finish_async_job(job_id, "error", {"ok": False, "error": err})
        return jsonify(ok=False, error=err), 503
    return jsonify(ok=True, job_id=job_id, status="pending")


def _admin_job_status(job_id):
    job = _ops.read_async_job(job_id)
    if not job:
        return jsonify(ok=False, status="error", error="Job not found"), 404
    if job["status"] == "pending":
        return jsonify(ok=True, status="pending")
    result = dict(job["result"] or {})
    result["status"] = job["status"]
    return jsonify(result)


@admin_bp.route("/admin/api/menu-extract/<job_id>")
@admin_required
def admin_menu_extract_status(job_id, current_user):
    """{status: pending} until done; then {ok, menu_notes, source, ...} or
    {ok: false, error}. The text is for the admin to review; nothing is
    saved (fix round #142)."""
    return _admin_job_status(job_id)


@admin_bp.route("/admin/api/create-client/<job_id>")
@admin_required
def admin_client_setup_status(job_id, current_user):
    """A new client's provider calls: {status: pending}, then
    {ok, restaurant_id, envelope_id, docusign_skipped, docusign_error,
    menu_notes_fetched} (fix round #153)."""
    return _admin_job_status(job_id)


# Response templates for THE client named in the URL. The settings page
# called the owner's session-scoped /api/templates, which listed, created and
# deleted the admin's own home restaurant's templates (fix round #73).
@admin_bp.route("/admin/api/templates/<int:restaurant_id>", methods=["GET"])
@admin_required
def admin_list_templates(restaurant_id, current_user):
    from models import get_response_templates
    if not get_restaurant(restaurant_id):
        return jsonify(ok=False, error="Restaurant not found"), 404
    return jsonify(ok=True, templates=get_response_templates(restaurant_id))


@admin_bp.route("/admin/api/templates/<int:restaurant_id>", methods=["POST"])
@admin_required
def admin_create_template(restaurant_id, current_user):
    """The owner route's rules (mobile_api.mobile_create_template)."""
    from models import create_response_template
    if not get_restaurant(restaurant_id):
        return jsonify(ok=False, error="Restaurant not found"), 404
    data = request.get_json(silent=True) or {}
    title = str(data.get("title") or "").strip()
    body = str(data.get("body") or "").strip()
    if not title or not body:
        return jsonify(ok=False, error="Title and body required"), 400
    if len(title) > 120:
        return jsonify(ok=False, error="Title too long (120 chars max)"), 400
    if len(body) > 5000:
        return jsonify(ok=False, error="Template text too long (5,000 chars max)"), 400
    category = data.get("category", "general")
    if category not in ("general", "positive", "negative", "neutral"):
        category = "general"
    tid = create_response_template(restaurant_id, title, body, category)
    return jsonify(ok=True, id=tid)


@admin_bp.route("/admin/api/templates/<int:restaurant_id>/<int:template_id>/delete", methods=["POST"])
@admin_required
def admin_delete_template(restaurant_id, template_id, current_user):
    from models import delete_response_template
    delete_response_template(template_id, restaurant_id)
    return jsonify(ok=True)


@admin_bp.route("/admin/import-reviews/<int:restaurant_id>", methods=["POST"])
@admin_required
def admin_import_reviews(restaurant_id, current_user):
    """A TripAdvisor, DoorDash or Uber Eats review export into THIS client
    (multipart: file, platform). The console posted to the owner's
    /api/import-tripadvisor, which filed every platform's rows under
    TripAdvisor's column names and, with no restaurant id, into the admin's
    own home restaurant (fix round #149); that route now sends an admin
    session here. Same body as the owner's (client_api._do_import_reviews)."""
    import client_api
    if not get_restaurant(restaurant_id):
        return jsonify(ok=False, error="Restaurant not found"), 404
    payload, status = client_api._do_import_reviews(restaurant_id, request.files.get("file"),
                                                    request.form.get("platform"))
    if payload.get("ok"):
        import admin_events
        actor = current_user.get("username") or "admin"
        admin_events.record("admin", "reviews.import", restaurant_id=restaurant_id,
                            summary=(f"{actor} imported {payload.get('imported', 0)} "
                                     f"{payload.get('platform', '')} reviews ({payload.get('new', 0)} new)")[:300],
                            payload={"actor": actor, **{k: payload.get(k) for k in
                                                        ("platform", "imported", "new", "already_had", "skipped")}})
    return jsonify(**payload), status


# Alert contacts, one add or remove at a time (fix round #73: the routes
# existed with no screen). The older /admin/alert-contacts routes add without
# the per-location limit and delete by id alone; nothing calls them —
# candidate for future cleanup after additional verification.
@admin_bp.route("/admin/api/alert-contacts/<int:restaurant_id>", methods=["POST"])
@admin_required
def admin_add_alert_contact(restaurant_id, current_user):
    """Add one contact. Never with SMS consent: an operator typing someone
    else's number isn't that person consenting (notify.add_alert_contact)."""
    from notify import add_alert_contact, get_alert_contacts, MAX_ALERT_CONTACTS
    from client_api import _normalize_phone_lenient
    if not get_restaurant(restaurant_id):
        return jsonify(ok=False, error="Restaurant not found"), 404
    data = request.get_json(silent=True) or {}
    name = " ".join(str(data.get("name") or "").split())[:80]
    phone = _normalize_phone_lenient(str(data.get("phone") or ""))
    if not phone:
        return jsonify(ok=False, error="Enter a mobile number, with its area code."), 400
    existing = get_alert_contacts(restaurant_id)
    if any(c["phone"] == phone for c in existing):
        return jsonify(ok=False, error="That number is already an alert contact here."), 400
    if len(existing) >= MAX_ALERT_CONTACTS:
        return jsonify(ok=False, error=f"Alert contacts are limited to {MAX_ALERT_CONTACTS} per location. "
                                       f"Remove one first."), 400
    contact_id = add_alert_contact(restaurant_id, name, phone, sms_consent=False)
    import admin_events
    admin_events.record("admin", "alert_contact.added", restaurant_id=restaurant_id,
                        summary=f"{current_user.get('username') or 'admin'} added {name or 'a contact'} …{phone[-4:]}",
                        payload={"actor": current_user.get("username"), "contact_id": contact_id,
                                 "name": name, "phone_last4": phone[-4:]})
    return jsonify(ok=True, id=contact_id, name=name, phone=phone)


@admin_bp.route("/admin/api/alert-contacts/<int:restaurant_id>/<int:contact_id>/delete", methods=["POST"])
@admin_required
def admin_delete_alert_contact(restaurant_id, contact_id, current_user):
    from notify import get_alert_contacts, delete_alert_contact
    mine = [c for c in get_alert_contacts(restaurant_id) if int(c["id"]) == int(contact_id)]
    if not mine:
        return jsonify(ok=False, error="That contact isn't this restaurant's."), 404
    delete_alert_contact(contact_id)
    import admin_events
    admin_events.record("admin", "alert_contact.removed", restaurant_id=restaurant_id,
                        summary=f"{current_user.get('username') or 'admin'} removed {mine[0]['name'] or 'a contact'}",
                        payload={"actor": current_user.get("username"), "contact_id": contact_id,
                                 "name": mine[0]["name"], "phone_last4": (mine[0]["phone"] or "")[-4:]})
    return jsonify(ok=True)


@admin_bp.route("/admin/api/client/<int:restaurant_id>/reviews-fetching", methods=["POST"])
@admin_required
def admin_set_reviews_fetching(restaurant_id, current_user):
    """Turn review fetching on or off: {on: true|false}. The switch the
    console's "Reviews is on but has never received data" issue needs —
    fetching was settable only at creation (fix round #110). Refused without
    a Place ID or a Google Business Profile, and for a demo sharing a live
    client's listing. {ok, reviews_live, via: "google_business_profile" |
    "places" | null}."""
    from models import update_restaurant as _upd_rl
    data = request.get_json(silent=True) or {}
    if "on" not in data:
        return jsonify(ok=False, error="Say on or off."), 400
    try:
        on = _settings_flag(data, "on")
    except _SettingsError as e:
        return jsonify(ok=False, error=str(e)), 400
    current = get_restaurant(restaurant_id)
    if not current:
        return jsonify(ok=False, error="Restaurant not found"), 404
    value, err = reviews_live_decision(restaurant_id, current, {}, explicit=on)
    if err:
        return jsonify(ok=False, error=err), 400
    if value != int(current.reviews_live or 0):
        _upd_rl(restaurant_id, {"reviews_live": value})
        _record_settings_change(restaurant_id, current_user, {"reviews_live": current.reviews_live},
                                {"reviews_live": value})
    via = "google_business_profile" if current.gmb_refresh_token else ("places" if value else None)
    return jsonify(ok=True, reviews_live=value, via=via)
