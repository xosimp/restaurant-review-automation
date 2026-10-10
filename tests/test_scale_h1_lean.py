"""Scheduler audit 10/10/26, workstream H1 — reading only what is used: the
baseline's window (SCALE-17), the published weeks' CSV alone (SCALE-18,
SCALE-19) and the Crew stage's sales summed in SQL and kept (SCALE-27)."""
import random
from datetime import date, timedelta

import pytest

import labor
import models
from models import Restaurant, create_restaurant


@pytest.fixture(autouse=True)
def _redirect(db_path, monkeypatch):
    real = models.get_conn
    monkeypatch.setattr(models, "get_conn", lambda *a, **k: real(db_path))
    monkeypatch.setattr(models, "DB_PATH", db_path)


# ── SCALE-17: the baseline counts only the weeks it reads ─────────────────

def _history(weeks, start=date(2024, 1, 1)):
    rng = random.Random(4)
    rows = []
    for k in range(weeks * 7):
        d = start + timedelta(days=k)
        if d.weekday() == 1 and k // 7 % 5 == 0:
            continue                      # a Tuesday closed some weeks: its own last 8 reach further back
        people = 3 + (k // 7) // 20 + rng.randint(0, 2)       # a crew that grows over the years
        for i in range(people):
            rows.append({"date": d.isoformat(), "day": d.strftime("%A"), "employee": f"S{i}", "role": "Server",
                         "shift_start": "17:00", "shift_end": "23:30", "scheduled_hours": "6.5"})
        rows.append({"date": d.isoformat(), "day": d.strftime("%A"), "employee": "Cook", "role": "Kitchen",
                     "shift_start": "10:00", "shift_end": "15:00", "scheduled_hours": "5"})
    # Long ago, S0 worked the bar once: still cross-trained.
    rows.append({"date": start.isoformat(), "day": start.strftime("%A"), "employee": "S0", "role": "Bartender",
                 "shift_start": "17:00", "shift_end": "23:00", "scheduled_hours": "6"})
    return rows


def test_the_baseline_reads_the_same_from_the_weeks_it_uses_as_from_three_years():
    rows = _history(156)
    full = labor.historical_patterns(rows)
    recent = labor._recent_rows(rows)
    assert len(recent) < len(rows) / 10
    # Every weekday keeps its own newest TYPICAL_WEEKS dates (the skipped
    # Tuesdays reach further back than eight calendar weeks).
    for wd in range(7):
        mine = sorted({r["date"] for r in rows if date.fromisoformat(r["date"]).weekday() == wd})
        kept = sorted({r["date"] for r in recent if date.fromisoformat(r["date"]).weekday() == wd})
        assert kept[-labor.TYPICAL_WEEKS:] == mine[-labor.TYPICAL_WEEKS:]
    # What the readings say is what counting only those weeks says...
    only = labor.historical_patterns(recent)
    for key in ("typical_headcount", "headcount_trends", "late_headcount"):
        assert full[key] == only[key], key
    # ...and who can flex still comes from every punch.
    assert full["cross_trained"]["S0"] == ["Bartender", "Server"]
    assert "S0" not in only["cross_trained"]


def test_a_row_whose_date_does_not_read_is_kept_as_the_count_keeps_it():
    rows = _history(20) + [{"date": "Friday", "day": "Friday", "employee": "X", "role": "Server",
                            "shift_start": "17:00", "shift_end": "22:00"}]
    assert rows[-1] in labor._recent_rows(rows)


# ── SCALE-18 / SCALE-19: the published weeks' CSV alone ────────────────────

def _published(rid, week_start, csv_text):
    hid = models.save_schedule_history(rid, week_start, (date.fromisoformat(week_start) + timedelta(days=6)).isoformat(),
                                       10, 100, 30, csv_text, [])
    conn = models.get_conn()
    conn.execute("UPDATE schedule_history SET published_at=datetime('now'), review_json=? WHERE id=?",
                 ('{"stations": {"assigned": [{"employee": "Ana", "date": "' + week_start
                  + '", "shift_start": "5:00pm", "night": "Grill"}]}}', hid))
    conn.commit()
    conn.close()
    return hid


def test_the_staff_portal_and_the_baseline_never_read_the_full_week_detail(monkeypatch):
    import staff_schedule
    rid = create_restaurant(Restaurant(name="Lean Co", owner_email="l@x.test"))
    today = date.today()
    mon = today - timedelta(days=today.weekday())
    csv_text = ("date,day,employee,role,shift_start,shift_end,scheduled_hours,notes\n"
                + "".join(f"{(mon + timedelta(days=i)).isoformat()},{(mon + timedelta(days=i)).strftime('%A')},"
                          f"Ana,Server,5:00pm,10:00pm,5,\n" for i in range(7)))
    _published(rid, mon.isoformat(), csv_text)
    _published(rid, (mon - timedelta(weeks=1)).isoformat(), csv_text.replace(mon.isoformat()[:7], mon.isoformat()[:7]))

    def refuse(*a, **k):
        raise AssertionError("SELECT * and four JSON parses for one column")
    monkeypatch.setattr(models, "get_schedule_history_detail", refuse)
    monkeypatch.setattr(staff_schedule, "get_schedule_history_detail", refuse)
    out = staff_schedule.shifts_for_employee(rid, "Ana", today=mon)
    assert out["published"] and len(out["upcoming"]) >= 6
    assert any(s.get("station") == "Grill" for s in out["upcoming"])            # the stations still come
    rows = labor.published_rows_for_baseline(rid, before=(mon + timedelta(days=7)).isoformat())
    assert rows and {r["employee"] for r in rows} == {"Ana"}


# ── SCALE-27: the Crew stage's sales and the matrix kept ───────────────────

def _ticket(rid, bd, opened, net):
    conn = models.get_conn()
    conn.execute("INSERT INTO pos_tickets (restaurant_id, provider, ticket_id, business_date, opened_at, net_sales, "
                 "cancelled) VALUES (?, 'rpower', ?, ?, ?, ?, 0)",
                 (rid, f"t{random.random()}", bd, opened, net))
    conn.commit()
    conn.close()


def _python_sales(rows, today):
    """The reading the SQL replaced: every check into Python, the hour off
    fromisoformat(stamp[:19])."""
    from datetime import datetime
    import crew_matrix as cm
    per = {}
    for bd, opened, net in rows:
        try:
            h = datetime.fromisoformat(str(opened)[:19]).hour
            b = date.fromisoformat(str(bd)[:10])
        except ValueError:
            continue
        k = (b.isoformat(), "morning" if 5 <= h < 15 else "night")
        per[k] = per.get(k, 0.0) + float(net or 0)
    by = {}
    for (d, part), v in per.items():
        by.setdefault((cm.DAYS[date.fromisoformat(d).weekday()], part), []).append(v)
    return {k: round(sorted(v)[len(v) // 2]) for k, v in by.items()}


def test_the_shift_sales_summed_in_sql_read_as_the_python_did():
    import crew_matrix as cm
    rid = create_restaurant(Restaurant(name="Sales Co", owner_email="s@x.test"))
    try:
        cols = {r[1] for r in models.get_conn().execute("PRAGMA table_info(pos_tickets)")}
    except Exception:
        cols = set()
    if not {"business_date", "opened_at", "net_sales", "cancelled"} <= cols:
        pytest.skip("pos_tickets has another shape here")
    today = date(2026, 10, 10)
    rows = []
    rng = random.Random(9)
    for k in range(40):
        d = today - timedelta(days=k + 1)
        for stamp in (f"{d}T11:30:00", f"{d} 14:59:00", f"{d}T15:00:00-05:00", f"{d}T18:45:00Z",
                      f"{d}T02:10:00", f"{d}T04:59:59.123"):
            net = round(rng.uniform(5, 90), 2)
            rows.append((d.isoformat(), stamp, net))
            _ticket(rid, d.isoformat(), stamp, net)
    rows.append(((today - timedelta(days=2)).isoformat(), "garbage", 50.0))
    _ticket(rid, (today - timedelta(days=2)).isoformat(), "garbage", 50.0)
    assert cm._shift_sales(rid, today=today) == _python_sales(rows, today)


def test_the_crew_matrix_is_kept_until_something_it_reads_changes(monkeypatch):
    import crew_matrix as cm
    cm.invalidate_cache()
    rid = create_restaurant(Restaurant(name="Crew Co", owner_email="c@x.test"))
    built = []
    real = cm.build
    monkeypatch.setattr(cm, "build", lambda *a, **k: (built.append(1), real(*a, **k))[1])
    monkeypatch.setattr("labor.staffing_baseline", lambda *a, **k: {"typical_headcount": {
        ("Monday", "night"): {"Server": 3}}})
    first, _ = cm.api_body(rid, "GET")
    again, _ = cm.api_body(rid, "GET")
    assert again == first and len(built) == 1
    # An answer, a floor, any setting: the restaurant's inputs moved.
    models.update_restaurant(rid, {"crew_answers_json": '{"Server|Monday|night": {"answer": "lean"}}'})
    cm.api_body(rid, "GET")
    assert len(built) == 2
    cm.invalidate_cache()
