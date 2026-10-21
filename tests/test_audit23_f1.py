"""Audit #23 fix round (10/21/26), F1 — the RPOWER client and its transport.

A1-05 store listing, A1-07/A4-01 breaker and store quarantine, A1-08/A4-02
429 slow-down and retry allowance, A1-09/A4-17 response contract, A1-10 page
size, A1-11/A4-21 the re-read hook, A1-12 the session, A1-13/A4-16/A6-09 the
auth latch, A1-17/A2-21 the 2000-01-01 sentinel, A1-18 store timezone, A1-19
the push, A1-23 RPOWER_BASE_URL, A5-07 the reserved bucket, A5-14 sized
caches, A5-15/A6-11 the probe, A6-24 reads per night."""
import threading
import time
from email.utils import format_datetime
from datetime import datetime, timedelta, timezone

import pytest
import requests

import models
import pos
import pos_io
import rpower


@pytest.fixture(autouse=True)
def _redirect(db_path, monkeypatch):
    real = models.get_conn
    monkeypatch.setattr(models, "get_conn", lambda *a, **k: real(db_path))
    monkeypatch.setattr(models, "DB_PATH", db_path)
    monkeypatch.setattr(pos, "PROVIDERS", None)
    monkeypatch.setattr(rpower, "REQUEST_SPACING_SECONDS", 0)
    monkeypatch.setattr(rpower, "FLEET_RPS", 0)
    monkeypatch.setattr(rpower.time, "sleep", lambda s: None)
    pos_io.reset_for_tests()
    yield
    pos_io.reset_for_tests()
    rpower._note_store(None)


def _restaurant(rid=1, **kw):
    conn = models.get_conn()
    cols = {"id": rid, "name": f"R{rid}", "owner_email": f"o{rid}@x.test",
            "rpower_token": f"tok-{rid}", "rpower_cg": 1280, "rpower_store_mid": f"store-{rid}"}
    cols.update(kw)
    conn.execute(f"INSERT INTO restaurants ({','.join(cols)}) VALUES ({','.join('?' for _ in cols)})",
                 tuple(cols.values()))
    conn.commit()
    conn.close()
    return rid


class _Resp:
    def __init__(self, status=200, body=None, headers=None):
        self.status_code, self.ok, self.text = status, 200 <= status < 300, "x"
        self.headers = headers or {}
        self._body = body if body is not None else []

    def json(self):
        return self._body


def _one(sql, args=()):
    conn = models.get_conn()
    try:
        r = conn.execute(sql, args).fetchone()
        return dict(r) if r is not None else None
    finally:
        conn.close()


def _store(i, cg=1280):
    return {"mid": f"m{i}", "cg": cg, "name": f"S{i}", "timezone": "America/Chicago"}


def _line(i, day="2026-10-10"):
    return {"rid": f"r{i}", "date": f"{day}T00:00:00", "sales": 10.0, "qty": 1, "slstype_mid": "1"}


# ── A1-05: store/get is one answer; partner tokens list by group ────────────

def test_a_complete_store_get_answer_of_1200_is_never_called_truncated(monkeypatch):
    stores = [_store(i) for i in range(1200)]
    calls = []

    def fake(token, path, params=None):
        calls.append((path, dict(params or {})))
        if path == "store/get":
            return stores                         # ignores paging: the whole set
        if path == "store/getbycg":
            page = int(params["pagenumber"])
            return stores[(page - 1) * 1000: page * 1000]
        raise AssertionError(path)
    monkeypatch.setattr(rpower, "_request", fake)
    got = rpower.list_stores("tok")
    assert len(got) == 1200 and len({s["mid"] for s in got}) == 1200
    assert all(p != "store/get" or "pagenumber" not in q for p, q in calls), "store/get is never paged"


def test_a_capped_store_get_is_completed_through_getbycg(monkeypatch):
    stores = [_store(i, cg=1280 if i < 1100 else 1300) for i in range(1300)]

    def fake(token, path, params=None):
        if path == "store/get":
            return stores[:1000]                  # capped at 1,000
        page = int(params["pagenumber"])
        mine = [s for s in stores if str(s["cg"]) == str(params["cg"])]
        return mine[(page - 1) * 1000: page * 1000]
    monkeypatch.setattr(rpower, "_request", fake)
    got = rpower.list_stores("tok", groups=(1300,))
    assert len(got) == 1300


def test_a_small_token_still_makes_one_request(monkeypatch):
    calls = []
    monkeypatch.setattr(rpower, "_request", lambda t, p, params=None: (calls.append(p), [_store(1)])[1])
    assert len(rpower.list_stores("tok")) == 1 and calls == ["store/get"]


def test_one_store_is_bound_by_its_mid(monkeypatch):
    monkeypatch.setattr(rpower, "_request", lambda t, p, params=None: dict(_store(7), mid=params["mid"]))
    got = rpower.store_by_mid("tok", "m7")
    assert got["mid"] == "m7" and got["cg"] == 1280


# ── A1-07 / A4-01: per request, per store; the fleet only on a share ───────

def test_a_failed_request_is_one_failure_not_three(monkeypatch):
    monkeypatch.setattr("requests.get", lambda *a, **k: _Resp(503))
    rpower._note_store(1)
    with pytest.raises(rpower.RPowerServerError) as got:
        rpower._request("tok", "x", {})
    assert got.value.store_failure and got.value.status == 503
    assert len(pos_io._breaker_local["rpower"]) == 1


def test_a_quarantined_store_does_not_stop_another(monkeypatch):
    monkeypatch.setattr("requests.get", lambda *a, **k: _Resp(500))
    rpower._note_store(1)
    for _ in range(pos_io.STORE_QUARANTINE_FAILURES):
        with pytest.raises(rpower.RPowerServerError):
            rpower._request("tok", "x", {})
    monkeypatch.setattr("requests.get", lambda *a, **k: _Resp(200, []))
    rpower._note_store(2)
    assert rpower._request("tok", "x", {}) == []
    rpower._note_store(1)
    with pytest.raises(rpower.RPowerStoreQuarantined) as got:
        rpower._request("tok", "x", {})
    assert got.value.retry_after > 0 and pos_io.breaker_state("rpower")["state"] == "closed"


def test_a_lapsed_quarantine_lets_one_probe_through_and_an_answer_closes_it(monkeypatch):
    key = pos_io.store_key("rpower", 1)
    pos_io._write_breaker(key, open_until=time.time() - 1, opened_at=time.time() - 300, cooldown=120.0)
    monkeypatch.setattr("requests.get", lambda *a, **k: _Resp(200, []))
    rpower._note_store(1)
    rpower._request("tok", "x", {})
    assert pos_io.quarantined("rpower") == []


def test_a_failed_store_probe_doubles_its_cooldown_and_stays_parked(monkeypatch):
    key = pos_io.store_key("rpower", 1)
    pos_io._write_breaker(key, open_until=time.time() - 1, opened_at=time.time() - 300, cooldown=120.0)
    sent = []
    monkeypatch.setattr("requests.get", lambda *a, **k: (sent.append(1), _Resp(500))[1])
    rpower._note_store(1)
    with pytest.raises(rpower.RPowerStoreQuarantined):
        rpower._request("tok", "x", {})
    assert len(sent) == 1, "a probe is one request"
    assert pos_io._breaker_row(key, fresh=True)["cooldown"] == 240.0


def test_the_fleet_rule_needs_a_share_of_distinct_stores():
    f = pos_io.fleet_should_open
    assert not f({1}, {1}, 40, 40), "one store alone never opens the fleet"
    assert f({1, 2}, {1, 2}, 4, 4), "both of two stores failing"
    assert not f(set(range(100)), {1, 2, 3, 4}, 200, 4), "4 of 100: under the minimum and the share"
    assert not f(set(range(100)), set(range(9)), 200, 9), "9 of 100: under 10%"
    assert f(set(range(100)), set(range(10)), 200, 10)
    assert f(set(range(1000)), set(range(100)), 2000, 100)
    assert not f(set(range(1000)), set(range(60)), 2000, 60)


def test_the_fleet_opens_when_many_stores_fail(monkeypatch):
    monkeypatch.setattr("requests.get", lambda *a, **k: _Resp(502))
    for rid in range(1, 7):
        rpower._note_store(rid)
        with pytest.raises(rpower.RPowerError):
            rpower._request("tok", "x", {})
    assert pos_io.breaker_state("rpower")["state"] == "open"


# ── A1-08 / A4-02: 429 ──────────────────────────────────────────────────────

def test_the_last_429_raises_rate_limited_with_at_least_a_minute(monkeypatch):
    monkeypatch.setattr("requests.get", lambda *a, **k: _Resp(429, headers={"Retry-After": "3"}))
    with pytest.raises(rpower.RPowerRateLimited) as got:
        rpower._request("tok", "x", {})
    assert got.value.retry_after >= 60


def test_retry_after_as_an_http_date_is_read():
    when = datetime.now(timezone.utc) + timedelta(seconds=120)
    secs = rpower._retry_after(_Resp(429, headers={"Retry-After": format_datetime(when, usegmt=True)}))
    assert 100 <= secs <= 121
    assert rpower._retry_after(_Resp(429, headers={"Retry-After": "nonsense"})) == 0.0
    assert rpower._retry_after(_Resp(429, headers={"Retry-After": "7"})) == 7.0


def test_a_429_slows_every_process_and_recovers(monkeypatch):
    monkeypatch.setattr(rpower, "FLEET_RPS", 10.0)
    monkeypatch.setattr("requests.get", lambda *a, **k: _Resp(429, headers={"Retry-After": "20"}))
    with pytest.raises(rpower.RPowerRateLimited):
        rpower._request("tok", "x", {})
    st = rpower.rate_state("tok")
    assert st["fleet"]["rate_share"] == 0.5 and st["token"]["rate_share"] == 0.5, "halved once per window"
    assert st["fleet"]["slow_for"] >= 25 and st["fleet"]["next_slot_in"] >= 15, "nobody sends before Retry-After"
    now = time.time()
    assert rpower._rate_factor(0.5, now + 10, now) == 0.5
    assert rpower._rate_factor(0.5, now - 60, now) == pytest.approx(0.6)
    assert rpower._rate_factor(0.5, now - 3600, now) == 1.0


def test_a_429_is_pressure_not_a_success_for_the_breaker(monkeypatch):
    rpower._note_store(1)
    monkeypatch.setattr("requests.get", lambda *a, **k: _Resp(500))
    with pytest.raises(rpower.RPowerServerError):
        rpower._request("tok", "x", {})
    monkeypatch.setattr("requests.get", lambda *a, **k: _Resp(429, headers={"Retry-After": "60"}))
    with pytest.raises(rpower.RPowerRateLimited):
        rpower._request("tok", "x", {})
    assert pos_io._store_runs.get(("rpower", 1)) == 1, "the 429 did not end the store's run of failures"


def test_retries_stop_when_the_fleet_allowance_is_spent(monkeypatch):
    monkeypatch.setattr(rpower, "FLEET_RPS", 10.0)
    monkeypatch.setattr(rpower, "RETRY_BUDGET_SHARE", 1.0 / 600)        # one retry a minute
    sent = []
    monkeypatch.setattr("requests.get", lambda *a, **k: (sent.append(1), _Resp(503))[1])
    with pytest.raises(rpower.RPowerServerError):
        rpower._request("tok", "x", {})
    assert len(sent) == 2, "first try plus the minute's one retry"
    sent.clear()
    with pytest.raises(rpower.RPowerServerError) as got:
        rpower._request("tok", "x", {})
    assert len(sent) == 1 and "retry allowance" in str(got.value)


# ── A1-09 / A4-17: the response contract ────────────────────────────────────

def test_an_object_where_a_list_is_documented_is_refused_not_kept(monkeypatch):
    monkeypatch.setattr(rpower, "_request", lambda t, p, params=None: {"Message": "An error has occurred."})
    with pytest.raises(rpower.RPowerSchemaError) as got:
        rpower._paged("tok", "ticketsales/getbybusinessdate", {"startdate": "2026-10-10"})
    assert got.value.endpoint == "ticketsales/getbybusinessdate" and "Message" in got.value.detail


def test_a_renamed_money_field_is_refused(monkeypatch):
    row = _line(1)
    row["net_sales"] = row.pop("sales")
    monkeypatch.setattr(rpower, "_request", lambda t, p, params=None: [row])
    with pytest.raises(rpower.RPowerSchemaError, match="sales"):
        rpower._paged("tok", "ticketsales/getbybusinessdate", {})
    monkeypatch.setattr(rpower, "_request", lambda t, p, params=None: [dict(_line(1), sales={"amount": 1})])
    with pytest.raises(rpower.RPowerSchemaError):
        rpower._paged("tok", "ticketsales/getbybusinessdate", {})
    monkeypatch.setattr(rpower, "_request", lambda t, p, params=None: [{"name": "x"}])
    with pytest.raises(rpower.RPowerSchemaError, match="mid"):
        rpower._paged("tok", "salestype/getbycg", {})


def test_a_contract_break_from_two_stores_pauses_the_provider_and_pages_at_once(monkeypatch):
    import ops
    import scheduler
    monkeypatch.setattr(rpower, "_request", lambda t, p, params=None: [{"date": "2026-10-10"}])
    for rid in (1, 2):
        rpower._note_store(rid)
        with pytest.raises(rpower.RPowerSchemaError):
            rpower._paged("tok", "ticketsales/getbybusinessdate", {})
    st = pos_io.breaker_state("rpower")
    assert st["state"] == "open" and st["reason"].startswith(pos_io.SCHEMA_REASON)
    monkeypatch.setattr(scheduler, "scheduling_allowed", lambda: True)
    pages = []
    monkeypatch.setattr(ops, "page_operator", lambda *a, **k: (pages.append(a[0]), {"sent": True})[1])
    assert pos_io.page_if_long_open("rpower") is True
    assert pos_io.page_if_long_open("rpower") is False
    assert pages == ["pos_schema:rpower"]


def test_the_demo_store_answers_inside_the_contract():
    for path in ("salestype/getbycg", "menuitem/getbycg", "job/getbycg", "employee/getbycg"):
        assert rpower._paged("demo-store:halsted", path, {"cg": 1}) is not None


# ── A1-10: a smaller server page size ───────────────────────────────────────

def _pager(rows, size):
    def fake(token, path, params=None):
        page = int(params["pagenumber"])
        return rows[(page - 1) * size: page * size]
    return fake


def test_a_server_page_of_500_reads_the_whole_night(monkeypatch):
    rows = [_line(i) for i in range(1300)]
    monkeypatch.setattr(rpower, "_request", _pager(rows, 500))
    assert len(rpower._paged("tok", "ticketsales/getbybusinessdate", {})) == 1300


def test_a_whole_answer_that_ignores_paging_is_whole(monkeypatch):
    rows = [_line(i) for i in range(500)]
    monkeypatch.setattr(rpower, "_request", lambda t, p, params=None: rows)
    got = rpower._paged("tok", "ticketsales/getbybusinessdate", {})
    assert len(got) == 500 and got.pages == 2


def test_an_ordinary_short_page_is_one_request(monkeypatch):
    calls = []
    rows = [_line(i) for i in range(437)]
    monkeypatch.setattr(rpower, "_request", lambda t, p, params=None: (calls.append(1), rows)[1])
    assert len(rpower._paged("tok", "ticketsales/getbybusinessdate", {})) == 437 and len(calls) == 1


# ── A1-11 / A4-21: a row deleted mid-read ───────────────────────────────────

def test_a_gap_found_by_the_callers_check_is_read_again_once(monkeypatch):
    full = [_line(i) for i in range(1500)]
    reads = {"n": 0}

    def fake(token, path, params=None):
        page = int(params["pagenumber"])
        if page == 1:
            reads["n"] += 1
        rows = full if reads["n"] > 1 else [r for r in full if r["rid"] != "r5"]   # r5 deleted after page 1
        if reads["n"] == 1 and page == 1:
            rows = full
        return rows[(page - 1) * 1000: page * 1000]
    monkeypatch.setattr(rpower, "_request", fake)
    got = rpower._paged("tok", "ticketsales/getbybusinessdate", {}, verify=1500)
    assert got.reread and got.verified is True and len(got) == 1500
    got = rpower._paged("tok", "ticketsales/getbybusinessdate", {}, verify=lambda rows: len(rows) == 1500)
    assert not got.reread and got.verified is True


# ── A1-12: one session per thread, no retries of its own, no cookies ───────

def test_each_thread_keeps_its_own_session():
    a = rpower._thread_session()
    assert rpower._thread_session() is a and isinstance(a, requests.Session)
    adapter = a.get_adapter("https://rpowerpos.com/")
    assert adapter.max_retries.total == 0 and adapter._pool_maxsize == rpower.HTTP_POOL_MAXSIZE
    other = []
    t = threading.Thread(target=lambda: other.append(rpower._thread_session()))
    t.start()
    t.join()
    assert other[0] is not a
    # No answer's cookie is kept to ride along with another store's request.
    assert a.cookies._policy.allowed_domains() == ()
    import http.cookiejar
    import urllib.request
    req = urllib.request.Request("https://rpowerpos.com/api/v1/coreapi/store/get")

    class _Msg:
        def get_all(self, name, default=None):
            return ["affinity=y; Domain=rpowerpos.com; Path=/"] if name.lower() == "set-cookie" else (default or [])

    class _R:
        def info(self):
            return _Msg()
    a.cookies.extract_cookies(_R(), req)
    assert len(a.cookies) == 0
    del http


def test_requests_go_through_the_session_with_their_timeout(monkeypatch):
    seen = []

    class _S:
        def get(self, url, **kw):
            seen.append(kw.get("timeout"))
            return _Resp(200, [])
    monkeypatch.setattr(rpower, "_session", lambda: _S())
    rpower._request("tok", "closeday/getbybusinessdate", {})
    rpower._request("tok", "ticket/getbybusinessdate", {})
    assert seen == [rpower.TIMEOUT_BY_PATH["closeday/getbybusinessdate"], rpower.TIMEOUT_SECONDS]


# ── A1-13 / A4-16 / A6-09: the auth latch ───────────────────────────────────

def test_a_refused_token_is_latched_and_not_asked_again(monkeypatch):
    rid = _restaurant()
    sent = []
    monkeypatch.setattr("requests.get", lambda *a, **k: (sent.append(1), _Resp(401))[1])
    with pytest.raises(rpower.RPowerAuthError):
        rpower.sales_types(rid)
    assert models.get_restaurant(rid).rpower_auth_failed_at
    with pytest.raises(rpower.RPowerAuthLatched) as got:
        rpower.void_reasons(rid)
    assert len(sent) == 1 and got.value.retry_after > 60, "nothing sent while latched"


def test_after_the_latch_one_reprobe_and_an_answer_clears_it(monkeypatch):
    rid = _restaurant()
    monkeypatch.setattr("requests.get", lambda *a, **k: _Resp(401))
    with pytest.raises(rpower.RPowerAuthError):
        rpower.sales_types(rid)
    conn = models.get_conn()
    conn.execute("UPDATE pos_auth_latch SET failed_at=?", (time.time() - pos_io.AUTH_LATCH_SECONDS - 5,))
    conn.commit()
    conn.close()
    pos_io.reset_for_tests()
    monkeypatch.setattr("requests.get", lambda *a, **k: _Resp(200, [{"mid": "1"}]))
    rpower.clear_caches()
    assert rpower.void_reasons(rid) is not None
    assert models.get_restaurant(rid).rpower_auth_failed_at is None
    assert _one("SELECT COUNT(*) AS n FROM pos_auth_latch")["n"] == 0


def test_a_first_403_on_a_store_that_answered_this_hour_is_try_later(monkeypatch):
    rid = _restaurant()
    monkeypatch.setattr("requests.get", lambda *a, **k: _Resp(200, [{"mid": "1"}]))
    rpower.sales_types(rid)
    pos_io.flush()                                   # the answer is in the ledger
    monkeypatch.setattr("requests.get", lambda *a, **k: _Resp(403))
    rpower.clear_caches()
    with pytest.raises(rpower.RPowerTryLater) as got:
        rpower.void_reasons(rid)
    assert not isinstance(got.value, rpower.RPowerAuthError) and got.value.retry_after == 300
    assert models.get_restaurant(rid).rpower_auth_failed_at is None
    pos_io.reset_for_tests()
    with pytest.raises(rpower.RPowerAuthError):
        rpower.void_reasons(rid)
    assert models.get_restaurant(rid).rpower_auth_failed_at


def test_a_wave_of_refusals_pages_once_and_holds_the_owners_notices(monkeypatch):
    import ops
    import scheduler
    for rid in (1, 2, 3):
        _restaurant(rid, rpower_token="partner")
    monkeypatch.setattr("requests.get", lambda *a, **k: _Resp(401))
    for rid in (1, 2, 3):
        with pytest.raises(rpower.RPowerAuthError):
            rpower.sales_types(rid)
    wave = pos_io.auth_wave("rpower", fresh=True)
    assert wave["active"] and wave["stores"] == 3
    assert _one("SELECT COUNT(*) AS n FROM pos_auth_latch WHERE store=''")["n"] == 1, "the token itself latched"
    monkeypatch.setattr(scheduler, "scheduling_allowed", lambda: True)
    pages = []
    monkeypatch.setattr(ops, "page_operator", lambda key, *a, **k: (pages.append(key), {"sent": True})[1])
    pos_io.page_if_long_open("rpower")
    assert pages == ["pos_auth_wave:rpower"]
    import pos_health
    monkeypatch.setattr(pos_health, "pos_sync_state", lambda r, now=None: {"state": "error", "provider": "rpower",
                                                                          "last_synced": None, "error": "401"})
    assert pos.note_sync_failure(1) is False


def test_an_admins_own_token_check_passes_the_latch_and_clears_it(monkeypatch):
    rid = _restaurant()
    monkeypatch.setattr("requests.get", lambda *a, **k: _Resp(401))
    with pytest.raises(rpower.RPowerAuthError):
        rpower._request("tok-1", "store/get")
    monkeypatch.setattr("requests.get", lambda *a, **k: _Resp(200, [_store(1)]))
    assert rpower.test_token("tok-1")["ok"]
    assert _one("SELECT COUNT(*) AS n FROM pos_auth_latch")["n"] == 0
    del rid


# ── A1-17 / A2-21: one reading of 2000-01-01 ────────────────────────────────

def test_a_close_with_an_unset_thru_date_closes_its_from_date(monkeypatch):
    rid = _restaurant()
    monkeypatch.setattr(rpower, "_request", lambda t, p, params=None: [
        {"from_date": "2026-10-10T00:00:00", "thru_date": "2000-01-01T00:00:00"}])
    assert rpower.fetch_day_closed(rid, "2026-10-10") is True
    assert rpower._biz_date("2000-01-01T00:00:00") is None
    assert rpower._hour_of("2000-01-01T11:00:00") is None and rpower._hour_of("2026-10-10T11:16:00") == "11"
    assert rpower._punch_dt("2000-01-01T00:00:00") is None


# ── A1-18: the store's timezone ─────────────────────────────────────────────

def test_windows_timezone_names_map_and_unknown_ones_are_refused():
    assert rpower.store_timezone("Pacific Standard Time") == ("America/Los_Angeles", None)
    assert rpower.store_timezone("Pacific/Honolulu") == ("Pacific/Honolulu", None)
    tz, problem = rpower.store_timezone("Moon Standard Time")
    assert tz is None and "Moon" in problem


def test_bootstrap_never_stores_an_unknown_timezone_and_says_so(monkeypatch):
    rid = _restaurant(rpower_cg=None, rpower_store_mid=None, timezone="America/Chicago")
    monkeypatch.setattr(rpower, "_request", lambda t, p, params=None: [dict(_store(1), timezone="Moon Time")])
    import strategy_jobs
    monkeypatch.setattr(strategy_jobs, "enqueue_pos_onboard", lambda *a, **k: None)
    out = rpower.bootstrap(rid)
    assert out["ok"] and out["timezone_warning"]
    assert models.get_restaurant(rid).timezone == "America/Chicago"
    assert _one("SELECT COUNT(*) AS n FROM activity_log WHERE event_type='pos_timezone_unrecognised'")["n"] == 1


def test_bootstrap_maps_a_windows_timezone(monkeypatch):
    rid = _restaurant(rpower_cg=None, rpower_store_mid=None, timezone="America/Chicago")
    monkeypatch.setattr(rpower, "_request", lambda t, p, params=None: [dict(_store(1),
                                                                            timezone="Pacific Standard Time")])
    import strategy_jobs
    monkeypatch.setattr(strategy_jobs, "enqueue_pos_onboard", lambda *a, **k: None)
    assert rpower.bootstrap(rid)["ok"]
    assert models.get_restaurant(rid).timezone == "America/Los_Angeles"


# ── A1-19: the push ─────────────────────────────────────────────────────────

def _push_ready(monkeypatch):
    rid = _restaurant()
    monkeypatch.setattr(rpower, "_schedule_payload", lambda *a, **k: ({"x": 1}, {"E1": [1]}, []))
    return rid


def test_the_push_takes_a_budget_slot(monkeypatch):
    rid = _push_ready(monkeypatch)
    took = []
    monkeypatch.setattr(rpower, "_sleep_for_spacing", lambda token="": took.append(token))
    monkeypatch.setattr("requests.post", lambda *a, **k: _Resp(200, {}))
    assert rpower.push_labor_schedule(rid, [{}])["ok"] and took == ["tok-1"]


def test_a_push_that_timed_out_after_sending_is_unknown_not_failed(monkeypatch):
    rid = _push_ready(monkeypatch)

    def slow(*a, **k):
        raise requests.exceptions.ReadTimeout("read timed out")
    monkeypatch.setattr("requests.post", slow)
    out = rpower.push_labor_schedule(rid, [{}])
    assert out["unknown"] and not out["ok"]

    def refused(*a, **k):
        raise requests.exceptions.ConnectTimeout("connect timed out")
    monkeypatch.setattr("requests.post", refused)
    out = rpower.push_labor_schedule(rid, [{}])
    assert not out.get("unknown") and "Couldn't reach" in out["error"]


# ── A1-23: RPOWER_BASE_URL ──────────────────────────────────────────────────

def test_the_base_url_is_a_setting_and_only_https(monkeypatch):
    monkeypatch.setenv("RPOWER_BASE_URL", "https://proxy.example.test/coreapi/")
    assert rpower._base_url_from_env() == "https://proxy.example.test/coreapi"
    monkeypatch.setenv("RPOWER_BASE_URL", "http://proxy.example.test/coreapi")
    assert rpower._base_url_from_env() == rpower.DEFAULT_BASE_URL
    monkeypatch.delenv("RPOWER_BASE_URL")
    assert rpower._base_url_from_env() == rpower.DEFAULT_BASE_URL


# ── A5-07: reserved slots, leased in batches, no polling ────────────────────

def test_one_round_trip_reserves_a_slot_for_each_waiting_thread():
    rpower._leases.clear()
    assert rpower._take_shared_slot("tok-z", 0.5, n=3) == 0.0
    assert len(rpower._leases[rpower._bucket_key("tok-z")]) == 2
    nxt = rpower._take_shared_slot("tok-z", 0.5)
    assert 1.4 <= nxt <= 1.5, "the next process's slot is after the three reserved"
    assert rpower._take_shared_slot("tok-z", 0.5, max_wait=0.1) > 0.1, "past max_wait: nothing taken"


def test_waiters_sleep_their_slot_and_do_not_poll(monkeypatch):
    monkeypatch.setattr(rpower, "REQUEST_SPACING_SECONDS", 0.02)
    trips = []
    real = rpower._take_shared_slot
    monkeypatch.setattr(rpower, "_take_shared_slot", lambda *a, **k: (trips.append(1), real(*a, **k))[1])
    monkeypatch.setattr(rpower.time, "sleep", time.sleep)
    threads = [threading.Thread(target=lambda: rpower._sleep_for_spacing("tok-herd")) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(trips) <= 8, "at most one database round trip per request, never a polling loop"


# ── A5-14: stamp caches sized by stores ─────────────────────────────────────

def test_stamp_caches_grow_with_the_connected_stores(monkeypatch):
    monkeypatch.setattr(rpower, "connected_store_count", lambda: 1500)
    cache = rpower._LRU(64, per_store=1)
    for i in range(1500):
        cache[i] = i
    assert len(cache) == 1500 and cache.get(0) == 0
    fixed = rpower._LRU(64)
    for i in range(100):
        fixed[i] = i
    assert len(fixed) == 64, "the heavy caches keep their fixed bound"
    assert rpower._people_refreshed.per_store == 1 and not rpower._menu_cache.per_store


# ── A5-15 / A6-11: the probe ────────────────────────────────────────────────

def test_the_probe_asks_stores_on_distinct_tokens_as_no_store(monkeypatch):
    import provider_health
    _restaurant(1, rpower_token="a")
    _restaurant(2, rpower_token="a")
    _restaurant(3, rpower_token="b")
    asked = []
    monkeypatch.setattr("requests.get", lambda url, **k: (asked.append((url, k["params"])),
                                                           _Resp(200, dict(_store(1), mid=k["params"]["mid"])))[1])
    got = provider_health.probe_rpower()
    assert got["state"] == "ok"
    assert [p["mid"] for _u, p in asked] == ["store-1", "store-3"] and all("getbymid" in u for u, _p in asked)
    pos_io.flush()
    assert _one("SELECT restaurant_id FROM pos_request_ledger")["restaurant_id"] == 0


def test_one_refused_token_is_that_stores_issue_two_are_the_providers(monkeypatch):
    import provider_health
    _restaurant(1, rpower_token="a")
    _restaurant(2, rpower_token="b")
    monkeypatch.setattr("requests.get", lambda url, headers=None, **k: _Resp(
        401 if headers["Authorization"].endswith(" a") else 200, dict(_store(1))))
    got = provider_health.probe_rpower()
    assert got["state"] == "ok" and "that store's issue" in got["detail"]
    pos_io.reset_for_tests()
    conn = models.get_conn()
    conn.execute("DELETE FROM pos_auth_latch")
    conn.commit()
    conn.close()
    monkeypatch.setattr("requests.get", lambda *a, **k: _Resp(401))
    assert provider_health.probe_rpower()["state"] == "failing"


# ── A6-24: reads per business night ─────────────────────────────────────────

def test_a_night_read_twice_shows_in_the_ledger(monkeypatch):
    rpower._note_store(1)
    monkeypatch.setattr("requests.get", lambda *a, **k: _Resp(200, [_line(1)]))
    for _ in range(2):
        rpower._paged("tok", "ticketsales/getbybusinessdate", {"startdate": "2026-10-10", "enddate": "2026-10-10"})
    rpower._paged("tok", "ticketsales/getbybusinessdate", {"startdate": "2026-10-09", "enddate": "2026-10-15"})
    pos_io.flush()
    row = _one("SELECT * FROM pos_request_nights")
    assert (row["night"], row["reads"], row["pages"], row["restaurant_id"]) == ("2026-10-10", 2, 2, 1)
    assert _one("SELECT COUNT(*) AS n FROM pos_request_nights")["n"] == 1, "a range read is no night"
    nr = pos_io.summary(24)["night_reads"]
    assert nr["read_again"] == 1 and nr["extra_reads"] == 1


def test_bulk_provisioning_asks_for_a_store_a_long_list_did_not_name(monkeypatch):
    import bulk_provisioning
    monkeypatch.setattr(rpower, "_request", lambda t, p, params=None: dict(_store(9, cg=1300), mid=params["mid"],
                                                                           timezone="Eastern Standard Time"))
    got = bulk_provisioning._rpower_store("tok", 1300, "m9")
    assert got["store_mid"] == "m9" and got["timezone"] == "America/New_York"
    assert bulk_provisioning._rpower_store("tok", 1280, "m9") is None, "another group's store is not this row's"
