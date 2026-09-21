"""Decision records — one object for what this restaurant decided, and
what came of it.

The moat audit's first finding: the pieces of a decision were held in
four tables that never met. A recommendation was hidden (home_dismissals,
with a count and now a reason), or answered "done"/"not for us"; an
outcome tracker measured what happened next (recommendation_outcomes);
an issue was raised and resolved (ops_issues); a proposal from Ask was
confirmed or dismissed (ask_cavnar_actions). Each was correct alone and
none could be read as "the history of what we decided here".

This module joins them by key into one record per decision and hands
the agent a short, dated history — so Ask reasons from *this restaurant's
decisions*, not only its numbers. Nothing here is generated; every field
is read from a row a person or a job wrote.
"""
from models import get_conn, DB_PATH

MAX_CONTEXT_LINES = 12


def _first(d, *keys):
    for k in keys:
        try:
            v = d[k]
        except (KeyError, IndexError, TypeError):
            continue
        if v not in (None, ""):
            return v
    return None


def _humanize(key):
    """"trim_day:Monday" reads as "Trim day: Monday" — the key is the subject."""
    key = str(key or "")
    head, _, tail = key.partition(":")
    head = head.replace("_", " ").strip().capitalize()
    return f"{head}: {tail}" if tail else head


def history(restaurant_id, limit=40, db_path=DB_PATH):
    """Decision records, newest first. Each: {key, title, kind, asked_on,
    answer, reason, times_hidden, outcome, issue}."""
    conn = get_conn(db_path)
    recs = {}

    def rec(key, title=None, kind="recommendation", when=None):
        r = recs.get(key)
        if r is None:
            r = recs[key] = {"key": key, "title": title or key, "kind": kind, "asked_on": when,
                             "answer": None, "reason": None, "times_hidden": 0, "outcome": None, "issue": None}
        if title and (r["title"] == key or not r["title"]):
            r["title"] = title
        if when and (not r["asked_on"] or when < r["asked_on"]):
            r["asked_on"] = when
        return r

    try:
        # What the owner said to a recommendation.
        try:
            for row in conn.execute("SELECT key, kind, dismissed_at, expires_at, COALESCE(times,1) AS times "
                                    "FROM home_dismissals WHERE restaurant_id=?", (restaurant_id,)).fetchall():
                r = rec(row["key"], title=_humanize(row["key"]), when=str(row["dismissed_at"] or "")[:10])
                r["times_hidden"] = int(row["times"] or 1)
                r["answer"] = {"done": "done", "not_for_us": "not for us"}.get(row["kind"], "hidden")
                r["answered_on"] = str(row["dismissed_at"] or "")[:10]
        except Exception:
            pass
        # What happened after they acted (or after the product saw them act).
        try:
            for row in conn.execute("SELECT source, source_key, title, metric, status, verdict, delta, dollars_monthly, "
                                    "started_on, evaluate_on FROM recommendation_outcomes WHERE restaurant_id=? "
                                    "ORDER BY created_at DESC LIMIT 200", (restaurant_id,)).fetchall():
                r = rec(row["source_key"], title=row["title"], when=str(row["started_on"] or "")[:10])
                r["outcome"] = {"metric": row["metric"], "status": row["status"], "verdict": row["verdict"],
                                "delta": row["delta"], "dollars_monthly": row["dollars_monthly"],
                                "started_on": row["started_on"], "evaluate_on": row["evaluate_on"],
                                "observed": row["source"] == "observed"}
                if not r["answer"]:
                    r["answer"] = "tracking" if row["status"] != "evaluated" else "measured"
        except Exception:
            pass
        # Issues raised from it, and how they ended.
        try:
            for row in conn.execute("SELECT * FROM ops_issues WHERE restaurant_id=? ORDER BY id DESC LIMIT 200",
                                    (restaurant_id,)).fetchall():
                key = _first(row, "source_key") or f"issue:{row['id']}"
                r = rec(key, title=_first(row, "title"), kind=_first(row, "kind") or "issue",
                        when=str(_first(row, "created_at") or "")[:10])
                if _first(row, "kind"):
                    r["kind"] = _first(row, "kind")     # the issue's kind is the more specific one
                r["issue"] = {"status": _first(row, "status"), "resolved_on": str(_first(row, "resolved_at") or "")[:10] or None,
                              "note": _first(row, "resolution_note")}
                if not r["answer"]:
                    r["answer"] = "resolved" if _first(row, "status") == "resolved" else "open"
        except Exception:
            pass
        # The why, when the owner gave one ("Not doing “X”: reason").
        try:
            for row in conn.execute("SELECT fact, created_at FROM ask_memory WHERE restaurant_id=? AND kind='preference'",
                                    (restaurant_id,)).fetchall():
                fact = row["fact"] or ""
                if fact.startswith("Not doing “") and "”: " in fact:
                    title, reason = fact[len("Not doing “"):].split("”: ", 1)
                    day = str(row["created_at"] or "")[:10]
                    target = None
                    for r in recs.values():
                        if title in (r["title"], r["key"], _humanize(r["key"])):
                            target = r
                            break
                    if target is None:
                        # The reason is written in the same call as the "not for us"
                        # (home_brief.dismiss), under the card's display title, which
                        # the dismissal row does not keep. Same day, same answer, no
                        # reason yet, and exactly one candidate: that is the one.
                        same_day = [r for r in recs.values() if r.get("answer") == "not for us"
                                    and r.get("answered_on") == day and not r.get("reason")]
                        if len(same_day) == 1:
                            target = same_day[0]
                    if target is not None:
                        target["reason"] = reason
                        if target["title"] == _humanize(target["key"]):
                            target["title"] = title
                    else:
                        r = rec("pref:" + title[:60], title=title, kind="preference", when=str(row["created_at"] or "")[:10])
                        r["answer"] = "not for us"; r["reason"] = reason
        except Exception:
            pass
        # Proposals from Ask, settled.
        try:
            for row in conn.execute("SELECT action, summary, outcome, created_at FROM ask_cavnar_actions "
                                    "WHERE restaurant_id=? AND outcome IN ('confirmed','dismissed') "
                                    "ORDER BY id DESC LIMIT 40", (restaurant_id,)).fetchall():
                key = f"ask:{row['action']}:{(row['summary'] or '')[:40]}"
                r = rec(key, title=row["summary"] or row["action"], kind="proposal", when=str(row["created_at"] or "")[:10])
                r["answer"] = row["outcome"]
        except Exception:
            pass
    finally:
        conn.close()
    out = sorted(recs.values(), key=lambda r: (r.get("answered_on") or r.get("asked_on") or ""), reverse=True)
    return out[:limit]


def _fmt_outcome(o):
    if not o:
        return ""
    if o.get("status") != "evaluated":
        return f" — measuring until {str(o.get('evaluate_on') or '')[:10]}"
    v = o.get("verdict") or "unknown"
    money = f", about ${abs(float(o['dollars_monthly'])):,.0f}/month" if o.get("dollars_monthly") else ""
    return f" — measured: {v}{money}"


def context(restaurant_id, db_path=DB_PATH):
    """The prompt section: short, dated, and only what was actually decided."""
    rows = history(restaurant_id, limit=MAX_CONTEXT_LINES, db_path=db_path)
    if not rows:
        return ""
    lines = ["WHAT THIS RESTAURANT HAS DECIDED BEFORE",
             "- Each line is a recommendation, issue or proposal and what the owner did with it, "
             "then what was measured afterwards. Do not re-propose something marked 'not for us' "
             "unless the owner asks; build on what worked; say when a measurement is still running."]
    for r in rows:
        when = r.get("answered_on") or r.get("asked_on") or ""
        ans = r.get("answer") or "open"
        line = f"- {r['title']}: {ans}"
        if r.get("times_hidden", 0) > 1:
            line += f" (hidden {r['times_hidden']}x)"
        if r.get("reason"):
            line += f" — because: {r['reason']}"
        line += _fmt_outcome(r.get("outcome"))
        if r.get("issue") and r["issue"].get("note"):
            line += f" — resolved: {r['issue']['note'][:80]}"
        if when:
            line += f" ({when})"
        lines.append(line)
    return "\n".join(lines) + "\n"
