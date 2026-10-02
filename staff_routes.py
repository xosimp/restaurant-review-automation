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

from auth import STAFF_SESSION_HOURS, SignupError, claim_staff_name, claimable_names, consume_portal_nonce, cookies_require_secure, create_staff_session, delete_session, get_join_code, get_membership, get_memberships_for_restaurant, issue_portal_nonce, mark_portal_attempt_ok, phone_for_signup_token, portal_attempts_exceeded, record_portal_attempt, restaurant_for_join_code, restaurant_for_staff_code, set_membership_pin, staff_login_required, start_staff_signup, validate_pin, verify_membership_pin, verify_staff_signup, PinError
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


def _token_for_native_app(data, session_token):
    """{"token": ...} for the iOS app, {} for a browser. The browser's session
    is the HttpOnly staff_session cookie set on the same response, and HttpOnly
    exists so page JavaScript can never read it — handing the same token back
    in the JSON body undid that for any script on the page (SEC-36). The app
    has no cookie jar it uses for this and stores the token in the Keychain;
    it is recognised by the device identity it sends with every sign-in
    (Keychain.deviceIdentity()), which the web pages never send."""
    if (data.get("device_id") or "").strip():
        return {"token": session_token}
    return {}


def _notify_owner_of_signin(rid, user_id, ip):
    """Owners could see every console sign-in and none of the portal ones,
    even though the portal is the surface with the wider door. Off by
    default — see restaurants.staff_signin_notify."""
    try:
        restaurant = get_restaurant(rid)
        if restaurant and getattr(restaurant, "staff_signin_notify", 0):
            membership = get_membership(user_id, rid) or {}
            from notify import send_staff_signin_alert
            send_staff_signin_alert(rid, restaurant.name or "", restaurant.owner_email or "",
                                    membership.get("employee_name") or "", ip)
    except Exception as exc:
        print(f"[StaffSignIn] alert failed: {exc}")


def _staff_context(current_user):
    """(restaurant_id, employee_name) for the signed-in employee.

    Both come from the membership the session resolved — never from the URL,
    a query string or a body. Every route below goes through this.
    """
    rid = current_user["restaurant_id"]
    name = current_user.get("employee_name") or current_user.get("username") or ""
    return rid, name


# ── Sign in ────────────────────────────────────────────────────────────────

def _app_url():
    """Where an employee gets the app: the App Store link once it is listed
    (IOS_APP_STORE_URL), else nothing and the page says to ask a manager."""
    import os as _os_app
    return (_os_app.getenv("IOS_APP_STORE_URL") or "").strip()


def _app_page(restaurant=None, join_code="", error=None, status=200, **extra):
    """The web side of the staff portal: one page saying the portal is in
    the iPhone app (owner, 9/30/26: "no web version for employees"), with
    the restaurant's join code when the link names one. It keeps the
    create-your-account flow because it is the opt-in page registered for
    the staff verification texts (A2P)."""
    return render_template("staff_login.html", restaurant=restaurant, roster=[],
                           portal_token="", login_nonce="", join_code=join_code or "",
                           error=error, app_url=_app_url(),
                           ready=request.args.get("ready") == "1", **extra), status


@staff_bp.route("/")
def portal_entry():
    """Where an employee lands with no restaurant link: get the app."""
    return _app_page()


@staff_bp.route("/signup")
def portal_signup():
    """Create-your-account, open on arrival at the phone step: the public
    page Twilio's A2P 10DLC reviewers are sent to for the staff verification
    program, so the number field, the unchecked consent box and the Terms /
    Privacy links are on screen without a click (9/28/26)."""
    return _app_page(open_signup=True)


@staff_bp.route("/r/<token>")
def portal_login(token):
    """The restaurant's staff link or posted code: it no longer opens a PIN
    pad in the browser. It names the restaurant and shows its join code for
    the app's Create your account (the app's own PIN sign-in posts to
    /staff/r/<token>/login, below)."""
    ip = _client_ip()
    throttled = _throttled(ip)
    if throttled:
        return _app_page(error="Too many attempts from this device. Wait a few minutes.", status=429)
    attempt = record_portal_attempt(ip)
    rid = restaurant_for_staff_code(token)
    if not rid:
        return _app_page(error="That staff link isn't valid any more. Ask a manager for the current code.",
                         status=404)
    mark_portal_attempt_ok(attempt)      # a real code: not a guess (SEC-18)
    return _app_page(restaurant=get_restaurant(rid), join_code=get_join_code(rid))


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
    attempt = record_portal_attempt(ip)
    rid = restaurant_for_staff_code(token)
    if not rid:
        return jsonify(ok=False, error="That staff link isn't valid any more."), 404
    mark_portal_attempt_ok(attempt)      # a real code: not a guess (SEC-18)
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
    rid = restaurant_for_staff_code(token)
    if not rid:
        return jsonify(ok=False, error="That staff link isn't valid any more."), 404

    data = request.get_json(silent=True) or {}
    try:
        membership_id = int(data.get("membership_id"))
    except (TypeError, ValueError):
        return jsonify(ok=False, error="Pick your name first."), 400

    attempt = record_portal_attempt(ip)
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

    # A sign-in that worked spends nothing from the address's failure budget
    # — a whole kitchen signs in on one Wi-Fi address at shift change (SEC-18).
    mark_portal_attempt_ok(attempt)
    _notify_owner_of_signin(rid, row["user_id"], ip)

    resp = make_response(jsonify(ok=True, redirect=url_for("staff.portal_home"),
                                 **_token_for_native_app(data, session_token)))
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


# ── Self-signup ────────────────────────────────────────────────────────────
#
# Five steps, each one a separate request so a client can render them as
# separate screens: phone → code → restaurant → name → PIN.
#
# Nothing here needs the owner. The control is on which NAME may be claimed
# (once, from the real roster), not on who may sign up — an account with no
# membership can see nothing at all.


@staff_bp.route("/api/signup/start", methods=["POST"])
def signup_start():
    """Text a verification code to a phone."""
    ip = _client_ip()
    throttled = _throttled(ip)
    if throttled:
        return throttled
    record_portal_attempt(ip)
    data = request.get_json(silent=True) or {}
    try:
        result = start_staff_signup(data.get("phone") or "", optin=bool(data.get("optin")))
    except SignupError as se:
        return jsonify(ok=False, error=str(se)), 400
    return jsonify(**result)


@staff_bp.route("/api/signup/verify", methods=["POST"])
def signup_verify():
    """Check the texted code and hand back a short-lived signup token."""
    ip = _client_ip()
    throttled = _throttled(ip)
    if throttled:
        return throttled
    record_portal_attempt(ip)
    data = request.get_json(silent=True) or {}
    try:
        token = verify_staff_signup(data.get("phone") or "", data.get("code") or "")
    except SignupError as se:
        return jsonify(ok=False, error=str(se)), 400
    return jsonify(ok=True, signup_token=token)


@staff_bp.route("/api/signup/where/<code>")
def signup_where(code):
    """Which restaurant a join code names — so someone can confirm they typed
    it right before they pick a name off a stranger's roster."""
    ip = _client_ip()
    throttled = _throttled(ip)
    if throttled:
        return throttled
    record_portal_attempt(ip)
    rid = restaurant_for_join_code(code)
    if not rid:
        return jsonify(ok=False, error="We don't recognise that code. Check with your manager."), 404
    restaurant = get_restaurant(rid)
    return jsonify(ok=True, restaurant=(restaurant.name if restaurant else ""))


@staff_bp.route("/api/signup/claimable/<code>")
def signup_claimable(code):
    """The names still available at this restaurant.

    Requires a verified signup token: the roster is a list of real people's
    names, and there is no reason to hand it to someone who has not at least
    proved they hold a phone.
    """
    token = (request.args.get("signup_token") or "").strip()
    if not phone_for_signup_token(token):
        return jsonify(ok=False, error="Verify your phone first.", signup_expired=True), 401
    rid = restaurant_for_join_code(code)
    if not rid:
        return jsonify(ok=False, error="We don't recognise that code."), 404
    names = claimable_names(rid)
    restaurant = get_restaurant(rid)
    return jsonify(ok=True, restaurant=(restaurant.name if restaurant else ""),
                   names=names,
                   none_left=(len(names) == 0))


@staff_bp.route("/api/signup/claim", methods=["POST"])
def signup_claim():
    """Claim a name, set a PIN, and get signed in — the account exists from
    here on."""
    ip = _client_ip()
    throttled = _throttled(ip)
    if throttled:
        return throttled
    record_portal_attempt(ip)
    data = request.get_json(silent=True) or {}
    rid = restaurant_for_join_code(data.get("join_code") or "")
    if not rid:
        return jsonify(ok=False, error="We don't recognise that code."), 404
    try:
        claimed = claim_staff_name(
            (data.get("signup_token") or "").strip(), rid,
            data.get("employee_name") or "", data.get("pin") or "")
    except SignupError as se:
        return jsonify(ok=False, error=str(se)), 400

    session_token = create_staff_session(
        claimed["user_id"], rid, ip_address=ip,
        user_agent=request.headers.get("User-Agent", ""),
        device_id=(data.get("device_id") or "").strip() or None)
    resp = make_response(jsonify(ok=True, redirect=url_for("staff.portal_home"),
                                 **_token_for_native_app(data, session_token),
                                 employee_name=claimed["employee_name"],
                                 job_role=claimed["job_role"]))
    resp.set_cookie("staff_session", session_token, httponly=True, samesite="Lax",
                    max_age=STAFF_SESSION_HOURS * 3600,
                    secure=cookies_require_secure())
    _notify_owner_of_signin(rid, claimed["user_id"], ip)
    return resp


@staff_bp.route("/logout", methods=["POST", "GET"])
def portal_logout():
    """A GET only asks. It used to sign out, so any page could end a shift
    session on the pass tablet with an <img src=/staff/logout> — the same
    hole /logout had (SEC-34). The staff cookie is SameSite=Lax, so the POST
    is only ever authenticated from the portal's own form."""
    if request.method != "POST":
        if not request.cookies.get("staff_session"):
            return redirect(url_for("staff.portal_entry"))
        return ("<!doctype html><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>"
                "<title>Sign out</title><div style=\"font-family:-apple-system,BlinkMacSystemFont,'Helvetica Neue',Arial,"
                "sans-serif;max-width:420px;margin:15vh auto;padding:24px;text-align:center\"><h2>Sign out?</h2>"
                "<form method='post' action='/staff/logout'><button type='submit' class='cbtn cbtn-primary' "
                "style='padding:12px 22px;border:0;border-radius:10px;background:#D4583A;color:#fff;font-size:16px'>"
                "Sign out</button></form><p><a href='/staff/home'>Back to my shifts</a></p></div>")
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
    """A signed-in employee who opens the web: the portal is in the app."""
    rid, _name = _staff_context(current_user)
    try:
        code = get_join_code(rid)
    except Exception:
        code = ""
    return _app_page(restaurant=get_restaurant(rid), join_code=code)


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


@staff_bp.route("/api/preshift")
@staff_login_required
def api_preshift(current_user):
    """Today's lineup briefing — see preshift.py. Staff-safe by construction:
    no money and no individuals, so it is the same for every employee."""
    rid, _name = _staff_context(current_user)
    import preshift
    try:
        return jsonify(ok=True, **preshift.build(rid))
    except Exception as e:
        import ops
        ops.capture(e, job="preshift", context=f"restaurant_id={rid}")
        return jsonify(ok=True, items=[])


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


_DAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")


def availability_from_submission(unavailable, notes):
    """The one rule for an employee's own availability, whichever surface
    they save it from — the portal here, or the /s/<token> link in the
    weekly schedule email (client_api.staff_availability_submit). The two
    used to disagree: the link stored "all 7 days blocked" and kept the old
    available_days, so a newly blocked day was both (CLIENT-11).

    Returns (available_days, unavailable_days, notes, error). The available
    days are the complement of the blocked ones, so the row can never
    contradict itself; the note is trimmed to 300 characters, None if blank.
    """
    wanted = {str(x).strip().capitalize() for x in (unavailable or [])}
    blocked = [d for d in _DAYS if d in wanted]
    if len(blocked) == len(_DAYS):
        return None, None, None, "Every day blocked — leave at least one you can work."
    clean = (str(notes or "").strip())[:300] or None
    return [d for d in _DAYS if d not in blocked], blocked, clean, None


@staff_bp.route("/api/availability")
@staff_login_required
def api_availability(current_user):
    """This employee's own availability — the days they cannot work and a
    note. The automation audit's one workflow where the person with the
    information had no way to enter it: availability was typed by a
    manager on the console on staff's behalf. The name comes from the
    session, so there is nobody else's to read.

    Each weekday is `any`, `off` or `window` (earliest/latest as "17:00"),
    either with optional `from`/`until` dates — "not before 5pm on
    Tuesdays until 12/15/26" (employee audit M5). `updated_at` is the
    version a save must send back (staff_settings.save_own_availability)."""
    rid, name = _staff_context(current_user)
    import staff_settings
    return jsonify(ok=True, **staff_settings.own_availability(rid, name))


@staff_bp.route("/api/availability", methods=["POST"])
@staff_login_required
def api_availability_save(current_user):
    """Save this employee's availability: `week` (the new shape) or
    `unavailable_days` (older apps), with `notes` and — always — the
    `updated_at` the screen loaded. A save against a version that has moved
    on is a 409 with the record as it is now: an app whose load failed has
    no version to send, so it can never save an empty week over the real
    one (PERF-05), and the app and the /s/ link cannot overwrite each other.
    The answer names the published shifts the new availability rules out
    (`conflicts`, `conflicts_text`) — the managers hear about them too
    (LG-35) — and a `hint` when the note reads like dates away (WF-32)."""
    rid, name = _staff_context(current_user)
    if not name:
        return jsonify(ok=False, error="No employee name on this session."), 400
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        return jsonify(ok=False, error="Send your availability as a JSON object."), 400
    week, raw = body.get("week"), body.get("unavailable_days")
    if week is None and not isinstance(raw, list):
        return jsonify(ok=False, error="Send week, or unavailable_days as a list of weekday names."), 400
    import staff_settings
    res = staff_settings.save_own_availability(
        rid, name, body["updated_at"] if "updated_at" in body else staff_settings.MISSING,
        week=week, unavailable_days=raw,
        notes=body.get("notes") if "notes" in body else staff_settings.KEEP, source="app")
    status = res.pop("status", 200)
    if not res.get("ok"):
        return jsonify(**res), status
    av = res.pop("availability")
    res.pop("updated_at", None)        # the same as av["updated_at"]
    # The top level keeps the old answer's fields (unavailable_days, notes)
    # beside the new ones.
    return jsonify(**av, **res), 200


@staff_bp.route("/api/time-off")
@staff_login_required
def api_time_off(current_user):
    """This employee's own time-off requests and their answers. The name
    comes from the session; there is nobody else's to read."""
    rid, name = _staff_context(current_user)
    import time_off
    if not name:
        return jsonify(ok=True, requests=[])
    return jsonify(ok=True, requests=time_off.mine(rid, name))


@staff_bp.route("/api/time-off", methods=["POST"])
@staff_login_required
def api_time_off_request(current_user):
    rid, name = _staff_context(current_user)
    if not name:
        return jsonify(ok=False, error="No employee name on this session."), 400
    body = request.get_json(silent=True) or {}
    import time_off
    # "That date has passed" is judged on the restaurant's own calendar, not
    # the server's — a request typed at 11pm Pacific is not for yesterday.
    from time_utils import restaurant_now_by_id
    row, err = time_off.request_time_off(rid, name, body.get("start_date"), body.get("end_date"),
                                         reason=body.get("reason"),
                                         today=restaurant_now_by_id(rid, naive=True).date())
    if err:
        return jsonify(ok=False, error=err), 400
    from models import log_event
    try:
        log_event(rid, "time_off_requested", {"employee": name, "start": row["start_date"], "end": row["end_date"]})
    except Exception:
        pass
    return jsonify(ok=True, request=row)


@staff_bp.route("/api/time-off/<int:request_id>/withdraw", methods=["POST"])
@staff_login_required
def api_time_off_withdraw(request_id, current_user):
    """Take back a pending request — the only way to correct one, since a
    second request over the same dates is refused (MOD-EMP-7)."""
    rid, name = _staff_context(current_user)
    import time_off
    if not name or not time_off.withdraw(rid, request_id, name):
        return jsonify(ok=False, error="That request is not yours, or is already answered."), 404
    return jsonify(ok=True)


@staff_bp.route("/api/shift-requests")
@staff_login_required
def api_shift_requests(current_user):
    """This employee's own requests to drop a published shift, and the
    open shifts anybody on the roster can pick up."""
    rid, name = _staff_context(current_user)
    import shift_requests
    if not name:
        return jsonify(ok=True, requests=[], open=[], asks=[])
    return jsonify(ok=True, requests=shift_requests.mine(rid, name), open=shift_requests.open_shifts(rid),
                   asks=shift_requests.asked_of_me(rid, name))


@staff_bp.route("/api/shift-requests", methods=["POST"])
@staff_login_required
def api_shift_request_drop(current_user):
    rid, name = _staff_context(current_user)
    if not name:
        return jsonify(ok=False, error="No employee name on this session."), 400
    body = request.get_json(silent=True) or {}
    import shift_requests
    from time_utils import restaurant_now_by_id
    try:
        today = restaurant_now_by_id(rid, naive=True).date()
        if (body.get("kind") or "drop") == "swap":
            row = shift_requests.request_swap(rid, name, body.get("date"), body.get("shift_start"),
                                              body.get("target_name"), body.get("target_date"), body.get("target_start"),
                                              reason=body.get("reason"), today=today)
        else:
            row = shift_requests.request_drop(rid, name, body.get("date"), body.get("shift_start"),
                                              reason=body.get("reason"), today=today)
    except shift_requests.ShiftRequestError as e:
        return jsonify(ok=False, error=str(e)), 400
    from models import log_event
    try:
        log_event(rid, "shift_drop_requested", {"employee": name, "date": row["date"], "start": row["shift_start"]})
    except Exception:
        pass
    return jsonify(ok=True, request=row)


@staff_bp.route("/api/colleagues")
@staff_login_required
def api_colleagues(current_user):
    """Who else is on the published week, with their shifts — what a swap
    request needs to name. Names and shifts only; nothing else about them."""
    rid, name = _staff_context(current_user)
    import staff_schedule
    from schedule_versions import rows_from_csv
    from models import get_schedule_history_detail
    from time_utils import restaurant_now_by_id
    if not name:
        return jsonify(ok=True, colleagues=[])
    # Every published week still ahead, each date read from the week that
    # owns it — the same reading the portal's own shifts use. This called a
    # helper that no longer exists (_newest_published), so the swap picker
    # was a 500.
    today = restaurant_now_by_id(rid, naive=True).date()
    weeks = staff_schedule._published_weeks(rid, today)
    out = {}
    for w in weeks:
        detail = get_schedule_history_detail(w["id"], rid) or {}
        for r in rows_from_csv(detail.get("schedule_csv") or ""):
            d = staff_schedule._parse_day(r["date"])
            if not d or d < today or staff_schedule._owner_of(weeks, d) != w["id"]:
                continue
            if r["employee"].strip().lower() == name.strip().lower():
                continue
            out.setdefault(r["employee"], []).append({"date": r["date"], "day": r["day"], "role": r["role"],
                                                      "shift_start": r["shift_start"], "shift_end": r["shift_end"]})
    return jsonify(ok=True, colleagues=[{"name": n, "shifts": sorted(v, key=lambda x: (x["date"], x["shift_start"]))}
                                        for n, v in sorted(out.items())])


@staff_bp.route("/api/preferences")
@staff_login_required
def api_preferences(current_user):
    rid, name = _staff_context(current_user)
    import staff_settings
    st = staff_settings.for_name(rid, name) if name else {}
    return jsonify(ok=True, preferred_dayparts=st.get("preferred_dayparts") or [], desired_hours=st.get("desired_hours"),
                   schedule_texts=_schedule_texts(current_user["id"], rid), **_texts_consent(current_user["id"], rid))


def _texts_consent(user_id, rid) -> dict:
    """What the "text me" switch means and whether it can do anything
    (employee audit H14): `sms_available` is false while the staff messaging
    service is not configured (TWILIO_STAFF_MESSAGING_SERVICE_SID) — the app
    hides the switch; `schedule_texts_consent` is the sentence to show beside
    it, sent back as `consent_version`; `schedule_texts_scope` is what the
    consent on file covers (["schedule"] for one given on the old wording)."""
    import people
    import preferences
    on = _schedule_texts(user_id, rid)
    scope = []
    if on:
        scope = preferences.staff_sms_scopes([(user_id, rid)]).get((int(user_id), int(rid)),
                                                                    list(preferences.STAFF_SMS_LEGACY_SCOPE))
    return {"sms_available": people.staff_sms_ready(),
            "schedule_texts_consent": people.STAFF_SMS_CONSENT_TEXT,
            "schedule_texts_consent_version": preferences.STAFF_SMS_CONSENT_VERSION,
            "schedule_texts_scope": scope}


def _schedule_texts(user_id, rid, value=None, consent_version=None):
    """Read (value None) or set this employee's own "text me" consent
    (memberships.schedule_texts_at, and what it covers in
    preferences.staff_sms_scope). Only ever set from their own tick on an
    unchecked-by-default box — the signup consent covered the one-time code,
    nothing more (Friction audit #17). The wording they were shown decides
    the scope: `consent_version` 2 (people.STAFF_SMS_CONSENT_TEXT) covers
    schedule and request notices, an older app's box the schedule only."""
    from models import get_conn as _gc
    conn = _gc()
    try:
        if value is not None:
            conn.execute("UPDATE memberships SET schedule_texts_at=CASE WHEN ? THEN datetime('now') ELSE NULL END "
                         "WHERE user_id=? AND restaurant_id=?", (1 if value else 0, user_id, rid))
            conn.commit()
        row = conn.execute("SELECT schedule_texts_at FROM memberships WHERE user_id=? AND restaurant_id=?",
                           (user_id, rid)).fetchone()
    finally:
        conn.close()
    if value is not None:
        import preferences
        preferences.set_staff_sms_scope(user_id, rid, consent_version=consent_version, on=bool(value))
    return bool(row and row["schedule_texts_at"])


@staff_bp.route("/api/preferences", methods=["POST"])
@staff_login_required
def api_preferences_save(current_user):
    """The employee's own wishes — dayparts and hours a week — read by the
    draft as a soft signal, never over a rule or coverage."""
    rid, name = _staff_context(current_user)
    if not name:
        return jsonify(ok=False, error="No employee name on this session."), 400
    body = request.get_json(silent=True) or {}
    if "schedule_texts" in body and set(body) <= {"schedule_texts", "consent_version"}:
        # The texts switch alone: nothing about dayparts or hours changes.
        on = _schedule_texts(current_user["id"], rid, bool(body["schedule_texts"]),
                             consent_version=body.get("consent_version"))
        return jsonify(ok=True, schedule_texts=on, **_texts_consent(current_user["id"], rid))
    import staff_settings
    try:
        row = staff_settings.upsert(rid, name, preferred_dayparts=body.get("preferred_dayparts") if "preferred_dayparts" in body else None,
                                    desired_hours=body.get("desired_hours") if "desired_hours" in body else None,
                                    updated_by=name)
    except staff_settings.StaffSettingsError as e:
        return jsonify(ok=False, error=str(e)), 400
    return jsonify(ok=True, preferred_dayparts=row.get("preferred_dayparts") or [], desired_hours=row.get("desired_hours"))


@staff_bp.route("/api/shift-requests/<int:request_id>/withdraw", methods=["POST"])
@staff_login_required
def api_shift_request_withdraw(request_id, current_user):
    rid, name = _staff_context(current_user)
    import shift_requests
    if not shift_requests.withdraw(rid, request_id, name):
        return jsonify(ok=False, error="That request is not yours, or is already answered."), 404
    return jsonify(ok=True)


@staff_bp.route("/api/shift-requests/<int:request_id>/respond", methods=["POST"])
@staff_login_required
def api_shift_request_respond(request_id, current_user):
    """A colleague's yes or no to a swap they were asked for. A swap moves
    their shift too, so it never goes ahead without this (SCHED-21)."""
    rid, name = _staff_context(current_user)
    if not name:
        return jsonify(ok=False, error="No employee name on this session."), 400
    body = request.get_json(silent=True) or {}
    import shift_requests
    try:
        row = shift_requests.respond_swap(rid, request_id, name, bool(body.get("accept")))
    except shift_requests.ShiftRequestError as e:
        return jsonify(ok=False, error=str(e)), 400
    return jsonify(ok=True, request=row)


@staff_bp.route("/api/open-shifts/<int:request_id>/claim", methods=["POST"])
@staff_login_required
def api_open_shift_claim(request_id, current_user):
    """Take an open shift. The same legality check the schedule itself is
    held to decides whether this person can — availability, time off,
    hours, rest, a note on file."""
    rid, name = _staff_context(current_user)
    if not name:
        return jsonify(ok=False, error="No employee name on this session."), 400
    import shift_requests
    try:
        row = shift_requests.claim(rid, request_id, name, actor=name)
    except shift_requests.ShiftRequestError as e:
        return jsonify(ok=False, error=str(e)), 400
    from models import log_event
    try:
        log_event(rid, "open_shift_claimed", {"employee": name, "date": row["date"], "start": row["shift_start"]})
    except Exception:
        pass
    return jsonify(ok=True, request=row)


TASK_DATE_WINDOW_DAYS = 1


def _task_today(restaurant_id):
    """The restaurant's own calendar day, not the server's UTC one — at 8pm
    in Chicago the server is already on tomorrow (MOD-EMP-5)."""
    from time_utils import restaurant_now_by_id
    try:
        return restaurant_now_by_id(restaurant_id, naive=True).date()
    except Exception:
        from datetime import date as _date
        return _date.today()


def _valid_task_date(value, restaurant_id=None):
    """An ISO date inside today ± TASK_DATE_WINDOW_DAYS, or None.

    A checklist is an accountability record, so the date it is filed under is
    part of the claim being made. Unvalidated, an employee could pre-complete
    a month of mornings or silently backfill days they missed — which defeats
    the only purpose the feature has. The ±1 day window is what a real shift
    needs: a closing task ticked after midnight, or an opening one ticked on a
    device whose clock is a day off.
    """
    from datetime import date as _date
    text = (value or "").strip()
    if not text:
        return None
    try:
        parsed = _date.fromisoformat(text[:10])
    except ValueError:
        return None
    today = _task_today(restaurant_id) if restaurant_id is not None else _date.today()
    if abs((parsed - today).days) > TASK_DATE_WINDOW_DAYS:
        return None
    return parsed.isoformat()


def _job_roles(rid, membership, name):
    """Every job code this person works: their job role, then the roles the
    POS and the owner keep for them (people.person_roles — 43 of Simple EJ's
    staff are cross-trained). Decides which unassigned sheets are theirs."""
    roles = []
    primary = _employee_job_role(rid, membership)
    if primary:
        roles.append(primary)
    try:
        import staff_settings as _ss
        from models import get_conn
        conn = get_conn()
        try:
            roles += [r[0] for r in conn.execute(
                "SELECT role FROM person_roles WHERE restaurant_id=? AND employee_key=? AND removed_at IS NULL",
                (rid, _ss.name_key(name)))]
        finally:
            conn.close()
    except Exception:
        pass
    return list(dict.fromkeys(r for r in roles if r))


def _legacy_tasks(view):
    """The flat list an older app reads (role, tasks[{id, label, done}]):
    every line of this person's own sheets, id = the line's id."""
    out = []
    for a in view["sheets"]:
        for l in a["lines"]:
            out.append({"id": l["line_id"], "role": a["job_code"], "label": l["label"], "done": l["done"],
                        "completed_by": l["completed_by"], "completed_at": l["completed_at"]})
    return out


# An app that reads only `sheets` sends this header with 2 or more, and the
# flat `tasks` list (every line a second time, PERF-10) is left out. An app
# that sends nothing is an older one and still gets it — the list stays
# until those versions are gone (CLAUDE.md, deletion rule).
TASKS_API_HEADER = "X-Staff-Tasks-Version"


def _wants_legacy_tasks():
    try:
        return int(str(request.headers.get(TASKS_API_HEADER) or "0").strip()) < 2
    except ValueError:
        return True


@staff_bp.route("/api/tasks")
@staff_login_required
def api_tasks(current_user):
    """This employee's sheets for today (task_sheets.staff_view): the ones
    the published schedule puts them on, or an unassigned sheet on their job
    code, with due times and proof. A manager also gets the floor, may sign a
    shift off, and — opening — last night's sign-off note. `role` and `tasks`
    keep the old flat shape for apps that predate sheets (`tasks` only when
    the app does not send X-Staff-Tasks-Version: 2)."""
    rid, name = _staff_context(current_user)
    membership = get_membership(current_user["id"], rid) or {}
    roles = _job_roles(rid, membership, name)
    import task_sheets as ts
    try:
        view = ts.staff_view(rid, name, roles)
    except Exception as e:
        import ops
        ops.capture(e, job="staff_task_sheets", context=f"restaurant_id={rid}")
        return jsonify(ok=False, error="Could not load your sheets."), 500
    payload = dict(ok=True, role=(roles[0] if roles else None), **view)
    if _wants_legacy_tasks():
        payload["tasks"] = _legacy_tasks(view)
    return jsonify(payload)


def _tick(current_user, data, photo=None):
    """Tick one line and answer with the sheet as it now stands, so the app
    replaces that sheet instead of reading every sheet again (PERF-08)."""
    rid, name = _staff_context(current_user)
    # The day is the server's (the business day the sheet was issued for);
    # an older app still sends task_date, and one outside today ± 1 is
    # refused as before (F-07) rather than silently ignored.
    if "task_date" in data and not _valid_task_date(str(data.get("task_date") or ""), rid):
        return jsonify(ok=False, error="That date isn't one you can check off."), 400
    membership = get_membership(current_user["id"], rid) or {}
    roles = _job_roles(rid, membership, name)
    import task_sheets as ts
    try:
        line_id = int(data.get("line_id") or data.get("template_id"))
    except (TypeError, ValueError):
        return jsonify(ok=False, error="line_id required"), 400
    assignment_id = data.get("assignment_id")
    if not assignment_id:
        # An older app sends only the line (template_id): the sheet is this
        # person's sheet for today that carries that line.
        view = ts.staff_view(rid, name, roles)
        hit = next((a for a in view["sheets"] for l in a["lines"] if l["line_id"] == line_id), None)
        if not hit:
            return jsonify(ok=False, error="That task isn't on your list."), 403
        assignment_id = hit["id"]
    try:
        assignment_id = int(assignment_id)
    except (TypeError, ValueError):
        return jsonify(ok=False, error="assignment_id required"), 400
    done = data.get("done", True)
    done = done if isinstance(done, bool) else str(done).strip().lower() not in ("0", "false", "no", "none", "")
    try:
        out = ts.complete_line(rid, assignment_id, line_id, done=done,
                               employee_name=name, job_roles=roles, user_id=current_user.get("id"),
                               value=data.get("value"), photo=photo)
    except ts.NotYourSheet as e:
        return jsonify(ok=False, error=str(e)), 403
    except ts.TaskSheetError as e:
        return jsonify(ok=False, error=str(e)), 403 if "isn't yours" in str(e) else 400
    try:
        sheet = ts.assignment_view(rid, assignment_id)
    except Exception as e:
        # The tick is saved; the app falls back to reading /tasks.
        import ops
        ops.capture(e, job="staff_task_tick_view", context=f"restaurant_id={rid}")
        sheet = None
    return jsonify(ok=True, late=out["late"], flagged=out["flagged"], alert=out.get("alert"),
                   sheet=sheet, task_date=(sheet or {}).get("task_date")), 200


@staff_bp.route("/api/tasks/complete", methods=["POST"])
@staff_login_required
def api_complete_task(current_user):
    """Tick or un-tick one line of this employee's own sheet today:
    {assignment_id, line_id, done, value?}. A number line needs the reading,
    a note line the note; a photo line goes through /api/tasks/photo.
    Answers {ok, late, flagged, alert, sheet, task_date}: `sheet` is the
    ticked sheet as it now stands, `alert` is set when a critical reading is
    out of range ("Tell your manager now")."""
    return _tick(current_user, request.get_json(silent=True) or {})


@staff_bp.route("/api/tasks/photo", methods=["POST"])
@staff_login_required
def api_task_photo(current_user):
    """Tick a line with its photo: multipart/form-data (the image in `file`
    or `photo`, plus assignment_id and line_id as form fields), or JSON with
    image_b64 and mime (the iPhone app today). The photo is stored only
    after the sheet and line are known to be this person's, with the tick
    (task_sheets.complete_line); JPEG, PNG or WebP, at most 3.5 MB, re-encoded
    to 1280 px, and at most PHOTO_UPLOADS_PER_HOUR per login."""
    if request.files.get("file") or request.files.get("photo"):
        f = request.files.get("file") or request.files.get("photo")
        raw, mime, data = f.read(), f.mimetype or "", request.form.to_dict()
    else:
        import base64
        data = request.get_json(silent=True) or {}
        text = str(data.get("image_b64") or "")
        if text.startswith("data:") and "," in text:
            head, text = text.split(",", 1)
            data.setdefault("mime", head[5:].split(";")[0])
        try:
            raw = base64.b64decode(text, validate=False)
        except Exception:
            raw = b""
        mime = str(data.get("mime") or "image/jpeg")
    if not raw:
        return jsonify(ok=False, error="Add a photo to tick this off."), 400
    data = dict(data)
    data["done"] = True
    return _tick(current_user, data, photo=(raw, mime))


@staff_bp.route("/api/tasks/signoff", methods=["POST"])
@staff_login_required
def api_task_signoff(current_user):
    """The manager on duty signs a shift's sheets off, as they stand."""
    rid, name = _staff_context(current_user)
    membership = get_membership(current_user["id"], rid) or {}
    roles = _job_roles(rid, membership, name)
    import task_sheets as ts
    if not ts.staff_view(rid, name, roles)["manager"]:
        return jsonify(ok=False, error="Only the manager on duty signs a shift off."), 403
    data = request.get_json(silent=True) or {}
    try:
        return jsonify(ok=True, **ts.sign_off(rid, data.get("shift_kind") or "", name, user_id=current_user.get("id"),
                                              note=data.get("note"))), 200
    except ts.TaskSheetError as e:
        return jsonify(ok=False, error=str(e)), 400


@staff_bp.route("/api/tasks/photo/<token>")
@staff_login_required
def api_task_photo_file(token, current_user):
    """A proof photo on this restaurant's sheets, for a signed-in employee.
    Never stored by a browser, a WebView or a proxy (PERF-11)."""
    import io
    import task_sheets as ts
    from flask import send_file
    rid, _name = _staff_context(current_user)
    found = ts.get_photo(rid, token)
    if not found:
        return jsonify(ok=False, error="Not found"), 404
    resp = send_file(io.BytesIO(found[0]), mimetype=found[1] or "image/jpeg")
    resp.headers["Cache-Control"] = "private, no-store"
    return resp


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
    """Job title from real restaurant data only — never a sample fallback.

    Same source as the claimable-name list at signup (staff_roster), so the
    job someone is offered when they claim their name is the same job that
    later decides which checklist they see.
    """
    target = name.strip().lower()
    try:
        from staff_roster import roster_names_for_restaurant
        for roster_name, job in roster_names_for_restaurant(restaurant_id):
            if roster_name.strip().lower() == target and job:
                return job
    except Exception:
        pass
    return None


# The staff app's push registration and notification switches (employee audit
# C4) add their routes to staff_bp: imported here, at the end, so they exist
# before any app registers the blueprint (Flask refuses routes after that).
import staff_device_routes  # noqa: E402,F401

# Running late, the inbox (announcements) and the thread with the managers
# live in staff_comms_routes and attach to staff_bp when it is imported —
# here, so staff_bp never reaches an app without them (a blueprint takes
# no new routes once registered).
import staff_comms_routes  # noqa: E402,F401
