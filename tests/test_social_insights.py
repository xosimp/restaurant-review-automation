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
    """One call, with metric names Graph v21 accepts for media insights
    (MB-7, 9/28/26): no `impressions` (retired) and no `comments_count` (a
    media field, not an insights metric) — either refused the whole list."""
    fake = FakeReq([FakeResp(200, {"data": [
        {"name": "reach", "values": [{"value": 200}]},
        {"name": "likes", "values": [{"value": 20}]},
        {"name": "comments", "values": [{"value": 3}]},
        {"name": "shares", "values": [{"value": 1}]},
    ]})])
    m = sr._ig_post_metrics("p1", "tok", fake)
    assert m == {"reach": 200, "likes": 20, "comments": 3, "shares": 1}
    assert len(fake.calls) == 1
    asked = fake.calls[0][1]["metric"].split(",")
    assert "impressions" not in asked and "comments_count" not in asked


def test_ig_metrics_fall_back_when_the_metric_list_is_rejected(monkeypatch):
    """A refused list: reach alone, then likes and comments from the media
    object's own fields — the failed first attempt reported, not silent."""
    captured = []
    monkeypatch.setattr(sr, "_capture_insights_error",
                        lambda what, post_id, resp: captured.append(what))
    fake = FakeReq([
        FakeResp(400, {"error": {"message": "(#100) metric[3] must be one of the following values"}}),
        FakeResp(200, {"data": [{"name": "reach", "values": [{"value": 120}]}]}),
        FakeResp(200, {"like_count": 9, "comments_count": 2, "id": "p1"}),
    ])
    m = sr._ig_post_metrics("p1", "tok", fake)
    assert m == {"reach": 120, "likes": 9, "comments": 2}
    assert len(fake.calls) == 3
    assert fake.calls[1][1]["metric"] == "reach"
    assert fake.calls[2][1]["fields"] == "like_count,comments_count"
    assert captured


def test_ig_metrics_empty_when_every_attempt_fails(monkeypatch):
    monkeypatch.setattr(sr, "_capture_insights_error", lambda *a, **k: None)
    fake = FakeReq([FakeResp(400, {}), FakeResp(400, {}), FakeResp(400, {})])
    assert sr._ig_post_metrics("p1", "tok", fake) == {}


def test_fb_metrics_read_views_reactions_and_activity_from_insights(monkeypatch):
    """The answer Meta gives a Page post today (read live 10/6/26): reach is
    unique viewers, impressions views, likes every reaction, comments and
    shares the activity counts. One call, no post fields (those need
    pages_read_user_content)."""
    monkeypatch.setattr(sr, "_capture_insights_error", lambda *a, **k: None)
    fake = FakeReq([FakeResp(200, {"data": [
        {"name": "post_total_media_view_unique", "values": [{"value": 300}]},
        {"name": "post_media_view", "values": [{"value": 420}]},
        {"name": "post_reactions_by_type_total", "values": [{"value": {"like": 4, "love": 1}}]},
        {"name": "post_activity_by_action_type", "values": [{"value": {"comment": 2, "share": 1, "like": 5}}]},
    ]})])
    m = sr._fb_post_metrics("p1", "tok", fake)
    assert m == {"reach": 300, "impressions": 420, "likes": 5, "comments": 2, "shares": 1}
    assert len(fake.calls) == 1 and fake.calls[0][0].endswith("p1/insights")
    assert "post_impressions" not in fake.calls[0][1]["metric"]


def test_fb_metrics_a_refused_list_is_asked_one_metric_at_a_time(monkeypatch):
    """One retired name must not blank the rest: each metric is asked for
    alone, and what Meta refuses is simply not measured."""
    errors = []
    monkeypatch.setattr(sr, "_capture_insights_error", lambda what, *a, **k: errors.append(what))
    fake = FakeReq([
        FakeResp(400, {"error": {"message": "(#100) The value must be a valid insights metric"}}),
        FakeResp(200, {"data": [{"name": "post_total_media_view_unique", "values": [{"value": 12}]}]}),
        FakeResp(400, {"error": {"message": "(#100) The value must be a valid insights metric"}}),
        FakeResp(200, {"data": [{"name": "post_reactions_by_type_total", "values": [{"value": {}}]}]}),
        FakeResp(200, {"data": [{"name": "post_activity_by_action_type", "values": [{"value": {}}]}]}),
    ])
    m = sr._fb_post_metrics("p1", "tok", fake)
    # {} is Meta's answer for none, so 0; views were refused, so not measured.
    assert m == {"reach": 12, "likes": 0, "comments": 0, "shares": 0}
    assert "impressions" not in m and len(errors) == 2


def test_fb_metrics_nothing_answered_is_never_written_as_zero(monkeypatch):
    monkeypatch.setattr(sr, "_capture_insights_error", lambda *a, **k: None)
    fake = FakeReq([FakeResp(400, {"error": {"message": "expired"}})] * 5)
    assert sr._fb_post_metrics("p1", "tok", fake) == {}


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
        {"name": "comments", "values": [{"value": 4}]},
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
