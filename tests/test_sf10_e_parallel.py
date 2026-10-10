"""Scheduler audit 10/10/26, workstream E — AI-4, AI-18, SCALE-23: a big
week's department calls (and a part's first date slices) go out together;
the cap counts the calls in flight; the clock is planned for batches."""
import threading
import time

import labor
import schedule_engine as se

WEEK = ["2026-10-12", "2026-10-13", "2026-10-14", "2026-10-15", "2026-10-16", "2026-10-17", "2026-10-18"]


def _gen_factory(active, peak, seen):
    lock = threading.Lock()

    def gen(analysis, shifts, week_slice=None, prior_rows=None, **kw):
        with lock:
            active[0] += 1
            peak[0] = max(peak[0], active[0])
        try:
            time.sleep(0.15)
            names = [n for n, _r in (kw.get("roster") or [])]
            seen.append({"dates": list(week_slice or WEEK), "names": names, "notes": kw.get("call_notes") or "",
                         "prior": len(prior_rows or [])})
            dates = list(week_slice or WEEK)
            lines = [f"{d},{se._date_of(d).strftime('%A')},{n},Role,11:00am,3:00pm,4," for d in dates for n in names]
            return {"schedule_csv": "date,day,employee,role,shift_start,shift_end,scheduled_hours,notes\n"
                    + "\n".join(lines), "narrative": [], "complete_dates": dates, "generation_seconds": 0.15}
        finally:
            with lock:
                active[0] -= 1
    return gen


def test_department_calls_run_at_the_same_time(monkeypatch):
    monkeypatch.delenv("SCHEDULE_PARALLEL_CALLS", raising=False)
    active, peak, seen = [0], [0], []
    monkeypatch.setattr(labor, "generate_optimized_schedule", _gen_factory(active, peak, seen))
    monkeypatch.setattr(se, "_expected_rows", lambda shifts, roster: 4 * se.CHUNK_ROWS_PER_CALL)   # by department
    roster = [("Ana", "Server"), ("Cy", "Line Cook"), ("Mo", "Manager")]
    t0 = time.time()
    out = se._generate_in_parts({}, [], roster, {"week_start": WEEK[0], "restaurant_id": None})
    assert peak[0] >= 2, "the kitchen and the front of house were written one after another"
    assert set(out["generated_dates"]) == set(WEEK)
    assert all(s.get("parallel") for s in out["slices"])
    # Sent together, so the batch took about as long as one call, not the sum.
    assert time.time() - t0 < 0.15 * len(seen)


def test_a_parts_own_first_slices_go_together_each_told_its_share_of_the_week(monkeypatch):
    monkeypatch.delenv("SCHEDULE_PARALLEL_CALLS", raising=False)
    active, peak, seen = [0], [0], []
    monkeypatch.setattr(labor, "generate_optimized_schedule", _gen_factory(active, peak, seen))
    monkeypatch.setattr(se, "_expected_rows", lambda shifts, roster: 2 * se.CHUNK_ROWS_PER_CALL - 10)   # two slices
    out = se._generate_in_parts({}, [], [("Ana", "Server")], {"week_start": WEEK[0], "restaurant_id": None,
                                                               "roster": [("Ana", "Server")]})
    assert peak[0] == 2 and len(seen) == 2
    assert all(s["prior"] == 0 for s in seen)        # neither saw the other's rows
    # Without a rule set there is nothing to compute a room from; with one,
    # each slice is told its share (below).
    assert set(out["generated_dates"]) == set(WEEK)


def test_the_room_note_splits_each_persons_week_by_the_slices_dates():
    from schedule_rules import Constraints
    c = Constraints(restaurant_id=1, week_dates=WEEK, week_days=[se._date_of(d).strftime("%A") for d in WEEK])
    c.hours_limits = {"ana": (0, 30)}
    c.base_hours = {"ana": {"b": 10.0}}
    lines = se._room_lines(c, [("Ana", "Server"), ("Bo", "Server")], WEEK[:4], WEEK)
    assert lines[0] == "- Ana: up to 11.4h on these dates"          # (30 - 10) x 4/7
    assert lines[1].startswith("- Bo: up to 22.9h")                  # (40 - 0) x 4/7
    t = {"dates": WEEK[:4], "people": [("Ana", "Server")], "parallel_room": True, "label": None, "retry": False,
         "missed": None, "what": "STAFF", "group": (0, 1), "depth": 0}
    dk = se._call_kwargs(t, {"roster": [("Ana", "Server")]}, c, None, set(), [], [], None, [], None,
                         open_dates=WEEK)
    assert "WRITTEN AT THE SAME TIME BY ANOTHER CALL" in dk["call_notes"] and "Ana: up to 11.4h" in dk["call_notes"]


def test_a_retry_waits_for_the_call_before_it():
    q = [{"label": "KITCHEN", "group": (0, 1), "depth": 0, "retry": False, "dates": WEEK[:3]},
         {"label": "KITCHEN", "group": (0, 1), "depth": 0, "retry": True, "dates": WEEK[3:]},
         {"label": "FRONT OF HOUSE", "group": (0, 1), "depth": 0, "retry": False, "dates": WEEK}]
    batch = se._take_batch(q, 3)
    assert [b["label"] for b in batch] == ["KITCHEN", "FRONT OF HOUSE"]
    assert q and q[0]["retry"] is True


def test_one_call_at_a_time_when_set(monkeypatch):
    monkeypatch.setenv("SCHEDULE_PARALLEL_CALLS", "1")
    q = [{"label": "KITCHEN", "group": (0, 1), "depth": 0, "retry": False, "dates": WEEK},
         {"label": "FRONT OF HOUSE", "group": (0, 1), "depth": 0, "retry": False, "dates": WEEK}]
    assert len(se._take_batch(q, se.parallel_calls())) == 1


def test_the_clock_is_planned_for_batches():
    plan = [{"label": "KITCHEN", "group": (0, 1), "depth": 0, "retry": False, "dates": WEEK},
            {"label": "FRONT OF HOUSE", "group": (0, 1), "depth": 0, "retry": False, "dates": WEEK}]
    assert se._planned_model_seconds(plan, lambda t: 400.0, 3) == 400.0
    assert se._planned_model_seconds(plan, lambda t: 400.0, 1) == 800.0


def test_calls_sent_together_count_toward_the_cap_before_they_go(monkeypatch):
    """Two slices that would go out together: the second is checked with the
    first's expected cost already in flight, and held back (AI-16)."""
    monkeypatch.delenv("SCHEDULE_PARALLEL_CALLS", raising=False)
    seen = []

    def cap(rows, costs=None, contract=None, heads=0, inflight_usd=0.0, inflight_calls=0):
        seen.append(inflight_calls)
        # The first call's spend is in flight, then on the ledger.
        over = inflight_calls or sent
        return {"calls": 1, "usd": 5.5, "next_usd": 1, "cap_calls": 16, "cap_usd": 6.0} if over else None
    monkeypatch.setattr(se, "_run_cap_reached", cap)
    sent = []

    def gen(analysis, shifts, **kw):
        sent.append(kw.get("week_slice"))
        dates = kw.get("week_slice") or []
        return {"schedule_csv": "date,day,employee,role,shift_start,shift_end,scheduled_hours,notes\n"
                + "\n".join(f"{d},{se._date_of(d).strftime('%A')},Ana,Server,11:00am,3:00pm,4," for d in dates),
                "narrative": [], "complete_dates": list(dates), "generation_seconds": 1}
    monkeypatch.setattr(labor, "generate_optimized_schedule", gen)
    monkeypatch.setattr(se, "_expected_rows", lambda shifts, roster: 2 * se.CHUNK_ROWS_PER_CALL - 10)
    out = se._generate_in_parts({}, [], [("Ana", "Server")], {"week_start": WEEK[0], "restaurant_id": None})
    assert seen[:2] == [0, 1]
    assert len(sent) == 1 and {u["why"] for u in out["unwritten_dates"]} == {"capped"}
