"""Schedule fix round 10/10/26, workstream J2: the marketplace reaches further.

MARKET-16  people from the group's other locations who opted in to travel are
           offered a shift here, judged by this site's rules with their own
           facts and their hours at home; a yes writes this site's week.
MARKET-25  an open shift offered by name to its best legal fit first, inside
           guardrails (legal, no overtime while a teammate has room, Shift
           Quality limit), once the managers' own offers earned it; a manager
           withdrawing one lapses it."""
import datetime as dt
import json

import pytest

import cross_site
import models
import people
import request_autonomy
import shift_requests
import schedule_versions as sv
import staff_settings as ss
from tests.test_sf10_j2_floor import db, _restaurant, _publish, _rows, W1  # noqa: F401 — db is the fixture


def _site(db, roster, org=7, **cols):
    rid = _restaurant(db, roster, **cols)
    conn = models.get_conn(db)
    conn.execute("UPDATE restaurants SET organization_id=? WHERE id=?", (org, rid))
    conn.commit()
    conn.close()
    return rid


def _open_extra(db, rid, day=W1[4], start="6:00pm", end="11:00pm", role="Server"):
    out = shift_requests.post_open_shift(rid, day, start, shift_end=end, role=role, actor="gm", broadcast=False,
                                         now=dt.datetime(2026, 10, 1, 9, 0))
    return out["request"]


def _two_sites(db, monkeypatch):
    a = _site(db, [("Ana", "Server"), ("Max", "Manager")])
    ss.upsert(a, "Max", floor_manager=True)
    _publish(db, a, [(W1[4], "Ana", "Server", "5:00pm", "11:00pm", 6),
                     (W1[4], "Max", "Manager", "4:00pm", "11:30pm", 7.5)])
    b = _site(db, [("Zoe", "Server"), ("Yan", "Server"), ("Kim", "Server"), ("Lou", "Cook")])
    models.save_staff_availability(b, "Yan", [], unavailable_days=["Friday"], db_path=db)
    for n in ("Zoe", "Yan", "Lou"):
        cross_site.set_travel(b, n, True)
    monkeypatch.setattr(people, "tell", lambda *a, **k: "push")
    return a, b


def test_only_opted_in_people_who_hold_the_role_are_offered_and_each_is_judged_here(db, monkeypatch):
    a, b = _two_sites(db, monkeypatch)
    req = _open_extra(db, a)
    got = {c["name"]: c for c in cross_site.candidates(a, req["id"])}
    assert set(got) == {"Zoe", "Yan"}                 # Kim never opted in; Lou is a cook
    assert got["Zoe"]["ok"] and got["Zoe"]["home_restaurant_id"] == b
    # Yan can't work Fridays at home — her own availability travels with her.
    assert not got["Yan"]["ok"] and got["Yan"]["why"]


def test_a_yes_from_another_location_is_written_into_this_sites_week(db, monkeypatch):
    a, b = _two_sites(db, monkeypatch)
    req = _open_extra(db, a)
    out = cross_site.offer(a, req["id"], "Zoe", b, actor="gm")
    assert out["offer"]["id"] and out["via"] == "push"
    with pytest.raises(cross_site.CrossSiteError):
        cross_site.offer(a, req["id"], "Kim", b)      # never opted in
    offers = cross_site.offers_for(b, "Zoe")
    assert offers and offers[0]["role"] == "Server"
    got = cross_site.respond(b, "Zoe", offers[0]["id"], True)
    assert got["status"] == "accepted"
    conn = models.get_conn(db)
    hid = conn.execute("SELECT id FROM schedule_history WHERE restaurant_id=? AND published_at IS NOT NULL", (a,)).fetchone()[0]
    status = conn.execute("SELECT status, replacement_name FROM shift_change_requests WHERE id=?", (req["id"],)).fetchone()
    conn.close()
    assert tuple(status) == ("covered", "Zoe")
    assert any(r["employee"] == "Zoe" and r["date"] == W1[4] for r in _rows(db, hid))


def test_hours_at_home_count_here(db, monkeypatch):
    a, b = _two_sites(db, monkeypatch)
    # Zoe already works 38 hours at home that week: five more here would
    # pass the 40-hour ceiling, and this site's engine sees it.
    _publish(db, b, [(W1[i], "Zoe", "Server", "10:00am", "5:36pm", 7.6) for i in range(5)])
    req = _open_extra(db, a, day=W1[5])
    zoe = next(c for c in cross_site.candidates(a, req["id"]) if c["name"] == "Zoe")
    assert not zoe["ok"]


# ── MARKET-25: the best fit first, earned ───────────────────────────────────

def _market(db, monkeypatch):
    # A 50-hour ceiling: past 40 is overtime, legal, and the owner's rule
    # decides it — never while a teammate has room.
    rid = _restaurant(db, [("Ana", "Server"), ("Bo", "Server"), ("Cy", "Server"), ("Max", "Manager")],
                      compliance_json=json.dumps({"weekly_hours_ceiling": 50}))
    ss.upsert(rid, "Max", floor_manager=True)
    # Bo already has 36 hours this week; Cy has 6.
    _publish(db, rid, [(W1[i], "Bo", "Server", "10:00am", "5:12pm", 7.2) for i in range(5)]
             + [(W1[0], "Cy", "Server", "5:00pm", "11:00pm", 6),
                (W1[4], "Ana", "Server", "5:00pm", "11:00pm", 6),
                (W1[4], "Max", "Manager", "4:00pm", "11:30pm", 7.5)])
    monkeypatch.setattr(shift_requests, "_has_login", lambda r, n, db_path=None: n)
    told = []
    monkeypatch.setattr(shift_requests, "_notify_offer", lambda *a, **k: told.append(a[2]["name"]) or "push")
    return rid, told


def test_automatic_offers_are_off_and_then_unearned_until_the_managers_offers_earn_them(db, monkeypatch):
    rid, told = _market(db, monkeypatch)
    req = _open_extra(db, rid, day=W1[4], start="6:00pm", end="11:00pm")
    assert request_autonomy.auto_offer_open(rid, req) is None and told == []
    request_autonomy.save_policy(rid, {"auto_offer": True})
    assert request_autonomy.auto_offer_open(rid, req) is None
    conn = models.get_conn(db)
    why = json.loads(conn.execute("SELECT auto_reasons_json FROM shift_change_requests WHERE id=?",
                                  (req["id"],)).fetchone()[0])
    conn.close()
    assert "not earned yet" in why["refusals"][0]
    assert request_autonomy.status(rid)["trust"]["offer"]["needed"] == request_autonomy.EARN_OFFERS


def test_earned_the_best_legal_fit_is_offered_by_name_never_into_overtime_while_a_teammate_has_room(db, monkeypatch):
    rid, told = _market(db, monkeypatch)
    request_autonomy.save_policy(rid, {"auto_offer": True})
    req = _open_extra(db, rid, day=W1[4], start="6:00pm", end="11:00pm")
    conn = models.get_conn(db)
    for i in range(request_autonomy.EARN_OFFERS):
        conn.execute("INSERT INTO shift_offers (restaurant_id, request_id, name, name_key, status, created_by) "
                     "VALUES (?,?,?,?,?,?)", (rid, req["id"], f"P{i}", f"p{i}", "declined", "gm"))
    conn.commit()
    conn.close()
    ranked = request_autonomy.rank_takers(rid, req)
    bo = next(e for e in ranked if e["name"] == "Bo")
    assert not bo["ok"] and "overtime" in bo["why"]
    told_board = []
    monkeypatch.setattr(shift_requests, "_tell_staff", lambda r, ppl, *a, **k: told_board.extend(ppl) or list(ppl))
    shift_requests._broadcast_open(rid, shift_requests._get_row(rid, req["id"], None), None, "Open shift")
    assert told == ["Cy"]                                     # Bo would go into overtime; Cy has room
    named = told[0]
    assert named not in told_board                            # not told twice
    conn = models.get_conn(db)
    o = conn.execute("SELECT id, created_by FROM shift_offers WHERE request_id=? AND name=?",
                     (req["id"], named)).fetchone()
    conn.close()
    assert o["created_by"] == request_autonomy.AUTO_ACTOR
    # A manager withdrawing it lapses automatic offers at once.
    out = request_autonomy.reverse_offer(rid, o["id"], actor="gm")
    assert out["ok"] and out["undone"]
    t = request_autonomy.offer_trust(rid)
    assert t["lapsed_at"] and "withdrew" in t["lapse_reason"]
