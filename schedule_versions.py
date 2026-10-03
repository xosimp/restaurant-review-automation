"""
schedule_versions.py — every state a schedule has been in, and what the
manager keeps changing.

A manager's edit used to overwrite the one schedule_history row: the draft
was gone, "what did I change" had no answer, and nothing could learn from
the edits. Now every save appends a version (generated, edited, fixes,
published) with a diff against the one before, and the recurring edits —
the same person moved off the same weekday three weeks running — are read
back into the next draft.
"""
import csv
import io
import json
import logging
import math
import re
import sqlite3
from datetime import date, datetime, timedelta

import models as _models_mod
from models import DB_PATH


def get_conn(db_path=None):
    """models.get_conn, resolved at call time (CLAUDE.md, bound imports). A
    bound copy — and a db_path default bound to DB_PATH — sent a test's
    patched models.get_conn to the real database. The module's own DB_PATH
    default means "whatever models uses now"."""
    if db_path is None or db_path == DB_PATH:
        return _models_mod.get_conn()
    return _models_mod.get_conn(db_path)

log = logging.getLogger(__name__)

COLS = ("date", "day", "employee", "role", "shift_start", "shift_end", "scheduled_hours", "notes")


def rows_from_csv(text) -> list:
    out = []
    for r in csv.DictReader(io.StringIO(text or "")):
        if (r.get("employee") or "").strip():
            out.append({c: (r.get(c) or "").strip() for c in COLS})
    return out


def _key(r):
    return (r.get("date", ""), (r.get("employee") or "").strip().lower(), r.get("shift_start", ""))


def canonical_rows(restaurant_id, row_lists, db_path=None, mapping=None, mapping_only=False):
    """Schedule rows with each employee spelled as the one live person the
    name means today (people.canonical_names): a week saved before "Bob S."
    was renamed "Bob Smith" reads as Bob Smith, so a learned pattern keys on
    the person, not a spelling (memory re-audit 9/29/26, INVENTORY-2).
    Copies — the stored CSVs are never rewritten here. With `mapping_only`
    returns the {name: canonical name} map instead; pass it back as
    `mapping` to apply it without another lookup. A name that means nobody
    or two people stays as written; a failed lookup changes nothing."""
    if mapping is None:
        names = sorted({(r.get("employee") or "").strip() for rows in row_lists or [] for r in rows or []
                        if (r.get("employee") or "").strip()})
        mapping = {}
        if names:
            try:
                import people as _people
                kw = {"db_path": db_path} if db_path and db_path != DB_PATH else {}
                mapping = _people.canonical_names(restaurant_id, names, **kw) or {}
            except Exception as e:
                log.warning("canonical names unavailable for restaurant %s: %s", restaurant_id, e)
                mapping = {}
    if mapping_only:
        return mapping
    out = []
    for rows in row_lists or []:
        cur = []
        for r in rows or []:
            name = (r.get("employee") or "").strip()
            if name and mapping.get(name) and mapping[name] != name:
                r = dict(r, employee=mapping[name])
            cur.append(r)
        out.append(cur)
    return out


def _hours(r):
    try:
        return float(r.get("scheduled_hours") or 0)
    except (TypeError, ValueError):
        return 0.0


def diff(before_rows: list, after_rows: list) -> dict:
    """What changed between two schedules, in the terms a manager uses.

    added / removed are whole shifts; moved is the same date, role and start
    with a different person; retimed is the same person and date with a
    different start or end. Hours by day and by role carry the totals.
    """
    before = {_key(r): r for r in before_rows}
    after = {_key(r): r for r in after_rows}
    added = [after[k] for k in after if k not in before]
    removed = [before[k] for k in before if k not in after]

    # A removed row and an added row on the same date, role and start with a
    # different name is one move, not two changes.
    moved, retimed = [], []
    used_added = set()
    for r in list(removed):
        for j, a in enumerate(added):
            if j in used_added:
                continue
            if a["date"] == r["date"] and a["shift_start"] == r["shift_start"] and \
               (a.get("role") or "").lower() == (r.get("role") or "").lower() and \
               (a.get("employee") or "").lower() != (r.get("employee") or "").lower():
                moved.append({"date": r["date"], "day": r.get("day"), "role": r.get("role"),
                              "shift_start": r["shift_start"], "from": r.get("employee"), "to": a.get("employee")})
                used_added.add(j)
                removed.remove(r)
                break
    for r in list(removed):
        for j, a in enumerate(added):
            if j in used_added:
                continue
            if a["date"] == r["date"] and (a.get("employee") or "").lower() == (r.get("employee") or "").lower():
                was, now = f"{r['shift_start']}–{r['shift_end']}", f"{a['shift_start']}–{a['shift_end']}"
                # "from"/"to" here are a shift's old and new times, not an
                # email header (tests/test_email_audit_fixes scans for those).
                retimed.append({"date": r["date"], "day": r.get("day"), "employee": r.get("employee"),
                                "from": was, "to": now, "hours_delta": round(_hours(a) - _hours(r), 1),
                                "role": a.get("role") or r.get("role"),
                                "old_start": r["shift_start"], "new_start": a["shift_start"],
                                "old_end": r.get("shift_end", ""), "new_end": a.get("shift_end", "")})
                used_added.add(j)
                removed.remove(r)
                break
    added = [a for j, a in enumerate(added) if j not in used_added]

    # Same person, date and start on both sides: a changed end is a retime
    # (it used to be no change at all, so the person was never told), and a
    # changed role is its own kind — the key cannot see either.
    role_changed = []
    for k in sorted(before.keys() & after.keys()):
        b, a = before[k], after[k]
        if (b.get("shift_end") or "") != (a.get("shift_end") or ""):
            was, now = f"{b['shift_start']}–{b.get('shift_end', '')}", f"{a['shift_start']}–{a.get('shift_end', '')}"
            retimed.append({"date": a["date"], "day": a.get("day"), "employee": a.get("employee"),
                            "from": was, "to": now,
                            "hours_delta": round(_hours(a) - _hours(b), 1), "role": a.get("role") or b.get("role"),
                            "old_start": b["shift_start"], "new_start": a["shift_start"],
                            "old_end": b.get("shift_end", ""), "new_end": a.get("shift_end", "")})
        if (b.get("role") or "").strip().lower() != (a.get("role") or "").strip().lower():
            role_changed.append({"date": a["date"], "day": a.get("day"), "employee": a.get("employee"),
                                 "shift_start": a["shift_start"], "old_role": b.get("role") or "",
                                 "new_role": a.get("role") or ""})

    def _by(rows, field):
        out = {}
        for r in rows:
            k = r.get(field) or ""
            out[k] = round(out.get(k, 0.0) + _hours(r), 1)
        return out

    hb, ha = _by(before_rows, "day"), _by(after_rows, "day")
    rb, ra = _by(before_rows, "role"), _by(after_rows, "role")
    return {
        "added": added, "removed": removed, "moved": moved, "retimed": retimed, "role_changed": role_changed,
        "hours_before": round(sum(_hours(r) for r in before_rows), 1),
        "hours_after": round(sum(_hours(r) for r in after_rows), 1),
        "hours_by_day": {d: [hb.get(d, 0.0), ha.get(d, 0.0)] for d in sorted(set(hb) | set(ha))},
        "hours_by_role": {r: [rb.get(r, 0.0), ra.get(r, 0.0)] for r in sorted(set(rb) | set(ra))},
        "changes": len(added) + len(removed) + len(moved) + len(retimed) + len(role_changed),
    }


def diff_lines(d: dict, limit=6, unchanged="Unchanged from the last published week.") -> list:
    """The diff as sentences. Deterministic — nothing here can describe a
    change that did not happen."""
    if not d:
        return []
    lines = []
    delta = round(d["hours_after"] - d["hours_before"], 1)
    if abs(delta) >= 0.5:
        lines.append(f"{'+' if delta > 0 else '−'}{abs(delta):g}h this week ({d['hours_before']:g}h → {d['hours_after']:g}h).")
    by_day = [(day, b, a) for day, (b, a) in d["hours_by_day"].items() if abs(a - b) >= 2]
    by_day.sort(key=lambda x: -abs(x[2] - x[1]))
    for day, b, a in by_day[:2]:
        lines.append(f"{day}: {b:g}h → {a:g}h.")
    by_role = [(r, b, a) for r, (b, a) in d["hours_by_role"].items() if r and abs(a - b) >= 2]
    by_role.sort(key=lambda x: -abs(x[2] - x[1]))
    for r, b, a in by_role[:2]:
        lines.append(f"{r}: {b:g}h → {a:g}h.")
    for m in d["moved"][:2]:
        lines.append(f"{m.get('day') or m['date']} {m.get('role') or ''} {m['shift_start']}: {m['from']} → {m['to']}.")
    for rc in (d.get("role_changed") or [])[:2]:
        lines.append(f"{rc.get('day') or rc['date']} {rc['employee']}: {rc['old_role'] or 'no role'} → {rc['new_role'] or 'no role'}.")
    if d.get("retimed") and not by_day:
        n = len(d["retimed"])
        lines.append(f"{n} shift{'s' if n != 1 else ''} retimed.")
    if d["added"] and not by_day:
        lines.append(f"{len(d['added'])} shift{'s' if len(d['added']) != 1 else ''} added.")
    if d["removed"] and not by_day:
        lines.append(f"{len(d['removed'])} shift{'s' if len(d['removed']) != 1 else ''} removed.")
    if not lines:
        lines.append(unchanged)
    return lines[:limit]


# ── versions ───────────────────────────────────────────────────────────────

def latest_version(conn, history_id) -> int:
    row = conn.execute("SELECT MAX(version) AS v FROM schedule_versions WHERE history_id=?", (history_id,)).fetchone()
    return int(row["v"] or 0) if row else 0


# saved_by on a version support saved through view-as.
SUPPORT_PREFIX = "support:"
# The learners' old filter: a week any support save touched taught nothing —
# one saved through view-as (saved_by SUPPORT_PREFIX, M1) or with an
# admin's authority (saved_authority, permissions.answer_authority, M3). No
# learner reads it now: an admin's changes are taken out of the week and
# the rest of the week still counts (learning_weeks, schedule audit 10/3/26
# L-8), and an adopted save counts as the owner's (learnable_version).
# Candidate for future cleanup after additional verification (still named
# by tests/test_mem_m1_kinds.py and DATABASE_SCHEMA.md).
NOT_SUPPORT_TOUCHED_SQL = ("history_id NOT IN (SELECT sv.history_id FROM schedule_versions sv "
                           "WHERE sv.saved_by LIKE 'support:%' OR sv.saved_authority = 'admin')")
# The same rule for one version as SQL (learnable_version is the reading
# the learners use, adoption included). Candidate for future cleanup after
# additional verification.
LEARNABLE_SQL = "COALESCE(saved_authority, '') <> 'admin' AND COALESCE(saved_by, '') NOT LIKE 'support:%'"


# The authority of a write no person made: the generator, a job, and the
# unattended automatic publish (schedule audit 10/3/26 L-7).
SYSTEM = "system"


def authority_of(user) -> str:
    """permissions.answer_authority for a save: admin | principal |
    delegate; "system" for none (the generator, a job) and for the
    automation actor (delayed.AUTOMATION_ACTOR, role "automation") — the
    unattended auto-publish is nobody looking at the week, and it used to be
    stamped "delegate", so the acceptance trend, the edit predictor and the
    experiment readout counted weeks nobody saw as a manager keeping every
    row (L-7). A person's own publish that waited out the undo window runs
    as the automation actor too; it carries `on_behalf_of` (the authority
    of the person who pressed Send) and is theirs."""
    if not isinstance(user, dict) or not user:
        return SYSTEM
    if str(user.get("role") or "").strip().lower() == "automation":
        behalf = user.get("on_behalf_of")
        if isinstance(behalf, dict) and behalf.get("authority") in ("principal", "delegate", "admin"):
            return behalf["authority"]
        return SYSTEM
    try:
        import permissions
        return permissions.answer_authority(user)
    except Exception:
        return "delegate"


# ── whose change each saved row is (schedule audit 10/3/26 L-5) ──────────────
#
# Apply fixes, Improve with Cavnar AI and the one-tap overtime move hand rows
# back that the owner then saves as an edit, and every learner read them as
# the manager's: the engine's own repair became "the manager keeps adding a
# server Friday" — learning fed by itself. A save now says, per changed row,
# whether the change was the manager's or Cavnar AI's, from
#   * the client's flag: a row carries `origin` ("cavnar:apply_fixes",
#     "cavnar:optimize", "cavnar:overtime_move") and `origin_sig` (row_sig of
#     the row as Cavnar AI handed it back — the routes stamp both); a flag
#     whose row was changed again since is the manager's;
#   * `cavnar_changes` in the save body for rows Cavnar AI took OUT
#     ([{date, employee, shift_start, source}] — a removed row has no notes);
#   * the notes Cavnar AI's own passes write on a row they change, read only
#     where this save added them.

CAVNAR_SOURCES = ("apply_fixes", "optimize", "overtime_move")
_CAVNAR_NOTE_TAGS = (
    ("optimize", re.compile(r"\bCavnar(?: AI)?:", re.I)),                  # Improve, the solver
    ("apply_fixes", re.compile(r"\(was [^()]+? — [^()]+\)")),              # shift_quality.apply_fixes
    ("overtime_move", re.compile(r"\bMoved to .+ to avoid overtime\b", re.I)),
)


def row_key(r) -> tuple:
    """(date, lower name, start) — the identity a change is keyed on."""
    return _key(r)


def key_str(k) -> str:
    return "|".join(str(x or "") for x in k)


def row_sig(r) -> str:
    """The whole row as Cavnar AI handed it back: a flag whose row no longer
    matches it was changed again by hand."""
    return "|".join((str(r.get("date") or "")[:10], " ".join(str(r.get("employee") or "").split()).lower(),
                     str(r.get("shift_start") or "").strip().lower(), str(r.get("shift_end") or "").strip().lower(),
                     str(r.get("role") or "").strip().lower()))


def _cavnar_source(value) -> str:
    v = str(value or "").strip().lower()
    if not v.startswith("cavnar"):
        return ""
    src = v.split(":", 1)[1].strip() if ":" in v else ""
    return src if src in CAVNAR_SOURCES else "cavnar"


def note_source(before_note, after_note) -> str:
    """The Cavnar AI pass whose tag this save added to a row's notes, or "":
    a tag counts only when the row carries more of it than it did before —
    a generated row's own "Cavnar AI:" note, or a NEEDS REVIEW mark taken
    off it, is not Cavnar AI changing the row now."""
    b, a = str(before_note or ""), str(after_note or "")
    if not a.strip():
        return ""
    for src, rx in _CAVNAR_NOTE_TAGS:
        if len(rx.findall(a)) > len(rx.findall(b)):
            return src
    return ""


def change_items(d: dict) -> list:
    """[(kind, keys, item)] — every change in a diff with the row keys it
    touches (a move touches the person who left and the one who came)."""
    out = []
    for r in d.get("added") or []:
        out.append(("added", {_key(r)}, r))
    for r in d.get("removed") or []:
        out.append(("removed", {_key(r)}, r))
    for m in d.get("moved") or []:
        out.append(("moved", {(m.get("date") or "", (m.get("from") or "").strip().lower(), m.get("shift_start") or ""),
                              (m.get("date") or "", (m.get("to") or "").strip().lower(), m.get("shift_start") or "")}, m))
    for rt in d.get("retimed") or []:
        who = (rt.get("employee") or "").strip().lower()
        out.append(("retimed", {(rt.get("date") or "", who, rt.get("old_start") or ""),
                                (rt.get("date") or "", who, rt.get("new_start") or "")}, rt))
    for rc in d.get("role_changed") or []:
        out.append(("role_changed", {(rc.get("date") or "", (rc.get("employee") or "").strip().lower(),
                                      rc.get("shift_start") or "")}, rc))
    return out


def _pair(kind, item, before_by, after_by):
    """(before row, after row) of one change, either may be None."""
    if kind == "added":
        return None, item
    if kind == "removed":
        return item, None
    if kind == "moved":
        k_from = (item.get("date") or "", (item.get("from") or "").strip().lower(), item.get("shift_start") or "")
        k_to = (item.get("date") or "", (item.get("to") or "").strip().lower(), item.get("shift_start") or "")
        return before_by.get(k_from), after_by.get(k_to)
    if kind == "retimed":
        who = (item.get("employee") or "").strip().lower()
        return (before_by.get((item.get("date") or "", who, item.get("old_start") or "")),
                after_by.get((item.get("date") or "", who, item.get("new_start") or "")))
    k = (item.get("date") or "", (item.get("employee") or "").strip().lower(), item.get("shift_start") or "")
    return before_by.get(k), after_by.get(k)


def step_origins(before_rows, after_rows, flagged_rows=None, cavnar_changes=None) -> dict:
    """What one save changed and whose each change was. `flagged_rows` are
    the rows as the client sent them (with `origin` / `origin_sig`),
    `cavnar_changes` the body's list of rows Cavnar AI removed. Returns
    {"changes": [{kind, keys, item, origin: "manager"|"cavnar", source}],
    "stored": {"cavnar": {key_str: source}} (schedule_versions.row_origins_json),
    "diff": the step's diff}. Pure."""
    d = diff(before_rows or [], after_rows or [])
    before_by = {_key(r): r for r in before_rows or []}
    after_by = {_key(r): r for r in after_rows or []}
    flags = {}
    for r in flagged_rows or []:
        if not isinstance(r, dict):
            continue
        src = _cavnar_source(r.get("origin"))
        sig = r.get("origin_sig")
        if src and (not sig or str(sig) == row_sig(r)):
            flags[_key({k: str(r.get(k) or "").strip() for k in ("date", "employee", "shift_start")})] = src
    gone = {}
    for c_ in cavnar_changes or []:
        if isinstance(c_, dict) and c_.get("date") and c_.get("employee"):
            gone[(str(c_["date"])[:10], " ".join(str(c_["employee"]).split()).lower(),
                  str(c_.get("shift_start") or "").strip())] = _cavnar_source("cavnar:" + str(c_.get("source") or ""))
    changes, stored = [], {}
    for kind, keys, item in change_items(d):
        b, a = _pair(kind, item, before_by, after_by)
        src = ""
        if a is not None:
            src = flags.get(_key(a)) or note_source((b or {}).get("notes"), a.get("notes"))
        if not src and kind == "removed":
            src = gone.get(_key(item)) or ""
        origin = "cavnar" if src else "manager"
        changes.append({"kind": kind, "keys": keys, "item": item, "origin": origin, "source": src or None,
                        "before": b, "after": a})
        if src:
            for k in keys:
                stored[key_str(k)] = src
    return {"changes": changes, "stored": {"cavnar": stored} if stored else {}, "diff": d}


def stamp_cavnar(before_rows, after_rows, source, flagged_rows=None) -> tuple:
    """What apply fixes and Improve hand back, flagged (L-5): (the rows,
    each one this pass changed or added carrying `origin`
    ("cavnar:<source>") and `origin_sig`; [{date, employee, shift_start,
    source}] for the rows it took out — the client returns that list as the
    save's `cavnar_changes`). A row the client had already flagged keeps its
    flag while it is unchanged."""
    st = step_origins(before_rows, after_rows)
    changed = {_key(ch["after"]) for ch in st["changes"] if ch.get("after") is not None}
    removed = [{"date": ch["item"].get("date"), "employee": ch["item"].get("employee"),
                "shift_start": ch["item"].get("shift_start"), "source": source}
               for ch in st["changes"] if ch["kind"] == "removed"]
    carried = {}
    for r in flagged_rows or []:
        if isinstance(r, dict) and _cavnar_source(r.get("origin")) and r.get("origin_sig") == row_sig(r):
            carried[row_sig(r)] = (r.get("origin"), r.get("origin_sig"))
    out = []
    for r in after_rows or []:
        r2 = dict(r)
        if _key(r) in changed:
            r2["origin"], r2["origin_sig"] = f"cavnar:{source}", row_sig(r)
        elif row_sig(r) in carried:
            r2["origin"], r2["origin_sig"] = carried[row_sig(r)]
        out.append(r2)
    return out, removed


def latest_rows(conn, history_id) -> list:
    """The rows of a week's newest version (the stored week when it has none)."""
    row = conn.execute("SELECT schedule_csv FROM schedule_versions WHERE history_id=? ORDER BY version DESC LIMIT 1",
                       (history_id,)).fetchone()
    if row is None:
        row = conn.execute("SELECT schedule_csv FROM schedule_history WHERE id=?", (history_id,)).fetchone()
    return rows_from_csv(row[0] if row else "")


def _insert_version(conn, restaurant_id, history_id, reason, schedule_csv, quality=None, saved_by=None,
                    saved_authority=None, row_origins=None):
    last = conn.execute("SELECT version, schedule_csv FROM schedule_versions WHERE history_id=? "
                        "ORDER BY version DESC LIMIT 1", (history_id,)).fetchone()
    version = (last["version"] + 1) if last else 1
    before_rows = rows_from_csv(last["schedule_csv"]) if last else []
    after_rows = rows_from_csv(schedule_csv)
    d = diff(before_rows, after_rows) if last else None
    # Whose each changed row is (L-5): the caller's reading (the save route,
    # from the client's flag), else Cavnar AI's own note tags on this save.
    if row_origins is None and last and reason == "edited":
        row_origins = step_origins(before_rows, after_rows)["stored"]
    # The advice the week carried before this save — read before the new
    # version (whose own quality is judged on the edited rows) lands.
    open_recs = _open_recommendations(conn, history_id) if (last and reason == "edited") else []
    # Support saving through view-as is not the manager's word (memory audit
    # 9/29/26, view_as): stamped SUPPORT_PREFIX, and the learners leave the
    # week out (schedule_learning, learned_patterns).
    try:
        from permissions import acting_via
        _via = acting_via()
    except Exception:
        _via = None
    if _via:
        saved_by = f"{SUPPORT_PREFIX}{_via.get('admin') or _via.get('admin_id')} (as {saved_by or 'the owner'})"
        saved_authority = "admin"        # whoever the caller named: the admin's hand
        open_recs = []                   # nor is it the owner carrying advice out
    cur = conn.execute(
        "INSERT INTO schedule_versions (restaurant_id, history_id, version, reason, schedule_csv, quality_json, "
        "diff_json, saved_by, saved_authority, row_origins_json) VALUES (?,?,?,?,?,?,?,?,?,?)",
        (restaurant_id, history_id, version, reason, schedule_csv,
         json.dumps(quality) if quality else None, json.dumps(d) if d else None,
         (saved_by or "").strip()[:120] or None, saved_authority,
         json.dumps(row_origins) if row_origins else None))
    row_id = cur.lastrowid
    if open_recs and d and d.get("changes"):
        _record_implied_acceptance(conn, restaurant_id, open_recs, before_rows, after_rows, saved_by)
    return row_id, version


def _open_recommendations(conn, history_id) -> list:
    """The recommendations on the newest version of this week that stored a
    quality verdict (the generated draft, or a manager save that rescored)."""
    row = conn.execute("SELECT quality_json FROM schedule_versions WHERE history_id=? AND quality_json IS NOT NULL "
                       "ORDER BY version DESC LIMIT 1", (history_id,)).fetchone()
    if not row:
        return []
    try:
        q = json.loads(row["quality_json"] or "null") or {}
    except (TypeError, ValueError):
        return []
    return [str(r) for r in (q.get("recommendations") or []) if r]


def _record_implied_acceptance(conn, restaurant_id, recs, before_rows, after_rows, saved_by) -> list:
    """An edit that carries out a recommendation accepted it, button or no
    button. Written on the caller's connection inside its transaction (a
    SAVEPOINT, so a failure here unwinds only these rows and never the
    version the save is for); one 'accepted' per recommendation per day,
    under the same kind and key the 'shown' event used."""
    try:
        from schedule_learning import addressed_recommendations
        from shift_quality import recommendation_kind
        hits = addressed_recommendations(recs, before_rows, after_rows)
    except Exception as e:          # a read-only match; the save must not fail on it
        log.warning("implied acceptance match failed for restaurant %s: %s", restaurant_id, e)
        return []
    if not hits:
        return []
    written = []
    conn.execute("SAVEPOINT implied_accept")
    try:
        for rec in hits:
            kind, key = recommendation_kind(rec)[:60], rec[:200]
            if conn.execute("SELECT 1 FROM schedule_recommendation_events WHERE restaurant_id=? AND kind=? AND key=? "
                            "AND action='accepted' AND created_at >= date('now')", (restaurant_id, kind, key)).fetchone():
                continue
            conn.execute("INSERT INTO schedule_recommendation_events (restaurant_id, kind, key, action, actor) "
                         "VALUES (?,?,?,'accepted',?)", (restaurant_id, kind, key, (saved_by or "").strip()[:120] or None))
            written.append(rec)
        # The edit carried the recommendation OUT: in the ledger it is
        # implemented (ROI #27), on this same connection and savepoint — a
        # second connection would wait on the lock this save holds.
        if written:
            conn.execute("SAVEPOINT implied_impl")
            try:
                import rec_ledger
                from schedule_intel import schedule_rec_key
                for rec in written:
                    rec_ledger.implemented_on(conn, restaurant_id, schedule_rec_key(recommendation_kind(rec), rec),
                                              "schedule_review", meta={"via": "schedule_edit", "module": "schedule"})
                conn.execute("RELEASE SAVEPOINT implied_impl")
            except Exception as e:      # neither the save nor the acceptance fails on the ledger
                conn.execute("ROLLBACK TO SAVEPOINT implied_impl")
                conn.execute("RELEASE SAVEPOINT implied_impl")
                log.warning("implementation not recorded for restaurant %s: %s", restaurant_id, e)
        conn.execute("RELEASE SAVEPOINT implied_accept")
    except sqlite3.Error as e:
        conn.execute("ROLLBACK TO SAVEPOINT implied_accept")
        conn.execute("RELEASE SAVEPOINT implied_accept")
        log.warning("implied acceptance not recorded for restaurant %s: %s", restaurant_id, e)
        return []
    return written


def append(restaurant_id, history_id, reason, schedule_csv, quality=None, saved_by=None, db_path=DB_PATH,
           saved_authority=None, row_origins=None) -> int:
    """Store one more state of a schedule, with its diff against the last."""
    conn = get_conn(db_path)
    try:
        row_id, _v = _insert_version(conn, restaurant_id, history_id, reason, schedule_csv, quality, saved_by,
                                     saved_authority, row_origins=row_origins)
        conn.commit()
        return row_id
    finally:
        conn.close()


class StaleVersion(Exception):
    """The week was saved by someone else since this copy was loaded."""

    def __init__(self, latest):
        super().__init__(f"the schedule is at version {latest}")
        self.latest = latest


def write_on(conn, restaurant_id, history_id, reason, schedule_csv, saved_by=None, quality=None,
             expected_version=None, saved_authority=None, row_origins=None) -> int:
    """Overwrite the stored week AND append its version row, on the caller's
    connection, inside the caller's write transaction (open it with BEGIN
    IMMEDIATE and commit after). The two used to be separate commits, so a
    lost version append left a week the conflict check could not see, and
    two saves checked against one version both landed (SCHED-19, SCHED-5).

    expected_version: the version the edit was made against; a newer one
    raises StaleVersion and nothing is written. hours_scheduled follows the
    rows, so a budget blocker judged at generation time cannot outlive the
    edit that fixed it (SCHED-17). Returns the new version number."""
    if expected_version is not None:
        latest = latest_version(conn, history_id)
        if latest and int(expected_version) != latest:
            raise StaleVersion(latest)
    hours = round(sum(_hours(r) for r in rows_from_csv(schedule_csv)), 1)
    old = conn.execute("SELECT schedule_csv FROM schedule_history WHERE id=? AND restaurant_id=?",
                       (history_id, restaurant_id)).fetchone()
    cur = conn.execute(
        "UPDATE schedule_history SET schedule_csv=?, quality_json=COALESCE(?, quality_json), hours_scheduled=?, "
        "edited_at=datetime('now'), edited_by=? WHERE id=? AND restaurant_id=?",
        (schedule_csv, json.dumps(quality) if quality else None, hours,
         (saved_by or "").strip()[:120] or None, history_id, restaurant_id))
    if cur.rowcount != 1:
        raise LookupError("that schedule is gone")
    _row_id, version = _insert_version(conn, restaurant_id, history_id, reason, schedule_csv, quality, saved_by,
                                       saved_authority, row_origins=row_origins)
    # A shift that changed hands in this rewrite keeps its floor section —
    # the Studio's swap saves here, as do covers and swaps (employee audit
    # B8 gap). Inside the caller's transaction.
    try:
        _models_mod.carry_shift_sections(conn, restaurant_id, rows_from_csv(old[0] if old else ""),
                                         rows_from_csv(schedule_csv))
    except sqlite3.OperationalError as e:
        if "no such table" not in str(e).lower():
            raise
    return version


def list_versions(restaurant_id, history_id, db_path=DB_PATH) -> list:
    return _versions(restaurant_id, history_id, db_path)


def newest_version(restaurant_id, history_id, db_path=DB_PATH) -> list:
    """[the latest version, as list_versions shapes it], or []."""
    return _versions(restaurant_id, history_id, db_path)[-1:]


def _versions(restaurant_id, history_id, db_path=DB_PATH) -> list:
    conn = get_conn(db_path)
    try:
        rows = conn.execute("SELECT id, version, reason, saved_by, created_at, diff_json, quality_json FROM "
                            "schedule_versions WHERE restaurant_id=? AND history_id=? ORDER BY version",
                            (restaurant_id, history_id)).fetchall()
    finally:
        conn.close()
    out = []
    for r in rows:
        try:
            d = json.loads(r["diff_json"] or "null")
        except Exception:
            d = None
        try:
            q = json.loads(r["quality_json"] or "null")
        except Exception:
            q = None
        out.append({"id": r["id"], "version": r["version"], "reason": r["reason"], "saved_by": r["saved_by"],
                    "created_at": r["created_at"], "changes": (d or {}).get("changes", 0),
                    "lines": diff_lines(d, unchanged="No changes in this version.") if d else [], "score": (q or {}).get("score")})
    return out


def _person_shifts(rows, key) -> list:
    return sorted((r.get("date", ""), r.get("shift_start", ""), r.get("shift_end", ""),
                   (r.get("role") or "").strip().lower())
                  for r in rows if " ".join((r.get("employee") or "").split()).casefold() == key)


def unsent_changes(restaurant_id, history_id, csv_text=None, db_path=DB_PATH):
    """Who has shifts on this PUBLISHED week that differ from what they were
    last told, or None when the week was never published.

    {"people": [names], "dates": [iso dates whose shifts changed]}.

    What a person was last told is the last `published` version (the week
    went out, or its changes did) — or, for the two people in a cover or a
    swap after it, that `swap` version: shift_requests told both of them
    itself. Diffing only against `published` re-sent a cover to both people
    on the manager's next edit (F2-2). `csv_text` is the week as it stands
    (default: the stored week)."""
    conn = get_conn(db_path)
    try:
        row = conn.execute("SELECT published_at, schedule_csv FROM schedule_history WHERE id=? AND restaurant_id=?",
                           (history_id, restaurant_id)).fetchone()
        if not row or not row["published_at"]:
            return None
        versions = conn.execute("SELECT version, reason, schedule_csv FROM schedule_versions WHERE history_id=? "
                                "AND restaurant_id=? ORDER BY version", (history_id, restaurant_id)).fetchall()
    finally:
        conn.close()
    now_rows = rows_from_csv(row["schedule_csv"] if csv_text is None else csv_text)
    last_pub = max((i for i, v in enumerate(versions) if v["reason"] == "published"), default=None)
    if last_pub is None:
        # A week published before versions were kept: nothing on file says
        # what anyone was told, so nobody is claimed to be behind.
        return {"people": [], "dates": []}
    base = rows_from_csv(versions[last_pub]["schedule_csv"])
    personal, prev = {}, base
    for v in versions[last_pub + 1:]:
        cur = rows_from_csv(v["schedule_csv"])
        if v["reason"] == "swap":
            d = diff(prev, cur)
            parties = {(r.get("employee") or "") for r in d["added"] + d["removed"] + d["retimed"] + d["role_changed"]}
            parties |= {m.get("from") or "" for m in d["moved"]} | {m.get("to") or "" for m in d["moved"]}
            for p in parties:
                k = " ".join(p.split()).casefold()
                if k:
                    personal[k] = cur
        prev = cur
    names = {}
    for r in base + now_rows + [r for rows in personal.values() for r in rows]:
        n = " ".join((r.get("employee") or "").split())
        if n:
            names.setdefault(n.casefold(), n)
    people, dates = [], set()
    for k, n in sorted(names.items()):
        was, now = _person_shifts(personal.get(k, base), k), _person_shifts(now_rows, k)
        if was != now:
            people.append(n)
            dates |= {s[0] for s in set(was) ^ set(now) if s[0]}
    return {"people": people, "dates": sorted(dates)}


def draft_vs_published(restaurant_id, history_id, db_path=DB_PATH) -> dict:
    """The first version against the latest, which is what a manager means
    by "what did I change before I sent it"."""
    conn = get_conn(db_path)
    try:
        first = conn.execute("SELECT schedule_csv FROM schedule_versions WHERE history_id=? AND restaurant_id=? "
                             "ORDER BY version ASC LIMIT 1", (history_id, restaurant_id)).fetchone()
        last = conn.execute("SELECT schedule_csv FROM schedule_versions WHERE history_id=? AND restaurant_id=? "
                            "ORDER BY version DESC LIMIT 1", (history_id, restaurant_id)).fetchone()
    finally:
        conn.close()
    if not first or not last:
        return {"available": False}
    d = diff(rows_from_csv(first["schedule_csv"]), rows_from_csv(last["schedule_csv"]))
    return {"available": True, **d, "lines": diff_lines(d, unchanged="No edits since the draft.")}


def _same_rows(a_rows: list, b_rows: list) -> int:
    """Rows of a_rows that survive untouched in b_rows: same date, person,
    start, end and role."""
    def k(r):
        return (r.get("date", ""), (r.get("employee") or "").strip().lower(), r.get("shift_start", ""),
                r.get("shift_end", ""), (r.get("role") or "").strip().lower())
    pool = {}
    for r in b_rows:
        pool[k(r)] = pool.get(k(r), 0) + 1
    same = 0
    for r in a_rows:
        if pool.get(k(r)):
            pool[k(r)] -= 1
            same += 1
    return same


# ── a week as the restaurant settled it (schedule audit 10/3/26 L-4, L-5,
#    L-7, L-8, L-26, L-27) ──────────────────────────────────────────────────
#
# The learners used to read a week as its generated draft against the newest
# non-swap version. So every save after the week went out counted (replacing
# Bob after his call-out taught "take Bob off Tuesday dinner", L-4); every
# change Cavnar AI made and the owner saved counted as the manager's (L-5);
# one view-as save by support dropped the whole week, the owner's own edits
# with it (L-8); a week auto-published with nobody looking counted as every
# row kept (L-7); and a redo of some days baked the earlier edits into the
# new "generated" draft, where no learner could see them again (L-26).
#
# A week is now read as a chain of steps from its ORIGINAL draft:
#   * the draft is the generated version — and when the owner redid some
#     days (schedule_rejections redo_days), the draft that redo replaced, for
#     the days it kept, so the edits made before the redo are not lost;
#   * the steps are the saves up to the FIRST publish (the pre-publish phase);
#     later saves are reactions — the save route observes them as such
#     (schedule_memory) and no habit is learned from them;
#   * each change in the net diff is the work of the step that last touched
#     its rows: Cavnar AI's (row_origins_json and its note tags), an admin's
#     (view-as, support — unless the account holder adopted it), a change
#     the owner said was this week only or a call-off (schedule_edit_answers),
#     or the manager's;
#   * `final` is the draft with only the manager's own changes applied — what
#     every preference learner reads; `final_owner` keeps Cavnar AI's kept
#     changes too (what went out, less an admin's hand — the acceptance figure);
#   * `looked_at` says a person of the restaurant edited or sent it; a week
#     the automatic publish sent with no edit, or only an admin touched, is
#     nobody's acceptance and no learner's evidence.

LEARN_WEEKS = 24
LEARN_DAYS = LEARN_WEEKS * 7        # the retention registry's reader window (ops)
# Evidence fades by half-life rather than falling off an 8- or 16-week cliff
# (L-27, raw_L §3 B): a person on a slot 120 days, a role's headcount or
# start time 180 days.
PERSON_HALF_LIFE_DAYS = 120
SLOT_HALF_LIFE_DAYS = 180
HALF_LIFE_DAYS = {"moved_off": PERSON_HALF_LIFE_DAYS, "moved_on": PERSON_HALF_LIFE_DAYS,
                  "role_change": PERSON_HALF_LIFE_DAYS, "leader_swap": SLOT_HALF_LIFE_DAYS,
                  "retime_start": SLOT_HALF_LIFE_DAYS, "retime_end": SLOT_HALF_LIFE_DAYS,
                  "headcount_add": SLOT_HALF_LIFE_DAYS, "headcount_cut": SLOT_HALF_LIFE_DAYS}
# A pattern is the manager's habit only when they made the change in at least
# this share of the weeks they could have (L-6) — weighted by recency.
PATTERN_MIN_RATE = 0.5
# The confidence bound on a rate: the Wilson lower bound at 90%, the level the
# experiment readout's intervals use.
WILSON_Z = 1.645
PRE_PUBLISH, POST_PUBLISH = "pre_publish", "post_publish"
# Answers to the one-tap "why" that keep a change out of the habits (L-35).
ONE_OFF_ANSWERS = ("this_week", "call_off")


def half_life(kind) -> int:
    return HALF_LIFE_DAYS.get(kind, PERSON_HALF_LIFE_DAYS)


def recency_weight(age_days, half_life_days) -> float:
    """1.0 for evidence from today, 0.5 one half-life ago."""
    try:
        return 0.5 ** (max(0.0, float(age_days or 0)) / float(half_life_days))
    except (TypeError, ValueError, ZeroDivisionError):
        return 1.0


def wilson_lower(hits, n, z=WILSON_Z) -> float:
    """The lower bound of the rate hits/n — 2 of 2 is far less sure than 20
    of 20. Weighted counts are allowed. 0.0 with no opportunities."""
    try:
        n, hits = float(n), float(hits)
    except (TypeError, ValueError):
        return 0.0
    if n <= 0:
        return 0.0
    p = min(1.0, max(0.0, hits / n))
    z2 = z * z
    centre = p + z2 / (2 * n)
    margin = z * math.sqrt(max(0.0, p * (1 - p) / n + z2 / (4 * n * n)))
    return max(0.0, (centre - margin) / (1 + z2 / n))


def _col(row, name):
    try:
        return row[name] if name in row.keys() else None
    except (AttributeError, TypeError):
        return (row or {}).get(name) if isinstance(row, dict) else None


def learnable_version(v) -> bool:
    """A save by the restaurant's own people: not an admin's (view-as or a
    support login) unless the account holder adopted it (adopt_admin_saves)."""
    if _col(v, "adopted_at"):
        return True
    return (_col(v, "saved_authority") or "") != "admin" and \
        not str(_col(v, "saved_by") or "").startswith(SUPPORT_PREFIX)


def _person_published(v, auto_ids) -> bool:
    """Whether a publish was a person of the restaurant sending the week:
    not the unattended automatic publish (authority 'system', or — before the
    stamp existed — a week a done automatic delayed action published), not an
    admin's unless adopted."""
    if v is None:
        return False
    auth = _col(v, "saved_authority") or ""
    if auth == SYSTEM:
        return False
    if not learnable_version(v):
        return False
    if _col(v, "history_id") in auto_ids and auth in ("", "delegate") and \
            str(_col(v, "saved_by") or "").strip().lower() in ("cavnar ai", "automation"):
        return False
    return True


def _auto_published_ids(conn, restaurant_id) -> set:
    """History ids an automatic (unattended) delayed publish sent — the
    legacy reading for publishes stamped before 'system' existed."""
    out = set()
    try:
        rows = conn.execute("SELECT payload_json FROM delayed_actions WHERE restaurant_id=? AND kind='schedule_publish' "
                            "AND status='done'", (restaurant_id,)).fetchall()
    except sqlite3.OperationalError:
        return out
    for r in rows:
        try:
            p = json.loads(r[0] or "{}") or {}
        except (TypeError, ValueError):
            continue
        manual = bool(p.get("manual")) or "acknowledge" in p
        if p.get("schedule_id") and (p.get("automatic") or not manual):
            try:
                out.add(int(p["schedule_id"]))
            except (TypeError, ValueError):
                continue
    return out


def _rows_sig_by_date(rows) -> dict:
    out = {}
    for r in rows or []:
        out.setdefault((r.get("date") or "")[:10], []).append(row_sig(r))
    return {d: sorted(v) for d, v in out.items()}


class _WeekData:
    """Everything the week records are built from, loaded in a handful of
    queries: histories, versions, the owner's redo/discard rejections, the
    one-tap answers, and the weeks an automatic publish sent."""

    def __init__(self, restaurant_id, db_path=DB_PATH, lookback_days=None, history_ids=None):
        self.rid = restaurant_id
        conn = get_conn(db_path)
        try:
            if history_ids is not None:
                ids = sorted({int(h) for h in history_ids if h})
            else:
                ids = [r[0] for r in conn.execute(
                    "SELECT DISTINCT history_id FROM schedule_versions WHERE restaurant_id=? "
                    "AND created_at >= datetime('now', ?)", (restaurant_id, f"-{int(lookback_days or 0)} days"))]
            self.final_ids = list(ids)
            hist = {}
            weeks = set()
            for i in range(0, len(ids), 400):
                chunk = ids[i:i + 400]
                marks = ",".join("?" for _ in chunk)
                for h in conn.execute(f"SELECT id, week_start, published_at, superseded_by FROM schedule_history "
                                      f"WHERE restaurant_id=? AND id IN ({marks})", (restaurant_id, *chunk)):
                    hist[h["id"]] = dict(h)
                    if h["week_start"]:
                        weeks.add(h["week_start"])
            # Every draft of those weeks: a redo's predecessor may be older
            # than the window.
            wk = sorted(weeks)
            for i in range(0, len(wk), 400):
                chunk = wk[i:i + 400]
                marks = ",".join("?" for _ in chunk)
                for h in conn.execute(f"SELECT id, week_start, published_at, superseded_by FROM schedule_history "
                                      f"WHERE restaurant_id=? AND week_start IN ({marks})", (restaurant_id, *chunk)):
                    hist.setdefault(h["id"], dict(h))
            self.hist = hist
            all_ids = sorted(hist)
            self.versions, self.rejections, self.answers = {}, {}, {}
            for i in range(0, len(all_ids), 400):
                chunk = all_ids[i:i + 400]
                marks = ",".join("?" for _ in chunk)
                for v in conn.execute(
                        f"SELECT id, history_id, version, reason, schedule_csv, saved_by, saved_authority, created_at, "
                        f"row_origins_json, adopted_at FROM schedule_versions WHERE restaurant_id=? AND history_id IN "
                        f"({marks}) ORDER BY history_id, version", (restaurant_id, *chunk)):
                    self.versions.setdefault(v["history_id"], []).append(dict(v))
                try:
                    for r in conn.execute(f"SELECT * FROM schedule_rejections WHERE restaurant_id=? AND history_id IN "
                                          f"({marks}) ORDER BY created_at, id", (restaurant_id, *chunk)):
                        self.rejections.setdefault(r["history_id"], []).append(dict(r))
                except sqlite3.OperationalError:
                    pass
                try:
                    for r in conn.execute(f"SELECT history_id, keys_json, answer, authority, adopted_at FROM "
                                          f"schedule_edit_answers WHERE restaurant_id=? AND history_id IN ({marks}) "
                                          f"AND answer IS NOT NULL", (restaurant_id, *chunk)):
                        self.answers.setdefault(r["history_id"], []).append(dict(r))
                except sqlite3.OperationalError:
                    pass
            self.auto_ids = _auto_published_ids(conn, restaurant_id)
        finally:
            conn.close()
        texts = [v["schedule_csv"] for vs in self.versions.values() for v in vs]
        self.canon = canonical_rows(restaurant_id, [rows_from_csv(t) for t in texts], db_path=db_path,
                                    mapping_only=True)
        self._rows = {}

    def rows(self, v) -> list:
        """A version's rows, each name read as the person it means now."""
        if v["id"] not in self._rows:
            self._rows[v["id"]] = canonical_rows(self.rid, [rows_from_csv(v["schedule_csv"])], mapping=self.canon)[0]
        return self._rows[v["id"]]

    def generated(self, hid):
        return next((v for v in self.versions.get(hid) or [] if v["reason"] == "generated"), None)

    def first_publish(self, hid):
        return next((v for v in self.versions.get(hid) or [] if v["reason"] == "published"), None)

    def finals(self) -> list:
        """The history that holds each week's word: the live published draft
        of the week, else the newest draft not replaced by another."""
        by_week = {}
        for hid in self.final_ids:
            h = self.hist.get(hid)
            if not h:
                continue
            by_week.setdefault(h["week_start"] or f"#{hid}", []).append(h)
        out = []
        for wk, hs in by_week.items():
            if not wk.startswith("#"):
                hs = [h for h in self.hist.values() if h["week_start"] == wk]
            pub = [h for h in hs if h["published_at"]]
            if pub:
                live = [h for h in pub if not h["superseded_by"]] or pub
                pick = max(live, key=lambda h: h["id"])
            else:
                open_ = [h for h in hs if not h["superseded_by"]] or hs
                pick = max(open_, key=lambda h: h["id"])
            out.append(pick["id"])
        return sorted(out)

    def predecessor(self, hid):
        """(the draft an owner's redo of some days replaced, the redone
        dates, whose redo it was) — or (None, None, None): no redo, or the
        latest rejection of the older draft was a whole-week regeneration,
        or its kept days do not match (a redo that never landed)."""
        gen = self.generated(hid)
        if gen is None:
            return None, None, None
        cands = sorted((h for h in self.hist.values() if h["superseded_by"] == hid and h["id"] < hid
                        and not h["published_at"]), key=lambda h: -h["id"])
        for p in cands:
            rej = [r for r in self.rejections.get(p["id"]) or [] if str(r["created_at"]) <= str(gen["created_at"])]
            if not rej:
                continue
            last = rej[-1]
            if last["kind"] != "redo_days":
                return None, None, None
            try:
                dates = {str(d)[:10] for d in json.loads(last["dates_json"] or "[]") if d}
            except (TypeError, ValueError):
                dates = set()
            pvs = self.versions.get(p["id"]) or []
            if not dates or not pvs:
                return None, None, None
            kept_new = {d: s for d, s in _rows_sig_by_date(self.rows(gen)).items() if d not in dates}
            kept_old = {d: s for d, s in _rows_sig_by_date(self.rows(pvs[-1])).items() if d not in dates}
            if kept_new != kept_old:
                return None, None, None
            return p["id"], dates, last.get("authority")
        return None, None, None

    def attrs(self, v) -> dict:
        try:
            stored = (json.loads(v.get("row_origins_json") or "{}") or {}).get("cavnar") or {}
        except (TypeError, ValueError):
            stored = {}
        return {"version": v["version"], "history_id": v["history_id"], "reason": v["reason"],
                "authority": v.get("saved_authority"), "editor": (str(v.get("saved_by") or "").strip() or None),
                "learnable": learnable_version(v), "created_at": v.get("created_at"), "cavnar": stored}

    def compose(self, hid, depth=0):
        """{base, steps: [(rows, attrs)], rejected, redo_dates, chain} for one
        draft and every owner redo before it."""
        gen = self.generated(hid)
        if gen is None:
            return None
        vs = self.versions.get(hid) or []
        pub = self.first_publish(hid)
        cutoff = pub["version"] if pub else (vs[-1]["version"] if vs else gen["version"])
        own = [(self.rows(v), self.attrs(v)) for v in vs
               if gen["version"] < v["version"] <= cutoff and v["reason"] != "swap"]
        pred, redo, by = self.predecessor(hid) if depth < 12 else (None, None, None)
        if pred is None:
            return {"base": self.rows(gen), "steps": own, "rejected": [], "redo_dates": set(), "chain": [hid]}
        back = self.compose(pred, depth + 1)
        if back is None:
            return {"base": self.rows(gen), "steps": own, "rejected": [], "redo_dates": set(), "chain": [hid]}
        redone = [r for r in self.rows(gen) if (r.get("date") or "")[:10] in redo]

        def keep(rows):
            return [r for r in rows if (r.get("date") or "")[:10] not in redo]
        # The rows the owner's redo threw away are the strongest "no" there
        # is; an admin's redo (view-as) links the drafts but is not the
        # owner's no.
        thrown = [r for r in back["base"] if (r.get("date") or "")[:10] in redo] if by != "admin" else []
        return {"base": keep(back["base"]) + redone,
                "steps": [(keep(rws) + redone, a) for rws, a in back["steps"]] + own,
                "rejected": back["rejected"] + thrown,
                "redo_dates": back["redo_dates"] | redo, "chain": back["chain"] + [hid]}

    def answered_keys(self, chain) -> set:
        out = set()
        for hid in chain:
            for a in self.answers.get(hid) or []:
                if a.get("answer") not in ONE_OFF_ANSWERS:
                    continue
                if (a.get("authority") or "") == "admin" and not a.get("adopted_at"):
                    continue
                try:
                    out |= {tuple(str(k).split("|")[:3]) for k in json.loads(a.get("keys_json") or "[]")}
                except (TypeError, ValueError):
                    continue
        return out


def _revert(final_rows, base_rows, items) -> list:
    """`final_rows` with each of `items` (changes from base to final) undone:
    the base's row put back where the change replaced or removed it."""
    rows = [dict(r) for r in final_rows]
    base_by = {_key(r): r for r in base_rows}

    def _drop(k):
        for i, r in enumerate(rows):
            if _key(r) == k:
                return rows.pop(i)
        return None
    for kind, keys, item in items:
        if kind == "added":
            _drop(_key(item))
        elif kind == "removed":
            rows.append(dict(item))
        elif kind == "moved":
            k_from = (item.get("date") or "", (item.get("from") or "").strip().lower(), item.get("shift_start") or "")
            k_to = (item.get("date") or "", (item.get("to") or "").strip().lower(), item.get("shift_start") or "")
            _drop(k_to)
            if k_from in base_by:
                rows.append(dict(base_by[k_from]))
        elif kind == "retimed":
            who = (item.get("employee") or "").strip().lower()
            _drop((item.get("date") or "", who, item.get("new_start") or ""))
            old = base_by.get((item.get("date") or "", who, item.get("old_start") or ""))
            if old:
                rows.append(dict(old))
        elif kind == "role_changed":
            k = (item.get("date") or "", (item.get("employee") or "").strip().lower(), item.get("shift_start") or "")
            _drop(k)
            if k in base_by:
                rows.append(dict(base_by[k]))
    return rows


def _week_date(h, gen):
    for v in ((h or {}).get("week_start"), (gen or {}).get("created_at")):
        try:
            return date.fromisoformat(str(v)[:10])
        except (TypeError, ValueError):
            continue
    return None


def _record(data: _WeekData, hid: int, today=None):
    """One week as the restaurant settled it (see the section above), or None
    when the week has no generated draft (an uploaded schedule)."""
    comp = data.compose(hid)
    if comp is None:
        return None
    base, steps = comp["base"], comp["steps"]
    final_all = steps[-1][0] if steps else base
    # Who last touched each row: the latest step's change wins.
    touch, prev = {}, base
    for idx, (rows, at) in enumerate(steps):
        if rows is prev:
            continue
        st = step_origins(prev, rows)
        for ch in st["changes"]:
            src = next((at["cavnar"].get(key_str(k)) for k in ch["keys"] if at["cavnar"].get(key_str(k))), None) \
                or ch["source"]
            for k in ch["keys"]:
                touch[k] = (idx, at, src)
        prev = rows
    answered = data.answered_keys(comp["chain"])
    items, excluded = [], {"cavnar": [], "admin": [], "answered": []}
    for kind, keys, item in change_items(diff(base, final_all)):
        hits = [touch[k] for k in keys if k in touch]
        idx, at, src = max(hits, key=lambda t: t[0]) if hits else (len(steps) - 1, steps[-1][1] if steps else {}, None)
        why = ("cavnar" if src else "admin" if not at.get("learnable", True) else
               "answered" if keys & answered else None)
        entry = {"kind": kind, "keys": keys, "item": item, "origin": "cavnar" if src else "manager",
                 "source": src, "authority": at.get("authority"), "editor": at.get("editor"),
                 "version": at.get("version"), "history_id": at.get("history_id"), "excluded": why}
        items.append(entry)
        if why:
            excluded[why].append(entry)
    learn_out = [(e["kind"], e["keys"], e["item"]) for e in items if e["excluded"]]
    admin_out = [(e["kind"], e["keys"], e["item"]) for e in items if e["excluded"] == "admin"]
    final = _revert(final_all, base, learn_out) if learn_out else final_all
    final_owner = _revert(final_all, base, admin_out) if admin_out else final_all
    gen = data.generated(hid)
    pub = data.first_publish(hid)
    person_edit = any(at["reason"] == "edited" and at["learnable"] for _rows, at in steps)
    h = data.hist.get(hid) or {}
    # A week sent before the publish was versioned: a person sent it unless
    # an automatic publish did.
    legacy_pub = pub is None and bool(h.get("published_at")) and hid not in data.auto_ids
    looked = person_edit or legacy_pub or _person_published(pub, data.auto_ids)
    learn_items = [e for e in items if not e["excluded"]]
    editor = next((e["editor"] for e in sorted(learn_items, key=lambda e: -(e["version"] or 0)) if e["editor"]), None) \
        or next((at["editor"] for _r, at in reversed(steps) if at["reason"] == "edited" and at["learnable"]), None)
    wd = _week_date(h, gen)
    today = today or date.today()
    return {"history_id": hid, "week_start": h.get("week_start"), "chain": comp["chain"],
            "base": base, "final": final, "final_all": final_all, "final_owner": final_owner,
            "diff": diff(base, final), "items": items, "excluded": {k: len(v) for k, v in excluded.items()},
            "rejected": comp["rejected"], "redo_dates": sorted(comp["redo_dates"]),
            "editor": editor, "edited": bool(learn_items), "looked_at": bool(looked),
            "published": pub is not None or bool(h.get("published_at")),
            "publish_authority": (pub or {}).get("saved_authority"),
            "published_by_person": legacy_pub or _person_published(pub, data.auto_ids),
            "generated_at": (gen or {}).get("created_at"), "week_date": wd,
            "age_days": max(0, (today - wd).days) if wd else 0}


def learning_weeks(restaurant_id, weeks=LEARN_WEEKS, db_path=DB_PATH, history_ids=None, include_unlooked=False,
                   today=None) -> list:
    """Every week in the last `weeks` weeks (by its saves) that a person of
    the restaurant finished with — edited, or sent themselves — as
    _record reads it, oldest first. Unedited weeks are in: they are the
    evidence that the draft was kept (L-6). `history_ids` reads those weeks
    instead of a window; `include_unlooked` keeps the weeks nobody looked at
    (flagged looked_at False)."""
    data = _WeekData(restaurant_id, db_path=db_path, lookback_days=int(weeks) * 7, history_ids=history_ids)
    ids = list(history_ids) if history_ids is not None else data.finals()
    out = []
    for hid in ids:
        try:
            rec = _record(data, int(hid), today=today)
        except Exception as e:          # one unreadable week never hides the rest
            log.warning("learning week %s unreadable for restaurant %s: %s", hid, restaurant_id, e)
            continue
        if rec is None or (not rec["looked_at"] and not include_unlooked):
            continue
        out.append(rec)
    out.sort(key=lambda w: (str(w["week_start"] or ""), w["history_id"]))
    return out


def acceptance(restaurant_id, weeks=8, db_path=DB_PATH) -> dict:
    """How much of each generated draft survived to the week that was sent.

    Per published week (newest first, the latest publish of each calendar
    week): the changes between the ORIGINAL draft (before any redo of some
    days, L-26) and the week as it was FIRST sent (later saves are reactions
    to the week, not a verdict on the draft — L-4), and the share of drafted
    rows that went out untouched. An admin's hand (view-as, support — unless
    the account holder adopted it) is taken out of the week rather than the
    week out of the record (L-8); the changes Cavnar AI made and the owner
    kept stay in (the draft did need them). A week nobody of the restaurant
    looked at — the automatic publish with no edit, or only an admin's hand
    — is listed with `looked_at` False and no share: it is not anybody's
    acceptance, and the trend, the scorecard and the experiment readout
    leave it out (L-7). The trend compares the older half of the weeks with
    the newer half — a rising share is a draft that needs less fixing. A
    week with no generated version (an uploaded schedule) has no draft to
    judge and is skipped. Pure read."""
    conn = get_conn(db_path)
    try:
        hist = conn.execute(
            "SELECT id, week_start FROM schedule_history WHERE restaurant_id=? AND published_at IS NOT NULL "
            "ORDER BY week_start DESC, id DESC LIMIT ?", (restaurant_id, int(weeks) * 3)).fetchall()
    finally:
        conn.close()
    picked, seen = [], set()
    for h in hist:
        wk = h["week_start"] or f"#{h['id']}"
        if wk in seen:
            continue
        seen.add(wk)
        picked.append(h)
        if len(picked) >= int(weeks):
            break
    recs = {}
    if picked:
        for rec in learning_weeks(restaurant_id, db_path=db_path, history_ids=[h["id"] for h in picked],
                                  include_unlooked=True):
            recs[rec["history_id"]] = rec
    out = []
    for h in picked:
        rec = recs.get(h["id"])
        if rec is None:
            continue
        g_rows, p_rows = rec["base"], rec["final_owner"]
        d = diff(g_rows, p_rows)
        same = _same_rows(g_rows, p_rows)
        looked = rec["looked_at"]
        out.append({"history_id": h["id"], "week_start": h["week_start"], "changes": d["changes"],
                    "rows_generated": len(g_rows), "rows_published": len(p_rows), "unchanged_rows": same,
                    "unchanged_share": round(same / len(g_rows), 3) if (g_rows and looked) else None,
                    "looked_at": looked, "publish_authority": rec["publish_authority"],
                    "admin_changes": rec["excluded"]["admin"], "cavnar_changes": rec["excluded"]["cavnar"],
                    "added": len(d["added"]), "removed": len(d["removed"]), "moved": len(d["moved"]),
                    "retimed": len(d["retimed"]), "role_changed": len(d["role_changed"]),
                    "hours_generated": d["hours_before"], "hours_published": d["hours_after"]})
    shares = [w["unchanged_share"] for w in reversed(out) if w["unchanged_share"] is not None]   # oldest first
    trend = None
    if len(shares) >= 3:
        half = len(shares) // 2
        older = sum(shares[:half]) / half
        newer = sum(shares[-half:]) / half
        delta = round(newer - older, 3)
        trend = {"direction": "rising" if delta >= 0.05 else "falling" if delta <= -0.05 else "steady",
                 "older": round(older, 3), "newer": round(newer, 3), "delta": delta}
    looked = [w for w in out if w["looked_at"]]
    return {"available": bool(out), "weeks": out, "trend": trend,
            "mean_unchanged_share": round(sum(shares) / len(shares), 3) if shares else None,
            "mean_changes": round(sum(w["changes"] for w in looked) / len(looked), 1) if looked else None}


# ── what the manager keeps changing ────────────────────────────────────────

def learned_patterns(restaurant_id, weeks=LEARN_WEEKS, min_repeats=2, db_path=DB_PATH, min_rate=PATTERN_MIN_RATE,
                     week_edits=None) -> list:
    """Edits that recur across recent weeks: the same person moved off the
    same weekday and daypart at least `min_repeats` times (moved_off /
    moved_on, up to 12), then the kinds schedule_learning.edit_patterns
    reads from each week's draft-to-final change (retime_start /
    retime_end, headcount_add / headcount_cut, role_change, leader_swap,
    up to 8). Returned as facts, and rendered into the prompt so the next
    draft starts where the manager keeps ending up.

    Read from every week a person of the restaurant finished with
    (learning_weeks: the manager's own pre-publish changes only — never a
    reaction after the week went out, Cavnar AI's kept changes, an admin's
    hand or a change the owner called a one-off). Each pattern carries its
    denominator (schedule audit 10/3/26 L-6): `opportunities` — the weeks the
    manager could have made the change — and `rate` / `confidence` (the
    recency-weighted share of them they did, and its Wilson lower bound);
    a pattern needs `min_rate` of its opportunities, so two edits in eight
    weeks Bob worked Tuesday dinner are not a habit, two of three are. Older
    weeks count less by half-life (L-27).

    Each carries `editors` ({who saved those weeks: weeks}) — per editor as
    well as per restaurant (memory audit 9/29/26, standing_patterns): two
    GMs with opposite habits on alternate weeks used to blend into one
    "manager"."""
    if week_edits is None:
        week_edits = learning_weeks(restaurant_id, weeks, db_path)
    out = _patterns_from_weeks(restaurant_id, week_edits, min_repeats, db_path, min_rate=min_rate)
    by_editor = {}
    for w in week_edits:
        if w.get("edited"):
            by_editor.setdefault(w.get("editor") or "", []).append(w)
    import schedule_intel as _si
    for editor, wks in by_editor.items():
        mine = {_si.pattern_key(p): p["times"] for p in _patterns_from_weeks(restaurant_id, wks, 1, db_path, min_rate=0)}
        for p in out:
            k = _si.pattern_key(p)
            if k in mine:
                p.setdefault("editors", {})[editor or "unknown"] = mine[k]
    return out


def weeks_phrase(n, of=None) -> str:
    """"3 recent weeks", or "3 of 5 recent weeks" when the change could have
    been made in more weeks than it was (L-6: the denominator, said)."""
    if of is not None and of > n:
        return f"{n} of {of} recent weeks"
    return f"{n} recent week{'s' if n != 1 else ''}"


def _strength(hits_w, opps_w) -> tuple:
    """(rate, confidence) of weighted hits over weighted opportunities."""
    if opps_w <= 0:
        return 0.0, 0.0
    return round(min(1.0, hits_w / opps_w), 3), round(wilson_lower(hits_w, opps_w), 3)


def _patterns_from_weeks(restaurant_id, week_edits, min_repeats=2, db_path=DB_PATH, min_rate=PATTERN_MIN_RATE) -> list:
    """learned_patterns' core over a given set of weeks (every week counts
    toward the denominators; only edited weeks bring hits)."""
    from shift_quality import present_dayparts
    # Once per WEEK, from that week's net change between the draft and the
    # manager's final version: counted per save, taking Bob off, putting him
    # back and taking him off again in one week read as "2 times recently",
    # and an undone edit taught the opposite of what the manager kept.
    counts, last, opps = {}, {}, {}

    def _key(kind, name, date_, start, end, day_hint):
        try:
            day = datetime.strptime(date_, "%Y-%m-%d").strftime("%A")
        except Exception:
            day = day_hint or ""
        part = present_dayparts({"shift_start": start or "", "shift_end": end or ""})[0]
        return (kind, (name or "").strip(), day, part)
    # Oldest week first, so a reversal clears only the evidence before it.
    for w in sorted(week_edits, key=lambda w_: (str(w_.get("week_start") or ""), int(w_.get("history_id") or 0))):
        d = w.get("diff") or {}
        seen, undone = set(), set()
        age = w.get("age_days") or 0
        # The weeks the change could have been made in (L-6): the draft had
        # the person on that slot (taking them off), or the slot ran and the
        # person worked that week without being drafted on it (putting them
        # on). An unedited week is a week the draft was kept.
        base = w.get("base") or []
        here = {(r.get("employee") or "").strip() for r in list(base) + list(w.get("final_all") or [])
                if (r.get("employee") or "").strip()}
        on_slot, slots = set(), set()
        for r in base:
            k = _key("moved_off", r.get("employee"), r.get("date"), r.get("shift_start"), r.get("shift_end"),
                     r.get("day"))
            on_slot.add(k)
            slots.add((k[2], k[3]))
        could = set(on_slot)
        for name in here:
            for day, part in slots:
                if ("moved_off", name, day, part) not in on_slot:
                    could.add(("moved_on", name, day, part))
        for m in d.get("moved") or []:
            seen.add(_key("moved_off", m.get("from"), m.get("date"), m.get("shift_start"), m.get("shift_end"), m.get("day")))
            seen.add(_key("moved_on", m.get("to"), m.get("date"), m.get("shift_start"), m.get("shift_end"), m.get("day")))
            # The same move read backwards: putting Cy on reverses "Cy off".
            undone.add(_key("moved_on", m.get("from"), m.get("date"), m.get("shift_start"), m.get("shift_end"), m.get("day")))
            undone.add(_key("moved_off", m.get("to"), m.get("date"), m.get("shift_start"), m.get("shift_end"), m.get("day")))
        for rm in d.get("removed") or []:
            seen.add(_key("moved_off", rm.get("employee"), rm.get("date"), rm.get("shift_start"), rm.get("shift_end"), rm.get("day")))
            undone.add(_key("moved_on", rm.get("employee"), rm.get("date"), rm.get("shift_start"), rm.get("shift_end"),
                            rm.get("day")))
        # A manager putting someone back on a slot the draft left them off is
        # a reversal of "taken off" (memory re-audit 9/29/26, LOOPS-2): it
        # used to be ignored, so the original edits kept teaching the move
        # the manager was now undoing, until they aged out of the window.
        for ad in d.get("added") or []:
            seen.add(_key("moved_on", ad.get("employee"), ad.get("date"), ad.get("shift_start"), ad.get("shift_end"),
                          ad.get("day")))
            undone.add(_key("moved_off", ad.get("employee"), ad.get("date"), ad.get("shift_start"), ad.get("shift_end"),
                            ad.get("day")))
        # A reversal clears the same editor's earlier evidence only: two
        # editors pulling opposite ways are a conflict for the owner
        # (patterns_for_draft), not one editor changing their mind.
        ed = w.get("editor") or ""
        for k in undone:
            if k in counts:
                counts[k] = [e for e in counts[k] if e[1] != ed]
        for k in seen - undone:
            counts.setdefault(k, []).append((int(w.get("history_id") or 0), ed, recency_weight(age, half_life(k[0])),
                                             str(w.get("week_start") or "")))
        for k in could | (seen - undone):
            opps.setdefault(k, []).append(recency_weight(age, half_life(k[0])))
    out = []
    for k in list(counts):
        last[k] = max((e[0] for e in counts[k]), default=0)
    ranked = sorted(counts.items(), key=lambda kv: -len(kv[1]))
    for (kind, name, day, part), ev in ranked:
        n = len(ev)
        if n < min_repeats or not name:
            continue
        rate, conf = _strength(sum(e[2] for e in ev), sum(opps.get((kind, name, day, part)) or [0.0]))
        if rate < min_rate:
            continue
        of = len(opps.get((kind, name, day, part)) or [])
        pretty = {"morning": "lunch/day", "night": "dinner/night"}.get(part, part)
        lw = last.get((kind, name, day, part))
        common = {"kind": kind, "employee": name, "day": day, "daypart": part, "times": n, "last_week": lw,
                  "last_week_start": max((e[3] for e in ev), default=""), "opportunities": of,
                  "rate": rate, "confidence": conf}
        if kind == "moved_off":
            out.append(dict(common, text=f"The manager has taken {name} off {day} {pretty} in {weeks_phrase(n, of)} — "
                                         f"avoid scheduling them there."))
        else:
            out.append(dict(common, text=f"The manager has put {name} on {day} {pretty} in {weeks_phrase(n, of)} — "
                                         f"a good default for them."))
    out = out[:12]
    # What else the manager keeps settling on — retimes, headcount per role,
    # role changes, a leader swapped onto a busy night — from the net change
    # between each week's draft and its final version. Same shape, so the
    # prompt block, the dismissal key and the Roster screen read them as is.
    try:
        import schedule_learning as _sl
        out += _sl.edit_patterns(restaurant_id, min_repeats=min_repeats, db_path=db_path, week_edits=week_edits,
                                 min_rate=min_rate)
    except Exception as e:          # a read; the draft still gets the patterns above
        log.warning("edit patterns unavailable for restaurant %s: %s", restaurant_id, e)
    return out


# ── what the draft has learned and keeps (memory audit 9/29/26,
#    standing_patterns) ─────────────────────────────────────────────────────
#
# learned_patterns reads only the last EDIT_WEEKS weeks in which the manager
# still had to make the correction. Once the draft learned "Bob off Tuesday
# dinner" no edit repeated it, the evidence aged out after 8 weeks, and Bob
# came back until the manager corrected it twice more. A pattern that
# crosses the repeat floor is now a standing row (schedule_standing_
# patterns): every published week that keeps it confirms it, and it retires
# only when a manager reverses it STANDING_RETIRE_AFTER times — never
# because nobody had to make the correction again. "Make it a rule" writes
# it into the person's own availability (staff_settings) with its author —
# or, for a headcount the manager keeps adding or a start or end they keep
# setting, into a role floor or a role time rule (L-33).
#
# But a pattern the draft merely carried was never tested again: born from
# two edits (perhaps two reactions to call-outs), it lasted forever (schedule
# audit 10/3/26 L-30). Each standing row now keeps its evidence — the
# published weeks that tested it (opportunities) and kept it (hits), an
# unedited week a keep (L-6) — and when a manager's own hand last confirmed
# it (last_hand). Its confidence is the Wilson lower bound of hits over
# opportunities times the half-life decay since last_hand (a person on a
# slot 120 days, a headcount or a start time 180). One half-life with no
# hand confirmation and it is re-tested: the next draft leaves it out once
# (status 'retest', like a suppressed recommendation kind's re-test,
# schedule_intel.suppression_state). The manager putting it back by hand
# confirms it; leaving it out retires it; the owner can say "keep it" or
# "let it go" at any time. Two half-lives with no hand confirmation and
# its confidence under STANDING_RETIRE_CONFIDENCE retire it outright.

STANDING_RETIRE_AFTER = 2
# Two reversals retire a pattern only when they fall inside this many days
# of each other (memory re-audit 9/29/26, FORGET-5): a pattern kept 40
# weeks used to retire on one exception in March and another in November.
STANDING_OVERRIDE_WINDOW_DAYS = 56
STANDING_RETIRE_CONFIDENCE = 0.3
# A re-test that found no week to read (the slot did not run, the person was
# away) ends after this many drafts sent, and the next waits this long.
RETEST_MAX_WEEKS = 3
RETEST_GAP_DAYS = 28
_PERSON_KINDS = ("moved_off", "moved_on")
# The kinds that are about one named person (a rename re-keys them, and
# they go dormant while that person has no shifts).
_NAMED_KINDS = _PERSON_KINDS + ("role_change",)
_HEADCOUNT_KINDS = ("headcount_add", "headcount_cut")


def _standing_rows(conn, restaurant_id):
    try:
        return [dict(r) for r in conn.execute("SELECT * FROM schedule_standing_patterns WHERE restaurant_id=? "
                                              "ORDER BY id", (restaurant_id,)).fetchall()]
    except sqlite3.OperationalError:
        return []


def _detail(row) -> dict:
    """The kind-specific fields a standing row keeps beside its key."""
    try:
        return json.loads((row or {}).get("detail") or "{}") or {}
    except (TypeError, ValueError):
        return {}


def _nk(name) -> str:
    import staff_settings
    return staff_settings.name_key(name)


def _on_slot(p, r) -> bool:
    from shift_quality import present_dayparts
    try:
        day = datetime.strptime(r.get("date") or "", "%Y-%m-%d").strftime("%A")
    except ValueError:
        day = r.get("day") or ""
    return day == p.get("day") and p.get("daypart") in present_dayparts(r)


def _respects(p, rows, need_presence=True):
    """Whether a week's rows keep the pattern: True, False, or None when the
    week says nothing about it. A person pattern is tested only in a week
    the person worked at all (memory re-audit 9/29/26, INVENTORY-2 /
    QUALITY-14): "Bob off Tuesday dinner" used to count as kept every week
    Bob — gone, or renamed — was simply not on the schedule, so a pattern
    about someone who left read as ever more confirmed. `need_presence`
    False reads only the slot (the draft side of a reversal: a draft that
    left Bob off the week entirely did leave him off Tuesday dinner)."""
    kind = p["kind"]
    if kind in _PERSON_KINDS:
        who = _nk(p.get("employee"))
        if need_presence and not any(_nk(r.get("employee")) == who for r in rows):
            return None
        there = any(_on_slot(p, r) and _nk(r.get("employee")) == who for r in rows)
        return (not there) if kind == "moved_off" else there
    if kind in ("retime_start", "retime_end"):
        want = (p.get("time") or "").replace(" ", "").lower()
        mine = [r for r in rows if _on_slot(p, r) and _nk(r.get("role")) == _nk(p.get("role"))]
        if not mine or not want:
            return None
        from schedule_learning import _clock
        col = "shift_start" if kind == "retime_start" else "shift_end"
        return all(_clock(r.get(col)).replace(" ", "").lower() == want for r in mine)
    if kind == "role_change":
        who = _nk(p.get("employee"))
        mine = [r for r in rows if _on_slot(p, r) and _nk(r.get("employee")) == who]
        if not mine:
            return None
        return any(_nk(r.get("role")) == _nk(p.get("role")) for r in mine)
    if kind == "leader_swap":
        names = {_nk(n) for n in (p.get("names") or _detail(p).get("names") or []) if n}
        mine = [r for r in rows if _on_slot(p, r) and (not p.get("role") or _nk(r.get("role")) == _nk(p.get("role")))]
        if not mine or not names:
            return None
        return any(_nk(r.get("employee")) in names for r in mine)
    return None                          # headcount is read against the draft (_week_verdict)


def _heads(p, rows) -> dict:
    """{date: people of the pattern's role on its weekday and daypart}."""
    out = {}
    for r in rows:
        try:
            day = datetime.strptime(r.get("date") or "", "%Y-%m-%d").strftime("%A")
        except ValueError:
            continue
        if day != p.get("day"):
            continue
        out.setdefault(r["date"], set())
        if _on_slot(p, r) and _nk(r.get("role")) == _nk(p.get("role")):
            out[r["date"]].add(_nk(r.get("employee")))
    return {d: len(v) for d, v in out.items()}


def _slot_rows(p, rows) -> list:
    return sorted((r.get("date") or "", _nk(r.get("employee")), _nk(r.get("role")), r.get("shift_start") or "",
                   r.get("shift_end") or "") for r in rows if _on_slot(p, r))


def _week_verdict(p, draft, final, edited):
    """What one published week says about a standing pattern:
      "confirmed"  the manager's own hand kept it — the draft had not already
                   carried it, or they edited that slot and kept it
      "kept"       it held because the draft carried it and nobody touched
                   the slot: it was used, but that is not new evidence
      "over"       the draft kept it and a manager's edit undid it
      None         the week does not test it
    (memory re-audit 9/29/26, QUALITY-14: every week the draft carried a
    pattern used to count as the manager confirming it, so a pattern
    confirmed itself.) Headcount (QUALITY-3) is read against the draft:
    the manager cutting back a person the draft added reverses an add."""
    kind = p["kind"]
    if kind in _HEADCOUNT_KINDS:
        if draft is None:
            return None
        hd, hf = _heads(p, draft), _heads(p, final)
        dates = set(hd) | set(hf)
        if not dates:
            return None
        sign = 1 if kind == "headcount_add" else -1
        moves = [sign * (hf.get(d, 0) - hd.get(d, 0)) for d in dates]
        if any(m < 0 for m in moves):
            return "over" if edited else None
        if any(m > 0 for m in moves):
            return "confirmed"             # the manager added (cut) still more themself
        return "confirmed" if edited and _slot_rows(p, draft) != _slot_rows(p, final) else "kept"
    kept = _respects(p, final)
    if kept is None:
        return None
    drafted = _respects(p, draft, need_presence=False) if draft is not None else None
    if kept:
        if draft is None or drafted is False:
            return "confirmed"
        if edited and _slot_rows(p, draft) != _slot_rows(p, final):
            return "confirmed"
        return "kept"
    if edited and drafted:
        return "over"
    return None


def _retest_verdict(p, draft, final):
    """What a week drafted while the pattern was left out on purpose says
    (L-30): "confirmed" — the manager put it back by hand; "failed" — the
    week went out without it; None — the week cannot tell (the draft carried
    it anyway, the slot did not run, the person was away)."""
    if draft is None:
        return None
    kind = p["kind"]
    if kind in _HEADCOUNT_KINDS:
        hd, hf = _heads(p, draft), _heads(p, final)
        if not (set(hd) | set(hf)):
            return None
        sign = 1 if kind == "headcount_add" else -1
        return "confirmed" if any(sign * (hf.get(d, 0) - hd.get(d, 0)) > 0 for d in set(hd) | set(hf)) else "failed"
    kept, drafted = _respects(p, final), _respects(p, draft, need_presence=False)
    if kept is None or drafted is None or drafted:
        return None
    return "confirmed" if kept else "failed"


def standing_confidence(row, today=None) -> float:
    """The Wilson lower bound of the weeks that kept a standing pattern over
    the weeks that tested it, times its half-life decay since a manager's
    own hand last confirmed it (L-6, L-30). 0-1."""
    today = today or date.today()
    hits, opps = int(_col(row, "hits") or 0), int(_col(row, "opportunities") or 0)
    hand = _when(_col(row, "last_hand") or _col(row, "first_learned"))
    age = (today - hand.date()).days if hand else 0
    return round(wilson_lower(hits, opps) * recency_weight(age, half_life(_col(row, "kind"))), 3)


def _person_id(restaurant_id, name, db_path=DB_PATH):
    if not (name or "").strip():
        return None
    try:
        import people as _people
        kw = {"db_path": db_path} if db_path and db_path != DB_PATH else {}
        return _people.person_id_for(restaurant_id, name, create=False, **kw)
    except Exception:
        return None


def _when(value):
    """A stored timestamp or date as a datetime, or None."""
    s = str(value or "").strip().replace("T", " ")
    for fmt, n in (("%Y-%m-%d %H:%M:%S", 19), ("%Y-%m-%d", 10)):
        try:
            return datetime.strptime(s[:n], fmt)
        except ValueError:
            continue
    return None


def _presence(conn, restaurant_id, db_path=DB_PATH):
    """({name_key: newest date they worked or are scheduled}, the
    restaurant's own newest such date, {name_key of an inactive person}) —
    from shift_facts and the published weeks of the last 120 days."""
    last = {}
    try:
        for r in conn.execute("SELECT employee_key, MAX(business_date) AS d FROM shift_facts WHERE restaurant_id=? "
                              "GROUP BY employee_key", (restaurant_id,)).fetchall():
            if r["d"]:
                last[r["employee_key"]] = str(r["d"])[:10]
    except sqlite3.OperationalError:
        pass
    try:
        pubs = conn.execute("SELECT schedule_csv FROM schedule_history WHERE restaurant_id=? AND published_at IS NOT NULL "
                            "AND COALESCE(week_start, '') >= date('now', '-120 days')", (restaurant_id,)).fetchall()
    except sqlite3.OperationalError:
        pubs = []
    rows = [r for p in pubs for r in rows_from_csv(p["schedule_csv"] or "")]
    for r in canonical_rows(restaurant_id, [rows], db_path=db_path)[0]:
        k, d = _nk(r.get("employee")), (r.get("date") or "")[:10]
        if k and len(d) == 10 and d > last.get(k, ""):
            last[k] = d
    inactive = set()
    try:
        for r in conn.execute("SELECT employee_name FROM staff_settings WHERE restaurant_id=? AND active=0",
                              (restaurant_id,)).fetchall():
            inactive.add(_nk(r["employee_name"]))
    except sqlite3.OperationalError:
        pass
    return last, (max(last.values()) if last else None), inactive


def _live(restaurant_id, db_path=DB_PATH) -> list:
    """The live window's patterns, less the owner's dismissals."""
    import schedule_intel as _si
    gone = _si.dismissed_patterns(restaurant_id, db_path)
    return [p for p in learned_patterns(restaurant_id, db_path=db_path) if _si.pattern_key(p) not in gone]


_OPPOSITE = {"headcount_add": "headcount_cut", "headcount_cut": "headcount_add",
             "moved_off": "moved_on", "moved_on": "moved_off"}


def _is_reversal(p, standing) -> bool:
    """A live pattern that is only the reversal of a retired standing one:
    the manager cutting back the server the draft had learned to add, in
    the very weeks that retired the add, is not a new "cut one" habit —
    read as one, it cut below the old figure (headcount is applied on top
    of the typical count)."""
    opp = _OPPOSITE.get(p.get("kind"))
    if not opp or not standing:
        return False
    for s_ in (standing.values() if isinstance(standing, dict) else standing):
        if s_.get("kind") != opp or s_.get("status") != "retired":
            continue
        if (s_.get("day"), s_.get("daypart")) != (p.get("day"), p.get("daypart")):
            continue
        if _nk(s_.get("role")) != _nk(p.get("role")) or _nk(s_.get("employee")) != _nk(p.get("employee")):
            continue
        if int(p.get("last_week") or 0) <= int(s_.get("retired_week") or 0):
            return True
    return False


def suppressed(p, row, standing=None) -> bool:
    """Whether a live pattern is held back by its standing row (memory
    re-audit 9/29/26, LOOPS-2 / QUALITY-2): a RETIRED row keeps the live
    copy out of the draft until the manager makes the move again in a week
    newer than the one that retired it (the window still holds the original
    edits for up to eight weeks, and used to put them straight back); a
    RULED row is the person's availability now; a DORMANT row is about
    someone with no shifts; a row being RE-TESTED (L-30) is left out of the
    draft on purpose, live copy included."""
    if _is_reversal(p, standing):
        return True
    if not row:
        return False
    if row.get("status") in ("ruled", "dormant", "retest"):
        return True
    if row.get("status") == "retired":
        return int(p.get("last_week") or 0) <= int(row.get("retired_week") or 0)
    return False


def _iso_day(value) -> str:
    """An ISO date from a week_start or a timestamp, '' when unreadable."""
    w = _when(value)
    return w.strftime("%Y-%m-%d") if w else ""


def refresh_standing_patterns(restaurant_id, db_path=DB_PATH, today=None) -> dict:
    """Keep the standing patterns current, in this order:

      1. each published week since a row was last checked is read
         (_week_verdict) — a week nobody of the restaurant looked at (the
         automatic publish with no edit, an admin's hand only) tests
         nothing (L-7, L-8): kept (times_applied, and a hit), confirmed by
         the manager's own hand (times_confirmed, last_hand) or reversed —
         STANDING_RETIRE_AFTER reversals inside STANDING_OVERRIDE_WINDOW_DAYS
         retire it (retired_at, retired_week); a week drafted while the
         pattern was being re-tested confirms it or retires it
         (_retest_verdict, L-30);
      2. every pattern the live window learned (not dismissed) becomes, or
         refreshes, a standing row — a retired one comes back only on
         evidence newer than its retirement, and a live pattern that is only
         the undoing of a retired one is not stored as a habit of its own;
      3. a person pattern goes dormant while its person has no shifts (or is
         inactive) and wakes when they return;
      4. each row's confidence is worked out again (standing_confidence); one
         half-life with no hand confirmation starts a re-test, two with its
         confidence under STANDING_RETIRE_CONFIDENCE retire it (L-30).
    Reading the weeks first means a pattern retired tonight is not revived,
    nor its reversal learned, by tonight's own live window. Returns
    {learned, confirmed, overridden, retired, dormant, woke, retests, decayed}."""
    import schedule_intel as _si
    stats = {"learned": 0, "confirmed": 0, "overridden": 0, "retired": 0, "dormant": 0, "woke": 0,
             "retests": 0, "decayed": 0}
    # A learner of its own: a demo (or an admin-excluded account) learns
    # nothing, not even its own draft (models.learns_for_itself — memory
    # re-audit 9/29/26 INVENTORY-1; patterns_for_draft holds the same line,
    # LOOPS-16). A test-named or internal account learns for itself.
    try:
        import models as _m_elig
        if not _m_elig.learns_for_itself(_m_elig.get_restaurant(restaurant_id, db_path)):
            return dict(stats, skipped="not_eligible")
    except Exception:
        return dict(stats, skipped="not_eligible")
    today = today or date.today()
    _read_weeks(restaurant_id, stats, db_path, today=today)
    live = _live(restaurant_id, db_path)
    pids = {p.get("employee"): _person_id(restaurant_id, p.get("employee"), db_path)
            for p in live if p.get("kind") in _NAMED_KINDS}
    try:
        import people as _people
        gone_days = int(_people.MEMORY_GONE_DAYS)
    except Exception:
        gone_days = 21
    conn = get_conn(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        have = {r["pattern_key"]: r for r in _standing_rows(conn, restaurant_id)}
        now = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
        for p in live:
            k = _si.pattern_key(p)
            if _is_reversal(p, have):
                continue                   # the undoing of a retired pattern, not a habit of its own
            editors = json.dumps(p.get("editors") or {})
            detail = json.dumps({f: p[f] for f in ("was_role", "delta", "names") if p.get(f) not in (None, "", [])})
            lw = int(p.get("last_week") or 0)
            # The weeks that taught it are its evidence: hits over the weeks
            # it could have been made in, and the newest of them is the
            # manager's hand last confirming it (L-6, L-30).
            hand = _iso_day(p.get("last_week_start")) or today.isoformat()
            opps, hits = int(p.get("opportunities") or p.get("times") or 0), int(p.get("times") or 0)
            row = have.get(k)
            if row is None:
                # The weeks that taught it are its evidence, not its
                # confirmations: reading starts after the newest of them.
                conn.execute("INSERT INTO schedule_standing_patterns (restaurant_id, pattern_key, kind, employee, role, "
                             "day, daypart, time, text, editors, first_learned, last_confirmed, person_id, detail, "
                             "checked_through, opportunities, hits, last_hand, source) "
                             "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'learned')",
                             (restaurant_id, k, p["kind"], p.get("employee") or None, p.get("role") or None,
                              p.get("day"), p.get("daypart"), p.get("time") or None, p.get("text"), editors, now, now,
                              pids.get(p.get("employee")), detail, lw, opps, hits, hand))
                stats["learned"] += 1
            elif row["status"] == "retired":
                if lw <= int(row.get("retired_week") or 0):
                    continue               # the edits that retired it are newer than this evidence
                # Made again after it was retired, in a newer week: the
                # manager wants it back.
                conn.execute("UPDATE schedule_standing_patterns SET status='active', times_overridden=0, "
                             "last_overridden=NULL, retired_at=NULL, retired_week=NULL, retired_reason=NULL, "
                             "retest_since=NULL, text=?, editors=?, detail=?, last_confirmed=?, "
                             "checked_through=MAX(checked_through, ?), opportunities=?, hits=?, last_hand=?, "
                             "updated_at=datetime('now') WHERE id=?",
                             (p.get("text"), editors, detail, now, lw, opps, hits, hand, row["id"]))
                stats["learned"] += 1
            elif row["status"] in ("active", "retest"):
                conn.execute("UPDATE schedule_standing_patterns SET text=?, editors=?, detail=?, "
                             "person_id=COALESCE(person_id, ?), updated_at=datetime('now') WHERE id=?",
                             (p.get("text"), editors, detail, pids.get(p.get("employee")), row["id"]))
        # Dormant while their person has no shifts (FORGET-5 / INVENTORY-2 /
        # QUALITY-14): measured against the restaurant's OWN newest shift, so
        # a sync that stopped is not everyone leaving; an inactive person is
        # dormant at once. They wake when the person is back.
        last, newest, inactive = _presence(conn, restaurant_id, db_path)
        if newest:
            edge = (datetime.strptime(newest, "%Y-%m-%d") - timedelta(days=gone_days)).strftime("%Y-%m-%d")
            for r in _standing_rows(conn, restaurant_id):
                if r["kind"] not in _NAMED_KINDS or not (r["employee"] or "").strip() \
                        or r["status"] not in ("active", "retest", "dormant"):
                    continue
                k_ = _nk(r["employee"])
                away = k_ in inactive or last.get(k_, "") < edge
                if away and r["status"] in ("active", "retest"):
                    conn.execute("UPDATE schedule_standing_patterns SET status='dormant', dormant_at=?, "
                                 "retest_since=NULL, updated_at=datetime('now') WHERE id=?", (now, r["id"]))
                    stats["dormant"] += 1
                elif not away and r["status"] == "dormant":
                    conn.execute("UPDATE schedule_standing_patterns SET status='active', dormant_at=NULL, "
                                 "updated_at=datetime('now') WHERE id=?", (r["id"],))
                    stats["woke"] += 1
        # Confidence and decay (L-30): a re-test once a half-life has passed
        # with no hand confirmation; retired once two have and the confidence
        # is under the floor. A row stored before the evidence columns takes
        # its kept weeks as hits and its reversals as misses.
        # A row retired by decay comes back only on a draft made after it.
        newest_week = conn.execute("SELECT MAX(id) FROM schedule_history WHERE restaurant_id=?",
                                   (restaurant_id,)).fetchone()[0] or 0
        for r in _standing_rows(conn, restaurant_id):
            if r["status"] not in ("active", "retest"):
                continue
            if not int(r.get("opportunities") or 0) and int(r.get("times_applied") or 0):
                r["hits"] = int(r["times_applied"])
                r["opportunities"] = int(r["times_applied"]) + int(r.get("times_overridden") or 0)
            if not r.get("last_hand"):
                r["last_hand"] = _iso_day(r.get("last_confirmed") if int(r.get("times_confirmed") or 0)
                                          else r.get("first_learned")) or today.isoformat()
            conf = standing_confidence(r, today)
            hl = half_life(r["kind"])
            hand = _when(r["last_hand"])
            age = (today - hand.date()).days if hand else 0
            status, retest_since, reason = r["status"], r.get("retest_since"), None
            if age >= 2 * hl and conf < STANDING_RETIRE_CONFIDENCE:
                status, retest_since, reason = "retired", None, "decayed"
                stats["decayed"] += 1
                stats["retired"] += 1
            elif status == "active" and age >= hl:
                gap = _when(r.get("last_retest_end"))
                if gap is None or (datetime.utcnow() - gap).days >= RETEST_GAP_DAYS:
                    status, retest_since = "retest", now
                    stats["retests"] += 1
            conn.execute("UPDATE schedule_standing_patterns SET confidence=?, opportunities=?, hits=?, last_hand=?, "
                         "status=?, retest_since=?, retired_reason=COALESCE(?, retired_reason), "
                         "retired_at=CASE WHEN ?='retired' THEN datetime('now') ELSE retired_at END, "
                         "retired_week=CASE WHEN ?='retired' THEN ? ELSE retired_week END, "
                         "updated_at=datetime('now') WHERE id=?",
                         (conf, int(r.get("opportunities") or 0), int(r.get("hits") or 0), r["last_hand"], status,
                          retest_since, reason, status, status, newest_week, r["id"]))
        conn.commit()
    finally:
        conn.close()
    return stats


def _read_weeks(restaurant_id, stats, db_path=DB_PATH, today=None):
    """refresh_standing_patterns' step 1: every active (or re-tested) row
    against the published weeks it has not been checked against yet, each
    week read as the restaurant settled it before it went out
    (learning_weeks: the original draft, the manager's own changes — L-4,
    L-5, L-8, L-26)."""
    conn = get_conn(db_path)
    try:
        rows = [r for r in _standing_rows(conn, restaurant_id) if r["status"] in ("active", "retest")]
    finally:
        conn.close()
    if not rows:
        return
    oldest = min(int(r["checked_through"] or 0) for r in rows)
    conn = get_conn(db_path)
    try:
        weeks = conn.execute(
            "SELECT h.id, h.published_at, h.week_start FROM schedule_history h WHERE h.restaurant_id=? "
            "AND h.published_at IS NOT NULL AND h.id > ? ORDER BY h.id", (restaurant_id, oldest)).fetchall()
    finally:
        conn.close()
    recs = {}
    if weeks:
        recs = {w["history_id"]: w for w in learning_weeks(restaurant_id, db_path=db_path, include_unlooked=True,
                                                           history_ids=[w["id"] for w in weeks], today=today)}
    state = {r["id"]: {"applied": 0, "confirmed": 0, "over": int(r["times_overridden"] or 0),
                       "last_over": _when(r.get("last_overridden")), "new_over": 0,
                       "through": int(r["checked_through"] or 0), "last": None, "retired": None, "row": r,
                       "hits": 0, "opps": 0, "hand": r.get("last_hand"), "reason": None,
                       "retest_since": r.get("retest_since") if r["status"] == "retest" else None,
                       "retest_weeks": 0, "retest_end": None}
             for r in rows}
    for w in weeks:
        rec = recs.get(w["id"])
        when = _when(w["published_at"])
        week_day = _iso_day(w["week_start"]) or (when.strftime("%Y-%m-%d") if when else None)
        for u in state.values():
            r = u["row"]
            if u["retired"] or u["through"] >= w["id"]:
                continue
            u["through"] = w["id"]
            # A week nobody of the restaurant looked at is not the manager's
            # word: it neither keeps nor reverses a standing pattern (an
            # admin's hand through view-as; the automatic publish, L-7).
            if rec is None or not rec["looked_at"]:
                continue
            p = dict(r, names=_detail(r).get("names"))
            draft, final, edited = rec["base"], rec["final"], rec["edited"]
            if u["retest_since"] and str(rec.get("generated_at") or "") >= str(u["retest_since"]):
                # Drafted while it was left out on purpose (L-30).
                rv = _retest_verdict(p, draft, final)
                u["retest_weeks"] += 1
                if rv == "confirmed":
                    u["applied"] += 1
                    u["confirmed"] += 1
                    u["hits"] += 1
                    u["opps"] += 1
                    u["hand"] = week_day or u["hand"]
                    u["last"] = str(w["published_at"])[:19]
                    u["retest_since"], u["retest_end"] = None, True
                    continue
                if rv == "failed":
                    u["opps"] += 1
                    u["retired"], u["reason"] = w["id"], "retest"
                    continue
                if u["retest_weeks"] >= RETEST_MAX_WEEKS:
                    u["retest_since"], u["retest_end"] = None, True
            verdict = _week_verdict(p, draft, final, edited)
            if verdict in ("kept", "confirmed"):
                u["applied"] += 1
                u["hits"] += 1
                u["opps"] += 1
                u["last"] = str(w["published_at"])[:19]
                if verdict == "confirmed":
                    u["confirmed"] += 1
                    u["hand"] = week_day or u["hand"]
            elif verdict == "over":
                u["new_over"] += 1
                u["opps"] += 1
                inside = (u["last_over"] is not None and when is not None
                          and (when - u["last_over"]).days <= STANDING_OVERRIDE_WINDOW_DAYS)
                u["over"] = u["over"] + 1 if inside else 1
                u["last_over"] = when or u["last_over"]
                if u["over"] >= STANDING_RETIRE_AFTER:
                    u["retired"], u["reason"] = w["id"], "reversed"
    conn = get_conn(db_path)
    try:
        for rid_, u in state.items():
            retire = u["retired"] is not None
            ended = bool(u["retest_end"])
            status = "retired" if retire else ("retest" if u["retest_since"] else "active")
            conn.execute("UPDATE schedule_standing_patterns SET times_applied=times_applied+?, "
                         "times_confirmed=COALESCE(times_confirmed, 0)+?, times_overridden=?, last_overridden=?, "
                         "last_confirmed=COALESCE(?, last_confirmed), checked_through=?, "
                         "hits=COALESCE(hits, 0)+?, opportunities=COALESCE(opportunities, 0)+?, last_hand=?, "
                         "status=?, retest_since=?, "
                         "retests=COALESCE(retests, 0)+?, "
                         "last_retest_end=CASE WHEN ? THEN datetime('now') ELSE last_retest_end END, "
                         "retired_reason=CASE WHEN ? THEN ? ELSE retired_reason END, "
                         "retired_at=CASE WHEN ? THEN datetime('now') ELSE retired_at END, "
                         "retired_week=CASE WHEN ? THEN ? ELSE retired_week END, updated_at=datetime('now') "
                         "WHERE id=? AND status IN ('active', 'retest')",
                         (u["applied"], u["confirmed"], u["over"],
                          u["last_over"].strftime("%Y-%m-%d %H:%M:%S") if u["last_over"] else None,
                          u["last"], u["through"], u["hits"], u["opps"], u["hand"], status,
                          u["retest_since"], 1 if (ended or (retire and u["reason"] == "retest")) else 0,
                          1 if (ended or (retire and u["reason"] == "retest")) else 0,
                          1 if retire else 0, u["reason"], 1 if retire else 0, 1 if retire else 0,
                          u["retired"], rid_))
            stats["confirmed"] += u["confirmed"]
            stats["overridden"] += u["new_over"]
            stats["retired"] += 1 if retire else 0
        conn.commit()
    finally:
        conn.close()


RULE_KINDS = _PERSON_KINDS + ("headcount_add", "retime_start", "retime_end")


def standing_patterns(restaurant_id, db_path=DB_PATH, include_retired=True) -> list:
    """The standing patterns for the owner's screen: [{key, kind, employee,
    role, day, daypart, text, editors, first_learned, last_confirmed (M/D/YY
    and ISO), times_applied, times_confirmed, times_overridden, status
    (active / retest / retired / ruled / dormant), retired_at, retired_week,
    retired_reason, dormant_since, can_be_rule, rule}] — and its evidence
    (L-6, L-30): opportunities, hits, confidence (0-1, Wilson × decay),
    last_hand (M/D/YY and ISO: a manager's own hand last confirmed it),
    retest_since (M/D/YY while the next draft leaves it out to check it is
    still wanted), source (learned | owner_said). `include_retired` False
    leaves the retired ones out."""
    from time_utils import mdy
    conn = get_conn(db_path)
    try:
        rows = _standing_rows(conn, restaurant_id)
    finally:
        conn.close()
    out = []
    for r in rows:
        if r["status"] == "retired" and not include_retired:
            continue
        try:
            editors = json.loads(r["editors"] or "{}") or {}
        except (TypeError, ValueError):
            editors = {}
        det = _detail(r)
        if r["kind"] in _HEADCOUNT_KINDS and det.get("delta") in (None, ""):
            # A row stored before `detail` existed: its size is in its words
            # ("The manager has added 1 Server to …" / "has cut 2 …").
            import re as _re
            m_ = _re.search(r"has (added|cut) (\d+) ", r["text"] or "")
            if m_:
                det["delta"] = int(m_.group(2)) * (1 if m_.group(1) == "added" else -1)
        out.append({"key": r["pattern_key"], "kind": r["kind"], "employee": r["employee"], "role": r["role"],
                    "day": r["day"], "daypart": r["daypart"], "time": r["time"], "text": r["text"], "editors": editors,
                    "was_role": det.get("was_role"), "delta": det.get("delta"), "names": det.get("names"),
                    "first_learned": mdy(r["first_learned"]), "first_learned_iso": str(r["first_learned"])[:10],
                    "last_confirmed": mdy(r["last_confirmed"]), "last_confirmed_iso": str(r["last_confirmed"])[:10],
                    "times_applied": r["times_applied"], "times_confirmed": int(r.get("times_confirmed") or 0),
                    "times_overridden": r["times_overridden"],
                    "status": r["status"], "retired_week": r.get("retired_week"),
                    "retired_at": mdy(r["retired_at"]) if r.get("retired_at") else None,
                    "retired_reason": r.get("retired_reason"),
                    "dormant_since": mdy(r["dormant_at"]) if r.get("dormant_at") else None,
                    "opportunities": int(r.get("opportunities") or 0), "hits": int(r.get("hits") or 0),
                    "confidence": r.get("confidence"),
                    "last_hand": mdy(r["last_hand"]) if r.get("last_hand") else None,
                    "last_hand_iso": str(r["last_hand"])[:10] if r.get("last_hand") else None,
                    "retest_since": mdy(r["retest_since"]) if r.get("retest_since") else None,
                    "source": r.get("source") or "learned",
                    "can_be_rule": r["kind"] in RULE_KINDS and r["status"] in ("active", "retest"),
                    "rule": ({"note": r["rule_note"], "by": r["ruled_by"]} if r["status"] == "ruled" else None)})
    return out


def patterns_for_draft(restaurant_id, db_path=DB_PATH) -> tuple:
    """(patterns, conflicts) the next draft reads: the live window's
    patterns plus every active standing one it no longer shows (worded as
    standing, with when it was learned and last kept), less what the owner
    dismissed, less what a standing row holds back (`suppressed`: retired
    without newer evidence, ruled, dormant) and less any pair two editors
    pull opposite ways — those are `conflicts`, for the owner to settle,
    never the model to guess.

    A restaurant that does not learn for itself (a demo, or an account an
    admin excluded — models.learns_for_itself) gets none: the standing rows
    were gated and the live window's patterns were not, so a demo's own
    draft still learned from its edits (memory re-audit 9/29/26, LOOPS-16)."""
    import schedule_intel as _si
    try:
        import models as _m_elig
        if not _m_elig.learns_for_itself(_m_elig.get_restaurant(restaurant_id, db_path)):
            return [], []
    except Exception:
        return [], []
    gone = _si.dismissed_patterns(restaurant_id, db_path)
    standing = {s_["key"]: s_ for s_ in standing_patterns(restaurant_id, db_path=db_path)}
    live = [p for p in learned_patterns(restaurant_id, db_path=db_path)
            if _si.pattern_key(p) not in gone and not suppressed(p, standing.get(_si.pattern_key(p)), standing)]
    keys = {_si.pattern_key(p) for p in live}
    out = list(live)
    for s_ in standing.values():
        if s_["key"] in keys or s_["key"] in gone or s_["status"] != "active":
            continue
        p = {k: s_[k] for k in ("kind", "employee", "role", "day", "daypart", "time", "editors", "was_role", "delta")}
        p["times"] = s_["times_applied"]
        p["standing"] = True
        p["text"] = (f"Standing preference (learned {s_['first_learned']}, last kept {s_['last_confirmed']}): "
                     + (s_["text"] or "").split(" — ")[0].replace("The manager has ", "the manager ").rstrip(".")
                     + (" — keep it." if s_["kind"] != "moved_on" else " — a good default."))
        out.append(p)
    # Opposite moves on one person, day and daypart: two editors disagree.
    conflicts, drop = [], set()
    idx = {}
    for p in out:
        if p.get("kind") in _PERSON_KINDS:
            idx.setdefault(((p.get("employee") or "").strip().lower(), p.get("day"), p.get("daypart")), []).append(p)
    for (who, day, part), ps in idx.items():
        kinds = {p["kind"] for p in ps}
        if kinds == set(_PERSON_KINDS):
            off = next(p for p in ps if p["kind"] == "moved_off")
            on = next(p for p in ps if p["kind"] == "moved_on")
            conflicts.append({"employee": off.get("employee"), "day": day, "daypart": part,
                              "off_by": sorted((off.get("editors") or {}).keys()),
                              "on_by": sorted((on.get("editors") or {}).keys()),
                              "text": f"{off.get('employee')} on {day} {part}: taken off in some weeks and put on in "
                                      f"others — which should the draft do?"})
            drop |= {id(off), id(on)}
    return [p for p in out if id(p) not in drop], conflicts


def _settled_heads(restaurant_id, role, day, part, db_path=DB_PATH, weeks=8) -> list:
    """The number of `role` people the manager settled on for `day`'s
    `part` in each recent week they finished (the learnable final — their
    own changes only), newest weeks first."""
    out = []
    for w in reversed(learning_weeks(restaurant_id, weeks, db_path)):
        days = {}
        for r in w["final"]:
            if _nk(r.get("role")) != _nk(role):
                continue
            try:
                wd = datetime.strptime(r.get("date") or "", "%Y-%m-%d").strftime("%A")
            except ValueError:
                continue
            from shift_quality import present_dayparts
            if wd == day and part in present_dayparts(r):
                days.setdefault(r["date"], set()).add(_nk(r.get("employee")))
        out.extend(len(v) for _d, v in sorted(days.items()))
    return out


def make_rule(restaurant_id, pattern_key, user=None, db_path=DB_PATH) -> dict:
    """The owner's "make it a rule": a learned pattern turned into something
    the code enforces, with its author — and the standing row is marked
    `ruled`, read from then on as the rule, not a learned habit:
      moved_off / moved_on  the person's own settings: taken off a day and
                            daypart becomes that daypart unavailable on that
                            day; put on becomes a preferred daypart;
      headcount_add         a role floor (schedule_note_rules.add_rule) at
                            the number of that role the manager settled on
                            there (the median of their recent weeks) — the
                            draft is built to it, the trim never goes under
                            it, a draft short of it is a coverage breach
                            (schedule audit 10/3/26 L-33);
      retime_start / _end   a role start or end rule
                            (schedule_note_rules.add_time_rule) the draft
                            is retimed to and checked against (L-33).
    A cut is not a rule: Cavnar AI holds minimums, not maximums, so it stays
    a learned habit and the refusal says so."""
    import staff_settings
    conn = get_conn(db_path)
    try:
        row = next((r for r in _standing_rows(conn, restaurant_id) if r["pattern_key"] == pattern_key), None)
    finally:
        conn.close()
    if row is None:
        # A live pattern the nightly job has not stored yet.
        refresh_standing_patterns(restaurant_id, db_path=db_path)
        conn = get_conn(db_path)
        try:
            row = next((r for r in _standing_rows(conn, restaurant_id) if r["pattern_key"] == pattern_key), None)
        finally:
            conn.close()
    if row is None:
        raise ValueError("That pattern isn't one the draft has learned.")
    kind = row["kind"]
    if kind == "headcount_cut":
        raise ValueError("A cut can't be a rule: Cavnar AI holds staffing minimums, not maximums. The draft keeps "
                         "drafting fewer there as a learned habit.")
    if kind not in RULE_KINDS:
        raise ValueError("Only a pattern about one person, a headcount the manager keeps adding, or a start or end "
                         "time can become a rule.")
    if kind in _PERSON_KINDS and not row["employee"]:
        raise ValueError("Only a pattern about one person can become their rule.")
    who = (user or {}).get("username") or (user or {}).get("email") or "owner"
    name, day, part = row["employee"], row["day"], row["daypart"]
    text = (row["text"] or "").split(" — ")[0].strip().rstrip(".")
    entity, field_, subject = "roster", "availability", name
    if kind == "moved_off":
        st = staff_settings.for_name(restaurant_id, name, db_path=db_path) or {}
        avail = dict(st.get("daypart_availability") or {})
        cur = avail.get(day, "any")
        other = "morning" if part == "night" else "night"
        # Not `part` on `day` any more: from "any" that leaves the other
        # daypart; if `part` was all they could do that day, the day is off;
        # already unable to work it, nothing changes.
        avail[day] = other if cur == "any" else ("off" if cur == part else cur)
        if all(v == "off" for v in {**{d: "any" for d in staff_settings.DAYS}, **avail}.values()):
            raise ValueError(f"That would leave {name} no day they can work.")
        staff_settings.upsert(restaurant_id, name, daypart_availability=avail, updated_by=who, db_path=db_path)
        note = f"{day}: {avail[day]} only" if avail[day] != "off" else f"{day}: off"
    elif kind == "moved_on":
        st = staff_settings.for_name(restaurant_id, name, db_path=db_path) or {}
        prefs = sorted(set(st.get("preferred_dayparts") or []) | {part})
        staff_settings.upsert(restaurant_id, name, preferred_dayparts=prefs, updated_by=who, db_path=db_path)
        note = f"prefers {part}s"
    elif kind == "headcount_add":
        import schedule_note_rules
        heads = sorted(_settled_heads(restaurant_id, row["role"], day, part, db_path=db_path))
        n = heads[len(heads) // 2] if heads else 0
        if n < 1:
            try:
                n = int(_detail(row).get("delta") or 1)
            except (TypeError, ValueError):
                n = 1
        rule = schedule_note_rules.add_rule(restaurant_id, row["role"], min(n, schedule_note_rules.MIN_MAX),
                                            [part], days=[day], scope="every",
                                            source_text=f"Learned from your edits: {text}", user=user,
                                            db_path=db_path)
        note = rule["words"]
        entity, field_, subject = "rules", "role_floor", row["role"]
    else:
        import schedule_note_rules
        rule = schedule_note_rules.add_time_rule(restaurant_id, row["role"],
                                                 "start" if kind == "retime_start" else "end", row["time"], part,
                                                 days=[day], source_text=f"Learned from your edits: {text}",
                                                 user=user, db_path=db_path)
        note = rule["words"]
        entity, field_, subject = "rules", "role_time", row["role"]
    conn = get_conn(db_path)
    try:
        conn.execute("UPDATE schedule_standing_patterns SET status='ruled', rule_note=?, ruled_by=?, retest_since=NULL, "
                     "updated_at=datetime('now') WHERE id=?", (note, who[:120], row["id"]))
        conn.commit()
    finally:
        conn.close()
    try:
        import change_log
        if user:
            change_log.record(restaurant_id, entity, field_, {"pattern": row["text"]}, {"rule": note},
                              subject=subject, user=user)
        else:
            change_log.record(restaurant_id, entity, field_, {"pattern": row["text"]}, {"rule": note},
                              subject=subject, source="owner")
    except Exception as e:
        log.warning("change_log failed for restaurant %s: %s", restaurant_id, e)
    return {"ok": True, "employee": name, "rule": note, "kind": kind}


def owner_said_pattern(restaurant_id, p, authority, history_id=None, who=None, db_path=DB_PATH) -> str:
    """The owner's one-tap "always" for a change they just made (schedule
    audit 10/3/26 L-35): the pattern becomes a standing row at once, with
    the owner's authority — the first weeks carry the most explicit intent,
    and two weeks of edits were needed before anything was learned. An
    existing row of the same key is confirmed by hand (active again,
    last_hand today); a ruled one is already a rule. Returns the key."""
    import schedule_intel as _si
    k = _si.pattern_key(p)
    today = date.today().isoformat()
    now = datetime.utcnow().strftime("%Y-%m-%d %H:%M:%S")
    detail = json.dumps({f: p[f] for f in ("was_role", "delta", "names") if p.get(f) not in (None, "", [])})
    conn = get_conn(db_path)
    try:
        conn.execute("BEGIN IMMEDIATE")
        row = next((r for r in _standing_rows(conn, restaurant_id) if r["pattern_key"] == k), None)
        if row is None:
            conn.execute("INSERT INTO schedule_standing_patterns (restaurant_id, pattern_key, kind, employee, role, day, "
                         "daypart, time, text, editors, first_learned, last_confirmed, detail, checked_through, "
                         "times_confirmed, opportunities, hits, last_hand, source, authority, person_id) "
                         "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,1,1,1,?,'owner_said',?,?)",
                         (restaurant_id, k, p["kind"], p.get("employee") or None, p.get("role") or None, p.get("day"),
                          p.get("daypart"), p.get("time") or None, p.get("text"),
                          json.dumps({(who or "owner"): 1}), now, now, detail, int(history_id or 0), today,
                          authority, _person_id(restaurant_id, p.get("employee"), db_path)
                          if p.get("employee") else None))
        elif row["status"] != "ruled":
            conn.execute("UPDATE schedule_standing_patterns SET status='active', last_hand=?, retest_since=NULL, "
                         "retired_at=NULL, retired_week=NULL, retired_reason=NULL, times_overridden=0, "
                         "times_confirmed=COALESCE(times_confirmed, 0)+1, last_confirmed=?, "
                         "authority=COALESCE(authority, ?), detail=CASE WHEN ?='{}' THEN detail ELSE ? END, "
                         "updated_at=datetime('now') WHERE id=?", (today, now, authority, detail, detail, row["id"]))
        conn.commit()
    finally:
        conn.close()
    return k


def confirm_standing(restaurant_id, pattern_key, user=None, keep=True, db_path=DB_PATH) -> dict:
    """The owner's answer to a standing pattern (L-30, "ask the owner"):
    `keep` — a hand confirmation (last_hand today, any re-test ended, the
    draft carries it again); not `keep` — "let it go", retired by the owner.
    An admin's answer (view-as) changes nothing: it is not the restaurant's
    word (L-8, L-10). Raises ValueError with the owner's words."""
    if authority_of(user) == "admin":
        raise ValueError("An answer through view-as doesn't change what the draft keeps — the owner answers this one.")
    conn = get_conn(db_path)
    try:
        row = next((r for r in _standing_rows(conn, restaurant_id) if r["pattern_key"] == pattern_key), None)
        if row is None or row["status"] not in ("active", "retest", "dormant"):
            raise ValueError("That pattern isn't one the draft is keeping now.")
        today = date.today().isoformat()
        if keep:
            conn.execute("UPDATE schedule_standing_patterns SET status=CASE WHEN status='dormant' THEN 'dormant' "
                         "ELSE 'active' END, last_hand=?, retest_since=NULL, "
                         "last_retest_end=CASE WHEN retest_since IS NOT NULL THEN datetime('now') ELSE last_retest_end END, "
                         "times_confirmed=COALESCE(times_confirmed, 0)+1, last_confirmed=datetime('now'), "
                         "updated_at=datetime('now') WHERE id=?", (today, row["id"]))
        else:
            # Only a draft made after this answer can bring it back.
            newest = conn.execute("SELECT MAX(id) FROM schedule_history WHERE restaurant_id=?",
                                  (restaurant_id,)).fetchone()[0] or 0
            conn.execute("UPDATE schedule_standing_patterns SET status='retired', retired_reason='owner', "
                         "retired_at=datetime('now'), retired_week=?, retest_since=NULL, updated_at=datetime('now') "
                         "WHERE id=?", (newest, row["id"]))
        conn.commit()
    finally:
        conn.close()
    return {"ok": True, "key": pattern_key, "kept": bool(keep)}


# ── an admin's saves, counted as the owner's (schedule audit 10/3/26 L-8) ────

def admin_saves_pending(restaurant_id, db_path=DB_PATH) -> dict:
    """What an admin saved through view-as or support that no learner counts
    until the account holder adopts it: {versions, weeks, answers}."""
    conn = get_conn(db_path)
    try:
        v = conn.execute("SELECT COUNT(*) AS n, COUNT(DISTINCT history_id) AS w FROM schedule_versions WHERE "
                         "restaurant_id=? AND adopted_at IS NULL AND (saved_authority='admin' OR saved_by LIKE "
                         "'support:%') AND reason IN ('edited', 'published')", (restaurant_id,)).fetchone()
        try:
            a = conn.execute("SELECT COUNT(*) FROM schedule_edit_answers WHERE restaurant_id=? AND authority='admin' "
                             "AND adopted_at IS NULL AND answer IS NOT NULL", (restaurant_id,)).fetchone()[0]
        except sqlite3.OperationalError:
            a = 0
    finally:
        conn.close()
    return {"versions": int(v["n"] or 0), "weeks": int(v["w"] or 0), "answers": int(a or 0)}


def adopt_admin_saves(restaurant_id, user, history_id=None, db_path=DB_PATH) -> dict:
    """The account holder counts, as their own, the schedule saves an admin
    made through view-as or support — usually Will editing beside Erik — so
    the edits teach the draft (mirrors models.adopt_admin_ratings). Only a
    principal signed in as themselves may; `history_id` limits it to one
    week. The one-tap answers an admin gave on those saves count too (an
    "always" becomes the owner's standing pattern, a call-off is recorded).
    Returns {versions, answers}."""
    from permissions import answer_authority
    from models import CapabilityError
    if answer_authority(user) != "principal":
        raise CapabilityError("Only the account holder, signed in as themselves, can count these edits as theirs.")
    who = ((user.get("username") or user.get("email") or "owner") + " (confirmed)")[:120]
    week = " AND history_id=?" if history_id else ""
    args = (who, restaurant_id) + ((int(history_id),) if history_id else ())
    conn = get_conn(db_path)
    try:
        n = conn.execute("UPDATE schedule_versions SET adopted_by=?, adopted_at=datetime('now') WHERE restaurant_id=? "
                         "AND adopted_at IS NULL AND (saved_authority='admin' OR saved_by LIKE 'support:%')" + week,
                         args).rowcount
        try:
            ids = [r[0] for r in conn.execute("SELECT id FROM schedule_edit_answers WHERE restaurant_id=? AND "
                                              "authority='admin' AND adopted_at IS NULL AND answer IS NOT NULL" + week,
                                              args[1:]).fetchall()]
            conn.executemany("UPDATE schedule_edit_answers SET adopted_by=?, adopted_at=datetime('now') WHERE id=?",
                             [(who, i) for i in ids])
        except sqlite3.OperationalError:
            ids = []
        conn.commit()
    finally:
        conn.close()
    if ids:
        import schedule_learning as _sl
        for i in ids:
            _sl.apply_edit_answer(restaurant_id, i, db_path=db_path)
    return {"versions": n, "answers": len(ids)}


# ── the owner throwing a draft away (schedule audit 10/3/26 L-26) ───────────

REJECTION_KINDS = ("redo_days", "draft_discarded")


def record_rejection(restaurant_id, history_id, kind, dates=None, reason_chip=None, reason_text=None, user=None,
                     db_path=DB_PATH):
    """Keep the owner's "redo these days" (with their optional reason) or a
    whole-week regeneration over a draft never sent, and observe it
    (schedule_memory: redo_days per date, draft_discarded). The learners
    read a redo to link the new draft to the original it replaced
    (learning_weeks), and the edit predictor counts the redone days' rows as
    rows the manager changed. Returns the row id, or None for a draft that
    is not this restaurant's. Raises on a write that failed."""
    if kind not in REJECTION_KINDS:
        raise ValueError(f"unknown rejection {kind!r}")
    days = sorted({str(d)[:10] for d in (dates or []) if str(d)[:10]})
    chip = " ".join(str(reason_chip or "").split())[:40] or None
    words = " ".join(str(reason_text or "").split())[:300] or None
    auth = authority_of(user) if user else SYSTEM
    who = ((user or {}).get("username") or (user or {}).get("email") or "") if isinstance(user, dict) else ""
    conn = get_conn(db_path)
    try:
        h = conn.execute("SELECT id, week_start, published_at FROM schedule_history WHERE id=? AND restaurant_id=?",
                         (int(history_id), restaurant_id)).fetchone()
        if h is None:
            return None
        cur = conn.execute("INSERT INTO schedule_rejections (restaurant_id, history_id, week_start, kind, dates_json, "
                           "reason_chip, reason_text, authority, requested_by) VALUES (?,?,?,?,?,?,?,?,?)",
                           (restaurant_id, h["id"], h["week_start"], kind, json.dumps(days) if days else None, chip,
                            words, auth, who[:120] or None))
        conn.commit()
        row_id = cur.lastrowid
        phase = POST_PUBLISH if h["published_at"] else PRE_PUBLISH
    finally:
        conn.close()
    import schedule_memory
    value = {"reason": chip, "text": words}
    if kind == "redo_days":
        for d in days:
            schedule_memory.observe(restaurant_id, "redo_days", week_start=h["week_start"], date=d,
                                    value=dict(value, dates=days), origin="manager", phase=phase, authority=auth,
                                    editor=who or None, source="redo_route", history_id=h["id"], db_path=db_path)
    else:
        schedule_memory.observe(restaurant_id, "draft_discarded", week_start=h["week_start"], value=value,
                                origin="manager", phase=phase, authority=auth, editor=who or None,
                                source="generate_route", history_id=h["id"], db_path=db_path)
    return row_id


def open_draft_for_week(restaurant_id, week_monday, db_path=DB_PATH):
    """The id of the week's draft in force that was never sent, or None."""
    conn = get_conn(db_path)
    try:
        row = conn.execute("SELECT id FROM schedule_history WHERE restaurant_id=? AND week_start=? AND published_at IS "
                           "NULL AND superseded_by IS NULL ORDER BY id DESC LIMIT 1",
                           (restaurant_id, str(week_monday)[:10])).fetchone()
    finally:
        conn.close()
    return row["id"] if row else None


# ── what a week's first publish says, into the observation log ─────────────

_OBS_KIND = {"moved": "edit_move", "removed": "edit_move", "added": "edit_move", "retimed": "edit_retime",
             "role_changed": "edit_role"}


def observe_publish(restaurant_id, history_id, authority=None, editor=None, db_path=DB_PATH) -> int:
    """At a week's FIRST publish, every change in it from the original
    draft into the observation log (schedule_memory.observe, phase
    pre_publish): the manager's own changes (edit_move / edit_retime /
    edit_role, and edit_headcount per slot), the changes Cavnar AI made that
    the owner kept (cavnar_change_saved, origin cavnar — trust in that move,
    never the manager's habit), an admin's (authority admin: stored, not
    counted until adopted), and the publish itself (week_published — whether
    a person looked: the automatic publish is authority 'system', L-7).
    Returns how many facts were observed."""
    recs = learning_weeks(restaurant_id, db_path=db_path, history_ids=[history_id], include_unlooked=True)
    if not recs:
        return 0
    rec = recs[0]
    import schedule_memory
    from shift_quality import present_dayparts
    n = 0
    for e in rec["items"]:
        it = e["item"]
        kind = "cavnar_change_saved" if e["origin"] == "cavnar" else _OBS_KIND.get(e["kind"], "edit_move")
        start = it.get("shift_start") or it.get("new_start") or ""
        person = it.get("employee") or it.get("to") or it.get("from")
        value = {"change": e["kind"], "excluded": e["excluded"], "source": e["source"]}
        for f in ("from", "to", "old_start", "new_start", "old_end", "new_end", "old_role", "new_role"):
            if it.get(f):
                value[f] = it[f]
        schedule_memory.observe(restaurant_id, kind, week_start=rec["week_start"], date=it.get("date"),
                                daypart=(present_dayparts({"shift_start": start, "shift_end": it.get("shift_end") or ""})
                                         or ["unknown"])[0],
                                role=it.get("role") or it.get("new_role"), person=person, value=value,
                                origin=e["origin"], phase=PRE_PUBLISH, authority=e["authority"] or "principal",
                                editor=e["editor"], source="publish", history_id=history_id, db_path=db_path)
        n += 1
    import schedule_learning as _sl
    before, _sb = _sl._slot_heads(rec["base"])
    after, _sa = _sl._slot_heads(rec["final"])
    for k in sorted(set(before) | set(after)):
        delta = len(after.get(k, ())) - len(before.get(k, ()))
        if delta:
            d, part, rl = k
            schedule_memory.observe(restaurant_id, "edit_headcount", week_start=rec["week_start"], date=d,
                                    daypart=part, role=rl, value={"delta": delta}, origin="manager",
                                    phase=PRE_PUBLISH, authority=authority or "principal", editor=rec["editor"],
                                    source="publish", history_id=history_id, db_path=db_path)
            n += 1
    schedule_memory.observe(restaurant_id, "week_published", week_start=rec["week_start"],
                            value={"looked_at": rec["looked_at"], "edited": rec["edited"],
                                   "redo_dates": rec["redo_dates"], "excluded": rec["excluded"]},
                            origin="system" if authority == SYSTEM else "manager", phase=PRE_PUBLISH,
                            authority=authority or "principal", editor=editor, source="publish",
                            history_id=history_id, db_path=db_path)
    return n + 1


def prompt_block(patterns: list) -> str:
    # Headcount the manager keeps adding or cutting is already IN the
    # requirements table (labor.apply_learned_headcount); saying it again
    # here asked the model for the same extra person twice.
    patterns = [p for p in (patterns or []) if p.get("kind") not in ("headcount_add", "headcount_cut")]
    if not patterns:
        return ""
    return ("\n\nWHAT THE MANAGER KEEPS CHANGING (learned from their edits to past drafts — treat as a "
            "standing preference unless a rule above contradicts it):\n" + "\n".join("  - " + p["text"] for p in patterns))
