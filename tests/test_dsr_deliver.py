"""dsr.deliver — who hears about a night's DSR, what they are sent, and that
nobody is ever told twice.

Simple EJ's-shaped: Chicago, open 11am–11pm. Business date Tuesday 9/22/26
closes at 11pm CDT = 04:00 UTC on 9/23. Times below are UTC.

The outside edges are faked: Resend (requests.post, recorded) and APNs
(push.fire_push, recorded). No test sends anything real.
"""
import json
import sys
import types
from datetime import date, datetime

import pytest

import auth
import emails
import models
import ops
import pos
import push
import requests
import scheduler
import dsr
from dsr import access, deliver, pipeline, store
from models import Restaurant, create_restaurant, update_restaurant, get_restaurant

DAY = date(2026, 9, 22)
DAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")
AT_CLOSE = datetime(2026, 9, 23, 4, 10)        # 11:10pm CDT
MORNING = datetime(2026, 9, 23, 13, 0)          # 8:00am CDT


@pytest.fixture
def db(db_path, monkeypatch):
    real = models.get_conn

    def conn(*a, **k):
        return real(db_path)
    for mod in list(sys.modules.values()):
        if mod is not None and getattr(mod, "get_conn", None) is real:
            monkeypatch.setattr(mod, "get_conn", conn)
    monkeypatch.setattr(models, "get_conn", conn)
    monkeypatch.setattr(models, "DB_PATH", db_path)
    monkeypatch.setattr(auth, "DB_PATH", db_path)
    monkeypatch.setattr(pos, "PROVIDERS", None)
    auth.init_auth(db_path=db_path)
    push.init_push(db_path)
    ops.init_ops(db_path)
    monkeypatch.setattr(scheduler, "scheduling_allowed", lambda: True)
    monkeypatch.setenv("BASE_URL", "https://dashboard.cavnar.ai")
    return db_path


@pytest.fixture
def sent(monkeypatch):
    """Resend and APNs, recorded instead of reached."""
    out = {"emails": [], "pushes": []}

    class _Resp:
        status_code = 200
        text = ""

        def json(self):
            return {"id": f"m{len(out['emails'])}"}

    def post(url, json=None, **kw):
        assert "api.resend.com" in url, url
        assert kw.get("timeout"), "every outbound call names a timeout"
        out["emails"].append(json)
        return _Resp()
    monkeypatch.setattr(emails, "_resend_key", lambda: "re_test")
    monkeypatch.setattr(requests, "post", post)

    def fire(rid, alert_type, title, body, data=None, db_path=None, user_ids=None):
        out["pushes"].append({"rid": rid, "type": alert_type, "title": title, "body": body,
                              "data": dict(data or {}), "user_ids": set(user_ids or ())})
    monkeypatch.setattr(push, "fire_push", fire)
    return out


def _restaurant(db, **fields):
    rid = create_restaurant(Restaurant(name="Simple EJ's", owner_email="erik@example.com",
                                       timezone="America/Chicago"), db_path=db)
    # Delivery is off by default; these restaurants have it on unless a test
    # says otherwise (test_delivery_is_off_until_the_owner_turns_it_on).
    fields = dict({"dsr_notify": 1}, **fields)
    update_restaurant(rid, {"open_times_json": json.dumps({d: "11:00am" for d in DAYS}),
                            "close_times_json": json.dumps({d: "11:00pm" for d in DAYS}), **fields}, db_path=db)
    return get_restaurant(rid, db_path=db)


def _people(db, rid, devices=True):
    ids = {
        "erik": auth.create_user(rid, "erik", "erik@example.com", "Passw0rd!long", db_path=db, role="client"),
        "jim": auth.create_user(rid, "jim", "jim@example.com", "Passw0rd!long", db_path=db, role="owner"),
        "maria": auth.create_user(rid, "maria", "maria@example.com", "Passw0rd!long", db_path=db, role="manager"),
        "cook": auth.create_user(rid, "cook", "cook@staff.invalid", "Passw0rd!long", db_path=db, role="employee"),
        "support": auth.create_user(rid, "sup", "support@cavnar.ai", "Passw0rd!long", db_path=db, role="support"),
        "will": auth.create_user(rid, "will", "will@cavnar.ai", "Passw0rd!long", is_admin=True, db_path=db),
    }
    if devices:
        for who in ids:
            push.register_device_token(ids[who], rid, f"tok-{who}", db_path=db)
    return ids


SALES = {"gross": 7415.0, "net": 6975.0, "transactions": 212, "comps": 180.0, "voids": 95.0,
         "last_week_net": 7110.0, "vs_last_week": -135.0, "vs_last_week_pct": -1.9,
         "last_year_net": 6720.0, "vs_last_year": 255.0, "vs_last_year_pct": 3.8,
         "budget_net": 7300.0, "vs_budget_net": -325.0, "vs_budget_net_pct": -4.5}
LABOR = {"cost": 1912.0, "pct": 27.4, "target_pct": 26.0, "vs_target_pts": 1.4, "hours": 124.5}
NARRATIVE = {
    "executive_summary": {"text": "Net sales were $6,975, 4.5% under budget. Labor ran 27.4%.",
                          "cites": ["sales.net", "sales.vs_budget_net_pct", "labor.pct"]},
    "went_well": [{"text": "Net sales topped last year by 3.8%.", "cites": ["sales.vs_last_year_pct"]}],
    "needs_attention": [{"text": "Labor ran 27.4%, 1.4 points over target.", "cites": ["labor.pct", "labor.vs_target_pts"]},
                        {"text": "Comps reached $180 tonight.", "cites": ["sales.comps"]},
                        {"text": "Food variance was $176.", "cites": ["food.variance_cost"]}],
    "actions_tomorrow": [{"text": "Cut one server from the Wednesday close.", "why": "Labor ran 27.4% against a 26% target.",
                          "cites": ["labor.pct"]}],
}


def _report(db, r, status="final", sales=True):
    """A terminal version written straight through dsr.store — the pipeline
    has its own tests; these are about who is told."""
    rep = store.create_report(r.id, DAY, trigger="sweep", db_path=db)
    if sales:
        store.save_block(rep["id"], "sales", dsr.block(dsr.READY, source="rpower", metrics=SALES), db_path=db)
    else:
        store.save_block(rep["id"], "sales", dsr.block(dsr.AWAITING, block_name="sales"), db_path=db)
    store.save_block(rep["id"], "labor", dsr.block(dsr.READY, source="rpower", metrics=LABOR), db_path=db)
    store.save_block(rep["id"], "food", dsr.block(dsr.READY, source="cavnar", metrics={"variance_cost": 176.4}),
                     db_path=db)
    store.save_fiscal(rep["id"], {"label": "Period 9 · Week 4"}, db_path=db)
    if sales:
        store.save_narrative(rep["id"], NARRATIVE, db_path=db)
        store.note(rep["id"], "narrative", {"status": "written", "reason": None}, db_path=db)
    else:
        store.save_narrative(rep["id"], None, db_path=db)
        store.note(rep["id"], "narrative", {"status": "skipped", "reason": pipeline.NO_SUMMARY_SALES_PENDING},
                   db_path=db)
    store.set_stage(rep["id"], status, db_path=db)
    return store.get_report_by_id(rep["id"], db_path=db)


def _rows(db):
    c = models.get_conn(db)
    try:
        return [dict(x) for x in c.execute("SELECT * FROM dsr_deliveries ORDER BY id").fetchall()]
    finally:
        c.close()


def _to(mail):
    return mail["to"][0]


# ── who ─────────────────────────────────────────────────────────────────────

def test_every_owner_gets_the_owner_view_every_manager_the_manager_view_nobody_else(db):
    r = _restaurant(db)
    ids = _people(db, r.id)
    got = {u["id"]: u["view"] for u in deliver.recipients(r.id, db)}
    assert got == {ids["erik"]: "owner", ids["jim"]: "owner", ids["maria"]: "manager"}


def test_a_group_owner_whose_login_lives_on_another_location_is_an_owner_here(db):
    base = _restaurant(db, location_group="EJ Group")
    other = _restaurant(db, location_group="EJ Group")
    boss = auth.create_user(base.id, "boss", "boss@example.com", "Passw0rd!long", db_path=db, role="owner")
    assert {u["id"]: u["view"] for u in deliver.recipients(other.id, db)} == {boss: "owner"}


def test_an_inactive_login_hears_nothing(db):
    r = _restaurant(db)
    ids = _people(db, r.id)
    c = models.get_conn(db)
    c.execute("UPDATE users SET is_active=0 WHERE id=?", (ids["jim"],))
    c.commit()
    c.close()
    assert ids["jim"] not in {u["id"] for u in deliver.recipients(r.id, db)}


# ── what they are sent ──────────────────────────────────────────────────────

def test_the_owner_and_manager_emails_carry_exactly_their_own_views(db, sent):
    r = _restaurant(db)
    ids = _people(db, r.id)
    rep = _report(db, r)
    out = deliver.on_terminal(r, rep["id"], now_utc=AT_CLOSE, db_path=db)
    assert out["email"] == 3 and out["push"] == 3
    by = {_to(m): m for m in sent["emails"]}
    assert set(by) == {"erik@example.com", "jim@example.com", "maria@example.com"}

    owner, manager = by["erik@example.com"], by["maria@example.com"]
    # The owner's leads with "did we win today?" (dsr.scorecard, 9/25/26 —
    # this pinned the plain subject both views used to share).
    assert owner["subject"].startswith("Simple EJ's · Tue 9/22/26 · ") and owner["subject"].endswith("/100 · $6,975 net")
    assert manager["subject"] == "Simple EJ's · Tue 9/22/26 · $6,975 net"
    link = "https://dashboard.cavnar.ai/#dsr/2026-09-22"
    for m in (owner, manager):
        assert link in m["html"] and "View full report" in m["html"]
        assert "2026-09-22<" not in m["html"], "no ISO date an owner reads"
        assert "Period 9 · Week 4" in m["html"] and "Tuesday 9/22/26" in m["html"]

    # The owner: budget, comps and food, and the lead that cites the budget.
    assert "vs budget" in owner["html"] and "4.5%" in owner["html"]
    assert "Comps reached $180" in owner["html"] and "Food variance was $176" in owner["html"]
    assert "Net sales were $6,975, 4.5% under budget." in owner["html"]
    assert "Daily report" in owner["html"]
    # The manager: exactly the manager payload — no budget, no loss lines,
    # no food cost, and not the lead that cited the budget.
    for gone in ("budget", "Comps reached", "Food variance", "under budget", "7,300"):
        assert gone not in manager["html"], gone
    assert "Manager report" in manager["html"]
    assert "Labor ran 27.4%, 1.4 points over target." in manager["html"]
    assert "Cut one server from the Wednesday close." in manager["html"]
    assert "$6,975" in manager["html"] and "vs last Tue" in manager["html"]


def test_the_email_is_rendered_from_access_render_never_rederived(db, sent, monkeypatch):
    r = _restaurant(db)
    _people(db, r.id, devices=False)
    rep = _report(db, r)
    seen = []
    real = access.render

    def spy(report, user, restaurant=None, versions=None):
        seen.append(access.view_for(user))
        return real(report, user, restaurant, versions)
    monkeypatch.setattr(access, "render", spy)
    deliver.on_terminal(r, rep["id"], now_utc=AT_CLOSE, db_path=db)
    assert sorted(seen) == ["manager", "owner", "owner"]


def test_the_push_payload_is_what_the_app_opens_the_report_from(db, sent):
    r = _restaurant(db)
    ids = _people(db, r.id)
    rep = _report(db, r)
    deliver.on_terminal(r, rep["id"], now_utc=AT_CLOSE, db_path=db)
    assert len(sent["pushes"]) == 3
    by = {next(iter(p["user_ids"])): p for p in sent["pushes"]}
    assert set(by) == {ids["erik"], ids["jim"], ids["maria"]}, "one push per person, to their own devices"
    p = by[ids["erik"]]
    assert p["type"] == "dsr" and p["data"]["type"] == "dsr" and p["data"]["business_date"] == "2026-09-22"
    assert p["title"] == "Your daily report is ready"
    # The owner's push leads with the score (dsr.scorecard; this pinned the plain figures line).
    assert p["body"].startswith("Simple EJ's · Tue 9/22/26: ") and "/100 · $6,975 net, −$325 vs budget." in p["body"]
    assert len(p["body"]) <= deliver.PUSH_BODY_MAX
    m = by[ids["maria"]]
    assert m["title"] == "Your manager report is ready" and "budget" not in m["body"]
    # One history row for the night, and its id rides every push.
    c = models.get_conn(db)
    rows = c.execute("SELECT id FROM alert_log WHERE restaurant_id=? AND alert_type='dsr'", (r.id,)).fetchall()
    c.close()
    assert len(rows) == 1 and all(x["data"]["alert_id"] == rows[0]["id"] for x in sent["pushes"])


def test_the_real_push_builder_puts_type_and_business_date_in_the_payload(db, monkeypatch):
    """Through push.fire_push itself: the `cavnar` dict the phone receives."""
    r = _restaurant(db)
    ids = _people(db, r.id)
    payloads = []

    def fake_deliver(row, alert_type, title, body, data, db_path=None):
        payloads.append((row["user_id"], alert_type, data))
    monkeypatch.setattr(push, "_deliver", fake_deliver)

    class _Now:
        def submit(self, fn, *a):
            fn(*a)
    monkeypatch.setattr(push, "_push_executor", lambda: _Now())
    push.fire_push(r.id, "dsr", "t", "b", data=deliver.push_data("2026-09-22", "first", 1, alert_id=9),
                   db_path=db, user_ids={ids["erik"]})
    assert len(payloads) == 1
    uid, t, data = payloads[0]
    assert uid == ids["erik"] and t == "dsr"
    assert data["type"] == "dsr" and data["business_date"] == "2026-09-22"
    assert data["restaurant_id"] == r.id and data["module"] == "home"


# ── exactly once ────────────────────────────────────────────────────────────

def test_a_repeat_call_a_retry_or_a_rerun_never_sends_twice(db, sent):
    r = _restaurant(db)
    _people(db, r.id)
    rep = _report(db, r)
    for _ in range(3):
        deliver.on_terminal(r, rep["id"], now_utc=AT_CLOSE, db_path=db)
    # The owner re-runs a final night: a new final version tells nobody.
    rerun = _report(db, r)
    deliver.on_terminal(r, rerun["id"], now_utc=AT_CLOSE, db_path=db)
    assert len(sent["emails"]) == 3 and len(sent["pushes"]) == 3
    assert {(x["channel"], x["kind"]) for x in _rows(db)} == {("email", "first"), ("push", "first"),
                                                              ("history", "first")}


def test_a_provisional_night_gets_one_update_when_sales_land_and_never_more(db, sent):
    r = _restaurant(db)
    ids = _people(db, r.id)
    v1 = _report(db, r, status="provisional", sales=False)
    deliver.on_terminal(r, v1["id"], now_utc=datetime(2026, 9, 23, 9, 5), db_path=db)
    first = {_to(m): m for m in sent["emails"]}
    assert first["erik@example.com"]["subject"] == "Simple EJ's · Tue 9/22/26 · Provisional, sales still syncing"
    html = first["erik@example.com"]["html"]
    assert "Provisional" in html and "Awaiting POS synchronization" in html
    assert pipeline.NO_SUMMARY_SALES_PENDING in html
    p = next(x for x in sent["pushes"] if ids["erik"] in x["user_ids"])
    assert p["title"] == "Your daily report is ready — provisional" and "still syncing" in p["body"]

    # Another provisional version (a re-run before sales land): nothing.
    v1b = _report(db, r, status="provisional", sales=False)
    deliver.on_terminal(r, v1b["id"], now_utc=datetime(2026, 9, 23, 10, 5), db_path=db)
    assert len(sent["emails"]) == 3 and len(sent["pushes"]) == 3

    v2 = _report(db, r, status="final")
    deliver.on_terminal(r, v2["id"], now_utc=datetime(2026, 9, 23, 15, 10), db_path=db)
    upd = [m for m in sent["emails"][3:]]
    assert len(upd) == 3
    assert {m["subject"] for m in upd} == {"Updated · Simple EJ's · Tue 9/22/26 · $6,975 net"}
    assert "Sales are now in" in upd[0]["html"]
    pu = [x for x in sent["pushes"][3:]]
    assert len(pu) == 3 and {x["title"] for x in pu} == {"Sales are now in — updated report"}
    assert {x["data"]["kind"] for x in pu} == {"updated"}

    # A later final version, a repeat of the hook: never a second update.
    v3 = _report(db, r, status="final")
    deliver.on_terminal(r, v3["id"], now_utc=datetime(2026, 9, 23, 16, 0), db_path=db)
    deliver.on_terminal(r, v2["id"], now_utc=datetime(2026, 9, 23, 16, 0), db_path=db)
    assert len(sent["emails"]) == 6 and len(sent["pushes"]) == 6


def test_a_night_that_went_out_final_never_gets_an_update(db, sent):
    r = _restaurant(db)
    _people(db, r.id)
    deliver.on_terminal(r, _report(db, r)["id"], now_utc=AT_CLOSE, db_path=db)
    deliver.on_terminal(r, _report(db, r)["id"], now_utc=MORNING, db_path=db)
    assert not [m for m in sent["emails"] if m["subject"].startswith("Updated")]


def test_a_failed_night_notifies_nobody(db, sent):
    r = _restaurant(db)
    _people(db, r.id)
    rep = _report(db, r, status="failed")
    out = deliver.on_terminal(r, rep["id"], now_utc=AT_CLOSE, db_path=db)
    assert out["email"] == out["push"] == 0
    assert sent["emails"] == [] and sent["pushes"] == [] and _rows(db) == []


def test_a_local_backend_never_sends(db, sent, monkeypatch):
    monkeypatch.setattr(scheduler, "scheduling_allowed", lambda: False)
    r = _restaurant(db)
    _people(db, r.id)
    deliver.on_terminal(r, _report(db, r)["id"], now_utc=AT_CLOSE, db_path=db)
    assert sent["emails"] == [] and sent["pushes"] == [] and _rows(db) == []


def test_an_old_night_rerun_announces_nothing(db, sent):
    r = _restaurant(db)
    _people(db, r.id)
    deliver.on_terminal(r, _report(db, r)["id"], now_utc=datetime(2026, 9, 30, 4, 0), db_path=db)
    assert sent["emails"] == [] and sent["pushes"] == []


# ── suppression ─────────────────────────────────────────────────────────────

def test_a_suppressed_address_is_not_mailed_and_is_not_retried(db, sent):
    r = _restaurant(db)
    _people(db, r.id, devices=False)
    models.suppress_email("jim@example.com", "bounce", db_path=db)
    rep = _report(db, r)
    deliver.on_terminal(r, rep["id"], now_utc=AT_CLOSE, db_path=db)
    deliver.on_terminal(r, rep["id"], now_utc=AT_CLOSE, db_path=db)
    assert sorted(_to(m) for m in sent["emails"]) == ["erik@example.com", "maria@example.com"]
    row = next(x for x in _rows(db) if x["channel"] == "email" and x["status"] == "skipped")
    assert "suppressed" in row["detail"]
    c = models.get_conn(db)
    logged = c.execute("SELECT to_email, status FROM email_log WHERE email_type='send_dsr_email' ORDER BY id").fetchall()
    c.close()
    assert [(x["to_email"], x["status"]) for x in logged].count(("erik@example.com", "sent")) == 1


# ── quiet hours ─────────────────────────────────────────────────────────────

def test_quiet_until_reads_the_restaurants_own_clock():
    r = types.SimpleNamespace(timezone="America/Chicago", alert_quiet_start="22:00", alert_quiet_end="07:00")
    assert deliver.quiet_until(r, AT_CLOSE) == datetime(2026, 9, 23, 12, 0)       # 11:10pm → 7am CDT
    assert deliver.quiet_until(r, datetime(2026, 9, 23, 9, 0)) == datetime(2026, 9, 23, 12, 0)   # 4am
    assert deliver.quiet_until(r, MORNING) is None                                  # 8am
    day = types.SimpleNamespace(timezone="America/Chicago", alert_quiet_start="13:00", alert_quiet_end="15:00")
    assert deliver.quiet_until(day, datetime(2026, 9, 23, 18, 30)) == datetime(2026, 9, 23, 20, 0)
    for s, e in ((None, "07:00"), ("22:00", None), ("22:00", "22:00"), ("junk", "07:00")):
        assert deliver.quiet_until(types.SimpleNamespace(timezone="America/Chicago", alert_quiet_start=s,
                                                         alert_quiet_end=e), AT_CLOSE) is None


def test_quiet_hours_hold_the_push_not_the_email_and_release_it_once(db, sent):
    r = _restaurant(db, alert_quiet_start="22:00", alert_quiet_end="07:00")
    _people(db, r.id)
    out = deliver.on_terminal(r, _report(db, r)["id"], now_utc=AT_CLOSE, db_path=db)
    assert out["email"] == 3 and out["push"] == 0 and out["held"] == 3
    assert len(sent["emails"]) == 3 and sent["pushes"] == []
    held = [x for x in _rows(db) if x["channel"] == "push"]
    assert {x["status"] for x in held} == {"held"} and {x["hold_until"] for x in held} == {"2026-09-23 12:00:00"}

    assert deliver.release_held(now_utc=datetime(2026, 9, 23, 11, 50), db_path=db)["released"] == 0
    assert sent["pushes"] == []
    assert deliver.release_held(now_utc=datetime(2026, 9, 23, 12, 5), db_path=db)["released"] == 3
    assert len(sent["pushes"]) == 3 and {p["title"] for p in sent["pushes"]} == {
        "Your daily report is ready", "Your manager report is ready"}
    # A second pass, a repeat of the hook: nothing more.
    assert deliver.release_held(now_utc=datetime(2026, 9, 23, 12, 15), db_path=db)["released"] == 0
    deliver.on_terminal(r, store.get_report(r.id, DAY, db_path=db)["id"], now_utc=MORNING, db_path=db)
    assert len(sent["pushes"]) == 3 and len(sent["emails"]) == 3


def test_a_push_held_when_the_final_version_lands_goes_out_once_from_the_final(db, sent):
    r = _restaurant(db, alert_quiet_start="22:00", alert_quiet_end="07:00")
    ids = _people(db, r.id)
    v1 = _report(db, r, status="provisional", sales=False)
    deliver.on_terminal(r, v1["id"], now_utc=datetime(2026, 9, 23, 9, 5), db_path=db)     # 4:05am, quiet
    v2 = _report(db, r, status="final")
    deliver.on_terminal(r, v2["id"], now_utc=datetime(2026, 9, 23, 10, 5), db_path=db)    # 5:05am, quiet
    # Email: the provisional notice, then the one update.
    assert [m["subject"].startswith("Updated") for m in sent["emails"] if _to(m) == "erik@example.com"] == [False, True]
    assert sent["pushes"] == []
    assert not [x for x in _rows(db) if x["channel"] == "push" and x["kind"] == "updated"]
    deliver.release_held(now_utc=datetime(2026, 9, 23, 12, 5), db_path=db)
    mine = [p for p in sent["pushes"] if ids["erik"] in p["user_ids"]]
    assert len(mine) == 1 and mine[0]["title"] == "Your daily report is ready"
    assert "$6,975 net" in mine[0]["body"] and mine[0]["data"]["version"] == 2
    # And no "Updated" push follows it later.
    deliver.on_terminal(r, v2["id"], now_utc=MORNING, db_path=db)
    assert len([p for p in sent["pushes"] if ids["erik"] in p["user_ids"]]) == 1


def test_a_held_push_is_not_released_after_notices_are_switched_off(db, sent):
    # D2-9: on_terminal checked dsr_notify; the 7am release did not.
    r = _restaurant(db, alert_quiet_start="22:00", alert_quiet_end="07:00")
    _people(db, r.id)
    deliver.on_terminal(r, _report(db, r)["id"], now_utc=AT_CLOSE, db_path=db)
    update_restaurant(r.id, {"dsr_notify": 0}, db_path=db)
    out = deliver.release_held(now_utc=datetime(2026, 9, 23, 12, 5), db_path=db)
    assert out["released"] == 0 and sent["pushes"] == []
    assert {x["status"] for x in _rows(db) if x["channel"] == "push"} == {"skipped"}


def test_an_update_to_someone_whose_first_notice_never_arrived_is_the_first_notice(db, sent, monkeypatch):
    # D2-10: Resend refused the provisional first email; the final version
    # then sent "Updated … went out provisional while sales were syncing" to
    # someone who had never been told anything.
    r = _restaurant(db)
    _people(db, r.id, devices=False)
    v1 = _report(db, r, status="provisional", sales=False)
    recording = requests.post                     # the `sent` fixture's recorder

    class _No:
        status_code = 422
        text = "refused"

        def json(self):
            return {"message": "refused"}
    monkeypatch.setattr(requests, "post", lambda url, json=None, **kw: _No())
    deliver.on_terminal(r, v1["id"], now_utc=datetime(2026, 9, 23, 9, 5), db_path=db)
    assert {x["status"] for x in _rows(db) if x["channel"] == "email"} == {"failed"}
    assert sent["emails"] == []
    monkeypatch.setattr(requests, "post", recording)
    v2 = _report(db, r, status="final")
    deliver.on_terminal(r, v2["id"], now_utc=datetime(2026, 9, 23, 15, 10), db_path=db)
    subjects = {m["subject"] for m in sent["emails"]}
    assert subjects and not [s for s in subjects if s.startswith("Updated")]
    assert all("$6,975" in s for s in subjects)


def test_a_login_removed_overnight_is_not_pushed_at_release(db, sent):
    r = _restaurant(db, alert_quiet_start="22:00", alert_quiet_end="07:00")
    ids = _people(db, r.id)
    deliver.on_terminal(r, _report(db, r)["id"], now_utc=AT_CLOSE, db_path=db)
    c = models.get_conn(db)
    c.execute("UPDATE users SET is_active=0 WHERE id=?", (ids["jim"],))
    c.commit()
    c.close()
    deliver.release_held(now_utc=datetime(2026, 9, 23, 12, 5), db_path=db)
    assert ids["jim"] not in set().union(*(p["user_ids"] for p in sent["pushes"]))
    assert len(sent["pushes"]) == 2


# ── the pipeline hook ───────────────────────────────────────────────────────

@pytest.fixture
def world(db, monkeypatch):
    monkeypatch.setattr(pos, "connected_provider", lambda rid: ("fakepos", object()))
    monkeypatch.setattr(pos, "fetch_day_closed", lambda rid, day: (True, "fakepos"))
    sales = {"gross": 2100.0, "net": 2000.0, "transactions": 80, "guests": 120, "discounts": 60.0, "comps": 40.0,
             "voids": 0.0, "refunds": 0.0, "tax": 160.0, "by_department": {"Food": 2000.0},
             "by_hour": {"12": 800.0, "19": 1200.0}, "items": [], "net_deductions": ["discounts", "comps"],
             "source_checks": {}}
    monkeypatch.setattr(pos, "fetch_day_sales", lambda rid, day: (dict(sales), "fakepos"))
    for name in ("food", "reviews", "marketing", "intel", "closeout"):
        mod = types.ModuleType(f"dsr.block_{name}")
        mod.collect = lambda ctx: dsr.block(dsr.READY, source="fake", metrics={"n": 1})
        monkeypatch.setitem(sys.modules, f"dsr.block_{name}", mod)
    narrative = types.ModuleType("dsr.narrative")
    narrative.write = lambda ctx, facts: {"ok": True, "narrative": {
        "executive_summary": {"text": "A good night.", "cites": ["sales.net"]}}, "reason": None}
    monkeypatch.setitem(sys.modules, "dsr.narrative", narrative)

    def labor_in(rid):
        c = models.get_conn(db)
        c.execute("INSERT INTO labor_daily_history (restaurant_id, date, day_of_week, labor_pct, labor_cost, sales, "
                  "total_hours) VALUES (?,?,?,?,?,?,?)", (rid, DAY.isoformat(), "Tuesday", 22.0, 440.0, 2000.0, 32.0))
        c.commit()
        c.close()
    return labor_in


def test_the_pipeline_tells_people_when_the_night_is_final(db, world, sent):
    r = _restaurant(db)
    world(r.id)
    _people(db, r.id, devices=False)
    out = pipeline.run_night(r, DAY, pipeline.TRIGGER_SWEEP, now_utc=AT_CLOSE, db_path=db)
    assert out["action"] == "final"
    assert sorted(_to(m) for m in sent["emails"]) == ["erik@example.com", "jim@example.com", "maria@example.com"]
    assert "A good night." in sent["emails"][0]["html"]


def test_a_delivery_failure_never_fails_or_rolls_back_the_report(db, world, sent, monkeypatch):
    r = _restaurant(db)
    world(r.id)
    _people(db, r.id)

    def boom(*a, **k):
        raise RuntimeError("resend exploded")
    monkeypatch.setattr(deliver, "on_terminal", boom)
    out = pipeline.run_night(r, DAY, pipeline.TRIGGER_SWEEP, now_utc=AT_CLOSE, db_path=db)
    assert out["action"] == "final" and store.get_report(r.id, DAY, db_path=db)["status"] == "final"
    c = models.get_conn(db)
    jobs = [x["job"] for x in c.execute("SELECT job FROM job_failures").fetchall()]
    c.close()
    assert "dsr_deliver" in jobs


def test_one_persons_failed_send_does_not_stop_the_others(db, sent, monkeypatch):
    r = _restaurant(db)
    ids = _people(db, r.id, devices=False)
    real = emails.send_dsr_email

    def flaky(to_email, d, restaurant_id=None):
        if to_email == "jim@example.com":
            raise RuntimeError("template bug")
        return real(to_email, d, restaurant_id=restaurant_id)
    monkeypatch.setattr(emails, "send_dsr_email", flaky)
    out = deliver.on_terminal(r, _report(db, r)["id"], now_utc=AT_CLOSE, db_path=db)
    assert out["email"] == 2 and out["failed"] == 1
    row = next(x for x in _rows(db) if x["user_id"] == ids["jim"])
    assert row["status"] == "failed" and "template bug" in row["detail"]


def test_a_night_the_pipeline_fails_tells_nobody(db, world, sent, monkeypatch):
    r = _restaurant(db)
    _people(db, r.id)

    def broken(*a, **k):
        raise RuntimeError("fiscal calendar broken")
    monkeypatch.setattr(pipeline, "_save_fiscal", broken)
    for i in range(pipeline.MAX_FAILURES + 1):
        pipeline.run_night(r, DAY, pipeline.TRIGGER_SWEEP, now_utc=datetime(2026, 9, 23, 4 + i, 10), db_path=db)
    assert store.get_report(r.id, DAY, db_path=db)["status"] == "failed"
    assert sent["emails"] == [] and sent["pushes"] == [] and _rows(db) == []
    c = models.get_conn(db)
    assert c.execute("SELECT COUNT(*) FROM job_failures WHERE job='dsr'").fetchone()[0] >= 1, \
        "the admin console hears about it"
    c.close()


# ── the closing summary it replaces ─────────────────────────────────────────

def test_the_dsr_replaces_the_closing_summary_only_where_it_runs(db, monkeypatch):
    on = _restaurant(db)
    off = _restaurant(db, dsr_enabled=0)
    no_pos = _restaurant(db)
    monkeypatch.setattr(pos, "connected_provider",
                        lambda rid: (None, None) if rid == no_pos.id else ("rpower", object()))
    assert deliver.replaces_closing_summary(on) is True
    assert deliver.replaces_closing_summary(off) is False
    assert deliver.replaces_closing_summary(no_pos) is False
    # With delivery off nothing announces the night, so the old push stays.
    assert deliver.replaces_closing_summary(_restaurant(db, dsr_notify=0)) is False


def test_delivery_is_off_until_the_owner_turns_it_on(db, monkeypatch):
    assert Restaurant(name="x", owner_email="x@x.com").dsr_notify == 0
    rid = create_restaurant(Restaurant(name="New Co", owner_email="n@x.com"), db_path=db)
    assert get_restaurant(rid, db_path=db).dsr_notify == 0
    update_restaurant(rid, {"dsr_notify": 1}, db_path=db)
    assert get_restaurant(rid, db_path=db).dsr_notify == 1       # the whitelist lets it through
    r = _restaurant(db, dsr_notify=0)
    monkeypatch.setattr(deliver, "_allowed", lambda: True)
    rep = store.create_report(r.id, "2026-09-22", trigger="sweep", db_path=db)
    store.set_stage(rep["id"], "collecting", db_path=db)
    store.set_stage(rep["id"], "final", db_path=db)
    out = deliver.on_terminal(r, rep["id"], now_utc=datetime(2026, 9, 23, 4, 30), db_path=db)
    assert out["reason"] == "delivery is off for this restaurant" and out["email"] == out["push"] == 0


def test_run_closing_summary_stays_quiet_for_a_dsr_restaurant(db, monkeypatch):
    import intraday
    import strategy_jobs
    import time_utils
    on = _restaurant(db)
    off = _restaurant(db, dsr_enabled=0)
    monkeypatch.setattr(pos, "connected_provider", lambda rid: ("rpower", object()))
    monkeypatch.setattr(time_utils, "restaurant_now", lambda *a, **k: datetime(2026, 9, 22, 23, 10))
    monkeypatch.setattr(intraday, "capture", lambda *a, **k: {"ok": False})
    monkeypatch.setattr(intraday, "closing_summary",
                        lambda rid, day=None, **k: {"available": False, "net_sales": 5000.0, "hour": 22, "day": day})
    reached = []
    monkeypatch.setattr(strategy_jobs, "_reach", lambda rid, *a, **k: reached.append(rid) or 1)
    strategy_jobs.run_closing_summary(db_path=db)
    assert reached == [off.id]


# ── the registries a new alert type must reach ──────────────────────────────

def test_dsr_is_registered_everywhere_a_notification_type_must_be():
    import admin_ops
    import client_api
    import inspect
    import notify
    assert push.PRIORITY["dsr"] == push.P4_SUMMARY and push.NOTIFICATION_MODULE["dsr"] == "home"
    assert "dsr" in models.NON_ALERT_TYPES and "dsr" in notify.BRIEFING_ALWAYS
    assert client_api._NOTIFICATION_LABELS["dsr"] == "Daily report ready"
    assert admin_ops.RUNNABLE_JOBS["dsr_delivery"]["target"] == ("dsr.deliver", "release_held")
    assert admin_ops.RUNNABLE_JOBS["dsr_delivery"]["sends"] is True
    src = inspect.getsource(scheduler)
    assert 'run_job("dsr_delivery", release_held)' in src


def test_the_deliveries_table_is_created_at_boot(db_path):
    c = models.get_conn(db_path)
    cols = {x["name"] for x in c.execute("PRAGMA table_info(dsr_deliveries)").fetchall()}
    c.close()
    assert {"restaurant_id", "business_date", "user_id", "channel", "kind", "status", "hold_until"} <= cols
