"""Schedule fix round 10/11/26, workstream W (wave 3): the shift table is
the read of a stored week (blind re-audit 10/11/26 ARCH-7, finishing D's
PARTIAL).

ARCH-7  every reader of a week's rows reads schedule_shifts by history id
        and copy, getting exactly what the text parses to (shape, order,
        ids), and the text's parse only — inside schedule_shifts — for a
        week the table cannot serve exactly; a ratchet holds every other
        parse of a stored week's text to the list below, which may only
        shrink; the database refuses one person on two overlapping shifts
        in a week's published copy (SQLite trigger + its plpgsql twin);
        the boot backfill is bounded (weeks and seconds) and resumable (a
        job_cursors cursor)."""
import ast
import functools
import pathlib
import sqlite3

import pytest

import models
import pg_copy
import schedule_publish
import schedule_rows
import schedule_shifts
import schedule_versions as sv
from tests.test_sf10_j2_floor import db, _restaurant, W1, HEADER, _day  # noqa: F401 — db is the fixture

ROOT = pathlib.Path(__file__).resolve().parent.parent
SERVERS = [("Ana", "Server"), ("Ben", "Server"), ("Cara", "Server"), ("Max", "Manager")]


def _text(rows):
    return HEADER + "\n" + "\n".join(f"{d},{_day(d)},{n},{role},{s},{e},{h},{note}"
                                     for d, n, role, s, e, h, note in rows)


def _week(db, rid, rows, publish=True):
    hid = models.save_schedule_history(rid, W1[0], W1[-1], 0, 0, 30, _text(rows), [], db_path=db)
    if publish:
        schedule_publish._stamp(rid, hid, "publish", "owner", "owner", None, db_path=db)
    return hid


def _stored(db, hid, col):
    conn = models.get_conn(db)
    try:
        return conn.execute(f"SELECT {col} FROM schedule_history WHERE id=?", (hid,)).fetchone()[0]
    finally:
        conn.close()


WEEK = [(W1[1], "Ana", "Server", "5:00pm", "11:00pm", 6, "\"bring keys, lock patio\""),
        (W1[1], "Max", "Manager", "4:00pm", "1:00am", 9, ""),
        (W1[2], "Ben", "Server", "11:00am", "3:00pm", 4, ""),
        (W1[2], "Ben", "Server", "5:00pm", "10:00pm", 5, "double"),
        (W1[0], "Cara", "Server", "10:00am", "4:00pm", 6, "")]


# ── the read ────────────────────────────────────────────────────────────────

def test_a_reader_gets_exactly_what_the_text_parses_from_the_table(db, monkeypatch):
    rid = _restaurant(db, SERVERS)
    hid = _week(db, rid, WEEK)
    want_live = schedule_rows.parse(_stored(db, hid, "published_csv"))
    want_working = schedule_rows.parse(_stored(db, hid, "schedule_csv"))
    conn = models.get_conn(db)
    try:
        assert schedule_shifts.current(conn, hid)
    finally:
        conn.close()
    # Served from the table: no parse of the week's text happens.
    monkeypatch.setattr(schedule_shifts._srows, "parse", lambda *_a, **_k: pytest.fail("parsed the text"))
    assert schedule_shifts.rows_of(hid, "live", db_path=db) == want_live
    assert schedule_shifts.rows_of(hid, "working", db_path=db) == want_working
    assert schedule_shifts.rows_by_week([hid, 999999], "live", db_path=db) == {hid: want_live, 999999: []}
    # Text order kept (Cara's Monday row was written last), commas and ids.
    assert [r["employee"] for r in want_live] == ["Ana", "Max", "Ben", "Ben", "Cara"]
    assert want_live[0]["notes"] == "bring keys, lock patio" and all(r["shift_id"] for r in want_live)
    # Another restaurant's id reads nothing.
    assert schedule_shifts.rows_of(hid, "live", db_path=db, restaurant_id=rid + 1000) == []


def test_a_week_the_table_cannot_hold_exactly_is_read_from_its_text(db):
    rid = _restaurant(db, SERVERS)
    hid = _week(db, rid, WEEK[:2], publish=False)
    # A week whose text has no ids (written before them, around store_week):
    # the projection would invent ids, so readers get the text.
    legacy = _text(WEEK[:2])
    conn = models.get_conn(db)
    try:
        conn.execute("UPDATE schedule_history SET schedule_csv=? WHERE id=?", (legacy, hid))
        conn.commit()
        conn.execute("BEGIN IMMEDIATE")
        schedule_shifts.project(conn, rid, hid)
        conn.commit()
        assert conn.execute("SELECT exact FROM schedule_shift_weeks WHERE history_id=?", (hid,)).fetchone()[0] == 0
        assert not schedule_shifts.current(conn, hid)
    finally:
        conn.close()
    got = schedule_shifts.rows_of(hid, "working", db_path=db)
    assert got == schedule_rows.parse(legacy) and all(r["shift_id"] == "" for r in got)


def test_a_week_changed_around_the_week_service_is_read_from_its_text(db):
    rid = _restaurant(db, SERVERS)
    hid = _week(db, rid, WEEK[:1])
    conn = models.get_conn(db)
    conn.execute("UPDATE schedule_history SET published_csv=replace(published_csv, 'Ana', 'Ben') WHERE id=?", (hid,))
    conn.commit()
    conn.close()
    assert [r["employee"] for r in schedule_shifts.rows_of(hid, "live", db_path=db)] == ["Ben"]
    assert [r["employee"] for r in schedule_shifts.rows_of(hid, "working", db_path=db)] == ["Ana"]


def test_the_storage_readers_read_the_table(db):
    rid = _restaurant(db, SERVERS)
    hid = _week(db, rid, WEEK)
    live = schedule_rows.parse(_stored(db, hid, "published_csv"))
    assert sv.published_rows(hid, rid, db_path=db) == live
    assert sv.draft_rows(hid, rid, db_path=db) == schedule_rows.parse(_stored(db, hid, "schedule_csv"))
    assert models.live_week_rows(rid, W1[1], db_path=db) == live
    conn = models.get_conn(db)
    try:
        version = conn.execute("SELECT MAX(version) FROM schedule_versions WHERE history_id=?", (hid,)).fetchone()[0]
        assert schedule_shifts.revision_rows(conn, hid, version, _stored(db, hid, "published_csv")) == live
        # An older revision is its own text.
        assert schedule_shifts.revision_rows(conn, hid, version - 1, _text(WEEK[:1])) == \
            schedule_rows.parse(_text(WEEK[:1]))
    finally:
        conn.close()


# ── the double-booking guard ────────────────────────────────────────────────

def _live_rows(db, hid):
    return schedule_rows.parse(_stored(db, hid, "published_csv"))


def test_a_live_write_that_double_books_someone_is_refused_and_nothing_is_written(db):
    rid = _restaurant(db, SERVERS)
    hid = _week(db, rid, WEEK[:3])
    before = _stored(db, hid, "published_csv")
    rows = _live_rows(db, hid)
    # Ana given Ben's Tuesday 11-3 is fine; Ana on a second Monday shift
    # overlapping her 5-11 is a double booking.
    rows.append(dict(rows[0], shift_id=schedule_rows.new_id(), shift_start="9:00pm", shift_end="11:30pm"))
    conn = models.get_conn(db)
    try:
        conn.execute("BEGIN IMMEDIATE")
        with pytest.raises(schedule_shifts.DoubleBooking) as got:
            sv.store_week(conn, rid, hid, published=schedule_rows.to_csv(rows))
        conn.rollback()
    finally:
        conn.close()
    assert "Ana" in str(got.value)
    assert _stored(db, hid, "published_csv") == before
    # The working copy (a draft, an unsent edit) is never refused.
    conn = models.get_conn(db)
    try:
        conn.execute("BEGIN IMMEDIATE")
        sv.store_week(conn, rid, hid, working=schedule_rows.to_csv(rows))
        conn.commit()
        assert schedule_shifts.double_bookings(conn, hid, "working")
        assert schedule_shifts.double_bookings(conn, hid, "published") == []
    finally:
        conn.close()


def test_the_database_itself_refuses_a_double_booking(db):
    rid = _restaurant(db, SERVERS)
    hid = _week(db, rid, WEEK[:1])
    conn = models.get_conn(db)
    try:
        r = dict(conn.execute("SELECT * FROM schedule_shifts WHERE history_id=? AND copy='published'",
                              (hid,)).fetchone())
        r.pop("id")
        r.update(shift_id=schedule_rows.new_id(), starts_at=r["starts_at"][:11] + "22:00",
                 ends_at=r["ends_at"][:11] + "23:30", overlap_ok_with=None)
        cols = ", ".join(r)
        with pytest.raises(sqlite3.IntegrityError, match="double booking"):
            conn.execute(f"INSERT INTO schedule_shifts ({cols}) VALUES ({','.join('?' * len(r))})", tuple(r.values()))
        # Somebody else at that time, or Ana after her shift, is fine.
        conn.execute(f"INSERT INTO schedule_shifts ({cols}) VALUES ({','.join('?' * len(r))})",
                     tuple(dict(r, shift_id=schedule_rows.new_id(), starts_at=r["ends_at"][:11] + "23:30",
                                ends_at=r["ends_at"][:11] + "23:59").values()))
        conn.rollback()
    finally:
        conn.close()


def test_an_overlap_the_gate_let_through_stays_through_later_live_writes(db):
    rid = _restaurant(db, SERVERS)
    double = WEEK[:1] + [(W1[1], "Ana", "Server", "10:00pm", "11:30pm", 1.5, "")]
    hid = _week(db, rid, double + WEEK[2:3])       # the Send went past the overlap with a reason
    conn = models.get_conn(db)
    try:
        pair = schedule_shifts.double_bookings(conn, hid, "published")
        assert len(pair) == 1
        # A cover on the same week (Ben's shift to Cara) is not refused for
        # the overlap staff already have.
        rows = _live_rows(db, hid)
        rows[2] = dict(rows[2], employee="Cara", person_id="")
        conn.execute("BEGIN IMMEDIATE")
        sv.store_week(conn, rid, hid, published=schedule_rows.to_csv(rows))
        conn.commit()
        assert schedule_shifts.double_bookings(conn, hid, "published") == pair
    finally:
        conn.close()


def test_the_postgres_guard_is_the_same_rule():
    conn = sqlite3.connect(":memory:")
    conn.execute("CREATE TABLE schedule_shifts (id INTEGER PRIMARY KEY, person_ref TEXT)")
    stmts = [s for s in pg_copy.trigger_statements(conn) if "double_booking" in s or "no_double_booking" in s]
    fn = next(s for s in stmts if s.startswith("CREATE OR REPLACE FUNCTION cavnar_no_double_booking"))
    sqlite_rule = schedule_shifts._TRIGGER_SQL
    for clause in ("NEW.copy = 'published'" if False else "copy", "o.history_id", "o.person_ref", "o.starts_at <",
                   "NEW.starts_at < o.ends_at", "overlap_ok_with"):
        assert clause in fn and clause in sqlite_rule
    assert any(s.startswith(f"CREATE TRIGGER {schedule_shifts.TRIGGER} BEFORE INSERT ON schedule_shifts")
               for s in stmts)


# ── the boot backfill ───────────────────────────────────────────────────────

def test_the_boot_backfill_is_bounded_and_resumes_from_its_cursor(db):
    rid = _restaurant(db, SERVERS)
    hids = [_week(db, rid, WEEK[:2], publish=False) for _ in range(7)]
    conn = models.get_conn(db)
    conn.execute("DELETE FROM schedule_shifts")
    conn.execute("DELETE FROM schedule_shift_weeks")
    conn.commit()
    conn.close()
    first = schedule_shifts.backfill(db_path=db, max_weeks=3)
    assert first["projected"] == 3 and first["hit_bound"] and first["cursor"] == sorted(hids)[-3]
    second = schedule_shifts.backfill(db_path=db, max_weeks=3)
    assert second["projected"] == 3 and second["cursor"] == sorted(hids)[-6]
    third = schedule_shifts.backfill(db_path=db, max_weeks=3)
    assert third["projected"] == 1 and not third["hit_bound"]     # reached the oldest: the cursor clears
    again = schedule_shifts.backfill(db_path=db, max_weeks=3)
    assert again["projected"] == 0 and again["examined"] == 3      # from the newest again, nothing to do
    # No time left: nothing examined, the place is kept.
    stopped = schedule_shifts.backfill(db_path=db, max_seconds=0, max_weeks=3)
    assert stopped["examined"] == 0 and stopped["hit_bound"]
    conn = models.get_conn(db)
    try:
        assert all(schedule_shifts.current(conn, h) for h in hids)
    finally:
        conn.close()


def test_the_backfill_reprojects_a_stale_or_old_format_week(db):
    rid = _restaurant(db, SERVERS)
    hid = _week(db, rid, WEEK[:2])
    conn = models.get_conn(db)
    conn.execute("UPDATE schedule_shift_weeks SET fmt=1 WHERE history_id=?", (hid,))
    conn.commit()
    conn.close()
    assert schedule_shifts.backfill(db_path=db)["projected"] == 1
    conn = models.get_conn(db)
    try:
        assert schedule_shifts.current(conn, hid)
    finally:
        conn.close()


# ── the ratchet: no new parse of a stored week's text ───────────────────────

# The storage modules: the week's text is theirs to read and write.
STORAGE = {"schedule_shifts.py", "schedule_versions.py", "schedule_rows.py", "models.py"}
PARSERS = {"rows_from_csv", "employee_shifts_from_csv", "timed_shifts_from_csv", "employees_in_schedule",
           "DictReader"}
COLUMNS = ("schedule_csv", "published_csv", "live_csv_sql")
# file::function -> (parse calls, why it still parses text). May only shrink.
STILL_ON_TEXT = {
    # The text of an OLDER revision (schedule_versions): the table keeps one
    # revision per copy (schedule_shifts' module doc).
    "mobile_api.py::_keep_mine_merge": (2, "a merge against version snapshots"),
    "pg_shadow.py::_events": (1, "the revision before, when no connection is given"),
    "predictability_pay.py::_last_published_rows": (2, "the published versions before a send"),
    "prepublish.py::_changed": (1, "the version before"),
    "prepublish.py::_previous_draft": (2, "the draft versions before"),
    "review_intelligence.py::slice_context": (2, "the generated and published versions"),
    # Text that is not a stored week, or the writer's own rewrite.
    "labor.py::employee_shifts_from_csv": (1, "text the caller holds (a test, the direct-send shape)"),
    "labor.py::timed_shifts_from_csv": (1, "text the caller holds (an RPOWER preview, a test)"),
    "labor.py::employees_in_schedule": (1, "text the caller holds"),
    "people.py::_rename_week": (2, "the rename's rewrite of both copies (a writer, through store_week)"),
    "people.py::unmerge_people": (1, "the POS shift file, not a week"),
    # In flight in another workstream this wave (P: the draft sources and
    # the engine) — to move once it lands.
    "schedule_engine.py::locked_rows_of": (1, "P's file this wave"),
    "schedule_engine.py::_build_schedule_result": (1, "P's file this wave"),
    "schedule_engine.py::_summarize_schedule_csv_by_day_role": (1, "P's file; the model's answer"),
    "schedule_engine.py::_prior_week_assignments": (1, "P's file this wave"),
    "schedule_engine.py::load_ledger": (1, "P's file this wave"),
    "schedule_engine.py::_quality_signals": (1, "P's file this wave"),
    "schedule_engine.py::_gate_fill_answer": (1, "P's file; the model's answer"),
    "schedule_engine.py::replay_checks": (1, "P's file this wave"),
    "schedule_job.py::_read_base": (1, "P's file this wave"),
    "schedule_job.py::_started_week": (1, "P's file this wave"),
    "schedule_job.py::_held_base": (1, "P's file this wave"),
    "schedule_job.py::_after_save": (1, "P's file; the generation's own text"),
    "schedule_fallback.py::base_week": (1, "the carry-forward draft source, beside P's"),
    # Harnesses on throwaway databases, and the Crew stage's raw read (it
    # keeps rows with no person, which the reader drops) — next to move.
    "schedule_benchmark.py::saved_rows": (1, "the benchmark harness"),
    "studio_bench.py::_history": (1, "the Studio bench harness"),
    "crew_matrix.py::_draft": (1, "the Crew stage's raw read"),
    "scripts/schedule_eval.py::evaluate": (1, "an offline script"),
    "scripts/schedule_model_eval.py::_rerender": (1, "an offline script; a result's text"),
}


@functools.lru_cache(maxsize=1)
def _parse_sites():
    found = {}
    files = [p for p in list(ROOT.glob("*.py")) + list(ROOT.glob("*/*.py"))
             if not {"tests", ".claude", ".venv", "venv"} & set(p.relative_to(ROOT).parts)
             and p.name not in STORAGE]
    for path in files:
        src = path.read_text()
        try:
            tree = ast.parse(src)
        except SyntaxError:
            continue
        for fn in ast.walk(tree):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            body = ast.get_source_segment(src, fn) or ""
            if not any(c in body for c in COLUMNS):
                continue
            inner = {id(n) for f in ast.walk(fn) if f is not fn and isinstance(f, (ast.FunctionDef,
                                                                                    ast.AsyncFunctionDef))
                     for n in ast.walk(f)}
            for node in ast.walk(fn):
                if id(node) in inner or not isinstance(node, ast.Call):
                    continue
                f = node.func
                name = f.attr if isinstance(f, ast.Attribute) else (f.id if isinstance(f, ast.Name) else None)
                rows_parse = name == "parse" and isinstance(f, ast.Attribute) and \
                    isinstance(f.value, ast.Name) and "rows" in f.value.id
                if name in PARSERS or rows_parse:
                    key = f"{path.relative_to(ROOT)}::{fn.name}"
                    found[key] = found.get(key, 0) + 1
    return found


def test_no_new_code_parses_a_stored_weeks_text_outside_the_storage_modules():
    """A reader of a stored week reads schedule_shifts (rows_of /
    rows_by_week / live_week_rows / revision_rows). Every parse of a week's
    text outside the storage modules is listed above with why; the list may
    only shrink — a new one, or one more in a listed function, fails here."""
    found = _parse_sites()
    new = {k: v for k, v in found.items() if k not in STILL_ON_TEXT}
    grown = {k: (v, STILL_ON_TEXT[k][0]) for k, v in found.items() if k in STILL_ON_TEXT and v > STILL_ON_TEXT[k][0]}
    assert new == {}, f"read these weeks through schedule_shifts.rows_of, not their text: {new}"
    assert grown == {}, f"more parses of a week's text than allowed: {grown}"


def test_the_ratchet_list_only_names_sites_that_still_exist():
    found = _parse_sites()
    gone = sorted(k for k in STILL_ON_TEXT if k not in found)
    assert gone == [], f"moved onto the table — take them off STILL_ON_TEXT: {gone}"


def test_the_readers_this_round_moved_read_the_table():
    """The ones D's report left on the text (ARCH-7 PARTIAL)."""
    moved = {"staff_schedule.py": "rows_by_week", "shift_requests.py": "_ss.rows_of", "labor_live.py": "live_week_rows",
             "labor_replacements.py": "live_week_rows", "pos_schedule_push.py": "live_week_rows",
             "commitment_ladder.py": "rows_of", "pg_shadow.py": "revision_rows", "prepublish.py": "rows_of",
             "schedule_publish.py": "rows_of", "schedule_learning.py": "rows_of", "schedule_intel.py": "rows_by_week",
             "schedule_memory.py": "rows_by_week", "staffing_signals.py": "rows_of", "time_off.py": "rows_by_week",
             "staff_routes.py": "rows_by_week", "task_sheets.py": "rows_of", "schedule_rules.py": "rows_by_week"}
    for name, call in moved.items():
        assert call in (ROOT / name).read_text(), name
