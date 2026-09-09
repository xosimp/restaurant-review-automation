"""Multi-tenant and multi-location isolation — the audit's findings, pinned.

Every test here corresponds to a finding from the isolation audit and is
written to fail against the code as it was. The theme running through all of
them: the request boundary was already sound (no route ever accepted a
restaurant_id from a client), and the failures were one layer down — helper
functions that took a tenant argument and didn't use it, and ambient
identifiers (a Google Place ID, an owner email, a phone number) treated as if
they were unique when nothing enforced that.
"""
import sqlite3

import pytest
from flask import Flask

import admin_routes
import auth
import client_api
import guest_marketing
import home_brief
import inventory_ledger
import mobile_api
import models
import notify
import webhook_routes
from auth import create_user, init_auth
from models import Restaurant, Review, create_restaurant, save_reviews, update_restaurant


def _redirect_db(monkeypatch, db_path):
    real = models.get_conn
    redirect = lambda *a, **k: real(db_path)
    for mod in (models, auth, admin_routes, client_api, mobile_api, guest_marketing,
                webhook_routes, home_brief, notify):
        monkeypatch.setattr(mod, "get_conn", redirect, raising=False)


@pytest.fixture(autouse=True)
def _init(db_path, monkeypatch):
    _redirect_db(monkeypatch, db_path)
    monkeypatch.setattr(models, "DB_PATH", db_path)
    init_auth(db_path=db_path)
    guest_marketing.init_guest_marketing(db_path=db_path)


def _loc(db_path, name, **kw):
    kw.setdefault("owner_email", "owner@x.test")
    return create_restaurant(Restaurant(name=name, **kw), db_path=db_path)


# ══ P0-1 · reviews are keyed per restaurant ═════════════════════════════════
#
# UNIQUE(platform, external_id) was global. Two restaurants on one
# google_place_id produce identical external_ids, so whichever was fetched
# first claimed every review and the rest were silently skipped — with the
# reviews filed under the wrong restaurant and replies drafted in the wrong
# brand voice and posted to the wrong Google listing.

def _google_review(rid, when="1725800000", author="Dana"):
    return Review(restaurant_id=rid, platform="google", external_id=f"google_{when}_{author}",
                  author=author, rating=2, text="Slow tonight.")


def test_two_locations_can_each_hold_the_same_google_review(db_path):
    # A live restaurant and the demo copy that mirrors its listing — the
    # shape that exists in the real database today, and the one the unique
    # index deliberately still permits.
    chicago = _loc(db_path, "Syrup Chicago", google_place_id="PLACE_X")
    dallas = _loc(db_path, "Syrup Chicago (demo)", google_place_id="PLACE_X", is_demo=1)

    n1, _ = save_reviews([_google_review(chicago)], db_path=db_path)
    n2, _ = save_reviews([_google_review(dallas)], db_path=db_path)
    assert (n1, n2) == (1, 1), "the second location used to be silently starved"

    conn = models.get_conn(db_path)
    owners = [r[0] for r in conn.execute(
        "SELECT restaurant_id FROM reviews WHERE platform='google' ORDER BY restaurant_id")]
    conn.close()
    assert owners == sorted([chicago, dallas])


def test_a_refetch_of_the_same_review_is_still_deduped(db_path):
    """The per-restaurant key must not turn dedupe off."""
    rid = _loc(db_path, "Solo")
    assert save_reviews([_google_review(rid)], db_path=db_path)[0] == 1
    assert save_reviews([_google_review(rid)], db_path=db_path)[0] == 0


def test_the_reviews_table_carries_the_per_restaurant_key(db_path):
    conn = models.get_conn(db_path)
    sql = conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='reviews'").fetchone()[0]
    conn.close()
    flat = " ".join(sql.split())
    assert "UNIQUE(restaurant_id, platform, external_id)" in flat
    assert "UNIQUE(platform, external_id)" not in flat


def test_the_migration_rekeys_an_existing_database_without_losing_rows(tmp_path):
    """The real upgrade path: a database created under the old global key."""
    old = str(tmp_path / "legacy.db")
    conn = sqlite3.connect(old)
    conn.executescript("""
        CREATE TABLE restaurants (id INTEGER PRIMARY KEY, name TEXT, google_place_id TEXT, is_demo INTEGER DEFAULT 0);
        CREATE TABLE reviews (
            id INTEGER PRIMARY KEY AUTOINCREMENT, restaurant_id INTEGER NOT NULL,
            platform TEXT NOT NULL, external_id TEXT NOT NULL, author TEXT,
            rating INTEGER NOT NULL, text TEXT NOT NULL, fetched_at TEXT NOT NULL,
            UNIQUE(platform, external_id));
        INSERT INTO restaurants (id, name) VALUES (1, 'A'), (2, 'B');
        INSERT INTO reviews (restaurant_id, platform, external_id, author, rating, text, fetched_at)
        VALUES (1,'google','g_1','Dana',2,'slow','2026-09-01'),
               (1,'google','g_2','Ali',5,'great','2026-09-02');
    """)
    conn.commit()

    assert models._reviews_unique_is_global(conn) is True
    models._migrate_reviews_unique(conn)
    assert models._reviews_unique_is_global(conn) is False
    assert conn.execute("SELECT COUNT(*) FROM reviews").fetchone()[0] == 2, "no row may be lost"

    # The other restaurant can now hold the same external_id.
    conn.execute("INSERT INTO reviews (restaurant_id, platform, external_id, author, rating, text, fetched_at)"
                 " VALUES (2,'google','g_1','Dana',2,'slow','2026-09-01')")
    conn.commit()
    assert conn.execute("SELECT COUNT(*) FROM reviews").fetchone()[0] == 3
    # ...and the indexes the rebuild dropped are back.
    idx = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='index' AND tbl_name='reviews'")}
    assert "idx_reviews_restaurant" in idx
    conn.close()


def test_the_migration_is_a_no_op_the_second_time(tmp_path, db_path):
    conn = models.get_conn(db_path)
    before = conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='reviews'").fetchone()[0]
    models._migrate_reviews_unique(conn)
    after = conn.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='reviews'").fetchone()[0]
    conn.close()
    assert before == after


# ══ P0-1 / P2-8 · one live restaurant per Google listing ═══════════════════

def test_a_place_id_already_on_a_live_restaurant_is_a_conflict(db_path):
    _loc(db_path, "Syrup Chicago", google_place_id="PLACE_X")
    assert models.place_id_conflict("PLACE_X", db_path=db_path) == "Syrup Chicago"
    assert models.place_id_conflict("PLACE_OTHER", db_path=db_path) is None
    assert models.place_id_conflict("", db_path=db_path) is None


def test_a_restaurant_does_not_conflict_with_itself(db_path):
    rid = _loc(db_path, "Syrup Chicago", google_place_id="PLACE_X")
    assert models.place_id_conflict("PLACE_X", exclude_id=rid, db_path=db_path) is None


def test_a_demo_copy_may_mirror_a_real_listing(db_path):
    """A demo deliberately points at a real listing and never fetches."""
    _loc(db_path, "Gia Mia (demo)", google_place_id="PLACE_X", is_demo=1)
    assert models.place_id_conflict("PLACE_X", db_path=db_path) is None


def test_the_admin_refuses_to_connect_a_listing_twice(db_path, monkeypatch):
    from admin_routes import admin_bp, save_client_settings
    monkeypatch.setattr(auth, "get_current_user", lambda: {"id": 999, "is_admin": 1})
    _loc(db_path, "Syrup Chicago", google_place_id="PLACE_X")
    mine = _loc(db_path, "Syrup Dallas")

    app = Flask(__name__)
    app.register_blueprint(admin_bp)
    with app.test_request_context(f"/admin/client-settings/{mine}", method="POST",
                                  json={"name": "Syrup Dallas", "owner_email": "o@x.test",
                                        "google_place_id": "PLACE_X"}):
        resp = save_client_settings(mine)
    body = resp.get_json() if hasattr(resp, "get_json") else resp[0].get_json()
    assert body["ok"] is False and "Syrup Chicago" in body["error"]
    assert models.get_restaurant(mine, db_path=db_path).google_place_id in (None, "")


# ══ P1-2 · alerts reach this location's recipients, nobody else's ══════════

def test_extra_alert_emails_come_from_the_restaurant_the_alert_is_about(db_path):
    chicago = _loc(db_path, "Syrup Chicago", owner_email="owner@syrup.test")
    dallas = _loc(db_path, "Syrup Dallas", owner_email="owner@syrup.test")
    update_restaurant(chicago, {"alert_extra_emails": "chicago-gm@syrup.test"}, db_path=db_path)
    update_restaurant(dallas, {"alert_extra_emails": "dallas-gm@syrup.test"}, db_path=db_path)

    to = notify.alert_recipients("owner@syrup.test", restaurant_id=dallas, db_path=db_path)
    assert to == ["owner@syrup.test", "dallas-gm@syrup.test"]
    assert "chicago-gm@syrup.test" not in to


def test_without_a_restaurant_the_owner_alone_is_mailed_rather_than_a_guess(db_path):
    """The old fallback was `WHERE owner_email=? LIMIT 1` — a lookup on a
    deliberately non-unique column, which returned an arbitrary location."""
    chicago = _loc(db_path, "Syrup Chicago", owner_email="owner@syrup.test")
    _loc(db_path, "Syrup Dallas", owner_email="owner@syrup.test")
    update_restaurant(chicago, {"alert_extra_emails": "chicago-gm@syrup.test"}, db_path=db_path)

    assert notify.alert_recipients("owner@syrup.test", db_path=db_path) == ["owner@syrup.test"]


def test_every_alert_email_call_site_names_its_restaurant():
    """A regression guard on the shape, not just this instance: three of the
    four call sites used to omit it."""
    import re
    src = open("notify.py").read()
    calls = re.findall(r"_send_alert_email\((?!owner_email: str)[^)]*\)", src, re.S)
    assert calls, "expected to find the call sites"
    for c in calls:
        assert "restaurant_id" in c, f"call site without a restaurant: {' '.join(c.split())}"


# ══ P1-3 · ingredient writes stay inside their restaurant ══════════════════

def _ingredient(rid, name="Tomatoes", **kw):
    return inventory_ledger.create_ingredient(rid, name=name, unit_cost=kw.pop("unit_cost", 1.0), **kw)


def test_an_ingredient_cannot_be_edited_through_another_locations_url(db_path):
    chicago, dallas = _loc(db_path, "Chicago"), _loc(db_path, "Dallas")
    theirs = _ingredient(dallas, "San Marzano", unit_cost=9.0)

    assert inventory_ledger.update_ingredient(chicago, theirs, unit_cost=0.01) is False
    rows = inventory_ledger.list_ingredients(dallas)
    assert [r["unit_cost"] for r in rows] == [9.0], "the write must not land"


def test_an_ingredient_edit_still_works_for_its_own_restaurant(db_path):
    rid = _loc(db_path, "Chicago")
    iid = _ingredient(rid, "Tomatoes", unit_cost=2.0)
    assert inventory_ledger.update_ingredient(rid, iid, unit_cost=3.5) is True
    assert inventory_ledger.list_ingredients(rid)[0]["unit_cost"] == 3.5


def test_an_ingredient_cannot_be_deactivated_from_another_restaurant(db_path):
    chicago, dallas = _loc(db_path, "Chicago"), _loc(db_path, "Dallas")
    theirs = _ingredient(dallas)
    assert inventory_ledger.deactivate_ingredient(chicago, theirs) is False
    assert len(inventory_ledger.list_ingredients(dallas)) == 1
    assert inventory_ledger.deactivate_ingredient(dallas, theirs) is True


# ══ P1-4 · a recipe cannot reach across restaurants ════════════════════════

def test_a_recipe_refuses_another_restaurants_ingredient(db_path):
    chicago, dallas = _loc(db_path, "Chicago"), _loc(db_path, "Dallas")
    dish = inventory_ledger.create_menu_item(chicago, "Margherita")
    their_tomatoes = _ingredient(dallas, "San Marzano", unit_cost=9.0)

    assert inventory_ledger.add_recipe_ingredient(chicago, dish, their_tomatoes, 0.5) == 0
    assert inventory_ledger.list_menu_items_with_recipes(chicago)[0]["recipe"] == []


def test_a_recipe_inside_one_restaurant_still_works(db_path):
    rid = _loc(db_path, "Chicago")
    dish = inventory_ledger.create_menu_item(rid, "Margherita")
    tomatoes = _ingredient(rid, "Tomatoes", unit_cost=2.0)
    assert inventory_ledger.add_recipe_ingredient(rid, dish, tomatoes, 0.5) > 0
    assert len(inventory_ledger.list_menu_items_with_recipes(rid)[0]["recipe"]) == 1


def test_plate_cost_ignores_a_cross_restaurant_recipe_row_already_in_the_data(db_path):
    """Rows written before the guard existed must not keep poisoning the
    number — the costing join is filtered too, not just the write."""
    chicago, dallas = _loc(db_path, "Chicago"), _loc(db_path, "Dallas")
    dish = inventory_ledger.create_menu_item(chicago, "Margherita")
    ours = _ingredient(chicago, "Tomatoes", unit_cost=2.0)
    theirs = _ingredient(dallas, "San Marzano", unit_cost=100.0)
    inventory_ledger.add_recipe_ingredient(chicago, dish, ours, 1.0)
    # Simulate legacy data: the cross-restaurant row, inserted directly.
    conn = models.get_conn(db_path)
    conn.execute("INSERT INTO recipe_ingredients (menu_item_id, ingredient_id, qty_per_unit) VALUES (?,?,?)",
                 (dish, theirs, 1.0))
    conn.commit(); conn.close()

    inventory_ledger.set_menu_item_price(chicago, dish, 10.0)
    priced = inventory_ledger.menu_profitability(chicago)["priced"]
    assert priced and priced[0]["plate_cost"] == 2.0, "Dallas's $100 ingredient must not cost Chicago's plate"


def test_a_recipe_row_cannot_be_deleted_from_another_restaurant(db_path):
    chicago, dallas = _loc(db_path, "Chicago"), _loc(db_path, "Dallas")
    dish = inventory_ledger.create_menu_item(dallas, "Queso")
    ing = _ingredient(dallas, "Cheddar")
    row = inventory_ledger.add_recipe_ingredient(dallas, dish, ing, 1.0)
    assert inventory_ledger.delete_recipe_ingredient(chicago, row) is False
    assert inventory_ledger.delete_recipe_ingredient(dallas, row) is True


# ══ P1-5 · the stock ledger validates its (restaurant, ingredient) pair ════

def test_a_recount_against_another_restaurants_ingredient_is_refused(db_path):
    chicago, dallas = _loc(db_path, "Chicago"), _loc(db_path, "Dallas")
    theirs = _ingredient(dallas, "Flour")
    result = inventory_ledger.record_recount(chicago, theirs, 5.0)
    assert result.get("ok") is False

    conn = models.get_conn(db_path)
    n = conn.execute("SELECT COUNT(*) FROM ingredient_stock_events WHERE restaurant_id=?", (chicago,)).fetchone()[0]
    conn.close()
    assert n == 0, "no event may be written against a foreign ingredient"


def test_receiving_against_another_restaurants_ingredient_is_refused(db_path):
    chicago, dallas = _loc(db_path, "Chicago"), _loc(db_path, "Dallas")
    theirs = _ingredient(dallas, "Flour")
    assert inventory_ledger.record_receiving(chicago, theirs, 10.0) == 0


def test_the_ledger_still_works_for_its_own_ingredient(db_path):
    rid = _loc(db_path, "Chicago")
    iid = _ingredient(rid, "Flour")
    assert inventory_ledger.record_recount(rid, iid, 5.0).get("ok") is not False
    assert inventory_ledger.record_receiving(rid, iid, 10.0) > 0


# ══ P1-6 · an ambiguous SMS reply is never attributed by guess ═════════════

def _invite(db_path, rid, phone="+13125550100"):
    return guest_marketing.record_optin_invite(rid, phone, db_path=db_path)


def test_one_pending_invite_resolves_cleanly(db_path):
    rid = _loc(db_path, "Syrup Chicago")
    _invite(db_path, rid)
    reply = guest_marketing.handle_inbound_sms("+13125550100", "YES", db_path=db_path)
    assert "Syrup Chicago" in reply
    assert [c["phone"] for c in guest_marketing.get_guest_contacts(rid, db_path=db_path)] == ["+13125550100"]


def test_two_restaurants_texting_one_guest_does_not_enrol_them_in_a_guess(db_path):
    """The failure: a guest consents to one restaurant and is added to the
    other's marketing list, with that owner then seeing their number."""
    chicago = _loc(db_path, "Syrup Chicago")
    other = _loc(db_path, "Kimball Diner", owner_email="other@x.test")
    _invite(db_path, chicago)
    _invite(db_path, other)

    reply = guest_marketing.handle_inbound_sms("+13125550100", "YES", db_path=db_path)
    assert "Which restaurant" in reply
    assert guest_marketing.get_guest_contacts(chicago, db_path=db_path) == []
    assert guest_marketing.get_guest_contacts(other, db_path=db_path) == []


def test_naming_the_restaurant_resolves_the_ambiguity(db_path):
    chicago = _loc(db_path, "Syrup Chicago")
    other = _loc(db_path, "Kimball Diner", owner_email="other@x.test")
    _invite(db_path, chicago)
    _invite(db_path, other)

    guest_marketing.handle_inbound_sms("+13125550100", "YES", db_path=db_path)
    reply = guest_marketing.handle_inbound_sms("+13125550100", "Kimball Diner", db_path=db_path)
    assert "Kimball Diner" in reply
    assert guest_marketing.get_guest_contacts(chicago, db_path=db_path) == []
    assert len(guest_marketing.get_guest_contacts(other, db_path=db_path)) == 1


def test_stop_is_still_global_and_needs_no_resolution(db_path):
    a = _loc(db_path, "Syrup Chicago")
    b = _loc(db_path, "Kimball Diner", owner_email="other@x.test")
    for rid in (a, b):
        guest_marketing.add_guest_contact_sms_optin(rid, "+13125550100", db_path=db_path)
    guest_marketing.handle_inbound_sms("+13125550100", "STOP", db_path=db_path)
    conn = models.get_conn(db_path)
    flags = [r[0] for r in conn.execute("SELECT unsubscribed FROM guest_contacts WHERE phone=?", ("+13125550100",))]
    conn.close()
    assert flags == [1, 1], "stop means stop, everywhere"


def test_the_resolved_restaurants_invite_is_the_one_closed(db_path):
    chicago = _loc(db_path, "Syrup Chicago")
    _invite(db_path, chicago)
    guest_marketing.handle_inbound_sms("+13125550100", "YES", db_path=db_path)
    conn = models.get_conn(db_path)
    row = conn.execute("SELECT restaurant_id, response FROM sms_optin_invites WHERE responded_at IS NOT NULL").fetchone()
    conn.close()
    assert row and row[0] == chicago and row[1] == "yes"


# ══ P1-7 · one subscription moves every location it bills ═════════════════

def test_billing_propagates_across_a_stripe_customer_when_no_group_is_set(db_path):
    """location_group is free text an admin types and is unset on every
    production row — without this the owner's single payment activated one
    location and the entitlement gate 402'd the rest."""
    a = _loc(db_path, "Syrup Chicago")
    b = _loc(db_path, "Syrup Dallas")
    solo = _loc(db_path, "Unrelated", owner_email="someone@else.test")
    for rid in (a, b):
        update_restaurant(rid, {"stripe_customer_id": "cus_syrup"}, db_path=db_path)
    update_restaurant(solo, {"stripe_customer_id": "cus_other"}, db_path=db_path)

    assert sorted(webhook_routes._sibling_restaurant_ids(a)) == sorted([a, b])
    assert webhook_routes._sibling_restaurant_ids(solo) == [solo]


def test_a_group_still_wins_over_the_stripe_customer(db_path):
    a = _loc(db_path, "Syrup Chicago", location_group="Syrup")
    b = _loc(db_path, "Syrup Dallas", location_group="Syrup")
    assert sorted(webhook_routes._sibling_restaurant_ids(a)) == sorted([a, b])


def test_a_location_with_neither_a_group_nor_a_customer_stands_alone(db_path):
    solo = _loc(db_path, "Solo")
    assert webhook_routes._sibling_restaurant_ids(solo) == [solo]


# ══ P2-10 · a group brief is invalidated by a change at any location ══════

def test_invalidating_one_location_clears_the_group_rollup(db_path):
    from datetime import datetime, timezone
    home_brief._CACHE.clear()
    now = datetime.now(timezone.utc)
    home_brief._CACHE[(7, 1)] = (now, {"single": True})
    home_brief._CACHE[("group", 7, 1)] = (now, {"group": True})
    home_brief._CACHE[(9, 2)] = (now, {"other": True})

    home_brief.invalidate(7)
    assert (7, 1) not in home_brief._CACHE
    assert ("group", 7, 1) not in home_brief._CACHE, "the group key starts with 'group', never the rid"
    assert (9, 2) in home_brief._CACHE
    home_brief._CACHE.clear()


def test_a_change_at_a_sibling_location_also_clears_the_group_rollup(db_path):
    """The group key only records the owner's BASE location, so matching on
    the changed location alone would still serve a stale rollup."""
    from datetime import datetime, timezone
    home_brief._CACHE.clear()
    home_brief._CACHE[("group", 7, 1)] = (datetime.now(timezone.utc), {"group": True})
    home_brief.invalidate(99)          # a sibling, not the base
    assert ("group", 7, 1) not in home_brief._CACHE
    home_brief._CACHE.clear()


# ══ P2-11 · a teammate cannot read the owner's assistant history ══════════

def test_a_conversation_is_visible_to_the_person_who_started_it(db_path):
    rid = _loc(db_path, "Chicago")
    owner = create_user(rid, "owner", "owner@x.test", "pw", db_path=db_path)
    mate = create_user(rid, "mate", "mate@x.test", "pw", db_path=db_path)
    cid = models.save_ask_message(rid, "user", "what is our labor cost", user_id=owner, db_path=db_path)

    assert models.get_ask_conversation(rid, cid, db_path=db_path, viewer_id=owner) is not None
    assert models.get_ask_conversation(rid, cid, db_path=db_path, viewer_id=mate) is None
    assert [c["id"] for c in models.list_ask_conversations(rid, db_path=db_path, viewer_id=owner)] == [cid]
    assert models.list_ask_conversations(rid, db_path=db_path, viewer_id=mate) == []
    assert models.get_ask_history(rid, conversation_id=cid, db_path=db_path, viewer_id=mate) == []


def test_a_teammate_cannot_delete_the_owners_conversation(db_path):
    rid = _loc(db_path, "Chicago")
    owner = create_user(rid, "owner", "owner@x.test", "pw", db_path=db_path)
    mate = create_user(rid, "mate", "mate@x.test", "pw", db_path=db_path)
    cid = models.save_ask_message(rid, "user", "secret", user_id=owner, db_path=db_path)
    assert models.delete_ask_conversation(rid, cid, db_path=db_path, viewer_id=mate) is False
    assert models.delete_ask_conversation(rid, cid, db_path=db_path, viewer_id=owner) is True


def test_history_from_before_the_user_column_stays_readable(db_path):
    """Legacy rows carry user_id NULL — hiding them from everyone would be a
    silent data loss, not a fix."""
    rid = _loc(db_path, "Chicago")
    mate = create_user(rid, "mate", "mate@x.test", "pw", db_path=db_path)
    cid = models.save_ask_message(rid, "user", "old chat", user_id=None, db_path=db_path)
    assert models.get_ask_conversation(rid, cid, db_path=db_path, viewer_id=mate) is not None


def test_a_conversation_is_still_refused_across_restaurants(db_path):
    mine, theirs = _loc(db_path, "Mine"), _loc(db_path, "Theirs", owner_email="t@x.test")
    uid = create_user(theirs, "them", "them@x.test", "pw", db_path=db_path)
    cid = models.save_ask_message(theirs, "user", "their secret", user_id=uid, db_path=db_path)
    assert models.get_ask_conversation(mine, cid, db_path=db_path, viewer_id=uid) is None


# ══ P2-12 · an admin view-as session is labelled as one ═══════════════════

def test_a_view_as_session_is_marked_and_not_mistaken_for_the_clients_own(db_path, monkeypatch):
    from admin_routes import admin_bp, view_as_client
    monkeypatch.setattr(auth, "get_current_user", lambda: {"id": 999, "is_admin": 1, "username": "will"})
    rid = _loc(db_path, "Chicago")
    create_user(rid, "client", "client@x.test", "pw", db_path=db_path)

    app = Flask(__name__)
    app.register_blueprint(admin_bp)
    with app.test_request_context(f"/admin/view-as/{rid}"):
        view_as_client(rid)

    conn = models.get_conn(db_path)
    kinds = [r[0] for r in conn.execute("SELECT device_type FROM sessions")]
    conn.close()
    assert kinds == ["admin-view-as"]


# ══ P2-13 · template use counts cannot be bumped unscoped ═════════════════

def test_incrementing_a_template_requires_its_restaurant(db_path):
    mine, theirs = _loc(db_path, "Mine"), _loc(db_path, "Theirs", owner_email="t@x.test")
    tid = models.create_response_template(theirs, "Thanks", "Thanks!", db_path=db_path)

    with pytest.raises(TypeError):
        models.increment_template_use(tid, db_path=db_path)      # no restaurant at all

    models.increment_template_use(tid, mine, db_path=db_path)     # wrong restaurant: no-op
    conn = models.get_conn(db_path)
    assert conn.execute("SELECT use_count FROM response_templates WHERE id=?", (tid,)).fetchone()[0] == 0
    conn.close()

    models.increment_template_use(tid, theirs, db_path=db_path)
    conn = models.get_conn(db_path)
    assert conn.execute("SELECT use_count FROM response_templates WHERE id=?", (tid,)).fetchone()[0] == 1
    conn.close()
