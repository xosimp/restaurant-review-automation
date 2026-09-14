"""permissions.py — the Identity → Roles → Permissions registry.

The point of these tests is not that the registry is internally consistent —
it is that the THREE ROLES THAT ALREADY EXIST IN PRODUCTION ('client',
'owner', 'member', plus is_admin) come out of it with exactly the privileges
the scattered inline role checks gave them before it existed. If that ever
stops being true, a real customer loses access to something they had.

The second half pins the new employee role closed: it is defined by what it
CANNOT do, and every one of those denials is load-bearing.
"""
import permissions as P


# ── the legacy roles are unchanged ─────────────────────────────────────────

def test_client_is_the_primary_login_and_keeps_everything_but_location_switch():
    """role='client' is the DB default and, today, the only role any real
    production account has. It could do everything except switch locations
    (client_api._do_switch_location refuses role != 'owner')."""
    u = {"role": "client"}
    assert P.has_permission(u, P.DASHBOARD_ACCESS)
    assert P.has_permission(u, P.TEAM_INVITE)
    assert P.has_permission(u, P.TEAM_REVOKE)
    assert P.has_permission(u, P.TEAM_RATE)
    assert P.has_permission(u, P.MARKETING_APPROVE)
    assert not P.has_permission(u, P.LOCATION_SWITCH)


def test_owner_is_client_plus_the_location_switcher():
    u = {"role": "owner"}
    assert P.has_permission(u, P.LOCATION_SWITCH)
    for perm in (P.DASHBOARD_ACCESS, P.TEAM_INVITE, P.TEAM_REVOKE,
                 P.TEAM_RATE, P.MARKETING_APPROVE):
        assert P.has_permission(u, perm), perm


def test_member_keeps_its_two_historical_denials_and_nothing_more():
    """An invited teammate was refused invite/revoke (mobile_api.py:4335,
    :4362) and marketing approval (marketing_drafts.CANNOT_APPROVE), and was
    allowed everything else — including team rating, because can_manage_team
    is never actually set to 0 anywhere in production code."""
    u = {"role": "member"}
    assert not P.has_permission(u, P.TEAM_INVITE)
    assert not P.has_permission(u, P.TEAM_REVOKE)
    assert not P.has_permission(u, P.MARKETING_APPROVE)
    assert not P.has_permission(u, P.LOCATION_SWITCH)
    assert P.has_permission(u, P.DASHBOARD_ACCESS)
    assert P.has_permission(u, P.TEAM_RATE)


def test_is_admin_bypasses_everything():
    """Matching how is_admin already short-circuits billing and module
    gating in auth.py."""
    u = {"role": "employee", "is_admin": 1}
    for perm in sorted(P.ALL_PERMISSIONS):
        assert P.has_permission(u, perm), perm


def test_a_row_written_before_the_role_column_existed_reads_as_client():
    """users.role is TEXT DEFAULT 'client', but a NULL would mean a row from
    before the migration — that is the primary login, and it must not lose
    access."""
    assert P.normalize_role(None) == P.ROLE_CLIENT
    assert P.normalize_role("") == P.ROLE_CLIENT
    assert P.has_permission({"role": None}, P.DASHBOARD_ACCESS)


def test_role_matching_is_case_and_whitespace_insensitive():
    assert P.has_permission({"role": " Owner "}, P.LOCATION_SWITCH)


# ── new roles are closed by default ────────────────────────────────────────

def test_an_unknown_role_gets_nothing():
    """set_user_role() has never validated its input. Junk in the column
    must fail closed rather than inheriting somebody else's privileges."""
    u = {"role": "sous-chef-supreme"}
    for perm in sorted(P.ALL_PERMISSIONS):
        assert not P.has_permission(u, perm), perm


def test_no_user_gets_nothing():
    assert not P.has_permission(None, P.DASHBOARD_ACCESS)
    assert not P.is_employee(None)


def test_employee_cannot_reach_the_owner_console():
    """The single most important assertion in this file. DASHBOARD_ACCESS is
    what the console decorators require; without it a PIN session cannot
    touch any of the ~370 existing owner routes."""
    u = {"role": "employee"}
    assert not P.has_permission(u, P.DASHBOARD_ACCESS)
    assert P.ROLE_EMPLOYEE not in P.CONSOLE_ROLES


def test_employee_cannot_manage_anything_or_switch_tenant():
    u = {"role": "employee"}
    for perm in (P.TEAM_INVITE, P.TEAM_REVOKE, P.TEAM_RATE,
                 P.MARKETING_APPROVE, P.LOCATION_SWITCH):
        assert not P.has_permission(u, perm), perm


def test_employee_gets_exactly_its_own_scoped_permissions():
    u = {"role": "employee"}
    assert P.has_permission(u, P.SCHEDULE_VIEW_OWN)
    assert P.has_permission(u, P.TASKS_VIEW_OWN)
    assert P.has_permission(u, P.TASKS_COMPLETE_OWN)
    assert P.has_permission(u, P.PROFILE_MANAGE_OWN)
    assert P.permissions_for("employee") == P.ROLE_PERMISSIONS[P.ROLE_EMPLOYEE]


def test_manager_runs_the_floor_without_administering_logins():
    u = {"role": "manager"}
    assert P.has_permission(u, P.DASHBOARD_ACCESS)
    assert P.has_permission(u, P.TEAM_RATE)
    assert P.has_permission(u, P.MARKETING_APPROVE)
    assert not P.has_permission(u, P.TEAM_INVITE)
    assert not P.has_permission(u, P.TEAM_REVOKE)
    assert not P.has_permission(u, P.LOCATION_SWITCH)


# ── registry invariants ────────────────────────────────────────────────────

def test_console_roles_is_derived_not_hand_listed():
    """CONSOLE_ROLES drives the console gate. If it were a second hand-kept
    list it could drift from ROLE_PERMISSIONS, which is exactly how a role
    ends up either locked out or silently let in."""
    assert P.CONSOLE_ROLES == frozenset(
        r for r, perms in P.ROLE_PERMISSIONS.items() if P.DASHBOARD_ACCESS in perms
    )
    assert P.ROLE_EMPLOYEE not in P.CONSOLE_ROLES
    for r in (P.ROLE_OWNER, P.ROLE_CLIENT, P.ROLE_MANAGER, P.ROLE_MEMBER):
        assert r in P.CONSOLE_ROLES, r


def test_every_granted_permission_is_a_declared_one():
    """Catches a typo'd permission string in ROLE_PERMISSIONS, which would
    otherwise silently grant nothing and never be noticed."""
    for role, perms in P.ROLE_PERMISSIONS.items():
        unknown = perms - P.ALL_PERMISSIONS
        assert not unknown, f"{role} grants undeclared permission(s): {unknown}"


def test_own_scoped_permissions_never_imply_the_unscoped_form():
    """schedule.view.own must not be readable as schedule.view. The employee
    tier's entire safety rests on that asymmetry."""
    u = {"role": "employee"}
    assert P.has_permission(u, P.SCHEDULE_VIEW_OWN)
    assert not P.has_permission(u, "schedule.view")
    assert not P.has_permission(u, "tasks.view")
