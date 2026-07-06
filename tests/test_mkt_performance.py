"""client_api.py's /api/mkt-performance aggregation — total reach/engagement
and top-post selection across a restaurant's posted content. Tested against
the same SQL the route runs (client_api.py routes use bare get_conn() with
no db_path override, so this exercises the query directly against the
fixture DB rather than through Flask, consistent with how the rest of this
suite tests DB-layer behavior)."""
from models import create_restaurant, Restaurant, get_conn


def _seed_post(db_path, rid, topic, post_id, platform, reach=0, likes=0, comments=0, shares=0):
    conn = get_conn(db_path)
    conn.execute("""CREATE TABLE IF NOT EXISTS marketing_content_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT, restaurant_id INTEGER NOT NULL,
        content_type TEXT, topic TEXT, post_id TEXT, post_platform TEXT,
        created_at TEXT DEFAULT (datetime('now')),
        reach INTEGER DEFAULT 0, impressions INTEGER DEFAULT 0, engaged INTEGER DEFAULT 0,
        likes INTEGER DEFAULT 0, comments INTEGER DEFAULT 0, shares INTEGER DEFAULT 0)""")
    conn.execute(
        """INSERT INTO marketing_content_log
           (restaurant_id, content_type, topic, post_id, post_platform, reach, likes, comments, shares)
           VALUES (?,?,?,?,?,?,?,?,?)""",
        (rid, "instagram_post", topic, post_id, platform, reach, likes, comments, shares)
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


def _run_performance_query(db_path, rid):
    """Mirrors client_api.py's mkt_performance_api query-for-query, since
    that route has no db_path override to call directly."""
    conn = get_conn(db_path)
    published = conn.execute(
        "SELECT COUNT(*) FROM marketing_content_log WHERE restaurant_id=? AND post_id IS NOT NULL", (rid,)
    ).fetchone()[0] or 0
    totals = conn.execute("""
        SELECT COALESCE(SUM(reach),0) as reach, COALESCE(SUM(impressions),0) as impressions,
               COALESCE(SUM(likes),0) as likes, COALESCE(SUM(comments),0) as comments,
               COALESCE(SUM(shares),0) as shares
        FROM marketing_content_log WHERE restaurant_id=? AND post_id IS NOT NULL
    """, (rid,)).fetchone()
    rows = conn.execute("""
        SELECT topic, post_platform, reach, impressions, likes, comments, shares
        FROM marketing_content_log
        WHERE restaurant_id=? AND post_id IS NOT NULL
          AND (reach > 0 OR impressions > 0 OR likes > 0 OR comments > 0)
    """, (rid,)).fetchall()
    conn.close()
    top_post = None
    if rows:
        best = max(rows, key=lambda r: (r["reach"] or 0) + (r["impressions"] or 0))
        top_post = {"topic": best["topic"], "platform": best["post_platform"]}
    return {
        "published": published,
        "has_data": bool(rows),
        "total_reach": (totals["reach"] or 0) + (totals["impressions"] or 0),
        "total_engagement": (totals["likes"] or 0) + (totals["comments"] or 0) + (totals["shares"] or 0),
        "top_post": top_post,
    }


def test_no_posts_yet(db_path):
    rid = create_restaurant(Restaurant(name="Fresh Spot", owner_email="f@x.com"), db_path=db_path)
    result = _run_performance_query(db_path, rid)
    assert result == {"published": 0, "has_data": False, "total_reach": 0,
                      "total_engagement": 0, "top_post": None}


def test_draft_without_post_id_excluded_from_totals(db_path):
    rid = create_restaurant(Restaurant(name="Drafting Spot", owner_email="d@x.com"), db_path=db_path)
    _seed_draft(db_path, rid, "Idea never published")
    result = _run_performance_query(db_path, rid)
    assert result["published"] == 0
    assert result["has_data"] is False


def test_totals_and_top_post(db_path):
    rid = create_restaurant(Restaurant(name="Active Spot", owner_email="a@x.com"), db_path=db_path)
    _seed_post(db_path, rid, "Weekend brunch", "ig1", "instagram", reach=500, likes=40, comments=5, shares=2)
    _seed_post(db_path, rid, "New menu launch", "fb1", "facebook", reach=1200, likes=90, comments=12, shares=8)
    _seed_draft(db_path, rid, "Never posted draft")

    result = _run_performance_query(db_path, rid)
    assert result["published"] == 2
    assert result["total_reach"] == 500 + 1200
    assert result["total_engagement"] == (40 + 5 + 2) + (90 + 12 + 8)
    assert result["top_post"]["topic"] == "New menu launch"


def test_tenant_isolation_across_restaurants(db_path):
    """One restaurant's post performance must never bleed into another's totals."""
    rid_a = create_restaurant(Restaurant(name="A", owner_email="a@x.com"), db_path=db_path)
    rid_b = create_restaurant(Restaurant(name="B", owner_email="b@x.com"), db_path=db_path)
    _seed_post(db_path, rid_a, "A's post", "a1", "instagram", reach=100, likes=10)
    _seed_post(db_path, rid_b, "B's post", "b1", "instagram", reach=99999, likes=9999)

    result_a = _run_performance_query(db_path, rid_a)
    assert result_a["total_reach"] == 100
    assert result_a["total_engagement"] == 10
    assert result_a["top_post"]["topic"] == "A's post"


def test_impressions_counted_toward_reach_alongside_reach_metric(db_path):
    """Facebook's paired reach/impressions metrics both roll into total_reach."""
    rid = create_restaurant(Restaurant(name="Impressions Spot", owner_email="i@x.com"), db_path=db_path)
    conn = get_conn(db_path)
    conn.execute("""CREATE TABLE IF NOT EXISTS marketing_content_log (
        id INTEGER PRIMARY KEY AUTOINCREMENT, restaurant_id INTEGER NOT NULL,
        content_type TEXT, topic TEXT, post_id TEXT, post_platform TEXT,
        created_at TEXT DEFAULT (datetime('now')),
        reach INTEGER DEFAULT 0, impressions INTEGER DEFAULT 0, engaged INTEGER DEFAULT 0,
        likes INTEGER DEFAULT 0, comments INTEGER DEFAULT 0, shares INTEGER DEFAULT 0)""")
    conn.execute(
        "INSERT INTO marketing_content_log (restaurant_id, content_type, topic, post_id, post_platform, reach, impressions) VALUES (?,?,?,?,?,?,?)",
        (rid, "facebook_post", "FB post", "fb1", "facebook", 300, 450))
    conn.commit()
    conn.close()
    result = _run_performance_query(db_path, rid)
    assert result["total_reach"] == 300 + 450
