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
            for row in conn.execute("SELECT action, summary, outcome, created_at, proposal_id, reason "
                                    "FROM ask_cavnar_actions "
                                    "WHERE restaurant_id=? AND outcome IN ('confirmed','dismissed') "
                                    "ORDER BY id DESC LIMIT 40", (restaurant_id,)).fetchall():
                # "ask:<proposal id>" — the key Ask, the queue and rec_ledger
                # share; an answer from an older client names no proposal.
                key = (f"ask:{row['proposal_id']}" if row["proposal_id"] else
                       f"ask:{row['action']}:{(row['summary'] or '')[:40]}")
                r = rec(key, title=row["summary"] or row["action"], kind="proposal", when=str(row["created_at"] or "")[:10])
                r["answer"] = row["outcome"]
                if row["reason"] and not r.get("reason"):
                    r["reason"] = row["reason"]      # the owner's own "why not"
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


# ── what the owner's silence says ──────────────────────────────────────────
#
# rec_ledger records every showing and every answer. Two questions every
# surface that is about to say something has to ask of it, answered here so
# each surface asks them the same way:
#
#   * has the owner stopped answering this KIND of recommendation? The last
#     QUIET_AFTER_EXPIRED episodes all expiring unanswered is an answer: the
#     kind drops below the top three on Home and never leads the brief,
#     until the owner asks for it back (restore_kind).
#   * has another surface already said this today? The same news on Home,
#     in the brief and in the queue is one piece of news said three times.

import models as _models_mod

QUIET_AFTER_EXPIRED = 4
_RESTORE_PREFIX = "restore_kind:"


def _conn(db_path):
    """models.get_conn resolved at call time (CLAUDE.md, bound imports)."""
    if db_path is None or db_path == DB_PATH:
        return _models_mod.get_conn()
    return _models_mod.get_conn(db_path)


def _restored_kind(key):
    """The kind a restore_kind:<kind>@<stamp> key restores, else None."""
    key = str(key or "")
    if not key.startswith(_RESTORE_PREFIX):
        return None
    return key[len(_RESTORE_PREFIX):].rsplit("@", 1)[0] or None


def quiet_kinds(restaurant_id, db_path=DB_PATH) -> set:
    """Recommendation kinds whose last QUIET_AFTER_EXPIRED episodes at this
    restaurant all expired unanswered, counting only episodes since the
    owner last restored the kind. Never raises."""
    out = set()
    try:
        conn = _conn(db_path)
    except Exception:
        return out
    try:
        rows = conn.execute(
            "SELECT kind, key, status, created_at FROM rec_instances WHERE restaurant_id=? "
            "ORDER BY created_at DESC, rowid DESC LIMIT 2000", (restaurant_id,)).fetchall()
    except Exception:
        rows = []
    finally:
        conn.close()
    restored = {}
    for r in rows:
        k = _restored_kind(r["key"])
        if k:
            restored[k] = max(restored.get(k, ""), r["created_at"] or "")
    by_kind = {}
    for r in rows:
        kind = r["kind"] or ""
        if not kind or _restored_kind(r["key"]):
            continue
        if (r["created_at"] or "") <= restored.get(kind, ""):
            continue
        by_kind.setdefault(kind, []).append(r["status"])
    for kind, statuses in by_kind.items():
        last = statuses[:QUIET_AFTER_EXPIRED]
        if len(last) == QUIET_AFTER_EXPIRED and all(s == "expired" for s in last):
            out.add(kind)
    return out


def restore_kind(restaurant_id, kind, user_id=None, surface="home", db_path=DB_PATH) -> bool:
    """The owner asked to see a quiet kind again. Recorded in the ledger as
    an accepted `restore_kind:<kind>@<stamp>` episode — a new key each time,
    so each restore starts its own count — and any answer still silencing a
    key of that kind is lifted too."""
    import rec_ledger
    from datetime import datetime as _dt
    kind = str(kind or "").strip()[:60]
    if not restaurant_id or not kind:
        return False
    key = f"{_RESTORE_PREFIX}{kind}@{_dt.utcnow().strftime('%Y%m%d%H%M%S%f')}"
    ok = rec_ledger.record(restaurant_id, key, "accepted", surface=surface, user_id=user_id,
                           meta={"module": "home"}, db_path=db_path)
    try:
        conn = _conn(db_path)
        try:
            keys = [r["key"] for r in conn.execute(
                "SELECT DISTINCT key FROM rec_instances WHERE restaurant_id=? AND kind=?", (restaurant_id, kind))]
        finally:
            conn.close()
        for k in keys:
            rec_ledger.unsilence(restaurant_id, k, db_path=db_path)
    except Exception as e:
        print(f"[decisions] restore_kind unsilence failed: {e}")
    return ok


def shown_elsewhere_today(restaurant_id, keys, surfaces, db_path=DB_PATH) -> set:
    """The keys among `keys` that a surface OTHER than `surfaces` (one name
    or several) showed today — the ledger's own UTC day. A brief or a queue
    drops these unless the item is critical, so one piece of news is said
    once a day. Never raises."""
    keys = [str(k)[:160] for k in (keys or []) if k]
    if not restaurant_id or not keys:
        return set()
    mine = {surfaces} if isinstance(surfaces, str) else set(surfaces or ())
    try:
        conn = _conn(db_path)
    except Exception:
        return set()
    try:
        marks = ",".join("?" for _ in keys)
        rows = conn.execute(
            f"SELECT DISTINCT key, surface FROM rec_events WHERE restaurant_id=? AND event='shown' "
            f"AND at >= date('now') AND key IN ({marks})", (restaurant_id, *keys)).fetchall()
    except Exception:
        rows = []
    finally:
        conn.close()
    return {r["key"] for r in rows if r["surface"] not in mine}


def kind_label(kind) -> str:
    """"trim_day" reads as "Trim day" in the quieter line."""
    return _humanize(kind)
