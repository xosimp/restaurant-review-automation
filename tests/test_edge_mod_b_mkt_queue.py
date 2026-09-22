"""Edge cases for the scheduled marketing post queue (marketing_publish.py).

The queue publishes to a restaurant's PUBLIC feed from a scheduler thread,
days after the owner last looked at the post. What these protect:

  * a post only ever carries its own restaurant's photo (MOD-MKT-1);
  * an ambiguous answer from Meta (5xx / 429 / a non-JSON gateway page) is
    never retried into a duplicate public post, and a failure where nothing
    was published is never burned as "may be live" (MOD-MKT-4);
  * one tick is bounded in wall-clock time (MOD-MKT-3) and nothing beyond a
    tick's batch is lost;
  * "11am" means 11am in each restaurant's own dining room, across time zones
    and DST changes;
  * the owner hears about a failed post, in owner-facing date format, at every
    owner address (MOD-MKT-18).

Graph is never reached: `requests` is swapped in sys.modules for a scripted
fake (social_routes imports it inside each function), and email stops at a
stubbed emails.deliver.
"""
import io
import re
import sqlite3
import sys
import time
import types
from datetime import datetime, timedelta, timezone

import pytest

import auth
import marketing_media
import marketing_publish as mp
import marketing_tags
import models
import outcomes
import social_routes
import time_utils
from models import Restaurant, create_restaurant

# Imported up front, not lazily inside a test: these swap `requests` in
# sys.modules for a fake, and a module first imported while the fake is in
# place (notify does `import requests` at top level) would keep it for the
# rest of the process.
import gmb  # noqa: E402,F401
import marketing  # noqa: E402,F401
import notify  # noqa: E402,F401
import scheduler  # noqa: E402,F401
import weather  # noqa: E402,F401


# ── fixtures ──────────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def _template_db(tmp_path_factory):
    """conftest's db_path runs the full init_db + ensure_columns migration
    (~0.6s) for every test. Built once here, with auth's schema, and copied."""
    path = str(tmp_path_factory.mktemp("mkt_queue_template") / "template.db")
    models.init_db(db_path=path)
    models.ensure_columns(db_path=path)
    auth.init_auth(db_path=path)
    return path


@pytest.fixture
def db_path(tmp_path, _template_db):
    """Overrides conftest's db_path with a copy of the module template —
    the same schema, a fresh file per test."""
    import os
    import shutil
    path = str(tmp_path / "test_reviews.db")
    for suffix in ("", "-wal", "-shm"):
        if os.path.exists(_template_db + suffix):
            shutil.copy(_template_db + suffix, path + suffix)
    return path

@pytest.fixture(autouse=True)
def _db(db_path, monkeypatch):
    """Every module here binds get_conn at import (CLAUDE.md "Bound
    imports"), so each is redirected by name."""
    real = models.get_conn
    redirect = lambda *a, **k: real(db_path)
    for mod in (models, auth, mp, marketing_media, marketing_tags, outcomes, social_routes):
        monkeypatch.setattr(mod, "get_conn", redirect, raising=False)
        monkeypatch.setattr(mod, "DB_PATH", db_path, raising=False)
    # ops.capture writes into job_failures; nothing here asserts on it except
    # where a test replaces it on purpose.
    import ops
    monkeypatch.setattr(ops, "capture", lambda *a, **k: None)
    monkeypatch.setattr(time, "sleep", lambda s: None)


@pytest.fixture
def mail(monkeypatch):
    """Every alert the queue sends, as (to, subject, html)."""
    import emails
    sent = []

    def fake_deliver(payload=None, restaurant_id=None, email_type=None, log_send=True):
        sent.append((list(payload.get("to") or []), payload.get("subject"), payload.get("html")))
        return types.SimpleNamespace(ok=True)
    monkeypatch.setattr(emails, "deliver", fake_deliver)
    return sent


def _restaurant(db_path, name="Queue Co", tz="America/Chicago", owner_email="owner@queue.test", connect=True):
    rid = create_restaurant(Restaurant(name=name, owner_email=owner_email, module_marketing=1,
                                       timezone=tz), db_path=db_path)
    if connect:
        conn = models.get_conn(db_path)
        conn.execute("UPDATE restaurants SET ig_token='igt', ig_user_id='igu', fb_page_token='fbt', "
                     "fb_page_id='fbp' WHERE id=?", (rid,))
        conn.commit()
        conn.close()
    return rid


def _png(size=(40, 30)):
    from PIL import Image
    buf = io.BytesIO()
    Image.new("RGB", size, (190, 70, 40)).save(buf, format="PNG")
    return buf.getvalue()


def _local(rid, hours=0.0):
    return (mp._local_now(rid) + timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%S")


def _insert_post(db_path, rid, platform="facebook", minutes_ago=5, media_id=None, body="Half price wings tonight",
                 scheduled_for=None):
    when = scheduled_for or (mp._local_now(rid) - timedelta(minutes=minutes_ago)).strftime("%Y-%m-%dT%H:%M:%S")
    conn = sqlite3.connect(db_path)
    cur = conn.execute(
        "INSERT INTO marketing_scheduled_posts (restaurant_id, platform, body, media_id, scheduled_for) "
        "VALUES (?,?,?,?,?)", (rid, platform, body, media_id, when))
    conn.commit()
    row_id = cur.lastrowid
    conn.close()
    return row_id


def _row(db_path, row_id):
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        return dict(conn.execute("SELECT * FROM marketing_scheduled_posts WHERE id=?", (row_id,)).fetchone())
    finally:
        conn.close()


class FakeResp:
    def __init__(self, status, body=None, text=None):
        self.status_code = status
        self._body = body
        self.text = text if text is not None else str(body)

    def json(self):
        if self._body is None:
            raise ValueError("Expecting value: line 1 column 1 (char 0)")
        return self._body


class FakeGraph:
    """Stands in for the `requests` module. Answers by (method, url
    substring), first match wins, and records every call with its kwargs."""

    def __init__(self, routes):
        self.routes = list(routes)
        self.calls = []

    def _answer(self, method, url, kw):
        self.calls.append((method, url, kw))
        for m, needle, resp in self.routes:
            if m == method and needle in url:
                return resp(url, kw) if callable(resp) else resp
        raise AssertionError(f"unexpected Graph call {method} {url}")

    def get(self, url, **kw):
        return self._answer("GET", url, kw)

    def post(self, url, **kw):
        return self._answer("POST", url, kw)

    def posts_to(self, needle):
        return [c for c in self.calls if c[0] == "POST" and needle in c[1]]


def _graph(monkeypatch, routes):
    fake = FakeGraph(routes)
    monkeypatch.setitem(sys.modules, "requests", fake)
    return fake


IG_OK = [
    ("POST", "igu/media_publish", FakeResp(200, {"id": "ig_post_1"})),
    ("POST", "igu/media", FakeResp(200, {"id": "container_1"})),
    ("GET", "container_1", FakeResp(200, {"status_code": "FINISHED"})),
]


# ── #14 horizon ───────────────────────────────────────────────────────────

def test_a_slot_more_than_six_months_out_is_refused_and_one_just_inside_is_taken(db_path):
    """A6 queue #14: a slot a year out is almost always a typo'd year; the
    queue refuses past 180 days and accepts anything inside it."""
    rid = _restaurant(db_path)
    far = mp.schedule_post(rid, "facebook", "Patio season", _local(rid, hours=24 * 181), db_path=db_path)
    assert not far["ok"]
    assert "six months" in far["error"]
    near = mp.schedule_post(rid, "facebook", "Patio season", _local(rid, hours=24 * 179), db_path=db_path)
    assert near["ok"]


# ── #15 foreign media (MOD-MKT-1) ─────────────────────────────────────────

def test_a_post_cannot_be_scheduled_with_another_restaurants_photo(db_path):
    """A6 queue #15 / MOD-MKT-1: media ids are sequential integers."""
    a = _restaurant(db_path, name="Alpha")
    b = _restaurant(db_path, name="Bravo")
    theirs = marketing_media.store_image(b, _png(), "image/png", db_path=db_path)
    result = mp.schedule_post(a, "instagram", "Our new menu", _local(a, 2), media_id=theirs["id"], db_path=db_path)
    assert not result["ok"], "restaurant A queued a post carrying restaurant B's photo"


def test_the_queue_listing_never_hands_back_another_restaurants_photo_token(db_path):
    """A6 queue #15 / MOD-MKT-1: the token is the only secret guarding the
    public /m/<token>.jpg route, so returning it is the leak."""
    a = _restaurant(db_path, name="Alpha")
    b = _restaurant(db_path, name="Bravo")
    theirs = marketing_media.store_image(b, _png(), "image/png", db_path=db_path)
    _insert_post(db_path, a, platform="instagram", minutes_ago=-120, media_id=theirs["id"])
    listed = mp.list_scheduled(a, db_path=db_path)
    assert listed, "setup: A's row should be listed"
    assert all(p.get("media_token") != theirs["token"] for p in listed)


def test_a_due_post_is_never_published_with_another_restaurants_photo(db_path, monkeypatch, mail):
    """A6 queue #15 / MOD-MKT-1: a row that already carries a foreign
    media_id (written before the fix) must not reach A's Instagram with B's
    image URL."""
    a = _restaurant(db_path, name="Alpha")
    b = _restaurant(db_path, name="Bravo")
    theirs = marketing_media.store_image(b, _png(), "image/png", db_path=db_path)
    _insert_post(db_path, a, platform="instagram", media_id=theirs["id"])
    urls = []
    monkeypatch.setattr(social_routes, "_do_post_to_instagram",
                        lambda rid, body, image_url, topic: urls.append(image_url) or ({"ok": True, "post_id": "x"}, 200))
    mp.run_due_posts(db_path=db_path)
    assert not any(theirs["token"] in (u or "") for u in urls), f"published with B's photo: {urls}"


# ── #16 photo deleted after scheduling ────────────────────────────────────

def test_a_photo_a_scheduled_post_still_needs_survives_a_delete_attempt(db_path, monkeypatch, mail):
    """A6 queue #16: the marketing_scheduled_posts.media_id foreign key
    blocks the delete, so the post still goes out with its photo."""
    rid = _restaurant(db_path)
    mine = marketing_media.store_image(rid, _png(), "image/png", db_path=db_path)
    _insert_post(db_path, rid, platform="instagram", media_id=mine["id"])
    try:
        deleted = marketing_media.delete_media(mine["id"], rid, db_path=db_path)
    except sqlite3.IntegrityError:
        deleted = False
    assert not deleted
    assert marketing_media.get_image(mine["token"], db_path=db_path) is not None
    fake = _graph(monkeypatch, IG_OK)
    assert mp.run_due_posts(db_path=db_path)["published"] == 1
    create = fake.posts_to("igu/media")[0]
    assert mine["token"] in create[2]["data"]["image_url"]


def test_a_post_whose_photo_vanished_fails_once_with_one_alert_and_never_posts_without_it(db_path, monkeypatch, mail):
    """A6 queue #16: on a legacy table without the foreign key the photo row
    can disappear. The post must never go out imageless, must fail after
    MAX_ATTEMPTS, and the owner is told once, about the photo."""
    rid = _restaurant(db_path)
    mine = marketing_media.store_image(rid, _png(), "image/png", db_path=db_path)
    row_id = _insert_post(db_path, rid, platform="instagram", media_id=mine["id"])
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA foreign_keys=OFF")
    conn.execute("DELETE FROM marketing_media WHERE id=?", (mine["id"],))
    conn.commit()
    conn.close()
    fake = _graph(monkeypatch, [])   # any Graph call at all is a failure
    for _ in range(mp.MAX_ATTEMPTS + 1):
        mp.run_due_posts(db_path=db_path)
    assert fake.calls == []
    assert _row(db_path, row_id)["status"] == "failed"
    assert len(mail) == 1
    assert "photo" in mail[0][2].lower()


# ── #17 token expired between scheduling and the slot ─────────────────────

def test_a_token_that_expired_after_scheduling_ends_in_one_reconnect_alert(db_path, monkeypatch, mail):
    """A6 queue #17: Meta answers OAuthException 190. Nothing was published,
    the post fails after its retries, and the one alert says to reconnect."""
    rid = _restaurant(db_path)
    row_id = _insert_post(db_path, rid)
    expired = FakeResp(400, {"error": {"message": "Error validating access token: Session has expired",
                                       "type": "OAuthException", "code": 190}})
    fake = _graph(monkeypatch, [("POST", "fbp/feed", expired)])
    for _ in range(mp.MAX_ATTEMPTS + 1):
        mp.run_due_posts(db_path=db_path)
    assert len(fake.posts_to("fbp/feed")) == mp.MAX_ATTEMPTS
    assert _row(db_path, row_id)["status"] == "failed"
    assert len(mail) == 1
    assert "Reconnect it under Account" in mail[0][2]


def test_a_platform_disconnected_after_scheduling_is_never_called_and_the_owner_is_told(db_path, monkeypatch, mail):
    """A6 queue #17: the owner disconnected Facebook after queueing. No
    Graph call is made with a blank token; the alert says reconnect."""
    rid = _restaurant(db_path)
    row_id = _insert_post(db_path, rid)
    models.update_restaurant(rid, {"fb_page_token": "", "fb_page_id": ""}, db_path=db_path)
    fake = _graph(monkeypatch, [])
    for _ in range(mp.MAX_ATTEMPTS):
        mp.run_due_posts(db_path=db_path)
    assert fake.calls == []
    assert _row(db_path, row_id)["status"] == "failed"
    assert len(mail) == 1 and "Reconnect" in mail[0][2]


# ── #18 5xx / 429 / non-JSON from media_publish or /feed (MOD-MKT-4) ──────

@pytest.mark.parametrize("status", [500, 502, 503, 429])
@pytest.mark.xfail(strict=True, reason="MOD-MKT-4: a 5xx/429 from /feed is treated as a definite refusal and the post is re-queued (can double-post)")
def test_an_ambiguous_answer_from_facebook_feed_is_never_retried(db_path, monkeypatch, mail, status):
    """A6 queue #18 / MOD-MKT-4: a 5xx or 429 after the request reached Meta
    may still have published. Retrying can put a second copy on the feed."""
    rid = _restaurant(db_path)
    row_id = _insert_post(db_path, rid)
    fake = _graph(monkeypatch, [("POST", "fbp/feed", FakeResp(status, {"error": {"message": "An unexpected error has occurred."}}))])
    mp.run_due_posts(db_path=db_path)
    mp.run_due_posts(db_path=db_path)
    row = _row(db_path, row_id)
    assert len(fake.posts_to("fbp/feed")) == 1, "the post was sent to /feed again after an ambiguous answer"
    assert row["status"] == "failed"
    assert "may already be live" in (row["error"] or "")


@pytest.mark.xfail(strict=True, reason="MOD-MKT-4: a 500 from Instagram media_publish is treated as definite and the post is re-queued")
def test_an_ambiguous_answer_from_instagram_media_publish_is_never_retried(db_path, monkeypatch, mail):
    """A6 queue #18 / MOD-MKT-4, the Instagram door."""
    rid = _restaurant(db_path)
    mine = marketing_media.store_image(rid, _png(), "image/png", db_path=db_path)
    row_id = _insert_post(db_path, rid, platform="instagram", media_id=mine["id"])
    fake = _graph(monkeypatch, [("POST", "igu/media_publish", FakeResp(500, {"error": {"message": "Internal"}}))] + IG_OK[1:])
    mp.run_due_posts(db_path=db_path)
    mp.run_due_posts(db_path=db_path)
    row = _row(db_path, row_id)
    assert len(fake.posts_to("igu/media_publish")) == 1
    assert row["status"] == "failed"
    assert "may already be live" in (row["error"] or "")


@pytest.mark.xfail(strict=True, reason="MOD-MKT-4: a non-JSON (HTML 502) body from media-create raises, is classed 'reached platform', and the post is failed for good though nothing was published")
def test_a_gateway_page_from_instagram_media_create_leaves_the_post_retryable(db_path, monkeypatch, mail):
    """A6 queue #18 / MOD-MKT-4 converse: media-create failing means nothing
    was published, so the post must stay queued for the next tick rather
    than being failed as "may already be live"."""
    rid = _restaurant(db_path)
    mine = marketing_media.store_image(rid, _png(), "image/png", db_path=db_path)
    row_id = _insert_post(db_path, rid, platform="instagram", media_id=mine["id"])
    _graph(monkeypatch, [("POST", "igu/media", FakeResp(502, None, text="<html><body>502 Bad Gateway</body></html>"))])
    mp.run_due_posts(db_path=db_path)
    row = _row(db_path, row_id)
    assert row["status"] == "scheduled", f"burned after a create that never succeeded: {row['status']} / {row['error']}"
    assert "may already be live" not in (row["error"] or "")


def test_a_definite_400_from_facebook_feed_is_still_retried(db_path, monkeypatch, mail):
    """A6 queue #18 control: a 4xx is Meta saying no, so the next tick may
    retry — the fix for MOD-MKT-4 must not stop this."""
    rid = _restaurant(db_path)
    row_id = _insert_post(db_path, rid)
    _graph(monkeypatch, [("POST", "fbp/feed", FakeResp(400, {"error": {"message": "(#100) Invalid parameter"}}))])
    mp.run_due_posts(db_path=db_path)
    assert _row(db_path, row_id)["status"] == "scheduled"
    assert mail == []


# ── #19 a big tick (MOD-MKT-3) ────────────────────────────────────────────

def _bulk_due(db_path, rid, n):
    when = (mp._local_now(rid) - timedelta(minutes=3)).strftime("%Y-%m-%dT%H:%M:%S")
    conn = sqlite3.connect(db_path)
    conn.executemany(
        "INSERT INTO marketing_scheduled_posts (restaurant_id, platform, body, scheduled_for) VALUES (?,?,?,?)",
        [(rid, "facebook", f"Post {i}", when) for i in range(n)])
    conn.commit()
    conn.close()


def _status_counts(db_path):
    conn = sqlite3.connect(db_path)
    try:
        return dict(conn.execute("SELECT status, COUNT(*) FROM marketing_scheduled_posts GROUP BY status").fetchall())
    finally:
        conn.close()


@pytest.mark.xfail(strict=True, reason="MOD-MKT-3: run_due_posts publishes up to 200 rows serially with no wall-clock budget")
def test_a_tick_with_250_due_posts_stops_on_a_wall_clock_budget_and_leaves_the_rest_scheduled(db_path, monkeypatch):
    """A6 queue #19 / MOD-MKT-3: each Instagram publish can take ~20s (the
    container poll). A popular slot across many restaurants must not hold
    the scheduler thread for an hour; the tick stops on a budget and the
    remainder waits, still 'scheduled', for the next tick."""
    rid = _restaurant(db_path)
    _bulk_due(db_path, rid, 250)
    clock = {"t": 1_000_000.0}
    monkeypatch.setattr(time, "time", lambda: clock["t"])
    monkeypatch.setattr(time, "monotonic", lambda: clock["t"])
    calls = []

    def slow_publish(*a, **kw):
        calls.append(kw.get("scheduled_post_id"))
        clock["t"] += 20.0          # one Instagram container poll at its ceiling
        return {"ok": True, "post_id": f"p{len(calls)}"}
    monkeypatch.setattr(mp, "publish_now", slow_publish)

    started = clock["t"]
    mp.run_due_posts(db_path=db_path)
    elapsed = clock["t"] - started

    assert elapsed <= 15 * 60, f"one tick held the scheduler for {elapsed/60:.0f} minutes ({len(calls)} serial publishes)"
    counts = _status_counts(db_path)
    assert counts.get("scheduled", 0) == 250 - len(calls)
    assert counts.get("failed", 0) == 0


def test_posts_beyond_one_ticks_batch_are_published_on_the_next_tick_exactly_once(db_path, monkeypatch):
    """A6 queue #19: nothing past the per-tick batch is lost or doubled."""
    rid = _restaurant(db_path)
    _bulk_due(db_path, rid, 250)
    calls = []
    monkeypatch.setattr(mp, "publish_now",
                        lambda *a, **kw: calls.append(kw["scheduled_post_id"]) or {"ok": True, "post_id": "p"})
    for _ in range(3):
        mp.run_due_posts(db_path=db_path)
    assert len(calls) == 250
    assert len(set(calls)) == 250
    assert _status_counts(db_path) == {"posted": 250}


# ── #20 / #21 wall clocks ─────────────────────────────────────────────────

class _FrozenDatetime(datetime):
    """time_utils' datetime, frozen at a UTC instant, so restaurant_now()
    still does its own ZoneInfo conversion."""
    frozen_utc = datetime(2026, 9, 24, 12, 0, tzinfo=timezone.utc)

    @classmethod
    def now(cls, tz=None):
        if tz is None:
            return cls.frozen_utc.replace(tzinfo=None)
        return cls.frozen_utc.astimezone(tz)


@pytest.fixture
def frozen(monkeypatch):
    monkeypatch.setattr(time_utils, "datetime", _FrozenDatetime)

    def set_utc(*args):
        _FrozenDatetime.frozen_utc = datetime(*args, tzinfo=timezone.utc)
    return set_utc


def test_two_restaurants_asking_for_11am_publish_at_their_own_11am(db_path, monkeypatch, frozen):
    """A6 queue #20: Chicago's 11am is 16:00 UTC; Los Angeles' is 18:00 UTC."""
    frozen(2026, 9, 24, 12, 0)
    chi = _restaurant(db_path, name="Chicago Spot", tz="America/Chicago")
    la = _restaurant(db_path, name="LA Spot", tz="America/Los_Angeles")
    a = mp.schedule_post(chi, "facebook", "Lunch", "2026-09-24T11:00", db_path=db_path)
    b = mp.schedule_post(la, "facebook", "Lunch", "2026-09-24T11:00", db_path=db_path)
    assert a["ok"] and b["ok"]
    calls = []
    monkeypatch.setattr(mp, "publish_now",
                        lambda rid, *x, **kw: calls.append(rid) or {"ok": True, "post_id": "p"})

    frozen(2026, 9, 24, 16, 5)          # 11:05 Chicago, 9:05 LA
    mp.run_due_posts(db_path=db_path)
    assert calls == [chi]

    frozen(2026, 9, 24, 18, 5)          # 11:05 LA
    mp.run_due_posts(db_path=db_path)
    assert calls == [chi, la]


def test_a_slot_inside_the_spring_forward_gap_goes_out_at_the_first_tick_after_it(db_path, monkeypatch, frozen):
    """A6 queue #21: 2:30am on 3/14/27 does not exist in Chicago. The post
    goes out when the clock jumps to 3:00, once, and is not failed as late."""
    frozen(2027, 3, 13, 18, 0)
    rid = _restaurant(db_path)
    made = mp.schedule_post(rid, "facebook", "Early bird", "2027-03-14T02:30", db_path=db_path)
    assert made["ok"]
    calls = []
    monkeypatch.setattr(mp, "publish_now", lambda *a, **kw: calls.append(1) or {"ok": True, "post_id": "p"})

    frozen(2027, 3, 14, 7, 55)          # 1:55 CST, before the gap
    mp.run_due_posts(db_path=db_path)
    assert calls == []
    frozen(2027, 3, 14, 8, 0)           # 3:00 CDT, straight after it
    mp.run_due_posts(db_path=db_path)
    assert calls == [1]
    assert _row(db_path, made["id"])["status"] == "posted"


def test_a_slot_inside_the_repeated_fall_back_hour_is_published_exactly_once(db_path, monkeypatch, frozen):
    """A6 queue #21: 1:30am on 11/1/26 happens twice in Chicago. The first
    one publishes; the second must not publish it again."""
    frozen(2026, 10, 31, 18, 0)
    rid = _restaurant(db_path)
    made = mp.schedule_post(rid, "facebook", "Brunch", "2026-11-01T01:30", db_path=db_path)
    assert made["ok"]
    calls = []
    monkeypatch.setattr(mp, "publish_now", lambda *a, **kw: calls.append(1) or {"ok": True, "post_id": "p"})

    frozen(2026, 11, 1, 6, 35)          # 1:35 CDT, the first time
    mp.run_due_posts(db_path=db_path)
    frozen(2026, 11, 1, 7, 35)          # 1:35 CST, the second time
    mp.run_due_posts(db_path=db_path)
    assert calls == [1]


def test_an_unreadable_scheduled_time_fails_that_row_and_the_tick_carries_on(db_path, monkeypatch, mail):
    """A6 queue #21: a corrupt row must not take the rest of the queue down."""
    rid = _restaurant(db_path)
    bad = _insert_post(db_path, rid, scheduled_for="next tuesday-ish")
    good = _insert_post(db_path, rid)
    calls = []
    monkeypatch.setattr(mp, "publish_now",
                        lambda *a, **kw: calls.append(kw["scheduled_post_id"]) or {"ok": True, "post_id": "p"})
    mp.run_due_posts(db_path=db_path)
    assert calls == [good]
    assert _row(db_path, bad)["status"] == "failed"
    assert _row(db_path, good)["status"] == "posted"


@pytest.mark.xfail(strict=True, reason="MOD-A6-queue-21: a post failed for an unreadable scheduled time is failed silently — every other terminal failure alerts the owner")
def test_an_unreadable_scheduled_time_tells_the_owner_the_post_did_not_go_out(db_path, mail):
    """A6 queue #21: the module's own rule is that a scheduled post that
    fails is invisible unless the owner is told; this path skips the alert."""
    rid = _restaurant(db_path)
    _insert_post(db_path, rid, scheduled_for="not a time")
    mp.run_due_posts(db_path=db_path)
    assert len(mail) == 1


# ── #22 who hears about a failure (MOD-MKT-18) ────────────────────────────

def _fail_row(rid, scheduled_for="2026-09-22T11:00:00"):
    return {"id": 1, "restaurant_id": rid, "platform": "facebook", "topic": "Wings",
            "body": "Half price wings tonight", "scheduled_for": scheduled_for}


@pytest.mark.xfail(strict=True, reason="MOD-MKT-18: the failed-post alert prints the slot as an ISO date ('2026-09-22 11:00')")
def test_the_failed_post_alert_writes_the_slot_as_m_d_yy(db_path, mail):
    """MOD-MKT-18: CLAUDE.md — an ISO date in owner-facing text is a bug."""
    rid = _restaurant(db_path)
    mp._alert_failed_post(_fail_row(rid), "Facebook not connected", db_path=db_path)
    assert len(mail) == 1
    html = mail[0][2]
    assert "9/22/26" in html
    assert not re.search(r"\d{4}-\d{2}-\d{2}", html)


@pytest.mark.xfail(strict=True, reason="MOD-MKT-18: the failed-post alert goes only to restaurants.owner_email, never to the other owner logins")
def test_a_failed_post_reaches_every_owner_login_not_just_owner_email(db_path, mail):
    """A6 queue #22 / MOD-MKT-18: two partners each with an owner login."""
    rid = _restaurant(db_path, owner_email="first@queue.test")
    auth.create_user(rid, "first", "first@queue.test", "correct-horse-battery-1", db_path=db_path)
    auth.create_user(rid, "partner", "partner@queue.test", "correct-horse-battery-2", db_path=db_path)
    mp._alert_failed_post(_fail_row(rid), "Facebook not connected", db_path=db_path)
    told = {addr for to, _s, _h in mail for addr in to}
    assert {"first@queue.test", "partner@queue.test"} <= told


@pytest.mark.xfail(strict=True, reason="MOD-MKT-18: a restaurant with a blank owner_email gets no failed-post signal even though an owner login has an email")
def test_a_restaurant_with_no_owner_email_still_hears_about_a_failed_post(db_path, mail):
    """A6 queue #22 / MOD-MKT-18."""
    rid = _restaurant(db_path, owner_email="")
    auth.create_user(rid, "boss", "boss@queue.test", "correct-horse-battery-3", db_path=db_path)
    mp._alert_failed_post(_fail_row(rid), "Facebook not connected", db_path=db_path)
    told = {addr for to, _s, _h in mail for addr in to}
    assert "boss@queue.test" in told
