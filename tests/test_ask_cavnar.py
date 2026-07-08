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
    fixture DB instead of the real reviews.db."""
    real_get_conn = models.get_conn
    monkeypatch.setattr(models, "get_conn", lambda *a, **k: real_get_conn(db_path))


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


def test_labor_module_with_no_shifts_says_so(db_path):
    r = _restaurant(db_path, module_labor=1)
    ctx = build_context(r)
    assert "LABOR" in ctx
    assert "upload a shifts CSV" in ctx


def test_inventory_module_with_no_data_says_so(db_path):
    r = _restaurant(db_path, module_inventory=1)
    ctx = build_context(r)
    assert "FOOD COST" in ctx
    assert "upload an inventory CSV" in ctx


def test_marketing_module_with_no_posts_says_so(db_path):
    r = _restaurant(db_path, module_marketing=1)
    ctx = build_context(r)
    assert "MARKETING" in ctx
    assert "No posts published" in ctx


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
