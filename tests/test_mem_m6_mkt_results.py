"""Memory audit 9/29/26, mkt_results (workstream M6): marketing learns from
sales and returning guests, not only likes. The post and calendar prompts read
the sales-lift verdicts by post kind, occasion and dish (2+ measured posts a
group); the text drafter reads the measured return by audience; the
Opportunity Feed is reordered by rec_learning; the marketing memory provider
serves Ask; and the marketing generators and the competitor read call
memory_context."""
import types

import pytest

import guest_marketing as gm
import marketing
import marketing_opportunities as mo
import marketing_signals as ms
import memory_context
import models
from models import Restaurant, create_restaurant


@pytest.fixture(autouse=True)
def _db(db_path, monkeypatch):
    real = models.get_conn
    monkeypatch.setattr(models, "get_conn", lambda *a, **k: real(db_path))
    monkeypatch.setattr(models, "DB_PATH", db_path)
    gm.init_guest_marketing(db_path)
    yield


def _rid(db_path, **kw):
    return create_restaurant(Restaurant(name="Gia Mia", owner_email="o@x.test", **kw), db_path=db_path)


SUMMARY = {
    "ok": True, "measured": 7,
    "by_kind": [{"group": "dish", "posts": 3, "median_lift_pct": 12.0, "median_item_lift_pct": 30.0,
                 "verdict": "lifted"},
                {"group": "holiday", "posts": 2, "median_lift_pct": 1.0, "median_item_lift_pct": None,
                 "verdict": "no_clear_change"}],
    "by_occasion": [{"group": "game_day", "posts": 2, "median_lift_pct": -3.0, "median_item_lift_pct": None,
                     "verdict": "no_clear_change"}],
    "by_dish": [{"group": "Carbonara", "posts": 2, "median_lift_pct": 9.0, "median_item_lift_pct": 30.0,
                 "verdict": "lifted"}],
}


def test_the_verdicts_by_kind_occasion_and_dish_reach_the_generators_context(db_path, monkeypatch):
    rid = _rid(db_path)
    monkeypatch.setattr(ms, "attribution_summary", lambda *a, **k: SUMMARY)
    monkeypatch.setattr(ms, "weather_signal", lambda r: {})
    ctx = ms.generation_context(rid)
    assert "WHAT MEASURABLY WORKED HERE" in ctx and "before and after, not proof" in ctx
    assert 'post kind "dish": 3 measured posts, median sales +12.0%' in ctx and "the dish's own units +30.0%" in ctx
    assert 'occasion "game day"' in ctx and 'dish "Carbonara"' in ctx and "most of them lifted sales" in ctx


def test_a_group_of_one_post_is_never_a_rule(monkeypatch):
    one = dict(SUMMARY, by_kind=[{"group": "dish", "posts": 1, "median_lift_pct": 40.0, "verdict": "lifted"}],
               by_occasion=[], by_dish=[])
    assert ms.measured_lines(1, summary=one) == []
    assert ms.measured_lines(1, summary={"ok": False}) == []


def test_a_demo_account_does_not_steer_its_posts_by_its_results(db_path, monkeypatch):
    rid = _rid(db_path, is_demo=1)
    monkeypatch.setattr(ms, "attribution_summary", lambda *a, **k: SUMMARY)
    monkeypatch.setattr(ms, "weather_signal", lambda r: {})
    assert "WHAT MEASURABLY WORKED HERE" not in ms.generation_context(rid)


# ── texts: the measured return by audience ───────────────────────────────────

def _camp(segment, sent, back, closed=True):
    return {"segment": segment, "sent_count": sent, "visits_matched": back, "window_closed": closed}


HISTORY = [_camp("lapsed_60", 40, 4), _camp("lapsed_60", 60, 5), _camp("all", 100, 1), _camp("all", 100, 1),
           _camp("regulars", 50, 9), _camp("lapsed_30", 30, 3, closed=False), _camp("lapsed_30", 5, 3)]


def test_the_return_by_audience_needs_two_measured_campaigns_of_ten_texts(db_path, monkeypatch):
    monkeypatch.setattr(gm, "campaign_history", lambda *a, **k: HISTORY)
    got = gm.segment_returns(1)
    assert set(got) == {"lapsed_60", "all"}
    assert got["lapsed_60"]["back_per_100"] == 9.0 and got["all"]["back_per_100"] == 1.0


def _msg(text):
    return types.SimpleNamespace(content=[types.SimpleNamespace(type="text", text=text)], stop_reason="end_turn")


def test_the_text_drafter_is_told_what_past_texts_did_here(db_path, monkeypatch):
    rid = _rid(db_path)
    monkeypatch.setattr(gm, "campaign_history", lambda *a, **k: HISTORY)
    seen = []
    monkeypatch.setattr(gm, "get_client", lambda *a, **k: object())
    monkeypatch.setattr(gm, "create_with_retry",
                        lambda *a, **k: seen.append(k["messages"][0]["content"]) or _msg("See you this week."))
    gm.draft_campaign_message(models.get_restaurant(rid), "win_back")
    p = seen[-1]
    assert "WHAT PAST TEXTS DID HERE" in p and "9 came back per 100 texted, over 2 campaigns" in p
    assert p.index("Haven't been in 60+ days") < p.index("Everyone consented")
    assert "Never put a figure in the text" in p


# ── the Opportunity Feed learns from results ─────────────────────────────────

class _Learned:
    def __init__(self, weights):
        self.w = weights

    def __call__(self, key, kind=None, tags=None):
        return self.w.get(key, 1.0), []


def test_feed_cards_are_reordered_by_this_restaurants_own_results(monkeypatch):
    import rec_learning
    cards = [{"key": "holiday:halloween", "kind": "holiday", "score": 84.0},
             {"key": "slow_day:Tuesday", "kind": "slow_night", "score": 80.0}]
    monkeypatch.setattr(rec_learning, "effectiveness", lambda *a, **k: _Learned({"slow_day:Tuesday": 1.3}))
    out = sorted(mo._learned(1, cards), key=lambda c: -c["score"])
    assert [c["key"] for c in out] == ["slow_day:Tuesday", "holiday:halloween"]
    assert out[0]["learned_weight"] == 1.3 and "learned_weight" not in out[1]


def test_the_feed_and_its_context_lines_both_read_the_learned_order(db_path, monkeypatch):
    import rec_learning
    rid = _rid(db_path, module_marketing=1)
    built = {"cards": [{"key": "holiday:halloween", "kind": "holiday", "title": "Halloween", "why": "w", "score": 84.0,
                        "sources": []},
                       {"key": "slow_day:Tuesday", "kind": "slow_night", "title": "Fill Tuesday", "why": "w",
                        "score": 80.0, "sources": []}],
             "sources": []}
    monkeypatch.setattr(mo, "cached_build", lambda *a, **k: built)
    monkeypatch.setattr(rec_learning, "effectiveness", lambda *a, **k: _Learned({"slow_day:Tuesday": 1.3}))
    assert [c["key"] for c in mo.feed(rid)["items"]][:2] == ["slow_day:Tuesday", "holiday:halloween"]
    assert [c["key"] for c in mo.context_lines(rid)][:2] == ["slow_day:Tuesday", "holiday:halloween"]


# ── the provider, and memory_context in the generators ───────────────────────

def _req(rid, surface="ask", viewer=None):
    return memory_context.MemoryRequest(restaurant_id=rid, surface=surface, viewer=viewer)


def test_the_marketing_provider_serves_ask_and_stays_quiet_where_blocks_are_built(db_path, monkeypatch):
    rid = _rid(db_path)
    monkeypatch.setattr(ms, "attribution_summary", lambda *a, **k: SUMMARY)
    monkeypatch.setattr(gm, "campaign_history", lambda *a, **k: HISTORY)
    lines = marketing.memory_lines(_req(rid))
    texts = [l["text"] for l in lines]
    assert any('post kind "dish"' in t for t in texts) and any("9 came back per 100" in t for t in texts)
    assert all(l.get("source") == "system" for l in lines)
    dish = next(l for l in lines if 'dish "Carbonara"' in l["text"])
    assert dish["trusted"] is False                                    # the dish name is the owner's
    assert marketing.memory_lines(_req(rid, "marketing")) == []
    assert marketing.memory_lines(_req(rid, "reply_drafter")) == []
    assert marketing.memory_lines(_req(rid, viewer={"role": "employee"})) == []
    block = memory_context.memory_context(rid, "ask")
    # (The section's heading is the assembler's own — M2 titles it.)
    assert block.sections.get("marketing") and "9 came back per 100" in block.text
    assert all(l.get("module") in ("marketing", "reviews") for l in lines)


def test_the_post_prompt_carries_memory_context(db_path, monkeypatch):
    rid = _rid(db_path)
    monkeypatch.setattr(memory_context, "memory_context",
                        lambda *a, **k: memory_context.MemoryBlock(text="CONSTRAINTS:\n- (10/1/26) Closed 10/12"))
    seen = []
    monkeypatch.setattr(marketing, "get_client", lambda *a, **k: object())
    monkeypatch.setattr(marketing, "create_with_retry",
                        lambda *a, **k: seen.append(k["messages"][0]["content"]) or _msg("Fall pasta is here."))
    marketing.generate_content("instagram_post", "fall pasta", restaurant_id=rid)
    assert "WHAT CAVNAR AI REMEMBERS" in seen[-1] and "Closed 10/12" in seen[-1]


def test_the_competitor_read_carries_memory_context(db_path, monkeypatch):
    import competitor
    rid = _rid(db_path)
    calls = []
    monkeypatch.setattr(memory_context, "memory_context",
                        lambda rid_, surface, **k: calls.append(surface) or memory_context.MemoryBlock(
                            text="DECISIONS:\n- Not for us: a patio promo"))
    monkeypatch.setattr(competitor, "ANTHROPIC_KEY", "test", raising=False)
    seen = []
    monkeypatch.setattr(competitor, "get_client", lambda *a, **k: object())
    monkeypatch.setattr(competitor, "create_with_retry",
                        lambda *a, **k: seen.append(k["messages"][0]["content"]) or _msg("Hi, here is your snapshot."))
    monkeypatch.setattr(competitor, "finish_competitor_insight", lambda text, *a, **k: text)
    competitor.generate_competitor_insight("Gia Mia", [{"name": "Rival", "rating": 4.5, "review_count": 100,
                                                        "reviews": []}], restaurant_id=rid)
    assert calls == ["competitor_read"] and "Not for us: a patio promo" in seen[-1]


def test_the_marketing_and_competitor_reads_are_kept_as_history():
    """ai_reads.record_read (the contract workstream M4 fills in) is called
    where each read is stored — the marketing read beside its insight_store
    put, the competitor read beside its weekly blob — so the next read and
    Ask can see what was said, not only the latest overwrite."""
    import inspect
    import client_api
    import competitor
    mkt = inspect.getsource(client_api._do_mkt_insight)
    assert 'ai_reads.record_read(rid, "marketing_read", insight, subject="marketing"' in mkt
    assert mkt.index("_ist_m.put(rid, \"marketing\"") < mkt.index("ai_reads.record_read")
    # The storing moved into _store_analysis (AI cost audit 10/7/26 #59).
    comp = inspect.getsource(competitor._store_analysis)
    assert 'ai_reads.record_read(restaurant_id, "competitor_read", str(insight), subject="intel"' in comp
