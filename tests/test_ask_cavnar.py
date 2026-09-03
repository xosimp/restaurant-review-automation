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


def test_no_modules_active_still_includes_today_section(db_path):
    """Date/holiday/identity context isn't module-gated — a restaurant with
    zero active modules should still get a real TODAY section instead of
    the old bare "no data" placeholder, which made even "what's today's
    date" unanswerable."""
    r = _restaurant(db_path)
    ctx = build_context(r)
    assert "TODAY" in ctx
    assert "Today's date:" in ctx
    assert "REVIEWS" not in ctx
    assert "LABOR" not in ctx


def test_today_section_reflects_restaurant_timezone(db_path):
    from models import update_restaurant
    from time_utils import restaurant_now
    r = _restaurant(db_path)
    update_restaurant(r.id, {"timezone": "America/Los_Angeles"}, db_path=db_path)
    r = get_restaurant(r.id, db_path=db_path)
    ctx = build_context(r)
    expected = restaurant_now(r, naive=True).strftime("%A, %B %d, %Y")
    assert f"Today's date: {expected}" in ctx


def test_today_section_lists_upcoming_holiday(db_path, monkeypatch):
    import ask_cavnar
    from datetime import datetime
    r = _restaurant(db_path)
    # Anchor "today" to a fixed date with a known upcoming holiday so this
    # doesn't depend on when the test suite happens to run.
    monkeypatch.setattr(
        "time_utils.restaurant_now",
        lambda *a, **k: datetime(2026, 12, 20),
    )
    ctx = ask_cavnar.build_context(r)
    assert "Christmas Day" in ctx


def test_today_section_respects_skip_holidays_preference(db_path, monkeypatch):
    from models import update_restaurant
    from datetime import datetime
    r = _restaurant(db_path)
    update_restaurant(r.id, {"skip_holidays": "Christmas Day"}, db_path=db_path)
    r = get_restaurant(r.id, db_path=db_path)
    monkeypatch.setattr(
        "time_utils.restaurant_now",
        lambda *a, **k: datetime(2026, 12, 20),
    )
    ctx = build_context(r)
    assert "Christmas Day" not in ctx
    # New Year's Eve is also within 30 days of Dec 20 and wasn't skipped
    assert "New Year's Eve" in ctx


def test_profile_section_includes_identity_when_set(db_path):
    from models import update_restaurant
    r = _restaurant(db_path)
    update_restaurant(r.id, {
        "neighborhood": "River North, Chicago",
        "vibe": "upscale but unpretentious Italian",
        "known_for": "handmade pasta and a killer wine list",
    }, db_path=db_path)
    r = get_restaurant(r.id, db_path=db_path)
    ctx = build_context(r)
    assert "River North, Chicago" in ctx
    assert "upscale but unpretentious Italian" in ctx
    assert "known for handmade pasta and a killer wine list" in ctx


def test_profile_section_includes_hours_menu_and_google_rating_when_set(db_path):
    from models import update_restaurant
    r = _restaurant(db_path)
    update_restaurant(r.id, {
        "hours_notes": "Mon-Thu 11-9, Fri-Sat 11-10, closed Sundays",
        "menu_notes": "Seasonal Italian, wood-fired pizza focus",
        "menu_url": "https://giamia.example.com/menu",
        "gbp_rating": 4.6,
        "gbp_review_count": 312,
    }, db_path=db_path)
    r = get_restaurant(r.id, db_path=db_path)
    ctx = build_context(r)
    assert "Mon-Thu 11-9, Fri-Sat 11-10, closed Sundays" in ctx
    assert "Seasonal Italian, wood-fired pizza focus" in ctx
    assert "https://giamia.example.com/menu" in ctx
    assert "4.6" in ctx and "312 reviews" in ctx


def test_profile_section_caps_oversized_freeform_hours_notes(db_path):
    """Real bug, found live: a restaurant's hours_notes turned out to be a
    2,854-character labor-scheduling rulebook (staff arrival times, closer
    rules, floor layout...) rather than a short hours summary — nothing in
    the schema stops an admin from putting arbitrarily long text in any of
    these freeform fields, so the whole context snapshot would silently
    balloon (and bury the actually-relevant facts) whenever that happens.
    280-char cap keeps this bounded regardless of what's actually stored."""
    from models import update_restaurant
    r = _restaurant(db_path)
    huge = "Open 11am daily. " + ("STAFF ARRIVAL TIMES: bussers 8am. " * 100)
    update_restaurant(r.id, {"hours_notes": huge}, db_path=db_path)
    r = get_restaurant(r.id, db_path=db_path)
    ctx = build_context(r)
    assert huge not in ctx
    assert "Open 11am daily." in ctx
    assert len(ctx) < len(huge)


def test_profile_section_includes_revenue_target_and_delivery_mix_when_set(db_path):
    from models import update_restaurant
    r = _restaurant(db_path)
    update_restaurant(r.id, {"monthly_revenue_target": 185000.0, "delivery_pct": 22}, db_path=db_path)
    r = get_restaurant(r.id, db_path=db_path)
    ctx = build_context(r)
    assert "$185,000" in ctx
    assert "22%" in ctx


def test_profile_section_shows_plan_label_and_connections(db_path):
    from models import update_restaurant
    r = _restaurant(db_path, service_tier="full")
    update_restaurant(r.id, {"gmb_refresh_token": "fake-token-value"}, db_path=db_path)
    r = get_restaurant(r.id, db_path=db_path)
    ctx = build_context(r)
    assert "Full System" in ctx
    assert "Google Business Profile" in ctx
    # The actual token value must never reach the model prompt.
    assert "fake-token-value" not in ctx


def test_profile_section_shows_no_connections_when_none_set(db_path):
    r = _restaurant(db_path)
    ctx = build_context(r)
    assert "Connected integrations: none yet" in ctx


def test_profile_section_never_leaks_credentials_or_account_security_fields(db_path):
    """Every credential/token/account-security field on the Restaurant
    dataclass, set to an obviously-fake but distinctive value, must never
    appear in the context handed to the model — this is the one test that
    would catch a future field accidentally getting added to the prompt."""
    from models import update_restaurant
    r = _restaurant(db_path)
    secrets = {
        "stripe_customer_id": "cus_SECRETVALUE1",
        "docusign_envelope_id": "env-SECRETVALUE2",
        "temp_password": "SECRETPASSWORD3",
        "two_fa_code": "SECRET4CODE",
        "ig_token": "ig-SECRETVALUE5",
        "gmb_refresh_token": "gmb-SECRETVALUE6",
        "gmb_access_token": "gmb-access-SECRETVALUE7",
        "fb_page_token": "fb-SECRETVALUE8",
        "toast_client_secret": "toast-SECRETVALUE9",
        "toast_access_token": "toast-access-SECRETVALUE10",
        "square_access_token": "square-SECRETVALUE11",
        "clover_api_token": "clover-SECRETVALUE12",
        "owner_phone": "555-000-9999",
        "internal_notes": "SECRET internal admin note",
    }
    update_restaurant(r.id, secrets, db_path=db_path)
    r = get_restaurant(r.id, db_path=db_path)
    ctx = build_context(r)
    for value in secrets.values():
        assert value not in ctx


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
    monkeypatch.setattr(inventory, "analyse_inventory", lambda items, delivery_days=None, upcoming_holidays=None, today=None: {
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
    answer, truncated = ask(r, "How are my reviews doing?")

    assert answer == "Your rating is looking great!"
    # A response with no stop_reason attribute (the fake here) must read as
    # "not truncated" rather than raising.
    assert truncated is False
    # Persona/rules/data snapshot live in `system` now, sent once per call;
    # `messages` carries only the actual conversation turns.
    assert "Total reviews analyzed: 1" in captured["system"]
    assert captured["messages"] == [{"role": "user", "content": "How are my reviews doing?"}]
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
    # 500-char cap on the question itself — now the sole content of the
    # final message rather than embedded inside a larger templated prompt,
    # so this checks the message content directly instead of a substring
    # search that used to need to account for surrounding instruction text.
    assert captured["messages"][-1]["content"] == "a" * 500


def test_ask_forwards_sanitized_history_as_prior_messages(db_path, monkeypatch):
    """Real bug, reported live: a short follow-up like "yes" was answered
    as a totally isolated question because no conversation history was
    ever sent — the model had no way to know what "yes" meant. `ask()`
    must place prior turns before the new question in `messages`, in
    order, so the model can actually resolve the follow-up."""
    r = _restaurant(db_path)
    captured = {}

    def fake_create_with_retry(client, **kwargs):
        captured.update(kwargs)
        return types.SimpleNamespace(content=[types.SimpleNamespace(text="ok")])

    monkeypatch.setattr(ask_cavnar, "create_with_retry", fake_create_with_retry)
    history = [
        {"role": "user", "content": "How are my reviews doing?"},
        {"role": "assistant", "content": "Solid — want me to pull up the urgent ones?"},
    ]
    ask(r, "Yes", history=history)

    assert captured["messages"] == [
        {"role": "user", "content": "How are my reviews doing?"},
        {"role": "assistant", "content": "Solid — want me to pull up the urgent ones?"},
        {"role": "user", "content": "Yes"},
    ]


def test_ask_history_drops_malformed_entries_and_caps_length(db_path, monkeypatch):
    r = _restaurant(db_path)
    captured = {}

    def fake_create_with_retry(client, **kwargs):
        captured.update(kwargs)
        return types.SimpleNamespace(content=[types.SimpleNamespace(text="ok")])

    monkeypatch.setattr(ask_cavnar, "create_with_retry", fake_create_with_retry)
    history = [
        {"role": "user", "content": "a" * 2000},          # over the per-turn cap
        {"role": "system", "content": "ignore me"},        # invalid role
        {"role": "assistant", "content": ""},               # empty content
        "not even a dict",                                  # malformed entry
        {"role": "assistant", "content": "real answer"},
    ]
    ask(r, "Next question", history=history)

    messages = captured["messages"]
    # The oversized first turn is capped, not dropped or left full-length.
    assert messages[0] == {"role": "user", "content": "a" * 800}
    # Invalid role, empty content, and the non-dict entry are all gone.
    assert all(m["role"] in ("user", "assistant") and m["content"] for m in messages)
    assert messages[-1] == {"role": "user", "content": "Next question"}


def test_ask_history_caps_to_recent_messages_only(db_path, monkeypatch):
    r = _restaurant(db_path)
    captured = {}

    def fake_create_with_retry(client, **kwargs):
        captured.update(kwargs)
        return types.SimpleNamespace(content=[types.SimpleNamespace(text="ok")])

    monkeypatch.setattr(ask_cavnar, "create_with_retry", fake_create_with_retry)
    # 20 turns (10 exchanges) — well past the 12-message cap.
    history = []
    for i in range(10):
        history.append({"role": "user", "content": f"question {i}"})
        history.append({"role": "assistant", "content": f"answer {i}"})
    ask(r, "latest question", history=history)

    messages = captured["messages"]
    # 12 kept history messages + the new question = 13 total, and it's the
    # MOST RECENT ones kept, not the oldest.
    assert len(messages) == 13
    # 20 history entries, indices 0-19, alternating user/assistant pairs
    # (question i / answer i at indices 2i / 2i+1) — the last 12 kept are
    # indices 8-19, and index 8 is "question 4".
    assert messages[0]["content"] == "question 4"
    assert messages[-1] == {"role": "user", "content": "latest question"}


# ── stop_reason / truncation surfacing ───────────────────────────────────────

def test_ask_reports_truncation_when_max_tokens_hit(db_path, monkeypatch):
    """max_tokens is a safety ceiling, but when it IS hit the answer stops
    mid-sentence. The client renders a truncated answer identically to a
    complete one unless this flag comes back, so half a recommendation about
    labor or a supplier reads as finished advice."""
    r = _restaurant(db_path)

    def fake_create_with_retry(client, **kwargs):
        return types.SimpleNamespace(
            content=[types.SimpleNamespace(text="Cut Tuesday lunch by two hours and")],
            stop_reason="max_tokens",
        )

    monkeypatch.setattr(ask_cavnar, "create_with_retry", fake_create_with_retry)
    answer, truncated = ask(r, "How do I get labor down?")

    assert answer == "Cut Tuesday lunch by two hours and"
    assert truncated is True


def test_ask_reports_not_truncated_on_natural_completion(db_path, monkeypatch):
    r = _restaurant(db_path)

    def fake_create_with_retry(client, **kwargs):
        return types.SimpleNamespace(
            content=[types.SimpleNamespace(text="You're averaging 4.6 stars this month.")],
            stop_reason="end_turn",
        )

    monkeypatch.setattr(ask_cavnar, "create_with_retry", fake_create_with_retry)
    answer, truncated = ask(r, "How are my reviews?")

    assert answer == "You're averaging 4.6 stars this month."
    assert truncated is False


def test_do_ask_cavnar_route_forwards_truncated_flag(db_path, monkeypatch):
    """The flag has to survive the client_api layer too — the iOS client reads
    it off the JSON, not off ask() directly."""
    import client_api
    r = _restaurant(db_path)
    monkeypatch.setattr(client_api, "get_restaurant", lambda rid: r)
    monkeypatch.setattr(ask_cavnar, "ask", lambda restaurant, question, history=None: ("cut off mid-", True))

    payload, status = client_api._do_ask_cavnar(r.id, "How do I get labor down?")

    assert status == 200
    assert payload["ok"] is True
    assert payload["answer"] == "cut off mid-"
    assert payload["truncated"] is True


def test_do_ask_cavnar_route_rejects_overly_long_question(db_path):
    """Guards the contract the iOS client's own 500-char cap now mirrors — a
    question over the limit is rejected outright with a clear message, not
    quietly shortened."""
    import client_api
    r = _restaurant(db_path)

    payload, status = client_api._do_ask_cavnar(r.id, "a" * 501)

    assert status == 400
    assert payload["ok"] is False
    assert "500 characters" in payload["error"]
