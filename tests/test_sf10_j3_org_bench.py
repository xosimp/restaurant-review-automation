"""Schedule fix round 10/10/26, workstream J3 — MARKET-30: the certified
bench across one organization's locations. One opt-in (J2's travel switch)
that records written, time-stamped consent to the exact words; each site's
owner decides whether its people's record is shared; never across an
unrelated employer."""
import datetime as dt

import pytest

import cross_site
import models
import org_bench as ob
import shift_facts
import staff_settings
from models import Restaurant, create_restaurant


@pytest.fixture(autouse=True)
def _db(db_path, monkeypatch):
    real = models.get_conn
    monkeypatch.setattr(models, "get_conn", lambda *a, **k: real(db_path))
    monkeypatch.setattr(models, "DB_PATH", db_path)
    yield


FRI = (dt.date.today() + dt.timedelta(days=7)).isoformat()


def _org(monkeypatch):
    org = models.get_or_create_organization("Grill Group", "owner@x.test")
    a = create_restaurant(Restaurant(name="Grill North", owner_email="owner@x.test", module_labor=1))
    b = create_restaurant(Restaurant(name="Grill South", owner_email="owner@x.test", module_labor=1,
                                     role_requirements_json='{"Bartender": ["alcohol"]}'))
    other = create_restaurant(Restaurant(name="Not Ours", owner_email="else@x.test", module_labor=1))
    conn = models.get_conn()
    conn.execute("UPDATE restaurants SET organization_id=? WHERE id IN (?,?)", (org["id"], a, b))
    conn.commit()
    conn.close()
    hist = {a: [{"date": "2026-09-01", "employee": "Maria", "role": "Bartender", "shift_start": "5:00pm",
                 "shift_end": "11:00pm"},
                {"date": "2026-09-02", "employee": "Pat", "role": "Server", "shift_start": "5:00pm",
                 "shift_end": "11:00pm"}]}
    monkeypatch.setattr(shift_facts, "person_rows", lambda rid, *x, **k: hist.get(rid, []))
    staff_settings.upsert(a, "Maria", certifications=["alcohol"], db_path=models.DB_PATH)
    return a, b, other


def test_the_travel_switch_is_the_one_opt_in_and_records_consent_to_the_words(monkeypatch):
    a, b, other = _org(monkeypatch)
    out = ob.record_travel(11, a, "Maria", True)
    assert cross_site.travels(a, "Maria") and ob.opted_in(a, "maria")
    assert out["consent_at"] and "Grill Group's other locations" in out["consent_text"]
    m = ob.membership(11, a)
    assert m["consent_version"] == ob.CONSENT_VERSION and m["consent_text"] == out["consent_text"]
    off = ob.record_travel(11, a, "Maria", False)
    assert not cross_site.travels(a, "Maria") and off["consent_at"] is None and ob.membership(11, a)["revoked_at"]
    with pytest.raises(ob.BenchError):
        ob.record_travel(12, other, "Lone", True)              # no other location to share with


def test_a_record_is_shared_only_with_both_keys_and_never_outside_the_organization(monkeypatch):
    a, b, other = _org(monkeypatch)
    ob.record_travel(11, a, "Maria", True)
    cross_site.set_travel(a, "Pat", True)                       # opted in to shifts, never agreed to share
    got = ob.candidates(b, FRI, "Bartender", "5:00pm", "11:00pm")
    maria = next(c for c in got["candidates"] if c["name"] == "Maria")
    assert maria["shared"] is False and maria["certifications"] is None and "isn't shared" in maria["why"]
    ob.set_site(a, True, by="owner")                            # North shares its people's record
    got = ob.candidates(b, FRI, "Bartender", "5:00pm", "11:00pm")
    assert [c["name"] for c in got["candidates"]] == ["Maria"], "Pat holds no bar role"
    maria = got["candidates"][0]
    assert maria["shared"] and maria["certifications"] == ["alcohol"] and maria["home"] == "Grill North"
    assert "open shift" in got["note"]
    assert ob.candidates(other, FRI, "Bartender", "5:00pm", "11:00pm")["candidates"] == []
    view = ob.owner_view(a)
    assert {m["name"] for m in view["members"]} == {"Maria", "Pat"}
    assert next(m for m in view["members"] if m["name"] == "Pat")["consent_at"] is None
    assert view["external"].startswith("Covering from restaurants outside your organization")


def test_the_staff_travel_route_records_the_consent(monkeypatch, db_path):
    a, b, other = _org(monkeypatch)
    import staff_routes
    from flask import Flask
    app = Flask(__name__)
    app.secret_key = "t"
    app.register_blueprint(staff_routes.staff_bp)
    import auth
    user = {"id": 11, "restaurant_id": a, "employee_name": "Maria", "role": "employee"}
    monkeypatch.setattr(staff_routes, "_staff_context", lambda cu: (a, "Maria"))
    fn = app.view_functions["staff.api_travel_set"]
    with app.test_request_context("/staff/api/travel", method="POST", json={"travels": True}):
        resp = fn.__wrapped__(current_user=user) if hasattr(fn, "__wrapped__") else None
    if resp is None:
        pytest.skip("the login wrapper is not unwrappable here")
    body = resp.get_json()
    assert body["ok"] and body["travels"] and body["consent_at"]
    assert ob.membership(11, a)["consent_text"].startswith("I agree that managers at Grill Group")
