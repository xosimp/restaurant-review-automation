"""Schedule audit 10/3/26, workstream E — labor standards and reservations.

D-25  per-role labor standards (guests per server-hour, bar tickets per
      bartender-hour, tickets per cook-hour) measured from the punches and
      the ticket archive, with the owner's own figure over them; an owner's
      standard resizes that role's requirement, with its reason.
D-31  a reservation system's booking export imports without any vendor
      partnership; each live provider says precisely what it still needs.
"""
import json
from datetime import date, timedelta

import pytest
from flask import Flask

import labor_standards as ls
import models
import reservation_feeds
import schedule_requirements as req

OWNER = {"role": "owner", "id": 1, "username": "erik"}


@pytest.fixture(autouse=True)
def _redirect(db_path, monkeypatch):
    real = models.get_conn
    monkeypatch.setattr(models, "get_conn", lambda *a, **k: real(db_path))
    monkeypatch.setattr(models, "DB_PATH", db_path)
    yield


def _exec(sql, args=()):
    c = models.get_conn()
    try:
        c.execute(sql, args)
        c.commit()
    finally:
        c.close()


def _rid(name="Standard Co"):
    return models.create_restaurant(models.Restaurant(name=name, owner_email=f"{name.split()[0].lower()}@x.test",
                                                      timezone="America/Chicago"))


TODAY = date(2026, 10, 3)
FRIDAYS = [TODAY - timedelta(days=1 + 7 * k) for k in range(7)]


def _world(rid, guests=40, tickets_per_night=10, bar_tickets=6):
    """Seven Friday dinners: 10 tables of 4 (40 guests) and 6 bar tickets a
    night; two servers on 6h each, one bartender on 6h."""
    shifts = []
    for k, f in enumerate(FRIDAYS):
        bd = f.isoformat()
        for t in range(tickets_per_night):
            _exec("INSERT INTO pos_tickets (restaurant_id, provider, ticket_id, business_date, opened_at, guest_count, "
                  "is_bar, net_sales) VALUES (?,?,?,?,?,?,?,?)",
                  (rid, "rpower", f"{k}-{t}", bd, f"{bd}T19:00:00", guests // tickets_per_night, 0, 100.0))
        for t in range(bar_tickets):
            _exec("INSERT INTO pos_tickets (restaurant_id, provider, ticket_id, business_date, opened_at, guest_count, "
                  "is_bar, net_sales) VALUES (?,?,?,?,?,?,?,?)",
                  (rid, "rpower", f"{k}-b{t}", bd, f"{bd}T21:00:00", 1, 1, 30.0))
        for name in ("Ana", "Bo"):
            shifts.append({"date": bd, "employee": name, "role": "Server", "shift_start": "17:00", "shift_end": "23:00",
                           "actual_hours": "6"})
        shifts.append({"date": bd, "employee": "Cy", "role": "Bartender", "shift_start": "17:00", "shift_end": "23:00",
                       "actual_hours": "6"})
    return shifts


# ── D-25: measured standards ─────────────────────────────────────────────

def test_standards_are_measured_from_the_punches_and_the_archive(db_path):
    rid = _rid()
    shifts = _world(rid)
    m = ls.measure(rid, shifts=shifts, today=TODAY, db_path=db_path)
    # 40 guests a night over 12 server-hours; 6 bar tickets over 6 bartender-hours.
    assert m["server"]["night"] == {"per_hour": round(40 / 12, 1), "unit": "guests", "days": 7, "hours": 84.0}
    assert m["bartender"]["night"]["per_hour"] == 1.0 and m["bartender"]["night"]["unit"] == "bar tickets"
    slot = m["_slots"][("Friday", "night")]
    assert slot["work"]["guests"] == 40 and slot["hours_per_person"]["server"] == 6.0      # the bar's guests are the bar's
    assert slot["work"]["bar tickets"] == 6 and slot["work"]["tickets"] == 16
    # Under the sample floor, no standard.
    few = ls.measure(rid, shifts=shifts[:9], today=TODAY, db_path=db_path)
    assert "server" not in few


def test_the_owners_standard_stands_over_the_measured_one_and_says_which(db_path):
    rid = _rid()
    shifts = _world(rid)
    models.update_restaurant(rid, {"labor_standards_json": json.dumps({"server": {"night": 5}})}, db_path=db_path)
    std = ls.standards(rid, shifts=shifts, db_path=db_path, today=TODAY)
    srv = std["families"]["server"]["night"]
    assert srv["per_hour"] == 5 and srv["source"] == "yours" and srv["measured"] == round(40 / 12, 1)
    assert srv["text"] == "5 guests per server-hour at dinner (your standard; measured here 3.3)"
    bar = std["families"]["bartender"]["night"]
    assert bar["source"] == "measured" and "measured over 7 shifts here" in bar["text"]


def test_an_owners_standard_resizes_the_role_and_says_why(db_path):
    rid = _rid()
    shifts = _world(rid)
    models.update_restaurant(rid, {"labor_standards_json": json.dumps({"server": {"all": 2.5}})}, db_path=db_path)
    std = ls.standards(rid, shifts=shifts, db_path=db_path, today=TODAY)
    typical = {("Friday", "night"): {"Server": 2, "Bartender": 1}}
    fri = "2026-10-09"
    needs = ls.needs_for_week([fri], typical, std)
    # 40 guests ÷ (2.5 an hour × 6h shifts) = 2.67 → 3 servers.
    assert needs[(fri, "night")]["server"]["people"] == 3
    assert "your standard of 2.5 guests per server-hour" in needs[(fri, "night")]["server"]["reason"]
    rows = req.shift_requirements([fri], typical_headcount=typical, standard_needs=needs)
    srv = next(x for x in next(r for r in rows if r["daypart"] == "night")["roles"] if x["role"] == "Server")
    assert srv["required"] == 3 and "your standard of 2.5" in srv["reason"]
    # The date's demand moves the work: +30% → 52 guests → 4.
    busy = ls.needs_for_week([fri], typical, std, {fri: {"ratio": 1.3}})
    assert busy[(fri, "night")]["server"]["people"] == 4
    # A measured standard alone is reported, never applied.
    models.update_restaurant(rid, {"labor_standards_json": None}, db_path=db_path)
    assert ls.needs_for_week([fri], typical, ls.standards(rid, shifts=shifts, db_path=db_path, today=TODAY)) == {}


def test_a_live_rescore_reads_no_archive_without_an_owners_standard(db_path, monkeypatch):
    rid = _rid()
    monkeypatch.setattr(ls, "measure", lambda *a, **k: pytest.fail("the archive was read"))
    assert ls.for_requirements(rid, ["2026-10-09"], {}, needs_only=True) == {"standards": {}, "needs": {}}


def test_a_bad_standard_is_refused_with_its_reason():
    assert ls.clean_overrides({"Server": {"dinner": "12", "lunch": ""}}) == {"server": {"night": 12.0}}
    with pytest.raises(ValueError, match="not a role whose work"):
        ls.clean_overrides({"dish": {"all": 3}})
    with pytest.raises(ValueError, match="outside"):
        ls.clean_overrides({"server": {"all": 9000}})
    with pytest.raises(ValueError, match="not lunch, dinner or all"):
        ls.clean_overrides({"server": {"brunch": 3}})


def _call(fn, user, path="/labor/labor-standards", method="POST", body=None):
    app = Flask(__name__)
    with app.test_request_context(path, method=method, json=body):
        return fn(user)


def test_the_standards_route_saves_one_family_without_touching_another(db_path):
    import strategy_routes as sr
    rid = _rid()
    owner = dict(OWNER, restaurant_id=rid)
    body, code = _call(sr._do_labor_standards_set, owner, body={"family": "server", "dinner": 12})
    assert code == 200 and body["standards"]["server"]["night"]["per_hour"] == 12
    body, code = _call(sr._do_labor_standards_set, owner, body={"family": "line_cook", "all": 20})
    stored = json.loads(models.get_restaurant(rid).labor_standards_json)
    assert stored == {"server": {"night": 12.0}, "line_cook": {"all": 20.0}}
    body, code = _call(sr._do_labor_standards_set, owner, body={"family": "server", "remove": True})
    assert json.loads(models.get_restaurant(rid).labor_standards_json) == {"line_cook": {"all": 20.0}}
    body, code = _call(sr._do_labor_standards_set, owner, body={"family": "server", "dinner": "lots"})
    assert code == 400 and "not a number" in body["error"]
    body, code = _call(sr._do_labor_standards_get, owner, method="GET")
    assert code == 200 and body["can_edit"] and "server" in body["families"]
    # Both twins come from the one route table.
    assert ("/labor/labor-standards", ["POST"], sr._do_labor_standards_set, "labor_standards_set") in sr._ROUTES


# ── D-31: a booking export imports as it is ──────────────────────────────

EXPORT = """Visit Date,Visit Time,Guest Name,Party Size,Status,Notes
10/09/2026,7:30 PM,Lee,4,Confirmed,
10/09/2026,8:00 PM,Kim,6,Seated,
10/09/2026,10:30 PM,Ray,2,Confirmed,late
10/09/2026,7:00 PM,Abe,8,Cancelled,
10/10/2026,6:00 PM,Moe,5,No Show,
10/10/2026,6:30 PM,Pat,3,Confirmed,
not a date,6:30 PM,X,3,Confirmed,
"""


def test_a_booking_export_is_summed_per_date_without_the_cancelled():
    import demand_signals as ds
    rows, summary = ds.parse_reservation_export(EXPORT)
    assert rows == [{"date": "2026-10-09", "kind": "reservations", "label": "Reservations", "covers": 12},
                    {"date": "2026-10-10", "kind": "reservations", "label": "Reservations", "covers": 3}]
    assert summary["bookings"] == 4 and summary["skipped_status"] == 2 and summary["skipped_unreadable"] == 1
    assert summary["late_covers"] == {"2026-10-09": 2}
    # The two-column paste still reads as before.
    assert ds.parse_reservations_csv("date,covers\n2026-10-09,40\n") == \
        [{"date": "2026-10-09", "kind": "reservations", "covers": "40"}]
    assert ds.parse_reservation_export("date,covers\n2026-10-09,40\n") == (None, None)


def test_other_systems_exports_read_by_their_header_words():
    import demand_signals as ds
    resy = "Date\tTime\tParty Size\tStatus\n2026-10-09\t19:00\t2\tBooked\n2026-10-09\t20:00\t4\tCanceled\n"
    rows, s = ds.parse_reservation_export(resy)
    assert rows[0]["covers"] == 2 and s["skipped_status"] == 1
    tock = "Booking Date,Booking Time,Guests,State\n\"Oct 9, 2026\",6:00 PM,6,Confirmed\n"
    rows, s = ds.parse_reservation_export(tock)
    assert rows == [{"date": "2026-10-09", "kind": "reservations", "label": "Reservations", "covers": 6}]


def test_the_import_route_writes_the_dates_the_schedule_reads(db_path):
    import strategy_routes as sr
    import demand_signals as ds
    rid = _rid("Booked Co")
    owner = dict(OWNER, restaurant_id=rid)
    body, code = _call(sr._do_demand_signals_save, owner, path="/labor/demand-signals", body={"csv": EXPORT})
    assert code == 200 and body["written"] == 2 and body["report"]["bookings"] == 4
    got = {s["date"]: s for s in ds.upcoming(rid, "2026-10-01", "2026-10-31")}
    assert got["2026-10-09"]["covers"] == 12 and got["2026-10-09"]["source"] == "report"


def test_each_live_provider_says_precisely_what_it_still_needs():
    for code in ("tock", "opentable", "resy"):
        r = models.Restaurant(name="R", owner_email="r@x.test")
        r.reservation_provider = code
        st = reservation_feeds.status(r)
        assert st["live"] is False
        assert "partner" in st["message"] and "authorized" in st["message"]
        assert "import the file under Events & reservations" in st["message"]
    none = reservation_feeds.status(models.Restaurant(name="N", owner_email="n@x.test"))
    assert none["import"] is True and "booking export" in none["message"]
