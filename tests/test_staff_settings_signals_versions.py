"""staff_settings, demand_signals and schedule_versions.

The roster was whoever appeared in the shift history; the schedule knew
nothing about next Saturday's party; a manager's edit overwrote the draft
and nothing learned from it. These pin the three modules that fix that.
"""
import datetime as dt

import pytest

import demand_signals as ds
import models
import schedule_versions as sv
import staff_settings as ss
from models import create_restaurant, Restaurant, get_conn

HEADER = "date,day,employee,role,shift_start,shift_end,scheduled_hours,notes\n"


@pytest.fixture
def rid(db_path, monkeypatch):
    real = models.get_conn
    for mod in (models, ss, ds, sv):
        monkeypatch.setattr(mod, "get_conn", lambda *a, **k: real(db_path), raising=False)
    return create_restaurant(Restaurant(name="Roster Co", owner_email="r@x.com"), db_path=db_path)


# ── roster ─────────────────────────────────────────────────────────────

def test_roster_is_history_plus_hand_added_minus_deactivated(db_path, rid, monkeypatch):
    monkeypatch.setattr(models, "_cached_shifts", lambda r: [
        {"employee": "Ana", "role": "Server", "date": "2026-09-01"},
        {"employee": "Ana", "role": "Bartender", "date": "2026-09-10"},
        {"employee": "Left", "role": "Server", "date": "2026-03-01"},
    ])
    models.add_manual_team_member(rid, "New Hire", role="Host", db_path=db_path)
    ss.upsert(rid, "Left", active=False, db_path=db_path)
    names = [e["name"] for e in ss.roster(rid, db_path=db_path)]
    assert names == ["Ana", "New Hire"]
    ana = next(e for e in ss.roster(rid, db_path=db_path) if e["name"] == "Ana")
    assert ana["role"] == "Bartender" and ana["shifts"] == 2 and not ana["is_manual"]
    everyone = ss.roster(rid, db_path=db_path, include_inactive=True)
    assert [e["name"] for e in everyone] == ["Ana", "New Hire", "Left"]
    assert everyone[-1]["active"] is False
    assert ss.active_names(rid, db_path=db_path) == ["Ana", "New Hire"]


def test_upsert_keeps_what_the_caller_did_not_mention(db_path, rid):
    ss.upsert(rid, "Ana", employment_type="part", max_hours=25, db_path=db_path)
    ss.upsert(rid, "Ana", is_minor=True, db_path=db_path)
    row = ss.get_all(rid, db_path=db_path)["Ana"]
    assert row["employment_type"] == "part" and row["max_hours"] == 25 and row["is_minor"] and row["active"]


def test_upsert_rejects_nonsense(db_path, rid):
    with pytest.raises(ss.StaffSettingsError):
        ss.upsert(rid, "", active=True, db_path=db_path)
    with pytest.raises(ss.StaffSettingsError):
        ss.upsert(rid, "Ana", employment_type="casual", db_path=db_path)
    with pytest.raises(ss.StaffSettingsError):
        ss.upsert(rid, "Ana", max_hours="lots", db_path=db_path)
    with pytest.raises(ss.StaffSettingsError):
        ss.upsert(rid, "Ana", min_hours=30, max_hours=20, db_path=db_path)
    row = ss.upsert(rid, "Ana", daypart_availability={"Monday": "morning", "Funday": "any", "Tuesday": "sometimes"}, db_path=db_path)
    assert row["daypart_availability"] == {"Monday": "morning"}


def test_pairs_are_unordered_and_kinds_are_checked(db_path, rid):
    p = ss.set_pair(rid, "Ana", "Bob", "avoid", note="argue", db_path=db_path)
    assert p["kind"] == "avoid"
    with pytest.raises(ss.StaffSettingsError):
        ss.set_pair(rid, "Ana", "Ana", "prefer", db_path=db_path)
    with pytest.raises(ss.StaffSettingsError):
        ss.set_pair(rid, "Ana", "Cy", "hate", db_path=db_path)
    ss.set_pair(rid, "Cy", "Ana", "prefer", db_path=db_path)
    sets = ss.pair_sets(rid, db_path=db_path)
    assert frozenset({"ana", "bob"}) in sets["avoid"]
    assert frozenset({"ana", "cy"}) in sets["prefer"]
    assert ss.delete_pair(rid, p["id"], db_path=db_path)
    assert not ss.delete_pair(rid, p["id"], db_path=db_path)
    assert len(ss.pairs(rid, db_path=db_path)) == 1


def test_reliability_needs_clock_data_and_a_sample(db_path, rid, monkeypatch):
    rows = []
    for i in range(8):
        rows.append({"employee": "Flaky", "scheduled_hours": 8, "actual_hours": 0 if i < 2 else 8})
        rows.append({"employee": "Solid", "scheduled_hours": 8, "actual_hours": 8})
        rows.append({"employee": "NoClock", "scheduled_hours": 8, "actual_hours": ""})
    rows.append({"employee": "Newbie", "scheduled_hours": 8, "actual_hours": 0})
    monkeypatch.setattr(models, "_cached_shifts", lambda r: rows)
    rel = ss.reliability(rid, db_path=db_path)
    assert rel["Flaky"]["no_show_rate"] == 0.25 and rel["Flaky"]["shifts"] == 8
    assert rel["Solid"]["no_show_rate"] == 0.0
    assert "NoClock" not in rel and "Newbie" not in rel


# ── demand signals ─────────────────────────────────────────────────────

def test_signals_are_validated_and_upserted(db_path, rid):
    res = ds.save(rid, [
        {"date": "2026-10-10", "kind": "event", "label": "Wedding", "covers": 80},
        {"date": "2026-10-10", "kind": "reservations", "covers": "45"},
        {"date": "2026-10-11", "kind": "event", "label": "Trivia"},
        {"date": "not-a-date", "kind": "event", "label": "x"},
        {"date": "2026-10-12", "kind": "party", "label": "x"},
        {"date": "2026-10-12", "kind": "event", "label": "", "covers": 5},
        {"date": "2026-10-12", "kind": "reservations", "covers": "many"},
    ], db_path=db_path)
    assert res["written"] == 3 and res["skipped"] == 4 and len(res["errors"]) == 4
    ds.save(rid, [{"date": "2026-10-10", "kind": "event", "label": "Wedding", "covers": 90}], db_path=db_path)
    up = ds.upcoming(rid, "2026-10-01", "2026-10-31", db_path=db_path)
    assert len(up) == 3
    wedding = next(s for s in up if s["label"] == "Wedding")
    assert wedding["covers"] == 90
    trivia = next(s for s in up if s["label"] == "Trivia")
    assert trivia["lift_pct"] == 25          # an event with no figure is a busy night


def test_by_date_turns_covers_into_a_lift_against_the_typical_day(db_path, rid, monkeypatch):
    ds.save(rid, [{"date": "2026-10-10", "kind": "reservations", "covers": 150},
                  {"date": "2026-10-11", "kind": "event", "label": "Slow", "lift_pct": -30}], db_path=db_path)
    monkeypatch.setattr(ds, "typical_covers", lambda r, db_path=None: {"Saturday": 100})
    by = ds.by_date(rid, ["2026-10-10", "2026-10-11", "2026-10-12"], db_path=db_path)
    assert by["2026-10-10"]["lift_pct"] == 50 and by["2026-10-10"]["covers"] == 150
    assert by["2026-10-11"]["lift_pct"] == -30
    assert "2026-10-12" not in by
    block = ds.prompt_block(by, ["2026-10-10", "2026-10-11"])
    assert "50% more than a typical Saturday" in block and "30% less" in block
    assert ds.prompt_block({}, ["2026-10-10"]) == ""


def test_reservation_csv_is_parsed_and_the_owner_can_delete(db_path, rid):
    rows = ds.parse_reservations_csv("date,covers\n2026-10-10,40\n2026-10-11\t55\n\nbad line")
    assert [(r["date"], r["covers"]) for r in rows] == [("2026-10-10", "40"), ("2026-10-11", "55")]
    ds.save(rid, rows, source="csv", db_path=db_path)
    up = ds.upcoming(rid, "2026-10-01", "2026-10-31", db_path=db_path)
    assert {s["source"] for s in up} == {"csv"}
    assert ds.delete(rid, up[0]["id"], db_path=db_path)
    other = create_restaurant(Restaurant(name="Other", owner_email="o@x.com"), db_path=db_path)
    assert not ds.delete(other, up[1]["id"], db_path=db_path)     # not theirs


# ── versions and diffs ─────────────────────────────────────────────────

def _rows(*specs):
    out = []
    for date, day, emp, role, start, end, hrs in specs:
        out.append({"date": date, "day": day, "employee": emp, "role": role, "shift_start": start,
                    "shift_end": end, "scheduled_hours": str(hrs), "notes": ""})
    return out


def test_diff_names_moves_retimes_adds_and_removes():
    before = _rows(("2026-10-05", "Monday", "Ana", "Server", "4:00pm", "10:00pm", 6),
                   ("2026-10-06", "Tuesday", "Bob", "Server", "4:00pm", "10:00pm", 6),
                   ("2026-10-07", "Wednesday", "Cy", "Cook", "8:00am", "4:00pm", 8))
    after = _rows(("2026-10-05", "Monday", "Dee", "Server", "4:00pm", "10:00pm", 6),      # moved
                  ("2026-10-06", "Tuesday", "Bob", "Server", "5:00pm", "11:00pm", 6),      # retimed
                  ("2026-10-08", "Thursday", "Cy", "Cook", "8:00am", "4:00pm", 8))         # removed Wed, added Thu
    d = sv.diff(before, after)
    assert d["moved"] == [{"date": "2026-10-05", "day": "Monday", "role": "Server", "shift_start": "4:00pm", "from": "Ana", "to": "Dee"}]
    assert d["retimed"][0]["employee"] == "Bob" and d["retimed"][0]["to"] == "5:00pm–11:00pm"
    assert [r["date"] for r in d["added"]] == ["2026-10-08"] and [r["date"] for r in d["removed"]] == ["2026-10-07"]
    assert d["changes"] == 4 and d["hours_before"] == d["hours_after"] == 20.0
    lines = sv.diff_lines(d)
    assert any("Ana → Dee" in l for l in lines)
    assert sv.diff_lines(sv.diff(before, before)) == ["Unchanged from the last published week."]


def test_diff_lines_say_the_hours_change_first():
    before = _rows(("2026-10-05", "Monday", "Ana", "Server", "4:00pm", "10:00pm", 6))
    after = before + _rows(("2026-10-06", "Tuesday", "Ana", "Server", "4:00pm", "10:00pm", 6),
                           ("2026-10-06", "Tuesday", "Bob", "Server", "4:00pm", "10:00pm", 6))
    lines = sv.diff_lines(sv.diff(before, after))
    assert lines[0] == "+12h this week (6h → 18h)."
    assert "Tuesday: 0h → 12h." in lines


def test_versions_append_with_a_diff_against_the_last(db_path, rid):
    conn = get_conn(db_path)
    cur = conn.execute("INSERT INTO schedule_history (restaurant_id, week_start, week_end, hours_scheduled, hours_budget, "
                       "labor_target, schedule_csv, summary_json) VALUES (?,?,?,?,?,?,?,'[]')",
                       (rid, "2026-10-05", "2026-10-11", 6, 40, 30, HEADER + "2026-10-05,Monday,Ana,Server,4:00pm,10:00pm,6.0,\n"))
    conn.commit()
    hid = cur.lastrowid
    conn.close()
    v1 = HEADER + "2026-10-05,Monday,Ana,Server,4:00pm,10:00pm,6.0,\n"
    v2 = HEADER + "2026-10-05,Monday,Bob,Server,4:00pm,10:00pm,6.0,\n"
    sv.append(rid, hid, "generated", v1, quality={"score": 70}, saved_by="Cavnar AI", db_path=db_path)
    sv.append(rid, hid, "edited", v2, saved_by="will", db_path=db_path)
    versions = sv.list_versions(rid, hid, db_path=db_path)
    assert [v["version"] for v in versions] == [1, 2]
    assert versions[0]["score"] == 70 and versions[0]["changes"] == 0
    assert versions[1]["changes"] == 1 and any("Ana → Bob" in l for l in versions[1]["lines"])
    dvp = sv.draft_vs_published(rid, hid, db_path=db_path)
    assert dvp["available"] and dvp["moved"][0]["to"] == "Bob"
    assert sv.draft_vs_published(rid, hid + 99, db_path=db_path) == {"available": False}


def test_learned_patterns_need_a_repeat(db_path, rid):
    conn = get_conn(db_path)
    hids = []
    for i in range(3):
        cur = conn.execute("INSERT INTO schedule_history (restaurant_id, week_start, week_end, hours_scheduled, hours_budget, "
                           "labor_target, schedule_csv, summary_json) VALUES (?,?,?,?,?,?,?,'[]')",
                           (rid, f"2026-10-{5 + 7 * i:02d}", f"2026-10-{11 + 7 * i:02d}", 6, 40, 30, HEADER))
        hids.append(cur.lastrowid)
    conn.commit()
    conn.close()
    for i, hid in enumerate(hids):
        date = f"2026-10-{10 + 7 * i:02d}"        # three Saturdays
        draft = HEADER + f"{date},Saturday,Ana,Server,4:00pm,10:00pm,6.0,\n"
        edited = HEADER + f"{date},Saturday,Bob,Server,4:00pm,10:00pm,6.0,\n"
        sv.append(rid, hid, "generated", draft, db_path=db_path)
        if i < 2:
            sv.append(rid, hid, "edited", edited, saved_by="will", db_path=db_path)
    learned = sv.learned_patterns(rid, db_path=db_path)
    kinds = {(p["kind"], p["employee"], p["day"], p["daypart"]) for p in learned}
    assert ("moved_off", "Ana", "Saturday", "night") in kinds
    assert ("moved_on", "Bob", "Saturday", "night") in kinds
    assert all(p["times"] == 2 for p in learned)
    block = sv.prompt_block(learned)
    assert "taken Ana off Saturday dinner/night in 2 recent weeks" in block
    assert sv.learned_patterns(rid, min_repeats=3, db_path=db_path) == []
