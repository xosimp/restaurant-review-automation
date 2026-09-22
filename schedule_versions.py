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
from datetime import datetime

from models import get_conn, DB_PATH

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
                retimed.append({"date": r["date"], "day": r.get("day"), "employee": r.get("employee"),
                                "from": f"{r['shift_start']}–{r['shift_end']}", "to": f"{a['shift_start']}–{a['shift_end']}",
                                "hours_delta": round(_hours(a) - _hours(r), 1)})
                used_added.add(j)
                removed.remove(r)
                break
    added = [a for j, a in enumerate(added) if j not in used_added]

    def _by(rows, field):
        out = {}
        for r in rows:
            k = r.get(field) or ""
            out[k] = round(out.get(k, 0.0) + _hours(r), 1)
        return out

    hb, ha = _by(before_rows, "day"), _by(after_rows, "day")
    rb, ra = _by(before_rows, "role"), _by(after_rows, "role")
    return {
        "added": added, "removed": removed, "moved": moved, "retimed": retimed,
        "hours_before": round(sum(_hours(r) for r in before_rows), 1),
        "hours_after": round(sum(_hours(r) for r in after_rows), 1),
        "hours_by_day": {d: [hb.get(d, 0.0), ha.get(d, 0.0)] for d in sorted(set(hb) | set(ha))},
        "hours_by_role": {r: [rb.get(r, 0.0), ra.get(r, 0.0)] for r in sorted(set(rb) | set(ra))},
        "changes": len(added) + len(removed) + len(moved) + len(retimed),
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
    if d["added"] and not by_day:
        lines.append(f"{len(d['added'])} shift{'s' if len(d['added']) != 1 else ''} added.")
    if d["removed"] and not by_day:
        lines.append(f"{len(d['removed'])} shift{'s' if len(d['removed']) != 1 else ''} removed.")
    if not lines:
        lines.append(unchanged)
    return lines[:limit]


# ── versions ───────────────────────────────────────────────────────────────

def append(restaurant_id, history_id, reason, schedule_csv, quality=None, saved_by=None, db_path=DB_PATH) -> int:
    """Store one more state of a schedule, with its diff against the last."""
    conn = get_conn(db_path)
    try:
        last = conn.execute("SELECT version, schedule_csv FROM schedule_versions WHERE history_id=? "
                            "ORDER BY version DESC LIMIT 1", (history_id,)).fetchone()
        version = (last["version"] + 1) if last else 1
        d = diff(rows_from_csv(last["schedule_csv"]), rows_from_csv(schedule_csv)) if last else None
        cur = conn.execute(
            "INSERT INTO schedule_versions (restaurant_id, history_id, version, reason, schedule_csv, quality_json, "
            "diff_json, saved_by) VALUES (?,?,?,?,?,?,?,?)",
            (restaurant_id, history_id, version, reason, schedule_csv,
             json.dumps(quality) if quality else None, json.dumps(d) if d else None,
             (saved_by or "").strip()[:120] or None))
        conn.commit()
        return cur.lastrowid
    finally:
        conn.close()


def list_versions(restaurant_id, history_id, db_path=DB_PATH) -> list:
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


# ── what the manager keeps changing ────────────────────────────────────────

def learned_patterns(restaurant_id, weeks=8, min_repeats=2, db_path=DB_PATH) -> list:
    """Edits that recur across recent weeks: the same person moved off the
    same weekday and daypart at least `min_repeats` times. Returned as
    facts, and rendered into the prompt so the next draft starts where the
    manager keeps ending up."""
    from shift_quality import daypart_of
    conn = get_conn(db_path)
    try:
        rows = conn.execute(
            "SELECT v.diff_json, v.created_at FROM schedule_versions v JOIN schedule_history h ON h.id=v.history_id "
            "WHERE v.restaurant_id=? AND v.reason='edited' AND v.created_at >= datetime('now', ?) "
            "ORDER BY v.created_at DESC LIMIT 200", (restaurant_id, f"-{int(weeks) * 7} days")).fetchall()
    finally:
        conn.close()
    counts = {}
    for r in rows:
        try:
            d = json.loads(r["diff_json"] or "{}") or {}
        except Exception:
            continue
        for m in d.get("moved") or []:
            try:
                day = datetime.strptime(m["date"], "%Y-%m-%d").strftime("%A")
            except Exception:
                day = m.get("day") or ""
            part = daypart_of(m.get("shift_start", ""))
            k = ("moved_off", (m.get("from") or "").strip(), day, part)
            counts[k] = counts.get(k, 0) + 1
            k2 = ("moved_on", (m.get("to") or "").strip(), day, part)
            counts[k2] = counts.get(k2, 0) + 1
        for rm in d.get("removed") or []:
            try:
                day = datetime.strptime(rm["date"], "%Y-%m-%d").strftime("%A")
            except Exception:
                day = rm.get("day") or ""
            k = ("moved_off", (rm.get("employee") or "").strip(), day, daypart_of(rm.get("shift_start", "")))
            counts[k] = counts.get(k, 0) + 1
    out = []
    for (kind, name, day, part), n in sorted(counts.items(), key=lambda kv: -kv[1]):
        if n < min_repeats or not name:
            continue
        pretty = {"morning": "lunch/day", "night": "dinner/night"}.get(part, part)
        if kind == "moved_off":
            out.append({"kind": kind, "employee": name, "day": day, "daypart": part, "times": n,
                        "text": f"The manager has taken {name} off {day} {pretty} {n} times recently — avoid scheduling them there."})
        else:
            out.append({"kind": kind, "employee": name, "day": day, "daypart": part, "times": n,
                        "text": f"The manager has put {name} on {day} {pretty} {n} times recently — a good default for them."})
    return out[:12]


def prompt_block(patterns: list) -> str:
    if not patterns:
        return ""
    return ("\n\nWHAT THE MANAGER KEEPS CHANGING (learned from their edits to past drafts — treat as a "
            "standing preference unless a rule above contradicts it):\n" + "\n".join("  - " + p["text"] for p in patterns))
