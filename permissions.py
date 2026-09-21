"""permissions.py — Identity → Roles → Permissions.

Until now authorization in this app was four disconnected things: the
`is_admin` boolean, ~11 inline `role == "owner"` / `role == "member"`
comparisons scattered across six modules, a `can_manage_team` column, and a
URL-prefix table for billing modules. Adding a fourth role to that was unsafe
in both directions, and the codebase had already shipped the bug twice:

  - auth.py's invite flow gated on `role == 'owner'`, which no real client
    login has, so the whole Team feature 403'd for every actual account.
  - marketing_drafts.py gated approval the same way, with the same result,
    and settled on a deny-list (`CANNOT_APPROVE = {"member"}`) precisely so
    that a NEW role would not get silently locked out.

Those two fixes pull in opposite directions: an allow-list locks a new role
out of things it needs, a deny-list silently grants it things it must never
have. Employee accounts add a role whose entire purpose is to be denied
almost everything, so neither default is acceptable and the decision has to
move somewhere central.

This module is that place. It is a code registry, not a database table:
permissions are deployed artifacts rather than customer data, and keeping
them in code means an authorization check is a frozenset lookup with no
query, on a path that runs on all ~390 decorated routes.

The contract:

  - A ROLE is a name. It carries no privileges of its own.
  - A PERMISSION is a capability string. Everything is denied by default.
  - ROLE_PERMISSIONS maps one to the other, exhaustively and explicitly.
  - A role missing from the map has NO permissions. New roles start closed.

`is_admin` remains a separate superuser flag rather than a role, because it
is orthogonal — it is Will's own staff access, it spans every restaurant, and
folding it into the role column would conflate "what may this person do here"
with "is this person us".
"""

# ── Permissions ────────────────────────────────────────────────────────────
#
# Named by resource.action, with `.own` marking the employee-scoped variant
# of a capability that also exists unscoped. `.own` NEVER implies the
# unscoped form — that asymmetry is the whole point of the employee tier.

# The owner/manager console itself. This is the gate that keeps an employee
# session out of the ~370 existing @login_required / @mobile_login_required
# routes; without it, issuing an employee a session would hand them the
# entire dashboard API.
DASHBOARD_ACCESS = "dashboard.access"

# Multi-location: switching the session's active restaurant within a group.
LOCATION_SWITCH = "location.switch"

# Team: inviting and revoking logins, and rating/target/profile writes
# (the capability the inert `can_manage_team` column was reaching for).
TEAM_INVITE = "team.invite"
TEAM_REVOKE = "team.revoke"
TEAM_RATE = "team.rate"

# Marketing drafts an AI wrote, published under the restaurant's own name.
MARKETING_APPROVE = "marketing.approve"

# Manager-to-manager direct messages.
TEAM_MESSAGE = "team.message"

# Per-module read access, one per product module.
#
# DASHBOARD_ACCESS was a single boolean: hold it and every non-employee route
# in the app was open, because there was nothing below it. That made "manager"
# unsellable as a real role — an owner could not hire a shift manager without
# also handing over food costs, menu margins, labor analytics and the manager
# DM inbox — and it made data minimisation impossible.
#
# These deliberately reuse the module keys in auth._MODULE_PREFIXES, which
# already maps every route in the product to its module for billing. One path
# table, two questions asked of it: "is this module sold to this restaurant?"
# (entitlement) and "may this role read it?" (authorization).
REVIEWS_VIEW = "reviews.view"
LABOR_VIEW = "labor.view"
FOOD_COST_VIEW = "foodcost.view"      # food cost, menu margins — the financials
MARKETING_VIEW = "marketing.view"
INTEL_VIEW = "intel.view"

# module key (auth._MODULE_PREFIXES) → the permission that reads it.
MODULE_VIEW_PERMISSIONS = {
    "reviews": REVIEWS_VIEW,
    "labor": LABOR_VIEW,
    "inventory": FOOD_COST_VIEW,
    "marketing": MARKETING_VIEW,
    "intel": INTEL_VIEW,
}

_ALL_MODULES = frozenset(MODULE_VIEW_PERMISSIONS.values())

# Comp, void and refund patterns (loss_detection.py). Its own permission, not
# part of FOOD_COST_VIEW: a loss signal can name the manager who approved the
# comps, so an owner opening the numbers to a GM is a separate decision from
# opening this — and the manager being granted it may be the one it names.
LOSS_VIEW = "loss.view"

# What an owner may grant an individual login on top of its role, per
# location (permission_grants). A fixed list on purpose: administering logins,
# billing and switching locations are never grantable.
GRANTABLE = {
    FOOD_COST_VIEW: "Food cost & margins",
    LOSS_VIEW: "Comps & voids",
}
# Roles a grant can be given to. Owners already hold everything; employees
# are PIN identities with no console at all.
GRANTABLE_ROLES = frozenset({"manager", "member"})

# Employee-scoped. Each of these is limited server-side to the acting
# identity's own membership — see the staff routes, which derive the
# employee name from the session and never from the request.
SCHEDULE_VIEW_OWN = "schedule.view.own"
TASKS_VIEW_OWN = "tasks.view.own"
TASKS_COMPLETE_OWN = "tasks.complete.own"
PROFILE_MANAGE_OWN = "profile.manage.own"

ALL_PERMISSIONS = frozenset({
    DASHBOARD_ACCESS,
    LOCATION_SWITCH,
    TEAM_INVITE,
    TEAM_REVOKE,
    TEAM_RATE,
    TEAM_MESSAGE,
    MARKETING_APPROVE,
    SCHEDULE_VIEW_OWN,
    TASKS_VIEW_OWN,
    TASKS_COMPLETE_OWN,
    PROFILE_MANAGE_OWN,
    LOSS_VIEW,
}) | _ALL_MODULES


# ── Roles ──────────────────────────────────────────────────────────────────

ROLE_OWNER = "owner"        # multi-location login; can switch active restaurant
ROLE_CLIENT = "client"      # a restaurant's primary login (the DB default)
ROLE_MANAGER = "manager"    # new: runs a restaurant, does not administer logins
ROLE_MEMBER = "member"      # legacy: a teammate invited by the primary login
ROLE_EMPLOYEE = "employee"  # new: PIN identity, staff portal only
ROLE_SUPPORT = "support"    # Cavnar staff: reads the admin console, opens view-as, writes nothing

# What every non-employee role could already do before this module existed.
# Kept as one name so the legacy roles below are provably unchanged rather
# than re-derived by hand.
_CONSOLE_BASE = frozenset({
    DASHBOARD_ACCESS,
    TEAM_RATE,          # can_manage_team defaults open, and nothing sets it to 0
    MARKETING_APPROVE,
}) | _ALL_MODULES       # every module, which is what holding the console meant

ROLE_PERMISSIONS = {
    # No client-console permissions at all: a support login is not a
    # restaurant's login. What it MAY do is decided in auth.admin_required.
    ROLE_SUPPORT: frozenset(),
    # Everything, plus the location switcher. The only role client_api's
    # _do_switch_location / _do_group_locations and home_brief's group brief
    # have ever accepted.
    ROLE_OWNER: _CONSOLE_BASE | {LOCATION_SWITCH, TEAM_INVITE, TEAM_REVOKE, TEAM_MESSAGE, LOSS_VIEW},

    # The primary per-restaurant login: everything except switching between
    # locations, which it is refused today.
    ROLE_CLIENT: _CONSOLE_BASE | {TEAM_INVITE, TEAM_REVOKE, TEAM_MESSAGE, LOSS_VIEW},

    # New. Runs the floor: writes the schedule, answers reviews, posts
    # marketing, reads competitor intel, and is in the manager DM thread —
    # but does NOT see food cost or menu margins, does not administer logins,
    # and cannot switch locations.
    #
    # The food-cost omission is the entire reason this tier of permissions
    # exists. A shift manager needs labor and reviews to do the job and has no
    # business in the restaurant's margins, and until there was something
    # below DASHBOARD_ACCESS an owner could not express that.
    ROLE_MANAGER: (_CONSOLE_BASE - {FOOD_COST_VIEW}) | {TEAM_MESSAGE},

    # Legacy invited teammate. Refused invite/revoke (mobile_api.py) and
    # marketing approval (marketing_drafts.CANNOT_APPROVE); everything else
    # was open, including team rating, because can_manage_team is never
    # actually set to 0 anywhere in production code.
    #
    # The modules stay open here ON PURPOSE. Every live teammate login has
    # read every module since the day it was created; narrowing that silently
    # in a migration would take access away from people mid-shift with no
    # warning and no way to grant it back. TEAM_MESSAGE is withheld instead
    # because the manager DM inbox is new — nobody has ever had it, so nothing
    # is taken away, and "managers can talk to each other" is the feature it
    # was asked for. An owner who wants a narrower teammate promotes them to
    # `manager`, which is the role that expresses it.
    ROLE_MEMBER: frozenset({DASHBOARD_ACCESS, TEAM_RATE}) | _ALL_MODULES,

    # New. The staff portal and nothing else. Deliberately has no
    # DASHBOARD_ACCESS: that single omission is what keeps a PIN session out
    # of every owner route in the app.
    ROLE_EMPLOYEE: frozenset({
        SCHEDULE_VIEW_OWN,
        TASKS_VIEW_OWN,
        TASKS_COMPLETE_OWN,
        PROFILE_MANAGE_OWN,
    }),
}

# Roles that may sign in to the owner/manager console at all. Derived from
# the map rather than listed again, so it cannot drift out of step with it.
CONSOLE_ROLES = frozenset(
    r for r, perms in ROLE_PERMISSIONS.items() if DASHBOARD_ACCESS in perms
)


def normalize_role(role) -> str:
    """The stored role, with the one legacy shape that needs interpreting.

    `users.role` is `TEXT DEFAULT 'client'` and set_user_role() has never
    validated its input, so the column can in principle hold anything. A
    NULL/blank role means a row written before the column existed, which is
    exactly the primary login — it resolves to 'client' so those accounts
    keep working untouched. Anything else unrecognised is returned as-is and
    will simply match no entry in ROLE_PERMISSIONS, i.e. fail closed.
    """
    if role is None:
        return ROLE_CLIENT
    role = str(role).strip().lower()
    return role or ROLE_CLIENT


def permissions_for(role) -> frozenset:
    """Every permission a role carries. Unknown roles get nothing."""
    return ROLE_PERMISSIONS.get(normalize_role(role), frozenset())


def has_permission(user, permission: str) -> bool:
    """Whether this identity may do `permission` in its current context.

    `user` is the dict the auth decorators inject as current_user, so callers
    never pass a role string around by hand — the role is read from the
    session-resolved identity every time.

    is_admin is a superuser bypass, matching how it already behaves for
    billing (auth._billing_blocked) and module gating (auth._module_blocked).
    """
    if not user:
        return False
    if user.get("is_admin"):
        return True
    if permission in permissions_for(user.get("role")):
        return True
    # Owner-granted extras for this login at the location it is acting in
    # (auth.get_session_user loads them per request). Only GRANTABLE
    # permissions count, and only on a role a grant may be given to, so a
    # stray row can never hand out anything beyond the fixed list.
    return (permission in GRANTABLE
            and normalize_role(user.get("role")) in GRANTABLE_ROLES
            and permission in (user.get("grants") or ()))


def require_all(user, *perms) -> bool:
    return all(has_permission(user, p) for p in perms)


def is_employee(user) -> bool:
    """True for a PIN identity. Used where the question really is "is this
    the staff tier" rather than a specific capability — notably the console
    gate's error message, which should tell an employee to use the staff
    portal instead of claiming their session expired."""
    if not user:
        return False
    return normalize_role(user.get("role")) == ROLE_EMPLOYEE
