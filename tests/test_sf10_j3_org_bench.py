"""Schedule fix round 10/10/26, workstream J3 — MARKET-30: the certified
bench across one organization's locations — two keys (the site's owner and
the person's written consent), never across an unrelated employer."""
import datetime as dt

import pytest

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
    told = []
    import people
    monkeypatch.setattr(people, "tell", lambda *a, **k: told.append(a) or "push")
    monkeypatch.setattr(people, "tell_deciders", lambda *a, **k: told.append(a))
    yield told


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


def test_joining_needs_the_sites_switch_a_sibling_and_written_consent(monkeypatch):
    a, b, other = _org(monkeypatch)
    with pytest.raises(ob.BenchError):
        ob.join(11, a, "Maria", agree=True)                     # the site hasn't turned it on
    ob.set_site(a, True, by="owner")
    with pytest.raises(ob.BenchError):
        ob.join(11, a, "Maria")                                 # no agreement ticked
    m = ob.join(11, a, "Maria", agree=True)
    assert m["consent_at"] and "Grill Group's other locations" in m["consent_text"] and m["consent_version"] == 1
    assert ob.opted_in(a, "maria")
    ob.set_site(other, True)
    with pytest.raises(ob.BenchError):
        ob.join(12, other, "Lone", agree=True)                  # no sibling to share with


def test_a_gap_sees_only_consented_qualified_people_from_enabled_siblings(monkeypatch, _db):
    a, b, other = _org(monkeypatch)
    ob.set_site(a, True)
    ob.join(11, a, "Maria", agree=True)
    ob.join(12, a, "Pat", agree=True)
    got = ob.candidates(b, FRI, "Bartender", "5:00pm", "11:00pm")
    assert got["available"] is False                            # the asking site hasn't turned it on
    ob.set_site(b, True)
    got = ob.candidates(b, FRI, "Bartender", "5:00pm", "11:00pm")
    names = [c["name"] for c in got["candidates"]]
    assert names == ["Maria"], "Pat holds no bar role; the role's certification is Maria's"
    assert got["candidates"][0]["home"] == "Grill North" and "alcohol" in got["candidates"][0]["certifications"]
    # An unrelated restaurant never sees them.
    assert ob.candidates(other, FRI, "Bartender", "5:00pm", "11:00pm")["candidates"] == []
    req = ob.ask(b, got["candidates"][0]["member_id"], FRI, "Bartender", "5:00pm", "11:00pm", by="gm")
    assert req["status"] == "asked" and req["via"] == "push" and _db[-1][0] == a, "told through their home site"
    mine = ob.for_staff(11, a)
    assert mine["on_bench"] and mine["asks"][0]["place"] == "Grill South"
    done = ob.respond(req["id"], 11, a, True)
    assert done["status"] == "accepted" and _db[-1][0] == b
    # Leaving takes them off at once.
    assert ob.leave(11, a)
    assert ob.candidates(b, FRI, "Bartender", "5:00pm", "11:00pm")["candidates"] == []
    assert ob.owner_view(b)["external"].startswith("Covering from restaurants outside your organization")
