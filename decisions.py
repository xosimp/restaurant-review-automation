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
import models as _models_mod
from models import DB_PATH

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


def _is_loss(r):
    key = str(r.get("key") or "")
    return r.get("kind") == "loss" or key == "loss" or key.startswith("loss:")


# Outcome metrics a login needs a permission to see: Food Cost's, and the
# comp/void rates (a loss figure).
_FOOD_METRICS = ("food_cost_pct", "weekly_waste")
_LOSS_METRICS = ("comp_rate", "void_rate")


def _redact(rows, viewer, conn, restaurant_id):
    """The decision records this login may read (re-audit B4): each is
    judged by rec_learning.viewer_sees on what the ledger knows of its key —
    the module, evidence and owner_only of every episode of it (the most
    restrictive wins) — or on the key alone when no episode exists, and a
    measured outcome on a food-cost or comp/void metric needs that
    permission. A manager's /decisions, Ask's decisions context and its
    read_decisions tool used to carry the owner's food-cost and owner-only
    DSR decisions verbatim."""
    import json as _json
    import rec_learning
    from permissions import has_permission, is_principal, FOOD_COST_VIEW
    try:
        import issues
        loss_ok = issues.viewer_sees_loss(viewer)
    except Exception:
        loss_ok = False
    keys = [r["key"] for r in rows if r.get("key")]
    known = {}
    for i in range(0, len(keys), 400):
        chunk = keys[i:i + 400]
        try:
            for row in conn.execute(f"SELECT key, module, kind, evidence_sources, owner_only FROM rec_instances "
                                    f"WHERE restaurant_id=? AND key IN ({','.join('?' for _ in chunk)})",
                                    (restaurant_id, *chunk)).fetchall():
                k = known.setdefault(row["key"], {"key": row["key"], "kind": row["kind"], "module": row["module"],
                                                  "evidence_sources": [], "owner_only": 0, "modules": set()})
                k["owner_only"] = max(int(k["owner_only"] or 0), int(row["owner_only"] or 0))
                if row["module"]:
                    k["modules"].add(row["module"])
                try:
                    k["evidence_sources"] += list(_json.loads(row["evidence_sources"] or "[]") or [])
                except (TypeError, ValueError):
                    pass
        except Exception as e:
            print(f"[decisions] redaction lookup failed closed: {e}")
            return []
    out = []
    for r in rows:
        ep = known.get(r.get("key"))
        if ep is not None:
            probe = {"key": ep["key"], "kind": ep["kind"], "owner_only": ep["owner_only"],
                     "evidence_sources": sorted(set(ep["evidence_sources"]) | ep["modules"]),
                     "module": ep["module"]}
        else:
            probe = {"key": r.get("key"), "kind": None if r.get("kind") in ("recommendation", "proposal", "preference",
                                                                            "issue") else r.get("kind")}
        if not rec_learning.viewer_sees(viewer, probe):
            continue
        # An Ask proposal answered by another login is theirs (B6's rule):
        # a teammate reads their own, a principal reads every one.
        if r.get("kind") == "proposal" and r.get("_uid") is not None and r["_uid"] != viewer.get("id") \
                and not is_principal(viewer):
            continue
        metric = str((r.get("outcome") or {}).get("metric") or "").split(":", 1)[0]
        if metric in _FOOD_METRICS and not has_permission(viewer, FOOD_COST_VIEW):
            continue
        if metric in _LOSS_METRICS and not loss_ok:
            continue
        out.append(r)
    return out


def history(restaurant_id, limit=40, db_path=DB_PATH, sees_loss=True, viewer=None):
    """Decision records, newest first. Each: {key, title, kind, asked_on,
    answer, reason, reason_code, times_hidden, outcome, issue}. `answer`
    includes "implemented" (the change was actually made — rec_ledger);
    `reason_code` is the owner's one-tap why (rec_ledger.REASON_CODES).

    `sees_loss=False` leaves out loss signals and loss issues (they name
    the approving manager), as issues.list_issues does — for anything a
    manager may read, including a narrative that renders into the manager's
    view of the daily report. `viewer` (a login dict) redacts to what that
    login may see (_redact: food cost, owner-only, loss); None is an
    internal caller or the owner's own view."""
    conn = _conn(db_path)
    recs = {}

    def rec(key, title=None, kind="recommendation", when=None):
        r = recs.get(key)
        if r is None:
            r = recs[key] = {"key": key, "title": title or key, "kind": kind, "asked_on": when,
                             "answer": None, "reason": None, "reason_code": None, "times_hidden": 0,
                             "outcome": None, "issue": None}
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
                r["answer"] = {"done": "done", "not_for_us": "not for us",
                               "snooze": "snoozed"}.get(row["kind"], "hidden")
                r["answered_on"] = str(row["dismissed_at"] or "")[:10]
        except Exception:
            pass
        # What the owner said everywhere else — the brief, Reviews, Food,
        # Marketing, the schedule, the queue, the alerts — is in rec_ledger.
        # Reading only home_dismissals, a "Not for us" given on any of them
        # never reached Ask's "do not re-propose" list. Ask's own proposals
        # are read from ask_cavnar_actions below.
        try:
            import json as _json
            import rec_ledger as _rl
            seen = set()
            for row in conn.execute(
                    "SELECT e.key, e.event, e.meta, e.at, i.title FROM rec_events e "
                    "JOIN rec_instances i ON i.rec_id=e.rec_id WHERE e.restaurant_id=? "
                    "AND e.event IN ('accepted','completed','dismissed','implemented') "
                    "ORDER BY e.at DESC, e.id DESC LIMIT 400",
                    (restaurant_id,)).fetchall():
                key = row["key"] or ""
                if key in seen or key.startswith("ask:") or not _rl.counts_in_acceptance(key):
                    continue
                try:
                    meta = _json.loads(row["meta"] or "{}") or {}
                except (TypeError, ValueError):
                    meta = {}
                seen.add(key)
                if row["event"] == "dismissed":
                    answer = "not for us" if meta.get("kind") == "not_for_us" else "hidden"
                else:
                    answer = {"completed": "done", "implemented": "implemented"}.get(row["event"], "accepted")
                day = str(row["at"] or "")[:10]
                r = rec(key, title=row["title"] or _humanize(key), when=day)
                if not r["answer"] or day > (r.get("answered_on") or ""):
                    r["answer"] = answer
                    r["answered_on"] = day
                if meta.get("reason") and not r.get("reason"):
                    r["reason"] = str(meta["reason"])[:200]
                if meta.get("reason_code") in _rl.REASON_CODES and not r.get("reason_code"):
                    # The owner's one-tap why (already doing it, too costly …).
                    r["reason_code"] = meta["reason_code"]
        except Exception:
            pass
        # What happened after they acted (or after the product saw them act).
        try:
            import outcomes as _oc
            import rec_learning as _rlearn
            for row in conn.execute("SELECT * FROM recommendation_outcomes WHERE restaurant_id=? "
                                    "ORDER BY created_at DESC LIMIT 200", (restaurant_id,)).fetchall():
                if _oc.is_informational(dict(row)) and str(row["source_key"] or "").startswith(_oc.UNTAKEN_PREFIX):
                    continue      # advice not taken: a comparison, not a decision
                full = _oc._row(row)
                r = rec(row["source_key"], title=row["title"], when=str(row["started_on"] or "")[:10])
                # The verdict as learning reads it and the grade it carries
                # (CA1 red flag 19): "improved $X" read the same for a result
                # tied to other changes and one that held.
                r["outcome"] = {"metric": row["metric"], "status": row["status"], "verdict": row["verdict"],
                                "learned_verdict": _rlearn.learned_verdict(row["verdict"], full),
                                "attribution": full.get("attribution"), "grade_phrase": full.get("grade_phrase"),
                                "counts": full.get("counts"),
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
                        # (The ledger may already have carried the same reason in.)
                        same_day = [r for r in recs.values() if r.get("answer") == "not for us"
                                    and r.get("answered_on") == day and r.get("reason") in (None, "", reason)]
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
            for row in conn.execute("SELECT action, summary, outcome, created_at, proposal_id, reason, user_id "
                                    "FROM ask_cavnar_actions "
                                    "WHERE restaurant_id=? AND outcome IN ('confirmed','dismissed') "
                                    "ORDER BY id DESC LIMIT 40", (restaurant_id,)).fetchall():
                # "ask:<proposal id>" — the key Ask, the queue and rec_ledger
                # share; an answer from an older client names no proposal.
                key = (f"ask:{row['proposal_id']}" if row["proposal_id"] else
                       f"ask:{row['action']}:{(row['summary'] or '')[:40]}")
                r = rec(key, title=row["summary"] or row["action"], kind="proposal", when=str(row["created_at"] or "")[:10])
                r["answer"] = row["outcome"]
                r["_uid"] = row["user_id"]         # who answered it (redaction only; never returned)
                if row["reason"] and not r.get("reason"):
                    r["reason"] = row["reason"]      # the owner's own "why not"
        except Exception:
            pass
        out = sorted(recs.values(), key=lambda r: (r.get("answered_on") or r.get("asked_on") or ""), reverse=True)
        if not sees_loss:
            out = [r for r in out if not _is_loss(r)]
        if viewer is not None and not (isinstance(viewer, dict) and viewer.get("is_admin")):
            out = _redact(out, viewer, conn, restaurant_id)
    finally:
        conn.close()
    for r in out:
        r.pop("_uid", None)
    return out[:limit]


def _reason_code_label(code):
    """The owner's one-tap why, as words ("too costly"), or ""."""
    if not code:
        return ""
    try:
        import rec_ledger
        return rec_ledger.reason_label(code)
    except Exception:
        return ""


def _fmt_outcome(o):
    if not o:
        return ""
    if o.get("status") != "evaluated":
        from time_utils import mdy
        return f" — measuring until {mdy(str(o.get('evaluate_on') or '')[:10])}"
    v = o.get("verdict") or "unknown"
    learned = o.get("learned_verdict", v)
    if v in ("improved", "worsened") and learned not in ("improved", "worsened"):
        # Learning does not count it (disowned, something else changed,
        # measured against its own trigger window, faded or reversed): the
        # move is said, never as the recommendation's result (CA2 #4).
        return f" — measured: {v}, but not counted as this recommendation's result"
    money = f", about ${abs(float(o['dollars_monthly'])):,.0f}/month" if o.get("dollars_monthly") else ""
    # The attribution grade (CA1 red flag 19): "held" only for a held result.
    grade = f" ({o['grade_phrase']}; before and after, not proven cause)" if o.get("grade_phrase") else ""
    return f" — measured: {v}{money}{grade}"


def context(restaurant_id, db_path=DB_PATH, sees_loss=True, viewer=None):
    """The prompt section: short, dated, and only what was actually decided.
    `sees_loss` and `viewer` as history()."""
    rows = history(restaurant_id, limit=MAX_CONTEXT_LINES, db_path=db_path, sees_loss=sees_loss, viewer=viewer)
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
        why = "; ".join(x for x in (_reason_code_label(r.get("reason_code")), r.get("reason")) if x)
        if why:
            line += f" — because: {why}"
        line += _fmt_outcome(r.get("outcome"))
        if r.get("issue") and r["issue"].get("note"):
            line += f" — resolved: {r['issue']['note'][:80]}"
        if when:
            from time_utils import mdy
            line += f" ({mdy(when)})"
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

import datetime as _dt_mod

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
        # Only settled episodes vote. Going quiet does not stop the kind
        # being shown below the top three, and that showing opens a new
        # episode — counted as 'not expired', it made the kind loud again
        # on the very next build. A superseded episode is not settled
        # either: Cavnar replaced it and the chain carries on (its expiry
        # lands on the replacement) — voting "not expired", a card re-priced
        # daily was never quiet (re-audit B11).
        if r["status"] in ("open", "superseded"):
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
            # Home's own answers hide keys too (rec_ledger.silenced_keys reads
            # home_dismissals): lifting only the ledger left Home hiding them.
            prefix = kind + ":"
            dropped = conn.execute("DELETE FROM home_dismissals WHERE restaurant_id=? AND "
                                   "(key=? OR substr(key, 1, ?)=?)",
                                   (restaurant_id, kind, len(prefix), prefix)).rowcount
            conn.commit()
        finally:
            conn.close()
        for k in keys:
            rec_ledger.unsilence(restaurant_id, k, db_path=db_path)
        if dropped:
            try:
                import home_brief
                home_brief.invalidate(restaurant_id)
            except Exception as e:
                print(f"[decisions] restore_kind home cache not cleared: {e}")
    except Exception as e:
        print(f"[decisions] restore_kind unsilence failed: {e}")
    return ok


def _local_midnight_utc(conn, restaurant_id, now=None) -> str:
    """The start of the restaurant's own today, as the UTC stamp rec_events
    stores. The UTC day began at 7pm CDT, so a Home view that evening
    counted as 'today' for the next morning's brief."""
    from datetime import timezone as _tz
    from time_utils import restaurant_tz
    try:
        row = conn.execute("SELECT timezone FROM restaurants WHERE id=?", (restaurant_id,)).fetchone()
        name = row["timezone"] if row else None
    except Exception:
        name = None
    tz = restaurant_tz(name) if name else restaurant_tz(None)
    local = (now or _dt_mod.datetime.now(_tz.utc)).astimezone(tz)
    start = local.replace(hour=0, minute=0, second=0, microsecond=0)
    return start.astimezone(_tz.utc).strftime("%Y-%m-%d %H:%M:%S")


def shown_elsewhere_today(restaurant_id, keys, surfaces, db_path=DB_PATH) -> set:
    """The keys among `keys` that a surface OTHER than `surfaces` (one name
    or several) showed today — the restaurant's own local day. A brief or a
    queue drops these unless the item is critical, so one piece of news is
    said once a day. Never raises."""
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
            f"AND at >= ? AND key IN ({marks})", (restaurant_id, _local_midnight_utc(conn, restaurant_id), *keys)).fetchall()
    except Exception:
        rows = []
    finally:
        conn.close()
    return {r["key"] for r in rows if r["surface"] not in mine}


def kind_label(kind) -> str:
    """"trim_day" reads as "Trim day" in the quieter line."""
    return _humanize(kind)
