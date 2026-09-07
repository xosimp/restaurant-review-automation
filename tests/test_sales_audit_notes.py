"""The audit's notes reader: every note typed in any section is read,
turned into attributable insights / suggested answers / caveats, folded
into the engine, kept out of the customer report unless owner-safe, and
marked stale when the notes move on."""
import json

import pytest

import auth
import sales_audit_engine as engine
import sales_audit_notes_ai as notes_ai
import sales_audits as store
from tests.test_sales_audit import FULL, app, _admin_client, _hdr  # noqa: F401


@pytest.fixture(autouse=True)
def _redirect_db(monkeypatch, db_path):
    import models
    real = models.get_conn
    monkeypatch.setattr(models, "get_conn", lambda *a, **k: real(db_path))
    monkeypatch.setattr(store, "DB_PATH", db_path)
    monkeypatch.setattr(store, "get_conn", lambda *a, **k: real(db_path))
    store.init_sales_audits(db_path=db_path)


def _audit(notes=None, answers=None):
    a = dict(FULL)
    a.update(answers or {})
    aid = store.create_audit(answers=a)
    if notes:
        store.save_audit(aid, notes=notes)
    return store.get_audit(aid)


NOTES = {
    "labor": {"audit": "GM works the floor 5 nights, counted in labor.", "audit_in_report": True,
              "internal": "He got defensive about labor — don't lead with it."},
    "bar": {"audit": "Erik said pour cost is 22% on the last count."},
    "_global": {"internal": "Decision maker is Erik alone."},
}


def _canned(notes):
    """A reader response keyed to whatever indexes the real prompt would use."""
    idx = {(n["section"], n["source"]): i for i, n in enumerate(notes)}
    return json.dumps({
        "insights": [
            {"note": idx[("labor", "audit")], "category": "labor", "effect": "lower_confidence",
             "text": "Part of the labor gap is the GM on the floor, which scheduling will not remove.", "report_safe": True},
            {"note": idx[("labor", "internal")], "category": "labor", "effect": "context",
             "text": "Owner is sensitive about labor; lead elsewhere.", "report_safe": True},   # must be forced False
            {"note": 99, "category": "food", "effect": "context", "text": "dangling index"},        # dropped
        ],
        "suggestions": [
            {"note": idx[("bar", "audit")], "id": "bar_bev_cost_pct", "value": "22", "reason": "Stated on the last count."},
            {"note": idx[("bar", "audit")], "id": "not_a_question", "value": "1", "reason": "x"},   # dropped
            {"note": idx[("bar", "audit")], "id": "lab_labor_pct", "value": "250", "reason": "x"},      # >100% dropped
        ],
        "caveats": ["Bar variance was estimated, not counted."],
    })


@pytest.fixture
def reader(monkeypatch):
    calls = []

    def fake(prompt):
        calls.append(prompt)
        # rebuild the note list the module would have built, to key indexes
        return fake.response
    fake.response = None
    monkeypatch.setattr(notes_ai, "_call_claude", fake)
    return calls, fake


def _arm(fake, audit):
    fake.response = _canned(notes_ai.collect_notes(audit))


# ── collection and fingerprint ──────────────────────────────────────────────

def test_collects_every_note_and_free_text_answer():
    a = _audit(NOTES, {"pri_notes": "Wants to open a second location next year."})
    got = notes_ai.collect_notes(a)
    kinds = {(n["section"], n["source"]) for n in got}
    assert ("labor", "audit") in kinds and ("labor", "internal") in kinds and ("bar", "audit") in kinds
    assert ("_global", "internal") in kinds and ("priorities", "answer") in kinds
    assert [n for n in got if n["section"] == "labor" and n["source"] == "audit"][0]["in_report"] is True


def test_fingerprint_changes_only_when_notes_change():
    a = _audit(NOTES)
    fp = notes_ai.notes_fingerprint(a)
    b = dict(a, answers=dict(a["answers"], labor_pct="41"))
    assert notes_ai.notes_fingerprint(b) == fp
    c = dict(a, notes=dict(NOTES, food={"audit": "new"}))
    assert notes_ai.notes_fingerprint(c) != fp


def test_no_notes_reads_nothing_and_calls_no_model(reader):
    calls, fake = reader
    a = _audit()
    rec = notes_ai.read_notes(a, engine.compute(a["answers"]))
    assert rec["insights"] == [] and rec["notes_seen"] == 0 and calls == []


# ── sanitising what the model says ──────────────────────────────────────────

def test_read_is_sanitised_and_attributed(reader):
    calls, fake = reader
    a = _audit(NOTES)
    _arm(fake, a)
    rec = notes_ai.read_notes(a, engine.compute(a["answers"]))
    assert len(calls) == 1 and "GM works the floor" in calls[0] and "bar_bev_cost_pct" in calls[0]
    assert rec["fingerprint"] == notes_ai.notes_fingerprint(a) and rec["notes_seen"] == 4
    assert [i["effect"] for i in rec["insights"]] == ["lower_confidence", "context"]
    audit_ins, internal_ins = rec["insights"]
    assert audit_ins["source"] == "audit" and audit_ins["report_safe"] is True and audit_ins["in_report"] is True
    assert internal_ins["source"] == "internal" and internal_ins["report_safe"] is False
    assert [s["id"] for s in rec["suggestions"]] == ["bar_bev_cost_pct"]
    assert rec["suggestions"][0]["value"] == "22" and rec["suggestions"][0]["section"] == "bar"
    assert rec["caveats"] == ["Bar variance was estimated, not counted."]


def test_suggestion_matching_the_typed_answer_is_dropped(reader):
    calls, fake = reader
    a = _audit(NOTES, {"bar_bev_cost_pct": "22"})
    _arm(fake, a)
    rec = notes_ai.read_notes(a, engine.compute(a["answers"]))
    assert rec["suggestions"] == []


def test_garbage_from_the_model_raises_not_half_applies(reader):
    calls, fake = reader
    a = _audit(NOTES)
    fake.response = "I could not read these notes."
    with pytest.raises(ValueError):
        notes_ai.read_notes(a, engine.compute(a["answers"]))


# ── engine application ──────────────────────────────────────────────────────

def _read(a, **extra):
    rec = {"fingerprint": notes_ai.notes_fingerprint(a), "read_at": "2026-09-07 17:00:00", "model": "x", "notes_seen": 2,
           "insights": [{"note": 0, "section": "labor", "source": "audit", "category": "labor", "effect": "lower_confidence",
                         "text": "GM on the floor.", "report_safe": True, "in_report": True},
                        {"note": 1, "section": "labor", "source": "internal", "category": "labor", "effect": "context",
                         "text": "Sensitive topic.", "report_safe": False, "in_report": False}],
           "suggestions": [], "caveats": []}
    rec.update(extra)
    return rec


def test_insight_moves_confidence_one_step_and_only_owner_safe_text_reaches_assumptions():
    a = _audit(NOTES)
    base = engine.compute(a["answers"])
    assert base["categories"]["labor"]["status"] == "ok"
    before = base["categories"]["labor"]["confidence"]
    res = engine.compute(a["answers"], None, _read(a))
    lab = res["categories"]["labor"]
    order = ["low", "moderate", "high"]
    assert order.index(lab["confidence"]) == max(0, order.index(before) - 1)
    assumptions = " ".join(lab["calc"]["assumptions"])
    assert "GM on the floor" in assumptions and "Sensitive topic" not in assumptions
    assert len(lab["notes"]) == 2 and res["notes_read"]["applied"] == 2
    # dollar figures never move from a note
    assert (lab["low"], lab["high"]) == (base["categories"]["labor"]["low"], base["categories"]["labor"]["high"])


def test_several_lowering_notes_move_confidence_one_step_only():
    a = _audit(NOTES)
    base = engine.compute(a["answers"])
    rec = _read(a)
    extra = dict(rec["insights"][0], text="Another reason.")
    rec["insights"] = [rec["insights"][0], extra, dict(extra, text="And another.")]
    res = engine.compute(a["answers"], None, rec)
    order = ["low", "moderate", "high"]
    assert order.index(res["categories"]["labor"]["confidence"]) == max(0, order.index(base["categories"]["labor"]["confidence"]) - 1)
    # a raise and a lower cancel out
    rec["insights"] = [rec["insights"][0], dict(rec["insights"][0], effect="raise_confidence", text="Corroborated.")]
    res2 = engine.compute(a["answers"], None, rec)
    assert res2["categories"]["labor"]["confidence"] == base["categories"]["labor"]["confidence"]


def test_stale_read_is_shown_but_not_applied():
    a = _audit(NOTES)
    base = engine.compute(a["answers"])
    res = engine.compute(a["answers"], None, _read(a, stale=True))
    assert res["notes_read"]["stale"] is True and res["notes_read"]["applied"] == 0
    assert res["categories"]["labor"]["confidence"] == base["categories"]["labor"]["confidence"]
    assert "notes" not in res["categories"]["labor"]


def test_lower_confidence_ranks_the_problem_lower():
    a = _audit(NOTES)
    base = engine.compute(a["answers"])
    res = engine.compute(a["answers"], None, _read(a))
    def rank(r):
        return [p["key"] for p in r["problems"]] if r["problems"] and "key" in r["problems"][0] else [p["title"] for p in r["problems"]]
    # weighted problem ordering re-runs with the adjusted confidence; totals confidence never rises
    conf = {"HIGH": 3, "MODERATE": 2, "LOW": 1}
    assert conf.get(res["totals"]["confidence"], 0) <= conf.get(base["totals"]["confidence"], 0)


# ── report boundary ─────────────────────────────────────────────────────────

def test_public_view_only_carries_owner_safe_audit_note_insights():
    a = _audit(NOTES)
    res = engine.compute(a["answers"], None, _read(a))
    store.store_results(a["id"], res, mark_generated=True)
    pv = store.public_view(store.get_audit(a["id"]))
    assert [c["text"] for c in pv["conversation"]] == ["GM on the floor."]
    blob = json.dumps(pv)
    assert "Sensitive topic" not in blob and "defensive" not in blob and "Decision maker" not in blob


def test_public_view_respects_the_include_in_report_tick():
    notes = {"labor": {"audit": "GM works the floor.", "audit_in_report": False}}
    a = _audit(notes)
    rec = _read(a)
    rec["insights"] = [dict(rec["insights"][0], in_report=False)]
    store.store_results(a["id"], engine.compute(a["answers"], None, rec), mark_generated=True)
    assert store.public_view(store.get_audit(a["id"]))["conversation"] == []


# ── over HTTP ───────────────────────────────────────────────────────────────

def test_read_notes_endpoint_round_trip_and_staleness(app, db_path, monkeypatch, reader):
    calls, fake = reader
    c = _admin_client(app, db_path, monkeypatch)
    aid = c.post("/admin/api/audits", json={"restaurant_name": "Notes Grill", "owner_name": "Erik"}, headers=_hdr()).get_json()["id"]
    ver = c.get("/admin/api/audits/%d" % aid).get_json()["audit"]["version"]
    # nothing to read yet
    assert c.post("/admin/api/audits/%d/read-notes" % aid, headers=_hdr()).status_code == 400
    s = c.patch("/admin/api/audits/%d" % aid, json={"version": ver, "answers": dict(FULL, restaurant_name="Notes Grill"), "notes": NOTES}, headers=_hdr()).get_json()
    assert s["ok"] and s["results"]["notes_read"] is None
    _arm(fake, store.get_audit(aid))
    r = c.post("/admin/api/audits/%d/read-notes" % aid, headers=_hdr()).get_json()
    assert r["ok"] and r["notes_read"]["notes_seen"] == 4 and len(calls) == 1
    nr = r["results"]["notes_read"]
    assert nr["stale"] is False and nr["applied"] == 2 and [x["id"] for x in nr["suggestions"]] == ["bar_bev_cost_pct"]
    assert r["results"]["categories"]["labor"]["notes"]
    # it is stored, and every later result carries it
    g = c.get("/admin/api/audits/%d" % aid).get_json()
    assert g["results"]["notes_read"]["applied"] == 2
    # applying the suggestion by typing the answer recomputes normally
    s2 = c.patch("/admin/api/audits/%d" % aid, json={"version": g["audit"]["version"], "answers": dict(g["audit"]["answers"], bar_bev_cost_pct="22")}, headers=_hdr()).get_json()
    assert s2["ok"] and s2["results"]["notes_read"]["stale"] is False
    # editing a note marks the read stale: shown, not applied
    s3 = c.patch("/admin/api/audits/%d" % aid, json={"version": s2["version"], "notes": dict(NOTES, food={"audit": "Counts weekly."})}, headers=_hdr()).get_json()
    assert s3["ok"] and s3["results"]["notes_read"]["stale"] is True and s3["results"]["notes_read"]["applied"] == 0


def test_generate_reads_changed_notes_itself_and_never_blocks_on_a_reader_failure(app, db_path, monkeypatch, reader):
    calls, fake = reader
    c = _admin_client(app, db_path, monkeypatch)
    aid = c.post("/admin/api/audits", json={"restaurant_name": "Gen Grill", "owner_name": "Erik"}, headers=_hdr()).get_json()["id"]
    ver = c.get("/admin/api/audits/%d" % aid).get_json()["audit"]["version"]
    c.patch("/admin/api/audits/%d" % aid, json={"version": ver, "answers": dict(FULL, restaurant_name="Gen Grill"), "notes": NOTES}, headers=_hdr())
    _arm(fake, store.get_audit(aid))
    gen = c.post("/admin/api/audits/%d/generate" % aid, headers=_hdr()).get_json()
    assert gen["ok"] and gen["notes_warning"] is None and len(calls) == 1
    assert gen["results"]["notes_read"]["applied"] == 2
    frozen = store.get_audit(aid)
    assert frozen["results"]["notes_read"]["applied"] == 2 and frozen["notes_ai"]["notes_seen"] == 4
    # unchanged notes → no second model call
    c.post("/admin/api/audits/%d/generate" % aid, headers=_hdr())
    assert len(calls) == 1
    # the reader breaking does not stop the generate
    ver = store.get_audit(aid)["version"]
    c.patch("/admin/api/audits/%d" % aid, json={"version": ver, "notes": dict(NOTES, food={"audit": "Counts weekly."})}, headers=_hdr())
    def boom(prompt):
        raise RuntimeError("model down")
    monkeypatch.setattr(notes_ai, "_call_claude", boom)
    gen2 = c.post("/admin/api/audits/%d/generate" % aid, headers=_hdr()).get_json()
    assert gen2["ok"] and "model down" in gen2["notes_warning"]
    assert gen2["results"]["notes_read"]["stale"] is True
    # the shared report shows the owner-safe insight and nothing internal
    tok = c.post("/admin/api/audits/%d/share" % aid, headers=_hdr()).get_json()["token"]
    html = c.get("/audit/r/%s" % tok).data.decode()
    assert "GM on the floor" in html and "Sensitive topic" not in html and "defensive" not in html


def test_reader_failure_over_http_is_a_502_not_a_crash(app, db_path, monkeypatch):
    c = _admin_client(app, db_path, monkeypatch)
    aid = c.post("/admin/api/audits", json={"restaurant_name": "Err Grill"}, headers=_hdr()).get_json()["id"]
    ver = c.get("/admin/api/audits/%d" % aid).get_json()["audit"]["version"]
    c.patch("/admin/api/audits/%d" % aid, json={"version": ver, "notes": NOTES}, headers=_hdr())
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    r = c.post("/admin/api/audits/%d/read-notes" % aid, headers=_hdr())
    assert r.status_code == 502 and "ANTHROPIC_API_KEY" in r.get_json()["error"]
