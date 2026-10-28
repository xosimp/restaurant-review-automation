"""Admin audit #26 (10/27/26), fix round 2, stream BE — the backend half of
the round's shared contract and the remaining performance, wiring and test
findings. One regression test (or a small group) per fixed defect, each on
the fixture's throwaway database. Finding IDs are the audit reports'
(06-performance P-*, 07-ops-code O*/C*, 04-workflows F*, 02-wiring W*,
05-security S*).

Stripe, Twilio, Resend, APNs, RPOWER and the model are never reached.
"""
import json
from datetime import timedelta

import pytest

import admin_ops
import models
from tests.clock import restaurant_now, utc_now

from test_fix_int2_admin import CSRF, _admin, _hq, _owner, _post, _rid, _rows, _world, app  # noqa: F401
from test_admin_audit_1027_ws2 import _support


def _x(db_path, sql, args=()):
    conn = models.get_conn(db_path)
    try:
        conn.execute(sql, args)
        conn.commit()
    finally:
        conn.close()


def _many(db_path, sql, rows):
    conn = models.get_conn(db_path)
    try:
        conn.executemany(sql, rows)
        conn.commit()
    finally:
        conn.close()


def _utc(dt):
    """An aware instant as the UTC space form the ledgers store."""
    from datetime import timezone
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def _ct_midnight(days_back=0):
    m = restaurant_now().replace(hour=0, minute=0, second=0, microsecond=0)
    return m - timedelta(days=days_back)


def _ct_day(days_back=0):
    return (restaurant_now().date() - timedelta(days=days_back)).isoformat()


# ── O6 / P-11 contract 1: jobs().failures_daily ─────────────────────────────

def test_o6_job_failures_a_day_are_counted_by_the_database_on_central_days(db_path):
    rows = [("pos_sync", "boom", None, _utc(_ct_midnight() + timedelta(minutes=30)), "job"),
            # Half an hour before Chicago midnight: yesterday in Chicago, even
            # where it is already today in UTC.
            ("pos_sync", "boom", None, _utc(_ct_midnight() - timedelta(minutes=30)), "job"),
            ("brief", "boom", None, _utc(_ct_midnight(3) + timedelta(hours=12)), "job"),
            ("brief", "boom", None, _utc(_ct_midnight(3) + timedelta(hours=13)), ""),
            # An AI finding and a console request error are not failed jobs.
            ("drafter", "guard", None, _utc(_ct_midnight() + timedelta(minutes=40)), "ai_quality"),
            ("admin_console", "query", None, _utc(_ct_midnight() + timedelta(minutes=41)), "request"),
            # Outside the seven days.
            ("pos_sync", "old", None, _utc(_ct_midnight(8) + timedelta(hours=12)), "job")]
    # More than the newest-hundred sample the page used to bucket.
    rows += [("review_fetch", f"e{i}", None, _utc(_ct_midnight(2) + timedelta(hours=10, seconds=i)), "job")
             for i in range(150)]
    _many(db_path, "INSERT INTO job_failures (job, error, context, created_at, kind) VALUES (?,?,?,?,?)", rows)
    out = admin_ops.jobs()
    daily = out["failures_daily"]
    assert [d["day"] for d in daily] == [_ct_day(i) for i in range(6, -1, -1)], "7 Central days, oldest first"
    by = {d["day"]: d["n"] for d in daily}
    assert by[_ct_day(0)] == 1 and by[_ct_day(1)] == 1 and by[_ct_day(2)] == 150 and by[_ct_day(3)] == 2
    assert sum(by.values()) == 154, "zero-filled, kind 'job' (or none) only, nothing past seven days"
    assert set(daily[0]) == {"day", "n"}


# ── O6 / P-11 contract 2: notifications().push_daily ────────────────────────

def _push_world(db_path):
    rid = _rid(db_path, "Push Grill")
    uid = _owner(db_path, rid)
    _x(db_path, "INSERT INTO device_tokens (id, restaurant_id, user_id, apns_token, environment) VALUES (1,?,?,?,?)",
       (rid, uid, "tok-1", "production"))
    return rid


def test_o6_push_deliveries_a_day_are_counted_by_the_database_with_failures(db_path):
    rid = _push_world(db_path)
    ins = ("INSERT INTO push_deliveries (device_token_id, restaurant_id, alert_type, status, ok, attempts, error, "
           "created_at) VALUES (1, ?, ?, 200, ?, 1, NULL, ?)")
    rows = [(rid, "urgent_review", 1, _utc(_ct_midnight() + timedelta(minutes=5 + i))) for i in range(250)]
    rows += [(rid, "urgent_review", 0, _utc(_ct_midnight() - timedelta(minutes=20)))]
    rows += [(rid, "brief", 1, _utc(_ct_midnight(5) + timedelta(hours=9)))]
    rows += [(rid, "brief", 1, _utc(_ct_midnight(9)))]
    _many(db_path, ins, rows)
    out = admin_ops.notifications()
    daily = out["push_daily"]
    assert [d["day"] for d in daily] == [_ct_day(i) for i in range(6, -1, -1)]
    by = {d["day"]: d for d in daily}
    assert by[_ct_day(0)]["n"] == 250 and by[_ct_day(0)]["failed"] == 0, "every delivery, not the newest 200"
    assert by[_ct_day(1)] == {"day": _ct_day(1), "n": 1, "failed": 1}
    assert by[_ct_day(5)]["n"] == 1 and sum(d["n"] for d in daily) == 252


# ── P-15: engagement is one grouped read per ledger ─────────────────────────

def test_p15_engagement_counts_by_type_are_unchanged_by_the_grouped_join(db_path):
    rid = _push_world(db_path)
    now = _utc(utc_now() - timedelta(hours=1))
    for t in ("urgent_review",) * 3 + ("brief",) * 2 + ("silent",):
        _x(db_path, "INSERT INTO alert_log (restaurant_id, alert_type, fired_at) VALUES (?,?,?)", (rid, t, now))
    for t, ok in (("urgent_review", 1), ("urgent_review", 1), ("urgent_review", 0), ("brief", 1)):
        _x(db_path, "INSERT INTO push_deliveries (device_token_id, restaurant_id, alert_type, status, ok, attempts, "
                    "created_at) VALUES (1, ?, ?, 200, ?, 1, ?)", (rid, t, ok, now))
    _x(db_path, "INSERT INTO notification_opens (restaurant_id, user_id, alert_type, opened_at) VALUES (?,?,?,?)",
       (rid, 1, "urgent_review", now))
    eng = {e["alert_type"]: e for e in admin_ops.notifications()["engagement"]}
    assert (eng["urgent_review"]["alerts"], eng["urgent_review"]["delivered"], eng["urgent_review"]["opened"]) == (3, 2, 1)
    assert eng["urgent_review"]["open_rate"] == 50.0
    assert (eng["brief"]["delivered"], eng["brief"]["opened"], eng["brief"]["open_rate"]) == (1, 0, 0.0)
    assert (eng["silent"]["delivered"], eng["silent"]["open_rate"]) == (0, None), "a type with no push reads 0"
    assert list(eng) == ["urgent_review", "brief", "silent"], "most alerts first"


# ── P-11 contract 3: the light filtered row reads ───────────────────────────

def _emails(db_path, rid):
    now = utc_now()
    rows = []
    for i in range(260):
        rows.append((rid, "newsletter", f"g{i}@ex.com", "Hi", _utc(now - timedelta(minutes=i + 1)), "delivered"))
    # Older than the newest 200 — the page's sample never saw them.
    rows += [(rid, "welcome", "a@ex.com", "Welcome", _utc(now - timedelta(hours=10)), "bounced"),
             (rid, "brief", "b@ex.com", "Brief", _utc(now - timedelta(hours=11)), "complained"),
             (rid, "brief", "c@ex.com", "Brief", _utc(now - timedelta(hours=12)), "failed"),
             (rid, "brief", "d@ex.com", "Brief", _utc(now - timedelta(hours=13)), "sent"),
             (rid, "brief", "e@ex.com", "Brief", _utc(now - timedelta(days=40)), "bounced")]
    _many(db_path, "INSERT INTO email_log (restaurant_id, email_type, to_email, subject, sent_at, status) "
                   "VALUES (?,?,?,?,?,?)", rows)


def test_p11_undeliverable_is_filtered_by_the_database_past_the_newest_200(app, db_path):
    rid = _rid(db_path, "Mail Grill")
    _emails(db_path, rid)
    c, _uid = _admin(app, db_path)
    r = c.get("/admin/api/emails/rows?status=undeliverable")
    assert r.status_code == 200
    d = r.get_json()
    assert d["ok"] and d["filter"] == {"status": "undeliverable", "type": ""} and d["limit"] == 200
    assert sorted(x["status"] for x in d["rows"]) == ["bounced", "complained", "failed"], \
        "failed, bounced or complained — the page's own filter — and nothing older than 30 days"
    row = d["rows"][0]
    assert set(row) >= {"id", "restaurant_id", "restaurant", "brand", "email_type", "to_email", "subject", "sent_at",
                        "status", "error"}, "emails().rows' shape"
    d = c.get("/admin/api/emails/rows?status=undeliverable&type=brief").get_json()
    assert sorted(x["status"] for x in d["rows"]) == ["complained", "failed"]
    d = c.get("/admin/api/emails/rows?type=newsletter&limit=900").get_json()
    assert d["limit"] == 500 and len(d["rows"]) == 260, "the LIMIT after the WHERE, at most 500"
    assert c.get("/admin/api/emails/rows?status=sent").get_json()["rows"][0]["to_email"] == "d@ex.com"
    assert c.get("/admin/api/emails/rows?status=nope").status_code == 400


def test_p11_failed_pushes_are_filtered_by_the_database(app, db_path):
    rid = _push_world(db_path)
    now = utc_now()
    rows = [(rid, "brief", 1, _utc(now - timedelta(minutes=i + 1))) for i in range(230)]
    rows += [(rid, "urgent_review", 0, _utc(now - timedelta(hours=6)))]
    _many(db_path, "INSERT INTO push_deliveries (device_token_id, restaurant_id, alert_type, status, ok, attempts, "
                   "created_at) VALUES (1, ?, ?, 200, ?, 1, ?)", rows)
    c, _uid = _admin(app, db_path)
    d = c.get("/admin/api/notifications/pushes?status=failed").get_json()
    assert d["ok"] and d["filter"] == {"status": "failed"} and [p["alert_type"] for p in d["pushes"]] == ["urgent_review"]
    assert set(d["pushes"][0]) >= {"id", "restaurant_id", "restaurant", "device_token_id", "user_id", "username",
                                   "alert_type", "status", "ok", "attempts", "error", "created_at"}
    d = c.get("/admin/api/notifications/pushes?limit=5").get_json()
    assert d["limit"] == 5 and len(d["pushes"]) == 5 and d["filter"] == {"status": ""}
    assert c.get("/admin/api/notifications/pushes?status=ok").status_code == 400


def test_p11_the_row_reads_are_masked_for_support_like_their_pages(app, db_path):
    rid = _rid(db_path, "Mail Grill")
    _emails(db_path, rid)
    s = _support(app, db_path)
    r = s.get("/admin/api/emails/rows?status=undeliverable")
    assert r.status_code == 200 and r.headers.get("X-Redacted") == "support"
    assert "a@ex.com" not in r.get_data(as_text=True), "an address is masked for a support login"


# ── O9 contract 4: data sources read failing | stale | ok ────────────────────

def _source(db_path, rid, source, failures=0, ok_hours_ago=None):
    _x(db_path, "INSERT INTO source_health (restaurant_id, source, provider, last_attempt_at, last_ok_at, "
                "consecutive_failures, last_error) VALUES (?,?,?,?,?,?,?)",
       (rid, source, "test", _utc(utc_now()), _utc(utc_now() - timedelta(hours=ok_hours_ago))
        if ok_hours_ago is not None else None, failures, "boom" if failures else None))


def test_o9_a_source_with_no_good_read_in_its_cadence_reads_stale_not_ok(db_path):
    rid = _rid(db_path, "Feed Grill")
    _source(db_path, rid, "pos", failures=2, ok_hours_ago=40)
    _source(db_path, rid, "labor", ok_hours_ago=30)           # nightly: stale past 24h + 2h slack
    _source(db_path, rid, "competitor", ok_hours_ago=200)     # weekly: stale past 168h + 2h
    _source(db_path, rid, "reviews", ok_hours_ago=14)         # overnight gap: not stale at 14h
    _source(db_path, rid, "weather", ok_hours_ago=24 * 5)     # fetched on read: never stale
    _source(db_path, rid, "sales", ok_hours_ago=2)
    out = admin_ops.data_sources()
    st = {r["source"]: r["state"] for r in out["rows"]}
    assert st == {"pos": "failing", "labor": "stale", "competitor": "stale", "reviews": "ok", "weather": "ok",
                  "sales": "ok"}
    assert [r["source"] for r in out["rows"]][:3] == ["pos", "competitor", "labor"], \
        "failing, then stale (oldest good read first), then ok"
    by = {s["source"]: s for s in out["summary"]}
    assert by["labor"]["stale"] == 1 and by["pos"]["failing"] == 1 and by["pos"]["stale"] == 0
    assert out["state_counts"] == {"failing": 1, "stale": 2, "ok": 3}
    rows = {r["source"]: r for r in out["rows"]}
    assert rows["labor"]["stale_after_hours"] == 26.0 and rows["weather"]["stale_after_hours"] is None
    assert rows["reviews"]["stale_after_hours"] == 18.0, "the quiet Places-only slots' longer gap"
    # Paged and filtered by the server.
    page = admin_ops.data_sources(page=1, state="stale")
    assert [r["source"] for r in page["rows"]] == ["competitor", "labor"] and page["total"] == 2
    assert [r["source"] for r in admin_ops.data_sources(page=1, failing=True)["rows"]] == ["pos"], \
        "failing=1 keeps working"
    assert admin_ops.data_sources(page=1, state="failing")["total"] == 1


def test_o9_the_data_sources_route_takes_the_stale_filter(app, db_path):
    rid = _rid(db_path, "Feed Grill")
    _source(db_path, rid, "labor", ok_hours_ago=30)
    _source(db_path, rid, "sales", ok_hours_ago=1)
    c, _uid = _admin(app, db_path)
    d = c.get("/admin/api/data-sources?state=stale").get_json()
    assert d["ok"] and [r["source"] for r in d["rows"]] == ["labor"] and d["state"] == "stale"


# ── O9 contract 5: the fleet list's slim rows carry the Data cell's facts ────

def test_o9_slim_fleet_rows_carry_freshness_for_the_data_cell(db_path):
    rid = _rid(db_path, "Fresh Grill")
    _x(db_path, "UPDATE restaurants SET last_fetched_at=? WHERE id=?",
       ((restaurant_now() - timedelta(hours=3)).strftime("%Y-%m-%dT%H:%M:%S"), rid))
    item = next(i for i in admin_ops.clients_page()["items"] if i["id"] == rid)
    f = item["freshness"]
    assert set(f) == {"pos_state", "reviews", "pos"}
    assert f["reviews"] and f["reviews"].endswith("Z"), "the review fetch, as the console's Z stamp"
    assert isinstance(f["pos_state"], dict) and f["pos_state"]["state"] == "not_connected" and f["pos"] is None
