"""Memory audit 9/29/26, workstream M1 — who answered, what the reasons
teach, and what learning did to a ranking.

  reasons       too costly → a bounded, decaying ease penalty on the kind;
                doesn't fit → a decline weight on the subject's tags; bad
                timing → a deferral in no denominator; don't trust the data →
                every card on the source capped until it is re-verified.
  who_answered  a delegate's decline holds for that login only, the owner is
                shown who passed on it, and learning reads each side apart.
  view_as       an admin's answer through view-as is kept in the trail and
                changes nothing else: no silence, no learning.
  rank_log      every shown card carries its rank score, learned weight, why,
                prior rung and model version; each build logs what it did not
                show; the admin read compares by version and weight bucket.
"""
import json

import pytest

import models
import rec_ledger as rl
import rec_learning
from models import Restaurant, create_restaurant


@pytest.fixture(autouse=True)
def _db(db_path, monkeypatch):
    real = models.get_conn
    monkeypatch.setattr(models, "get_conn", lambda *a, **k: real(db_path))
    monkeypatch.setattr(models, "DB_PATH", db_path)
    yield


def _rid(db_path, name="Answer Co"):
    return create_restaurant(Restaurant(name=name, owner_email=f"{name.split()[0].lower()}@x.test"), db_path=db_path)


def _sql(db_path, sql, *args):
    c = models.get_conn(db_path)
    c.execute(sql, args)
    c.commit()
    c.close()


def _one(db_path, sql, *args):
    c = models.get_conn(db_path)
    try:
        return c.execute(sql, args).fetchone()
    finally:
        c.close()


def _user(db_path, rid, name, role):
    from auth import init_auth
    init_auth(db_path)
    c = models.get_conn(db_path)
    cur = c.execute("INSERT INTO users (username, email, password_hash, restaurant_id, role) VALUES (?,?,?,?,?)",
                    (name, f"{name.lower()}@x.test", "x", rid, role))
    c.commit()
    uid = cur.lastrowid
    c.close()
    return uid


# ── reasons ──────────────────────────────────────────────────────────────────

def test_too_costly_answers_lower_the_kind_boundedly_and_decay(db_path):
    rid = _rid(db_path)
    for dish in ("Soup", "Salad", "Steak"):
        rl.present(rid, f"reprice:{dish}", "food", "home", db_path=db_path)
        rl.record(rid, f"reprice:{dish}", "dismissed", meta={"kind": "not_for_us", "reason_code": "too_costly"},
                  db_path=db_path)
    eff = rec_learning.effectiveness(rid, db_path=db_path)
    w, why = eff.weight("reprice:Burger")
    base, _ = rec_learning.effectiveness(_rid(db_path, "Other Co"), db_path=db_path).weight("reprice:Burger")
    assert w < base and any("cost too much" in y for y in why)
    costly, _unfit = eff.reason_penalties("reprice", [])
    assert costly == pytest.approx(0.15)                         # 3 × 0.05, at the cap
    # Old answers weigh less: a year on, the penalty has decayed.
    _sql(db_path, "UPDATE rec_events SET at=datetime('now','-365 days') WHERE restaurant_id=? AND event='dismissed'",
         rid)
    assert rec_learning.effectiveness(rid, db_path=db_path).reason_penalties("reprice", [])[0] < 0.02


def test_doesnt_fit_lowers_advice_on_the_same_subject_tag(db_path):
    rid = _rid(db_path)
    rl.present(rid, "trim_day:Saturday", "labor", "home", db_path=db_path)
    rl.record(rid, "trim_day:Saturday", "dismissed", meta={"kind": "not_for_us", "reason_code": "doesnt_fit"},
              db_path=db_path)
    eff = rec_learning.effectiveness(rid, db_path=db_path)
    _c, unfit = eff.reason_penalties("trim_day", rl.tags_for("trim_day:Sunday"))       # weekend staffing
    assert unfit > 0
    assert eff.reason_penalties("trim_day", rl.tags_for("trim_day:Tuesday"))[1] == 0 or \
        eff.reason_penalties("trim_day", rl.tags_for("trim_day:Tuesday"))[1] <= unfit


def test_bad_timing_is_in_no_acceptance_denominator(db_path):
    rid = _rid(db_path)
    rl.present(rid, "trim_day:Friday", "labor", "home", db_path=db_path)
    rl.record(rid, "trim_day:Friday", "dismissed", meta={"kind": "not_for_us", "reason_code": "bad_timing"},
              db_path=db_path)
    s = rec_learning.summary(rid, days=30, db_path=db_path)
    assert s["totals"]["settled"] == 0 and s["by_module"]["labor"]["dismissed"] == 0
    item = rec_learning.timeline(rid, db_path=db_path)["items"][0]
    assert item["answer"] == "deferred" and item["reason_code"] == "bad_timing"


def test_already_doing_counts_as_taken(db_path):
    rid = _rid(db_path)
    rl.present(rid, "trim_day:Monday", "labor", "home", db_path=db_path)
    rl.record(rid, "trim_day:Monday", "dismissed", meta={"kind": "not_for_us", "reason_code": "already_doing"},
              db_path=db_path)
    s = rec_learning.summary(rid, days=30, db_path=db_path)
    assert s["totals"]["taken"] == 1


def test_dont_trust_the_data_caps_every_card_on_its_source_until_reverified(db_path):
    import rec_trust
    rid = _rid(db_path)
    rl.present(rid, "trim_day:Monday", "labor", "home", evidence_sources=["labor"], db_path=db_path)
    rl.record(rid, "trim_day:Monday", "dismissed", meta={"kind": "not_for_us", "reason_code": "dont_trust_data"},
              db_path=db_path)
    open_rows = rl.distrusted_sources(rid, db_path=db_path)
    assert "labor" in open_rows and "trim_day" in open_rows["labor"]["kinds"]
    # A different KIND on the same source is capped too.
    conf = rec_trust.assess(rid, "labor_over:2026-09-21", evidence={"n": 60, "kind": "trading_days"},
                            sources=("labor", "pos"), db_path=db_path)
    assert conf["dimensions"]["evidence"]["pct"] <= rec_trust.DISTRUST_CAP
    # The answered key holds until the source is re-verified.
    assert _one(db_path, "SELECT silence_rule FROM rec_instances WHERE key='trim_day:Monday'")[0] == "distrust"
    # Still held after 30 days (it used to be forgotten then).
    _sql(db_path, "UPDATE rec_events SET at=datetime('now','-40 days') WHERE restaurant_id=?", rid)
    conf = rec_trust.assess(rid, "labor_over:2026-09-21", evidence={"n": 60, "kind": "trading_days"},
                            sources=("labor",), db_path=db_path)
    assert conf["dimensions"]["evidence"]["pct"] <= rec_trust.DISTRUST_CAP
    # Data Health shows it as an open item.
    import data_health
    items = data_health.distrusted_items(rid, db_path=db_path)
    assert items and items[0]["source"] == "labor" and "re-verify" in items[0]["text"]
    assert "-" not in items[0]["since"]                                   # M/D/YY
    out = rl.verify_source(rid, "labor", user_id=1, db_path=db_path)
    assert out["closed"] == 1 and out["released"] == 1
    assert "trim_day:Monday" not in rl.silenced_keys(rid, db_path=db_path)
    conf = rec_trust.assess(rid, "labor_over:2026-09-21", evidence={"n": 60, "kind": "trading_days"},
                            sources=("labor",), db_path=db_path)
    assert conf["dimensions"]["evidence"]["pct"] > rec_trust.DISTRUST_CAP


def test_the_platform_sync_reads_what_each_reason_means():
    from intelligence import feedback
    assert feedback._ledger_action("k", "dismissed", {"kind": "not_for_us", "reason_code": "already_doing"}) == "done"
    assert feedback._ledger_action("k", "dismissed", {"kind": "not_for_us", "reason_code": "bad_timing"}) == "snoozed"
    assert feedback._ledger_action("k", "dismissed", {"kind": "not_for_us", "reason_code": "too_costly"}) == \
        "not_for_us"


# ── who answered ─────────────────────────────────────────────────────────────

def test_a_managers_decline_holds_for_that_manager_only(db_path):
    rid = _rid(db_path)
    dana = _user(db_path, rid, "Dana", "manager")
    rl.present(rid, "trim_day:Tuesday", "labor", "home", title="Trim Tuesday lunch by one server", db_path=db_path)
    rl.record(rid, "trim_day:Tuesday", "dismissed", user_id=dana, role="manager", authority="delegate",
              meta={"kind": "not_for_us", "reason_code": "already_doing"}, db_path=db_path)
    # Open for the owner, silenced for Dana.
    row = _one(db_path, "SELECT status, silenced_until FROM rec_instances WHERE key='trim_day:Tuesday'")
    assert row["status"] == "open" and row["silenced_until"] is None
    assert "trim_day:Tuesday" not in rl.silenced_keys(rid, db_path=db_path)
    assert "trim_day:Tuesday" in rl.silenced_keys(rid, db_path=db_path, viewer={"id": dana})
    assert "trim_day:Tuesday" in rl.silenced_keys(rid, db_path=db_path, viewer=dana)
    # The owner is told who passed on it, and why.
    d = rl.delegate_answers(rid, ["trim_day:Tuesday"], db_path=db_path)["trim_day:Tuesday"]
    assert d["by"] == "Dana" and d["text"].startswith("Dana passed on this: already doing it")
    # Not the owner's decline on any other surface.
    import insight_store
    assert "labor:day:tuesday" not in insight_store.declined_signatures(rid, db_path=db_path)
    # Recorded with its authority.
    assert _one(db_path, "SELECT authority FROM rec_events WHERE event='dismissed'")[0] == "delegate"


def test_learning_reads_each_side_apart_and_the_owner_outranks(db_path):
    rid = _rid(db_path)
    dana = _user(db_path, rid, "Dana", "manager")
    rl.present(rid, "trim_day:Tuesday", "labor", "home", db_path=db_path)
    rl.record(rid, "trim_day:Tuesday", "dismissed", user_id=dana, authority="delegate",
              meta={"kind": "not_for_us"}, db_path=db_path)
    _sql(db_path, "UPDATE rec_instances SET created_at=datetime('now','-20 days')")
    rl.expire_stale(db_path=db_path)
    conn = models.get_conn(db_path)
    try:
        own = rec_learning._load(conn, rid)
        mgr = rec_learning._load(conn, rid, perspective="delegate")
    finally:
        conn.close()
    # From the owner's side the manager's decline is not the owner ignoring it.
    assert own[0]["state"] == "delegated" and mgr[0]["state"] == "dismissed"
    s = rec_learning.summary(rid, days=30, db_path=db_path)
    assert s["totals"]["settled"] == 0
    # A principal's answer outranks it on both sides.
    rl.present(rid, "trim_day:Wednesday", "labor", "home", db_path=db_path)
    rl.record(rid, "trim_day:Wednesday", "dismissed", user_id=dana, authority="delegate",
              meta={"kind": "not_for_us"}, db_path=db_path)
    rl.record(rid, "trim_day:Wednesday", "completed", authority="principal", db_path=db_path)
    conn = models.get_conn(db_path)
    try:
        mgr = {e["key"]: e for e in rec_learning._load(conn, rid, perspective="delegate")}
    finally:
        conn.close()
    assert mgr["trim_day:Wednesday"]["state"] == "completed"


def test_the_perspective_of_a_login():
    assert rec_learning.perspective_of({"id": 1, "role": "manager"}) == "delegate"
    assert rec_learning.perspective_of({"id": 1, "is_admin": 1}) == "principal"


# ── view as ──────────────────────────────────────────────────────────────────

def test_an_admins_view_as_answer_is_kept_and_changes_nothing(db_path):
    rid = _rid(db_path)
    rl.present(rid, "reprice:Soup", "food", "home", db_path=db_path)
    via = {"admin_id": 999, "admin": "will", "role": "admin"}
    assert rl.record(rid, "reprice:Soup", "dismissed", user_id=5, via=via,
                     meta={"kind": "not_for_us", "reason_code": "too_costly"}, db_path=db_path)
    row = _one(db_path, "SELECT status, silenced_until FROM rec_instances WHERE key='reprice:Soup'")
    assert row["status"] == "open" and row["silenced_until"] is None
    ev = _one(db_path, "SELECT authority, meta FROM rec_events WHERE event='dismissed'")
    assert ev["authority"] == "admin" and json.loads(ev["meta"])["via"]["admin_id"] == 999
    # Not silenced for the owner, silenced for the admin's own login.
    assert "reprice:Soup" not in rl.silenced_keys(rid, db_path=db_path)
    assert "reprice:Soup" in rl.silenced_keys(rid, db_path=db_path, viewer=999)
    # Never learned from: no "too costly" penalty, no decision in the history.
    assert rec_learning.effectiveness(rid, db_path=db_path).reason_penalties("reprice", [])[0] == 0
    import decisions
    assert not [r for r in decisions.history(rid, db_path=db_path) if r["key"] == "reprice:Soup"
                and r["answer"] == "not for us"]


def test_a_view_as_request_is_read_as_the_admin_behind_it():
    via = rl.request_via({"id": 5, "acting_admin_id": 99, "acting_admin": "will", "acting_admin_role": "admin"})
    assert via == {"admin_id": 99, "admin": "will", "role": "admin"}
    assert rl.silence_subject({"id": 5, "acting_admin_id": 99, "acting_admin_role": "support"}) == 99
    assert rl.silence_subject({"id": 5}) == 5
    assert rl.request_via({"id": 5}) is None


# ── rank log ─────────────────────────────────────────────────────────────────

def test_a_shown_card_carries_what_learning_did_to_its_rank(db_path):
    import home_brief
    rid = _rid(db_path)
    eff = rec_learning.effectiveness(rid, db_path=db_path)
    recs = [{"key": "trim_day:Saturday", "title": "Trim Saturday", "timeframe": "Next schedule",
             "dollars_monthly": 400, "effort": "medium"},
            {"key": "reprice:Soup", "title": "Reprice Soup", "timeframe": "This week", "dollars_monthly": 90,
             "effort": "low"}]
    ranked = home_brief.order_recommendations(recs, learned=eff)
    rank = ranked[0]["rank"]
    assert {"base", "score", "weight", "rung", "version"} <= set(rank)
    assert rank["version"] == rec_learning.EFFECTIVENESS_VERSION
    ids = rl.present_many(rid, [{"key": r["key"], "module": "labor", "rank": r["rank"]} for r in ranked], "home",
                          db_path=db_path)
    meta = json.loads(_one(db_path, "SELECT meta FROM rec_events WHERE rec_id=? AND event='shown'",
                           ids["trim_day:Saturday"])["meta"])
    assert meta["rank"]["version"] == rec_learning.EFFECTIVENESS_VERSION and "weight" in meta["rank"]
    assert rl.log_rank_build(rid, "home", shown=[dict(ranked[0]["rank"], key="trim_day:Saturday")],
                             not_shown=[dict(ranked[1]["rank"], key="reprice:Soup")], version=2, db_path=db_path)
    row = _one(db_path, "SELECT shown, not_shown, version FROM rec_rank_builds WHERE restaurant_id=?", rid)
    assert json.loads(row["not_shown"])[0]["key"] == "reprice:Soup" and row["version"] == 2
    # One row per surface and local day: the latest build wins.
    rl.log_rank_build(rid, "home", shown=[], not_shown=[], version=2, db_path=db_path)
    assert _one(db_path, "SELECT COUNT(*) FROM rec_rank_builds WHERE restaurant_id=?", rid)[0] == 1


def test_the_admin_read_compares_by_version_and_weight(db_path):
    import admin_ops
    rid = _rid(db_path)
    rl.present_many(rid, [{"key": "trim_day:Monday", "module": "labor",
                           "rank": {"weight": 1.2, "version": 2, "rung": "own"}}], "home", db_path=db_path)
    rl.record(rid, "trim_day:Monday", "completed", db_path=db_path)
    rl.present_many(rid, [{"key": "trim_day:Friday", "module": "labor",
                           "rank": {"weight": 0.8, "version": 2, "rung": "own"}}], "home", db_path=db_path)
    out = admin_ops.rank_learning(days=30, restaurant_id=rid)
    by = {(g["version"], g["bucket"]): g for g in out["groups"]}
    assert by[(2, "raised (over 1.1x)")]["taken"] == 1
    assert by[(2, "lowered (under 0.9x)")]["shown"] == 1


def test_an_ineligible_account_ranks_on_the_neutral_model(db_path):
    rid = _rid(db_path, "Demo Co")
    for dish in ("Soup", "Salad", "Steak"):
        rl.present(rid, f"reprice:{dish}", "food", "home", db_path=db_path)
        rl.record(rid, f"reprice:{dish}", "dismissed", meta={"kind": "not_for_us", "reason_code": "too_costly"},
                  db_path=db_path)
    _sql(db_path, "UPDATE restaurants SET is_demo=1 WHERE id=?", rid)
    eff = rec_learning.effectiveness(rid, db_path=db_path)
    assert eff.weight("reprice:Burger") == (1.0, []) or eff.weight("reprice:Burger")[0] >= 0.9
    assert eff.reason_penalties("reprice", []) == (0, 0)
