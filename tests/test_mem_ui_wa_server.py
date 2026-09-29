"""Memory round, UI wave — web owner dashboard, part A (UI-WA): the server
pieces the screens needed that the workstreams' payloads did not carry, and
the reads the new screens make, answered with the fields they draw.

  * the 30-day policy notice (policy_notice): one date, principals only,
    dismissed per login for good, on Home and the group Home, both twins;
  * Ask's `declined_repeats` reaches the client (it stopped at the meta);
  * a confirm card's dates read M/D/YY ("Closed on: 2026-10-05" was ISO);
  * the targets card's goal (`targets.labor.goal` / `.food.goal`) and who
    set each target (`set_notes`) — M2's report named the goal field, the
    merged payload had none;
  * the alert types a login may mute, with the bell's labels;
  * the server-rendered answer row says "Not for us", never "Pass";
  * a render check: every JSON read the new Account, Data Health and Home
    pieces make answers with the fields the page reads.
"""
import sys
from datetime import date, timedelta

import pytest
from flask import Flask

import auth
import change_log
import goals
import home_brief
import models
import owner_memory
import policy_notice
import strategy_routes
from models import Restaurant, create_restaurant, update_restaurant


@pytest.fixture(autouse=True)
def _db(monkeypatch, db_path):
    real = models.get_conn

    def conn(*a, **k):
        return real(db_path)
    for mod in list(sys.modules.values()):
        if mod is not None and getattr(mod, "get_conn", None) is real:
            monkeypatch.setattr(mod, "get_conn", conn, raising=False)
    monkeypatch.setattr(models, "get_conn", conn)
    monkeypatch.setattr(models, "DB_PATH", db_path)
    auth.init_auth(db_path=db_path)
    owner_memory.invalidate_targets()
    home_brief.invalidate()
    yield
    owner_memory.invalidate_targets()
    home_brief.invalidate()


def _rid(name="Notice Co", **kw):
    return create_restaurant(Restaurant(name=name, owner_email=f"{name[:3].lower()}@x.test", owner_name="Erik",
                                        **kw))


def _login(rid, username, role):
    uid = auth.create_user(rid, username, f"{username}@x.test", "correct-horse")
    c = models.get_conn()
    c.execute("UPDATE users SET role=? WHERE id=?", (role, uid))
    c.commit()
    c.close()
    return {"id": uid, "restaurant_id": rid, "base_restaurant_id": rid, "is_admin": 0, "role": role,
            "username": username, "email": f"{username}@x.test"}


# ── the policy notice ────────────────────────────────────────────────────────

def test_no_notice_until_the_date_is_set(monkeypatch):
    rid = _rid()
    owner = _login(rid, "erik", "client")
    monkeypatch.setattr(policy_notice, "POLICY_UPDATED_ON", None)
    assert policy_notice.notice_for(owner) is None
    assert policy_notice.dismiss(owner) is False


def test_an_account_holder_sees_it_for_thirty_days_in_mdy_with_the_link(monkeypatch):
    rid = _rid()
    owner = _login(rid, "erik", "client")
    start = date(2026, 10, 1)
    monkeypatch.setattr(policy_notice, "POLICY_UPDATED_ON", start)
    n = policy_notice.notice_for(owner, today=start)
    assert n["text"] == ("We updated our Privacy Policy and Terms on 10/1/26: how Cavnar AI’s benchmarks "
                         "use pooled, de-identified figures.")
    assert n["url"] == "https://cavnar.ai/privacy" and n["link_label"] == "Read what changed"
    assert n["updated_label"] == "10/1/26" and "2026-" not in n["text"]
    assert n["dismiss"] == {"web": "/api/account/policy-notice/dismiss",
                            "mobile": "/mobile/api/account/policy-notice/dismiss"}
    assert policy_notice.notice_for(owner, today=start + timedelta(days=29)) is not None
    assert policy_notice.notice_for(owner, today=start + timedelta(days=30)) is None
    assert policy_notice.notice_for(owner, today=start - timedelta(days=1)) is None


def test_a_manager_and_an_admin_view_as_never_see_it_or_dismiss_it(monkeypatch):
    rid = _rid()
    start = date.today()
    monkeypatch.setattr(policy_notice, "POLICY_UPDATED_ON", start)
    manager = _login(rid, "dana", "manager")
    owner = _login(rid, "erik", "client")
    view_as = dict(owner, acting_admin_id=99)
    assert policy_notice.notice_for(manager) is None
    assert policy_notice.notice_for(view_as) is None
    assert policy_notice.dismiss(view_as) is False
    assert policy_notice.notice_for(owner) is not None      # support's view-as left it for the owner
    out, st = strategy_routes._do_policy_notice_dismiss(manager)
    assert st == 403


def test_a_dismissal_holds_per_login_and_a_later_change_shows_again(monkeypatch):
    rid = _rid()
    start = date.today()
    monkeypatch.setattr(policy_notice, "POLICY_UPDATED_ON", start)
    erik = _login(rid, "erik", "client")
    co = _login(rid, "maria", "client")
    out, st = strategy_routes._do_policy_notice_dismiss(erik)
    assert st == 200 and out["dismissed"] is True
    assert policy_notice.notice_for(erik) is None
    assert policy_notice.notice_for(co) is not None          # per login, not per restaurant
    monkeypatch.setattr(policy_notice, "POLICY_UPDATED_ON", start + timedelta(days=1))
    assert policy_notice.notice_for(erik, today=start + timedelta(days=1)) is not None


def test_the_dismiss_route_is_on_both_surfaces():
    rules = {(p, tuple(m)) for p, m, _b, _e in strategy_routes._ROUTES}
    assert ("/account/policy-notice/dismiss", ("POST",)) in rules
    app = Flask(__name__)
    app.register_blueprint(strategy_routes.strategy_bp)
    app.register_blueprint(strategy_routes.strategy_mobile_bp)
    have = {r.rule for r in app.url_map.iter_rules()}
    assert "/api/account/policy-notice/dismiss" in have
    assert "/mobile/api/account/policy-notice/dismiss" in have


def test_home_and_the_group_home_carry_it(monkeypatch):
    rid = _rid(module_reviews=1)
    owner = _login(rid, "erik", "client")
    monkeypatch.setattr(policy_notice, "POLICY_UPDATED_ON", date.today())
    import anthropic
    monkeypatch.setattr(anthropic, "Anthropic", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("AI on Home")))
    p, st = home_brief.build_home_brief(owner, fresh=True)
    assert st == 200 and p["policy_notice"] and p["policy_notice"]["text"].startswith("We updated our Privacy")
    assert "kind_holds" in p and "quieter" in p
    strategy_routes._do_policy_notice_dismiss(owner)
    p2, _ = home_brief.build_home_brief(owner)                # the dismissal dropped this login's cache
    assert p2["policy_notice"] is None
    src = open(home_brief.__file__, encoding="utf-8").read()
    group = src[src.index("def build_group_brief("):]
    group = group[:group.find("\ndef ", 10) if group.find("\ndef ", 10) > 0 else len(group)]
    assert '"policy_notice": _policy_notice(current_user, restaurant=base)' in group


def test_the_users_column_is_created_at_boot():
    c = models.get_conn()
    cols = {r[1] for r in c.execute("PRAGMA table_info(users)").fetchall()}
    c.close()
    assert "policy_notice_dismissed" in cols
    src = open(auth.__file__, encoding="utf-8").read()
    assert '"ALTER TABLE users ADD COLUMN policy_notice_dismissed TEXT"' in src


# ── Ask: declined repeats and the confirm card's dates ───────────────────────

def test_declined_repeats_reach_the_client():
    import client_api
    reps = [{"text": "Cut a Friday closer", "signature": "labor:day:friday", "declined_on": "8/12/26"}]
    assert client_api._ask_meta({"declined_repeats": reps})["declined_repeats"] == reps
    assert client_api._ask_meta(None)["declined_repeats"] == []


def test_a_confirm_cards_dates_read_mdy_and_the_body_stays_iso():
    import ask_cavnar_tools as tools
    rid = _rid()
    day = (date.today() + timedelta(days=14))
    card = tools.build_proposal("add_closed_date", {"date": day.isoformat()}, restaurant_id=rid)
    assert card["body"] == {"add": day.isoformat()}
    shown = {f["key"]: f["value"] for f in card["fields_shown"]}
    assert shown["add"] == f"{day.month}/{day.day}/{day.strftime('%y')}"
    assert tools._shown(["2026-10-05", "2026-10-12"]) == "10/5/26, 10/12/26"
    assert tools._shown("Tuesday") == "Tuesday" and tools._shown(True) == "On"


def test_the_server_rendered_answer_row_says_not_for_us():
    import client_api
    html = client_api.rec_controls_html("insight_intel:a", "intel", "intel")
    assert ">Not for us<" in html and ">Pass<" not in html


# ── targets: the goal that applies, and who set each ─────────────────────────

def test_the_targets_card_names_the_goal_that_applies_and_who_set_the_target():
    rid = _rid()
    owner = _login(rid, "erik", "client")
    until = date.today() + timedelta(days=60)
    goals.set_goal(rid, "labor_pct", 26, deadline=until.isoformat(), user_id=owner["id"], authority="principal",
                   source="goals")
    owner_memory.invalidate_targets()
    change_log.record(rid, "restaurant", "food_cost_target", 30, 28, actor_user_id=owner["id"], source="owner")
    out, st = strategy_routes._do_targets_get(owner)
    t = out["targets"]
    g = t["labor"]["goal"]
    assert st == 200 and g["pct"] == 26.0
    assert g["label"] == f"Your goal of 26% by {until.month}/{until.day}/{until.strftime('%y')}"
    assert t["food"]["goal"] is None
    assert t["set_notes"]["food_cost_target"].startswith("Set by the owner on ")
    assert "-" not in t["set_notes"]["food_cost_target"].split(" on ")[1]


# ── notifications: the alert types a login may mute ──────────────────────────

def test_the_mute_list_leaves_out_what_no_login_can_mute():
    import preferences
    rid = _rid()
    owner = _login(rid, "erik", "client")
    out, st = strategy_routes._do_preferences_get(owner)
    types = {t["type"]: t["label"] for t in out["alert_types"]}
    assert st == 200 and types["5star"] == "5★ review received"
    assert not set(types) & set(preferences.UNMUTABLE_TYPES)
    assert len(set(types.values())) == len(types)             # one row per label
    assert set(out["mine"]) >= {"push_enabled", "push_muted_types", "quiet_start", "quiet_end", "morning_brief"}


# ── the reads the new screens make, and the fields they draw ─────────────────

def test_every_read_the_new_pieces_make_answers_with_the_fields_they_draw(monkeypatch):
    import data_health
    rid = _rid(module_reviews=1, module_labor=1, module_inventory=1)
    owner = _login(rid, "erik", "client")
    owner_memory.remember(rid, "We close the Monday after Labor Day", kind="constraint", modules=["labor"],
                          user=owner, source="Account", origin="account")
    out, st = strategy_routes._do_memory_list(owner)
    f = out["facts"][0]
    assert st == 200 and {"id", "fact", "kind", "modules", "author", "audience", "valid_until_label", "due_label",
                          "created_on", "can_forget"} <= set(f)
    assert {"kind", "count", "cap"} <= set(out["lanes"][0]) and "archived" in out
    out, st = strategy_routes._do_goals_list(owner)
    assert st == 200 and {"goals", "proposed", "can_confirm"} <= set(out)
    out, st = strategy_routes._do_trust(owner)
    assert st == 200 and "lapsed" in out and {"unedited_in_a_row", "needed", "enabled"} <= set(out["schedule"])
    for band in out["auto_approve"]["bands"].values():
        assert {"trusted", "weak_credit", "autoposted_clean"} <= set(band)
    out, st = strategy_routes._do_preferences_get(owner)
    assert st == 200 and {"mine", "location", "can_apply_to_all", "locations", "alert_types",
                          "unmutable_types", "never_opened"} <= set(out)
    assert all("source" in v for v in out["location"].values())
    import rec_ledger
    rec_ledger.present(rid, "labor_over:x", "labor", "home", title="Labor ran over")
    rec_ledger.record(rid, "labor_over:x", "dismissed", meta={"kind": "not_for_us", "reason_code": "dont_trust_data"},
                      authority="principal")
    dh = data_health.snapshot(rid, use_cache=False)
    assert dh["ok"] and "distrusted" in dh
    for x in dh["distrusted"]:
        assert {"source", "label", "since", "text", "verify"} <= set(x) and x["verify"]["web"] == "/api/data-health/verify"
    change_log.record(rid, "restaurant", "labor_target_pct", 30, 28, actor_user_id=owner["id"], source="owner")
    rows = change_log.for_viewer(rid, owner)
    assert rows and rows[0]["line"].startswith("Labor target: 30 → 28, by the owner on ")
