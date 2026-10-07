"""
schedule_output.py — what the week's schedule call hands back, what the
finished week does not meet, and the record of every call (schedule audit
10/3/26: PR-11, PR-12, PR-13, P-35, PR-15, PR-28, PR-31).

The output contract. labor.generate_optimized_schedule asks the model for
JSON against a schema built for each generation (schedule_schema): the
people on the roster (or the department's part of it), the roles they hold
or have worked, the week's dates and the clock times are enums, so a name, a
role, a date or a time format the model invents cannot be written. Rows come
grouped under their date and carry no column the code works out itself (the
weekday, the hours), which roughly halves what a row costs in output tokens.
A row's note is one of NOTE_VALUES, because the note is printed on the
employee's own schedule. parse_answer turns the answer into the CSV-shaped
rows the rest of the pipeline reads; an answer cut short keeps every complete
row (salvage_json), and a structured answer is never read as CSV. Two more
contracts sit beside it behind SCHEDULE_CONTRACT (AI cost audit 10/7/26 #69,
#70; see CONTRACTS): "compact", the same rows with one-letter keys, and
"shape", slots without names whose people the solver chooses. An answer
whose JSON broke partway keeps the days before the break (#71), and a
streamed answer's finished days are counted as it is written (DayWatch,
#36).

What the finished week does not meet (unmet_items): every floor, manager
minute, closer, target, leader rule, staffing ask, minimum and limit the
rows the owner sees miss — worked out from those rows, never from the
model's own account, and never squeezed into its three-bullet summary.

The record (schedule_model_calls): every schedule call's full input — the
exact request, and the generator's own arguments — and its answer, keyed to
its generation and, once the draft is saved, to its schedule_history row, so
a model, effort or prompt change can be replayed against real weeks
(scripts/schedule_model_eval.py). Created at boot (init_schedule_output, from
models.init_db); pruned by the retention registry (ops._RETENTION_DAYS).
"""
import dataclasses
import json
import re
import zlib
from datetime import date as _date, datetime as _datetime


def get_conn(db_path=None):
    """models.get_conn resolved at call time: a test that points
    models.get_conn at its own database reaches this module too (CLAUDE.md,
    bound imports)."""
    import models
    return models.get_conn(db_path) if db_path else models.get_conn()


# ── the output contract ────────────────────────────────────────────────────

# A row's note is printed on that employee's own schedule (labor.
# employee_shifts_from_csv → staff_facing_note), so the model may only ever
# write one of these. It used to write "one brief phrase per shift" while
# its prompt carried ratings, "Still developing" and "miss shifts often"
# (schedule audit 10/3/26 PR-15).
NOTE_VALUES = ("", "opener", "closer", "staggered start", "training shift", "standby")

# The clock times a row may start or end at: every quarter hour, plus any
# odd time the restaurant's own settings state (a 9:50pm close). The audit
# asked for half hours; a quarter-hour grid keeps every arrival or close an
# owner can state ("hosts at 4:45pm", a 10:15pm close) writable while still
# allowing no invented format.
TIME_STEP_MINUTES = 15

# What a row cost in output tokens under the old contract (eight keys, the
# weekday and the hours written out on every row) and under this one, for
# sizing calls before any measurement exists. measured_tokens_per_row reads
# the real figure from schedule_model_calls once calls have run.
OLD_TOKENS_PER_ROW = 60
ANSWER_TOKENS_PER_ROW_ESTIMATE = 30

# ── the output contracts (AI cost audit 10/7/26 #69, #70) ──────────────────
#
# "schema" is the contract above: a row is {"employee", "role", "start",
# "end", "note"} — about 30 output tokens, a fifth of them the five key names
# repeated on every row. Two more, chosen per generation by
# SCHEDULE_CONTRACT (read once a generation, schedule_contract):
#
# "compact" — the same rows, the same enums, the same grouping under each
#   date, with one-letter keys: {"e", "r", "s", "t", "n"} (employee, role,
#   start, end — "t" for "till" — and note). Nothing the model chooses
#   changes, only the key names it spells; parse_answer reads it back to
#   the same rows.
# "shape" — the model writes the week's SHAPE only: under each date, slots
#   {"r", "s", "t", "c", "n"} — a role, its times, how many people (c) and a
#   note — and no names. Who works each slot is solved in code by
#   schedule_solver over the same rules and objective the repair loop uses
#   (schedule_engine.assign_shape_slots), and every repair and gate step
#   runs after as before. Behind the flag: the week's quality has not been
#   measured on it (scripts/schedule_model_eval.py --prompts rerender:shape).
#
# Each "plain_" form is its contract's shape without the enums (the API
# refusing a roster too large to compile — labor's schema_enums=False).
# "csv" is the text fallback, whatever the contract asked for.
CONTRACTS = ("schema", "compact", "shape")
DEFAULT_CONTRACT = "schema"
# The keys a row (or a slot) is written with, by contract.
ROW_KEYS = {
    "schema": {"employee": "employee", "role": "role", "start": "start", "end": "end", "note": "note"},
    "compact": {"employee": "e", "role": "r", "start": "s", "end": "t", "note": "n"},
    "shape": {"role": "r", "start": "s", "end": "t", "count": "c", "note": "n"},
}
# The array each date's rows sit in.
DAY_ROWS_KEY = {"schema": "shifts", "compact": "shifts", "shape": "slots"}
# A slot asks for at most this many people; a larger count is read as this.
SHAPE_MAX_COUNT = 30
# What one row costs in answer tokens under each contract, for sizing a
# call's seconds before any call on it is measured. Measured with a
# synthetic answer (tests/test_schedule_contracts.py: characters one more row
# adds, ~3.2 characters a token for dense JSON — the same yardstick the
# schema's 30 was checked with): a compact row is ~63 characters against the
# schema's ~79, a one-person shape slot ~51 (a slot of two or more people is
# cheaper still per row). The real figure replaces these once calls on a
# contract are recorded (call_costs reads them by contract). The rows a call
# carries are still sized by the schema's 30 (schedule_engine.
# CHUNK_ROWS_PER_CALL): a cheaper row never grows a call toward the ceiling.
ANSWER_TOKENS_PER_ROW = {"schema": ANSWER_TOKENS_PER_ROW_ESTIMATE, "plain_schema": ANSWER_TOKENS_PER_ROW_ESTIMATE,
                         "compact": 24, "plain_compact": 24, "shape": 20, "plain_shape": 20,
                         "csv": ANSWER_TOKENS_PER_ROW_ESTIMATE}


def schedule_contract(value=None) -> str:
    """The output contract a generation answers on: `value`, else the
    SCHEDULE_CONTRACT environment variable, read at call time — one of
    CONTRACTS, DEFAULT_CONTRACT ("schema") for anything else. The engine
    reads it once per generation (every call of one generation answers on
    the same contract, so its schema and prompt cache are shared)."""
    import os
    v = str(value if value is not None else os.getenv("SCHEDULE_CONTRACT", "") or "").strip().lower()
    return v if v in CONTRACTS else DEFAULT_CONTRACT


def contract_base(contract) -> str:
    """"compact" for "compact" or "plain_compact"; "schema" for "schema",
    "plain_schema", "csv" or anything unknown."""
    c = str(contract or "").strip().lower()
    if c.startswith("plain_"):
        c = c[len("plain_"):]
    return c if c in CONTRACTS else DEFAULT_CONTRACT


def contract_family(contract) -> tuple:
    """The recorded contract values one contract's calls are stored under
    (with enums, and the plain fallback): what call_costs reads a
    contract's measured cost from."""
    base = contract_base(contract)
    return (base, "plain_" + base)


def answer_tokens_per_row(contract=None) -> float:
    """ANSWER_TOKENS_PER_ROW for `contract` (the schema's for one it does
    not know)."""
    return float(ANSWER_TOKENS_PER_ROW.get(str(contract or DEFAULT_CONTRACT), ANSWER_TOKENS_PER_ROW_ESTIMATE))

_CSV_COLS = ("date", "day", "employee", "role", "shift_start", "shift_end", "scheduled_hours", "notes")
CSV_HEADER = ",".join(_CSV_COLS)


def _fmt_minutes(m) -> str:
    m = int(m) % (24 * 60)
    h, mm = divmod(m, 60)
    return f"{h % 12 or 12}:{mm:02d}{'am' if h < 12 else 'pm'}"


def _minutes(value):
    from schedule_rules import parse_minutes
    return parse_minutes(str(value or ""))


def canonical_time(value):
    """"4:00pm" for any time the pipeline can read ("16:00", "4pm",
    "4:00 PM"), or None."""
    m = _minutes(value)
    return None if m is None else _fmt_minutes(m)


_STATED_TIME = re.compile(r"\b(\d{1,2})(?::([0-5]\d))?\s*([ap])\.?\s*m\b\.?|\b([01]?\d|2[0-3]):([0-5]\d)\b", re.I)


def stated_times(*sources) -> list:
    """Every clock time named in `sources` — dicts of times (open and close
    by weekday), lists of times, or free text (the owner's hours and shift
    rules) — as canonical strings."""
    out = []
    for src in sources:
        if not src:
            continue
        if isinstance(src, dict):
            vals = list(src.values())
        elif isinstance(src, (list, tuple, set)):
            vals = list(src)
        else:
            vals = [m.group(0) for m in _STATED_TIME.finditer(str(src))]
        for v in vals:
            t = canonical_time(v)
            if t and t not in out:
                out.append(t)
    return out


def clock_values(extra=()) -> list:
    """The time enum: every quarter hour from 12:00am, plus each time in
    `extra` that is not on the grid, in clock order."""
    vals = {m: _fmt_minutes(m) for m in range(0, 24 * 60, TIME_STEP_MINUTES)}
    for v in extra or ():
        m = _minutes(v)
        if m is not None:
            vals.setdefault(m % (24 * 60), _fmt_minutes(m))
    return [vals[m] for m in sorted(vals)]


def schema_roles(employees, worked=None, extra=()) -> list:
    """Every role a row may carry: each person's roster role, the roles
    they have worked (`worked`: {name: roles}), and `extra` (roles held
    beyond the roster's — Constraints.held_roles). One spelling per role,
    matched case-insensitively, the roster's first."""
    names = {str(n) for n, _r in (employees or [])}
    seen = {}
    for _n, r in (employees or []):
        r = " ".join(str(r or "").split())
        if r:
            seen.setdefault(r.lower(), r)
    for n, roles in sorted((worked or {}).items(), key=lambda kv: str(kv[0])):
        if str(n) not in names:
            continue
        for r in sorted(roles or (), key=str):
            r = " ".join(str(r or "").split())
            if r:
                seen.setdefault(r.lower(), r)
    for r in extra or ():
        r = " ".join(str(r or "").split())
        if r:
            seen.setdefault(r.lower(), r)
    return list(seen.values())


def _enum(values) -> dict:
    vals = list(dict.fromkeys(str(v) for v in (values or ()) if str(v).strip()))
    return {"type": "string", "enum": vals} if vals else {"type": "string"}


_ISO_IN = re.compile(r"(?<!\d)(\d{4}-\d{2}-\d{2})(?!\d)")


def iso_date_of(text) -> str:
    """The ISO date a date cell carries — "2026-10-05" from "2026-10-05",
    "Mon 2026-10-05" or "Monday, 2026-10-05" — or "" when it carries none or
    more than one. The request names each date with its weekday; an answer
    that copies that form is the same date (schedule re-audit 10/4/26
    PROMPT-7: such a row was dropped and the day read as missing)."""
    found = _ISO_IN.findall(str(text or ""))
    if len(found) != 1:
        return ""
    try:
        _datetime.strptime(found[0], "%Y-%m-%d")
    except ValueError:
        return ""
    return found[0]


def schedule_schema(employees=None, roles=None, dates=None, times=None) -> dict:
    """The JSON schema one generation answers against. Each argument that
    is given becomes an enum (an empty one is left a plain string): the
    roster, the roles, the dates, the clock times. `dates` is the whole
    week for every call of a generation — a slice is told its own dates in
    the prompt — so the schema, and with it the prompt cache, is the same
    for every date slice of one generation (structured outputs compile a
    schema once and keep it 24 hours; a changed output format invalidates a
    cached prompt). A department call's roster and roles are its own, so
    its schema is too (schedule re-audit 10/4/26 PROMPT-8).

    Rows are grouped under their date, and the weekday and the hours are not
    asked for: the code derives both (the hours from the times — times win
    over the model's arithmetic, schedule_engine._reconcile_scheduled_hours).
    `note` may be left out, and is one of NOTE_VALUES when written."""
    shift = {
        "type": "object",
        "properties": {
            "employee": _enum(employees),
            "role": _enum(roles),
            "start": _enum(times),
            "end": _enum(times),
            "note": {"type": "string", "enum": list(NOTE_VALUES)},
        },
        "required": ["employee", "role", "start", "end"],
        "additionalProperties": False,
    }
    day = {
        "type": "object",
        "properties": {"date": _enum(dates), "shifts": {"type": "array", "items": shift}},
        "required": ["date", "shifts"],
        "additionalProperties": False,
    }
    return {
        "type": "object",
        "properties": {
            "days": {"type": "array", "items": day},
            "summary": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["days", "summary"],
        "additionalProperties": False,
    }


def contract_schema(contract="schema", employees=None, roles=None, dates=None, times=None) -> dict:
    """The JSON schema for `contract` (CONTRACTS): "schema" is
    schedule_schema; "compact" the same rows with one-letter keys (ROW_KEYS);
    "shape" a role, its times, a count and a note per slot, and no names —
    `employees` is not used. The enums and the grouping under each date are
    the same in all three, so a call's schema stays the same for every call
    of one generation."""
    base = contract_base(contract)
    if base == "schema":
        return schedule_schema(employees=employees, roles=roles, dates=dates, times=times)
    keys = ROW_KEYS[base]
    if base == "compact":
        props = {keys["employee"]: _enum(employees), keys["role"]: _enum(roles), keys["start"]: _enum(times),
                 keys["end"]: _enum(times), keys["note"]: {"type": "string", "enum": list(NOTE_VALUES)}}
        required = [keys["employee"], keys["role"], keys["start"], keys["end"]]
    else:
        # The count is an integer with no bounds in the schema (structured
        # outputs compile a narrow keyword set); parse_answer holds it to
        # 1..SHAPE_MAX_COUNT.
        props = {keys["role"]: _enum(roles), keys["start"]: _enum(times), keys["end"]: _enum(times),
                 keys["count"]: {"type": "integer"},
                 keys["note"]: {"type": "string", "enum": list(NOTE_VALUES)}}
        required = [keys["role"], keys["start"], keys["end"], keys["count"]]
    row = {"type": "object", "properties": props, "required": required, "additionalProperties": False}
    rows_key = DAY_ROWS_KEY[base]
    day = {
        "type": "object",
        "properties": {"date": _enum(dates), rows_key: {"type": "array", "items": row}},
        "required": ["date", rows_key],
        "additionalProperties": False,
    }
    return {
        "type": "object",
        "properties": {
            "days": {"type": "array", "items": day},
            "summary": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["days", "summary"],
        "additionalProperties": False,
    }


def vocabulary_note(note) -> str:
    """A free-text note (the CSV contract's) as one of NOTE_VALUES, or ""
    when it is not one: the CSV fallback must not put the model's own words
    on an employee's schedule either (PR-15)."""
    n = " ".join(str(note or "").split()).strip(" .;-–—").lower()
    return n if n in NOTE_VALUES else ""


def salvage_json(text):
    """(obj, open_stack) for an answer that is not whole JSON — the answer
    cut short (stop_reason max_tokens or model_context_window_exceeded).
    The text is cut back to the last point where every value before it is
    complete — just after a container opens, after an array's element ends,
    or after an object's value closes — and the containers still open there
    are closed. `open_stack` is what was open at that cut ("{" / "["), so
    the caller knows which group the cut fell inside. (None, ()) when not
    even the opening brace is there."""
    s = text or ""
    start = s.find("{")
    if start < 0:
        return None, ()
    stack, safe = [], None
    in_str = esc = False
    for i in range(start, len(s)):
        ch = s[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
                if stack and stack[-1] == "[":
                    safe = (i + 1, tuple(stack))       # a string element of an array ended
            continue
        if ch == '"':
            in_str = True
        elif ch in "{[":
            stack.append(ch)
            safe = (i + 1, tuple(stack))               # an empty container is complete
        elif ch in "}]":
            if not stack:
                break
            stack.pop()
            if not stack:
                try:
                    return json.loads(s[start:i + 1]), ()
                except ValueError:
                    return None, ()
            safe = (i + 1, tuple(stack))               # an element or a value just closed
    if safe is None:
        return None, ()
    pos, open_ = safe
    closers = "".join("}" if c == "{" else "]" for c in reversed(open_))
    try:
        return json.loads(s[start:pos] + closers), open_
    except ValueError:
        return None, ()


def _row_hours(start_m, end_m) -> float:
    span = end_m - start_m
    if span < 0:
        span += 24 * 60           # a close past midnight
    return round(span / 60.0, 2)


def _recover_broken(text):
    """(obj, open_stack) for an answer that was NOT cut short but is still
    not whole JSON — a malformed value partway (AI cost audit 10/7/26 #71):
    the text is read up to the point the JSON broke and salvaged there
    (salvage_json), so every day written whole before the break is kept and
    only the rest is asked for again. (None, ()) when nothing before the
    break is readable."""
    s = text or ""
    start = s.find("{")
    if start < 0:
        return None, ()
    try:
        json.JSONDecoder().raw_decode(s, start)
        return None, ()          # whole JSON from the brace: salvage_json reads it
    except ValueError as e:
        pos = getattr(e, "pos", None)
    if not isinstance(pos, int) or pos <= start:
        return None, ()
    obj, open_ = salvage_json(s[:pos])
    if obj is None:
        return None, ()
    return obj, open_


def _row_field(sh, base, field):
    """A row's `field` under the contract it was written in, read leniently:
    a compact answer that wrote a full key name still reads."""
    keys = ROW_KEYS.get(base) or ROW_KEYS["schema"]
    if keys.get(field) in sh:
        return sh.get(keys[field])
    return sh.get(field)


def parse_answer(raw, dates=None, contract=None) -> dict:
    """The model's JSON answer as the pipeline's rows.

    {"rows": [{date, day, employee, role, shift_start, shift_end,
    scheduled_hours, notes}], "summary": [str], "complete_dates": [...],
    "partial_dates": [...], "salvaged": bool, "recovered": bool,
    "parsed": bool, "problems": [str], "dropped": [str], "answer_chars":
    int, "contract": str}.

    `contract` is the one the call asked for (CONTRACTS; a "plain_" form
    reads the same): "schema" and "compact" rows carry their employee;
    "shape" slots become `count` rows with employee "" each, carrying
    "_slot": True — code assigns the names (schedule_engine.
    assign_shape_slots). `dates` (the dates this call was asked for) keeps
    rows to them. A day the answer was cut off inside is in partial_dates
    and contributes no rows — half a day staffed is not a day written; the
    caller regenerates it. A row that starts and ends at the same time is
    dropped and named in `dropped`. Anything that is not JSON yields no
    rows: a structured answer is never read as CSV (PR-28) — garbage lines
    used to become rows.

    An answer that was not cut short but whose JSON broke partway is read
    up to the break (`recovered`, AI cost audit 10/7/26 #71): the days
    written whole before it are kept, the day it broke inside is partial,
    and only the days still missing are written again — it used to read as
    no days at all, and the whole call was paid for twice."""
    base = contract_base(contract)
    rows_key = DAY_ROWS_KEY[base]
    text = (raw or "").strip()
    out = {"rows": [], "summary": [], "complete_dates": [], "partial_dates": [], "salvaged": False,
           "recovered": False, "parsed": False, "problems": [], "dropped": [], "answer_chars": len(text),
           "contract": base}
    obj, open_ = None, ()
    if text:
        try:
            obj = json.loads(text)
        except ValueError:
            obj, open_ = salvage_json(text)
            if obj is None:
                obj, open_ = _recover_broken(text)
                out["recovered"] = obj is not None
            out["salvaged"] = obj is not None
    if not isinstance(obj, dict):
        out["problems"].append("the answer was not the JSON the schema describes")
        return out
    out["parsed"] = True
    days = obj.get("days") if isinstance(obj.get("days"), list) else []
    # Which day group the cut fell inside: open containers root{ days[ day{
    # shifts[ means the last day's shifts were still being written; root{
    # days[ day{ means its object was open — partial unless its shifts had
    # closed before the cut.
    partial_index = None
    if out["salvaged"] and len(open_) >= 3 and open_[:2] == ("{", "[") and days:
        last = days[-1] if isinstance(days[-1], dict) else {}
        if len(open_) >= 4 or rows_key not in last:
            partial_index = len(days) - 1
    wanted = set(dates or ())
    for idx, day in enumerate(days):
        if not isinstance(day, dict):
            continue
        d = iso_date_of(day.get("date")) or str(day.get("date") or "").strip()[:10]
        day_rows = day.get(rows_key)
        try:
            weekday = _datetime.strptime(d, "%Y-%m-%d").strftime("%A")
        except ValueError:
            if day_rows:
                out["problems"].append(f"a day with no readable date ({len(day_rows or [])} shifts)")
            continue
        if wanted and d not in wanted:
            continue
        if idx == partial_index:
            if d not in out["partial_dates"]:
                out["partial_dates"].append(d)
            continue
        if d not in out["complete_dates"]:
            out["complete_dates"].append(d)
        for sh in day_rows or []:
            if not isinstance(sh, dict):
                continue
            if base == "shape":
                emp, count = "", _slot_count(_row_field(sh, base, "count"))
            else:
                emp, count = " ".join(str(_row_field(sh, base, "employee") or "").split()), 1
                if not emp:
                    continue
            raw_s, raw_e = _row_field(sh, base, "start"), _row_field(sh, base, "end")
            s_m, e_m = _minutes(raw_s), _minutes(raw_e)
            start = _fmt_minutes(s_m) if s_m is not None else str(raw_s or "").strip()
            end = _fmt_minutes(e_m) if e_m is not None else str(raw_e or "").strip()
            role = " ".join(str(_row_field(sh, base, "role") or "").split())
            if s_m is not None and e_m is not None and s_m == e_m:
                out["dropped"].append(f"{d} {emp or role}: starts and ends at {start}")
                continue
            hours = _row_hours(s_m, e_m) if (s_m is not None and e_m is not None) else ""
            for _k in range(count):
                row = {"date": d, "day": weekday, "employee": emp, "role": role,
                       "shift_start": start, "shift_end": end,
                       "scheduled_hours": (f"{hours:g}" if hours != "" else ""),
                       "notes": vocabulary_note(_row_field(sh, base, "note"))}
                if base == "shape":
                    row["_slot"] = True
                out["rows"].append(row)
    out["summary"] = [" ".join(str(b).split()) for b in (obj.get("summary") or [])
                      if isinstance(b, str) and str(b).strip()]
    return out


def _slot_count(v) -> int:
    """A shape slot's people: an integer 1..SHAPE_MAX_COUNT (a missing or
    unreadable count is one person; a count over the cap is the cap)."""
    try:
        n = int(float(v))
    except (TypeError, ValueError):
        return 1
    return max(1, min(SHAPE_MAX_COUNT, n))


def complete_dates_so_far(text, contract=None, dates=None) -> list:
    """The dates an answer still being written has finished — what the
    Building screen shows as "N of 7 days drafted" while the call streams
    (AI cost audit 10/7/26 #36): parse_answer on the partial text, where the
    day being written is partial and every one before it complete. Never
    raises."""
    try:
        return list(parse_answer(text, dates=dates, contract=contract)["complete_dates"])
    except Exception:
        return []


# ── the days a streamed answer has finished (AI cost audit 10/7/26 #36) ────

class _WatchedStream:
    """A streamed answer read through exactly as the SDK reads it, each text
    delta handed to the watcher on the way. Everything else is the SDK's own
    stream (current_message_snapshot, close)."""

    def __init__(self, inner, on_text):
        self._inner = inner
        self._on_text = on_text
        self._consumed = False

    def __iter__(self):
        for event in self._inner:
            try:
                if getattr(event, "type", None) == "content_block_delta":
                    delta = getattr(event, "delta", None)
                    if getattr(delta, "type", None) == "text_delta":
                        self._on_text(getattr(delta, "text", "") or "")
            except Exception:
                pass
            yield event
        self._consumed = True

    def get_final_message(self):
        if not self._consumed:
            for _event in self:
                pass
        return self._inner.get_final_message()

    def __getattr__(self, name):
        return getattr(self._inner, name)


class _WatchedManager:
    def __init__(self, inner, on_text, on_begin):
        self._inner = inner
        self._on_text = on_text
        self._on_begin = on_begin

    def __enter__(self):
        # Each attempt is a new answer (create_with_retry sends the call
        # again after a failure): its text starts from nothing.
        self._on_begin()
        return _WatchedStream(self._inner.__enter__(), self._on_text)

    def __exit__(self, *exc):
        return self._inner.__exit__(*exc)


class _WatchedMessages:
    def __init__(self, inner, on_text, on_begin):
        self._inner = inner
        self._on_text = on_text
        self._on_begin = on_begin

    def stream(self, **kwargs):
        return _WatchedManager(self._inner.stream(**kwargs), self._on_text, self._on_begin)

    def __getattr__(self, name):
        return getattr(self._inner, name)


class DayWatch:
    """A client whose streamed schedule answer is watched as it is written:
    each time the answer starts a new date, the dates finished so far
    (complete_dates_so_far) go to `on_days(dates)` — once per newly finished
    day, so a seven-day answer is parsed about seven times, never once a
    token (AI cost audit 10/7/26 #36). Everything else is the wrapped
    client's — the call, its timeouts, its retries (ai_utils.
    create_with_retry is unchanged). A watcher that fails never fails the
    call: it is only the Building screen's count."""

    _DAY_KEY = '"date"'

    def __init__(self, client, on_days, contract=None, dates=None):
        self._client = client
        self._on_days = on_days
        self._contract = contract
        self._dates = list(dates or [])
        self._reported = set()
        self._begin()
        self.messages = _WatchedMessages(client.messages, self._take, self._begin)

    def __getattr__(self, name):
        return getattr(self._client, name)

    def _begin(self):
        self._text, self._tail, self._starts = [], "", 0

    def _take(self, piece):
        if not piece:
            return
        self._text.append(piece)
        # A day starts with its "date" key; the key can arrive split across
        # two deltas, so the last few characters are carried over.
        window = self._tail + piece
        n = window.count(self._DAY_KEY)
        self._tail = window[-(len(self._DAY_KEY) - 1):]
        if not n:
            return
        self._starts += n
        if self._starts < 2:
            return                  # the first day has only just begun
        done = complete_dates_so_far("".join(self._text), contract=self._contract, dates=self._dates)
        fresh = [d for d in done if d not in self._reported]
        if fresh:
            self._reported.update(fresh)
            try:
                self._on_days(sorted(self._reported))
            except Exception:
                pass

    @property
    def reported(self) -> list:
        return sorted(self._reported)


def csv_lines(rows) -> list:
    """Rows as the CSV lines (no header) the pipeline reads, in its one
    column order; a comma inside a field becomes ";" as everywhere else."""
    return [",".join(str(r.get(c, "") if r.get(c) is not None else "").replace(",", ";") for c in _CSV_COLS)
            for r in rows or []]


# ── what the finished week does not meet (PR-11) ───────────────────────────

_PART_WORDS = {"morning": "lunch/day", "night": "dinner/night"}

# The order an owner reads them in: the hard rules the week was built
# around first (schedule_rules' tiers), then targets and asks, then the
# budget, then what code cannot check.
_UNMET_RANK = {"manager": 0, "floor": 1, "owner_rule": 1, "closer": 1, "close": 1, "role_close": 1,
               "role_max": 2, "coverage": 2, "leadership": 3, "strength": 3, "station": 3, "ask": 4, "min_hours": 5,
               "budget": 6, "unchecked_rule": 7}


def _weekday(d) -> str:
    try:
        return _datetime.strptime(str(d)[:10], "%Y-%m-%d").strftime("%A")
    except ValueError:
        return ""


def _item(kind, what, why, date="", daypart="", source="rules", **extra):
    out = {"kind": kind, "date": date or "", "day": _weekday(date) if date else "",
           "daypart": daypart or "", "what": what, "why": why, "source": source}
    out.update({k: v for k, v in extra.items() if v is not None})
    return out


def unmet_items(rows, constraints=None, violations=None, quality=None, soft_requirements=None,
                hours_budget=None, station_gaps=None, owner_rules_unchecked=None) -> list:
    """Everything the finished week does not meet, worked out from the rows
    the owner sees (schedule audit 10/3/26 PR-11): [{kind, date, day,
    daypart, what, why, source, …}].

    The prompt asked the model to report misses "in the summary" from eleven
    places — strength targets it could not clear, leader rules, the trim to
    the ceiling, staffing asks not applied — against a summary of three
    ten-word bullets, two per slice. What was promised was silently dropped,
    and what the model reports is its own draft before the repairs. These
    are read from the final rows instead:

    - `violations` (schedule_rules.violations on the final rows): a minute
      with no manager, a floor short, the owner's rule, a closer or anybody
      until close, a role's stay past close, nobody on the roster managing;
    - `constraints`: each person's minimum hours (somebody with no shift at
      all included — the sweep only sees people with a row);
    - `quality` (shift_quality.score_rows on the final rows): a position
      the shift requires left unfilled, a strength target missed, a leader
      rule or "somebody able to run it" missed;
    - `soft_requirements` (staffing_signals.applied): an ask the rows do
      not carry;
    - `hours_budget`: the week over its hourly ceiling;
    - `station_gaps` (station_report's gaps) and `owner_rules_unchecked`
      (the owner's rules no code can check).

    A shortage the owner's floor names is said once, as the floor."""
    from schedule_rules import daypart_of
    out, seen_floor = [], set()
    for v in violations or []:
        kind, d = v.get("kind"), v.get("date") or ""
        detail = v.get("detail") or v.get("label") or ""
        if kind == "no_manager":
            gs = v.get("gap_start")
            part = daypart_of(_fmt_minutes(gs)) if isinstance(gs, int) else ""
            out.append(_item("manager", "A manager on the floor", detail[:1].upper() + detail[1:], d,
                             part if part in _PART_WORDS else "", minutes=v.get("minutes")))
        elif kind == "no_manager_roster":
            out.append(_item("manager", "A manager on the floor", detail[:1].upper() + detail[1:]))
        elif kind == "coverage_floor":
            role, part = v.get("floor_role") or (v.get("role") or "").lower(), v.get("daypart") or ""
            seen_floor.add((d, part, role.strip().lower()))
            out.append(_item("floor", f"Your {_role_label(v, role)} floor",
                             detail[:1].upper() + detail[1:], d, part, short=v.get("severity")))
        elif kind == "owner_rule":
            out.append(_item("owner_rule", "Your standing rule", detail[:1].upper() + detail[1:], d,
                             short=v.get("severity")))
        elif kind == "keyholder_until_close":
            out.append(_item("closer", "A closer on until close", detail[:1].upper() + detail[1:], d, "night"))
        elif kind == "nobody_at_close":
            out.append(_item("close", "Somebody on until close", detail[:1].upper() + detail[1:], d, "night"))
        elif kind == "ends_before_role_close":
            out.append(_item("role_close", f"{v.get('role') or 'The role'} on past close",
                             detail[:1].upper() + detail[1:], d, "night"))
        elif kind == "over_role_max":
            # The most of a role on at once by the owner's hours & shift
            # rules (schedule re-audit 10/4/26 PROMPT-1).
            out.append(_item("role_max", f"Your {_role_label(v, v.get('floor_role') or '')} maximum",
                             detail[:1].upper() + detail[1:], d, over=v.get("severity")))
    c = constraints
    if c is not None:
        hours = {}
        for r in rows or []:
            key = (r.get("employee") or "").strip().lower()
            if key:
                try:
                    hours[key] = hours.get(key, 0.0) + float(r.get("scheduled_hours") or 0)
                except (TypeError, ValueError):
                    pass
        for name in sorted(getattr(c, "roster_names", None) or [], key=str.lower):
            mn = c.min_hours(name)
            have = round(hours.get(name.strip().lower(), 0.0), 2)
            if mn and have + 0.05 < mn:
                full = (getattr(c, "employment", {}) or {}).get(name.strip().lower()) == "full"
                out.append(_item("min_hours", f"{name}'s minimum hours",
                                 f"{have:g}h this week, wants at least {mn:g}h"
                                 + (" — full-time" if full else ""),
                                 source="rules", employee=name, short=round(mn - have, 2), full_time=full))
    for s in ((quality or {}).get("shifts") or []):
        if not s.get("scored"):
            continue
        d, part = s.get("date") or "", s.get("daypart") or ""
        when = f"{s.get('day') or _weekday(d)} {_PART_WORDS.get(part, part)}".strip()
        for dim in s.get("dimensions") or []:
            facts = dim.get("facts") or {}
            if dim.get("key") == "coverage":
                for role, short in sorted((facts.get("short") or {}).items()):
                    if (d, part, str(role).strip().lower()) in seen_floor or not short:
                        continue
                    gap = next((g for g in facts.get("gaps") or []
                                if str(g).lower().startswith(f"{str(role).lower()} short")), "")
                    out.append(_item("coverage", f"{role} on {when}",
                                     (gap[:1].upper() + gap[1:]) if gap else f"Short {int(short)}", d, part,
                                     source="quality", short=int(short)))
            elif dim.get("key") == "operational_strength":
                for miss in facts.get("shortfalls") or []:
                    out.append(_item("strength", f"{miss.get('role')} strength target on {when}",
                                     f"{float(miss.get('strength') or 0):g} against a target of "
                                     f"{float(miss.get('target') or 0):g}", d, part, source="quality"))
            elif dim.get("key") == "leadership":
                for miss in facts.get("misses") or []:
                    out.append(_item("leadership", f"Needs {miss.get('rule')} on {when}",
                                     f"Found {int(miss.get('found') or 0)}", d, part, source="quality"))
                if facts.get("profile_leader_missing"):
                    out.append(_item("leadership", f"Somebody able to run {when}",
                                     "Nobody on it is authorized to close or rated "
                                     f"{float(facts.get('leader_min_score') or 0):g} or above", d, part,
                                     source="quality"))
    for r in soft_requirements or []:
        if r.get("applied"):
            continue
        part = r.get("daypart") or ""
        who = "the reviews diagnosis" if r.get("source") != "dsr" else "a nightly report"
        typ = r.get("typical")
        out.append(_item("ask", f"+1 {r.get('role') or 'person'} on {r.get('day') or _weekday(r.get('date'))} "
                                f"{_PART_WORDS.get(part, 'all day')}",
                         f"Asked by {who}; the draft has {int(r.get('scheduled') or 0)}"
                         + (f", the usual is {typ:g}" if isinstance(typ, (int, float)) else ""),
                         r.get("date") or "", part, source="asks"))
    try:
        budget = float(hours_budget or 0)
    except (TypeError, ValueError):
        budget = 0.0
    if budget > 0:
        from schedule_rules import hourly_hours
        have = hourly_hours(rows, c) if c is not None else sum(
            float(r.get("scheduled_hours") or 0) for r in rows or [] if str(r.get("scheduled_hours") or "").strip())
        if have > budget * 1.02:
            out.append(_item("budget", "The weekly hours ceiling",
                             f"{have:,.0f}h scheduled against {budget:,.0f}h — {have - budget:,.0f}h over",
                             source="budget", over=round(have - budget, 1)))
    for g in station_gaps or []:
        part = g.get("daypart") or ""
        out.append(_item("station", f"{g.get('station')} station",
                         f"Nobody trained on it could be scheduled {g.get('day') or _weekday(g.get('date'))} "
                         f"{_PART_WORDS.get(part, part)}".strip(), g.get("date") or "", part, source="stations"))
    for txt in owner_rules_unchecked or []:
        out.append(_item("unchecked_rule", f"Your rule “{str(txt)[:120]}”",
                         "Cavnar AI can't check this one automatically — check the draft against it",
                         source="owner_rules"))
    out.sort(key=lambda x: (_UNMET_RANK.get(x["kind"], 9), x["date"] or "9999", x["daypart"], x["what"]))
    return out


def week_review_extras(restaurant_id, rows, constraints, violations=None, quality=None, typical=None,
                       history_id=None, db_path=None) -> dict:
    """{"unmet", "soft_requirements"} for a week someone is editing (the
    re-score and re-check routes), so the review says what the week misses
    however it got to its rows: the generation's own hours budget and
    staffing asks — stored with the week — read against the rows as they
    stand now (staffing_signals.applied), the stations, and the owner's
    rules no code can check. The asks ride on in the review an edit saves,
    so a second edit still has them."""
    budget, asks = None, []
    if history_id:
        conn = get_conn(db_path)
        try:
            row = conn.execute("SELECT hours_budget, review_json FROM schedule_history WHERE id=? AND restaurant_id=?",
                               (history_id, restaurant_id)).fetchone()
        finally:
            conn.close()
        if row:
            budget = row["hours_budget"]
            try:
                asks = (json.loads(row["review_json"] or "{}") or {}).get("soft_requirements") or []
            except (TypeError, ValueError):
                asks = []
    if asks:
        # Judged against the usual crew the generation judged them by (each
        # ask keeps its own `typical`), today's typical where it kept none.
        import staffing_signals
        merged = {k: dict(v) for k, v in (typical or {}).items()}
        for a in asks:
            t = a.get("typical")
            if isinstance(t, (int, float)) and a.get("day") and a.get("daypart") and a.get("role"):
                merged.setdefault((a["day"], a["daypart"]), {})[a["role"]] = t
        asks = staffing_signals.applied(asks, rows, merged or None)
    gaps = None
    if getattr(constraints, "stations", None):
        from schedule_engine import station_report
        gaps = (station_report(rows, constraints, list(constraints.week_dates or [])) or {}).get("gaps")
    return {"unmet": unmet_items(rows, constraints=constraints, violations=violations, quality=quality,
                                 soft_requirements=asks, hours_budget=budget, station_gaps=gaps,
                                 owner_rules_unchecked=list(dict.fromkeys(
                                     list(getattr(constraints, "owner_rules_unchecked", None) or [])
                                     + list(getattr(constraints, "hours_rules_unchecked", None) or []))) or None),
            "soft_requirements": asks}


def _role_label(v, role) -> str:
    """The floor's role as a row spells it ("Line Cook"); the row the
    breach is pinned to may be any role's when nobody of the floor's was on."""
    r = (v.get("role") or "").strip()
    if r and r.lower() == str(role or "").strip().lower():
        return r
    return " ".join(w.capitalize() for w in str(role or "").split()) or "role"


# ── the record of every schedule call (PR-31) ──────────────────────────────

_CALLS_DDL = """CREATE TABLE IF NOT EXISTS schedule_model_calls (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at     TEXT    NOT NULL DEFAULT (datetime('now')),
    restaurant_id  INTEGER,
    generation_id  TEXT,
    history_id     INTEGER REFERENCES schedule_history(id) ON DELETE SET NULL,
    week_start     TEXT,
    dates_json     TEXT,
    ai_call_id     TEXT,
    model          TEXT,
    effort         TEXT,
    contract       TEXT,
    stop_reason    TEXT,
    outcome        TEXT,
    error          TEXT,
    seconds        REAL,
    usage_json     TEXT,
    rows           INTEGER,
    answer_chars   INTEGER,
    request_z      BLOB,
    inputs_z       BLOB,
    shared_z       BLOB,
    answer_z       BLOB,
    call_kind      TEXT,
    tier           TEXT
)"""

# What a call wrote (schedule re-audit 10/4/26 PROMPT-3): a whole week, a
# date or department slice of a fresh generation, an owner's redo of some
# days, or the quality gate's rewrite of the weakest ones. A small call
# carries the same thinking as a big one, so its tokens per row read high;
# the call planner models a call as a fixed cost plus a cost per row
# (call_costs) instead of one median over every kind.
CALL_KINDS = ("week", "slice", "redo", "gate")


def init_schedule_output(db_path=None):
    """schedule_model_calls and its indexes — at boot (models.init_db), never
    on a call."""
    conn = get_conn(db_path)
    try:
        conn.execute(_CALLS_DDL)
        cols = {r[1] for r in conn.execute("PRAGMA table_info(schedule_model_calls)").fetchall()}
        if "call_kind" not in cols:
            conn.execute("ALTER TABLE schedule_model_calls ADD COLUMN call_kind TEXT")
        # The route's tier on the labor_schedule ladder (T3, T4, or "default"
        # for the SCHEDULE_MODEL pin), so the eval and the learner compare
        # calls by tier (AI orchestration, 10/7/26).
        if "tier" not in cols:
            conn.execute("ALTER TABLE schedule_model_calls ADD COLUMN tier TEXT")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_sched_model_calls_created ON schedule_model_calls(created_at)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_sched_model_calls_gen ON schedule_model_calls(generation_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_sched_model_calls_hist ON schedule_model_calls(history_id)")
        conn.execute("CREATE INDEX IF NOT EXISTS idx_sched_model_calls_rid ON schedule_model_calls(restaurant_id, id)")
        conn.commit()
    finally:
        conn.close()


# The generator's arguments hold shapes JSON cannot carry: tuple keys (a
# borrowed headcount's (weekday, daypart)), hours as int keys (the sales
# curve), sets, tuples (the roster's pairs), ShiftProfile dataclasses. They
# are tagged so a replay rebuilds exactly what the call was given.
def encode(obj):
    if isinstance(obj, dict) or (hasattr(obj, "keys") and hasattr(obj, "__getitem__") and not isinstance(obj, str)):
        items = [(k, obj[k]) for k in obj.keys()]
        if all(isinstance(k, str) for k, _v in items) and not any(k.startswith("__") and k.endswith("__")
                                                                 for k, _v in items):
            return {k: encode(v) for k, v in items}
        return {"__map__": [[encode(k), encode(v)] for k, v in items]}
    if isinstance(obj, tuple):
        return {"__tuple__": [encode(x) for x in obj]}
    if isinstance(obj, (set, frozenset)):
        return {"__set__": sorted((encode(x) for x in obj), key=lambda x: json.dumps(x, sort_keys=True))}
    if isinstance(obj, list):
        return [encode(x) for x in obj]
    if dataclasses.is_dataclass(obj) and not isinstance(obj, type):
        fields = {f.name: getattr(obj, f.name) for f in dataclasses.fields(obj)}
        return {"__dataclass__": f"{type(obj).__module__}.{type(obj).__qualname__}", "fields": encode(fields)}
    if isinstance(obj, (_datetime, _date)):
        return obj.isoformat()
    if obj is None or isinstance(obj, (str, int, float, bool)):
        return obj
    return str(obj)


def decode(obj):
    if isinstance(obj, list):
        return [decode(x) for x in obj]
    if not isinstance(obj, dict):
        return obj
    if set(obj) == {"__map__"}:
        return {_hashable(decode(k)): decode(v) for k, v in obj["__map__"]}
    if set(obj) == {"__tuple__"}:
        return tuple(decode(x) for x in obj["__tuple__"])
    if set(obj) == {"__set__"}:
        return {_hashable(decode(x)) for x in obj["__set__"]}
    if set(obj) == {"__dataclass__", "fields"}:
        import importlib
        mod, _, name = obj["__dataclass__"].rpartition(".")
        cls = getattr(importlib.import_module(mod), name)
        return cls(**decode(obj["fields"]))
    return {k: decode(v) for k, v in obj.items()}


def _hashable(x):
    if isinstance(x, list):
        return tuple(_hashable(i) for i in x)
    return x


def _z(obj) -> bytes:
    return zlib.compress(json.dumps(obj, default=str, sort_keys=False).encode("utf-8", "replace"))


def _unz(blob):
    if not blob:
        return None
    return json.loads(zlib.decompress(blob).decode("utf-8"))


def _scrub(obj, names=()):
    """Every string in `obj` through ai_utils.redact_pii — a guest's contact
    details or labelled name in a memory line or a review's words — the
    trace's own rule. Staff names and the restaurant's figures stay: they
    are what a replay needs."""
    from ai_utils import redact_pii
    if isinstance(obj, str):
        return redact_pii(obj, names)
    if isinstance(obj, list):
        return [_scrub(x, names) for x in obj]
    if isinstance(obj, dict):
        return {k: _scrub(v, names) for k, v in obj.items()}
    return obj


# The generator's arguments that every call of one generation shares and
# that are large (the whole shift history): stored once, on the
# generation's first call.
SHARED_INPUTS = ("analysis", "shifts")


def record_call(restaurant_id, request, inputs=None, answer=None, generation_id=None, week_start=None,
                dates=None, ai_call_id=None, model=None, effort=None, contract=None, stop_reason=None,
                outcome=None, error=None, seconds=None, usage=None, rows=None, answer_chars=None,
                db_path=None, call_kind=None, tier=None):
    """Store one schedule call: the exact request (model, max_tokens, system,
    messages, thinking, output_config with its schema), the generator's
    arguments (`inputs`, encoded so a replay rebuilds them), and the answer
    text. Returns the row id. The caller (labor.generate_optimized_schedule)
    records a failure to store with ops.capture: a lost record is a week
    that cannot be replayed, never a failed generation."""
    from ai_utils import guest_names_in
    req = dict(request or {})
    prompt_text = json.dumps(req.get("messages") or [], default=str)
    names = guest_names_in(prompt_text)
    req = _scrub(encode(req), names)
    enc_inputs = encode(dict(inputs or {})) if inputs is not None else None
    shared = None
    if isinstance(enc_inputs, dict):
        # The shift history and the labor analysis are the restaurant's own
        # shift records and figures — no guest's words — kept as they are
        # (scrubbing six thousand rows cost most of a second for nothing).
        shared = {k: enc_inputs.pop(k) for k in SHARED_INPUTS if k in enc_inputs}
        enc_inputs = _scrub(enc_inputs, names)
    conn = get_conn(db_path)
    try:
        if shared and generation_id and conn.execute(
                "SELECT 1 FROM schedule_model_calls WHERE generation_id=? AND shared_z IS NOT NULL LIMIT 1",
                (generation_id,)).fetchone():
            shared = None                       # this generation's first call holds it
        cur = conn.execute(
            "INSERT INTO schedule_model_calls (restaurant_id, generation_id, week_start, dates_json, ai_call_id, "
            "model, effort, contract, stop_reason, outcome, error, seconds, usage_json, rows, answer_chars, "
            "request_z, inputs_z, shared_z, answer_z, call_kind, tier) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (restaurant_id, generation_id, week_start, json.dumps(list(dates or [])), ai_call_id, model, effort,
             contract, stop_reason, outcome, (str(error)[:500] if error else None), seconds,
             json.dumps(usage or {}), rows, answer_chars, _z(req),
             _z(enc_inputs) if enc_inputs is not None else None,
             _z(shared) if shared else None,
             zlib.compress(_scrub(str(answer), names).encode("utf-8", "replace")) if answer else None,
             call_kind if call_kind in CALL_KINDS else None, (str(tier)[:16] if tier else None)))
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def link_calls(restaurant_id, history_id, generation_id, db_path=None) -> int:
    """Key every call of `generation_id` to the schedule_history row its
    draft was saved as. Returns how many were linked."""
    if not (history_id and generation_id):
        return 0
    conn = get_conn(db_path)
    try:
        n = conn.execute("UPDATE schedule_model_calls SET history_id=? WHERE restaurant_id=? AND generation_id=? "
                         "AND history_id IS NULL", (history_id, restaurant_id, generation_id)).rowcount
        conn.commit()
        return n or 0
    finally:
        conn.close()


def load_calls(generation_id=None, history_id=None, db_path=None) -> list:
    """Every stored call of one generation (or of the generation a history
    row was saved from), oldest first, decoded: {id, created_at,
    restaurant_id, generation_id, history_id, week_start, dates, model,
    effort, contract, stop_reason, outcome, seconds, usage, rows,
    answer_chars, call_kind, tier, request, inputs (the shared history merged back in),
    answer}."""
    conn = get_conn(db_path)
    try:
        if history_id and not generation_id:
            row = conn.execute("SELECT generation_id FROM schedule_model_calls WHERE history_id=? "
                               "AND generation_id IS NOT NULL ORDER BY id LIMIT 1", (history_id,)).fetchone()
            generation_id = row["generation_id"] if row else None
        if not generation_id:
            return []
        rows = conn.execute("SELECT * FROM schedule_model_calls WHERE generation_id=? ORDER BY id",
                            (generation_id,)).fetchall()
    finally:
        conn.close()
    shared = {}
    for r in rows:
        if r["shared_z"]:
            shared = _unz(r["shared_z"]) or {}
            break
    out = []
    for r in rows:
        inputs = _unz(r["inputs_z"])
        if isinstance(inputs, dict):
            inputs = decode(dict(shared, **inputs))
        out.append({"id": r["id"], "created_at": r["created_at"], "restaurant_id": r["restaurant_id"],
                    "generation_id": r["generation_id"], "history_id": r["history_id"],
                    "week_start": r["week_start"], "dates": json.loads(r["dates_json"] or "[]"),
                    "model": r["model"], "effort": r["effort"], "contract": r["contract"],
                    "stop_reason": r["stop_reason"], "outcome": r["outcome"], "error": r["error"],
                    "seconds": r["seconds"], "usage": json.loads(r["usage_json"] or "{}"), "rows": r["rows"],
                    "answer_chars": r["answer_chars"], "call_kind": r["call_kind"] if "call_kind" in r.keys() else None,
                    "tier": r["tier"] if "tier" in r.keys() else None,
                    "request": decode(_unz(r["request_z"]) or {}),
                    "inputs": inputs,
                    "answer": zlib.decompress(r["answer_z"]).decode("utf-8") if r["answer_z"] else ""})
    return out


def generations(restaurant_ids=None, history_ids=None, limit=20, linked_only=True, db_path=None) -> list:
    """The stored generations, newest first: [{generation_id, restaurant_id,
    week_start, history_id, calls, created_at}]. The newest generation of
    each restaurant-week, unless `history_ids` names them."""
    where, args = ["generation_id IS NOT NULL"], []
    if linked_only:
        where.append("history_id IS NOT NULL")
    if restaurant_ids:
        where.append("restaurant_id IN (%s)" % ",".join("?" * len(restaurant_ids)))
        args += [int(r) for r in restaurant_ids]
    if history_ids:
        where.append("history_id IN (%s)" % ",".join("?" * len(history_ids)))
        args += [int(h) for h in history_ids]
    conn = get_conn(db_path)
    try:
        rows = conn.execute(
            "SELECT generation_id, restaurant_id, week_start, MAX(history_id) AS history_id, COUNT(*) AS calls, "
            "MIN(created_at) AS created_at, MAX(id) AS last_id FROM schedule_model_calls WHERE "
            + " AND ".join(where) + " GROUP BY generation_id ORDER BY last_id DESC", args).fetchall()
    finally:
        conn.close()
    out, seen = [], set()
    for r in rows:
        key = (r["restaurant_id"], r["week_start"])
        if not history_ids and key in seen:
            continue
        seen.add(key)
        out.append({k: r[k] for k in ("generation_id", "restaurant_id", "week_start", "history_id", "calls",
                                      "created_at")})
        if len(out) >= int(limit or 20):
            break
    return out


# A fit of a call's cost on its rows needs this many finished calls, and
# their row counts this far apart (the largest at least SPREAD x the
# smallest): calls all of one size say nothing about the fixed part.
FIT_MIN_CALLS = 3
FIT_MIN_SPREAD = 1.5


def _median(xs):
    xs = sorted(xs)
    if not xs:
        return None
    mid = len(xs) // 2
    return xs[mid] if len(xs) % 2 else (xs[mid - 1] + xs[mid]) / 2


def _fit(points):
    """(intercept, slope) of the least-squares line through [(rows, y)], or
    None when the rows do not spread far enough to tell a fixed cost from a
    cost per row."""
    pts = [(float(x), float(y)) for x, y in points if x and y is not None]
    if len(pts) < FIT_MIN_CALLS:
        return None
    xs = [x for x, _y in pts]
    if max(xs) < FIT_MIN_SPREAD * min(xs):
        return None
    n = len(pts)
    mx, my = sum(xs) / n, sum(y for _x, y in pts) / n
    var = sum((x - mx) ** 2 for x in xs)
    if var <= 0:
        return None
    slope = sum((x - mx) * (y - my) for x, y in pts) / var
    return my - slope * mx, slope


# A call's thinking is most of its cost, and the effort sets how much of it
# there is: one call at "high" (62,353 tokens for 215 rows, Simple EJ's
# 10/6/26) read as the cost of a row at "medium" planned the next week as
# two calls, each paying the whole input and its own thinking (AI cost
# audit 10/7/26). So the measured cost is read at the effort in force:
# `effort` None reads every call, "" the calls that ran without one (a
# model that does not think), anything else that effort only.
def _effort_where(where, args, effort):
    if effort is None:
        return
    if effort == "":
        where.append("(effort IS NULL OR effort='')")
    else:
        where.append("effort=?")
        args.append(effort)


def call_costs(restaurant_id=None, model=None, days=60, db_path=None, effort=None, contract=None) -> dict:
    """What a schedule call really costs, from the stored calls of the last
    `days` that finished (end_turn) on the structured contract (schedule
    re-audit 10/4/26 PROMPT-3, PROMPT-4) — `contract`'s own calls, with and
    without enums (contract_family; the schema's when None): a compact row
    costs less than a schema row, so one contract is never sized from
    another's calls (AI cost audit 10/7/26 #70). A call whose broken JSON
    was read up to the break (outcome 'salvaged', #71) is left out: its
    tokens bought days it did not keep.

    Adaptive thinking is mostly a cost per CALL — every call reads the same
    roster, rules and week — so one median of output tokens per row over
    every call read a one-day gate rewrite as a very expensive row and split
    every later week that fitted one call. A call is modelled as
        output_tokens = fixed + per_row x rows,   seconds = s_fixed + s_row x rows
    fitted over the restaurant's calls once FIT_MIN_CALLS of them spread
    over FIT_MIN_SPREAD x in rows ("fit"); before that, the median tokens a
    row over whole-week and slice calls only, the kinds a plan is made of
    ("comparable"); before any call, None ("estimate" — the caller's
    constants). Seconds: the fit, else output tokens over seconds measured
    across the calls (`tokens_per_second`), else None.

    {"fixed", "per_row", "source", "calls", "seconds_fixed",
    "seconds_per_row", "seconds_source", "tokens_per_second", "by_kind":
    {kind: {"calls", "rows", "tokens_per_row", "tokens_per_call",
    "seconds", "tokens_per_second", "cache_read_tokens"}}} — by_kind keys
    are CALL_KINDS, and "unknown" for calls recorded before kinds were."""
    where, args = ["stop_reason='end_turn'", "rows > 0", "contract IN (?, ?)",
                   "COALESCE(outcome, '') <> 'salvaged'",
                   "created_at >= datetime('now', ?)"], list(contract_family(contract)) + [f"-{int(days)} days"]
    if restaurant_id:
        where.append("restaurant_id=?")
        args.append(restaurant_id)
    if model:
        where.append("model=?")
        args.append(model)
    _effort_where(where, args, effort)
    conn = get_conn(db_path)
    try:
        rows = conn.execute("SELECT usage_json, rows, seconds, call_kind FROM schedule_model_calls WHERE "
                            + " AND ".join(where) + " ORDER BY id DESC LIMIT 200", args).fetchall()
    finally:
        conn.close()
    calls = []
    for r in rows:
        try:
            usage = json.loads(r["usage_json"] or "{}") or {}
        except (TypeError, ValueError):
            usage = {}
        out_tokens = int(usage.get("output_tokens") or 0)
        if not out_tokens:
            continue
        calls.append({"rows": int(r["rows"]), "tokens": out_tokens, "seconds": float(r["seconds"] or 0) or None,
                      "kind": r["call_kind"] if r["call_kind"] in CALL_KINDS else "unknown",
                      "cache_read": int(usage.get("cache_read_tokens") or 0)})
    by_kind = {}
    for k in sorted({c["kind"] for c in calls}):
        mine = [c for c in calls if c["kind"] == k]
        timed = [c for c in mine if c["seconds"]]
        by_kind[k] = {
            "calls": len(mine), "rows": _median([c["rows"] for c in mine]),
            "tokens_per_row": round(_median([c["tokens"] / c["rows"] for c in mine]), 1),
            "tokens_per_call": _median([c["tokens"] for c in mine]),
            "seconds": _median([c["seconds"] for c in timed]),
            "tokens_per_second": (round(sum(c["tokens"] for c in timed) / sum(c["seconds"] for c in timed), 1)
                                  if timed else None),
            "cache_read_tokens": _median([c["cache_read"] for c in mine])}
    out = {"fixed": None, "per_row": None, "source": "estimate", "calls": len(calls), "seconds_fixed": None,
           "seconds_per_row": None, "seconds_source": "estimate", "tokens_per_second": None, "by_kind": by_kind}
    fit = _fit([(c["rows"], c["tokens"]) for c in calls])
    if fit and fit[1] > 0:
        out.update(fixed=round(max(0.0, fit[0]), 1), per_row=round(fit[1], 2), source="fit")
    else:
        plan_kinds = [c for c in calls if c["kind"] in ("week", "slice")]
        if plan_kinds:
            out.update(fixed=0.0, per_row=round(_median([c["tokens"] / c["rows"] for c in plan_kinds]), 1),
                       source="comparable")
    timed = [c for c in calls if c["seconds"]]
    if timed:
        out["tokens_per_second"] = round(sum(c["tokens"] for c in timed) / sum(c["seconds"] for c in timed), 1)
    sfit = _fit([(c["rows"], c["seconds"]) for c in timed])
    if sfit and sfit[1] > 0:
        out.update(seconds_fixed=round(max(0.0, sfit[0]), 1), seconds_per_row=round(sfit[1], 3), seconds_source="fit")
    elif out["tokens_per_second"]:
        out["seconds_source"] = "rate"
    return out


def measured_tokens_per_row(restaurant_id=None, model=None, days=60, db_path=None, effort=None,
                            contract=None) -> dict:
    """What a row has really cost, from the stored calls of the last `days`
    that finished (end_turn) on the structured contract (`contract`'s
    family, as call_costs reads it): {"output_tokens_per_row"
    — every output token, thinking included, per row written (what a call's
    max_tokens must hold), "answer_chars_per_row", "calls", "source"
    ("measured" | "estimate")}. Medians; the estimate stands until a call
    has run."""
    where, args = ["stop_reason='end_turn'", "rows > 0", "contract IN (?, ?)",
                   "COALESCE(outcome, '') <> 'salvaged'",
                   "created_at >= datetime('now', ?)"], list(contract_family(contract)) + [f"-{int(days)} days"]
    if restaurant_id:
        where.append("restaurant_id=?")
        args.append(restaurant_id)
    if model:
        where.append("model=?")
        args.append(model)
    _effort_where(where, args, effort)
    conn = get_conn(db_path)
    try:
        rows = conn.execute("SELECT usage_json, rows, answer_chars FROM schedule_model_calls WHERE "
                            + " AND ".join(where) + " ORDER BY id DESC LIMIT 200", args).fetchall()
    finally:
        conn.close()
    per_row, chars = [], []
    for r in rows:
        try:
            out_tokens = int((json.loads(r["usage_json"] or "{}") or {}).get("output_tokens") or 0)
        except (TypeError, ValueError):
            out_tokens = 0
        if out_tokens:
            per_row.append(out_tokens / r["rows"])
        if r["answer_chars"]:
            chars.append(r["answer_chars"] / r["rows"])
    if not per_row:
        return {"output_tokens_per_row": answer_tokens_per_row(contract), "answer_chars_per_row": None,
                "calls": 0, "source": "estimate"}

    def _median(xs):
        xs = sorted(xs)
        mid = len(xs) // 2
        return xs[mid] if len(xs) % 2 else (xs[mid - 1] + xs[mid]) / 2
    return {"output_tokens_per_row": round(_median(per_row), 1),
            "answer_chars_per_row": round(_median(chars), 1) if chars else None,
            "calls": len(per_row), "source": "measured"}
