"""Density / 3-30-300 round, web modules (fix agent B, 9/25/26).

The server halves: the Reviews inbox sorts answered reviews last (#33), the
line above the inbox (#32) and the Marketing header (#10) are built from
counts and stored rows with their floors held. And the web rules that must
hold across the page source: one opportunity figure on Food Cost (#25),
Generate the only Marketing primary (#24), chips on constants neutral (#31),
a Today line under every module h1 (#48), How you compare slim rows (#34).
"""
import json
import os
import re

import pytest

import marketing_signals
import models
import review_intelligence as ri

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _src():
    return open(os.path.join(ROOT, "templates", "dashboard.html"), encoding="utf-8").read()


def _panel(s, pid, end_marker):
    i = s.index('id="%s"' % pid)
    return s[i:s.index(end_marker, i)]


@pytest.fixture(autouse=True)
def _redirect(db_path, monkeypatch):
    real = models.get_conn
    for mod in (models, ri):
        monkeypatch.setattr(mod, "get_conn", lambda *a, **k: real(db_path), raising=False)
    monkeypatch.setattr(models, "DB_PATH", db_path)
    monkeypatch.setattr(ri, "DB_PATH", db_path)
    yield


def _restaurant(db_path):
    return models.create_restaurant(models.Restaurant(name="Density", owner_email="d@x.test"), db_path=db_path)


def _review(db_path, rid, ext, *, rating=4, days_ago=3, status="pending", urgency="normal", sentiment="positive"):
    conn = models.get_conn(db_path)
    conn.execute(
        """INSERT INTO reviews (restaurant_id, platform, external_id, author, rating, text, review_date,
           fetched_at, sentiment, categories, urgency, processed, response_status)
           VALUES (?, 'google', ?, ?, ?, 'text', date('now', ?), datetime('now'), ?, '[]', ?, 1, ?)""",
        (rid, ext, "G" + ext, rating, "-%d days" % days_ago, sentiment, urgency, status))
    conn.commit()
    conn.close()


# ── #33: the inbox puts everything still waiting first ─────────────────────

def test_answered_reviews_sort_below_every_review_still_waiting(db_path):
    rid = _restaurant(db_path)
    # An old urgent negative with a live reply used to outrank today's
    # unanswered four-star review.
    _review(db_path, rid, "old-urgent", rating=1, days_ago=20, status="posted", urgency="high", sentiment="negative")
    _review(db_path, rid, "skipped", rating=2, days_ago=10, status="skipped", sentiment="negative")
    _review(db_path, rid, "approved", rating=3, days_ago=5, status="approved", sentiment="neutral")
    _review(db_path, rid, "today", rating=4, days_ago=0, status="pending")
    _review(db_path, rid, "drafted", rating=5, days_ago=2, status="drafted")
    rows, _ = models.get_reviews_data(rid, "all", include_total=True)
    order = [r["external_id"] for r in rows]
    waiting = [o for o in order if o in ("today", "drafted")]
    assert order[:2] == waiting, order
    assert set(order[2:]) == {"old-urgent", "skipped", "approved"}


def test_the_web_draws_one_answered_divider_above_the_trailing_answered_run():
    s = _src()
    fn = s[s.index("function rvAnsweredDivider(){"):s.index("function rvLoadWhy(){")]
    assert "st==='posted'||st==='approved'||st==='skipped'" in fn
    assert "'Answered ('+n+')'" in fn
    assert "rvAnsweredDivider();" in s[s.index("function filterReviews(){"):]


# ── #32: the line above the inbox ─────────────────────────────────────────

def test_why_line_states_a_direction_only_past_the_floor(db_path):
    rid = _restaurant(db_path)
    for i in range(2):
        _review(db_path, rid, "r%d" % i, rating=5, days_ago=3)
    for i in range(3):
        _review(db_path, rid, "p%d" % i, rating=3, days_ago=40)
    out = ri.inbox_why(rid, db_path=db_path)
    assert out["rating_delta"] is None, "two recent reviews are not a direction"
    assert out["recent_n"] == 2 and out["prior_n"] == 3
    _review(db_path, rid, "r2", rating=5, days_ago=1)
    out = ri.inbox_why(rid, db_path=db_path)
    assert out["rating_delta"] == 2.0
    assert out["complaint"] is None, "no stored diagnosis, no complaint"


def test_why_line_names_the_top_stored_diagnosis(db_path, monkeypatch):
    rid = _restaurant(db_path)
    monkeypatch.setattr(ri, "get_diagnoses", lambda *a, **k: [
        {"category": "food_quality", "mention_count": 6, "window_days": 90, "stale": False, "as_of": "9/24/26"}])
    out = ri.inbox_why(rid, db_path=db_path)
    assert out["complaint"] == {"category": "food_quality", "label": "food quality", "mentions": 6,
                                "window_days": 90, "stale": False, "as_of": "9/24/26"}


def test_why_line_route_is_reviews_gated_and_calls_no_model():
    import auth
    assert any(p == "/api/reviews" and m == "reviews" for p, m in auth._MODULE_PREFIXES)
    src = open(os.path.join(ROOT, "review_intelligence.py"), encoding="utf-8").read()
    body = src[src.index("def inbox_why("):]
    for call in ("create_with_retry", "get_client(", "messages.create"):
        assert call not in body


# ── #10: the Marketing header ─────────────────────────────────────────────

def _post(db_path, rid, days_ago, reach=100):
    conn = models.get_conn(db_path)
    conn.execute("INSERT INTO marketing_content_log (restaurant_id, content_type, topic, post_id, post_platform, reach, posted_at, created_at) "
                 "VALUES (?, 'instagram_post', 't', ?, 'instagram', ?, datetime('now', ?), datetime('now', ?))",
                 (rid, "p%d-%d" % (days_ago, reach), reach, "-%d days" % days_ago, "-%d days" % days_ago))
    conn.commit()
    conn.close()


def test_marketing_header_says_nothing_posted_and_never_invents_a_change(db_path):
    rid = _restaurant(db_path)
    h = marketing_signals.header_summary(rid, db_path=db_path)
    assert h["status"] == "Nothing posted yet" and h["tone"] == "neutral"
    assert h["reach_change"] is None and h["next_scheduled"] is None and h["posts"] == 0


def test_marketing_header_goes_quiet_and_names_the_next_post(db_path):
    rid = _restaurant(db_path)
    _post(db_path, rid, 12)
    conn = models.get_conn(db_path)
    conn.execute("INSERT INTO marketing_scheduled_posts (restaurant_id, platform, body, scheduled_for, status) "
                 "VALUES (?, 'instagram', 'b', '2099-01-02T18:45', 'scheduled')", (rid,))
    conn.commit()
    conn.close()
    h = marketing_signals.header_summary(rid, db_path=db_path)
    assert h["status"] == "Nothing posted in 12 days" and h["tone"] == "warn"
    assert h["next_scheduled"] == {"date": "1/2/99", "time": "6:45pm", "platform": "instagram"}


def test_marketing_header_reach_change_keeps_the_post_floor(db_path):
    rid = _restaurant(db_path)
    for d in (1, 2, 3):
        _post(db_path, rid, d, reach=300)
    for d in (35, 36):
        _post(db_path, rid, d, reach=100)
    h = marketing_signals.header_summary(rid, db_path=db_path)
    assert h["reach_change"] is None, "two posts in the prior window is under the floor"
    _post(db_path, rid, 37, reach=100)
    h = marketing_signals.header_summary(rid, db_path=db_path)
    assert h["reach_change"] == 200.0 and h["tone"] == "good" and h["status"].startswith("Reach up 200%")


# ── web rules that must hold across the page ──────────────────────────────

def test_food_cost_carries_one_opportunity_figure():
    s = _src()
    panel = _panel(s, "panel-inventory", "<!-- /panel-inventory -->")
    for gone in ('id="inv-annual-recoverable"', 'id="fc2-gauge"', "Recoverable / year", "projected / mo",
                 ">waste items<", "'Annual opportunity'"):
        assert gone not in panel, gone
    assert "tile('Annual opportunity'" not in s and "function renderFcGauge(" not in s
    cfo = s[s.index("function fc2LoadCfo(){"):s.index("// One line for a forecast's own record")]
    assert "monthly_at_stake_basis" in cfo and "an opportunity, not money saved" in cfo


def test_marketing_has_one_primary_generate():
    s = _src()
    panel = _panel(s, "panel-marketing", "<!-- COMPETITOR INTEL -->")
    content = panel[panel.index('<div id="mkt-tab-content">'):panel.index("<!-- /mkt-tab-content -->")]
    prim = re.findall(r'<(?:button|label|a)[^>]*class="cbtn cbtn-primary(?! cbtn-(?:instagram|facebook))[^"]*"[^>]*>([^<]*)', content)
    assert [p.strip() for p in prim] == ["Generate"], prim
    cal = s[s.index("function renderCal(ideas){"):]
    cal = cal[:cal.index("\n}\n")]
    assert "cbtn-primary" not in cal and "Write this →" in cal


def test_marketing_header_is_the_shared_one_and_the_calendar_stacks_on_a_phone():
    s = _src()
    panel = _panel(s, "panel-marketing", "<!-- COMPETITOR INTEL -->")
    assert '<h1 class="hb-h1">Marketing. <span class="hb-head neutral" id="mkt-head">' in panel
    for chip in ("mkt-chip-posts", "mkt-chip-reach", "mkt-chip-next"):
        assert 'id="%s"' % chip in panel
    assert "font-size:36px" not in panel, "no centred 36px tab titles"
    assert "@media (max-width:760px){.cal-grid{grid-template-columns:1fr}" in s
    # the diagnosis sits under the brief, How you compare after it
    assert panel.index('id="mkt-insight"') < panel.index('id="guest-diag"') < panel.index('data-bm-module="marketing"') < panel.index('id="mkt-guests"')


def test_chips_on_constants_are_neutral():
    s = _src()
    assert '<span class="hb-chip neutral"><span class="dot"><b></b><i></i></span><span class="v"><span class="hb-num stat-n">{{ _lt|int }}%</span>' in s
    assert "{{ 'sample' if not inv.is_live else 'neutral' }}\"><span class=\"dot\"><b></b><i></i></span><span class=\"v\"><span class=\"hb-num\">${{ inv.total_stock_value" in s
    assert "{{ 'neutral' if comp_count else 'sample' }}" in s
    assert ".hb-chip.neutral .dot{display:none}" in s


def test_every_module_has_a_today_line():
    s = _src()
    for tid in ("lb2-today", "fc2-today", "rv-today", "mkt-today", "in2-today", "fc2-stock-status"):
        assert 'class="mod-today" id="%s"' % tid in s, tid


def test_how_you_compare_rows_are_slim_with_one_why_drawer():
    s = _src()
    js = s[s.index('<script id="cav-bench">'):]
    js = js[:js.index("</script>")]
    row = js[js.index("function rowHtml(r,home){"):js.index("function profileAction(a){")]
    assert "rowDetail(" not in row, "a row reads label, value and standing only"
    card = js[js.index("function cardHtml(d){"):js.index("function load(module){")]
    assert '<details class="bm-why"><summary>Why? · who, how strong, as of when</summary>' in card
    assert "r.strength_pct!==cardPct" in js


def test_labor_says_its_percent_once_and_the_money_sits_under_waiting_on_you():
    s = _src()
    panel = _panel(s, "panel-labor", "<!-- /panel-labor -->")
    wait = panel.index('id="lb2-wait"')
    assert wait < panel.index('id="lb2-went"') < panel.index('aria-label="Labor % by day"')
    assert 'id="gap-current-pct"' not in panel and 'id="lb2-hero-n"' not in panel
    assert panel.index('id="lb2-money-all"') > panel.index('id="sched-history-list"')
    # one place answers a request
    to = s[s.index("function lb2LoadTimeOff(){"):s.index("function lb2LoadCovers(){")]
    assert "data-timeoff=" not in to
    rs = s[s.index("window.renderShiftRequests=function(){"):s.index("window.decideShiftRequest=function")]
    assert "decideShiftRequest(" not in rs
