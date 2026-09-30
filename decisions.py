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
import re

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
            me = (viewer or {}).get("id") if isinstance(viewer, dict) else None
            # The newest answer per key, chosen in SQL with whose-answer
            # rules in the WHERE (memory re-audit 9/29/26, INVENTORY-13): a
            # LIMIT over every answer event, filtered afterwards, dropped a
            # reasoned "not for us" older than the newest 400 answers out of
            # every prompt although rec_events are kept 800 days. Whose
            # decision (memory audit 9/29/26): support's answer through
            # view-as is never the restaurant's; a manager's decline (and
            # their taking it back) held for that manager alone, so it is
            # theirs to read back, not the owner's "do not re-propose". A
            # `reopened` (QUALITY-1: "Use again", a restored quiet kind) is
            # the owner taking the answer back: when it is the newest, the
            # key reads "asked to see it again", never "not for us".
            bookkeeping = " ".join(f"AND e.key NOT LIKE '{p}%'" for p in _rl.BOOKKEEPING_PREFIXES)
            for row in conn.execute(
                    "SELECT key, event, meta, at, user_id, authority, title, model_written, signature FROM ("
                    "SELECT e.key, e.event, e.meta, e.at, e.user_id, e.authority, i.title, i.model_written, "
                    "i.signature, ROW_NUMBER() OVER (PARTITION BY e.key ORDER BY e.at DESC, e.id DESC) AS rn "
                    "FROM rec_events e JOIN rec_instances i ON i.rec_id=e.rec_id WHERE e.restaurant_id=? "
                    "AND e.event IN ('accepted','completed','dismissed','implemented','reopened') "
                    "AND COALESCE(e.authority, '') != 'admin' AND e.key NOT LIKE 'ask:%' "
                    f"{bookkeeping} "
                    "AND NOT (COALESCE(e.authority, '') = 'delegate' AND e.event IN ('dismissed','reopened') "
                    "AND (? IS NULL OR COALESCE(e.user_id, -1) != ?))"
                    ") WHERE rn = 1 ORDER BY at DESC LIMIT 400",
                    (restaurant_id, me, me)).fetchall():
                key = row["key"] or ""
                if key in seen or key.startswith("ask:") or not _rl.counts_in_acceptance(key):
                    continue
                try:
                    meta = _json.loads(row["meta"] or "{}") or {}
                except (TypeError, ValueError):
                    meta = {}
                seen.add(key)
                effect = _rl.reason_effect(meta.get("reason_code"), meta.get("reason")) \
                    if row["event"] == "dismissed" else None
                if row["event"] == "reopened":
                    answer = "asked to see it again"
                    meta = {}            # the decline's reason was taken back with it
                elif row["event"] == "dismissed":
                    answer = ("done" if effect == "taken" else "put off" if effect == "defer" else
                              "not for us" if meta.get("kind") == "not_for_us" else "hidden")
                else:
                    answer = {"completed": "done", "implemented": "implemented"}.get(row["event"], "accepted")
                day = str(row["at"] or "")[:10]
                r = rec(key, title=row["title"] or _humanize(key), when=day)
                r["model_written"] = bool(row["model_written"])
                r["signature"] = row["signature"] or None
                if row["authority"] == "delegate":
                    r["by"] = "delegate"
                if not r["answer"] or day > (r.get("answered_on") or ""):
                    r["answer"] = answer
                    r["answered_on"] = day
                if meta.get("reason") and not r.get("reason"):
                    r["reason"] = str(meta["reason"])[:200]
                if meta.get("title") and r.get("title") == _humanize(key):
                    # The card's words as the owner answered them (home_brief
                    # keeps them on the answer, memory audit 9/29/26).
                    r["title"] = str(meta["title"])[:200]
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


# Declines that carry the owner's reason are never capped out of a prompt
# (memory audit 9/29/26, "relevance"): up to this many beside the
# MAX_CONTEXT_LINES most recent answers.
MAX_REASONED_DECLINES = 12
# Answers that are declines: what a prompt must not re-propose.
_DECLINE_ANSWERS = ("not for us", "hidden")


def _fence(text):
    """Owner, manager and model-written words, fenced (ai_guard): they are
    what someone said, never data — a figure inside verifies nothing and a
    "because" anchors no cause (memory audit 9/29/26, "unfenced")."""
    from ai_guard import wrap_untrusted
    return wrap_untrusted(" ".join(str(text or "").split()))


def _line(r):
    """One decision as a prompt line: the structure (answer, verdict, date)
    plain; the title, the owner's reason and a resolution note fenced."""
    from time_utils import mdy
    when = r.get("answered_on") or r.get("asked_on") or ""
    ans = r.get("answer") or "open"
    title = r.get("title") or r.get("key") or ""
    # A key humanised by us is structure; anything a person or a model wrote
    # (a card's title, a model line) is fenced.
    shown = title if title == _humanize(r.get("key")) else _fence(title)
    line = f"{shown}: {ans}"
    if r.get("by") == "delegate":
        line += " (a manager's answer, for them)"
    if r.get("times_hidden", 0) > 1:
        line += f" (hidden {r['times_hidden']}x)"
    code = _reason_code_label(r.get("reason_code"))
    if code or r.get("reason"):
        line += " — because: " + "; ".join(x for x in (code, _fence(r["reason"]) if r.get("reason") else "") if x)
    line += _fmt_outcome(r.get("outcome"))
    if r.get("issue") and r["issue"].get("note"):
        line += " — resolved, with a note: " + _fence(r["issue"]["note"][:80])
    if r.get("collapsed"):
        line += f" (said the same way to {r['collapsed']} other wording{'s' if r['collapsed'] != 1 else ''})"
    if when:
        line += f" ({mdy(when)})"
    return line


def _collapse(rows):
    """One line per piece of advice: rows sharing an advice signature fold
    into the most recent, which counts the others (memory audit, relevance:
    15 answered food lines were 15 prompt lines, three of them cut)."""
    out, by_sig = [], {}
    for r in rows:
        sig = r.get("signature")
        if sig and sig in by_sig:
            by_sig[sig]["collapsed"] = by_sig[sig].get("collapsed", 0) + 1
            if not by_sig[sig].get("reason") and r.get("reason"):
                by_sig[sig]["reason"] = r["reason"]
            continue
        r = dict(r)
        if sig:
            by_sig[sig] = r
        out.append(r)
    return out


def _with_signatures(rows):
    for r in rows:
        if "signature" not in r or r.get("signature") is None:
            try:
                import insight_store
                r["signature"] = insight_store.advice_signature(r.get("key"), r.get("title"))
            except Exception:
                r["signature"] = None
    return rows


def pick_relevant(rows, subjects=(), modules=(), limit=MAX_CONTEXT_LINES):
    """The decisions a prompt should carry, most relevant first (memory
    audit 9/29/26, "relevance"): a decline with a reason about a subject or
    module in play, then any decline about one, then any other answer about
    one, then every decline with a reason (never capped out), then the most
    recent — collapsed by advice signature. Returns rows with `weight`."""
    subjects = {str(s).lower() for s in subjects or () if s}
    modules = {str(m).lower() for m in modules or () if m}

    def about(r):
        sig = str(r.get("signature") or "").lower()
        if sig and (sig in subjects or any(sig.startswith(s + ":") or s.startswith(sig) for s in subjects)):
            return True
        fam = sig.split(":", 1)[0] if sig else ""
        key_kind = str(r.get("key") or "").split(":", 1)[0].lower()
        return bool(modules and (fam in modules or key_kind in modules or _module_of(r) in modules))
    rows = _collapse(_with_signatures(list(rows or [])))
    scored = []
    for i, r in enumerate(rows):
        declined = r.get("answer") in _DECLINE_ANSWERS
        reasoned = bool(r.get("reason") or r.get("reason_code"))
        rel = about(r)
        w = (100 if (rel and declined and reasoned) else 80 if (rel and declined) else 60 if rel
             else 40 if (declined and reasoned) else 0) + max(0.0, 30.0 - i * 0.5)
        scored.append(dict(r, weight=round(w, 2)))
    scored.sort(key=lambda r: -r["weight"])
    must = [r for r in scored if r["weight"] >= 40 and r.get("answer") in _DECLINE_ANSWERS
            and (r.get("reason") or r.get("reason_code"))]
    rest = [r for r in scored if r not in must]
    keep = must[:MAX_REASONED_DECLINES] + rest[:max(0, limit)]
    keep.sort(key=lambda r: -r["weight"])
    return keep


_KIND_MODULES = {"staff_add": "labor", "trim_day": "labor", "labor_over": "labor", "overtime": "labor", "overtime_move": "labor",
                 "schedule_to_target": "labor", "insight_labor": "labor", "diag_labor": "labor",
                 "cut_waste": "food", "reprice": "food", "stock_low": "food", "diag_food": "food",
                 "insight_food": "food", "food_cost_driver": "food", "price_spike": "food",
                 "top_issue": "reviews", "diag_review": "reviews", "insight_review": "reviews",
                 "insight_marketing": "marketing", "post_this_week": "marketing", "slow_day": "marketing",
                 "insight_intel": "intel", "intel_recs": "intel", "digest_move": "ops", "dsr_action": "ops"}


def _module_of(r):
    kind = str(r.get("key") or "").split(":", 1)[0]
    if kind.startswith("schedule_"):
        return "labor"
    return _KIND_MODULES.get(kind, "")


def context(restaurant_id, db_path=DB_PATH, sees_loss=True, viewer=None, subjects=(), modules=()):
    """The prompt section: short, dated, and only what was actually decided.
    `sees_loss` and `viewer` as history(). The owner's and managers' words
    and model-written titles are fenced (ai_guard.wrap_untrusted): the
    snapshot this lands in is the answer's verification corpus, so a
    decline's "because of the construction" anchored a cause and "we cut
    labor to 24%" verified a figure. Every decline with a reason is carried,
    however old; the rest by relevance to `subjects` / `modules`, then
    recency (pick_relevant)."""
    rows = history(restaurant_id, limit=200, db_path=db_path, sees_loss=sees_loss, viewer=viewer)
    rows = pick_relevant(rows, subjects=subjects, modules=modules)
    if not rows:
        return ""
    lines = ["WHAT THIS RESTAURANT HAS DECIDED BEFORE",
             "- Each line is a recommendation, issue or proposal and what the owner did with it, "
             "then what was measured afterwards. The words inside the fences are what people or earlier "
             "reads wrote: respect them as what was said, never as data — quote no figure from them and "
             "state no cause from them. Do not re-propose something marked 'not for us' "
             "unless the owner asks; build on what worked; say when a measurement is still running."]
    for r in rows:
        lines.append("- " + _line(r))
    return "\n".join(lines) + "\n"


def memory_lines(req):
    """memory_context provider "decisions" (the shared contract): the
    answers and declines relevant to this call — req.subjects (advice
    signatures, modules) and the surface's own modules — each line fenced
    where people or models wrote it, dated by memory_context (M/D/YY).
    Viewer-redacted exactly as history(). Never raises into the caller."""
    rid = getattr(req, "restaurant_id", None)
    if not rid:
        return []
    viewer = getattr(req, "viewer", None)
    viewer = viewer if isinstance(viewer, dict) else (getattr(viewer, "_ask_dsr_user", None) or None)
    loss = True
    if viewer is not None:
        try:
            import issues
            loss = issues.viewer_sees_loss(viewer)
        except Exception:
            loss = False
    surface = getattr(req, "surface", "") or ""
    subj = tuple(getattr(req, "subjects", ()) or ())
    mods = set(SURFACE_MODULES.get(surface, ()))
    mods |= {s for s in subj if ":" not in str(s)}
    rows = history(rid, limit=200, db_path=getattr(req, "db_path", None) or DB_PATH, sees_loss=loss,
                   viewer=viewer)
    out = []
    for r in pick_relevant(rows, subjects=[s for s in subj if ":" in str(s)], modules=mods,
                           limit=MAX_CONTEXT_LINES):
        text = _line(dict(r, answered_on=None, asked_on=None))
        out.append({"text": text, "date": r.get("answered_on") or r.get("asked_on"),
                    "source": "manager" if r.get("by") == "delegate" else "owner",
                    "subject": r.get("signature") or _module_of(r) or None,
                    "weight": r.get("weight", 0), "trusted": True})
    return out


# A kind is named as declined only on this much evidence (memory audit
# 9/29/26, "one_hide"): one "hide for now" on a reprice card a year ago put
# the whole reprice kind on Ask's do-not-propose list for good.
DECLINE_MIN_ANSWERS = 3
DECLINE_WINDOW_DAYS = 180
DECLINE_HALF_LIFE_DAYS = 90
# ...and three "not for us" answers all near the edge of the window weigh
# less than this, recency-weighted: they are not a standing no.
DECLINE_MIN_WEIGHT = 1.5


def declined_subjects(restaurant_id, db_path=DB_PATH, now=None) -> list:
    """The advice this owner has plainly said "not for us" to, named by
    subject, never by whole kind (memory audit 9/29/26, "one_hide"): only
    "not for us" answers count — a plain hide, a timing answer, "already
    doing it" and "don't trust the data" are not a no — at least
    DECLINE_MIN_ANSWERS of a kind within DECLINE_WINDOW_DAYS whose
    recency-weighted sum (half-life DECLINE_HALF_LIFE_DAYS) reaches
    DECLINE_MIN_WEIGHT. Principal answers only (a delegate's decline is
    theirs; support's through view-as is nobody's). Returns [{kind, label,
    subjects, n, weight, since (M/D/YY)}], strongest first. Never raises."""
    import json as _json
    from datetime import datetime as _dt
    now = now or _dt.utcnow()
    since = (now - _dt_mod.timedelta(days=DECLINE_WINDOW_DAYS)).strftime("%Y-%m-%d %H:%M:%S")
    try:
        conn = _conn(db_path)
    except Exception:
        return []
    try:
        rows = conn.execute(
            "SELECT e.id, e.key, e.meta, e.at, e.authority, i.kind, i.title, i.signature FROM rec_events e "
            "JOIN rec_instances i ON i.rec_id=e.rec_id WHERE e.restaurant_id=? AND e.event='dismissed' "
            "AND e.at >= ?", (restaurant_id, since)).fetchall()
        # A "not for us" the owner took back afterwards ("Use again", a
        # restored kind — rec_ledger.unsilence's `reopened`, memory re-audit
        # 9/29/26 QUALITY-1) is no longer a no: Ask kept being told "do not
        # re-propose" the advice the owner had just asked for back.
        reopened = {}
        for r in conn.execute("SELECT key, at, id FROM rec_events WHERE restaurant_id=? AND event='reopened' "
                              "AND COALESCE(authority, '') NOT IN ('delegate', 'admin')", (restaurant_id,)).fetchall():
            reopened[r["key"]] = max(reopened.get(r["key"], ("", 0)), (str(r["at"] or ""), int(r["id"])))
    except Exception as e:
        print(f"[decisions] declined subjects unreadable: {e}")
        return []
    finally:
        conn.close()
    import rec_ledger as _rl
    by_kind = {}
    for r in rows:
        if r["authority"] in ("delegate", "admin"):
            continue
        if reopened.get(r["key"], ("", 0)) > (str(r["at"] or ""), int(r["id"])):
            continue
        try:
            meta = _json.loads(r["meta"] or "{}") or {}
        except (TypeError, ValueError):
            meta = {}
        if meta.get("kind") != "not_for_us" or \
                _rl.reason_effect(meta.get("reason_code"), meta.get("reason")) not in (None, "decline"):
            continue
        kind = r["kind"] or _rl.kind_of(r["key"])
        if not _rl.counts_in_acceptance(r["key"]) or kind == "ask":
            continue
        try:
            age = max(0.0, (now - _dt.strptime(str(r["at"])[:19], "%Y-%m-%d %H:%M:%S")).total_seconds() / 86400.0)
        except ValueError:
            age = 0.0
        b = by_kind.setdefault(kind, {"n": 0, "weight": 0.0, "subjects": [], "first": r["at"]})
        b["n"] += 1
        b["weight"] += 0.5 ** (age / DECLINE_HALF_LIFE_DAYS)
        b["first"] = min(b["first"], r["at"])
        subject = r["key"].split(":", 1)[1] if ":" in r["key"] else ""
        if r["signature"]:
            import insight_store as _ist_ds
            base, direction = _ist_ds.split_signature(r["signature"])
            label = base.split(":", 1)[1].split(":", 1)[-1] + (f" ({direction})" if direction else "")
        elif subject and not re.fullmatch(r"[0-9a-f]{10}", subject):
            label = subject
        else:
            label = (r["title"] or "")[:60]
        label = label.replace("_", " ").strip()
        if label and label.lower() not in [x.lower() for x in b["subjects"]]:
            b["subjects"].append(label[:60])
    out = []
    from time_utils import mdy
    for kind, b in by_kind.items():
        if b["n"] < DECLINE_MIN_ANSWERS or b["weight"] < DECLINE_MIN_WEIGHT:
            continue
        out.append({"kind": kind, "label": kind_label(kind), "subjects": b["subjects"][:6], "n": b["n"],
                    "weight": round(b["weight"], 2), "since": mdy(str(b["first"])[:10])})
    out.sort(key=lambda x: (-x["weight"], x["kind"]))
    return out


def annotate_declined(restaurant_id, text, db_path=DB_PATH):
    """(text, repeats): an answer's imperative lines (ask_cavnar.
    extract_suggestions — the lines it already finds for the chips) checked
    against the advice the owner said "not for us" to on any surface
    (insight_store.declines_by_signature). A repeat is kept — the model may
    have a reason — but caveated in place, "(you passed on this on
    8/12/26)", so a declined Friday-closer cut is never offered as if new
    (memory audit 9/29/26, "relevance": only Ask's chips were filtered, the
    prose was never checked). `repeats` [{text, signature, declined_on}]
    for the answer's meta. Never raises: on failure the text is unchanged."""
    try:
        import insight_store
        from ask_cavnar import extract_suggestions, _LIST_ITEM
        from time_utils import mdy
        items = extract_suggestions(text, limit=None)
        if not items:
            return text, []
        declines = insight_store.declines_by_signature(restaurant_id, db_path=db_path)
        if not declines:
            return text, []
        hits = {}
        subjects = insight_store.known_subjects(restaurant_id, db_path=db_path)
        for it in items:
            sig = insight_store.advice_signature("ask_tip:x", it["text"], subjects=subjects)
            if sig and sig in declines:
                hits[it["text"]] = (sig, mdy(str(declines[sig]["on"])[:10]))
        if not hits:
            return text, []
        out_lines, repeats = [], []
        for line in str(text).split("\n"):
            m = _LIST_ITEM.match(line)
            if m:
                body = " ".join(m.group(1).replace("**", "").split())
                hit = hits.get(body)
                if hit and "you passed on this" not in line:
                    line = line.rstrip() + f" (you passed on this on {hit[1]})"
                    repeats.append({"text": body[:200], "signature": hit[0], "declined_on": hit[1]})
            out_lines.append(line)
        return "\n".join(out_lines), repeats
    except Exception as e:
        print(f"[decisions] declined check skipped for {restaurant_id}: {e}")
        return text, []


# What each memory_context surface is about, for relevance.
SURFACE_MODULES = {
    "labor_read": ("labor",), "schedule": ("labor",), "food_read": ("food",), "food_diagnosis": ("food",),
    "review_diagnosis": ("reviews",), "competitor_read": ("intel", "marketing"),
    "marketing": ("marketing",), "reply_drafter": ("reviews",),
    "dsr_narrative": ("labor", "food", "reviews", "ops"), "brief": (), "digest": (), "weekly_plan": (), "ask": (),
}


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
    """Recommendation kinds this owner has gone quiet on and that are not
    due a re-test (quiet_state): every surface that ranks drops them below
    the top three. Never raises."""
    try:
        return {k for k, st in quiet_state(restaurant_id, db_path=db_path).items() if not st.get("retest")}
    except Exception as e:
        print(f"[decisions] quiet kinds unavailable for {restaurant_id}: {e}")
        return quiet_kinds_vote(restaurant_id, db_path=db_path)


def quiet_kinds_vote(restaurant_id, db_path=DB_PATH) -> set:
    """Recommendation kinds whose last QUIET_AFTER_EXPIRED episodes at this
    restaurant all expired unanswered, counting only episodes since the
    owner last restored the kind (the ledger's restore marker, or the
    durable state's restored_at). Never raises."""
    out = set()
    try:
        conn = _conn(db_path)
    except Exception:
        return out
    try:
        import rec_ledger as _rl_seen
        # An expiry votes only when someone could have seen the card
        # (memory re-audit 9/29/26, LOOPS-10): a kind carried only by
        # weekly emails nobody opened went quiet on Home, where it was
        # never shown. The seen check runs only on expired rows.
        rows = conn.execute(
            "SELECT i.kind, i.key, i.status, i.created_at, CASE WHEN i.status = 'expired' THEN "
            f"{_rl_seen.seen_sql('i')} ELSE 1 END AS seen FROM rec_instances i WHERE i.restaurant_id=? "
            "ORDER BY i.created_at DESC, i.rowid DESC LIMIT 2000", (restaurant_id,)).fetchall()
    except Exception:
        rows = []
    finally:
        conn.close()
    restored = {}
    for r in rows:
        k = _restored_kind(r["key"])
        if k:
            restored[k] = max(restored.get(k, ""), r["created_at"] or "")
    # The restore held durably too: the ledger rows above are read over a
    # 2,000-episode window, which a busy restaurant fills in weeks.
    try:
        c2 = _conn(db_path)
        try:
            for st in _kind_state_rows(c2, restaurant_id, "restore").values():
                if st.get("restored_at"):
                    restored[st["kind"]] = max(restored.get(st["kind"], ""), st["restored_at"])
        finally:
            c2.close()
    except Exception:
        pass
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
        if r["status"] == "expired" and not r["seen"]:
            continue                 # delivered, never seen: no vote either way
        by_kind.setdefault(kind, []).append(r["status"])
    for kind, statuses in by_kind.items():
        last = statuses[:QUIET_AFTER_EXPIRED]
        if len(last) == QUIET_AFTER_EXPIRED and all(s == "expired" for s in last):
            out.add(kind)
    return out


# A kind gone quiet is re-tested once this long after it went quiet, and
# again after each re-test that also went unanswered (memory audit 9/29/26,
# "quiet_kinds": a kind ignored during one busy month stayed quiet for good).
RETEST_AFTER_DAYS = 60
_FAMILY = "home"


def _kind_state_rows(conn, restaurant_id, family):
    try:
        return {r["kind"]: dict(r) for r in conn.execute(
            "SELECT * FROM rec_kind_states WHERE restaurant_id=? AND family=?", (restaurant_id, family)).fetchall()}
    except Exception:
        return {}


def quiet_state(restaurant_id, db_path=DB_PATH, now=None, write=True) -> dict:
    """{kind: {since, review_on, retest, retests, last_dollars}} for every
    kind this owner has gone quiet on (quiet_kinds' vote), held as durable
    state with a review date (rec_kind_states, family "home"): from
    RETEST_AFTER_DAYS after it went quiet the kind is shown again, once,
    labelled a re-test (`retest` True) — answered, it is loud again;
    ignored too, it is quiet again until the next review date. A kind the
    vote no longer calls quiet leaves the state. `write=False` (a build that
    records nothing) judges without writing. Never raises."""
    from datetime import datetime as _dt, timedelta as _td
    now = now or _dt.utcnow()
    now_s = now.strftime("%Y-%m-%d %H:%M:%S")
    voted = quiet_kinds_vote(restaurant_id, db_path=db_path)
    out = {}
    try:
        conn = _conn(db_path)
    except Exception:
        return {k: {"since": None, "review_on": None, "retest": False, "retests": 0, "last_dollars": None}
                for k in voted}
    try:
        states = _kind_state_rows(conn, restaurant_id, _FAMILY)
        for kind in voted:
            st = states.get(kind)
            last = conn.execute("SELECT dollar_value FROM rec_instances WHERE restaurant_id=? AND kind=? "
                                "AND dollar_value IS NOT NULL ORDER BY created_at DESC LIMIT 1",
                                (restaurant_id, kind)).fetchone()
            last_dollars = float(last["dollar_value"]) if last else None
            if st is None:
                review = (now + _td(days=RETEST_AFTER_DAYS)).strftime("%Y-%m-%d %H:%M:%S")
                st = {"since": now_s, "review_on": review, "retests": 0, "last_dollars": last_dollars}
                if write:
                    conn.execute("INSERT OR REPLACE INTO rec_kind_states (restaurant_id, family, kind, state, reason, "
                                 "since, review_on, retests, last_dollars, updated_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                                 (restaurant_id, _FAMILY, kind, "quiet",
                                  f"the last {QUIET_AFTER_EXPIRED} went unanswered", now_s, review, 0, last_dollars,
                                  now_s))
            retest = False
            if st.get("review_on") and st["review_on"] <= now_s:
                # Due its re-test: loud until a showing since the review date
                # settles. Ignored again, it is quiet to the next review.
                settled = conn.execute(
                    "SELECT status FROM rec_instances WHERE restaurant_id=? AND kind=? AND created_at >= ? "
                    "AND status NOT IN ('open','superseded') ORDER BY created_at DESC LIMIT 1",
                    (restaurant_id, kind, st["review_on"])).fetchone()
                if settled is not None and settled["status"] == "expired":
                    review = (now + _td(days=RETEST_AFTER_DAYS)).strftime("%Y-%m-%d %H:%M:%S")
                    st = dict(st, review_on=review, retests=int(st.get("retests") or 0) + 1)
                    if write:
                        conn.execute("UPDATE rec_kind_states SET review_on=?, retests=?, updated_at=? "
                                     "WHERE restaurant_id=? AND family=? AND kind=?",
                                     (review, st["retests"], now_s, restaurant_id, _FAMILY, kind))
                else:
                    retest = True
            out[kind] = {"since": st.get("since"), "review_on": st.get("review_on"), "retest": retest,
                         "retests": int(st.get("retests") or 0),
                         "last_dollars": st.get("last_dollars") if st.get("last_dollars") is not None else last_dollars}
        for kind, st in states.items():
            if kind in voted:
                continue
            # The vote reads the newest 2,000 episodes; a kind that fell out
            # of that window has not been answered — it stays quiet until an
            # answer to one of its episodes since it went quiet says so.
            answered = conn.execute(
                "SELECT 1 FROM rec_instances WHERE restaurant_id=? AND kind=? AND created_at >= ? "
                "AND status IN ('accepted','completed','dismissed','implemented') LIMIT 1",
                (restaurant_id, kind, st.get("since") or "")).fetchone()
            if answered:
                if write:
                    conn.execute("DELETE FROM rec_kind_states WHERE restaurant_id=? AND family=? AND kind=?",
                                 (restaurant_id, _FAMILY, kind))
                continue
            retest = bool(st.get("review_on") and st["review_on"] <= now_s)
            out[kind] = {"since": st.get("since"), "review_on": st.get("review_on"), "retest": retest,
                         "retests": int(st.get("retests") or 0), "last_dollars": st.get("last_dollars")}
        if write:
            conn.commit()
    except Exception as e:
        print(f"[decisions] quiet state unavailable for {restaurant_id}: {e}")
    finally:
        conn.close()
    return out


def restore_kind(restaurant_id, kind, user_id=None, surface="home", db_path=DB_PATH, authority=None) -> bool:
    """The owner asked to see a quiet kind again. Recorded in the ledger as
    an accepted `restore_kind:<kind>@<stamp>` episode — a new key each time,
    so each restore starts its own count — and any answer still silencing a
    key of that kind is lifted too, each reversal kept in the trail as a
    `reopened` with `authority` (permissions.answer_authority of the login;
    None reads as the owner's) — rec_ledger.unsilence, QUALITY-1."""
    import rec_ledger
    from datetime import datetime as _dt
    kind = str(kind or "").strip()[:60]
    if not restaurant_id or not kind:
        return False
    key = f"{_RESTORE_PREFIX}{kind}@{_dt.utcnow().strftime('%Y%m%d%H%M%S%f')}"
    ok = rec_ledger.record(restaurant_id, key, "accepted", surface=surface, user_id=user_id,
                           meta={"module": "home"}, db_path=db_path, authority=authority)
    # Held durably (memory audit, quiet_kinds): the quiet state goes and the
    # restore is remembered beyond the vote's 2,000-episode window.
    try:
        conn = _conn(db_path)
        try:
            now_s = _dt.utcnow().strftime("%Y-%m-%d %H:%M:%S")
            conn.execute("DELETE FROM rec_kind_states WHERE restaurant_id=? AND family=? AND kind=?",
                         (restaurant_id, _FAMILY, kind))
            conn.execute("INSERT OR REPLACE INTO rec_kind_states (restaurant_id, family, kind, state, since, "
                         "restored_at, updated_at) VALUES (?,?,?,?,?,?,?)",
                         (restaurant_id, "restore", kind, "restored", now_s, now_s, now_s))
            conn.commit()
        finally:
            conn.close()
    except Exception as e:
        print(f"[decisions] restore not held durably: {e}")
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
            rec_ledger.unsilence(restaurant_id, k, db_path=db_path, user_id=user_id,
                                 authority=authority or "principal", surface=surface)
        # Any remembered "Not doing X" about a key of the restored kind is
        # retracted with its silence (memory audit 9/29/26, owner_lanes).
        try:
            import owner_memory
            owner_memory.retract_for_keys(restaurant_id, keys, archived_by=user_id,
                                          db_path=None if db_path == DB_PATH else db_path)
        except Exception as e:
            print(f"[decisions] restore_kind memory not retracted: {e}")
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
