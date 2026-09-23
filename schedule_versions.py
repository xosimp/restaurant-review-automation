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
from datetime import datetime

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


def _insert_version(conn, restaurant_id, history_id, reason, schedule_csv, quality=None, saved_by=None):
    last = conn.execute("SELECT version, schedule_csv FROM schedule_versions WHERE history_id=? "
                        "ORDER BY version DESC LIMIT 1", (history_id,)).fetchone()
    version = (last["version"] + 1) if last else 1
    before_rows = rows_from_csv(last["schedule_csv"]) if last else []
    after_rows = rows_from_csv(schedule_csv)
    d = diff(before_rows, after_rows) if last else None
    # The advice the week carried before this save — read before the new
    # version (whose own quality is judged on the edited rows) lands.
    open_recs = _open_recommendations(conn, history_id) if (last and reason == "edited") else []
    cur = conn.execute(
        "INSERT INTO schedule_versions (restaurant_id, history_id, version, reason, schedule_csv, quality_json, "
        "diff_json, saved_by) VALUES (?,?,?,?,?,?,?,?)",
        (restaurant_id, history_id, version, reason, schedule_csv,
         json.dumps(quality) if quality else None, json.dumps(d) if d else None,
         (saved_by or "").strip()[:120] or None))
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
        conn.execute("RELEASE SAVEPOINT implied_accept")
    except sqlite3.Error as e:
        conn.execute("ROLLBACK TO SAVEPOINT implied_accept")
        conn.execute("RELEASE SAVEPOINT implied_accept")
        log.warning("implied acceptance not recorded for restaurant %s: %s", restaurant_id, e)
        return []
    return written


def append(restaurant_id, history_id, reason, schedule_csv, quality=None, saved_by=None, db_path=DB_PATH) -> int:
    """Store one more state of a schedule, with its diff against the last."""
    conn = get_conn(db_path)
    try:
        row_id, _v = _insert_version(conn, restaurant_id, history_id, reason, schedule_csv, quality, saved_by)
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
             expected_version=None) -> int:
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
    cur = conn.execute(
        "UPDATE schedule_history SET schedule_csv=?, quality_json=COALESCE(?, quality_json), hours_scheduled=?, "
        "edited_at=datetime('now'), edited_by=? WHERE id=? AND restaurant_id=?",
        (schedule_csv, json.dumps(quality) if quality else None, hours,
         (saved_by or "").strip()[:120] or None, history_id, restaurant_id))
    if cur.rowcount != 1:
        raise LookupError("that schedule is gone")
    _row_id, version = _insert_version(conn, restaurant_id, history_id, reason, schedule_csv, quality, saved_by)
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
                                  f"AND history_id IN ({marks}) ORDER BY version", (restaurant_id, *[h["id"] for h in picked])).fetchall():
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
    draft starts where the manager keeps ending up."""
    from shift_quality import present_dayparts
    from schedule_learning import edited_weeks
    # Once per WEEK, from that week's net change between the draft and the
    # manager's final version: counted per save, taking Bob off, putting him
    # back and taking him off again in one week read as "2 times recently",
    # and an undone edit taught the opposite of what the manager kept.
    counts = {}
    for w in edited_weeks(restaurant_id, weeks, db_path):
        d = w.get("diff") or {}
        seen = set()

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
        for rm in d.get("removed") or []:
            seen.add(_key("moved_off", rm.get("employee"), rm.get("date"), rm.get("shift_start"), rm.get("shift_end"), rm.get("day")))
        for k in seen:
            counts[k] = counts.get(k, 0) + 1
    out = []
    for (kind, name, day, part), n in sorted(counts.items(), key=lambda kv: -kv[1]):
        if n < min_repeats or not name:
            continue
        pretty = {"morning": "lunch/day", "night": "dinner/night"}.get(part, part)
        if kind == "moved_off":
            out.append({"kind": kind, "employee": name, "day": day, "daypart": part, "times": n,
                        "text": f"The manager has taken {name} off {day} {pretty} in {n} recent weeks — avoid scheduling them there."})
        else:
            out.append({"kind": kind, "employee": name, "day": day, "daypart": part, "times": n,
                        "text": f"The manager has put {name} on {day} {pretty} in {n} recent weeks — a good default for them."})
    out = out[:12]
    # What else the manager keeps settling on — retimes, headcount per role,
    # role changes, a leader swapped onto a busy night — from the net change
    # between each week's draft and its final version. Same shape, so the
    # prompt block, the dismissal key and the Roster screen read them as is.
    try:
        import schedule_learning as _sl
        out += _sl.edit_patterns(restaurant_id, weeks=weeks, min_repeats=min_repeats, db_path=db_path)
    except Exception as e:          # a read; the draft still gets the patterns above
        log.warning("edit patterns unavailable for restaurant %s: %s", restaurant_id, e)
    return out


def prompt_block(patterns: list) -> str:
    # Headcount the manager keeps adding or cutting is already IN the
    # requirements table (labor.apply_learned_headcount); saying it again
    # here asked the model for the same extra person twice.
    patterns = [p for p in (patterns or []) if p.get("kind") not in ("headcount_add", "headcount_cut")]
    if not patterns:
        return ""
    return ("\n\nWHAT THE MANAGER KEEPS CHANGING (learned from their edits to past drafts — treat as a "
            "standing preference unless a rule above contradicts it):\n" + "\n".join("  - " + p["text"] for p in patterns))
