"""MOD audit, Reviews, part 2: approving and publishing replies (appendix A1 R2).

What these tests protect: an approve is only ever an approve of a drafted
reply this restaurant owns; a double-click publishes once; a failure to
publish tells the owner what to do in words, never in a JSON fragment; and a
batch approve against a dead Google token does not hold a request thread
for minutes.

Tests marked xfail(strict=True) assert the CORRECT behaviour for a defect the
MOD audit confirmed; each flips to a failure the day it is fixed.

Google is never reached: gmb.post_reply / get_valid_token / requests.put are
stubbed per test, and the confirmation alert and webhooks are recorded, not
sent.
"""
import sqlite3
import threading

import pytest
import requests

import client_api
import gmb
import models
import notify


# ── fixtures ───────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _redirect(db_path, monkeypatch):
    """client_api resolves get_conn through models at call time; webhooks
    binds it at import, so it is patched by name too."""
    real = models.get_conn
    fake = lambda *a, **k: real(db_path)  # noqa: E731
    monkeypatch.setattr(models, "get_conn", fake)
    monkeypatch.setattr(models, "DB_PATH", db_path)
    import webhooks
    monkeypatch.setattr(webhooks, "get_conn", fake, raising=False)
    yield


@pytest.fixture
def world(db_path, monkeypatch):
    """Two restaurants, GBP connected on the first, with every side effect of
    an approve recorded rather than performed."""
    conn = sqlite3.connect(db_path)
    for rid in (1, 2):
        conn.execute("INSERT INTO restaurants (id, name, owner_email, gmb_refresh_token) VALUES (?,?,?,?)",
                     (rid, f"R{rid}", f"o{rid}@x.test", "rt"))
    conn.commit()
    conn.close()
    posts, alerts, hooks = [], [], []
    lock = threading.Lock()

    def _post(restaurant_id, name, text):
        with lock:
            posts.append((restaurant_id, name))
        return {"ok": True}

    def _alert(restaurant_id, review_id, posted=False, **k):
        with lock:
            alerts.append((restaurant_id, review_id, posted))

    import webhooks
    monkeypatch.setattr(gmb, "is_connected", lambda rid: True)
    monkeypatch.setattr(gmb, "post_reply", _post)
    monkeypatch.setattr(notify, "fire_response_approved_alert", _alert)
    monkeypatch.setattr(webhooks, "fire_webhook", lambda rid, event, payload: hooks.append((rid, event)))
    return {"db": db_path, "posts": posts, "alerts": alerts, "hooks": hooks}


def _review(db_path, rid, ext, *, status="drafted", draft="Thank you for coming in!",
            posted_at=None, deleted_at=None, rating=5, review_name="auto"):
    name = f"accounts/1/locations/{rid}/reviews/{ext}" if review_name == "auto" else review_name
    conn = sqlite3.connect(db_path)
    cur = conn.execute(
        "INSERT INTO reviews (restaurant_id, platform, external_id, author, rating, text, review_date, "
        "fetched_at, processed, response_status, draft_response, review_name, posted_at, deleted_at) "
        "VALUES (?,?,?,?,?,?,datetime('now','-1 day'),datetime('now'),1,?,?,?,?,?)",
        (rid, "google", ext, "Guest", rating, "Lovely evening", status, draft, name, posted_at, deleted_at))
    conn.commit()
    conn.close()
    return cur.lastrowid


def _row(db_path, review_id):
    conn = sqlite3.connect(db_path)
    try:
        return conn.execute("SELECT response_status, posted_at, approved_at, draft_response "
                            "FROM reviews WHERE id=?", (review_id,)).fetchone()
    finally:
        conn.close()


# ── the happy path the gate must keep working ──────────────────────────────

def test_approving_a_drafted_reply_publishes_it_once(world):
    rid = _review(world["db"], 1, "ok")
    payload, status = client_api._do_approve(rid, 1)
    assert status == 200 and payload["ok"] and payload["auto_posted"] is True
    assert len(world["posts"]) == 1
    assert _row(world["db"], rid)[0] == "posted"


# ── MOD-REV-4: approve has no status gate (R2 #2, #3, #4) ───────────────────

def _case(world, which):
    db = world["db"]
    if which == "already_posted":
        return _review(db, 1, "p", status="posted", posted_at="2026-09-01 12:00:00")
    if which == "pending_without_draft":
        return _review(db, 1, "n", status="pending", draft=None)
    if which == "soft_deleted":
        return _review(db, 1, "d", deleted_at="2026-09-10 12:00:00")
    if which == "another_restaurants_review":
        return _review(db, 2, "b")
    if which == "nonexistent":
        return 999999
    raise AssertionError(which)


@pytest.mark.parametrize("which", ["already_posted", "pending_without_draft", "soft_deleted",
                                   "another_restaurants_review", "nonexistent"])
def test_approving_anything_but_this_restaurants_drafted_reply_is_refused_and_changes_nothing(world, which):
    review_id = _case(world, which)
    before = _row(world["db"], review_id)
    payload, status = client_api._do_approve(review_id, 1)
    assert status != 200 and not payload.get("ok"), f"approving a {which} review returned {status} ok"
    assert _row(world["db"], review_id) == before, "the row changed"
    assert world["posts"] == [], "Google was asked to publish"
    assert world["alerts"] == [], "the owner was sent a confirmation about nothing"
    assert not [h for h in world["hooks"] if h[1].startswith("response.")], "a phantom webhook went out"


# ── MOD-REV-5: two approves at once publish once (R2 #5) ───────────────────

def test_two_simultaneous_approves_of_one_review_post_once_and_confirm_once(world):
    rid = _review(world["db"], 1, "race")
    start = threading.Barrier(2, timeout=5)
    results, errors = [], []

    def _go():
        try:
            start.wait()
            results.append(client_api._do_approve(rid, 1))
        except Exception as e:  # pragma: no cover - surfaced below
            errors.append(e)

    threads = [threading.Thread(target=_go) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(5)
    assert not errors, errors
    assert len(world["posts"]) == 1, f"Google was asked to publish {len(world['posts'])} times"
    assert len(world["alerts"]) == 1, f"{len(world['alerts'])} confirmations for one reply"
    assert sorted(s for _p, s in results)[0] == 200


def test_two_simultaneous_approve_alls_publish_each_reply_once(world, monkeypatch):
    ids = [_review(world["db"], 1, f"bulk{i}") for i in range(3)]
    start = threading.Barrier(2, timeout=5)
    # Hold each Google PUT until the other run is also mid-publish, so the
    # two runs overlap the way a real 10s-timeout PUT makes them overlap.
    # A run with nobody to meet (the fixed, compare-and-set code) waits
    # at most 0.1s once and then proceeds.
    overlap = threading.Barrier(2, timeout=0.1)
    record = gmb.post_reply

    def _slow_post(restaurant_id, name, text):
        try:
            overlap.wait()
        except threading.BrokenBarrierError:
            pass
        return record(restaurant_id, name, text)
    monkeypatch.setattr(gmb, "post_reply", _slow_post)

    def _go():
        start.wait()
        client_api._do_approve_all(1)

    threads = [threading.Thread(target=_go) for _ in range(2)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)
    assert sorted(name for _r, name in world["posts"]) == sorted(
        f"accounts/1/locations/1/reviews/bulk{i}" for i in range(3))
    assert len(world["alerts"]) == len(ids)


# ── real gmb.post_reply against a stubbed Google ───────────────────────────

class _Resp:
    def __init__(self, status, text):
        self.status_code, self.text = status, text


# Captured at import, before any fixture stubs it.
_REAL_POST_REPLY = gmb.post_reply


_GOOGLE_403 = ('{\n  "error": {\n    "code": 403,\n    "message": "The caller does not have permission",\n'
               '    "status": "PERMISSION_DENIED"\n  }\n}')


@pytest.fixture
def real_post(world, monkeypatch):
    monkeypatch.setattr(gmb, "post_reply", _REAL_POST_REPLY)
    monkeypatch.setattr(gmb, "get_valid_token", lambda rid: "tok")
    return world


@pytest.mark.parametrize("status,body", [
    (401, '{"error": {"code": 401, "status": "UNAUTHENTICATED"}}'),
    (403, _GOOGLE_403),
    (500, '{"error": {"code": 500, "status": "INTERNAL"}}'),
    (503, '{"error": {"code": 503, "status": "UNAVAILABLE"}}'),
], ids=["401", "403", "500", "503"])
def test_a_google_error_on_publish_reaches_the_owner_as_words_not_json(real_post, monkeypatch, status, body):
    rid = _review(real_post["db"], 1, f"e{status}")
    monkeypatch.setattr(requests, "put", lambda *a, **k: _Resp(status, body))
    payload, _ = client_api._do_approve(rid, 1)
    err = payload.get("post_error") or ""
    assert err, "a failed publish said nothing"
    assert "{" not in err and "API error" not in err, f"owner shown: {err!r}"


def test_a_network_failure_on_publish_is_not_shown_as_an_exception_string(real_post, monkeypatch):
    rid = _review(real_post["db"], 1, "net")

    def _boom(*a, **k):
        raise requests.exceptions.ConnectionError(
            "HTTPSConnectionPool(host='mybusinessreviews.googleapis.com', port=443): Max retries exceeded")
    monkeypatch.setattr(requests, "put", _boom)
    payload, _ = client_api._do_approve(rid, 1)
    err = payload.get("post_error") or ""
    assert err and "HTTPSConnectionPool" not in err, f"owner shown: {err!r}"


# ── MOD-REV-14: a reply to a review Google removed (R2 #15) ────────────────

def test_publishing_to_a_review_google_removed_says_it_was_removed(real_post, monkeypatch):
    rid = _review(real_post["db"], 1, "gone")
    monkeypatch.setattr(requests, "put", lambda *a, **k: _Resp(
        404, '{"error": {"code": 404, "status": "NOT_FOUND"}}'))
    payload, _ = client_api._do_approve(rid, 1)
    assert "removed" in (payload.get("post_error") or "").lower()


# ── R2 #16: a Places copy (no review_name) cannot post; the owner is told ──

@pytest.mark.xfail(strict=True, reason="MOD A1 R2 #16 (with MOD-REV-3): a Google review with no review_name "
                                        "is approved with auto_posted False and no reason, although GBP is "
                                        "connected and the reply will never reach Google")
def test_approving_a_review_that_cannot_be_posted_tells_the_owner_why(world):
    rid = _review(world["db"], 1, "google_1757000000_https://maps.google.com/u/1", review_name=None)
    payload, status = client_api._do_approve(rid, 1)
    assert payload.get("auto_posted") is False
    assert payload.get("post_error"), "GBP is connected, nothing was posted, and nothing says why"


# ── R2 #17: approve-all against an expired token holds the thread ─────────

@pytest.mark.xfail(strict=True, reason="MOD A1 R2 #17: every review in an approve-all batch re-attempts the "
                                        "token refresh (10s timeout each) after the first one has failed")
def test_approve_all_with_a_dead_token_tries_the_refresh_once_not_per_review(world, monkeypatch):
    """Up to 25 sequential 10-second refresh timeouts on one request thread,
    with --workers 1 --threads 4, is a quarter of the platform for minutes.
    Counted rather than timed: one failing refresh should end the batch's
    attempts to reach Google."""
    for i in range(5):
        _review(world["db"], 1, f"t{i}")
    monkeypatch.setattr(gmb, "post_reply", _REAL_POST_REPLY)
    refresh_calls = []

    def _timeout(url, *a, **k):
        refresh_calls.append(url)
        raise requests.exceptions.Timeout("oauth2.googleapis.com read timed out")
    monkeypatch.setattr(requests, "post", _timeout)
    monkeypatch.setattr(requests, "put", lambda *a, **k: pytest.fail("no token, so no PUT"))
    payload, status = client_api._do_approve_all(1)
    assert status == 200 and payload["approved"] == 5
    assert len(refresh_calls) <= 1, f"{len(refresh_calls)} token refreshes in one batch"
