"""staff_routes.py — the employee portal.

A deliberately separate surface from the owner dashboard. Nothing here reads
a restaurant_id from the request: the portal token names the restaurant at
sign-in, and from then on every route derives BOTH the restaurant and the
employee's own name from the session's membership. That is what makes "an
employee cannot see another restaurant, or another employee" a property of
the routing rather than a rule each handler has to remember.

What an employee gets today is scheduling: today's shift, the week, shift
detail, their profile, and their own task checklist. The identity layer under
it (auth.memberships) is built so shift acknowledgment, swaps, availability,
time-off and messaging can be added without touching authentication again.
"""
from flask import (Blueprint, jsonify, make_response, redirect, render_template,
                   request, url_for)

from auth import (STAFF_SESSION_HOURS, consume_portal_nonce, cookies_require_secure,
                  create_staff_session, delete_session, get_membership,
                  get_memberships_for_restaurant, issue_portal_nonce,
                  pin_lockout_state, portal_attempts_exceeded,
                  record_portal_attempt, restaurant_for_portal_token,
                  set_membership_pin, staff_login_required, validate_pin,
                  verify_membership_pin, PinError)
from models import get_restaurant

staff_bp = Blueprint("staff", __name__, url_prefix="/staff")

# Per-IP throttle on the portal's public surface, on top of the per-membership
# lockout. The membership lockout stops someone grinding one person's PIN;
# this stops someone spraying one guess across the whole roster, which no
# per-membership counter would ever notice.
#
# It lives in the database (auth.portal_attempts) rather than a module dict:
# a dict resets on deploy, is not shared across gunicorn workers — so the real
# ceiling was 30 × worker count — and never evicted a key.


def _client_ip():
    # ProxyFix (hosted_dashboard.py) already rewrites remote_addr from the one
    # trusted proxy hop, so this no longer parses the raw header itself.
    return request.remote_addr or "?"


def _throttled(ip):
    """(response, status) to return when this IP is over budget, else None.
    Every public portal route calls this — the roster reads used to be
    unthrottled entirely, which left token guessing and roster enumeration
    with no ceiling at all."""
    if portal_attempts_exceeded(ip):
        return jsonify(ok=False,
                       error="Too many attempts from this device. Wait a few minutes."), 429
    return None


def _staff_context(current_user):
    """(restaurant_id, employee_name) for the signed-in employee.

    Both come from the membership the session resolved — never from the URL,
    a query string or a body. Every route below goes through this.
    """
    rid = current_user["restaurant_id"]
    name = current_user.get("employee_name") or current_user.get("username") or ""
    return rid, name


# ── Sign in ────────────────────────────────────────────────────────────────

@staff_bp.route("/")
def portal_entry():
    """Landing when we don't know which restaurant this is — the employee
    needs their restaurant's link."""
    return render_template("staff_login.html", restaurant=None, roster=[],
                           portal_token="", error=None)


@staff_bp.route("/r/<token>")
def portal_login(token):
    """The restaurant's staff link: pick your name, enter your PIN."""
    ip = _client_ip()
    throttled = _throttled(ip)
    if throttled:
        return render_template("staff_login.html", restaurant=None, roster=[],
                               portal_token="", login_nonce="",
                               error="Too many attempts from this device. Wait a few minutes."), 429
    record_portal_attempt(ip)
    rid = restaurant_for_portal_token(token)
    if not rid:
        return render_template("staff_login.html", restaurant=None, roster=[],
                               portal_token="", login_nonce="",
                               error="That staff link isn't valid any more. Ask a manager for the current one."), 404
    restaurant = get_restaurant(rid)
    roster = [
        {"membership_id": m["id"],
         "name": m.get("employee_name") or m["username"],
         "role": m["role"]}
        for m in get_memberships_for_restaurant(rid, role="employee")
        if m.get("pin_hash")
    ]
    return render_template("staff_login.html", restaurant=restaurant, roster=roster,
                           portal_token=token, login_nonce=issue_portal_nonce(rid),
                           error=None)


@staff_bp.route("/api/roster/<token>")
def api_roster(token):
    """The same roster the HTML sign-in screen renders, as JSON, for the iOS
    staff view. Deliberately the identical shape and the identical filter
    (PIN set, employee-tier, this restaurant only) so the two clients cannot
    drift into showing different people."""
    ip = _client_ip()
    throttled = _throttled(ip)
    if throttled:
        return throttled
    record_portal_attempt(ip)
    rid = restaurant_for_portal_token(token)
    if not rid:
        return jsonify(ok=False, error="That staff link isn't valid any more."), 404
    restaurant = get_restaurant(rid)
    roster = [
        {"membership_id": m["id"], "name": m.get("employee_name") or m["username"]}
        for m in get_memberships_for_restaurant(rid, role="employee")
        if m.get("pin_hash")
    ]
    return jsonify(ok=True, restaurant=(restaurant.name if restaurant else ""),
                   roster=roster, login_nonce=issue_portal_nonce(rid))


@staff_bp.route("/r/<token>/login", methods=["POST"])
def portal_authenticate(token):
    """Verify a PIN and start a shift session.

    restaurant_id comes from the token. membership_id comes from the client,
    but is only ever used together with that restaurant_id, so naming a real
    membership from another tenant resolves to nothing.
    """
    ip = _client_ip()
    throttled = _throttled(ip)
    if throttled:
        return throttled
    rid = restaurant_for_portal_token(token)
    if not rid:
        return jsonify(ok=False, error="That staff link isn't valid any more."), 404

    data = request.get_json(silent=True) or {}
    try:
        membership_id = int(data.get("membership_id"))
    except (TypeError, ValueError):
        return jsonify(ok=False, error="Pick your name first."), 400

    record_portal_attempt(ip)
    # Spend the one-shot nonce BEFORE the PIN is checked, so a captured
    # request body cannot be replayed even if the PIN it carries is correct.
    # A stale one is a distinct, non-sensitive failure: the client refetches
    # the roster and the employee taps again, rather than being told their
    # PIN was wrong when it wasn't.
    if not consume_portal_nonce((data.get("nonce") or "").strip(), rid):
        return jsonify(ok=False, error="That sign-in expired — tap your name again.",
                       nonce_expired=True), 409

    result = verify_membership_pin(membership_id, rid, data.get("pin") or "", ip_address=ip)
    if not result["ok"]:
        return jsonify(ok=False, error=result["error"],
                       locked=result.get("locked", False),
                       login_nonce=issue_portal_nonce(rid)), 401

    from auth import get_conn as _gc
    conn = _gc()
    try:
        row = conn.execute("SELECT user_id FROM memberships WHERE id=? AND restaurant_id=?",
                           (membership_id, rid)).fetchone()
    finally:
        conn.close()
    if not row:
        return jsonify(ok=False, error="That PIN didn't match."), 401

    session_token = create_staff_session(
        row["user_id"], rid, ip_address=ip,
        user_agent=request.headers.get("User-Agent", ""),
        device_id=(data.get("device_id") or "").strip() or None)

    # Owners could see every console sign-in and none of the portal ones,
    # even though the portal is the surface with the wider door. Off by
    # default — see restaurants.staff_signin_notify.
    try:
        restaurant = get_restaurant(rid)
        if restaurant and getattr(restaurant, "staff_signin_notify", 0):
            membership = get_membership(row["user_id"], rid) or {}
            from notify import send_staff_signin_alert
            send_staff_signin_alert(rid, restaurant.name or "", restaurant.owner_email or "",
                                    membership.get("employee_name") or "", ip)
    except Exception as exc:
        print(f"[StaffSignIn] alert failed: {exc}")

    resp = make_response(jsonify(ok=True, redirect=url_for("staff.portal_home"),
                                 token=session_token))
    # httponly so the portal's own JS can't read it either; a staff device is
    # the least trusted place a session lives in this product.
    # `secure` comes from the deployment, not from a request header. It used
    # to read X-Forwarded-Proto — a header the app neither validated nor
    # normalised — so a proxy that spelled it differently would have shipped
    # staff cookies over plaintext silently. cookies_require_secure() is the
    # same signal the owner cookie uses.
    resp.set_cookie("staff_session", session_token, httponly=True, samesite="Lax",
                    max_age=STAFF_SESSION_HOURS * 3600,
                    secure=cookies_require_secure())
    return resp


@staff_bp.route("/logout", methods=["POST", "GET"])
def portal_logout():
    token = request.cookies.get("staff_session")
    if token:
        try:
            delete_session(token)
        except Exception:
            pass
    resp = make_response(redirect(url_for("staff.portal_entry")))
    resp.delete_cookie("staff_session")
    return resp


# ── Portal ─────────────────────────────────────────────────────────────────

@staff_bp.route("/home")
@staff_login_required
def portal_home(current_user):
    rid, name = _staff_context(current_user)
    restaurant = get_restaurant(rid)
    return render_template("staff_portal.html", restaurant=restaurant,
                           employee_name=name)


@staff_bp.route("/api/me")
@staff_login_required
def api_me(current_user):
    rid, name = _staff_context(current_user)
    restaurant = get_restaurant(rid)
    membership = get_membership(current_user["id"], rid) or {}
    return jsonify(ok=True, employee={
        "name": name,
        "role": current_user.get("role"),
        "restaurant": restaurant.name if restaurant else "",
        "has_pin": bool(membership.get("pin_hash")),
        "pin_set_at": membership.get("pin_set_at"),
    })


@staff_bp.route("/api/shifts")
@staff_login_required
def api_shifts(current_user):
    """This employee's own shifts. The name is taken from the session, so
    there is no id to tamper with and no other employee to ask for."""
    rid, name = _staff_context(current_user)
    if not name:
        return jsonify(ok=True, today=None, upcoming=[], week=[])
    from staff_schedule import shifts_for_employee
    return jsonify(ok=True, **shifts_for_employee(rid, name))


TASK_DATE_WINDOW_DAYS = 1


def _valid_task_date(value):
    """An ISO date inside today ± TASK_DATE_WINDOW_DAYS, or None.

    A checklist is an accountability record, so the date it is filed under is
    part of the claim being made. Unvalidated, an employee could pre-complete
    a month of mornings or silently backfill days they missed — which defeats
    the only purpose the feature has. The ±1 day window is what a real shift
    needs: a closing task ticked after midnight, or an opening one ticked on a
    device whose clock is a day off.
    """
    from datetime import date as _date, timedelta as _td
    text = (value or "").strip()
    if not text:
        return None
    try:
        parsed = _date.fromisoformat(text[:10])
    except ValueError:
        return None
    today = _date.today()
    if abs((parsed - today).days) > TASK_DATE_WINDOW_DAYS:
        return None
    return parsed.isoformat()


@staff_bp.route("/api/tasks")
@staff_login_required
def api_tasks(current_user):
    """The checklist for this employee's own role."""
    rid, _name = _staff_context(current_user)
    membership = get_membership(current_user["id"], rid) or {}
    role = _employee_job_role(rid, membership)
    if not role:
        return jsonify(ok=True, role=None, tasks=[])
    from models import get_todays_tasks
    raw = (request.args.get("date") or "").strip()
    date = _valid_task_date(raw)
    if raw and not date:
        return jsonify(ok=False, error="That date isn't one you can check off."), 400
    return jsonify(ok=True, role=role, tasks=get_todays_tasks(rid, role, task_date=date))


@staff_bp.route("/api/tasks/complete", methods=["POST"])
@staff_login_required
def api_complete_task(current_user):
    """Check off a task. Only tasks belonging to this employee's own job role
    at this restaurant — a template_id from any other role or restaurant is
    refused rather than trusted."""
    rid, name = _staff_context(current_user)
    membership = get_membership(current_user["id"], rid) or {}
    role = _employee_job_role(rid, membership)
    data = request.get_json(silent=True) or {}
    try:
        template_id = int(data.get("template_id"))
    except (TypeError, ValueError):
        return jsonify(ok=False, error="template_id required"), 400
    raw_date = (data.get("task_date") or "").strip()
    if not raw_date:
        return jsonify(ok=False, error="task_date required"), 400
    task_date = _valid_task_date(raw_date)
    if not task_date:
        return jsonify(ok=False, error="That date isn't one you can check off."), 400

    from models import get_task_templates, set_task_completion
    mine = {t["id"] for t in get_task_templates(rid, role=role)} if role else set()
    if template_id not in mine:
        return jsonify(ok=False, error="That task isn't on your list."), 403
    ok = set_task_completion(rid, template_id, task_date, bool(data.get("done")),
                             completed_by=name)
    return (jsonify(ok=True), 200) if ok else (jsonify(ok=False, error="Not found"), 404)


@staff_bp.route("/api/pin", methods=["POST"])
@staff_login_required
def api_change_pin(current_user):
    """Change your own PIN. Requires the current one — a shared device left
    signed in must not let the next person lock out its owner."""
    rid, _name = _staff_context(current_user)
    membership = get_membership(current_user["id"], rid)
    if not membership:
        return jsonify(ok=False, error="This isn't a staff account."), 403
    data = request.get_json(silent=True) or {}
    current = verify_membership_pin(membership["id"], rid, data.get("current_pin") or "")
    if not current["ok"]:
        return jsonify(ok=False, error="Your current PIN didn't match."), 401
    try:
        new_pin = validate_pin(data.get("new_pin") or "")
    except PinError as pe:
        return jsonify(ok=False, error=str(pe)), 400
    set_membership_pin(membership["id"], rid, new_pin)
    # set_membership_pin ends this membership's staff sessions, including the
    # one that just made this call — the portal re-authenticates after.
    return jsonify(ok=True, signed_out=True)


def _employee_job_role(restaurant_id, membership):
    """The JOB role ("Bartender") for a staff membership, as distinct from
    its AUTHORIZATION role ("employee").

    These are deliberately different things. Authorization lives on the
    membership; the job title is what decides which checklist this employee
    sees and may complete.

    Resolution order, and why it is this order:

      1. memberships.job_role — what an owner set. Authoritative, and the only
         source that is real data by construction.
      2. manual_team_members — the Operational Score roster an owner typed.
      3. The most recent PUBLISHED schedule.

    It used to start at labor.load_shifts_for_restaurant, whose own docstring
    says it returns bundled SAMPLE shifts when a restaurant has no CSV. At a
    restaurant with no uploaded shift data, an employee whose name collided
    with a fixture was handed that fixture's job title — and that title gates
    which tasks they can check off. Authorization derived from invented data
    is the one thing staff_schedule.py exists to prevent, and this is where
    that discipline had slipped.

    Cached on flask.g: both task endpoints call it, and step 3 parses a whole
    schedule CSV, so an uncached version cost O(all shifts) per tap of a
    checkbox.
    """
    name = (membership or {}).get("employee_name")
    if not name:
        return None

    stored = (membership or {}).get("job_role")
    if stored:
        return stored

    from flask import g
    cache_key = f"_staff_job_role:{restaurant_id}:{name.strip().lower()}"
    cached = getattr(g, cache_key, None) if g else None
    if cached is not None:
        return cached or None

    resolved = _resolve_job_role_from_data(restaurant_id, name)
    try:
        setattr(g, cache_key, resolved or "")
    except Exception:
        pass
    return resolved


def _resolve_job_role_from_data(restaurant_id, name):
    """Job title from real restaurant data only — never a sample fallback."""
    target = name.strip().lower()
    try:
        from models import get_manual_team_members
        for m in get_manual_team_members(restaurant_id):
            if (m.get("name") or "").strip().lower() == target:
                role = (m.get("role") or "").strip()
                if role:
                    return role
    except Exception:
        pass
    try:
        # The same source staff_schedule.py reads: the most recently published
        # schedule, which either exists or doesn't. No fallback, by design.
        from labor import employee_shifts_from_csv
        from models import get_schedule_history, get_schedule_history_detail
        history = get_schedule_history(restaurant_id) or []
        if history:
            detail = get_schedule_history_detail(history[0]["id"], restaurant_id) or {}
            latest, role = "", None
            for sh in employee_shifts_from_csv(detail.get("schedule_csv") or "", name):
                d = sh.get("date") or ""
                if d >= latest:
                    latest, role = d, (sh.get("role") or "").strip() or role
            if role:
                return role
    except Exception:
        pass
    return None
