"""Marketing and Food Cost round, 9/28/26: Marketing's header, its brief in
the modules' AI read box, Preview where the owner is, the calendar (every
day, larger cards, one actions row), the guest contacts modal, the AI
monitor's motion, and the Food Cost hero closer to its trend."""
import json

import pytest

import marketing
import models
from models import Restaurant, create_restaurant

SRC = open("templates/dashboard.html", encoding="utf-8").read()
DAYS = ("Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday")


def _between(a, b):
    i = SRC.index(a)
    return SRC[i:SRC.index(b, i)]


# ── Marketing, top of the page ──────────────────────────────────────────────

def test_no_today_line_and_the_brief_is_the_ai_read_box():
    assert 'id="mkt-today"' not in SRC and "mkt-today-t" not in SRC
    assert '<section class="lb2-ai mkt-brief" aria-label="Cavnar AI marketing brief">' in SRC
    assert '<div class="tag">Cavnar AI&rsquo;s marketing brief</div>' in SRC
    assert "mkt-brief-ft" not in SRC and ".mkt-brief .insight-text{" not in SRC
    # The panel has no --hb-tint of its own; the box gets Food Cost's.
    assert "#panel-marketing .lb2-ai,#panel-marketing .cal-card{--hb-tint:rgba(200,75,47,.06);" in SRC


def test_preview_opens_where_the_owner_is():
    js = _between("function closeMktPreview(){", "// ── Drafts")
    assert "['mkt-preview-modal', 'brand-voice-modal', 'posting-overlay'].forEach" in js
    assert "document.body.appendChild(m)" in js


def test_the_edit_hint_is_readable():
    hint = _between('<span id="output-hint"', "</span>")
    assert "font-size:14px" in hint and "font-size:10px" not in hint


# ── the content calendar ────────────────────────────────────────────────────

def test_the_calendar_buttons_match_the_row_above():
    head = _between('<section class="mkt-sec" aria-label="Content calendar">', '<div class="cal-grid"')
    assert '<button class="cbtn cbtn-secondary" onclick="loadCal()">Generate week' in head
    assert '<button class="cbtn cbtn-secondary" id="cal-download-btn"' in head and ">Download<svg" in head
    assert "Download CSV<svg" not in head and "cbtn-sm" not in head


def test_every_day_has_a_large_card_with_one_actions_row():
    assert ".cal-grid{display:grid;grid-template-columns:repeat(4,minmax(0,1fr));gap:16px" in SRC
    cal = _between("function renderCal(ideas){", "\n}\n")
    assert "var DAYS = ['Sunday','Monday','Tuesday','Wednesday','Thursday','Friday','Saturday']" in cal
    assert "cal-card cal-empty" in cal and "Nothing planned for '+dayName+'." in cal
    assert '<div class="cal-acts"><button type="button" data-idx="' in cal
    assert ".cal-acts>.cbtn+.rec-ans>.cbtn:first-child" in SRC
    assert "min-height:250px" in SRC and ".cal-angle{flex:1;font-size:16px" in SRC


@pytest.fixture
def rid(db_path, monkeypatch):
    real = models.get_conn
    monkeypatch.setattr(models, "get_conn", lambda *a, **k: real(db_path))
    return create_restaurant(Restaurant(name="Cal Co", owner_email="c@x.test"), db_path=db_path)


def _model(monkeypatch, replies, calls):
    def _create(*a, **kw):
        calls.append(kw["messages"][0]["content"])
        return replies[min(len(calls) - 1, len(replies) - 1)]
    monkeypatch.setattr(marketing, "create_with_retry", _create)
    monkeypatch.setattr(marketing, "extract_text", lambda m: json.dumps(m))


def _idea(day, angle=None):
    return {"day": day, "platform": "Google", "angle": angle or f"{day} idea", "type": "google_promo"}


def test_a_day_the_model_left_out_is_asked_for_once(rid, monkeypatch):
    calls = []
    six = [_idea(d) for d in DAYS if d != "Monday"]
    _model(monkeypatch, [six, [_idea("Monday, Sept 28", "Monday lunch"), _idea("Tuesday", "a second Tuesday")]], calls)
    week = marketing.get_content_calendar_ideas(restaurant_id=rid, force=True)
    assert [i["day"] for i in week] == list(DAYS)
    assert next(i for i in week if i["day"] == "Monday")["angle"] == "Monday lunch"
    assert next(i for i in week if i["day"] == "Tuesday")["angle"] == "Tuesday idea"     # the first plan keeps its day
    assert len(calls) == 2 and "Only these days are still open: Monday." in calls[1]


def test_a_full_week_costs_one_call_and_a_still_empty_day_stays_empty(rid, monkeypatch):
    calls = []
    _model(monkeypatch, [[_idea(d) for d in DAYS]], calls)
    assert len(marketing.get_content_calendar_ideas(restaurant_id=rid, force=True)) == 7 and len(calls) == 1
    calls.clear()
    marketing._cache_calendar(rid, [])
    no_monday = [_idea(d) for d in DAYS if d != "Monday"]
    _model(monkeypatch, [no_monday, no_monday], calls)
    monkeypatch.setattr(marketing, "RECENT_CALENDAR_SECONDS", -1)
    week = marketing.get_content_calendar_ideas(restaurant_id=rid, force=True)
    assert "Monday" not in [i["day"] for i in week] and len(week) == 6                  # never invented
    assert len(calls) == 2


def test_one_idea_a_day_and_the_margin_idea_takes_its_day():
    ideas = [_idea("Thursday", "model"), {**_idea("Thursday", "margin"), "source": "menu_margins"}, _idea("Monday")]
    out = marketing._one_per_day(ideas)
    assert [i["day"] for i in out] == ["Monday", "Thursday"] and out[1]["angle"] == "margin"
    src = open("marketing.py", encoding="utf-8").read()
    assert 'return [i for i in ideas if i.get("source") != "menu_margins" and i.get("day") != day] + [idea]' in src


# ── guest contacts, the AI monitor ──────────────────────────────────────────

def test_guest_contacts_reads_at_page_size():
    modal = _between('<div id="guest-contacts-modal"', '<div id="pw-modal"')
    assert 'class="gc-title">Guest contacts</div>' in modal and 'class="gc-help"' in modal
    assert "font-size:11px" not in modal and "font-size:12px" not in modal
    assert 'class="ac-input gc-in"' in modal
    rows = _between("list.innerHTML = mktLoading('Loading your guests", "function ")
    assert "font-size:15.5px;font-weight:600" in rows and "font-size:12px" not in rows


def test_the_monitor_breathes_rather_than_laps_its_edge():
    assert "@keyframes aiOrbit" not in SRC and "stroke-dasharray:14 86" not in SRC
    assert "animation:aiRing 6s ease-in-out infinite" in SRC and "animation:aiSweep 9s ease-in-out infinite" in SRC
    assert "@media (prefers-reduced-motion:reduce){.cbtn.ai-strip::after{animation:none;display:none}" in SRC


# ── Food Cost hero ──────────────────────────────────────────────────────────

def test_the_food_cost_figure_has_no_list_under_it_and_the_trend_sits_close():
    fn = _between("function fc2LoadFoodCostPct(){", "\n}\n")
    assert '<ul class="miss">' not in fn and "title=\"'+wtEsc(why.join('; '))+'\"" in fn
    assert "#panel-inventory .fc2-position:not(:has(>:not([hidden]))){display:none}" in SRC
