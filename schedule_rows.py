"""
schedule_rows.py — the one format of a week's rows.

A week was an 8-column positional CSV written by four writers and read by
nine parsers, none of which quoted anything: a comma in a note became ";",
a newline in a note split the line and the tail was silently dropped, and a
row had no identity beyond its spelling — (date, name, start) — so every
feature downstream re-discovered a shift by a tuple that moves when the
shift does. This module is the format, and the only place it is written:

* STORED — what schedule_history.schedule_csv / published_csv and every
  schedule_versions row hold: the eight columns every reader knows, then
  `shift_id` (uuid4 hex, kept by a shift across edits, versions, swaps and
  covers; a new shift gets a new one), `person_id` (people.id of the person
  the name means, when it means exactly one) and `locked` ("1": the owner
  pinned the row and every pass, the solver and a regenerate keep it
  exactly — contract #4). Written with csv.writer, so commas and quotes
  survive; every field is one line (newlines become spaces at the boundary).
* ANSWER — the model pipeline's internal 8-column text (the answer parsed
  by schedule_engine and schedule_skeleton): commas in a field become ";"
  and nothing is quoted, as that parser expects. Never stored.
* EXPORT — the owner's download: the eight columns, quoted, no ids.

Pure (L0): no data access. Person ids are stamped by schedule_versions.
"""
import csv
import io
import re
import uuid

COLS = ("date", "day", "employee", "role", "shift_start", "shift_end", "scheduled_hours", "notes")
ID_COLS = ("shift_id", "person_id", "locked")
STORED_COLS = COLS + ID_COLS
HEADER = ",".join(COLS)
STORED_HEADER = ",".join(STORED_COLS)

_BREAKS = re.compile(r"[\r\n\t\v\f\x85  ]+")
_HEX_ID = re.compile(r"^[0-9a-f]{32}$")


def clean(value) -> str:
    """One field as stored: a line break or tab inside it becomes a space
    (a note "bring keys\\nthen lock patio, back door" keeps every word and
    the comma), outer space trimmed. None is ""."""
    if value is None:
        return ""
    return _BREAKS.sub(" ", str(value)).strip()


def new_id() -> str:
    """A fresh shift id."""
    return uuid.uuid4().hex


def valid_id(value) -> bool:
    return bool(_HEX_ID.match(str(value or "").strip().lower()))


def is_locked(row) -> bool:
    """Whether a row is locked: True, 1, "1", "true", "yes"."""
    v = (row or {}).get("locked") if isinstance(row, dict) else row
    if isinstance(v, bool):
        return v
    return str(v or "").strip().lower() in ("1", "true", "yes", "y", "on")


def _field(row, col) -> str:
    if col == "locked":
        return "1" if is_locked(row) else ""
    if col == "person_id":
        v = row.get("person_id")
        try:
            return str(int(v)) if v not in (None, "") else ""
        except (TypeError, ValueError):
            return ""
    if col == "shift_id":
        v = clean(row.get("shift_id")).lower()
        return v if valid_id(v) else ""
    return clean(row.get(col))


def parse(text) -> list:
    """The rows of a stored week (or any CSV with this header), every
    STORED column a string ("" when the text had none) — a row with no
    employee is not a shift and is left out. Reads the legacy 8-column text
    the same way."""
    out = []
    for r in csv.DictReader(io.StringIO(text or "")):
        if (r.get("employee") or "").strip():
            out.append({c: (r.get(c) or "").strip() for c in STORED_COLS})
    return out


def to_csv(rows) -> str:
    """Rows as the stored text: the STORED header, then each row, quoted
    where a field needs it. No trailing newline (the byte shape every
    writer used)."""
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(STORED_COLS)
    for r in rows or []:
        w.writerow([_field(r, c) for c in STORED_COLS])
    return buf.getvalue().rstrip("\n")


def export_rows(text) -> list:
    """[[header...], [row...]] — the owner's download: the eight columns of
    every row as stored, no ids."""
    return [list(COLS)] + [[r.get(c, "") for c in COLS] for r in parse(text)]


def answer_line(row) -> str:
    """One row as the model pipeline's internal line (ANSWER): eight
    columns, a comma in a field becomes ";", one line."""
    return ",".join(clean(row.get(c) if row.get(c) is not None else "").replace(",", ";") for c in COLS)


def answer_csv(rows) -> str:
    """Rows as the model pipeline's internal text, header first."""
    return "\n".join([HEADER] + [answer_line(r) for r in rows or []])


def _fold(name) -> str:
    return " ".join(str(name or "").split()).casefold()


def key(row) -> tuple:
    """(date, folded name, start) — how a shift was found before it had an id."""
    return (str(row.get("date") or "")[:10], _fold(row.get("employee")), str(row.get("shift_start") or "").strip())


def carry_ids(rows, prior_rows, known_ids=None) -> list:
    """`rows` (copies), each carrying the shift id it keeps. A row keeps:

    1. its own id when the week already had it (in `prior_rows`, or in
       `known_ids` — every id an earlier version of the week held) and no
       earlier row in this list took it;
    2. else the id of the prior row with its exact key (date, person,
       start) — a client that dropped ids (an older app) loses none;
    3. else the id of the prior row in its slot (date, role, start, end)
       whose person changed — a reassign is the same shift;
    4. else the id of the one prior row of its person on its date — a
       retime is the same shift;
    5. else a new id.

    An id is used once per week (a copied row gets its own). Pure."""
    prior = [r for r in prior_rows or [] if valid_id(r.get("shift_id"))]
    known = {str(r["shift_id"]).lower() for r in prior} | {str(i).lower() for i in known_ids or [] if valid_id(i)}
    out = [dict(r) for r in rows or []]
    used = set()
    for r in out:
        sid = str(r.get("shift_id") or "").strip().lower()
        if valid_id(sid) and sid in known and sid not in used:
            r["shift_id"] = sid
            used.add(sid)
        else:
            r["shift_id"] = ""

    def _take(matchers):
        for match in matchers:
            free = [p for p in prior if p["shift_id"].lower() not in used]
            if not free:
                return
            index = {}
            for p in free:
                index.setdefault(match(p), []).append(p)
            for r in out:
                if r["shift_id"]:
                    continue
                cands = [p for p in index.get(match(r), []) if p["shift_id"].lower() not in used]
                if cands:
                    sid = cands[0]["shift_id"].lower()
                    r["shift_id"] = sid
                    used.add(sid)

    def _slot(r):
        return (str(r.get("date") or "")[:10], str(r.get("role") or "").strip().lower(),
                str(r.get("shift_start") or "").strip(), str(r.get("shift_end") or "").strip())

    def _person_day(r):
        return (str(r.get("date") or "")[:10], _fold(r.get("employee")))

    _take([key, _slot, _person_day])
    for r in out:
        if not r["shift_id"]:
            r["shift_id"] = new_id()
            used.add(r["shift_id"])
    return out


def by_id(rows) -> dict:
    """{shift_id: row} for the rows that have one."""
    return {str(r.get("shift_id")).lower(): r for r in rows or [] if valid_id(r.get("shift_id"))}


# The fields a change to a live shift carries into the working copy.
MERGE_FIELDS = ("date", "day", "employee", "role", "shift_start", "shift_end", "scheduled_hours", "notes",
                "person_id")


def merge_live_change(draft_rows, old_live, new_live) -> list:
    """The working copy (`draft_rows`) with the change from `old_live` to
    `new_live` — a staff swap, cover or claim on the week staff have —
    applied to it shift by shift (by shift_id), three-way: a field the
    draft still holds as it was live takes the new value; a field the
    manager changed in the draft (an unsent edit) keeps the manager's
    value. A shift the swap added is added (once); one it removed goes from
    the draft when the draft still holds it as it was. Rows without ids are
    matched by key. Pure; returns new rows."""
    old_by = by_id(old_live)
    new_by = by_id(new_live)
    draft = [dict(r) for r in draft_rows or []]
    draft_by = by_id(draft)
    for sid, new in new_by.items():
        old = old_by.get(sid)
        cur = draft_by.get(sid)
        if old is None:
            if cur is None:
                row = dict(new)
                draft.append(row)
                draft_by[sid] = row
            continue
        if cur is None:
            continue            # the manager removed it from the draft: their unsent change stands
        for f in MERGE_FIELDS:
            if str(new.get(f) or "") != str(old.get(f) or "") and str(cur.get(f) or "") == str(old.get(f) or ""):
                cur[f] = new.get(f) or ""
    gone = [sid for sid in old_by if sid not in new_by]
    if gone:
        keep = []
        for r in draft:
            sid = str(r.get("shift_id") or "").lower()
            if sid in gone and all(str(r.get(f) or "") == str(old_by[sid].get(f) or "") for f in COLS):
                continue
            keep.append(r)
        draft = keep
    return draft


def person_rows_sig(rows, name_key) -> list:
    """One person's shifts as a comparable value (what they were told)."""
    return sorted((r.get("date", ""), r.get("shift_start", ""), r.get("shift_end", ""),
                   (r.get("role") or "").strip().lower())
                  for r in rows or [] if _fold(r.get("employee")) == name_key)


def people_changed(before_rows, after_rows) -> dict:
    """{"changed": [names whose shifts differ, the removed included],
    "added": [of those, the names with no shift before], "removed": [names
    with no shift after], "dates": [iso dates whose shifts differ]} —
    between what staff were told and the week about to go out."""
    names = {}
    for r in list(before_rows or []) + list(after_rows or []):
        n = " ".join(str(r.get("employee") or "").split())
        if n:
            names.setdefault(n.casefold(), n)
    changed, added, removed, dates = [], [], [], set()
    for k, n in sorted(names.items()):
        was, now = person_rows_sig(before_rows, k), person_rows_sig(after_rows, k)
        if was != now:
            changed.append(n)
            if not was:
                added.append(n)
            if not now:
                removed.append(n)
            dates |= {s[0] for s in set(was) ^ set(now) if s[0]}
    return {"changed": changed, "added": added, "removed": removed, "dates": sorted(dates)}
