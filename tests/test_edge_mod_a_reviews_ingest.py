"""MOD audit, Reviews, part 1: getting reviews in (appendix A1 R1).

What these tests protect: a review fetch that did not reach Google must never
look like a quiet day, one guest review must be one row whichever Google API
it arrived through, a review's day must be the restaurant's day on both
paths, credentials must never reach the failure log, and what Google no
longer lists must stop counting.

Tests marked xfail(strict=True) assert the CORRECT behaviour for a defect the
MOD audit confirmed. They flip to a failure the day the defect is fixed, and
the marker goes with the fix.

No test here reaches the network: requests.get / requests.post are stubbed on
the shared `requests` module (monkeypatch restores them) and every database
call is redirected to the throwaway `db_path` file.
"""
import sqlite3

import pytest
import requests

import fetcher
import gmb
import models
import ops
from models import Review, save_reviews


# ── fixtures ───────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _redirect(db_path, monkeypatch):
    """Every module on these paths reaches the database through
    models.get_conn at call time (scheduler, fetcher, gmb, ops and ai_utils
    all import it lazily), except webhooks, which binds it at import."""
    real = models.get_conn
    fake = lambda *a, **k: real(db_path)  # noqa: E731
    monkeypatch.setattr(models, "get_conn", fake)
    monkeypatch.setattr(models, "DB_PATH", db_path)
    import webhooks
    monkeypatch.setattr(webhooks, "get_conn", fake, raising=False)
    monkeypatch.setattr(webhooks, "fire_webhook", lambda *a, **k: None)
    # A review fetch meters itself; the meter must not be what a test sees.
    monkeypatch.setattr(fetcher, "_meter_places", lambda *a, **k: None)
    yield


def _restaurant(db_path, rid=1, **kw):
    cols = {"id": rid, "name": f"R{rid}", "owner_email": f"o{rid}@x.test"}
    cols.update(kw)
    conn = sqlite3.connect(db_path)
    conn.execute(f"INSERT INTO restaurants ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                 tuple(cols.values()))
    conn.commit()
    conn.close()
    return rid


def _q(db_path, sql, args=()):
    conn = sqlite3.connect(db_path)
    try:
        return conn.execute(sql, args).fetchall()
    finally:
        conn.close()


def _captures(db_path):
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(ops._TABLE_SQL)
        return conn.execute("SELECT job, error, context FROM job_failures").fetchall()
    finally:
        conn.close()


class _Resp:
    def __init__(self, body, status=200):
        self._body, self.status_code = body, status
        self.text = str(body)

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code}")

    def json(self):
        return self._body


def _no_ai(monkeypatch):
    """run_daily_fetch analyses and drafts whatever it saved; neither may
    reach Anthropic here."""
    import analyser
    import drafter
    monkeypatch.setattr(analyser, "analyse_review", lambda *a, **k: None)
    monkeypatch.setattr(drafter, "draft_response", lambda *a, **k: None)
    import notify
    monkeypatch.setattr(notify, "deliver_alert", lambda *a, **k: None)
    monkeypatch.setattr(notify, "rush_release_at", lambda *a, **k: None)


def _run_fetch(monkeypatch):
    import scheduler
    _no_ai(monkeypatch)
    monkeypatch.setattr(scheduler, "auto_approve_five_stars", lambda *a, **k: 0)
    return scheduler.run_daily_fetch()


# ── MOD-REV-1: a non-OK Places status is a failed fetch (R1 #6) ─────────────

_DENIALS = [
    {"status": "REQUEST_DENIED", "error_message": "The provided API key is invalid."},
    {"status": "OVER_QUERY_LIMIT", "error_message": "You have exceeded your daily request quota."},
    {"status": "NOT_FOUND"},
    {"status": "INVALID_REQUEST"},
]


@pytest.mark.parametrize("body", _DENIALS, ids=[b["status"] for b in _DENIALS])
def test_a_places_response_that_is_not_ok_raises_instead_of_returning_nothing(db_path, monkeypatch, body):
    """Places reports a denied key, an exhausted quota and a dead Place ID as
    HTTP 200 with a `status` and no `result` — so raise_for_status never
    fires and the fetch returned an empty list."""
    _restaurant(db_path, 1, google_place_id="ChIJ_dead")
    monkeypatch.setattr(requests, "get", lambda *a, **k: _Resp(body))
    with pytest.raises(Exception):
        fetcher.fetch_google("ChIJ_dead", 1)


@pytest.mark.parametrize("status", ["OK", "ZERO_RESULTS"])
def test_an_ok_or_empty_places_response_is_still_a_successful_fetch(db_path, monkeypatch, status):
    """The fix must not turn a listing with no reviews into an outage."""
    _restaurant(db_path, 1, google_place_id="ChIJ_quiet")
    body = {"status": status, "result": {"reviews": []}} if status == "OK" else {"status": status}
    monkeypatch.setattr(requests, "get", lambda *a, **k: _Resp(body))
    assert fetcher.fetch_google("ChIJ_quiet", 1) == []


def test_a_denied_places_key_leaves_the_sync_stale_and_reaches_the_failure_log(db_path, monkeypatch):
    rid = _restaurant(db_path, 1, google_place_id="ChIJ_fake", reviews_live=1)
    monkeypatch.setattr(requests, "get", lambda *a, **k: _Resp(
        {"status": "REQUEST_DENIED", "error_message": "The provided API key is invalid."}))
    _run_fetch(monkeypatch)
    assert _q(db_path, "SELECT last_fetched_at FROM restaurants WHERE id=?", (rid,))[0][0] is None, \
        "a fetch Google refused was recorded as a fresh sync"
    assert any(job == "review_fetch" for job, _e, _c in _captures(db_path)), \
        "a Places denial reached no failure log"


# ── MOD-REV-8: the Places key never reaches job_failures (R1 #7) ────────────

_KEY = "AIzaSECRETKEY1234567890"


def _connection_error():
    return requests.exceptions.ConnectionError(
        "HTTPSConnectionPool(host='maps.googleapis.com', port=443): Max retries exceeded with url: "
        f"/maps/api/place/details/json?place_id=P&fields=reviews&key={_KEY}&reviews_sort=newest")


@pytest.mark.xfail(strict=True, reason="MOD-REV-8: ops.capture stores str(exc) unredacted, so a requests "
                                        "error carrying ?key= puts the Places key in job_failures")
def test_ops_capture_redacts_an_api_key_in_the_exception_text(db_path):
    ops.capture(_connection_error(), job="review_fetch", context="Google R1")
    rows = _captures(db_path)
    assert rows, "nothing was captured at all"
    assert all(_KEY not in (err or "") for _j, err, _c in rows)
    assert any("[redacted]" in (err or "") for _j, err, _c in rows)


@pytest.mark.xfail(strict=True, reason="MOD-REV-8: run_daily_fetch captures the raw Places connection error, "
                                        "key included")
def test_a_places_connection_error_during_the_fetch_does_not_log_the_key(db_path, monkeypatch):
    _restaurant(db_path, 1, google_place_id="ChIJ_x", reviews_live=1)

    def _boom(*a, **k):
        raise _connection_error()
    monkeypatch.setattr(requests, "get", _boom)
    _run_fetch(monkeypatch)
    rows = _captures(db_path)
    assert rows, "the failed fetch was not captured"
    assert all(_KEY not in (err or "") for _j, err, _c in rows)


def test_safe_error_already_redacts_the_key_it_is_given():
    """The redactor exists and works; REV-8 is that capture never calls it."""
    from ai_guard import safe_error
    assert _KEY not in safe_error(_connection_error())


# ── MOD-REV-3: one guest review is one row across sources (R1 #11) ──────────

def _places_copy(rid):
    body = {"status": "OK", "result": {"reviews": [{
        "time": 1757000000, "rating": 1, "text": "Cold food, never again",
        "author_name": "Ann", "author_url": "https://www.google.com/maps/contrib/111/reviews"}]}}
    return body


def _gbp_copy():
    # 1757000000 == 2025-09-04T15:33:20Z, the same instant as the Places copy.
    return {"reviews": [{
        "name": "accounts/1/locations/2/reviews/abc", "starRating": "ONE",
        "comment": "Cold food, never again", "createTime": "2025-09-04T15:33:20Z",
        "updateTime": "2025-09-04T15:33:20Z", "reviewer": {"displayName": "Ann"}}]}


def _save_both_copies(db_path, monkeypatch, rid):
    monkeypatch.setattr(requests, "get", lambda *a, **k: _Resp(_places_copy(rid)))
    places = fetcher.fetch_google("ChIJ_fake", rid)
    n1, new1 = save_reviews(places, db_path=db_path)
    monkeypatch.setattr(requests, "get", lambda *a, **k: _Resp(_gbp_copy()))
    via_gbp = gmb.fetch_reviews_via_gmb("tok", "locations/2", rid)
    n2, new2 = save_reviews(via_gbp, db_path=db_path)
    return new1, new2


@pytest.mark.xfail(strict=True, reason="MOD-REV-3: Places and GBP key the same review differently "
                                        "(google_<time>_<url> vs the resource name), so it is stored twice")
def test_the_same_review_through_places_then_gbp_is_one_row_with_its_review_name(db_path, monkeypatch):
    rid = _restaurant(db_path, 1, google_place_id="ChIJ_fake", timezone="UTC")
    _save_both_copies(db_path, monkeypatch, rid)
    rows = _q(db_path, "SELECT review_name FROM reviews WHERE restaurant_id=? AND deleted_at IS NULL", (rid,))
    assert len(rows) == 1, f"one guest review became {len(rows)} rows"
    assert rows[0][0] == "accounts/1/locations/2/reviews/abc", \
        "the surviving row cannot be replied to on Google"


# ── MOD-REV-7: GBP backfill gets past the newest 1,000 (R1 #13) ─────────────

def _paged_gbp(pages_requested, total_pages=30):
    def fake_get(url, headers=None, params=None, timeout=None):
        tok = (params or {}).get("pageToken")
        pages_requested.append(tok)
        n = int(tok or 0)
        revs = [{"name": f"accounts/1/locations/2/reviews/{n * 50 + i}", "starRating": "FIVE",
                 "comment": "x", "createTime": "2020-01-01T00:00:00Z",
                 "reviewer": {"displayName": "A"}} for i in range(50)]
        nxt = str(n + 1) if n + 1 < total_pages else None
        return _Resp({"reviews": revs, "nextPageToken": nxt})
    return fake_get


@pytest.mark.xfail(strict=True, reason="MOD-REV-7: no page token is persisted; every call starts at page 1, "
                                        "so reviews past the newest 1,000 are never fetched")
def test_a_second_gbp_fetch_continues_past_the_first_thousand(db_path, monkeypatch):
    rid = _restaurant(db_path, 1, gmb_refresh_token="rt", gmb_location_id="locations/2")
    pages = []
    monkeypatch.setattr(requests, "get", _paged_gbp(pages))
    first = gmb.fetch_reviews_via_gmb("tok", "locations/2", rid)
    save_reviews(first, db_path=db_path)
    second = gmb.fetch_reviews_via_gmb("tok", "locations/2", rid)
    ids = {int(r.external_id.rsplit("/", 1)[1]) for r in first + second}
    assert max(ids) >= 1000, "the older 500 reviews are never fetched, however many passes run"


@pytest.mark.xfail(strict=True, reason="MOD-REV-7: an incremental GBP fetch walks all 20 pages even when "
                                        "page 1 is entirely already stored")
def test_an_incremental_gbp_fetch_whose_first_page_is_known_stops_after_one_request(db_path, monkeypatch):
    rid = _restaurant(db_path, 1, gmb_refresh_token="rt", gmb_location_id="locations/2")
    pages = []
    monkeypatch.setattr(requests, "get", _paged_gbp(pages))
    # Everything Google would list is already stored.
    save_reviews([Review(restaurant_id=rid, platform="google",
                         external_id=f"accounts/1/locations/2/reviews/{i}", author="A", rating=5,
                         text="x", review_name=f"accounts/1/locations/2/reviews/{i}")
                  for i in range(1500)], db_path=db_path)
    pages.clear()
    gmb.fetch_reviews_via_gmb("tok", "locations/2", rid)
    assert len(pages) == 1, f"{len(pages)} GBP requests for a page of reviews we already have"


# ── MOD-REV-9: GBP review dates are the restaurant's local time (R1 #23) ────

@pytest.mark.xfail(strict=True, reason="MOD-REV-9: gmb stores createTime verbatim in UTC, so a 9:30pm CDT "
                                        "review is dated the next day")
def test_a_gbp_review_written_at_0230z_is_dated_the_chicago_evening_before(db_path, monkeypatch):
    rid = _restaurant(db_path, 1, timezone="America/Chicago")
    monkeypatch.setattr(requests, "get", lambda *a, **k: _Resp({"reviews": [{
        "name": "accounts/1/locations/2/reviews/eve", "starRating": "TWO", "comment": "Slow",
        "createTime": "2026-09-22T02:30:00Z", "reviewer": {"displayName": "E"}}]}))
    out = gmb.fetch_reviews_via_gmb("tok", "locations/2", rid)
    save_reviews(out, db_path=db_path)
    stored_day = _q(db_path, f"SELECT date({models.REVIEW_TIME_AXIS_BARE}) FROM reviews WHERE restaurant_id=?",
                    (rid,))[0][0]
    assert stored_day == "2026-09-21", f"a Monday-night review was bucketed on {stored_day}"


def test_a_places_review_written_at_0230z_is_already_dated_the_chicago_evening_before(db_path, monkeypatch):
    """The Places half of the same rule already holds; the two paths should
    agree, which is what REV-9 is about."""
    rid = _restaurant(db_path, 1, timezone="America/Chicago")
    # 2026-09-22T02:30:00Z
    monkeypatch.setattr(requests, "get", lambda *a, **k: _Resp({"status": "OK", "result": {"reviews": [
        {"time": 1790044200, "rating": 2, "text": "Slow", "author_name": "E",
         "author_url": "https://maps.google.com/u/9"}]}}))
    out = fetcher.fetch_google("ChIJ_x", rid)
    assert out[0].review_date.startswith("2026-09-21T21:30")


# ── MOD-REV-11: a Places window with more than five new reviews (R1 #14) ────

@pytest.mark.xfail(strict=True, reason="MOD-REV-11: Places returns at most 5 reviews and nothing compares "
                                        "user_ratings_total, so reviews beyond the newest five vanish unrecorded")
def test_a_places_fetch_that_misses_reviews_records_the_gap(db_path, monkeypatch):
    """Five new reviews came back while Google's own total rose by eight: the
    three that were never returned must be recorded somewhere an operator or
    owner can see, not silently lost."""
    rid = _restaurant(db_path, 1, google_place_id="ChIJ_busy", reviews_live=1)
    seen_fields = []

    def _reviews(start):
        return [{"time": 1757000000 + start + i, "rating": 5, "text": "ok", "author_name": f"G{start + i}",
                 "author_url": f"https://maps.google.com/u/{start + i}"} for i in range(5)]

    bodies = [
        {"status": "OK", "result": {"reviews": _reviews(0), "user_ratings_total": 100}},
        {"status": "OK", "result": {"reviews": _reviews(100), "user_ratings_total": 108}},
    ]

    def fake_get(url, params=None, timeout=None, **k):
        seen_fields.append((params or {}).get("fields", ""))
        return _Resp(bodies.pop(0))
    monkeypatch.setattr(requests, "get", fake_get)
    _run_fetch(monkeypatch)
    _run_fetch(monkeypatch)
    assert all("user_ratings_total" in f for f in seen_fields), \
        "the fetch never asks Google how many reviews exist, so a gap cannot be seen"
    trail = " ".join(str(e) for _j, e, _c in _captures(db_path))
    trail += " ".join(str(r[0]) for r in _q(db_path, "SELECT event_data FROM activity_log WHERE restaurant_id=?",
                                           (rid,)))
    assert "3" in trail and ("gap" in trail.lower() or "missed" in trail.lower()), \
        "three reviews Google has were never fetched and nothing recorded it"


# ── MOD-REV-12: a transient refresh failure is not "reconnect Google" (R1 #31) ─

def _gbp_restaurant(db_path):
    return _restaurant(db_path, 1, gmb_refresh_token="rt-live", gmb_location_id="locations/2",
                       google_place_id="ChIJ_fallback", reviews_live=1)


def _record_alerts(monkeypatch):
    import notify
    raised = []
    monkeypatch.setattr(notify, "raise_alert", lambda rid, kind, *a, **k: raised.append(kind) or True)
    return raised


def _places_fallback_ok(monkeypatch):
    monkeypatch.setattr(requests, "get", lambda *a, **k: _Resp({"status": "OK", "result": {"reviews": []}}))


@pytest.mark.xfail(strict=True, reason="MOD-REV-12: get_valid_token turns a timeout into None, which "
                                        "run_daily_fetch reports to the owner as a lost connection")
def test_a_token_refresh_that_times_out_does_not_tell_the_owner_to_reconnect(db_path, monkeypatch):
    _gbp_restaurant(db_path)
    raised = _record_alerts(monkeypatch)
    _places_fallback_ok(monkeypatch)

    def _timeout(*a, **k):
        raise requests.exceptions.Timeout("oauth2.googleapis.com read timed out")
    monkeypatch.setattr(requests, "post", _timeout)
    _run_fetch(monkeypatch)
    assert "connection_lost" not in raised


def _invalid_grant(*a, **k):
    return _Resp({"error": "invalid_grant", "error_description": "Token has been expired or revoked."}, 400)


def test_a_revoked_refresh_token_still_tells_the_owner_to_reconnect(db_path, monkeypatch):
    _gbp_restaurant(db_path)
    raised = _record_alerts(monkeypatch)
    _places_fallback_ok(monkeypatch)
    monkeypatch.setattr(requests, "post", _invalid_grant)
    _run_fetch(monkeypatch)
    assert "connection_lost" in raised


@pytest.mark.xfail(strict=True, reason="MOD-REV-12: a revoked (invalid_grant) refresh token is never cleared, "
                                        "so it is re-tried four times a day forever")
def test_a_revoked_refresh_token_is_cleared(db_path, monkeypatch):
    rid = _gbp_restaurant(db_path)
    _record_alerts(monkeypatch)
    _places_fallback_ok(monkeypatch)
    monkeypatch.setattr(requests, "post", _invalid_grant)
    _run_fetch(monkeypatch)
    assert _q(db_path, "SELECT gmb_refresh_token FROM restaurants WHERE id=?", (rid,))[0][0] is None


# ── MOD-REV-14: a review Google removed stops counting (R1 #19) ─────────────

@pytest.mark.xfail(strict=True, reason="MOD-REV-14: save_reviews is insert-or-edit only; a review missing from "
                                        "a complete GBP listing keeps counting in get_review_stats")
def test_a_review_missing_from_a_complete_gbp_listing_drops_out_of_the_stats(db_path, monkeypatch):
    rid = _gbp_restaurant(db_path)
    kept = {"name": "accounts/1/locations/2/reviews/kept", "starRating": "FIVE", "comment": "Great",
            "createTime": "2026-09-01T12:00:00Z", "reviewer": {"displayName": "K"}}
    gone = {"name": "accounts/1/locations/2/reviews/gone", "starRating": "ONE", "comment": "Fake review",
            "createTime": "2026-09-02T12:00:00Z", "reviewer": {"displayName": "F"}}
    monkeypatch.setattr(gmb, "get_valid_token", lambda rid: "tok")
    monkeypatch.setattr(gmb, "fetch_gmb_logo_url", lambda *a, **k: None)
    listings = [{"reviews": [kept, gone]}, {"reviews": [kept]}]   # no nextPageToken: complete
    monkeypatch.setattr(requests, "get", lambda *a, **k: _Resp(listings.pop(0)))
    _run_fetch(monkeypatch)
    assert models.get_review_stats(rid)["total"] == 2
    _run_fetch(monkeypatch)                  # Google took the fake 1-star down
    stats = models.get_review_stats(rid)
    assert stats["total"] == 1 and stats["avg_rating"] == 5.0, \
        "a review Google removed still drags the rating"


# ── R1 #20: a Places edit that moves `time` (unverified upstream) ───────────

def test_a_places_review_id_embeds_the_review_time():
    """R1 #20 is 'Unable to verify from implementation' whether Google moves a
    review's `time` when the guest edits it. What CAN be pinned is the
    dependency: the Places identity embeds `time`, so if Google does move it,
    the edit arrives as a new row. This records that shape so a change to it
    is deliberate. Not a defect assertion."""
    a = fetcher._places_external_id({"time": 1757000000, "author_url": "https://maps.google.com/u/1"})
    b = fetcher._places_external_id({"time": 1757000999, "author_url": "https://maps.google.com/u/1"})
    assert a != b


# ── R1 #32: CSV ingest (dev-only path, main.py) ────────────────────────────

def _csv(tmp_path, text, name="reviews.csv"):
    p = tmp_path / name
    p.write_bytes(text.encode("utf-8"))
    return str(p)


@pytest.mark.xfail(strict=True, reason="MOD A1 R1 #32: ingest_csv opens with utf-8, not utf-8-sig, so an "
                                        "Excel BOM turns the 'id' header into '\\ufeffid' and every row raises")
def test_csv_ingest_reads_a_file_saved_with_a_byte_order_mark(tmp_path):
    path = _csv(tmp_path, "﻿id,author,rating,text,date\nc1,Ann,4,Nice,2026-09-01\n")
    out = fetcher.ingest_csv(path, 1)
    assert [r.external_id for r in out] == ["c1"]


def test_csv_ingest_reads_a_plain_file(tmp_path):
    """Control for the BOM case: the happy path works."""
    path = _csv(tmp_path, "id,author,rating,text,date\nc1,Ann,4,Nice,2026-09-01\n")
    out = fetcher.ingest_csv(path, 1)
    assert [(r.external_id, r.rating) for r in out] == [("c1", 4)]


@pytest.mark.xfail(strict=True, reason="MOD A1 R1 #32: a row with a blank id is accepted with "
                                        "external_id '' instead of being refused")
def test_csv_ingest_skips_a_row_with_no_id_and_keeps_the_rest(tmp_path):
    path = _csv(tmp_path, "id,author,rating,text\n,Ann,4,Nice\nc2,Bob,5,Great\n")
    out = fetcher.ingest_csv(path, 1)
    assert [r.external_id for r in out] == ["c2"]


@pytest.mark.xfail(strict=True, reason="MOD A1 R1 #32: a '4.5' rating raises ValueError and discards the "
                                        "whole file instead of refusing or rounding that one row")
def test_csv_ingest_does_not_lose_the_file_to_one_fractional_rating(tmp_path):
    path = _csv(tmp_path, "id,author,rating,text\nc1,Ann,4.5,Nice\nc2,Bob,5,Great\n")
    out = fetcher.ingest_csv(path, 1)
    assert "c2" in [r.external_id for r in out]


@pytest.mark.xfail(strict=True, reason="MOD A1 R1 #32: a rating of 0 is accepted into a Review the table's "
                                        "CHECK(rating BETWEEN 1 AND 5) will refuse")
def test_csv_ingest_refuses_a_zero_rating(tmp_path):
    path = _csv(tmp_path, "id,author,rating,text\nc1,Ann,0,Nice\nc2,Bob,5,Great\n")
    out = fetcher.ingest_csv(path, 1)
    assert [r.external_id for r in out] == ["c2"]


# ── MOD-REV-18: the discarded "urgent" query ────────────────────────────────
# "Test to add: none needed beyond the existing backlog-sweep tests." No test
# is written: the finding is a candidate for future cleanup, and a test that
# pinned the query's absence would amount to recommending its deletion.
