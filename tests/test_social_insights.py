"""social_routes.py's Meta insights fetching — the resilient fallback (one
deprecated metric name must not zero out the whole response) and the shared
refresh_post_metrics() function used by both /api/post-insights and the
nightly scheduler sync."""
import social_routes as sr
from models import create_restaurant, save_reviews, update_restaurant, Restaurant, get_conn


class FakeResp:
    def __init__(self, status, body):
        self.status_code = status
        self._body = body
        self.text = str(body)

    def json(self):
        return self._body


class FakeReq:
    """Stands in for the `requests` module — returns responses in order,
    recording every call so tests can assert on fallback behavior."""
    def __init__(self, script):
        self.script = list(script)
        self.calls = []

    def get(self, url, params=None, timeout=None):
        self.calls.append((url, params))
        return self.script.pop(0)


def test_ig_metrics_succeed_on_first_try():
    fake = FakeReq([FakeResp(200, {"data": [
        {"name": "reach", "values": [{"value": 200}]},
        {"name": "impressions", "values": [{"value": 250}]},
    ]})])
    m = sr._ig_post_metrics("p1", "tok", fake)
    assert m == {"reach": 200, "impressions": 250}
    assert len(fake.calls) == 1


def test_ig_metrics_fall_back_when_impressions_rejected(monkeypatch):
    captured = []
    monkeypatch.setattr(sr, "_capture_insights_error",
                        lambda what, post_id, resp: captured.append(what))
    fake = FakeReq([
        FakeResp(400, {"error": {"message": "Invalid metric impressions"}}),
        FakeResp(200, {"data": [{"name": "reach", "values": [{"value": 120}]}]}),
    ])
    m = sr._ig_post_metrics("p1", "tok", fake)
    assert m == {"reach": 120}
    assert len(fake.calls) == 2
    assert captured  # the failed first attempt was reported, not silent


def test_ig_metrics_empty_when_both_attempts_fail(monkeypatch):
    monkeypatch.setattr(sr, "_capture_insights_error", lambda *a, **k: None)
    fake = FakeReq([FakeResp(400, {}), FakeResp(400, {})])
    assert sr._ig_post_metrics("p1", "tok", fake) == {}


def test_fb_metrics_engagement_and_reach_are_independent(monkeypatch):
    """A broken impressions metric must not also blank out likes/comments —
    they come from a separate request."""
    monkeypatch.setattr(sr, "_capture_insights_error", lambda *a, **k: None)
    fake = FakeReq([
        FakeResp(200, {"reactions": {"summary": {"total_count": 5}},
                      "comments": {"summary": {"total_count": 2}},
                      "shares": {"count": 1}}),
        FakeResp(400, {"error": {"message": "bad metric"}}),
        FakeResp(200, {"data": [{"name": "post_impressions_unique", "values": [{"value": 300}]}]}),
    ])
    m = sr._fb_post_metrics("p1", "tok", fake)
    assert m == {"likes": 5, "comments": 2, "shares": 1, "reach": 300}


def test_refresh_post_metrics_not_connected(monkeypatch, db_path):
    real_get_conn = __import__("models").get_conn
    monkeypatch.setattr("models.get_conn", lambda *a, **k: real_get_conn(db_path))
    rid = create_restaurant(Restaurant(name="No Meta", owner_email="n@x.com"), db_path=db_path)
    result = sr.refresh_post_metrics(rid)
    assert result == {"ok": False, "error": "Not connected", "posts": []}


def test_refresh_post_metrics_writes_back_to_db(monkeypatch, db_path):
    real_get_conn = __import__("models").get_conn
    monkeypatch.setattr("models.get_conn", lambda *a, **k: real_get_conn(db_path))

    rid = create_restaurant(
        Restaurant(name="Connected Spot", owner_email="c@x.com"),
        db_path=db_path)
    # ig_token/ig_user_id are set post-creation via the real OAuth callback
    # flow (update_restaurant), never through create_restaurant's INSERT —
    # matching how social_routes.instagram_callback() actually connects an account.
    update_restaurant(rid, {"ig_token": "ig-tok", "ig_user_id": "ig-user"}, db_path=db_path)

    conn = real_get_conn(db_path)
    conn.execute("""CREATE TABLE IF NOT EXISTS marketing_content_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT, restaurant_id INTEGER NOT NULL,
        content_type TEXT, topic TEXT, post_id TEXT, post_platform TEXT,
        created_at TEXT DEFAULT (datetime('now')),
        reach INTEGER DEFAULT 0, impressions INTEGER DEFAULT 0, engaged INTEGER DEFAULT 0,
        likes INTEGER DEFAULT 0, comments INTEGER DEFAULT 0, shares INTEGER DEFAULT 0)""")
    conn.execute(
        "INSERT INTO marketing_content_log (restaurant_id, content_type, topic, post_id, post_platform) VALUES (?,?,?,?,?)",
        (rid, "instagram_post", "Weekend brunch", "ig1", "instagram"))
    conn.commit()
    conn.close()

    fake = FakeReq([FakeResp(200, {"data": [
        {"name": "reach", "values": [{"value": 400}]},
        {"name": "likes", "values": [{"value": 30}]},
    ]})])
    # refresh_post_metrics() does `import requests as _req` inline every call —
    # swapping the module in sys.modules (auto-reverted by monkeypatch) is
    # the only way to intercept that without touching real network.
    import sys
    monkeypatch.setitem(sys.modules, "requests", fake)

    result = sr.refresh_post_metrics(rid)
    assert result["ok"] is True
    assert result["posts"][0]["metrics"]["reach"] == 400

    conn = real_get_conn(db_path)
    row = conn.execute("SELECT reach, likes FROM marketing_content_log WHERE post_id='ig1'").fetchone()
    conn.close()
    assert row["reach"] == 400
    assert row["likes"] == 30
