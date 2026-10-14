"""Scheduler audit 10/10/26, workstream H1 — the Studio's kept inputs:
keyed on what they were read from, sized for many editors, locked
(SCALE-8, BUGSLIFE-23), and the re-score answered as a delta (SCALE-29)."""
import json
import os
import re
import shutil
import subprocess
import threading

import pytest

import schedule_engine as se

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


@pytest.fixture
def fake_build(monkeypatch):
    """studio_prepared with the database reads replaced: counts the builds."""
    built = []

    def inputs(rid, daily_target_hours=None, week_rows=None):
        built.append(rid)
        return {"constraints": None, "roster": []}
    monkeypatch.setattr(se, "quality_inputs_from_db", inputs)
    monkeypatch.setattr(se, "_quality_signals", lambda rid, inputs, **k: ({}, {}))
    versions = {}
    monkeypatch.setattr("models.schedule_inputs_version", lambda rid, *a, **k: versions.get(rid, "v1"))
    se.studio_invalidate()
    yield {"built": built, "versions": versions}
    se.studio_invalidate()


def _rows(n=3, day="2026-10-05"):
    return [{"date": day, "day": "Monday", "employee": f"P{i}", "role": "Server", "shift_start": "5:00pm",
             "shift_end": "10:00pm", "scheduled_hours": "5", "notes": ""} for i in range(n)]


def test_many_editors_at_once_keep_their_inputs(fake_build):
    """The store held 8 entries for the whole process: a ninth editor
    evicted the first, and past eight every drag rebuilt (SCALE-8)."""
    for rid in range(1, 41):
        se.studio_prepared(rid, _rows(), history_id=rid)
    for rid in range(1, 41):
        se.studio_prepared(rid, _rows(), history_id=rid)
    assert len(fake_build["built"]) == 40


def test_the_kept_inputs_follow_the_inputs_version_not_a_save(fake_build):
    a = se.studio_prepared(7, _rows(), history_id=70)
    assert se.studio_prepared(7, _rows(), history_id=70, fresh=True) is a          # a save re-uses them
    fake_build["versions"][7] = "v2"                                               # a rating changed
    assert se.studio_prepared(7, _rows(), history_id=70) is not a
    assert fake_build["built"] == [7, 7]


def test_one_restaurant_keeps_a_few_weeks_and_a_large_week_weighs_its_bytes(fake_build, monkeypatch):
    monkeypatch.setattr(se, "STUDIO_CACHE_PER_RESTAURANT", 2)
    for wk in ("2026-10-05", "2026-10-12", "2026-10-19"):
        se.studio_prepared(3, _rows(day=wk), history_id=None)
    assert sum(1 for k in se._studio_cache if k[0] == 3) == 2
    se.studio_invalidate()
    big = se.studio_prepared(1, _rows(1100), history_id=1)
    small = se.studio_prepared(2, _rows(150), history_id=2)
    # Sized in bytes (blind re-audit 10/11/26 SCALE-22): a 1,100-row week
    # weighs several times a 150-row one, measured, not counted in rows.
    assert big["bytes"] > 5 * small["bytes"] > 0
    se.studio_invalidate()
    monkeypatch.setattr(se, "STUDIO_CACHE_BUDGET_MB", (big["bytes"] + small["bytes"] - 1) / 1048576.0)
    se.studio_prepared(1, _rows(1100), history_id=1)
    se.studio_prepared(2, _rows(150), history_id=2)
    # Over the budget together: the least recently used went, and it says so.
    assert [k[0] for k in se._studio_cache] == [2]
    stats = se.studio_cache_stats()
    assert stats["entries"] == 1 and stats["evictions"] >= 1 and 0 < stats["bytes"] <= stats["budget_bytes"]


def test_the_kept_inputs_survive_many_threads_at_once(fake_build):
    """BUGSLIFE-23: four request threads iterated and mutated the store with
    no lock — 'dictionary changed size during iteration', a 500."""
    errors = []

    def editor(base):
        try:
            for i in range(60):
                se.studio_prepared(base + (i % 25), _rows(), history_id=i % 3)
                se.studio_is_live  # noqa: B018 - the live map shares the lock
        except Exception as e:      # pragma: no cover - the failure being guarded
            errors.append(repr(e))
    threads = [threading.Thread(target=editor, args=(t * 7,)) for t in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    src = open(se.__file__, encoding="utf-8").read()
    fn = src[src.index("def studio_prepared"):src.index("def _score_schedule_quality")]
    assert "with _studio_lock:" in fn


# ── SCALE-29: the re-score as a delta ──────────────────────────────────────

def _quality(scores):
    return {"checked": True, "score": sum(scores) // len(scores), "band": "good",
            "week_dimensions": [{"key": "coverage", "score": 80}],
            "shifts": [{"date": f"2026-10-0{5 + i // 2}", "daypart": "morning" if i % 2 == 0 else "night",
                        "score": s, "dimensions": [{"key": "coverage", "score": s}], "people": ["A" * 400]}
                       for i, s in enumerate(scores)]}


def _merge(held, body):
    """The web client's merge (_sqTakeQuality), in Python."""
    dl = body["quality_delta"]
    assert dl["base"] == held["etag"]
    prev = held["quality"]["shifts"]
    had = dict(zip(se._shift_key_list(prev), prev))
    out = dict(dl["head"])
    out["shifts"] = [dl["changed"][k] if k in dl["changed"] else had[k] for k in dl["order"]]
    return out


def test_a_drag_is_answered_with_the_readings_it_changed():
    prepared = {"lock": threading.Lock()}
    first_q = _quality([70, 80, 90, 60, 75, 85])
    first = se.quality_payload(prepared, first_q)
    assert first["quality"] is first_q and first["quality_etag"]
    held = {"etag": first["quality_etag"], "quality": first_q}
    second_q = _quality([70, 80, 55, 60, 75, 85])             # one reading moved
    second = se.quality_payload(prepared, second_q, want_delta=True, base_etag=held["etag"])
    assert second["quality"] is None
    assert list(second["quality_delta"]["changed"]) == ["2026-10-06|morning"]
    assert _merge(held, second) == second_q
    assert len(json.dumps(second)) < len(json.dumps(second_q)) / 3
    # An etag this process never served (another worker, a rebuilt week): whole.
    assert se.quality_payload(prepared, second_q, want_delta=True, base_etag="nope")["quality"] is second_q
    # Without asking: whole, as iOS gets it.
    assert se.quality_payload(prepared, second_q)["quality"] is second_q


@pytest.mark.skipif(shutil.which("node") is None, reason="node is not installed")
def test_the_web_client_merges_a_delta_into_the_whole_answer():
    html = open(os.path.join(ROOT, "templates", "dashboard.html"), encoding="utf-8").read()
    start = html.index("var _sqHeld = null;")
    end = html.index("// Score the rows without storing anything")
    js = html[start:end]
    prepared = {"lock": threading.Lock()}
    q1 = _quality([70, 80, 90, 60, 75, 85])
    r1 = se.quality_payload(prepared, q1)
    q2 = _quality([70, 81, 90, 60, 75, 40])
    r2 = se.quality_payload(prepared, q2, want_delta=True, base_etag=r1["quality_etag"])
    js += ("\nvar a = _sqTakeQuality(" + json.dumps(dict(r1, ok=True)) + ");"
           "\nvar body = _sqDeltaBody({rows: []});"
           "\nvar b = _sqTakeQuality(" + json.dumps(dict(r2, ok=True)) + ");"
           "\nconsole.log(JSON.stringify({sent: body, merged: b.quality, held: _sqHeld.etag}));\n")
    out = subprocess.run(["node", "-"], input=js, capture_output=True, text=True, timeout=10)
    assert out.returncode == 0, out.stderr
    got = json.loads(out.stdout.strip())
    assert got["sent"] == {"rows": [], "delta": True, "quality_etag": r1["quality_etag"]}
    assert got["merged"] == json.loads(json.dumps(q2)) and got["held"] == r2["quality_etag"]


def test_every_web_score_call_asks_for_a_delta_and_merges_it():
    html = open(os.path.join(ROOT, "templates", "dashboard.html"), encoding="utf-8").read()
    calls = [m.start() for m in re.finditer(r"/api/labor/schedule/score", html)]
    assert len(calls) == 3
    for at in calls:
        near = html[at - 600:at + 900]
        assert "_sqDeltaBody" in near and "_sqTakeQuality" in near, near[:200]
