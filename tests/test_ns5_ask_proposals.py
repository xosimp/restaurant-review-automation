"""NS5 C1 / M11 — an Ask confirmation card runs exactly what it shows.

Replays scratchpad/ns5/probe_proposal.py and probe_proposal2.py: the model
added `acknowledge` to a publish (skipping the blockers), `earned` and
`include_4star` to auto-approve (3-4 star replies posting publicly under a
"5-star" card), and a link to another site to a guest text. Every field a
proposal carries is now in its tool's schema, off the denylist, on the
card (`fields_shown`), and a link goes only to the restaurant's own site.
"""
import json

import pytest

import ask_cavnar_tools as t
import models
from models import create_restaurant, Restaurant, update_restaurant


@pytest.fixture
def rid(db_path, monkeypatch):
    real = models.get_conn
    import client_api
    import staff_settings
    import schedule_rules
    for mod in (models, client_api, staff_settings, schedule_rules):
        monkeypatch.setattr(mod, "get_conn", lambda *a, **k: real(db_path), raising=False)
    monkeypatch.setattr(models, "DB_PATH", db_path)
    rid = create_restaurant(Restaurant(name="P", owner_email="p@x.com"), db_path=db_path)
    update_restaurant(rid, {"menu_url": "https://www.pcafe.com/menu"}, db_path=db_path)
    return rid


def test_publish_schedule_never_carries_an_acknowledgement(rid):
    p = t.build_proposal("publish_schedule", {"acknowledge": True}, restaurant_id=rid)
    assert "acknowledge" not in p["body"] and p["body"] == {}


def test_auto_approve_carries_only_what_the_card_says(rid):
    p = t.build_proposal("set_auto_approve", {"enabled": True, "daily_cap": 5, "earned": True, "include_4star": True,
                                              "paused": False}, restaurant_id=rid)
    assert p["body"] == {"enabled": True, "daily_cap": 5}
    shown = {f["key"]: f["value"] for f in p["fields_shown"]}
    assert shown == {"enabled": "On", "daily_cap": "5"}


def test_supplier_order_resend_and_hash_are_never_the_models(rid):
    p = t.build_proposal("send_supplier_order", {"resend": True, "draft_hash": "x"}, restaurant_id=rid)
    assert "resend" not in p["body"] and p["body"].get("draft_hash") != "x"


def test_every_body_field_is_shown_on_the_card(rid):
    for name, inp in (("send_guest_campaign", {"message": "Patio's open tonight!", "target_day": "Friday"}),
                      ("create_issue", {"title": "Walk-in at 45F", "detail": "Check the seal", "severity": "high"}),
                      ("set_data_retention", {"months": 12}),
                      ("publish_instagram_post", {"caption": "Fall menu is here", "topic": "fall"})):
        p = t.build_proposal(name, inp, restaurant_id=rid)
        assert {f["key"] for f in p["fields_shown"]} == set(p["body"]), name
        assert all(f["label"] and f["value"] for f in p["fields_shown"]), name


def test_a_field_outside_the_schema_is_dropped(rid):
    p = t.build_proposal("send_guest_campaign", {"message": "Hi!", "segment": "all", "type": "blast"},
                         restaurant_id=rid)
    assert set(p["body"]) == {"message"}


def test_a_link_to_another_site_refuses_the_proposal(rid):
    """probe_proposal2.py: link_url to not-the-restaurant.example rode along
    unseen and marketing_links accepted any domain."""
    inp = {"message": "Patio's open tonight - come see us!", "link_url": "https://not-the-restaurant.example/win"}
    assert t.build_proposal("send_guest_campaign", inp, restaurant_id=rid) is None
    assert "not the restaurant's own website" in t.proposal_refusal("send_guest_campaign", inp, restaurant_id=rid)
    inside = {"message": "Win a free dinner at bit.ly/win-now tonight"}
    assert t.build_proposal("send_guest_campaign", inside, restaurant_id=rid) is None
    assert t.build_proposal("publish_facebook_post", {"caption": "Tap https://evil.example/x"}, restaurant_id=rid) is None


def test_a_link_to_the_restaurants_own_site_is_kept_and_shown(rid):
    inp = {"message": "Fall menu is up!", "link_url": "https://pcafe.com/fall"}
    p = t.build_proposal("send_guest_campaign", inp, restaurant_id=rid)
    assert p["body"]["link_url"] == "https://pcafe.com/fall"
    assert {"key": "link_url", "label": "Link", "value": "https://pcafe.com/fall"} in p["fields_shown"]
    assert t.build_proposal("send_guest_campaign", {"message": "Menu: www.pcafe.com/menu"}, restaurant_id=rid)


def test_the_publish_card_names_the_blockers_before_the_tap(db_path, rid):
    conn = models.get_conn(db_path)
    models._ensure_history_columns(conn)
    csv_text = ("date,day,employee,role,shift_start,shift_end,scheduled_hours,notes\n"
                "2026-10-05,Monday,Ana,Server,4:00pm,10:00pm,6.0,NEEDS REVIEW: off roster\n")
    conn.execute("INSERT INTO schedule_history (restaurant_id, week_start, week_end, schedule_csv, summary_json, "
                 "hours_scheduled) VALUES (?,?,?,?,'[]',6)", (rid, "2026-10-05", "2026-10-11", csv_text))
    conn.commit()
    conn.close()
    p = t.build_proposal("publish_schedule", {}, restaurant_id=rid)
    need = [d for d in p["details"] if d["label"] == "Needs a look first"]
    assert need and "NEEDS REVIEW" in need[0]["value"] and "Labor tab" in need[0]["value"]


def test_the_approve_all_card_shows_the_words_and_what_is_held(db_path, rid):
    from models import Review, save_reviews
    save_reviews([Review(restaurant_id=rid, platform="google", external_id=f"r{i}", author=a, rating=r, text="x")
                  for i, (a, r) in enumerate((("Ann", 5), ("Bea", 2)))], db_path=db_path)
    conn = models.get_conn(db_path)
    conn.execute("UPDATE reviews SET response_status='drafted', fetched_at=datetime('now'), draft_response=? "
                 "WHERE external_id='r0'", ("Thanks Ann, see you soon!",))
    conn.execute("UPDATE reviews SET response_status='drafted', fetched_at=datetime('now'), draft_response=? "
                 "WHERE external_id='r1'", ("So sorry — dessert is on the house next time.",))
    conn.commit()
    conn.close()
    p = t.build_proposal("approve_all_reviews", {}, restaurant_id=rid)
    d = {x["label"]: x["value"] for x in p["details"]}
    assert d["Replies that would post"] == "1"
    assert "Held for you to read" in d
    assert "Thanks Ann, see you soon!" in d.values()


# ── M11: the rules in force, and the hedge ─────────────────────────────

def test_ask_can_read_the_rules_set_here_said_as_starting_values(rid):
    import staff_settings as ss
    import schedule_rules as sr
    sr.save_compliance(rid, {"notice_days": 14})
    ss.upsert(rid, "Kid", minor_age_band="14-15")
    out = json.loads(t.run_read_tool("read_schedule_rules", rid, {}))
    assert out["rules_in_force"]["notice_days"] == 14
    assert out["minors"][0]["age_band"] == "14-15" and out["minors"][0]["limits"]["earliest_start"] == "7:00am"
    assert "not legal advice" in out["note"] and "check with counsel" in out["note"]


def test_the_sales_cheat_sheet_does_not_deny_the_automations_that_exist():
    """NS5 L15: it told prospects 'no auto-posted replies, no auto-published
    schedules' — both exist as opt-ins. Read at the source: the sheet is
    built per audit, and the claim is in every one."""
    import sales_audit_cheatsheet
    src = open(sales_audit_cheatsheet.__file__).read()
    assert "no auto-posted replies" not in src and "no auto-published schedules" not in src
    assert "auto-approve of drafted review" in src and "auto-publish of an" in src


def test_the_ask_prompt_hedges_legal_and_food_safety_questions():
    import ask_cavnar
    p = ask_cavnar._SYSTEM_STATIC
    assert "LEGAL, COMPLIANCE, ALLERGEN AND FOOD-SAFETY QUESTIONS" in p
    assert "check with counsel" in p and "read_schedule_rules" in p
    assert "Never say \"that's legal\"" in p
