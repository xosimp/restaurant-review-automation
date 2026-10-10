"""Schedule fix round 10/10/26, workstream J3 — MARKET-28: an event that
changes after a week is published becomes the exact priced change to the
live week, for one-tap approval."""
import datetime as dt
import json

import pytest

import event_reactive as er
import models
import shift_requests
from models import Restaurant, create_restaurant


@pytest.fixture(autouse=True)
def _db(db_path, monkeypatch):
    real = models.get_conn
    monkeypatch.setattr(models, "get_conn", lambda *a, **k: real(db_path))
    monkeypatch.setattr(models, "DB_PATH", db_path)
    import people
    told = []
    monkeypatch.setattr(people, "tell_deciders", lambda *a, **k: told.append(a))
    yield told


TODAY = dt.date(2026, 10, 10)
FRI = "2026-10-16"
HEAD = "date,day,employee,role,shift_start,shift_end,scheduled_hours,notes\n"


def _setup():
    rid = create_restaurant(Restaurant(name="Events Co", owner_email="e@x.test", module_labor=1, hourly_rate=15.0))
    for n in ("Ana", "Bob", "Cy", "Dee"):
        models.add_manual_team_member(rid, n, role="Server")
    rows = [("Ana", "4:00pm", "11:00pm"), ("Bob", "5:00pm", "10:00pm"), ("Cy", "6:00pm", "9:00pm")]
    csv = HEAD + "".join(f"{FRI},Friday,{n},Server,{s},{e},5,\n" for n, s, e in rows)
    conn = models.get_conn()
    hid = conn.execute("INSERT INTO schedule_history (restaurant_id, week_start, week_end, schedule_csv, published_at) "
                       "VALUES (?,?,?,?,datetime('now'))", (rid, "2026-10-12", "2026-10-18", csv)).lastrowid
    sid = conn.execute("INSERT INTO event_series (slug, name, short_name, category) VALUES (?,?,?,?)",
                       ("test-sox", "Test Sox", "Sox", "sports")).lastrowid
    conn.execute("INSERT INTO event_follows (restaurant_id, series_id, active, source) VALUES (?,?,1,'owner')", (rid, sid))
    conn.commit()
    conn.close()
    return rid, hid, sid


def _add_game(sid, kickoff="19:10", status="scheduled"):
    conn = models.get_conn()
    conn.execute("INSERT INTO catalog_events (series_id, external_id, event_date, kickoff_local, opponent, status) "
                 "VALUES (?,?,?,?,?,?) ON CONFLICT(series_id, external_id) DO UPDATE SET kickoff_local=excluded."
                 "kickoff_local, status=excluded.status", (sid, "g1", FRI, kickoff, "Twins", status))
    conn.commit()
    conn.close()


def test_a_game_added_after_publish_proposes_a_priced_shift_offered_to_somebody_legal(monkeypatch, _db):
    rid, hid, sid = _setup()
    pct = {"v": 0.0}
    monkeypatch.setattr(er, "_effect_pct", lambda *a, **k: pct["v"])
    first = er.scan(rid, today=TODAY)
    assert first["baselined"] >= 1 and first["proposals"] == 0
    _add_game(sid)
    pct["v"] = 34.0                        # this restaurant's own measured Sox-night lift
    got = er.scan(rid, today=TODAY)
    assert got["proposals"] == 1 and _db, "the deciders are told"
    p = er.proposals(rid)[0]
    assert p["status"] == "open" and p["change"] == "added"
    assert "Sox vs Twins was added (19:10)" in p["summary"] and "+34%" in p["summary"]
    add = p["actions"][0]
    assert add["kind"] == "add" and add["role"] == "Server" and add["offer_to"] == "Dee"
    assert add["shift_start"] == "4:00pm" and add["cost"] == pytest.approx(7 * 15.0)
    assert p["cost"]["added_labor"] == pytest.approx(105.0)
    # Seen once: the next pass does not propose it again.
    assert er.scan(rid, today=TODAY)["proposals"] == 0
    posted = []
    monkeypatch.setattr(shift_requests, "post_open_shift",
                        lambda rid_, day, start, end=None, **kw: posted.append((day, start, end, kw)) or
                        {"request": {"id": 9}, "offer": {"id": 3}})
    done = er.approve(rid, p["id"], by="erik")
    assert done["status"] == "applied" and done["result"][0]["ok"] and done["result"][0]["offered_to"] == "Dee"
    assert posted[0][:3] == (FRI, "4:00pm", "11:00pm") and posted[0][3]["offer_to"] == "Dee"
    with pytest.raises(er.ProposalError):
        er.approve(rid, p["id"])


def test_an_unmeasured_effect_moves_nobody_and_says_so(monkeypatch):
    rid, hid, sid = _setup()
    monkeypatch.setattr(er, "_effect_pct", lambda *a, **k: 0.0)
    er.scan(rid, today=TODAY)
    _add_game(sid)
    assert er.scan(rid, today=TODAY)["proposals"] == 1
    p = er.proposals(rid)[0]
    assert p["status"] == "noted" and p["actions"] == [] and "nothing is guessed" in p["summary"]


def test_a_cancelled_game_takes_off_the_tentative_shift_first_and_never_unseen(monkeypatch):
    rid, hid, sid = _setup()
    _add_game(sid)
    pct = {"v": 40.0}
    monkeypatch.setattr(er, "_effect_pct", lambda *a, **k: pct["v"])
    er.scan(rid, today=TODAY)
    conn = models.get_conn()
    conn.execute("INSERT INTO shift_commitments (restaurant_id, date, name_key, shift_start, employee_name, level, "
                 "first_level) VALUES (?,?,?,?,?,?,?)", (rid, FRI, "bob", "5:00pm", "Bob", "tentative", "tentative"))
    conn.commit()
    conn.close()
    _add_game(sid, status="cancelled")
    pct["v"] = 0.0
    er.scan(rid, today=TODAY)
    p = er.proposals(rid)[0]
    assert p["change"] == "cancelled"
    cut = p["actions"][0]
    assert cut["kind"] == "cut" and cut["employee"] == "Bob" and cut["level"] == "tentative"
    done = er.approve(rid, p["id"])
    assert done["result"][0]["studio"] is True
    assert er.dismiss(rid, p["id"]) is False             # already decided
