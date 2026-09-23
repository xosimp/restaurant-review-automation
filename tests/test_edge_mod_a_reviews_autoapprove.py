"""MOD audit, Reviews, part 3: auto-approve (appendix A1 R3).

Auto-approve is the one path that publishes a model-written reply to a live
Google listing with nobody reading it. What these tests protect: the owner's
daily cap is a cap on the owner's day, a reply that failed to publish is not
counted as published, a draft the guard refused is reported once rather than
four times a day forever, and a first-connect backlog does not spend the
day's cap on replies to reviews from years ago.

Tests marked xfail(strict=True) assert the CORRECT behaviour for a defect the
MOD audit confirmed; each flips to a failure the day it is fixed.
"""
import os
import sqlite3
import time

import pytest

import client_api  # noqa: F401  (auto_approve_five_stars imports _do_approve from it)
import gmb
import models
import notify
import ops


# ── fixtures ───────────────────────────────────────────────────────────────

def _redirect_to(db_path, monkeypatch, wrap=None):
    real = models.get_conn
    if wrap is None:
        fake = lambda *a, **k: real(db_path)  # noqa: E731
    else:
        fake = lambda *a, **k: wrap(real(db_path))  # noqa: E731
    monkeypatch.setattr(models, "get_conn", fake)
    monkeypatch.setattr(models, "DB_PATH", db_path)
    import webhooks
    monkeypatch.setattr(webhooks, "get_conn", fake, raising=False)
    monkeypatch.setattr(webhooks, "fire_webhook", lambda *a, **k: None)


@pytest.fixture(autouse=True)
def _redirect(db_path, monkeypatch):
    _redirect_to(db_path, monkeypatch)
    monkeypatch.setattr(notify, "fire_response_approved_alert", lambda *a, **k: None)
    monkeypatch.setattr(gmb, "is_connected", lambda rid: True)
    yield


@pytest.fixture
def posts(monkeypatch):
    sent = []
    monkeypatch.setattr(gmb, "post_reply", lambda rid, name, text: sent.append(name) or {"ok": True})
    return sent


def _restaurant(db_path, rid=1, **kw):
    cols = {"id": rid, "name": f"R{rid}", "owner_email": f"o{rid}@x.test",
            "auto_approve_5star": 1, "auto_approve_daily_cap": 5, "gmb_refresh_token": "rt",
            "timezone": "America/Chicago"}
    cols.update(kw)
    conn = sqlite3.connect(db_path)
    conn.execute(f"INSERT INTO restaurants ({','.join(cols)}) VALUES ({','.join('?' * len(cols))})",
                 tuple(cols.values()))
    conn.commit()
    conn.close()
    return rid


def _drafted(db_path, rid, ext, *, draft="Thank you so much for the kind words!",
             written="datetime('now','-1 day')", fetched="datetime('now')"):
    conn = sqlite3.connect(db_path)
    cur = conn.execute(
        "INSERT INTO reviews (restaurant_id, platform, external_id, author, rating, text, review_date, "
        f"fetched_at, processed, response_status, draft_response, review_name, urgency) "
        f"VALUES (?,?,?,?,5,?,{written},{fetched},1,'drafted',?,?,'normal')",
        (rid, "google", ext, "Guest", "Wonderful", draft, f"accounts/1/locations/{rid}/reviews/{ext}"))
    conn.commit()
    conn.close()
    return cur.lastrowid


def _status(db_path, review_id):
    conn = sqlite3.connect(db_path)
    try:
        return conn.execute("SELECT response_status FROM reviews WHERE id=?", (review_id,)).fetchone()[0]
    finally:
        conn.close()


def _run(db_path, rid):
    import scheduler
    return scheduler.auto_approve_five_stars(rid, models.get_restaurant(rid))


def _captures(db_path, job):
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(ops._TABLE_SQL)
        return conn.execute("SELECT error, context FROM job_failures WHERE job=?", (job,)).fetchall()
    finally:
        conn.close()


# ── the rule working as intended ───────────────────────────────────────────

def test_a_published_auto_approval_counts_toward_the_cap(db_path, posts):
    rid = _restaurant(db_path)
    _drafted(db_path, rid, "a")
    assert _run(db_path, rid) == 1
    assert posts == ["accounts/1/locations/1/reviews/a"]
    assert models.count_auto_approved_today(rid) == 1


# ── MOD-REV-10: a failed publish is recorded, not counted (R3 #6) ───────────

def test_an_auto_approval_that_google_refused_is_not_counted_and_is_reported(db_path, monkeypatch):
    rid = _restaurant(db_path)
    review_id = _drafted(db_path, rid, "refused")
    monkeypatch.setattr(gmb, "post_reply", lambda *a, **k: {
        "ok": False, "error": "API error 401: token expired"})
    _run(db_path, rid)
    assert models.count_auto_approved_today(rid) == 0, \
        "a reply that never reached Google used up the owner's daily cap"
    captured = _captures(db_path, "auto_approve_five_stars") + _captures(db_path, "review_post")
    assert any(str(review_id) in (ctx or "") + (err or "") for err, ctx in captured), \
        "a failed auto-publish reached no failure log"


# ── MOD-REV-13: the cap is the restaurant's day, not the server's (R3 #5) ───

class _FrozenNow:
    """A connection whose SQL clock reads a fixed instant.

    SQLite's 'now' cannot be patched, so every `'now'` literal in a query is
    rewritten to the frozen UTC instant before it runs. That is exactly what
    count_auto_approved_today's `date('now','localtime')` needs to be
    deterministic. If a fix moves the day boundary into Python (time_utils),
    freeze the same instant there too."""

    def __init__(self, conn, instant):
        self._c, self._instant = conn, instant

    def execute(self, sql, *a, **k):
        return self._c.execute(sql.replace("'now'", f"'{self._instant}'"), *a, **k)

    def __getattr__(self, name):
        return getattr(self._c, name)


@pytest.fixture
def utc_process():
    """Railway containers run in UTC; so does this test."""
    old = os.environ.get("TZ")
    os.environ["TZ"] = "UTC"
    time.tzset()
    try:
        yield
    finally:
        if old is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = old
        time.tzset()


def _approved_at(db_path, rid, stamps):
    conn = sqlite3.connect(db_path)
    for s in stamps:
        # log_event stamps America/Chicago wall time.
        conn.execute("INSERT INTO activity_log (restaurant_id, event_type, event_data, created_at) "
                     "VALUES (?, 'review_auto_approved', '{}', ?)", (rid, s))
    conn.commit()
    conn.close()


# 7:40pm CDT on 9/22 is 00:40 UTC on 9/23 — the 8pm Chicago fetch slot.
_EVENING_SLOT_UTC = "2026-09-23 00:40:00"
# 4pm CDT on 9/22 is 21:00 UTC the same day.
_AFTERNOON_SLOT_UTC = "2026-09-22 21:00:00"


def test_five_approvals_at_730pm_ct_still_count_at_the_8pm_ct_slot(db_path, monkeypatch, utc_process):
    rid = _restaurant(db_path)
    _approved_at(db_path, rid, ["2026-09-22T19:30:00"] * 5)
    _redirect_to(db_path, monkeypatch, wrap=lambda c: _FrozenNow(c, _EVENING_SLOT_UTC))
    assert models.count_auto_approved_today(rid) == 5


def test_five_approvals_at_730pm_ct_count_at_the_4pm_ct_slot_the_same_day(db_path, monkeypatch, utc_process):
    """Control: before 7pm CT the UTC date and the Chicago date agree, which
    is why the bug only shows in the evening."""
    rid = _restaurant(db_path)
    _approved_at(db_path, rid, ["2026-09-22T15:30:00"] * 5)
    _redirect_to(db_path, monkeypatch, wrap=lambda c: _FrozenNow(c, _AFTERNOON_SLOT_UTC))
    assert models.count_auto_approved_today(rid) == 5


def test_yesterdays_approvals_do_not_count_toward_today(db_path, monkeypatch, utc_process):
    rid = _restaurant(db_path)
    _approved_at(db_path, rid, ["2026-09-21T19:30:00"] * 5)
    _redirect_to(db_path, monkeypatch, wrap=lambda c: _FrozenNow(c, _EVENING_SLOT_UTC))
    assert models.count_auto_approved_today(rid) == 0


def test_a_restaurant_at_its_cap_publishes_nothing_more_at_the_8pm_ct_slot(db_path, monkeypatch, utc_process,
                                                                             posts):
    rid = _restaurant(db_path)
    _approved_at(db_path, rid, ["2026-09-22T19:30:00"] * 5)
    _drafted(db_path, rid, "sixth")
    _redirect_to(db_path, monkeypatch, wrap=lambda c: _FrozenNow(c, _EVENING_SLOT_UTC))
    _run(db_path, rid)
    assert posts == [], "a sixth unread reply went out on a day capped at five"


# ── MOD-REV-16: a held draft is reported once (R3 #7) ──────────────────────

def test_a_held_draft_is_captured_once_across_two_runs(db_path, posts):
    rid = _restaurant(db_path)
    _drafted(db_path, rid, "linky", draft="Thanks! See our menu at https://example.com/menu")
    _run(db_path, rid)
    _run(db_path, rid)
    assert posts == []
    assert len(_captures(db_path, "auto_approve_five_stars")) == 1, \
        "the same held draft reached the failure digest again"
    conn = sqlite3.connect(db_path)
    held = conn.execute("SELECT COUNT(*) FROM activity_log WHERE restaurant_id=? "
                        "AND event_type='review_auto_approve_held'", (rid,)).fetchone()[0]
    conn.close()
    assert held == 1


def test_a_held_draft_is_never_published(db_path, posts):
    """The half of R3 #7 that already holds."""
    rid = _restaurant(db_path)
    review_id = _drafted(db_path, rid, "linky", draft="Thanks! See our menu at https://example.com/menu")
    _run(db_path, rid)
    assert posts == [] and _status(db_path, review_id) == "drafted"


# ── R3 #9: a first-connect backlog does not spend the cap on old reviews ───

def test_the_daily_cap_goes_to_this_weeks_review_not_a_2019_backlog(db_path, posts):
    rid = _restaurant(db_path, auto_approve_daily_cap=1)
    old = _drafted(db_path, rid, "backlog2019", written="'2019-05-01T12:00:00'",
                   fetched="datetime('now','-2 hours')")
    new = _drafted(db_path, rid, "thisweek", written="datetime('now','-1 day')",
                   fetched="datetime('now')")
    _run(db_path, rid)
    assert _status(db_path, new) in ("approved", "posted"), "this week's guest was not thanked"
    assert _status(db_path, old) == "drafted", "the day's one unread reply went to a 2019 review"
