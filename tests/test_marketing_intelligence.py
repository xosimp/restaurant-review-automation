"""Posts read against what they promoted (marketing_tags, marketing_signals).

What these pin: a post is tagged with a dish the restaurant actually has
and an occasion from a fixed vocabulary; its result is the promoted
dish's own units and the reviews that named it, not only total sales;
publishing starts the month's observed tracker; the engine features it;
and what did not land is shown next to what did.
"""
from datetime import date, datetime, timedelta

import pytest

import models
from models import Restaurant, create_restaurant, get_conn

import marketing_tags, marketing_signals, marketing, marketing_publish, outcomes, home_brief  # noqa: E402
from intelligence import features  # noqa: E402


@pytest.fixture(autouse=True)
def _redirect(monkeypatch, db_path):
    real = models.get_conn
    for mod in (models, marketing_tags, marketing_signals, marketing_publish, outcomes, home_brief, features):
        monkeypatch.setattr(mod, "get_conn", lambda *a, **k: real(db_path), raising=False)
        monkeypatch.setattr(mod, "DB_PATH", db_path, raising=False)
    import auth
    auth.init_auth(db_path=db_path)


def _rid(db_path, **kw):
    kw.setdefault("module_marketing", 1)
    return create_restaurant(Restaurant(name="Simple EJ's", owner_email="e@x.com", **kw), db_path=db_path)


def _menu(db_path, rid, *names):
    conn = get_conn(db_path)
    ids = []
    for n in names:
        cur = conn.execute("INSERT INTO menu_items (restaurant_id, name, is_active) VALUES (?,?,1)", (rid, n))
        ids.append(cur.lastrowid)
    conn.commit(); conn.close()
    return ids


def _post(db_path, rid, topic, posted_at, post_id="p1", reach=0, likes=0):
    conn = get_conn(db_path)
    cur = conn.execute("INSERT INTO marketing_content_log (restaurant_id, content_type, topic, post_id, post_platform, posted_at, "
                       "created_at, reach, likes) VALUES (?,?,?,?,?,?,?,?,?)",
                       (rid, "instagram_post", topic, post_id, "instagram", posted_at, posted_at, reach, likes))
    conn.commit(); conn.close()
    return cur.lastrowid


# ── tagging ──────────────────────────────────────────────────────────────────

def test_a_post_is_tagged_with_a_dish_it_actually_has_and_a_fixed_occasion(db_path):
    rid = _rid(db_path)
    other = create_restaurant(Restaurant(name="Other", owner_email="o@x.com"), db_path=db_path)
    burger_id, wings_id = _menu(db_path, rid, "Smash Burger", "Buffalo Wings")
    (other_item,) = _menu(db_path, other, "Bears Nachos")
    t = marketing_tags.infer(rid, "Buffalo Wings for the Bears game this Sunday", db_path=db_path)
    assert t == {"menu_item_id": wings_id, "menu_item_name": "Buffalo Wings", "occasion": "game_day", "post_kind": "dish"}
    # another restaurant's dish is never matched, even by name
    assert marketing_tags.infer(rid, "Bears Nachos tonight", db_path=db_path)["menu_item_id"] is None
    assert marketing_tags.infer(rid, "Half price happy hour", db_path=db_path)["post_kind"] == "offer"
    assert marketing_tags.infer(rid, "Live music on the patio Friday", db_path=db_path)["occasion"] == "event"
    assert marketing_tags.infer(rid, "New hours", db_path=db_path) == {"menu_item_id": None, "menu_item_name": None,
                                                                          "occasion": None, "post_kind": "general"}
    # the longest whole-name match wins; "burger" inside "Smash Burger" is not a second item
    row = _post(db_path, rid, "Smash Burger night", "2026-09-10 12:00:00")
    tags = marketing_tags.tag_row(row, rid, "Smash Burger night", db_path=db_path)
    assert tags["menu_item_id"] == burger_id
    conn = get_conn(db_path)
    r = conn.execute("SELECT menu_item_id, occasion, post_kind FROM marketing_content_log WHERE id=?", (row,)).fetchone()
    conn.close()
    assert (r["menu_item_id"], r["occasion"], r["post_kind"]) == (burger_id, None, "dish")
    # the owner can correct: an override naming another restaurant's item is refused, clearing is honoured
    t2 = marketing_tags.tag_row(row, rid, "Smash Burger night", overrides={"menu_item_id": other_item}, db_path=db_path)
    assert t2["menu_item_id"] is None
    t3 = marketing_tags.tag_row(row, rid, "Smash Burger night", clear_item=True, db_path=db_path)
    assert t3["menu_item_id"] is None and t3["post_kind"] == "general"
    assert marketing_tags.label({"menu_item_name": "Buffalo Wings", "occasion": "game_day"}) == "Buffalo Wings · game day"


def test_logging_a_published_post_tags_it_and_starts_the_months_tracker(db_path, monkeypatch):
    rid = _rid(db_path, module_labor=1)
    _menu(db_path, rid, "Margherita")
    observed = []
    monkeypatch.setattr(outcomes, "observe", lambda r, action, detail=None, **k: observed.append((r, action, detail)))
    row = marketing.log_content(rid, "instagram_post", "Margherita for the game", post_id="ig1", post_platform="instagram",
                                body="Bears kick off at noon — our Margherita is ready.")
    conn = get_conn(db_path)
    r = conn.execute("SELECT post_kind, occasion, menu_item_id FROM marketing_content_log WHERE id=?", (row,)).fetchone()
    conn.close()
    assert r["post_kind"] == "dish" and r["occasion"] == "game_day" and r["menu_item_id"]
    assert observed == [(rid, "post_published", "Margherita for the game")]
    # a draft (no post_id) is tagged but starts nothing
    marketing.log_content(rid, "instagram_post", "Margherita draft")
    assert len(observed) == 1
    # The call stays, but a post is not measured against sales: that would
    # credit every unrelated sales move to the post (recommendation-trust
    # audit #10), so observe() records nothing for it.
    assert "post_published" not in outcomes.OBSERVED_ACTIONS


def test_marketing_publish_logs_google_and_scheduled_posts_the_same_way(db_path, monkeypatch):
    rid = _rid(db_path)
    _menu(db_path, rid, "Tuna Poke")
    observed = []
    monkeypatch.setattr(outcomes, "observe", lambda r, action, detail=None, **k: observed.append(action))
    marketing_publish._log_published(rid, "google_promo", "Tuna Poke special", "gp1", "google", body="Poke bowls half off", db_path=db_path)
    conn = get_conn(db_path)
    r = conn.execute("SELECT post_kind, occasion, menu_item_id FROM marketing_content_log WHERE post_id='gp1'").fetchone()
    conn.close()
    assert r["menu_item_id"] and r["occasion"] == "offer" and r["post_kind"] == "dish"
    assert observed == ["post_published"]


# ── beyond total sales ───────────────────────────────────────────────────────

def _sales(db_path, rid, days, base=3000):
    conn = get_conn(db_path)
    for i in range(days):
        d = date(2026, 9, 20) - timedelta(days=i)
        conn.execute("INSERT INTO labor_daily_history (restaurant_id, date, day_of_week, labor_pct, labor_cost, sales, total_hours) "
                     "VALUES (?,?,?,?,?,?,?)", (rid, d.isoformat(), "x", 30, 900, base + (600 if d >= date(2026, 9, 15) else 0), 40))
    conn.commit(); conn.close()


def test_attribution_reads_the_dish_units_reviews_and_guest_list_around_the_post(db_path, monkeypatch):
    rid = _rid(db_path, module_labor=1)
    (wings,) = _menu(db_path, rid, "Buffalo Wings")
    _sales(db_path, rid, 42)
    monkeypatch.setattr(marketing_signals, "daily_sales", lambda r: {
        (date(2026, 9, 20) - timedelta(days=i)).isoformat(): 3600.0 if date(2026, 9, 20) - timedelta(days=i) >= date(2026, 9, 15) else 3000.0
        for i in range(42)})
    conn = get_conn(db_path)
    for i in range(42):
        d = date(2026, 9, 20) - timedelta(days=i)
        conn.execute("INSERT INTO menu_item_sales (restaurant_id, menu_item_id, business_date, qty_sold) VALUES (?,?,?,?)",
                     (rid, wings, d.isoformat(), 60 if d >= date(2026, 9, 15) else 40))
    for i, txt in enumerate(("Best buffalo wings in town", "Great service", "the wings were perfect for the game")):
        conn.execute("INSERT INTO reviews (restaurant_id, platform, external_id, author, rating, text, review_date, fetched_at, response_status) "
                     "VALUES (?,?,?,?,?,?,?,?,?)", (rid, "google", f"r{i}", "A", 5, txt, "2026-09-16", "2026-09-16", "pending"))
    conn.execute("INSERT INTO reviews (restaurant_id, platform, external_id, author, rating, text, review_date, fetched_at, response_status) "
                 "VALUES (?,?,?,?,?,?,?,?,?)", (rid, "google", "old", "A", 5, "wings", "2026-08-01", "2026-08-01", "pending"))
    conn.commit(); conn.close()
    import guest_marketing
    guest_marketing.init_guest_marketing(db_path=db_path)
    conn = get_conn(db_path)
    for i, when in enumerate(("2026-09-16T10:00:00", "2026-09-17T10:00:00", "2026-09-10T10:00:00")):
        conn.execute("INSERT INTO guest_contacts (restaurant_id, phone, consent, consent_at) VALUES (?,?,1,?)", (rid, f"+1312555010{i}", when))
    conn.commit(); conn.close()
    row = _post(db_path, rid, "Buffalo Wings for the Bears game", "2026-09-15 11:00:00", reach=1000, likes=50)
    marketing_tags.tag_row(row, rid, "Buffalo Wings for the Bears game", db_path=db_path)
    a = marketing_signals.attribution_for_post(rid, row, db_path=db_path)
    assert a["ok"] and a["lift_pct"] == 20.0
    assert a["menu_item_name"] == "Buffalo Wings" and a["item_lift_pct"] == 50.0 and a["item_window_qty"] == 60.0
    assert a["reviews_mentioning"] == 2                       # "wings" named twice in the fortnight after, not the August one
    assert a["guest_list_delta"] == 1 and a["engagement_rate"] == 0.05 and a["occasion"] == "game_day"
    conn = get_conn(db_path)
    cached = conn.execute("SELECT item_lift_pct, reviews_mentioning, guest_list_delta, engagement_rate FROM marketing_attribution WHERE content_log_id=?",
                         (row,)).fetchone()
    conn.close()
    assert tuple(cached) == (50.0, 2, 1, 0.05)
    # a post with no dish keeps item fields None — never 0 for "not tracked"
    row2 = _post(db_path, rid, "Patio is open", "2026-09-16 11:00:00", post_id="p2")
    marketing_tags.tag_row(row2, rid, "Patio is open", db_path=db_path)
    a2 = marketing_signals.attribution_for_post(rid, row2, db_path=db_path)
    assert a2["ok"] and a2["item_lift_pct"] is None and a2["menu_item_id"] is None


def test_the_summary_shows_what_did_not_land_and_groups_by_kind_occasion_and_dish(db_path, monkeypatch):
    rid = _rid(db_path, module_labor=1)
    _menu(db_path, rid, "Buffalo Wings")
    sales = {(date(2026, 9, 20) - timedelta(days=i)).isoformat(): 3000.0 for i in range(60)}
    for d in ("2026-09-15", "2026-09-16", "2026-09-08", "2026-09-09"):
        sales[d] = 3600.0                               # two lifted posts
    for d in ("2026-09-01", "2026-09-02"):
        sales[d] = 2400.0                               # one that fell
    monkeypatch.setattr(marketing_signals, "daily_sales", lambda r: sales)
    _post(db_path, rid, "Buffalo Wings for the Bears game", "2026-09-15 11:00:00", post_id="a")
    _post(db_path, rid, "Buffalo Wings Packers game", "2026-09-08 11:00:00", post_id="b")
    _post(db_path, rid, "New parking rules", "2026-09-01 11:00:00", post_id="c")
    s = marketing_signals.attribution_summary(rid, db_path=db_path)
    assert s["ok"] and s["measured"] == 3
    assert [p["topic"] for p in s["weakest"]] == ["New parking rules"] and s["weakest"][0]["lift_pct"] < 0
    assert s["by_kind"][0]["group"] == "dish" and s["by_kind"][0]["posts"] == 2 and s["by_kind"][0]["median_lift_pct"] > 0
    assert s["by_occasion"][0]["group"] == "game_day" and s["by_dish"][0]["group"] == "Buffalo Wings"
    # a single post never becomes a group rule
    assert not any(g["group"] == "general" for g in s["by_kind"])
    # the lazy backfill tagged rows written before tags existed
    conn = get_conn(db_path)
    assert conn.execute("SELECT COUNT(*) FROM marketing_content_log WHERE restaurant_id=? AND post_kind IS NULL", (rid,)).fetchone()[0] == 0
    conn.close()


# ── the engine sees posts ────────────────────────────────────────────────────

def test_engine_features_read_post_kinds_lift_and_engagement(db_path):
    rid = _rid(db_path)
    (wings,) = _menu(db_path, rid, "Buffalo Wings")
    today = date.today()
    ids = []
    for i, (topic, reach, likes) in enumerate((("Buffalo Wings game day", 1000, 60), ("Buffalo Wings Friday", 500, 10), ("Half off apps happy hour", 800, 8))):
        ids.append(_post(db_path, rid, topic, (today - timedelta(days=3 + i * 5)).isoformat() + " 12:00:00", post_id=f"p{i}", reach=reach, likes=likes))
        marketing_tags.tag_row(ids[-1], rid, topic, db_path=db_path)
    conn = get_conn(db_path)
    for cid, lift, il in zip(ids, (12.0, 4.0, -3.0), (30.0, 5.0, None)):
        conn.execute("INSERT INTO marketing_attribution (restaurant_id, content_log_id, window_hours, lift_pct, item_lift_pct) VALUES (?,?,48,?,?)",
                     (rid, cid, lift, il))
    conn.commit(); conn.close()
    f = features.compute(rid, today=today, db_path=db_path)
    assert f["posts_28d"] == 3 and f["dish_posts_28d"] == 2 and f["offer_posts_28d"] == 1 and f["occasion_posts_28d"] == 1
    # Two item lifts are under MIN_POSTS_FOR_RATE (re-audit B3 #11): not a median yet.
    assert f["post_lift_median_28d"] == 4.0 and f["item_lift_median_28d"] is None
    assert f["post_engagement_rate_28d"] == round(78 / 2300, 4)
    assert "specials_28d" not in f
    from intelligence import patterns
    keys = {h["key"] for h in patterns.HYPOTHESES}
    assert {"dish_posts_lift", "occasion_posts_engagement", "offer_posts_lift", "post_cadence_lift"} <= keys
    assert "specials_engagement" not in keys
    assert "post_lift_median_28d" in features.BENCHMARK_KEYS


def test_home_says_what_the_latest_post_did(db_path, monkeypatch):
    rid = _rid(db_path)
    (wings,) = _menu(db_path, rid, "Buffalo Wings")
    row = _post(db_path, rid, "Buffalo Wings for the Bears game", datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    conn = get_conn(db_path)
    conn.execute("UPDATE marketing_content_log SET menu_item_id=?, post_kind='dish', occasion='game_day' WHERE id=?", (wings, row))
    conn.execute("INSERT INTO marketing_attribution (restaurant_id, content_log_id, window_hours, lift_pct, item_lift_pct, reviews_mentioning) "
                 "VALUES (?,?,48,18.0,42.0,2)", (rid, row))
    conn.commit()
    r = home_brief._one_dict(conn, "SELECT c.topic, a.lift_pct, a.item_lift_pct, a.reviews_mentioning, m.name AS menu_item_name "
                              "FROM marketing_attribution a JOIN marketing_content_log c ON c.id=a.content_log_id "
                              "LEFT JOIN menu_items m ON m.id=c.menu_item_id WHERE a.restaurant_id=?", (rid,))
    conn.close()
    assert r["menu_item_name"] == "Buffalo Wings" and r["item_lift_pct"] == 42.0
    src = open(home_brief.__file__).read()
    assert 'mkt["post_result"]' in src and "Your post on" in src and '"bad" if pr["lift_pct"] >= 0' not in src


# ── every POS, not only Toast ────────────────────────────────────────────────

def test_guest_matching_asks_the_connected_pos_and_refuses_one_that_cannot(db_path, monkeypatch):
    """Erik's RPOWER reports line items but not guest records yet. Campaign
    visit matching and opt-in invites must skip such a store (no calls, no
    zero), and light up the day its provider grows fetch_order_customers."""
    import pos, types
    rid = _rid(db_path)
    fake = types.SimpleNamespace(is_connected=lambda r: r == rid,
                                 fetch_order_selections=lambda r, d: [],
                                 sync_to_db=lambda r: {}, build_shifts_csv=lambda r, days=60: None)
    monkeypatch.setattr(pos, "PROVIDERS", {"rpower": fake})
    assert pos.supports(rid, "fetch_order_selections") and not pos.supports(rid, "fetch_order_customers")
    with pytest.raises(pos.POSCapabilityError):
        pos.fetch_order_customers(rid, date.today())
    import guest_marketing as gm
    monkeypatch.setattr(gm, "get_conn", lambda *a, **k: models.get_conn(db_path))
    gm.init_guest_marketing(db_path=db_path)
    conn = get_conn(db_path)
    cur = conn.execute("INSERT INTO guest_campaigns (restaurant_id, message, sent_count, created_at) VALUES (?,?,?,?)",
                       (rid, "Patio", 5, (date.today() - timedelta(days=3)).isoformat() + " 12:00:00"))
    conn.execute("INSERT INTO guest_campaign_recipients (campaign_id, restaurant_id, contact_id, phone) VALUES (?,?,?,?)",
                 (cur.lastrowid, rid, 1, "+13125550100"))
    conn.commit(); conn.close()
    assert gm.run_campaign_attribution(db_path=db_path) == {"campaigns_checked": 0, "visits_matched": 0}
    # once the provider can answer, the same store is matched without any Toast field
    fake.fetch_order_customers = lambda r, d: []
    assert pos.supports(rid, "fetch_order_customers")
    assert gm.run_campaign_attribution(db_path=db_path)["campaigns_checked"] == 1
    src = open(gm.__file__).read()
    assert "toast_restaurant_guid" not in src.split("def run_campaign_attribution")[1].split("def ")[1]
    assert 'toast_client_id IS NOT NULL' not in src


def test_item_sales_are_recorded_for_marketing_restaurants_without_recipes():
    """Dish lift reads menu_item_sales; the nightly pass used to write them
    only for restaurants with recipes, which a Back Office user never has."""
    import scheduler, rpower
    src = open(scheduler.__file__).read()
    body = src.split("def run_daily_depletion_sync")[1].split("\ndef ")[0]
    assert "wants_item_sales" in body and 'getattr(r, "module_marketing", 0)' in body
    assert "discover_menu_items(restaurant_id, days=2)" in open(rpower.__file__).read().split("def sync_to_db")[1]
