"""Halsted & Rye, the showcase demo (demo_store, 10/16/26): a fictional RPOWER
store that answers RPOWER's own API, and an account that can never send,
push to a POS or ask Google about itself."""
from datetime import date, datetime, timedelta

import pytest

import demo_store
from demo_store import api, calendar as cal, day as D, purchasing as P, world as w
from models import Restaurant, create_restaurant, update_restaurant


@pytest.fixture
def showcase():
    rid = create_restaurant(Restaurant(name="Halsted & Rye", owner_email="maya@halstedandrye.example", is_demo=1))
    update_restaurant(rid, {"rpower_token": demo_store.TOKEN, "rpower_cg": w.CG, "rpower_store_mid": w.STORE_MID})
    return rid


def test_a_past_day_is_the_same_every_time_and_rings_like_rpower():
    day = date(2026, 9, 18)
    a, b = D.business_day(day), D.business_day(day)
    assert a is b or a["lines"] == b["lines"]
    assert a["tickets"] and a["lines"] and a["punches"] and a["payments"]
    kinds = {l["slstype_mid"] for l in a["lines"]}
    assert w.ST["Sale"] in kinds and w.ST["Non Sale"] in kinds
    net = sum(t["type_sale"] + t["type_discnt"] for t in a["tickets"])
    assert 0.9 * cal.demand(day)["target"] <= net <= 1.15 * cal.demand(day)["target"]
    # RPOWER's "no value" date, never a None.
    assert all(t["need_dttm"] for t in a["tickets"])


def test_the_api_pages_a_thousand_and_today_only_shows_what_has_happened(monkeypatch):
    monkeypatch.setattr(api, "FIXED_NOW", datetime(2026, 10, 10, 19, 30))
    rows = api.respond(demo_store.TOKEN, "ticketsales/getbybusinessdate",
                       {"startdate": "2026-10-10", "enddate": "2026-10-10", "pagenumber": 1})
    assert 0 < len(rows) <= api.PAGE_SIZE
    assert all(r["item_dttm"] <= "2026-10-10T19:30:00" for r in rows)
    assert not any(k.startswith("_") for k in rows[0])
    punches = api.respond(demo_store.TOKEN, "timeclock/getbydaterange",
                          {"startdate": "2026-10-10", "enddate": "2026-10-11"})
    assert any(p["out_dttm"] == D.NEVER for p in punches)          # still on the clock
    assert api.respond(demo_store.TOKEN, "ticket/getbybusinessdate",
                       {"startdate": "2026-10-11", "enddate": "2026-10-11"}) == []
    assert api.respond(demo_store.TOKEN, "closeday/getbybusinessdate",
                       {"startdate": "2026-10-09", "enddate": "2026-10-10"})[0]["from_date"].startswith("2026-10-09")


def test_a_demo_token_never_reaches_rpowerpos(monkeypatch):
    import requests
    import rpower

    def boom(*a, **k):
        raise AssertionError("a demo-store token went to the network")
    monkeypatch.setattr(requests, "get", boom)
    stores = rpower.list_stores(demo_store.TOKEN)
    assert stores and stores[0]["name"] == w.STORE_NAME


def test_a_schedule_is_never_pushed_from_the_demo(showcase, monkeypatch):
    import requests
    import rpower
    monkeypatch.setattr(requests, "post", lambda *a, **k: (_ for _ in ()).throw(AssertionError("pushed")))
    out = rpower.push_labor_schedule(showcase, [{"employee": "Kayla Brennan", "role": "Server",
                                                 "start": "2026-10-20T16:00:00", "end": "2026-10-20T22:00:00"}])
    assert out["ok"] is False and "demo store" in out["error"]


def test_the_demo_never_emails_texts_or_pushes(showcase, monkeypatch):
    import emails
    import notify
    import push
    import requests
    monkeypatch.setattr(requests, "post", lambda *a, **k: (_ for _ in ()).throw(AssertionError("sent")))
    monkeypatch.setattr(emails, "_resend_key", lambda: "re_test")
    res = emails.deliver({"to": "maya@halstedandrye.example", "subject": "x", "html": "x"}, restaurant_id=showcase,
                         email_type="digest")
    assert not res.ok and res.reason == "demo_store" and res.refused
    sms = notify.send_sms_result("+13125550101", "hi", restaurant_id=showcase)
    assert not sms.ok and sms.permanent
    got = push._deliver({"restaurant_id": showcase, "apns_token": "x"}, "health", "t", "b", {})
    assert got["ok"] is False


def test_google_is_never_asked_about_the_demo(showcase):
    import ai_utils
    with pytest.raises(ai_utils.PlacesUnavailable):
        ai_utils.places_request("details", {"key": "k", "place_id": "x"}, restaurant_id=showcase)


def test_the_showcase_learns_for_itself_and_an_ordinary_demo_does_not(showcase):
    import models
    plain = create_restaurant(Restaurant(name="Some Demo", owner_email="d@x.test", is_demo=1))
    assert models.learns_for_itself(models.get_restaurant(showcase))
    assert not models.learns_for_itself(models.get_restaurant(plain))
    assert demo_store.is_simulated(showcase) and not demo_store.is_simulated(plain)


def test_an_invoice_is_the_same_whenever_it_is_written():
    a = {i["number"]: i["lines"] for i in P.bar_invoices(date(2026, 9, 1), date(2026, 9, 21))}
    b = P.bar_invoices(date(2026, 9, 14), date(2026, 9, 21))
    assert b and all(a[i["number"]] == i["lines"] for i in b)
    f1 = {i["number"]: i["lines"] for i in P.food_invoices(date(2026, 9, 1), date(2026, 9, 21))}
    assert all(f1[i["number"]] == i["lines"] for i in P.food_invoices(date(2026, 9, 14), date(2026, 9, 21)))


def test_people_and_reviews_are_fictional():
    # Reserved: .example addresses, 555-01xx numbers; nobody shares a name with a station login.
    names = [e["name"] for e in w.employees() if not e["station"]]
    assert len(names) == len(set(names)) >= 45
    from demo_store import reviews as R
    rows = R.generate(date(2026, 9, 1), date(2026, 9, 30))
    assert rows and 4.2 <= sum(r["rating"] for r in rows) / len(rows) <= 4.8
    assert all(r["external_id"].startswith("hr-g-") for r in rows)
