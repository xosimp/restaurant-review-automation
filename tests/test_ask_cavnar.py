"""ask_cavnar.py — the in-dashboard AI copilot. Context assembly must never
invent data for a module the client doesn't have, and must degrade
gracefully when a module is on but no data has been uploaded yet."""
import types

import pytest

import ask_cavnar
import models
from ask_cavnar import build_context, ask
from models import create_restaurant, get_restaurant, get_conn, Restaurant, save_reviews, Review, update_analysis


def _save_analyzed_review(db_path, **kwargs):
    """save_reviews() only inserts the 8 raw-fetch columns (restaurant_id,
    platform, external_id, author, rating, text, review_date, fetched_at) —
    it doesn't even backfill .id onto the objects it returns — sentiment/
    processed are set by a separate update_analysis() call, same as the
    real analyse pipeline. get_review_stats() only counts processed=1
    rows, so a plain save_reviews() alone leaves the review invisible to it."""
    sentiment = kwargs.pop("sentiment", "neutral")
    external_id = kwargs["external_id"]
    save_reviews([Review(**kwargs)], db_path=db_path)
    conn = get_conn(db_path)
    row = conn.execute("SELECT id FROM reviews WHERE external_id=?", (external_id,)).fetchone()
    conn.close()
    update_analysis(row["id"], sentiment, [], "test summary", "normal", db_path=db_path)


@pytest.fixture(autouse=True)
def _redirect_db(monkeypatch, db_path):
    """get_review_stats(), and the models.py functions labor.py/inventory.py
    lazily import per-call (get_client_data, get_restaurant), don't take a
    db_path argument at all — they always resolve models.get_conn(), so
    patching that one name is what redirects this whole chain to the test
    fixture DB instead of the real reviews.db. guest_marketing.py is the
    exception — it does `from models import get_conn` at module top level,
    a bound reference independent of the patch above, so it needs its own
    patch too (same gotcha documented in test_guest_marketing.py)."""
    real_get_conn = models.get_conn
    redirect = lambda *a, **k: real_get_conn(db_path)
    monkeypatch.setattr(models, "get_conn", redirect)
    import guest_marketing
    monkeypatch.setattr(guest_marketing, "get_conn", redirect)
    guest_marketing.init_guest_marketing(db_path=db_path)


def _restaurant(db_path, **modules):
    defaults = dict(module_reviews=0, module_labor=0, module_inventory=0, module_marketing=0)
    defaults.update(modules)
    rid = create_restaurant(Restaurant(name="Copilot Test Co", owner_email="c@x.com", **defaults), db_path=db_path)
    return get_restaurant(rid, db_path=db_path)


def test_no_modules_active_returns_placeholder(db_path):
    r = _restaurant(db_path)
    assert build_context(r) == "No data available yet for this restaurant."


def test_reviews_module_with_no_reviews_says_so(db_path):
    r = _restaurant(db_path, module_reviews=1)
    ctx = build_context(r)
    assert "REVIEWS" in ctx
    assert "No reviews recorded yet" in ctx


def test_reviews_module_with_data_includes_real_numbers(db_path):
    r = _restaurant(db_path, module_reviews=1)
    _save_analyzed_review(db_path, restaurant_id=r.id, platform="google", external_id="r1",
                           author="A", rating=5, text="Great!", sentiment="positive")
    _save_analyzed_review(db_path, restaurant_id=r.id, platform="google", external_id="r2",
                           author="B", rating=1, text="Bad.", sentiment="negative")
    ctx = build_context(r)
    assert "Total reviews analyzed: 2" in ctx
    assert "Negative: 1" in ctx


def test_reviews_context_distinguishes_needs_draft_from_awaiting_approval(db_path):
    """Real bug, found live: a question like "how many reviews need
    approval" only ever saw awaiting_approval (a draft already written,
    pending the owner's approve click) — reviews with no draft at all
    (response_status='pending', needing 'Generate response' first) were
    invisible to the copilot entirely, so it undercounted what the owner
    actually needed to do by however many were still undrafted."""
    r = _restaurant(db_path, module_reviews=1)
    _save_analyzed_review(db_path, restaurant_id=r.id, platform="google", external_id="r1",
                           author="A", rating=5, text="Great!", sentiment="positive")
    _save_analyzed_review(db_path, restaurant_id=r.id, platform="google", external_id="r2",
                           author="B", rating=4, text="Good.", sentiment="positive")
    conn = get_conn(db_path)
    ids = [row["id"] for row in conn.execute("SELECT id FROM reviews WHERE restaurant_id=?", (r.id,)).fetchall()]
    conn.execute("UPDATE reviews SET response_status='drafted' WHERE id=?", (ids[0],))
    conn.execute("UPDATE reviews SET response_status='pending' WHERE id=?", (ids[1],))
    conn.commit()
    conn.close()

    ctx = build_context(r)

    assert "Need a response drafted" in ctx
    assert "Need a response drafted (no AI draft written yet — owner must click 'Generate response'): 1" in ctx
    assert "awaiting the owner's final approval to post: 1" in ctx


def test_labor_module_with_no_shifts_says_so(db_path):
    r = _restaurant(db_path, module_labor=1)
    ctx = build_context(r)
    assert "LABOR" in ctx
    assert "upload a shifts CSV" in ctx


def test_labor_context_includes_target_comparison_and_savings(db_path, monkeypatch):
    import labor
    r = _restaurant(db_path, module_labor=1)
    monkeypatch.setattr(labor, "analyse_shifts_for_restaurant", lambda rid: {
        "is_live": True, "overall_labor_pct": 34.0, "labor_target": 30.0,
        "total_labor_cost": 12000, "total_sales": 35000, "potential_savings": 1400,
        "overstaffed_days": ["Mon"], "understaffed_days": [],
    })
    ctx = build_context(r)
    assert "over this restaurant's 30.0% target" in ctx
    assert "$1,400" in ctx


def test_labor_context_flags_under_target_correctly(db_path, monkeypatch):
    import labor
    r = _restaurant(db_path, module_labor=1)
    monkeypatch.setattr(labor, "analyse_shifts_for_restaurant", lambda rid: {
        "is_live": True, "overall_labor_pct": 25.0, "labor_target": 30.0,
        "total_labor_cost": 8000, "total_sales": 32000, "potential_savings": 0,
        "overstaffed_days": [], "understaffed_days": ["Fri"],
    })
    ctx = build_context(r)
    assert "under this restaurant's 30.0% target" in ctx


def test_inventory_module_with_no_data_says_so(db_path):
    r = _restaurant(db_path, module_inventory=1)
    ctx = build_context(r)
    assert "FOOD COST" in ctx
    assert "upload an inventory CSV" in ctx


def test_inventory_context_names_critical_and_reorder_items(db_path, monkeypatch):
    import inventory
    r = _restaurant(db_path, module_inventory=1)
    monkeypatch.setattr(inventory, "load_inventory_for_restaurant", lambda rid: ([{"item": "Salmon"}], True))
    monkeypatch.setattr(inventory, "analyse_inventory", lambda items: {
        "total_waste_cost_week": 200, "monthly_waste_projection": 800, "total_stock_value": 5000,
        "critical_low": [{"item": "Salmon", "days_remaining": 1}],
        "reorder_soon": [{"item": "Chicken", "days_remaining": 4}],
    })
    ctx = build_context(r)
    assert "Salmon (1d left)" in ctx
    assert "Chicken" in ctx


def test_marketing_module_with_no_posts_says_so(db_path):
    r = _restaurant(db_path, module_marketing=1)
    ctx = build_context(r)
    assert "MARKETING" in ctx
    assert "No posts published" in ctx
    assert "Guest text club: 0 text-eligible" in ctx


def test_marketing_context_includes_guest_text_club_summary(db_path):
    from guest_marketing import init_guest_marketing, add_guest_contact_public_optin
    init_guest_marketing(db_path=db_path)
    r = _restaurant(db_path, module_marketing=1)
    add_guest_contact_public_optin(r.id, "555-123-4567", name="Jane", db_path=db_path)
    ctx = build_context(r)
    assert "1 text-eligible contact" in ctx


def test_inactive_modules_are_omitted_entirely(db_path):
    """A restaurant with only reviews active must not see LABOR/FOOD
    COST/MARKETING sections at all — not even a placeholder for them."""
    r = _restaurant(db_path, module_reviews=1)
    ctx = build_context(r)
    assert "LABOR" not in ctx
    assert "FOOD COST" not in ctx
    assert "MARKETING" not in ctx


def test_full_tier_includes_all_four_sections(db_path):
    r = _restaurant(db_path, module_reviews=1, module_labor=1, module_inventory=1, module_marketing=1)
    ctx = build_context(r)
    assert "REVIEWS" in ctx
    assert "LABOR" in ctx
    assert "FOOD COST" in ctx
    assert "MARKETING" in ctx


def test_intel_context_included_for_full_tier_with_place_id(db_path):
    from models import update_restaurant
    r = _restaurant(db_path, module_reviews=1, module_labor=1, module_inventory=1, module_marketing=1,
                     google_place_id="ChIJtest")
    update_restaurant(r.id, {"competitor_intel": "Recommendations:\n1. Add a happy hour\n2. Post more photos"}, db_path=db_path)
    r = get_restaurant(r.id, db_path=db_path)

    ctx = build_context(r)

    assert "COMPETITOR INTEL" in ctx
    assert "Add a happy hour" in ctx


def test_intel_context_omitted_without_full_tier(db_path):
    """Full tier requires all 4 modules — missing even one (marketing here)
    means no Intel tab, so no Intel section in the copilot's data either."""
    r = _restaurant(db_path, module_reviews=1, module_labor=1, module_inventory=1, module_marketing=0,
                     google_place_id="ChIJtest")
    ctx = build_context(r)
    assert "COMPETITOR INTEL" not in ctx


def test_intel_context_omitted_without_place_id(db_path):
    r = _restaurant(db_path, module_reviews=1, module_labor=1, module_inventory=1, module_marketing=1)
    ctx = build_context(r)
    assert "COMPETITOR INTEL" not in ctx


def test_a_crashing_context_builder_does_not_break_the_others(db_path, monkeypatch):
    """One module's data being malformed must not take down the whole
    snapshot — the owner should still get an answer grounded in whatever
    modules DID build cleanly."""
    import labor
    r = _restaurant(db_path, module_reviews=1, module_labor=1)
    monkeypatch.setattr(labor, "analyse_shifts_for_restaurant", lambda rid: (_ for _ in ()).throw(RuntimeError("boom")))
    ctx = build_context(r)
    assert "REVIEWS" in ctx
    assert "LABOR" not in ctx  # the crashing one is just skipped, not fatal


def test_ask_builds_prompt_with_context_and_question(db_path, monkeypatch):
    r = _restaurant(db_path, module_reviews=1)
    _save_analyzed_review(db_path, restaurant_id=r.id, platform="google", external_id="r1",
                           author="A", rating=5, text="Great!", sentiment="positive")

    captured = {}

    def fake_create_with_retry(client, **kwargs):
        captured.update(kwargs)
        return types.SimpleNamespace(content=[types.SimpleNamespace(text="Your rating is looking great!")])

    monkeypatch.setattr(ask_cavnar, "create_with_retry", fake_create_with_retry)
    answer = ask(r, "How are my reviews doing?")

    assert answer == "Your rating is looking great!"
    prompt = captured["messages"][0]["content"]
    assert "How are my reviews doing?" in prompt
    assert "Total reviews analyzed: 1" in prompt
    assert captured["restaurant_id"] == r.id
    assert captured["action"] == "ask_cavnar"


def test_ask_truncates_overly_long_questions(db_path, monkeypatch):
    r = _restaurant(db_path)
    captured = {}

    def fake_create_with_retry(client, **kwargs):
        captured.update(kwargs)
        return types.SimpleNamespace(content=[types.SimpleNamespace(text="ok")])

    monkeypatch.setattr(ask_cavnar, "create_with_retry", fake_create_with_retry)
    ask(r, "a" * 2000)
    prompt = captured["messages"][0]["content"]
    # 500-char cap on the question itself, embedded inside a longer prompt
    assert prompt.count("a") <= 600
