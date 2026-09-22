"""Edge cases of shift requests: drops, swaps and open-shift claims (SCHED audit).

Two people claiming one open shift at once, two claims on different shifts
racing the same CSV, a minor, an uncertified person or the wrong role
claiming, open shifts from the past, a manager naming an illegal
replacement, a swap nobody asked the colleague about, silent open shifts,
a staff note that locks someone out, and a claim written into a superseded
published row.

xfail(strict=True) marks a confirmed defect, asserting the correct
behaviour; the marker comes off with the fix. The races are made
deterministic with a barrier placed after the legality check, exactly where
the audit's probe found the window.
"""
import datetime as dt
import json
import sys
import threading

import pytest

# Imported here, before any fixture patches models.get_conn, so no module is
# first imported mid-test and left holding a redirect to a deleted database.
import activity, covers, decisions, delayed, demand_signals, goals, issues, metrics, outcomes  # noqa: E401,F401
import push, schedule_economics, schedule_intel, schedule_rules, schedule_versions  # noqa: E401,F401
import shift_requests, shift_quality, staff_schedule, staff_settings, strategy_jobs, time_off  # noqa: E401,F401
import labor_replacements  # noqa: F401

import emails
import models
import notify
import push
import schedule_engine as se
import schedule_rules as sr
import schedule_versions as sv
import shift_requests as srq
import staff_settings as ss
import strategy_jobs
from models import create_restaurant, Restaurant

HEADER = "date,day,employee,role,shift_start,shift_end,scheduled_hours,notes"
W1 = ["2026-10-05", "2026-10-06", "2026-10-07", "2026-10-08", "2026-10-09", "2026-10-10", "2026-10-11"]
TODAY = dt.date(2026, 10, 1)


@pytest.fixture
def db(db_path, monkeypatch):
    real = models.get_conn

    def conn(*a, **k):
        return real(db_path)
    for mod in list(sys.modules.values()):
        bound = getattr(mod, "get_conn", None) if mod is not None else None
        # The real one, or a redirect some earlier test left bound in a
        # module it imported for the first time mid-test.
        if bound is real or str(getattr(bound, "__module__", "")).startswith(("test_", "tests.", "conftest")):
            monkeypatch.setattr(mod, "get_conn", conn)
    monkeypatch.setattr(models, "get_conn", conn)
    monkeypatch.setattr(models, "DB_PATH", db_path)
    monkeypatch.setattr(models, "_cached_shifts", lambda r: [])
    return db_path


def _day(d):
    return dt.date.fromisoformat(d).strftime("%A")


def _restaurant(db_path, roster, **cols):
    rid = create_restaurant(Restaurant(name="Requests Co", owner_email="q@x.com"), db_path=db_path)
    if cols:
        conn = models.get_conn(db_path)
        conn.execute("UPDATE restaurants SET " + ", ".join(f"{k}=?" for k in cols) + " WHERE id=?", (*cols.values(), rid))
        conn.commit()
        conn.close()
    for name, role in roster:
        models.add_manual_team_member(rid, name, role=role, db_path=db_path)
    return rid


def _publish(db_path, rid, rows, dates=W1):
    text = HEADER + "\n" + "\n".join(f"{d},{_day(d)},{n},{role},{s},{e},{h}," for d, n, role, s, e, h in rows)
    hid = models.save_schedule_history(rid, dates[0], dates[-1], 0, 0, 30, text, [], db_path=db_path)
    conn = models.get_conn(db_path)
    conn.execute("UPDATE schedule_history SET published_at=datetime('now') WHERE id=?", (hid,))
    conn.commit()
    conn.close()
    sv.append(rid, hid, "published", text, db_path=db_path)
    return hid


def _csv_rows(db_path, hid):
    conn = models.get_conn(db_path)
    try:
        return sv.rows_from_csv(conn.execute("SELECT schedule_csv FROM schedule_history WHERE id=?", (hid,)).fetchone()[0])
    finally:
        conn.close()


def _open(rid, name, date, start, today=TODAY):
    req = srq.request_drop(rid, name, date, start, today=today)
    return srq.decide(rid, req["id"], True, decided_by="mgr")


def _racing_legality(monkeypatch):
    barrier = threading.Barrier(2)
    real = se.replacement_is_legal

    def slow_legal(*a, **k):
        out = real(*a, **k)
        try:
            barrier.wait(timeout=30)
        except threading.BrokenBarrierError:
            pass
        return out
    monkeypatch.setattr(se, "replacement_is_legal", slow_legal)
    return barrier


def _claim_in_threads(pairs):
    results = {}

    def go(key, rid, req_id, who):
        try:
            results[key] = srq.claim(rid, req_id, who)["status"]
        except Exception as e:
            results[key] = "refused: " + str(e)
    threads = [threading.Thread(target=go, args=p) for p in pairs]
    [t.start() for t in threads]
    [t.join(60) for t in threads]
    return results


SERVERS = [("Ana", "Server"), ("Ben", "Server"), ("Cara", "Server"), ("Dev", "Server")]


# ── SCHED-5: claims race ──────────────────────────────────────────────────

@pytest.mark.xfail(strict=True, reason="SCHED-5: the final UPDATE has no AND status='open', so two people are both told they have one shift")
def test_two_people_claiming_the_same_open_shift_at_once_gives_one_success(db, monkeypatch):
    rid = _restaurant(db, SERVERS)
    _publish(db, rid, [(W1[2], "Ana", "Server", "11:00am", "3:00pm", 4)])
    req = _open(rid, "Ana", W1[2], "11:00am")
    barrier = _racing_legality(monkeypatch)
    results = _claim_in_threads([("Cara", rid, req["id"], "Cara"), ("Dev", rid, req["id"], "Dev")])
    if barrier.broken:
        pytest.skip("the race window was not staged (a thread missed the barrier under load)")
    assert sorted(v == "covered" for v in results.values()) == [False, True], results


@pytest.mark.xfail(strict=True, reason="SCHED-5: each claim writes the whole CSV back unconditionally, so one cover is lost")
def test_two_claims_of_different_open_shifts_both_persist(db, monkeypatch):
    rid = _restaurant(db, SERVERS)
    hid = _publish(db, rid, [(W1[1], "Ana", "Server", "11:00am", "3:00pm", 4), (W1[1], "Ben", "Server", "5:00pm", "9:00pm", 4)])
    r1, r2 = _open(rid, "Ana", W1[1], "11:00am"), _open(rid, "Ben", W1[1], "5:00pm")
    barrier = _racing_legality(monkeypatch)
    results = _claim_in_threads([("A", rid, r1["id"], "Cara"), ("B", rid, r2["id"], "Dev")])
    if barrier.broken:
        pytest.skip("the race window was not staged (a thread missed the barrier under load)")
    assert set(results.values()) == {"covered"}, results
    tuesday = {(r["shift_start"], r["employee"]) for r in _csv_rows(db, hid) if r["date"] == W1[1]}
    assert tuesday == {("11:00am", "Cara"), ("5:00pm", "Dev")}


def test_one_claim_writes_the_cover_back_and_a_second_claim_is_refused(db):
    rid = _restaurant(db, SERVERS)
    hid = _publish(db, rid, [(W1[2], "Ana", "Server", "11:00am", "3:00pm", 4)])
    req = _open(rid, "Ana", W1[2], "11:00am")
    assert srq.claim(rid, req["id"], "Cara")["status"] == "covered"
    with pytest.raises(srq.ShiftRequestError):
        srq.claim(rid, req["id"], "Dev")
    assert [r["employee"] for r in _csv_rows(db, hid)] == ["Cara"]


# ── SCHED-6: claim legality equals sweep legality ─────────────────────────

BAR = [("Ben", "Bartender"), ("Dev", "Bartender"), ("Fay", "Bartender"), ("Dan", "Dishwasher"), ("Ana", "Server")]


@pytest.fixture
def bar_shift(db):
    rid = _restaurant(db, BAR, role_requirements_json=json.dumps({"Bartender": ["alcohol"]}))
    ss.upsert(rid, "Dev", is_minor=True, certifications=["alcohol"])
    ss.upsert(rid, "Dan", certifications=["alcohol"])
    ss.upsert(rid, "Ben", certifications=["alcohol"])
    hid = _publish(db, rid, [(W1[4], "Ben", "Bartender", "6:00pm", "1:00am", 7)])
    req = _open(rid, "Ben", W1[4], "6:00pm")
    return rid, hid, req


def _hard_kinds_after_claim(db, rid, hid):
    c = sr.build_constraints(rid, W1, list(sr.DAYS))
    return {(v["kind"], v["employee"]) for v in sr.violations(_csv_rows(db, hid), c) if v["hard"]}


@pytest.mark.xfail(strict=True, reason="SCHED-6: a minor can claim a 6pm-1am close; the claim never checks minor_latest_end")
def test_a_minor_cannot_claim_a_shift_ending_after_the_minors_latest_end(db, bar_shift):
    rid, hid, req = bar_shift
    with pytest.raises(srq.ShiftRequestError):
        srq.claim(rid, req["id"], "Dev")
    assert ("minor_late", "Dev") not in _hard_kinds_after_claim(db, rid, hid)


@pytest.mark.xfail(strict=True, reason="SCHED-6: a claim never checks the certification the role requires")
def test_a_person_without_the_roles_certification_cannot_claim_it(db, bar_shift):
    rid, hid, req = bar_shift
    with pytest.raises(srq.ShiftRequestError):
        srq.claim(rid, req["id"], "Fay")


@pytest.mark.xfail(strict=True, reason="SCHED-6: a claim never checks that the claimant works the shift's role")
def test_a_person_in_a_different_role_cannot_claim_the_shift(db, bar_shift):
    rid, hid, req = bar_shift
    with pytest.raises(srq.ShiftRequestError):
        srq.claim(rid, req["id"], "Dan")


def test_a_person_on_approved_time_off_cannot_claim(db):
    rid = _restaurant(db, SERVERS)
    _publish(db, rid, [(W1[2], "Ana", "Server", "11:00am", "3:00pm", 4)])
    conn = models.get_conn(db)
    conn.execute("INSERT INTO staff_time_off (restaurant_id, employee_name, start_date, end_date, status) "
                 "VALUES (?,?,?,?, 'approved')", (rid, "Cara", W1[2], W1[2]))
    conn.commit()
    conn.close()
    req = _open(rid, "Ana", W1[2], "11:00am")
    with pytest.raises(srq.ShiftRequestError) as e:
        srq.claim(rid, req["id"], "Cara")
    assert sr.LABELS["approved_time_off"] in str(e.value)


# ── SCHED-34: past open shifts, and a same-day pickup ─────────────────────

@pytest.mark.xfail(strict=True, reason="SCHED-34: open_shifts has no date filter and claim has no past-date check")
def test_an_open_shift_from_yesterday_is_neither_listed_nor_claimable(db):
    # open_shifts and claim read the real clock, so the week is built around it.
    today = dt.date.today()
    week = [(today - dt.timedelta(days=3 - i)).isoformat() for i in range(7)]
    yesterday = week[2]
    rid = _restaurant(db, SERVERS)
    _publish(db, rid, [(yesterday, "Ana", "Server", "11:00am", "3:00pm", 4)], dates=week)
    req = _open(rid, "Ana", yesterday, "11:00am", today=today - dt.timedelta(days=2))   # dropped before it happened
    assert req["id"] not in [r["id"] for r in srq.open_shifts(rid)]
    with pytest.raises(srq.ShiftRequestError):
        srq.claim(rid, req["id"], "Cara")


@pytest.mark.xfail(strict=True, reason="SCHED-34: anyone already working that date is refused, so a lunch server cannot pick up the dinner leg")
def test_a_lunch_server_can_pick_up_that_nights_dinner_shift(db):
    rid = _restaurant(db, SERVERS)
    _publish(db, rid, [(W1[2], "Ana", "Server", "11:00am", "3:00pm", 4), (W1[2], "Ben", "Server", "5:00pm", "10:00pm", 5)])
    req = _open(rid, "Ben", W1[2], "5:00pm")
    assert srq.claim(rid, req["id"], "Ana")["status"] == "covered"


# ── SCHED-20: approving a drop with an illegal named replacement ─────────

@pytest.mark.xfail(strict=True, reason="SCHED-20: decide commits status='open' before the replacement's legality check fails")
def test_approving_with_an_illegal_replacement_leaves_the_request_pending(db):
    rid = _restaurant(db, SERVERS)
    _publish(db, rid, [(W1[2], "Ana", "Server", "11:00am", "3:00pm", 4)])
    ss.upsert(rid, "Dev", active=False)
    req = srq.request_drop(rid, "Ana", W1[2], "11:00am", today=TODAY)
    with pytest.raises(srq.ShiftRequestError):
        srq.decide(rid, req["id"], True, decided_by="mgr", replacement="Dev")
    assert srq.for_manager(rid)[0]["status"] == "pending"
    assert srq.open_shifts(rid) == []


def test_approving_with_a_legal_replacement_covers_it_in_one_step(db):
    rid = _restaurant(db, SERVERS)
    hid = _publish(db, rid, [(W1[2], "Ana", "Server", "11:00am", "3:00pm", 4)])
    req = srq.request_drop(rid, "Ana", W1[2], "11:00am", today=TODAY)
    out = srq.decide(rid, req["id"], True, decided_by="mgr", replacement="Cara")
    assert out["status"] == "covered" and _csv_rows(db, hid)[0]["employee"] == "Cara"


# ── SCHED-21: consent and notification ────────────────────────────────────

@pytest.fixture
def outbox(monkeypatch):
    """Every channel the product could reach a person through, recorded."""
    calls = []

    def spy(label):
        return lambda *a, **k: calls.append((label, a, k))
    monkeypatch.setattr(push, "fire_push", spy("push"))
    monkeypatch.setattr(emails, "deliver", spy("email"), raising=False)
    for n in dir(emails):
        if n.startswith("send_") and callable(getattr(emails, n)):
            monkeypatch.setattr(emails, n, spy("email:" + n))
    for n in ("send_sms", "raise_alert", "deliver_alert"):
        monkeypatch.setattr(notify, n, spy("notify:" + n))
    monkeypatch.setattr(strategy_jobs, "_reach", spy("reach"))
    return calls


@pytest.mark.xfail(strict=True, reason="SCHED-21: a manager's approval moves the colleague's shift without the colleague ever accepting")
def test_a_swap_is_not_executed_until_the_colleague_accepts(db, outbox):
    rid = _restaurant(db, SERVERS)
    hid = _publish(db, rid, [(W1[1], "Ana", "Server", "11:00am", "3:00pm", 4), (W1[3], "Ben", "Server", "11:00am", "3:00pm", 4)])
    req = srq.request_swap(rid, "Ana", W1[1], "11:00am", "Ben", W1[3], "11:00am", today=TODAY)
    out = srq.decide(rid, req["id"], True, decided_by="mgr")
    assert out["status"] != "covered"
    assert {(r["date"], r["employee"]) for r in _csv_rows(db, hid)} == {(W1[1], "Ana"), (W1[3], "Ben")}


@pytest.mark.xfail(strict=True, reason="SCHED-21: a claim notifies nobody — not the person who dropped it, not the claimant")
def test_a_claim_tells_the_person_who_dropped_the_shift(db, outbox):
    rid = _restaurant(db, SERVERS)
    _publish(db, rid, [(W1[2], "Ana", "Server", "11:00am", "3:00pm", 4)])
    req = _open(rid, "Ana", W1[2], "11:00am")
    outbox.clear()
    srq.claim(rid, req["id"], "Cara")
    assert any("Ana" in json.dumps([a, k], default=str) for _, a, k in outbox), outbox


@pytest.mark.xfail(strict=True, reason="SCHED-21: an approved drop becomes an open shift that nobody is told about")
def test_an_approved_drop_is_announced_to_staff_who_could_take_it(db, outbox):
    rid = _restaurant(db, SERVERS)
    _publish(db, rid, [(W1[2], "Ana", "Server", "11:00am", "3:00pm", 4)])
    req = srq.request_drop(rid, "Ana", W1[2], "11:00am", today=TODAY)
    outbox.clear()
    srq.decide(rid, req["id"], True, decided_by="mgr")
    assert outbox, "five call-offs must not become five silent open shifts"


# ── SCHED-35: any staff note locks a person out ───────────────────────────

@pytest.mark.xfail(strict=True, reason="SCHED-35: any non-empty staff note marks the person constrained and refuses every claim")
def test_a_person_with_a_compliment_on_file_can_still_claim_a_legal_shift(db):
    rid = _restaurant(db, SERVERS)
    models.save_staff_note(rid, "Cara", "great with regulars", db_path=db)
    _publish(db, rid, [(W1[2], "Ana", "Server", "11:00am", "3:00pm", 4)])
    req = _open(rid, "Ana", W1[2], "11:00am")
    assert srq.claim(rid, req["id"], "Cara")["status"] == "covered"


# ── SCHED-10: a claim against a superseded published row ─────────────────

@pytest.mark.xfail(strict=True, reason="SCHED-10: a claim is written into the superseded version it was opened on, not the week staff now see")
def test_a_claim_opened_before_a_republish_lands_in_the_live_week(db):
    rid = _restaurant(db, SERVERS)
    v1 = _publish(db, rid, [(W1[2], "Ana", "Server", "11:00am", "3:00pm", 4)])
    req = _open(rid, "Ana", W1[2], "11:00am")
    v2 = _publish(db, rid, [(W1[2], "Ana", "Server", "11:00am", "3:00pm", 4), (W1[3], "Ben", "Server", "5:00pm", "9:00pm", 4)])
    srq.claim(rid, req["id"], "Cara")
    live = {(r["date"], r["employee"]) for r in _csv_rows(db, v2)}
    assert (W1[2], "Cara") in live and (W1[2], "Ana") not in live
