"""dsr.scorecard — "Did we win today?" at the top of the Owner DSR (9/25/26):
Today's Score, the executive summary, Today's Wins and Today's Risks — every
figure from the night's measured blocks, nothing scored that wasn't
measured, and none of it in the manager's view."""
import sys
from datetime import date, timedelta

import pytest

import auth
import models
import pos
import dsr
from dsr import access, scorecard, store
from models import Restaurant, create_restaurant, update_restaurant, get_restaurant

SAT = date(2026, 9, 19)


@pytest.fixture
def db(db_path, monkeypatch):
    real = models.get_conn

    def conn(*a, **k):
        return real(db_path)
    for mod in list(sys.modules.values()):
        if mod is not None and getattr(mod, "get_conn", None) is real:
            monkeypatch.setattr(mod, "get_conn", conn)
    monkeypatch.setattr(models, "get_conn", conn)
    monkeypatch.setattr(models, "DB_PATH", db_path)
    monkeypatch.setattr(auth, "DB_PATH", db_path)
    monkeypatch.setattr(pos, "PROVIDERS", None)
    return db_path


def _rest(db, **kw):
    rid = create_restaurant(Restaurant(name="Simple EJ's", owner_email="e@x.com", timezone="America/Chicago"),
                            db_path=db)
    fields = {"labor_target_pct": 28.0, "labor_target_source": "set", "food_cost_target": 30.0,
              "food_cost_target_source": "set"}
    fields.update(kw)
    update_restaurant(rid, fields, db_path=db)
    return get_restaurant(rid, db_path=db)


def _night(db, rid, day, net, cats, avg_ticket=40.0, budget_net=None, labor=None, food=None, reviews=None):
    r = store.create_report(rid, day, trigger="sweep", db_path=db)
    m = dict({"net": net, "gross": round(net * 1.06, 2), "avg_ticket": avg_ticket},
             **{f"cat:{k}": v for k, v in cats.items()})
    if budget_net is not None:
        m.update(budget_net=budget_net, vs_budget_net=round(net - budget_net, 2),
                 vs_budget_net_pct=round((net - budget_net) / budget_net * 100, 1))
    store.save_block(r["id"], "sales", dsr.block(dsr.READY, source="rpower", metrics=m), db_path=db)
    if labor:
        store.save_block(r["id"], "labor", dsr.block(dsr.READY, source="rpower", metrics=labor), db_path=db)
    if food:
        fm, fd = food
        store.save_block(r["id"], "food", dsr.block(dsr.READY, source="cavnar", metrics=fm, detail=fd), db_path=db)
    if reviews:
        rm, rd = reviews
        store.save_block(r["id"], "reviews", dsr.block(dsr.READY, source="google", metrics=rm, detail=rd), db_path=db)
    store.set_stage(r["id"], "collecting", db_path=db)
    store.set_stage(r["id"], "final", db_path=db)
    return store.get_report(rid, day, db_path=db)


def _history(db, rid):
    # Four usual Saturdays (liquor ~$1,000) and a fortnight of tickets under $44.
    for w in range(1, 5):
        _night(db, rid, SAT - timedelta(days=7 * w), 8000.0, {"Food": 5200.0, "Liquor": 1000.0, "Beer": 900.0},
               avg_ticket=41.0)
    for d in range(1, 15):
        day = SAT - timedelta(days=d)
        if day.weekday() != 5:
            _night(db, rid, day, 5000.0, {"Food": 3400.0}, avg_ticket=43.5)


def _big_night(db, r):
    return _night(
        db, r.id, SAT, 9200.0, {"Food": 5600.0, "Liquor": 1400.0, "Beer": 640.0}, avg_ticket=46.25, budget_net=8480.0,
        labor={"pct": 27.2, "cost": 2502.0, "target_pct": 28.0, "overtime_hours": 0, "no_shows": 0},
        food=({"est_food_cost_pct": 29.9}, {"stock": {"critical": [
            {"item": "Miller Lite keg", "days_remaining": 1.5}, {"item": "Limes", "days_remaining": 3}],
            "critical_count": 3}}),
        reviews=({"received": 5, "avg_rating": 4.6, "negative": 1, "drafts_awaiting": 2},
                 {"reviews": [{"rating": 5}, {"rating": 5}, {"rating": 5}, {"rating": 4}, {"rating": 4}]}))


def test_the_owner_sees_today_s_score_first(db):
    r = _rest(db)
    _history(db, r.id)
    rep = _big_night(db, r)
    p = access.render(rep, {"role": "owner"}, r)
    card = p["scorecard"]
    comps = {c["key"]: c for c in card["components"]}
    assert comps["sales"]["value"] == "+$720 vs budget"
    assert comps["labor"]["value"] == "0.8 pts below your target"
    assert comps["food"]["value"] == "On target"
    assert comps["guests"]["value"] == "★★★★☆" and comps["guests"]["detail"] == "4.6 across 5 reviews"
    assert card["overall"] is not None and 85 <= card["overall"] <= 100
    assert card["verdict"] == {"label": "Excellent day", "tone": "good"}
    wins = [w["text"] for w in card["wins"]]
    assert "Net sales +$720 vs budget (+8.5%)" in wins
    assert "Liquor sales +40.0% vs a usual Saturday" in wins
    assert len(wins) <= scorecard.MAX_ITEMS
    risks = [x["text"] for x in card["risks"]]
    assert risks[0] == "Miller Lite keg running low (1.5 days left)"
    assert risks[1] == "Limes running low (3 days left)" and "1 negative review" in risks
    assert len(risks) == scorecard.MAX_ITEMS         # "1 more item critically low" ranks sixth
    assert "2 review replies waiting to go out" in risks
    # Beer at $640 against a usual $900 is −28.9%.
    assert "Beer sales −28.9% vs a usual Saturday" in risks


def test_wins_that_need_history_say_nothing_without_it(db):
    r = _rest(db)
    rep = _big_night(db, r)                    # no earlier nights at all
    card = access.render(rep, {"role": "owner"}, r)["scorecard"]
    texts = " ".join(w["text"] for w in card["wins"] + card["risks"])
    assert "usual Saturday" not in texts and "Highest average ticket" not in texts


def test_the_highest_ticket_in_three_weeks_is_a_win(db):
    r = _rest(db)
    _history(db, r.id)
    rep = _big_night(db, r)
    comps = scorecard.build(rep["facts"], r)["components"]
    wins, _risks = scorecard._signals(rep["facts"]["blocks"], r, SAT, None, comps)
    assert "Highest average ticket in 3 weeks ($46.25)" in [w[1]["text"] for w in wins]


def test_an_unmeasured_component_is_left_out_never_zero(db):
    r = _rest(db)
    rep = _night(db, r.id, SAT, 9200.0, {"Food": 5600.0}, budget_net=8480.0,
                 labor={"pct": 27.2, "target_pct": 28.0})
    card = access.render(rep, {"role": "owner"}, r)["scorecard"]
    comps = {c["key"]: c for c in card["components"]}
    assert comps["food"]["measured"] is False and comps["food"]["value"] is None
    assert comps["guests"]["measured"] is False
    # sales 40 × curve(+8.5%) and labor 25 × curve(−0.8 pts), re-weighted over 65.
    want = round((40 * scorecard.curve("sales", 8.5) + 25 * scorecard.curve("labor", -0.8)) / 65)
    assert card["overall"] == want


def test_sales_alone_is_not_enough_to_score(db):
    r = _rest(db)
    card = access.render(_night(db, r.id, SAT, 9200.0, {"Food": 5600.0}, budget_net=8480.0),
                         {"role": "owner"}, r)["scorecard"]
    assert card["overall"] is None and card["verdict"] is None and "Not enough" in card["basis"]


def test_a_starting_target_never_turns_red(db):
    r = _rest(db, labor_target_source="default", labor_target_pct=30.0)
    rep = _night(db, r.id, SAT, 9200.0, {"Food": 5600.0}, budget_net=8480.0,
                 labor={"pct": 36.0, "target_pct": 30.0})
    lab = {c["key"]: c for c in access.render(rep, {"role": "owner"}, r)["scorecard"]["components"]}["labor"]
    assert lab["tone"] == "warn" and "Cavnar AI's starting target" in lab["value"]


def test_a_tough_night_reads_tough(db):
    r = _rest(db)
    rep = _night(db, r.id, SAT, 6800.0, {"Food": 5000.0}, budget_net=8480.0,
                 labor={"pct": 34.0, "target_pct": 28.0, "overtime_hours": 6.5},
                 food=({"est_food_cost_pct": 34.0}, {}))
    card = access.render(rep, {"role": "owner"}, r)["scorecard"]
    assert card["verdict"]["label"] == "Tough day" and card["verdict"]["tone"] == "bad"
    risks = [x["text"] for x in card["risks"]]
    assert "Net sales −$1,680 vs budget (−19.8%)" in risks and "6.5 overtime hours" in risks


def test_the_manager_view_has_no_scorecard(db):
    r = _rest(db)
    p = access.render(_big_night(db, r), {"role": "manager"}, r)
    assert p["scorecard"] is None


def test_the_narrative_tops_up_the_lists(db):
    r = _rest(db)
    rep = _night(db, r.id, SAT, 9200.0, {"Food": 5600.0}, budget_net=8480.0)
    card = scorecard.build(rep["facts"], r, narrative={"went_well": [{"text": "The patio turned twice."}],
                                                       "needs_attention": ["Sunday reservations are light."]})
    assert card["wins"][-1] == {"text": "The patio turned twice.", "key": None, "source": "narrative"}
    assert card["risks"][-1]["text"] == "Sunday reservations are light."


def test_the_email_and_push_lead_with_the_score(db):
    import emails
    from dsr import deliver
    r = _rest(db)
    _history(db, r.id)
    rep = _big_night(db, r)
    d = deliver.digest(access.render(rep, {"role": "owner"}, r), r)
    card = d["scorecard"]
    subject, html, pre = emails.dsr_email(d)
    assert subject == f"Simple EJ's · Sat 9/19/26 · Excellent day {card['overall']}/100 · $9,200 net"
    assert pre.startswith(f"Excellent day {card['overall']}/100 · $9,200 net · +$720 vs budget")
    i = [html.index(k) for k in ("Today&rsquo;s score", "Today&rsquo;s wins", "Today&rsquo;s risks")]
    assert i == sorted(i) and "Miller Lite keg running low" in html
    assert "Went well" not in html and "Needs attention" not in html
    title, body = deliver.push_text(d)
    assert body.startswith(f"Simple EJ's · Sat 9/19/26: Excellent day, {card['overall']}/100 · $9,200 net, +$720 vs budget.")
    assert "Watch: Miller Lite keg running low (1.5 days left)." in body


def test_the_manager_email_keeps_its_layout(db):
    import emails
    from dsr import deliver
    r = _rest(db)
    d = deliver.digest(access.render(_big_night(db, r), {"role": "manager"}, r), r)
    subject, html, _pre = emails.dsr_email(d)
    assert d["scorecard"] is None and "/100" not in subject and "Today&rsquo;s score" not in html
