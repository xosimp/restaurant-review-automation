"""Employee audit fix round — server integration (S9, 10/2/26).

The handoffs eight backend workstreams (B1–B8) could not finish inside
their own files: retention for every new staff ledger, the people
rename/erase registry over the new stores, the bell's name for a task-sheet
flag, announcements in the reader's language, the brief line on the shift
reminder (AI-07), a guard against live model calls in tests, the overtime
heads-up on the pickup board, expired certificates in schedule legality,
the live week by one rule (LG-34), a floor section following a shift that
changed hands, one helper for telling the deciders, the signup token off
the URL, and a running-late report on an issue that is already open.

Every clock is pinned.
"""
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

import auth
import models
from auth import create_user, upsert_membership
from models import Restaurant, create_restaurant, get_conn

ROOT = Path(__file__).resolve().parent.parent
DAY = date(2026, 9, 21)                      # a Monday
NOON_ISH = datetime(2026, 9, 21, 10, 50)


@pytest.fixture(autouse=True)
def db(db_path, monkeypatch):
    real = models.get_conn

    def conn(*a, **k):
        return real(db_path)
    for mod in list(sys.modules.values()):
        bound = getattr(mod, "get_conn", None) if mod is not None else None
        if bound is real or str(getattr(bound, "__module__", "")).startswith(("test_", "tests.", "conftest")):
            monkeypatch.setattr(mod, "get_conn", conn)
    monkeypatch.setattr(models, "get_conn", conn)
    monkeypatch.setattr(models, "DB_PATH", db_path)
    monkeypatch.setattr(auth, "DB_PATH", db_path)
    auth.init_auth(db_path=db_path)
    import push
    push.init_push(db_path)                   # staff_notices (staff_reminders) is made there
    return db_path


def _rid(db_path, name="Integration Co"):
    return create_restaurant(Restaurant(name=name, owner_email=f"{name[:3].lower()}@x.test", module_labor=1),
                             db_path=db_path)


def _staff(db_path, rid, name, job_role=None, username=None):
    uid = create_user(rid, username or (name.split()[0].lower() + str(rid)),
                      f"{(username or name.split()[0]).lower()}{rid}@x.test", "unused-pass-9", db_path=db_path)
    m = upsert_membership(uid, rid, "employee", employee_name=name, job_role=job_role, db_path=db_path)
    return uid, m["id"]


def _q(db_path, sql, args=()):
    c = get_conn(db_path)
    try:
        return [dict(r) for r in c.execute(sql, args).fetchall()]
    finally:
        c.close()


def _x(db_path, sql, args=()):
    c = get_conn(db_path)
    try:
        cur = c.execute(sql, args)
        c.commit()
        return cur.lastrowid
    finally:
        c.close()


# ═══ 1. retention for every new staff ledger ════════════════════════════════

NEW_LEDGERS = {"staff_announcement_recipients": "created_at", "staff_announcements": "created_at",
               "staff_thread_messages": "created_at", "staff_running_late": "business_date",
               "shift_offers": "created_at", "staff_shift_pulse": "business_date", "staff_briefs": "business_date",
               "staff_calendar_links": "revoked_at", "shift_sections": "date", "staff_translations": "created_at"}


def test_every_new_staff_ledger_is_registered_with_a_floor_and_its_column():
    import ops
    for table, col in NEW_LEDGERS.items():
        assert table in ops._RETENTION_DAYS, table
        assert ops._RETENTION_COLUMN[table] == col, table
        assert 0 < ops._RETENTION_FLOOR_DAYS[table] <= ops._RETENTION_DAYS[table], table
    # The comms keep two years, the operational records one.
    assert ops._RETENTION_DAYS["staff_thread_messages"] == 730 and ops._RETENTION_DAYS["staff_briefs"] == 365
    # A recipient row is pruned before its announcement, and an announcement
    # only once none is left.
    order = list(ops._RETENTION_DAYS)
    assert order.index("staff_announcement_recipients") < order.index("staff_announcements")
    assert "staff_announcement_recipients" in ops._RETENTION_ONLY["staff_announcements"]
    # Tables that prune themselves are not registered twice.
    assert not {"staff_notices", "staff_otp_sends", "staff_pin_resets"} & set(ops._RETENTION_DAYS)


def test_a_pass_prunes_old_announcements_their_receipts_and_messages_and_keeps_live_links(db):
    import ops
    rid = _rid(db)
    old = "2023-01-01 10:00:00"
    aid = _x(db, "INSERT INTO staff_announcements (restaurant_id, title, created_at) VALUES (?, 'Old', ?)", (rid, old))
    _x(db, "INSERT INTO staff_announcement_recipients (restaurant_id, announcement_id, membership_id, employee_name, "
           "created_at) VALUES (?,?,1,'Ana',?)", (rid, aid, old))
    new = _x(db, "INSERT INTO staff_announcements (restaurant_id, title) VALUES (?, 'New')", (rid,))
    _x(db, "INSERT INTO staff_announcement_recipients (restaurant_id, announcement_id, membership_id, employee_name) "
           "VALUES (?,?,1,'Ana')", (rid, new))
    tid = _x(db, "INSERT INTO staff_threads (restaurant_id, membership_id, employee_name) VALUES (?,1,'Ana')", (rid,))
    _x(db, "INSERT INTO staff_thread_messages (restaurant_id, thread_id, sender_kind, body, created_at) "
           "VALUES (?,?,'staff','old words',?)", (rid, tid, old))
    _x(db, "INSERT INTO staff_calendar_links (restaurant_id, membership_id, nonce, token_hash, created_at) "
           "VALUES (?,1,'n','live-hash',?)", (rid, old))
    _x(db, "INSERT INTO staff_calendar_links (restaurant_id, membership_id, nonce, token_hash, created_at, revoked_at) "
           "VALUES (?,1,'n','dead-hash',?,?)", (rid, old, old))
    out = ops.prune_ledgers(db)
    assert out.get("staff_announcements") == 1 and out.get("staff_announcement_recipients") == 1
    assert [a["title"] for a in _q(db, "SELECT title FROM staff_announcements")] == ["New"]
    assert len(_q(db, "SELECT 1 FROM staff_announcement_recipients")) == 1
    assert _q(db, "SELECT 1 FROM staff_thread_messages") == []
    assert [r["token_hash"] for r in _q(db, "SELECT token_hash FROM staff_calendar_links")] == ["live-hash"]


def test_an_existing_recipients_table_gains_its_stamp_from_the_announcement(db):
    import staff_comms
    c = get_conn(db)
    c.execute("DROP TABLE staff_announcement_recipients")
    c.execute("CREATE TABLE staff_announcement_recipients (id INTEGER PRIMARY KEY AUTOINCREMENT, restaurant_id INTEGER, "
              "announcement_id INTEGER, membership_id INTEGER, employee_name TEXT, delivered_via TEXT, "
              "delivered_at TEXT, acked_at TEXT, UNIQUE(announcement_id, membership_id))")
    c.commit()
    c.close()
    rid = _rid(db)
    aid = _x(db, "INSERT INTO staff_announcements (restaurant_id, title, created_at) VALUES (?, 'T', "
                 "'2026-01-02 03:04:05')", (rid,))
    _x(db, "INSERT INTO staff_announcement_recipients (restaurant_id, announcement_id, membership_id, employee_name) "
           "VALUES (?,?,1,'Ana')", (rid, aid))
    staff_comms.init_staff_comms(db)
    assert _q(db, "SELECT created_at FROM staff_announcement_recipients")[0]["created_at"] == "2026-01-02 03:04:05"


# ═══ 2. people: rename and erase reach the new stores ═══════════════════════

def _seed_person_rows(db, rid, name, mid):
    import staff_settings
    key = staff_settings.name_key(name)
    hid = models.save_schedule_history(rid, "2026-09-21", "2026-09-27", 40, 40, 30, "date\n", [], db_path=db)
    req = _x(db, "INSERT INTO shift_change_requests (restaurant_id, history_id, employee_name, date, shift_start, "
                 "status, kind, target_name) VALUES (?,?, 'Ana B.', '2026-09-22', '16:00', 'open', 'swap', ?)",
             (rid, hid, name))
    _x(db, "INSERT INTO shift_offers (restaurant_id, request_id, name, name_key) VALUES (?,?,?,?)", (rid, req, name, key))
    _x(db, "INSERT INTO staff_running_late (restaurant_id, membership_id, employee_name, employee_key, business_date, "
           "shift_start, eta_minutes) VALUES (?,?,?,?, '2026-09-21', '16:00', 20)", (rid, mid, name, key))
    aid = _x(db, "INSERT INTO staff_announcements (restaurant_id, title) VALUES (?, 'Hi')", (rid,))
    _x(db, "INSERT INTO staff_announcement_recipients (restaurant_id, announcement_id, membership_id, employee_name) "
           "VALUES (?,?,?,?)", (rid, aid, mid, name))
    tid = _x(db, "INSERT INTO staff_threads (restaurant_id, membership_id, employee_name) VALUES (?,?,?)", (rid, mid, name))
    _x(db, "INSERT INTO staff_thread_messages (restaurant_id, thread_id, sender_kind, body) VALUES (?,?,'staff','hey')",
       (rid, tid))
    _x(db, "INSERT INTO staff_notices (restaurant_id, kind, claim_key, employee_name, state) "
           "VALUES (?, 'held_text', 'k-1', ?, 'held')", (rid, name))
    _x(db, "INSERT INTO staff_certs (restaurant_id, employee_key, employee_name, cert, expires_on) "
           "VALUES (?,?,?, 'food handler', '2027-01-01')", (rid, key, name))
    _x(db, "INSERT INTO staff_shift_pulse (restaurant_id, membership_id, business_date, rating, note) "
           "VALUES (?,?, '2026-09-20', 2, 'my knee hurts')", (rid, mid))
    _x(db, "INSERT INTO staff_calendar_links (restaurant_id, membership_id, nonce, token_hash) VALUES (?,?, 'n', 'h1')",
       (rid, mid))
    _x(db, "INSERT INTO staff_language (membership_id, restaurant_id, language) VALUES (?,?, 'es')", (mid, rid))
    return req


def test_a_rename_carries_the_person_into_every_new_store(db):
    import people
    import staff_settings
    rid = _rid(db)
    staff_settings.upsert(rid, "Zed Q.", max_hours=30, db_path=db)
    _uid, mid = _staff(db, rid, "Zed Q.")
    req = _seed_person_rows(db, rid, "Zed Q.", mid)
    pid = people.person_id_for(rid, "Zed Q.", db_path=db)
    people.rename_person(rid, pid, "Zed Quinn", db_path=db)
    assert _q(db, "SELECT target_name FROM shift_change_requests WHERE id=?", (req,))[0]["target_name"] == "Zed Quinn"
    assert _q(db, "SELECT name, name_key FROM shift_offers")[0] == {"name": "Zed Quinn", "name_key": "zed quinn"}
    for table in ("staff_running_late", "staff_announcement_recipients", "staff_threads", "staff_notices", "staff_certs"):
        assert {r["employee_name"] for r in _q(db, f"SELECT employee_name FROM {table}")} == {"Zed Quinn"}, table
    assert _q(db, "SELECT employee_key FROM staff_certs")[0]["employee_key"] == "zed quinn"


def test_erasing_a_person_removes_their_staff_app_records_and_only_clears_a_mention(db):
    import people
    import staff_settings
    rid = _rid(db)
    staff_settings.upsert(rid, "Zed Q.", max_hours=30, db_path=db)
    _uid, mid = _staff(db, rid, "Zed Q.")
    other_uid, other_mid = _staff(db, rid, "Ana B.")
    req = _seed_person_rows(db, rid, "Zed Q.", mid)
    _x(db, "INSERT INTO staff_shift_pulse (restaurant_id, membership_id, business_date, rating) "
           "VALUES (?,?, '2026-09-20', 5)", (rid, other_mid))
    pid = people.person_id_for(rid, "Zed Q.", db_path=db)
    staff_settings.upsert(rid, "Zed Q.", active=False, db_path=db)
    _x(db, "UPDATE memberships SET is_active=0 WHERE id=?", (mid,))
    out = people.erase_person(rid, pid, db_path=db)
    assert out["ok"]
    for table in ("shift_offers", "staff_running_late", "staff_announcement_recipients", "staff_threads",
                  "staff_thread_messages", "staff_notices", "staff_certs", "staff_calendar_links", "staff_language"):
        assert _q(db, f"SELECT 1 FROM {table}") == [], table
    # Another person's pulse stays; theirs (with its note) does not.
    assert _q(db, "SELECT membership_id FROM staff_shift_pulse") == [{"membership_id": other_mid}]
    # The swap they were asked to take is Ana's request: the row stays, the mention goes.
    row = _q(db, "SELECT employee_name, target_name FROM shift_change_requests WHERE id=?", (req,))[0]
    assert row == {"employee_name": "Ana B.", "target_name": None}


def test_update_person_sets_the_pin_on_the_active_login_not_an_old_one(db):
    import people
    import staff_settings
    rid = _rid(db)
    staff_settings.upsert(rid, "Zed Q.", max_hours=30, db_path=db)
    _old_uid, old_mid = _staff(db, rid, "Zed Q.", username="zedold")
    _x(db, "UPDATE memberships SET is_active=0 WHERE id=?", (old_mid,))
    _new_uid, new_mid = _staff(db, rid, "Zed Q.", job_role="Server", username="zednew")
    people.update_person(rid, people.person_key("Zed Q."), {"job_title": "Lead server"}, may_manage_logins=True,
                         db_path=db)
    roles = {r["id"]: r["job_role"] for r in _q(db, "SELECT id, job_role FROM memberships WHERE id IN (?,?)",
                                                (old_mid, new_mid))}
    assert roles[new_mid] == "Lead server" and roles[old_mid] != "Lead server"


# ═══ 3. the bell names a task-sheet flag ════════════════════════════════════

def test_a_task_flag_issue_has_its_own_place_in_the_bell():
    import client_api
    assert client_api._ISSUE_WHERE["task_flag"] == "Labor · a reading out of range"


# ═══ 4. announcements in the reader's language ══════════════════════════════

@pytest.fixture
def told(monkeypatch):
    import people, strategy_jobs, staff_comms
    out = {"staff": []}

    def _tell(rid, name, title, lines, *, email_type="staff_notice", channel=None, db_path=None, data=None,
              priority=None, **kw):
        out["staff"].append({"name": name, "title": title, "lines": lines})
        return "push"
    monkeypatch.setattr(people, "tell", _tell)
    monkeypatch.setattr(people, "reach", lambda rid, names, db_path=None: {n: {} for n in names})
    monkeypatch.setattr(strategy_jobs, "_reach", lambda *a, **k: 1)
    monkeypatch.setattr(staff_comms, "_local_now", lambda rid: NOON_ISH)
    return out


def test_an_announcement_goes_out_translated_and_the_inbox_shows_both(db, told, monkeypatch):
    import staff_comms
    import staff_knowledge
    rid = _rid(db)
    _ua, ana = _staff(db, rid, "Ana R")
    _ub, ben = _staff(db, rid, "Ben T")
    staff_knowledge.set_language(ben, rid, "es", db_path=db)
    calls = []

    def fake(restaurant_id, text, lang, reader=None, db_path=None):
        calls.append(text)
        return {"Menu night": "Noche de menú", "Tasting at 3pm.": "Degustación a las 3pm."}[text], "ok"
    monkeypatch.setattr(staff_knowledge, "_translate", fake)
    staff_comms.create_announcement(rid, {"id": 1, "username": "boss"}, "Menu night", "Tasting at 3pm.",
                                    db_path=db)
    by = {s["name"]: s for s in told["staff"]}
    assert by["Ben T"]["title"] == "Noche de menú" and by["Ben T"]["lines"] == ["Degustación a las 3pm."]
    assert by["Ana R"]["title"] == "Menu night"
    # The inbox reads the cache only: a model that would now fail is never called.
    monkeypatch.setattr(staff_knowledge, "_translate", lambda *a, **k: pytest.fail("a list read called the model"))
    (item,) = staff_comms.staff_announcements(rid, ben, now_local=NOON_ISH, db_path=db)
    assert (item["title"], item["body"], item["translated"]) == ("Noche de menú", "Degustación a las 3pm.", True)
    assert (item["original_title"], item["original_body"], item["language"]) == ("Menu night", "Tasting at 3pm.", "es")
    (plain,) = staff_comms.staff_announcements(rid, ana, now_local=NOON_ISH, db_path=db)
    assert plain["title"] == "Menu night" and plain["translated"] is False and plain["original_title"] is None
    assert sorted(calls) == ["Menu night", "Tasting at 3pm."]       # once per text and language


# ═══ 5. AI-07: the brief line on the shift reminder ═════════════════════════

@pytest.fixture
def remind(db, monkeypatch):
    import people, preferences, task_sheets
    sent = []
    monkeypatch.setattr(task_sheets, "published_day_shifts", lambda r, day: (
        [{"employee": "Ana R", "role": "Server", "start": "2026-10-01T16:00:00", "end": "2026-10-01T22:00:00"}]
        if day == date(2026, 10, 1) else [], True))
    monkeypatch.setattr(people, "reach", lambda rid, names, db_path=None: {n: {"push_user_id": 99} for n in names})
    monkeypatch.setattr(preferences, "push_allowed", lambda *a, **k: True)

    def _tell_staff(rid, name, kind, title, body, **kw):
        sent.append({"name": name, "title": title, "body": body})
        return "push"
    monkeypatch.setattr(people, "tell_staff", _tell_staff)
    return sent


def test_the_shift_reminder_carries_the_approved_brief_headline(db, remind, monkeypatch):
    import staff_brief
    import staff_reminders
    rid = _rid(db)
    monkeypatch.setattr(staff_brief, "approved", lambda r, d, db_path=None: {
        "brief_text": "Big game at 7, expect a rush from 6:30. Push the patio specials.", "focus": None})
    out = staff_reminders.remind_restaurant(rid, datetime(2026, 10, 1, 15, 0), db_path=db)
    assert out["attempted"] == 1
    (s,) = remind
    assert s["body"] == "You're on 4pm–10pm (Server). Big game at 7, expect a rush from 6:30."


def test_with_nothing_approved_the_reminder_carries_the_first_preshift_line(db, remind, monkeypatch):
    import preshift
    import staff_brief
    import staff_reminders
    rid = _rid(db)
    monkeypatch.setattr(staff_brief, "approved", lambda r, d, db_path=None: {"brief_text": None, "focus": None})
    monkeypatch.setattr(preshift, "build_cached", lambda r, day=None, db_path=None: {
        "items": [{"kind": "busy", "text": "Busier than a typical Thursday."}, {"kind": "x", "text": "Second."}]})
    staff_reminders.remind_restaurant(rid, datetime(2026, 10, 1, 15, 0), db_path=db)
    assert remind[0]["body"].endswith("(Server). Busier than a typical Thursday.")


def test_revert_check_no_brief_no_extra_line(db, remind, monkeypatch):
    import preshift
    import staff_brief
    import staff_reminders
    rid = _rid(db)
    monkeypatch.setattr(staff_brief, "approved", lambda r, d, db_path=None: {"brief_text": None, "focus": None})
    monkeypatch.setattr(preshift, "build_cached", lambda r, day=None, db_path=None: {"items": []})
    staff_reminders.remind_restaurant(rid, datetime(2026, 10, 1, 15, 0), db_path=db)
    assert remind[0]["body"] == "You're on 4pm–10pm (Server)."


# ═══ 6. a live model call fails the test ════════════════════════════════════

def test_a_real_anthropic_request_is_refused_loudly(_no_live_anthropic_calls):
    import anthropic
    from conftest import LiveAnthropicCall
    client = anthropic.Anthropic(api_key="sk-test-not-real", max_retries=0)
    with pytest.raises(LiveAnthropicCall):
        client.messages.create(model="claude-haiku-4-5", max_tokens=5, messages=[{"role": "user", "content": "x"}])
    assert _no_live_anthropic_calls and "anthropic.com" in _no_live_anthropic_calls[0]
    _no_live_anthropic_calls.clear()          # tripped on purpose


def test_a_stubbed_model_call_never_reaches_the_guard(monkeypatch, _no_live_anthropic_calls):
    import ai_utils

    class _Fake:
        class messages:
            @staticmethod
            def create(**k):
                return {"ok": True}
    monkeypatch.setattr(ai_utils, "get_client", lambda timeout=None: _Fake())
    assert ai_utils.get_client().messages.create(model="m") == {"ok": True}
    assert _no_live_anthropic_calls == []


# ═══ 7. the overtime heads-up on the pickup board and offers ═══════════════

def test_open_shifts_and_offers_carry_the_viewers_overtime_note(monkeypatch):
    import shift_requests as sr
    import staff_insights
    seen = []

    def note(rid, name, shift_date, hours, today=None, db_path=None):
        seen.append((name, shift_date, hours))
        return f"This {hours:g}h pickup takes you past 40 hours that week (to 44h)."
    monkeypatch.setattr(staff_insights, "pickup_overtime_note", note)
    monkeypatch.setattr(sr, "mine", lambda *a, **k: [])
    monkeypatch.setattr(sr, "asked_of_me", lambda *a, **k: [])
    shift = {"id": 5, "kind": "post", "date": "2026-10-03", "shift_start": "16:00", "shift_end": "22:00",
             "role": "Server", "status": "open", "employee_name": ""}
    monkeypatch.setattr(sr, "open_shifts", lambda *a, **k: [shift])
    monkeypatch.setattr(sr, "live_offers", lambda *a, **k: [dict(shift, id=9, request_id=5, status="offered",
                                                                   name="Ana R", note=None, issue_id=None,
                                                                   created_at="2026-10-01 10:00:00")])
    monkeypatch.setattr(sr, "_judge", lambda *a, **k: None)
    out = sr.for_staff(1, "Ana R", now=datetime(2026, 10, 1, 12, 0))
    assert out["open"][0]["pickup_overtime_note"].startswith("This 6h pickup")
    assert out["offers"][0]["pickup_overtime_note"].startswith("This 6h pickup")
    assert seen == [("Ana R", "2026-10-03", 6.0), ("Ana R", "2026-10-03", 6.0)]


def test_a_shift_they_cannot_take_carries_no_overtime_note(monkeypatch):
    import shift_requests as sr
    import staff_insights
    monkeypatch.setattr(staff_insights, "pickup_overtime_note", lambda *a, **k: "past 40")
    monkeypatch.setattr(sr, "mine", lambda *a, **k: [])
    monkeypatch.setattr(sr, "asked_of_me", lambda *a, **k: [])
    monkeypatch.setattr(sr, "live_offers", lambda *a, **k: [])
    monkeypatch.setattr(sr, "open_shifts", lambda *a, **k: [{"id": 5, "kind": "post", "date": "2026-10-03",
                                                              "shift_start": "16:00", "shift_end": "22:00",
                                                              "role": "Server", "status": "open"}])

    def judge(*a, **k):
        raise sr.ShiftRequestError("you'd be over your hours")
    monkeypatch.setattr(sr, "_judge", judge)
    out = sr.for_staff(1, "Ana R", now=datetime(2026, 10, 1, 12, 0))
    assert out["open"][0]["can_take"] is False and out["open"][0]["pickup_overtime_note"] is None


# ═══ 8. an expired certificate is not held ══════════════════════════════════

def test_an_expired_certificate_does_not_satisfy_a_role_that_needs_it(db):
    import schedule_rules as sr
    import staff_settings
    rid = _rid(db)
    models.update_restaurant(rid, {"role_requirements_json": '{"Bartender": ["food handler"]}'}, db_path=db)
    for n in ("Ana R", "Ben T"):
        models.add_manual_team_member(rid, n, role="Bartender", db_path=db)
        staff_settings.upsert(rid, n, certifications=["food_handler"], db_path=db)
    future = (date.today() + timedelta(days=60)).isoformat()
    _x(db, "INSERT INTO staff_certs (restaurant_id, employee_key, employee_name, cert, expires_on) VALUES "
           "(?, 'ana r', 'Ana R', 'food handler', '2020-01-01')", (rid,))
    _x(db, "INSERT INTO staff_certs (restaurant_id, employee_key, employee_name, cert, expires_on) VALUES "
           "(?, 'ben t', 'Ben T', 'Food-Handler', ?)", (rid, future))
    week = [(date.today() + timedelta(days=i)).isoformat() for i in range(7)]
    c = sr.build_constraints(rid, week, list(sr.DAYS))
    ok_ana, why = c.cert_ok("Ana R", "Bartender")
    assert ok_ana is False and "food_handler" in why
    assert c.cert_ok("Ben T", "Bartender") == (True, "")


def test_a_certificate_expiring_before_next_week_starts_is_not_held_that_week(db):
    import schedule_rules as sr
    import staff_settings
    rid = _rid(db)
    models.update_restaurant(rid, {"role_requirements_json": '{"Bartender": ["alcohol"]}'}, db_path=db)
    models.add_manual_team_member(rid, "Ana R", role="Bartender", db_path=db)
    staff_settings.upsert(rid, "Ana R", certifications=["alcohol"], db_path=db)
    _x(db, "INSERT INTO staff_certs (restaurant_id, employee_key, employee_name, cert, expires_on) VALUES "
           "(?, 'ana r', 'Ana R', 'alcohol', ?)", (rid, (date.today() + timedelta(days=3)).isoformat()))
    this_week = [(date.today() + timedelta(days=i)).isoformat() for i in range(7)]
    later = [(date.today() + timedelta(days=10 + i)).isoformat() for i in range(7)]
    assert sr.build_constraints(rid, this_week, list(sr.DAYS)).cert_ok("Ana R", "Bartender")[0] is True
    assert sr.build_constraints(rid, later, list(sr.DAYS)).cert_ok("Ana R", "Bartender")[0] is False


# ═══ 9. LG-34: the live week by one rule ════════════════════════════════════

def _week(db, rid, start, end, csv="date,day,employee,role,shift_start,shift_end,scheduled_hours,notes\n"):
    return models.save_schedule_history(rid, start, end, 40, 40, 30, csv, [], db_path=db)


def test_a_re_published_older_copy_is_the_live_week(db):
    import staff_schedule
    rid = _rid(db)
    v1 = _week(db, rid, "2026-10-05", "2026-10-11")
    v2 = _week(db, rid, "2026-10-05", "2026-10-11")
    # v2 went out, then the owner put v1 back and re-sent it: v2 is superseded by v1.
    _x(db, "UPDATE schedule_history SET published_at='2026-10-01 09:00:00', superseded_by=? WHERE id=?", (v1, v2))
    _x(db, "UPDATE schedule_history SET published_at='2026-09-30 09:00:00', republished_at='2026-10-01 10:00:00', "
           "superseded_by=NULL WHERE id=?", (v1,))
    weeks = staff_schedule._published_weeks(rid, date(2026, 10, 2))
    assert [w["id"] for w in weeks] == [v1]


def test_a_superseded_copy_with_share_rows_does_not_own_any_day(db):
    import staff_schedule
    rid = _rid(db)
    old = _week(db, rid, "2026-10-05", "2026-10-12")          # a longer, older cut of the week
    new = _week(db, rid, "2026-10-05", "2026-10-11")
    _x(db, "UPDATE schedule_history SET published_at='2026-09-30 09:00:00', superseded_by=? WHERE id=?", (new, old))
    _x(db, "UPDATE schedule_history SET published_at='2026-10-01 09:00:00' WHERE id=?", (new,))
    _x(db, "INSERT INTO schedule_shares (restaurant_id, schedule_id, employee_name, token) "
           "VALUES (?,?, 'Ana R', 'tok-old')", (rid, old))
    weeks = staff_schedule._published_weeks(rid, date(2026, 10, 2))
    assert [w["id"] for w in weeks] == [new]
    assert staff_schedule._owner_of(weeks, date(2026, 10, 12)) is None


def test_a_week_shared_before_the_publish_stamp_still_counts(db):
    import staff_schedule
    rid = _rid(db)
    hid = _week(db, rid, "2026-10-05", "2026-10-11")
    _x(db, "INSERT INTO schedule_shares (restaurant_id, schedule_id, employee_name, token) "
           "VALUES (?,?, 'Ana R', 'tok')", (rid, hid))
    assert [w["id"] for w in staff_schedule._published_weeks(rid, date(2026, 10, 2))] == [hid]


# ═══ 10. a floor section follows a shift that changed hands ═════════════════

CSV_HEAD = "date,day,employee,role,shift_start,shift_end,scheduled_hours,notes\n"


def test_a_studio_swap_saved_over_the_week_carries_the_section(db):
    import schedule_versions as sv
    rid = _rid(db)
    models.set_foh_sections(rid, ["Patio", "Bar"], db_path=db)
    before = CSV_HEAD + ("2026-10-05,Monday,Ana R,Server,5:00pm,10:00pm,5,\n"
                         "2026-10-05,Monday,Cal D,Server,5:00pm,10:00pm,5,\n")
    hid = _week(db, rid, "2026-10-05", "2026-10-11", before)
    models.set_shift_section(rid, "2026-10-05", "Ana R", "5:00pm", "Patio", db_path=db)
    models.set_shift_section(rid, "2026-10-05", "Cal D", "5:00pm", "Bar", db_path=db)
    after = CSV_HEAD + ("2026-10-05,Monday,Ben T,Server,5:00pm,10:00pm,5,(was Ana R)\n"
                        "2026-10-05,Monday,Cal D,Server,5:00pm,10:00pm,5,\n")
    c = get_conn(db)
    c.execute("BEGIN IMMEDIATE")
    sv.write_on(c, rid, hid, "edited", after, saved_by="boss")
    c.commit()
    c.close()
    got = {s["employee"]: s["section"] for s in models.shift_sections_between(rid, "2026-10-05", "2026-10-05", db_path=db)}
    assert got == {"Ben T": "Patio", "Cal D": "Bar"}


def test_two_people_changing_in_one_slot_is_left_alone(db):
    rid = _rid(db)
    models.set_foh_sections(rid, ["Patio"], db_path=db)
    models.set_shift_section(rid, "2026-10-05", "Ana R", "17:00", "Patio", db_path=db)
    row = lambda n: {"date": "2026-10-05", "employee": n, "role": "Server", "shift_start": "5:00pm",  # noqa: E731
                     "shift_end": "10:00pm"}
    c = get_conn(db)
    n = models.carry_shift_sections(c, rid, [row("Ana R"), row("Cal D")], [row("Ben T"), row("Dee E")])
    c.commit()
    c.close()
    assert n == 0
    assert [s["employee"] for s in models.shift_sections_between(rid, "2026-10-05", "2026-10-05", db_path=db)] == ["Ana R"]


# ═══ 11. one helper tells the deciders ═════════════════════════════════════

def test_a_request_waiting_on_an_answer_goes_through_tell_deciders(monkeypatch):
    import people
    import shift_requests as sr
    import strategy_jobs
    got, reached = [], []
    monkeypatch.setattr(people, "tell_deciders", lambda rid, title, body, **k: got.append((title, k)) or 1)
    monkeypatch.setattr(strategy_jobs, "_reach", lambda *a, **k: reached.append((a, k)) or 1)
    sr._tell_managers(1, "Shift drop request", "Ana asked to drop tonight.", "x.db",
                      req={"id": 12, "request_kind": "time_off"})
    assert got == [("Shift drop request", {"request_id": 12, "request_kind": "time_off", "db_path": "x.db"})]
    assert reached == []
    sr._tell_managers(1, "Shifts swapped", "Ana and Ben traded.", "x.db")
    (args, kw), = reached
    assert args[1] == "shift_request" and args[4] == {"tab": "labor"} and "deciders" not in kw


# ═══ 12. the signup token is off the URL ════════════════════════════════════

def test_the_web_signup_sends_its_token_in_a_header():
    page = (ROOT / "templates" / "staff_login.html").read_text(encoding="utf-8")
    assert "'X-Signup-Token': suToken" in page
    assert "?signup_token=" not in page


# ═══ 13. a late report on an issue already open ═════════════════════════════

def test_running_late_after_the_issue_opened_says_so_on_the_issue(db, told):
    import issues
    import staff_comms
    rid = _rid(db)
    uid, mid = _staff(db, rid, "Dana K")
    csv = CSV_HEAD + f"{DAY.isoformat()},Monday,Dana K,Server,11:00am,7:00pm,8,\n"
    hid = _week(db, rid, DAY.isoformat(), DAY.isoformat(), csv)
    _x(db, "UPDATE schedule_history SET published_at=datetime('now') WHERE id=?", (hid,))
    issue, _tok = issues.create_issue(
        rid, "coverage", "Dana K hasn't clocked in",
        detail="Scheduled 11:00am as Server — 15 minutes ago, with no clock-in on the POS. Ben could cover.",
        severity="high", source_key=f"coverage:{DAY.isoformat()}:dana k", notify=False, db_path=db)
    staff_comms.report_late(rid, {"id": mid, "user_id": uid, "employee_name": "Dana K"}, DAY.isoformat(),
                            "11:00am", 20, now_local=NOON_ISH, db_path=db)
    detail = issues.get_issue(rid, issue["id"], db_path=db)["detail"]
    assert "with no clock-in on the POS. They said at 10:50am they'd be about 20 minutes late. Ben could cover." \
        in detail or ("about 20 minutes late" in detail and detail.endswith("Ben could cover."))
    # A second report replaces the ETA; it does not stack.
    staff_comms.report_late(rid, {"id": mid, "user_id": uid, "employee_name": "Dana K"}, DAY.isoformat(),
                            "11:00am", 45, now_local=NOON_ISH, db_path=db)
    detail = issues.get_issue(rid, issue["id"], db_path=db)["detail"]
    assert "about 45 minutes late" in detail and "about 20 minutes late" not in detail
    assert detail.count("They said") == 1
