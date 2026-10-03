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
import sqlite3
from datetime import datetime, timedelta

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
# The learners' filter: a week any support save touched teaches nothing —
# one saved through view-as (saved_by SUPPORT_PREFIX, M1) or with an
# admin's authority (saved_authority, permissions.answer_authority, M3).
NOT_SUPPORT_TOUCHED_SQL = ("history_id NOT IN (SELECT sv.history_id FROM schedule_versions sv "
                           "WHERE sv.saved_by LIKE 'support:%' OR sv.saved_authority = 'admin')")
# The same rule for one version: an admin's save is never the manager's
# word (SHARED_MEM: an admin's answer never trains the owner's preferences).
LEARNABLE_SQL = "COALESCE(saved_authority, '') <> 'admin' AND COALESCE(saved_by, '') NOT LIKE 'support:%'"


def authority_of(user) -> str:
    """permissions.answer_authority for a save: admin | principal |
    delegate; "system" for none (the generator, a job)."""
    if not isinstance(user, dict) or not user:
        return "system"
    try:
        import permissions
        return permissions.answer_authority(user)
    except Exception:
        return "delegate"


def _insert_version(conn, restaurant_id, history_id, reason, schedule_csv, quality=None, saved_by=None,
                    saved_authority=None):
    last = conn.execute("SELECT version, schedule_csv FROM schedule_versions WHERE history_id=? "
                        "ORDER BY version DESC LIMIT 1", (history_id,)).fetchone()
    version = (last["version"] + 1) if last else 1
    before_rows = rows_from_csv(last["schedule_csv"]) if last else []
    after_rows = rows_from_csv(schedule_csv)
    d = diff(before_rows, after_rows) if last else None
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
        "diff_json, saved_by, saved_authority) VALUES (?,?,?,?,?,?,?,?,?)",
        (restaurant_id, history_id, version, reason, schedule_csv,
         json.dumps(quality) if quality else None, json.dumps(d) if d else None,
         (saved_by or "").strip()[:120] or None, saved_authority))
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
           saved_authority=None) -> int:
    """Store one more state of a schedule, with its diff against the last."""
    conn = get_conn(db_path)
    try:
        row_id, _v = _insert_version(conn, restaurant_id, history_id, reason, schedule_csv, quality, saved_by,
                                     saved_authority)
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
             expected_version=None, saved_authority=None) -> int:
    """Overwrite the stored week AND append its version row, on the caller's
    connection, inside the caller's write transaction (open it with BEGIN
    IMMEDIATE and commit after). The two used to be separate commits, so a
    lost version append left a week the conflict check could not see, and
    two saves checked against one version both landed (SCHED-19, SCHED-5).

    expected_version: the version the edit was made against; a newer one
    raises StaleVersion and nothing is written. hours_scheduled follows the
    rows, so a budget blocker judged at generation time cannot outlive the
    edit that fixed it (SCHED-17) — and so does its split by pay,
    hours_hourly / hours_salaried (schedule audit 10/3/26 E-7, P-6).
    Returns the new version number."""
    if expected_version is not None:
        latest = latest_version(conn, history_id)
        if latest and int(expected_version) != latest:
            raise StaleVersion(latest)
    hours = round(sum(_hours(r) for r in rows_from_csv(schedule_csv)), 1)
    try:
        split = _models_mod.history_hours(restaurant_id, schedule_csv)
    except Exception as e:
        # Unknown, never a guess: a reader falls back to the all-in figure.
        print(f"[schedule versions] hours split unavailable rid={restaurant_id}: {e!r}")
        split = {"hourly": None, "salaried": None}
    old = conn.execute("SELECT schedule_csv FROM schedule_history WHERE id=? AND restaurant_id=?",
                       (history_id, restaurant_id)).fetchone()
    cur = conn.execute(
        "UPDATE schedule_history SET schedule_csv=?, quality_json=COALESCE(?, quality_json), hours_scheduled=?, "
        "hours_hourly=?, hours_salaried=?, edited_at=datetime('now'), edited_by=? WHERE id=? AND restaurant_id=?",
        (schedule_csv, json.dumps(quality) if quality else None, hours, split["hourly"], split["salaried"],
         (saved_by or "").strip()[:120] or None, history_id, restaurant_id))
    if cur.rowcount != 1:
        raise LookupError("that schedule is gone")
    _row_id, version = _insert_version(conn, restaurant_id, history_id, reason, schedule_csv, quality, saved_by,
                                       saved_authority)
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


def shift_changes(before_rows, after_rows) -> dict:
    """{"people": [names], "dates": [iso dates]} whose shifts differ between
    two versions of a week — unsent_changes' own per-person comparison, for
    two weeks given as rows: a regenerated draft against the published week
    it would replace (schedule audit 10/3/26 E-22 — its changed dates are
    what the notice-window warning is about)."""
    names = {}
    for r in list(before_rows or []) + list(after_rows or []):
        n = " ".join((r.get("employee") or "").split())
        if n:
            names.setdefault(n.casefold(), n)
    people, dates = [], set()
    for k, n in sorted(names.items()):
        was, now = _person_shifts(before_rows or [], k), _person_shifts(after_rows or [], k)
        if was != now:
            people.append(n)
            dates |= {s[0] for s in set(was) ^ set(now) if s[0]}
    return {"people": people, "dates": sorted(dates)}


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


def acceptance(restaurant_id, weeks=8, db_path=DB_PATH) -> dict:
    """How much of each generated draft survived to the week that was sent.

    Per published week (newest first, the latest publish of each calendar
    week): the changes between the generated version and the published one,
    and the share of generated rows that went out untouched. The trend
    compares the older half of the weeks with the newer half — a rising
    share is a draft that needs less fixing. A week with no generated
    version (an uploaded schedule) has no draft to judge and is skipped.
    Pure read."""
    conn = get_conn(db_path)
    try:
        hist = conn.execute(
            "SELECT id, week_start, schedule_csv FROM schedule_history WHERE restaurant_id=? AND published_at IS NOT NULL "
            "ORDER BY week_start DESC, id DESC LIMIT ?", (restaurant_id, int(weeks) * 3)).fetchall()
        picked, seen = [], set()
        for h in hist:
            wk = h["week_start"] or f"#{h['id']}"
            if wk in seen:
                continue
            seen.add(wk)
            picked.append(h)
            if len(picked) >= int(weeks):
                break
        versions = {}
        if picked:
            marks = ",".join("?" for _ in picked)
            for v in conn.execute(f"SELECT history_id, version, reason, schedule_csv FROM schedule_versions WHERE restaurant_id=? "
                                  f"AND history_id IN ({marks}) AND {NOT_SUPPORT_TOUCHED_SQL} ORDER BY version",
                                  (restaurant_id, *[h["id"] for h in picked])).fetchall():
                versions.setdefault(v["history_id"], []).append(v)
    finally:
        conn.close()
    out = []
    for h in picked:
        vs = versions.get(h["id"]) or []
        gen = next((v for v in vs if v["reason"] == "generated"), None)
        if gen is None:
            continue
        pub = next((v for v in reversed(vs) if v["reason"] == "published"), None)
        g_rows = rows_from_csv(gen["schedule_csv"])
        p_rows = rows_from_csv(pub["schedule_csv"] if pub else h["schedule_csv"])
        d = diff(g_rows, p_rows)
        same = _same_rows(g_rows, p_rows)
        out.append({"history_id": h["id"], "week_start": h["week_start"], "changes": d["changes"],
                    "rows_generated": len(g_rows), "rows_published": len(p_rows), "unchanged_rows": same,
                    "unchanged_share": round(same / len(g_rows), 3) if g_rows else None,
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
    return {"available": bool(out), "weeks": out, "trend": trend,
            "mean_unchanged_share": round(sum(shares) / len(shares), 3) if shares else None,
            "mean_changes": round(sum(w["changes"] for w in out) / len(out), 1) if out else None}


# ── what the manager keeps changing ────────────────────────────────────────

def learned_patterns(restaurant_id, weeks=8, min_repeats=2, db_path=DB_PATH) -> list:
    """Edits that recur across recent weeks: the same person moved off the
    same weekday and daypart at least `min_repeats` times (moved_off /
    moved_on, up to 12), then the kinds schedule_learning.edit_patterns
    reads from each week's draft-to-final change (retime_start /
    retime_end, headcount_add / headcount_cut, role_change, leader_swap,
    up to 8). Returned as facts, and rendered into the prompt so the next
    draft starts where the manager keeps ending up.

    Each carries `editors` ({who saved those weeks: weeks}) — per editor as
    well as per restaurant (memory audit 9/29/26, standing_patterns): two
    GMs with opposite habits on alternate weeks used to blend into one
    "manager"."""
    from schedule_learning import edited_weeks
    week_edits = edited_weeks(restaurant_id, weeks, db_path)
    out = _patterns_from_weeks(restaurant_id, week_edits, min_repeats, db_path)
    by_editor = {}
    for w in week_edits:
        by_editor.setdefault(w.get("editor") or "", []).append(w)
    import schedule_intel as _si
    for editor, wks in by_editor.items():
        mine = {_si.pattern_key(p): p["times"] for p in _patterns_from_weeks(restaurant_id, wks, 1, db_path)}
        for p in out:
            k = _si.pattern_key(p)
            if k in mine:
                p.setdefault("editors", {})[editor or "unknown"] = mine[k]
    return out


def _patterns_from_weeks(restaurant_id, week_edits, min_repeats=2, db_path=DB_PATH) -> list:
    """learned_patterns' core over a given set of edited weeks."""
    from shift_quality import present_dayparts
    # Once per WEEK, from that week's net change between the draft and the
    # manager's final version: counted per save, taking Bob off, putting him
    # back and taking him off again in one week read as "2 times recently",
    # and an undone edit taught the opposite of what the manager kept.
    counts, last = {}, {}
    # Oldest week first, so a reversal clears only the evidence before it.
    for w in sorted(week_edits, key=lambda w_: int(w_.get("history_id") or 0)):
        d = w.get("diff") or {}
        seen, undone = set(), set()

        def _key(kind, name, date, start, end, day_hint):
            try:
                day = datetime.strptime(date, "%Y-%m-%d").strftime("%A")
            except Exception:
                day = day_hint or ""
            part = present_dayparts({"shift_start": start or "", "shift_end": end or ""})[0]
            return (kind, (name or "").strip(), day, part)
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
            counts.setdefault(k, []).append((int(w.get("history_id") or 0), ed))
    for k in list(counts):
        last[k] = max((e[0] for e in counts[k]), default=0)
        counts[k] = len(counts[k])
    out = []
    for (kind, name, day, part), n in sorted(counts.items(), key=lambda kv: -kv[1]):
        if n < min_repeats or not name:
            continue
        pretty = {"morning": "lunch/day", "night": "dinner/night"}.get(part, part)
        lw = last.get((kind, name, day, part))
        if kind == "moved_off":
            out.append({"kind": kind, "employee": name, "day": day, "daypart": part, "times": n, "last_week": lw,
                        "text": f"The manager has taken {name} off {day} {pretty} in {n} recent weeks — avoid scheduling them there."})
        else:
            out.append({"kind": kind, "employee": name, "day": day, "daypart": part, "times": n, "last_week": lw,
                        "text": f"The manager has put {name} on {day} {pretty} in {n} recent weeks — a good default for them."})
    out = out[:12]
    # What else the manager keeps settling on — retimes, headcount per role,
    # role changes, a leader swapped onto a busy night — from the net change
    # between each week's draft and its final version. Same shape, so the
    # prompt block, the dismissal key and the Roster screen read them as is.
    try:
        import schedule_learning as _sl
        out += _sl.edit_patterns(restaurant_id, min_repeats=min_repeats, db_path=db_path, week_edits=week_edits)
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
# it into the person's own availability (staff_settings) with its author.

STANDING_RETIRE_AFTER = 2
# Two reversals retire a pattern only when they fall inside this many days
# of each other (memory re-audit 9/29/26, FORGET-5): a pattern kept 40
# weeks used to retire on one exception in March and another in November.
STANDING_OVERRIDE_WINDOW_DAYS = 56
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
    if kind == "retime_start":
        want = (p.get("time") or "").replace(" ", "").lower()
        mine = [r for r in rows if _on_slot(p, r) and _nk(r.get("role")) == _nk(p.get("role"))]
        if not mine or not want:
            return None
        from schedule_learning import _clock
        return all(_clock(r.get("shift_start")).replace(" ", "").lower() == want for r in mine)
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
    someone with no shifts."""
    if _is_reversal(p, standing):
        return True
    if not row:
        return False
    if row.get("status") in ("ruled", "dormant"):
        return True
    if row.get("status") == "retired":
        return int(p.get("last_week") or 0) <= int(row.get("retired_week") or 0)
    return False


def refresh_standing_patterns(restaurant_id, db_path=DB_PATH) -> dict:
    """Keep the standing patterns current, in this order:

      1. each published week since a row was last checked is read
         (_week_verdict): kept (times_applied), confirmed by the manager's
         own hand (times_confirmed, last_confirmed) or reversed —
         STANDING_RETIRE_AFTER reversals inside STANDING_OVERRIDE_WINDOW_DAYS
         retire it (retired_at, retired_week);
      2. every pattern the live window learned (not dismissed) becomes, or
         refreshes, a standing row — a retired one comes back only on
         evidence newer than its retirement, and a live pattern that is only
         the undoing of a retired one is not stored as a habit of its own;
      3. a person pattern goes dormant while its person has no shifts (or is
         inactive) and wakes when they return.
    Reading the weeks first means a pattern retired tonight is not revived,
    nor its reversal learned, by tonight's own live window. Returns
    {learned, confirmed, overridden, retired, dormant, woke}."""
    import schedule_intel as _si
    stats = {"learned": 0, "confirmed": 0, "overridden": 0, "retired": 0, "dormant": 0, "woke": 0}
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
    _read_weeks(restaurant_id, stats, db_path)
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
            row = have.get(k)
            if row is None:
                # The weeks that taught it are its evidence, not its
                # confirmations: reading starts after the newest of them.
                conn.execute("INSERT INTO schedule_standing_patterns (restaurant_id, pattern_key, kind, employee, role, "
                             "day, daypart, time, text, editors, first_learned, last_confirmed, person_id, detail, "
                             "checked_through) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                             (restaurant_id, k, p["kind"], p.get("employee") or None, p.get("role") or None,
                              p.get("day"), p.get("daypart"), p.get("time") or None, p.get("text"), editors, now, now,
                              pids.get(p.get("employee")), detail, lw))
                stats["learned"] += 1
            elif row["status"] == "retired":
                if lw <= int(row.get("retired_week") or 0):
                    continue               # the edits that retired it are newer than this evidence
                # Made again after it was retired, in a newer week: the
                # manager wants it back.
                conn.execute("UPDATE schedule_standing_patterns SET status='active', times_overridden=0, "
                             "last_overridden=NULL, retired_at=NULL, retired_week=NULL, text=?, editors=?, detail=?, "
                             "last_confirmed=?, checked_through=MAX(checked_through, ?), updated_at=datetime('now') "
                             "WHERE id=?", (p.get("text"), editors, detail, now, lw, row["id"]))
                stats["learned"] += 1
            elif row["status"] == "active":
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
                        or r["status"] not in ("active", "dormant"):
                    continue
                k_ = _nk(r["employee"])
                away = k_ in inactive or last.get(k_, "") < edge
                if away and r["status"] == "active":
                    conn.execute("UPDATE schedule_standing_patterns SET status='dormant', dormant_at=?, "
                                 "updated_at=datetime('now') WHERE id=?", (now, r["id"]))
                    stats["dormant"] += 1
                elif not away and r["status"] == "dormant":
                    conn.execute("UPDATE schedule_standing_patterns SET status='active', dormant_at=NULL, "
                                 "updated_at=datetime('now') WHERE id=?", (r["id"],))
                    stats["woke"] += 1
        conn.commit()
    finally:
        conn.close()
    return stats


def _read_weeks(restaurant_id, stats, db_path=DB_PATH):
    """refresh_standing_patterns' step 1: every active row against the
    published weeks it has not been checked against yet."""
    conn = get_conn(db_path)
    try:
        rows = [r for r in _standing_rows(conn, restaurant_id) if r["status"] == "active"]
    finally:
        conn.close()
    if not rows:
        return
    # The published weeks each row has not yet been checked against, with
    # their generated draft and the manager's final.
    oldest = min(int(r["checked_through"] or 0) for r in rows)
    conn = get_conn(db_path)
    try:
        weeks = conn.execute(
            "SELECT h.id, h.published_at FROM schedule_history h WHERE h.restaurant_id=? AND h.published_at IS NOT NULL "
            "AND h.id > ? ORDER BY h.id", (restaurant_id, oldest)).fetchall()
        vers = {}
        if weeks:
            marks = ",".join("?" for _ in weeks)
            for v in conn.execute(f"SELECT history_id, version, reason, schedule_csv, saved_authority, saved_by FROM "
                                  f"schedule_versions WHERE restaurant_id=? AND history_id IN ({marks}) ORDER BY version",
                                  (restaurant_id, *[w["id"] for w in weeks])).fetchall():
                vers.setdefault(v["history_id"], []).append(v)
    finally:
        conn.close()
    names = canonical_rows(restaurant_id, [rows_from_csv(v["schedule_csv"]) for vs in vers.values() for v in vs],
                           db_path=db_path, mapping_only=True)
    state = {r["id"]: {"applied": 0, "confirmed": 0, "over": int(r["times_overridden"] or 0),
                       "last_over": _when(r.get("last_overridden")), "new_over": 0,
                       "through": int(r["checked_through"] or 0), "last": None, "retired": None, "row": r}
             for r in rows}
    for w in weeks:
        vs = vers.get(w["id"]) or []
        # A week an admin's hand saved or published (view-as) is not the
        # manager's word: it neither keeps nor reverses a standing pattern.
        admin_hand = any((v["saved_authority"] or "") == "admin"
                         or str(v["saved_by"] or "").startswith(SUPPORT_PREFIX) for v in vs)
        gen = next((v for v in vs if v["reason"] == "generated"), None)
        pub = next((v for v in reversed(vs) if v["reason"] == "published"), None) or (vs[-1] if vs else None)
        if pub is None:
            continue
        final = canonical_rows(restaurant_id, [rows_from_csv(pub["schedule_csv"])], mapping=names)[0]
        edited = any(v["reason"] == "edited" for v in vs)
        draft = canonical_rows(restaurant_id, [rows_from_csv(gen["schedule_csv"])], mapping=names)[0] if gen else None
        when = _when(w["published_at"])
        for u in state.values():
            r = u["row"]
            if u["retired"] or u["through"] >= w["id"]:
                continue
            u["through"] = w["id"]
            if admin_hand:
                continue
            p = dict(r, names=_detail(r).get("names"))
            verdict = _week_verdict(p, draft, final, edited)
            if verdict in ("kept", "confirmed"):
                u["applied"] += 1
                u["last"] = str(w["published_at"])[:19]
                if verdict == "confirmed":
                    u["confirmed"] += 1
            elif verdict == "over":
                u["new_over"] += 1
                inside = (u["last_over"] is not None and when is not None
                          and (when - u["last_over"]).days <= STANDING_OVERRIDE_WINDOW_DAYS)
                u["over"] = u["over"] + 1 if inside else 1
                u["last_over"] = when or u["last_over"]
                if u["over"] >= STANDING_RETIRE_AFTER:
                    u["retired"] = w["id"]
    conn = get_conn(db_path)
    try:
        for rid_, u in state.items():
            retire = u["retired"] is not None
            conn.execute("UPDATE schedule_standing_patterns SET times_applied=times_applied+?, "
                         "times_confirmed=COALESCE(times_confirmed, 0)+?, times_overridden=?, last_overridden=?, "
                         "last_confirmed=COALESCE(?, last_confirmed), checked_through=?, "
                         "status=CASE WHEN ? THEN 'retired' ELSE status END, "
                         "retired_at=CASE WHEN ? THEN datetime('now') ELSE retired_at END, "
                         "retired_week=CASE WHEN ? THEN ? ELSE retired_week END, updated_at=datetime('now') "
                         "WHERE id=? AND status='active'",
                         (u["applied"], u["confirmed"], u["over"],
                          u["last_over"].strftime("%Y-%m-%d %H:%M:%S") if u["last_over"] else None,
                          u["last"], u["through"], 1 if retire else 0, 1 if retire else 0, 1 if retire else 0,
                          u["retired"], rid_))
            stats["confirmed"] += u["confirmed"]
            stats["overridden"] += u["new_over"]
            stats["retired"] += 1 if retire else 0
        conn.commit()
    finally:
        conn.close()


def standing_patterns(restaurant_id, db_path=DB_PATH, include_retired=True) -> list:
    """The standing patterns for the owner's screen: [{key, kind, employee,
    role, day, daypart, text, editors, first_learned, last_confirmed (M/D/YY
    and ISO), times_applied, times_confirmed, times_overridden, status
    (active / retired / ruled / dormant), retired_at, retired_week,
    dormant_since, can_be_rule, rule}]. `include_retired` False leaves the
    retired ones out."""
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
                    "dormant_since": mdy(r["dormant_at"]) if r.get("dormant_at") else None,
                    "can_be_rule": r["kind"] in _PERSON_KINDS and r["status"] == "active",
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


def make_rule(restaurant_id, pattern_key, user=None, db_path=DB_PATH) -> dict:
    """The owner's "make it a rule": a person pattern written into that
    person's own settings, with its author — taken off a day and daypart
    becomes that daypart unavailable on that day; put on becomes a
    preferred daypart. The standing row is marked `ruled` and the rule reads
    from then on as the person's availability, not a learned habit."""
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
    if row["kind"] not in _PERSON_KINDS or not row["employee"]:
        raise ValueError("Only a pattern about one person can become their rule.")
    who = (user or {}).get("username") or (user or {}).get("email") or "owner"
    name, day, part = row["employee"], row["day"], row["daypart"]
    st = staff_settings.for_name(restaurant_id, name, db_path=db_path) or {}
    if row["kind"] == "moved_off":
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
    else:
        prefs = sorted(set(st.get("preferred_dayparts") or []) | {part})
        staff_settings.upsert(restaurant_id, name, preferred_dayparts=prefs, updated_by=who, db_path=db_path)
        note = f"prefers {part}s"
    conn = get_conn(db_path)
    try:
        conn.execute("UPDATE schedule_standing_patterns SET status='ruled', rule_note=?, ruled_by=?, "
                     "updated_at=datetime('now') WHERE id=?", (note, who[:120], row["id"]))
        conn.commit()
    finally:
        conn.close()
    try:
        import change_log
        if user:
            change_log.record(restaurant_id, "roster", "availability", {"pattern": row["text"]}, {"rule": note},
                              subject=name, user=user)
        else:
            change_log.record(restaurant_id, "roster", "availability", {"pattern": row["text"]}, {"rule": note},
                              subject=name, source="owner")
    except Exception as e:
        log.warning("change_log failed for restaurant %s: %s", restaurant_id, e)
    return {"ok": True, "employee": name, "rule": note}


def prompt_block(patterns: list) -> str:
    # Headcount the manager keeps adding or cutting is already IN the
    # requirements table (labor.apply_learned_headcount); saying it again
    # here asked the model for the same extra person twice.
    patterns = [p for p in (patterns or []) if p.get("kind") not in ("headcount_add", "headcount_cut")]
    if not patterns:
        return ""
    return ("\n\nWHAT THE MANAGER KEEPS CHANGING (learned from their edits to past drafts — treat as a "
            "standing preference unless a rule above contradicts it):\n" + "\n".join("  - " + p["text"] for p in patterns))
