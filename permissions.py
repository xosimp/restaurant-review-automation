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
    MARKETING_APPROVE,
    SCHEDULE_VIEW_OWN,
    TASKS_VIEW_OWN,
    TASKS_COMPLETE_OWN,
    PROFILE_MANAGE_OWN,
})


# ── Roles ──────────────────────────────────────────────────────────────────

ROLE_OWNER = "owner"        # multi-location login; can switch active restaurant
ROLE_CLIENT = "client"      # a restaurant's primary login (the DB default)
ROLE_MANAGER = "manager"    # new: runs a restaurant, does not administer logins
ROLE_MEMBER = "member"      # legacy: a teammate invited by the primary login
ROLE_EMPLOYEE = "employee"  # new: PIN identity, staff portal only

# What every non-employee role could already do before this module existed.
# Kept as one name so the legacy roles below are provably unchanged rather
# than re-derived by hand.
_CONSOLE_BASE = frozenset({
    DASHBOARD_ACCESS,
    TEAM_RATE,          # can_manage_team defaults open, and nothing sets it to 0
    MARKETING_APPROVE,
})

ROLE_PERMISSIONS = {
    # Everything, plus the location switcher. The only role client_api's
    # _do_switch_location / _do_group_locations and home_brief's group brief
    # have ever accepted.
    ROLE_OWNER: _CONSOLE_BASE | {LOCATION_SWITCH, TEAM_INVITE, TEAM_REVOKE},

    # The primary per-restaurant login: everything except switching between
    # locations, which it is refused today.
    ROLE_CLIENT: _CONSOLE_BASE | {TEAM_INVITE, TEAM_REVOKE},

    # New. Runs the floor: full console, rates the team, approves marketing —
    # but does not administer logins and cannot switch locations.
    ROLE_MANAGER: _CONSOLE_BASE,

    # Legacy invited teammate. Refused invite/revoke (mobile_api.py) and
    # marketing approval (marketing_drafts.CANNOT_APPROVE); everything else
    # was open, including team rating, because can_manage_team is never
    # actually set to 0 anywhere in production code.
    ROLE_MEMBER: frozenset({DASHBOARD_ACCESS, TEAM_RATE}),

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
    return permission in permissions_for(user.get("role"))


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
