"""Schedule fix round 10/11/26, workstream A2 — the schedule's model calls
(blind re-audit AI-21: a big week an owner waits for in parallel parts;
AI-30: a server-side refusal fallback, off until confirmed live). No model
is called here."""
import model_gateway as mg
import labor
import schedule_engine as se

WEEK = ["2026-10-14", "2026-10-15", "2026-10-16", "2026-10-17", "2026-10-18", "2026-10-19", "2026-10-20"]
ROSTER = [(f"S{i}", "Server") for i in range(160)] + [(f"C{i}", "Line Cook") for i in range(80)]


def test_a_big_week_an_owner_waits_for_is_planned_in_parallel_department_parts(monkeypatch):
    monkeypatch.delenv("SCHEDULE_PARALLEL_SPLIT_ROWS", raising=False)
    monkeypatch.delenv("SCHEDULE_PARALLEL_CALLS", raising=False)
    rows = 1100.0
    one = se._plan_tasks(rows, WEEK, ROSTER, per_call=se.CHUNK_ROWS_PER_CALL)
    assert len(one) == 1                                   # it fits one 8-13 minute call ...
    split = se._plan_tasks(rows, WEEK, ROSTER, per_call=se.CHUNK_ROWS_PER_CALL, split_rows=se.PARALLEL_SPLIT_ROWS)
    labels = {t["label"] for t in split}
    assert labels == {"KITCHEN", "FRONT OF HOUSE"}         # ... an owner waiting gets parallel parts
    assert 2 <= len(split) <= se.parallel_calls()
    # Each part writes far fewer rows than the one call did.
    heads = len(ROSTER)
    for t in split:
        share = len(t["people"]) / heads * len(t["dates"]) / len(WEEK)
        assert rows * share <= rows * 0.7
    # A small week is never split.
    assert len(se._plan_tasks(300.0, WEEK, ROSTER, split_rows=se.PARALLEL_SPLIT_ROWS)) == 1


def test_only_an_owner_waiting_splits(monkeypatch):
    monkeypatch.delenv("SCHEDULE_PARALLEL_SPLIT_ROWS", raising=False)
    monkeypatch.delenv("SCHEDULE_PARALLEL_CALLS", raising=False)
    with mg.call_class_scope("interactive"):
        assert se.parallel_split_rows() == se.PARALLEL_SPLIT_ROWS
    with mg.call_class_scope("scheduled"):                  # the auto-draft: nobody waiting, one call
        assert se.parallel_split_rows() is None
    monkeypatch.setenv("SCHEDULE_PARALLEL_SPLIT_ROWS", "0")
    with mg.call_class_scope("interactive"):
        assert se.parallel_split_rows() is None


def test_the_refusal_fallback_is_off_by_default_and_never_stored(monkeypatch):
    call = {"model": "claude-sonnet-5-5", "thinking": {"type": "adaptive"}, "messages": []}
    monkeypatch.delenv("SCHEDULE_REFUSAL_FALLBACK", raising=False)
    assert labor.refusal_fallback(call) is call
    monkeypatch.setenv("SCHEDULE_REFUSAL_FALLBACK", "1")
    sent = labor.refusal_fallback(call)
    assert sent["extra_body"] == {"fallbacks": "default"}
    assert sent["extra_headers"]["anthropic-beta"] == labor.REFUSAL_FALLBACK_BETA
    assert "extra_body" not in call                         # the stored request never carries it
    # Not on a model without it, nor with thinking turned off by between_tools.
    assert labor.refusal_fallback({"model": "claude-haiku-5-5"})  == {"model": "claude-haiku-5-5"}
    btw = {"model": "claude-sonnet-5-5", "thinking": {"type": "between_tools"}}
    assert labor.refusal_fallback(btw) is btw


def test_the_batch_request_is_built_without_the_fallback():
    import inspect
    import schedule_batch
    assert "refusal_fallback" not in inspect.getsource(schedule_batch)
    src = "".join(inspect.getsource(f) for f in labor.SCHEDULE_CALL_STEPS)
    assert "create_with_retry(_client, readiness=_ready_sched, **refusal_fallback(_call))" in src
    assert "request=dict(_call)" in src
