"""client_api.py's /api/mkt-performance aggregation — total reach/engagement
and top-post selection across a restaurant's posted content.

It used to test a copy of the route's SQL; that copy summed reach and
impressions as "reach" and crowned the only measured post "top performing"
(MB-6, AUX-13, 9/28/26). It now calls the one body behind the web route and
its phone twin, client_api._do_mkt_performance, against the fixture DB."""
import pytest

import client_api
import marketing_signals
import models
from models import create_restaurant, Restaurant, get_conn


@pytest.fixture(autouse=True)
def _db(db_path, monkeypatch):
    real = models.get_conn
    monkeypatch.setattr(models, "get_conn", lambda *a, **k: real(db_path))
    monkeypatch.setattr(marketing_signals, "get_conn", lambda *a, **k: real(db_path))


def _seed_post(db_path, rid, topic, post_id, platform, reach=0, likes=0, comments=0, shares=0,
               impressions=0):
    conn = get_conn(db_path)
    conn.execute(
        """INSERT INTO marketing_content_log
           (restaurant_id, content_type, topic, post_id, post_platform, reach, impressions, likes,
            comments, shares)
           VALUES (?,?,?,?,?,?,?,?,?,?)""",
        (rid, "instagram_post", topic, post_id, platform, reach, impressions, likes, comments, shares)
    )
    conn.commit()
    conn.close()


def _seed_draft(db_path, rid, topic):
    """A generated-but-never-posted piece — post_id stays NULL."""
    conn = get_conn(db_path)
    conn.execute(
        "INSERT INTO marketing_content_log (restaurant_id, content_type, topic) VALUES (?,?,?)",
        (rid, "instagram_post", topic)
    )
    conn.commit()
    conn.close()


def _perf(rid):
    payload, status = client_api._do_mkt_performance(rid)
    assert status == 200, payload
    return payload


def test_no_posts_yet(db_path):
    rid = create_restaurant(Restaurant(name="Fresh Spot", owner_email="f@x.com"), db_path=db_path)
    result = _perf(rid)
    assert (result["published"], result["has_data"], result["total_reach"],
            result["total_engagement"], result["top_post"]) == (0, False, 0, 0, None)


def test_draft_without_post_id_excluded_from_totals(db_path):
    rid = create_restaurant(Restaurant(name="Drafting Spot", owner_email="d@x.com"), db_path=db_path)
    _seed_draft(db_path, rid, "Idea never published")
    result = _perf(rid)
    assert result["published"] == 0
    assert result["has_data"] is False


def test_totals_and_no_top_post_under_the_floor(db_path):
    """Two measured posts are totals, not a "top performing post" (AUX-13)."""
    rid = create_restaurant(Restaurant(name="Active Spot", owner_email="a@x.com"), db_path=db_path)
    _seed_post(db_path, rid, "Weekend brunch", "ig1", "instagram", reach=500, likes=40, comments=5, shares=2)
    _seed_post(db_path, rid, "New menu launch", "fb1", "facebook", reach=1200, likes=90, comments=12, shares=8)
    _seed_draft(db_path, rid, "Never posted draft")

    result = _perf(rid)
    assert result["published"] == 2
    assert result["total_reach"] == 500 + 1200
    assert result["total_engagement"] == (40 + 5 + 2) + (90 + 12 + 8)
    assert result["top_post"] is None
    assert result["top_post_floor"] == marketing_signals.BEST_POST_MIN_POSTS
    assert result["measured_posts"] == 2


def test_the_top_post_is_the_most_engaged_once_six_are_measured(db_path):
    rid = create_restaurant(Restaurant(name="Busy Spot", owner_email="b@x.com"), db_path=db_path)
    for i in range(5):
        _seed_post(db_path, rid, f"Post {i}", f"ig{i}", "instagram", reach=5000, likes=10)
    _seed_post(db_path, rid, "Wings night", "ig9", "instagram", reach=800, likes=120, comments=30)
    top = _perf(rid)["top_post"]
    assert top["topic"] == "Wings night"
    assert (top["reach"], top["likes"], top["comments"]) == (800, 120, 30)


def test_tenant_isolation_across_restaurants(db_path):
    """One restaurant's post performance must never bleed into another's totals."""
    rid_a = create_restaurant(Restaurant(name="A", owner_email="a@x.com"), db_path=db_path)
    rid_b = create_restaurant(Restaurant(name="B", owner_email="b@x.com"), db_path=db_path)
    _seed_post(db_path, rid_a, "A's post", "a1", "instagram", reach=100, likes=10)
    _seed_post(db_path, rid_b, "B's post", "b1", "instagram", reach=99999, likes=9999)

    result_a = _perf(rid_a)
    assert result_a["total_reach"] == 100
    assert result_a["total_engagement"] == 10


def test_impressions_are_never_counted_as_reach(db_path):
    """Facebook's 300 reach and 450 impressions are 300 reach (MB-6): the
    two used to be added into one "reach" of 750."""
    rid = create_restaurant(Restaurant(name="Impressions Spot", owner_email="i@x.com"), db_path=db_path)
    _seed_post(db_path, rid, "FB post", "fb1", "facebook", reach=300, impressions=450)
    result = _perf(rid)
    assert result["total_reach"] == 300
    assert result["total_impressions"] == 450
