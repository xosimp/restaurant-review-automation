"""Every blueprint the app serves, and the hooks that go with them — one list
(audit #25 A04-06).

hosted_dashboard registered 22 blueprints by hand while the module-gate sweep
(tests/test_edge_sec_permissions.py) built its own app from 11 of them, so 21
API rules sat outside the test that exists to catch an unmapped route. Now
hosted_dashboard calls `register_all(app)` and every test that needs "all the
routes" builds its app from `all_blueprints()`, so a blueprint added here is
in both on the day it is written, and one added anywhere else is caught by
tests/test_cross_tenant_crawl.py (the real app's blueprints must equal this
list).

`plan()` is the registration sequence as data — CSRF wiring, blueprint
registration and the app-wide hooks, in hosted_dashboard's order — so a test
can check what is wired without registering anything. `register_all(app)`
runs it. Order matters for the app-level after_request hooks (Flask runs them
in reverse): support masking is registered after hosted_dashboard's security
headers and before `ensure_csrf_cookie`, exactly where it always was.

Calling `register_all` mutates the shared blueprint objects (csrf_protect
adds a before_request to each protected blueprint, guarded by `_csrf_wired`
so it happens once per process). Tests that only need the url_map register
`all_blueprints()` on a throwaway Flask app instead and never wire CSRF.
"""


def plan():
    """[(step, obj)] in registration order. step is one of "csrf" (wire
    double-submit CSRF onto the blueprint), "blueprint" (register it),
    "before_request" / "after_request" (an app-wide hook)."""
    from admin_routes import admin_bp, _admin_support_redaction
    from webhook_routes import webhook_bp
    from social_routes import social_bp
    from auth_routes import auth_bp
    from client_api import client_bp
    from toast_routes import toast_bp
    from square_routes import square_bp
    from clover_routes import clover_bp
    from rpower_routes import rpower_bp
    from status_routes import status_bp
    from mobile_api import mobile_bp
    from csrf import ensure_csrf_cookie
    from sales_audit_routes import audit_bp
    from onboarding_routes import onboarding_bp
    from strategy_routes import strategy_bp, strategy_mobile_bp, issue_link_bp
    from staff_routes import staff_bp, staff_csrf_check
    from staff_comms_routes import staff_comms_bp, staff_comms_mobile_bp
    from staff_knowledge_routes import staff_knowledge_bp, knowledge_bp, knowledge_mobile_bp

    steps = []
    # Every browser-facing blueprint gets double-submit CSRF enforcement.
    # webhook_bp (HMAC-verified external callers), auth_bp (own form tokens)
    # and mobile_bp (bearer-token auth, no cookie jar to carry a CSRF cookie —
    # see mobile_api.py's module docstring) are intentionally exempt.
    # status_bp is protected: its public pages are GETs, which the check
    # never touches, and its /admin/status writes post the public outage
    # banner (SEC-23).
    for bp in (admin_bp, client_bp, social_bp, toast_bp, square_bp, clover_bp, rpower_bp, status_bp):
        steps.append(("csrf", bp))
    steps.append(("blueprint", admin_bp))
    # Support's masking on every blueprint under /admin, not only admin_bp's
    # (re-audit 10/20/26 R7-11; applied once per response).
    steps.append(("after_request", _admin_support_redaction))
    # The in-person sales audit and onboarding at scale (bulk provisioning,
    # backfill pacing, setup confirmations, checklists, the DSR calendar,
    # partners): admin-console writes, CSRF-protected like admin_bp.
    steps += [("csrf", audit_bp), ("blueprint", audit_bp), ("csrf", onboarding_bp), ("blueprint", onboarding_bp)]
    for bp in (webhook_bp, social_bp, auth_bp, client_bp, toast_bp, square_bp, clover_bp, rpower_bp,
               status_bp, mobile_bp):
        steps.append(("blueprint", bp))
    # Issues, goals, outcomes, menu, invoices, demand, loss, morning brief —
    # web half CSRF-protected like client_bp; the mobile half bearer-token
    # like mobile_bp; the /i/<token> issue page is a public link whose token
    # is the credential (no session, no cookie jar to double-submit from).
    steps += [("csrf", strategy_bp), ("blueprint", strategy_bp), ("blueprint", strategy_mobile_bp),
              ("blueprint", issue_link_bp)]
    # The staff portal. Deliberately NOT csrf_protect-ed: the iPhone app calls
    # it with a Bearer token, and its sign-in POST happens before any session
    # exists. The browser portal (/staff/home) is cookie-authenticated, so
    # staff_csrf_check — app-wide, for staff_bp and staff_knowledge_bp only —
    # holds every cookie-authenticated write to the double-submit token
    # (staff_routes, "CSRF for the browser portal").
    steps.append(("before_request", staff_csrf_check))
    # Running late, announcements and the staff <-> manager thread: its staff
    # routes are on staff_bp; its owner half is the Team inbox under Labor —
    # web CSRF-protected, mobile bearer-token.
    steps += [("csrf", staff_comms_bp), ("blueprint", staff_comms_bp), ("blueprint", staff_comms_mobile_bp),
              ("blueprint", staff_bp)]
    # The staff brief's approval, house rules, docs and certifications
    # (console, web + mobile twins) and the employee's language, docs and ask.
    # The web half is CSRF-protected; the staff half takes the staff portal's
    # posture above.
    steps += [("csrf", knowledge_bp), ("blueprint", knowledge_bp), ("blueprint", knowledge_mobile_bp),
              ("blueprint", staff_knowledge_bp)]
    steps.append(("after_request", ensure_csrf_cookie))
    return steps


def all_blueprints():
    """Every blueprint the app registers, in registration order (22)."""
    return tuple(obj for step, obj in plan() if step == "blueprint")


def register_all(app):
    """Wire CSRF, register every blueprint and install the app-wide hooks on
    `app`, in hosted_dashboard's order. CSRF is wired once per blueprint per
    process (`_csrf_wired`), so a second app in the same process does not
    stack a second check."""
    from csrf import csrf_protect
    for step, obj in plan():
        if step == "csrf":
            if not getattr(obj, "_csrf_wired", False):
                csrf_protect(obj)
                obj._csrf_wired = True
        elif step == "blueprint":
            app.register_blueprint(obj)
        elif step == "before_request":
            app.before_request(obj)
        elif step == "after_request":
            app.after_request(obj)
        else:  # pragma: no cover — plan() writes only the four steps above
            raise ValueError(f"unknown registration step {step!r}")
    return app


def url_map_app(template_folder=None):
    """A throwaway Flask app with every blueprint registered and no app-wide
    hooks or boot: what a test needs to read the whole url_map or call a view
    without importing hosted_dashboard. Each blueprint's CSRF check is wired
    first (once per process, as register_all does): a blueprint takes no new
    hook once any app has registered it, so registering it bare here left a
    later test unable to wire it ("can no longer be called on the blueprint")."""
    from flask import Flask
    from csrf import csrf_protect
    app = Flask("cavnar_url_map", template_folder=template_folder)
    app.secret_key = "url-map-only"
    for step, obj in plan():
        if step == "csrf":
            # Skipped when a test app already registered it bare: it can take
            # no new hook then, and the url_map is the same either way.
            if not getattr(obj, "_csrf_wired", False) and not getattr(obj, "_got_registered_once", False):
                csrf_protect(obj)
                obj._csrf_wired = True
        elif step == "blueprint":
            app.register_blueprint(obj)
    return app
