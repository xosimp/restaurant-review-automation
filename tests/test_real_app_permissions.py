"""Permissions and tenancy through the REAL composed app (audit #25 A01-15).

Every test here signs in through the app's own door (tests/real_app.py:
the /login form, /mobile/api/login, the staff PIN, the console's view-as)
and goes through hosted_dashboard's hooks — CSRF, the staff portal's CSRF
check, support masking, the session's real role resolution. Per role, one
read and one write; then the tenant boundary, CSRF and support masking, and
the rollout console. The in-process versions of several of these
(tests/test_edge_sec_permissions.py) fake the user or the wiring; these do
not.
"""
import pytest

from real_app import real_app  # noqa: F401  (the module-scoped fixture)

# The deploy gate's contract slice (TESTING.md, Tiers; blind re-audit 10/27/26).
pytestmark = pytest.mark.contract


@pytest.fixture(scope="module")
def world(real_app):
    rid = real_app.restaurant(name="Real Grill", module_reviews=1, module_labor=1, module_inventory=1,
                              module_marketing=1)
    other = real_app.restaurant(name="Other Bistro", module_reviews=1)
    other_owner = real_app.call("auth.create_user", other, "otherowner", "other@realapp.test", "other-pass-1")
    real_app.call("auth.upsert_membership", other_owner, other, "client")
    return {"rid": rid, "other": other, "other_owner": other_owner}


def _events(real_app, action):
    return real_app.query("SELECT target, result, before_json, after_json FROM admin_events "
                          "WHERE source='admin' AND event_type=? ORDER BY id", action)


def _retention(real_app, rid):
    return int(real_app.call("models.get_restaurant", rid)["data_retention_months"] or 0)


# ── owner ────────────────────────────────────────────────────────────────────

def test_the_owner_reads_the_team_and_sets_review_retention(real_app, world):
    owner = real_app.client("owner", world["rid"])
    assert owner.get("/api/account/team").status == 200
    r = owner.post("/api/account-settings/data-retention", json={"months": 12})
    assert r.status == 200, r
    assert _retention(real_app, world["rid"]) == 12


def test_a_web_write_without_the_csrf_header_is_refused(real_app, world):
    owner = real_app.client("owner", world["rid"])
    r = owner.post("/api/account-settings/data-retention", json={"months": 3}, csrf=False)
    assert r.status == 403 and "CSRF" in r.text
    assert _retention(real_app, world["rid"]) != 3


# ── manager ──────────────────────────────────────────────────────────────────

def test_a_manager_reads_the_console_but_cannot_shorten_retention(real_app, world):
    before = _retention(real_app, world["rid"])
    mgr = real_app.client("manager", world["rid"])
    assert mgr.get("/api/home/brief").status == 200
    assert mgr.post("/api/account-settings/data-retention", json={"months": 6}).status == 403
    phone = real_app.client("mobile:manager", world["rid"])
    assert phone.get("/mobile/api/me").status == 200
    assert phone.post("/mobile/api/account/data-retention", json={"months": 6}).status == 403
    assert _retention(real_app, world["rid"]) == before


def test_a_manager_is_refused_food_cost_margins(real_app, world):
    """The role gate fails closed through the real session: a manager does
    not hold FOOD_COST_VIEW."""
    mgr = real_app.client("mobile:manager", world["rid"])
    r = mgr.get("/mobile/api/food-cost/analytics")
    assert r.status == 403 and r.json and r.json.get("ok") is False


# ── staff (PIN) ──────────────────────────────────────────────────────────────

def test_a_staff_pin_session_reads_its_own_portal_and_writes_its_language(real_app, world):
    staff = real_app.client("staff", world["rid"], name="Ana")
    assert staff.get("/staff/api/me").status == 200
    assert staff.post("/staff/api/language", json={"language": "es"}).status == 200


def test_a_staff_pin_session_never_reaches_the_owner_console(real_app, world):
    staff = real_app.client("staff", world["rid"], name="Ben")
    assert staff.get("/api/account/team").status in (401, 403)
    assert staff.post("/api/account-settings/data-retention", json={"months": 1}).status in (401, 403)


def test_a_staff_cookie_write_without_the_csrf_header_is_refused(real_app, world):
    staff = real_app.client("staff", world["rid"], name="Cara")
    assert staff.post("/staff/api/language", json={"language": "es"}, csrf=False).status == 403


# ── support (view-as) and support masking ────────────────────────────────────

def test_support_reads_as_the_client_through_view_as_and_is_refused_every_write(real_app, world):
    support = real_app.client("support")
    support.view_as(world["rid"])
    assert support.get("/api/account/team").status == 200
    before = _retention(real_app, world["rid"])
    assert support.post("/api/account-settings/data-retention", json={"months": 1}).status == 403
    assert support.post("/api/toggle-2fa", json={"enabled": True}).status == 403
    assert _retention(real_app, world["rid"]) == before


def test_support_sees_the_console_with_personal_details_masked(real_app, world):
    support = real_app.client("support")
    r = support.get(f"/admin/api/client/{world['rid']}")
    assert r.status == 200, r
    assert r.headers.get("X-Redacted") == "support"
    email = real_app.call("models.get_restaurant", world["rid"])["owner_email"]
    assert email and email not in r.text                                 # the owner email is masked


def test_an_admin_sees_the_same_console_unmasked(real_app, world):
    admin = real_app.client("admin")
    r = admin.get(f"/admin/api/client/{world['rid']}")
    assert r.status == 200 and r.headers.get("X-Redacted") is None
    assert real_app.call("models.get_restaurant", world["rid"])["owner_email"] in r.text


def test_an_admin_view_as_session_is_labelled_as_one(real_app, world):
    """Moved from tests/test_tenant_isolation.py (which faked the admin with
    a patched get_current_user): the real view-as POST mints a session the
    app marks admin-view-as."""
    admin = real_app.client("admin")
    admin.view_as(world["other"])
    rows = real_app.query("SELECT device_type FROM sessions WHERE user_id IN "
                          "(SELECT id FROM users WHERE restaurant_id=?)", world["other"])
    assert "admin-view-as" in [r["device_type"] for r in rows]


# ── the tenant boundary ──────────────────────────────────────────────────────

def test_another_restaurants_conversation_is_refused_through_the_real_app(real_app, world):
    them = world["other_owner"]
    cid = real_app.call("models.save_ask_message", world["other"], "user", "their secret plan", user_id=them)
    owner = real_app.client("owner", world["rid"])
    r = owner.get(f"/api/ask-cavnar/conversations/{cid}")
    assert r.status == 404 and "their secret plan" not in r.text
    assert owner.request("DELETE", f"/api/ask-cavnar/conversations/{cid}").status in (403, 404)
    assert real_app.call("models.get_ask_conversation", world["other"], cid, viewer_id=them) is not None


def test_a_switch_to_another_owners_location_is_refused(real_app, world):
    """A multi-location owner switches only within its own group: B's id in
    the body is refused even when the group NAME matches."""
    mine = real_app.restaurant(name="Mine One", location_group="Same Name", owner_email="mine@realapp.test")
    theirs = real_app.restaurant(name="Theirs One", location_group="Same Name", owner_email="theirs@realapp.test")
    owner = real_app.client("owner", mine, multi=True)
    r = owner.post("/api/switch-location", json={"restaurant_id": theirs})
    assert r.status in (403, 404) or (r.json or {}).get("ok") is False, r
    assert "Theirs One" not in owner.get("/api/account/team").text


# ── the rollout console (rollout.py) ─────────────────────────────────────────

def test_an_admin_sets_a_rollout_and_it_is_audited(real_app, world):
    admin = real_app.client("admin")
    r = admin.post("/admin/api/rollout", json={"feature": "real_app_probe", "percent": 25, "allow": [world["rid"]]})
    assert r.status == 200 and r.json["flag"]["percent"] == 25, r
    assert real_app.call("rollout.enabled", "real_app_probe", world["rid"]) is True
    events = _events(real_app, "rollout.set")
    assert events and events[-1]["target"] == "rollout:real_app_probe" and events[-1]["result"] == "ok"
    listed = admin.get("/admin/api/rollout").json["flags"]
    assert [f["feature"] for f in listed] == ["real_app_probe"]


def test_support_cannot_set_a_rollout(real_app, world):
    support = real_app.client("support")
    assert support.post("/admin/api/rollout", json={"feature": "real_app_probe", "percent": 100}).status == 403
    assert support.get("/admin/api/rollout").status == 200


def test_a_malformed_rollout_is_refused_and_audited(real_app, world):
    admin = real_app.client("admin")
    r = admin.post("/admin/api/rollout", json={"feature": "Bad Name!", "percent": 50})
    assert r.status == 400
    assert _events(real_app, "rollout.set")[-1]["result"] == "refused"
