"""Schedule fix round 10/3/26, workstream H1 — what the schedule learns from,
and whose word it is.

L-4   a change after the week went out is a reaction, never a habit
L-5   Cavnar AI's changes the owner saved are trust in that move, never the
      manager's habit
L-6   every pattern has a denominator: the weeks it could have been made in
L-7   the unattended automatic publish is nobody's acceptance
L-8   an admin's (view-as) change is taken out of the week, not the week out
      of the record — and the owner can adopt it
L-10  an admin's pattern dismissal does not count until adopted
L-15  the edit predictor steers only once its own backtest has earned it
L-26  the owner's redo and discard are kept, and a redo never hides the edits
      made before it
L-27  evidence fades by half-life instead of falling off a cliff
L-30  a standing pattern decays without hand confirmation and is re-tested
L-33  "make it a rule" for a headcount (a role floor) and a start time
L-35  the first weeks ask a one-tap why for a big change
"""
import json
import sys
from datetime import date, timedelta

import pytest
from flask import Flask

import models
import schedule_intel
import schedule_learning as sl
import schedule_memory
import schedule_note_rules as snr
import schedule_rules
import schedule_versions as sv
import staff_settings
from models import Restaurant, create_restaurant

HEAD = "date,day,employee,role,shift_start,shift_end,scheduled_hours,notes\n"
OWNER = {"id": 1, "username": "erik", "role": "owner", "restaurant_id": None}
VIEW_AS = {"id": 1, "username": "erik", "role": "owner", "acting_admin_id": 9, "acting_admin": "will"}


@pytest.fixture(autouse=True)
def _db(db_path, monkeypatch):
    real = models.get_conn

    def conn(*a, **k):
        return real(db_path)
    for mod in list(sys.modules.values()):
        if mod is not None and getattr(mod, "get_conn", None) is real:
            monkeypatch.setattr(mod, "get_conn", conn)
    monkeypatch.setattr(models, "get_conn", conn)
    monkeypatch.setattr(models, "DB_PATH", db_path)
    monkeypatch.setattr(staff_settings, "get_conn", conn)
    monkeypatch.setattr(models, "get_leader_flags", lambda r, db_path=None: {})
    monkeypatch.setattr(models, "get_operational_scores", lambda r, db_path=None: {})
    import auth
    monkeypatch.setattr(auth, "DB_PATH", db_path, raising=False)
    auth.init_auth(db_path=db_path)
    yield


@pytest.fixture
def observed(monkeypatch):
    """Every schedule_memory.observe call: (kind, kwargs)."""
    seen = []
    monkeypatch.setattr(schedule_memory, "observe", lambda rid, kind, **kw: seen.append((kind, kw)))
    return seen


def _rid(**kw):
    kw.setdefault("name", "Learning Co")
    kw.setdefault("owner_email", "l@x.test")
    kw.setdefault("module_labor", 1)
    return create_restaurant(Restaurant(**kw))


def _monday(weeks_ago=0):
    t = date.today()
    return t - timedelta(days=t.weekday()) - timedelta(weeks=weeks_ago)


def _row(d, emp, start="5:00pm", end="10:00pm", role="Server", hours=5, notes=""):
    d = d if isinstance(d, str) else d.isoformat()
    return {"date": d, "day": date.fromisoformat(d).strftime("%A"), "employee": emp, "role": role,
            "shift_start": start, "shift_end": end, "scheduled_hours": str(hours), "notes": notes}


def _csv(rows):
    return HEAD + "".join(",".join(str(r[c]).replace(",", ";") for c in sv.COLS) + "\n" for r in rows)


def _sql(q, *args):
    conn = models.get_conn()
    try:
        cur = conn.execute(q, args)
        conn.commit()
        return cur
    finally:
        conn.close()


def _hist(rid, mon, rows, published=False):
    return _sql("INSERT INTO schedule_history (restaurant_id, week_start, week_end, hours_scheduled, hours_budget, "
                "labor_target, schedule_csv, summary_json, published_at) VALUES (?,?,?,0,100,30,?,'[]',?)",
                rid, mon.isoformat(), (mon + timedelta(days=6)).isoformat(), _csv(rows),
                (mon - timedelta(days=3)).isoformat() + " 12:00:00" if published else None).lastrowid


def _v(rid, hid, reason, rows, by="erik", auth="principal", origins=None):
    sv.append(rid, hid, reason, _csv(rows), saved_by=by, saved_authority=auth, row_origins=origins)


def _age(hid, days):
    _sql("UPDATE schedule_versions SET created_at=datetime('now', ?) WHERE history_id=?", f"-{int(days)} days", hid)


def _crew(mon, tue_people, wed=("Bob",), role="Server"):
    """A week: who works Tuesday dinner, and Bob's Wednesday lunch (so a week
    without Bob on Tuesday is still a week he worked)."""
    tue, wd = mon + timedelta(days=1), mon + timedelta(days=2)
    return [_row(tue, p, role=role) for p in tue_people] + \
        [_row(wd, p, "11:00am", "3:00pm", hours=4) for p in wed]


def _week(rid, mon, draft, final, by="erik", auth="principal", pub_auth=None, published=True, post=None,
          post_auth=None):
    """A week: the generated draft, the manager's edit when the final
    differs, the publish, and an optional save after it (a reaction)."""
    hid = _hist(rid, mon, post or final, published=published)
    _v(rid, hid, "generated", draft, by="Cavnar AI", auth="system")
    if final != draft:
        _v(rid, hid, "edited", final, by=by, auth=auth)
    if published:
        _v(rid, hid, "published", final, by=by, auth=pub_auth or auth)
    if post is not None:
        _v(rid, hid, "edited", post, by=by, auth=post_auth or auth)
    return hid


EVERYONE, WITHOUT_BOB = ["Ana", "Bob", "Cy"], ["Ana", "Cy"]


def _bob_off(rid, **kw):
    return [p for p in sv.learned_patterns(rid, **kw) if p["kind"] == "moved_off" and p["employee"] == "Bob"]


# ══ L-4: a change after the week went out is a reaction ══════════════════

def test_a_replacement_after_the_week_went_out_is_not_learned_as_a_habit():
    rid = _rid()
    for w in (2, 1):
        mon = _monday(w)
        # Published as drafted; Bob calls off and the manager replaces him.
        _week(rid, mon, _crew(mon, EVERYONE), _crew(mon, EVERYONE),
              post=_crew(mon, ["Ana", "Dee", "Cy"]))
    assert not _bob_off(rid), "two call-out replacements taught 'take Bob off Tuesday dinner'"
    assert not sl.edited_weeks(rid)
    # The same change made BEFORE the week went out is the manager's habit.
    rid2 = _rid()
    for w in (2, 1):
        mon = _monday(w)
        _week(rid2, mon, _crew(mon, EVERYONE), _crew(mon, WITHOUT_BOB))
    assert _bob_off(rid2)


def test_a_reaction_neither_reverses_a_standing_pattern_nor_lowers_acceptance():
    rid = _rid()
    for w in (6, 5):
        mon = _monday(w)
        _week(rid, mon, _crew(mon, EVERYONE), _crew(mon, WITHOUT_BOB))
    sv.refresh_standing_patterns(rid)
    for w in (2, 1):
        mon = _monday(w)
        # The draft leaves Bob off; after it went out the manager puts him on
        # Tuesday to cover somebody — twice.
        _week(rid, mon, _crew(mon, WITHOUT_BOB), _crew(mon, WITHOUT_BOB), post=_crew(mon, EVERYONE))
    sv.refresh_standing_patterns(rid)
    row = next(s for s in sv.standing_patterns(rid) if s["kind"] == "moved_off" and s["employee"] == "Bob")
    assert row["status"] == "active" and row["times_overridden"] == 0
    weeks = {w["week_start"]: w for w in sv.acceptance(rid)["weeks"]}
    assert weeks[_monday(1).isoformat()]["unchanged_share"] == 1.0, "the reaction was counted against the draft"


def test_the_predictor_reads_the_week_as_first_sent():
    rid = _rid()
    mon = _monday(1)
    _week(rid, mon, _crew(mon, EVERYONE), _crew(mon, EVERYONE), post=_crew(mon, ["Ana", "Dee", "Cy"]))
    weeks = sl.prediction_weeks(rid)
    assert len(weeks) == 1 and not any(weeks[0]["edited"]), "a post-publish replacement counted as an edit"


def test_the_save_route_observes_a_change_to_a_sent_week_as_a_reaction_with_the_call_out(observed, monkeypatch):
    import labor
    import mobile_api
    from auth import create_session, create_user, upsert_membership
    rid = _rid(timezone="America/Chicago")
    mon = _monday(-1)
    hid = _week(rid, mon, _crew(mon, EVERYONE), _crew(mon, EVERYONE))
    tue = (mon + timedelta(days=1)).isoformat()
    import attendance
    attendance.record(rid, "Bob", tue, "called_out", "manual", shift_start="5:00pm")
    uid = create_user(rid, "erik", "erik@x.test", "pw-h1-test-1")
    upsert_membership(uid, rid, "client")
    monkeypatch.setattr(labor, "analyse_shifts_for_restaurant", lambda *a, **k: {"is_live": True})
    monkeypatch.setattr(labor, "load_shifts_for_restaurant", lambda *a, **k: [])
    app = Flask(__name__)
    app.register_blueprint(mobile_api.mobile_bp)
    rows = _crew(mon, ["Ana", "Dee", "Cy"])
    version = sv.list_versions(rid, hid)[-1]["version"]
    resp = app.test_client().post("/mobile/api/labor/schedule/score",
                                  json={"rows": rows, "save": True, "history_id": hid, "version": version},
                                  headers={"Authorization": "Bearer " + create_session(uid, device_type="ios")})
    assert resp.status_code == 200 and resp.get_json()["saved"], resp.get_json()
    reactions = [kw for kind, kw in observed if kind == "reaction"]
    assert reactions and all(kw["phase"] == "post_publish" and kw["origin"] == "manager" for kw in reactions)
    bob = next(kw for kw in reactions if kw["value"].get("from") == "Bob")
    assert bob["value"]["attendance"]["outcome"] == "called_out"


# ══ L-5: Cavnar AI's kept changes are never the manager's habit ═══════════

@pytest.mark.parametrize("note", ["Cavnar AI: a stronger hand on Tuesday", "(was Bob — marked unavailable that day)",
                                  "Moved to Dee to avoid overtime"])
def test_cavnar_ais_saved_changes_are_not_learned_as_the_managers(note):
    rid = _rid()
    for w in (2, 1):
        mon = _monday(w)
        final = [r if r["employee"] != "Bob" or r["day"] != "Tuesday" else dict(r, employee="Dee", notes=note)
                 for r in _crew(mon, EVERYONE)]
        _week(rid, mon, _crew(mon, EVERYONE), final)
    assert not _bob_off(rid), f"Cavnar AI's change ({note!r}) taught the manager's habit"
    w = sl.learning_weeks(rid)[-1]
    assert w["excluded"]["cavnar"] == 1 and not w["edited"]


def test_a_manager_change_and_a_cavnar_change_on_one_slot_are_never_merged():
    """The manager takes Bob off Tuesday dinner; Improve then swaps Cy for
    Dee on the same slot. The net diff alone pairs Bob with Dee — one
    "move" that is half each — so each kind of change is read on its own."""
    rid = _rid()
    for w in (2, 1):
        mon = _monday(w)
        tue = (mon + timedelta(days=1)).isoformat()
        hid = _hist(rid, mon, _crew(mon, EVERYONE), published=True)
        _v(rid, hid, "generated", _crew(mon, EVERYONE), by="Cavnar AI", auth="system")
        mine = _crew(mon, WITHOUT_BOB)
        _v(rid, hid, "edited", mine)
        fixed = [dict(r, employee="Dee", notes="Cavnar AI: a stronger hand") if r["employee"] == "Cy"
                 and r["date"] == tue else r for r in mine]
        _v(rid, hid, "edited", fixed)
        _v(rid, hid, "published", fixed)
    learned = {(p["kind"], p["employee"]) for p in sv.learned_patterns(rid)}
    assert ("moved_off", "Bob") in learned, "the manager's own change was lost in Cavnar AI's"
    assert not learned & {("moved_off", "Cy"), ("moved_on", "Dee")}, "Cavnar AI's swap was learned as the manager's"
    w = sl.learning_weeks(rid)[-1]
    assert w["excluded"]["cavnar"] == 1 and w["edited"]


def test_a_note_cavnar_ai_wrote_at_generation_is_not_a_new_change():
    """A drafted row already carrying "Cavnar AI:" that the manager retimes
    is the manager's change — only a tag the save ADDED is Cavnar AI's."""
    tue = _monday(-1) + timedelta(days=1)
    before = [_row(tue, "Ana", notes="Cavnar AI: solver put Ana here (was Bob) — NEEDS REVIEW: x")]
    after = [_row(tue, "Ana", start="4:30pm", hours=5.5, notes="Cavnar AI: solver put Ana here (was Bob)")]
    st = sv.step_origins(before, after)
    assert [c["origin"] for c in st["changes"]] == ["manager"]


def test_the_clients_flag_marks_cavnar_rows_and_a_hand_edit_after_clears_it():
    tue = _monday(-1) + timedelta(days=1)
    before = [_row(tue, "Bob"), _row(tue, "Ana", start="4:00pm"), _row(tue, "Cy")]
    fixed = _row(tue, "Dee")
    flagged = dict(fixed, origin="cavnar:apply_fixes", origin_sig=sv.row_sig(fixed))
    # The manager moved Ana's start after Cavnar AI handed her row back.
    stale = dict(_row(tue, "Ana", start="4:30pm"), origin="cavnar:optimize",
                 origin_sig=sv.row_sig(_row(tue, "Ana", start="4:00pm")))
    after = [fixed, _row(tue, "Ana", start="4:30pm")]
    st = sv.step_origins(before, after, flagged_rows=[flagged, stale],
                         cavnar_changes=[{"date": tue.isoformat(), "employee": "Cy", "shift_start": "5:00pm",
                                          "source": "optimize"}])
    by = {(c["kind"], c["source"]) for c in st["changes"]}
    assert ("moved", "apply_fixes") in by
    assert ("removed", "optimize") in by, "a row Cavnar AI took out was read as the manager's removal"
    assert ("retimed", None) in by, "a stale flag kept a hand edit as Cavnar AI's"
    assert set(st["stored"]["cavnar"].values()) == {"apply_fixes", "optimize"}


def test_stamp_cavnar_flags_changed_rows_and_returns_the_removals():
    tue = _monday(-1) + timedelta(days=1)
    before = [_row(tue, "Bob"), _row(tue, "Ana"), _row(tue, "Cy")]
    after = [_row(tue, "Dee"), _row(tue, "Ana")]
    out, gone = sv.stamp_cavnar(before, after, "optimize")
    assert out[0]["origin"] == "cavnar:optimize" and out[0]["origin_sig"] == sv.row_sig(after[0])
    assert "origin" not in out[1]
    assert gone == [{"date": tue.isoformat(), "employee": "Cy", "shift_start": "5:00pm", "source": "optimize"}]


def test_the_save_credits_the_move_kind_and_stores_the_origin(observed, monkeypatch):
    import rec_ledger as rl
    rid = _rid()
    mon = _monday(-1)
    tue = (mon + timedelta(days=1)).isoformat()
    hid = _hist(rid, mon, _crew(mon, EVERYONE))
    _v(rid, hid, "generated", _crew(mon, EVERYONE), by="Cavnar AI", auth="system")
    key = rl.rec_key("overtime_move", f"Bob:{tue}")
    rl.present(rid, key, "schedule", "schedule_review", kind="overtime_move", title="Move Bob's Tuesday")
    rows = [dict(r, employee="Dee", notes="Moved to Dee to avoid overtime") if r["employee"] == "Bob"
            and r["date"] == tue else r for r in _crew(mon, EVERYONE)]
    conn = models.get_conn()
    try:
        conn.execute("BEGIN IMMEDIATE")
        step = sv.step_origins(sv.latest_rows(conn, hid), rows)
        version = sv.write_on(conn, rid, hid, "edited", _csv(rows), saved_by="erik", saved_authority="principal",
                              row_origins=step["stored"])
        conn.commit()
    finally:
        conn.close()
    out = sl.capture_save(rid, hid, version, step, user={"id": 1, "username": "erik", "role": "owner"})
    assert out["cavnar_changes"] == 1 and out["credited"] == 1
    stored = json.loads(_sql("SELECT row_origins_json FROM schedule_versions WHERE history_id=? AND version=?",
                             hid, version).fetchone()[0])
    assert set(stored["cavnar"].values()) == {"overtime_move"}
    assert _sql("SELECT status FROM rec_instances WHERE restaurant_id=? AND key=?", rid, key).fetchone()[0] \
        == "implemented"
    assert [k for k, _kw in observed] == ["cavnar_change_saved"]


def test_apply_fixes_flags_its_rows_and_presents_itself(monkeypatch):
    import schedule_engine
    import shift_quality
    import strategy_routes
    rid = _rid()
    tue = _monday(-1) + timedelta(days=1)
    rows = [_row(tue, "Bob"), _row(tue, "Ana")]
    fixed = [dict(rows[0], employee="Dee", notes="(was Bob — marked unavailable that day)"), rows[1]]
    monkeypatch.setattr(strategy_routes, "_may_draft", lambda u: True)
    c = schedule_rules.Constraints(restaurant_id=rid, week_dates=[tue.isoformat()], week_days=["Tuesday"])
    monkeypatch.setattr(schedule_engine, "quality_inputs_from_db", lambda *a, **k: {"constraints": c})
    monkeypatch.setattr(schedule_engine, "_quality_signals", lambda *a, **k: ({}, {}))
    monkeypatch.setattr(schedule_engine, "_score_schedule_quality", lambda *a, **k: ({"score": 80}, {}))
    monkeypatch.setattr(schedule_engine, "present_quality", lambda *a, **k: None)
    monkeypatch.setattr(shift_quality, "apply_fixes", lambda *a, **k: {
        "rows": fixed, "fixes": [{"index": 0, "from": "Bob", "to": "Dee"}], "unfixed": []})
    u = {"restaurant_id": rid, "id": 1, "username": "erik", "role": "owner"}
    with Flask(__name__).test_request_context("/labor/schedule/apply-fixes", method="POST", json={"rows": rows}):
        body, status = strategy_routes._do_schedule_apply_fixes(u)
    assert status == 200
    assert body["rows"][0]["origin"] == "cavnar:apply_fixes" and "origin" not in body["rows"][1]
    assert _sql("SELECT COUNT(*) FROM rec_instances WHERE restaurant_id=? AND kind='apply_fixes' AND status='open'",
                rid).fetchone()[0] == 1


# ══ L-6: every pattern has a denominator ═════════════════════════════════

def test_two_edits_in_eight_weeks_are_not_a_habit_two_of_three_are():
    rid = _rid()
    for w in range(8, 0, -1):
        mon = _monday(w)
        _week(rid, mon, _crew(mon, EVERYONE), _crew(mon, WITHOUT_BOB if w in (7, 3) else EVERYONE))
    assert not _bob_off(rid), "two of eight weeks Bob was drafted read as a habit"
    rid2 = _rid()
    for w in (3, 2, 1):
        mon = _monday(w)
        _week(rid2, mon, _crew(mon, EVERYONE), _crew(mon, WITHOUT_BOB if w != 2 else EVERYONE))
    p = _bob_off(rid2)[0]
    assert (p["times"], p["opportunities"]) == (2, 3) and 0.5 < p["rate"] < 0.8 and 0 < p["confidence"] < p["rate"]
    assert "in 2 of 3 recent weeks" in p["text"]


def test_a_headcount_added_on_two_of_six_fridays_is_an_occasion_not_a_habit():
    rid = _rid()
    for w in range(6, 0, -1):
        mon = _monday(w)
        fri = mon + timedelta(days=4)
        draft = [_row(fri, "Ana"), _row(fri, "Bob")]
        final = draft + [_row(fri, "Dee")] if w in (5, 2) else draft
        _week(rid, mon, draft, final)
    assert not [p for p in sv.learned_patterns(rid) if p["kind"] == "headcount_add"]
    assert sl.learned_headcount_adjustments(rid) == {}


def test_a_standing_row_carries_its_evidence_and_a_confidence():
    rid = _rid()
    for w in (3, 2):
        mon = _monday(w)
        _week(rid, mon, _crew(mon, EVERYONE), _crew(mon, WITHOUT_BOB))
    sv.refresh_standing_patterns(rid)
    mon = _monday(1)
    _week(rid, mon, _crew(mon, WITHOUT_BOB), _crew(mon, WITHOUT_BOB))      # kept: an unedited week is a keep
    sv.refresh_standing_patterns(rid)
    row = next(s for s in sv.standing_patterns(rid) if s["kind"] == "moved_off")
    assert row["opportunities"] == 3 and row["hits"] == 3 and 0 < row["confidence"] < 1
    assert row["last_hand_iso"] == (_monday(2) + timedelta(days=0)).isoformat()


# ══ L-7: the unattended automatic publish is nobody's acceptance ══════════

def test_the_automation_actor_is_system_and_a_queued_person_is_themself():
    import delayed
    assert sv.authority_of(delayed.AUTOMATION_ACTOR) == "system"
    assert sv.authority_of(delayed.queued_actor({"schedule_id": 3, "automatic": True})) == "system"
    assert sv.authority_of(delayed.queued_actor({"schedule_id": 3})) == "system"
    me = delayed.queued_actor({"schedule_id": 3, "manual": True, "acknowledge": [], "authority": "principal"})
    assert sv.authority_of(me) == "principal" and me["role"] == "automation"
    old = delayed.queued_actor({"schedule_id": 3, "manual": True, "acknowledge": []})
    assert sv.authority_of(old) == "delegate"


def test_a_queued_send_runs_with_the_senders_authority_and_auto_publish_as_system(monkeypatch):
    import client_api
    import delayed
    rid = _rid()
    models.update_restaurant(rid, {"send_delay_minutes": 30})
    mon = _monday(-1)
    hid = _week(rid, mon, _crew(mon, EVERYONE), _crew(mon, EVERYONE), published=False)
    monkeypatch.setattr(client_api, "publish_review", lambda *a, **k: {"blockers": []})
    owner = {"id": 1, "username": "erik", "role": "owner", "restaurant_id": rid}
    with Flask(__name__).test_request_context("/api/labor/publish-schedule", method="POST",
                                              json={"schedule_id": hid}):
        resp = client_api._publish_schedule_request(owner)
    assert resp.get_json()["queued"]
    payload = json.loads(_sql("SELECT payload_json FROM delayed_actions WHERE restaurant_id=?", rid).fetchone()[0])
    assert payload["manual"] and payload["authority"] == "principal" and payload["queued_by"] == "erik"
    ran = []
    monkeypatch.setattr(client_api, "_publish_schedule",
                        lambda r, sid, actor, acknowledge=None: ran.append(sv.authority_of(actor)) or ({"ok": True}, 200))
    delayed._run_schedule_publish(rid, payload, None)
    delayed._run_schedule_publish(rid, {"schedule_id": hid, "automatic": True}, None)
    assert ran == ["principal", "system"]


def test_the_first_publish_observes_every_change_by_origin(observed):
    rid = _rid()
    mon = _monday(-1)
    tue = (mon + timedelta(days=1)).isoformat()
    hid = _hist(rid, mon, _crew(mon, EVERYONE), published=True)
    _v(rid, hid, "generated", _crew(mon, EVERYONE), by="Cavnar AI", auth="system")
    mine = _crew(mon, WITHOUT_BOB)                                   # the manager takes Bob off
    _v(rid, hid, "edited", mine)
    fixed = [dict(r, employee="Dee", notes="Cavnar AI: a stronger hand") if r["employee"] == "Cy"
             and r["date"] == tue else r for r in mine]                # Improve swaps Cy for Dee
    _v(rid, hid, "edited", fixed)
    _v(rid, hid, "published", fixed)
    assert sv.observe_publish(rid, hid, authority="principal", editor="erik") >= 3
    kinds = {(k, kw["origin"]) for k, kw in observed}
    assert ("edit_move", "manager") in kinds and ("cavnar_change_saved", "cavnar") in kinds
    assert ("edit_headcount", "manager") in kinds and ("week_published", "manager") in kinds
    assert all(kw["phase"] == "pre_publish" for _k, kw in observed)
    import inspect
    import client_api
    assert "observe_publish(rid, schedule_id" in inspect.getsource(client_api._publish_schedule)


def test_a_week_the_automatic_publish_sent_is_nobodys_acceptance_or_evidence():
    rid = _rid()
    for w in (3, 2, 1):
        mon = _monday(w)
        _week(rid, mon, _crew(mon, EVERYONE), _crew(mon, EVERYONE), pub_auth="system")
    acc = sv.acceptance(rid)
    assert acc["available"] and all(w["looked_at"] is False and w["unchanged_share"] is None for w in acc["weeks"])
    assert acc["trend"] is None and acc["mean_unchanged_share"] is None
    assert sl.prediction_weeks(rid) == [], "a week nobody looked at counted as every row kept"
    assert sl.learning_weeks(rid) == []


def test_an_old_automatic_publish_is_read_from_its_delayed_action():
    rid = _rid()
    mon = _monday(1)
    hid = _week(rid, mon, _crew(mon, EVERYONE), _crew(mon, EVERYONE), by="Cavnar AI", pub_auth="delegate")
    _sql("INSERT INTO delayed_actions (restaurant_id, kind, payload_json, execute_at, status) VALUES "
         "(?, 'schedule_publish', ?, datetime('now'), 'done')", rid, json.dumps({"schedule_id": hid, "automatic": True}))
    assert sv.acceptance(rid)["weeks"][0]["looked_at"] is False


def test_an_unattended_week_neither_keeps_nor_reverses_a_standing_pattern():
    rid = _rid()
    for w in (4, 3):
        mon = _monday(w)
        _week(rid, mon, _crew(mon, EVERYONE), _crew(mon, WITHOUT_BOB))
    sv.refresh_standing_patterns(rid)
    before = next(s for s in sv.standing_patterns(rid) if s["kind"] == "moved_off")["times_applied"]
    for w in (2, 1):
        mon = _monday(w)
        _week(rid, mon, _crew(mon, WITHOUT_BOB), _crew(mon, WITHOUT_BOB), pub_auth="system")
    sv.refresh_standing_patterns(rid)
    row = next(s for s in sv.standing_patterns(rid) if s["kind"] == "moved_off")
    assert row["times_applied"] == before and row["hits"] == 2


def test_the_experiment_readout_reads_acceptance_from_looked_at_weeks_only():
    """Three published weeks on one arm, one of them sent by the automatic
    publish: its acceptance is left out of the A/B readout, its outcome and
    quality are not."""
    import schedule_experiments as sx
    rid = _rid(name="R0 Grill")
    exp = sx.EXPERIMENTS[0]
    for k, auth in enumerate(("principal", "principal", "system")):
        mon = date(2026, 1, 5) + timedelta(weeks=k)
        rows = [_row(mon, f"P{i}") for i in range(10)]
        hid = _week(rid, mon, rows, rows, pub_auth=auth)
        _sql("INSERT INTO schedule_outcomes (restaurant_id, history_id, date, daypart, hours, issues, labor_pct) "
             "VALUES (?,?,?,?,?,?,?)", rid, hid, mon.isoformat(), "night", 50, 0, 30.0)
        sx.record(rid, hid, [{"experiment": exp["key"], "arm": "model", "pinned": False}], 80)
    arm = next(a for a in sx.readout()["experiments"][0]["arms"] if a["arm"] == "model")
    assert arm["acceptance"]["n"] == 2, "a week nobody looked at counted as accepted"
    assert arm["issues"]["n"] == 3 and arm["quality"]["n"] == 3 and arm["restaurants"] == 1


# ══ L-8: an admin's change is taken out of the week, not the week out ═════

def _co_edited_week(rid, mon):
    """Erik takes Bob off Tuesday dinner; Will, through view-as, takes Ana
    off Wednesday lunch in the same week."""
    draft = _crew(mon, EVERYONE, wed=("Bob", "Ana"))
    erik = _crew(mon, WITHOUT_BOB, wed=("Bob", "Ana"))
    will = _crew(mon, WITHOUT_BOB, wed=("Bob",))
    hid = _hist(rid, mon, will, published=True)
    _v(rid, hid, "generated", draft, by="Cavnar AI", auth="system")
    _v(rid, hid, "edited", erik, by="erik", auth="principal")
    _v(rid, hid, "edited", will, by="support:will (as erik)", auth="admin")
    _v(rid, hid, "published", will, by="erik", auth="principal")
    return hid


def test_the_owners_edits_count_in_a_week_an_admin_also_edited():
    rid = _rid()
    for w in (2, 1):
        _co_edited_week(rid, _monday(w))
    learned = {(p["kind"], p["employee"]) for p in sv.learned_patterns(rid)}
    assert ("moved_off", "Bob") in learned, "one view-as save dropped the owner's own edits"
    assert ("moved_off", "Ana") not in learned, "an admin's change was learned as the owner's"
    assert sv.admin_saves_pending(rid) == {"versions": 2, "weeks": 2, "answers": 0}


def test_the_account_holder_can_adopt_an_admins_saves():
    rid = _rid()
    for w in (2, 1):
        _co_edited_week(rid, _monday(w))
    from models import CapabilityError
    with pytest.raises(CapabilityError):
        sv.adopt_admin_saves(rid, dict(VIEW_AS))
    with pytest.raises(CapabilityError):
        sv.adopt_admin_saves(rid, {"id": 4, "username": "mgr", "role": "manager"})
    out = sv.adopt_admin_saves(rid, {"id": 1, "username": "erik", "role": "owner"})
    assert out == {"versions": 2, "answers": 0}
    assert ("moved_off", "Ana") in {(p["kind"], p["employee"]) for p in sv.learned_patterns(rid)}
    assert sv.admin_saves_pending(rid)["versions"] == 0


def test_the_adopt_routes_exist_for_web_and_phone():
    import strategy_routes
    app = Flask(__name__)
    app.register_blueprint(strategy_routes.strategy_bp)
    app.register_blueprint(strategy_routes.strategy_mobile_bp)
    rules = {r.rule for r in app.url_map.iter_rules()}
    for path in ("/labor/schedule/adopt-admin-saves", "/labor/learned-patterns/adopt", "/labor/schedule/edit-why"):
        assert "/api" + path in rules and "/mobile/api" + path in rules


# ══ L-10: an admin's dismissal waits for the owner ═══════════════════════

def test_an_admins_dismissal_counts_only_once_adopted():
    rid = _rid()
    key = "moved_off|bob|Tuesday|night"
    schedule_intel.dismiss_pattern(rid, key, actor="erik", authority="admin")
    assert schedule_intel.dismissed_patterns(rid) == set()
    assert schedule_intel.admin_dismissed_patterns(rid) == {key: "erik"}
    from models import CapabilityError
    with pytest.raises(CapabilityError):
        schedule_intel.adopt_admin_dismissals(rid, dict(VIEW_AS))
    assert schedule_intel.adopt_admin_dismissals(rid, {"id": 1, "username": "erik", "role": "owner"}) == 1
    assert schedule_intel.dismissed_patterns(rid) == {key}


def test_the_owner_dismissing_what_support_dismissed_counts_at_once():
    rid = _rid()
    key = "moved_on|cy|Friday|night"
    schedule_intel.dismiss_pattern(rid, key, actor="erik", authority="admin")
    schedule_intel.dismiss_pattern(rid, key, actor="erik", authority="principal")
    assert schedule_intel.dismissed_patterns(rid) == {key}
    # ...and support dismissing what the owner already dismissed changes nothing.
    schedule_intel.dismiss_pattern(rid, key, actor="will", authority="admin")
    assert schedule_intel.dismissed_patterns(rid) == {key}


def test_the_dismiss_route_stores_the_logins_authority(monkeypatch):
    import strategy_routes
    rid = _rid()
    monkeypatch.setattr(strategy_routes, "_may_draft", lambda u: True)
    u = dict(VIEW_AS, restaurant_id=rid)
    with Flask(__name__).test_request_context("/labor/learned-patterns", method="POST",
                                              json={"key": "moved_off|bob|Tuesday|night"}):
        body, status = strategy_routes._do_learned_pattern_set(u)
    assert status == 200 and body["counted"] is False
    assert _sql("SELECT authority FROM schedule_pattern_dismissals WHERE restaurant_id=?", rid).fetchone()[0] == "admin"
    assert schedule_intel.dismissed_patterns(rid) == set()


# ══ L-26: redos and discards are kept; a redo hides no edit ═══════════════

def _redo_chain(rid, mon, owner_redo=True):
    """Draft A; Erik takes Bob off Monday dinner; then redoes Friday (B keeps
    A's Monday as edited); then edits B's Friday."""
    monday, fri = mon, mon + timedelta(days=4)
    a_draft = [_row(monday, "Ana"), _row(monday, "Bob"), _row(fri, "Cy"), _row(fri, "Dee")]
    a_edit = [_row(monday, "Ana"), _row(fri, "Cy"), _row(fri, "Dee")]
    a = _hist(rid, mon, a_edit)
    _v(rid, a, "generated", a_draft, by="Cavnar AI", auth="system")
    _v(rid, a, "edited", a_edit)
    if owner_redo:
        sv.record_rejection(rid, a, "redo_days", dates=[fri.isoformat()], reason_chip="too_thin",
                            reason_text="Friday needs a closer", user={"id": 1, "username": "erik", "role": "owner"})
    b_draft = [_row(monday, "Ana"), _row(fri, "Eve"), _row(fri, "Fay")]
    b_final = [_row(monday, "Ana"), _row(fri, "Eve"), _row(fri, "Gus")]
    b = _hist(rid, mon, b_final)
    _v(rid, b, "generated", b_draft, by="Cavnar AI", auth="system")
    _v(rid, b, "edited", b_final)
    _sql("UPDATE schedule_history SET superseded_by=? WHERE id=?", b, a)
    return a, b


def test_a_redo_of_some_days_keeps_the_edits_made_before_it(observed):
    rid = _rid()
    a, b = _redo_chain(rid, _monday(-1))
    w = sl.edited_weeks(rid)[0]
    assert w["history_id"] == b and w["chain"] == [a, b]
    removed = {(r["employee"], r["day"]) for r in w["diff"]["removed"]}
    moved = {(m["from"], m["to"]) for m in w["diff"]["moved"]}
    assert ("Bob", "Monday") in removed, "the Monday edit made before the redo vanished"
    assert ("Fay", "Gus") in moved
    assert [k for k, _kw in observed] == ["redo_days"]
    assert observed[0][1]["value"]["reason"] == "too_thin" and observed[0][1]["value"]["text"]
    row = _sql("SELECT kind, reason_chip, reason_text, authority FROM schedule_rejections WHERE history_id=?",
               a).fetchone()
    assert tuple(row) == ("redo_days", "too_thin", "Friday needs a closer", "principal")


def test_the_rows_a_redo_threw_away_are_rows_the_manager_changed():
    rid = _rid()
    a, b = _redo_chain(rid, _monday(-1))
    week = sl.prediction_weeks(rid)[0]
    thrown = [(r["employee"], hit) for r, hit in zip(week["rows"], week["edited"]) if r["day"] == "Friday"
              and r["employee"] in ("Cy", "Dee")]
    assert thrown == [("Cy", True), ("Dee", True)]


def test_a_rewrite_with_no_owner_redo_is_not_read_as_one():
    rid = _rid()
    a, b = _redo_chain(rid, _monday(-1), owner_redo=False)
    w = sl.learning_weeks(rid)[0]
    assert w["chain"] == [b] and w["rejected"] == []


def test_the_generate_route_keeps_a_redo_and_a_discarded_draft(observed, monkeypatch):
    import mobile_api
    import ops
    import schedule_engine
    from auth import create_session, create_user, upsert_membership
    rid = _rid(timezone="America/Chicago")
    uid = create_user(rid, "erik", "erik@x.test", "pw-h1-test-2")
    upsert_membership(uid, rid, "client")
    monkeypatch.setattr(schedule_engine, "_run_schedule_job", lambda *a, **k: None)
    monkeypatch.setattr(ops, "active_job", lambda *a, **k: None)
    app = Flask(__name__)
    app.register_blueprint(mobile_api.mobile_bp)
    head = {"Authorization": "Bearer " + create_session(uid, device_type="ios")}
    mon = _monday(-1)
    draft = _hist(rid, mon, [_row(mon, "Ana")])
    fri = (mon + timedelta(days=4)).isoformat()
    r = app.test_client().post("/mobile/api/labor/generate-schedule", headers=head,
                               json={"week_start": mon.isoformat(), "dates": [fri], "history_id": draft,
                                     "reason_chip": "too_thin", "reason_text": "nobody to close"})
    assert r.status_code == 200, r.get_json()
    _sql("DELETE FROM async_jobs")
    r = app.test_client().post("/mobile/api/labor/generate-schedule", headers=head,
                               json={"week_start": mon.isoformat()})
    assert r.status_code == 200, r.get_json()
    rows = [tuple(x) for x in _sql("SELECT kind, history_id, dates_json, reason_chip, reason_text FROM "
                                   "schedule_rejections WHERE restaurant_id=? ORDER BY id", rid).fetchall()]
    assert rows == [("redo_days", draft, json.dumps([fri]), "too_thin", "nobody to close"),
                    ("draft_discarded", draft, None, None, None)]
    assert [k for k, _kw in observed] == ["redo_days", "draft_discarded"]


# ══ L-27: evidence fades by half-life ═════════════════════════════════════

def test_older_evidence_weighs_less_than_newer():
    # Bob taken off in two weeks five months ago, kept on in the last two:
    # two of four, but the recent weeks outweigh the old.
    rid = _rid()
    for w, off in ((22, True), (21, True), (2, False), (1, False)):
        mon = _monday(w)
        _week(rid, mon, _crew(mon, EVERYONE), _crew(mon, WITHOUT_BOB if off else EVERYONE))
    assert not _bob_off(rid)
    assert _bob_off(rid, min_rate=0)[0]["rate"] < 0.5
    # The same counts the other way round: the recent edits carry it.
    rid2 = _rid()
    for w, off in ((22, False), (21, False), (2, True), (1, True)):
        mon = _monday(w)
        _week(rid2, mon, _crew(mon, EVERYONE), _crew(mon, WITHOUT_BOB if off else EVERYONE))
    p = _bob_off(rid2)[0]
    assert p["rate"] > 0.5 and p["opportunities"] == 4


def test_the_learners_look_past_eight_weeks():
    rid = _rid()
    for w in (13, 12):
        mon = _monday(w)
        hid = _week(rid, mon, _crew(mon, EVERYONE), _crew(mon, WITHOUT_BOB))
        _age(hid, 7 * w)
    assert _bob_off(rid), "an edit twelve weeks old fell off a flat eight-week window"
    assert sl.EDIT_WEEKS == sv.LEARN_WEEKS == 24 and sl.PREDICT_WEEKS == sv.LEARN_WEEKS


def test_the_predictor_weights_a_week_by_its_age():
    rows = [_row(_monday(-1) + timedelta(days=k % 7), n) for k in range(10) for n in ("Ana", "Bob")]
    old = {"history_id": 1, "week_start": "a", "rows": rows, "edited": [True] * len(rows), "weight": 0.25}
    new = {"history_id": 2, "week_start": "b", "rows": rows, "edited": [False] * len(rows), "weight": 1.0}
    wk = [old, new, dict(new, history_id=3)]
    m = sl.fit_edit_model(wk)
    ana = m["tables"]["person"]["ana"]
    assert ana[2:] == [10, 30] and abs(ana[0] - 2.5) < 1e-9 and abs(ana[1] - 22.5) < 1e-9
    assert m["base_rate"] < 0.2, "a quarter-weight week counted in full"


# ══ L-30: a standing pattern decays and is re-tested ══════════════════════

def _standing_bob(rid, weeks_ago=(20, 19)):
    for w in weeks_ago:
        mon = _monday(w)
        hid = _week(rid, mon, _crew(mon, EVERYONE), _crew(mon, WITHOUT_BOB))
        _age(hid, 7 * w)
        # Learned while the teaching weeks were in the window.
    sv.refresh_standing_patterns(rid)


def test_a_pattern_unconfirmed_for_a_half_life_is_re_tested_and_left_out_once():
    rid = _rid()
    _standing_bob(rid)
    row = next(s for s in sv.standing_patterns(rid) if s["kind"] == "moved_off")
    assert row["status"] == "retest" and row["retest_since"], row
    drafted, _c = sv.patterns_for_draft(rid)
    assert not [p for p in drafted if p.get("employee") == "Bob"], "a re-tested pattern was still drafted"


def _retest_week(rid, final_people):
    mon = _monday(0)
    _sql("UPDATE schedule_standing_patterns SET retest_since=datetime('now', '-1 hour') WHERE restaurant_id=?", rid)
    return _week(rid, mon, _crew(mon, EVERYONE), _crew(mon, final_people))


def test_the_manager_putting_it_back_by_hand_confirms_it():
    rid = _rid()
    _standing_bob(rid)
    _retest_week(rid, WITHOUT_BOB)
    sv.refresh_standing_patterns(rid)
    row = next(s for s in sv.standing_patterns(rid) if s["kind"] == "moved_off")
    assert row["status"] == "active" and row["last_hand_iso"] == _monday(0).isoformat() and row["retest_since"] is None


def test_a_week_sent_without_it_retires_it():
    rid = _rid()
    _standing_bob(rid)
    _retest_week(rid, EVERYONE)
    sv.refresh_standing_patterns(rid)
    row = next(s for s in sv.standing_patterns(rid) if s["kind"] == "moved_off")
    assert row["status"] == "retired" and row["retired_reason"] == "retest"
    drafted, _c = sv.patterns_for_draft(rid)
    assert not [p for p in drafted if p.get("employee") == "Bob"], "the live copy brought it back"


def test_two_half_lives_without_a_hand_retire_it():
    rid = _rid()
    _standing_bob(rid, weeks_ago=(3, 2))
    _sql("UPDATE schedule_standing_patterns SET last_hand=?, status='active', retest_since=NULL, "
         "last_retest_end=datetime('now', '-1 day') WHERE restaurant_id=?",
         (date.today() - timedelta(days=250)).isoformat(), rid)
    stats = sv.refresh_standing_patterns(rid)
    row = next(s for s in sv.standing_patterns(rid) if s["kind"] == "moved_off")
    assert row["status"] == "retired" and row["retired_reason"] == "decayed" and stats["decayed"] == 1


def test_the_owner_can_keep_it_or_let_it_go():
    rid = _rid()
    _standing_bob(rid)
    key = next(s for s in sv.standing_patterns(rid) if s["kind"] == "moved_off")["key"]
    with pytest.raises(ValueError):
        sv.confirm_standing(rid, key, user=dict(VIEW_AS))
    sv.confirm_standing(rid, key, user={"id": 1, "username": "erik", "role": "owner"})
    row = next(s for s in sv.standing_patterns(rid) if s["key"] == key)
    assert row["status"] == "active" and row["last_hand_iso"] == date.today().isoformat()
    sv.confirm_standing(rid, key, user={"id": 1, "username": "erik", "role": "owner"}, keep=False)
    row = next(s for s in sv.standing_patterns(rid) if s["key"] == key)
    assert row["status"] == "retired" and row["retired_reason"] == "owner"


def test_a_re_tested_headcount_is_left_out_of_the_requirements():
    rid = _rid()
    for w in (3, 2):
        mon = _monday(w)
        fri = mon + timedelta(days=4)
        _week(rid, mon, [_row(fri, "Ana"), _row(fri, "Bob")], [_row(fri, "Ana"), _row(fri, "Bob"), _row(fri, "Dee")])
    sv.refresh_standing_patterns(rid)
    row = next(s for s in sv.standing_patterns(rid) if s["kind"] == "headcount_add")
    assert row["status"] == "active" and sl.learned_headcount_adjustments(rid)
    # A headcount's half-life is 180 days: 150 days on it still stands...
    _sql("UPDATE schedule_standing_patterns SET last_hand=? WHERE restaurant_id=?",
         (date.today() - timedelta(days=150)).isoformat(), rid)
    sv.refresh_standing_patterns(rid)
    assert next(s for s in sv.standing_patterns(rid) if s["kind"] == "headcount_add")["status"] == "active"
    # ...past it, the next draft leaves it out once, live copy included.
    _sql("UPDATE schedule_standing_patterns SET last_hand=? WHERE restaurant_id=?",
         (date.today() - timedelta(days=185)).isoformat(), rid)
    sv.refresh_standing_patterns(rid)
    assert next(s for s in sv.standing_patterns(rid) if s["kind"] == "headcount_add")["status"] == "retest"
    assert sl.learned_headcount_adjustments(rid) == {}


# ══ L-33: "make it a rule" for a headcount and a start time ═══════════════

ROLES = ["Server", "Host"]


def _rules_roles(monkeypatch):
    monkeypatch.setattr(snr, "restaurant_roles", lambda rid, db_path=None: list(ROLES))
    monkeypatch.setattr(snr, "roster_roles", lambda rid, db_path=None: {r.lower() for r in ROLES})


def _constraints(rid, mon):
    dates = [(mon + timedelta(days=i)).isoformat() for i in range(7)]
    return schedule_rules.Constraints(restaurant_id=rid, week_dates=dates, week_days=list(snr.DAYS))


def test_a_headcount_habit_becomes_a_role_floor(monkeypatch):
    _rules_roles(monkeypatch)
    rid = _rid()
    for w in (3, 2, 1):
        mon = _monday(w)
        fri = mon + timedelta(days=4)
        three = [_row(fri, n) for n in ("Ana", "Bob", "Cy")]
        _week(rid, mon, three, three + [_row(fri, "Dee")])
    sv.refresh_standing_patterns(rid)
    key = next(s["key"] for s in sv.standing_patterns(rid) if s["kind"] == "headcount_add")
    out = sv.make_rule(rid, key, user={"id": 1, "username": "erik", "role": "owner"})
    assert out["kind"] == "headcount_add" and "at least 4 Server at dinner, Fri" in out["rule"]
    c = _constraints(rid, _monday(-1))
    snr.apply_note_rules(c, rid)
    assert schedule_rules.floor_for(c.role_floors, "Server", "Friday", "night") == 4
    assert schedule_rules.floor_for(c.role_floors, "Server", "Thursday", "night") == 0
    assert next(s for s in sv.standing_patterns(rid) if s["key"] == key)["status"] == "ruled"


def test_a_start_the_manager_keeps_setting_becomes_a_role_start_rule(monkeypatch):
    _rules_roles(monkeypatch)
    rid = _rid()
    for w in (2, 1):
        mon = _monday(w)
        fri = mon + timedelta(days=4)
        _week(rid, mon, [_row(fri, "Ana"), _row(fri, "Bob")],
              [_row(fri, "Ana", start="4:30pm", hours=5.5), _row(fri, "Bob", start="4:30pm", hours=5.5)])
    sv.refresh_standing_patterns(rid)
    key = next(s["key"] for s in sv.standing_patterns(rid) if s["kind"] == "retime_start")
    out = sv.make_rule(rid, key, user={"id": 1, "username": "erik", "role": "owner"})
    assert "Server at dinner start at 4:30pm, Fri" in out["rule"]
    mon = _monday(-1)
    c = _constraints(rid, mon)
    snr.apply_note_rules(c, rid)
    assert c.role_times[("server", "Friday", "night")]["start"] == 16 * 60 + 30
    fri = (mon + timedelta(days=4)).isoformat()
    rows = [_row(fri, "Ana", start="4:00pm", hours=6), _row(fri, "Bob", start="4:30pm", hours=5.5)]
    viols = [v for v in schedule_rules.violations(rows, c) if v["kind"] == "role_time"]
    assert len(viols) == 1 and viols[0]["employee"] == "Ana" and not viols[0]["hard"]
    assert "4:30pm" in viols[0]["detail"]
    fixed = schedule_rules.apply_role_times(rows, c)
    assert [r["shift_start"] for r in fixed["rows"]] == ["4:30pm", "4:30pm"]
    assert fixed["rows"][0]["scheduled_hours"] == "5.5" and len(fixed["retimed"]) == 1
    assert "STARTS AND ENDS BY ROLE" in schedule_rules.prompt_block(c) and "start at 4:30pm" in \
        schedule_rules.prompt_block(c)


def test_a_retime_rule_never_breaks_a_persons_window(monkeypatch):
    _rules_roles(monkeypatch)
    rid = _rid()
    mon = _monday(-1)
    snr.add_time_rule(rid, "Server", "start", "4:00pm", "night", days=["Friday"])
    c = _constraints(rid, mon)
    snr.apply_note_rules(c, rid)
    fri = (mon + timedelta(days=4)).isoformat()
    c.time_windows = {"ana": {"Friday": (16 * 60 + 30, None)}}
    out = schedule_rules.apply_role_times([_row(fri, "Ana", start="4:30pm", hours=5.5)], c)
    assert out["rows"][0]["shift_start"] == "4:30pm" and out["left"] and not out["retimed"]


def test_a_cut_is_refused_in_words_and_a_floor_is_the_owners(monkeypatch):
    import strategy_routes
    _rules_roles(monkeypatch)
    rid = _rid()
    for w in (2, 1):
        mon = _monday(w)
        fri = mon + timedelta(days=4)
        three = [_row(fri, n) for n in ("Ana", "Bob", "Cy")]
        _week(rid, mon, three, three[:2])
    sv.refresh_standing_patterns(rid)
    key = next(s["key"] for s in sv.standing_patterns(rid) if s["kind"] == "headcount_cut")
    with pytest.raises(ValueError, match="minimums, not maximums"):
        sv.make_rule(rid, key)
    monkeypatch.setattr(strategy_routes, "_may_draft", lambda u: True)
    monkeypatch.setattr(strategy_routes, "_principal", lambda u: False)
    with Flask(__name__).test_request_context("/labor/learned-patterns", method="POST",
                                              json={"key": key, "rule": True}):
        assert strategy_routes._do_learned_pattern_set({"restaurant_id": rid, "id": 4})[1] == 403


# ══ L-35: the first weeks ask a one-tap why ═══════════════════════════════

def _save(rid, hid, rows, user=None, published=False):
    conn = models.get_conn()
    try:
        conn.execute("BEGIN IMMEDIATE")
        step = sv.step_origins(sv.latest_rows(conn, hid), rows)
        version = sv.write_on(conn, rid, hid, "edited", _csv(rows), saved_by="erik",
                              saved_authority=sv.authority_of(user or {"id": 1, "role": "owner"}),
                              row_origins=step["stored"])
        conn.commit()
    finally:
        conn.close()
    return sl.capture_save(rid, hid, version, step, user=user or {"id": 1, "username": "erik", "role": "owner"},
                           published=published)


def test_a_big_edit_in_the_first_weeks_asks_why(observed):
    rid = _rid()
    mon = _monday(-1)
    hid = _hist(rid, mon, _crew(mon, EVERYONE))
    _v(rid, hid, "generated", _crew(mon, EVERYONE), by="Cavnar AI", auth="system")
    out = _save(rid, hid, _crew(mon, WITHOUT_BOB))
    q = out["why_questions"]
    assert len(q) == 1 and q[0]["kind"] == "moved_off" and q[0]["employee"] == "Bob"
    assert [o["answer"] for o in q[0]["options"]] == ["always", "this_week"], "a call-off before the week went out"
    assert "Bob off Tue" in q[0]["text"] and "/" in q[0]["text"]
    # Asked once per change: the same week saved again does not ask again.
    assert _save(rid, hid, _crew(mon, WITHOUT_BOB) + [_row(mon, "Zed")])["why_questions"][0]["kind"] == "headcount_add"


def test_no_why_after_the_first_four_weeks():
    rid = _rid()
    for w in (5, 4, 3, 2):
        mon = _monday(w)
        _week(rid, mon, _crew(mon, EVERYONE), _crew(mon, EVERYONE))
    mon = _monday(-1)
    hid = _hist(rid, mon, _crew(mon, EVERYONE))
    _v(rid, hid, "generated", _crew(mon, EVERYONE), by="Cavnar AI", auth="system")
    assert _save(rid, hid, _crew(mon, WITHOUT_BOB))["why_questions"] == []


def test_always_makes_a_standing_pattern_now_with_the_owners_authority(observed):
    rid = _rid()
    mon = _monday(-1)
    hid = _hist(rid, mon, _crew(mon, EVERYONE))
    _v(rid, hid, "generated", _crew(mon, EVERYONE), by="Cavnar AI", auth="system")
    key = _save(rid, hid, _crew(mon, WITHOUT_BOB))["why_questions"][0]["key"]
    out = sl.answer_edit_question(rid, hid, key, "always", user={"id": 1, "username": "erik", "role": "owner"})
    assert out["counted"] and out["applied"]["pattern"] == "moved_off|bob|Tuesday|night"
    row = next(s for s in sv.standing_patterns(rid) if s["key"] == "moved_off|bob|Tuesday|night")
    assert (row["status"], row["source"]) == ("active", "owner_said")
    assert _sql("SELECT authority FROM schedule_standing_patterns WHERE restaurant_id=?", rid).fetchone()[0] == \
        "principal"
    drafted, _c = sv.patterns_for_draft(rid)
    assert [p for p in drafted if p.get("employee") == "Bob" and p["kind"] == "moved_off"]
    why = [kw for k, kw in observed if k == "edit_why"]
    assert why and why[0]["value"]["answer"] == "always" and why[0]["authority"] == "principal"
    with pytest.raises(ValueError):
        sl.answer_edit_question(rid, hid, key, "this_week", user={"id": 1, "role": "owner"})


def test_just_this_week_keeps_the_change_out_of_the_habits():
    rid = _rid()
    for w in (2, 1):
        mon = _monday(w)
        hid = _hist(rid, mon, _crew(mon, EVERYONE), published=True)
        _v(rid, hid, "generated", _crew(mon, EVERYONE), by="Cavnar AI", auth="system")
        q = _save(rid, hid, _crew(mon, WITHOUT_BOB))["why_questions"]
        if w == 1:
            sl.answer_edit_question(rid, hid, q[0]["key"], "this_week", user={"id": 1, "role": "owner"})
        _v(rid, hid, "published", _crew(mon, WITHOUT_BOB))
    assert not _bob_off(rid), "a one-off the owner named was learned as a habit"
    assert sum(1 for w in sl.learning_weeks(rid) if w["excluded"]["answered"]) == 1


def test_a_call_off_goes_to_attendance():
    rid = _rid()
    mon = _monday(-1)
    hid = _week(rid, mon, _crew(mon, EVERYONE), _crew(mon, EVERYONE))
    q = _save(rid, hid, _crew(mon, ["Ana", "Dee", "Cy"]), published=True)["why_questions"]
    assert [o["answer"] for o in q[0]["options"]] == ["always", "this_week", "call_off"]
    sl.answer_edit_question(rid, hid, q[0]["key"], "call_off", user={"id": 1, "role": "owner"})
    row = _sql("SELECT outcome, source, covered_by FROM attendance_events WHERE restaurant_id=?", rid).fetchone()
    assert tuple(row) == ("called_out", "manual", "Dee")


def test_an_admins_answer_waits_for_the_owner():
    rid = _rid()
    mon = _monday(-1)
    hid = _hist(rid, mon, _crew(mon, EVERYONE))
    _v(rid, hid, "generated", _crew(mon, EVERYONE), by="Cavnar AI", auth="system")
    key = _save(rid, hid, _crew(mon, WITHOUT_BOB), user=dict(VIEW_AS))["why_questions"][0]["key"]
    out = sl.answer_edit_question(rid, hid, key, "always", user=dict(VIEW_AS))
    assert out["counted"] is False and sv.standing_patterns(rid) == []
    sv.adopt_admin_saves(rid, {"id": 1, "username": "erik", "role": "owner"})
    assert [s["source"] for s in sv.standing_patterns(rid)] == ["owner_said"]


# ══ L-15: the predictor steers once its backtest has earned it ════════════

def _predict_history(rid, weeks):
    for i in range(weeks):
        mon = _monday(i + 1)
        draft = [_row(mon + timedelta(days=d), n) for n in ("Ana", "Ben", "Cy", "Dee", "Eve", "Flo") for d in range(5)]
        draft += [_row(mon + timedelta(days=5), "Zed"), _row(mon + timedelta(days=6), "Zed")]
        final = [r for r in draft if r["employee"] != "Zed"]
        _week(rid, mon, draft, final[:i] + final[i + 1:])


def test_the_predictor_steers_only_at_its_backtest_hit_rate():
    rid = _rid()
    _predict_history(rid, 6)
    mon = _monday(-1)
    rows = [_row(mon + timedelta(days=d), n) for n in ("Ana", "Ben") for d in range(5)] + \
        [_row(mon + timedelta(days=5), "Zed")]
    sig = sl.likely_edit_signals(rid, rows)
    assert sig["ready"] and sig["hit_rate"] >= sl.PREDICT_ACTIONABLE_HIT_RATE
    zed = [f for f in sig["flags"] if f["employee"] == "Zed"]
    assert zed and 0 < zed[0]["weight"] <= 1 and {"index", "date", "shift_start", "role", "likelihood",
                                                  "features", "origin"} <= set(zed[0])
    review = sl.likely_to_change(rid, rows)
    assert review["ready"] and review["rows"][0]["employee"] == "Zed" and "were right" in review["note"]
    # A manager who changes Zed's rows about half the time: the predictor
    # flags them, but its own backtest is right only 38% of the time — shown,
    # never steering.
    import random
    rng = random.Random(5)
    weeks = []
    for i in range(8):
        start = _monday(-1) + timedelta(weeks=i)
        wrows, edited = [], []
        for n in ("Ana", "Ben", "Cy", "Dee", "Eve", "Flo", "Zed"):
            for d in range(7):
                if rng.random() < 0.6:
                    wrows.append(_row(start + timedelta(days=d), n, start="4:00pm", hours=6))
                    edited.append(rng.random() < (0.5 if n == "Zed" else 0.05))
        weeks.append({"history_id": i, "week_start": start.isoformat(), "rows": wrows, "edited": edited})
    bt = sl.edit_prediction_backtest(weeks)
    assert bt["weeks"] >= sl.PREDICT_MIN_BACKTEST_WEEKS and bt["flagged"] and bt["hit_rate"] < 0.6
    weak = sl.likely_edit_signals(rid, rows, weeks=weeks)
    assert weak["ready"] is False and weak["flags"] == [] and "right 8 of 21 times" in weak["reason"]


def test_the_publish_check_carries_likely_to_change(monkeypatch):
    import client_api
    import people
    import strategy_routes
    rid = _rid()
    mon = _monday(-1)
    hid = _hist(rid, mon, _crew(mon, EVERYONE))
    monkeypatch.setattr(client_api, "publish_review", lambda *a, **k: {"blockers": []})
    monkeypatch.setattr(sl, "likely_to_change", lambda r, rows, **k: {"ready": True, "rows": [{"employee": "Zed"}]})
    monkeypatch.setattr(people, "reach", lambda *a, **k: [])
    monkeypatch.setattr(people, "reach_summary", lambda *a, **k: {})
    monkeypatch.setattr(strategy_routes, "_sees_labor", lambda u: True)
    with Flask(__name__).test_request_context(f"/labor/publish-check?schedule_id={hid}"):
        body, status = strategy_routes._do_publish_check({"restaurant_id": rid, "id": 1, "role": "owner"})
    assert status == 200 and body["likely_to_change"]["rows"] == [{"employee": "Zed"}]


def test_the_draft_is_retimed_to_the_owners_time_rules():
    """The generator honours a start/end rule in code, not only in the
    prompt: the job runs apply_role_times over the draft (L-33)."""
    import inspect
    import schedule_engine
    src = inspect.getsource(schedule_engine._run_schedule_job)
    # Once after the fill, trim and overtime passes, and once more on the
    # finished rows after the solver and the optimizer.
    assert src.count("_rules.apply_role_times(preview_rows, _constraints, editable=_editable)") == 2
    assert src.index("apply_role_times") < src.index("_opt.optimize(") < src.rindex("apply_role_times")
    assert "role_times" in inspect.getsource(snr.apply_note_rules)
